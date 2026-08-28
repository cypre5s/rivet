"""编排 Level 0 至 Level 2 的渐进、可解释仓库检索。"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from rivet.contracts.common import Timestamp
from rivet.contracts.context import (
    ContextBudget,
    ContextItem,
    ContextLevel,
    ContextSelection,
)
from rivet.kernel.resources import ResourceScope
from rivet.tools.errors import FileToolError, SearchToolError
from rivet.tools.files import FileReader
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.process import ProcessRunner
from rivet.tools.search import SearchService, TextSearchMatch

from .budget import select_ranked_context
from .inventory import (
    FileRole,
    InventoryEntry,
    RepositoryInventoryBuilder,
    RepositorySnapshot,
)
from .scoring import CandidateEvidence, ContextScorer
from .signals import TaskSignals, extract_task_signals
from .syntax import extract_syntax_document, supports_syntax_path

MAX_LEXICAL_RESULTS = 2_000
MAX_SYNTAX_FILES = 256
MAX_SNIPPET_LINES = 80


@dataclass(frozen=True, slots=True)
class ProgressiveContextResult:
    """返回仓库快照、任务信号、探索记录和最终选择。"""

    snapshot: RepositorySnapshot
    signals: TaskSignals
    selection: ContextSelection
    explored_items: tuple[ContextItem, ...]
    syntax_activated: bool = False
    escalation_reason: str | None = None


class ProgressiveContext:
    """以单次 ripgrep 和按需 Tree-sitter 完成有限上下文选择。"""

    def __init__(
        self,
        repository_root: Path,
        *,
        scope: ResourceScope,
        clock: Callable[[], Timestamp] | None = None,
    ) -> None:
        self._boundary = WorkspaceBoundary(repository_root)
        self._scope = scope
        self._runner = ProcessRunner(
            self._boundary,
            scope=scope,
            max_capture_bytes=16 * 1024 * 1024,
            root_kind="repository_read_only",
        )
        self._inventory = RepositoryInventoryBuilder(
            self._boundary, runner=self._runner
        )
        self._search = SearchService(self._boundary, runner=self._runner)
        self._reader = FileReader(self._boundary, max_file_bytes=512 * 1024)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def retrieve(
        self,
        task: str,
        *,
        budget: ContextBudget,
        include_syntax: bool | None = False,
    ) -> ProgressiveContextResult:
        """建立清单后逐级检索，并用冻结预算确定最终条目。"""
        snapshot = await self._inventory.build()
        signals = extract_task_signals(task)
        candidates = self._inventory_candidates(snapshot, signals)
        lexical = await self._lexical_candidates(snapshot, signals)
        candidates.extend(lexical)
        syntax_activated = include_syntax is True or (
            include_syntax is None and self._lexical_requires_syntax(lexical)
        )
        if syntax_activated:
            candidates.extend(self._syntax_candidates(snapshot, signals, lexical))
        candidates = self._attach_corpus_and_pairs(candidates, snapshot)
        recent_paths = await self._recent_git_paths()
        ranked = ContextScorer().rank(
            tuple(candidates), signals, recent_paths=recent_paths
        )
        selected = select_ranked_context(ranked, budget, selected_at=self._clock())
        return ProgressiveContextResult(
            snapshot=snapshot,
            signals=signals,
            selection=selected.selection,
            explored_items=selected.explored_items,
            syntax_activated=syntax_activated,
            escalation_reason=("词法候选缺失或歧义过高" if syntax_activated else None),
        )

    @staticmethod
    def _lexical_requires_syntax(lexical: list[CandidateEvidence]) -> bool:
        """只在 L1 无证据或命中路径过多时升级 Tree-sitter。"""
        paths = {candidate.path for candidate in lexical}
        return not paths or len(paths) > 4

    def _inventory_candidates(
        self, snapshot: RepositorySnapshot, signals: TaskSignals
    ) -> list[CandidateEvidence]:
        """为命中文件名和关键项目结构建立 Level 0 候选。"""
        candidates: list[CandidateEvidence] = []
        query_text = " ".join(signals.search_terms).casefold()
        for entry in snapshot.entries:
            matched_terms = tuple(
                term
                for term in signals.search_terms
                if len(term) >= 3 and term.casefold() in entry.path.casefold()
            )
            role_relevant = self._role_is_relevant(entry, query_text)
            if not matched_terms and not role_relevant:
                continue
            content = (
                f"仓库文件：{entry.path}\n"
                f"角色：{entry.role.value}\n"
                f"大小：{entry.size_bytes} bytes"
            )
            candidates.append(
                CandidateEvidence(
                    path=entry.path,
                    content=content,
                    retrieval_level=ContextLevel.INVENTORY,
                    matched_terms=matched_terms,
                    term_frequency=len(matched_terms),
                    filename_match_count=len(matched_terms),
                )
            )
        return candidates

    @staticmethod
    def _role_is_relevant(entry: InventoryEntry, query_text: str) -> bool:
        """仅在任务含结构提示时加入清单、入口、测试或构建配置。"""
        role_terms = {
            FileRole.MANIFEST: ("dependency", "dependencies", "依赖", "manifest"),
            FileRole.ENTRYPOINT: ("bootstrap", "cli", "入口", "启动"),
            FileRole.TEST: ("test", "pytest", "测试", "regression"),
            FileRole.BUILD_CONFIG: ("build", "strict", "构建", "配置"),
        }
        return any(term in query_text for term in role_terms.get(entry.role, ()))

    async def _lexical_candidates(
        self, snapshot: RepositorySnapshot, signals: TaskSignals
    ) -> list[CandidateEvidence]:
        """用一次 ripgrep 正则搜索全部任务词并读取有限行窗。"""
        terms = tuple(
            term for term in signals.search_terms if len(term) >= 3 and "/" not in term
        )[:24]
        if not terms:
            return []
        pattern = "(?i:" + "|".join(re.escape(term) for term in terms) + ")"
        try:
            result = await self._search.text(
                pattern,
                regex=True,
                max_results=MAX_LEXICAL_RESULTS,
            )
        except SearchToolError:
            return []
        eligible = {entry.path for entry in snapshot.entries if entry.content_eligible}
        grouped: dict[str, list[TextSearchMatch]] = defaultdict(list)
        for match in result.matches:
            if match.path in eligible:
                grouped[match.path].append(match)
        candidates: list[CandidateEvidence] = []
        document_count = max(len(grouped), 1)
        term_documents = {
            term.casefold(): sum(
                any(term.casefold() in match.preview.casefold() for match in matches)
                for matches in grouped.values()
            )
            for term in terms
        }
        for path in sorted(grouped):
            matches = grouped[path]
            first_line = min(match.line_number for match in matches)
            last_line = min(
                max(match.line_number for match in matches),
                first_line + MAX_SNIPPET_LINES - 1,
            )
            start_line = max(1, first_line - 3)
            end_line = last_line + 3
            try:
                read = self._reader.read_range(
                    path, start_line=start_line, end_line=end_line
                )
            except FileToolError:
                continue
            lowered_content = read.content.casefold()
            matched_terms = tuple(
                term for term in terms if term.casefold() in lowered_content
            )
            if not matched_terms:
                continue
            frequency = sum(
                min(lowered_content.count(term.casefold()), 20)
                for term in matched_terms
            )
            document_frequency = max(
                term_documents[term.casefold()] for term in matched_terms
            )
            candidates.append(
                CandidateEvidence(
                    path=path,
                    content=read.content,
                    retrieval_level=ContextLevel.LEXICAL,
                    start_line=read.start_line,
                    end_line=read.end_line,
                    matched_terms=matched_terms,
                    term_frequency=frequency,
                    document_frequency=document_frequency,
                    document_count=document_count,
                    filename_match_count=sum(
                        term.casefold() in path.casefold() for term in terms
                    ),
                )
            )
        return candidates

    def _syntax_candidates(
        self,
        snapshot: RepositorySnapshot,
        signals: TaskSignals,
        lexical: list[CandidateEvidence],
    ) -> list[CandidateEvidence]:
        """只在显式升级后解析相关源码的函数、类、导入和测试名。"""
        lexical_paths = {candidate.path for candidate in lexical}
        supported = [
            entry
            for entry in snapshot.entries
            if entry.content_eligible and supports_syntax_path(entry.path)
        ]
        supported.sort(key=lambda entry: (entry.path not in lexical_paths, entry.path))
        candidates: list[CandidateEvidence] = []
        signal_keys = {term.casefold() for term in signals.search_terms}
        for entry in supported[:MAX_SYNTAX_FILES]:
            try:
                read = self._reader.read_text(entry.path)
                document = extract_syntax_document(entry.path, read.content)
            except (FileToolError, UnicodeDecodeError):
                continue
            for symbol in document.symbols:
                symbol_key = symbol.name.casefold()
                exact = symbol_key in signal_keys
                related = any(
                    len(term) >= 4 and (term in symbol_key or symbol_key in term)
                    for term in signal_keys
                )
                if not exact and not related and entry.path not in lexical_paths:
                    continue
                try:
                    snippet = self._reader.read_range(
                        entry.path,
                        start_line=max(1, symbol.start_line - 2),
                        end_line=min(symbol.end_line + 2, symbol.start_line + 79),
                    )
                except FileToolError:
                    continue
                candidates.append(
                    CandidateEvidence(
                        path=entry.path,
                        content=snippet.content,
                        retrieval_level=ContextLevel.SYNTAX,
                        start_line=snippet.start_line,
                        end_line=snippet.end_line,
                        symbol=symbol.name,
                        matched_terms=tuple(
                            term
                            for term in signals.search_terms
                            if term.casefold() in snippet.content.casefold()
                            or term.casefold() == symbol_key
                        ),
                        term_frequency=1,
                        filename_match_count=sum(
                            term.casefold() in entry.path.casefold()
                            for term in signals.search_terms
                        ),
                    )
                )
            for imported in document.imports:
                matched = tuple(
                    term
                    for term in signals.search_terms
                    if term.casefold() in imported.casefold()
                )
                if matched:
                    candidates.append(
                        CandidateEvidence(
                            path=entry.path,
                            content=imported,
                            retrieval_level=ContextLevel.SYNTAX,
                            matched_terms=matched,
                            term_frequency=len(matched),
                        )
                    )
        return candidates

    @staticmethod
    def _attach_corpus_and_pairs(
        candidates: list[CandidateEvidence], snapshot: RepositorySnapshot
    ) -> list[CandidateEvidence]:
        """补齐语料规模，并以路径词交集建立轻量测试邻域。"""
        document_paths = {candidate.path for candidate in candidates}
        document_count = max(len(document_paths), 1)
        source_paths = tuple(
            entry.path
            for entry in snapshot.entries
            if entry.role in {FileRole.SOURCE, FileRole.ENTRYPOINT}
        )
        test_paths = tuple(
            entry.path for entry in snapshot.entries if entry.role is FileRole.TEST
        )
        paired: dict[str, str] = {}
        for test_path in test_paths:
            best_path: str | None = None
            best_overlap = 0
            test_tokens = ProgressiveContext._path_tokens(test_path) - {"test", "tests"}
            for source_path in source_paths:
                overlap = len(
                    test_tokens.intersection(
                        ProgressiveContext._path_tokens(source_path)
                    )
                )
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_path = source_path
            if best_path is not None and best_overlap > 0:
                paired[test_path] = best_path
                paired.setdefault(best_path, test_path)
        return [
            replace(
                candidate,
                document_count=max(candidate.document_count, document_count),
                paired_with=paired.get(candidate.path),
            )
            for candidate in candidates
        ]

    @staticmethod
    def _path_tokens(path: str) -> set[str]:
        """把路径拆为测试配对使用的稳定小写词。"""
        return {
            token
            for token in re.split(
                r"[^a-z0-9]+", PurePosixPath(path).as_posix().casefold()
            )
            if len(token) >= 2 and token not in {"py", "js", "jsx", "ts", "tsx", "src"}
        }

    async def _recent_git_paths(self) -> tuple[str, ...]:
        """读取最近提交的文件顺序，非 Git 目录安全降级为空。"""
        result = await self._runner.run(
            (
                "git",
                "--no-pager",
                "log",
                "--format=@@%ct",
                "--name-only",
                "--no-renames",
                "-n",
                "200",
                "--",
            ),
            timeout_seconds=15.0,
        )
        if result.returncode != 0 or result.timed_out or result.stdout_truncated:
            return ()
        seen: set[str] = set()
        paths: list[str] = []
        for line in result.stdout.decode("utf-8", errors="replace").splitlines():
            if not line or line.startswith("@@"):
                continue
            normalized = PurePosixPath(line).as_posix()
            if normalized not in seen:
                seen.add(normalized)
                paths.append(normalized)
        return tuple(paths)
