"""实现 LSP 3.17 stdio 的 Content-Length 增量 framing。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

DEFAULT_MAX_HEADER_BYTES = 16 * 1024
DEFAULT_MAX_MESSAGE_BYTES = 16 * 1024 * 1024


class LspFrameError(RuntimeError):
    """表示 framing、编码或 JSON 消息不满足协议边界。"""


def encode_lsp_message(message: Mapping[str, object]) -> bytes:
    """按 UTF-8 字节长度编码紧凑 JSON-RPC 消息。"""
    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise LspFrameError("LSP 消息无法编码为 JSON") from error
    return f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload


class LspFrameParser:
    """跨任意字节分片解析一个或多个有界 LSP 消息。"""

    def __init__(
        self,
        *,
        max_header_bytes: int = DEFAULT_MAX_HEADER_BYTES,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    ) -> None:
        if max_header_bytes <= 0 or max_message_bytes <= 0:
            raise ValueError("LSP header 与消息大小上限必须大于零")
        self._max_header_bytes = max_header_bytes
        self._max_message_bytes = max_message_bytes
        self._buffer = bytearray()
        self._expected_content_length: int | None = None

    def feed(self, chunk: bytes) -> list[dict[str, object]]:
        """加入一个分片并返回当前已完整解析的消息。"""
        self._buffer.extend(chunk)
        messages: list[dict[str, object]] = []
        while True:
            if self._expected_content_length is None:
                header_end = self._buffer.find(b"\r\n\r\n")
                if header_end < 0:
                    if len(self._buffer) > self._max_header_bytes:
                        raise LspFrameError("LSP header 超出大小上限")
                    break
                if header_end > self._max_header_bytes:
                    raise LspFrameError("LSP header 超出大小上限")
                header = bytes(self._buffer[:header_end])
                del self._buffer[: header_end + 4]
                self._expected_content_length = self._parse_header(header)
            expected = self._expected_content_length
            if len(self._buffer) < expected:
                break
            payload = bytes(self._buffer[:expected])
            del self._buffer[:expected]
            self._expected_content_length = None
            messages.append(self._parse_payload(payload))
        return messages

    def finish(self) -> None:
        """在 EOF 处拒绝未完成的 header 或正文。"""
        if self._buffer or self._expected_content_length is not None:
            raise LspFrameError("LSP 流在完整消息前结束")

    def _parse_header(self, header: bytes) -> int:
        """解析 ASCII header 并要求唯一 Content-Length。"""
        try:
            lines = header.decode("ascii", errors="strict").split("\r\n")
        except UnicodeDecodeError as error:
            raise LspFrameError("LSP header 必须是 ASCII") from error
        headers: dict[str, str] = {}
        for line in lines:
            if ":" not in line:
                raise LspFrameError("LSP header 行格式无效")
            name, value = line.split(":", maxsplit=1)
            normalized_name = name.strip().casefold()
            if normalized_name in headers:
                raise LspFrameError("LSP header 字段不得重复")
            headers[normalized_name] = value.strip()
        raw_length = headers.get("content-length")
        if raw_length is None or not raw_length.isascii() or not raw_length.isdecimal():
            raise LspFrameError("LSP header 缺少有效 Content-Length")
        content_length = int(raw_length)
        if content_length <= 0 or content_length > self._max_message_bytes:
            raise LspFrameError("LSP Content-Length 超出允许范围")
        return content_length

    @staticmethod
    def _parse_payload(payload: bytes) -> dict[str, object]:
        """严格解码 UTF-8 JSON object 与 JSON-RPC 版本。"""
        try:
            raw_message: object = json.loads(payload.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LspFrameError("LSP payload 不是有效 UTF-8 JSON") from error
        if not isinstance(raw_message, dict):
            raise LspFrameError("LSP payload 根节点必须是对象")
        message = cast(dict[str, object], raw_message)
        if message.get("jsonrpc") != "2.0":
            raise LspFrameError("LSP 消息必须使用 JSON-RPC 2.0")
        return message
