"""把 Kernel Demand 与模块生命周期直接绑定到耐久 NDJSON。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast

from pydantic import JsonValue

from rivet.kernel.capability_demand import (
    CapabilityDemand,
    CapabilityDemandSource,
    DemandContext,
    DemandJournalRecord,
)
from rivet.kernel.errors import DemandCausalityError, DemandJournalError
from rivet.kernel.module_events import (
    ModuleActivationEvent,
    ModuleActivationFailure,
    ModuleReleaseEvent,
)
from rivet.trace.audit import audit_demand_trace
from rivet.trace.builder import TraceEventBuilder
from rivet.trace.store import TraceStore


@dataclass(frozen=True, slots=True)
class _IndexedDemand:
    demand_id: str
    source: CapabilityDemandSource
    context: DemandContext
    event_id: str
    sequence: int


class TraceDemandJournal:
    """只有 `demand.created` 完成 fsync 后才签发 Journal 收据。"""

    def __init__(
        self,
        trace: TraceStore,
        *,
        builder: TraceEventBuilder | None = None,
    ) -> None:
        self._trace = trace
        self._builder = builder or TraceEventBuilder()
        self._lock = asyncio.Lock()
        self._demands: dict[str, _IndexedDemand] = {}
        self._load_existing()

    def event_id_for(self, demand_id: str) -> str:
        indexed = self._demands.get(demand_id)
        if indexed is None:
            raise DemandCausalityError("Demand 尚未落盘")
        return indexed.event_id

    def context_for(self, demand_id: str) -> DemandContext:
        indexed = self._demands.get(demand_id)
        if indexed is None:
            raise DemandCausalityError("Demand 尚未落盘")
        return indexed.context

    async def append(self, demand: CapabilityDemand) -> DemandJournalRecord:
        """原子校验父链并等待 NDJSON append/fsync。"""
        async with self._lock:
            self._validate(demand)
            event = self._builder.build(
                event_type="demand.created",
                run_id=demand.context.run_id,
                session_id=demand.context.session_id,
                transaction_id=demand.context.transaction_id,
                parent_event_id=(
                    self.event_id_for(demand.parent_demand_id)
                    if demand.parent_demand_id is not None
                    else None
                ),
                input_summary=f"{demand.source.value} capability demand",
                payload=_demand_payload(demand),
            )
            try:
                persisted = await self._trace.emit(event)
            except BaseException as error:
                raise DemandJournalError("demand.created 未能耐久写入") from error
            record = DemandJournalRecord(
                demand=demand,
                sequence=persisted.sequence,
                event_id=event.event_id,
            )
            self._demands[demand.demand_id] = _IndexedDemand(
                demand_id=demand.demand_id,
                source=demand.source,
                context=demand.context,
                event_id=event.event_id,
                sequence=persisted.sequence,
            )
            return record

    def _validate(self, demand: CapabilityDemand) -> None:
        if demand.demand_id in self._demands:
            raise DemandCausalityError("Demand ID 重复")
        parent_id = demand.parent_demand_id
        if demand.source is CapabilityDemandSource.USER_EXPLICIT:
            if parent_id is not None:
                raise DemandCausalityError("USER_EXPLICIT 必须是根 Demand")
            return
        if parent_id is None:
            raise DemandCausalityError(f"{demand.source.value} 必须引用已落盘父 Demand")
        parent = self._demands.get(parent_id)
        if parent is None:
            raise DemandCausalityError("Demand 父节点尚未落盘")
        if parent.context != demand.context:
            raise DemandCausalityError("Demand 父子节点必须属于同一运行上下文")

    def _load_existing(self) -> None:
        """从唯一事实源恢复因果索引；非法历史会失败关闭。"""
        records = self._trace.events()
        audit = audit_demand_trace(records)
        if not audit.passed:
            raise DemandJournalError("历史 Demand 因果审计失败")
        for record in records:
            event = record.event
            if event.event_type != "demand.created":
                continue
            payload = event.payload
            try:
                demand_id = cast(str, payload["demand_id"])
                source = CapabilityDemandSource(cast(str, payload["demand_source"]))
                context = DemandContext(
                    run_id=event.run_id,
                    session_id=event.session_id,
                    transaction_id=event.transaction_id,
                )
            except (KeyError, TypeError, ValueError) as error:
                raise DemandJournalError("历史 demand.created 载荷无效") from error
            if demand_id in self._demands:
                raise DemandJournalError("历史 Demand ID 重复")
            parent_id = cast(str | None, payload.get("parent_demand_id"))
            if source is CapabilityDemandSource.USER_EXPLICIT:
                if parent_id is not None:
                    raise DemandJournalError("历史 USER_EXPLICIT 不是根节点")
            else:
                parent = self._demands.get(parent_id or "")
                if parent is None or parent.context != context:
                    raise DemandJournalError("历史 Demand 父链无效")
            self._demands[demand_id] = _IndexedDemand(
                demand_id=demand_id,
                source=source,
                context=context,
                event_id=event.event_id,
                sequence=record.sequence,
            )


class TraceModuleLifecycleSink:
    """在 Runtime 发布 ACTIVE 前记录精确 Demand 归因。"""

    def __init__(
        self,
        trace: TraceStore,
        demands: TraceDemandJournal,
        *,
        builder: TraceEventBuilder | None = None,
    ) -> None:
        self._trace = trace
        self._demands = demands
        self._builder = builder or TraceEventBuilder()

    async def activated(self, event: ModuleActivationEvent) -> None:
        await self._emit(
            "module.activated",
            event.demand_id,
            {
                "dependency": event.dependency,
                "demand_id": event.demand_id,
                "demand_sequence": event.demand_sequence,
                "demand_source": event.demand_source.value,
                "module_id": event.module_id,
                "requested_capability_id": event.requested_capability_id,
            },
            f"activated {event.module_id}",
        )

    async def activation_failed(self, event: ModuleActivationFailure) -> None:
        await self._emit(
            "module.activation_failed",
            event.demand_id,
            {
                "demand_id": event.demand_id,
                "demand_sequence": event.demand_sequence,
                "demand_source": event.demand_source.value,
                "error_type": event.error_type,
                "module_id": event.module_id,
                "requested_capability_id": event.requested_capability_id,
            },
            f"activation failed {event.module_id}",
        )

    async def released(self, event: ModuleReleaseEvent) -> None:
        await self._emit(
            "module.released",
            event.activated_by_demand_id,
            {
                "activated_by_demand_id": event.activated_by_demand_id,
                "module_id": event.module_id,
            },
            f"released {event.module_id}",
        )

    async def _emit(
        self,
        event_type: str,
        demand_id: str,
        payload: dict[str, JsonValue],
        summary: str,
    ) -> None:
        context = self._demands.context_for(demand_id)
        await self._trace.emit(
            self._builder.build(
                event_type=event_type,
                run_id=context.run_id,
                session_id=context.session_id,
                transaction_id=context.transaction_id,
                parent_event_id=self._demands.event_id_for(demand_id),
                result_summary=summary,
                payload=payload,
            )
        )


def _demand_payload(demand: CapabilityDemand) -> dict[str, JsonValue]:
    return {
        "capability_id": demand.capability_id,
        "demand_id": demand.demand_id,
        "demand_source": demand.source.value,
        "operation_id": demand.operation_id,
        "parent_demand_id": demand.parent_demand_id,
        "reason": demand.reason,
    }
