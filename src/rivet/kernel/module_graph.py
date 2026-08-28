"""验证模块依赖并生成稳定拓扑激活顺序。"""

from __future__ import annotations

import heapq
from collections.abc import Iterable

from rivet.contracts.modules import ModuleManifest
from rivet.kernel.errors import ModuleDependencyError


def stable_activation_order(
    manifests: Iterable[ModuleManifest],
) -> tuple[str, ...]:
    """按模块 ID 打破并列顺序，并拒绝缺失依赖与环。"""
    manifest_by_id: dict[str, ModuleManifest] = {}
    for manifest in manifests:
        if manifest.module_id in manifest_by_id:
            raise ModuleDependencyError(f"模块 ID 重复：{manifest.module_id}")
        manifest_by_id[manifest.module_id] = manifest

    dependents: dict[str, set[str]] = {module_id: set() for module_id in manifest_by_id}
    remaining_dependencies: dict[str, int] = {}
    for module_id, manifest in manifest_by_id.items():
        missing = sorted(
            dependency
            for dependency in manifest.requires
            if dependency not in manifest_by_id
        )
        if missing:
            raise ModuleDependencyError(
                f"模块 {module_id} 缺少依赖：{', '.join(missing)}"
            )
        remaining_dependencies[module_id] = len(manifest.requires)
        for dependency in manifest.requires:
            dependents[dependency].add(module_id)

    ready = [
        module_id
        for module_id, dependency_count in remaining_dependencies.items()
        if dependency_count == 0
    ]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        module_id = heapq.heappop(ready)
        ordered.append(module_id)
        for dependent in sorted(dependents[module_id]):
            remaining_dependencies[dependent] -= 1
            if remaining_dependencies[dependent] == 0:
                heapq.heappush(ready, dependent)

    if len(ordered) != len(manifest_by_id):
        cyclic_modules = sorted(
            module_id
            for module_id, count in remaining_dependencies.items()
            if count > 0
        )
        raise ModuleDependencyError(f"模块依赖图存在环：{', '.join(cyclic_modules)}")
    return tuple(ordered)
