"""验证 Phase 0 启动基线的统计边界。"""

import pytest

from scripts.measure_startup import percentile_95


def test_percentile_95_uses_nearest_rank() -> None:
    samples = [float(value) for value in range(1, 21)]

    assert percentile_95(samples) == 19.0


def test_percentile_95_rejects_empty_samples() -> None:
    with pytest.raises(ValueError, match="至少需要一个样本"):
        percentile_95([])
