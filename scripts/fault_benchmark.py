"""对 Provider、LSP、磁盘、进程和 Trace 执行确定性故障注入。"""

from __future__ import annotations

import errno
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import httpx

from rivet.context.lsp_manifest import LspServerManifest
from rivet.context.lsp_models import LspPosition
from rivet.context.lsp_sidecar import LspRestartLimitError, LspSidecar
from rivet.contracts.events import TraceEventEnvelope
from rivet.contracts.messages import UserMessage
from rivet.contracts.provider import ModelRequest
from rivet.kernel.resources import ResourceScope
from rivet.providers.deepseek import DeepSeekProvider
from rivet.providers.errors import ProviderUnavailableError
from rivet.providers.models import DeepSeekConfig
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.process import ProcessRunner
from rivet.trace.artifacts import TraceArtifactStore
from rivet.trace.errors import TraceWriteError
from rivet.trace.paths import RuntimePaths
from rivet.trace.redaction import SecretRedactor
from rivet.trace.store import TraceStore

FIXED_NOW = datetime(2026, 8, 28, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FaultResult:
    """保存单项故障的分类、耗时与资源归零事实。"""

    fault_id: str
    expected_code: str
    actual_code: str
    passed: bool
    duration_ms: float
    resource_count_after: int


async def run_fault_benchmark() -> dict[str, object]:
    """运行五种故障并要求全部分类且资源归零。"""
    with tempfile.TemporaryDirectory(prefix="rivet-fault-benchmark-") as raw_root:
        root = Path(raw_root)
        probes = (
            await _provider_disconnect(),
            await _lsp_crash(root / "lsp"),
            _disk_full(root / "disk"),
            await _process_timeout(root / "process"),
            await _trace_tail_corruption(root / "trace"),
        )
    passed = all(result.passed for result in probes)
    return {
        "schema_version": 1,
        "suite": "faults",
        "passed": passed,
        "case_count": len(probes),
        "passed_count": sum(result.passed for result in probes),
        "resource_leak_count": sum(
            result.resource_count_after != 0 for result in probes
        ),
        "results": [asdict(result) for result in probes],
    }


async def _provider_disconnect() -> FaultResult:
    """模拟连续断连并确认有界重试后分类失败。"""
    started_at = perf_counter()
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        """在任何请求上模拟连接失败。"""
        calls[0] += 1
        raise httpx.ConnectError("fixture disconnect", request=request)

    async def no_wait(_delay: float) -> None:
        """保留重试次数但不引入墙钟等待。"""

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
    finally:
        await scope.close()
    expected_code = "provider.network_unavailable"
    return FaultResult(
        fault_id="api_disconnect",
        expected_code=expected_code,
        actual_code=actual_code,
        passed=actual_code == expected_code
        and calls[0] == 2
        and not scope.counts().resource_count,
        duration_ms=round((perf_counter() - started_at) * 1_000, 3),
        resource_count_after=scope.counts().resource_count,
    )


async def _lsp_crash(root: Path) -> FaultResult:
    """运行真实崩溃 sidecar 并验证只重启一次。"""
    started_at = perf_counter()
    repository = root / "repository"
    repository.mkdir(parents=True)
    (repository / "target.py").write_text("symbol = 1\n", encoding="utf-8")
    server = Path("tests/fixtures/context/lsp_server.py").resolve(strict=True)
    manifest = LspServerManifest(
        server_id="fault-fixture",
        language_ids=("python",),
        suffixes=(".py",),
        executable_candidates=(sys.executable,),
        arguments=(
            str(server),
            "--behavior",
            "crash-always",
            "--definition-uri",
            (repository / "target.py").as_uri(),
        ),
        initialization_options={},
        idle_timeout_seconds=300,
        request_timeout_seconds=1,
        max_restarts=1,
    )
    scope = ResourceScope("fault.lsp_crash")
    sidecar = LspSidecar(manifest, repository_root=repository, scope=scope)
    actual_code = "fault.unexpected_success"
    try:
        await sidecar.definition("target.py", LspPosition(0, 1))
    except LspRestartLimitError:
        actual_code = "lsp.restart_limit"
    finally:
        await sidecar.close()
        await scope.close()
    expected_code = "lsp.restart_limit"
    return FaultResult(
        fault_id="lsp_crash",
        expected_code=expected_code,
        actual_code=actual_code,
        passed=(
            actual_code == expected_code
            and sidecar.start_count == 2
            and sidecar.restart_count == 1
            and not scope.counts().resource_count
        ),
        duration_ms=round((perf_counter() - started_at) * 1_000, 3),
        resource_count_after=scope.counts().resource_count,
    )


def _disk_full(root: Path) -> FaultResult:
    """注入 ENOSPC 并确认 artifact 不会留下临时文件或伪造引用。"""
    started_at = perf_counter()
    root.mkdir(parents=True)
    paths = RuntimePaths.for_repository(
        root,
        environment={"XDG_CACHE_HOME": str(root / "cache")},
    )
    store = TraceArtifactStore(paths, SecretRedactor(environment={}))
    actual_code = "fault.unexpected_success"
    with patch.object(
        Path,
        "write_bytes",
        side_effect=OSError(errno.ENOSPC, "fixture disk full"),
    ):
        try:
            store.capture(
                run_id="run_fault_disk",
                event_id="event_fault_disk",
                stdout="bounded output",
                stderr="",
            )
        except TraceWriteError:
            actual_code = "trace.artifact_write_failed"
    temporary_files = tuple(paths.runtime_root.rglob("*.tmp"))
    expected_code = "trace.artifact_write_failed"
    return FaultResult(
        fault_id="disk_full",
        expected_code=expected_code,
        actual_code=actual_code,
        passed=actual_code == expected_code and not temporary_files,
        duration_ms=round((perf_counter() - started_at) * 1_000, 3),
        resource_count_after=0,
    )


async def _process_timeout(root: Path) -> FaultResult:
    """让真实子进程超时并验证进程组与 drain 任务全部回收。"""
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
    actual_code = "process.timeout" if result.timed_out else "fault.unexpected_success"
    expected_code = "process.timeout"
    return FaultResult(
        fault_id="process_timeout",
        expected_code=expected_code,
        actual_code=actual_code,
        passed=(
            actual_code == expected_code
            and result.returncode is not None
            and not scope.counts().resource_count
        ),
        duration_ms=round((perf_counter() - started_at) * 1_000, 3),
        resource_count_after=scope.counts().resource_count,
    )


async def _trace_tail_corruption(root: Path) -> FaultResult:
    """追加半条 NDJSON 并确认只截断损坏尾部后继续序号。"""
    started_at = perf_counter()
    root.mkdir(parents=True)
    paths = RuntimePaths.for_repository(
        root,
        environment={"XDG_CACHE_HOME": str(root / "cache")},
    )
    store = TraceStore(paths, redactor=SecretRedactor(environment={}))
    await store.start()
    await store.emit(_trace_event(1))
    await store.close()
    with paths.events_path.open("ab") as stream:
        stream.write(b'{"schema_version":1,"sequence":2')
    recovered = TraceStore(paths, redactor=SecretRedactor(environment={}))
    await recovered.start()
    record = await recovered.emit(_trace_event(2))
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


def _trace_event(sequence: int) -> TraceEventEnvelope:
    """构造固定 Trace 事件。"""
    return TraceEventEnvelope(
        event_id=f"event_fault_trace_{sequence}",
        event_type="trace.fault_observed",
        timestamp=FIXED_NOW,
        run_id="run_fault_trace",
        session_id="session_fault_trace",
        payload={"sequence": sequence},
    )
