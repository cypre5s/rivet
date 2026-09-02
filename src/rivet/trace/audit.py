"""独立计算 Demand Traceability 与 Orphan Activation 门禁。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from rivet.kernel.capability_demand import CapabilityDemandSource
from rivet.trace.models import DEFAULT_MAX_EVENT_BYTES, PersistedTraceEvent


@dataclass(frozen=True, slots=True)
class DemandTraceAudit:
    """保存可直接进入 CI 的因果完整性指标。"""

    event_count: int
    demand_count: int
    valid_demand_count: int
    activation_count: int
    traceable_activation_count: int
    orphan_activation_count: int
    demand_traceability_percent: float
    violations: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.violations


@dataclass(frozen=True, slots=True)
class _DemandFact:
    event_id: str
    sequence: int
    source: CapabilityDemandSource
    capability_id: str
    context: tuple[str, str, str | None]


def load_trace(path: Path) -> tuple[PersistedTraceEvent, ...]:
    """严格读取完整、有界的 NDJSON，不执行尾部恢复或修改。"""
    records: list[PersistedTraceEvent] = []
    with path.open("rb") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.endswith(b"\n") or len(line) > DEFAULT_MAX_EVENT_BYTES:
                raise ValueError(f"第 {line_number} 行不完整或超大")
            try:
                records.append(PersistedTraceEvent.model_validate_json(line))
            except ValidationError as error:
                raise ValueError(f"第 {line_number} 行不是合法 Trace 事件") from error
    return tuple(records)


def audit_demand_trace(
    records: tuple[PersistedTraceEvent, ...],
) -> DemandTraceAudit:
    """逐序验证 Demand 父链和每个真实 activation 的耐久归因。"""
    violations: list[str] = []
    event_runs: dict[str, str] = {}
    demands: dict[str, _DemandFact] = {}
    demand_count = 0
    activation_count = 0
    traceable_activation_count = 0

    for expected_sequence, record in enumerate(records, start=1):
        event = record.event
        if record.sequence != expected_sequence:
            violations.append("trace.sequence_invalid")
        if event.event_id in event_runs:
            violations.append("trace.event_id_duplicate")
        parent_event_id = event.parent_event_id
        if (
            parent_event_id is not None
            and event_runs.get(parent_event_id) != event.run_id
        ):
            violations.append("trace.parent_invalid")
        event_runs[event.event_id] = event.run_id

        if event.event_type == "demand.created":
            demand_count += 1
            _index_demand(record, demands=demands, violations=violations)
            continue
        if event.event_type != "module.activated":
            continue
        activation_count += 1
        if _activation_is_traceable(record, demands=demands):
            traceable_activation_count += 1
        else:
            violations.append("activation.orphan")

    orphan_activation_count = activation_count - traceable_activation_count
    traceability = (
        100.0
        if activation_count == 0
        else 100.0 * traceable_activation_count / activation_count
    )
    return DemandTraceAudit(
        event_count=len(records),
        demand_count=demand_count,
        valid_demand_count=len(demands),
        activation_count=activation_count,
        traceable_activation_count=traceable_activation_count,
        orphan_activation_count=orphan_activation_count,
        demand_traceability_percent=traceability,
        violations=tuple(violations),
    )


def _index_demand(
    record: PersistedTraceEvent,
    *,
    demands: dict[str, _DemandFact],
    violations: list[str],
) -> None:
    event = record.event
    payload = event.payload
    demand_id = payload.get("demand_id")
    source_value = payload.get("demand_source")
    parent_demand_id = payload.get("parent_demand_id")
    capability_id = payload.get("capability_id")
    reason = payload.get("reason")
    if (
        not isinstance(demand_id, str)
        or not demand_id
        or demand_id in demands
        or not isinstance(source_value, str)
        or not isinstance(capability_id, str)
        or not capability_id
        or (parent_demand_id is not None and not isinstance(parent_demand_id, str))
    ):
        violations.append("demand.payload_invalid")
        return
    if not isinstance(reason, str) or not reason.strip() or reason != reason.strip():
        violations.append("demand.reason_invalid")
        return
    try:
        source = CapabilityDemandSource(source_value)
    except ValueError:
        violations.append("demand.source_invalid")
        return
    context = (event.run_id, event.session_id, event.transaction_id)
    if source is CapabilityDemandSource.USER_EXPLICIT:
        if parent_demand_id is not None or event.parent_event_id is not None:
            violations.append("demand.root_invalid")
            return
    else:
        parent = demands.get(cast(str, parent_demand_id))
        if (
            parent is None
            or parent.context != context
            or event.parent_event_id != parent.event_id
        ):
            violations.append("demand.parent_invalid")
            return
        if source is CapabilityDemandSource.MODEL_TOOL_CALL:
            operation_id = payload.get("operation_id")
            if not isinstance(operation_id, str) or not operation_id:
                violations.append("demand.operation_id_missing")
                return
    demands[demand_id] = _DemandFact(
        event_id=event.event_id,
        sequence=record.sequence,
        source=source,
        capability_id=capability_id,
        context=context,
    )


def _activation_is_traceable(
    record: PersistedTraceEvent,
    *,
    demands: dict[str, _DemandFact],
) -> bool:
    event = record.event
    payload = event.payload
    demand_id = payload.get("demand_id")
    demand = demands.get(demand_id) if isinstance(demand_id, str) else None
    return bool(
        demand is not None
        and demand.source is CapabilityDemandSource.KERNEL_REQUIRED
        and demand.context == (event.run_id, event.session_id, event.transaction_id)
        and event.parent_event_id == demand.event_id
        and payload.get("demand_sequence") == demand.sequence
        and payload.get("demand_source") == demand.source.value
        and payload.get("requested_capability_id") == demand.capability_id
        and isinstance(payload.get("module_id"), str)
    )
