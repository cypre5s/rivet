"""验证主仓库漂移和补丁漂移均在 apply 前失败关闭。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.contracts.transactions import TransactionRecord, TransactionState
from rivet.kernel.resources import ResourceScope
from rivet.tools.files import TransactionFileWriter
from rivet.transaction.errors import TransactionError
from rivet.transaction.manager import TransactionManager
from rivet.transaction.store import TransactionStore
from tests.transaction_helpers import (
    acceptance_spec,
    initialize_repository,
    make_manager,
    passed_verdict,
    run_git,
)


async def _verified_transaction(
    tmp_path: Path,
    *,
    transaction_id: str,
) -> tuple[Path, ResourceScope, TransactionManager]:
    """创建修改两个文件且已接收 Verdict 的事务。"""
    repository = initialize_repository(tmp_path)
    scope = ResourceScope(f"transaction.conflict.{transaction_id}")
    manager = make_manager(repository, tmp_path, scope)
    record = await manager.create(
        acceptance_spec(
            acceptance_id=f"acceptance_{transaction_id.removeprefix('tx_')}"
        ),
        confirmed=True,
        transaction_id=transaction_id,
    )
    writer = TransactionFileWriter(manager.transaction_boundary(record.transaction_id))
    writer.write("tracked.txt", "transaction tracked\n")
    writer.write("second.txt", "transaction second\n")
    await manager.record_patch_set(
        record.transaction_id,
        patch_id=f"patch_{transaction_id.removeprefix('tx_')}",
    )
    verifying = await manager.begin_verification(record.transaction_id)
    await manager.record_verdict(passed_verdict(verifying, manager))
    return repository, scope, manager


@pytest.mark.asyncio
async def test_main_repository_drift_rejects_without_partial_apply(
    tmp_path: Path,
) -> None:
    repository, scope, raw_manager = await _verified_transaction(
        tmp_path,
        transaction_id="tx_main_drift",
    )
    manager = raw_manager
    (repository / "tracked.txt").write_text("external drift\n", encoding="utf-8")

    with pytest.raises(TransactionError, match="漂移"):
        await manager.apply("tx_main_drift")

    assert (repository / "tracked.txt").read_text(encoding="utf-8") == (
        "external drift\n"
    )
    assert (repository / "second.txt").read_text(encoding="utf-8") == "second base\n"
    assert manager.worktree_path("tx_main_drift").exists()
    await manager.abort("tx_main_drift")
    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_worktree_change_after_verdict_rejects_stale_patch(
    tmp_path: Path,
) -> None:
    _repository, scope, raw_manager = await _verified_transaction(
        tmp_path,
        transaction_id="tx_patch_drift",
    )
    manager = raw_manager
    (manager.worktree_path("tx_patch_drift") / "second.txt").write_text(
        "changed after verdict\n",
        encoding="utf-8",
    )

    with pytest.raises(TransactionError, match="补丁漂移"):
        await manager.apply("tx_patch_drift")

    await manager.abort("tx_patch_drift")
    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_apply_recovers_when_state_write_fails_after_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, scope, manager = await _verified_transaction(
        tmp_path,
        transaction_id="tx_apply_recovery",
    )
    original_save = TransactionStore.save_record
    failed_once = False

    def fail_applied_once(
        store: TransactionStore,
        record: TransactionRecord,
    ) -> None:
        """在补丁已应用后模拟一次原子记录故障。"""
        nonlocal failed_once
        if record.state is TransactionState.APPLIED and not failed_once:
            failed_once = True
            raise OSError("injected state failure")
        original_save(store, record)

    monkeypatch.setattr(TransactionStore, "save_record", fail_applied_once)
    with pytest.raises(OSError, match="injected"):
        await manager.apply("tx_apply_recovery")
    monkeypatch.setattr(TransactionStore, "save_record", original_save)

    store = TransactionStore(tmp_path / "state" / "transactions")
    record = store.load_record("tx_apply_recovery")
    assert record.current_patch_id is not None
    patch, _ = store.load_patch("tx_apply_recovery", record.current_patch_id)
    intent = store.load_apply_intent("tx_apply_recovery")
    assert intent.base_commit == record.base_commit
    assert intent.acceptance_sha256 == record.acceptance_sha256
    assert intent.patch_sha256 == patch.patch_sha256
    assert intent.evidence_manifest_sha256 == record.evidence_manifest_sha256
    worktree = manager.worktree_path("tx_apply_recovery")
    record_before_abort = store.record_path("tx_apply_recovery").read_bytes()

    with pytest.raises(TransactionError) as abort_error:
        await manager.abort("tx_apply_recovery")

    assert abort_error.value.code == "transaction.apply_recovery_required"
    assert store.record_path("tx_apply_recovery").read_bytes() == record_before_abort
    assert store.load_record("tx_apply_recovery").state is TransactionState.VERIFIED
    assert worktree.is_dir()
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == (
        "transaction tracked\n"
    )

    applied = await manager.apply("tx_apply_recovery")

    assert applied.state is TransactionState.APPLIED
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == (
        "transaction tracked\n"
    )
    assert not worktree.exists()
    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_persisted_patch_tampering_blocks_apply(tmp_path: Path) -> None:
    _repository, scope, manager = await _verified_transaction(
        tmp_path,
        transaction_id="tx_patch_tamper",
    )
    patch_path = manager.patch_path("tx_patch_tamper", "patch_patch_tamper")
    patch_path.write_bytes(patch_path.read_bytes() + b"tampered")

    with pytest.raises(TransactionError, match="哈希不匹配"):
        await manager.apply("tx_patch_tamper")

    await manager.abort("tx_patch_tamper")
    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_branch_change_is_detected_before_apply(tmp_path: Path) -> None:
    repository, scope, manager = await _verified_transaction(
        tmp_path,
        transaction_id="tx_branch_drift",
    )
    run_git(repository, "checkout", "-qb", "external-branch")

    with pytest.raises(TransactionError, match="漂移"):
        await manager.apply("tx_branch_drift")

    assert (repository / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    await manager.abort("tx_branch_drift")
    scope.assert_empty()
    await scope.close()
