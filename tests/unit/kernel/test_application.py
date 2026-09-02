"""验证 Demand Journal 是唯一能力激活入口。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.contracts.modules import ModuleManifest, ModuleState
from rivet.kernel.application import RivetKernel
from rivet.kernel.capability_demand import (
    CapabilityDemandSource,
    DemandContext,
    InMemoryDemandJournal,
)
from rivet.kernel.errors import (
    DemandCausalityError,
    DemandJournalError,
    ModuleActivationError,
)
from rivet.kernel.module_api import ModuleActivationContext
from rivet.kernel.module_events import InMemoryModuleLifecycleSink
from tests.fixtures.kernel import fake_modules


def _manifest() -> ModuleManifest:
    return ModuleManifest(
        module_id="test.application",
        factory="tests.fixtures.kernel.fake_modules:create_recording_module",
        provides=("test.application.resolve",),
    )


def _context() -> DemandContext:
    return DemandContext(run_id="run_kernel", session_id="session_kernel")


def _kernel(
    tmp_path: Path,
    *,
    journal: InMemoryDemandJournal | None = None,
    sink: InMemoryModuleLifecycleSink | None = None,
) -> tuple[RivetKernel, InMemoryDemandJournal, InMemoryModuleLifecycleSink]:
    fake_modules.reset_observations()
    selected_journal = journal or InMemoryDemandJournal()
    selected_sink = sink or InMemoryModuleLifecycleSink()
    return (
        RivetKernel.from_manifests(
            (_manifest(),),
            demand_journal=selected_journal,
            lifecycle_sink=selected_sink,
            activation_context=ModuleActivationContext(
                repository=tmp_path,
            ),
        ),
        selected_journal,
        selected_sink,
    )


@pytest.mark.asyncio
async def test_start_is_zero_activation_and_public_api_has_no_runtime_bypass(
    tmp_path: Path,
) -> None:
    kernel, journal, sink = _kernel(tmp_path)

    await kernel.start()

    assert fake_modules.factory_calls == {}
    assert journal.records == ()
    assert sink.activation_events == []
    assert kernel.state("test.application") is ModuleState.INACTIVE
    assert not hasattr(kernel, "runtime")
    assert not hasattr(kernel, "acquire")
    assert not hasattr(kernel, "resolve")
    assert not hasattr(kernel, "wake_module")
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_durable_parent_chain_precedes_activation_and_release(
    tmp_path: Path,
) -> None:
    kernel, journal, sink = _kernel(tmp_path)
    await kernel.start()
    root = await kernel.begin_user_demand(
        "ask",
        reason="用户提交任务",
        context=_context(),
    )

    lease = await kernel.acquire_required(
        "test.application.resolve",
        parent=root,
        reason="任务需要测试能力",
        operation_id="activate:test",
    )

    assert [record.demand.source for record in journal.records] == [
        CapabilityDemandSource.USER_EXPLICIT,
        CapabilityDemandSource.KERNEL_REQUIRED,
    ]
    child = journal.records[1]
    assert child.demand.parent_demand_id == root.demand_id
    assert child.sequence < 3
    assert sink.activation_events[0].demand_id == child.demand.demand_id
    assert sink.activation_events[0].demand_sequence == child.sequence
    assert kernel.snapshots()[0].activated_by_demand_id == child.demand.demand_id
    assert lease.demand_handle.demand_id == child.demand.demand_id

    await lease.release()
    assert kernel.state("test.application") is ModuleState.INACTIVE
    assert kernel.resource_counts().resource_count == 0
    assert sink.release_events[0].activated_by_demand_id == child.demand.demand_id
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_journal_failure_prevents_factory_import_and_activation(
    tmp_path: Path,
) -> None:
    journal = InMemoryDemandJournal(fail_after=1)
    kernel, _, sink = _kernel(tmp_path, journal=journal)
    await kernel.start()
    root = await kernel.begin_user_demand(
        "fix",
        reason="用户提交修复",
        context=_context(),
    )

    with pytest.raises(DemandJournalError):
        await kernel.acquire_required(
            "test.application.resolve",
            parent=root,
            reason="需要能力",
        )

    assert fake_modules.factory_calls == {}
    assert kernel.state("test.application") is ModuleState.INACTIVE
    assert sink.activation_events == []
    assert kernel.resource_counts().resource_count == 0
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_model_tool_and_kernel_demands_form_one_parent_chain(
    tmp_path: Path,
) -> None:
    kernel, journal, _ = _kernel(tmp_path)
    await kernel.start()
    root = await kernel.begin_user_demand(
        "fix",
        reason="用户提交修复",
        context=_context(),
    )
    tool = await kernel.begin_model_tool_demand(
        "file_write",
        parent=root,
        reason="模型请求写文件",
        operation_id="call_write",
    )
    lease = await kernel.acquire_required(
        "test.application.resolve",
        parent=tool,
        reason="写文件需要运行能力",
        operation_id="call_write",
    )

    assert [record.demand.parent_demand_id for record in journal.records] == [
        None,
        root.demand_id,
        tool.demand_id,
    ]
    assert [record.demand.source for record in journal.records] == [
        CapabilityDemandSource.USER_EXPLICIT,
        CapabilityDemandSource.MODEL_TOOL_CALL,
        CapabilityDemandSource.KERNEL_REQUIRED,
    ]
    await lease.release()
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_parent_handle_cannot_cross_kernel_boundary(tmp_path: Path) -> None:
    first, _, _ = _kernel(tmp_path / "first")
    second, second_journal, _ = _kernel(tmp_path / "second")
    await first.start()
    await second.start()
    parent = await first.begin_user_demand(
        "ask",
        reason="第一个 Kernel",
        context=_context(),
    )

    with pytest.raises(DemandCausalityError):
        await second.acquire_required(
            "test.application.resolve",
            parent=parent,
            reason="伪造跨 Kernel 父链",
        )

    assert second_journal.records == ()
    assert fake_modules.factory_calls == {}
    await first.shutdown()
    await second.shutdown()


@pytest.mark.asyncio
async def test_lifecycle_sink_failure_rolls_back_before_active(tmp_path: Path) -> None:
    sink = InMemoryModuleLifecycleSink(
        fail_activation_for=frozenset({"test.application"})
    )
    kernel, _, _ = _kernel(tmp_path, sink=sink)
    await kernel.start()
    root = await kernel.begin_user_demand(
        "ask",
        reason="用户提交任务",
        context=_context(),
    )

    with pytest.raises(ModuleActivationError):
        await kernel.acquire_required(
            "test.application.resolve",
            parent=root,
            reason="触发 Sink 失败",
        )

    assert kernel.state("test.application") is ModuleState.FAILED
    assert kernel.resource_counts().resource_count == 0
    assert fake_modules.lifecycle_events == [
        "activate:recording",
        "shutdown:recording",
    ]
    assert sink.activation_events == []
    assert sink.failure_events[0].module_id == "test.application"
    await kernel.shutdown()
