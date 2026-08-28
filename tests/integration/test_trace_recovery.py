"""验证 Trace 顺序、SQLite 重建、尾部恢复与进程崩溃恢复。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rivet.trace.paths import RuntimePaths
from rivet.trace.store import TraceStore
from tests.fixtures.trace.events import make_event


def _paths(repository: Path) -> RuntimePaths:
    return RuntimePaths.for_repository(
        repository,
        environment={"XDG_CACHE_HOME": str(repository / "cache")},
    )


@pytest.mark.asyncio
async def test_reopen_rebuilds_sqlite_and_replay_matches_online_state(
    tmp_path: Path,
) -> None:
    store = TraceStore(_paths(tmp_path))
    await store.start()
    first = make_event(1, event_type="run.started")
    second = make_event(
        2,
        event_type="tool.started",
        parent_event_id=first.event_id,
        payload={"input_tokens": 2, "output_tokens": 3},
    )
    persisted = await store.emit_many((first, second))
    online_state = store.online_state("run_trace_test")
    await store.close()

    reopened = TraceStore(_paths(tmp_path))
    await reopened.start()
    replay = reopened.replay("run_trace_test")

    assert [record.sequence for record in persisted] == [1, 2]
    assert reopened.database.event_count() == 2
    assert replay.state == online_state
    assert replay.events[1].event.parent_event_id == first.event_id
    await reopened.close()


@pytest.mark.asyncio
async def test_trace_json_cli_replays_persisted_run(tmp_path: Path) -> None:
    store = TraceStore(_paths(tmp_path))
    await store.start()
    await store.emit(make_event(1, event_type="run.started"))
    await store.close()

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rivet",
            "trace",
            "run_trace_test",
            "--json",
            "--repository",
            str(tmp_path),
        ],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(Path.cwd()),
            "PYTHONNOUSERSITE": "1",
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        },
    )
    payload = json.loads(completed.stdout)

    assert completed.returncode == 0
    assert payload["state"]["event_count"] == 1


@pytest.mark.asyncio
async def test_start_truncates_partial_tail_and_continues_sequence(
    tmp_path: Path,
) -> None:
    store = TraceStore(_paths(tmp_path))
    await store.start()
    await store.emit_many(tuple(make_event(index) for index in range(1, 21)))
    await store.close()
    with store.paths.events_path.open("ab") as events_file:
        events_file.write(b'{"schema_version":1,"sequence":21')

    recovered = TraceStore(_paths(tmp_path))
    await recovered.start()
    next_record = await recovered.emit(make_event(21))

    assert recovered.recovery_report.truncated_bytes > 0
    assert next_record.sequence == 21
    assert recovered.database.event_count() == 21
    await recovered.close()


@pytest.mark.asyncio
async def test_random_process_termination_recovers_last_acknowledged_event(
    tmp_path: Path,
) -> None:
    script = Path("tests/fixtures/trace/writer_process.py").resolve()
    environment = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(Path.cwd()),
        "PYTHONNOUSERSITE": "1",
    }
    process = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path)],
        cwd=Path.cwd(),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    acknowledged = [int(process.stdout.readline().strip()) for _ in range(37)]
    process.kill()
    process.wait(timeout=5)

    recovered = TraceStore(_paths(tmp_path))
    await recovered.start()
    replay = recovered.replay("run_trace_test")

    assert acknowledged == list(range(1, 38))
    assert replay.state.event_count >= acknowledged[-1]
    assert [record.sequence for record in replay.events] == list(
        range(1, replay.state.event_count + 1)
    )
    await recovered.close()
