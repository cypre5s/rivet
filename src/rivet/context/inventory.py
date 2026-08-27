"""建立尊重忽略规则、只读取文件元数据的仓库清单。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from rivet.tools.errors import PathBoundaryError
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.process import ProcessRunner

from .errors import ContextInventoryError

MAX_CONTEXT_FILE_BYTES = 512 * 1024
DEFAULT_MAX_INVENTORY_FILES = 100_000
BUILTIN_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".next",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "venv",
    }
)
SENSITIVE_NAMES = frozenset(
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
MANIFEST_NAMES = frozenset(
    {
        "cargo.toml",
        "composer.json",
        "deno.json",
        "deno.jsonc",
        "go.mod",
        "package.json",
        "pom.xml",
        "pyproject.toml",
        "requirements.txt",
    }
)
BUILD_CONFIG_NAMES = frozenset(
    {
        ".env.example",
        ".gitignore",
        "bun.lock",
        "dockerfile",
        "justfile",
        "makefile",
        "ruff.toml",
        "tsconfig.json",
        "uv.lock",
    }
)
ENTRYPOINT_NAMES = frozenset(
    {
        "__main__.py",
        "app.py",
        "cli.py",
        "index.js",
        "index.ts",
        "main.go",
        "main.py",
        "main.rs",
    }
)
SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".lua",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".sql",
        ".swift",
        ".ts",
        ".tsx",
        ".vue",
    }
)
TEXT_SUFFIXES = SOURCE_SUFFIXES | frozenset(
    {
        ".cfg",
        ".conf",
        ".csv",
        ".ini",
        ".json",
        ".jsonl",
        ".lock",
        ".md",
        ".rst",
        ".toml",
        ".tsv",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)


class FileRole(StrEnum):
    """表示文件在项目结构中的低成本角色。"""

    MANIFEST = "manifest"
    ENTRYPOINT = "entrypoint"
    TEST = "test"
    BUILD_CONFIG = "build_config"
    SOURCE = "source"
    DOCUMENTATION = "documentation"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    """保存单个文件的路径、大小、角色和正文读取资格。"""

    path: str
    size_bytes: int
    modified_ns: int
    role: FileRole
    content_eligible: bool


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """保存稳定排序的文件清单与仓库状态摘要。"""

    entries: tuple[InventoryEntry, ...]
    repository_sha256: str
    truncated: bool = False

    @property
    def manifests(self) -> tuple[str, ...]:
        """返回项目清单路径。"""
        return tuple(
            entry.path for entry in self.entries if entry.role is FileRole.MANIFEST
        )

    @property
    def entrypoints(self) -> tuple[str, ...]:
        """返回可能的正式入口路径。"""
        return tuple(
            entry.path for entry in self.entries if entry.role is FileRole.ENTRYPOINT
        )

    @property
    def tests(self) -> tuple[str, ...]:
        """返回测试文件路径。"""
        return tuple(
            entry.path for entry in self.entries if entry.role is FileRole.TEST
        )

    @property
    def build_configs(self) -> tuple[str, ...]:
        """返回构建与工具配置路径。"""
        return tuple(
            entry.path for entry in self.entries if entry.role is FileRole.BUILD_CONFIG
        )


def classify_repository_path(relative_path: str) -> FileRole:
    """按稳定名称和目录规则推断文件角色。"""
    path = PurePosixPath(relative_path)
    lowered_name = path.name.lower()
    lowered_parts = tuple(part.lower() for part in path.parts)
    if lowered_name in MANIFEST_NAMES:
        return FileRole.MANIFEST
    if lowered_name in ENTRYPOINT_NAMES:
        return FileRole.ENTRYPOINT
    if (
        "tests" in lowered_parts
        or "test" in lowered_parts
        or lowered_name.startswith("test_")
        or lowered_name.endswith((".test.ts", ".test.tsx", ".test.js", ".spec.ts"))
    ):
        return FileRole.TEST
    if lowered_name in BUILD_CONFIG_NAMES or lowered_name.startswith(
        ("dockerfile.", "tsconfig.")
    ):
        return FileRole.BUILD_CONFIG
    if path.suffix.lower() in SOURCE_SUFFIXES:
        return FileRole.SOURCE
    if path.suffix.lower() in {".md", ".rst", ".txt"}:
        return FileRole.DOCUMENTATION
    return FileRole.OTHER


def _is_builtin_ignored(relative_path: str) -> bool:
    """识别生成目录、版本库内部和凭据文件。"""
    path = PurePosixPath(relative_path)
    lowered_parts = tuple(part.lower() for part in path.parts)
    if any(part in BUILTIN_IGNORED_DIRECTORY_NAMES for part in lowered_parts[:-1]):
        return True
    if len(lowered_parts) >= 2 and lowered_parts[:2] == (".rivet", "evidence"):
        return True
    lowered_name = path.name.lower()
    if lowered_name == ".env.example":
        return False
    return lowered_name in SENSITIVE_NAMES or lowered_name.startswith(".env.")


def _can_read_content(path: str, size_bytes: int, maximum: int) -> bool:
    """只允许有界的已知文本与项目配置进入正文检索。"""
    pure_path = PurePosixPath(path)
    return size_bytes <= maximum and (
        pure_path.suffix.lower() in TEXT_SUFFIXES
        or pure_path.name.lower() in MANIFEST_NAMES | BUILD_CONFIG_NAMES
    )


class RepositoryInventoryBuilder:
    """通过固定 ripgrep argv 建立不读取正文的文件清单。"""

    def __init__(
        self,
        boundary: WorkspaceBoundary,
        *,
        runner: ProcessRunner,
        max_files: int = DEFAULT_MAX_INVENTORY_FILES,
        max_content_file_bytes: int = MAX_CONTEXT_FILE_BYTES,
    ) -> None:
        if max_files <= 0 or max_content_file_bytes <= 0:
            raise ValueError("清单文件数和正文大小上限必须大于零")
        self._boundary = boundary
        self._runner = runner
        self._max_files = max_files
        self._max_content_file_bytes = max_content_file_bytes

    async def build(self) -> RepositorySnapshot:
        """读取 NUL 分隔路径并用 stat 构造确定性快照。"""
        arguments = [
            "rg",
            "--files",
            "--hidden",
            "--null",
            "--no-messages",
            "--no-require-git",
        ]
        for directory_name in sorted(BUILTIN_IGNORED_DIRECTORY_NAMES):
            arguments.extend(("--glob", f"!**/{directory_name}/**"))
        arguments.extend(("--glob", "!.rivet/evidence/**"))
        for sensitive_name in sorted(SENSITIVE_NAMES):
            arguments.extend(("--glob", f"!**/{sensitive_name}"))
        result = await self._runner.run(tuple(arguments), timeout_seconds=30.0)
        if result.returncode not in {0, 1} or result.timed_out:
            raise ContextInventoryError("ripgrep 无法建立仓库文件清单")
        if result.stdout_truncated:
            raise ContextInventoryError("仓库文件清单超过进程输出上限")
        decoded: list[str] = []
        for raw_path in result.stdout.split(b"\0"):
            if not raw_path:
                continue
            try:
                relative_path = raw_path.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise ContextInventoryError("仓库包含非 UTF-8 文件名") from error
            normalized = PurePosixPath(relative_path).as_posix()
            if not _is_builtin_ignored(normalized):
                decoded.append(normalized)
        unique_paths = sorted(set(decoded))
        truncated = len(unique_paths) > self._max_files
        entries: list[InventoryEntry] = []
        for relative_path in unique_paths[: self._max_files]:
            try:
                path = self._boundary.resolve_repository(
                    relative_path, require_exists=True, require_file=True
                )
            except PathBoundaryError:
                continue
            stat_result = path.stat()
            entries.append(
                InventoryEntry(
                    path=relative_path,
                    size_bytes=stat_result.st_size,
                    modified_ns=stat_result.st_mtime_ns,
                    role=classify_repository_path(relative_path),
                    content_eligible=_can_read_content(
                        relative_path,
                        stat_result.st_size,
                        self._max_content_file_bytes,
                    ),
                )
            )
        digest = hashlib.sha256()
        for entry in entries:
            digest.update(entry.path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(entry.size_bytes).encode("ascii"))
            digest.update(b":")
            digest.update(str(entry.modified_ns).encode("ascii"))
            digest.update(b"\n")
        return RepositorySnapshot(
            entries=tuple(entries),
            repository_sha256=f"sha256:{digest.hexdigest()}",
            truncated=truncated,
        )
