"""验证五类 Phase 14 故障均失败关闭且不泄漏资源。"""

from __future__ import annotations

import pytest

from scripts.fault_benchmark import run_fault_benchmark


@pytest.mark.asyncio
async def test_fault_injection_matrix_passes_and_cleans_resources() -> None:
    result = await run_fault_benchmark()

    assert result["passed"] is True
    assert result["case_count"] == 5
    assert result["passed_count"] == 5
    assert result["resource_leak_count"] == 0
