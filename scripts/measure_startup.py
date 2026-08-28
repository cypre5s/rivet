"""记录 Phase 0 可获得的启动、内存和偷跑基线。"""

from __future__ import annotations

import json
import math
import os
import resource
import subprocess
import sys
import time
from argparse import ArgumentParser
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

AUDIT_PREFIX = "RIVET_STARTUP_AUDIT="
SAFE_ENVIRONMENT_NAMES = frozenset(
    {"LANG", "LC_ALL", "PATH", "PYTHONPATH", "TERM", "VIRTUAL_ENV"}
)


def percentile_95(samples: Sequence[float]) -> float:
    """用 nearest-rank 方法计算小样本 p95。"""
    if not samples:
        raise ValueError("计算 p95 至少需要一个样本")
    ordered_samples = sorted(samples)
    rank = max(1, math.ceil(0.95 * len(ordered_samples)))
    return ordered_samples[rank - 1]


def _safe_subprocess_environment() -> dict[str, str]:
    """为基准子进程传递最小环境，显式排除凭据。"""
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in SAFE_ENVIRONMENT_NAMES
    }
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _run_checked(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """执行不含凭据的基准子进程并保留可诊断输出。"""
    completed = subprocess.run(
        list(arguments),
        check=False,
        capture_output=True,
        text=True,
        env=_safe_subprocess_environment(),
        timeout=10,
    )
    if completed.returncode != 0:
        raise RuntimeError("启动基准子进程失败")
    return completed


def _collect_audit_counts() -> Mapping[str, int]:
    """运行审计探针并解析不含参数的事件计数。"""
    completed = _run_checked([sys.executable, "scripts/startup_probe.py"])
    for line in completed.stderr.splitlines():
        if line.startswith(AUDIT_PREFIX):
            payload = cast(object, json.loads(line.removeprefix(AUDIT_PREFIX)))
            if not isinstance(payload, dict):
                continue
            typed_payload = cast(dict[object, object], payload)
            if all(
                isinstance(key, str) and isinstance(value, int)
                for key, value in typed_payload.items()
            ):
                return cast(dict[str, int], typed_payload)
    raise RuntimeError("启动审计探针未返回结果")


def _collect_import_count() -> int:
    """统计导入最小 Rivet 包时新增的 Python 模块数。"""
    program = (
        "import sys; before = set(sys.modules); import rivet; "
        "print(len(set(sys.modules) - before))"
    )
    completed = _run_checked([sys.executable, "-c", program])
    return int(completed.stdout.strip())


def collect_startup_baseline(sample_count: int) -> dict[str, object]:
    """采集可在空包阶段真实测量的基线并标记未实现项。"""
    if sample_count < 3:
        raise ValueError("启动基准至少需要 3 个样本")

    durations_ms: list[float] = []
    for _sample_index in range(sample_count):
        started_at = time.perf_counter()
        _run_checked([sys.executable, "-m", "rivet", "--help"])
        durations_ms.append((time.perf_counter() - started_at) * 1000)

    child_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    audit_counts = _collect_audit_counts()
    return {
        "schema_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "sample_count": sample_count,
        "help_cold_start_ms": {
            "samples": [round(duration, 3) for duration in durations_ms],
            "p95": round(percentile_95(durations_ms), 3),
        },
        "headless_peak_rss_mib": round(child_usage.ru_maxrss / 1024, 3),
        "initial_import_count": _collect_import_count(),
        "network_connections_before_first_action": audit_counts.get(
            "network_connections", 0
        ),
        "subprocess_starts_before_first_action": audit_counts.get(
            "subprocess_starts", 0
        ),
        "tui_first_frame_ms": {
            "status": "not_available_phase_0",
            "value": None,
        },
        "module_activation_metrics": {
            "status": "not_available_phase_0",
        },
        "task_usage_metrics": {
            "status": "not_available_phase_0",
        },
    }


def _build_parser() -> ArgumentParser:
    """构造样本数和未跟踪输出路径参数。"""
    parser = ArgumentParser(description="测量 Rivet Phase 0 启动基线")
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """采集并输出基线，不将机器特定结果写入跟踪文件。"""
    arguments = _build_parser().parse_args(argv)
    sample_count = cast(int, arguments.samples)
    output_path = cast(Path | None, arguments.output)
    serialized = json.dumps(
        collect_startup_baseline(sample_count),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    if output_path is None:
        print(serialized)
        return 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(f"{serialized}\n", encoding="utf-8")
    print(f"启动基线已写入 {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
