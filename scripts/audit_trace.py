"""离线计算 NDJSON Trace 的 Demand/Activation 因果门禁。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from rivet.trace.audit import audit_demand_trace, load_trace


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="审计 Demand Traceability")
    parser.add_argument("events", type=Path, help="events.ndjson 路径")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = audit_demand_trace(load_trace(arguments.events))
    except (OSError, ValueError) as error:
        print(f"Trace 审计失败：{error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "activation_count": report.activation_count,
                "demand_count": report.demand_count,
                "demand_traceability_percent": report.demand_traceability_percent,
                "orphan_activation_count": report.orphan_activation_count,
                "passed": report.passed,
                "violations": list(report.violations),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
