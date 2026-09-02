"""维护 capability 到唯一模块的静态映射。"""

from __future__ import annotations

from collections.abc import Iterable

from rivet.contracts.common import CapabilityId
from rivet.contracts.modules import ModuleManifest
from rivet.kernel.errors import CapabilityConflictError, CapabilityNotFoundError


class CapabilityRegistry:
    """注册全部模块能力并在构造期拒绝提供者歧义。"""

    def __init__(self, manifests: Iterable[ModuleManifest]) -> None:
        all_providers: dict[str, ModuleManifest] = {}
        for manifest in manifests:
            for capability_id in manifest.provides:
                previous = all_providers.get(capability_id)
                if previous is not None:
                    raise CapabilityConflictError(
                        f"capability {capability_id} 同时由 "
                        f"{previous.module_id} 与 {manifest.module_id} 提供"
                    )
                all_providers[capability_id] = manifest
        self._providers = all_providers

    def provider_for(self, capability_id: CapabilityId) -> ModuleManifest:
        """返回唯一提供者，缺失时给出稳定错误。"""
        provider = self._providers.get(capability_id)
        if provider is None:
            raise CapabilityNotFoundError(f"没有模块提供 capability {capability_id}")
        return provider

    def capabilities(self) -> tuple[str, ...]:
        """按稳定顺序列出全部可解析能力。"""
        return tuple(sorted(self._providers))
