"""以静态元数据展示内置能力的生命周期与资源事实。"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class ModuleStatusItem:
    """保存不导入重型实现即可展示的模块状态。"""

    module_id: str
    state: str
    activation: str
    dependencies: tuple[str, ...]
    capabilities: tuple[str, ...]
    resource_count: int
    quarantine_reason: str | None


MODULE_CATALOG = (
    ModuleStatusItem(
        "provider.deepseek",
        "INACTIVE",
        "on_demand",
        (),
        ("provider.chat.completions",),
        0,
        None,
    ),
    ModuleStatusItem(
        "context.lexical",
        "INACTIVE",
        "on_demand",
        (),
        ("context.search.lexical",),
        0,
        None,
    ),
    ModuleStatusItem(
        "context.syntax",
        "INACTIVE",
        "on_demand",
        ("context.lexical",),
        ("context.search.syntax",),
        0,
        None,
    ),
    ModuleStatusItem(
        "context.lsp",
        "INACTIVE",
        "on_demand",
        ("context.syntax",),
        ("context.search.lsp",),
        0,
        None,
    ),
    ModuleStatusItem(
        "reader.core",
        "INACTIVE",
        "on_demand",
        (),
        ("reader.detect", "reader.text", "reader.structured"),
        0,
        None,
    ),
    ModuleStatusItem(
        "reader.rich",
        "INACTIVE",
        "on_demand",
        ("reader.core",),
        (
            "reader.office",
            "reader.pdf",
            "reader.image",
            "reader.audio",
            "reader.video",
            "reader.archive",
        ),
        0,
        None,
    ),
    ModuleStatusItem(
        "transaction.git",
        "INACTIVE",
        "on_demand",
        (),
        ("transaction.worktree",),
        0,
        None,
    ),
    ModuleStatusItem(
        "verify.matrix",
        "INACTIVE",
        "on_demand",
        ("transaction.git",),
        ("verify.deterministic",),
        0,
        None,
    ),
    ModuleStatusItem(
        "guard.sandbox",
        "INACTIVE",
        "on_demand",
        (),
        ("guard.local_execution",),
        0,
        None,
    ),
)


def module_status_mapping() -> dict[str, object]:
    """返回确定顺序且显式包含依赖、资源和隔离原因的映射。"""
    return {
        "modules": [asdict(item) for item in MODULE_CATALOG],
        "schema_version": 1,
        "summary": {
            "active": 0,
            "quarantined": 0,
            "resource_count": 0,
            "total": len(MODULE_CATALOG),
        },
    }
