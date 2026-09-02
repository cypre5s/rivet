from __future__ import annotations

import subprocess
import sys

from rivet.contracts.guard import PermissionScope
from rivet.tools.catalog import TOOL_CATALOG, catalog_for_mode


def test_tool_catalog_is_exactly_the_nine_snake_case_tools() -> None:
    expected_names = (
        "workspace_info",
        "context_search",
        "file_read",
        "file_write",
        "file_replace",
        "file_create",
        "file_delete",
        "process_run",
        "git_diff",
    )
    assert tuple(spec.name for spec in TOOL_CATALOG) == expected_names
    assert tuple(spec.executor for spec in TOOL_CATALOG) == expected_names
    assert len({spec.name for spec in TOOL_CATALOG}) == 9
    assert len({spec.executor for spec in TOOL_CATALOG}) == 9
    assert all("." not in spec.name and len(spec.name) <= 64 for spec in TOOL_CATALOG)


def test_catalog_declares_exact_capabilities_and_modes() -> None:
    by_name = {spec.name: spec for spec in TOOL_CATALOG}
    assert by_name["workspace_info"].required_capabilities == ()
    assert by_name["context_search"].required_capabilities == (
        "context.search.lexical",
    )
    for name in ("file_write", "file_replace", "file_create", "file_delete"):
        assert by_name[name].required_capabilities == (
            "transaction.worktree",
            "guard.local_execution",
        )
    assert tuple(spec.name for spec in catalog_for_mode("ask")) == (
        "workspace_info",
        "context_search",
        "file_read",
    )
    assert all(
        (spec.path_argument is not None)
        == (spec.permission_scope is PermissionScope.SPECIFIC_PATHS)
        for spec in TOOL_CATALOG
    )
    assert by_name["git_diff"].path_argument is None


def test_importing_catalog_does_not_import_runtime_implementations() -> None:
    script = """
import sys
import rivet.tools.catalog
forbidden = (
    'rivet.context.lexical',
    'rivet.guard.sandbox',
    'rivet.providers.deepseek',
    'rivet.tools.process',
    'rivet.transaction.manager',
)
raise SystemExit(1 if any(name in sys.modules for name in forbidden) else 0)
"""
    completed = subprocess.run(
        (sys.executable, "-c", script),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
