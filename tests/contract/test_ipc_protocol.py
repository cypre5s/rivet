"""验证 Phase 12 IPC 的取消消息与严格联合解析。"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from rivet.contracts.ipc import IpcCancel, IpcMessage


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
