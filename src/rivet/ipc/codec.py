"""严格解析有大小上限的一行一个 JSON IPC 消息。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from pydantic import TypeAdapter, ValidationError

from rivet.contracts.ipc import IPC_PROTOCOL_VERSION, IpcMessage

MAX_IPC_LINE_BYTES = 1024 * 1024
_MESSAGE_ADAPTER = TypeAdapter[IpcMessage](IpcMessage)


@dataclass(frozen=True, slots=True)
class IpcProtocolError(ValueError):
    """保存不会包含原始消息内容的稳定协议错误。"""

    code: str
    summary: str
    request_id: str | None = None

    def __str__(self) -> str:
        """只展示已脱敏摘要。"""
        return self.summary


def decode_ipc_line(line: bytes) -> IpcMessage:
    """解析单行并拒绝空行、超限、错误版本和非契约字段。"""
    if not line or len(line) > MAX_IPC_LINE_BYTES:
        raise IpcProtocolError("ipc.line_size_invalid", "IPC 消息为空或超过上限")
    stripped = line.rstrip(b"\r\n")
    if not stripped or b"\n" in stripped or b"\r" in stripped:
        raise IpcProtocolError("ipc.line_invalid", "IPC 输入必须恰好为一行")
    try:
        raw = cast(object, json.loads(stripped))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IpcProtocolError("ipc.json_invalid", "IPC 输入不是有效 JSON") from error
    request_id = _safe_request_id(raw)
    raw_record = cast(dict[str, object], raw) if isinstance(raw, dict) else None
    if (
        raw_record is not None
        and raw_record.get("protocol_version") != IPC_PROTOCOL_VERSION
    ):
        raise IpcProtocolError(
            "ipc.protocol_mismatch",
            "IPC 协议版本不兼容",
            request_id,
        )
    try:
        return _MESSAGE_ADAPTER.validate_python(raw)
    except ValidationError as error:
        raise IpcProtocolError(
            "ipc.message_invalid",
            "IPC 消息未通过严格契约",
            request_id,
        ) from error


def _safe_request_id(raw: object) -> str | None:
    """仅提取可安全用于错误关联的合法请求 ID。"""
    if not isinstance(raw, dict):
        return None
    record = cast(dict[str, object], raw)
    value = record.get("request_id")
    if not isinstance(value, str) or not value.startswith("request_"):
        return None
    if len(value) > 84 or not all(
        character.isascii()
        and (character.islower() or character.isdigit() or character in "_-")
        for character in value
    ):
        return None
    return value
