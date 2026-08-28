"""提供 `rivet trace --json` 的只读 headless 输出。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter, ValidationError

from rivet.contracts.common import RunId
from rivet.trace.errors import TraceError
from rivet.trace.paths import RuntimePaths
from rivet.trace.replay import TraceReplayer, scan_trace_file


def run_trace_command(
    *,
    repository: Path,
    run_id: str | None,
    json_output: bool,
) -> int:
    """只读回放 Trace，不创建目录或修改恢复边界。"""
    paths = RuntimePaths.for_repository(repository)
    replayer = TraceReplayer(paths.events_path)
    validated_run_id: str | None = None
    if run_id is not None:
        try:
            validated_run_id = cast(
                str,
                TypeAdapter(RunId).validate_python(run_id, strict=True),
            )
        except ValidationError:
            print("Trace run_id 格式无效", file=sys.stderr)
            return 2
    try:
        if validated_run_id is None:
            scan_result = scan_trace_file(paths.events_path)
            run_ids: list[str] = sorted(
                {located.record.event.run_id for located in scan_result.located_events}
            )
            payload = {
                "schema_version": 1,
                "run_ids": run_ids,
                "event_count": len(scan_result.located_events),
                "warnings": list(scan_result.report.warnings),
            }
            if json_output:
                _print_json(payload)
            else:
                print(f"Trace runs: {len(run_ids)}")
                for discovered_run_id in run_ids:
                    print(discovered_run_id)
            return 0
        else:
            replay = replayer.replay(validated_run_id)
            payload = replay.model_dump(mode="json")
    except TraceError as error:
        print(f"Trace 读取失败：{error}", file=sys.stderr)
        return 1

    if json_output:
        _print_json(payload)
        return 0
    print(f"Run: {validated_run_id}")
    print(f"Events: {replay.state.event_count}")
    print(f"Last sequence: {replay.state.last_sequence}")
    return 0


def _print_json(payload: object) -> None:
    """以稳定紧凑格式输出 headless JSON。"""
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
