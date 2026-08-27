"""验证 SSE 在任意网络与 UTF-8 分片下稳定解析。"""

from __future__ import annotations

from rivet.providers.sse import SSEDecoder


def test_decoder_handles_every_byte_as_chunk_and_unicode() -> None:
    encoded = "data: 你\r\ndata: 好\r\n\r\ndata: [DONE]\n\n".encode()
    decoder = SSEDecoder()
    events: list[str] = []

    for byte in encoded:
        events.extend(decoder.feed(bytes((byte,))))
    events.extend(decoder.finalize())

    assert events == ["你\n好", "[DONE]"]


def test_decoder_ignores_comments_and_non_data_fields() -> None:
    decoder = SSEDecoder()

    events = decoder.feed(b": keepalive\nid: 1\ndata: value\n\n")

    assert events == ("value",)
