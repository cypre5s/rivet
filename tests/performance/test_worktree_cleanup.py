"""验证批量事务清理和崩溃后 Worktree 恢复扫描。"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from rivet.contracts.transactions import TransactionState
from rivet.kernel.resources import ResourceScope
from rivet.transaction.store import TransactionStore
from tests.transaction_helpers import (
    acceptance_spec,
    initialize_repository,
    make_manager,
    run_git,
)


@pytest.mark.asyncio
async def test_repeated_abort_leaves_no_worktree_or_resource(tmp_path: Path) -> None:
    repository = initialize_repository(tmp_path)
    scope = ResourceScope("transaction.cleanup.batch")
    manager = make_manager(repository, tmp_path, scope)
    durations: list[float] = []

    for index in range(12):
        started = time.perf_counter()
        record = await manager.create(
            acceptance_spec(acceptance_id=f"acceptance_cleanup_{index}"),
            confirmed=True,
            transaction_id=f"tx_cleanup_{index}",
        )
        await manager.abort(record.transaction_id)
        durations.append(time.perf_counter() - started)

    recovery = await manager.scan_recovery()

    assert recovery.recoverable == ()
    assert recovery.orphans == ()
    assert not tuple((tmp_path / "cache" / "rivet" / "worktrees").rglob("tx_*"))
    assert sorted(durations)[-1] < 2.0
    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_startup_scan_recovers_known_and_cleans_unknown_worktree(
    tmp_path: Path,
) -> None:
    repository = initialize_repository(tmp_path)
    original_scope = ResourceScope("transaction.cleanup.crashed")
    original = make_manager(repository, tmp_path, original_scope)
    record = await original.create(
        acceptance_spec(acceptance_id="acceptance_crashed"),
        confirmed=True,
        transaction_id="tx_crashed",
    )
    worktree = original.worktree_path(record.transaction_id)
    recovery_scope = ResourceScope("transaction.cleanup.recovery")
    recovery_manager = make_manager(repository, tmp_path, recovery_scope)

    first_scan = await recovery_manager.scan_recovery()
    recovered = await recovery_manager.recover(record.transaction_id)

    assert tuple(item.transaction_id for item in first_scan.recoverable) == (
        "tx_crashed",
    )
    assert recovered.transaction_id == record.transaction_id

    shutil.rmtree(tmp_path / "state" / "transactions" / record.transaction_id)
    second_scan = await recovery_manager.scan_recovery()
    assert tuple(item.path for item in second_scan.orphans) == (worktree,)

    cleaned = await recovery_manager.cleanup_orphans()

    assert cleaned == (worktree,)
    assert not worktree.exists()
    await recovery_scope.close()
    await original_scope.close()
    recovery_scope.assert_empty()
    original_scope.assert_empty()


@pytest.mark.asyncio
async def test_scope_close_aborts_and_prunes_active_worktree(tmp_path: Path) -> None:
    repository = initialize_repository(tmp_path)
    scope = ResourceScope("transaction.cleanup.exit")
    manager = make_manager(repository, tmp_path, scope)
    record = await manager.create(
        acceptance_spec(acceptance_id="acceptance_exit"),
        confirmed=True,
        transaction_id="tx_exit",
    )
    worktree = manager.worktree_path(record.transaction_id)

    await scope.close()

    persisted = TransactionStore(tmp_path / "state" / "transactions").load_record(
        record.transaction_id
    )
    assert persisted.state is TransactionState.ABORTED
    assert not worktree.exists()
    assert str(worktree) not in run_git(repository, "worktree", "list", "--porcelain")
    scope.assert_empty()
