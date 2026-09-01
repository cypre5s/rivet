"""验证 RivetKernel 只编排模块运行时。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.kernel.application import RivetKernel


def _manifest(path: Path) -> None:
    """写入一个可由薄 Kernel 加载的 Manifest。"""
    path.write_text(
        "\n".join(
            (
                'module_id = "test.application"',
                'module_version = "1.0.0"',
                'activation = "on_demand"',
                'factory = "tests.fixtures.kernel.fake_modules:create_recording_module"',
                'provides = ["test.application.resolve"]',
                "requires = []",
            )
        ),
        encoding="utf-8",
    )


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


@pytest.mark.asyncio
async def test_kernel_loads_paths_resolves_and_leases(tmp_path: Path) -> None:
    manifest_path = tmp_path / "module.toml"
    _manifest(manifest_path)
    kernel = RivetKernel.from_manifest_paths(
        (manifest_path,),
        journal_path=tmp_path / "journal.json",
    )

    await kernel.start()
    assert await kernel.resolve("test.application.resolve")
    lease = await kernel.acquire("test.application.resolve")
    await lease.release()
    await kernel.shutdown()

    assert kernel.runtime.resource_counts().resource_count == 0
