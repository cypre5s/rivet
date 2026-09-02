"""验证 Runtime Permit、并发归因和资源回滚边界。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rivet.contracts.modules import ModuleManifest, ModuleState
from rivet.kernel.application import RivetKernel
from rivet.kernel.capability_demand import (
    DemandContext,
    DemandHandle,
    InMemoryDemandJournal,
)
from rivet.kernel.errors import ModuleActivationError, ModuleShutdownError
from rivet.kernel.module_api import ModuleActivationContext
from rivet.kernel.module_events import InMemoryModuleLifecycleSink
from rivet.kernel.module_runtime import ModuleRuntime
from tests.fixtures.kernel import fake_modules


def _manifest(
    module_id: str,
    capability_id: str,
    *,
    factory: str = "create_recording_module",
    requires: tuple[str, ...] = (),
) -> ModuleManifest:
    return ModuleManifest(
        module_id=module_id,
        factory=f"tests.fixtures.kernel.fake_modules:{factory}",
        provides=(capability_id,),
        requires=requires,
    )


def _kernel(
    tmp_path: Path,
    manifests: tuple[ModuleManifest, ...],
    *,
    sink: InMemoryModuleLifecycleSink | None = None,
) -> tuple[RivetKernel, InMemoryDemandJournal, InMemoryModuleLifecycleSink]:
    fake_modules.reset_observations()
    journal = InMemoryDemandJournal()
    lifecycle = sink or InMemoryModuleLifecycleSink()
    kernel = RivetKernel.from_manifests(
        manifests,
        demand_journal=journal,
        lifecycle_sink=lifecycle,
        activation_context=ModuleActivationContext(
            repository=tmp_path,
        ),
    )
    return kernel, journal, lifecycle


async def _root(kernel: RivetKernel, *, suffix: str = "root") -> DemandHandle:
    return await kernel.begin_user_demand(
        "test",
        reason="测试根需求",
        context=DemandContext(
            run_id=f"run_{suffix}",
            session_id=f"session_{suffix}",
        ),
    )


def test_runtime_exposes_no_string_activation_methods(tmp_path: Path) -> None:
    runtime = ModuleRuntime(
        (_manifest("test.runtime", "test.runtime.resolve"),),
        activation_context=ModuleActivationContext(
            repository=tmp_path,
        ),
        lifecycle_sink=InMemoryModuleLifecycleSink(),
        activation_seal=object(),
    )

    assert not hasattr(runtime, "acquire")
    assert not hasattr(runtime, "resolve")
    assert not hasattr(runtime, "wake_module")


@pytest.mark.asyncio
async def test_concurrent_demands_activate_once_and_keep_lock_owner_attribution(
    tmp_path: Path,
) -> None:
    kernel, journal, sink = _kernel(
        tmp_path,
        (_manifest("test.concurrent", "test.concurrent.resolve"),),
    )
    await kernel.start()
    first_root = await _root(kernel, suffix="first")
    second_root = await _root(kernel, suffix="second")

    first, second = await asyncio.gather(
        kernel.acquire_required(
            "test.concurrent.resolve",
            parent=first_root,
            reason="第一个并发请求",
        ),
        kernel.acquire_required(
            "test.concurrent.resolve",
            parent=second_root,
            reason="第二个并发请求",
        ),
    )

    child_ids = {
        record.demand.demand_id
        for record in journal.records
        if record.demand.capability_id == "test.concurrent.resolve"
    }
    assert len(sink.activation_events) == 1
    assert first.capability is second.capability
    assert sink.activation_events[0].demand_id in child_ids
    assert kernel.snapshots()[0].activated_by_demand_id == (
        sink.activation_events[0].demand_id
    )
    assert kernel.snapshots()[0].lease_count == 2

    await first.release()
    assert kernel.state("test.concurrent") is ModuleState.ACTIVE
    await second.release()
    assert kernel.state("test.concurrent") is ModuleState.INACTIVE
    assert kernel.resource_counts().resource_count == 0
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_dependency_activation_uses_same_durable_demand_inside_lock(
    tmp_path: Path,
) -> None:
    dependency = _manifest(
        "test.dependency",
        "test.dependency.resolve",
        factory="create_dependency_module",
    )
    target = _manifest(
        "test.target",
        "test.target.resolve",
        requires=("test.dependency",),
    )
    kernel, journal, sink = _kernel(tmp_path, (target, dependency))
    await kernel.start()
    root = await _root(kernel)

    lease = await kernel.acquire_required(
        "test.target.resolve",
        parent=root,
        reason="目标能力依赖底层能力",
    )

    child = journal.records[-1]
    assert [
        (event.module_id, event.dependency) for event in sink.activation_events
    ] == [
        ("test.dependency", True),
        ("test.target", False),
    ]
    assert {event.demand_id for event in sink.activation_events} == {
        child.demand.demand_id
    }
    assert all(
        event.demand_sequence == child.sequence for event in sink.activation_events
    )
    await lease.release()
    assert [event.module_id for event in sink.release_events] == [
        "test.target",
        "test.dependency",
    ]
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_factory_failure_keeps_demand_but_never_publishes_active(
    tmp_path: Path,
) -> None:
    kernel, journal, sink = _kernel(
        tmp_path,
        (
            _manifest(
                "test.failing",
                "test.failing.resolve",
                factory="create_failing_module",
            ),
        ),
    )
    await kernel.start()
    root = await _root(kernel)

    with pytest.raises(ModuleActivationError):
        await kernel.acquire_required(
            "test.failing.resolve",
            parent=root,
            reason="触发可控 factory 失败",
        )

    assert len(journal.records) == 2
    assert sink.activation_events == []
    assert sink.failure_events[0].demand_id == journal.records[-1].demand.demand_id
    assert kernel.state("test.failing") is ModuleState.FAILED
    assert kernel.resource_counts().resource_count == 0
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_sink_failure_closes_real_resources_before_returning_error(
    tmp_path: Path,
) -> None:
    sink = InMemoryModuleLifecycleSink(fail_activation_for=frozenset({"test.resource"}))
    kernel, _, _ = _kernel(
        tmp_path,
        (
            _manifest(
                "test.resource",
                "test.resource.resolve",
                factory="create_resource_module",
            ),
        ),
        sink=sink,
    )
    await kernel.start()
    root = await _root(kernel)

    with pytest.raises(ModuleActivationError):
        await kernel.acquire_required(
            "test.resource.resolve",
            parent=root,
            reason="Lifecycle sink 必须先落盘",
        )

    assert kernel.state("test.resource") is ModuleState.FAILED
    assert kernel.resource_counts().resource_count == 0
    assert sink.activation_events == []
    assert sink.failure_events[0].error_type == "RuntimeError"
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_lease_release_failure_still_releases_dependency_closure(
    tmp_path: Path,
) -> None:
    dependency = _manifest(
        "test.release_dependency",
        "test.release_dependency.resolve",
        factory="create_dependency_module",
    )
    target = _manifest(
        "test.release_target",
        "test.release_target.resolve",
        factory="create_fail_once_shutdown_module",
        requires=("test.release_dependency",),
    )
    kernel, _, _ = _kernel(tmp_path, (target, dependency))
    await kernel.start()
    root = await _root(kernel)
    lease = await kernel.acquire_required(
        "test.release_target.resolve",
        parent=root,
        reason="验证失败后继续释放依赖闭包",
    )

    with pytest.raises(ModuleShutdownError, match="test.release_target"):
        await lease.release()

    snapshots = {snapshot.module_id: snapshot for snapshot in kernel.snapshots()}
    assert snapshots["test.release_target"].state is ModuleState.FAILED
    assert snapshots["test.release_target"].lease_count == 0
    assert snapshots["test.release_dependency"].state is ModuleState.INACTIVE
    assert snapshots["test.release_dependency"].lease_count == 0
    assert fake_modules.lifecycle_events[-2:] == [
        "shutdown:fail_once_shutdown",
        "shutdown:dependency",
    ]

    await kernel.shutdown()
    assert kernel.resource_counts().resource_count == 0
