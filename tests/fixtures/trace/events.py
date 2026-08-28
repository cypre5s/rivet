"""构造时间、ID 和载荷可重复的 Trace 测试事件。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import JsonValue

from rivet.contracts.events import TraceEventEnvelope

BASE_TIME = datetime(2026, 8, 28, tzinfo=UTC)


def make_event(
    sequence: int,
    *,
    event_type: str = "trace.observed",
    run_id: str = "run_trace_test",
    session_id: str = "session_trace_test",
    parent_event_id: str | None = None,
    transaction_id: str | None = None,
    payload: dict[str, JsonValue] | None = None,
) -> TraceEventEnvelope:
    """用确定性序号构造严格事件信封。"""
    return TraceEventEnvelope(
        event_id=f"event_trace_{sequence}",
        event_type=event_type,
        timestamp=BASE_TIME + timedelta(milliseconds=sequence),
        run_id=run_id,
        session_id=session_id,
        parent_event_id=parent_event_id,
        transaction_id=transaction_id,
        input_summary="固定输入摘要",
        result_summary="固定结果摘要",
        payload=payload or {"ordinal": sequence},
    )
