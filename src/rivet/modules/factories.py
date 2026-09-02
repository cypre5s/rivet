"""提供五个核心能力模块的惰性生产工厂。"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rivet.contracts.common import CapabilityId
from rivet.kernel.module_api import ModuleActivationContext
from rivet.kernel.resources import ResourceScope

if TYPE_CHECKING:
    from rivet.kernel.model_provider import ModelProvider
    from rivet.tools.paths import WorkspaceBoundary
    from rivet.tools.process import ProcessExecutor
    from rivet.transaction.manager import TransactionManager
    from rivet.verify.service import VerificationOutcome

    from .capabilities import GuardCapability


def _create_deepseek_provider(
    context: ModuleActivationContext,
    scope: ResourceScope,
) -> ModelProvider:
    """从受控配置与凭据 accessor 构造正式 Provider。"""
    if context.provider_base_url is None or context.credential_accessor is None:
        raise RuntimeError("Provider 激活缺少受控配置或凭据 accessor")
    from rivet.providers.deepseek import DeepSeekProvider
    from rivet.providers.models import DeepSeekConfig

    credential = context.credential_accessor("DEEPSEEK_API_KEY")
    environment = {} if credential is None else {"DEEPSEEK_API_KEY": credential}
    return DeepSeekProvider(
        DeepSeekConfig(base_url=context.provider_base_url),
        scope=scope,
        environment=environment,
    )


class _ScopeOwnedModule:
    """表示服务状态已由 Runtime capability mapping 与 Scope 完整持有。"""

    async def sleep(self) -> None:
        """服务引用由 Runtime 清除，资源由 Scope 回收。"""

    async def shutdown(self) -> None:
        """服务引用由 Runtime 清除，资源由 Scope 回收。"""


class ProviderCapabilityModule(_ScopeOwnedModule):
    """在 activate 中构造并拥有真实 ModelProvider。"""

    async def activate(
        self,
        context: ModuleActivationContext,
        scope: ResourceScope,
    ) -> Mapping[CapabilityId, object]:
        """创建本地就绪的 Provider 客户端，不执行远端健康检查。"""
        provider = _create_deepseek_provider(context, scope)
        return {"provider.chat.completions": provider}


class ContextCapabilityService:
    """把词法搜索器延迟绑定到每个任务的实际 WorkspaceBoundary。"""

    def __init__(self, scope: ResourceScope) -> None:
        self._scope = scope

    async def search(
        self,
        boundary: WorkspaceBoundary,
        query: str,
        *,
        max_results: int = 8,
        paths: tuple[str, ...] = (".",),
    ):
        """ASK 搜主仓库，FIX 只搜候选 Worktree 的冻结读范围。"""
        from rivet.context.lexical import LexicalContext

        return await LexicalContext(
            boundary.effective_root,
            scope=self._scope,
        ).search(query, max_results=max_results, paths=paths)


class ContextCapabilityModule(_ScopeOwnedModule):
    """只构造 Lexical Context 检索器。"""

    async def activate(
        self,
        context: ModuleActivationContext,
        scope: ResourceScope,
    ) -> Mapping[CapabilityId, object]:
        """初始化仅含 ripgrep/Git inventory 的 Context service。"""
        if context.module_id != "context.lexical":
            raise RuntimeError("Context factory 只允许绑定 context.lexical")
        capability = ContextCapabilityService(scope)
        return {"context.search.lexical": capability}


class TransactionCapabilityModule(_ScopeOwnedModule):
    """拥有真实 TransactionManager 与其 Worktree Scope。"""

    async def activate(
        self,
        context: ModuleActivationContext,
        scope: ResourceScope,
    ) -> Mapping[CapabilityId, object]:
        """初始化 Git backend，并在失败时保持模块非 ACTIVE。"""
        from rivet.transaction.manager import TransactionManager

        manager = TransactionManager(
            context.repository,
            scope=scope,
            state_root=context.transaction_state_root,
            evidence_root=context.evidence_state_root,
            cache_root=context.worktree_cache_root,
        )
        await manager.inspect_repository()
        return {"transaction.worktree": manager}


class VerificationCapabilityService:
    """将 Verify Scope 与已激活 TransactionManager 固定绑定。"""

    def __init__(
        self,
        manager: TransactionManager,
        guard: GuardCapability,
        scope: ResourceScope,
    ) -> None:
        self._manager = manager
        self._guard = guard
        self._scope = scope

    async def verify(
        self,
        transaction_id: str,
    ) -> VerificationOutcome:
        """为本次任务构造独立七类验证器。"""
        from rivet.verify.service import VerificationService

        def executor_factory(
            boundary: WorkspaceBoundary,
            runtime_scope: ResourceScope,
            environment: Mapping[str, str],
            allowlist: frozenset[str],
        ) -> ProcessExecutor:
            del allowlist
            return self._guard.create_process_runner(
                boundary,
                scope=runtime_scope,
                environment=environment,
                max_capture_bytes=1024 * 1024,
            )

        return await VerificationService(
            self._manager,
            scope=self._scope,
            executor_factory=executor_factory,
        ).verify(transaction_id)


class VerificationCapabilityModule(_ScopeOwnedModule):
    """拥有独立 Verify orchestrator factory。"""

    async def activate(
        self,
        context: ModuleActivationContext,
        scope: ResourceScope,
    ) -> Mapping[CapabilityId, object]:
        """绑定 Runtime 已激活的 Transaction dependency。"""
        manager = cast(
            "TransactionManager",
            context.dependencies["transaction.worktree"],
        )
        guard = cast(
            "GuardCapability",
            context.dependencies["guard.local_execution"],
        )
        service = VerificationCapabilityService(manager, guard, scope)
        return {"verify.deterministic": service}


class GuardCapabilityService:
    """用 Guard Scope 创建失败关闭的 Bubblewrap 进程执行器。"""

    def __init__(
        self,
        scope: ResourceScope,
        *,
        executable: Path | None = None,
    ) -> None:
        self._scope = scope
        configured = os.environ.get("RIVET_BWRAP_PATH")
        discovered = shutil.which("bwrap")
        self._executable = Path(executable or configured or discovered or "bwrap")

    @property
    def available(self) -> bool:
        """写入或进程能力激活前即验证 bubblewrap 可执行文件。"""
        return self._executable.is_file() and os.access(self._executable, os.X_OK)

    def create_process_runner(
        self,
        boundary: WorkspaceBoundary,
        *,
        executable: Path | None = None,
        scope: ResourceScope | None = None,
        environment: Mapping[str, str] | None = None,
        max_capture_bytes: int = 5 * 1024 * 1024,
    ) -> ProcessExecutor:
        """创建资源归属当前 Lease、沙箱不可用即拒绝裸跑的执行器。"""
        from rivet.guard.sandbox import BubblewrapSandbox

        return BubblewrapSandbox(
            boundary,
            scope=scope or self._scope,
            executable=executable or self._executable,
            environment=environment,
            max_capture_bytes=max_capture_bytes,
        )


class GuardCapabilityModule(_ScopeOwnedModule):
    """拥有本地安全工具的 task-bound capability factory。"""

    async def activate(
        self,
        context: ModuleActivationContext,
        scope: ResourceScope,
    ) -> Mapping[CapabilityId, object]:
        """构造不启动任何进程的本地工具 factory。"""
        service = GuardCapabilityService(scope)
        if not service.available:
            raise RuntimeError("bubblewrap not found")
        return {"guard.local_execution": service}


def create_provider_module() -> ProviderCapabilityModule:
    """构造模型 Provider 生命周期模块。"""
    return ProviderCapabilityModule()


def create_context_module() -> ContextCapabilityModule:
    """构造上下文检索生命周期模块。"""
    return ContextCapabilityModule()


def create_transaction_module() -> TransactionCapabilityModule:
    """构造隔离事务生命周期模块。"""
    return TransactionCapabilityModule()


def create_verify_module() -> VerificationCapabilityModule:
    """构造确定性验证生命周期模块。"""
    return VerificationCapabilityModule()


def create_guard_module() -> GuardCapabilityModule:
    """构造本地权限与工具生命周期模块。"""
    return GuardCapabilityModule()
