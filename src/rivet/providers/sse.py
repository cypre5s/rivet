"""增量解析任意字节和 UTF-8 分片的 Server-Sent Events。"""

from __future__ import annotations

import codecs

from rivet.providers.errors import ProviderProtocolError


class SSEDecoder:
    """按 SSE 空行边界聚合多个 data 字段。"""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._text_buffer = ""
        self._data_lines: list[str] = []

    def feed(self, chunk: bytes) -> tuple[str, ...]:
        """输入任意字节 chunk 并返回本次完成的数据事件。"""
        try:
            self._text_buffer += self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError as error:
            raise ProviderProtocolError(
                "provider.sse_utf8_invalid",
                "模型 SSE 包含无效 UTF-8",
                retryable=False,
            ) from error
        return self._consume_lines(final=False)

    def finalize(self) -> tuple[str, ...]:
        """完成 UTF-8 解码，并拒绝没有事件边界的残余数据。"""
        try:
            self._text_buffer += self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as error:
            raise ProviderProtocolError(
                "provider.sse_utf8_incomplete",
                "模型 SSE 以不完整 UTF-8 结束",
                retryable=True,
            ) from error
        events = self._consume_lines(final=True)
        if self._text_buffer or self._data_lines:
            raise ProviderProtocolError(
                "provider.sse_event_incomplete",
                "模型 SSE 连接在完整事件边界前结束",
                retryable=True,
            )
        return events

    def _consume_lines(self, *, final: bool) -> tuple[str, ...]:
        """逐行处理 CRLF/LF，并在空行提交 data 内容。"""
        events: list[str] = []
        while "\n" in self._text_buffer:
            line, self._text_buffer = self._text_buffer.split("\n", maxsplit=1)
            if line.endswith("\r"):
                line = line[:-1]
            if not line:
                if self._data_lines:
                    events.append("\n".join(self._data_lines))
                    self._data_lines.clear()
                continue
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if field != "data":
                continue
            if separator and value.startswith(" "):
                value = value[1:]
            self._data_lines.append(value)
        if final and self._text_buffer == "":
            return tuple(events)
        return tuple(events)
