"""实现惰性模块激活、Lease、隔离恢复、休眠与有界关闭。"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from rivet.contracts.common import CapabilityId, ModuleId
from rivet.contracts.modules import ActivationPolicy, ModuleManifest, ModuleState
from rivet.kernel.capabilities import CapabilityRegistry
from rivet.kernel.errors import (
    ActivationJournalError,
    CapabilityNotFoundError,
    ModuleActivationError,
    ModuleDependencyError,
    ModuleQuarantinedError,
    ModuleShutdownError,
    SafeModeViolationError,
)
from rivet.kernel.module_api import ModuleInstance
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
    state: ModuleState
    lock: asyncio.Lock
    instance: ModuleInstance | None = None
    scope: ResourceScope | None = None
    lease_count: int = 0
    idle_task: asyncio.Task[None] | None = None


class ModuleLease:
    """在调用方使用能力期间阻止提供者及其依赖休眠。"""

    def __init__(
        self,
        runtime: ModuleRuntime,
        module_ids: tuple[str, ...],
        instance: ModuleInstance,
    ) -> None:
        self._runtime = runtime
        self._module_ids = module_ids
        self.instance = instance
        self._released = False

    async def __aenter__(self) -> ModuleInstance:
        """返回已激活的提供者实例。"""
        return self.instance

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
    ) -> None:
        self.journal = journal
        self.safe_mode = safe_mode
        self._activation_order = stable_activation_order(manifests)
        self._manifest_by_id = {manifest.module_id: manifest for manifest in manifests}
        self._registry = CapabilityRegistry(manifests)
        pending_module_ids = journal.pending_module_ids()
        self._modules = {
            manifest.module_id: _RuntimeModule(
                manifest=manifest,
                state=(
                    ModuleState.QUARANTINED
                    if manifest.module_id in pending_module_ids
                    else ModuleState.INACTIVE
                ),
                lock=asyncio.Lock(),
            )
            for manifest in manifests
        }
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
            if manifest.enabled and manifest.activation is ActivationPolicy.REQUIRED:
                await self._activate_module(module_id)

    async def resolve(self, capability_id: CapabilityId) -> ModuleInstance:
        """激活唯一提供者并在无 Lease 时安排可选模块休眠。"""
        manifest = self._resolve_manifest(capability_id)
        instance = await self._activate_module(manifest.module_id)
        for module_id in self._dependency_closure(manifest.module_id):
            self._schedule_idle(module_id)
        return instance

    async def acquire_lease(self, capability_id: CapabilityId) -> ModuleLease:
        """激活能力并同时租用其完整依赖闭包。"""
        manifest = self._resolve_manifest(capability_id)
        instance = await self._activate_module(manifest.module_id)
        module_ids = self._dependency_closure(manifest.module_id)
        for module_id in module_ids:
            node = self._modules[module_id]
            await self._cancel_idle(node)
            node.lease_count += 1
            node.state = ModuleState.ACTIVE
        return ModuleLease(self, module_ids, instance)

    def state(self, module_id: ModuleId) -> ModuleState:
        """返回模块当前状态，未知 ID 使用与 capability 一致的清晰错误。"""
        node = self._modules.get(module_id)
        if node is None:
            raise CapabilityNotFoundError(f"未知模块 {module_id}")
        return node.state

    async def sleep_module(self, module_id: ModuleId) -> bool:
        """无 Lease 时休眠可选模块并回收其全部 scope 资源。"""
        node = self._modules.get(module_id)
        if node is None:
            raise CapabilityNotFoundError(f"未知模块 {module_id}")
        async with node.lock:
            if node.manifest.activation is ActivationPolicy.REQUIRED:
                return False
            if node.lease_count:
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
            except Exception as error:
                node.state = ModuleState.FAILED
                raise ModuleActivationError(f"模块 {module_id} 休眠失败") from error
            finally:
                node.instance = None
                node.scope = None
                with suppress(ValueError):
                    self._active_sequence.remove(module_id)
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
        active_module_ids = set(self._active_sequence)
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
                try:
                    if instance is not None:
                        await instance.shutdown()
                except Exception as error:
                    if first_error is None:
                        first_error = error
                try:
                    if scope is not None:
                        await scope.close()
                        scope.assert_empty()
                except Exception as error:
                    if first_error is None:
                        first_error = error
                node.instance = None
                node.scope = None
                node.lease_count = 0
                if node.state is not ModuleState.QUARANTINED:
                    node.state = ModuleState.INACTIVE
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
            if not node.manifest.enabled:
                raise ModuleActivationError(f"模块 {module_id} 已禁用")
            for dependency_id in sorted(node.manifest.requires):
                await self._activate_module(dependency_id)

            node.state = ModuleState.ACTIVATING
            self.journal.mark_pending(module_id)
            scope = ResourceScope(module_id)
            try:
                factory = self._load_factory(node.manifest.factory)
                factory_result = factory()
                if not isinstance(factory_result, ModuleInstance):
                    raise TypeError("factory 返回对象不满足 ModuleInstance 协议")
                instance = factory_result
                await instance.activate(scope)
            except asyncio.CancelledError:
                await scope.close()
                node.state = ModuleState.FAILED
                self.journal.clear_pending(module_id)
                raise
            except Exception as error:
                with suppress(Exception):
                    await scope.close()
                node.state = ModuleState.FAILED
                self.journal.clear_pending(module_id)
                raise ModuleActivationError(f"模块 {module_id} 激活失败") from error

            self.journal.clear_pending(module_id)
            node.instance = instance
            node.scope = scope
            node.state = ModuleState.ACTIVE
            with suppress(ValueError):
                self._active_sequence.remove(module_id)
            self._active_sequence.append(module_id)
            return instance

    def _resolve_manifest(self, capability_id: CapabilityId) -> ModuleManifest:
        """应用 registry 与 Safe Mode 权限边界。"""
        manifest = self._registry.provider_for(capability_id)
        if self.safe_mode and manifest.activation is not ActivationPolicy.REQUIRED:
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
            if manifest.activation is not ActivationPolicy.REQUIRED:
                continue
            optional_dependencies = [
                dependency_id
                for dependency_id in self._dependency_closure(manifest.module_id)
                if self._manifest_by_id[dependency_id].activation
                is not ActivationPolicy.REQUIRED
            ]
            if optional_dependencies:
                raise ModuleDependencyError(
                    f"Safe Mode required 模块 {manifest.module_id} "
                    f"依赖可选模块：{', '.join(optional_dependencies)}"
                )
