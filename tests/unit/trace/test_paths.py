"""验证运行事实只进入按仓库隔离的 XDG 目录。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.trace.errors import RuntimePathError
from rivet.trace.paths import RuntimePaths


def test_runtime_paths_prepare_creates_only_demanded_trace_directory(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    cache_root = tmp_path / "xdg-cache"
    state_root = tmp_path / "xdg-state"

    paths = RuntimePaths.for_repository(
        repository,
        environment={
            "XDG_CACHE_HOME": str(cache_root),
            "XDG_STATE_HOME": str(state_root),
        },
    )
    paths.prepare()

    assert paths.runtime_root == state_root / "rivet" / paths.repository_id
    assert paths.events_path == paths.runtime_root / "trace" / "events.ndjson"
    assert paths.transactions_root == paths.runtime_root / "transactions"
    assert paths.evidence_root == paths.runtime_root / "evidence"
    assert paths.cache_root == cache_root / "rivet"
    assert paths.worktrees_root == cache_root / "rivet" / "worktrees"
    assert paths.events_path.parent.is_dir()
    assert not paths.transactions_root.exists()
    assert not paths.evidence_root.exists()
    assert not paths.cache_root.exists()
    assert not paths.worktrees_root.exists()
    assert not (repository / ".rivet").exists()
    assert not any(
        path.suffix in {".sqlite", ".sqlite3", ".db"}
        for path in paths.runtime_root.rglob("*")
    )


def test_runtime_paths_reject_relative_xdg_roots(tmp_path: Path) -> None:
    with pytest.raises(RuntimePathError, match="XDG_CACHE_HOME"):
        RuntimePaths.for_repository(
            tmp_path,
            environment={"XDG_CACHE_HOME": "relative-cache"},
        )
    with pytest.raises(RuntimePathError, match="XDG_STATE_HOME"):
        RuntimePaths.for_repository(
            tmp_path,
            environment={"XDG_STATE_HOME": "relative-state"},
        )


def test_runtime_paths_reject_state_nested_in_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    with pytest.raises(RuntimePathError, match="仓库之外"):
        RuntimePaths.for_repository(
            repository,
            environment={"XDG_STATE_HOME": str(repository / ".state")},
        )
