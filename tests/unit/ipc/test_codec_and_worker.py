"""验证 IPC 失败关闭、Worker 请求关联和取消回收。"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue

from rivet.contracts.ipc import IpcCancel, IpcRequest
from rivet.ipc.codec import IpcProtocolError, decode_ipc_line
from rivet.ipc.worker import (
    BaseWorkerApplication,
    EmitEvent,
    WorkerSession,
    read_bounded_ipc_line,
)


def _request(request_id: str, method: str) -> bytes:
    """生成一条合法请求。"""
    return (
        IpcRequest(request_id=request_id, method=method).model_dump_json() + "\n"
    ).encode()


def _messages(lines: list[str]) -> list[dict[str, object]]:
    """解析测试 writer 捕获的协议行。"""
    return [cast(dict[str, object], json.loads(line)) for line in lines]


async def _discard_event(_event_type: str, _payload: dict[str, JsonValue]) -> None:
    """为直接应用测试提供无副作用事件接收器。"""


@pytest.mark.parametrize(
    ("line", "code"),
    [
        (b"", "ipc.line_size_invalid"),
        (b"{}\n{}\n", "ipc.line_invalid"),
        (b"not-json\n", "ipc.json_invalid"),
        (
            b'{"schema_version":1,"protocol_version":2,"request_id":"request_bad"}',
            "ipc.protocol_mismatch",
        ),
        (b"{}\n", "ipc.protocol_mismatch"),
    ],
)
def test_codec_rejects_invalid_lines_without_echoing_input(
    line: bytes, code: str
) -> None:
    with pytest.raises(IpcProtocolError) as captured:
        decode_ipc_line(line)

    assert captured.value.code == code
    if line:
        assert line.decode(errors="ignore") not in str(captured.value)


def test_codec_rejects_oversized_and_invalid_contract() -> None:
    with pytest.raises(IpcProtocolError, match="超过上限"):
        decode_ipc_line(b"x" * (1024 * 1024 + 1))
    with pytest.raises(IpcProtocolError) as captured:
        decode_ipc_line(
            b'{"schema_version":1,"message_type":"request",'
            b'"protocol_version":1,"request_id":"request_ok",'
            b'"method":"bad","params":{}}\n'
        )
    assert captured.value.code == "ipc.message_invalid"


def test_bounded_reader_discards_oversized_line_tail() -> None:
    valid = _request("request_after_large", "worker.handshake")
    stream = io.BytesIO(b"x" * (1024 * 1024 + 100) + b"\n" + valid)

    oversized = read_bounded_ipc_line(stream)
    following = read_bounded_ipc_line(stream)

    assert len(oversized) == 1024 * 1024 + 1
    assert following == valid


@pytest.mark.asyncio
async def test_worker_handshake_ping_snapshot_duplicate_and_shutdown(
    tmp_path: Path,
) -> None:
    lines: list[str] = []

    async def write(message: str) -> None:
        """捕获完整协议行。"""
        lines.append(message)

    session = WorkerSession(tmp_path, write_message=write)
    await session.receive(_request("request_handshake", "worker.handshake"))
    await session.receive(_request("request_handshake_again", "worker.handshake"))
    await session.receive(_request("request_ping", "worker.ping"))
    await session.receive(_request("request_snapshot", "worker.snapshot"))
    await asyncio.sleep(0)
    await session.receive(_request("request_handshake", "worker.handshake"))
    await session.receive(_request("request_shutdown", "worker.shutdown"))
    await session.close()

    messages = _messages(lines)
    assert session.shutdown_requested is True
    assert any(message.get("event_type") == "worker.ready" for message in messages)
    assert any(message.get("event_type") == "worker.stopping" for message in messages)
    errors = [
        cast(dict[str, object], message["error"])
        for message in messages
        if message.get("ok") is False
    ]
    assert any(error["code"] == "ipc.request_duplicate" for error in errors)


@pytest.mark.asyncio
async def test_worker_rejects_unknown_method_and_sanitizes_internal_error(
    tmp_path: Path,
) -> None:
    lines: list[str] = []

    async def write(message: str) -> None:
        """捕获完整协议行。"""
        lines.append(message)

    class FailingApplication:
        """模拟包含敏感细节的未分类业务异常。"""

        async def handle(
            self,
            request: IpcRequest,
            *,
            emit: EmitEvent,
            cancel_event: asyncio.Event,
        ) -> JsonValue:
            """抛出不得跨越 Worker 边界的原始异常。"""
            raise RuntimeError("private-detail")

    base = WorkerSession(tmp_path, write_message=write)
    await base.receive(_request("request_handshake_one", "worker.handshake"))
    await base.receive(_request("request_unknown", "command.unknown"))
    await asyncio.sleep(0)
    await base.close()
    failing = WorkerSession(
        tmp_path,
        write_message=write,
        application=FailingApplication(),
    )
    await failing.receive(_request("request_handshake_two", "worker.handshake"))
    await failing.receive(_request("request_failing", "command.failing"))
    await asyncio.sleep(0)
    await failing.close()

    serialized = "".join(lines)
    assert "ipc.method_unknown" in serialized
    assert "ipc.worker_internal" in serialized
    assert "private-detail" not in serialized


@pytest.mark.asyncio
async def test_worker_cancel_reaches_inflight_request(tmp_path: Path) -> None:
    lines: list[str] = []
    started = asyncio.Event()

    async def write(message: str) -> None:
        """捕获完整协议行。"""
        lines.append(message)

    class WaitingApplication:
        """等待取消的长业务请求。"""

        async def handle(
            self,
            request: IpcRequest,
            *,
            emit: EmitEvent,
            cancel_event: asyncio.Event,
        ) -> JsonValue:
            """暴露启动边界后等待取消标记。"""
            started.set()
            await cancel_event.wait()
            return {"cancelled": True}

    session = WorkerSession(
        tmp_path,
        write_message=write,
        application=WaitingApplication(),
    )
    await session.receive(_request("request_handshake", "worker.handshake"))
    await session.receive(_request("request_running", "command.ask"))
    await started.wait()
    cancellation = IpcCancel(
        request_id="request_cancel",
        target_request_id="request_running",
    )
    await session.receive((cancellation.model_dump_json() + "\n").encode())
    await asyncio.sleep(0)
    await session.close()

    serialized = "".join(lines)
    assert "ipc.request_cancelled" in serialized
    assert '"request_id":"request_cancel"' in serialized


@pytest.mark.asyncio
async def test_worker_reports_missing_cancel_target_and_invalid_request(
    tmp_path: Path,
) -> None:
    lines: list[str] = []

    async def write(message: str) -> None:
        """捕获完整协议行。"""
        lines.append(message)

    session = WorkerSession(tmp_path, write_message=write)
    await session.receive(b"not-json\n")
    await session.receive(_request("request_handshake", "worker.handshake"))
    cancellation = IpcCancel(
        request_id="request_cancel",
        target_request_id="request_missing",
    )
    await session.receive((cancellation.model_dump_json() + "\n").encode())
    await session.close()

    assert "ipc.cancel_target_missing" in "".join(lines)


@pytest.mark.asyncio
async def test_base_application_snapshot_is_read_only(tmp_path: Path) -> None:
    application = BaseWorkerApplication(tmp_path)
    result = await application.handle(
        IpcRequest(request_id="request_snapshot", method="worker.snapshot"),
        emit=_discard_event,
        cancel_event=asyncio.Event(),
    )

    assert isinstance(result, dict)
    assert result["repository"] == str(tmp_path)
    assert result["inspector_tabs"] == [
        "Plan",
        "Context",
        "Diff",
        "Verify",
        "Evidence",
        "Modules",
    ]
