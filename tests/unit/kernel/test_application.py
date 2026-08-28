"""验证 RivetKernel 只编排模块运行时。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.kernel.application import RivetKernel


@pytest.mark.asyncio
async def test_empty_kernel_starts_and_shuts_down_without_resources(
    tmp_path: Path,
) -> None:
    kernel = RivetKernel.from_manifests(
        (), journal_path=tmp_path / "activation-journal.json", safe_mode=True
    )

    await kernel.start()
    await kernel.shutdown()

    assert kernel.runtime.resource_counts().resource_count == 0
