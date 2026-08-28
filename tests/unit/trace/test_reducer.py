"""验证在线指标 reducer 的 token、工具与模块驻留聚合。"""

from __future__ import annotations

from rivet.trace.models import PersistedTraceEvent
from rivet.trace.reducer import TraceReducer
from tests.fixtures.trace.events import make_event


def test_reducer_aggregates_metrics_and_state_deterministically() -> None:
    reducer = TraceReducer("run_trace_test")
    events = (
        make_event(
            1,
            event_type="module.activated",
            payload={"module_id": "reader.text", "state": "ACTIVE"},
        ),
        make_event(
            2,
            event_type="tool.started",
            payload={"input_tokens": 3, "output_tokens": 5, "duration_ms": 7},
        ),
        make_event(
            11,
            event_type="module.slept",
            payload={"module_id": "reader.text", "state": "SLEEPING"},
        ),
    )

    for sequence, event in enumerate(events, start=1):
        reducer.apply(PersistedTraceEvent(sequence=sequence, event=event))

    state = reducer.snapshot()
    assert state.event_count == 3
    assert state.input_tokens == 3
    assert state.output_tokens == 5
    assert state.total_tokens == 8
    assert state.tool_call_count == 1
    assert state.total_duration_ms == 7
    assert state.module_states == {"reader.text": "SLEEPING"}
    assert state.module_residence_ms == {"reader.text": 10}
