"""在事件、输出和异常进入持久层前移除常见秘密。"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from pydantic import JsonValue

from rivet.contracts.events import (
    FORBIDDEN_EVENT_PAYLOAD_KEYS,
    TraceEventEnvelope,
)

REDACTED_TEXT = "[REDACTED]"
SENSITIVE_ENVIRONMENT_NAME = re.compile(
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH|CREDENTIAL)", re.IGNORECASE
)
STATIC_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:ghp_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"password|passwd|secret)\s*[:=]\s*[^\s,;]+"
    ),
)


class SecretRedactor:
    """组合固定模式与敏感环境变量值执行确定性脱敏。"""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        selected_environment = os.environ if environment is None else environment
        self._environment_secrets = tuple(
            sorted(
                {
                    value
                    for name, value in selected_environment.items()
                    if SENSITIVE_ENVIRONMENT_NAME.search(name) and len(value) >= 4
                },
                key=len,
                reverse=True,
            )
        )

    def redact_text(self, text: str) -> str:
        """按最长环境值和固定 token 模式替换秘密文本。"""
        redacted = text
        for secret_value in self._environment_secrets:
            redacted = redacted.replace(secret_value, REDACTED_TEXT)
        for pattern in STATIC_SECRET_PATTERNS:
            redacted = pattern.sub(REDACTED_TEXT, redacted)
        return redacted

    def redact_payload(self, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """递归删除禁止字段，并保留不含值的字段名审计清单。"""
        return self._redact_mapping(payload)

    def redact_event(self, event: TraceEventEnvelope) -> TraceEventEnvelope:
        """重建严格事件，确保摘要和载荷都经过脱敏。"""
        return TraceEventEnvelope(
            event_id=event.event_id,
            event_type=event.event_type,
            timestamp=event.timestamp,
            run_id=event.run_id,
            session_id=event.session_id,
            transaction_id=event.transaction_id,
            parent_event_id=event.parent_event_id,
            input_summary=(
                self.redact_text(event.input_summary)
                if event.input_summary is not None
                else None
            ),
            result_summary=(
                self.redact_text(event.result_summary)
                if event.result_summary is not None
                else None
            ),
            payload=self.redact_payload(event.payload),
        )

    def _redact_mapping(self, mapping: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """递归处理 JSON 对象并防止审计字段被输入覆盖。"""
        redacted: dict[str, JsonValue] = {}
        removed_fields: list[str] = []
        for key, value in mapping.items():
            if key.lower() in FORBIDDEN_EVENT_PAYLOAD_KEYS:
                removed_fields.append(key.lower())
                continue
            if key == "_redacted_fields":
                continue
            redacted[key] = self._redact_value(value)
        if removed_fields:
            redacted["_redacted_fields"] = [
                field_name for field_name in sorted(set(removed_fields))
            ]
        return redacted

    def _redact_value(self, value: JsonValue) -> JsonValue:
        """在 JSON 递归结构中只转换字符串和映射。"""
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        if isinstance(value, dict):
            return self._redact_mapping(value)
        return value
