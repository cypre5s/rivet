"""验证 ToolExecutor 的 Validate/Demand/Authorize/Acquire/Execute 顺序。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel

from rivet.contracts.guard import (
    AuthorizationDecision,
    AuthorizationStatus,
    PermissionRequest,
    PermissionScope,
)
from rivet.contracts.modules import ModuleManifest
from rivet.contracts.tools import ToolCall
from rivet.kernel.agent_tools import AgentToolRejectedError
from rivet.kernel.application import RivetKernel
from rivet.kernel.capability_demand import DemandContext
from rivet.kernel.module_api import ModuleActivationContext
from rivet.kernel.module_runtime import CapabilityLease
from rivet.tools import executor as executor_module
from rivet.tools.catalog import TOOL_CATALOG
from rivet.tools.executor import (
    CatalogToolExecutor,
    SideEffectJournal,
    ToolAuthorizer,
    ToolExecutionContext,
    ToolExecutionError,
)
from rivet.trace.adapters import TraceDemandJournal, TraceModuleLifecycleSink
from rivet.trace.builder import TraceEventBuilder
from rivet.trace.paths import RuntimePaths
from rivet.trace.store import TraceStore
from tests.fixtures.kernel import fake_modules


async def _allow(request: PermissionRequest) -> AuthorizationDecision:
    del request
    return AuthorizationDecision(
        status=AuthorizationStatus.ALLOWED,
        code="guard.test_allowed",
        summary="test allowed",
    )


async def _deny(request: PermissionRequest) -> AuthorizationDecision:
    del request
    return AuthorizationDecision(
        status=AuthorizationStatus.DENIED,
        code="guard.test_denied",
        summary="test denied",
    )


def _paths(tmp_path: Path) -> RuntimePaths:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".git").mkdir()
    return RuntimePaths.for_repository(
        repository,
        environment={
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
    )


def _manifests() -> tuple[ModuleManifest, ...]:
    factory = "tests.fixtures.kernel.fake_modules:create_recording_module"
    return (
        ModuleManifest(
            module_id="context.lexical",
            factory=factory,
            provides=("context.search.lexical",),
        ),
        ModuleManifest(
            module_id="transaction.git",
            factory=factory,
            provides=("transaction.worktree",),
        ),
        ModuleManifest(
            module_id="guard.sandbox",
            factory=factory,
            provides=("guard.local_execution",),
        ),
    )


async def _runtime(
    tmp_path: Path,
    *,
    mode: str,
    authorizer: ToolAuthorizer = _allow,
    handler_error: BaseException | None = None,
) -> tuple[
    RivetKernel,
    TraceStore,
    CatalogToolExecutor,
    list[str],
    ToolExecutionContext,
]:
    fake_modules.reset_observations()
    paths = _paths(tmp_path)
    trace = TraceStore(paths)
    await trace.start()
    demands = TraceDemandJournal(trace)
    lifecycle = TraceModuleLifecycleSink(trace, demands)
    kernel = RivetKernel.from_manifests(
        _manifests(),
        demand_journal=demands,
        lifecycle_sink=lifecycle,
        activation_context=ModuleActivationContext(
            repository=paths.repository_root,
        ),
    )
    await kernel.start()
    transaction_id = "tx_tools" if mode == "fix" else None
    root = await kernel.begin_user_demand(
        f"task.{mode}",
        reason="user task",
        context=DemandContext(
            run_id="run_tools",
            session_id="session_tools",
            transaction_id=transaction_id,
        ),
    )
    observations: list[str] = []

    async def handler(
        _arguments: BaseModel,
        capabilities: Mapping[str, object],
    ) -> str:
        observations.append(",".join(sorted(capabilities)))
        if handler_error is not None:
            raise handler_error
        return "handled"

    handlers = {spec.executor: handler for spec in TOOL_CATALOG}
    tool_context = ToolExecutionContext(
        parent_demand=root,
        run_id="run_tools",
        session_id="session_tools",
        transaction_id=transaction_id,
    )
    executor = CatalogToolExecutor(
        kernel,
        mode=mode,
        context=tool_context,
        authorizer=authorizer,
        handlers=handlers,
        side_effect_journal=SideEffectJournal(trace),
    )
    return kernel, trace, executor, observations, tool_context


@pytest.mark.asyncio
async def test_executor_registry_rejects_missing_and_unknown_bindings(
    tmp_path: Path,
) -> None:
    kernel, trace, _executor, observations, tool_context = await _runtime(
        tmp_path,
        mode="ask",
    )

    async def handler(
        _arguments: BaseModel,
        _capabilities: Mapping[str, object],
    ) -> str:
        observations.append("unexpected")
        return "handled"

    handlers = {spec.executor: handler for spec in TOOL_CATALOG}
    handlers.pop("workspace_info")
    handlers["workspace_info_typo"] = handler
    before = trace.event_count

    with pytest.raises(
        ValueError,
        match=r"missing=\['workspace_info'\].*unknown_or_extra=\['workspace_info_typo'\]",
    ):
        CatalogToolExecutor(
            kernel,
            mode="ask",
            context=tool_context,
            authorizer=_allow,
            handlers=handlers,
        )

    assert trace.event_count == before
    assert observations == []
    assert fake_modules.factory_calls == {}
    await kernel.shutdown()
    await trace.close()


@pytest.mark.asyncio
async def test_executor_registry_rejects_duplicate_catalog_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel, trace, _executor, _, tool_context = await _runtime(tmp_path, mode="ask")
    duplicate_catalog = list(TOOL_CATALOG)
    duplicate_catalog[1] = replace(
        duplicate_catalog[1],
        executor=duplicate_catalog[0].executor,
    )
    monkeypatch.setattr(
        executor_module,
        "TOOL_CATALOG",
        tuple(duplicate_catalog),
    )

    async def handler(
        _arguments: BaseModel,
        _capabilities: Mapping[str, object],
    ) -> str:
        return "handled"

    with pytest.raises(ValueError, match="重复工具 executor key"):
        CatalogToolExecutor(
            kernel,
            mode="ask",
            context=tool_context,
            authorizer=_allow,
            handlers={spec.executor: handler for spec in TOOL_CATALOG},
        )

    await kernel.shutdown()
    await trace.close()


@pytest.mark.asyncio
async def test_tool_routes_through_explicit_executor_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel, trace, _executor, _, tool_context = await _runtime(tmp_path, mode="ask")
    rebound_catalog = list(TOOL_CATALOG)
    rebound_catalog[0] = replace(
        rebound_catalog[0],
        executor="workspace_info_executor",
    )
    monkeypatch.setattr(
        executor_module,
        "TOOL_CATALOG",
        tuple(rebound_catalog),
    )
    observations: list[str] = []

    async def default_handler(
        _arguments: BaseModel,
        _capabilities: Mapping[str, object],
    ) -> str:
        return "default"

    async def rebound_handler(
        _arguments: BaseModel,
        _capabilities: Mapping[str, object],
    ) -> str:
        observations.append("workspace_info_executor")
        return "rebound"

    handlers = {spec.executor: default_handler for spec in tuple(rebound_catalog)}
    handlers["workspace_info_executor"] = rebound_handler
    executor = CatalogToolExecutor(
        kernel,
        mode="ask",
        context=tool_context,
        authorizer=_allow,
        handlers=handlers,
    )

    result = await executor.execute(
        ToolCall(
            tool_call_id="call_rebound",
            tool_name="workspace_info",
            arguments={},
        )
    )

    assert result == "rebound"
    assert observations == ["workspace_info_executor"]
    await kernel.shutdown()
    await trace.close()


@pytest.mark.asyncio
async def test_invalid_schema_creates_no_tool_demand_or_activation(
    tmp_path: Path,
) -> None:
    kernel, trace, executor, observations, _ = await _runtime(tmp_path, mode="ask")
    before = trace.event_count
    call = ToolCall(
        tool_call_id="call_invalid",
        tool_name="file_read",
        arguments={"path": "README.md", "unknown": True},
    )

    with pytest.raises(ValueError, match="Schema"):
        await executor.execute(call)

    assert trace.event_count == before
    assert observations == []
    assert fake_modules.factory_calls == {}
    await kernel.shutdown()
    await trace.close()


@pytest.mark.asyncio
async def test_read_only_tool_without_capability_never_activates_module(
    tmp_path: Path,
) -> None:
    kernel, trace, executor, observations, _ = await _runtime(tmp_path, mode="ask")
    result = await executor.execute(
        ToolCall(
            tool_call_id="call_info",
            tool_name="workspace_info",
            arguments={},
        )
    )

    assert result == "handled"
    assert observations == [""]
    assert fake_modules.factory_calls == {}
    demand_events = [
        item.event
        for item in trace.events()
        if item.event.event_type == "demand.created"
    ]
    assert [event.payload["demand_source"] for event in demand_events] == [
        "USER_EXPLICIT",
        "MODEL_TOOL_CALL",
    ]
    await kernel.shutdown()
    await trace.close()


@pytest.mark.asyncio
async def test_transaction_scoped_git_diff_with_path_does_not_build_invalid_permission(
    tmp_path: Path,
) -> None:
    requests: list[PermissionRequest] = []

    async def capture(request: PermissionRequest) -> AuthorizationDecision:
        requests.append(request)
        return await _allow(request)

    kernel, trace, executor, observations, _ = await _runtime(
        tmp_path,
        mode="fix",
        authorizer=capture,
    )

    result = await executor.execute(
        ToolCall(
            tool_call_id="call_diff_path",
            tool_name="git_diff",
            arguments={"path": "calculator.py"},
        )
    )

    assert result == "handled"
    assert observations == ["transaction.worktree"]
    assert len(requests) == 1
    assert requests[0].scope is PermissionScope.TRANSACTION
    assert requests[0].paths == ()
    assert not any(item.event.event_type == "tool.failed" for item in trace.events())
    await kernel.shutdown()
    await trace.close()


@pytest.mark.asyncio
async def test_read_only_tool_failure_is_visible_without_persisting_error_text(
    tmp_path: Path,
) -> None:
    kernel, trace, executor, _, _ = await _runtime(
        tmp_path,
        mode="ask",
        handler_error=RuntimeError("private-tool-detail"),
    )

    with pytest.raises(RuntimeError, match="private-tool-detail"):
        await executor.execute(
            ToolCall(
                tool_call_id="call_failed_info",
                tool_name="workspace_info",
                arguments={},
            )
        )

    failures = [
        item.event for item in trace.events() if item.event.event_type == "tool.failed"
    ]
    assert len(failures) == 1
    assert failures[0].payload == {
        "error_code": None,
        "error_type": "RuntimeError",
        "operation_id": "call_failed_info",
        "status": "FAILED",
        "tool_name": "workspace_info",
    }
    assert "private-tool-detail" not in trace.paths.events_path.read_text(
        encoding="utf-8"
    )
    await kernel.shutdown()
    await trace.close()


@pytest.mark.asyncio
async def test_authorization_denial_precedes_capability_activation(
    tmp_path: Path,
) -> None:
    kernel, trace, executor, observations, _ = await _runtime(
        tmp_path, mode="ask", authorizer=_deny
    )

    with pytest.raises(AgentToolRejectedError, match="test denied"):
        await executor.execute(
            ToolCall(
                tool_call_id="call_context",
                tool_name="context_search",
                arguments={"query": "needle"},
            )
        )

    assert observations == []
    assert fake_modules.factory_calls == {}
    assert not any(
        item.event.event_type == "module.activated" for item in trace.events()
    )
    await kernel.shutdown()
    await trace.close()


@pytest.mark.asyncio
async def test_required_capability_is_child_demand_and_lease_is_released(
    tmp_path: Path,
) -> None:
    kernel, trace, executor, observations, _ = await _runtime(tmp_path, mode="ask")
    result = await executor.execute(
        ToolCall(
            tool_call_id="call_context",
            tool_name="context_search",
            arguments={"query": "needle"},
        )
    )

    assert result == "handled"
    assert observations == ["context.search.lexical"]
    demand_events = [
        item.event
        for item in trace.events()
        if item.event.event_type == "demand.created"
    ]
    assert [event.payload["demand_source"] for event in demand_events] == [
        "USER_EXPLICIT",
        "MODEL_TOOL_CALL",
        "KERNEL_REQUIRED",
    ]
    assert demand_events[2].parent_event_id == demand_events[1].event_id
    assert any(item.event.event_type == "module.released" for item in trace.events())
    assert kernel.resource_counts().resource_count == 0
    await kernel.shutdown()
    await trace.close()


@pytest.mark.asyncio
async def test_multiple_capability_leases_release_sequentially_in_reverse_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel, trace, executor, observations, _ = await _runtime(tmp_path, mode="fix")
    release_order: list[str] = []
    original_release = cast(
        Callable[[CapabilityLease[object]], Awaitable[None]],
        CapabilityLease.release,
    )

    async def observed_release(lease: CapabilityLease[object]) -> None:
        release_order.append(lease.module_id)
        await original_release(lease)
        if lease.module_id == "guard.sandbox":
            raise RuntimeError("可控 Guard Lease 释放错误")

    monkeypatch.setattr(CapabilityLease, "release", observed_release)

    with pytest.raises(ToolExecutionError, match="Lease 释放失败"):
        await executor.execute(
            ToolCall(
                tool_call_id="call_release_order",
                tool_name="file_write",
                arguments={"path": "src/example.py", "content": "value = 1\n"},
            )
        )

    assert observations == ["guard.local_execution,transaction.worktree"]
    assert release_order == ["guard.sandbox", "transaction.git"]
    assert kernel.resource_counts().resource_count == 0
    await kernel.shutdown()
    await trace.close()


@pytest.mark.asyncio
async def test_side_effect_records_only_started_and_succeeded_facts(
    tmp_path: Path,
) -> None:
    kernel, trace, executor, observations, _ = await _runtime(tmp_path, mode="fix")
    result = await executor.execute(
        ToolCall(
            tool_call_id="call_write",
            tool_name="file_write",
            arguments={"path": "src/example.py", "content": "value = 1\n"},
        )
    )

    assert result == "handled"
    assert observations == ["guard.local_execution,transaction.worktree"]
    facts = [
        item.event.payload
        for item in trace.events()
        if item.event.event_type == "side_effect.checkpoint"
    ]
    assert [fact["status"] for fact in facts] == ["STARTED", "SUCCEEDED"]
    assert all("content" not in json_value for fact in facts for json_value in fact)
    assert SideEffectJournal(trace).unknown_operations(run_id="run_tools") == ()
    await kernel.shutdown()
    await trace.close()


@pytest.mark.asyncio
async def test_unknown_is_derived_from_started_without_terminal_fact(
    tmp_path: Path,
) -> None:
    kernel, trace, _executor, _, tool_context = await _runtime(tmp_path, mode="fix")
    call = ToolCall(
        tool_call_id="call_interrupted",
        tool_name="file_write",
        arguments={"path": "src/example.py", "content": "secret input"},
    )
    journal = SideEffectJournal(trace)
    await journal.started(
        call=call,
        arguments_sha256="sha256:" + "0" * 64,
        context=tool_context,
        parent_event_id=tool_context.parent_demand.event_id,
    )

    assert journal.unknown_operations(run_id="run_tools") == ("call_interrupted",)
    assert not any(
        item.event.payload.get("status") == "UNKNOWN" for item in trace.events()
    )
    await kernel.shutdown()
    await trace.close()


@pytest.mark.asyncio
async def test_reused_operation_id_cannot_hide_unknown_from_another_run(
    tmp_path: Path,
) -> None:
    """副作用身份包含来源 run，恢复终态必须显式指向原始 run。"""
    trace = TraceStore(_paths(tmp_path))
    await trace.start()
    builder = TraceEventBuilder()
    transaction_id = "tx_operation_collision"

    async def checkpoint(
        run_id: str,
        status: str,
        *,
        originating_run_id: str | None = None,
    ) -> None:
        parent = builder.build(
            event_type="test.root",
            run_id=run_id,
            session_id=f"session_{run_id}",
            transaction_id=transaction_id,
            payload={},
        )
        await trace.emit(parent)
        await trace.emit(
            builder.build(
                event_type="side_effect.checkpoint",
                run_id=run_id,
                session_id=f"session_{run_id}",
                transaction_id=transaction_id,
                parent_event_id=parent.event_id,
                payload={
                    "arguments_sha256": "sha256:" + "1" * 64,
                    "error_type": None,
                    "operation": "file_write",
                    "operation_id": "provider_reused_id",
                    "originating_run_id": originating_run_id or run_id,
                    "result_sha256": None,
                    "status": status,
                },
            )
        )

    await checkpoint("run_a", "STARTED")
    await checkpoint("run_b", "STARTED")
    await checkpoint("run_b", "SUCCEEDED")

    journal = SideEffectJournal(trace)
    unknowns = journal.unknown_for_transaction(transaction_id=transaction_id)
    assert len(unknowns) == 1
    unknown = unknowns[0]
    assert (unknown.originating_run_id, unknown.operation_id) == (
        "run_a",
        "provider_reused_id",
    )

    await checkpoint("run_recovery", "FAILED", originating_run_id="run_a")
    assert journal.unknown_for_transaction(transaction_id=transaction_id) == ()
    await trace.close()


@pytest.mark.asyncio
async def test_failed_write_records_failed_terminal_without_error_text(
    tmp_path: Path,
) -> None:
    kernel, trace, executor, _, _ = await _runtime(
        tmp_path,
        mode="fix",
        handler_error=RuntimeError("do-not-persist-this-message"),
    )

    with pytest.raises(RuntimeError, match="do-not-persist"):
        await executor.execute(
            ToolCall(
                tool_call_id="call_failed_write",
                tool_name="file_write",
                arguments={"path": "src/example.py", "content": "value = 1\n"},
            )
        )

    facts = [
        item.event.payload
        for item in trace.events()
        if item.event.event_type == "side_effect.checkpoint"
        and item.event.payload.get("operation_id") == "call_failed_write"
    ]
    assert [fact["status"] for fact in facts] == ["STARTED", "FAILED"]
    assert facts[-1]["error_type"] == "RuntimeError"
    assert "do-not-persist-this-message" not in trace.paths.events_path.read_text(
        encoding="utf-8"
    )
    assert SideEffectJournal(trace).unknown_operations(run_id="run_tools") == ()
    await kernel.shutdown()
    await trace.close()


@pytest.mark.asyncio
async def test_process_run_accepts_json_array_and_records_three_fact_protocol(
    tmp_path: Path,
) -> None:
    kernel, trace, executor, observations, _ = await _runtime(tmp_path, mode="fix")

    result = await executor.execute(
        ToolCall(
            tool_call_id="call_process",
            tool_name="process_run",
            arguments={"argv": ["python", "-V"]},
        )
    )

    assert result == "handled"
    assert observations == ["guard.local_execution,transaction.worktree"]
    facts = [
        item.event.payload
        for item in trace.events()
        if item.event.event_type == "side_effect.checkpoint"
        and item.event.payload.get("operation_id") == "call_process"
    ]
    assert [fact["status"] for fact in facts] == ["STARTED", "SUCCEEDED"]
    assert [fact["operation"] for fact in facts] == ["process_run", "process_run"]
    await kernel.shutdown()
    await trace.close()
