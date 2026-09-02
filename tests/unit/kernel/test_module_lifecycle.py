"""验证已删除用户模块控制后保留的 Demand 生命周期契约。"""

from __future__ import annotations

import pytest

from rivet.kernel.capability_demand import (
    CapabilityDemand,
    CapabilityDemandSource,
    DemandContext,
    InMemoryDemandJournal,
)
from rivet.kernel.errors import DemandCausalityError


def _context(suffix: str = "one") -> DemandContext:
    return DemandContext(
        run_id=f"run_{suffix}",
        session_id=f"session_{suffix}",
    )


def test_kernel_required_has_no_public_constructor() -> None:
    assert not hasattr(CapabilityDemand, "create")
    assert not hasattr(CapabilityDemand, "kernel_required")


def test_non_user_demand_requires_parent_at_construction() -> None:
    with pytest.raises(DemandCausalityError, match="父 Demand"):
        CapabilityDemand.model_tool_call(
            "context_search",
            reason="模型请求上下文",
            context=_context(),
            operation_id="call_context",
            parent_demand_id="",
        )


@pytest.mark.asyncio
async def test_journal_rejects_unknown_and_cross_run_parent() -> None:
    journal = InMemoryDemandJournal()
    root = CapabilityDemand.user_explicit(
        "ask",
        reason="用户请求",
        context=_context("root"),
    )
    await journal.append(root)
    unknown_parent = CapabilityDemand.model_tool_call(
        "context_search",
        reason="未知父节点",
        context=_context("root"),
        operation_id="call_unknown",
        parent_demand_id="demand_missing",
    )
    cross_run = CapabilityDemand.model_tool_call(
        "context_search",
        reason="跨运行父节点",
        context=_context("other"),
        operation_id="call_cross",
        parent_demand_id=root.demand_id,
    )

    with pytest.raises(DemandCausalityError, match="尚未落盘"):
        await journal.append(unknown_parent)
    with pytest.raises(DemandCausalityError, match="同一运行上下文"):
        await journal.append(cross_run)


@pytest.mark.asyncio
async def test_journal_rejects_duplicate_demand_id() -> None:
    journal = InMemoryDemandJournal()
    demand = CapabilityDemand.user_explicit(
        "fix",
        reason="用户请求",
        context=_context(),
    )
    await journal.append(demand)

    with pytest.raises(DemandCausalityError, match="重复"):
        await journal.append(demand)


@pytest.mark.asyncio
async def test_journal_sequence_proves_parent_before_child() -> None:
    journal = InMemoryDemandJournal()
    root = CapabilityDemand.user_explicit(
        "fix",
        reason="用户请求",
        context=_context(),
    )
    root_record = await journal.append(root)
    child = CapabilityDemand.model_tool_call(
        "file_write",
        reason="模型工具调用",
        context=_context(),
        operation_id="call_write",
        parent_demand_id=root.demand_id,
    )
    child_record = await journal.append(child)

    assert root_record.sequence < child_record.sequence
    assert root_record.demand.source is CapabilityDemandSource.USER_EXPLICIT
    assert child_record.demand.source is CapabilityDemandSource.MODEL_TOOL_CALL
