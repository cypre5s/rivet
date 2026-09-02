"""提供唯一的、按模型真实需求激活的词法仓库上下文。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from rivet.kernel.resources import ResourceScope
from rivet.tools.errors import FileToolError, SearchToolError
from rivet.tools.files import FileReader
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.process import ProcessRunner
from rivet.tools.search import SearchService

MAX_QUERY_CHARS = 4_096
MAX_SEARCH_TERMS = 24
MAX_MATCHES = 64
MAX_SNIPPET_LINES = 48
MAX_RESULT_CHARS = 32_768

_TERM_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}")
_SENSITIVE_NAMES = frozenset(
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


@dataclass(frozen=True, slots=True)
class LexicalMatch:
    """保存一个有界、可解释且稳定排序的代码片段。"""

    path: str
    start_line: int
    end_line: int
    content: str
    reason: str
    score: int


@dataclass(frozen=True, slots=True)
class LexicalSearchResult:
    """显式区分正常命中与无结果，而不自动升级其他检索能力。"""

    query: str
    status: str
    matches: tuple[LexicalMatch, ...]
    truncated: bool


class LexicalContext:
    """组合 ``git ls-files``、ripgrep 和有界文本读取。"""

    def __init__(self, repository_root: Path, *, scope: ResourceScope) -> None:
        self._boundary = WorkspaceBoundary(repository_root)
        self._runner = ProcessRunner(
            self._boundary,
            scope=scope,
            max_capture_bytes=8 * 1024 * 1024,
            root_kind="repository_read_only",
        )
        self._search = SearchService(self._boundary, runner=self._runner)
        self._reader = FileReader(self._boundary, max_file_bytes=512 * 1024)

    async def search(
        self,
        query: str,
        *,
        max_results: int = 8,
        max_chars: int = MAX_RESULT_CHARS,
        paths: tuple[str, ...] = (".",),
    ) -> LexicalSearchResult:
        """返回有限相关片段；零命中时稳定返回 ``NO_MATCH``。"""
        normalized_query = query.strip()
        if not normalized_query or len(normalized_query) > MAX_QUERY_CHARS:
            raise ValueError("Context 查询必须为 1 到 4096 个字符")
        if not 1 <= max_results <= 32:
            raise ValueError("Context 结果数量必须为 1 到 32")
        if not 1 <= max_chars <= MAX_RESULT_CHARS:
            raise ValueError("Context 输出上限必须为 1 到 32768 字符")

        normalized_paths = tuple(
            self._boundary.repository_relative(
                self._boundary.resolve_repository(path, require_exists=True)
            )
            for path in paths
        )
        if not normalized_paths:
            raise ValueError("Context 读范围不能为空")
        terms = _stable_terms(normalized_query)
        files = await self._tracked_files(normalized_paths)
        if not files:
            return LexicalSearchResult(normalized_query, "NO_MATCH", (), False)

        candidates: dict[str, list[int]] = {}
        if terms:
            pattern = "(?:" + "|".join(re.escape(term) for term in terms) + ")"
            try:
                result = await self._search.text(
                    pattern,
                    paths=normalized_paths,
                    regex=True,
                    max_results=MAX_MATCHES,
                )
            except SearchToolError:
                result = None
            if result is not None:
                for match in result.matches:
                    if match.path in files and _safe_context_path(match.path):
                        candidates.setdefault(match.path, []).append(match.line_number)

        for path in files:
            path_key = path.casefold()
            if any(term.casefold() in path_key for term in terms):
                candidates.setdefault(path, []).append(1)

        ranked_paths = sorted(
            candidates,
            key=lambda path: (-_path_score(path, candidates[path], terms), path),
        )
        selected: list[LexicalMatch] = []
        consumed_chars = 0
        truncated = len(ranked_paths) > max_results
        for path in ranked_paths:
            line_numbers = candidates[path]
            first = max(1, min(line_numbers) - 4)
            last = min(max(line_numbers) + 8, first + MAX_SNIPPET_LINES - 1)
            try:
                snippet = self._reader.read_range(
                    path,
                    start_line=first,
                    end_line=last,
                )
            except (FileToolError, UnicodeDecodeError):
                continue
            remaining = max_chars - consumed_chars
            if remaining <= 0:
                truncated = True
                break
            content = snippet.content
            if len(content) > remaining:
                content = content[:remaining]
                truncated = True
            score = _path_score(path, line_numbers, terms)
            selected.append(
                LexicalMatch(
                    path=path,
                    start_line=snippet.start_line,
                    end_line=snippet.end_line,
                    content=content,
                    reason=_match_reason(path, content, terms),
                    score=score,
                )
            )
            consumed_chars += len(content)
            if len(selected) >= max_results:
                break

        return LexicalSearchResult(
            query=normalized_query,
            status="MATCH" if selected else "NO_MATCH",
            matches=tuple(selected),
            truncated=truncated,
        )

    async def _tracked_files(self, paths: tuple[str, ...]) -> frozenset[str]:
        """用 Git 取得尊重忽略规则的稳定文件清单。"""
        result = await self._runner.run(
            (
                "git",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                *paths,
            ),
            timeout_seconds=10.0,
        )
        if result.returncode != 0 or result.timed_out or result.stdout_truncated:
            raise SearchToolError(
                "context.git_inventory_failed",
                "Git 无法建立 Context 文件清单",
            )
        try:
            decoded = result.stdout.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise SearchToolError(
                "context.path_encoding_invalid",
                "仓库包含非 UTF-8 文件名",
            ) from error
        return frozenset(
            path for path in decoded.split("\0") if path and _safe_context_path(path)
        )


def _stable_terms(query: str) -> tuple[str, ...]:
    seen: set[str] = set()
    terms: list[str] = []
    for term in _TERM_PATTERN.findall(query):
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) == MAX_SEARCH_TERMS:
            break
    return tuple(terms)


def _safe_context_path(path: str) -> bool:
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts:
        return False
    lowered = tuple(part.casefold() for part in pure.parts)
    if not lowered or lowered[0] in {".git", ".rivet"}:
        return False
    name = lowered[-1]
    if name == ".env.example":
        return True
    return name not in _SENSITIVE_NAMES and not name.startswith(".env.")


def _path_score(path: str, lines: list[int], terms: tuple[str, ...]) -> int:
    lowered = path.casefold()
    filename_hits = sum(term.casefold() in lowered for term in terms)
    content_hits = min(len(set(lines)), 8)
    depth_penalty = min(path.count("/"), 8)
    return filename_hits * 100 + content_hits * 10 - depth_penalty


def _match_reason(path: str, content: str, terms: tuple[str, ...]) -> str:
    lowered_path = path.casefold()
    lowered_content = content.casefold()
    path_terms = [term for term in terms if term.casefold() in lowered_path]
    content_terms = [term for term in terms if term.casefold() in lowered_content]
    parts: list[str] = []
    if path_terms:
        parts.append("路径命中 " + ", ".join(path_terms[:4]))
    if content_terms:
        parts.append("正文命中 " + ", ".join(content_terms[:4]))
    return "；".join(parts) or "仓库词法候选"
