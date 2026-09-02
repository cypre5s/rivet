"""验证丢失的事务 Worktree 只能从冻结基线与持久 Patch 重建。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from rivet.contracts.transactions import PatchSet, TransactionRecord, TransactionState
from rivet.kernel.resources import ResourceScope
from rivet.tools.files import TransactionFileWriter
from rivet.transaction.errors import TransactionError
from rivet.transaction.git_backend import GitBackend
from tests.transaction_helpers import (
    acceptance_spec,
    initialize_repository,
    make_manager,
    passed_verdict,
    run_git,
)


async def _persist_candidate_then_delete_cache(
    tmp_path: Path,
    *,
    state: TransactionState,
) -> tuple[Path, Path, TransactionRecord, PatchSet]:
    """建立指定非终态事务、移交资源并模拟 cache Worktree 丢失。"""
    repository = initialize_repository(tmp_path)
    original_scope = ResourceScope(f"transaction.rebuild.original.{state.value}")
    original = make_manager(repository, tmp_path, original_scope)
    transaction_id = f"tx_rebuild_{state.value.lower()}"
    record = await original.create(
        acceptance_spec(acceptance_id=f"acceptance_{transaction_id}"),
        confirmed=True,
        transaction_id=transaction_id,
    )
    worktree = original.worktree_path(transaction_id)
    writer = TransactionFileWriter(original.transaction_boundary(transaction_id))
    writer.write("tracked.txt", "rebuilt candidate\n")
    writer.create("新增.txt", "rebuilt new path\n")
    (worktree / "binary.bin").write_bytes(b"\x00REBUILT\xfe")
    patch = await original.record_patch_set(
        transaction_id,
        patch_id=f"patch_rebuild_{state.value.lower()}",
    )
    if state in {TransactionState.VERIFYING, TransactionState.VERIFIED}:
        record = await original.begin_verification(transaction_id)
    else:
        record = original.store().load_record(transaction_id)
    if state is TransactionState.VERIFIED:
        record = await original.record_verdict(passed_verdict(record, original))

    assert record.state is state
    original.suspend(transaction_id)
    original_scope.assert_empty()
    await original_scope.close()

    # 模拟 cache 被外部清理：目录消失，但 Git common-dir 仍保留 prunable 登记。
    shutil.rmtree(worktree)
    assert not worktree.exists()
    assert str(worktree) in run_git(repository, "worktree", "list", "--porcelain")
    return repository, worktree, record, patch


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    (TransactionState.PATCHING, TransactionState.VERIFYING),
)
async def test_missing_candidate_is_rebuilt_before_verification(
    tmp_path: Path,
    state: TransactionState,
) -> None:
    """PATCHING/VERIFYING 都恢复同一候选内容后才允许读取验证上下文。"""
    (
        _repository,
        worktree,
        persisted,
        patch,
    ) = await _persist_candidate_then_delete_cache(tmp_path, state=state)
    recovery_scope = ResourceScope(f"transaction.rebuild.recovery.{state.value}")
    recovery = make_manager(_repository, tmp_path, recovery_scope)

    recovered = await recovery.recover(persisted.transaction_id)

    assert recovered.state is state
    assert (worktree / "tracked.txt").read_text(encoding="utf-8") == (
        "rebuilt candidate\n"
    )
    assert (worktree / "新增.txt").read_text(encoding="utf-8") == ("rebuilt new path\n")
    assert (worktree / "binary.bin").read_bytes() == b"\x00REBUILT\xfe"
    verifying = (
        await recovery.begin_verification(persisted.transaction_id)
        if state is TransactionState.PATCHING
        else recovered
    )
    context = await recovery.verification_context(persisted.transaction_id)
    assert verifying.state is TransactionState.VERIFYING
    assert context.patch == patch
    assert (
        context.patch_path.read_bytes()
        == recovery.patch_path(
            persisted.transaction_id,
            patch.patch_id,
        ).read_bytes()
    )

    await recovery.abort(persisted.transaction_id)
    recovery_scope.assert_empty()
    await recovery_scope.close()


@pytest.mark.asyncio
async def test_verified_candidate_is_rebuilt_then_explicitly_applied(
    tmp_path: Path,
) -> None:
    """VERIFIED 丢失 cache 后仍须 recover，且只有显式 apply 才改主仓库。"""
    (
        repository,
        worktree,
        persisted,
        _patch,
    ) = await _persist_candidate_then_delete_cache(
        tmp_path,
        state=TransactionState.VERIFIED,
    )
    recovery_scope = ResourceScope("transaction.rebuild.recovery.verified")
    recovery = make_manager(repository, tmp_path, recovery_scope)

    recovered = await recovery.recover(persisted.transaction_id)

    assert recovered.state is TransactionState.VERIFIED
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    assert not (repository / "新增.txt").exists()
    assert (worktree / "tracked.txt").read_text(encoding="utf-8") == (
        "rebuilt candidate\n"
    )

    applied = await recovery.apply(persisted.transaction_id)

    assert applied.state is TransactionState.APPLIED
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == (
        "rebuilt candidate\n"
    )
    assert (repository / "新增.txt").read_text(encoding="utf-8") == (
        "rebuilt new path\n"
    )
    assert (repository / "binary.bin").read_bytes() == b"\x00REBUILT\xfe"
    assert not worktree.exists()
    recovery_scope.assert_empty()
    await recovery_scope.close()


@pytest.mark.asyncio
async def test_rebuild_hash_mismatch_cleans_new_worktree_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """应用后 binary diff 不等于持久 Patch 时不得留下可用候选目录。"""
    (
        repository,
        worktree,
        persisted,
        _patch,
    ) = await _persist_candidate_then_delete_cache(
        tmp_path,
        state=TransactionState.PATCHING,
    )
    original_binary_diff = GitBackend.binary_diff

    async def mismatched_binary_diff(
        backend: GitBackend,
        candidate: Path,
        base_commit: str,
        *,
        paths: tuple[str, ...] = (),
    ) -> bytes:
        content = await original_binary_diff(
            backend,
            candidate,
            base_commit,
            paths=paths,
        )
        return content + b"injected rebuild drift\n"

    monkeypatch.setattr(GitBackend, "binary_diff", mismatched_binary_diff)
    recovery_scope = ResourceScope("transaction.rebuild.hash-mismatch")
    recovery = make_manager(repository, tmp_path, recovery_scope)

    with pytest.raises(TransactionError, match="持久化 PatchSet") as caught:
        await recovery.recover(persisted.transaction_id)

    assert caught.value.code == "transaction.worktree_rebuild_patch_mismatch"
    assert not worktree.exists()
    assert str(worktree) not in run_git(repository, "worktree", "list", "--porcelain")
    assert recovery.store().load_record(persisted.transaction_id).state is (
        TransactionState.PATCHING
    )
    recovery_scope.assert_empty()
    await recovery_scope.close()
