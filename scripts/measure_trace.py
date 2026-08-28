"""测量 Trace 的 10,000 事件写入、序列化与关闭性能。"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import cast

from rivet.contracts.events import TraceEventEnvelope
from rivet.trace.models import PersistedTraceEvent, serialize_persisted_event
from rivet.trace.paths import RuntimePaths
from rivet.trace.redaction import SecretRedactor
from rivet.trace.store import TraceStore

EVENT_COUNT = 10_000
SERIALIZATION_SAMPLE_COUNT = 1_000


def _event(sequence: int) -> TraceEventEnvelope:
    """构造不依赖模型或真实时间的性能样本。"""
    return TraceEventEnvelope(
        event_id=f"event_measure_{sequence}",
        event_type="trace.measured",
        timestamp=datetime(2026, 8, 28, tzinfo=UTC) + timedelta(milliseconds=sequence),
        run_id="run_trace_measure",
        session_id="session_trace_measure",
        payload={"ordinal": sequence},
    )


async def measure_trace() -> dict[str, object]:
    """在临时仓库中采集不含凭据的可重复性能指标。"""
    with tempfile.TemporaryDirectory(prefix="rivet-trace-measure-") as directory:
        repository_root = Path(directory)
        paths = RuntimePaths.for_repository(
            repository_root,
            environment={"XDG_CACHE_HOME": str(repository_root / "cache")},
        )
        store = TraceStore(
            paths,
            redactor=SecretRedactor(environment={}),
            queue_capacity=256,
            batch_size=128,
        )
        await store.start()
        events = tuple(_event(sequence) for sequence in range(1, EVENT_COUNT + 1))
        write_started_at = perf_counter()
        persisted = await store.emit_many(events)
        write_duration_ms = (perf_counter() - write_started_at) * 1_000

        sample_record = PersistedTraceEvent(sequence=1, event=events[0])
        serialization_samples_ms: list[float] = []
        for _ in range(SERIALIZATION_SAMPLE_COUNT):
            started_at = perf_counter()
            serialize_persisted_event(sample_record)
            serialization_samples_ms.append((perf_counter() - started_at) * 1_000)
        shutdown_started_at = perf_counter()
        await store.close()
        shutdown_duration_ms = (perf_counter() - shutdown_started_at) * 1_000
        return {
            "schema_version": 1,
            "event_count": len(persisted),
            "first_sequence": persisted[0].sequence,
            "last_sequence": persisted[-1].sequence,
            "write_duration_ms": round(write_duration_ms, 3),
            "serialization_p95_ms": round(sorted(serialization_samples_ms)[949], 6),
            "writer_shutdown_ms": round(shutdown_duration_ms, 3),
            "queue_capacity": store.queue_capacity,
            "queue_peak_size": store.queue_peak_size,
            "pending_event_count": store.pending_event_count,
            "database_event_count": store.database_event_count_before_close,
            "ndjson_size_bytes": paths.events_path.stat().st_size,
        }


def _build_parser() -> argparse.ArgumentParser:
    """构造可选的未跟踪结果文件参数。"""
    parser = argparse.ArgumentParser(description="测量 Rivet Trace 性能")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """输出或写入脱敏性能 JSON。"""
    arguments = _build_parser().parse_args(argv)
    output_path = cast(Path | None, arguments.output)
    serialized = json.dumps(
        asyncio.run(measure_trace()),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    if output_path is None:
        print(serialized)
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(f"{serialized}\n", encoding="utf-8")
        print(f"Trace 性能结果已写入 {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
