"""提供无法绕过耐久 Demand 门禁的最小 Rivet Kernel。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from rivet.contracts.common import CapabilityId
from rivet.contracts.modules import ModuleManifest, ModuleState
from rivet.kernel.capability_demand import (
    CapabilityDemand,
    DemandContext,
    DemandHandle,
    DemandJournal,
)
from rivet.kernel.errors import DemandCausalityError, DemandJournalError
from rivet.kernel.manifests import ManifestLoader
from rivet.kernel.module_api import ModuleActivationContext
from rivet.kernel.module_events import ModuleLifecycleSink
from rivet.kernel.module_runtime import (
    CapabilityLease,
    ModuleRuntime,
    ModuleRuntimeSnapshot,
    _ActivationPermit,  # pyright: ignore[reportPrivateUsage]
)
from rivet.kernel.resources import ResourceCounts


class RivetKernel:
    """先落盘 Demand，再签发 Runtime 私有 Permit。"""

    def __init__(
        self,
        runtime: ModuleRuntime,
        *,
        demand_journal: DemandJournal,
        activation_seal: object,
    ) -> None:
        self._runtime = runtime
        self._demand_journal = demand_journal
        self._activation_seal = activation_seal

    @classmethod
    def from_manifests(
        cls,
        manifests: tuple[ModuleManifest, ...],
        *,
        demand_journal: DemandJournal,
        lifecycle_sink: ModuleLifecycleSink,
        activation_context: ModuleActivationContext,
    ) -> RivetKernel:
        """构造只解析静态 Manifest、不会导入 factory 的 Kernel。"""
        seal = object()
        runtime = ModuleRuntime(
            manifests,
            activation_context=activation_context,
            lifecycle_sink=lifecycle_sink,
            activation_seal=seal,
        )
        return cls(runtime, demand_journal=demand_journal, activation_seal=seal)

    @classmethod
    def from_manifest_paths(
        cls,
        paths: Iterable[Path],
        *,
        demand_journal: DemandJournal,
        lifecycle_sink: ModuleLifecycleSink,
        activation_context: ModuleActivationContext,
    ) -> RivetKernel:
        manifests = ManifestLoader().load_paths(paths)
        return cls.from_manifests(
            manifests,
            demand_journal=demand_journal,
            lifecycle_sink=lifecycle_sink,
            activation_context=activation_context,
        )

    async def start(self) -> None:
        """启动只读 Runtime 元数据；不得激活任何模块。"""
        await self._runtime.start()

    async def begin_user_demand(
        self,
        target: str,
        *,
        reason: str,
        context: DemandContext,
        operation_id: str | None = None,
    ) -> DemandHandle:
        demand = CapabilityDemand.user_explicit(
            target,
            reason=reason,
            context=context,
            operation_id=operation_id,
        )
        return await self._persist(demand)

    async def begin_model_tool_demand(
        self,
        tool_name: str,
        *,
        parent: DemandHandle,
        reason: str,
        operation_id: str,
    ) -> DemandHandle:
        """把模型工具调用耐久绑定到本次用户任务。"""
        self._validate_parent(parent)
        demand = CapabilityDemand.model_tool_call(
            tool_name,
            reason=reason,
            context=parent.context,
            operation_id=operation_id,
            parent_demand_id=parent.demand_id,
        )
        return await self._persist(demand)

    async def acquire_required(
        self,
        capability_id: CapabilityId,
        *,
        parent: DemandHandle,
        reason: str,
        operation_id: str | None = None,
    ) -> CapabilityLease[object]:
        """耐久写入 KERNEL_REQUIRED 后，才允许 Runtime 激活能力。"""
        self._validate_parent(parent)
        demand = CapabilityDemand._kernel_required(  # pyright: ignore[reportPrivateUsage]
            capability_id,
            reason=reason,
            context=parent.context,
            operation_id=operation_id,
            parent_demand_id=parent.demand_id,
        )
        handle = await self._persist(demand)
        permit = _ActivationPermit(
            handle=handle,
            requested_capability_id=capability_id,
            _seal=self._activation_seal,
        )
        return await self._runtime._acquire(  # pyright: ignore[reportPrivateUsage]
            permit
        )

    def state(self, module_id: str) -> ModuleState:
        return self._runtime.state(module_id)

    def snapshots(self) -> tuple[ModuleRuntimeSnapshot, ...]:
        return self._runtime.snapshots()

    def resource_counts(self) -> ResourceCounts:
        return self._runtime.resource_counts()

    def provider_module_id(self, capability_id: CapabilityId) -> str:
        return self._runtime.provider_module_id(capability_id)

    async def shutdown(self) -> None:
        await self._runtime.shutdown()

    def _validate_parent(self, parent: DemandHandle) -> None:
        if (
            parent._kernel_seal  # pyright: ignore[reportPrivateUsage]
            is not self._activation_seal
        ):
            raise DemandCausalityError("父 DemandHandle 不属于当前 Kernel")

    async def _persist(self, demand: CapabilityDemand) -> DemandHandle:
        """拒绝不自洽 Journal 收据，成功返回即代表 Demand 已耐久。"""
        record = await self._demand_journal.append(demand)
        if record.demand != demand:
            raise DemandJournalError("Demand Journal 返回了不匹配的收据")
        return DemandHandle(record=record, _kernel_seal=self._activation_seal)
