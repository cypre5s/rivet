"""提供在 Activation Journal 内构造真实服务的生产模块工厂。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from rivet.contracts.common import CapabilityId
from rivet.kernel.module_api import ModuleActivationContext
from rivet.kernel.resources import ResourceScope

if TYPE_CHECKING:
    from rivet.contracts.readers import ReaderRequest, ReaderResult
    from rivet.kernel.model_provider import ModelProvider
    from rivet.readers.base import FileInspection
    from rivet.tools.paths import WorkspaceBoundary
    from rivet.tools.process import ProcessExecutor
    from rivet.tools.registry import ToolAuthorizer, ToolRegistry
    from rivet.transaction.manager import TransactionManager
    from rivet.verify.detector import ProjectConfiguration
    from rivet.verify.service import VerificationOutcome


class _AsyncCloseable(Protocol):
    """描述 Context 模块需要显式关闭的 capability。"""

    async def close(self) -> None:
        """关闭内部 sidecar。"""
        ...


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


class ContextCapabilityModule:
    """按 Manifest ID 构造真实渐进检索器或语义检索器。"""

    def __init__(self) -> None:
        self._closeable: _AsyncCloseable | None = None

    async def activate(
        self,
        context: ModuleActivationContext,
        scope: ResourceScope,
    ) -> Mapping[CapabilityId, object]:
        """初始化当前层级的 Context service。"""
        if context.module_id in {"context.lexical", "context.syntax"}:
            from rivet.context.engine import ProgressiveContext

            capability = ProgressiveContext(context.repository, scope=scope)
            capability_id = (
                "context.search.lexical"
                if context.module_id == "context.lexical"
                else "context.search.syntax"
            )
            return {capability_id: capability}
        if context.module_id == "context.lsp":
            from rivet.context.lsp_manifest import LspManifestRegistry
            from rivet.context.semantic import SemanticContextRetriever

            capability = SemanticContextRetriever(
                context.repository,
                scope=scope,
                registry=LspManifestRegistry.load_builtin(
                    repository_root=context.repository
                ),
            )
            self._closeable = capability
            return {"context.search.lsp": capability}
        raise RuntimeError("Context factory 绑定了未知模块")

    async def sleep(self) -> None:
        """先关闭可能存在的 LSP sidecar，再由 Scope 执行资源归零。"""
        closeable, self._closeable = self._closeable, None
        if closeable is not None:
            await closeable.close()

    async def shutdown(self) -> None:
        """程序退出与休眠使用同一幂等关闭路径。"""
        await self.sleep()


class ReaderCapabilityService:
    """把格式检测和结构化读取绑定到同一个 Reader Scope。"""

    def __init__(self, repository: Path, scope: ResourceScope) -> None:
        from rivet.readers.service import ReaderService

        self._service = ReaderService(repository, scope=scope)

    def detect(self, source: Path, *, source_path: str) -> FileInspection:
        """执行不导入具体 Reader 实现的格式检测。"""
        from rivet.readers.detection import detect_file

        return detect_file(source, source_path=source_path)

    async def read(self, request: ReaderRequest) -> ReaderResult:
        """委托已属于模块的 ReaderService。"""
        return await self._service.read(request)


class ReaderCapabilityModule(_ScopeOwnedModule):
    """在激活边界构造 Reader service，并映射紧密相关能力。"""

    async def activate(
        self,
        context: ModuleActivationContext,
        scope: ResourceScope,
    ) -> Mapping[CapabilityId, object]:
        """构造 Reader service；具体重量实现仍按文件格式延迟导入。"""
        service = ReaderCapabilityService(context.repository, scope)
        return {
            capability_id: service for capability_id in context.declared_capabilities
        }


class TransactionCapabilityModule(_ScopeOwnedModule):
    """拥有真实 TransactionManager 与其 Worktree Scope。"""

    async def activate(
        self,
        context: ModuleActivationContext,
        scope: ResourceScope,
    ) -> Mapping[CapabilityId, object]:
        """初始化 Git backend，并在失败时保持模块非 ACTIVE。"""
        from rivet.transaction.manager import TransactionManager

        manager = TransactionManager(context.repository, scope=scope)
        await manager.inspect_repository()
        return {"transaction.worktree": manager}


class VerificationCapabilityService:
    """将 Verify Scope 与已激活 TransactionManager 固定绑定。"""

    def __init__(self, manager: TransactionManager, scope: ResourceScope) -> None:
        self._manager = manager
        self._scope = scope

    async def verify(
        self,
        transaction_id: str,
        *,
        project_configuration: ProjectConfiguration | None,
        configuration_confirmed: bool,
    ) -> VerificationOutcome:
        """为本次任务构造独立验证器并执行 V0-V10。"""
        from rivet.verify.service import VerificationService

        return await VerificationService(
            self._manager,
            scope=self._scope,
            project_configuration=project_configuration,
            configuration_confirmed=configuration_confirmed,
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
        service = VerificationCapabilityService(manager, scope)
        return {"verify.deterministic": service}


class WorkspaceToolCapabilityService:
    """确保任务级 Tool Registry 的进程资源归属于 Guard Scope。"""

    def __init__(self, scope: ResourceScope) -> None:
        self._scope = scope

    def create_registry(
        self,
        boundary: WorkspaceBoundary,
        *,
        model_preview_chars: int = 8_192,
        tui_preview_chars: int = 65_536,
        authorizer: ToolAuthorizer | None = None,
        process_executor: ProcessExecutor | None = None,
        read_only: bool = False,
    ) -> ToolRegistry:
        """使用正式 toolset factory 创建受管 Registry。"""
        from rivet.tools.toolset import build_workspace_tool_registry

        return build_workspace_tool_registry(
            boundary,
            scope=self._scope,
            model_preview_chars=model_preview_chars,
            tui_preview_chars=tui_preview_chars,
            authorizer=authorizer,
            process_executor=process_executor,
            read_only=read_only,
        )


class GuardCapabilityModule(_ScopeOwnedModule):
    """拥有本地安全工具的 task-bound capability factory。"""

    async def activate(
        self,
        context: ModuleActivationContext,
        scope: ResourceScope,
    ) -> Mapping[CapabilityId, object]:
        """构造不启动任何进程的本地工具 factory。"""
        service = WorkspaceToolCapabilityService(scope)
        return {"guard.local_execution": service}


def create_provider_module() -> ProviderCapabilityModule:
    """构造模型 Provider 生命周期模块。"""
    return ProviderCapabilityModule()


def create_context_module() -> ContextCapabilityModule:
    """构造上下文检索生命周期模块。"""
    return ContextCapabilityModule()


def create_reader_module() -> ReaderCapabilityModule:
    """构造文件读取生命周期模块。"""
    return ReaderCapabilityModule()


def create_transaction_module() -> TransactionCapabilityModule:
    """构造隔离事务生命周期模块。"""
    return TransactionCapabilityModule()


def create_verify_module() -> VerificationCapabilityModule:
    """构造确定性验证生命周期模块。"""
    return VerificationCapabilityModule()


def create_guard_module() -> GuardCapabilityModule:
    """构造本地权限与工具生命周期模块。"""
    return GuardCapabilityModule()
