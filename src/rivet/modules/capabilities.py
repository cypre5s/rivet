"""定义业务层可租用的真实 capability 最小协议。"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from rivet.context.engine import ProgressiveContextResult
    from rivet.context.semantic import SemanticRequest, SemanticRetrievalResult
    from rivet.contracts.context import ContextBudget
    from rivet.contracts.readers import ReaderRequest, ReaderResult
    from rivet.readers.base import FileInspection
    from rivet.tools.paths import WorkspaceBoundary
    from rivet.tools.process import ProcessExecutor
    from rivet.tools.registry import ToolAuthorizer, ToolRegistry
    from rivet.verify.detector import ProjectConfiguration
    from rivet.verify.service import VerificationOutcome


class ProgressiveContextCapability(Protocol):
    """提供 Level 0-2 渐进上下文检索。"""

    async def retrieve(
        self,
        task: str,
        *,
        budget: ContextBudget,
        include_syntax: bool | None = False,
    ) -> ProgressiveContextResult:
        """按冻结预算检索仓库上下文。"""
        ...


class SemanticContextCapability(Protocol):
    """提供按需 LSP 与语法降级检索。"""

    async def retrieve(
        self,
        task: str,
        *,
        budget: ContextBudget,
        semantic_request: SemanticRequest | None,
    ) -> SemanticRetrievalResult:
        """执行一次精确语义查询。"""
        ...


class ReaderCapability(Protocol):
    """把格式检测与结构化读取绑定到 Reader 模块。"""

    def detect(self, source: Path, *, source_path: str) -> FileInspection:
        """检测文件并返回唯一 Reader capability。"""
        ...

    async def read(self, request: ReaderRequest) -> ReaderResult:
        """返回受限且显式标记的不可信读取结果。"""
        ...


class WorkspaceToolCapability(Protocol):
    """按任务边界创建资源仍归本模块所有的 Tool Registry。"""

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
        """创建一个受 Module Scope 管理的任务级 Registry。"""
        ...


class VerificationCapability(Protocol):
    """把独立验证器绑定到 Transaction dependency 与 Verify Scope。"""

    async def verify(
        self,
        transaction_id: str,
        *,
        project_configuration: ProjectConfiguration | None,
        configuration_confirmed: bool,
    ) -> VerificationOutcome:
        """运行 V0-V10 并发布原子 Evidence。"""
        ...
