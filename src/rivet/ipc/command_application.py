"""把 TUI 命令路由到同一正式 CLI，并在 Worker 内仲裁权限。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from pydantic import JsonValue

from rivet.cli.config import load_config
from rivet.cli.errors import CliConfigurationError
from rivet.contracts.ipc import IpcRequest
from rivet.ipc.worker import (
    BaseWorkerApplication,
    EmitEvent,
    WorkerMethodError,
)

MAX_COMMAND_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_FILE_LIST_RESULTS = 2_000
MAX_SELECTED_CONTEXT_FILES = 20
MAX_TUI_QUERY_CHARS = 65_536
OFFICIAL_TUI_COMMANDS = frozenset(
    {
        "abort",
        "apply",
        "ask",
        "benchmark",
        "clean",
        "config",
        "diff",
        "doctor",
        "fix",
        "init",
        "modules",
        "plan",
        "read",
        "resume",
        "trace",
        "verify",
    }
)
CommandRunner = Callable[[tuple[str, ...], EmitEvent], Awaitable["CommandExecution"]]


@dataclass(frozen=True, slots=True)
class CommandExecution:
    """保存子 CLI 的有界退出事实。"""

    return_code: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool = False
    stderr_truncated: bool = False


@dataclass(frozen=True, slots=True)
class _ProjectedTraceEvent:
    """保存投影前的事件类型、载荷、Run 与 Worker 流关联。"""

    event_type: str
    payload: dict[str, JsonValue]
    run_id: str
    stream_id: str | None


class CommandWorkerApplication(BaseWorkerApplication):
    """串行运行 Agent 命令，同时允许权限响应并发到达。"""

    def __init__(
        self,
        repository: Path,
        *,
        environment: Mapping[str, str] | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        super().__init__(repository)
        self._repository = repository.resolve(strict=True)
        self._environment = os.environ if environment is None else environment
        self._runner = runner or self._run_subprocess
        self._command_lock = asyncio.Lock()
        self._permissions: dict[str, asyncio.Future[bool]] = {}

    def ready_payload(self) -> dict[str, JsonValue]:
        """返回首屏需要的真实模型状态，凭据只提供布尔值。"""
        payload = super().ready_payload()
        try:
            config = load_config(
                self._repository,
                environment=self._environment,
            )
        except CliConfigurationError:
            payload.update(
                {
                    "credential_configured": False,
                    "model": "配置错误",
                }
            )
            return payload
        payload.update(
            {
                "credential_configured": config.credential_configured,
                "model": config.model,
            }
        )
        return payload

    async def handle(
        self,
        request: IpcRequest,
        *,
        emit: EmitEvent,
        cancel_event: asyncio.Event,
    ) -> JsonValue:
        """处理基础方法、文件清单、权限决议和正式 CLI 命令。"""
        if request.method in {"worker.ping", "worker.snapshot"}:
            return await super().handle(
                request,
                emit=emit,
                cancel_event=cancel_event,
            )
        if request.method == "permission.resolve":
            return await self._resolve_permission(request, emit=emit)
        if request.method == "workspace.files":
            return await self._list_repository_files(request, emit=emit)
        if request.method == "sessions.list":
            return await self._list_sessions(request, emit=emit)
        command = request.method.removeprefix("command.")
        if (
            not request.method.startswith("command.")
            or command not in OFFICIAL_TUI_COMMANDS
        ):
            raise WorkerMethodError(
                "ipc.method_unknown",
                "Worker 方法未注册",
                "检查客户端版本或使用已公布的方法",
            )
        async with self._command_lock:
            await emit(
                "plan.updated",
                {"phase": command.upper(), "summary": "命令已提交"},
            )
            context_paths = (
                self._selected_context_paths(request)
                if command in {"ask", "plan", "fix"}
                else ()
            )
            for path in context_paths:
                await emit(
                    "context.selected",
                    {"path": path, "reason": "用户显式选择"},
                )
            arguments = self._arguments(
                request,
                command,
                context_paths=context_paths,
            )
            if command == "fix":
                await self._request_fix_permission(
                    emit=emit,
                    cancel_event=cancel_event,
                )
                arguments = (*arguments, "--yes")
            execution = await self._runner(arguments, emit)
            payload = self._decode_execution(execution)
            accepted_return_codes = {0, 4} if command in {"fix", "verify"} else {0}
            if execution.return_code not in accepted_return_codes:
                self._raise_execution_error(execution)
            await self._emit_payload(command, payload, emit=emit)
            await emit(
                "command.completed",
                {"command": command, "summary": f"/{command} 已完成"},
            )
            await emit(
                "plan.updated",
                {"phase": "IDLE", "summary": f"{command} 已结束"},
            )
            return cast(JsonValue, payload)

    async def _list_repository_files(
        self,
        request: IpcRequest,
        *,
        emit: EmitEvent,
    ) -> JsonValue:
        """按需返回 Git 可见文件名，不读取文件正文或激活 Context。"""
        query = request.params.get("query", "")
        limit = request.params.get("limit", 200)
        if not isinstance(query, str) or len(query) > 512:
            raise WorkerMethodError(
                "workspace.query_invalid",
                "文件搜索文本无效",
                "使用不超过 512 字符的文件名或路径",
            )
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 500
        ):
            raise WorkerMethodError(
                "workspace.limit_invalid",
                "文件搜索数量上限无效",
                "使用 1 到 500 之间的结果上限",
            )
        environment = {
            "LANG": self._environment.get("LANG", "C.UTF-8"),
            "PATH": self._environment.get("PATH", "/usr/bin:/bin"),
        }
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self._repository),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        if process.stdout is None or process.stderr is None:
            await self._terminate_process_group(process)
            raise WorkerMethodError(
                "workspace.files_pipe_missing",
                "仓库文件清单管道不可用",
                "使用 headless 命令检查本地运行环境",
            )
        stdout_task = asyncio.create_task(self._read_bounded(process.stdout))
        stderr_task = asyncio.create_task(self._read_bounded(process.stderr))
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.CancelledError:
            await self._terminate_process_group(process)
            await asyncio.gather(
                stdout_task,
                stderr_task,
                return_exceptions=True,
            )
            raise
        except TimeoutError as error:
            await self._terminate_process_group(process)
            await asyncio.gather(
                stdout_task,
                stderr_task,
                return_exceptions=True,
            )
            raise WorkerMethodError(
                "workspace.files_timeout",
                "仓库文件清单生成超时",
                "缩小仓库范围后重试",
            ) from error
        stdout_result, _ = await asyncio.gather(stdout_task, stderr_task)
        stdout, stdout_truncated = stdout_result
        if process.returncode != 0:
            raise WorkerMethodError(
                "workspace.git_required",
                "文件选择器需要有效 Git 仓库",
                "先运行 rivet init 或检查仓库状态",
            )
        if stdout_truncated:
            raise WorkerMethodError(
                "workspace.files_too_large",
                "仓库文件清单超过安全上限",
                "输入更精确的文件路径关键词",
            )
        normalized_query = query.casefold()
        paths: list[str] = []
        for raw_path in stdout.split(b"\0"):
            if not raw_path:
                continue
            try:
                path = raw_path.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                continue
            if _unsafe_picker_path(path):
                continue
            if (self._repository / path).is_symlink():
                continue
            if normalized_query and not _picker_query_matches(
                path.casefold(),
                normalized_query,
            ):
                continue
            paths.append(path)
            if len(paths) >= MAX_FILE_LIST_RESULTS:
                break
        paths.sort(key=lambda path: (path.count("/"), path.casefold()))
        selected = paths[:limit]
        json_paths = [cast(JsonValue, path) for path in selected]
        await emit(
            "workspace.tree_updated",
            {
                "paths": json_paths,
                "summary": f"已加载 {len(selected)} 个仓库文件路径",
            },
        )
        result: dict[str, JsonValue] = {
            "paths": json_paths,
            "truncated": len(paths) > limit,
        }
        return result

    async def _list_sessions(
        self,
        request: IpcRequest,
        *,
        emit: EmitEvent,
    ) -> JsonValue:
        """按需读取经过哈希验证的近期会话标识。"""
        limit = request.params.get("limit", 20)
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 100
        ):
            raise WorkerMethodError(
                "session.limit_invalid",
                "会话数量上限无效",
                "使用 1 到 100 之间的结果上限",
            )
        from rivet.storage.sessions import SessionStore

        try:
            sessions = SessionStore(self._repository).list_recent_ids(limit=limit)
        except ValueError as error:
            raise WorkerMethodError(
                "session.list_invalid",
                "近期会话目录无法安全读取",
                "运行 rivet doctor 并检查本地会话状态",
            ) from error
        json_sessions = [cast(JsonValue, session_id) for session_id in sessions]
        await emit(
            "sessions.snapshot",
            {
                "sessions": json_sessions,
                "summary": f"已加载 {len(sessions)} 个近期会话",
            },
        )
        return {"sessions": json_sessions}

    async def _request_fix_permission(
        self,
        *,
        emit: EmitEvent,
        cancel_event: asyncio.Event,
    ) -> None:
        """只有 TUI 明确批准后才把 --yes 交给 headless 核心。"""
        permission_id = f"request_permission_{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self._permissions[permission_id] = future
        await emit(
            "permission.requested",
            {
                "argv": "rivet fix --yes",
                "cwd": ".",
                "network": "仅模型 Provider",
                "paths": "隔离事务 Worktree",
                "permission": "WRITE+EXECUTE+NETWORK",
                "reason": "修改隔离副本并运行冻结验证命令",
                "request_id": permission_id,
                "timeout_seconds": 900,
            },
        )
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {future, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done:
                raise asyncio.CancelledError
            approved = future.result()
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            self._permissions.pop(permission_id, None)
        if not approved:
            raise WorkerMethodError(
                "guard.permission_denied",
                "用户拒绝 fix 权限请求",
                "调整任务范围后重新提交",
            )

    async def _resolve_permission(
        self,
        request: IpcRequest,
        *,
        emit: EmitEvent,
    ) -> JsonValue:
        """只决议当前 Worker 创建且尚未结束的权限请求。"""
        permission_id = request.params.get("request_id")
        approved = request.params.get("approved")
        if not isinstance(permission_id, str) or not isinstance(approved, bool):
            raise WorkerMethodError(
                "permission.input_invalid",
                "权限响应参数无效",
                "刷新权限面板后重试",
            )
        future = self._permissions.get(permission_id)
        if future is None or future.done():
            raise WorkerMethodError(
                "permission.request_missing",
                "权限请求不存在或已经结束",
                "刷新当前任务状态",
            )
        future.set_result(approved)
        await emit(
            "permission.resolved",
            {"approved": approved, "request_id": permission_id},
        )
        return {"approved": approved, "request_id": permission_id}

    def _arguments(
        self,
        request: IpcRequest,
        command: str,
        *,
        context_paths: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        """严格把 JSON 参数转成无 shell argv。"""
        prefix: tuple[str, ...] = (
            sys.executable,
            "-m",
            "rivet",
            "--repository",
            str(self._repository),
            "--json",
        )
        model = request.params.get("model")
        if model is not None:
            if (
                not isinstance(model, str)
                or not model
                or len(model) > 128
                or model.startswith("-")
            ):
                raise WorkerMethodError(
                    "config.model_invalid",
                    "模型名称无效",
                    "从 /model 列表中选择受支持模型",
                )
            prefix = (*prefix, "--model", model)
        prefix = (*prefix, command)
        if command in {"ask", "plan", "fix"}:
            query = request.params.get("query")
            if (
                not isinstance(query, str)
                or not query
                or len(query) > MAX_TUI_QUERY_CHARS
            ):
                raise WorkerMethodError(
                    "task.query_invalid",
                    "任务文本为空或超过长度上限",
                    "输入明确且有界的任务文本",
                )
            if context_paths:
                context_suffix = "\n\n用户显式选择的仓库文件：\n" + "\n".join(
                    f"- @{path}" for path in context_paths
                )
                if len(query) + len(context_suffix) > MAX_TUI_QUERY_CHARS:
                    raise WorkerMethodError(
                        "task.query_invalid",
                        "任务文本和上下文路径超过长度上限",
                        "减少输入文本或上下文文件后重试",
                    )
                query = f"{query}{context_suffix}"
            return (*prefix, query)
        if command in {"read", "init"}:
            path = request.params.get("file")
            if path is None and command == "init":
                return prefix
            if not isinstance(path, str) or not path or len(path) > 4_096:
                raise WorkerMethodError(
                    "workspace.path_invalid",
                    "仓库路径无效",
                    "从 /files 选择仓库内路径",
                )
            return (*prefix, path)
        if command == "resume":
            session_id = request.params.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                raise WorkerMethodError(
                    "session.id_invalid",
                    "会话 ID 无效",
                    "从 /sessions 选择可恢复会话",
                )
            return (*prefix, session_id)
        if command == "trace":
            run_id = request.params.get("run_id")
            if run_id is None:
                return prefix
            if not isinstance(run_id, str) or not run_id:
                raise WorkerMethodError(
                    "trace.run_id_invalid",
                    "Run ID 无效",
                    "从 Trace 面板选择有效 Run",
                )
            return (*prefix, run_id)
        if command not in {"verify", "diff", "apply", "abort"}:
            return prefix
        transaction_id = request.params.get("transaction_id")
        if transaction_id is None and command in {"verify", "diff"}:
            return prefix
        if not isinstance(transaction_id, str) or not transaction_id:
            raise WorkerMethodError(
                "transaction.id_invalid",
                "事务 ID 无效",
                "刷新事务状态并使用有效 TX_ID",
            )
        return (*prefix, transaction_id)

    def _selected_context_paths(self, request: IpcRequest) -> tuple[str, ...]:
        """校验用户显式选择的仓库文件，且不读取文件正文。"""
        raw_paths = request.params.get("context_paths", [])
        if (
            not isinstance(raw_paths, list)
            or len(raw_paths) > MAX_SELECTED_CONTEXT_FILES
        ):
            raise WorkerMethodError(
                "context.paths_invalid",
                "上下文文件列表无效或超过数量上限",
                "最多选择 20 个仓库内普通文件",
            )
        selected: list[str] = []
        for raw_path in raw_paths:
            if not isinstance(raw_path, str) or len(raw_path) > 4_096:
                raise WorkerMethodError(
                    "context.path_invalid",
                    "上下文路径无效",
                    "从文件选择器重新选择仓库内文件",
                )
            pure_path = PurePosixPath(raw_path)
            if pure_path.as_posix() != raw_path or _unsafe_picker_path(raw_path):
                raise WorkerMethodError(
                    "context.path_invalid",
                    "上下文路径不属于安全仓库路径",
                    "从文件选择器重新选择仓库内文件",
                )
            candidate = self._repository
            try:
                for part in pure_path.parts:
                    candidate = candidate / part
                    if candidate.is_symlink():
                        raise OSError
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(self._repository)
            except (OSError, RuntimeError, ValueError) as error:
                raise WorkerMethodError(
                    "context.path_invalid",
                    "上下文文件不存在或越出仓库",
                    "从文件选择器重新选择仓库内普通文件",
                ) from error
            if not resolved.is_file():
                raise WorkerMethodError(
                    "context.path_invalid",
                    "上下文路径不是普通文件",
                    "从文件选择器重新选择仓库内普通文件",
                )
            if raw_path not in selected:
                selected.append(raw_path)
        return tuple(selected)

    async def _run_subprocess(
        self,
        argv: tuple[str, ...],
        emit: EmitEvent,
    ) -> CommandExecution:
        """以白名单环境运行同一 CLI，并按 TERM/KILL 回收取消任务。"""
        environment = {
            name: value
            for name, value in self._environment.items()
            if name
            in {
                "DEEPSEEK_API_KEY",
                "LANG",
                "LC_ALL",
                "PATH",
                "RIVET_BASE_URL",
                "RIVET_BWRAP_PATH",
                "RIVET_MAX_COST_USD",
                "RIVET_MAX_ROUNDS",
                "RIVET_MAX_TOTAL_TOKENS",
                "RIVET_MODEL",
                "RIVET_SAFE_MODE",
                "TZ",
                "XDG_CACHE_HOME",
                "XDG_CONFIG_HOME",
                "XDG_STATE_HOME",
            }
        }
        stream_id = f"stream_{uuid.uuid4().hex}"
        environment["RIVET_STREAM_ID"] = stream_id
        trace_path = self._repository / ".rivet" / "trace" / "events.ndjson"
        trace_offset = self._trace_offset(trace_path)
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self._repository,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        if process.stdout is None or process.stderr is None:
            await self._terminate_process_group(process)
            raise WorkerMethodError(
                "ipc.command_pipe_missing",
                "命令输出管道不可用",
                "使用 headless 命令诊断本地运行环境",
            )
        stdout_task = asyncio.create_task(self._read_bounded(process.stdout))
        stderr_task = asyncio.create_task(self._read_bounded(process.stderr))
        trace_task = asyncio.create_task(
            self._stream_trace_events(
                process,
                trace_path=trace_path,
                initial_offset=trace_offset,
                stream_id=stream_id,
                emit=emit,
            )
        )
        try:
            await process.wait()
        except asyncio.CancelledError:
            await self._terminate_process_group(process)
            await asyncio.gather(
                stdout_task,
                stderr_task,
                trace_task,
                return_exceptions=True,
            )
            raise
        stdout_result, stderr_result, _ = await asyncio.gather(
            stdout_task,
            stderr_task,
            trace_task,
        )
        stdout, stdout_truncated = stdout_result
        stderr, stderr_truncated = stderr_result
        if stdout_truncated or stderr_truncated:
            raise WorkerMethodError(
                "ipc.command_output_too_large",
                "命令输出超过 Worker 上限",
                "使用 headless 命令检查完整 Trace 或 Evidence",
            )
        return CommandExecution(
            process.returncode or 0,
            stdout,
            stderr,
            stdout_truncated,
            stderr_truncated,
        )

    @staticmethod
    def _trace_offset(trace_path: Path) -> int:
        """只把命令启动后追加的 Trace 事件投影到当前 TUI 请求。"""
        try:
            if trace_path.is_symlink() or not trace_path.is_file():
                return 0
            return trace_path.stat().st_size
        except OSError:
            return 0

    async def _stream_trace_events(
        self,
        process: asyncio.subprocess.Process,
        *,
        trace_path: Path,
        initial_offset: int,
        stream_id: str,
        emit: EmitEvent,
    ) -> None:
        """在子 CLI 运行期间持续投影已持久化、已脱敏的 Trace 行。"""
        offset = initial_offset
        pending = bytearray()
        discarding_oversized_line = False
        active_run_id: str | None = None
        while True:
            chunk = self._read_trace_chunk(trace_path, offset)
            if chunk:
                offset += len(chunk)
                pending.extend(chunk)
                while b"\n" in pending:
                    raw_line, _, remainder = pending.partition(b"\n")
                    pending = bytearray(remainder)
                    if discarding_oversized_line:
                        discarding_oversized_line = False
                        continue
                    if len(raw_line) > MAX_COMMAND_OUTPUT_BYTES:
                        continue
                    projected = self._project_trace_line(bytes(raw_line))
                    if projected is None:
                        continue
                    if active_run_id is None:
                        if projected.stream_id != stream_id:
                            continue
                        active_run_id = projected.run_id
                    if projected.run_id == active_run_id:
                        await emit(projected.event_type, projected.payload)
                if len(pending) > MAX_COMMAND_OUTPUT_BYTES:
                    pending.clear()
                    discarding_oversized_line = True
            if process.returncode is not None:
                final_chunk = self._read_trace_chunk(trace_path, offset)
                if final_chunk:
                    continue
                return
            await asyncio.sleep(0.02)

    @staticmethod
    def _read_trace_chunk(trace_path: Path, offset: int) -> bytes:
        """有界读取一个普通 Trace 文件的新追加部分。"""
        try:
            if trace_path.is_symlink() or not trace_path.is_file():
                return b""
            with trace_path.open("rb") as stream:
                stream.seek(offset)
                return stream.read(MAX_COMMAND_OUTPUT_BYTES)
        except OSError:
            return b""

    @staticmethod
    def _project_trace_line(
        raw_line: bytes,
    ) -> _ProjectedTraceEvent | None:
        """严格收窄 Trace envelope，并补充 TUI 时间线所需摘要。"""
        try:
            raw_record: object = json.loads(raw_line)
        except (UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw_record, dict):
            return None
        record = cast(dict[str, object], raw_record)
        raw_event = record.get("event")
        if not isinstance(raw_event, dict):
            return None
        event = cast(dict[str, object], raw_event)
        event_type = event.get("event_type")
        run_id = event.get("run_id")
        raw_payload = event.get("payload")
        if (
            not isinstance(event_type, str)
            or not isinstance(run_id, str)
            or not isinstance(raw_payload, dict)
        ):
            return None
        try:
            payload = cast(
                dict[str, JsonValue],
                json.loads(
                    json.dumps(
                        raw_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                ),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        summary = event.get("result_summary") or event.get("input_summary")
        if isinstance(summary, str):
            payload.setdefault("summary", summary)
        projected_type = (
            "transaction.started" if event_type == "transaction.created" else event_type
        )
        raw_stream_id = payload.get("stream_id")
        stream_id = raw_stream_id if isinstance(raw_stream_id, str) else None
        payload.pop("stream_id", None)
        return _ProjectedTraceEvent(
            event_type=projected_type,
            payload=payload,
            run_id=run_id,
            stream_id=stream_id,
        )

    @staticmethod
    async def _read_bounded(
        stream: asyncio.StreamReader,
    ) -> tuple[bytes, bool]:
        """持续 drain 子进程输出，但内存只保留固定上限。"""
        content = bytearray()
        truncated = False
        while chunk := await stream.read(65_536):
            remaining = MAX_COMMAND_OUTPUT_BYTES - len(content)
            if remaining > 0:
                content.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        return bytes(content), truncated

    @staticmethod
    async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
        """按 TERM、有界等待、KILL、wait 回收整个命令会话。"""
        if process.returncode is not None:
            await process.wait()
            return
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
            return
        except TimeoutError:
            pass
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()

    @staticmethod
    def _decode_execution(execution: CommandExecution) -> dict[str, JsonValue]:
        """只接受正式 CLI 的单个 JSON 对象成功输出。"""
        try:
            raw_payload: object = json.loads(execution.stdout)
        except (UnicodeError, json.JSONDecodeError) as error:
            if execution.return_code == 0:
                raise WorkerMethodError(
                    "ipc.command_output_invalid",
                    "命令未返回有效 JSON",
                    "查看 Worker stderr 诊断",
                ) from error
            return {}
        if not isinstance(raw_payload, dict):
            raise WorkerMethodError(
                "ipc.command_output_invalid",
                "命令返回的 JSON 不是对象",
                "升级 CLI 与 TUI 到同一版本",
            )
        return cast(dict[str, JsonValue], raw_payload)

    @staticmethod
    def _raise_execution_error(execution: CommandExecution) -> None:
        """提取 CLI 已脱敏分类错误，不转发原始 stderr。"""
        try:
            raw_error: object = json.loads(execution.stderr)
        except (UnicodeError, json.JSONDecodeError):
            raw_error = None
        if isinstance(raw_error, dict):
            envelope = cast(dict[str, object], raw_error)
            detail = envelope.get("error")
            if isinstance(detail, dict):
                fields = cast(dict[str, object], detail)
                code = fields.get("code")
                summary = fields.get("summary")
                next_action = fields.get("next_action")
                if all(
                    isinstance(value, str) for value in (code, summary, next_action)
                ):
                    raise WorkerMethodError(
                        cast(str, code),
                        cast(str, summary),
                        cast(str, next_action),
                    )
        raise WorkerMethodError(
            "ipc.command_failed",
            "命令执行失败且未返回分类结果",
            "使用 headless --debug 在本机诊断",
        )

    async def _emit_payload(
        self,
        command: str,
        payload: dict[str, JsonValue],
        *,
        emit: EmitEvent,
    ) -> None:
        """把同一命令结果投影为 TUI 各面板消费的结构化事件。"""
        answer = payload.get("answer")
        if isinstance(answer, str) and answer:
            await emit("agent.completed", {"summary": answer})
        session_id = payload.get("session_id")
        if isinstance(session_id, str):
            await emit(
                "session.updated",
                {"session_id": session_id, "summary": "会话状态已保存"},
            )
        transaction_id = payload.get("transaction_id")
        if isinstance(transaction_id, str):
            await emit(
                "transaction.started",
                {"summary": "事务已更新", "transaction_id": transaction_id},
            )
        diff = payload.get("diff")
        if isinstance(diff, str):
            await emit("patch.updated", {"diff": diff, "summary": "补丁已更新"})
        status = payload.get("status")
        if isinstance(status, str) and command in {"fix", "verify"}:
            await emit(
                "verification.completed",
                {"status": status, "summary": "验证已完成"},
            )
        evidence_id = payload.get("evidence_id")
        if isinstance(evidence_id, str):
            await emit(
                "evidence.published",
                {"evidence_id": evidence_id, "summary": "证据已发布"},
            )
        usage = payload.get("usage")
        if isinstance(usage, dict):
            usage_fields = cast(dict[str, JsonValue], usage)
            tokens = usage_fields.get("total_tokens")
            cost = usage_fields.get("cost_usd")
            await emit(
                "budget.updated",
                {
                    "cost_usd": cost if isinstance(cost, (int, float)) else 0,
                    "elapsed_ms": 0,
                    "tokens": tokens if isinstance(tokens, int) else 0,
                },
            )


def _unsafe_picker_path(path: str) -> bool:
    """拒绝绝对、穿越、内部状态和疑似凭据路径。"""
    pure_path = PurePosixPath(path)
    lowered_parts = tuple(part.casefold() for part in pure_path.parts)
    if (
        pure_path.is_absolute()
        or ".." in pure_path.parts
        or any(unicodedata.category(character) in {"Cc", "Cf"} for character in path)
    ):
        return True
    if not lowered_parts:
        return True
    if any(
        part in {".aws", ".git", ".gnupg", ".rivet", ".ssh"} for part in lowered_parts
    ):
        return True
    name = lowered_parts[-1]
    if name == ".env.example":
        return False
    return (
        name == ".env"
        or name.startswith(".env.")
        or name
        in {
            "credentials.json",
            "id_dsa",
            "id_ecdsa",
            "id_ed25519",
            "id_rsa",
            "service-account.json",
        }
    )


def _picker_query_matches(path: str, query: str) -> bool:
    """支持连续匹配与稳定的字符子序列模糊匹配。"""
    if query in path:
        return True
    query_index = 0
    for character in path:
        if character == query[query_index]:
            query_index += 1
            if query_index == len(query):
                return True
    return False
