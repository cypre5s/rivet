"""用真实异步并发、导入和子进程验证模块运行时。"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

import pytest

from rivet.contracts.modules import ActivationPolicy, ModuleManifest, ModuleState
from rivet.kernel.errors import ModuleActivationError, ModuleQuarantinedError
from rivet.kernel.module_api import ModuleInstance
from rivet.kernel.module_runtime import ActivationJournal, ModuleRuntime

FACTORY_MODULE = "tests.fixtures.kernel.fake_modules"


class RecordingInstance(ModuleInstance, Protocol):
    """补充集成测试需要观察的激活计数。"""

    activation_count: int


class ResourceInstance(ModuleInstance, Protocol):
    """补充集成测试需要观察的真实资源句柄。"""

    temporary_directory: Path | None
    process: asyncio.subprocess.Process | None


def _manifest(
    module_id: str,
    capability_id: str,
    factory_name: str,
    *,
    activation: ActivationPolicy = ActivationPolicy.ON_DEMAND,
    requires: tuple[str, ...] = (),
    idle_timeout_seconds: int | None = None,
    enabled: bool = True,
) -> ModuleManifest:
    return ModuleManifest(
        module_id=module_id,
        module_version="1.0.0",
        activation=activation,
        factory=f"{FACTORY_MODULE}:{factory_name}",
        enabled=enabled,
        provides=(capability_id,),
        requires=requires,
        idle_timeout_seconds=idle_timeout_seconds,
    )


def _observations() -> ModuleType:
    module = importlib.import_module(FACTORY_MODULE)
    module.reset_observations()
    return module


@pytest.mark.asyncio
async def test_on_demand_factory_is_not_imported_before_resolve(tmp_path: Path) -> None:
    sys.modules.pop(FACTORY_MODULE, None)
    runtime = ModuleRuntime(
        (_manifest("test.lazy", "test.lazy.resolve", "create_recording_module"),),
        journal=ActivationJournal(tmp_path / "journal.json"),
    )

    assert FACTORY_MODULE not in sys.modules
    await runtime.resolve("test.lazy.resolve")

    assert FACTORY_MODULE in sys.modules
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_disabled_factory_call_count_is_zero(tmp_path: Path) -> None:
    observations = _observations()
    runtime = ModuleRuntime(
        (
            _manifest(
                "test.disabled",
                "test.disabled.resolve",
                "create_optional_module",
                enabled=False,
            ),
        ),
        journal=ActivationJournal(tmp_path / "journal.json"),
    )

    await runtime.start()
    await runtime.shutdown()

    assert observations.factory_calls["optional"] == 0


@pytest.mark.asyncio
async def test_twenty_concurrent_resolves_activate_once(tmp_path: Path) -> None:
    observations = _observations()
    runtime = ModuleRuntime(
        (_manifest("test.shared", "test.shared.resolve", "create_recording_module"),),
        journal=ActivationJournal(tmp_path / "journal.json"),
    )

    instances = await asyncio.gather(
        *(runtime.resolve("test.shared.resolve") for _ in range(20))
    )

    assert all(instance is instances[0] for instance in instances)
    assert observations.factory_calls["recording"] == 1
    assert cast(RecordingInstance, instances[0]).activation_count == 1
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_activation_failure_enters_failed_state(tmp_path: Path) -> None:
    runtime = ModuleRuntime(
        (_manifest("test.failing", "test.failing.resolve", "create_failing_module"),),
        journal=ActivationJournal(tmp_path / "journal.json"),
    )

    with pytest.raises(ModuleActivationError, match="test.failing"):
        await runtime.resolve("test.failing.resolve")

    assert runtime.state("test.failing") is ModuleState.FAILED
    assert runtime.journal.pending_module_ids() == frozenset()
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_crash_marker_quarantines_before_factory_import(tmp_path: Path) -> None:
    sys.modules.pop(FACTORY_MODULE, None)
    journal = ActivationJournal(tmp_path / "journal.json")
    journal.mark_pending("test.crashed")
    runtime = ModuleRuntime(
        (_manifest("test.crashed", "test.crashed.resolve", "create_recording_module"),),
        journal=journal,
    )

    with pytest.raises(ModuleQuarantinedError, match="test.crashed"):
        await runtime.resolve("test.crashed.resolve")

    assert runtime.state("test.crashed") is ModuleState.QUARANTINED
    assert FACTORY_MODULE not in sys.modules
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_safe_mode_only_activates_required_module(tmp_path: Path) -> None:
    observations = _observations()
    runtime = ModuleRuntime(
        (
            _manifest(
                "test.required",
                "test.required.resolve",
                "create_required_module",
                activation=ActivationPolicy.REQUIRED,
            ),
            _manifest(
                "test.optional",
                "test.optional.resolve",
                "create_failing_module",
            ),
        ),
        journal=ActivationJournal(tmp_path / "journal.json"),
        safe_mode=True,
    )

    await runtime.start()

    assert observations.factory_calls["required"] == 1
    assert observations.factory_calls["failing"] == 0
    assert runtime.state("test.required") is ModuleState.ACTIVE
    assert runtime.state("test.optional") is ModuleState.INACTIVE
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_lease_blocks_sleep_and_resolve_rebuilds_after_sleep(
    tmp_path: Path,
) -> None:
    observations = _observations()
    runtime = ModuleRuntime(
        (_manifest("test.sleep", "test.sleep.resolve", "create_recording_module"),),
        journal=ActivationJournal(tmp_path / "journal.json"),
    )
    first_instance = await runtime.resolve("test.sleep.resolve")
    lease = await runtime.acquire("test.sleep.resolve")

    assert not await runtime.sleep_module("test.sleep")
    await lease.release()
    assert await runtime.sleep_module("test.sleep")
    assert runtime.state("test.sleep") is ModuleState.INACTIVE

    second_instance = await runtime.resolve("test.sleep.resolve")
    assert second_instance is not first_instance
    assert observations.factory_calls["recording"] == 2
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_zero_idle_timeout_sleeps_and_next_resolve_rebuilds(
    tmp_path: Path,
) -> None:
    observations = _observations()
    runtime = ModuleRuntime(
        (
            _manifest(
                "test.idle",
                "test.idle.resolve",
                "create_recording_module",
                idle_timeout_seconds=0,
            ),
        ),
        journal=ActivationJournal(tmp_path / "journal.json"),
    )

    first_instance = await runtime.resolve("test.idle.resolve")
    for _ in range(10):
        if runtime.state("test.idle") is ModuleState.INACTIVE:
            break
        await asyncio.sleep(0)

    assert runtime.state("test.idle") is ModuleState.INACTIVE
    second_instance = await runtime.resolve("test.idle.resolve")
    assert second_instance is not first_instance
    assert observations.factory_calls["recording"] == 2
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_dependencies_activate_first_and_shutdown_in_reverse_order(
    tmp_path: Path,
) -> None:
    observations = _observations()
    runtime = ModuleRuntime(
        (
            _manifest(
                "test.root",
                "test.root.resolve",
                "create_recording_module",
                requires=("test.dependency",),
            ),
            _manifest(
                "test.dependency",
                "test.dependency.resolve",
                "create_dependency_module",
            ),
        ),
        journal=ActivationJournal(tmp_path / "journal.json"),
    )

    await runtime.resolve("test.root.resolve")
    await runtime.shutdown()

    assert observations.lifecycle_events == [
        "activate:dependency",
        "activate:recording",
        "shutdown:recording",
        "shutdown:dependency",
    ]


@pytest.mark.asyncio
async def test_shutdown_releases_task_process_and_temp_directory(
    tmp_path: Path,
) -> None:
    runtime = ModuleRuntime(
        (
            _manifest(
                "test.resource", "test.resource.resolve", "create_resource_module"
            ),
        ),
        journal=ActivationJournal(tmp_path / "journal.json"),
    )
    instance = await runtime.resolve("test.resource.resolve")
    resource_instance = cast(ResourceInstance, instance)
    temporary_directory = resource_instance.temporary_directory
    process = resource_instance.process

    await runtime.shutdown()

    assert temporary_directory is not None
    assert not temporary_directory.exists()
    assert process is not None
    assert process.returncode is not None
    counts = runtime.resource_counts()
    assert counts.active_task_count == 0
    assert counts.active_process_count == 0
    assert counts.temporary_directory_count == 0
    assert counts.resource_count == 0
