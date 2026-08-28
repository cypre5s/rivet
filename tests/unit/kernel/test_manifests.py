"""验证 Manifest 静态加载、顺序与性能。"""

from __future__ import annotations

import sys
from pathlib import Path
from time import perf_counter

import pytest

from rivet.kernel.errors import ManifestError
from rivet.kernel.manifests import ManifestLoader

FACTORY_MODULE = "tests.fixtures.kernel.fake_modules"


def _write_manifest(path: Path, index: int, *, enabled: bool = True) -> None:
    path.write_text(
        "\n".join(
            (
                f'module_id = "test.module_{index}"',
                'module_version = "1.0.0"',
                'activation = "on_demand"',
                f'factory = "{FACTORY_MODULE}:create_recording_module"',
                f"enabled = {str(enabled).lower()}",
                f'provides = ["test.capability_{index}"]',
                "requires = []",
                "idle_timeout_seconds = 300",
            )
        ),
        encoding="utf-8",
    )


def test_loader_does_not_import_factory(tmp_path: Path) -> None:
    sys.modules.pop(FACTORY_MODULE, None)
    manifest_path = tmp_path / "module.toml"
    _write_manifest(manifest_path, 1)

    manifests = ManifestLoader().load_paths((manifest_path,))

    assert manifests[0].module_id == "test.module_1"
    assert FACTORY_MODULE not in sys.modules


def test_loader_rejects_unknown_field(tmp_path: Path) -> None:
    manifest_path = tmp_path / "invalid.toml"
    _write_manifest(manifest_path, 1)
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8") + "\nunknown = true\n",
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="invalid.toml"):
        ManifestLoader().load_paths((manifest_path,))


def test_loader_rejects_missing_directory_non_toml_and_duplicate_id(
    tmp_path: Path,
) -> None:
    loader = ManifestLoader()
    with pytest.raises(ManifestError, match="目录不存在"):
        loader.load_directory(tmp_path / "missing")

    text_path = tmp_path / "manifest.txt"
    text_path.write_text("not toml", encoding="utf-8")
    with pytest.raises(ManifestError, match="TOML"):
        loader.load_paths((text_path,))

    first = tmp_path / "first.toml"
    second = tmp_path / "second.toml"
    _write_manifest(first, 1)
    _write_manifest(second, 1)
    with pytest.raises(ManifestError, match="重复声明"):
        loader.load_paths((first, second))


def test_loader_returns_stable_path_order(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "z.toml", 2)
    _write_manifest(tmp_path / "a.toml", 1)

    manifests = ManifestLoader().load_directory(tmp_path)

    assert [manifest.module_id for manifest in manifests] == [
        "test.module_1",
        "test.module_2",
    ]


def test_loading_one_hundred_manifests_p95_within_thirty_milliseconds(
    tmp_path: Path,
) -> None:
    for index in range(100):
        _write_manifest(tmp_path / f"module-{index:03d}.toml", index)
    loader = ManifestLoader()
    durations: list[float] = []
    manifests = ()

    for _ in range(20):
        started_at = perf_counter()
        manifests = loader.load_directory(tmp_path)
        durations.append((perf_counter() - started_at) * 1_000)

    percentile_95 = sorted(durations)[18]
    assert len(manifests) == 100
    assert percentile_95 <= 30
