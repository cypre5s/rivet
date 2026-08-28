"""验证 10,000 事件顺序、队列有界、序列化与关闭性能。"""

from __future__ import annotations

from typing import cast

import pytest

from scripts.measure_trace import measure_trace


@pytest.mark.asyncio
async def test_ten_thousand_events_are_ordered_bounded_and_fast() -> None:
    measurement = await measure_trace()

    assert measurement["event_count"] == 10_000
    assert measurement["first_sequence"] == 1
    assert measurement["last_sequence"] == 10_000
    assert measurement["database_event_count"] == 10_000
    assert cast(int, measurement["queue_peak_size"]) <= cast(
        int, measurement["queue_capacity"]
    )
    assert measurement["pending_event_count"] == 0
    assert cast(float, measurement["serialization_p95_ms"]) <= 2
    assert cast(float, measurement["writer_shutdown_ms"]) <= 1_000
