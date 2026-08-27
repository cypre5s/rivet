"""通过固定 ripgrep argv 提供结构化文本和文件搜索。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from rivet.tools.errors import SearchToolError
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.process import ProcessRunner

SENSITIVE_EXCLUDE_GLOBS = (
    "!.env",
    "!.env.*",
    "!**/.env",
    "!**/.env.*",
    "!credentials.json",
    "!**/credentials.json",
    "!service-account.json",
    "!**/service-account.json",
    "!id_dsa",
    "!id_ecdsa",
    "!id_ed25519",
    "!id_rsa",
    "!**/id_dsa",
    "!**/id_ecdsa",
    "!**/id_ed25519",
    "!**/id_rsa",
    "!.rivet/evidence/**",
    "!**/.rivet/evidence/**",
)


@dataclass(frozen=True, slots=True)
class TextSearchMatch:
    """保存带定位信息的单个文本命中。"""

    path: str
    line_number: int
    column_number: int
    preview: str


@dataclass(frozen=True, slots=True)
class TextSearchResult:
    """保存稳定排序文本命中与截断状态。"""

    matches: tuple[TextSearchMatch, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class FileSearchMatch:
    """保存一个文件名命中。"""

    path: str


@dataclass(frozen=True, slots=True)
class FileSearchResult:
    """保存稳定排序文件名命中与截断状态。"""

    matches: tuple[FileSearchMatch, ...]
    truncated: bool


class SearchService:
    """只调用 ripgrep 固定子命令并解析机器可读输出。"""

    def __init__(self, boundary: WorkspaceBoundary, *, runner: ProcessRunner) -> None:
        self._boundary = boundary
        self._runner = runner

    async def text(
        self,
        pattern: str,
        *,
        paths: tuple[str, ...] = (".",),
        regex: bool = False,
        max_results: int = 200,
    ) -> TextSearchResult:
        """用 JSON 协议搜索文本，默认把用户输入视为字面量。"""
        if not pattern or max_results <= 0:
            raise SearchToolError("search.arguments_invalid", "搜索参数无效")
        normalized_paths = self._validated_paths(paths)
        arguments = ["rg", "--json", "--color=never", "--no-messages"]
        for exclusion in SENSITIVE_EXCLUDE_GLOBS:
            arguments.extend(("--glob", exclusion))
        if not regex:
            arguments.append("--fixed-strings")
        arguments.extend(("--", pattern, *normalized_paths))
        result = await self._runner.run(tuple(arguments), timeout_seconds=30.0)
        if result.returncode not in {0, 1}:
            raise SearchToolError("search.ripgrep_failed", "ripgrep 文本搜索失败")
        matches: list[TextSearchMatch] = []
        parse_truncated = False
        for raw_line in result.stdout.splitlines():
            try:
                event = cast(dict[str, object], json.loads(raw_line))
            except (json.JSONDecodeError, UnicodeDecodeError):
                parse_truncated = True
                continue
            if event.get("type") != "match":
                continue
            data = cast(dict[str, object], event.get("data"))
            path_data = cast(dict[str, object], data.get("path"))
            lines_data = cast(dict[str, object], data.get("lines"))
            submatches = cast(list[object], data.get("submatches"))
            if not submatches:
                continue
            first_submatch = cast(dict[str, object], submatches[0])
            path_text = path_data.get("text")
            line_text = lines_data.get("text")
            line_number = data.get("line_number")
            start = first_submatch.get("start")
            if not (
                isinstance(path_text, str)
                and isinstance(line_text, str)
                and isinstance(line_number, int)
                and isinstance(start, int)
            ):
                raise SearchToolError(
                    "search.protocol_invalid", "ripgrep JSON 命中结构无效"
                )
            matches.append(
                TextSearchMatch(
                    path=Path(path_text).as_posix(),
                    line_number=line_number,
                    column_number=start + 1,
                    preview=line_text.rstrip("\r\n")[:4_096],
                )
            )
        ordered = sorted(
            matches,
            key=lambda match: (match.path, match.line_number, match.column_number),
        )
        truncated = (
            result.stdout_truncated or parse_truncated or len(ordered) > max_results
        )
        return TextSearchResult(tuple(ordered[:max_results]), truncated)

    async def files(
        self,
        glob: str | None = None,
        *,
        max_results: int = 1_000,
    ) -> FileSearchResult:
        """用 NUL 分隔列出尊重 ignore 的文件名。"""
        if max_results <= 0 or (glob is not None and not glob):
            raise SearchToolError("search.arguments_invalid", "文件搜索参数无效")
        arguments = ["rg", "--files", "--null"]
        for exclusion in SENSITIVE_EXCLUDE_GLOBS:
            arguments.extend(("--glob", exclusion))
        if glob is not None:
            arguments.extend(("--glob", glob))
        result = await self._runner.run(tuple(arguments), timeout_seconds=30.0)
        if result.returncode not in {0, 1}:
            raise SearchToolError("search.ripgrep_failed", "ripgrep 文件搜索失败")
        decoded_paths: list[str] = []
        for encoded_path in result.stdout.split(b"\0"):
            if not encoded_path:
                continue
            try:
                decoded_paths.append(encoded_path.decode("utf-8", errors="strict"))
            except UnicodeDecodeError as error:
                raise SearchToolError(
                    "search.path_encoding_invalid", "ripgrep 返回非 UTF-8 文件名"
                ) from error
        ordered = sorted(decoded_paths)
        return FileSearchResult(
            tuple(
                FileSearchMatch(Path(path).as_posix()) for path in ordered[:max_results]
            ),
            result.stdout_truncated or len(ordered) > max_results,
        )

    def _validated_paths(self, paths: tuple[str, ...]) -> tuple[str, ...]:
        """先验证每个搜索根，再传递规范相对路径给 argv。"""
        if not paths:
            raise SearchToolError("search.paths_empty", "搜索路径不得为空")
        return tuple(
            self._boundary.repository_relative(
                self._boundary.resolve_repository(path, require_exists=True)
            )
            for path in paths
        )
