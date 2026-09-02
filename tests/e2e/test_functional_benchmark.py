"""验证开发期离线功能 benchmark 不绕过隔离与 oracle。"""

from __future__ import annotations

from typing import cast

import pytest

from scripts.functional_benchmark import run_functional_benchmark


@pytest.mark.asyncio
async def test_functional_benchmark_keeps_main_clean_and_rejects_flawed_patch() -> None:
    result = await run_functional_benchmark(run_count=1, task_limit=8)
    metrics = cast(dict[str, object], result["metrics"])

    assert result["passed"] is True
    assert result["task_count"] == 8
    assert result["run_count"] == 8
    assert metrics["false_allow_count"] == 0
    assert metrics["gold_file_recall_at_10"] == 1.0
    assert metrics["main_worktree_pollution_count"] == 0
    assert metrics["resource_leak_count"] == 0
    runs = cast(list[dict[str, object]], result["runs"])
    flawed = [run for run in runs if run["proposal_label"] == "flawed"]
    assert flawed
    assert all(run["accepted"] is False for run in flawed)
