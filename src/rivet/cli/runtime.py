"""把正式 CLI 连接到同一 RivetKernel 与生产模块目录。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from rivet.kernel.application import RivetKernel
from rivet.kernel.module_api import ModuleInstance, ScopedModuleInstance
from rivet.kernel.module_runtime import ModuleLease
from rivet.kernel.resources import ResourceScope
from rivet.modules.catalog import BUILTIN_MODULE_MANIFESTS
from rivet.storage.module_overrides import SQLiteModuleOverrideStore
from rivet.trace.paths import RuntimePaths


def create_cli_kernel(repository: Path, *, safe_mode: bool) -> RivetKernel:
    """构造尚未启动且不会导入模块 factory 的正式 Kernel。"""
    paths = RuntimePaths.for_repository(repository)
    override_store = SQLiteModuleOverrideStore(paths.module_database_path, repository)
    persisted_overrides = override_store.load(BUILTIN_MODULE_MANIFESTS)
    enabled_overrides = {
        module_id: enabled
        for module_id, enabled in persisted_overrides.items()
        if enabled is not None
    }
    return RivetKernel.from_manifests(
        BUILTIN_MODULE_MANIFESTS,
        journal_path=paths.runtime_root / "module-activation.json",
        safe_mode=safe_mode,
        enabled_overrides=enabled_overrides,
        persisted_overrides=persisted_overrides,
        override_repository=override_store,
    )


def module_scope(instance: ModuleInstance) -> ResourceScope:
    """验证生产模块契约后返回其受 ModuleRuntime 管理的资源域。"""
    if not isinstance(instance, ScopedModuleInstance):
        raise TypeError("生产模块未提供受管 ResourceScope")
    return instance.resource_scope


async def shutdown_cli_kernel(
    kernel: RivetKernel,
    leases: Sequence[ModuleLease],
) -> None:
    """尽力释放全部 Lease 和 Kernel，并在清理完成后重抛首个错误。"""
    first_error: BaseException | None = None
    for lease in reversed(leases):
        try:
            await lease.release()
        except BaseException as error:
            if first_error is None:
                first_error = error
    try:
        await kernel.shutdown()
    except BaseException as error:
        if first_error is None:
            first_error = error
    if first_error is not None:
        raise first_error
