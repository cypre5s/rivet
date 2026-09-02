"""定义能力激活前必须耐久记录的需求因果链。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from rivet.contracts.common import CapabilityId, RunId, SessionId, TransactionId
from rivet.kernel.errors import DemandCausalityError, DemandJournalError


class CapabilityDemandSource(StrEnum):
    """限定合法需求的三个来源。"""

    USER_EXPLICIT = "USER_EXPLICIT"
    MODEL_TOOL_CALL = "MODEL_TOOL_CALL"
    KERNEL_REQUIRED = "KERNEL_REQUIRED"


@dataclass(frozen=True, slots=True)
class DemandContext:
    """绑定需求所属运行，防止跨运行借用父节点。"""

    run_id: RunId
    session_id: SessionId
    transaction_id: TransactionId | None = None

    def __post_init__(self) -> None:
        if not self.run_id or not self.session_id:
            raise DemandCausalityError("Demand 必须绑定非空 run_id 与 session_id")


@dataclass(frozen=True, slots=True, init=False)
class CapabilityDemand:
    """尚未获得运行资格、只能由受限工厂创建的需求事实。"""

    demand_id: str
    capability_id: CapabilityId
    source: CapabilityDemandSource
    reason: str
    context: DemandContext
    operation_id: str | None
    parent_demand_id: str | None

    @classmethod
    def _create(
        cls,
        capability_id: CapabilityId,
        *,
        source: CapabilityDemandSource,
        reason: str,
        context: DemandContext,
        operation_id: str | None = None,
        parent_demand_id: str | None = None,
    ) -> CapabilityDemand:
        if not capability_id:
            raise DemandCausalityError("Demand capability_id 不得为空")
        if not reason.strip():
            raise DemandCausalityError("Demand reason 不得为空")
        if (
            source is CapabilityDemandSource.USER_EXPLICIT
            and parent_demand_id is not None
        ):
            raise DemandCausalityError("USER_EXPLICIT 必须是根 Demand")
        if source is not CapabilityDemandSource.USER_EXPLICIT and not parent_demand_id:
            raise DemandCausalityError(f"{source.value} 必须引用已落盘父 Demand")
        if source is CapabilityDemandSource.MODEL_TOOL_CALL and not operation_id:
            raise DemandCausalityError(
                "MODEL_TOOL_CALL 必须绑定 tool_call operation_id"
            )
        return cls._from_validated(
            demand_id=f"demand_{uuid.uuid4().hex}",
            capability_id=capability_id,
            source=source,
            reason=reason.strip(),
            context=context,
            operation_id=operation_id,
            parent_demand_id=parent_demand_id,
        )

    @classmethod
    def _from_validated(
        cls,
        *,
        demand_id: str,
        capability_id: CapabilityId,
        source: CapabilityDemandSource,
        reason: str,
        context: DemandContext,
        operation_id: str | None,
        parent_demand_id: str | None,
    ) -> CapabilityDemand:
        instance = object.__new__(cls)
        object.__setattr__(instance, "demand_id", demand_id)
        object.__setattr__(instance, "capability_id", capability_id)
        object.__setattr__(instance, "source", source)
        object.__setattr__(instance, "reason", reason)
        object.__setattr__(instance, "context", context)
        object.__setattr__(instance, "operation_id", operation_id)
        object.__setattr__(instance, "parent_demand_id", parent_demand_id)
        return instance

    @classmethod
    def user_explicit(
        cls,
        target: str,
        *,
        reason: str,
        context: DemandContext,
        operation_id: str | None = None,
    ) -> CapabilityDemand:
        """创建唯一允许没有父节点的用户根需求。"""
        return cls._create(
            target,
            source=CapabilityDemandSource.USER_EXPLICIT,
            reason=reason,
            context=context,
            operation_id=operation_id,
        )

    @classmethod
    def model_tool_call(
        cls,
        tool_name: str,
        *,
        reason: str,
        context: DemandContext,
        operation_id: str,
        parent_demand_id: str,
    ) -> CapabilityDemand:
        """创建由已落盘任务派生的模型工具需求。"""
        return cls._create(
            tool_name,
            source=CapabilityDemandSource.MODEL_TOOL_CALL,
            reason=reason,
            context=context,
            operation_id=operation_id,
            parent_demand_id=parent_demand_id,
        )

    @classmethod
    def _kernel_required(
        cls,
        capability_id: CapabilityId,
        *,
        reason: str,
        context: DemandContext,
        operation_id: str | None,
        parent_demand_id: str,
    ) -> CapabilityDemand:
        """仅供 RivetKernel 在 acquire_required 内创建派生需求。"""
        return cls._create(
            capability_id,
            source=CapabilityDemandSource.KERNEL_REQUIRED,
            reason=reason,
            context=context,
            operation_id=operation_id,
            parent_demand_id=parent_demand_id,
        )


@dataclass(frozen=True, slots=True)
class DemandJournalRecord:
    """Journal 完成耐久写后返回的不可变收据。"""

    demand: CapabilityDemand
    sequence: int
    event_id: str

    def __post_init__(self) -> None:
        if self.sequence <= 0 or not self.event_id:
            raise DemandJournalError("Demand Journal 收据无效")


class DemandJournal(Protocol):
    """生产 Trace 可实现的最小耐久 Demand 写入协议。"""

    async def append(self, demand: CapabilityDemand) -> DemandJournalRecord:
        """校验父链、耐久 append/fsync，并仅在成功后返回收据。"""
        ...


@dataclass(frozen=True, slots=True)
class DemandHandle:
    """证明 Demand 已由当前 Kernel 的 Journal 耐久接受。"""

    record: DemandJournalRecord
    _kernel_seal: object = field(repr=False, compare=False)

    @property
    def demand_id(self) -> str:
        return self.record.demand.demand_id

    @property
    def capability_id(self) -> str:
        return self.record.demand.capability_id

    @property
    def source(self) -> CapabilityDemandSource:
        return self.record.demand.source

    @property
    def context(self) -> DemandContext:
        return self.record.demand.context

    @property
    def event_id(self) -> str:
        return self.record.event_id

    @property
    def sequence(self) -> int:
        return self.record.sequence


class InMemoryDemandJournal:
    """测试用 Journal；实现与生产 Trace 相同的因果校验语义。"""

    def __init__(self, *, fail_after: int | None = None) -> None:
        self._records: dict[str, DemandJournalRecord] = {}
        self._ordered: list[DemandJournalRecord] = []
        self._fail_after = fail_after

    @property
    def records(self) -> Sequence[DemandJournalRecord]:
        return tuple(self._ordered)

    async def append(self, demand: CapabilityDemand) -> DemandJournalRecord:
        if self._fail_after is not None and len(self._ordered) >= self._fail_after:
            raise DemandJournalError("测试 Demand Journal 写入失败")
        if demand.demand_id in self._records:
            raise DemandCausalityError(f"Demand ID 重复：{demand.demand_id}")
        parent_id = demand.parent_demand_id
        if demand.source is CapabilityDemandSource.USER_EXPLICIT:
            if parent_id is not None:
                raise DemandCausalityError("USER_EXPLICIT 必须是根 Demand")
        else:
            if parent_id is None:
                raise DemandCausalityError(
                    f"{demand.source.value} 必须引用已落盘父 Demand"
                )
            parent = self._records.get(parent_id)
            if parent is None:
                raise DemandCausalityError("Demand 父节点尚未落盘")
            if parent.demand.context != demand.context:
                raise DemandCausalityError("Demand 父子节点必须属于同一运行上下文")
        sequence = len(self._ordered) + 1
        record = DemandJournalRecord(
            demand=demand,
            sequence=sequence,
            event_id=f"demand_event_{sequence}",
        )
        self._records[demand.demand_id] = record
        self._ordered.append(record)
        return record
