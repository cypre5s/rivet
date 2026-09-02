"""验证任意脏仓库都被 clean-only 事务失败关闭。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rivet.kernel.resources import ResourceScope
from rivet.transaction.errors import TransactionError
from tests.transaction_helpers import (
    acceptance_spec,
    initialize_repository,
    make_manager,
    run_git,
    worktree_digest,
)


def _dirty_status(repository: Path) -> str:
    """返回不会被 submodule ignore 配置隐藏的完整工作区状态。"""
    return run_git(
        repository,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )


async def _assert_rejected_before_transaction_persistence(
    repository: Path,
    root: Path,
    *,
    dirty_kind: str,
) -> None:
    """证明脏状态被原样保留，且 Acceptance 与 Worktree 从未创建。"""
    status_before = _dirty_status(repository)
    head_before = run_git(repository, "rev-parse", "HEAD")
    worktree_list_before = run_git(repository, "worktree", "list", "--porcelain")
    content_before = worktree_digest(repository)
    assert status_before

    transaction_id = f"tx_rejected_{dirty_kind}"
    scope = ResourceScope(f"transaction.dirty.reject.{dirty_kind}")
    manager = make_manager(repository, root, scope)
    try:
        with pytest.raises(TransactionError, match="脏工作区"):
            await manager.create(
                acceptance_spec(
                    acceptance_id=f"acceptance_dirty_{dirty_kind}",
                ),
                confirmed=True,
                transaction_id=transaction_id,
            )

        assert _dirty_status(repository) == status_before
        assert run_git(repository, "rev-parse", "HEAD") == head_before
        assert (
            run_git(repository, "worktree", "list", "--porcelain")
            == worktree_list_before
        )
        assert worktree_digest(repository) == content_before
        transaction_root = root / "state" / "transactions" / transaction_id
        assert not transaction_root.exists()
        assert not (transaction_root / "acceptance_spec.json").exists()
        assert not (root / "state" / "transactions").exists()
        assert not (root / "state" / "evidence").exists()
        assert not (root / "cache" / "rivet" / "worktrees").exists()
        scope.assert_empty()
    finally:
        await scope.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("dirty_kind", ("staged", "unstaged", "untracked"))
async def test_dirty_repository_is_rejected_without_changing_status(
    tmp_path: Path,
    dirty_kind: str,
) -> None:
    repository = initialize_repository(tmp_path)
    if dirty_kind == "untracked":
        (repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    else:
        (repository / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        if dirty_kind == "staged":
            run_git(repository, "add", "--", "tracked.txt")
    await _assert_rejected_before_transaction_persistence(
        repository,
        tmp_path,
        dirty_kind=dirty_kind,
    )


@pytest.mark.asyncio
async def test_renamed_path_is_rejected_before_acceptance_or_worktree(
    tmp_path: Path,
) -> None:
    repository = initialize_repository(tmp_path)
    run_git(repository, "mv", "--", "tracked.txt", "renamed.txt")

    status = _dirty_status(repository)
    assert "renamed.txt" in status
    assert "tracked.txt" in status
    await _assert_rejected_before_transaction_persistence(
        repository,
        tmp_path,
        dirty_kind="rename",
    )


@pytest.mark.asyncio
async def test_unmerged_conflict_is_rejected_before_acceptance_or_worktree(
    tmp_path: Path,
) -> None:
    repository = initialize_repository(tmp_path)
    run_git(repository, "checkout", "-qb", "conflicting-side")
    (repository / "tracked.txt").write_text("side change\n", encoding="utf-8")
    run_git(repository, "add", "--", "tracked.txt")
    run_git(repository, "commit", "-qm", "side change")
    run_git(repository, "checkout", "-q", "main")
    (repository / "tracked.txt").write_text("main change\n", encoding="utf-8")
    run_git(repository, "add", "--", "tracked.txt")
    run_git(repository, "commit", "-qm", "main change")

    with pytest.raises(subprocess.CalledProcessError):
        run_git(repository, "merge", "--no-edit", "conflicting-side")
    assert _dirty_status(repository).startswith("UU tracked.txt")

    await _assert_rejected_before_transaction_persistence(
        repository,
        tmp_path,
        dirty_kind="conflict",
    )


@pytest.mark.asyncio
async def test_dirty_submodule_is_rejected_even_when_repository_config_ignores_it(
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
    run_git(repository, "config", "submodule.vendor/child.ignore", "all")
    (repository / "vendor" / "child" / "tracked.txt").write_text(
        "dirty submodule\n",
        encoding="utf-8",
    )

    assert run_git(repository, "status", "--porcelain=v1", "-z") == ""
    assert "vendor/child" in _dirty_status(repository)
    await _assert_rejected_before_transaction_persistence(
        repository,
        tmp_path,
        dirty_kind="submodule",
    )
