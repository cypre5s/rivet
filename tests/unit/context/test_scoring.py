"""验证可解释评分、稳定排序和重复惩罚。"""

from rivet.context.scoring import CandidateEvidence, ContextScorer
from rivet.context.signals import extract_task_signals
from rivet.contracts.context import ContextLevel


def _candidate(
    path: str,
    *,
    symbol: str | None = None,
    matched_terms: tuple[str, ...] = (),
    term_frequency: int = 0,
    paired_with: str | None = None,
) -> CandidateEvidence:
    """创建只覆盖当前断言所需字段的候选。"""
    return CandidateEvidence(
        path=path,
        content=f"content for {path}",
        retrieval_level=ContextLevel.LEXICAL,
        symbol=symbol,
        matched_terms=matched_terms,
        term_frequency=term_frequency,
        paired_with=paired_with,
    )


def test_exact_stack_hit_outranks_general_lexical_match() -> None:
    signals = extract_task_signals(
        'File "src/payment.py", line 8, in charge PaymentDeclined'
    )
    candidates = (
        _candidate(
            "docs/payment.md",
            matched_terms=("PaymentDeclined",),
            term_frequency=3,
        ),
        _candidate(
            "src/payment.py",
            matched_terms=("PaymentDeclined",),
            term_frequency=1,
        ),
    )

    ranked = ContextScorer().rank(candidates, signals, recent_paths=())

    assert ranked[0].evidence.path == "src/payment.py"
    assert "错误栈精确路径命中" in ranked[0].reasons


def test_score_reports_symbol_test_pair_and_git_recency() -> None:
    signals = extract_task_signals("test_open_session open_session regression")
    candidate = _candidate(
        "tests/test_session.py",
        symbol="test_open_session",
        matched_terms=("open_session",),
        paired_with="src/session.py",
    )

    ranked = ContextScorer().rank(
        (candidate,), signals, recent_paths=("tests/test_session.py",)
    )

    assert "符号精确命中" in ranked[0].reasons
    assert "测试与实现配对" in ranked[0].reasons
    assert "Git 最近修改" in ranked[0].reasons


def test_equal_scores_have_stable_path_order() -> None:
    signals = extract_task_signals("needle")
    candidates = (
        _candidate("src/z.py", matched_terms=("needle",)),
        _candidate("src/a.py", matched_terms=("needle",)),
    )

    first = ContextScorer().rank(candidates, signals, recent_paths=())
    second = ContextScorer().rank(tuple(reversed(candidates)), signals, recent_paths=())

    assert [item.evidence.path for item in first] == ["src/a.py", "src/z.py"]
    assert first == second
