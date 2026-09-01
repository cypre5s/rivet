"""覆盖 ModuleRuntime 的失败关闭、幂等和 Safe Mode 分支。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from rivet.contracts.modules import (
    ActivationPolicy,
    ModuleAvailability,
    ModuleManifest,
    ModuleState,
)
from rivet.kernel.errors import (
    ActivationJournalError,
    CapabilityNotFoundError,
    ModuleActivationError,
    ModuleDependencyError,
    ModuleShutdownError,
    ModuleUnavailableError,
    SafeModeViolationError,
)
from rivet.kernel.module_api import ModuleActivationContext
from rivet.kernel.module_runtime import ActivationJournal, ModuleRuntime
from rivet.kernel.resources import ResourceScope

FACTORY = "tests.fixtures.kernel.fake_modules:create_recording_module"


class ControlledModule:
    """按开关制造激活、休眠或关闭故障。"""

    def __init__(
        self,
        *,
        fail_sleep: bool = False,
        fail_shutdown: bool = False,
        block_activation: bool = False,
        capability_override: str | None = None,
    ) -> None:
        self.fail_sleep = fail_sleep
        self.fail_shutdown = fail_shutdown
        self.block_activation = block_activation
        self.capability_override = capability_override
        self.activation_started = asyncio.Event()

    async def activate(
        self,
        context: ModuleActivationContext,
        scope: ResourceScope,
    ) -> dict[str, object]:
        """记录开始，并可等待取消。"""
        del scope
        self.activation_started.set()
        if self.block_activation:
            await asyncio.Event().wait()
        capabilities = (
            (self.capability_override,)
            if self.capability_override is not None
            else context.declared_capabilities
        )
        return {capability_id: self for capability_id in capabilities}

    async def sleep(self) -> None:
        """按配置制造休眠故障。"""
        if self.fail_sleep:
            raise RuntimeError("fixture sleep failure")

    async def shutdown(self) -> None:
        """按配置制造关闭故障。"""
        if self.fail_shutdown:
            raise RuntimeError("fixture shutdown failure")


def _manifest(
    module_id: str = "test.hardening",
    capability_id: str = "test.hardening.resolve",
    *,
    activation: ActivationPolicy = ActivationPolicy.ON_DEMAND,
    requires: tuple[str, ...] = (),
    required_python_packages: tuple[str, ...] = (),
    install_extra: str | None = None,
) -> ModuleManifest:
    """构造最小有效 Manifest。"""
    return ModuleManifest(
        module_id=module_id,
        module_version="1.0.0",
        activation=activation,
        factory=FACTORY,
        provides=(capability_id,),
        requires=requires,
        required_python_packages=required_python_packages,
        install_extra=install_extra,
        idle_timeout_seconds=None,
    )


def _factory_loader(instance: object) -> Callable[[str], Callable[[], object]]:
    """构造带完整类型的固定 factory loader。"""

    def load(_factory_path: str) -> Callable[[], object]:
        """忽略路径并返回固定实例 factory。"""
        return lambda: instance

    return load


@pytest.mark.parametrize(
    "document",
    (
        "not-json",
        "[]",
        '{"schema_version":2,"pending_module_ids":[]}',
        '{"schema_version":1,"pending_module_ids":"bad"}',
        '{"schema_version":1,"pending_module_ids":[1]}',
        '{"schema_version":1,"pending_module_ids":["test.one","test.one"]}',
    ),
)
def test_activation_journal_rejects_each_corrupt_shape(
    tmp_path: Path,
    document: str,
) -> None:
    path = tmp_path / "journal.json"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ActivationJournalError):
        ActivationJournal(path).pending_module_ids()


def test_activation_journal_write_failure_is_classified(tmp_path: Path) -> None:
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ActivationJournalError, match="原子写入"):
        ActivationJournal(blocked_parent / "journal.json").mark_pending(
            "test.hardening"
        )


@pytest.mark.asyncio
async def test_safe_mode_start_sleep_and_shutdown_are_idempotent(
    tmp_path: Path,
) -> None:
    runtime = ModuleRuntime(
        (
            _manifest(
                "test.required",
                "test.required.resolve",
                activation=ActivationPolicy.REQUIRED,
            ),
            _manifest("test.optional", "test.optional.resolve"),
        ),
        journal=ActivationJournal(tmp_path / "journal.json"),
        safe_mode=True,
    )

    await runtime.start()
    await runtime.start()

    assert runtime.state("test.required") is ModuleState.ACTIVE
    assert runtime.state("test.optional") is ModuleState.INACTIVE
    assert not await runtime.sleep_module("test.required")
    assert not await runtime.sleep_module("test.optional")
    with pytest.raises(SafeModeViolationError):
        await runtime.resolve("test.optional.resolve")
    with pytest.raises(CapabilityNotFoundError):
        runtime.state("test.missing")
    await runtime.shutdown()
    await runtime.shutdown()


def test_safe_mode_rejects_required_dependency_on_optional(tmp_path: Path) -> None:
    with pytest.raises(ModuleDependencyError, match="依赖可选模块"):
        ModuleRuntime(
            (
                _manifest("test.optional", "test.optional.resolve"),
                _manifest(
                    "test.required",
                    "test.required.resolve",
                    activation=ActivationPolicy.REQUIRED,
                    requires=("test.optional",),
                ),
            ),
            journal=ActivationJournal(tmp_path / "journal.json"),
            safe_mode=True,
        )


@pytest.mark.asyncio
async def test_missing_optional_dependency_stays_inactive_and_never_loads_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺 extra 是静态 Availability 事实，不得污染 Runtime FAILED。"""
    runtime = ModuleRuntime(
        (
            _manifest(
                required_python_packages=("rivet_fixture_missing_package",),
                install_extra="syntax",
            ),
        ),
        journal=ActivationJournal(tmp_path / "journal.json"),
    )
    factory_loaded = False

    def fail_if_loaded(_factory_path: str) -> Callable[[], object]:
        nonlocal factory_loaded
        factory_loaded = True
        return lambda: ControlledModule()

    monkeypatch.setattr(ModuleRuntime, "_load_factory", staticmethod(fail_if_loaded))

    report = runtime.availability("test.hardening")
    assert report.state is ModuleAvailability.MISSING_DEPENDENCY
    assert report.missing_components == ("rivet_fixture_missing_package",)
    assert report.suggested_action == "运行 uv sync --extra syntax 安装该能力"
    with pytest.raises(ModuleUnavailableError) as captured:
        await runtime.acquire("test.hardening.resolve")

    assert captured.value.availability == "MISSING_DEPENDENCY"
    assert runtime.state("test.hardening") is ModuleState.INACTIVE
    assert not factory_loaded
    assert not runtime.journal.path.exists()
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_lease_context_release_is_idempotent_and_underflow_is_rejected(
    tmp_path: Path,
) -> None:
    runtime = ModuleRuntime(
        (_manifest(),),
        journal=ActivationJournal(tmp_path / "journal.json"),
    )
    lease = await runtime.acquire("test.hardening.resolve")

    async with lease as instance:
        assert instance is lease.capability
    await lease.release()
    with pytest.raises(ModuleActivationError, match="下溢"):
        await runtime.release_lease(("test.hardening",))
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_invalid_factory_result_enters_failed_and_cannot_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = ModuleRuntime(
        (_manifest(),),
        journal=ActivationJournal(tmp_path / "journal.json"),
    )
    monkeypatch.setattr(
        ModuleRuntime,
        "_load_factory",
        staticmethod(_factory_loader(object())),
    )

    with pytest.raises(ModuleActivationError, match="激活失败"):
        await runtime.resolve("test.hardening.resolve")
    with pytest.raises(ModuleActivationError, match="FAILED"):
        await runtime.resolve("test.hardening.resolve")

    assert runtime.journal.pending_module_ids() == frozenset()
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_sleep_and_shutdown_failures_are_classified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeping = ControlledModule(fail_sleep=True)
    sleep_runtime = ModuleRuntime(
        (_manifest("test.sleep", "test.sleep.resolve"),),
        journal=ActivationJournal(tmp_path / "sleep-journal.json"),
    )
    monkeypatch.setattr(
        ModuleRuntime,
        "_load_factory",
        staticmethod(_factory_loader(sleeping)),
    )
    await sleep_runtime.resolve("test.sleep.resolve")

    with pytest.raises(ModuleActivationError, match="休眠失败"):
        await sleep_runtime.sleep_module("test.sleep")
    assert sleep_runtime.state("test.sleep") is ModuleState.FAILED
    await sleep_runtime.shutdown()

    shutting_down = ControlledModule(fail_shutdown=True)
    shutdown_runtime = ModuleRuntime(
        (_manifest("test.shutdown", "test.shutdown.resolve"),),
        journal=ActivationJournal(tmp_path / "shutdown-journal.json"),
    )
    monkeypatch.setattr(
        ModuleRuntime,
        "_load_factory",
        staticmethod(_factory_loader(shutting_down)),
    )
    await shutdown_runtime.resolve("test.shutdown.resolve")

    with pytest.raises(ModuleShutdownError):
        await shutdown_runtime.shutdown()
    await shutdown_runtime.shutdown()


@pytest.mark.asyncio
async def test_capability_mapping_must_exactly_match_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ACTIVE 不得建立在缺失或伪造 capability 的模块实例上。"""
    mismatched = ControlledModule(capability_override="test.unexpected")
    runtime = ModuleRuntime(
        (_manifest(),),
        journal=ActivationJournal(tmp_path / "mapping-journal.json"),
    )
    monkeypatch.setattr(
        ModuleRuntime,
        "_load_factory",
        staticmethod(_factory_loader(mismatched)),
    )

    with pytest.raises(ModuleActivationError):
        await runtime.resolve("test.hardening.resolve")

    assert runtime.state("test.hardening") is ModuleState.FAILED
    assert not runtime.journal.path.exists()
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_cancelled_activation_clears_journal_and_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocking = ControlledModule(block_activation=True)
    runtime = ModuleRuntime(
        (_manifest(),),
        journal=ActivationJournal(tmp_path / "journal.json"),
    )
    monkeypatch.setattr(
        ModuleRuntime,
        "_load_factory",
        staticmethod(_factory_loader(blocking)),
    )
    activation = asyncio.create_task(runtime.resolve("test.hardening.resolve"))
    await blocking.activation_started.wait()

    activation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await activation

    assert runtime.state("test.hardening") is ModuleState.FAILED
    assert runtime.journal.pending_module_ids() == frozenset()
    assert runtime.resource_counts().resource_count == 0
    await runtime.shutdown()


def test_activation_journal_serializes_stable_sorted_set(tmp_path: Path) -> None:
    journal = ActivationJournal(tmp_path / "journal.json")
    journal.mark_pending("test.zed")
    journal.mark_pending("test.alpha")
    document = json.loads(journal.path.read_text(encoding="utf-8"))

    assert document["pending_module_ids"] == ["test.alpha", "test.zed"]
    journal.clear_pending("test.missing")
    journal.clear_pending("test.alpha")
    assert journal.pending_module_ids() == frozenset({"test.zed"})


def test_activation_journal_rejects_symlink_file(tmp_path: Path) -> None:
    external = tmp_path / "external.json"
    external.write_text(
        '{"schema_version":1,"pending_module_ids":[]}',
        encoding="utf-8",
    )
    link = tmp_path / "journal.json"
    link.symlink_to(external)
    journal = ActivationJournal(link)

    with pytest.raises(ActivationJournalError, match="符号链接"):
        journal.pending_module_ids()
    with pytest.raises(ActivationJournalError, match="符号链接"):
        journal.mark_pending("test.hardening")

    assert json.loads(external.read_text(encoding="utf-8"))["pending_module_ids"] == []
