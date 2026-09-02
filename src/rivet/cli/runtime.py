"""把 CLI 连接到 NDJSON Demand Journal 与五模块 Kernel。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from rivet.kernel.application import RivetKernel
from rivet.kernel.module_api import CredentialAccessor, ModuleActivationContext
from rivet.kernel.module_runtime import (
    CapabilityLease,
    release_capability_leases,
)
from rivet.modules.catalog import BUILTIN_MODULE_MANIFESTS
from rivet.trace.adapters import TraceDemandJournal, TraceModuleLifecycleSink
from rivet.trace.builder import TraceEventBuilder
from rivet.trace.paths import RuntimePaths
from rivet.trace.redaction import SecretRedactor
from rivet.trace.store import TraceStore


@dataclass(slots=True)
class CliRuntime:
    """保存一次命令拥有且必须共同关闭的 Kernel 与 Trace。"""

    kernel: RivetKernel
    trace: TraceStore
    demands: TraceDemandJournal
    builder: TraceEventBuilder

    async def close(self) -> None:
        """先释放全部模块，再排空 Trace Writer。"""
        first_error: BaseException | None = None
        try:
            await self.kernel.shutdown()
        except BaseException as error:
            first_error = error
        try:
            await self.trace.close()
        except BaseException as error:
            if first_error is None:
                first_error = error
        if first_error is not None:
            raise first_error


async def close_cli_runtime(
    runtime: CliRuntime,
    leases: Iterable[CapabilityLease[object]],
) -> None:
    """释放全部任务 Lease，并保证 Kernel 与 Trace 最终都会关闭。"""
    first_error: BaseException | None = None
    try:
        await release_capability_leases(leases)
    except BaseException as error:
        first_error = error
    try:
        await runtime.close()
    except BaseException as error:
        if first_error is None:
            first_error = error
    if first_error is not None:
        raise first_error


async def start_cli_runtime(
    repository: Path,
    *,
    environment: Mapping[str, str],
    provider_base_url: str | None = None,
    credential_accessor: CredentialAccessor | None = None,
) -> CliRuntime:
    """先启动耐久 Trace，再构造零激活 Kernel。"""
    redactor = SecretRedactor(environment)
    builder = TraceEventBuilder(redactor=redactor)
    paths = RuntimePaths.for_repository(repository, environment=environment)
    trace = TraceStore(
        paths,
        redactor=redactor,
    )
    await trace.start()
    demands = TraceDemandJournal(trace, builder=builder)
    lifecycle = TraceModuleLifecycleSink(
        trace,
        demands,
        builder=builder,
    )
    kernel = RivetKernel.from_manifests(
        BUILTIN_MODULE_MANIFESTS,
        demand_journal=demands,
        lifecycle_sink=lifecycle,
        activation_context=ModuleActivationContext(
            repository=repository.resolve(strict=True),
            provider_base_url=provider_base_url,
            credential_accessor=credential_accessor,
            transaction_state_root=paths.transactions_root,
            evidence_state_root=paths.evidence_root,
            worktree_cache_root=paths.cache_root,
        ),
    )
    try:
        await kernel.start()
    except BaseException:
        await trace.close()
        raise
    return CliRuntime(
        kernel=kernel,
        trace=trace,
        demands=demands,
        builder=builder,
    )
