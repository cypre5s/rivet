"""按证据升级 LSP，并把精确位置合并为有预算的 Context Item。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from rivet.contracts.common import Timestamp
from rivet.contracts.context import (
    ContextBudget,
    ContextItem,
    ContextLevel,
    ContextSelection,
)
from rivet.kernel.resources import ResourceScope
from rivet.tools.errors import FileToolError, PathBoundaryError
from rivet.tools.files import FileReader
from rivet.tools.paths import WorkspaceBoundary

from .budget import select_ranked_context
from .engine import ProgressiveContext, ProgressiveContextResult
from .lsp_client import LspClientError
from .lsp_manifest import (
    LspManifestError,
    LspManifestRegistry,
    LspServerManifest,
)
from .lsp_models import LspLocation, LspPosition, LspResultError
from .lsp_sidecar import LspSidecar, LspSidecarError
from .scoring import CandidateEvidence, RankedCandidate

EXPLICIT_SEMANTIC_TERMS = (
    "definition",
    "references",
    "callers",
    "cross-file",
    "uses",
    "引用",
    "定义",
    "调用者",
    "跨文件",
    "谁使用",
)


class SemanticOperation(StrEnum):
    """限定首版允许的精确语义查询。"""

    DEFINITION = "definition"
    REFERENCES = "references"


class SemanticRetrievalStatus(StrEnum):
    """明确区分无需升级、成功、无收益和语法降级。"""

    NOT_NEEDED = "not_needed"
    COMPLETED = "completed"
    NO_RESULTS = "no_results"
    NO_MARGINAL_GAIN = "no_marginal_gain"
    FALLBACK_SYNTAX = "fallback_syntax"


@dataclass(frozen=True, slots=True)
class SemanticRequest:
    """保存发起 LSP 查询所需的文件、位置和操作。"""

    path: str
    position: LspPosition
    operation: SemanticOperation


@dataclass(frozen=True, slots=True)
class SemanticRetrievalResult:
    """保存基础证据、最终选择和可审计升级事实。"""

    base: ProgressiveContextResult
    selection: ContextSelection
    status: SemanticRetrievalStatus
    lsp_requested: bool
    lsp_started: bool
    fallback_used: bool
    failure_code: str | None
    marginal_gain: float


class SemanticSidecar(Protocol):
    """描述语义编排器需要的最小可替换 sidecar 接口。"""

    @property
    def is_running(self) -> bool:
        """指示是否已有活动进程。"""
        ...

    async def definition(
        self, path: str, position: LspPosition
    ) -> tuple[LspLocation, ...]:
        """返回 Definition 位置。"""
        ...

    async def references(
        self, path: str, position: LspPosition
    ) -> tuple[LspLocation, ...]:
        """返回 References 位置。"""
        ...

    async def close(self) -> None:
        """关闭可能存在的 sidecar。"""
        ...


class SemanticEscalationPolicy:
    """仅在任务明确要求或 Level 2 证据不足时允许升级。"""

    def should_activate(self, task: str, base_selection: ContextSelection) -> bool:
        """使用稳定词表和最高检索级别作出确定性决策。"""
        lowered = task.casefold()
        explicitly_requested = any(
            term.casefold() in lowered for term in EXPLICIT_SEMANTIC_TERMS
        )
        syntax_sufficient = any(
            item.retrieval_level >= ContextLevel.SYNTAX for item in base_selection.items
        )
        return explicitly_requested or not syntax_sufficient


class SemanticContextRetriever:
    """编排渐进检索、按需 LSP、边际收益判断与语法 fallback。"""

    def __init__(
        self,
        repository_root: Path,
        *,
        scope: ResourceScope,
        registry: LspManifestRegistry,
        sidecar_factory: Callable[[LspServerManifest], SemanticSidecar] | None = None,
        clock: Callable[[], Timestamp] | None = None,
        minimum_marginal_gain: float = 0.15,
    ) -> None:
        if not 0.0 <= minimum_marginal_gain <= 1.0:
            raise ValueError("上下文最小边际收益必须位于 0 到 1")
        self._repository_root = repository_root.resolve(strict=True)
        self._scope = scope
        self._registry = registry
        self._clock = clock or (lambda: datetime.now(UTC))
        self._minimum_marginal_gain = minimum_marginal_gain
        self._progressive = ProgressiveContext(
            self._repository_root, scope=scope, clock=self._clock
        )
        self._boundary = WorkspaceBoundary(self._repository_root)
        self._reader = FileReader(self._boundary, max_file_bytes=512 * 1024)
        self._sidecars: dict[str, SemanticSidecar] = {}
        if sidecar_factory is None:
            self._sidecar_factory: Callable[[LspServerManifest], SemanticSidecar] = (
                self._create_sidecar
            )
        else:
            self._sidecar_factory = sidecar_factory
        self._policy = SemanticEscalationPolicy()

    def _create_sidecar(self, manifest: LspServerManifest) -> SemanticSidecar:
        """构造绑定当前仓库和资源域的默认 LSP sidecar。"""
        return LspSidecar(
            manifest,
            repository_root=self._repository_root,
            scope=self._scope,
        )

    async def retrieve(
        self,
        task: str,
        *,
        budget: ContextBudget,
        semantic_request: SemanticRequest | None,
    ) -> SemanticRetrievalResult:
        """先完成 Level 0-2，再按策略决定是否发起唯一 Level 3 查询。"""
        base = await self._progressive.retrieve(
            task,
            budget=budget,
            include_syntax=True,
        )
        if semantic_request is None or not self._policy.should_activate(
            task, base.selection
        ):
            return self._result(
                base,
                status=SemanticRetrievalStatus.NOT_NEEDED,
                lsp_requested=False,
            )
        sidecar: SemanticSidecar | None = None
        try:
            manifest = self._registry.for_path(semantic_request.path)
            sidecar = self._sidecars.get(manifest.server_id)
            if sidecar is None:
                sidecar = self._sidecar_factory(manifest)
                self._sidecars[manifest.server_id] = sidecar
            if semantic_request.operation is SemanticOperation.DEFINITION:
                locations = await sidecar.definition(
                    semantic_request.path, semantic_request.position
                )
            else:
                locations = await sidecar.references(
                    semantic_request.path, semantic_request.position
                )
        except (
            FileToolError,
            LspClientError,
            LspManifestError,
            LspResultError,
            LspSidecarError,
            PathBoundaryError,
        ):
            return self._result(
                base,
                status=SemanticRetrievalStatus.FALLBACK_SYNTAX,
                lsp_requested=True,
                lsp_started=sidecar is not None and sidecar.is_running,
                fallback_used=True,
                failure_code="context.lsp.unavailable",
            )
        if not locations:
            return self._result(
                base,
                status=SemanticRetrievalStatus.NO_RESULTS,
                lsp_requested=True,
                lsp_started=sidecar.is_running,
            )
        ranked, marginal_gain = self._semantic_candidates(
            locations,
            operation=semantic_request.operation,
            existing_items=base.selection.items,
        )
        if not ranked:
            return self._result(
                base,
                status=SemanticRetrievalStatus.NO_MARGINAL_GAIN,
                lsp_requested=True,
                lsp_started=sidecar.is_running,
                marginal_gain=marginal_gain,
            )
        semantic_selection = select_ranked_context(
            ranked,
            budget,
            selected_at=self._clock(),
        ).selection
        merged = self._merge_selections(semantic_selection, base.selection)
        return self._result(
            base,
            selection=merged,
            status=SemanticRetrievalStatus.COMPLETED,
            lsp_requested=True,
            lsp_started=sidecar.is_running,
            marginal_gain=marginal_gain,
        )

    async def close(self) -> None:
        """关闭所有实际创建过的语言 sidecar。"""
        for server_id in sorted(self._sidecars):
            await self._sidecars[server_id].close()
        self._sidecars.clear()

    def _semantic_candidates(
        self,
        locations: tuple[LspLocation, ...],
        *,
        operation: SemanticOperation,
        existing_items: tuple[ContextItem, ...],
    ) -> tuple[tuple[RankedCandidate, ...], float]:
        """读取精确位置邻域并淘汰与已有内容高度重复的候选。"""
        existing_contents = tuple(item.content for item in existing_items)
        candidates: list[RankedCandidate] = []
        observed_gains: list[float] = []
        reason = (
            "LSP Definition 精确定位"
            if operation is SemanticOperation.DEFINITION
            else "LSP References 精确定位"
        )
        for location in locations:
            start_line = max(1, location.range.start.line)
            end_line = location.range.end.line + 3
            read = self._reader.read_range(
                location.path,
                start_line=start_line,
                end_line=end_line,
            )
            gain = self._marginal_gain(read.content, existing_contents)
            observed_gains.append(gain)
            if gain < self._minimum_marginal_gain:
                continue
            candidates.append(
                RankedCandidate(
                    evidence=CandidateEvidence(
                        path=location.path,
                        content=read.content,
                        retrieval_level=ContextLevel.LSP,
                        start_line=read.start_line,
                        end_line=read.end_line,
                    ),
                    score=250.0,
                    reasons=(reason,),
                )
            )
        ranked = tuple(
            sorted(
                candidates,
                key=lambda item: (
                    item.evidence.path,
                    item.evidence.start_line,
                    item.evidence.end_line,
                ),
            )
        )
        return ranked, max(observed_gains, default=0.0)

    @staticmethod
    def _marginal_gain(content: str, existing_contents: tuple[str, ...]) -> float:
        """以标识符 Jaccard 相似度估计新增内容边际收益。"""
        if not existing_contents:
            return 1.0
        content_tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", content.casefold()))
        if not content_tokens:
            return 0.0
        maximum_similarity = 0.0
        for existing in existing_contents:
            existing_tokens = set(
                re.findall(r"[A-Za-z_][A-Za-z0-9_]*", existing.casefold())
            )
            union = content_tokens | existing_tokens
            if union:
                maximum_similarity = max(
                    maximum_similarity,
                    len(content_tokens & existing_tokens) / len(union),
                )
        return round(1.0 - maximum_similarity, 6)

    @staticmethod
    def _merge_selections(
        semantic: ContextSelection, base: ContextSelection
    ) -> ContextSelection:
        """语义条目优先，并按 working 分区对路径和正文再次去重。"""
        budget = base.budget
        available_tokens = (
            budget.working_tokens
            if budget.working_tokens > 0
            else budget.total_tokens - budget.required_tokens - budget.history_tokens
        )
        selected: list[ContextItem] = []
        evicted_ids = list(semantic.evicted_item_ids) + list(base.evicted_item_ids)
        paths: set[str] = set()
        hashes: set[str] = set()
        used_tokens = 0
        for item in (*semantic.items, *base.items):
            duplicate = item.repository_path in paths or item.content_sha256 in hashes
            fits = used_tokens + item.token_estimate <= available_tokens
            if duplicate or not fits:
                evicted_ids.append(item.context_item_id)
                continue
            selected.append(item)
            paths.add(item.repository_path)
            hashes.add(item.content_sha256)
            used_tokens += item.token_estimate
        return ContextSelection(
            items=tuple(selected),
            budget=budget,
            estimated_tokens=used_tokens,
            evicted_item_ids=tuple(dict.fromkeys(evicted_ids)),
        )

    @staticmethod
    def _result(
        base: ProgressiveContextResult,
        *,
        status: SemanticRetrievalStatus,
        selection: ContextSelection | None = None,
        lsp_requested: bool,
        lsp_started: bool = False,
        fallback_used: bool = False,
        failure_code: str | None = None,
        marginal_gain: float = 0.0,
    ) -> SemanticRetrievalResult:
        """统一构造不可变语义检索结果。"""
        return SemanticRetrievalResult(
            base=base,
            selection=selection or base.selection,
            status=status,
            lsp_requested=lsp_requested,
            lsp_started=lsp_started,
            fallback_used=fallback_used,
            failure_code=failure_code,
            marginal_gain=marginal_gain,
        )
