"""验证预算选择、使用状态和受保护信息压缩。"""

from datetime import UTC, datetime

import pytest

from rivet.context.budget import (
    ContextBudgetError,
    ContextCompactor,
    ContextFragment,
    ContextFragmentKind,
    consume_selection,
    select_ranked_context,
)
from rivet.context.scoring import CandidateEvidence, RankedCandidate
from rivet.contracts.context import ContextBudget, ContextLevel, ContextUseState

NOW = datetime(2026, 8, 28, tzinfo=UTC)


def _ranked(path: str, content: str, score: float) -> RankedCandidate:
    """构造预算选择所需的稳定评分候选。"""
    return RankedCandidate(
        evidence=CandidateEvidence(
            path=path,
            content=content,
            retrieval_level=ContextLevel.LEXICAL,
        ),
        score=score,
        reasons=("词法内容命中",),
    )


def test_budget_deduplicates_content_and_records_eviction() -> None:
    budget = ContextBudget(
        total_tokens=40,
        required_tokens=0,
        working_tokens=40,
        history_tokens=0,
    )
    ranked = (
        _ranked("src/a.py", "same content", 10),
        _ranked("generated/a.py", "same content", 9),
        _ranked("src/b.py", "different content", 8),
    )

    result = select_ranked_context(ranked, budget, selected_at=NOW)

    assert [item.repository_path for item in result.selection.items] == [
        "src/a.py",
        "src/b.py",
    ]
    assert len(result.explored_items) == 3
    assert result.explored_items[1].use_state is ContextUseState.EXPLORED
    assert result.selection.items[0].use_state is ContextUseState.SELECTED
    assert len(result.selection.evicted_item_ids) == 1


def test_consumption_is_explicit_and_counted() -> None:
    budget = ContextBudget(
        total_tokens=20,
        required_tokens=0,
        working_tokens=20,
        history_tokens=0,
    )
    selected = select_ranked_context(
        (_ranked("src/a.py", "content", 1),), budget, selected_at=NOW
    ).selection

    consumed = consume_selection(selected)

    assert consumed.items[0].use_state is ContextUseState.CONSUMED
    assert consumed.items[0].consumed_count == 1


def test_compaction_preserves_acceptance_constraints_and_failures() -> None:
    fragments = (
        ContextFragment(ContextFragmentKind.HISTORY, "old history", 12),
        ContextFragment(ContextFragmentKind.ACCEPTANCE_CONSTRAINT, "不得修改主分支", 6),
        ContextFragment(ContextFragmentKind.FAILURE, "pytest failed: assertion", 8),
        ContextFragment(ContextFragmentKind.OBSERVATION, "extra observation", 10),
    )

    compacted = ContextCompactor().compact(fragments, max_tokens=18)

    assert {fragment.kind for fragment in compacted} == {
        ContextFragmentKind.ACCEPTANCE_CONSTRAINT,
        ContextFragmentKind.FAILURE,
    }


def test_compaction_fails_closed_when_protected_content_exceeds_budget() -> None:
    fragments = (
        ContextFragment(ContextFragmentKind.ACCEPTANCE_CONSTRAINT, "constraint", 8),
        ContextFragment(ContextFragmentKind.FAILURE, "failure", 8),
    )

    with pytest.raises(ContextBudgetError, match="受保护"):
        ContextCompactor().compact(fragments, max_tokens=10)
