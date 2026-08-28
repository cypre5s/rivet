"""提供不递归 symlink 的仓库概览与受限目录清单。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from rivet.tools.errors import WorkspaceToolError
from rivet.tools.paths import WorkspaceBoundary


@dataclass(frozen=True, slots=True)
class WorkspaceInfo:
    """描述仓库根、Git 类型和当前 HEAD 形态。"""

    root: str
    is_git_repository: bool
    is_bare_repository: bool
    detached_head: bool
    current_branch: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceEntry:
    """描述单个目录项但不展开文件内容。"""

    path: str
    kind: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class WorkspaceListing:
    """保存稳定排序的目录项和显式截断状态。"""

    entries: tuple[WorkspaceEntry, ...]
    truncated: bool


class WorkspaceInspector:
    """以纯文件系统元数据实现低成本工作区检查。"""

    def __init__(self, boundary: WorkspaceBoundary) -> None:
        self._boundary = boundary

    async def info(self) -> WorkspaceInfo:
        """读取 Git HEAD 文件，不启动外部进程。"""
        root = self._boundary.effective_root
        git_path = root / ".git"
        is_bare = (
            not git_path.exists()
            and (root / "HEAD").is_file()
            and (root / "objects").is_dir()
            and (root / "refs").is_dir()
        )
        head_path = self._head_path(git_path, root, is_bare)
        if head_path is None or not head_path.is_file():
            return WorkspaceInfo(str(root), False, False, False, None)
        try:
            head = head_path.read_text(encoding="utf-8", errors="strict").strip()
        except (OSError, UnicodeDecodeError) as error:
            raise WorkspaceToolError(
                "workspace.git_head_unreadable", "Git HEAD 无法安全读取"
            ) from error
        branch_prefix = "ref: refs/heads/"
        attached = head.startswith(branch_prefix)
        return WorkspaceInfo(
            root=str(root),
            is_git_repository=True,
            is_bare_repository=is_bare,
            detached_head=not attached,
            current_branch=head.removeprefix(branch_prefix) if attached else None,
        )

    def list(
        self,
        relative_path: str,
        *,
        max_depth: int = 2,
        max_entries: int = 1_000,
    ) -> WorkspaceListing:
        """稳定列出目录且不进入 symlink、.git 或 .rivet。"""
        if max_depth < 0 or max_entries <= 0:
            raise WorkspaceToolError(
                "workspace.list_budget_invalid", "目录深度和条目预算无效"
            )
        root = self._boundary.resolve_repository(relative_path, require_directory=True)
        entries: list[WorkspaceEntry] = []
        truncated = False

        def walk(directory: Path, depth: int) -> None:
            nonlocal truncated
            if truncated:
                return
            try:
                children = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError as error:
                raise WorkspaceToolError(
                    "workspace.list_failed", "目录无法安全列出"
                ) from error
            for child in children:
                if child.name in {".git", ".rivet"}:
                    continue
                if len(entries) >= max_entries:
                    truncated = True
                    return
                child_path = Path(child.path)
                relative = child_path.relative_to(
                    self._boundary.effective_root
                ).as_posix()
                if child.is_symlink():
                    kind = "symlink"
                    size = 0
                elif child.is_dir(follow_symlinks=False):
                    kind = "directory"
                    size = 0
                else:
                    kind = "file"
                    size = child.stat(follow_symlinks=False).st_size
                entries.append(WorkspaceEntry(relative, kind, size))
                if kind == "directory" and depth < max_depth:
                    walk(child_path, depth + 1)

        walk(root, 0)
        return WorkspaceListing(tuple(entries), truncated)

    @staticmethod
    def _head_path(git_path: Path, root: Path, is_bare: bool) -> Path | None:
        """解析普通仓库、worktree 的 gitdir 文件和 bare HEAD。"""
        if is_bare:
            return root / "HEAD"
        if git_path.is_dir():
            return git_path / "HEAD"
        if not git_path.is_file():
            return None
        try:
            marker = git_path.read_text(encoding="utf-8", errors="strict").strip()
        except (OSError, UnicodeDecodeError):
            return None
        prefix = "gitdir: "
        if not marker.startswith(prefix):
            return None
        admin_path = Path(marker.removeprefix(prefix))
        if not admin_path.is_absolute():
            admin_path = git_path.parent / admin_path
        return admin_path.resolve(strict=False) / "HEAD"
