"""验证运行目录策略、SQLite pragma 与幂等 migration。"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from rivet.trace.database import TraceDatabase
from rivet.trace.errors import RuntimePathError
from rivet.trace.paths import RuntimePaths


def test_runtime_paths_prepare_repository_and_xdg_directories(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    cache_root = tmp_path / "xdg-cache"

    paths = RuntimePaths.for_repository(
        repository,
        environment={"XDG_CACHE_HOME": str(cache_root)},
    )
    paths.prepare()

    assert paths.runtime_root == repository / ".rivet"
    assert paths.cache_root == cache_root / "rivet"
    assert paths.database_path.parent.is_dir()
    assert paths.events_path.parent.is_dir()
    assert paths.artifacts_root.is_dir()


def test_runtime_paths_reject_relative_xdg_cache(tmp_path: Path) -> None:
    with pytest.raises(RuntimePathError, match="XDG_CACHE_HOME"):
        RuntimePaths.for_repository(
            tmp_path,
            environment={"XDG_CACHE_HOME": "relative-cache"},
        )


def test_database_migration_is_repeatable_and_enables_required_pragmas(
    tmp_path: Path,
) -> None:
    database = TraceDatabase(tmp_path / "state.sqlite3")

    database.open()
    first_version = database.schema_version()
    database.migrate()
    second_version = database.schema_version()

    assert first_version == second_version == 1
    assert cast(str, database.pragma("journal_mode")).lower() == "wal"
    assert database.pragma("foreign_keys") == 1
    assert database.pragma("busy_timeout") == 5_000
    assert database.migration_count() == 1
    assert {
        "sessions",
        "runs",
        "events",
        "module_states",
        "transactions",
        "run_metrics",
        "schema_migrations",
    } <= database.table_names()
    database.close()
