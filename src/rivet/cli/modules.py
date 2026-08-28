"""从正式 ModuleRuntime 展示内置能力的实时生命周期事实。"""

from __future__ import annotations

from pathlib import Path

from rivet.cli.runtime import create_cli_kernel
from rivet.kernel.module_runtime import ModuleRuntimeSnapshot


def module_status_mapping(
    snapshots: tuple[ModuleRuntimeSnapshot, ...],
) -> dict[str, object]:
    """把不触发额外激活的运行时快照转换为稳定公开映射。"""
    active_count = sum(snapshot.state.value == "ACTIVE" for snapshot in snapshots)
    quarantined_count = sum(
        snapshot.state.value == "QUARANTINED" for snapshot in snapshots
    )
    resource_count = sum(
        snapshot.resource_counts.resource_count for snapshot in snapshots
    )
    modules = [
        {
            "activation": snapshot.activation.value,
            "capabilities": list(snapshot.capabilities),
            "dependencies": list(snapshot.dependencies),
            "lease_count": snapshot.lease_count,
            "module_id": snapshot.module_id,
            "quarantine_reason": snapshot.quarantine_reason,
            "resource_count": snapshot.resource_counts.resource_count,
            "safe_mode_allowed": snapshot.safe_mode_allowed,
            "state": snapshot.state.value,
        }
        for snapshot in snapshots
    ]
    return {
        "modules": modules,
        "schema_version": 1,
        "source": "module_runtime",
        "summary": {
            "active": active_count,
            "quarantined": quarantined_count,
            "resource_count": resource_count,
            "total": len(modules),
        },
    }


async def load_module_status_mapping(
    repository: Path,
    *,
    safe_mode: bool,
) -> dict[str, object]:
    """启动正式 Kernel，读取真实状态并执行资源归零关闭。"""
    kernel = create_cli_kernel(repository, safe_mode=safe_mode)
    try:
        await kernel.start()
        return module_status_mapping(kernel.runtime.snapshots())
    finally:
        await kernel.shutdown()
