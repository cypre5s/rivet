"""对最终内核、Provider、进程和 NDJSON Trace 执行离线故障注入。"""

from __future__ import annotations

import errno
import sys
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import httpx

from rivet.contracts.events import TraceEventEnvelope
from rivet.contracts.messages import UserMessage
from rivet.contracts.modules import ModuleManifest
from rivet.contracts.provider import ModelRequest
from rivet.kernel.application import RivetKernel
from rivet.kernel.capability_demand import DemandContext, InMemoryDemandJournal
from rivet.kernel.errors import DemandJournalError
from rivet.kernel.module_api import ModuleActivationContext
from rivet.kernel.module_events import InMemoryModuleLifecycleSink
from rivet.kernel.resources import ResourceScope
from rivet.providers.deepseek import DeepSeekProvider
from rivet.providers.errors import ProviderUnavailableError
from rivet.providers.models import DeepSeekConfig
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.process import ProcessRunner
from rivet.trace.errors import TraceWriteError
from rivet.trace.paths import RuntimePaths
from rivet.trace.store import TraceStore

FIXED_NOW = datetime(2026, 8, 28, tzinfo=UTC)
_activation_count = 0


@dataclass(frozen=True, slots=True)
class FaultResult:
    fault_id: str
    expected_code: str
    actual_code: str
    passed: bool
    duration_ms: float
    resource_count_after: int


class _FaultModule:
    async def activate(
        self,
        context: ModuleActivationContext,
        scope: ResourceScope,
    ) -> Mapping[str, object]:
        del scope
        global _activation_count
        _activation_count += 1
        return {capability: self for capability in context.declared_capabilities}

    async def sleep(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


def create_fault_module() -> _FaultModule:
    return _FaultModule()


async def run_fault_benchmark() -> dict[str, object]:
    """运行五种最终架构故障并要求分类稳定、资源归零。"""
    with tempfile.TemporaryDirectory(prefix="rivet-fault-benchmark-") as raw_root:
        root = Path(raw_root)
        probes = (
            await _demand_journal_failure(root / "demand"),
            await _provider_disconnect(),
            await _trace_fsync_failure(root / "fsync"),
            await _process_timeout(root / "process"),
            await _trace_tail_corruption(root / "tail"),
        )
    return {
        "schema_version": 1,
        "suite": "faults",
        "passed": all(result.passed for result in probes),
        "case_count": len(probes),
        "passed_count": sum(result.passed for result in probes),
        "resource_leak_count": sum(
            result.resource_count_after != 0 for result in probes
        ),
        "results": [asdict(result) for result in probes],
    }


async def _demand_journal_failure(root: Path) -> FaultResult:
    started_at = perf_counter()
    root.mkdir(parents=True)
    global _activation_count
    _activation_count = 0
    kernel = RivetKernel.from_manifests(
        (
            ModuleManifest(
                module_id="fault.demand",
                factory="scripts.fault_benchmark:create_fault_module",
                provides=("fault.capability",),
            ),
        ),
        demand_journal=InMemoryDemandJournal(fail_after=1),
        lifecycle_sink=InMemoryModuleLifecycleSink(),
        activation_context=ModuleActivationContext(repository=root),
    )
    await kernel.start()
    parent = await kernel.begin_user_demand(
        "fault-test",
        reason="故障测试根需求",
        context=DemandContext(
            run_id="run_fault_demand",
            session_id="session_fault_demand",
        ),
    )
    actual_code = "fault.unexpected_success"
    try:
        await kernel.acquire_required(
            "fault.capability",
            parent=parent,
            reason="必须先耐久记录的能力需求",
        )
    except DemandJournalError:
        actual_code = "demand.journal_write_failed"
    await kernel.shutdown()
    resources = kernel.resource_counts().resource_count
    expected_code = "demand.journal_write_failed"
    return FaultResult(
        fault_id="demand_journal_failure",
        expected_code=expected_code,
        actual_code=actual_code,
        passed=(
            actual_code == expected_code and _activation_count == 0 and resources == 0
        ),
        duration_ms=round((perf_counter() - started_at) * 1_000, 3),
        resource_count_after=resources,
    )


async def _provider_disconnect() -> FaultResult:
    started_at = perf_counter()
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        raise httpx.ConnectError("fixture disconnect", request=request)

    async def no_wait(_delay: float) -> None:
        return None

    scope = ResourceScope("fault.provider_disconnect")
    provider = DeepSeekProvider(
        DeepSeekConfig(max_attempts=2, base_backoff_seconds=0),
        scope=scope,
        environment={"DEEPSEEK_API_KEY": "offline-fixture-only"},
        transport=httpx.MockTransport(handler),
        sleep=no_wait,
    )
    actual_code = "fault.unexpected_success"
    try:
        await provider.complete(
            ModelRequest(
                model="deepseek-v4-pro",
                messages=(UserMessage(content="离线故障", created_at=FIXED_NOW),),
                stream=False,
                max_tokens=8,
            )
        )
    except ProviderUnavailableError as error:
        actual_code = error.code
    await scope.close()
    resources = scope.counts().resource_count
    expected_code = "provider.network_unavailable"
    return FaultResult(
        fault_id="provider_disconnect",
        expected_code=expected_code,
        actual_code=actual_code,
        passed=actual_code == expected_code and calls[0] == 2 and resources == 0,
        duration_ms=round((perf_counter() - started_at) * 1_000, 3),
        resource_count_after=resources,
    )


async def _trace_fsync_failure(root: Path) -> FaultResult:
    started_at = perf_counter()
    repository = root / "repository"
    repository.mkdir(parents=True)
    paths = _runtime_paths(repository, root)
    store = TraceStore(paths)
    await store.start()
    actual_code = "fault.unexpected_success"
    with patch(
        "rivet.trace.store.os.fsync",
        side_effect=OSError(errno.ENOSPC, "fixture disk full"),
    ):
        try:
            await store.emit(_trace_event(1, run_id="run_fault_fsync"))
        except TraceWriteError:
            actual_code = "trace.write_failed"
    with suppress(TraceWriteError):
        await store.close()
    expected_code = "trace.write_failed"
    return FaultResult(
        fault_id="trace_fsync_failure",
        expected_code=expected_code,
        actual_code=actual_code,
        passed=(
            actual_code == expected_code
            and store.pending_event_count == 0
            and not tuple(paths.runtime_root.rglob("*.tmp"))
        ),
        duration_ms=round((perf_counter() - started_at) * 1_000, 3),
        resource_count_after=store.pending_event_count,
    )


async def _process_timeout(root: Path) -> FaultResult:
    started_at = perf_counter()
    repository = root / "repository"
    repository.mkdir(parents=True)
    scope = ResourceScope("fault.process_timeout")
    runner = ProcessRunner(
        WorkspaceBoundary(repository),
        scope=scope,
        root_kind="repository_read_only",
        termination_grace_seconds=0.1,
    )
    result = await runner.run(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        timeout_seconds=0.05,
    )
    await scope.close()
    resources = scope.counts().resource_count
    actual_code = "process.timeout" if result.timed_out else "fault.unexpected_success"
    expected_code = "process.timeout"
    return FaultResult(
        fault_id="process_timeout",
        expected_code=expected_code,
        actual_code=actual_code,
        passed=(
            actual_code == expected_code
            and result.returncode is not None
            and resources == 0
        ),
        duration_ms=round((perf_counter() - started_at) * 1_000, 3),
        resource_count_after=resources,
    )


async def _trace_tail_corruption(root: Path) -> FaultResult:
    started_at = perf_counter()
    repository = root / "repository"
    repository.mkdir(parents=True)
    paths = _runtime_paths(repository, root)
    store = TraceStore(paths)
    await store.start()
    await store.emit(_trace_event(1, run_id="run_fault_tail"))
    await store.close()
    with paths.events_path.open("ab") as stream:
        stream.write(b'{"schema_version":1,"sequence":2')
    recovered = TraceStore(paths)
    await recovered.start()
    record = await recovered.emit(_trace_event(2, run_id="run_fault_tail"))
    truncated_bytes = recovered.recovery_report.truncated_bytes
    await recovered.close()
    actual_code = (
        "trace.tail_recovered"
        if truncated_bytes > 0 and record.sequence == 2
        else "fault.unexpected_success"
    )
    expected_code = "trace.tail_recovered"
    return FaultResult(
        fault_id="trace_tail_corruption",
        expected_code=expected_code,
        actual_code=actual_code,
        passed=actual_code == expected_code,
        duration_ms=round((perf_counter() - started_at) * 1_000, 3),
        resource_count_after=0,
    )


def _runtime_paths(repository: Path, root: Path) -> RuntimePaths:
    return RuntimePaths.for_repository(
        repository,
        environment={
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_STATE_HOME": str(root / "state"),
        },
    )


def _trace_event(sequence: int, *, run_id: str) -> TraceEventEnvelope:
    return TraceEventEnvelope(
        event_id=f"event_fault_trace_{sequence}",
        event_type="trace.fault_observed",
        timestamp=FIXED_NOW,
        run_id=run_id,
        session_id="session_fault_trace",
        payload={"sequence": sequence},
    )
