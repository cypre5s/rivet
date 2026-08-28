"""验证 clean 只删除带有效 ownership marker 的受控资源。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.storage.ownership import OwnershipKind, SafeCleaner, write_ownership


def test_clean_removes_only_owned_temporary_directories(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    owned = cache_root / "owned"
    unowned = cache_root / "unowned"
    persistent = cache_root / "persistent"
    owned.mkdir()
    unowned.mkdir()
    persistent.mkdir()
    (owned / "payload.txt").write_text("temporary", encoding="utf-8")
    (unowned / "payload.txt").write_text("user", encoding="utf-8")
    write_ownership(owned, kind=OwnershipKind.TEMPORARY, resource_id="owned")
    write_ownership(
        persistent,
        kind=OwnershipKind.SESSION,
        resource_id="persistent",
    )

    report = SafeCleaner(cache_root).clean()

    assert report.removed == (owned,)
    assert not owned.exists()
    assert unowned.is_dir()
    assert persistent.is_dir()


def test_clean_dry_run_and_symlink_never_remove_external_data(tmp_path: Path) -> None:
    cache_root = tmp_path / "cache"
    external = tmp_path / "external"
    cache_root.mkdir()
    external.mkdir()
    (external / "user.txt").write_text("keep", encoding="utf-8")
    link = cache_root / "linked"
    link.symlink_to(external, target_is_directory=True)
    owned = cache_root / "owned"
    owned.mkdir()
    write_ownership(owned, kind=OwnershipKind.TEMPORARY, resource_id="owned")

    report = SafeCleaner(cache_root).clean(dry_run=True)

    assert report.candidates == (owned,)
    assert report.removed == ()
    assert owned.is_dir()
    assert link.is_symlink()
    assert (external / "user.txt").read_text(encoding="utf-8") == "keep"


def test_clean_rejects_symlink_root(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    owned = external / "owned"
    owned.mkdir()
    write_ownership(owned, kind=OwnershipKind.TEMPORARY, resource_id="owned")
    link = tmp_path / "cache-link"
    link.symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="clean 根"):
        SafeCleaner(link).clean()

    assert owned.is_dir()
