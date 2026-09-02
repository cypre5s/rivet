"""把最小 TUI IPC 方法路由到同一正式 CLI。"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import signal
import sys
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from pydantic import JsonValue, ValidationError

from rivet.cli.config import load_config
from rivet.cli.errors import CliConfigurationError
from rivet.contracts.ipc import IPC_APPLICATION_METHODS, IpcRequest
from rivet.ipc.worker import BaseWorkerApplication, EmitEvent, WorkerMethodError
from rivet.trace.errors import RuntimePathError
from rivet.trace.paths import RuntimePaths
from rivet.trace.redaction import SecretRedactor

if TYPE_CHECKING:
    from rivet.transaction.store import TransactionStore

MAX_COMMAND_OUTPUT_BYTES = 768 * 1024
MAX_FILE_LIST_BYTES = 4 * 1024 * 1024
MAX_FILE_LIST_RESULTS = 2_000
MAX_FILE_RESULT_BYTES = 512 * 1024
MAX_SELECTED_CONTEXT_FILES = 20
MAX_SELECTED_SCOPE_PATHS = 40
MAX_TUI_QUERY_CHARS = 65_536
MODEL_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
RUN_ID_PATTERN = re.compile(r"run_[a-z0-9][a-z0-9_-]{0,62}")
TRANSACTION_ID_PATTERN = re.compile(r"tx_[a-z0-9][a-z0-9_-]{0,62}")
SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
GIT_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
TUI_METHODS = frozenset(IPC_APPLICATION_METHODS)
TUI_COMMANDS = tuple(
    method.removeprefix("command.")
    for method in IPC_APPLICATION_METHODS
    if method.startswith("command.")
)
COMMAND_RESULT_FIELDS = frozenset(
    {
        "acceptance_sha256",
        "answer",
        "apply_eligible",
        "apply_required",
        "base_commit",
        "changed_files",
        "decided_at",
        "diff",
        "evidence_id",
        "evidence_verified",
        "files",
        "manifest_sha256",
        "model_status",
        "next_action",
        "passed",
        "patch_id",
        "patch_sha256",
        "run_id",
        "state",
        "status",
        "termination_reason",
        "transaction_id",
        "updated_at",
        "verdict_status",
        "verification_results",
        "verification_status",
    }
)
VERIFICATION_KINDS = frozenset(
    {"BASELINE", "BEHAVIOR", "REGRESSION", "SCOPE", "SECRET", "BINDING", "RESOURCE"}
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
    """保存可投影到当前 Worker 请求的 Trace 事件。"""

    event_type: str
    payload: dict[str, JsonValue]
    run_id: str
    stream_id: str | None


@dataclass(frozen=True, slots=True)
class _FixProposal:
    """保存只读调查形成且尚未创建事务的 FIX 提案。"""

    acceptance: dict[str, JsonValue]
    acceptance_sha256: str
    base_commit: str
    goal: str
    read_scope: tuple[str, ...]
    write_scope: tuple[str, ...]
    allowed_new_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    expected_behaviors: tuple[str, ...]
    preserved_behaviors: tuple[str, ...]
    acceptance_commands: tuple[tuple[str, ...], ...]
    regression_commands: tuple[tuple[str, ...], ...]
    budgets: dict[str, JsonValue]
    investigation: str
    next_action: str
    run_id: str

    def event_payload(self) -> dict[str, JsonValue]:
        return {
            "acceptance": self.acceptance,
            "acceptance_sha256": self.acceptance_sha256,
            "base_commit": self.base_commit,
            "confirmed": False,
            "investigation": self.investigation,
            "next_action": self.next_action,
            "run_id": self.run_id,
            "summary": "只读调查已完成，AcceptanceSpec 等待确认",
            "transaction_created": False,
        }


class CommandWorkerApplication(BaseWorkerApplication):
    """只公开两条主线真正需要的命令、事务和证据方法。"""

    def __init__(
        self,
        repository: Path,
        *,
        environment: Mapping[str, str] | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        super().__init__(repository)
        self._repository = repository.resolve(strict=True)
        self._environment = dict(os.environ if environment is None else environment)
        self._redactor = SecretRedactor(self._environment)
        self._runner = runner or self._run_subprocess
        self._command_lock = asyncio.Lock()
        self._permissions: dict[str, asyncio.Future[bool]] = {}

    def ready_payload(self) -> dict[str, JsonValue]:
        """只读投影模型与 Acceptance 就绪度，不探测全局依赖。"""
        payload = super().ready_payload()
        try:
            config = load_config(self._repository, environment=self._environment)
        except CliConfigurationError:
            payload.update(
                {
                    "acceptance_ready": False,
                    "credential_configured": False,
                    "model": "配置错误",
                    "models": [],
                }
            )
            return payload
        payload.update(
            {
                "credential_configured": config.credential_configured,
                "model": config.model,
                "models": list(config.models),
            }
        )
        payload.update(self._acceptance_readiness_payload())
        return payload

    async def handle(
        self,
        request: IpcRequest,
        *,
        emit: EmitEvent,
        cancel_event: asyncio.Event,
    ) -> JsonValue:
        """严格分发最小方法集合；外围方法统一失败关闭。"""
        if request.method == "permission.resolve":
            return await self._resolve_permission(request, emit=emit)
        if request.method == "workspace.files":
            return await self._list_repository_files(request, emit=emit)
        if request.method == "transactions.list":
            return await self._list_transactions(request, emit=emit)
        if request.method == "evidence.get":
            return await self._get_evidence(request, emit=emit)
        if request.method == "evidence.log":
            return await self._get_evidence_log(request, emit=emit)
        if not request.method.startswith("command."):
            raise self._unknown_method()
        command = request.method.removeprefix("command.")
        if command not in TUI_COMMANDS:
            raise self._unknown_method()
        async with self._command_lock:
            context_paths = (
                self._selected_context_paths(request)
                if command in {"ask", "fix"}
                else ()
            )
            write_scope, allowed_new_paths = (
                self._selected_fix_scopes(request) if command == "fix" else ((), ())
            )
            if command == "fix" and not write_scope and not allowed_new_paths:
                raise WorkerMethodError(
                    "acceptance.write_scope_required",
                    "FIX 必须具有非空显式写范围",
                    "在 /fix 中使用 --write PATH 或 --new PATH 明确授权",
                )
            arguments = self._arguments(
                request,
                command,
                context_paths=context_paths,
                write_scope=write_scope,
                allowed_new_paths=allowed_new_paths,
            )
            await emit(
                "command.started",
                {"command": command, "summary": f"/{command} 已提交"},
            )
            for path in context_paths:
                await emit(
                    "context.selected",
                    {"path": path, "reason": "用户显式选择"},
                )
            if cancel_event.is_set():
                raise asyncio.CancelledError
            if command == "fix":
                proposal_execution = await self._runner(arguments, emit)
                if proposal_execution.return_code != 0:
                    self._raise_execution_error(proposal_execution)
                proposal = self._decode_fix_proposal(
                    proposal_execution,
                    expected_query=cast(str, request.params["query"]),
                    expected_read_scope=tuple(sorted({*context_paths, *write_scope})),
                    expected_write_scope=tuple(
                        sorted({*write_scope, *allowed_new_paths})
                    ),
                    expected_allowed_new_paths=allowed_new_paths,
                )
                await emit("acceptance.proposed", proposal.event_payload())
                await self._request_fix_permission(
                    proposal,
                    emit=emit,
                    cancel_event=cancel_event,
                )
                arguments = (
                    *arguments,
                    "--yes",
                    "--acceptance-sha256",
                    proposal.acceptance_sha256,
                    "--base-commit",
                    proposal.base_commit,
                )
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
            return payload

    async def close(self) -> None:
        """取消仍等待用户决议的权限 Future。"""
        for future in self._permissions.values():
            if not future.done():
                future.cancel()
        self._permissions.clear()

    def _acceptance_readiness_payload(self) -> dict[str, JsonValue]:
        """只解析项目文件，不执行检测到的任何命令。"""
        from rivet.verify.detector import ProjectDetector, evidence_readiness
        from rivet.verify.errors import VerificationError

        try:
            readiness = evidence_readiness(ProjectDetector().detect(self._repository))
        except VerificationError as error:
            return {
                "acceptance_action": "修复 .rivet/project.toml 后刷新 Worker",
                "acceptance_ready": False,
                "acceptance_reason": error.summary,
            }
        return {
            "acceptance_action": readiness.next_action,
            "acceptance_ready": readiness.ready,
            "acceptance_reason": readiness.reason,
        }

    async def _list_repository_files(
        self,
        request: IpcRequest,
        *,
        emit: EmitEvent,
    ) -> JsonValue:
        """仅在打开 @ 选择器时列出 Git 可见路径。"""
        if set(request.params) - {"limit", "query"}:
            raise self._invalid_params("workspace.files")
        query = request.params.get("query", "")
        limit = request.params.get("limit", 200)
        if not isinstance(query, str) or len(query) > 512 or "\x00" in query:
            raise WorkerMethodError(
                "workspace.query_invalid",
                "文件搜索文本无效",
                "使用不超过 512 字符的文件名或路径",
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
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
                "检查本地 Git 运行环境",
            )
        stdout_task = asyncio.create_task(
            self._read_bounded(process.stdout, limit=MAX_FILE_LIST_BYTES)
        )
        stderr_task = asyncio.create_task(
            self._read_bounded(process.stderr, limit=MAX_COMMAND_OUTPUT_BYTES)
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.CancelledError:
            await self._terminate_process_group(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        except TimeoutError as error:
            await self._terminate_process_group(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
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
                "先初始化 Git 仓库或检查仓库状态",
            )
        if stdout_truncated:
            raise WorkerMethodError(
                "workspace.files_too_large",
                "仓库文件清单超过安全上限",
                "输入更精确的路径关键词或缩小仓库",
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
            if _unsafe_picker_path(path) or self._path_contains_symlink(path):
                continue
            if normalized_query and not _picker_query_matches(
                path.casefold(), normalized_query
            ):
                continue
            paths.append(path)
            if len(paths) >= MAX_FILE_LIST_RESULTS:
                break
        paths.sort(key=lambda path: (path.count("/"), path.casefold()))
        selected: list[str] = []
        selected_bytes = 0
        for path in paths[:limit]:
            path_bytes = len(path.encode("utf-8"))
            if selected_bytes + path_bytes > MAX_FILE_RESULT_BYTES:
                break
            selected.append(path)
            selected_bytes += path_bytes
        result: dict[str, JsonValue] = {
            "paths": cast(JsonValue, selected),
            "truncated": len(selected) < len(paths),
        }
        await emit(
            "workspace.tree_updated",
            {
                "paths": cast(JsonValue, selected),
                "summary": f"已加载 {len(selected)} 个仓库路径",
            },
        )
        return result

    async def _list_transactions(
        self,
        request: IpcRequest,
        *,
        emit: EmitEvent,
    ) -> JsonValue:
        """从按仓库隔离的 XDG 状态根读取近期事务。"""
        if set(request.params) - {"limit"}:
            raise self._invalid_params("transactions.list")
        limit = request.params.get("limit", 20)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise WorkerMethodError(
                "transaction.limit_invalid",
                "事务数量上限无效",
                "使用 1 到 100 之间的结果上限",
            )
        from rivet.transaction.errors import TransactionError

        store = self._transaction_store()
        try:
            records = store.list_recent_records(limit=limit)
            transactions: list[JsonValue] = []
            for record in records:
                patch = (
                    store.load_patch(record.transaction_id, record.current_patch_id)[0]
                    if record.current_patch_id is not None
                    else None
                )
                transactions.append(
                    {
                        "apply_eligible": record.state.value == "VERIFIED",
                        "evidence_id": record.evidence_id,
                        "patch_id": record.current_patch_id,
                        "patch_sha256": (
                            patch.patch_sha256 if patch is not None else None
                        ),
                        "state": record.state.value,
                        "transaction_id": record.transaction_id,
                        "updated_at": record.updated_at.isoformat(),
                    }
                )
        except TransactionError as error:
            raise WorkerMethodError(
                "transaction.list_invalid",
                "近期事务无法通过完整性校验",
                "保留 XDG 状态并使用 headless diff 诊断",
            ) from error
        result: dict[str, JsonValue] = {"transactions": transactions}
        await emit(
            "transactions.snapshot",
            {
                "summary": f"已加载 {len(transactions)} 个近期事务",
                "transactions": transactions,
            },
        )
        return result

    async def _get_evidence(
        self,
        request: IpcRequest,
        *,
        emit: EmitEvent,
    ) -> JsonValue:
        """只投影完整性复核通过的 Evidence。"""
        if set(request.params) != {"transaction_id"}:
            raise self._invalid_params("evidence.get")
        transaction_id = self._transaction_id(request)
        from rivet.transaction.errors import TransactionError
        from rivet.verify.evidence_query import EvidenceQueryService

        try:
            payload = EvidenceQueryService(self._transaction_store()).detail(
                transaction_id
            )
        except TransactionError as error:
            raise WorkerMethodError(
                error.code,
                error.summary,
                "使用 rivet diff/verify 检查事务与 Evidence 完整性",
            ) from error
        redacted = self._redactor.redact_payload(payload)
        await emit("evidence.snapshot", redacted)
        return redacted

    async def _get_evidence_log(
        self,
        request: IpcRequest,
        *,
        emit: EmitEvent,
    ) -> JsonValue:
        """显式读取一个与 manifest 绑定的真实验证日志。"""
        if set(request.params) - {"step_id", "transaction_id"} or (
            "transaction_id" not in request.params
        ):
            raise self._invalid_params("evidence.log")
        transaction_id = self._transaction_id(request)
        step_id = request.params.get("step_id")
        if step_id is not None and (
            not isinstance(step_id, str)
            or not step_id
            or len(step_id) > 256
            or "\x00" in step_id
        ):
            raise WorkerMethodError(
                "evidence.step_id_invalid",
                "Evidence 步骤 ID 无效",
                "刷新 Evidence 详情后重试",
            )
        from rivet.transaction.errors import TransactionError
        from rivet.verify.evidence_query import EvidenceQueryService

        try:
            payload = EvidenceQueryService(self._transaction_store()).log(
                transaction_id,
                step_id=step_id,
            )
        except TransactionError as error:
            raise WorkerMethodError(
                error.code,
                error.summary,
                "选择带日志的真实验证步骤后重试",
            ) from error
        redacted = self._redactor.redact_payload(payload)
        await emit("evidence.log", redacted)
        return redacted

    async def _request_fix_permission(
        self,
        proposal: _FixProposal,
        *,
        emit: EmitEvent,
        cancel_event: asyncio.Event,
    ) -> None:
        """展示真实提案，并在把 --yes 交给 FIX 前等待明确决议。"""
        permission_id = f"request_permission_{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self._permissions[permission_id] = future
        acceptance_commands = [
            list(command) for command in proposal.acceptance_commands
        ]
        regression_commands = [
            list(command) for command in proposal.regression_commands
        ]
        rendered_scope = ", ".join(proposal.write_scope)
        existing_write_scope = tuple(
            path
            for path in proposal.write_scope
            if path not in set(proposal.allowed_new_paths)
        )
        rendered_command = "rivet fix"
        for path in proposal.read_scope:
            if path not in existing_write_scope:
                rendered_command += f" --allow-read {path}"
        for path in existing_write_scope:
            rendered_command += f" --allow-write {path}"
        for path in proposal.allowed_new_paths:
            rendered_command += f" --allow-new {path}"
        rendered_command += (
            " --yes"
            f" --acceptance-sha256 {proposal.acceptance_sha256}"
            f" --base-commit {proposal.base_commit}"
        )
        await emit(
            "permission.requested",
            {
                "acceptance_commands": cast(JsonValue, acceptance_commands),
                "acceptance_sha256": proposal.acceptance_sha256,
                "allowed_new_paths": cast(JsonValue, list(proposal.allowed_new_paths)),
                "argv": rendered_command,
                "base_commit": proposal.base_commit,
                "budgets": cast(JsonValue, proposal.budgets),
                "cwd": "批准后创建独立 Git Worktree",
                "expected_behaviors": cast(
                    JsonValue, list(proposal.expected_behaviors)
                ),
                "forbidden_paths": cast(JsonValue, list(proposal.forbidden_paths)),
                "goal": _terminal_text(proposal.goal),
                "investigation": _terminal_text(proposal.investigation),
                "network": "Provider API 可联网；本地工具与验证进程保持断网",
                "paths": rendered_scope,
                "permission": "WRITE+EXECUTE",
                "preserved_behaviors": cast(
                    JsonValue, list(proposal.preserved_behaviors)
                ),
                "proposal_run_id": proposal.run_id,
                "read_scope": cast(JsonValue, list(proposal.read_scope)),
                "reason": "确认真实 Goal、读写边界、新建范围与独立验证命令",
                "regression_commands": cast(JsonValue, regression_commands),
                "request_id": permission_id,
                "timeout_seconds": cast(int, proposal.budgets["max_wall_seconds"]),
                "write_scope": cast(JsonValue, list(proposal.write_scope)),
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
                "用户拒绝 FIX 权限请求",
                "调整目标或 AcceptanceSpec 后重新提交",
            )

    async def _resolve_permission(
        self,
        request: IpcRequest,
        *,
        emit: EmitEvent,
    ) -> JsonValue:
        """只决议当前 Worker 创建且尚未结束的权限请求。"""
        if set(request.params) != {"approved", "request_id"}:
            raise self._invalid_params("permission.resolve")
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
        result: dict[str, JsonValue] = {
            "approved": approved,
            "request_id": permission_id,
        }
        await emit("permission.resolved", result)
        return result

    def _arguments(
        self,
        request: IpcRequest,
        command: str,
        *,
        context_paths: tuple[str, ...] = (),
        write_scope: tuple[str, ...] = (),
        allowed_new_paths: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        """严格把最小 JSON 参数映射为无 shell argv。"""
        if command == "ask":
            allowed = {"context_paths", "model", "query"}
        elif command == "fix":
            allowed = {
                "allowed_new_paths",
                "context_paths",
                "model",
                "query",
                "write_scope",
            }
        else:
            allowed = {"transaction_id"}
        if set(request.params) - allowed:
            raise self._invalid_params(f"command.{command}")
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
                command not in {"ask", "fix"}
                or not isinstance(model, str)
                or MODEL_NAME_PATTERN.fullmatch(model) is None
            ):
                raise WorkerMethodError(
                    "config.model_invalid",
                    "模型名称无效",
                    "从 /model 列表中选择 Worker 公布的模型",
                )
            prefix = (*prefix, "--model", model)
        prefix = (*prefix, command)
        if command in {"ask", "fix"}:
            query = request.params.get("query")
            if (
                not isinstance(query, str)
                or not query
                or len(query) > MAX_TUI_QUERY_CHARS
                or "\x00" in query
            ):
                raise WorkerMethodError(
                    "task.query_invalid",
                    "任务文本为空或超过长度上限",
                    "输入明确且有界的任务文本",
                )
            if context_paths and command == "ask":
                suffix = "\n\n用户显式选择的仓库文件：\n" + "\n".join(
                    f"- @{path}" for path in context_paths
                )
                if len(query) + len(suffix) > MAX_TUI_QUERY_CHARS:
                    raise WorkerMethodError(
                        "task.query_invalid",
                        "任务文本和上下文路径超过长度上限",
                        "减少输入文本或上下文文件后重试",
                    )
                query = f"{query}{suffix}"
            arguments = (*prefix, query)
            if command == "fix":
                for path in context_paths:
                    arguments = (*arguments, "--allow-read", path)
                for path in write_scope:
                    arguments = (*arguments, "--allow-write", path)
                for path in allowed_new_paths:
                    arguments = (*arguments, "--allow-new", path)
            return arguments
        transaction_id = request.params.get("transaction_id")
        if transaction_id is None and command in {"diff", "verify"}:
            return prefix
        if (
            not isinstance(transaction_id, str)
            or TRANSACTION_ID_PATTERN.fullmatch(transaction_id) is None
        ):
            raise WorkerMethodError(
                "transaction.id_invalid",
                "事务 ID 无效",
                "从近期事务列表重新选择",
            )
        return (*prefix, transaction_id)

    def _selected_context_paths(self, request: IpcRequest) -> tuple[str, ...]:
        """验证用户选择的仓库普通文件，不读取其正文。"""
        raw_paths = request.params.get("context_paths", [])
        if (
            not isinstance(raw_paths, list)
            or len(raw_paths) > MAX_SELECTED_CONTEXT_FILES
        ):
            raise WorkerMethodError(
                "context.paths_invalid",
                "显式上下文文件列表无效",
                "从 @ 文件选择器选择不超过 20 个仓库文件",
            )
        selected: list[str] = []
        for raw_path in raw_paths:
            if (
                not isinstance(raw_path, str)
                or len(raw_path) > 4_096
                or _unsafe_picker_path(raw_path)
                or self._path_contains_symlink(raw_path)
            ):
                raise self._invalid_context_path()
            candidate = self._repository / PurePosixPath(raw_path)
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(self._repository)
            except (OSError, RuntimeError, ValueError) as error:
                raise self._invalid_context_path() from error
            if not resolved.is_file():
                raise self._invalid_context_path()
            if raw_path not in selected:
                selected.append(raw_path)
        return tuple(selected)

    def _selected_fix_scopes(
        self,
        request: IpcRequest,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """分别校验现有写路径和明确不存在的新建路径。"""
        write_scope = self._selected_scope_paths(
            request.params.get("write_scope", []),
            require_exists=True,
            key="write_scope",
        )
        allowed_new_paths = self._selected_scope_paths(
            request.params.get("allowed_new_paths", []),
            require_exists=False,
            key="allowed_new_paths",
        )
        overlap = set(write_scope).intersection(allowed_new_paths)
        if overlap:
            raise WorkerMethodError(
                "acceptance.scope_overlap",
                "现有写范围与新建范围不能重复",
                "已存在路径用 --write，不存在路径用 --new",
            )
        return write_scope, allowed_new_paths

    def _selected_scope_paths(
        self,
        raw_paths: JsonValue | None,
        *,
        require_exists: bool,
        key: str,
    ) -> tuple[str, ...]:
        """校验显式写范围，不把只读 @ context 隐式升级为写权限。"""
        if not isinstance(raw_paths, list) or len(raw_paths) > MAX_SELECTED_SCOPE_PATHS:
            raise WorkerMethodError(
                "acceptance.scope_invalid",
                f"{key} 不是有效的有界路径列表",
                "使用 /fix 的 --write PATH 或 --new PATH",
            )
        selected: list[str] = []
        for raw_path in raw_paths:
            if (
                not isinstance(raw_path, str)
                or len(raw_path) > 4_096
                or _unsafe_picker_path(raw_path)
                or self._path_contains_symlink(raw_path)
            ):
                raise self._invalid_scope_path(require_exists=require_exists)
            candidate = self._repository / PurePosixPath(raw_path)
            try:
                resolved = candidate.resolve(strict=False)
                resolved.relative_to(self._repository)
            except (OSError, RuntimeError, ValueError) as error:
                raise self._invalid_scope_path(require_exists=require_exists) from error
            if require_exists:
                if not resolved.exists() or not (
                    resolved.is_file() or resolved.is_dir()
                ):
                    raise self._invalid_scope_path(require_exists=True)
                if resolved.is_file() and resolved.stat().st_nlink > 1:
                    raise self._invalid_scope_path(require_exists=True)
            elif resolved.exists():
                raise self._invalid_scope_path(require_exists=False)
            if raw_path in selected:
                raise WorkerMethodError(
                    "acceptance.scope_duplicate",
                    f"{key} 包含重复路径",
                    "每个显式授权路径只保留一次",
                )
            selected.append(raw_path)
        return tuple(sorted(selected))

    def _path_contains_symlink(self, path: str) -> bool:
        """拒绝路径任一现存组成部分为符号链接。"""
        current = self._repository
        for part in PurePosixPath(path).parts:
            current /= part
            if current.is_symlink():
                return True
        return False

    def _transaction_store(self) -> TransactionStore:
        """惰性构造 XDG TransactionStore，避免打开面板产生目录。"""
        from rivet.transaction.store import TransactionStore

        try:
            paths = RuntimePaths.for_repository(
                self._repository,
                environment=self._environment,
            )
        except RuntimePathError as error:
            raise WorkerMethodError(
                "runtime.path_invalid",
                "Rivet 运行状态路径无效",
                "将 XDG_STATE_HOME/XDG_CACHE_HOME 设置为仓库外绝对路径",
            ) from error
        return TransactionStore(
            paths.transactions_root,
            evidence_root=paths.evidence_root,
        )

    @staticmethod
    def _transaction_id(request: IpcRequest) -> str:
        value = request.params.get("transaction_id")
        if (
            not isinstance(value, str)
            or TRANSACTION_ID_PATTERN.fullmatch(value) is None
        ):
            raise WorkerMethodError(
                "transaction.id_invalid",
                "事务 ID 无效",
                "从近期事务列表重新选择",
            )
        return value

    @staticmethod
    def _invalid_context_path() -> WorkerMethodError:
        return WorkerMethodError(
            "context.path_invalid",
            "上下文文件不存在、敏感、经过符号链接或越出仓库",
            "从 @ 文件选择器重新选择仓库内普通文本文件",
        )

    @staticmethod
    def _invalid_scope_path(*, require_exists: bool) -> WorkerMethodError:
        return WorkerMethodError(
            "acceptance.scope_invalid",
            (
                "写范围必须是仓库内现有的安全普通文件或目录"
                if require_exists
                else "新建范围必须是仓库内尚不存在的安全路径"
            ),
            "已存在路径用 --write，不存在路径用 --new",
        )

    @staticmethod
    def _invalid_params(method: str) -> WorkerMethodError:
        return WorkerMethodError(
            "ipc.params_invalid",
            f"{method} 参数不符合最小协议",
            "刷新客户端并只发送该方法公布的参数",
        )

    @staticmethod
    def _unknown_method() -> WorkerMethodError:
        return WorkerMethodError(
            "ipc.method_unknown",
            "Worker 方法未注册",
            "检查客户端版本或使用最小协议公布的方法",
        )

    async def _run_subprocess(
        self,
        argv: tuple[str, ...],
        emit: EmitEvent,
    ) -> CommandExecution:
        """以白名单环境运行同一 CLI，并同步投影耐久 Trace。"""
        allowed_environment = {
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
            "TZ",
            "XDG_CACHE_HOME",
            "XDG_STATE_HOME",
        }
        environment = {
            name: value
            for name, value in self._environment.items()
            if name in allowed_environment
        }
        environment.setdefault("PATH", "/usr/bin:/bin")
        stream_id = f"stream_{uuid.uuid4().hex}"
        environment["RIVET_STREAM_ID"] = stream_id
        try:
            trace_path = RuntimePaths.for_repository(
                self._repository,
                environment=self._environment,
            ).events_path
        except RuntimePathError as error:
            raise WorkerMethodError(
                "runtime.path_invalid",
                "Rivet 运行状态路径无效",
                "将 XDG 状态与缓存目录设置为仓库外绝对路径",
            ) from error
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
                "命令输出超过 Worker 消息上限",
                "使用 headless 命令查看完整 Diff 或 Evidence",
            )
        return CommandExecution(
            process.returncode if process.returncode is not None else -1,
            stdout,
            stderr,
            stdout_truncated,
            stderr_truncated,
        )

    @staticmethod
    def _trace_offset(trace_path: Path) -> int:
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
        """持续投影当前命令新追加且 stream_id 匹配的 Trace。"""
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
        try:
            if trace_path.is_symlink() or not trace_path.is_file():
                return b""
            with trace_path.open("rb") as stream:
                stream.seek(offset)
                return stream.read(MAX_COMMAND_OUTPUT_BYTES)
        except OSError:
            return b""

    def _project_trace_line(self, raw_line: bytes) -> _ProjectedTraceEvent | None:
        """收窄、再次脱敏 Trace envelope 并保留 Demand 因果字段。"""
        try:
            raw_record: object = json.loads(raw_line)
        except (UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw_record, dict):
            return None
        raw_event = cast(dict[str, object], raw_record).get("event")
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
                    json.dumps(raw_payload, ensure_ascii=False, separators=(",", ":"))
                ),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        payload = self._redactor.redact_payload(payload)
        summary = event.get("result_summary") or event.get("input_summary")
        if isinstance(summary, str):
            payload.setdefault("summary", self._redactor.redact_text(summary))
        trace_event_id = event.get("event_id")
        if isinstance(trace_event_id, str):
            payload.setdefault("trace_event_id", trace_event_id)
        parent_event_id = event.get("parent_event_id")
        if isinstance(parent_event_id, str):
            payload.setdefault("parent_event_id", parent_event_id)
        stream_value = payload.pop("stream_id", None)
        stream = stream_value if isinstance(stream_value, str) else None
        return _ProjectedTraceEvent(event_type, payload, run_id, stream)

    @staticmethod
    async def _read_bounded(
        stream: asyncio.StreamReader,
        *,
        limit: int = MAX_COMMAND_OUTPUT_BYTES,
    ) -> tuple[bytes, bool]:
        """持续 drain 子进程输出，但只保留固定上限。"""
        content = bytearray()
        truncated = False
        while chunk := await stream.read(65_536):
            remaining = limit - len(content)
            if remaining > 0:
                content.extend(chunk[:remaining])
            if len(chunk) > max(remaining, 0):
                truncated = True
        return bytes(content), truncated

    @staticmethod
    async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
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

    def _decode_fix_proposal(
        self,
        execution: CommandExecution,
        *,
        expected_query: str,
        expected_read_scope: tuple[str, ...],
        expected_write_scope: tuple[str, ...],
        expected_allowed_new_paths: tuple[str, ...],
    ) -> _FixProposal:
        """严格校验只读提案未越过事务创建与确认边界。"""
        try:
            raw_payload: object = json.loads(execution.stdout)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise self._proposal_error("只读 FIX 未返回有效 JSON 提案") from error
        if not isinstance(raw_payload, dict):
            raise self._proposal_error("只读 FIX 提案不是 JSON 对象")
        payload = cast(dict[str, object], raw_payload)
        required_fields = {
            "acceptance",
            "acceptance_sha256",
            "base_commit",
            "confirmed",
            "investigation",
            "next_action",
            "run_id",
            "transaction_created",
        }
        if set(payload) != required_fields:
            raise self._proposal_error("只读 FIX 提案字段与协议不一致")
        if payload.get("confirmed") is not False:
            raise self._proposal_error("只读 FIX 提案错误地声明已确认")
        if payload.get("transaction_created") is not False:
            raise self._proposal_error("确认前禁止创建事务")
        acceptance_sha256 = self._proposal_text(
            payload,
            "acceptance_sha256",
            maximum=71,
        )
        if SHA256_PATTERN.fullmatch(acceptance_sha256) is None:
            raise self._proposal_error("只读 FIX 提案 acceptance_sha256 无效")
        base_commit = self._proposal_text(payload, "base_commit", maximum=40)
        if GIT_COMMIT_PATTERN.fullmatch(base_commit) is None:
            raise self._proposal_error("只读 FIX 提案 base_commit 无效")
        raw_acceptance = payload.get("acceptance")
        if not isinstance(raw_acceptance, dict):
            raise self._proposal_error("只读 FIX 提案缺少 AcceptanceSpec")
        from rivet.contracts.transactions import AcceptanceSpec
        from rivet.transaction.hashing import (
            acceptance_sha256 as compute_acceptance_sha256,
        )

        try:
            specification = AcceptanceSpec.model_validate_json(
                json.dumps(raw_acceptance, ensure_ascii=False)
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise self._proposal_error(
                "只读 FIX 提案包含无效 AcceptanceSpec"
            ) from error
        if not hmac.compare_digest(
            compute_acceptance_sha256(specification),
            acceptance_sha256,
        ):
            raise self._proposal_error("只读 FIX 提案哈希未绑定 AcceptanceSpec")
        goal = specification.user_goal
        if goal != expected_query:
            raise self._proposal_error("提案 Goal 未绑定当前 FIX 任务")
        if specification.scope_source != "explicit":
            raise self._proposal_error("提案写范围不是用户显式来源")
        read_scope = specification.read_scope
        write_scope = specification.write_scope or specification.allowed_paths
        allowed_new_paths = specification.allowed_new_paths
        if tuple(sorted(read_scope)) != tuple(sorted(expected_read_scope)):
            raise self._proposal_error("提案读范围与只读 Context/现有写范围不一致")
        if tuple(sorted(write_scope)) != tuple(sorted(expected_write_scope)):
            raise self._proposal_error("提案写范围与显式授权范围不一致")
        if tuple(sorted(allowed_new_paths)) != tuple(
            sorted(expected_allowed_new_paths)
        ):
            raise self._proposal_error("提案新建范围与 --new 授权不一致")
        acceptance_commands = specification.behavior_verification_commands
        regression_commands = specification.verification_commands
        investigation = self._proposal_text(
            payload,
            "investigation",
            maximum=MAX_TUI_QUERY_CHARS,
        )
        next_action = self._proposal_text(payload, "next_action", maximum=4_096)
        run_id = self._proposal_text(payload, "run_id", maximum=80)
        if RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise self._proposal_error("只读 FIX 提案 run_id 无效")
        redacted_acceptance = self._redactor.redact_payload(
            cast(dict[str, JsonValue], specification.model_dump(mode="json"))
        )
        budgets: dict[str, JsonValue] = {
            "max_cost_usd": (
                str(specification.max_cost_usd)
                if specification.max_cost_usd is not None
                else None
            ),
            "max_tokens": specification.max_tokens,
            "max_tool_calls": specification.max_tool_calls,
            "max_wall_seconds": specification.max_wall_seconds,
        }
        return _FixProposal(
            acceptance=redacted_acceptance,
            acceptance_sha256=acceptance_sha256,
            base_commit=base_commit,
            goal=self._redactor.redact_text(goal),
            read_scope=read_scope,
            write_scope=write_scope,
            allowed_new_paths=allowed_new_paths,
            forbidden_paths=specification.forbidden_paths,
            expected_behaviors=tuple(
                self._redactor.redact_text(item)
                for item in specification.expected_behaviors
            ),
            preserved_behaviors=tuple(
                self._redactor.redact_text(item)
                for item in specification.preserved_behaviors
            ),
            acceptance_commands=self._redact_commands(acceptance_commands),
            regression_commands=self._redact_commands(regression_commands),
            budgets=budgets,
            investigation=self._redactor.redact_text(investigation),
            next_action=self._redactor.redact_text(next_action),
            run_id=run_id,
        )

    @classmethod
    def _proposal_text(
        cls,
        payload: Mapping[str, object],
        key: str,
        *,
        maximum: int,
    ) -> str:
        value = payload.get(key)
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or len(value) > maximum
        ):
            raise cls._proposal_error(f"FIX 提案字段无效：{key}")
        return value

    def _redact_commands(
        self,
        commands: tuple[tuple[str, ...], ...],
    ) -> tuple[tuple[str, ...], ...]:
        return tuple(
            tuple(
                _terminal_text(self._redactor.redact_text(argument))
                for argument in command
            )
            for command in commands
        )

    @staticmethod
    def _proposal_error(summary: str) -> WorkerMethodError:
        return WorkerMethodError(
            "ipc.fix_proposal_invalid",
            summary,
            "保持工作区不变，升级 CLI/Worker 或重新发起 FIX",
        )

    def _decode_execution(self, execution: CommandExecution) -> dict[str, JsonValue]:
        """只接受正式 CLI 的单个 JSON 对象并再次脱敏。"""
        try:
            raw_payload: object = json.loads(execution.stdout)
        except (UnicodeError, json.JSONDecodeError) as error:
            if execution.return_code == 0:
                raise WorkerMethodError(
                    "ipc.command_output_invalid",
                    "命令未返回有效 JSON",
                    "使用 headless 命令诊断",
                ) from error
            return {}
        if not isinstance(raw_payload, dict):
            raise WorkerMethodError(
                "ipc.command_output_invalid",
                "命令返回的 JSON 不是对象",
                "升级 CLI 与 TUI 到同一版本",
            )
        payload = cast(dict[str, JsonValue], raw_payload)
        minimal = {
            key: value for key, value in payload.items() if key in COMMAND_RESULT_FIELDS
        }
        redacted = self._redactor.redact_payload(minimal)
        verification_results = redacted.get("verification_results")
        if isinstance(verification_results, list):
            for result in verification_results:
                if not isinstance(result, dict):
                    continue
                kind = result.get("kind")
                if isinstance(kind, str) and kind not in VERIFICATION_KINDS:
                    raise WorkerMethodError(
                        "ipc.command_output_invalid",
                        "命令返回了未知验证种类",
                        "升级 CLI 与 Worker 到同一最小协议版本",
                    )
        return redacted

    @staticmethod
    def _raise_execution_error(execution: CommandExecution) -> None:
        """提取 CLI 分类错误，不转发原始 stderr。"""
        try:
            raw_error: object = json.loads(execution.stderr)
        except (UnicodeError, json.JSONDecodeError):
            raw_error = None
        if isinstance(raw_error, dict):
            detail = cast(dict[str, object], raw_error).get("error")
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
        """把命令结果投影到最小 Timeline、Diff 与 Evidence 状态。"""
        answer = payload.get("answer")
        if isinstance(answer, str) and answer:
            model_status = payload.get("model_status", payload.get("status"))
            event_type = (
                "agent.answered"
                if model_status == "ANSWERED"
                else "agent.patch_ready"
                if model_status == "READY_FOR_VERIFICATION"
                else "agent.completed"
            )
            answer_payload: dict[str, JsonValue] = {
                "status": model_status if isinstance(model_status, str) else "UNKNOWN",
                "summary": answer,
            }
            run_id = payload.get("run_id")
            if isinstance(run_id, str):
                answer_payload["response_id"] = f"response_{run_id}"
            await emit(event_type, answer_payload)
        transaction_id = payload.get("transaction_id")
        if isinstance(transaction_id, str):
            await emit(
                "transaction.started",
                {"summary": "事务已更新", "transaction_id": transaction_id},
            )
        diff = payload.get("diff")
        if isinstance(diff, str):
            patch_payload: dict[str, JsonValue] = {
                "diff": diff,
                "summary": "补丁已更新",
            }
            if isinstance(transaction_id, str):
                patch_payload["transaction_id"] = transaction_id
            await emit("patch.updated", patch_payload)
        if command in {"fix", "verify"}:
            verification_status = payload.get(
                "verdict_status",
                payload.get("verification_status", payload.get("status")),
            )
            if isinstance(verification_status, str):
                await emit(
                    "verification.completed",
                    {"status": verification_status, "summary": "独立验证已完成"},
                )
        evidence_id = payload.get("evidence_id")
        if isinstance(evidence_id, str):
            evidence_payload: dict[str, JsonValue] = {
                "evidence_id": evidence_id,
                "summary": "证据已发布",
            }
            for key in (
                "acceptance_sha256",
                "base_commit",
                "changed_files",
                "manifest_sha256",
                "patch_sha256",
                "transaction_id",
                "verification_results",
            ):
                value = payload.get(key)
                if value is not None:
                    evidence_payload[key] = value
            await emit("evidence.published", evidence_payload)


def _unsafe_picker_path(path: str) -> bool:
    """拒绝绝对、穿越、内部状态、控制字符和疑似凭据路径。"""
    if "\\" in path:
        return True
    pure_path = PurePosixPath(path)
    lowered_parts = tuple(part.casefold() for part in pure_path.parts)
    if (
        not path
        or pure_path.is_absolute()
        or pure_path.as_posix() != path
        or any(part in {"", ".", ".."} for part in pure_path.parts)
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


def _terminal_text(value: str, *, maximum: int = 12_000) -> str:
    """移除会改变终端状态的字符，并限制弹窗文本大小。"""
    safe = "".join(
        character
        for character in value
        if character in {"\n", "\t"}
        or unicodedata.category(character) not in {"Cc", "Cf"}
    )
    if len(safe) <= maximum:
        return safe
    return f"{safe[:maximum]}…"
