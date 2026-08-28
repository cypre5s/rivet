"""声明正式 CLI 启动时可静态解析的内置模块目录。"""

from __future__ import annotations

from rivet.contracts.modules import ActivationPolicy, ModuleManifest

FACTORY_ROOT = "rivet.modules.factories"

BUILTIN_MODULE_MANIFESTS = (
    ModuleManifest(
        module_id="provider.deepseek",
        module_version="1.0.0",
        activation=ActivationPolicy.ON_DEMAND,
        factory=f"{FACTORY_ROOT}:create_provider_module",
        safe_mode_allowed=True,
        provides=("provider.chat.completions",),
        idle_timeout_seconds=None,
    ),
    ModuleManifest(
        module_id="context.lexical",
        module_version="1.0.0",
        activation=ActivationPolicy.ON_DEMAND,
        factory=f"{FACTORY_ROOT}:create_context_module",
        safe_mode_allowed=True,
        provides=("context.search.lexical",),
        idle_timeout_seconds=None,
    ),
    ModuleManifest(
        module_id="context.syntax",
        module_version="1.0.0",
        activation=ActivationPolicy.ON_DEMAND,
        factory=f"{FACTORY_ROOT}:create_context_module",
        provides=("context.search.syntax",),
        requires=("context.lexical",),
    ),
    ModuleManifest(
        module_id="context.lsp",
        module_version="1.0.0",
        activation=ActivationPolicy.ON_DEMAND,
        factory=f"{FACTORY_ROOT}:create_context_module",
        provides=("context.search.lsp",),
        requires=("context.syntax",),
    ),
    ModuleManifest(
        module_id="reader.core",
        module_version="1.0.0",
        activation=ActivationPolicy.ON_DEMAND,
        factory=f"{FACTORY_ROOT}:create_reader_module",
        safe_mode_allowed=True,
        provides=(
            "reader.detect",
            "reader.text",
            "reader.structured",
            "reader.notebook",
            "reader.binary",
        ),
        idle_timeout_seconds=None,
    ),
    ModuleManifest(
        module_id="reader.rich",
        module_version="1.0.0",
        activation=ActivationPolicy.ON_DEMAND,
        factory=f"{FACTORY_ROOT}:create_reader_module",
        provides=(
            "reader.document",
            "reader.image",
            "reader.media",
            "reader.archive",
            "reader.email",
        ),
        requires=("reader.core",),
    ),
    ModuleManifest(
        module_id="transaction.git",
        module_version="1.0.0",
        activation=ActivationPolicy.ON_DEMAND,
        factory=f"{FACTORY_ROOT}:create_transaction_module",
        provides=("transaction.worktree",),
    ),
    ModuleManifest(
        module_id="verify.matrix",
        module_version="1.0.0",
        activation=ActivationPolicy.ON_DEMAND,
        factory=f"{FACTORY_ROOT}:create_verify_module",
        provides=("verify.deterministic",),
        requires=("transaction.git",),
    ),
    ModuleManifest(
        module_id="guard.sandbox",
        module_version="1.0.0",
        activation=ActivationPolicy.ON_DEMAND,
        factory=f"{FACTORY_ROOT}:create_guard_module",
        safe_mode_allowed=True,
        provides=("guard.local_execution",),
        idle_timeout_seconds=None,
    ),
)
