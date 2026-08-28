"""验证模型控制的工具不能绕过权限和污点边界。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rivet.contracts.guard import (
    Permission,
    PermissionRequest,
    PermissionScope,
    TaintSource,
)
from rivet.contracts.tools import ToolCall
from rivet.guard.permissions import GuardPolicy
from rivet.guard.sandbox import BubblewrapSandbox
from rivet.kernel.resources import ResourceScope
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.registry import ToolInvocationContext
from rivet.tools.toolset import build_workspace_tool_registry
from rivet.trace.paths import RuntimePaths
from rivet.trace.store import TraceStore

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


async def _trace(tmp_path: Path, repository: Path) -> TraceStore:
    """建立不会写入仓库的真实 Trace。"""
    trace = TraceStore(
        RuntimePaths.for_repository(
            repository,
            environment={"XDG_CACHE_HOME": str(tmp_path / "cache")},
        )
    )
    await trace.start()
    return trace


@pytest.mark.asyncio
async def test_sensitive_tool_without_lease_fails_and_records_guard_event(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    trace = await _trace(tmp_path, repository)
    scope = ResourceScope("guard.registry.denied")
    policy = GuardPolicy(headless=True, clock=lambda: NOW)
    registry = build_workspace_tool_registry(
        WorkspaceBoundary(repository, transaction),
        scope=scope,
        authorizer=policy.authorize,
    )
    context = ToolInvocationContext(
        run_id="run_guard_denied",
        session_id="session_guard_denied",
        transaction_id="tx_guard_denied",
        trace=trace,
    )

    view = await registry.invoke(
        ToolCall(
            tool_call_id="call_guard_denied",
            tool_name="file.create_transaction",
            arguments={"path": "escaped.txt", "content": "bad"},
        ),
        context=context,
    )

    event_types = [
        item.event.event_type for item in trace.replay("run_guard_denied").events
    ]
    assert not view.result.success
    assert view.result.error is not None
    assert view.result.error.code == "guard.permission_required"
    assert not (transaction / "escaped.txt").exists()
    assert event_types == [
        "tool.started",
        "guard.authorization_denied",
        "tool.failed",
    ]
    await trace.close()
    await scope.close()


@pytest.mark.asyncio
async def test_repository_prompt_injection_cannot_use_interactive_prompt(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    trace = await _trace(tmp_path, repository)
    scope = ResourceScope("guard.registry.taint")
    policy = GuardPolicy(headless=False, clock=lambda: NOW)
    registry = build_workspace_tool_registry(
        WorkspaceBoundary(repository, transaction),
        scope=scope,
        authorizer=policy.authorize,
    )
    context = ToolInvocationContext(
        run_id="run_guard_taint",
        session_id="session_guard_taint",
        transaction_id="tx_guard_taint",
        trace=trace,
        taint_sources=(TaintSource.REPOSITORY_DATA,),
    )

    view = await registry.invoke(
        ToolCall(
            tool_call_id="call_guard_taint",
            tool_name="file.delete_transaction",
            arguments={"path": "target.txt"},
        ),
        context=context,
    )

    assert not view.result.success
    assert view.result.error is not None
    assert view.result.error.code == "guard.tainted_permission_denied"
    await trace.close()
    await scope.close()


@pytest.mark.asyncio
async def test_explicit_path_lease_allows_only_approved_transaction_file(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    trace = await _trace(tmp_path, repository)
    scope = ResourceScope("guard.registry.allowed")
    policy = GuardPolicy(headless=True, clock=lambda: NOW)
    request = PermissionRequest(
        permission=Permission.WRITE,
        scope=PermissionScope.SPECIFIC_PATHS,
        reason="创建已确认文件",
        run_id="run_guard_allowed",
        transaction_id="tx_guard_allowed",
        paths=("approved.txt",),
    )
    policy.issue_lease(
        request,
        approved_by_user=True,
        expires_at=NOW + timedelta(minutes=1),
        max_uses=1,
    )
    registry = build_workspace_tool_registry(
        WorkspaceBoundary(repository, transaction),
        scope=scope,
        authorizer=policy.authorize,
    )
    context = ToolInvocationContext(
        run_id="run_guard_allowed",
        session_id="session_guard_allowed",
        transaction_id="tx_guard_allowed",
        trace=trace,
    )

    view = await registry.invoke(
        ToolCall(
            tool_call_id="call_guard_allowed",
            tool_name="file.create_transaction",
            arguments={"path": "approved.txt", "content": "ok\n"},
        ),
        context=context,
    )

    assert view.result.success
    assert (transaction / "approved.txt").read_text(encoding="utf-8") == "ok\n"
    await trace.close()
    await scope.close()


@pytest.mark.asyncio
async def test_missing_sandbox_records_violation_after_execute_lease(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    boundary = WorkspaceBoundary(repository, transaction)
    trace = await _trace(tmp_path, repository)
    scope = ResourceScope("guard.registry.sandbox")
    policy = GuardPolicy(headless=True, clock=lambda: NOW)
    request = PermissionRequest(
        permission=Permission.EXECUTE,
        scope=PermissionScope.TRANSACTION,
        reason="运行已确认检查",
        run_id="run_guard_sandbox",
        transaction_id="tx_guard_sandbox",
    )
    policy.issue_lease(
        request,
        approved_by_user=True,
        expires_at=NOW + timedelta(minutes=1),
        max_uses=1,
    )
    registry = build_workspace_tool_registry(
        boundary,
        scope=scope,
        authorizer=policy.authorize,
        process_executor=BubblewrapSandbox(
            boundary,
            scope=scope,
            executable=tmp_path / "missing-bwrap",
        ),
    )
    context = ToolInvocationContext(
        run_id="run_guard_sandbox",
        session_id="session_guard_sandbox",
        transaction_id="tx_guard_sandbox",
        trace=trace,
    )

    view = await registry.invoke(
        ToolCall(
            tool_call_id="call_guard_sandbox",
            tool_name="process.run",
            arguments={"argv": ["python", "-V"], "timeout_seconds": 2.0},
        ),
        context=context,
    )

    event_types = [
        item.event.event_type for item in trace.replay("run_guard_sandbox").events
    ]
    assert not view.result.success
    assert view.result.error is not None
    assert view.result.error.code == "sandbox.unavailable"
    assert event_types == ["tool.started", "sandbox.violation", "tool.failed"]
    await trace.close()
    await scope.close()
