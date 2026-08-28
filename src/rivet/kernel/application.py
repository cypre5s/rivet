"""提供只编排 Manifest 与 ModuleRuntime 的最小常驻 Kernel。"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from rivet.contracts.common import CapabilityId
from rivet.contracts.modules import ModuleManifest
from rivet.kernel.manifests import ManifestLoader
from rivet.kernel.module_api import ModuleInstance
from rivet.kernel.module_runtime import ActivationJournal, ModuleLease, ModuleRuntime


class RivetKernel:
    """保持薄边界，只负责模块运行时启动、解析与关闭。"""

    def __init__(self, runtime: ModuleRuntime) -> None:
        self.runtime = runtime

    @classmethod
    def from_manifests(
        cls,
        manifests: tuple[ModuleManifest, ...],
        *,
        journal_path: Path,
        safe_mode: bool = False,
    ) -> RivetKernel:
        """从已验证 Manifest 构造无副作用 Kernel。"""
        return cls(
            ModuleRuntime(
                manifests,
                journal=ActivationJournal(journal_path),
                safe_mode=safe_mode,
            )
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

    async def resolve(self, capability_id: CapabilityId) -> ModuleInstance:
        """委托运行时按需解析能力。"""
        return await self.runtime.resolve(capability_id)

    async def acquire_lease(self, capability_id: CapabilityId) -> ModuleLease:
        """委托运行时租用能力。"""
        return await self.runtime.acquire_lease(capability_id)

    async def shutdown(self) -> None:
        """关闭运行时并执行资源归零门禁。"""
        await self.runtime.shutdown()
