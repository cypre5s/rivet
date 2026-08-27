"""以显式特征对仓库上下文候选进行稳定轻量评分。"""

from __future__ import annotations

import math
from dataclasses import dataclass

from rivet.contracts.context import ContextLevel

from .signals import TaskSignals


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    """保存评分所需且不含隐式模型判断的候选证据。"""

    path: str
    content: str
    retrieval_level: ContextLevel
    start_line: int = 1
    end_line: int = 1
    symbol: str | None = None
    matched_terms: tuple[str, ...] = ()
    term_frequency: int = 0
    document_frequency: int = 1
    document_count: int = 1
    filename_match_count: int = 0
    paired_with: str | None = None


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """保存候选总分和可直接展示的中文原因。"""

    evidence: CandidateEvidence
    score: float
    reasons: tuple[str, ...]


class ContextScorer:
    """综合路径、符号、测试、Git 和 BM25-like 词法证据。"""

    def rank(
        self,
        candidates: tuple[CandidateEvidence, ...],
        signals: TaskSignals,
        *,
        recent_paths: tuple[str, ...],
    ) -> tuple[RankedCandidate, ...]:
        """返回总分降序、路径和定位升序的稳定结果。"""
        recent_rank = {path: index for index, path in enumerate(recent_paths)}
        stack_paths = {path.casefold() for path in signals.stack_paths}
        signal_symbols = {symbol.casefold() for symbol in signals.symbols}
        ranked: list[RankedCandidate] = []
        for candidate in candidates:
            score = 0.0
            reasons: list[str] = []
            if candidate.path.casefold() in stack_paths:
                score += 120.0
                reasons.append("错误栈精确路径命中")
            filename_matches = candidate.filename_match_count or sum(
                term.casefold() in candidate.path.casefold()
                for term in signals.search_terms
                if len(term) >= 3
            )
            if filename_matches:
                score += min(filename_matches, 4) * 18.0
                reasons.append("文件名或路径命中")
            if (
                candidate.symbol is not None
                and candidate.symbol.casefold() in signal_symbols
            ):
                score += 60.0
                reasons.append("符号精确命中")
            if candidate.paired_with is not None:
                score += 25.0
                reasons.append("测试与实现配对")
            if candidate.matched_terms:
                frequency = max(
                    candidate.term_frequency, len(candidate.matched_terms), 1
                )
                document_count = max(candidate.document_count, 1)
                document_frequency = min(
                    max(candidate.document_frequency, 1), document_count
                )
                inverse_document_frequency = math.log(
                    1
                    + (document_count - document_frequency + 0.5)
                    / (document_frequency + 0.5)
                )
                saturation = (frequency * 2.2) / (frequency + 1.2)
                score += 12.0 * inverse_document_frequency * saturation
                reasons.append("BM25-like 词法内容命中")
            if candidate.path in recent_rank:
                score += max(1.0, 10.0 - recent_rank[candidate.path] * 0.25)
                reasons.append("Git 最近修改")
            depth = candidate.path.count("/")
            if depth:
                score -= min(depth * 0.2, 3.0)
                reasons.append("路径距离惩罚")
            if not reasons:
                reasons.append("仓库结构候选")
            ranked.append(
                RankedCandidate(
                    evidence=candidate,
                    score=round(score, 6),
                    reasons=tuple(reasons),
                )
            )
        return tuple(
            sorted(
                ranked,
                key=lambda item: (
                    -item.score,
                    item.evidence.path,
                    item.evidence.start_line,
                    item.evidence.symbol or "",
                    item.evidence.content,
                ),
            )
        )
