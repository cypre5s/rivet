"""提供只编排 Manifest 与 ModuleRuntime 的最小常驻 Kernel。"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path

from rivet.contracts.common import CapabilityId
from rivet.contracts.modules import ModuleManifest
from rivet.kernel.manifests import ManifestLoader
from rivet.kernel.module_api import ModuleActivationContext
from rivet.kernel.module_lifecycle import (
    InMemoryModuleOverrideRepository,
    ModuleLifecycleService,
    ModuleOverrideRepository,
)
from rivet.kernel.module_runtime import (
    ActivationJournal,
    CapabilityLease,
    ModuleRuntime,
)


class RivetKernel:
    """保持薄边界，只负责模块运行时启动、解析与关闭。"""

    def __init__(
        self,
        runtime: ModuleRuntime,
        module_lifecycle: ModuleLifecycleService,
    ) -> None:
        self.runtime = runtime
        self.module_lifecycle = module_lifecycle

    @classmethod
    def from_manifests(
        cls,
        manifests: tuple[ModuleManifest, ...],
        *,
        journal_path: Path,
        safe_mode: bool = False,
        enabled_overrides: dict[str, bool] | None = None,
        persisted_overrides: dict[str, bool | None] | None = None,
        override_repository: ModuleOverrideRepository | None = None,
        activation_context: ModuleActivationContext | None = None,
    ) -> RivetKernel:
        """从已验证 Manifest 构造无副作用 Kernel。"""
        runtime = ModuleRuntime(
            manifests,
            journal=ActivationJournal(journal_path),
            safe_mode=safe_mode,
            enabled_overrides=enabled_overrides,
            activation_context=activation_context,
        )
        return cls(
            runtime,
            ModuleLifecycleService(
                runtime,
                override_repository or InMemoryModuleOverrideRepository(),
                persisted_overrides=persisted_overrides,
            ),
        )

    @classmethod
    def from_manifest_paths(
        cls,
        paths: Iterable[Path],
        *,
        journal_path: Path,
        safe_mode: bool = False,
    ) -> RivetKernel:
        """静态加载 TOML 后构造 Kernel，仍不导入 factory。"""
        manifests = ManifestLoader().load_paths(paths)
        return cls.from_manifests(
            manifests, journal_path=journal_path, safe_mode=safe_mode
        )

    async def start(self) -> None:
        """启动 required 模块。"""
        await self.runtime.start()

    async def resolve(self, capability_id: CapabilityId) -> object:
        """委托运行时按需解析能力。"""
        return await self.runtime.resolve(capability_id)

    async def acquire(self, capability_id: CapabilityId) -> CapabilityLease[object]:
        """委托运行时租用并返回真实 capability。"""
        return await self.runtime.acquire(capability_id)

    @asynccontextmanager
    async def capability(self, capability_id: CapabilityId) -> AsyncGenerator[object]:
        """以结构化上下文保证 capability Lease 必定归还。"""
        lease = await self.acquire(capability_id)
        try:
            yield lease.capability
        finally:
            await lease.release()

    async def shutdown(self) -> None:
        """关闭运行时并执行资源归零门禁。"""
        await self.runtime.shutdown()
