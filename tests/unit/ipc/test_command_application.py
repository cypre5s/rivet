"""验证 TUI 命令共用正式 CLI、权限仲裁和取消边界。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from pydantic import JsonValue

import rivet.ipc.command_application as command_application
from rivet.contracts.ipc import IpcRequest
from rivet.ipc.command_application import (
    CommandExecution,
    CommandWorkerApplication,
)
from rivet.ipc.worker import WorkerMethodError


def _request(
    request_id: str,
    method: str,
    params: dict[str, JsonValue] | None = None,
) -> IpcRequest:
    """构造严格 Worker 请求。"""
    return IpcRequest(
        request_id=request_id,
        method=method,
        params=params or {},
    )


@pytest.mark.asyncio
async def test_ask_routes_to_json_cli_and_emits_result(tmp_path: Path) -> None:
    arguments: list[tuple[str, ...]] = []
    events: list[tuple[str, dict[str, JsonValue]]] = []

    async def runner(argv: tuple[str, ...]) -> CommandExecution:
        """返回固定 CLI 成功结果。"""
        arguments.append(argv)
        return CommandExecution(
            0,
            json.dumps(
                {
                    "answer": "回答",
                    "run_id": "run_one",
                    "session_id": "session_one",
                    "usage": {"cost_usd": None, "total_tokens": 12},
                }
            ).encode(),
            b"",
        )

    async def emit(
        event_type: str,
        payload: dict[str, JsonValue],
    ) -> None:
        """捕获应用事件。"""
        events.append((event_type, payload))

    application = CommandWorkerApplication(tmp_path, runner=runner)
    result = await application.handle(
        _request("request_ask", "command.ask", {"query": "解释"}),
        emit=emit,
        cancel_event=asyncio.Event(),
    )

    assert isinstance(result, dict)
    assert result["answer"] == "回答"
    assert "--json" in arguments[0]
    assert arguments[0][-2:] == ("ask", "解释")
    assert any(event_type == "agent.completed" for event_type, _ in events)
    assert any(event_type == "budget.updated" for event_type, _ in events)


@pytest.mark.asyncio
async def test_fix_waits_for_explicit_permission_then_adds_yes(
    tmp_path: Path,
) -> None:
    arguments: list[tuple[str, ...]] = []
    events: list[tuple[str, dict[str, JsonValue]]] = []
    permission_ready = asyncio.Event()

    async def runner(argv: tuple[str, ...]) -> CommandExecution:
        """记录最终 argv 并返回验证失败的隔离事务。"""
        arguments.append(argv)
        return CommandExecution(
            4,
            json.dumps(
                {
                    "evidence_id": "evidence_one",
                    "status": "FAILED",
                    "transaction_id": "tx_one",
                }
            ).encode(),
            b"",
        )

    async def emit(
        event_type: str,
        payload: dict[str, JsonValue],
    ) -> None:
        """捕获权限请求并通知测试协程。"""
        events.append((event_type, payload))
        if event_type == "permission.requested":
            permission_ready.set()

    application = CommandWorkerApplication(tmp_path, runner=runner)
    fix_task = asyncio.create_task(
        application.handle(
            _request("request_fix", "command.fix", {"query": "修复"}),
            emit=emit,
            cancel_event=asyncio.Event(),
        )
    )
    await permission_ready.wait()
    permission_event = next(
        payload
        for event_type, payload in events
        if event_type == "permission.requested"
    )
    permission_id = permission_event["request_id"]
    assert isinstance(permission_id, str)
    await application.handle(
        _request(
            "request_resolve",
            "permission.resolve",
            {"approved": True, "request_id": permission_id},
        ),
        emit=emit,
        cancel_event=asyncio.Event(),
    )

    result = await fix_task

    assert isinstance(result, dict)
    assert arguments[0][-1] == "--yes"
    assert any(event_type == "permission.resolved" for event_type, _ in events)
    assert any(event_type == "verification.completed" for event_type, _ in events)


@pytest.mark.asyncio
async def test_cli_error_is_classified_without_forwarding_stderr(
    tmp_path: Path,
) -> None:
    async def runner(_argv: tuple[str, ...]) -> CommandExecution:
        """返回含分类字段和额外私有文本的 CLI 失败。"""
        return CommandExecution(
            3,
            b"",
            json.dumps(
                {
                    "error": {
                        "code": "provider.api_key_missing",
                        "next_action": "配置环境变量",
                        "summary": "缺少凭据",
                    },
                    "private": "must-not-cross",
                }
            ).encode(),
        )

    async def emit(
        _event_type: str,
        _payload: dict[str, JsonValue],
    ) -> None:
        """丢弃事件。"""

    application = CommandWorkerApplication(tmp_path, runner=runner)
    with pytest.raises(WorkerMethodError) as captured:
        await application.handle(
            _request("request_ask", "command.ask", {"query": "解释"}),
            emit=emit,
            cancel_event=asyncio.Event(),
        )

    assert captured.value.code == "provider.api_key_missing"
    assert "must-not-cross" not in str(captured.value)


@pytest.mark.asyncio
async def test_subprocess_output_is_drained_with_bounded_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ProbeApplication(CommandWorkerApplication):
        """只向测试暴露受保护的默认子进程 runner。"""

        async def run_probe(self, argv: tuple[str, ...]) -> CommandExecution:
            """运行固定测试 argv。"""
            return await self._run_subprocess(argv)

    monkeypatch.setattr(command_application, "MAX_COMMAND_OUTPUT_BYTES", 64)
    application = ProbeApplication(
        tmp_path,
        environment={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )

    with pytest.raises(WorkerMethodError) as captured:
        await application.run_probe(
            (sys.executable, "-c", "print('x' * 4096)"),
        )

    assert captured.value.code == "ipc.command_output_too_large"
