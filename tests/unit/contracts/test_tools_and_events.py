"""验证工具结果截断和 Trace 脱敏边界。"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from rivet.contracts.events import TraceEventEnvelope
from rivet.contracts.tools import ToolOutput, ToolResult


def test_tool_result_preserves_independent_truncation_flags() -> None:
    started_at = datetime(2026, 8, 28, tzinfo=UTC)
    result = ToolResult(
        tool_call_id="call_example",
        tool_name="file.read_text",
        success=True,
        output=ToolOutput(
            stdout="preview",
            stderr="",
            stdout_truncated=True,
            stderr_truncated=False,
        ),
        started_at=started_at,
        completed_at=started_at + timedelta(milliseconds=10),
    )

    restored = ToolResult.model_validate_json(result.model_dump_json())

    assert restored.output.stdout_truncated is True
    assert restored.output.stderr_truncated is False


def test_tool_result_rejects_success_with_error() -> None:
    started_at = datetime(2026, 8, 28, tzinfo=UTC)
    with pytest.raises(ValidationError):
        ToolResult.model_validate(
            {
                "schema_version": 1,
                "tool_call_id": "call_example",
                "tool_name": "file.read_text",
                "success": True,
                "output": {
                    "schema_version": 1,
                    "stdout": "",
                    "stderr": "",
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                },
                "error": {
                    "schema_version": 1,
                    "code": "tool.failed",
                    "summary": "失败",
                    "next_action": "检查参数",
                    "retryable": False,
                },
                "started_at": started_at,
                "completed_at": started_at,
            }
        )


def test_tool_output_rejects_oversized_preview_even_when_marked_truncated() -> None:
    with pytest.raises(ValidationError):
        ToolOutput(
            stdout="x" * 65_537,
            stderr="",
            stdout_truncated=True,
            stderr_truncated=False,
        )


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
