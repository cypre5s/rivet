"""验证未知事件版本的拒绝与显式跳过报告。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rivet.trace.errors import UnknownTraceVersionError
from rivet.trace.models import PersistedTraceEvent, serialize_persisted_event
from rivet.trace.replay import TraceReplayer, UnknownEventPolicy
from tests.fixtures.trace.events import make_event


def test_replay_rejects_unknown_version_by_default(tmp_path: Path) -> None:
    events_path = tmp_path / "events.ndjson"
    known = serialize_persisted_event(
        PersistedTraceEvent(sequence=1, event=make_event(1))
    )
    unknown_document = {
        "schema_version": 999,
        "sequence": 2,
        "event": {"run_id": "run_trace_test"},
    }
    events_path.write_bytes(
        known
        + json.dumps(unknown_document, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )

    with pytest.raises(UnknownTraceVersionError, match="999"):
        TraceReplayer(events_path).replay("run_trace_test")


def test_replay_can_skip_unknown_version_with_warning(tmp_path: Path) -> None:
    events_path = tmp_path / "events.ndjson"
    known = serialize_persisted_event(
        PersistedTraceEvent(sequence=1, event=make_event(1))
    )
    unknown_document: dict[str, object] = {
        "schema_version": 999,
        "sequence": 2,
        "event": {},
    }
    events_path.write_bytes(
        known + json.dumps(unknown_document).encode("utf-8") + b"\n"
    )

    result = TraceReplayer(events_path).replay(
        "run_trace_test", unknown_policy=UnknownEventPolicy.SKIP
    )

    assert result.state.event_count == 1
    assert result.skipped_event_count == 1
    assert result.warnings
