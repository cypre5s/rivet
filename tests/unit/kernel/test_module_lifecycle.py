"""验证模块生命周期唯一写服务的策略、资源与失败关闭语义。"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest
from pydantic import JsonValue

from rivet.contracts.modules import (
    ActivationPolicy,
    ModuleManifest,
    ModuleOperationSource,
    ModuleState,
    SleepPolicy,
)
from rivet.kernel.module_lifecycle import (
    InMemoryModuleOverrideRepository,
    ModuleLifecycleError,
    ModuleLifecycleService,
)
from rivet.kernel.module_runtime import ActivationJournal, ModuleRuntime

FACTORY_MODULE = "tests.fixtures.kernel.fake_modules"


def _manifest(
    module_id: str,
    capability_id: str,
    factory_name: str = "create_recording_module",
    *,
    enabled: bool = True,
    activation: ActivationPolicy = ActivationPolicy.ON_DEMAND,
    requires: tuple[str, ...] = (),
    manual_control: bool = True,
    sleep_policy: SleepPolicy = SleepPolicy.AUTOMATIC,
) -> ModuleManifest:
    """构造带正式控制字段的最小测试 Manifest。"""
    return ModuleManifest(
        module_id=module_id,
        module_version="1.0.0",
        activation=activation,
        factory=f"{FACTORY_MODULE}:{factory_name}",
        enabled=enabled,
        manual_control=manual_control,
        sleep_policy=sleep_policy,
        provides=(capability_id,),
        requires=requires,
        idle_timeout_seconds=None,
    )


def _service(
    tmp_path: Path,
    manifests: tuple[ModuleManifest, ...],
    *,
    safe_mode: bool = False,
) -> tuple[
    ModuleRuntime,
    ModuleLifecycleService,
    InMemoryModuleOverrideRepository,
]:
    """构造共享同一 Runtime 的生命周期服务。"""
    runtime = ModuleRuntime(
        manifests,
        journal=ActivationJournal(tmp_path / "activation.json"),
        safe_mode=safe_mode,
    )
    store = InMemoryModuleOverrideRepository()
    return runtime, ModuleLifecycleService(runtime, store), store


class RecordingSink:
    """记录生命周期 Trace 事件字段并返回稳定事件 ID。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, JsonValue]]] = []

    async def emit(self, event_type: str, payload: dict[str, JsonValue]) -> str:
        """保存事件副本。"""
        self.events.append((event_type, payload))
        return f"event_lifecycle_{len(self.events)}"


class FailingStore:
    """模拟 SQLite 原子提交失败。"""

    def set_many(self, changes: object) -> None:
        """始终拒绝持久化。"""
        del changes
        from rivet.storage.module_overrides import ModuleOverrideStoreError

        raise ModuleOverrideStoreError("fixture")


@pytest.mark.asyncio
async def test_enable_changes_policy_without_importing_or_instantiating(
    tmp_path: Path,
) -> None:
    sys.modules.pop(FACTORY_MODULE, None)
    runtime, service, _ = _service(
        tmp_path,
        (_manifest("test.disabled", "test.disabled.use", enabled=False),),
    )

    result = await service.enable(
        "test.disabled",
        source=ModuleOperationSource.CLI,
        request_id="request_enable",
    )

    assert result.changed
    assert result.current_state is ModuleState.INACTIVE
    assert runtime.configured_enabled("test.disabled")
    assert FACTORY_MODULE not in sys.modules
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_wake_sleep_disable_and_idempotency_are_distinct(
    tmp_path: Path,
) -> None:
    observations = importlib.import_module(FACTORY_MODULE)
    observations.reset_observations()
    runtime, service, _ = _service(
        tmp_path,
        (_manifest("test.module", "test.module.use"),),
    )

    first_wake = await service.wake(
        "test.module",
        source=ModuleOperationSource.CLI,
        request_id="request_wake_one",
    )
    second_wake = await service.wake(
        "test.module",
        source=ModuleOperationSource.CLI,
        request_id="request_wake_two",
    )
    slept = await service.sleep(
        "test.module",
        source=ModuleOperationSource.CLI,
        request_id="request_sleep_one",
    )
    slept_again = await service.sleep(
        "test.module",
        source=ModuleOperationSource.CLI,
        request_id="request_sleep_two",
    )
    disabled = await service.disable(
        "test.module",
        source=ModuleOperationSource.CLI,
        request_id="request_disable_one",
    )
    disabled_again = await service.disable(
        "test.module",
        source=ModuleOperationSource.CLI,
        request_id="request_disable_two",
    )

    assert first_wake.changed
    assert not second_wake.changed
    assert slept.changed
    assert slept.current_state is ModuleState.INACTIVE
    assert not slept_again.changed
    assert disabled.changed
    assert not disabled.effective_enabled
    assert not disabled_again.changed
    assert observations.factory_calls["recording"] == 1
    assert runtime.resource_counts().resource_count == 0
    await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("activation", "operation"),
    [
        (ActivationPolicy.REQUIRED, "disable"),
        (ActivationPolicy.REQUIRED, "sleep"),
        (ActivationPolicy.EAGER, "disable"),
        (ActivationPolicy.EAGER, "sleep"),
    ],
)
async def test_required_and_eager_modules_are_protected(
    tmp_path: Path,
    activation: ActivationPolicy,
    operation: str,
) -> None:
    runtime, service, _ = _service(
        tmp_path,
        (_manifest("test.system", "test.system.use", activation=activation),),
    )
    await runtime.start()

    with pytest.raises(ModuleLifecycleError) as captured:
        if operation == "disable":
            await service.disable(
                "test.system",
                source=ModuleOperationSource.CLI,
                request_id="request_disable",
            )
        else:
            await service.sleep(
                "test.system",
                source=ModuleOperationSource.CLI,
                request_id="request_sleep",
            )

    assert captured.value.code == "module.required"
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_manual_control_safe_mode_and_quarantine_cannot_be_bypassed(
    tmp_path: Path,
) -> None:
    runtime, service, _ = _service(
        tmp_path,
        (
            _manifest(
                "test.internal",
                "test.internal.use",
                manual_control=False,
            ),
            _manifest("test.optional", "test.optional.use"),
        ),
        safe_mode=True,
    )

    with pytest.raises(ModuleLifecycleError) as manual_error:
        await service.enable(
            "test.internal",
            source=ModuleOperationSource.CLI,
            request_id="request_internal",
        )
    with pytest.raises(ModuleLifecycleError) as safe_mode_error:
        await service.wake(
            "test.optional",
            source=ModuleOperationSource.CLI,
            request_id="request_safe_mode",
        )

    assert manual_error.value.code == "module.manual_control_denied"
    assert safe_mode_error.value.code == "module.safe_mode_restricted"
    await runtime.shutdown()

    journal = ActivationJournal(tmp_path / "quarantined.json")
    journal.mark_pending("test.crashed")
    quarantined_runtime = ModuleRuntime(
        (_manifest("test.crashed", "test.crashed.use"),),
        journal=journal,
    )
    quarantined_service = ModuleLifecycleService(
        quarantined_runtime, InMemoryModuleOverrideRepository()
    )
    with pytest.raises(ModuleLifecycleError) as quarantined_error:
        await quarantined_service.wake(
            "test.crashed",
            source=ModuleOperationSource.CLI,
            request_id="request_quarantined",
        )
    assert quarantined_error.value.code == "module.quarantined"
    await quarantined_runtime.shutdown()


@pytest.mark.asyncio
async def test_dependency_enable_and_cascade_follow_topology(tmp_path: Path) -> None:
    observations = importlib.import_module(FACTORY_MODULE)
    observations.reset_observations()
    runtime, service, _ = _service(
        tmp_path,
        (
            _manifest(
                "test.dependency",
                "test.dependency.use",
                "create_dependency_module",
                enabled=False,
            ),
            _manifest(
                "test.root",
                "test.root.use",
                requires=("test.dependency",),
            ),
        ),
    )

    with pytest.raises(ModuleLifecycleError) as disabled_error:
        await service.wake(
            "test.root",
            source=ModuleOperationSource.CLI,
            request_id="request_blocked",
        )
    assert disabled_error.value.code == "module.dependency_disabled"

    woke = await service.wake(
        "test.root",
        with_dependencies=True,
        source=ModuleOperationSource.CLI,
        request_id="request_with_dependencies",
    )
    assert woke.affected_modules == ("test.dependency", "test.root")
    assert observations.lifecycle_events[:2] == [
        "activate:dependency",
        "activate:recording",
    ]

    with pytest.raises(ModuleLifecycleError) as dependent_error:
        await service.sleep(
            "test.dependency",
            source=ModuleOperationSource.CLI,
            request_id="request_sleep_blocked",
        )
    assert dependent_error.value.code == "module.active_dependents"

    slept = await service.sleep(
        "test.dependency",
        cascade=True,
        confirmed=True,
        source=ModuleOperationSource.CLI,
        request_id="request_sleep_cascade",
    )
    assert slept.affected_modules == ("test.root", "test.dependency")
    assert observations.lifecycle_events[-2:] == [
        "sleep:recording",
        "sleep:dependency",
    ]
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_lease_blocks_and_wait_succeeds_after_release(tmp_path: Path) -> None:
    runtime, service, _ = _service(
        tmp_path,
        (_manifest("test.lease", "test.lease.use"),),
    )
    lease = await runtime.acquire("test.lease.use")

    with pytest.raises(ModuleLifecycleError) as blocked:
        await service.sleep(
            "test.lease",
            source=ModuleOperationSource.CLI,
            request_id="request_lease_blocked",
        )
    assert blocked.value.code == "module.lease_blocked"

    pending_sleep = asyncio.create_task(
        service.sleep(
            "test.lease",
            wait=True,
            timeout_seconds=1,
            source=ModuleOperationSource.CLI,
            request_id="request_lease_wait",
        )
    )
    await asyncio.sleep(0)
    await lease.release()
    result = await pending_sleep

    assert result.changed
    assert runtime.state("test.lease") is ModuleState.INACTIVE
    assert runtime.resource_counts().resource_count == 0
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_persistence_and_cleanup_failures_do_not_report_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ModuleRuntime(
        (_manifest("test.persistence", "test.persistence.use", enabled=False),),
        journal=ActivationJournal(tmp_path / "persistence.json"),
    )
    service = ModuleLifecycleService(runtime, FailingStore())

    with pytest.raises(ModuleLifecycleError) as persistence_error:
        await service.enable(
            "test.persistence",
            source=ModuleOperationSource.CLI,
            request_id="request_persistence",
        )
    assert persistence_error.value.code == "module.persistence_failed"
    assert not runtime.configured_enabled("test.persistence")
    await runtime.shutdown()

    cleanup_runtime, cleanup_service, _ = _service(
        tmp_path,
        (_manifest("test.cleanup", "test.cleanup.use"),),
    )
    await cleanup_service.wake(
        "test.cleanup",
        source=ModuleOperationSource.CLI,
        request_id="request_cleanup_wake",
    )

    async def fail_sleep(_module_id: str) -> bool:
        """模拟 Runtime 资源清理失败。"""
        raise ModuleActivationError("fixture")

    from rivet.kernel.errors import ModuleActivationError

    monkeypatch.setattr(cleanup_runtime, "sleep_module", fail_sleep)
    with pytest.raises(ModuleLifecycleError) as cleanup_error:
        await cleanup_service.sleep(
            "test.cleanup",
            source=ModuleOperationSource.CLI,
            request_id="request_cleanup",
        )
    assert cleanup_error.value.code == "module.resource_cleanup_failed"
    await cleanup_runtime.shutdown()


@pytest.mark.asyncio
async def test_concurrent_wake_sleep_stays_legal_and_trace_is_complete(
    tmp_path: Path,
) -> None:
    runtime, service, _ = _service(
        tmp_path,
        (_manifest("test.concurrent", "test.concurrent.use"),),
    )
    sink = RecordingSink()

    results = await asyncio.gather(
        service.wake(
            "test.concurrent",
            source=ModuleOperationSource.TUI,
            request_id="request_concurrent_wake",
            event_sink=sink,
        ),
        service.sleep(
            "test.concurrent",
            source=ModuleOperationSource.TUI,
            request_id="request_concurrent_sleep",
            event_sink=sink,
        ),
    )

    assert runtime.state("test.concurrent") in {
        ModuleState.ACTIVE,
        ModuleState.INACTIVE,
    }
    assert all(result.trace_event_id is not None for result in results)
    event_types = [event_type for event_type, _ in sink.events]
    assert event_types.count("module.operation.requested") == 2
    assert event_types.count("module.operation.completed") == 2
    for _, payload in sink.events:
        assert {
            "request_id",
            "module_id",
            "operation",
            "source",
            "previous_state",
            "current_state",
            "previous_enabled",
            "effective_enabled",
            "affected_modules",
            "blockers",
            "duration_ms",
            "error_code",
        } <= payload.keys()
    await runtime.shutdown()
