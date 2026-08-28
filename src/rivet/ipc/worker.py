"""通过 stdout 纯 NDJSON 提供可取消、可注入业务网关的 Python Worker。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import BinaryIO, Protocol

from pydantic import JsonValue

from rivet.contracts.common import ErrorDetail
from rivet.contracts.ipc import IpcCancel, IpcEvent, IpcRequest, IpcResponse
from rivet.ipc.codec import MAX_IPC_LINE_BYTES, IpcProtocolError, decode_ipc_line

WriteMessage = Callable[[str], Awaitable[None]]
EmitEvent = Callable[[str, dict[str, JsonValue]], Awaitable[None]]


class WorkerMethodError(RuntimeError):
    """表示业务方法拒绝，且不向 IPC 泄露异常细节。"""

    def __init__(self, code: str, summary: str, next_action: str) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary
        self.next_action = next_action


class WorkerApplication(Protocol):
    """隔离 IPC 传输与 Phase 13 命令编排实现。"""

    async def handle(
        self,
        request: IpcRequest,
        *,
        emit: EmitEvent,
        cancel_event: asyncio.Event,
    ) -> JsonValue:
        """处理一个已握手请求，并可发布结构化事件。"""
        ...


class BaseWorkerApplication:
    """提供不执行 Agent 副作用的健康检查与初始快照。"""

    def __init__(self, repository: Path) -> None:
        self._repository = repository.resolve(strict=True)

    async def handle(
        self,
        request: IpcRequest,
        *,
        emit: EmitEvent,
        cancel_event: asyncio.Event,
    ) -> JsonValue:
        """处理基础方法；未知业务命令明确留给 Phase 13 网关。"""
        if request.method == "worker.ping":
            return "pong"
        if request.method == "worker.snapshot":
            return {
                "connection": "ready",
                "repository": str(self._repository),
                "phase": "IDLE",
                "inspector_tabs": [
                    "Plan",
                    "Context",
                    "Diff",
                    "Verify",
                    "Evidence",
                    "Modules",
                ],
            }
        raise WorkerMethodError(
            "ipc.method_unknown",
            "Worker 方法未注册",
            "检查客户端版本或使用已公布的方法",
        )


class WorkerSession:
    """强制先握手、请求 ID 唯一、取消可达和有界关闭。"""

    def __init__(
        self,
        repository: Path,
        *,
        write_message: WriteMessage,
        application: WorkerApplication | None = None,
    ) -> None:
        self._repository = repository.resolve(strict=True)
        self._write_message = write_message
        self._application = application or BaseWorkerApplication(self._repository)
        self._pending: dict[str, tuple[asyncio.Task[None], asyncio.Event]] = {}
        self._seen_request_ids: set[str] = set()
        self._handshaken = False
        self._sequence = 0
        self._event_counter = 0
        self.shutdown_requested = False

    async def receive(self, line: bytes) -> None:
        """接收一行消息，并让长请求在后台运行以保持取消可达。"""
        try:
            message = decode_ipc_line(line)
        except IpcProtocolError as error:
            if error.request_id is not None:
                await self._send_error(
                    error.request_id,
                    code=error.code,
                    summary=error.summary,
                    next_action="升级客户端或检查 IPC 输入",
                )
            return
        if isinstance(message, IpcCancel):
            await self._cancel(message)
            return
        if not isinstance(message, IpcRequest):
            return
        if message.request_id in self._seen_request_ids:
            await self._send_error(
                message.request_id,
                code="ipc.request_duplicate",
                summary="请求 ID 已使用",
                next_action="为每次请求生成新的 ID",
            )
            return
        self._seen_request_ids.add(message.request_id)
        if not self._handshaken:
            if message.method != "worker.handshake":
                await self._send_error(
                    message.request_id,
                    code="ipc.handshake_required",
                    summary="业务请求前必须完成握手",
                    next_action="先调用 worker.handshake",
                )
                return
            await self._handshake(message)
            return
        if message.method == "worker.handshake":
            await self._send_error(
                message.request_id,
                code="ipc.handshake_duplicate",
                summary="当前连接已经完成握手",
                next_action="继续使用现有连接",
            )
            return
        if message.method == "worker.shutdown":
            await self._send_success(message.request_id, {"status": "stopping"})
            await self.emit("worker.stopping", {})
            self.shutdown_requested = True
            return
        cancel_event = asyncio.Event()
        task = asyncio.create_task(self._run_request(message, cancel_event))
        self._pending[message.request_id] = (task, cancel_event)
        task.add_done_callback(
            lambda completed, request_id=message.request_id: self._finish_task(
                request_id, completed
            )
        )

    async def emit(self, event_type: str, payload: dict[str, JsonValue]) -> None:
        """按单调 sequence 发布一个可由 reducer 消费的事件。"""
        event = IpcEvent(
            event_id=f"event_worker_{self._event_counter:08d}",
            event_type=event_type,
            sequence=self._sequence,
            payload=payload,
        )
        self._event_counter += 1
        self._sequence += 1
        await self._write(event.model_dump_json() + "\n")

    async def close(self) -> None:
        """有界取消所有在途请求并等待任务回收。"""
        pending = tuple(self._pending.values())
        for task, cancel_event in pending:
            cancel_event.set()
            task.cancel()
        if pending:
            await asyncio.gather(*(task for task, _ in pending), return_exceptions=True)
        self._pending.clear()

    async def _handshake(self, request: IpcRequest) -> None:
        """返回协议能力后才把连接标记为可用。"""
        self._handshaken = True
        await self._send_success(
            request.request_id,
            {
                "status": "ready",
                "protocol_version": 1,
                "capabilities": [
                    "events",
                    "cancel",
                    "snapshot",
                    "commands",
                    "permissions",
                ],
            },
        )
        await self.emit(
            "worker.ready",
            {"repository": str(self._repository), "model": "未配置"},
        )

    async def _run_request(
        self, request: IpcRequest, cancel_event: asyncio.Event
    ) -> None:
        """执行注入应用并把分类结果关联回原请求。"""
        try:
            result = await self._application.handle(
                request,
                emit=self.emit,
                cancel_event=cancel_event,
            )
        except asyncio.CancelledError:
            await self._send_error(
                request.request_id,
                code="ipc.request_cancelled",
                summary="请求已取消",
                next_action="可修改任务后重新提交",
            )
        except WorkerMethodError as error:
            await self._send_error(
                request.request_id,
                code=error.code,
                summary=error.summary,
                next_action=error.next_action,
            )
        except Exception:
            await self._send_error(
                request.request_id,
                code="ipc.worker_internal",
                summary="Worker 发生已脱敏内部错误",
                next_action="查看 stderr 诊断并恢复会话",
            )
        else:
            await self._send_success(request.request_id, result)

    async def _cancel(self, message: IpcCancel) -> None:
        """只取消明确目标，不把未知目标视为成功。"""
        pending = self._pending.get(message.target_request_id)
        if pending is None:
            await self._send_error(
                message.request_id,
                code="ipc.cancel_target_missing",
                summary="待取消请求不存在或已经完成",
                next_action="刷新当前运行状态",
            )
            return
        task, cancel_event = pending
        cancel_event.set()
        task.cancel()
        await self._send_success(
            message.request_id,
            {"target_request_id": message.target_request_id},
        )

    def _finish_task(self, request_id: str, task: asyncio.Task[None]) -> None:
        """移除已完成任务并消费异常以避免后台告警。"""
        self._pending.pop(request_id, None)
        if not task.cancelled():
            task.exception()

    async def _send_success(self, request_id: str, result: JsonValue) -> None:
        """发送带原请求 ID 的成功响应。"""
        response = IpcResponse(request_id=request_id, ok=True, result=result)
        await self._write(response.model_dump_json() + "\n")

    async def _send_error(
        self,
        request_id: str,
        *,
        code: str,
        summary: str,
        next_action: str,
    ) -> None:
        """发送不含 Python 堆栈和原始输入的失败响应。"""
        response = IpcResponse(
            request_id=request_id,
            ok=False,
            error=ErrorDetail(
                code=code,
                summary=summary,
                next_action=next_action,
                retryable=False,
            ),
        )
        await self._write(response.model_dump_json() + "\n")

    async def _write(self, message: str) -> None:
        """确保每次只把完整协议行交给唯一 writer。"""
        await self._write_message(message)


async def run_stdio_worker(repository: Path) -> int:
    """运行 stdin/stdout Worker，并把普通诊断完全留在 stderr。"""
    write_lock = asyncio.Lock()

    async def write_message(message: str) -> None:
        """串行写完整 UTF-8 行并立即 flush。"""
        async with write_lock:
            sys.stdout.buffer.write(message.encode("utf-8"))
            sys.stdout.buffer.flush()

    from rivet.ipc.command_application import CommandWorkerApplication

    session = WorkerSession(
        repository,
        write_message=write_message,
        application=CommandWorkerApplication(repository),
    )
    try:
        while not session.shutdown_requested:
            line = await asyncio.to_thread(read_bounded_ipc_line, sys.stdin.buffer)
            if not line:
                break
            await session.receive(line)
        await asyncio.sleep(0)
    finally:
        await session.close()
    return 0


def read_bounded_ipc_line(stream: BinaryIO) -> bytes:
    """有界读取一行，并在超限时丢弃至换行边界。"""
    line = stream.readline(MAX_IPC_LINE_BYTES + 1)
    if len(line) <= MAX_IPC_LINE_BYTES or line.endswith((b"\n", b"\r")):
        return line
    while True:
        remainder = stream.readline(MAX_IPC_LINE_BYTES + 1)
        if not remainder or remainder.endswith((b"\n", b"\r")):
            return line
