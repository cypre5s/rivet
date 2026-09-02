"""以真实 stdio 子进程验证最小 Worker 协议与 stdout 纯度。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import cast

from rivet.contracts.ipc import IPC_APPLICATION_METHODS


def _request(request_id: str, method: str, params: dict[str, object]) -> str:
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


def _run_worker(
    tmp_path: Path, lines: tuple[str, ...]
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
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


def test_stdio_worker_handshake_shutdown_and_stdout_are_strict_ndjson(
    tmp_path: Path,
) -> None:
    completed = _run_worker(
        tmp_path,
        (
            _request("request_handshake", "worker.handshake", {"client": "test"}),
            _request("request_shutdown", "worker.shutdown", {}),
        ),
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
    handshake = next(
        message
        for message in messages
        if message.get("request_id") == "request_handshake"
    )
    result = cast(dict[str, object], handshake["result"])

    assert completed.returncode == 0
    assert completed.stderr == b""
    assert response_ids == {"request_handshake", "request_shutdown"}
    assert result["methods"] == list(IPC_APPLICATION_METHODS)
    assert all(message.get("protocol_version") == 1 for message in messages)
    assert any(message.get("event_type") == "worker.ready" for message in messages)


def test_stdio_worker_rejects_every_request_before_handshake(tmp_path: Path) -> None:
    completed = _run_worker(
        tmp_path,
        (_request("request_early", "command.diff", {}),),
    )

    response = cast(dict[str, object], json.loads(completed.stdout.decode()))
    error = cast(dict[str, object], response["error"])
    assert completed.returncode == 0
    assert response["ok"] is False
    assert error["code"] == "ipc.handshake_required"


def test_stdio_worker_rejects_removed_peripheral_method(tmp_path: Path) -> None:
    completed = _run_worker(
        tmp_path,
        (
            _request("request_handshake", "worker.handshake", {}),
            _request("request_removed", "sessions.list", {}),
            _request("request_shutdown", "worker.shutdown", {}),
        ),
    )

    messages = [
        cast(dict[str, object], json.loads(line))
        for line in completed.stdout.decode().splitlines()
    ]
    removed = next(
        message
        for message in messages
        if message.get("request_id") == "request_removed"
    )
    assert cast(dict[str, object], removed["error"])["code"] == ("ipc.method_unknown")
