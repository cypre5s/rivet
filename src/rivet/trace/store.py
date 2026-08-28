"""以单 Writer Task 持久化有界、脱敏且可恢复的 Trace。"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

from rivet.contracts.common import RunId
from rivet.contracts.events import TraceEventEnvelope
from rivet.kernel.resources import ResourceScope
from rivet.trace.artifacts import TraceArtifactStore
from rivet.trace.database import TraceDatabase
from rivet.trace.errors import TraceShutdownError, TraceWriteError
from rivet.trace.models import (
    DEFAULT_MAX_EVENT_BYTES,
    LocatedTraceEvent,
    OutputCapture,
    PersistedTraceEvent,
    RecoveryReport,
    TraceReplayResult,
    TraceState,
    serialize_persisted_event,
)
from rivet.trace.paths import RuntimePaths
from rivet.trace.redaction import SecretRedactor
from rivet.trace.reducer import TraceReducer
from rivet.trace.replay import TraceReplayer, scan_trace_file


@dataclass(slots=True)
class _WriteRequest:
    """绑定已脱敏事件与等待持久化确认的 Future。"""

    event: TraceEventEnvelope
    future: asyncio.Future[PersistedTraceEvent]


class TraceStore:
    """协调 NDJSON 事实源、SQLite 索引、在线 reducer 与资源回收。"""

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        redactor: SecretRedactor | None = None,
        queue_capacity: int = 256,
        batch_size: int = 128,
        max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
        shutdown_timeout_seconds: float = 1.0,
    ) -> None:
        if queue_capacity <= 0 or batch_size <= 0:
            raise ValueError("Trace queue_capacity 与 batch_size 必须大于 0")
        if batch_size > queue_capacity:
            raise ValueError("Trace batch_size 不得大于 queue_capacity")
        if max_event_bytes <= 0 or shutdown_timeout_seconds <= 0:
            raise ValueError("Trace 大小与关闭时限必须大于 0")
        self.paths = paths
        self.queue_capacity = queue_capacity
        self._batch_size = batch_size
        self._max_event_bytes = max_event_bytes
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._redactor = redactor or SecretRedactor()
        self.database = TraceDatabase(paths.database_path)
        self.artifacts = TraceArtifactStore(paths, self._redactor)
        self._queue: asyncio.Queue[_WriteRequest | None] | None = None
        self._scope: ResourceScope | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._reducers: dict[str, TraceReducer] = {}
        self._pending_event_runs: dict[str, str] = {}
        self._next_sequence = 1
        self._started = False
        self._accepting = False
        self._fatal_error: TraceWriteError | None = None
        self.queue_peak_size = 0
        self.database_event_count_before_close = 0
        self.recovery_report = RecoveryReport(
            recovered_event_count=0,
            skipped_event_count=0,
            truncated_bytes=0,
        )

    @property
    def pending_event_count(self) -> int:
        """返回尚未收到 SQLite 提交确认的事件数。"""
        return len(self._pending_event_runs)

    async def start(self) -> None:
        """恢复尾部、重建 SQLite，并启动唯一 Writer Task。"""
        if self._started:
            return
        self.paths.prepare()
        self.paths.events_path.touch(mode=0o600, exist_ok=True)
        self.paths.events_path.chmod(0o600)
        scan_result = scan_trace_file(self.paths.events_path, recover_tail=True)
        self.recovery_report = scan_result.report
        self._reducers.clear()
        for located_event in scan_result.located_events:
            record = located_event.record
            reducer = self._reducers.setdefault(
                record.event.run_id, TraceReducer(record.event.run_id)
            )
            reducer.apply(record)
        states = tuple(
            self._reducers[run_id].snapshot() for run_id in sorted(self._reducers)
        )
        self.database.open()
        try:
            self.database.rebuild_indexes(scan_result.located_events, states)
        except Exception:
            self.database.close()
            raise
        self._next_sequence = (
            scan_result.located_events[-1].record.sequence + 1
            if scan_result.located_events
            else 1
        )
        self._queue = asyncio.Queue(maxsize=self.queue_capacity)
        self._scope = ResourceScope("trace.store")
        self._scope.register_connection(self.database, description="Trace SQLite")
        self._writer_task = self._scope.create_task(
            self._writer_loop(), description="Trace 单 Writer"
        )
        self._started = True
        self._accepting = True

    async def emit(self, event: TraceEventEnvelope) -> PersistedTraceEvent:
        """等待单个事件完成 NDJSON 与 SQLite 双重持久化。"""
        records = await self.emit_many((event,))
        return records[0]

    async def emit_many(
        self, events: tuple[TraceEventEnvelope, ...]
    ) -> tuple[PersistedTraceEvent, ...]:
        """通过有界队列提交多个事件并保持调用顺序。"""
        self._ensure_accepting()
        futures: list[asyncio.Future[PersistedTraceEvent]] = []
        try:
            for event in events:
                futures.append(await self._enqueue(event))
        except BaseException:
            if futures:
                await asyncio.gather(*futures, return_exceptions=True)
            raise
        if not futures:
            return ()
        results = await asyncio.gather(*futures)
        return tuple(results)

    def online_state(self, run_id: RunId) -> TraceState:
        """返回当前 Writer 已确认事件的 reducer 快照。"""
        reducer = self._reducers.get(run_id)
        return (reducer or TraceReducer(run_id)).snapshot()

    def replay(self, run_id: RunId) -> TraceReplayResult:
        """从 NDJSON 事实源独立回放指定 run。"""
        return TraceReplayer(self.paths.events_path).replay(run_id)

    def capture_output(
        self,
        *,
        run_id: str,
        event_id: str,
        stdout: str,
        stderr: str,
    ) -> OutputCapture:
        """把完整脱敏输出写入独立 artifact。"""
        return self.artifacts.capture(
            run_id=run_id,
            event_id=event_id,
            stdout=stdout,
            stderr=stderr,
        )

    async def close(self) -> None:
        """停止接收、排空 Writer，并在一秒门禁内归零资源。"""
        if not self._started:
            return
        self._accepting = False
        queue = self._require_queue()
        writer_task = self._writer_task
        scope = self._scope
        if writer_task is None or scope is None:
            raise TraceShutdownError("Trace Store 状态不完整")
        await queue.put(None)
        shutdown_error: Exception | None = None
        try:
            await asyncio.wait_for(
                asyncio.shield(writer_task),
                timeout=self._shutdown_timeout_seconds,
            )
        except TimeoutError as error:
            shutdown_error = TraceShutdownError("Trace Writer 关闭超过时限")
            writer_task.cancel()
            await asyncio.gather(writer_task, return_exceptions=True)
            shutdown_error.__cause__ = error
        self.database_event_count_before_close = self.database.event_count()
        try:
            await scope.close()
            scope.assert_empty()
        except Exception as error:
            if shutdown_error is None:
                shutdown_error = error
        self._started = False
        self._writer_task = None
        self._scope = None
        if shutdown_error is not None:
            raise TraceShutdownError("Trace Store 未满足关闭门禁") from shutdown_error
        if self._fatal_error is not None:
            raise self._fatal_error

    async def _enqueue(
        self, event: TraceEventEnvelope
    ) -> asyncio.Future[PersistedTraceEvent]:
        """脱敏、限制大小、校验父链并放入有界队列。"""
        self._ensure_accepting()
        redacted_event = self._redactor.redact_event(event)
        probe_record = PersistedTraceEvent(
            sequence=max(1, self._next_sequence), event=redacted_event
        )
        serialize_persisted_event(probe_record, max_event_bytes=self._max_event_bytes)
        event_id = redacted_event.event_id
        if event_id in self._pending_event_runs:
            raise TraceWriteError(f"Trace event_id 重复：{event_id}")
        persisted_run_id = self.database.event_run_id(event_id)
        if persisted_run_id is not None:
            raise TraceWriteError(f"Trace event_id 重复：{event_id}")
        parent_event_id = redacted_event.parent_event_id
        if parent_event_id is not None:
            parent_run_id = self._pending_event_runs.get(parent_event_id)
            if parent_run_id is None:
                parent_run_id = self.database.event_run_id(parent_event_id)
            if parent_run_id != redacted_event.run_id:
                raise TraceWriteError("父事件必须先提交且属于同一 run")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[PersistedTraceEvent] = loop.create_future()
        self._pending_event_runs[event_id] = redacted_event.run_id
        queue = self._require_queue()
        try:
            await queue.put(_WriteRequest(event=redacted_event, future=future))
        except BaseException:
            self._pending_event_runs.pop(event_id, None)
            raise
        self.queue_peak_size = max(self.queue_peak_size, queue.qsize())
        return future

    async def _writer_loop(self) -> None:
        """按批写文件、fsync、提交索引，再确认 Future。"""
        queue = self._require_queue()
        with self.paths.events_path.open("ab", buffering=0) as event_file:
            while True:
                request = await queue.get()
                if request is None:
                    return
                batch = [request]
                close_after_batch = False
                while len(batch) < self._batch_size:
                    try:
                        next_request = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if next_request is None:
                        close_after_batch = True
                        break
                    batch.append(next_request)
                try:
                    located_events: list[LocatedTraceEvent] = []
                    serialized_lines: list[bytes] = []
                    byte_offset = event_file.tell()
                    for batch_index, batch_request in enumerate(batch):
                        record = PersistedTraceEvent(
                            sequence=self._next_sequence + batch_index,
                            event=batch_request.event,
                        )
                        serialized = serialize_persisted_event(
                            record, max_event_bytes=self._max_event_bytes
                        )
                        serialized_lines.append(serialized)
                        located_events.append(
                            LocatedTraceEvent(
                                record=record,
                                byte_offset=byte_offset,
                                byte_length=len(serialized),
                            )
                        )
                        byte_offset += len(serialized)
                    for serialized in serialized_lines:
                        event_file.write(serialized)
                    event_file.flush()
                    os.fsync(event_file.fileno())
                    located_tuple = tuple(located_events)
                    self.database.append_events(located_tuple)
                    affected_runs: set[str] = set()
                    for located_event in located_tuple:
                        record = located_event.record
                        reducer = self._reducers.setdefault(
                            record.event.run_id,
                            TraceReducer(record.event.run_id),
                        )
                        reducer.apply(record)
                        affected_runs.add(record.event.run_id)
                    states = tuple(
                        self._reducers[run_id].snapshot()
                        for run_id in sorted(affected_runs)
                    )
                    self.database.update_metrics(states)
                    self._next_sequence += len(batch)
                except Exception as error:
                    failure = TraceWriteError("Trace Writer 持久化批次失败")
                    failure.__cause__ = error
                    self._fatal_error = failure
                    self._accepting = False
                    self._fail_requests(batch, failure)
                    await self._reject_until_shutdown(queue, failure)
                    return
                for batch_request, located_event in zip(
                    batch, located_events, strict=True
                ):
                    self._pending_event_runs.pop(batch_request.event.event_id, None)
                    if not batch_request.future.done():
                        batch_request.future.set_result(located_event.record)
                if close_after_batch:
                    return

    def _fail_requests(
        self, requests: list[_WriteRequest], failure: TraceWriteError
    ) -> None:
        """向当前失败批次传播同一脱敏错误。"""
        for request in requests:
            self._pending_event_runs.pop(request.event.event_id, None)
            if not request.future.done():
                request.future.set_exception(failure)

    async def _reject_until_shutdown(
        self,
        queue: asyncio.Queue[_WriteRequest | None],
        failure: TraceWriteError,
    ) -> None:
        """持续拒绝已通过入口的请求，直至 close 发送哨兵。"""
        while True:
            request = await queue.get()
            if request is None:
                return
            self._fail_requests([request], failure)

    def _ensure_accepting(self) -> None:
        """拒绝未启动、关闭中或已致命失败的写入。"""
        if self._fatal_error is not None:
            raise self._fatal_error
        if not self._started or not self._accepting:
            raise TraceWriteError("Trace Store 尚未启动或正在关闭")

    def _require_queue(self) -> asyncio.Queue[_WriteRequest | None]:
        """返回已初始化队列。"""
        if self._queue is None:
            raise TraceWriteError("Trace Writer 队列尚未初始化")
        return self._queue
