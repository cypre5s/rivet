"""验证 B0-B4 双次回放达到 Phase 14 硬指标。"""

from __future__ import annotations

from typing import cast

import pytest

from scripts.functional_benchmark import run_functional_benchmark


@pytest.mark.asyncio
async def test_functional_benchmark_runs_twice_and_fails_closed() -> None:
    result = await run_functional_benchmark(run_count=2)
    raw_metrics = result["metrics_by_group"]
    assert isinstance(raw_metrics, dict)
    metrics = cast(dict[str, dict[str, object]], raw_metrics)
    b4 = metrics["B4"]

    assert result["passed"] is True
    assert result["task_count"] == 24
    assert result["run_count"] == 240
    assert b4["task_resolve_rate"] == 0.75
    assert b4["false_allow_rate"] == 0.0
    assert b4["gold_file_recall_at_10"] == 1.0
    assert b4["main_worktree_pollution_count"] == 0
    assert b4["resource_leak_count"] == 0
    assert b4["guard_authorized_count"] == 48
    assert b4["evidence_bundle_count"] == 48
    assert b4["kernel_activation_count"] == 48
    assert b4["kernel_resource_leak_count"] == 0
