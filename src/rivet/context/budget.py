"""按 token 预算选择、去重并压缩可审计上下文。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from math import ceil

from rivet.contracts.common import SourceSpan, Timestamp
from rivet.contracts.context import (
    ContextBudget,
    ContextItem,
    ContextSelection,
    ContextUseState,
)

from .errors import ContextBudgetError
from .scoring import RankedCandidate


@dataclass(frozen=True, slots=True)
class ContextSelectionResult:
    """同时返回最终选择和所有候选的使用状态。"""

    selection: ContextSelection
    explored_items: tuple[ContextItem, ...]


class ContextFragmentKind(StrEnum):
    """标识压缩时各片段的保留优先级。"""

    ACCEPTANCE_CONSTRAINT = "acceptance_constraint"
    FAILURE = "failure"
    TASK = "task"
    OBSERVATION = "observation"
    HISTORY = "history"


@dataclass(frozen=True, slots=True)
class ContextFragment:
    """保存已知 token 成本和语义类别的上下文片段。"""

    kind: ContextFragmentKind
    content: str
    token_estimate: int

    def __post_init__(self) -> None:
        if not self.content or self.token_estimate < 0:
            raise ValueError("上下文片段内容和 token 估算必须有效")


def estimate_tokens(content: str) -> int:
    """使用 UTF-8 字节数给出保守且确定的轻量估算。"""
    if not content:
        return 0
    return max(1, ceil(len(content.encode("utf-8")) / 4))


def _context_item(
    ranked: RankedCandidate,
    *,
    selected_at: Timestamp,
    use_state: ContextUseState,
) -> ContextItem:
    """把评分证据转换为稳定 ID 的公共 Context 契约。"""
    evidence = ranked.evidence
    content_digest = hashlib.sha256(evidence.content.encode("utf-8")).hexdigest()
    identity = hashlib.sha256(
        (
            f"{evidence.path}\0{evidence.start_line}\0{evidence.end_line}\0"
            f"{evidence.symbol or ''}\0{content_digest}"
        ).encode()
    ).hexdigest()
    return ContextItem(
        context_item_id=f"context_{identity[:24]}",
        repository_path=evidence.path,
        span=SourceSpan(
            repository_path=evidence.path,
            start_line=evidence.start_line,
            end_line=max(evidence.start_line, evidence.end_line),
        ),
        symbol=evidence.symbol,
        content=evidence.content,
        reason="；".join(ranked.reasons),
        retrieval_level=evidence.retrieval_level,
        content_sha256=f"sha256:{content_digest}",
        token_estimate=estimate_tokens(evidence.content),
        selected_at=selected_at,
        use_state=use_state,
    )


def select_ranked_context(
    ranked_candidates: tuple[RankedCandidate, ...],
    budget: ContextBudget,
    *,
    selected_at: Timestamp,
) -> ContextSelectionResult:
    """按稳定评分选择候选，并对路径与正文重复执行失败关闭淘汰。"""
    available_tokens = (
        budget.working_tokens
        if budget.working_tokens > 0
        else budget.total_tokens - budget.required_tokens - budget.history_tokens
    )
    selected: list[ContextItem] = []
    explored: list[ContextItem] = []
    evicted_ids: list[str] = []
    selected_paths: set[str] = set()
    selected_hashes: set[str] = set()
    used_tokens = 0
    for ranked in ranked_candidates:
        explored_item = _context_item(
            ranked, selected_at=selected_at, use_state=ContextUseState.EXPLORED
        )
        is_duplicate = (
            explored_item.repository_path in selected_paths
            or explored_item.content_sha256 in selected_hashes
        )
        fits = used_tokens + explored_item.token_estimate <= available_tokens
        if is_duplicate or not fits:
            explored.append(explored_item)
            evicted_ids.append(explored_item.context_item_id)
            continue
        selected_item = explored_item.model_copy(
            update={"use_state": ContextUseState.SELECTED}
        )
        selected.append(selected_item)
        explored.append(selected_item)
        selected_paths.add(selected_item.repository_path)
        selected_hashes.add(selected_item.content_sha256)
        used_tokens += selected_item.token_estimate
    return ContextSelectionResult(
        selection=ContextSelection(
            items=tuple(selected),
            budget=budget,
            estimated_tokens=used_tokens,
            evicted_item_ids=tuple(evicted_ids),
        ),
        explored_items=tuple(explored),
    )


def consume_selection(selection: ContextSelection) -> ContextSelection:
    """显式记录模型已消费所选上下文及消费次数。"""
    consumed = tuple(
        item.model_copy(
            update={
                "use_state": ContextUseState.CONSUMED,
                "consumed_count": item.consumed_count + 1,
            }
        )
        for item in selection.items
    )
    return selection.model_copy(update={"items": consumed})


class ContextCompactor:
    """优先保留验收约束与失败，预算不足时拒绝伪压缩。"""

    _priority = {
        ContextFragmentKind.ACCEPTANCE_CONSTRAINT: 0,
        ContextFragmentKind.FAILURE: 1,
        ContextFragmentKind.TASK: 2,
        ContextFragmentKind.OBSERVATION: 3,
        ContextFragmentKind.HISTORY: 4,
    }

    def compact(
        self,
        fragments: tuple[ContextFragment, ...],
        *,
        max_tokens: int,
    ) -> tuple[ContextFragment, ...]:
        """在不修改片段正文的前提下按类别和原顺序选择。"""
        if max_tokens <= 0:
            raise ContextBudgetError("上下文压缩预算必须大于零")
        protected_kinds = {
            ContextFragmentKind.ACCEPTANCE_CONSTRAINT,
            ContextFragmentKind.FAILURE,
        }
        protected_tokens = sum(
            fragment.token_estimate
            for fragment in fragments
            if fragment.kind in protected_kinds
        )
        if protected_tokens > max_tokens:
            raise ContextBudgetError("受保护的验收约束和失败信息超出预算")
        indexed = tuple(enumerate(fragments))
        ordered = sorted(
            indexed,
            key=lambda item: (self._priority[item[1].kind], item[0]),
        )
        selected_indexes: set[int] = set()
        used_tokens = 0
        for index, fragment in ordered:
            if used_tokens + fragment.token_estimate <= max_tokens:
                selected_indexes.add(index)
                used_tokens += fragment.token_estimate
        return tuple(
            fragment for index, fragment in indexed if index in selected_indexes
        )
