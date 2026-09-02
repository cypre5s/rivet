"""验证最小工具契约和 Trace 脱敏边界。"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

import rivet.contracts.common as common_contracts
import rivet.contracts.tools as tool_contracts
from rivet.contracts.events import TraceEventEnvelope
from rivet.contracts.tools import SideEffectClass


def test_tool_contract_surface_contains_no_legacy_result_or_artifact_models() -> None:
    assert set(SideEffectClass) == {
        SideEffectClass.READ_ONLY,
        SideEffectClass.TRANSACTIONAL_WRITE,
        SideEffectClass.LOCAL_PROCESS,
    }
    for name in ("ToolOutput", "ToolError", "ToolResult"):
        assert not hasattr(tool_contracts, name)
    assert not hasattr(common_contracts, "ArtifactReference")


@pytest.mark.parametrize(
    "payload",
    [
        {"environment": {"DEEPSEEK_API_KEY": "redacted"}},
        {"http": {"authorization": "redacted"}},
        {"headers": {"x-safe": "value"}},
        {"nested": [{"access_token": "redacted"}]},
    ],
)
def test_trace_event_rejects_secret_bearing_fields(payload: object) -> None:
    with pytest.raises(ValidationError):
        TraceEventEnvelope.model_validate(
            {
                "schema_version": 1,
                "event_id": "event_example",
                "event_type": "tool.completed",
                "timestamp": datetime(2026, 8, 28, tzinfo=UTC),
                "run_id": "run_example",
                "session_id": "session_example",
                "payload": payload,
            }
        )


def test_trace_event_accepts_token_count_metric() -> None:
    event = TraceEventEnvelope(
        event_id="event_example",
        event_type="model.completed",
        timestamp=datetime(2026, 8, 28, tzinfo=UTC),
        run_id="run_example",
        session_id="session_example",
        payload={"token_count": 12},
    )

    assert event.payload == {"token_count": 12}
