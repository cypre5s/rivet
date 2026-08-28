"""集中执行仓库读取与事务写入的路径授权。"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rivet.tools.errors import PathBoundaryError

PROTECTED_DIRECTORY_PREFIXES = (
    (".git",),
    (".rivet", "evidence"),
)
SENSITIVE_FILENAMES = frozenset(
    {
        ".env",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "service-account.json",
    }
)

WorkspaceMode = Literal["ASK", "PLAN", "FIX"]


@dataclass(frozen=True, slots=True)
class WorkspaceView:
    """描述仓库身份与当前命令实际观察到的唯一文件系统根。"""

    repository_root: Path
    effective_root: Path
    transaction_root: Path | None
    transaction_id: str | None
    mode: WorkspaceMode

    def as_dict(self) -> dict[str, str | None]:
        """返回可写入 Trace、Session 与 IPC 的稳定 JSON 元数据。"""
        return {
            "effective_root": str(self.effective_root),
            "mode": self.mode,
            "repository_root": str(self.repository_root),
            "transaction_id": self.transaction_id,
            "transaction_root": (
                str(self.transaction_root)
                if self.transaction_root is not None
                else None
            ),
        }


def _is_relative_to(path: Path, root: Path) -> bool:
    """兼容清晰表达的授权根包含判断。"""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_sensitive(relative_path: Path) -> bool:
    """识别凭据文件和禁止直接访问的内部目录。"""
    parts = tuple(part.lower() for part in relative_path.parts)
    if ".git" in parts:
        return True
    if any(
        parts[index : index + len(prefix)] == prefix
        for prefix in PROTECTED_DIRECTORY_PREFIXES
        for index in range(len(parts) - len(prefix) + 1)
    ):
        return True
    filename = relative_path.name.lower()
    if filename == ".env.example":
        return False
    return filename in SENSITIVE_FILENAMES or filename.startswith(".env.")


class WorkspaceBoundary:
    """为主仓库只读路径和独立事务写路径提供唯一验证入口。"""

    def __init__(
        self,
        repository_root: Path,
        transaction_root: Path | None = None,
        *,
        transaction_id: str | None = None,
        mode: WorkspaceMode | None = None,
    ) -> None:
        repository = repository_root.resolve(strict=True)
        if not repository.is_dir():
            raise PathBoundaryError(
                "workspace.root_not_directory", "仓库授权根不是目录"
            )
        transaction = (
            transaction_root.resolve(strict=True)
            if transaction_root is not None
            else None
        )
        if transaction is not None:
            if not transaction.is_dir():
                raise PathBoundaryError(
                    "workspace.transaction_not_directory", "事务授权根不是目录"
                )
            if _is_relative_to(transaction, repository) or _is_relative_to(
                repository, transaction
            ):
                raise PathBoundaryError(
                    "workspace.transaction_not_isolated",
                    "事务根必须与主工作区保持独立",
                )
        self.repository_root = repository
        self.transaction_root = transaction
        effective = transaction if transaction is not None else repository
        resolved_mode: WorkspaceMode = mode or (
            "FIX" if transaction is not None else "ASK"
        )
        if resolved_mode == "FIX" and transaction is None:
            raise PathBoundaryError(
                "workspace.transaction_missing", "FIX 视图必须绑定事务根"
            )
        if resolved_mode != "FIX" and transaction is not None:
            raise PathBoundaryError(
                "workspace.mode_root_mismatch", "只读视图不得绑定事务根"
            )
        if transaction_id is not None and transaction is None:
            raise PathBoundaryError(
                "workspace.transaction_missing", "事务标识必须绑定事务根"
            )
        self.workspace_view = WorkspaceView(
            repository_root=repository,
            effective_root=effective,
            transaction_root=transaction,
            transaction_id=transaction_id,
            mode=resolved_mode,
        )

    @property
    def effective_root(self) -> Path:
        """返回所有读、搜、Git、Context、Reader 与进程共同观察的根。"""
        return self.workspace_view.effective_root

    def resolve_repository(
        self,
        relative_path: str,
        *,
        require_exists: bool = True,
        require_file: bool = False,
        require_directory: bool = False,
    ) -> Path:
        """在当前有效视图解析只读路径并拒绝逃逸与凭据文件。"""
        return self._resolve(
            self.effective_root,
            relative_path,
            require_exists=require_exists,
            require_file=require_file,
            require_directory=require_directory,
            reject_symlinks=False,
        )

    def resolve_transaction(
        self,
        relative_path: str,
        *,
        require_exists: bool,
        require_file: bool = False,
        require_directory: bool = False,
    ) -> Path:
        """只在独立事务根内解析写路径并拒绝任何 symlink 分量。"""
        if self.transaction_root is None:
            raise PathBoundaryError(
                "workspace.transaction_missing", "当前工具没有独立事务写入授权"
            )
        return self._resolve(
            self.transaction_root,
            relative_path,
            require_exists=require_exists,
            require_file=require_file,
            require_directory=require_directory,
            reject_symlinks=True,
        )

    def repository_relative(self, path: Path) -> str:
        """把有效视图中的授权路径转换为稳定 POSIX 相对路径。"""
        resolved = path.resolve(strict=False)
        if not _is_relative_to(resolved, self.effective_root):
            raise PathBoundaryError("workspace.path_escape", "路径不属于仓库授权根")
        relative = resolved.relative_to(self.effective_root)
        return "." if not relative.parts else relative.as_posix()

    def transaction_relative(self, path: Path) -> str:
        """把已授权事务路径转换为稳定 POSIX 相对路径。"""
        if self.transaction_root is None:
            raise PathBoundaryError(
                "workspace.transaction_missing", "当前工具没有独立事务写入授权"
            )
        resolved = path.resolve(strict=False)
        if not _is_relative_to(resolved, self.transaction_root):
            raise PathBoundaryError("workspace.path_escape", "路径不属于事务授权根")
        relative = resolved.relative_to(self.transaction_root)
        return "." if not relative.parts else relative.as_posix()

    @staticmethod
    def _validate_relative_text(relative_path: str) -> Path:
        """在接触文件系统前拒绝绝对路径、NUL 与跳转段。"""
        if not relative_path or "\x00" in relative_path:
            raise PathBoundaryError("workspace.path_invalid", "路径必须是非空相对路径")
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise PathBoundaryError(
                "workspace.path_escape", "绝对路径或父目录跳转不在授权范围内"
            )
        return path

    def _resolve(
        self,
        root: Path,
        relative_path: str,
        *,
        require_exists: bool,
        require_file: bool,
        require_directory: bool,
        reject_symlinks: bool,
    ) -> Path:
        """执行共同的规范化、保护路径和文件类型检查。"""
        lexical_path = self._validate_relative_text(relative_path)
        candidate = root / lexical_path
        if reject_symlinks:
            cursor = root
            for part in lexical_path.parts:
                if part == ".":
                    continue
                cursor /= part
                if cursor.is_symlink():
                    raise PathBoundaryError(
                        "workspace.symlink_write_forbidden",
                        "事务写路径不得包含 symlink",
                    )
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                metadata = None
            except OSError as error:
                raise PathBoundaryError(
                    "workspace.path_unreadable", "无法检查事务写路径"
                ) from error
            if (
                metadata is not None
                and stat.S_ISREG(metadata.st_mode)
                and metadata.st_nlink > 1
            ):
                raise PathBoundaryError(
                    "workspace.hardlink_write_forbidden",
                    "事务写路径不得是硬链接文件",
                )
        resolved = candidate.resolve(strict=False)
        if not _is_relative_to(resolved, root):
            raise PathBoundaryError("workspace.path_escape", "路径解析后越过授权根")
        relative = resolved.relative_to(root)
        if _is_sensitive(relative):
            raise PathBoundaryError(
                "workspace.protected_path", "路径属于内部目录或凭据文件"
            )
        if require_exists and not resolved.exists():
            raise PathBoundaryError("workspace.path_missing", "目标路径不存在")
        if require_file and not resolved.is_file():
            raise PathBoundaryError("workspace.path_not_file", "目标路径不是普通文件")
        if require_directory and not resolved.is_dir():
            raise PathBoundaryError("workspace.path_not_directory", "目标路径不是目录")
        return resolved
