"""定义 Runtime 向耐久 Trace 发布生命周期事实的最小协议。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from rivet.kernel.capability_demand import CapabilityDemandSource, DemandHandle


@dataclass(frozen=True, slots=True)
class ModuleActivationEvent:
    """把锁内发生的模块激活归因到唯一已落盘 Demand。"""

    module_id: str
    requested_capability_id: str
    demand_id: str
    demand_source: CapabilityDemandSource
    demand_sequence: int
    dependency: bool

    @classmethod
    def from_handle(
        cls,
        *,
        module_id: str,
        requested_capability_id: str,
        handle: DemandHandle,
        dependency: bool,
    ) -> ModuleActivationEvent:
        return cls(
            module_id=module_id,
            requested_capability_id=requested_capability_id,
            demand_id=handle.demand_id,
            demand_source=handle.source,
            demand_sequence=handle.sequence,
            dependency=dependency,
        )


@dataclass(frozen=True, slots=True)
class ModuleActivationFailure:
    """记录激活失败，同时不持久化原始异常文本。"""

    module_id: str
    requested_capability_id: str
    demand_id: str
    demand_source: CapabilityDemandSource
    demand_sequence: int
    error_type: str


@dataclass(frozen=True, slots=True)
class ModuleReleaseEvent:
    """记录最后一个 Lease 归还后模块资源已归零。"""

    module_id: str
    activated_by_demand_id: str


class ModuleLifecycleSink(Protocol):
    """生产 Trace 可实现的最小模块生命周期写入协议。"""

    async def activated(self, event: ModuleActivationEvent) -> None:
        """在 Runtime 把模块标记 ACTIVE 前耐久记录激活事实。"""
        ...

    async def activation_failed(self, event: ModuleActivationFailure) -> None:
        """记录一次已经完成资源回滚的激活失败。"""
        ...

    async def released(self, event: ModuleReleaseEvent) -> None:
        """记录模块实例与 Scope 已完成释放。"""
        ...


class InMemoryModuleLifecycleSink:
    """测试用 Sink，可注入 activated 写失败验证 Runtime 回滚。"""

    def __init__(self, *, fail_activation_for: frozenset[str] = frozenset()) -> None:
        self.activation_events: list[ModuleActivationEvent] = []
        self.failure_events: list[ModuleActivationFailure] = []
        self.release_events: list[ModuleReleaseEvent] = []
        self._fail_activation_for = fail_activation_for

    @property
    def events(
        self,
    ) -> Sequence[ModuleActivationEvent | ModuleActivationFailure | ModuleReleaseEvent]:
        return (*self.activation_events, *self.failure_events, *self.release_events)

    async def activated(self, event: ModuleActivationEvent) -> None:
        if event.module_id in self._fail_activation_for:
            raise RuntimeError("测试 Lifecycle Sink 写入失败")
        self.activation_events.append(event)

    async def activation_failed(self, event: ModuleActivationFailure) -> None:
        self.failure_events.append(event)

    async def released(self, event: ModuleReleaseEvent) -> None:
        self.release_events.append(event)
