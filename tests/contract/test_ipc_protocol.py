"""验证 Phase 12 IPC 的取消消息与严格联合解析。"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from rivet.contracts.ipc import (
    IPC_APPLICATION_METHODS,
    IPC_CONTROL_METHODS,
    IPC_REQUEST_METHODS,
    IpcCancel,
    IpcMessage,
)


def test_protocol_v1_has_only_the_minimal_request_surface() -> None:
    assert IPC_CONTROL_METHODS == ("worker.handshake", "worker.shutdown")
    assert IPC_APPLICATION_METHODS == (
        "command.ask",
        "command.fix",
        "command.diff",
        "command.verify",
        "command.apply",
        "command.abort",
        "permission.resolve",
        "workspace.files",
        "transactions.list",
        "evidence.get",
        "evidence.log",
    )
    assert (*IPC_CONTROL_METHODS, *IPC_APPLICATION_METHODS) == IPC_REQUEST_METHODS
    rendered = "\n".join(IPC_REQUEST_METHODS)
    for removed in (
        "benchmark",
        "candidate",
        "config",
        "context.read",
        "export",
        "module",
        "plan",
        "reader",
        "resume",
        "session",
        "trace.query",
    ):
        assert removed not in rendered


def test_cancel_message_has_independent_and_target_request_ids() -> None:
    message = IpcCancel(
        request_id="request_cancel_one",
        target_request_id="request_running_one",
    )

    parsed = TypeAdapter[IpcMessage](IpcMessage).validate_python(message.model_dump())

    assert isinstance(parsed, IpcCancel)
    assert parsed.request_id != parsed.target_request_id


def test_cancel_rejects_self_target_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        IpcCancel(
            request_id="request_same",
            target_request_id="request_same",
        )
    with pytest.raises(ValidationError):
        IpcCancel.model_validate(
            {
                "request_id": "request_cancel_two",
                "target_request_id": "request_running_two",
                "unexpected": True,
            }
        )
