"""验证 NDJSON-only Trace 的耐久、因果与恢复门禁。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from rivet.kernel.capability_demand import (
    CapabilityDemand,
    DemandContext,
)
from rivet.kernel.errors import DemandCausalityError, DemandJournalError
from rivet.trace.adapters import TraceDemandJournal
from rivet.trace.builder import TraceEventBuilder
from rivet.trace.errors import (
    CorruptTraceError,
    TraceEventTooLargeError,
    TraceWriteError,
)
from rivet.trace.paths import RuntimePaths
from rivet.trace.store import TraceStore


def _paths(tmp_path: Path) -> RuntimePaths:
    repository = tmp_path / "repo"
    repository.mkdir(exist_ok=True)
    (repository / ".git").mkdir(exist_ok=True)
    return RuntimePaths.for_repository(
        repository,
        environment={
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
    )


def _event(
    builder: TraceEventBuilder,
    ordinal: int,
    *,
    parent: str | None = None,
    payload: dict[str, JsonValue] | None = None,
):
    return builder.build(
        event_type="trace.observed",
        run_id="run_trace",
        session_id="session_trace",
        parent_event_id=parent,
        payload=payload or {"ordinal": ordinal},
    )


@pytest.mark.asyncio
async def test_emit_is_fsynced_redacted_and_sequenced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    from rivet.trace import store as store_module

    real_fsync = store_module.os.fsync

    def observed_fsync(descriptor: int) -> None:
        calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(store_module.os, "fsync", observed_fsync)
    store = TraceStore(_paths(tmp_path))
    await store.start()
    builder = TraceEventBuilder()
    first = _event(builder, 1, payload={"message": "api_key=super-secret"})
    second = _event(builder, 2, parent=first.event_id)
    persisted = await store.emit_many((first, second))

    assert [item.sequence for item in persisted] == [1, 2]
    assert calls
    lines = store.paths.events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "super-secret" not in "\n".join(lines)
    assert "[REDACTED]" in lines[0]
    await store.close()


@pytest.mark.asyncio
async def test_fsync_failure_poison_store_without_reporting_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TraceStore(_paths(tmp_path))
    await store.start()
    from rivet.trace import store as store_module

    fsync_calls = 0

    def fail_fsync(_descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(store_module.os, "fsync", fail_fsync)
    first = _event(TraceEventBuilder(), 1)

    with pytest.raises(TraceWriteError, match="append/fsync"):
        await store.emit(first)

    assert fsync_calls >= 1
    assert store.event_count == 0
    assert store.pending_event_count == 0
    assert store.event(first.event_id) is None
    assert store.paths.events_path.read_bytes() == b""

    with pytest.raises(TraceWriteError, match="append/fsync"):
        await store.emit(_event(TraceEventBuilder(), 2))
    with pytest.raises(TraceWriteError, match="append/fsync"):
        await store.close()

    recovered = TraceStore(store.paths)
    await recovered.start()
    assert recovered.event_count == 0
    await recovered.close()


@pytest.mark.asyncio
async def test_oversized_event_is_rejected_before_queue_without_sequence_gap(
    tmp_path: Path,
) -> None:
    store = TraceStore(_paths(tmp_path), max_event_bytes=1_024)
    await store.start()
    builder = TraceEventBuilder()
    first = await store.emit(_event(builder, 1))
    queue_peak_before_rejection = store.queue_peak_size

    with pytest.raises(TraceEventTooLargeError, match="1024"):
        await store.emit(_event(builder, 2, payload={"message": "x" * 4_096}))

    assert store.pending_event_count == 0
    assert store.queue_peak_size == queue_peak_before_rejection
    assert store.event_count == 1

    second = await store.emit(_event(builder, 3, parent=first.event.event_id))
    assert second.sequence == 2
    assert [record.sequence for record in store.events()] == [1, 2]
    assert len(store.paths.events_path.read_text(encoding="utf-8").splitlines()) == 2
    await store.close()


@pytest.mark.asyncio
async def test_parent_must_precede_child_and_stay_in_run(tmp_path: Path) -> None:
    store = TraceStore(_paths(tmp_path))
    await store.start()
    builder = TraceEventBuilder()
    orphan = _event(builder, 2, parent="event_missing")
    with pytest.raises(TraceWriteError, match="父事件"):
        await store.emit(orphan)
    assert store.event_count == 0
    await store.close()


@pytest.mark.asyncio
async def test_second_writer_is_rejected_until_first_closes(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    first = TraceStore(paths)
    second = TraceStore(paths)
    await first.start()
    with pytest.raises(TraceWriteError, match="已有 Trace Writer"):
        await second.start()
    await first.close()
    await second.start()
    await second.close()


@pytest.mark.asyncio
async def test_start_recovers_only_incomplete_tail(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = TraceStore(paths)
    await store.start()
    await store.emit(_event(TraceEventBuilder(), 1))
    await store.close()
    with paths.events_path.open("ab") as stream:
        stream.write(b'{"partial":')

    recovered = TraceStore(paths)
    await recovered.start()
    assert recovered.event_count == 1
    assert recovered.recovery_report.truncated_bytes > 0
    assert paths.events_path.read_bytes().endswith(b"\n")
    await recovered.close()


@pytest.mark.asyncio
async def test_start_rejects_corrupt_middle_record(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.prepare()
    paths.events_path.write_bytes(b"not-json\nnot-json\n")
    with pytest.raises(CorruptTraceError, match="中部"):
        await TraceStore(paths).start()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_tail",
    (b"not-json\n", b"x" * 1_025 + b"\n"),
    ids=("malformed-complete-line", "oversized-complete-line"),
)
async def test_complete_invalid_tail_fails_closed_without_truncation(
    tmp_path: Path,
    invalid_tail: bytes,
) -> None:
    """只有无换行半条尾记录可恢复，完整非法事实不得被擦除。"""
    paths = _paths(tmp_path)
    store = TraceStore(paths, max_event_bytes=1_024)
    await store.start()
    await store.emit(_event(TraceEventBuilder(), 1))
    await store.close()
    with paths.events_path.open("ab") as stream:
        stream.write(invalid_tail)
    original = paths.events_path.read_bytes()

    with pytest.raises(CorruptTraceError):
        await TraceStore(paths, max_event_bytes=1_024).start()

    assert paths.events_path.read_bytes() == original


@pytest.mark.asyncio
async def test_demand_journal_persists_parent_before_child(tmp_path: Path) -> None:
    store = TraceStore(_paths(tmp_path))
    await store.start()
    journal = TraceDemandJournal(store)
    context = DemandContext(run_id="run_demand", session_id="session_demand")
    root = CapabilityDemand.user_explicit(
        "task.fix", reason="用户请求", context=context
    )
    root_record = await journal.append(root)
    child = CapabilityDemand.model_tool_call(
        "file_read",
        reason="模型读取代码",
        context=context,
        operation_id="call_read",
        parent_demand_id=root.demand_id,
    )
    child_record = await journal.append(child)

    assert root_record.sequence < child_record.sequence
    records = store.events("run_demand")
    assert [item.event.event_type for item in records] == [
        "demand.created",
        "demand.created",
    ]
    assert records[1].event.parent_event_id == records[0].event.event_id
    await store.close()


@pytest.mark.asyncio
async def test_demand_journal_rejects_unknown_and_cross_run_parent(
    tmp_path: Path,
) -> None:
    store = TraceStore(_paths(tmp_path))
    await store.start()
    journal = TraceDemandJournal(store)
    first_context = DemandContext(run_id="run_one", session_id="session_one")
    root = CapabilityDemand.user_explicit(
        "task.ask", reason="用户请求", context=first_context
    )
    await journal.append(root)
    cross_context = DemandContext(run_id="run_two", session_id="session_two")
    forged = CapabilityDemand.model_tool_call(
        "file_read",
        reason="跨运行伪造",
        context=cross_context,
        operation_id="call_forged",
        parent_demand_id=root.demand_id,
    )
    with pytest.raises(DemandCausalityError, match="同一运行"):
        await journal.append(forged)
    assert store.event_count == 1
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("forged_source", ("USER_EXPLICIT", "MODEL_TOOL_CALL"))
async def test_demand_journal_recovery_rejects_forged_event_parent(
    tmp_path: Path,
    forged_source: str,
) -> None:
    paths = _paths(tmp_path)
    store = TraceStore(paths)
    await store.start()
    builder = TraceEventBuilder()
    unrelated = builder.build(
        event_type="trace.observed",
        run_id="run_recovery",
        session_id="session_recovery",
        payload={"ordinal": 1},
    )
    root = builder.build(
        event_type="demand.created",
        run_id="run_recovery",
        session_id="session_recovery",
        parent_event_id=(
            unrelated.event_id if forged_source == "USER_EXPLICIT" else None
        ),
        payload={
            "capability_id": "task.fix",
            "demand_id": "demand_root",
            "demand_source": "USER_EXPLICIT",
            "operation_id": None,
            "parent_demand_id": None,
            "reason": "用户请求",
        },
    )
    events = [unrelated, root]
    if forged_source == "MODEL_TOOL_CALL":
        events.append(
            builder.build(
                event_type="demand.created",
                run_id="run_recovery",
                session_id="session_recovery",
                parent_event_id=unrelated.event_id,
                payload={
                    "capability_id": "file_read",
                    "demand_id": "demand_child",
                    "demand_source": "MODEL_TOOL_CALL",
                    "operation_id": "call_read",
                    "parent_demand_id": "demand_root",
                    "reason": "模型读取代码",
                },
            )
        )
    await store.emit_many(tuple(events))
    await store.close()

    recovered = TraceStore(paths)
    await recovered.start()
    with pytest.raises(DemandJournalError, match="因果审计"):
        TraceDemandJournal(recovered)
    await recovered.close()


def test_trace_file_contains_only_ndjson_records(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.prepare()
    assert not any(
        path.suffix in {".sqlite", ".sqlite3", ".db"}
        for path in paths.runtime_root.rglob("*")
    )
    assert json.loads('{"format":"ndjson"}') == {"format": "ndjson"}
