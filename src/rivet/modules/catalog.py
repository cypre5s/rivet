"""声明只服务两条核心闭环的五个惰性生产模块。"""

from __future__ import annotations

from rivet.contracts.modules import ModuleManifest

FACTORY_ROOT = "rivet.modules.factories"

BUILTIN_MODULE_MANIFESTS = (
    ModuleManifest(
        module_id="provider.deepseek",
        factory=f"{FACTORY_ROOT}:create_provider_module",
        provides=("provider.chat.completions",),
    ),
    ModuleManifest(
        module_id="context.lexical",
        factory=f"{FACTORY_ROOT}:create_context_module",
        provides=("context.search.lexical",),
    ),
    ModuleManifest(
        module_id="transaction.git",
        factory=f"{FACTORY_ROOT}:create_transaction_module",
        provides=("transaction.worktree",),
    ),
    ModuleManifest(
        module_id="guard.sandbox",
        factory=f"{FACTORY_ROOT}:create_guard_module",
        provides=("guard.local_execution",),
    ),
    ModuleManifest(
        module_id="verify.matrix",
        factory=f"{FACTORY_ROOT}:create_verify_module",
        provides=("verify.deterministic",),
        requires=("transaction.git", "guard.sandbox"),
    ),
)
