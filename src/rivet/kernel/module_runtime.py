"""实现只接受耐久 Demand Permit 的惰性模块运行时。"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import cast

from rivet.contracts.common import CapabilityId, ModuleId
from rivet.contracts.modules import ModuleManifest, ModuleState
from rivet.kernel.capabilities import CapabilityRegistry
from rivet.kernel.capability_demand import CapabilityDemandSource, DemandHandle
from rivet.kernel.errors import (
    CapabilityNotFoundError,
    DemandCausalityError,
    ModuleActivationError,
    ModuleShutdownError,
)
from rivet.kernel.module_api import ModuleActivationContext, ModuleInstance
from rivet.kernel.module_events import (
    ModuleActivationEvent,
    ModuleActivationFailure,
    ModuleLifecycleSink,
    ModuleReleaseEvent,
)
from rivet.kernel.module_graph import stable_activation_order
from rivet.kernel.resources import ResourceCounts, ResourceScope


@dataclass(frozen=True, slots=True)
class _ActivationPermit:
    """Kernel 在 Demand 落盘后签发给 Runtime 的一次性激活资格。"""

    handle: DemandHandle
    requested_capability_id: CapabilityId
    _seal: object = field(repr=False, compare=False)


@dataclass(slots=True)
class _RuntimeModule:
    """保存单个模块的最小进程内生命周期事实。"""

    manifest: ModuleManifest
    state: ModuleState
    lock: asyncio.Lock
    instance: ModuleInstance | None = None
    capabilities: dict[str, object] | None = None
    scope: ResourceScope | None = None
    lease_count: int = 0
    activated_by_demand_id: str | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class ModuleRuntimeSnapshot:
    """公开不具备激活能力的只读模块状态。"""

    module_id: str
    state: ModuleState
    dependencies: tuple[str, ...]
    dependents: tuple[str, ...]
    capabilities: tuple[str, ...]
    lease_count: int
    resource_counts: ResourceCounts
    activated_by_demand_id: str | None
    last_error: str | None


class CapabilityLease[CapabilityT]:
    """暴露真实 capability，并在使用期间持有完整依赖闭包。"""

    def __init__(
        self,
        runtime: ModuleRuntime,
        module_ids: tuple[str, ...],
        *,
        capability_id: str,
        module_id: str,
        capability: CapabilityT,
        demand_handle: DemandHandle,
    ) -> None:
        self._runtime = runtime
        self._module_ids = module_ids
        self.capability_id = capability_id
        self.module_id = module_id
        self.capability = capability
        self.demand_handle = demand_handle
        self._released = False

    async def __aenter__(self) -> CapabilityT:
        return self.capability

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exception_type, exception, traceback
        await self.release()

    async def release(self) -> None:
        """幂等归还 Lease，并在最后一次归还时关闭模块资源。"""
        if self._released:
            return
        self._released = True
        await self._runtime._release_lease(  # pyright: ignore[reportPrivateUsage]
            self._module_ids
        )


async def release_capability_leases(
    leases: Iterable[CapabilityLease[object]],
) -> None:
    """确定性逆序释放全部 Lease，并在清理完成后重新抛出首个错误。"""
    first_error: BaseException | None = None
    for lease in reversed(tuple(leases)):
        try:
            await lease.release()
        except BaseException as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


class ModuleRuntime:
    """内部 Runtime；业务层不得持有实例或传入字符串请求能力。"""

    def __init__(
        self,
        manifests: tuple[ModuleManifest, ...],
        *,
        activation_context: ModuleActivationContext,
        lifecycle_sink: ModuleLifecycleSink,
        activation_seal: object,
    ) -> None:
        self._activation_context = activation_context
        self._lifecycle_sink = lifecycle_sink
        self._activation_seal = activation_seal
        self._activation_order = stable_activation_order(manifests)
        self._manifest_by_id = {manifest.module_id: manifest for manifest in manifests}
        self._registry = CapabilityRegistry(manifests)
        self._modules = {
            manifest.module_id: _RuntimeModule(
                manifest=manifest,
                state=ModuleState.INACTIVE,
                lock=asyncio.Lock(),
            )
            for manifest in manifests
        }
        self._active_sequence: list[str] = []
        self._started = False
        self._shutting_down = False

    async def start(self) -> None:
        """只标记 Runtime 就绪；任何 factory 都不得在启动阶段导入。"""
        self._started = True

    async def _acquire(
        self,
        permit: _ActivationPermit,
    ) -> CapabilityLease[object]:
        """消费 Kernel 私有 Permit，惰性激活并租用完整依赖闭包。"""
        self._validate_permit(permit)
        if not self._started:
            raise ModuleActivationError("ModuleRuntime 尚未启动")
        if self._shutting_down:
            raise ModuleActivationError("ModuleRuntime 正在关闭")
        manifest = self._registry.provider_for(permit.requested_capability_id)
        activated_now: list[str] = []
        try:
            await self._activate_module(
                manifest.module_id,
                permit=permit,
                target_module_id=manifest.module_id,
                activated_now=activated_now,
            )
        except BaseException:
            await self._rollback_new_activations(activated_now)
            raise
        capability = self._capability(
            manifest.module_id,
            permit.requested_capability_id,
        )
        module_ids = self._dependency_closure(manifest.module_id)
        for module_id in module_ids:
            node = self._modules[module_id]
            node.lease_count += 1
        return CapabilityLease(
            self,
            module_ids,
            capability_id=permit.requested_capability_id,
            module_id=manifest.module_id,
            capability=capability,
            demand_handle=permit.handle,
        )

    def state(self, module_id: ModuleId) -> ModuleState:
        node = self._modules.get(module_id)
        if node is None:
            raise CapabilityNotFoundError(f"未知模块 {module_id}")
        return node.state

    def manifest(self, module_id: ModuleId) -> ModuleManifest:
        node = self._modules.get(module_id)
        if node is None:
            raise CapabilityNotFoundError(f"未知模块 {module_id}")
        return node.manifest

    def provider_module_id(self, capability_id: CapabilityId) -> ModuleId:
        """只解析静态提供者，不导入 factory 或激活模块。"""
        return self._registry.provider_for(capability_id).module_id

    def snapshots(self) -> tuple[ModuleRuntimeSnapshot, ...]:
        snapshots: list[ModuleRuntimeSnapshot] = []
        for module_id in self._activation_order:
            node = self._modules[module_id]
            snapshots.append(
                ModuleRuntimeSnapshot(
                    module_id=module_id,
                    state=node.state,
                    dependencies=node.manifest.requires,
                    dependents=tuple(
                        candidate
                        for candidate in self._activation_order
                        if module_id in self._manifest_by_id[candidate].requires
                    ),
                    capabilities=node.manifest.provides,
                    lease_count=node.lease_count,
                    resource_counts=(
                        node.scope.counts()
                        if node.scope is not None
                        else ResourceCounts()
                    ),
                    activated_by_demand_id=node.activated_by_demand_id,
                    last_error=node.last_error,
                )
            )
        return tuple(snapshots)

    def resource_counts(self) -> ResourceCounts:
        total = ResourceCounts()
        for node in self._modules.values():
            if node.scope is not None:
                total += node.scope.counts()
        return total

    async def shutdown(self) -> None:
        """逆拓扑关闭全部实例，并以资源归零作为门禁。"""
        if self._shutting_down:
            return
        self._shutting_down = True
        first_error: BaseException | None = None
        for module_id in reversed(self._activation_order):
            node = self._modules[module_id]
            node.lease_count = 0
            try:
                await self._deactivate_module(module_id, force=True)
            except BaseException as error:
                if first_error is None:
                    first_error = error
        self._active_sequence.clear()
        counts = self.resource_counts()
        if counts.resource_count and first_error is None:
            first_error = RuntimeError(
                f"shutdown 后仍有 {counts.resource_count} 项资源"
            )
        if first_error is not None:
            raise ModuleShutdownError(
                "模块运行时关闭未满足资源归零门禁"
            ) from first_error

    def _validate_permit(self, permit: _ActivationPermit) -> None:
        if (
            permit._seal  # pyright: ignore[reportPrivateUsage]
            is not self._activation_seal
        ):
            raise DemandCausalityError("Activation Permit 不属于当前 Runtime")
        handle = permit.handle
        if (
            handle._kernel_seal  # pyright: ignore[reportPrivateUsage]
            is not self._activation_seal
        ):
            raise DemandCausalityError("DemandHandle 不属于当前 Kernel")
        demand = handle.record.demand
        if demand.source is not CapabilityDemandSource.KERNEL_REQUIRED:
            raise DemandCausalityError("只有 KERNEL_REQUIRED Demand 可以激活能力")
        if demand.capability_id != permit.requested_capability_id:
            raise DemandCausalityError("Activation Permit capability 与 Demand 不一致")

    async def _activate_module(
        self,
        module_id: str,
        *,
        permit: _ActivationPermit,
        target_module_id: str,
        activated_now: list[str],
    ) -> ModuleInstance:
        """在模块锁内完成激活、归因落盘，再发布 ACTIVE 状态。"""
        node = self._modules[module_id]
        async with node.lock:
            if node.state is ModuleState.ACTIVE:
                if node.instance is None:
                    raise ModuleActivationError(f"模块 {module_id} 状态与实例不一致")
                return node.instance
            if node.state is ModuleState.FAILED:
                raise ModuleActivationError(f"模块 {module_id} 已处于 FAILED")
            for dependency_id in sorted(node.manifest.requires):
                await self._activate_module(
                    dependency_id,
                    permit=permit,
                    target_module_id=target_module_id,
                    activated_now=activated_now,
                )

            node.state = ModuleState.ACTIVATING
            scope = ResourceScope(module_id)
            instance: ModuleInstance | None = None
            try:
                factory = self._load_factory(node.manifest.factory)
                factory_result = factory()
                if not isinstance(factory_result, ModuleInstance):
                    raise TypeError("factory 返回对象不满足 ModuleInstance 协议")
                instance = factory_result
                dependency_capabilities: dict[str, object] = {}
                for dependency_id in node.manifest.requires:
                    dependency = self._modules[dependency_id]
                    if dependency.capabilities is None:
                        raise RuntimeError(
                            f"依赖模块 {dependency_id} 未提供 capability"
                        )
                    dependency_capabilities.update(dependency.capabilities)
                context = self._activation_context.bind(
                    module_id,
                    node.manifest.provides,
                    dependency_capabilities,
                )
                capability_mapping = dict(await instance.activate(context, scope))
                expected = set(node.manifest.provides)
                actual = set(capability_mapping)
                if actual != expected or any(
                    value is None for value in capability_mapping.values()
                ):
                    raise TypeError(
                        "模块 capability mapping 与 Manifest 不一致："
                        f"expected={sorted(expected)}, actual={sorted(actual)}"
                    )
                activation_event = ModuleActivationEvent.from_handle(
                    module_id=module_id,
                    requested_capability_id=permit.requested_capability_id,
                    handle=permit.handle,
                    dependency=module_id != target_module_id,
                )
                await self._lifecycle_sink.activated(activation_event)
            except asyncio.CancelledError:
                cleanup_error = await self._record_failed_activation(
                    node,
                    module_id,
                    permit=permit,
                    instance=instance,
                    scope=scope,
                    error_type="CancelledError",
                )
                if cleanup_error is not None:
                    raise ModuleShutdownError(
                        f"模块 {module_id} 取消激活后的资源回滚失败"
                    ) from cleanup_error
                raise
            except BaseException as error:
                cleanup_error = await self._record_failed_activation(
                    node,
                    module_id,
                    permit=permit,
                    instance=instance,
                    scope=scope,
                    error_type=type(error).__name__,
                )
                if cleanup_error is not None:
                    raise ModuleShutdownError(
                        f"模块 {module_id} 激活失败后的资源回滚失败"
                    ) from cleanup_error
                raise ModuleActivationError(f"模块 {module_id} 激活失败") from error

            node.instance = instance
            node.capabilities = capability_mapping
            node.scope = scope
            node.state = ModuleState.ACTIVE
            node.activated_by_demand_id = permit.handle.demand_id
            node.last_error = None
            with suppress(ValueError):
                self._active_sequence.remove(module_id)
            self._active_sequence.append(module_id)
            activated_now.append(module_id)
            return instance

    async def _emit_activation_failure(
        self,
        module_id: str,
        *,
        permit: _ActivationPermit,
        error_type: str,
    ) -> None:
        with suppress(BaseException):
            await self._lifecycle_sink.activation_failed(
                ModuleActivationFailure(
                    module_id=module_id,
                    requested_capability_id=permit.requested_capability_id,
                    demand_id=permit.handle.demand_id,
                    demand_source=permit.handle.source,
                    demand_sequence=permit.handle.sequence,
                    error_type=error_type,
                )
            )

    async def _record_failed_activation(
        self,
        node: _RuntimeModule,
        module_id: str,
        *,
        permit: _ActivationPermit,
        instance: ModuleInstance | None,
        scope: ResourceScope,
        error_type: str,
    ) -> BaseException | None:
        """尽力回滚并保留未清理句柄，使 shutdown 可以继续回收。"""
        cleanup_error = await self._cleanup_failed_activation(instance, scope)
        node.instance = instance if cleanup_error is not None else None
        node.capabilities = None
        node.scope = scope if cleanup_error is not None else None
        node.state = ModuleState.FAILED
        node.last_error = type(cleanup_error).__name__ if cleanup_error else error_type
        await self._emit_activation_failure(
            module_id,
            permit=permit,
            error_type=error_type,
        )
        return cleanup_error

    @staticmethod
    async def _cleanup_failed_activation(
        instance: ModuleInstance | None,
        scope: ResourceScope,
    ) -> BaseException | None:
        first_error: BaseException | None = None
        if instance is not None:
            try:
                await instance.shutdown()
            except BaseException as error:
                first_error = error
        try:
            await scope.close()
            scope.assert_empty()
        except BaseException as error:
            if first_error is None:
                first_error = error
        return first_error

    async def _rollback_new_activations(self, module_ids: list[str]) -> None:
        for module_id in reversed(module_ids):
            with suppress(BaseException):
                await self._deactivate_module(module_id)

    async def _release_lease(self, module_ids: tuple[str, ...]) -> None:
        first_error: BaseException | None = None
        for module_id in reversed(module_ids):
            node = self._modules[module_id]
            if node.lease_count <= 0:
                if first_error is None:
                    first_error = ModuleActivationError(
                        f"模块 {module_id} Lease 计数下溢"
                    )
                continue
            node.lease_count -= 1
            if node.lease_count == 0:
                try:
                    await self._deactivate_module(module_id)
                except BaseException as error:
                    if first_error is None:
                        first_error = error
        if first_error is not None:
            raise first_error

    async def _deactivate_module(self, module_id: str, *, force: bool = False) -> bool:
        node = self._modules[module_id]
        async with node.lock:
            if node.state is not ModuleState.ACTIVE:
                has_residual_state = node.instance is not None or node.scope is not None
                if not force or not has_residual_state:
                    return False
            if not force and (
                node.lease_count or self._has_active_dependents(module_id)
            ):
                return False
            instance = node.instance
            scope = node.scope
            demand_id = node.activated_by_demand_id
            first_error: BaseException | None = None
            try:
                if instance is not None:
                    await instance.shutdown()
            except BaseException as error:
                first_error = error
            try:
                if scope is not None:
                    await scope.close()
                    scope.assert_empty()
            except BaseException as error:
                if first_error is None:
                    first_error = error
            if first_error is not None:
                node.state = ModuleState.FAILED
                node.last_error = type(first_error).__name__
                raise ModuleShutdownError(f"模块 {module_id} 释放失败") from first_error
            node.instance = None
            node.capabilities = None
            node.scope = None
            node.state = ModuleState.INACTIVE
            node.activated_by_demand_id = None
            node.last_error = None
            with suppress(ValueError):
                self._active_sequence.remove(module_id)
            if demand_id is not None:
                await self._lifecycle_sink.released(
                    ModuleReleaseEvent(
                        module_id=module_id,
                        activated_by_demand_id=demand_id,
                    )
                )
            return True

    def _has_active_dependents(self, module_id: str) -> bool:
        return any(
            module_id in node.manifest.requires and node.state is ModuleState.ACTIVE
            for node in self._modules.values()
        )

    def _capability(self, module_id: str, capability_id: str) -> object:
        capabilities = self._modules[module_id].capabilities
        if capabilities is None or capability_id not in capabilities:
            raise ModuleActivationError(
                f"模块 {module_id} 未提供 capability {capability_id}"
            )
        return capabilities[capability_id]

    @staticmethod
    def _load_factory(factory_path: str) -> Callable[[], object]:
        module_name, attribute_name = factory_path.split(":", maxsplit=1)
        module = importlib.import_module(module_name)
        factory_object = getattr(module, attribute_name)
        if not callable(factory_object):
            raise TypeError(f"factory {factory_path} 不可调用")
        return cast(Callable[[], object], factory_object)

    def _dependency_closure(self, module_id: str) -> tuple[str, ...]:
        included: set[str] = set()

        def visit(current_id: str) -> None:
            for dependency_id in self._manifest_by_id[current_id].requires:
                if dependency_id not in included:
                    visit(dependency_id)
            included.add(current_id)

        visit(module_id)
        return tuple(
            candidate for candidate in self._activation_order if candidate in included
        )
