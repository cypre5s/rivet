"""定义带因果链与脱敏边界的 Trace 事件信封。"""

from __future__ import annotations

from typing import cast

from pydantic import JsonValue, field_validator

from rivet.contracts.common import (
    ContractModel,
    EventId,
    EventType,
    RunId,
    SessionId,
    SummaryText,
    Timestamp,
    TransactionId,
)

FORBIDDEN_EVENT_PAYLOAD_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "credentials",
        "env",
        "environment",
        "headers",
        "password",
        "private_key",
        "refresh_token",
        "secret",
    }
)


def _contains_forbidden_payload_key(value: object) -> bool:
    """递归检查环境、认证头和凭据字段，不读取其值。"""
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        for key, nested_value in mapping.items():
            if isinstance(key, str) and key.lower() in FORBIDDEN_EVENT_PAYLOAD_KEYS:
                return True
            if _contains_forbidden_payload_key(nested_value):
                return True
    elif isinstance(value, list):
        items = cast(list[object], value)
        return any(_contains_forbidden_payload_key(item) for item in items)
    return False


class TraceEventEnvelope(ContractModel):
    """保存版本、关联 ID、因果父节点与已脱敏载荷。"""

    event_id: EventId
    event_type: EventType
    timestamp: Timestamp
    run_id: RunId
    session_id: SessionId
    transaction_id: TransactionId | None = None
    parent_event_id: EventId | None = None
    input_summary: SummaryText | None = None
    result_summary: SummaryText | None = None
    payload: dict[str, JsonValue]

    @field_validator("payload")
    @classmethod
    def _reject_secret_fields(
        cls, payload: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        """在事件进入 Trace 之前拒绝可能承载原始凭据的字段。"""
        if _contains_forbidden_payload_key(payload):
            raise ValueError("Trace 事件不得携带凭据或环境字段")
        return payload
