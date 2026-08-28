"""测量空 Kernel RSS 与帮助入口的惰性导入边界。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import cast

PROBE_PREFIX = "RIVET_KERNEL_PROBE="
SAFE_ENVIRONMENT_NAMES = frozenset(
    {"LANG", "LC_ALL", "PATH", "PYTHONPATH", "TERM", "VIRTUAL_ENV"}
)
FORBIDDEN_STARTUP_MODULES = (
    "httpx",
    "tree_sitter",
    "markitdown",
    "PIL",
    "pytesseract",
    "whisper",
)


def _forbidden_loaded_modules() -> list[str]:
    """列出被帮助入口或空 Kernel 意外加载的重型模块。"""
    return sorted(
        module_name
        for module_name in sys.modules
        if any(
            module_name == prefix or module_name.startswith(f"{prefix}.")
            for prefix in FORBIDDEN_STARTUP_MODULES
        )
    )


async def _probe_empty_kernel() -> dict[str, object]:
    """在独立进程中启动并关闭无模块 Kernel。"""
    from rivet.kernel.application import RivetKernel

    with tempfile.TemporaryDirectory(prefix="rivet-kernel-probe-") as directory:
        kernel = RivetKernel.from_manifests(
            (),
            journal_path=Path(directory) / "activation-journal.json",
            safe_mode=True,
        )
        await kernel.start()
        await kernel.shutdown()
        resource_count = kernel.runtime.resource_counts().resource_count
    return {
        "peak_rss_mib": round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            3,
        ),
        "resource_count": resource_count,
        "forbidden_loaded_modules": _forbidden_loaded_modules(),
    }


def _probe_help() -> dict[str, object]:
    """执行正式帮助入口并检查禁止的偷跑导入。"""
    import rivet

    try:
        rivet.main(["--help"])
    except SystemExit as error:
        if error.code != 0:
            raise RuntimeError("rivet --help 返回非零状态") from error
    return {"forbidden_loaded_modules": _forbidden_loaded_modules()}


def _safe_environment() -> dict[str, str]:
    """为测量进程传递最小环境并排除所有凭据。"""
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in SAFE_ENVIRONMENT_NAMES
    }
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _run_probe(probe_name: str) -> dict[str, object]:
    """运行独立探针并解析带固定前缀的单行 JSON。"""
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", probe_name],
        check=False,
        capture_output=True,
        text=True,
        env=_safe_environment(),
        timeout=10,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Kernel {probe_name} 探针失败")
    for line in completed.stdout.splitlines():
        if not line.startswith(PROBE_PREFIX):
            continue
        raw_payload: object = json.loads(line.removeprefix(PROBE_PREFIX))
        if isinstance(raw_payload, dict):
            return cast(dict[str, object], raw_payload)
    raise RuntimeError(f"Kernel {probe_name} 探针未返回结构化结果")


def _measure_manifest_loading() -> dict[str, object]:
    """测量重复加载 100 个静态 Manifest 的 p95。"""
    from rivet.kernel.manifests import ManifestLoader

    durations_ms: list[float] = []
    with tempfile.TemporaryDirectory(prefix="rivet-manifest-probe-") as directory:
        manifest_directory = Path(directory)
        for index in range(100):
            (manifest_directory / f"module-{index:03d}.toml").write_text(
                "\n".join(
                    (
                        f'module_id = "probe.module_{index}"',
                        'module_version = "1.0.0"',
                        'activation = "on_demand"',
                        'factory = "rivet.modules.probe:create_module"',
                        f'provides = ["probe.capability_{index}"]',
                        "requires = []",
                        "idle_timeout_seconds = 300",
                    )
                ),
                encoding="utf-8",
            )
        loader = ManifestLoader()
        loaded_count = 0
        for _ in range(20):
            started_at = time.perf_counter()
            loaded_count = len(loader.load_directory(manifest_directory))
            durations_ms.append((time.perf_counter() - started_at) * 1_000)
        if loaded_count != 100:
            raise RuntimeError("Manifest 探针未加载完整样本")
    return {
        "manifest_count": 100,
        "sample_count": 20,
        "p95_ms": round(sorted(durations_ms)[18], 3),
        "samples_ms": [round(duration, 3) for duration in durations_ms],
    }


def collect_kernel_baseline() -> dict[str, object]:
    """采集 Phase 2 可重复的空 Kernel 性能与导入证据。"""
    return {
        "schema_version": 1,
        "empty_kernel": _run_probe("empty-kernel"),
        "help_entrypoint": _run_probe("help"),
        "manifest_loading": _measure_manifest_loading(),
    }


def _build_parser() -> argparse.ArgumentParser:
    """构造公开输出与内部探针参数。"""
    parser = argparse.ArgumentParser(description="测量空 Rivet Kernel")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--probe", choices=("empty-kernel", "help"), help=argparse.SUPPRESS
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """运行探针或输出不含机器凭据的性能 JSON。"""
    arguments = _build_parser().parse_args(argv)
    probe_name = cast(str | None, arguments.probe)
    if probe_name == "empty-kernel":
        payload = asyncio.run(_probe_empty_kernel())
        print(f"{PROBE_PREFIX}{json.dumps(payload, sort_keys=True)}")
        return 0
    if probe_name == "help":
        payload = _probe_help()
        print(f"{PROBE_PREFIX}{json.dumps(payload, sort_keys=True)}")
        return 0

    output_path = cast(Path | None, arguments.output)
    serialized = json.dumps(
        collect_kernel_baseline(), ensure_ascii=False, indent=2, sort_keys=True
    )
    if output_path is None:
        print(serialized)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(f"{serialized}\n", encoding="utf-8")
        print(f"Kernel 基线已写入 {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
