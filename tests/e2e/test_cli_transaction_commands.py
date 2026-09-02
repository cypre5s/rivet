"""验证跨 CLI 生命周期的 XDG 事务 diff 与 abort。"""

from __future__ import annotations

import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from rivet.cli.errors import CliSecurityError
from rivet.cli.runtime import start_cli_runtime
from rivet.cli.transaction_commands import run_transaction_command
from rivet.contracts.transactions import TransactionState
from rivet.kernel.capability_demand import DemandContext
from rivet.kernel.resources import ResourceScope
from rivet.tools.executor import SideEffectJournal, ToolExecutionContext
from rivet.tools.files import TransactionFileWriter
from rivet.trace.paths import RuntimePaths
from rivet.trace.store import TraceStore
from rivet.transaction.manager import TransactionManager
from rivet.transaction.store import TransactionStore
from tests.transaction_helpers import (
    acceptance_spec,
    initialize_repository,
    passed_verdict,
)


@pytest.mark.asyncio
async def test_suspended_transaction_can_diff_and_abort_without_session_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = initialize_repository(tmp_path)
    environment = {
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    scope = ResourceScope("transaction.cli.prepare")
    manager = TransactionManager(repository, scope=scope)
    record = await manager.create(
        acceptance_spec(acceptance_id="acceptance_cli_transaction"),
        confirmed=True,
        transaction_id="tx_cli_transaction",
    )
    TransactionFileWriter(manager.transaction_boundary(record.transaction_id)).write(
        "tracked.txt",
        "cli patched\n",
    )
    await manager.record_patch_set(
        record.transaction_id,
        patch_id="patch_cli_transaction",
    )
    worktree = manager.worktree_path(record.transaction_id)
    manager.suspend(record.transaction_id)
    scope.assert_empty()
    await scope.close()

    diff_code = await run_transaction_command(
        Namespace(command="diff", transaction_id=record.transaction_id),
        repository=repository,
        environment=environment,
        json_output=False,
    )
    diff_output = capsys.readouterr().out
    abort_code = await run_transaction_command(
        Namespace(command="abort", transaction_id=record.transaction_id),
        repository=repository,
        environment=environment,
        json_output=True,
    )

    assert diff_code == 0
    assert "cli patched" in diff_output
    assert abort_code == 0
    assert not worktree.exists()
    paths = RuntimePaths.for_repository(repository, environment=environment)
    assert paths.transactions_root.is_dir()
    assert not (repository / ".rivet" / "sessions").exists()


async def _persist_interrupted_side_effect(
    repository: Path,
    environment: dict[str, str],
    *,
    transaction_id: str,
    operation_id: str,
    operation: str,
) -> None:
    runtime = await start_cli_runtime(repository, environment=environment)
    root = await runtime.kernel.begin_user_demand(
        "test.interrupted-side-effect",
        reason="模拟副作用跨进程丢失终态",
        context=DemandContext(
            run_id=f"run_{operation_id}",
            session_id=f"session_{operation_id}",
            transaction_id=transaction_id,
        ),
    )
    context = ToolExecutionContext(
        parent_demand=root,
        run_id=f"run_{operation_id}",
        session_id=f"session_{operation_id}",
        transaction_id=transaction_id,
    )
    await SideEffectJournal(runtime.trace).operation_started(
        operation_id=operation_id,
        operation=operation,
        arguments_sha256="sha256:" + "0" * 64,
        context=context,
        parent_event_id=root.event_id,
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_unknown_write_blocks_cross_process_verify_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = initialize_repository(tmp_path)
    environment = {
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    scope = ResourceScope("transaction.unknown-write")
    manager = TransactionManager(repository, scope=scope)
    record = await manager.create(
        acceptance_spec(acceptance_id="acceptance_unknown_write"),
        confirmed=True,
        transaction_id="tx_unknown_write",
    )
    TransactionFileWriter(manager.transaction_boundary(record.transaction_id)).write(
        "tracked.txt",
        "unknown candidate\n",
    )
    await manager.record_patch_set(
        record.transaction_id, patch_id="patch_unknown_write"
    )
    manager.suspend(record.transaction_id)
    await scope.close()
    await _persist_interrupted_side_effect(
        repository,
        environment,
        transaction_id=record.transaction_id,
        operation_id="write_interrupted",
        operation="file_write",
    )

    with pytest.raises(CliSecurityError, match="状态未知"):
        await run_transaction_command(
            Namespace(command="verify", transaction_id=record.transaction_id),
            repository=repository,
            environment=environment,
            json_output=True,
        )

    paths = RuntimePaths.for_repository(repository, environment=environment)
    assert (
        TransactionStore(
            paths.transactions_root,
            evidence_root=paths.evidence_root,
        )
        .load_record(record.transaction_id)
        .state
        is TransactionState.PATCHING
    )
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == "base\n"


@pytest.mark.asyncio
async def test_unknown_apply_is_recovered_once_and_receives_terminal_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = initialize_repository(tmp_path)
    environment = {
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    scope = ResourceScope("transaction.unknown-apply")
    manager = TransactionManager(repository, scope=scope)
    record = await manager.create(
        acceptance_spec(acceptance_id="acceptance_unknown_apply"),
        confirmed=True,
        transaction_id="tx_unknown_apply",
    )
    TransactionFileWriter(manager.transaction_boundary(record.transaction_id)).write(
        "tracked.txt",
        "recovered apply\n",
    )
    patched = await manager.record_patch_set(
        record.transaction_id,
        patch_id="patch_unknown_apply",
    )
    verifying = await manager.begin_verification(record.transaction_id)
    verified = await manager.record_verdict(passed_verdict(verifying, manager))
    assert verified.state is TransactionState.VERIFIED
    manager.suspend(record.transaction_id)
    await scope.close()
    await _persist_interrupted_side_effect(
        repository,
        environment,
        transaction_id=record.transaction_id,
        operation_id="apply_interrupted",
        operation="apply",
    )
    worktree = manager.worktree_path(record.transaction_id)

    with pytest.raises(CliSecurityError) as abort_error:
        await run_transaction_command(
            Namespace(command="abort", transaction_id=record.transaction_id),
            repository=repository,
            environment=environment,
            json_output=True,
        )

    assert abort_error.value.code == "transaction.apply_recovery_required"
    paths = RuntimePaths.for_repository(repository, environment=environment)
    store = TransactionStore(
        paths.transactions_root,
        evidence_root=paths.evidence_root,
    )
    assert store.load_record(record.transaction_id).state is TransactionState.VERIFIED
    assert worktree.is_dir()
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == "base\n"

    code = await run_transaction_command(
        Namespace(command="apply", transaction_id=record.transaction_id),
        repository=repository,
        environment=environment,
        json_output=True,
    )

    assert code == 0
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == (
        "recovered apply\n"
    )
    trace = await _opened_trace(paths)
    try:
        journal = SideEffectJournal(trace)
        assert (
            journal.unknown_for_transaction(transaction_id=record.transaction_id) == ()
        )
        statuses = [
            event.event.payload["status"]
            for event in trace.events()
            if event.event.event_type == "side_effect.checkpoint"
            and event.event.payload.get("operation_id") == "apply_interrupted"
        ]
        assert statuses == ["STARTED", "SUCCEEDED"]
    finally:
        await trace.close()
    assert patched.patch_sha256


@pytest.mark.asyncio
async def test_failed_apply_has_terminal_fact_and_never_changes_main_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply 漂移失败必须闭合 checkpoint，且不能触碰当前主工作区。"""
    repository = initialize_repository(tmp_path)
    environment = {
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    scope = ResourceScope("transaction.failed-apply")
    manager = TransactionManager(repository, scope=scope)
    record = await manager.create(
        acceptance_spec(acceptance_id="acceptance_failed_apply"),
        confirmed=True,
        transaction_id="tx_failed_apply",
    )
    TransactionFileWriter(manager.transaction_boundary(record.transaction_id)).write(
        "tracked.txt",
        "candidate value\n",
    )
    await manager.record_patch_set(
        record.transaction_id,
        patch_id="patch_failed_apply",
    )
    verifying = await manager.begin_verification(record.transaction_id)
    verified = await manager.record_verdict(passed_verdict(verifying, manager))
    assert verified.state is TransactionState.VERIFIED
    manager.suspend(record.transaction_id)
    await scope.close()

    (repository / "tracked.txt").write_text("user drift\n", encoding="utf-8")
    content_before = (repository / "tracked.txt").read_bytes()
    status_before = subprocess.run(
        ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout

    with pytest.raises(CliSecurityError, match="漂移"):
        await run_transaction_command(
            Namespace(command="apply", transaction_id=record.transaction_id),
            repository=repository,
            environment=environment,
            json_output=True,
        )

    assert (repository / "tracked.txt").read_bytes() == content_before
    status_after = subprocess.run(
        ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout
    assert status_after == status_before
    paths = RuntimePaths.for_repository(repository, environment=environment)
    store = TransactionStore(
        paths.transactions_root,
        evidence_root=paths.evidence_root,
    )
    assert store.load_record(record.transaction_id).state is TransactionState.VERIFIED
    trace = await _opened_trace(paths)
    try:
        journal = SideEffectJournal(trace)
        assert (
            journal.unknown_for_transaction(transaction_id=record.transaction_id) == ()
        )
        facts = [
            event.event.payload
            for event in trace.events()
            if event.event.event_type == "side_effect.checkpoint"
            and event.event.payload.get("operation") == "apply"
        ]
        assert [fact["status"] for fact in facts] == ["STARTED", "FAILED"]
        assert facts[-1]["error_type"] == "TransactionError"
        assert facts[0]["originating_run_id"] == facts[1]["originating_run_id"]
    finally:
        await trace.close()


async def _opened_trace(paths: RuntimePaths) -> TraceStore:
    trace = TraceStore(paths)
    await trace.start()
    return trace
