"""定义 NDJSON Trace 唯一事实源所需的最小持久契约。"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import Field

from rivet.contracts.common import ContractModel
from rivet.contracts.events import TraceEventEnvelope
from rivet.trace.errors import TraceEventTooLargeError

DEFAULT_MAX_EVENT_BYTES = 65_536


class PersistedTraceEvent(ContractModel):
    """为事件信封增加仓库级单调序号。"""

    sequence: int = Field(ge=1)
    event: TraceEventEnvelope


class RecoveryReport(ContractModel):
    """报告启动扫描保留与裁掉的损坏尾部。"""

    recovered_event_count: int = Field(ge=0)
    truncated_bytes: int = Field(ge=0)


@dataclass(frozen=True, slots=True)
class TraceScan:
    """保存已验证事件与恢复报告。"""

    events: tuple[PersistedTraceEvent, ...]
    report: RecoveryReport


def serialize_persisted_event(
    record: PersistedTraceEvent,
    *,
    max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
) -> bytes:
    """以确定性、单行 JSON 序列化事件并限制大小。"""
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
            f"Trace 事件超过 {max_event_bytes} 字节；请缩短事件摘要"
        )
    return serialized
