"""把 append-only 事件流归约为可比较的运行状态与指标。"""

from __future__ import annotations

from datetime import datetime

from pydantic import JsonValue

from rivet.contracts.common import RunId
from rivet.trace.errors import TraceReplayError
from rivet.trace.models import PersistedTraceEvent, TraceState


def _json_integer(value: JsonValue | None) -> int | None:
    """只接受非布尔整数，避免宽松转换污染指标。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _json_number(value: JsonValue | None) -> float | None:
    """只接受非布尔数值。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


class TraceReducer:
    """按 sequence 单调应用同一 run 的事件。"""

    def __init__(self, run_id: RunId) -> None:
        self.run_id = run_id
        self._event_count = 0
        self._last_sequence = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_tokens = 0
        self._tool_call_count = 0
        self._total_duration_ms = 0.0
        self._event_type_counts: dict[str, int] = {}
        self._module_states: dict[str, str] = {}
        self._transaction_states: dict[str, str] = {}
        self._module_residence_ms: dict[str, int] = {}
        self._module_activated_at: dict[str, datetime] = {}

    def apply(self, record: PersistedTraceEvent) -> None:
        """校验 run 与顺序后更新全部可重放指标。"""
        event = record.event
        if event.run_id != self.run_id:
            raise TraceReplayError(
                f"Reducer {self.run_id} 收到其他 run 事件 {event.run_id}"
            )
        if record.sequence <= self._last_sequence:
            raise TraceReplayError("Trace sequence 必须严格递增")
        self._event_count += 1
        self._last_sequence = record.sequence
        self._event_type_counts[event.event_type] = (
            self._event_type_counts.get(event.event_type, 0) + 1
        )

        input_tokens = _json_integer(event.payload.get("input_tokens")) or 0
        output_tokens = _json_integer(event.payload.get("output_tokens")) or 0
        explicit_total = _json_integer(event.payload.get("total_tokens"))
        self._input_tokens += max(0, input_tokens)
        self._output_tokens += max(0, output_tokens)
        self._total_tokens += max(
            0,
            explicit_total
            if explicit_total is not None
            else input_tokens + output_tokens,
        )
        duration_ms = _json_number(event.payload.get("duration_ms"))
        if duration_ms is not None:
            self._total_duration_ms += max(0.0, duration_ms)
        if event.event_type == "tool.started":
            self._tool_call_count += 1
        self._apply_module_state(record)
        self._apply_transaction_state(record)

    def snapshot(self) -> TraceState:
        """冻结当前状态，使用排序字典保证 JSON 输出稳定。"""
        return TraceState(
            run_id=self.run_id,
            event_count=self._event_count,
            last_sequence=self._last_sequence,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            total_tokens=self._total_tokens,
            tool_call_count=self._tool_call_count,
            total_duration_ms=self._total_duration_ms,
            event_type_counts=dict(sorted(self._event_type_counts.items())),
            module_states=dict(sorted(self._module_states.items())),
            transaction_states=dict(sorted(self._transaction_states.items())),
            module_residence_ms=dict(sorted(self._module_residence_ms.items())),
        )

    def _apply_module_state(self, record: PersistedTraceEvent) -> None:
        """根据 module 事件维护最新状态与累计驻留时间。"""
        event = record.event
        module_id = event.payload.get("module_id")
        state = event.payload.get("state")
        if not event.event_type.startswith("module."):
            return
        if not isinstance(module_id, str) or not isinstance(state, str):
            return
        normalized_state = state.upper()
        self._module_states[module_id] = normalized_state
        if normalized_state == "ACTIVE":
            self._module_activated_at[module_id] = event.timestamp
            return
        activated_at = self._module_activated_at.pop(module_id, None)
        if activated_at is None:
            return
        duration_ms = max(
            0,
            int((event.timestamp - activated_at).total_seconds() * 1_000),
        )
        self._module_residence_ms[module_id] = (
            self._module_residence_ms.get(module_id, 0) + duration_ms
        )

    def _apply_transaction_state(self, record: PersistedTraceEvent) -> None:
        """维护 transaction 的最新可回放状态。"""
        event = record.event
        if event.transaction_id is None:
            return
        state = event.payload.get("transaction_state")
        if isinstance(state, str):
            self._transaction_states[event.transaction_id] = state.upper()
