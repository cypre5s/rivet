"""运行可重复的 Rivet 小型基准套件并执行硬阈值门禁。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from time import perf_counter
from typing import cast

from context_fixtures import (
    load_context_cases,
    materialize_context_case,
)

from rivet.context.engine import ProgressiveContext
from rivet.context.inventory import RepositoryInventoryBuilder
from rivet.contracts.context import ContextBudget
from rivet.kernel.resources import ResourceScope
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.process import ProcessRunner

CONTEXT_BUDGET = ContextBudget(
    total_tokens=8_000,
    required_tokens=1_000,
    working_tokens=6_000,
    history_tokens=1_000,
)
FIXED_NOW = datetime(2026, 8, 28, tzinfo=UTC)


def _percentile(values: list[float], percentile: float) -> float:
    """以最近秩方法计算不插值百分位。"""
    if not values:
        raise ValueError("百分位样本不得为空")
    ordered = sorted(values)
    index = max(0, ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _tree_sitter_import_count() -> int:
    """统计已经进入解释器的 Tree-sitter binding 与 grammar。"""
    return sum(
        name == "tree_sitter" or name.startswith("tree_sitter_") for name in sys.modules
    )


async def _measure_inventory(root: Path) -> tuple[float, int, int]:
    """创建 10,000 文件仓库并只测清单建立耗时。"""
    repository = root / "inventory-10k"
    repository.mkdir()
    for directory_index in range(100):
        directory = repository / f"package_{directory_index:03d}"
        directory.mkdir()
        for file_index in range(100):
            (directory / f"module_{file_index:03d}.py").write_text(
                "VALUE = 1\n", encoding="utf-8"
            )
    ignored = repository / "ignored"
    ignored.mkdir()
    (ignored / "not-listed.py").write_text("SECRET = True\n", encoding="utf-8")
    (repository / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    boundary = WorkspaceBoundary(repository)
    scope = ResourceScope("context.benchmark.inventory")
    runner = ProcessRunner(
        boundary,
        scope=scope,
        max_capture_bytes=16 * 1024 * 1024,
        root_kind="repository_read_only",
    )
    builder = RepositoryInventoryBuilder(boundary, runner=runner)
    started_at = perf_counter()
    snapshot = await builder.build()
    duration_ms = (perf_counter() - started_at) * 1_000
    ignored_count = sum(entry.path.startswith("ignored/") for entry in snapshot.entries)
    await scope.close()
    scope.assert_empty()
    return duration_ms, len(snapshot.entries), ignored_count


async def run_context_smoke() -> dict[str, object]:
    """测量十二个 Gold 样本、词法延迟、延迟导入与 10k 清单。"""
    with tempfile.TemporaryDirectory(prefix="rivet-context-benchmark-") as directory:
        root = Path(directory)
        cases_root = root / "cases"
        cases_root.mkdir()
        covered = 0
        gold_count = 0
        lexical_durations_ms: list[float] = []
        selected_by_case: dict[str, list[str]] = {}
        imports_before_lexical = _tree_sitter_import_count()
        imports_after_first_lexical = imports_before_lexical
        for case_index, case in enumerate(load_context_cases()):
            repository = materialize_context_case(case, cases_root)
            scope = ResourceScope(f"context.benchmark.{case.case_id.replace('-', '_')}")
            engine = ProgressiveContext(
                repository, scope=scope, clock=lambda: FIXED_NOW
            )
            started_at = perf_counter()
            lexical_result = await engine.retrieve(
                case.task,
                budget=CONTEXT_BUDGET,
                include_syntax=False,
            )
            lexical_durations_ms.append((perf_counter() - started_at) * 1_000)
            if case_index == 0:
                imports_after_first_lexical = _tree_sitter_import_count()
            result = lexical_result
            if case.include_syntax:
                result = await engine.retrieve(
                    case.task,
                    budget=CONTEXT_BUDGET,
                    include_syntax=True,
                )
            selected_paths = sorted(
                {item.repository_path for item in result.selection.items}
            )
            selected_by_case[case.case_id] = selected_paths
            covered += len(set(selected_paths).intersection(case.gold_files))
            gold_count += len(case.gold_files)
            await scope.close()
            scope.assert_empty()
        inventory_ms, inventory_file_count, ignored_count = await _measure_inventory(
            root
        )
        coverage = covered / gold_count
        lexical_p95_ms = _percentile(lexical_durations_ms, 0.95)
        lazy_import_delta = imports_after_first_lexical - imports_before_lexical
        passed = (
            coverage >= 0.70
            and inventory_ms <= 1_000.0
            and lexical_p95_ms <= 300.0
            and lazy_import_delta == 0
            and inventory_file_count == 10_001
            and ignored_count == 0
        )
        return {
            "schema_version": 1,
            "suite": "context-smoke",
            "passed": passed,
            "case_count": len(selected_by_case),
            "gold_file_count": gold_count,
            "covered_gold_file_count": covered,
            "gold_coverage": round(coverage, 6),
            "lexical_p95_ms": round(lexical_p95_ms, 3),
            "lexical_sample_count": len(lexical_durations_ms),
            "inventory_10k_ms": round(inventory_ms, 3),
            "inventory_file_count": inventory_file_count,
            "ignored_file_count": ignored_count,
            "tree_sitter_import_delta_without_syntax": lazy_import_delta,
            "tree_sitter_import_count_after_suite": _tree_sitter_import_count(),
            "selected_by_case": selected_by_case,
            "thresholds": {
                "gold_coverage_minimum": 0.70,
                "inventory_10k_ms_maximum": 1_000.0,
                "lexical_p95_ms_maximum": 300.0,
                "tree_sitter_import_delta_without_syntax_maximum": 0,
            },
        }


def _build_parser() -> argparse.ArgumentParser:
    """构造基准套件与可选结果文件参数。"""
    parser = argparse.ArgumentParser(description="运行 Rivet 可重复基准")
    parser.add_argument("--suite", choices=("context-smoke",), required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """运行选定套件，输出 JSON，并以阈值结果决定退出码。"""
    arguments = _build_parser().parse_args(argv)
    suite = cast(str, arguments.suite)
    output_path = cast(Path | None, arguments.output)
    if suite != "context-smoke":
        raise AssertionError("参数解析器不得产生未知套件")
    result = asyncio.run(run_context_smoke())
    serialized = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if output_path is None:
        print(serialized)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(f"{serialized}\n", encoding="utf-8")
        print(f"Context 基准结果已写入 {output_path}")
    return 0 if cast(bool, result["passed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
