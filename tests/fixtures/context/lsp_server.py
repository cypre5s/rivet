"""实现测试专用、协议真实的最小 LSP stdio 服务器。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import cast


def _read_message() -> dict[str, object] | None:
    """读取单个 Content-Length 帧。"""
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line == b"\r\n":
            break
        name, value = line.decode("ascii").split(":", maxsplit=1)
        headers[name.casefold()] = value.strip()
    content_length = int(headers["content-length"])
    payload = sys.stdin.buffer.read(content_length)
    return cast(dict[str, object], json.loads(payload))


def _write_message(message: dict[str, object]) -> None:
    """发送单个紧凑 UTF-8 JSON-RPC 帧。"""
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def _build_parser() -> argparse.ArgumentParser:
    """构造崩溃注入和 Definition 目标参数。"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--behavior",
        choices=("normal", "crash-once", "crash-always", "no-response"),
    )
    parser.add_argument("--marker", type=Path)
    parser.add_argument("--definition-uri", required=True)
    return parser


def main() -> int:
    """处理测试覆盖的生命周期、请求、通知与服务端请求。"""
    arguments = _build_parser().parse_args()
    pending_initialize_id: object | None = None
    while message := _read_message():
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            pending_initialize_id = request_id
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": "server-config",
                    "method": "workspace/configuration",
                    "params": {"items": [{"section": "fixture"}]},
                }
            )
            continue
        if request_id == "server-config" and pending_initialize_id is not None:
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": pending_initialize_id,
                    "result": {
                        "capabilities": {
                            "definitionProvider": True,
                            "referencesProvider": True,
                            "documentSymbolProvider": True,
                        },
                        "server_request_handled": message.get("result") == [None],
                    },
                }
            )
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "method": "window/logMessage",
                    "params": {"type": 3, "message": "fixture ready"},
                }
            )
            pending_initialize_id = None
            continue
        if method in {"initialized", "textDocument/didOpen"}:
            continue
        if method == "echo":
            _write_message(
                {"jsonrpc": "2.0", "id": request_id, "result": message.get("params")}
            )
            continue
        if method == "textDocument/definition":
            if arguments.behavior == "no-response":
                continue
            if arguments.behavior == "crash-always":
                return 17
            if (
                arguments.behavior == "crash-once"
                and arguments.marker is not None
                and not arguments.marker.exists()
            ):
                arguments.marker.write_text("crashed", encoding="utf-8")
                return 17
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "uri": arguments.definition_uri,
                        "range": {
                            "start": {"line": 0, "character": 0},
                            "end": {"line": 0, "character": 6},
                        },
                    },
                }
            )
            continue
        if method == "textDocument/references":
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": [
                        {
                            "uri": arguments.definition_uri,
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 6},
                            },
                        }
                    ],
                }
            )
            continue
        if method == "textDocument/documentSymbol":
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": [
                        {
                            "name": "symbol",
                            "kind": 12,
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 6},
                            },
                            "selectionRange": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 6},
                            },
                        }
                    ],
                }
            )
            continue
        if method == "shutdown":
            _write_message({"jsonrpc": "2.0", "id": request_id, "result": None})
            continue
        if method == "exit":
            return 0
        if request_id is not None:
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "method not found"},
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
