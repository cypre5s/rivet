"""解析按仓库隔离的 XDG 状态目录与可重建缓存目录。"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from rivet.trace.errors import RuntimePathError


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """集中描述按仓库隔离的状态与可重建 Worktree 缓存路径。"""

    repository_root: Path
    repository_id: str
    runtime_root: Path
    events_path: Path
    transactions_root: Path
    evidence_root: Path
    cache_root: Path
    worktrees_root: Path

    @classmethod
    def for_repository(
        cls,
        repository_root: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> RuntimePaths:
        """只解析路径，不在构造阶段创建目录。"""
        resolved_repository = repository_root.resolve()
        selected_environment = os.environ if environment is None else environment
        xdg_cache_value = selected_environment.get("XDG_CACHE_HOME")
        if xdg_cache_value:
            xdg_cache_root = Path(xdg_cache_value)
            if not xdg_cache_root.is_absolute():
                raise RuntimePathError("XDG_CACHE_HOME 必须是绝对路径")
        else:
            xdg_cache_root = Path.home() / ".cache"
        xdg_state_value = selected_environment.get("XDG_STATE_HOME")
        if xdg_state_value:
            xdg_state_root = Path(xdg_state_value)
            if not xdg_state_root.is_absolute():
                raise RuntimePathError("XDG_STATE_HOME 必须是绝对路径")
        else:
            xdg_state_root = Path.home() / ".local" / "state"
        repository_id = _repository_id(resolved_repository)
        runtime_root = xdg_state_root.resolve() / "rivet" / repository_id
        if _is_relative_to(runtime_root, resolved_repository) or _is_relative_to(
            resolved_repository, runtime_root
        ):
            raise RuntimePathError("XDG_STATE_HOME 必须位于目标仓库之外")
        return cls(
            repository_root=resolved_repository,
            repository_id=repository_id,
            runtime_root=runtime_root,
            events_path=runtime_root / "trace" / "events.ndjson",
            transactions_root=runtime_root / "transactions",
            evidence_root=runtime_root / "evidence",
            cache_root=xdg_cache_root.resolve() / "rivet",
            worktrees_root=xdg_cache_root.resolve() / "rivet" / "worktrees",
        )

    def prepare(self) -> None:
        """只为 Trace 显式创建私有目录，并拒绝不存在的仓库根。"""
        if not self.repository_root.is_dir():
            raise RuntimePathError(f"仓库目录不存在：{self.repository_root}")
        for directory in (
            self.runtime_root,
            self.events_path.parent,
        ):
            if directory.is_symlink():
                raise RuntimePathError(f"运行状态目录不得是符号链接：{directory}")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not directory.is_dir():
                raise RuntimePathError(f"运行状态路径不是目录：{directory}")
            directory.chmod(0o700)


def _is_relative_to(path: Path, root: Path) -> bool:
    """判断规范路径是否位于另一路径内部。"""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _repository_id(repository_root: Path) -> str:
    """用规范仓库路径和 Git common-dir 派生稳定 XDG 状态键。"""
    common_dir = _git_common_dir(repository_root)
    payload = json.dumps(
        {
            "git_common_dir": str(common_dir),
            "repository_root": str(repository_root),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _git_common_dir(repository_root: Path) -> Path:
    """不启动进程地解析普通仓库或 linked-worktree 的 common-dir。"""
    dot_git = repository_root / ".git"
    if dot_git.is_dir() and not dot_git.is_symlink():
        git_dir = dot_git.resolve(strict=True)
    elif dot_git.is_file() and not dot_git.is_symlink():
        try:
            marker = dot_git.read_text(encoding="utf-8", errors="strict").strip()
        except (OSError, UnicodeError) as error:
            raise RuntimePathError("无法解析 Git 状态目录") from error
        if not marker.startswith("gitdir: ") or "\x00" in marker:
            raise RuntimePathError("Git worktree 标记无效")
        candidate = Path(marker.removeprefix("gitdir: "))
        git_dir = (
            candidate if candidate.is_absolute() else repository_root / candidate
        ).resolve(strict=False)
    else:
        return repository_root
    common_marker = git_dir / "commondir"
    if not common_marker.is_file() or common_marker.is_symlink():
        return git_dir
    try:
        common_value = common_marker.read_text(
            encoding="utf-8", errors="strict"
        ).strip()
    except (OSError, UnicodeError) as error:
        raise RuntimePathError("无法解析 Git common-dir") from error
    if not common_value or "\x00" in common_value:
        raise RuntimePathError("Git common-dir 标记无效")
    candidate = Path(common_value)
    return (candidate if candidate.is_absolute() else git_dir / candidate).resolve(
        strict=False
    )
