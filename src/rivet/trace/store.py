"""以单 Writer、NDJSON 和 fsync 提供最小耐久 Trace。"""

from __future__ import annotations

import asyncio
import fcntl
import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from pydantic import ValidationError

from rivet.contracts.events import TraceEventEnvelope
from rivet.kernel.resources import ResourceScope
from rivet.trace.errors import (
    CorruptTraceError,
    TraceShutdownError,
    TraceWriteError,
)
from rivet.trace.models import (
    DEFAULT_MAX_EVENT_BYTES,
    PersistedTraceEvent,
    RecoveryReport,
    TraceScan,
    serialize_persisted_event,
)
from rivet.trace.paths import RuntimePaths
from rivet.trace.redaction import SecretRedactor


@dataclass(slots=True)
class _WriteRequest:
    event: TraceEventEnvelope
    future: asyncio.Future[PersistedTraceEvent]


class TraceStore:
    """NDJSON 是唯一事实源；事件成功返回前已 append、flush、fsync。"""

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        redactor: SecretRedactor | None = None,
        queue_capacity: int = 256,
        batch_size: int = 64,
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
        self._queue: asyncio.Queue[_WriteRequest | None] | None = None
        self._scope: ResourceScope | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._lock_file: BinaryIO | None = None
        self._events: list[PersistedTraceEvent] = []
        self._event_runs: dict[str, str] = {}
        self._pending_event_runs: dict[str, str] = {}
        self._next_sequence = 1
        self._started = False
        self._accepting = False
        self._fatal_error: TraceWriteError | None = None
        self.queue_peak_size = 0
        self.recovery_report = RecoveryReport(
            recovered_event_count=0,
            truncated_bytes=0,
        )

    @property
    def pending_event_count(self) -> int:
        return len(self._pending_event_runs)

    @property
    def event_count(self) -> int:
        return len(self._events)

    async def start(self) -> None:
        """获得仓库级独占锁、恢复尾部并启动唯一 Writer。"""
        if self._started:
            return
        self.paths.prepare()
        self.paths.events_path.touch(mode=0o600, exist_ok=True)
        self.paths.events_path.chmod(0o600)
        lock_path = self.paths.events_path.with_suffix(".lock")
        lock_path.touch(mode=0o600, exist_ok=True)
        lock_file = lock_path.open("a+b", buffering=0)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            lock_file.close()
            raise TraceWriteError("当前仓库已有 Trace Writer") from error
        try:
            scan = _scan_and_recover_tail(
                self.paths.events_path,
                max_event_bytes=self._max_event_bytes,
            )
        except BaseException:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
            raise
        self._lock_file = lock_file
        self._events = list(scan.events)
        self._event_runs = {
            item.event.event_id: item.event.run_id for item in scan.events
        }
        self._next_sequence = len(scan.events) + 1
        self.recovery_report = scan.report
        self._queue = asyncio.Queue(maxsize=self.queue_capacity)
        self._scope = ResourceScope("trace.ndjson")
        self._writer_task = self._scope.create_task(
            self._writer_loop(), description="Trace NDJSON 单 Writer"
        )
        self._started = True
        self._accepting = True

    async def emit(self, event: TraceEventEnvelope) -> PersistedTraceEvent:
        records = await self.emit_many((event,))
        return records[0]

    async def emit_many(
        self,
        events: tuple[TraceEventEnvelope, ...],
    ) -> tuple[PersistedTraceEvent, ...]:
        """保持调用顺序入队；每个 Future 只在整批 fsync 后完成。"""
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
        return tuple(await asyncio.gather(*futures))

    def events(self, run_id: str | None = None) -> tuple[PersistedTraceEvent, ...]:
        """从当前已 fsync 的事实中返回稳定快照。"""
        if run_id is None:
            return tuple(self._events)
        return tuple(item for item in self._events if item.event.run_id == run_id)

    def event(self, event_id: str) -> PersistedTraceEvent | None:
        for item in self._events:
            if item.event.event_id == event_id:
                return item
        return None

    async def close(self) -> None:
        """停止接收、排空队列、关闭资源并释放跨进程锁。"""
        if not self._started:
            return
        self._accepting = False
        queue = self._require_queue()
        writer_task = self._writer_task
        scope = self._scope
        if writer_task is None or scope is None:
            raise TraceShutdownError("Trace Store 状态不完整")
        await queue.put(None)
        shutdown_error: BaseException | None = None
        try:
            await asyncio.wait_for(
                asyncio.shield(writer_task),
                timeout=self._shutdown_timeout_seconds,
            )
        except TimeoutError as error:
            writer_task.cancel()
            await asyncio.gather(writer_task, return_exceptions=True)
            shutdown_error = error
        try:
            await scope.close()
            scope.assert_empty()
        except BaseException as error:
            if shutdown_error is None:
                shutdown_error = error
        lock_file = self._lock_file
        self._lock_file = None
        if lock_file is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        self._started = False
        self._writer_task = None
        self._scope = None
        if shutdown_error is not None:
            raise TraceShutdownError("Trace Store 未满足关闭门禁") from shutdown_error
        if self._fatal_error is not None:
            raise self._fatal_error

    async def _enqueue(
        self,
        event: TraceEventEnvelope,
    ) -> asyncio.Future[PersistedTraceEvent]:
        self._ensure_accepting()
        redacted = self._redactor.redact_event(event)
        serialize_persisted_event(
            PersistedTraceEvent(sequence=self._next_sequence, event=redacted),
            max_event_bytes=self._max_event_bytes,
        )
        event_id = redacted.event_id
        if event_id in self._event_runs or event_id in self._pending_event_runs:
            raise TraceWriteError(f"Trace event_id 重复：{event_id}")
        parent_id = redacted.parent_event_id
        if parent_id is not None:
            parent_run = self._pending_event_runs.get(parent_id)
            if parent_run is None:
                parent_run = self._event_runs.get(parent_id)
            if parent_run != redacted.run_id:
                raise TraceWriteError("父事件必须先进入队列且属于同一 run")
        future: asyncio.Future[PersistedTraceEvent] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending_event_runs[event_id] = redacted.run_id
        try:
            queue = self._require_queue()
            await queue.put(_WriteRequest(redacted, future))
        except BaseException:
            self._pending_event_runs.pop(event_id, None)
            raise
        self.queue_peak_size = max(self.queue_peak_size, queue.qsize())
        return future

    async def _writer_loop(self) -> None:
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
                durable_offset = event_file.tell()
                try:
                    records = tuple(
                        PersistedTraceEvent(
                            sequence=self._next_sequence + index,
                            event=item.event,
                        )
                        for index, item in enumerate(batch)
                    )
                    for record in records:
                        event_file.write(
                            serialize_persisted_event(
                                record,
                                max_event_bytes=self._max_event_bytes,
                            )
                        )
                    event_file.flush()
                    os.fsync(event_file.fileno())
                except BaseException as error:
                    rollback_error = _discard_uncommitted_tail(
                        event_file,
                        durable_offset=durable_offset,
                    )
                    failure = TraceWriteError("Trace NDJSON append/fsync 失败")
                    failure.__cause__ = error
                    if rollback_error is not None:
                        failure.add_note("Trace 未确认耐久尾部回滚也失败；状态不可信")
                    self._fatal_error = failure
                    self._accepting = False
                    self._fail_requests(batch, failure)
                    await self._reject_until_shutdown(queue, failure)
                    return
                self._next_sequence += len(records)
                for item, record in zip(batch, records, strict=True):
                    self._pending_event_runs.pop(item.event.event_id, None)
                    self._event_runs[item.event.event_id] = item.event.run_id
                    self._events.append(record)
                    if not item.future.done():
                        item.future.set_result(record)
                if close_after_batch:
                    return

    def _fail_requests(
        self,
        requests: list[_WriteRequest],
        failure: TraceWriteError,
    ) -> None:
        for request in requests:
            self._pending_event_runs.pop(request.event.event_id, None)
            if not request.future.done():
                request.future.set_exception(failure)

    async def _reject_until_shutdown(
        self,
        queue: asyncio.Queue[_WriteRequest | None],
        failure: TraceWriteError,
    ) -> None:
        while True:
            request = await queue.get()
            if request is None:
                return
            self._fail_requests([request], failure)

    def _ensure_accepting(self) -> None:
        if self._fatal_error is not None:
            raise self._fatal_error
        if not self._started or not self._accepting:
            raise TraceWriteError("Trace Store 尚未启动或正在关闭")

    def _require_queue(self) -> asyncio.Queue[_WriteRequest | None]:
        if self._queue is None:
            raise TraceWriteError("Trace Writer 队列尚未初始化")
        return self._queue


def _scan_and_recover_tail(
    path: Path,
    *,
    max_event_bytes: int,
) -> TraceScan:
    """只截断无换行的尾部半条；完整损坏与序列/父链错误失败关闭。"""
    data = path.read_bytes()
    events: list[PersistedTraceEvent] = []
    event_runs: dict[str, str] = {}
    offset = 0
    truncate_at: int | None = None
    lines = data.splitlines(keepends=True)
    for index, line in enumerate(lines):
        is_last = index == len(lines) - 1
        complete = line.endswith(b"\n")
        if not complete:
            if not is_last:
                raise CorruptTraceError("Trace 中部事件不完整")
            truncate_at = offset
            break
        if len(line) > max_event_bytes:
            raise CorruptTraceError("Trace 包含超大完整事件")
        try:
            record = PersistedTraceEvent.model_validate_json(line)
        except (ValidationError, ValueError):
            if not is_last:
                raise CorruptTraceError("Trace 中部事件损坏") from None
            raise CorruptTraceError("Trace 完整事件损坏") from None
        if record.sequence != len(events) + 1:
            raise CorruptTraceError("Trace sequence 不连续")
        event = record.event
        if event.event_id in event_runs:
            raise CorruptTraceError("Trace event_id 重复")
        if event.parent_event_id is not None and (
            event_runs.get(event.parent_event_id) != event.run_id
        ):
            raise CorruptTraceError("Trace 父事件缺失、乱序或跨 run")
        events.append(record)
        event_runs[event.event_id] = event.run_id
        offset += len(line)
    if truncate_at is not None:
        with path.open("r+b") as stream:
            stream.truncate(truncate_at)
            stream.flush()
            os.fsync(stream.fileno())
    truncated = len(data) - (truncate_at if truncate_at is not None else len(data))
    return TraceScan(
        events=tuple(events),
        report=RecoveryReport(
            recovered_event_count=len(events),
            truncated_bytes=truncated,
        ),
    )


def _discard_uncommitted_tail(
    event_file: BinaryIO,
    *,
    durable_offset: int,
) -> BaseException | None:
    """尽力移除未获得 fsync 确认的完整行，防止重启误认为事实。"""
    try:
        event_file.truncate(durable_offset)
        event_file.flush()
        os.fsync(event_file.fileno())
    except BaseException as error:
        return error
    return None
