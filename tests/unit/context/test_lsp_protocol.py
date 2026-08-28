"""验证 LSP Content-Length framing 与并发 JSON-RPC 调度。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from rivet.context.lsp_client import LspClient
from rivet.context.lsp_models import LspResultError, parse_locations
from rivet.context.lsp_protocol import (
    LspFrameError,
    LspFrameParser,
    encode_lsp_message,
)
from rivet.kernel.resources import ResourceScope


def test_frame_uses_utf8_byte_length_and_supports_fragmentation() -> None:
    message = {"jsonrpc": "2.0", "id": 1, "result": "中文"}
    encoded = encode_lsp_message(message)
    parser = LspFrameParser()

    parsed: list[dict[str, object]] = []
    for byte in encoded:
        parsed.extend(parser.feed(bytes((byte,))))

    assert parsed == [message]
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
    assert encoded.startswith(f"Content-Length: {len(payload)}\r\n\r\n".encode())


def test_frame_parser_reads_multiple_frames_and_rejects_bad_header() -> None:
    first = {"jsonrpc": "2.0", "method": "one"}
    second = {"jsonrpc": "2.0", "method": "two"}
    parser = LspFrameParser()

    assert parser.feed(encode_lsp_message(first) + encode_lsp_message(second)) == [
        first,
        second,
    ]
    with pytest.raises(LspFrameError, match="Content-Length"):
        LspFrameParser().feed(b"Content-Type: application/json\r\n\r\n{}")


def test_frame_parser_rejects_incomplete_message_at_eof() -> None:
    parser = LspFrameParser()

    assert parser.feed(b"Content-Length: 3\r\n\r\n{") == []
    with pytest.raises(LspFrameError, match="完整消息前结束"):
        parser.finish()


@pytest.mark.asyncio
async def test_client_correlates_concurrent_requests_and_answers_server_request(
    tmp_path: Path,
) -> None:
    source = tmp_path / "target.py"
    source.write_text("symbol = 1\n", encoding="utf-8")
    server = Path("tests/fixtures/context/lsp_server.py").resolve()
    scope = ResourceScope("context.lsp.protocol")
    client = await LspClient.start(
        (
            sys.executable,
            str(server),
            "--behavior",
            "normal",
            "--definition-uri",
            source.as_uri(),
        ),
        repository_root=tmp_path,
        scope=scope,
        request_timeout_seconds=2.0,
    )

    initialization = await client.initialize(tmp_path, initialization_options={})
    results = await asyncio.gather(
        *(client.request("echo", {"ordinal": index}) for index in range(20))
    )

    assert initialization["server_request_handled"] is True
    assert results == [{"ordinal": index} for index in range(20)]
    assert any(
        notification.get("method") == "window/logMessage"
        for notification in client.notifications
    )
    await client.shutdown()
    await scope.close()
    scope.assert_empty()


def test_location_link_uses_selection_range_and_rejects_external_uri(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.py"
    target.write_text("symbol = 1\n", encoding="utf-8")
    selection_range = {
        "start": {"line": 0, "character": 0},
        "end": {"line": 0, "character": 6},
    }

    locations = parse_locations(
        [
            {
                "targetUri": target.as_uri(),
                "targetRange": selection_range,
                "targetSelectionRange": selection_range,
            }
        ],
        tmp_path,
    )

    assert [location.path for location in locations] == ["target.py"]
    with pytest.raises(LspResultError, match="仓库边界"):
        parse_locations(
            {"uri": Path("/tmp/outside.py").as_uri(), "range": selection_range},
            tmp_path,
        )
