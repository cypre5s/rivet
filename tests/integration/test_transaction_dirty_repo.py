"""验证脏仓库拒绝和非破坏性 tracked/untracked 快照。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.contracts.transactions import TransactionState
from rivet.kernel.resources import ResourceScope
from rivet.tools.files import TransactionFileWriter
from rivet.transaction.errors import TransactionError
from rivet.transaction.models import DirtyPolicy
from tests.transaction_helpers import (
    acceptance_spec,
    initialize_repository,
    make_manager,
    passed_verdict,
    run_git,
    worktree_digest,
)


@pytest.mark.asyncio
async def test_dirty_repository_is_rejected_without_changing_status(
    tmp_path: Path,
) -> None:
    repository = initialize_repository(tmp_path)
    (repository / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    status_before = run_git(repository, "status", "--porcelain=v1", "-z")
    scope = ResourceScope("transaction.dirty.reject")
    manager = make_manager(repository, tmp_path, scope)

    with pytest.raises(TransactionError, match="脏工作区"):
        await manager.create(transaction_id="tx_rejected")

    assert run_git(repository, "status", "--porcelain=v1", "-z") == status_before
    assert not (tmp_path / "cache" / "rivet" / "worktrees").exists()
    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_dirty_snapshot_preserves_index_worktree_and_untracked_files(
    tmp_path: Path,
) -> None:
    repository = initialize_repository(tmp_path)
    (repository / "tracked.txt").write_text("staged\n", encoding="utf-8")
    run_git(repository, "add", "--", "tracked.txt")
    (repository / "tracked.txt").write_text("unstaged after staged\n", encoding="utf-8")
    (repository / "未跟踪.txt").write_text("untracked baseline\n", encoding="utf-8")
    status_before = run_git(repository, "status", "--porcelain=v1", "-z")
    digest_before = worktree_digest(repository)
    scope = ResourceScope("transaction.dirty.snapshot")
    manager = make_manager(repository, tmp_path, scope)

    record = await manager.create(
        transaction_id="tx_dirty",
        dirty_policy=DirtyPolicy.SNAPSHOT,
    )
    worktree = manager.worktree_path(record.transaction_id)

    assert record.dirty is True
    assert record.dirty_snapshot_hash is not None
    assert record.base_commit == record.dirty_snapshot_hash
    assert (worktree / "tracked.txt").read_text(encoding="utf-8") == (
        "unstaged after staged\n"
    )
    assert (worktree / "未跟踪.txt").read_text(encoding="utf-8") == (
        "untracked baseline\n"
    )
    assert run_git(repository, "status", "--porcelain=v1", "-z") == status_before
    assert worktree_digest(repository) == digest_before

    aborted = await manager.abort(record.transaction_id)
    aborted_again = await manager.abort(record.transaction_id)

    assert aborted.state is TransactionState.ABORTED
    assert aborted_again == aborted
    assert run_git(repository, "status", "--porcelain=v1", "-z") == status_before
    assert worktree_digest(repository) == digest_before
    assert not worktree.exists()
    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_dirty_snapshot_patch_applies_on_top_of_unchanged_dirty_state(
    tmp_path: Path,
) -> None:
    repository = initialize_repository(tmp_path)
    (repository / "tracked.txt").write_text("dirty baseline\n", encoding="utf-8")
    (repository / "未跟踪.txt").write_text("keep me\n", encoding="utf-8")
    scope = ResourceScope("transaction.dirty.apply")
    manager = make_manager(repository, tmp_path, scope)
    record = await manager.create(
        transaction_id="tx_dirty_apply",
        dirty_policy=DirtyPolicy.SNAPSHOT,
    )
    await manager.freeze_acceptance(
        record.transaction_id,
        acceptance_spec(),
        confirmed=True,
    )
    writer = TransactionFileWriter(manager.transaction_boundary(record.transaction_id))
    writer.write("tracked.txt", "dirty and patched\n")
    await manager.record_patch_set(
        record.transaction_id,
        patch_id="patch_dirty_apply",
    )
    verifying = await manager.begin_verification(record.transaction_id)
    await manager.record_verdict(passed_verdict(verifying, manager))

    applied = await manager.apply(record.transaction_id)

    assert applied.state is TransactionState.APPLIED
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == (
        "dirty and patched\n"
    )
    assert (repository / "未跟踪.txt").read_text(encoding="utf-8") == "keep me\n"
    scope.assert_empty()
    await scope.close()
