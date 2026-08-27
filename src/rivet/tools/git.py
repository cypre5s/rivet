"""只开放 status、diff 和 show 的固定 Git argv。"""

from __future__ import annotations

import re

from rivet.tools.errors import GitToolError
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.process import ProcessRunner, ProcessRunResult
from rivet.tools.workspace import WorkspaceInspector

REVISION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@{}^~:+-]{0,255}$")


class GitService:
    """在非 bare 工作树中执行固定、无 pager 的只读 Git 命令。"""

    def __init__(self, boundary: WorkspaceBoundary, *, runner: ProcessRunner) -> None:
        self._boundary = boundary
        self._runner = runner
        self._inspector = WorkspaceInspector(boundary)

    async def status(self) -> str:
        """返回短格式分支与工作树状态。"""
        await self._require_worktree()
        result = await self._runner.run(
            (
                "git",
                "--no-pager",
                "status",
                "--short",
                "--branch",
                "--untracked-files=normal",
            ),
            timeout_seconds=15.0,
        )
        return self._text_result(result)

    async def diff(self, *, path: str | None = None, cached: bool = False) -> str:
        """返回禁用外部 diff 的工作树或暂存区补丁。"""
        await self._require_worktree()
        arguments = ["git", "--no-pager", "diff", "--no-ext-diff"]
        if cached:
            arguments.append("--cached")
        if path is not None:
            resolved = self._boundary.resolve_repository(path, require_exists=False)
            arguments.extend(("--", self._boundary.repository_relative(resolved)))
        result = await self._runner.run(tuple(arguments), timeout_seconds=30.0)
        return self._text_result(result)

    async def show(self, revision: str, *, path: str | None = None) -> str:
        """显示一个受校验 revision，拒绝将其解释为选项。"""
        await self._require_worktree()
        if not REVISION_PATTERN.fullmatch(revision) or revision.startswith("-"):
            raise GitToolError("git.revision_invalid", "Git revision 格式无效")
        arguments = [
            "git",
            "--no-pager",
            "show",
            "--no-ext-diff",
            "--format=fuller",
            "--stat",
            "--patch",
            revision,
        ]
        if path is not None:
            resolved = self._boundary.resolve_repository(path, require_exists=False)
            arguments.extend(("--", self._boundary.repository_relative(resolved)))
        result = await self._runner.run(tuple(arguments), timeout_seconds=30.0)
        return self._text_result(result)

    async def _require_worktree(self) -> None:
        """稳定拒绝普通目录与 bare 仓库。"""
        info = await self._inspector.info()
        if not info.is_git_repository:
            raise GitToolError("git.repository_missing", "目标不是 Git 仓库")
        if info.is_bare_repository:
            raise GitToolError("git.bare_unsupported", "bare Git 仓库没有工作树")

    @staticmethod
    def _text_result(result: ProcessRunResult) -> str:
        """检查退出码并把有界字节输出转换为安全文本。"""
        if result.returncode != 0 or result.timed_out:
            raise GitToolError("git.command_failed", "固定 Git 命令执行失败")
        text = result.stdout.decode("utf-8", errors="replace")
        if result.stdout_truncated:
            text += "\n[TRUNCATED]\n"
        return text
