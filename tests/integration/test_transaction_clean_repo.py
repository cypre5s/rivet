"""验证干净、detached 和 submodule 仓库的隔离事务闭环。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rivet.contracts.transactions import TransactionState
from rivet.kernel.resources import ResourceScope
from rivet.tools.files import TransactionFileWriter
from rivet.transaction.errors import TransactionError
from rivet.transaction.git_backend import GitBackend
from rivet.transaction.manager import TransactionManager
from tests.transaction_helpers import (
    acceptance_spec,
    initialize_repository,
    make_manager,
    passed_verdict,
    run_git,
    worktree_digest,
)


@pytest.mark.asyncio
async def test_clean_repository_patch_apply_isolated_and_idempotent(
    tmp_path: Path,
) -> None:
    repository = initialize_repository(tmp_path)
    scope = ResourceScope("transaction.clean")
    manager = make_manager(repository, tmp_path, scope)
    main_before = worktree_digest(repository)

    snapshot = await manager.inspect_repository()
    record = await manager.create(transaction_id="tx_clean")
    transaction_path = manager.worktree_path(record.transaction_id)

    assert snapshot.repository_root == repository.resolve()
    assert snapshot.branch == "main"
    assert snapshot.dirty is False
    assert record.state is TransactionState.BASELINED
    assert record.base_commit == record.head_commit
    assert transaction_path.is_relative_to((tmp_path / "cache").resolve())
    assert not transaction_path.is_relative_to(repository.resolve())

    specification = acceptance_spec()
    with pytest.raises(TransactionError, match="确认"):
        await manager.freeze_acceptance(
            record.transaction_id,
            specification,
            confirmed=False,
        )
    acceptance_hash = await manager.freeze_acceptance(
        record.transaction_id,
        specification,
        confirmed=True,
    )
    assert (
        await manager.freeze_acceptance(
            record.transaction_id,
            specification,
            confirmed=True,
        )
        == acceptance_hash
    )
    with pytest.raises(TransactionError, match="冻结"):
        await manager.freeze_acceptance(
            record.transaction_id,
            acceptance_spec(expected_behavior="不同的行为"),
            confirmed=True,
        )

    writer = TransactionFileWriter(manager.transaction_boundary(record.transaction_id))
    writer.write("tracked.txt", "patched\n")
    writer.create("新增.txt", "Unicode path\n")
    (transaction_path / "binary.bin").write_bytes(b"\x00PATCHED\xfe")
    patch = await manager.record_patch_set(
        record.transaction_id,
        patch_id="patch_clean",
    )

    assert worktree_digest(repository) == main_before
    assert patch.changed_files == ("binary.bin", "tracked.txt", "新增.txt")
    assert patch.created_files == ("新增.txt",)
    assert patch.contains_binary_diff is True
    assert manager.patch_path(record.transaction_id, patch.patch_id).is_file()

    with pytest.raises(TransactionError, match="VERIFIED"):
        await manager.apply(record.transaction_id)
    verifying = await manager.begin_verification(record.transaction_id)
    verified = await manager.record_verdict(passed_verdict(verifying, manager))
    applied = await manager.apply(record.transaction_id)
    applied_again = await manager.apply(record.transaction_id)

    assert verified.state is TransactionState.VERIFIED
    assert applied.state is TransactionState.APPLIED
    assert applied_again == applied
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == "patched\n"
    assert (repository / "新增.txt").read_text(encoding="utf-8") == "Unicode path\n"
    assert (repository / "binary.bin").read_bytes() == b"\x00PATCHED\xfe"
    assert not transaction_path.exists()
    assert manager.patch_path(record.transaction_id, patch.patch_id).is_file()
    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_detached_head_is_recorded_and_abort_is_idempotent(
    tmp_path: Path,
) -> None:
    repository = initialize_repository(tmp_path, detached=True)
    scope = ResourceScope("transaction.detached")
    manager = make_manager(repository, tmp_path, scope)

    record = await manager.create(transaction_id="tx_detached")
    aborted = await manager.abort(record.transaction_id)
    aborted_again = await manager.abort(record.transaction_id)

    assert record.detached_head is True
    assert record.branch is None
    assert aborted.state is TransactionState.ABORTED
    assert aborted_again == aborted
    assert not manager.worktree_path(record.transaction_id).exists()
    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_repository_with_submodule_records_status_without_initializing_it(
    tmp_path: Path,
) -> None:
    child_root = tmp_path / "child-root"
    child_root.mkdir()
    child = initialize_repository(child_root)
    parent_root = tmp_path / "parent-root"
    parent_root.mkdir()
    repository = initialize_repository(parent_root)
    run_git(
        repository,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(child),
        "vendor/child",
    )
    run_git(repository, "add", "--", ".gitmodules", "vendor/child")
    run_git(repository, "commit", "-qm", "add submodule")
    scope = ResourceScope("transaction.submodule")
    manager = make_manager(repository, parent_root, scope)

    record = await manager.create(transaction_id="tx_submodule")

    assert record.has_submodules is True
    assert record.submodule_status_sha256.startswith("sha256:")
    await manager.abort(record.transaction_id)
    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_frozen_acceptance_tampering_blocks_patch_record(tmp_path: Path) -> None:
    repository = initialize_repository(tmp_path)
    scope = ResourceScope("transaction.acceptance.tamper")
    manager = make_manager(repository, tmp_path, scope)
    record = await manager.create(transaction_id="tx_acceptance_tamper")
    specification = acceptance_spec()
    await manager.freeze_acceptance(
        record.transaction_id,
        specification,
        confirmed=True,
    )
    writer = TransactionFileWriter(manager.transaction_boundary(record.transaction_id))
    writer.write("tracked.txt", "patched\n")
    acceptance_path = (
        tmp_path
        / "state"
        / "transactions"
        / record.transaction_id
        / "acceptance_spec.json"
    )
    tampered = specification.model_dump(mode="json")
    tampered["expected_behaviors"] = ["被篡改的行为"]
    acceptance_path.chmod(0o600)
    acceptance_path.write_text(
        json.dumps(tampered, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(TransactionError, match="哈希不匹配"):
        await manager.record_patch_set(
            record.transaction_id,
            patch_id="patch_acceptance_tamper",
        )

    await manager.abort(record.transaction_id)
    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_default_worktree_uses_xdg_cache_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = initialize_repository(tmp_path)
    xdg_cache_home = tmp_path / "xdg-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_cache_home))
    scope = ResourceScope("transaction.xdg")
    manager = TransactionManager(repository, scope=scope)

    record = await manager.create(transaction_id="tx_xdg")
    worktree = manager.worktree_path(record.transaction_id)

    assert worktree.is_relative_to(xdg_cache_home / "rivet" / "worktrees")
    await manager.abort(record.transaction_id)
    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_partial_worktree_creation_failure_is_fully_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = initialize_repository(tmp_path)
    digest_before = worktree_digest(repository)
    scope = ResourceScope("transaction.create.failure")
    manager = make_manager(repository, tmp_path, scope)

    async def fail_after_directory(
        _backend: GitBackend,
        path: Path,
        _base_commit: str,
    ) -> None:
        """模拟 Git 在建立部分目录后失败。"""
        path.mkdir(parents=True)
        (path / "partial").write_text("partial", encoding="utf-8")
        raise TransactionError(
            "transaction.injected_worktree_failure",
            "注入 Worktree 失败",
        )

    monkeypatch.setattr(GitBackend, "add_worktree", fail_after_directory)

    with pytest.raises(TransactionError, match="注入"):
        await manager.create(transaction_id="tx_partial_failure")

    assert worktree_digest(repository) == digest_before
    assert not tuple((tmp_path / "cache").rglob("tx_partial_failure"))
    assert "tx_partial_failure" not in run_git(
        repository, "worktree", "list", "--porcelain"
    )
    scope.assert_empty()
    await scope.close()
