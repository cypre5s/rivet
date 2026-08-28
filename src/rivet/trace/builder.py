"""构造带关联 ID、父因果链与预持久化脱敏的 Trace 事件。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import JsonValue

from rivet.contracts.common import RunId, SessionId, TransactionId
from rivet.contracts.events import TraceEventEnvelope
from rivet.trace.redaction import SecretRedactor

Clock = Callable[[], datetime]
EventIdFactory = Callable[[], str]


def _utc_now() -> datetime:
    """返回带时区的当前 UTC 时间。"""
    return datetime.now(UTC)


def _event_id() -> str:
    """生成满足公共契约的随机事件 ID。"""
    return f"event_{uuid.uuid4().hex}"


class TraceEventBuilder:
    """集中生成事件 ID、时间戳并执行字段级脱敏。"""

    def __init__(
        self,
        *,
        redactor: SecretRedactor | None = None,
        clock: Clock = _utc_now,
        event_id_factory: EventIdFactory = _event_id,
    ) -> None:
        self._redactor = redactor or SecretRedactor()
        self._clock = clock
        self._event_id_factory = event_id_factory

    def build(
        self,
        *,
        event_type: str,
        run_id: RunId,
        session_id: SessionId,
        payload: dict[str, JsonValue],
        transaction_id: TransactionId | None = None,
        parent_event_id: str | None = None,
        input_summary: str | None = None,
        result_summary: str | None = None,
    ) -> TraceEventEnvelope:
        """生成严格事件，并保留 run/session/transaction/parent 关联。"""
        event = TraceEventEnvelope(
            event_id=self._event_id_factory(),
            event_type=event_type,
            timestamp=self._clock(),
            run_id=run_id,
            session_id=session_id,
            transaction_id=transaction_id,
            parent_event_id=parent_event_id,
            input_summary=input_summary,
            result_summary=result_summary,
            payload=self._redactor.redact_payload(payload),
        )
        return self._redactor.redact_event(event)
