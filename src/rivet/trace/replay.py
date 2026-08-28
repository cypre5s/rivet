"""扫描、恢复并确定性回放 append-only NDJSON 事件。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from rivet.contracts.common import RunId
from rivet.trace.errors import (
    CorruptTraceError,
    UnknownTraceVersionError,
)
from rivet.trace.models import (
    LocatedTraceEvent,
    PersistedTraceEvent,
    RecoveryReport,
    TraceReplayResult,
)
from rivet.trace.reducer import TraceReducer


class UnknownEventPolicy(StrEnum):
    """控制未知事件版本是失败关闭还是显式跳过。"""

    REJECT = "reject"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class TraceScanResult:
    """返回带字节位置事件与恢复报告。"""

    located_events: tuple[LocatedTraceEvent, ...]
    report: RecoveryReport


def scan_trace_file(
    path: Path,
    *,
    unknown_policy: UnknownEventPolicy = UnknownEventPolicy.REJECT,
    recover_tail: bool = False,
) -> TraceScanResult:
    """逐行扫描事件，只允许在物理尾部执行截断恢复。"""
    if not path.exists():
        return TraceScanResult(
            located_events=(),
            report=RecoveryReport(
                recovered_event_count=0,
                skipped_event_count=0,
                truncated_bytes=0,
            ),
        )
    file_size = path.stat().st_size
    located_events: list[LocatedTraceEvent] = []
    warnings: list[str] = []
    skipped_count = 0
    truncate_offset: int | None = None
    last_sequence = 0

    with path.open("rb") as trace_file:
        while True:
            byte_offset = trace_file.tell()
            line = trace_file.readline()
            if not line:
                break
            byte_length = len(line)
            is_tail = trace_file.tell() == file_size
            if not line.endswith(b"\n"):
                if recover_tail and is_tail:
                    truncate_offset = byte_offset
                    break
                raise CorruptTraceError(f"Trace 尾部缺少换行：offset {byte_offset}")
            try:
                raw_document: object = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                if recover_tail and is_tail:
                    truncate_offset = byte_offset
                    break
                raise CorruptTraceError(
                    f"Trace JSON 损坏：offset {byte_offset}"
                ) from error
            if not isinstance(raw_document, dict):
                if recover_tail and is_tail:
                    truncate_offset = byte_offset
                    break
                raise CorruptTraceError(f"Trace 事件不是对象：offset {byte_offset}")
            document = cast(dict[str, object], raw_document)
            version = document.get("schema_version")
            if version != 1:
                if unknown_policy is UnknownEventPolicy.REJECT:
                    raise UnknownTraceVersionError(
                        f"未知 Trace 事件版本 {version}：offset {byte_offset}"
                    )
                skipped_count += 1
                warnings.append(
                    f"跳过未知 Trace 事件版本 {version}：offset {byte_offset}"
                )
                continue
            try:
                record = PersistedTraceEvent.model_validate_json(line)
            except ValidationError as error:
                if recover_tail and is_tail:
                    truncate_offset = byte_offset
                    break
                raise CorruptTraceError(
                    f"Trace 事件契约损坏：offset {byte_offset}"
                ) from error
            if record.sequence <= last_sequence:
                raise CorruptTraceError("Trace sequence 必须严格递增")
            if unknown_policy is UnknownEventPolicy.REJECT and (
                record.sequence != last_sequence + 1
            ):
                raise CorruptTraceError(
                    f"Trace sequence 不连续：期望 {last_sequence + 1}，"
                    f"实际 {record.sequence}"
                )
            last_sequence = record.sequence
            located_events.append(
                LocatedTraceEvent(
                    record=record,
                    byte_offset=byte_offset,
                    byte_length=byte_length,
                )
            )

    truncated_bytes = 0
    if truncate_offset is not None:
        truncated_bytes = file_size - truncate_offset
        with path.open("r+b") as trace_file:
            trace_file.truncate(truncate_offset)
        warnings.append(f"已截断 {truncated_bytes} 字节损坏尾部")
    return TraceScanResult(
        located_events=tuple(located_events),
        report=RecoveryReport(
            recovered_event_count=len(located_events),
            skipped_event_count=skipped_count,
            truncated_bytes=truncated_bytes,
            warnings=tuple(warnings),
        ),
    )


class TraceReplayer:
    """从事实源重建指定 run 的 reducer 状态。"""

    def __init__(self, events_path: Path) -> None:
        self._events_path = events_path

    def replay(
        self,
        run_id: RunId,
        *,
        unknown_policy: UnknownEventPolicy = UnknownEventPolicy.REJECT,
    ) -> TraceReplayResult:
        """按物理顺序回放指定 run，并返回跳过报告。"""
        scan_result = scan_trace_file(
            self._events_path,
            unknown_policy=unknown_policy,
            recover_tail=False,
        )
        reducer = TraceReducer(run_id)
        accepted: list[PersistedTraceEvent] = []
        for located_event in scan_result.located_events:
            record = located_event.record
            if record.event.run_id != run_id:
                continue
            reducer.apply(record)
            accepted.append(record)
        return TraceReplayResult(
            state=reducer.snapshot(),
            events=tuple(accepted),
            skipped_event_count=scan_result.report.skipped_event_count,
            warnings=scan_result.report.warnings,
        )
