"""构造不包含完整大正文的 Reader TUI 预览事件。"""

from __future__ import annotations

from rivet.contracts.common import EventId
from rivet.contracts.ipc import IpcEvent
from rivet.contracts.readers import ReaderResult

MAX_PREVIEW_CHARS = 8_192


def build_reader_preview_event(
    result: ReaderResult,
    *,
    event_id: EventId,
    sequence: int,
) -> IpcEvent:
    """只传递有界预览、来源和不可信事实。"""
    return IpcEvent(
        event_id=event_id,
        event_type="reader.previewed",
        sequence=sequence,
        payload={
            "source_path": result.source_path,
            "source_sha256": result.source_sha256,
            "reader_id": result.reader_id,
            "status": result.status.value,
            "detected_format": result.detected_format,
            "preview": result.content[:MAX_PREVIEW_CHARS],
            "preview_truncated": len(result.content) > MAX_PREVIEW_CHARS,
            "untrusted": True,
        },
    )
