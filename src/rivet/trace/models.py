"""定义持久事件、回放状态、恢复报告与输出 artifact 契约。"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import Field

from rivet.contracts.common import ArtifactReference, ContractModel, RunId
from rivet.contracts.events import TraceEventEnvelope
from rivet.trace.errors import TraceEventTooLargeError

DEFAULT_MAX_EVENT_BYTES = 65_536


class PersistedTraceEvent(ContractModel):
    """为 append-only 事件增加全局单调 sequence。"""

    sequence: int = Field(ge=1)
    event: TraceEventEnvelope


class TraceState(ContractModel):
    """保存可由事件流完全重建的确定性运行状态与指标。"""

    run_id: RunId
    event_count: int = Field(ge=0)
    last_sequence: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    total_duration_ms: float = Field(ge=0)
    event_type_counts: dict[str, int]
    module_states: dict[str, str]
    transaction_states: dict[str, str]
    module_residence_ms: dict[str, int]


class RecoveryReport(ContractModel):
    """报告启动扫描保留、跳过和截断的事件边界。"""

    recovered_event_count: int = Field(ge=0)
    skipped_event_count: int = Field(ge=0)
    truncated_bytes: int = Field(ge=0)
    warnings: tuple[str, ...] = ()


class CapturedStream(ContractModel):
    """保存脱敏预览、截断标记与完整日志引用。"""

    preview: str = Field(max_length=65_536)
    preview_truncated: bool
    artifact_truncated: bool
    artifact: ArtifactReference


class OutputCapture(ContractModel):
    """绑定同一事件的 stdout 与 stderr 证据。"""

    stdout: CapturedStream
    stderr: CapturedStream


class TraceReplayResult(ContractModel):
    """返回回放状态、已接受事件与显式跳过报告。"""

    state: TraceState
    events: tuple[PersistedTraceEvent, ...]
    skipped_event_count: int = Field(ge=0)
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LocatedTraceEvent:
    """记录 NDJSON 事件的字节偏移，供 SQLite 建索引。"""

    record: PersistedTraceEvent
    byte_offset: int
    byte_length: int


def serialize_persisted_event(
    record: PersistedTraceEvent,
    *,
    max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
) -> bytes:
    """以排序键和紧凑分隔符生成确定性单行 JSON。"""
    serialized = (
        json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    if len(serialized) > max_event_bytes:
        raise TraceEventTooLargeError(
            f"Trace 事件超过 {max_event_bytes} 字节，应改存 artifact"
        )
    return serialized
