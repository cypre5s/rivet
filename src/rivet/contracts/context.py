"""定义可解释、分级且受 token 预算约束的上下文契约。"""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Self

from pydantic import Field, model_validator

from rivet.contracts.common import (
    ContextItemId,
    ContractModel,
    NonEmptyText,
    RepositoryPath,
    Sha256Digest,
    SourceSpan,
    Timestamp,
)


class ContextLevel(IntEnum):
    """表示从便宜仓库清单到按需语义能力的检索级别。"""

    INVENTORY = 0
    LEXICAL = 1
    SYNTAX = 2
    LSP = 3
    OPTIONAL_SEMANTIC = 4


class ContextUseState(StrEnum):
    """区分只探索、已选择和已消费的上下文。"""

    EXPLORED = "explored"
    SELECTED = "selected"
    CONSUMED = "consumed"


class ContextItem(ContractModel):
    """保存内容来源、选择原因、检索成本和新鲜度。"""

    context_item_id: ContextItemId
    repository_path: RepositoryPath
    span: SourceSpan | None = None
    symbol: str | None = Field(default=None, max_length=512)
    content: str = Field(max_length=1_000_000)
    reason: NonEmptyText
    retrieval_level: ContextLevel
    content_sha256: Sha256Digest
    token_estimate: int = Field(ge=0)
    selected_at: Timestamp
    freshness: Sha256Digest | None = None
    use_state: ContextUseState = ContextUseState.EXPLORED
    consumed_count: int = Field(default=0, ge=0)


class ContextBudget(ContractModel):
    """将必需、工作和历史预算限制在总 token 预算内。"""

    total_tokens: int = Field(gt=0)
    required_tokens: int = Field(ge=0)
    working_tokens: int = Field(ge=0)
    history_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_allocation(self) -> Self:
        """拒绝各分区配额之和超出总预算。"""
        allocated = self.required_tokens + self.working_tokens + self.history_tokens
        if allocated > self.total_tokens:
            raise ValueError("上下文分区配额超出总预算")
        return self


class ContextSelection(ContractModel):
    """记录一次确定性选择的条目、预算和被淘汰数量。"""

    items: tuple[ContextItem, ...]
    budget: ContextBudget
    estimated_tokens: int = Field(ge=0)
    evicted_item_ids: tuple[ContextItemId, ...] = ()

    @model_validator(mode="after")
    def _validate_selection(self) -> Self:
        """拒绝重复条目或超出总预算的选择。"""
        item_ids = [item.context_item_id for item in self.items]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("上下文选择不得包含重复条目")
        if self.estimated_tokens > self.budget.total_tokens:
            raise ValueError("上下文选择超出总预算")
        return self
