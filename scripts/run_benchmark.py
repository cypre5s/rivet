"""运行不进入产品 CLI 的最小开发验证套件。"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from environment_fingerprint import collect_environment_fingerprint
from fault_benchmark import run_fault_benchmark
from functional_benchmark import run_functional_benchmark
from performance_benchmark import run_performance_benchmark
from verify_licenses import inspect_licenses


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行 Rivet 开发期离线验证")
    parser.add_argument(
        "--suite",
        choices=("functional", "faults", "performance", "licenses", "all"),
        required=True,
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    suite = cast(str, arguments.suite)
    output_path = cast(Path | None, arguments.output)
    if suite == "functional":
        result = asyncio.run(run_functional_benchmark())
    elif suite == "faults":
        result = asyncio.run(run_fault_benchmark())
    elif suite == "performance":
        result = run_performance_benchmark()
    elif suite == "licenses":
        result = inspect_licenses(Path(__file__).parents[1])
    elif suite == "all":
        result = _run_all()
    else:
        raise AssertionError("参数解析器不得产生未知 suite")

    serialized = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if output_path is None:
        print(serialized)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(f"{serialized}\n", encoding="utf-8")
        print(f"开发验证结果已写入 {output_path}")
    return 0 if cast(bool, result["passed"]) else 1


def _run_all() -> dict[str, object]:
    components = {
        "faults": asyncio.run(run_fault_benchmark()),
        "functional": asyncio.run(run_functional_benchmark()),
        "licenses": inspect_licenses(Path(__file__).parents[1]),
        "performance": run_performance_benchmark(),
    }
    return {
        "schema_version": 1,
        "suite": "all",
        "passed": all(cast(bool, item["passed"]) for item in components.values()),
        "recorded_at": datetime.now(UTC).isoformat(),
        "environment": collect_environment_fingerprint(),
        "components": components,
        "limitations": [
            "离线固定提案不代表真实模型泛化能力",
            "benchmark 仅存在于 scripts/tests，不是 Rivet 产品命令",
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
