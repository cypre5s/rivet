"""把 TUI 命令路由到同一正式 CLI，并在 Worker 内仲裁权限。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from rivet.contracts.ipc import IpcRequest
from rivet.ipc.worker import (
    BaseWorkerApplication,
    EmitEvent,
    WorkerMethodError,
)

MAX_COMMAND_OUTPUT_BYTES = 8 * 1024 * 1024
CommandRunner = Callable[[tuple[str, ...]], Awaitable["CommandExecution"]]


@dataclass(frozen=True, slots=True)
class CommandExecution:
    """保存子 CLI 的有界退出事实。"""

    return_code: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool = False
    stderr_truncated: bool = False


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

    async def handle(
        self,
        request: IpcRequest,
        *,
        emit: EmitEvent,
        cancel_event: asyncio.Event,
    ) -> JsonValue:
        """处理基础方法、权限决议和六个 TUI 正式命令。"""
        if request.method in {"worker.ping", "worker.snapshot"}:
            return await super().handle(
                request,
                emit=emit,
                cancel_event=cancel_event,
            )
        if request.method == "permission.resolve":
            return await self._resolve_permission(request, emit=emit)
        command = request.method.removeprefix("command.")
        if request.method not in {
            "command.ask",
            "command.plan",
            "command.fix",
            "command.verify",
            "command.diff",
            "command.apply",
        }:
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
            arguments = self._arguments(request, command)
            if command == "fix":
                await self._request_fix_permission(
                    emit=emit,
                    cancel_event=cancel_event,
                )
                arguments = (*arguments, "--yes")
            execution = await self._runner(arguments)
            payload = self._decode_execution(execution)
            if execution.return_code not in {0, 4}:
                self._raise_execution_error(execution)
            await self._emit_payload(command, payload, emit=emit)
            await emit(
                "plan.updated",
                {"phase": "IDLE", "summary": f"{command} 已结束"},
            )
            return cast(JsonValue, payload)

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

    def _arguments(self, request: IpcRequest, command: str) -> tuple[str, ...]:
        """严格把 JSON 参数转成无 shell argv。"""
        prefix = (
            sys.executable,
            "-m",
            "rivet",
            "--repository",
            str(self._repository),
            "--json",
            command,
        )
        if command in {"ask", "plan", "fix"}:
            query = request.params.get("query")
            if not isinstance(query, str) or not query or len(query) > 65_536:
                raise WorkerMethodError(
                    "task.query_invalid",
                    "任务文本为空或超过长度上限",
                    "输入明确且有界的任务文本",
                )
            return (*prefix, query)
        transaction_id = request.params.get("transaction_id")
        if transaction_id is None and command != "apply":
            return prefix
        if not isinstance(transaction_id, str) or not transaction_id:
            raise WorkerMethodError(
                "transaction.id_invalid",
                "事务 ID 无效",
                "刷新事务状态并使用有效 TX_ID",
            )
        return (*prefix, transaction_id)

    async def _run_subprocess(self, argv: tuple[str, ...]) -> CommandExecution:
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
        try:
            await process.wait()
        except asyncio.CancelledError:
            await self._terminate_process_group(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        stdout_result, stderr_result = await asyncio.gather(
            stdout_task,
            stderr_task,
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
