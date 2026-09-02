"""定义业务层可租用的真实 capability 最小协议。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from rivet.context.lexical import LexicalSearchResult
    from rivet.kernel.resources import ResourceScope
    from rivet.tools.paths import WorkspaceBoundary
    from rivet.tools.process import ProcessExecutor
    from rivet.verify.service import VerificationOutcome


class LexicalContextCapability(Protocol):
    """为当前任务的有效工作区提供有界 Lexical Context 检索。"""

    async def search(
        self,
        boundary: WorkspaceBoundary,
        query: str,
        *,
        max_results: int = 8,
        paths: tuple[str, ...] = (".",),
    ) -> LexicalSearchResult:
        """只搜索任务边界内的冻结读范围。"""
        ...


class GuardCapability(Protocol):
    """创建资源归 Guard Lease 所有的沙箱进程执行器。"""

    def create_process_runner(
        self,
        boundary: WorkspaceBoundary,
        *,
        executable: Path | None = None,
        scope: ResourceScope | None = None,
        environment: Mapping[str, str] | None = None,
        max_capture_bytes: int = 5 * 1024 * 1024,
    ) -> ProcessExecutor:
        """创建 Bubblewrap 执行器；沙箱不可用时失败关闭。"""
        ...


class VerificationCapability(Protocol):
    """把独立验证器绑定到 Transaction dependency 与 Verify Scope。"""

    async def verify(
        self,
        transaction_id: str,
    ) -> VerificationOutcome:
        """运行七类检查并发布原子 Evidence。"""
        ...
