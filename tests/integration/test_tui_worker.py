"""以真实子进程验证 Worker 握手、请求、取消和 stdout 纯度。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import cast


def _request(request_id: str, method: str, params: dict[str, object]) -> str:
    """生成严格 IPC 请求行。"""
    return json.dumps(
        {
            "schema_version": 1,
            "message_type": "request",
            "protocol_version": 1,
            "request_id": request_id,
            "method": method,
            "params": params,
        },
        separators=(",", ":"),
    )


def test_stdio_worker_requires_handshake_and_keeps_stdout_ndjson(
    tmp_path: Path,
) -> None:
    lines = (
        _request("request_handshake", "worker.handshake", {"client": "test"}),
        _request("request_ping", "worker.ping", {}),
        _request("request_snapshot", "worker.snapshot", {}),
        _request("request_shutdown", "worker.shutdown", {}),
    )

    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "rivet",
            "internal",
            "worker",
            "--stdio",
            "--repository",
            str(tmp_path),
        ),
        input=("\n".join(lines) + "\n").encode(),
        capture_output=True,
        timeout=5,
        check=False,
    )

    messages = [
        cast(dict[str, object], json.loads(line))
        for line in completed.stdout.decode().splitlines()
    ]
    response_ids = {
        message["request_id"]
        for message in messages
        if message.get("message_type") == "response"
    }
    assert completed.returncode == 0
    assert completed.stderr == b""
    assert response_ids == {
        "request_handshake",
        "request_ping",
        "request_snapshot",
        "request_shutdown",
    }
    assert all(message.get("protocol_version") == 1 for message in messages)
    assert any(message.get("event_type") == "worker.ready" for message in messages)


def test_stdio_worker_rejects_request_before_handshake(tmp_path: Path) -> None:
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "rivet",
            "internal",
            "worker",
            "--stdio",
            "--repository",
            str(tmp_path),
        ),
        input=(_request("request_early", "worker.ping", {}) + "\n").encode(),
        capture_output=True,
        timeout=5,
        check=False,
    )

    response = cast(dict[str, object], json.loads(completed.stdout.decode()))
    error = cast(dict[str, object], response["error"])
    assert completed.returncode == 0
    assert response["ok"] is False
    assert error["code"] == "ipc.handshake_required"
