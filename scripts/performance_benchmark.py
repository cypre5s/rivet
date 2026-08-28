"""在隔离子进程中采集 Phase 14 启动、RSS、模块和 Trace 指标。"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import cast

SAFE_ENVIRONMENT_NAMES = frozenset(
    {"LANG", "LC_ALL", "PATH", "PYTHONPATH", "TERM", "VIRTUAL_ENV"}
)


def run_performance_benchmark() -> dict[str, object]:
    """采集可自动复现的指标，并诚实标注非交互 TUI 缺口。"""
    startup = _run_json_script("measure_startup.py", "--samples", "10")
    kernel = _run_json_script("measure_kernel.py")
    trace = _run_json_script("measure_trace.py")
    help_metrics = _mapping(startup, "help_cold_start_ms")
    empty_kernel = _mapping(kernel, "empty_kernel")
    help_entrypoint = _mapping(kernel, "help_entrypoint")
    manifest_loading = _mapping(kernel, "manifest_loading")
    measurable_checks = {
        "help_cold_start": _number(help_metrics, "p95") <= 300,
        "headless_kernel_rss": _number(empty_kernel, "peak_rss_mib") <= 80,
        "help_optional_imports": not _list(help_entrypoint, "forbidden_loaded_modules"),
        "kernel_resource_cleanup": _integer(empty_kernel, "resource_count") == 0,
        "manifest_loading": _number(manifest_loading, "p95_ms") <= 30,
        "startup_network": _integer(startup, "network_connections_before_first_action")
        == 0,
        "startup_subprocess": _integer(startup, "subprocess_starts_before_first_action")
        == 0,
        "trace_serialization": _number(trace, "serialization_p95_ms") <= 2,
        "trace_resource_cleanup": _integer(trace, "pending_event_count") == 0,
    }
    deviations = {
        "tui_first_frame_ms": {
            "status": "INCONCLUSIVE",
            "target": "<= 1500 ms",
            "reason": "非交互 CI 无法把测试渲染耗时冒充真实终端首帧",
        },
        "tui_kernel_idle_rss_mib": {
            "status": "INCONCLUSIVE",
            "target": "<= 220 MiB",
            "reason": "需要稳定 PTY 与真实 OpenTUI 前台采样，保留人工发布检查",
        },
    }
    return {
        "schema_version": 1,
        "suite": "performance",
        "passed": all(measurable_checks.values()),
        "environment": {
            "architecture": platform.machine(),
            "operating_system": platform.platform(),
            "python": platform.python_version(),
        },
        "checks": measurable_checks,
        "startup": startup,
        "kernel": kernel,
        "trace": trace,
        "deviations": deviations,
        "thresholds": {
            "help_cold_start_p95_ms_maximum": 300,
            "headless_kernel_idle_rss_mib_maximum": 80,
            "manifest_loading_p95_ms_maximum": 30,
            "trace_event_encoding_p95_ms_maximum": 2,
        },
    }


def _run_json_script(script_name: str, *arguments: str) -> dict[str, object]:
    """用不携带凭据的环境运行单一性能脚本。"""
    root = Path(__file__).parents[1]
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in SAFE_ENVIRONMENT_NAMES
    }
    environment["PYTHONNOUSERSITE"] = "1"
    completed = subprocess.run(
        (sys.executable, str(root / "scripts" / script_name), *arguments),
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"性能脚本 {script_name} 失败")
    raw_result = cast(object, json.loads(completed.stdout))
    if not isinstance(raw_result, dict):
        raise RuntimeError(f"性能脚本 {script_name} 输出无效")
    return cast(dict[str, object], raw_result)


def _mapping(payload: dict[str, object], key: str) -> dict[str, object]:
    """读取嵌套指标映射。"""
    value = payload[key]
    if not isinstance(value, dict):
        raise ValueError(f"性能指标 {key} 不是映射")
    return cast(dict[str, object], value)


def _number(payload: dict[str, object], key: str) -> float:
    """读取非布尔数值指标。"""
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"性能指标 {key} 不是数值")
    return float(value)


def _integer(payload: dict[str, object], key: str) -> int:
    """读取整数指标。"""
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"性能指标 {key} 不是整数")
    return value


def _list(payload: dict[str, object], key: str) -> list[object]:
    """读取列表指标。"""
    value = payload[key]
    if not isinstance(value, list):
        raise ValueError(f"性能指标 {key} 不是列表")
    return cast(list[object], value)
