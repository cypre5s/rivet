"""实现惰性模块激活、Lease、隔离恢复、休眠与有界关闭。"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from rivet.contracts.common import CapabilityId, ModuleId
from rivet.contracts.modules import (
    ActivationPolicy,
    ModuleAvailability,
    ModuleManifest,
    ModuleState,
    SleepPolicy,
)
from rivet.kernel.capabilities import CapabilityRegistry
from rivet.kernel.errors import (
    ActivationJournalError,
    CapabilityNotFoundError,
    ModuleActivationError,
    ModuleDependencyError,
    ModuleQuarantinedError,
    ModuleShutdownError,
    ModuleUnavailableError,
    SafeModeViolationError,
)
from rivet.kernel.module_api import ModuleActivationContext, ModuleInstance
from rivet.kernel.module_availability import (
    ModuleAvailabilityReport,
    probe_module_availability,
)
from rivet.kernel.module_graph import stable_activation_order
from rivet.kernel.resources import ResourceCounts, ResourceScope

JOURNAL_SCHEMA_VERSION = 1


class ActivationJournal:
    """在 factory 导入前持久化未完成激活标记。"""

    def __init__(self, path: Path) -> None:
        self.path = path.absolute()

    def pending_module_ids(self) -> frozenset[str]:
        """读取 pending 集合，损坏时失败关闭而不猜测。"""
        self._validate_path()
        if not self.path.exists():
            return frozenset()
        try:
            raw_document: object = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ActivationJournalError("激活日志无法读取或解析") from error
        if not isinstance(raw_document, dict):
            raise ActivationJournalError("激活日志根节点必须是对象")
        document = cast(dict[str, object], raw_document)
        if document.get("schema_version") != JOURNAL_SCHEMA_VERSION:
            raise ActivationJournalError("激活日志协议版本不受支持")
        raw_module_ids = document.get("pending_module_ids")
        if not isinstance(raw_module_ids, list):
            raise ActivationJournalError("激活日志 pending_module_ids 无效")
        module_ids: list[str] = []
        for module_id in cast(list[object], raw_module_ids):
            if not isinstance(module_id, str):
                raise ActivationJournalError("激活日志 pending_module_ids 无效")
            module_ids.append(module_id)
        if len(set(module_ids)) != len(module_ids):
            raise ActivationJournalError("激活日志包含重复模块 ID")
        return frozenset(module_ids)

    def mark_pending(self, module_id: ModuleId) -> None:
        """在危险激活边界前原子加入模块标记。"""
        pending = set(self.pending_module_ids())
        pending.add(module_id)
        self._write(pending)

    def clear_pending(self, module_id: ModuleId) -> None:
        """仅在可确定成功或可控失败后清除模块标记。"""
        pending = set(self.pending_module_ids())
        pending.discard(module_id)
        self._write(pending)

    def _write(self, module_ids: set[str]) -> None:
        """通过同目录替换写入固定版本与稳定排序。"""
        self._validate_path()
        if not module_ids:
            try:
                self.path.unlink(missing_ok=True)
            except OSError as error:
                raise ActivationJournalError("激活日志无法清除") from error
            return
        temporary_path = self.path.with_name(f".{self.path.name}.tmp")
        payload = json.dumps(
            {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "pending_module_ids": sorted(module_ids),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(payload, encoding="utf-8")
            temporary_path.chmod(0o600)
            os.replace(temporary_path, self.path)
        except OSError as error:
            with suppress(OSError):
                temporary_path.unlink()
            raise ActivationJournalError("激活日志无法原子写入") from error

    def _validate_path(self) -> None:
        """拒绝日志文件或已存在父目录的符号链接跳转。"""
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ActivationJournalError("激活日志路径不得是符号链接")


@dataclass(slots=True)
class _RuntimeModule:
    """保存单个模块的进程内状态与资源所有权。"""

    manifest: ModuleManifest
    enabled: bool
    state: ModuleState
    lock: asyncio.Lock
    instance: ModuleInstance | None = None
    capabilities: dict[str, object] | None = None
    scope: ResourceScope | None = None
    lease_count: int = 0
    idle_task: asyncio.Task[None] | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class ModuleRuntimeSnapshot:
    """公开单个模块的实时状态、Lease 和资源事实。"""

    module_id: str
    state: ModuleState
    activation: ActivationPolicy
    manifest_default_enabled: bool
    effective_enabled: bool
    safe_mode_allowed: bool
    manual_control: bool
    scope: str
    sleep_policy: SleepPolicy
    dependencies: tuple[str, ...]
    dependents: tuple[str, ...]
    capabilities: tuple[str, ...]
    availability: ModuleAvailability
    missing_components: tuple[str, ...]
    availability_action: str | None
    lease_count: int
    resource_counts: ResourceCounts
    quarantine_reason: str | None
    last_error: str | None


class CapabilityLease[CapabilityT]:
    """暴露真实 capability，并在使用期间阻止依赖闭包休眠。"""

    def __init__(
        self,
        runtime: ModuleRuntime,
        module_ids: tuple[str, ...],
        *,
        capability_id: str,
        module_id: str,
        capability: CapabilityT,
    ) -> None:
        self._runtime = runtime
        self._module_ids = module_ids
        self.capability_id = capability_id
        self.module_id = module_id
        self.capability = capability
        self._released = False

    async def __aenter__(self) -> CapabilityT:
        """返回已激活的真实 capability。"""
        return self.capability

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object | None,
    ) -> None:
        """离开上下文时幂等释放 Lease。"""
        del exception_type, exception, traceback
        await self.release()

    async def release(self) -> None:
        """归还提供者及依赖 Lease，并重新安排空闲休眠。"""
        if self._released:
            return
        self._released = True
        await self._runtime.release_lease(self._module_ids)


class ModuleRuntime:
    """按 capability 惰性解析模块并维护完整生命周期不变量。"""

    def __init__(
        self,
        manifests: tuple[ModuleManifest, ...],
        *,
        journal: ActivationJournal,
        safe_mode: bool = False,
        enabled_overrides: Mapping[str, bool] | None = None,
        activation_context: ModuleActivationContext | None = None,
    ) -> None:
        self.journal = journal
        self.safe_mode = safe_mode
        self._activation_context = activation_context or ModuleActivationContext(
            repository=journal.path.parent,
            safe_mode=safe_mode,
        )
        if self._activation_context.safe_mode != safe_mode:
            raise ValueError("ModuleActivationContext 与 Runtime Safe Mode 不一致")
        self._activation_order = stable_activation_order(manifests)
        self._manifest_by_id = {manifest.module_id: manifest for manifest in manifests}
        self._registry = CapabilityRegistry(manifests)
        pending_module_ids = journal.pending_module_ids()
        overrides = {} if enabled_overrides is None else dict(enabled_overrides)
        unknown_overrides = sorted(set(overrides).difference(self._manifest_by_id))
        if unknown_overrides:
            raise ModuleDependencyError(
                f"启用覆盖包含未知模块：{', '.join(unknown_overrides)}"
            )
        self._modules = {
            manifest.module_id: _RuntimeModule(
                manifest=manifest,
                enabled=(
                    True
                    if manifest.activation is ActivationPolicy.REQUIRED
                    else overrides.get(manifest.module_id, manifest.enabled)
                ),
                state=(
                    ModuleState.QUARANTINED
                    if manifest.module_id in pending_module_ids
                    else ModuleState.INACTIVE
                ),
                lock=asyncio.Lock(),
            )
            for manifest in manifests
        }
        for module_id, node in self._modules.items():
            self._registry.set_module_enabled(module_id, node.enabled)
        self._active_sequence: list[str] = []
        self._started = False
        self._shutting_down = False
        if safe_mode:
            self._validate_safe_mode_dependencies()

    async def start(self) -> None:
        """仅激活启用的 required 模块，保持可选模块未导入。"""
        if self._started:
            return
        self._started = True
        for module_id in self._activation_order:
            manifest = self._manifest_by_id[module_id]
            if self.effective_enabled(module_id) and manifest.activation in {
                ActivationPolicy.REQUIRED,
                ActivationPolicy.EAGER,
            }:
                await self._activate_module(module_id)

    async def resolve(self, capability_id: CapabilityId) -> object:
        """激活唯一提供者并返回 Manifest 对应的真实 capability。"""
        manifest = self._resolve_manifest(capability_id)
        await self._activate_module(manifest.module_id)
        capability = self._capability(manifest.module_id, capability_id)
        for module_id in self._dependency_closure(manifest.module_id):
            self._schedule_idle(module_id)
        return capability

    async def acquire(self, capability_id: CapabilityId) -> CapabilityLease[object]:
        """激活能力并同时租用其完整依赖闭包。"""
        manifest = self._resolve_manifest(capability_id)
        await self._activate_module(manifest.module_id)
        capability = self._capability(manifest.module_id, capability_id)
        module_ids = self._dependency_closure(manifest.module_id)
        for module_id in module_ids:
            node = self._modules[module_id]
            await self._cancel_idle(node)
            node.lease_count += 1
            node.state = ModuleState.ACTIVE
        return CapabilityLease(
            self,
            module_ids,
            capability_id=capability_id,
            module_id=manifest.module_id,
            capability=capability,
        )

    def state(self, module_id: ModuleId) -> ModuleState:
        """返回模块当前状态，未知 ID 使用与 capability 一致的清晰错误。"""
        node = self._modules.get(module_id)
        if node is None:
            raise CapabilityNotFoundError(f"未知模块 {module_id}")
        return node.state

    def manifest(self, module_id: ModuleId) -> ModuleManifest:
        """返回静态 Manifest，未知 ID 使用稳定错误。"""
        node = self._modules.get(module_id)
        if node is None:
            raise CapabilityNotFoundError(f"未知模块 {module_id}")
        return node.manifest

    def configured_enabled(self, module_id: ModuleId) -> bool:
        """返回 Manifest 与持久化覆盖合成后的启用策略。"""
        node = self._modules.get(module_id)
        if node is None:
            raise CapabilityNotFoundError(f"未知模块 {module_id}")
        return node.enabled

    def effective_enabled(self, module_id: ModuleId) -> bool:
        """应用 Safe Mode 与系统约束后返回实际可激活策略。"""
        node = self._modules.get(module_id)
        if node is None:
            raise CapabilityNotFoundError(f"未知模块 {module_id}")
        if node.manifest.activation is ActivationPolicy.REQUIRED:
            return True
        if self.safe_mode and not node.manifest.safe_mode_allowed:
            return False
        return node.enabled

    def availability(self, module_id: ModuleId) -> ModuleAvailabilityReport:
        """不导入实现地返回当前本机的激活前提。"""
        node = self._modules.get(module_id)
        if node is None:
            raise CapabilityNotFoundError(f"未知模块 {module_id}")
        return probe_module_availability(node.manifest, safe_mode=self.safe_mode)

    def set_configured_enabled(self, module_id: ModuleId, enabled: bool) -> bool:
        """仅供统一生命周期服务在持久化成功后更新运行策略。"""
        node = self._modules.get(module_id)
        if node is None:
            raise CapabilityNotFoundError(f"未知模块 {module_id}")
        previous = node.enabled
        node.enabled = enabled
        self._registry.set_module_enabled(module_id, enabled)
        return previous != enabled

    def dependency_closure(self, module_id: ModuleId) -> tuple[str, ...]:
        """公开按拓扑顺序排列的依赖闭包。"""
        if module_id not in self._modules:
            raise CapabilityNotFoundError(f"未知模块 {module_id}")
        return self._dependency_closure(module_id)

    def dependent_closure(self, module_id: ModuleId) -> tuple[str, ...]:
        """公开按拓扑顺序排列的递归依赖者闭包。"""
        if module_id not in self._modules:
            raise CapabilityNotFoundError(f"未知模块 {module_id}")
        included = {module_id}
        changed = True
        while changed:
            changed = False
            for candidate, manifest in self._manifest_by_id.items():
                if candidate in included or not set(manifest.requires) & included:
                    continue
                included.add(candidate)
                changed = True
        return tuple(
            candidate for candidate in self._activation_order if candidate in included
        )

    def active_dependents(self, module_id: ModuleId) -> tuple[str, ...]:
        """列出当前持有实例的直接或间接依赖者。"""
        closure = self.dependent_closure(module_id)
        return tuple(
            candidate
            for candidate in closure
            if candidate != module_id
            and self._modules[candidate].state
            in {ModuleState.ACTIVE, ModuleState.IDLE, ModuleState.ACTIVATING}
        )

    def enabled_dependents(self, module_id: ModuleId) -> tuple[str, ...]:
        """列出仍配置为启用的直接或间接依赖者。"""
        closure = self.dependent_closure(module_id)
        return tuple(
            candidate
            for candidate in closure
            if candidate != module_id and self._modules[candidate].enabled
        )

    def lease_blockers(self, module_id: ModuleId) -> tuple[str, ...]:
        """返回不泄露任务内容的活动 Lease 摘要。"""
        node = self._modules.get(module_id)
        if node is None:
            raise CapabilityNotFoundError(f"未知模块 {module_id}")
        if node.lease_count == 0:
            return ()
        return (f"{module_id} 存在 {node.lease_count} 个活动 Lease",)

    async def wake_module(self, module_id: ModuleId) -> tuple[str, ...]:
        """经正式激活路径唤醒模块，并回滚本次已激活依赖。"""
        if module_id not in self._modules:
            raise CapabilityNotFoundError(f"未知模块 {module_id}")
        closure = self._dependency_closure(module_id)
        previous_active = {
            candidate
            for candidate in closure
            if self._modules[candidate].state in {ModuleState.ACTIVE, ModuleState.IDLE}
        }
        try:
            await self._activate_module(module_id)
        except BaseException:
            for candidate in reversed(closure):
                if candidate in previous_active:
                    continue
                node = self._modules[candidate]
                if node.state in {ModuleState.ACTIVE, ModuleState.IDLE}:
                    with suppress(Exception):
                        await self.sleep_module(candidate)
            raise
        return tuple(
            candidate for candidate in closure if candidate not in previous_active
        )

    def snapshots(self) -> tuple[ModuleRuntimeSnapshot, ...]:
        """按拓扑顺序返回当前运行时事实，不激活任何模块。"""
        snapshots: list[ModuleRuntimeSnapshot] = []
        for module_id in self._activation_order:
            node = self._modules[module_id]
            availability = self.availability(module_id)
            snapshots.append(
                ModuleRuntimeSnapshot(
                    module_id=module_id,
                    state=node.state,
                    activation=node.manifest.activation,
                    manifest_default_enabled=node.manifest.enabled,
                    effective_enabled=self.effective_enabled(module_id),
                    safe_mode_allowed=node.manifest.safe_mode_allowed,
                    manual_control=node.manifest.manual_control,
                    scope=node.manifest.scope.value,
                    sleep_policy=node.manifest.sleep_policy,
                    dependencies=node.manifest.requires,
                    dependents=tuple(
                        candidate
                        for candidate in self.dependent_closure(module_id)
                        if candidate != module_id
                    ),
                    capabilities=node.manifest.provides,
                    availability=availability.state,
                    missing_components=availability.missing_components,
                    availability_action=availability.suggested_action,
                    lease_count=node.lease_count,
                    resource_counts=(
                        node.scope.counts()
                        if node.scope is not None
                        else ResourceCounts()
                    ),
                    quarantine_reason=(
                        "上次激活未完成"
                        if node.state is ModuleState.QUARANTINED
                        else None
                    ),
                    last_error=node.last_error,
                )
            )
        return tuple(snapshots)

    def provider_module_id(self, capability_id: CapabilityId) -> ModuleId:
        """只解析 capability 提供者标识，不触发 factory 导入。"""
        return self._registry.provider_for(capability_id).module_id

    async def sleep_module(self, module_id: ModuleId) -> bool:
        """无 Lease 时休眠可选模块并回收其全部 scope 资源。"""
        node = self._modules.get(module_id)
        if node is None:
            raise CapabilityNotFoundError(f"未知模块 {module_id}")
        async with node.lock:
            if (
                node.manifest.activation
                in {
                    ActivationPolicy.REQUIRED,
                    ActivationPolicy.EAGER,
                }
                or node.manifest.sleep_policy is SleepPolicy.NEVER
            ):
                return False
            if node.lease_count:
                return False
            if self.active_dependents(module_id):
                return False
            if node.state not in {ModuleState.ACTIVE, ModuleState.IDLE}:
                return False
            await self._cancel_idle(node)
            node.state = ModuleState.SLEEPING
            instance = node.instance
            scope = node.scope
            try:
                if instance is not None:
                    await instance.sleep()
                if scope is not None:
                    await scope.close()
                    scope.assert_empty()
            except asyncio.CancelledError:
                if scope is not None:
                    with suppress(Exception):
                        await asyncio.shield(scope.close())
                node.state = ModuleState.FAILED
                node.last_error = "模块休眠被取消"
                raise
            except Exception as error:
                node.state = ModuleState.FAILED
                node.last_error = "模块休眠或资源清理失败"
                raise ModuleActivationError(f"模块 {module_id} 休眠失败") from error
            node.instance = None
            node.capabilities = None
            node.scope = None
            with suppress(ValueError):
                self._active_sequence.remove(module_id)
            node.state = ModuleState.INACTIVE
            return True

    def resource_counts(self) -> ResourceCounts:
        """汇总所有当前 module scope 的活动资源。"""
        total = ResourceCounts()
        for node in self._modules.values():
            if node.scope is not None:
                total += node.scope.counts()
        return total

    async def shutdown(self) -> None:
        """逆激活顺序关闭模块并以资源归零作为确定性门禁。"""
        if self._shutting_down:
            return
        self._shutting_down = True
        first_error: Exception | None = None
        for node in self._modules.values():
            try:
                await self._cancel_idle(node)
            except Exception as error:
                if first_error is None:
                    first_error = error
        active_module_ids = set(self._active_sequence) | {
            module_id
            for module_id, node in self._modules.items()
            if node.instance is not None or node.scope is not None
        }
        shutdown_order = tuple(
            module_id
            for module_id in reversed(self._activation_order)
            if module_id in active_module_ids
        )
        for module_id in shutdown_order:
            node = self._modules[module_id]
            async with node.lock:
                instance = node.instance
                scope = node.scope
                cleanup_succeeded = True
                try:
                    if instance is not None:
                        await instance.shutdown()
                except Exception as error:
                    cleanup_succeeded = False
                    if first_error is None:
                        first_error = error
                try:
                    if scope is not None:
                        await scope.close()
                        scope.assert_empty()
                except Exception as error:
                    cleanup_succeeded = False
                    if first_error is None:
                        first_error = error
                node.lease_count = 0
                if cleanup_succeeded:
                    node.instance = None
                    node.capabilities = None
                    node.scope = None
                if cleanup_succeeded and node.state is not ModuleState.QUARANTINED:
                    node.state = ModuleState.INACTIVE
                elif not cleanup_succeeded:
                    node.state = ModuleState.FAILED
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

    async def _activate_module(self, module_id: str) -> ModuleInstance:
        """按依赖递归激活，并用每模块 Lock 去重并发调用。"""
        node = self._modules[module_id]
        async with node.lock:
            if node.state in {ModuleState.ACTIVE, ModuleState.IDLE}:
                if node.instance is None:
                    raise ModuleActivationError(f"模块 {module_id} 状态与实例不一致")
                return node.instance
            if node.state is ModuleState.QUARANTINED:
                raise ModuleQuarantinedError(
                    f"模块 {module_id} 因上次激活中断已进入 QUARANTINED"
                )
            if node.state is ModuleState.FAILED:
                raise ModuleActivationError(f"模块 {module_id} 已处于 FAILED")
            if not self.effective_enabled(module_id):
                raise ModuleActivationError(f"模块 {module_id} 已禁用")
            if (
                self.safe_mode
                and node.manifest.activation is not ActivationPolicy.REQUIRED
                and not node.manifest.safe_mode_allowed
            ):
                raise SafeModeViolationError(
                    f"Safe Mode 不允许激活可选模块 {module_id}"
                )
            availability = self.availability(module_id)
            if availability.state is not ModuleAvailability.AVAILABLE:
                raise ModuleUnavailableError(
                    f"模块 {module_id} 当前不可用：{availability.state.value}",
                    availability=availability.state.value,
                    missing_components=availability.missing_components,
                    suggested_action=availability.suggested_action,
                )
            for dependency_id in sorted(node.manifest.requires):
                await self._activate_module(dependency_id)

            node.state = ModuleState.ACTIVATING
            self.journal.mark_pending(module_id)
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
                        raise ModuleDependencyError(
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
                if actual != expected:
                    missing = sorted(expected - actual)
                    unexpected = sorted(actual - expected)
                    raise TypeError(
                        "模块 capability mapping 与 Manifest 不一致："
                        f"missing={missing}, unexpected={unexpected}"
                    )
                if any(value is None for value in capability_mapping.values()):
                    raise TypeError("模块不得返回空 capability")
            except asyncio.CancelledError:
                cleanup_failed = False
                if instance is not None:
                    try:
                        await instance.shutdown()
                    except Exception:
                        cleanup_failed = True
                try:
                    await scope.close()
                except Exception:
                    cleanup_failed = True
                    node.instance = instance
                    node.scope = scope
                node.state = ModuleState.FAILED
                node.last_error = "模块激活被取消"
                if not cleanup_failed:
                    self.journal.clear_pending(module_id)
                raise
            except Exception as error:
                cleanup_failed = False
                if instance is not None:
                    try:
                        await instance.shutdown()
                    except Exception:
                        cleanup_failed = True
                try:
                    await scope.close()
                except Exception:
                    cleanup_failed = True
                    node.instance = instance
                    node.scope = scope
                node.state = ModuleState.FAILED
                node.last_error = "模块激活失败"
                if not cleanup_failed:
                    self.journal.clear_pending(module_id)
                raise ModuleActivationError(f"模块 {module_id} 激活失败") from error

            self.journal.clear_pending(module_id)
            node.instance = instance
            node.capabilities = capability_mapping
            node.scope = scope
            node.state = ModuleState.ACTIVE
            node.last_error = None
            with suppress(ValueError):
                self._active_sequence.remove(module_id)
            self._active_sequence.append(module_id)
            return instance

    def _capability(self, module_id: str, capability_id: str) -> object:
        """从已激活模块读取严格校验过的 capability。"""
        capabilities = self._modules[module_id].capabilities
        if capabilities is None or capability_id not in capabilities:
            raise ModuleActivationError(
                f"模块 {module_id} 未提供 capability {capability_id}"
            )
        return capabilities[capability_id]

    def _resolve_manifest(self, capability_id: CapabilityId) -> ModuleManifest:
        """应用 registry 与 Safe Mode 权限边界。"""
        manifest = self._registry.provider_for(capability_id)
        if (
            self.safe_mode
            and manifest.activation is not ActivationPolicy.REQUIRED
            and not manifest.safe_mode_allowed
        ):
            raise SafeModeViolationError(
                f"Safe Mode 不允许激活可选模块 {manifest.module_id}"
            )
        return manifest

    @staticmethod
    def _load_factory(factory_path: str) -> Callable[[], object]:
        """仅在激活边界解析字符串 factory 并验证可调用性。"""
        module_name, attribute_name = factory_path.split(":", maxsplit=1)
        module = importlib.import_module(module_name)
        factory_object = getattr(module, attribute_name)
        if not callable(factory_object):
            raise TypeError(f"factory {factory_path} 不可调用")
        return cast(Callable[[], object], factory_object)

    def _dependency_closure(self, module_id: str) -> tuple[str, ...]:
        """按全局拓扑顺序返回依赖闭包与目标模块。"""
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

    async def release_lease(self, module_ids: tuple[str, ...]) -> None:
        """逆依赖顺序减少 Lease，并在归零时安排空闲释放。"""
        for module_id in reversed(module_ids):
            node = self._modules[module_id]
            if node.lease_count <= 0:
                raise ModuleActivationError(f"模块 {module_id} Lease 计数下溢")
            node.lease_count -= 1
            if node.lease_count == 0:
                self._schedule_idle(module_id)

    def _schedule_idle(self, module_id: str) -> None:
        """为无 Lease 的可选活动模块创建唯一空闲等待任务。"""
        node = self._modules[module_id]
        timeout = node.manifest.idle_timeout_seconds
        if (
            timeout is None
            or node.manifest.activation is ActivationPolicy.REQUIRED
            or node.lease_count
            or node.scope is None
            or node.state not in {ModuleState.ACTIVE, ModuleState.IDLE}
        ):
            return
        previous_task = node.idle_task
        if previous_task is not None and not previous_task.done():
            previous_task.cancel()

        async def wait_until_idle() -> None:
            await asyncio.sleep(timeout)
            await self.sleep_module(module_id)

        node.idle_task = node.scope.create_task(
            wait_until_idle(), description="模块空闲休眠定时器"
        )
        node.state = ModuleState.IDLE

    async def _cancel_idle(self, node: _RuntimeModule) -> None:
        """取消唯一空闲等待任务，避免关闭时留下后台 Task。"""
        task = node.idle_task
        node.idle_task = None
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def _validate_safe_mode_dependencies(self) -> None:
        """拒绝 required 模块通过依赖间接加载可选 factory。"""
        for manifest in self._manifest_by_id.values():
            if (
                manifest.activation is not ActivationPolicy.REQUIRED
                and not manifest.safe_mode_allowed
            ):
                continue
            optional_dependencies = [
                dependency_id
                for dependency_id in self._dependency_closure(manifest.module_id)
                if self._manifest_by_id[dependency_id].activation
                is not ActivationPolicy.REQUIRED
                and not self._manifest_by_id[dependency_id].safe_mode_allowed
            ]
            if optional_dependencies:
                raise ModuleDependencyError(
                    f"Safe Mode required 模块 {manifest.module_id} "
                    f"依赖可选模块：{', '.join(optional_dependencies)}"
                )
