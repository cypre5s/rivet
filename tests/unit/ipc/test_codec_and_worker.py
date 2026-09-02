"""验证 IPC 编解码、关联、取消、背压和最小 Worker 生命周期。"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue

from rivet.contracts.ipc import IPC_APPLICATION_METHODS, IpcCancel, IpcRequest
from rivet.ipc.codec import MAX_IPC_LINE_BYTES, IpcProtocolError, decode_ipc_line
from rivet.ipc.worker import (
    BaseWorkerApplication,
    EmitEvent,
    WorkerSession,
    read_bounded_ipc_line,
)


def _request(
    request_id: str,
    method: str,
    params: dict[str, JsonValue] | None = None,
) -> bytes:
    return (
        IpcRequest(
            request_id=request_id,
            method=method,
            params=params or {},
        ).model_dump_json()
        + "\n"
    ).encode()


def _messages(lines: list[str]) -> list[dict[str, object]]:
    return [cast(dict[str, object], json.loads(line)) for line in lines]


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
    line: bytes,
    code: str,
) -> None:
    with pytest.raises(IpcProtocolError) as captured:
        decode_ipc_line(line)

    assert captured.value.code == code
    if line:
        assert line.decode(errors="ignore") not in str(captured.value)


def test_codec_rejects_oversized_and_invalid_contract() -> None:
    with pytest.raises(IpcProtocolError, match="超过上限"):
        decode_ipc_line(b"x" * (MAX_IPC_LINE_BYTES + 1))
    with pytest.raises(IpcProtocolError) as captured:
        decode_ipc_line(
            b'{"schema_version":1,"message_type":"request",'
            b'"protocol_version":1,"request_id":"request_ok",'
            b'"method":"bad","params":{}}\n'
        )
    assert captured.value.code == "ipc.message_invalid"


def test_bounded_reader_discards_oversized_line_tail() -> None:
    valid = _request("request_after_large", "worker.handshake")
    stream = io.BytesIO(b"x" * (MAX_IPC_LINE_BYTES + 100) + b"\n" + valid)

    oversized = read_bounded_ipc_line(stream)
    following = read_bounded_ipc_line(stream)

    assert len(oversized) == MAX_IPC_LINE_BYTES + 1
    assert following == valid


@pytest.mark.asyncio
async def test_worker_handshake_advertises_exact_surface_and_shutdown(
    tmp_path: Path,
) -> None:
    lines: list[str] = []

    async def write(message: str) -> None:
        lines.append(message)

    session = WorkerSession(tmp_path, write_message=write)
    await session.receive(
        _request(
            "request_handshake",
            "worker.handshake",
            {"client": "rivet-tui"},
        )
    )
    await session.receive(_request("request_removed_ping", "worker.ping"))
    await asyncio.sleep(0)
    await session.receive(_request("request_shutdown", "worker.shutdown"))
    await session.close()

    messages = _messages(lines)
    handshake = next(
        message
        for message in messages
        if message.get("request_id") == "request_handshake"
    )
    result = cast(dict[str, object], handshake["result"])
    assert result["methods"] == list(IPC_APPLICATION_METHODS)
    assert "snapshot" not in cast(list[str], result["capabilities"])
    assert session.shutdown_requested is True
    assert any(message.get("event_type") == "worker.ready" for message in messages)
    assert any(message.get("event_type") == "worker.stopping" for message in messages)
    removed = next(
        message
        for message in messages
        if message.get("request_id") == "request_removed_ping"
    )
    error = cast(dict[str, object], removed["error"])
    assert error["code"] == "ipc.method_unknown"


@pytest.mark.asyncio
async def test_worker_requires_valid_handshake_and_empty_shutdown_params(
    tmp_path: Path,
) -> None:
    lines: list[str] = []

    async def write(message: str) -> None:
        lines.append(message)

    session = WorkerSession(tmp_path, write_message=write)
    await session.receive(_request("request_early", "command.ask", {"query": "x"}))
    await session.receive(
        _request(
            "request_bad_handshake",
            "worker.handshake",
            {"unexpected": True},
        )
    )
    await session.receive(_request("request_handshake", "worker.handshake"))
    await session.receive(
        _request("request_bad_shutdown", "worker.shutdown", {"now": True})
    )
    await session.close()

    errors = {
        cast(str, message["request_id"]): cast(dict[str, object], message["error"])[
            "code"
        ]
        for message in _messages(lines)
        if message.get("ok") is False
    }
    assert errors == {
        "request_early": "ipc.handshake_required",
        "request_bad_handshake": "ipc.params_invalid",
        "request_bad_shutdown": "ipc.params_invalid",
    }


@pytest.mark.asyncio
async def test_worker_ready_uses_read_only_branch_and_never_exposes_credentials(
    tmp_path: Path,
) -> None:
    lines: list[str] = []
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text(
        "ref: refs/heads/feature/minimal-ipc\n",
        encoding="utf-8",
    )

    async def write(message: str) -> None:
        lines.append(message)

    session = WorkerSession(tmp_path, write_message=write)
    await session.receive(_request("request_handshake", "worker.handshake"))
    await session.close()

    ready = next(
        message
        for message in _messages(lines)
        if message.get("event_type") == "worker.ready"
    )
    payload = cast(dict[str, object], ready["payload"])
    assert payload == {
        "branch": "feature/minimal-ipc",
        "credential_configured": False,
        "model": "未配置",
        "repository": str(tmp_path),
    }


@pytest.mark.asyncio
async def test_worker_rejects_unknown_method_and_sanitizes_internal_error(
    tmp_path: Path,
) -> None:
    lines: list[str] = []

    async def write(message: str) -> None:
        lines.append(message)

    class FailingApplication:
        async def handle(
            self,
            request: IpcRequest,
            *,
            emit: EmitEvent,
            cancel_event: asyncio.Event,
        ) -> JsonValue:
            del request, emit, cancel_event
            raise RuntimeError("private-detail")

    failing = WorkerSession(
        tmp_path,
        write_message=write,
        application=FailingApplication(),
    )
    await failing.receive(_request("request_handshake", "worker.handshake"))
    await failing.receive(_request("request_failing", "command.ask"))
    await asyncio.sleep(0)
    await failing.close()

    serialized = "".join(lines)
    assert "ipc.worker_internal" in serialized
    assert "private-detail" not in serialized


@pytest.mark.asyncio
async def test_worker_cancel_is_correlated_and_cancel_id_cannot_be_reused(
    tmp_path: Path,
) -> None:
    lines: list[str] = []
    started = asyncio.Event()

    async def write(message: str) -> None:
        lines.append(message)

    class WaitingApplication:
        async def handle(
            self,
            request: IpcRequest,
            *,
            emit: EmitEvent,
            cancel_event: asyncio.Event,
        ) -> JsonValue:
            del request, emit
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
    await session.receive(_request("request_cancel", "command.ask"))
    await session.close()

    messages = _messages(lines)
    target = next(
        message
        for message in messages
        if message.get("request_id") == "request_running"
    )
    assert cast(dict[str, object], target["error"])["code"] == ("ipc.request_cancelled")
    duplicate = [
        message
        for message in messages
        if message.get("request_id") == "request_cancel" and message.get("ok") is False
    ]
    assert cast(dict[str, object], duplicate[0]["error"])["code"] == (
        "ipc.request_duplicate"
    )


@pytest.mark.asyncio
async def test_worker_reports_missing_cancel_target(tmp_path: Path) -> None:
    lines: list[str] = []

    async def write(message: str) -> None:
        lines.append(message)

    session = WorkerSession(tmp_path, write_message=write)
    await session.receive(_request("request_handshake", "worker.handshake"))
    cancellation = IpcCancel(
        request_id="request_cancel",
        target_request_id="request_missing",
    )
    await session.receive((cancellation.model_dump_json() + "\n").encode())
    await session.close()

    assert "ipc.cancel_target_missing" in "".join(lines)


@pytest.mark.asyncio
async def test_worker_applies_pending_request_backpressure(tmp_path: Path) -> None:
    lines: list[str] = []
    started = asyncio.Event()

    async def write(message: str) -> None:
        lines.append(message)

    class WaitingApplication:
        async def handle(
            self,
            request: IpcRequest,
            *,
            emit: EmitEvent,
            cancel_event: asyncio.Event,
        ) -> JsonValue:
            del request, emit
            started.set()
            await cancel_event.wait()
            return None

    session = WorkerSession(
        tmp_path,
        write_message=write,
        application=WaitingApplication(),
        max_pending_requests=1,
    )
    await session.receive(_request("request_handshake", "worker.handshake"))
    await session.receive(_request("request_first", "command.ask"))
    await started.wait()
    await session.receive(_request("request_second", "command.diff"))
    await session.close()

    response = next(
        message
        for message in _messages(lines)
        if message.get("request_id") == "request_second"
    )
    error = cast(dict[str, object], response["error"])
    assert error["code"] == "ipc.backpressure"
    assert error["retryable"] is True


@pytest.mark.asyncio
async def test_worker_bounds_request_id_history_per_connection(tmp_path: Path) -> None:
    lines: list[str] = []

    async def write(message: str) -> None:
        lines.append(message)

    session = WorkerSession(
        tmp_path,
        write_message=write,
        max_request_ids=2,
    )
    await session.receive(_request("request_handshake", "worker.handshake"))
    await session.receive(_request("request_unknown", "worker.ping"))
    await asyncio.sleep(0)
    await session.receive(_request("request_limit", "worker.shutdown"))
    await session.close()

    response = next(
        message
        for message in _messages(lines)
        if message.get("request_id") == "request_limit"
    )
    assert cast(dict[str, object], response["error"])["code"] == (
        "ipc.connection_request_limit"
    )
    assert session.shutdown_requested is True


@pytest.mark.asyncio
async def test_concurrent_events_are_serialized_with_monotonic_sequence(
    tmp_path: Path,
) -> None:
    lines: list[str] = []
    writers = 0
    peak_writers = 0

    async def write(message: str) -> None:
        nonlocal writers, peak_writers
        writers += 1
        peak_writers = max(peak_writers, writers)
        await asyncio.sleep(0)
        lines.append(message)
        writers -= 1

    class StreamingApplication:
        async def handle(
            self,
            request: IpcRequest,
            *,
            emit: EmitEvent,
            cancel_event: asyncio.Event,
        ) -> JsonValue:
            del request, cancel_event
            await asyncio.gather(
                *(emit("agent.output.delta", {"index": index}) for index in range(20))
            )
            return {"status": "done"}

    session = WorkerSession(
        tmp_path,
        write_message=write,
        application=StreamingApplication(),
    )
    await session.receive(_request("request_handshake", "worker.handshake"))
    await session.receive(_request("request_stream", "command.ask"))
    for _ in range(100):
        if any(
            message.get("request_id") == "request_stream"
            for message in _messages(lines)
        ):
            break
        await asyncio.sleep(0)
    await session.close()

    events = [
        message
        for message in _messages(lines)
        if message.get("message_type") == "event"
    ]
    sequences = [cast(int, event["sequence"]) for event in events]
    assert peak_writers == 1
    assert sequences == list(range(len(sequences)))
    assert len(set(cast(str, event["event_id"]) for event in events)) == len(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized_kind", ["event", "response"])
async def test_worker_bounds_every_outbound_message(
    tmp_path: Path,
    oversized_kind: str,
) -> None:
    lines: list[str] = []

    async def write(message: str) -> None:
        lines.append(message)

    class OversizedApplication:
        async def handle(
            self,
            request: IpcRequest,
            *,
            emit: EmitEvent,
            cancel_event: asyncio.Event,
        ) -> JsonValue:
            del request, cancel_event
            huge = "x" * (MAX_IPC_LINE_BYTES + 1)
            if oversized_kind == "event":
                await emit("agent.output.delta", {"summary": huge})
                return {"status": "unreachable"}
            return {"value": huge}

    session = WorkerSession(
        tmp_path,
        write_message=write,
        application=OversizedApplication(),
    )
    await session.receive(_request("request_handshake", "worker.handshake"))
    await session.receive(_request("request_large", "command.ask"))
    for _ in range(100):
        if any(
            message.get("request_id") == "request_large" for message in _messages(lines)
        ):
            break
        await asyncio.sleep(0)
    await session.close()

    response = next(
        message
        for message in _messages(lines)
        if message.get("request_id") == "request_large"
    )
    error = cast(dict[str, object], response["error"])
    assert error["code"] == (
        "ipc.event_too_large" if oversized_kind == "event" else "ipc.response_too_large"
    )
    assert all(len(line.encode()) <= MAX_IPC_LINE_BYTES for line in lines)


@pytest.mark.asyncio
async def test_worker_close_releases_closable_application(tmp_path: Path) -> None:
    closed = False

    async def write(_message: str) -> None:
        return None

    class ClosableApplication(BaseWorkerApplication):
        async def close(self) -> None:
            nonlocal closed
            closed = True

    session = WorkerSession(
        tmp_path,
        write_message=write,
        application=ClosableApplication(tmp_path),
    )
    await session.receive(_request("request_handshake", "worker.handshake"))
    await session.close()

    assert closed is True
