"""验证 TUI 命令共用正式 CLI、权限仲裁和取消边界。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
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
from rivet.ipc.worker import EmitEvent, WorkerMethodError


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

    async def runner(
        argv: tuple[str, ...],
        _emit: EmitEvent,
    ) -> CommandExecution:
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
async def test_explicit_context_paths_are_validated_and_reach_the_formal_cli(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "service.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    arguments: list[tuple[str, ...]] = []
    events: list[tuple[str, dict[str, JsonValue]]] = []

    async def runner(
        argv: tuple[str, ...],
        _emit: EmitEvent,
    ) -> CommandExecution:
        """记录包含上下文路径的正式 CLI argv。"""
        arguments.append(argv)
        return CommandExecution(0, b"{}", b"")

    async def emit(event_type: str, payload: dict[str, JsonValue]) -> None:
        """捕获显式上下文事件。"""
        events.append((event_type, payload))

    application = CommandWorkerApplication(tmp_path, runner=runner)
    await application.handle(
        _request(
            "request_context",
            "command.ask",
            {"context_paths": ["src/service.py"], "query": "解释服务"},
        ),
        emit=emit,
        cancel_event=asyncio.Event(),
    )

    assert "用户显式选择的仓库文件" in arguments[0][-1]
    assert "@src/service.py" in arguments[0][-1]
    assert (
        "context.selected",
        {"path": "src/service.py", "reason": "用户显式选择"},
    ) in events


@pytest.mark.asyncio
async def test_explicit_context_rejects_symlink_and_control_character_paths(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / "outside-context.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "outside-link.py").symlink_to(outside)

    async def emit(
        _event_type: str,
        _payload: dict[str, JsonValue],
    ) -> None:
        """丢弃拒绝路径时的状态事件。"""

    application = CommandWorkerApplication(tmp_path)
    for path in ("outside-link.py", "bad\x1b-name.py"):
        with pytest.raises(WorkerMethodError) as captured:
            await application.handle(
                _request(
                    f"request_{len(path)}",
                    "command.ask",
                    {"context_paths": [path], "query": "解释"},
                ),
                emit=emit,
                cancel_event=asyncio.Event(),
            )
        assert captured.value.code == "context.path_invalid"


def test_ready_payload_exposes_only_model_and_credential_presence(
    tmp_path: Path,
) -> None:
    application = CommandWorkerApplication(
        tmp_path,
        environment={
            "DEEPSEEK_API_KEY": "configured-placeholder",
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
        },
    )

    payload = application.ready_payload()
    serialized = json.dumps(payload)

    assert payload["credential_configured"] is True
    assert payload["model"] == "deepseek-v4-pro"
    assert "configured-placeholder" not in serialized
    assert "api_key" not in serialized.casefold()


@pytest.mark.asyncio
async def test_workspace_files_is_on_demand_git_aware_and_secret_safe(
    tmp_path: Path,
) -> None:
    subprocess.run(
        ("git", "init", "--quiet", str(tmp_path)),
        check=True,
        capture_output=True,
    )
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("ignored\n", encoding="utf-8")
    (tmp_path / ".env").write_text("placeholder\n", encoding="utf-8")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "config").write_text("placeholder\n", encoding="utf-8")
    (tmp_path / "bad\x1b-name.py").write_text("placeholder\n", encoding="utf-8")
    (tmp_path / "outside-link").symlink_to(tmp_path.parent)
    events: list[tuple[str, dict[str, JsonValue]]] = []

    async def emit(event_type: str, payload: dict[str, JsonValue]) -> None:
        """捕获按需文件树事件。"""
        events.append((event_type, payload))

    application = CommandWorkerApplication(tmp_path, environment={})
    result = await application.handle(
        _request(
            "request_files",
            "workspace.files",
            {"limit": 20, "query": ""},
        ),
        emit=emit,
        cancel_event=asyncio.Event(),
    )

    assert isinstance(result, dict)
    paths = result["paths"]
    assert isinstance(paths, list)
    assert "src/app.py" in paths
    assert "ignored.py" not in paths
    assert ".env" not in paths
    assert ".ssh/config" not in paths
    assert "bad\x1b-name.py" not in paths
    assert "outside-link" not in paths
    assert events[0][0] == "workspace.tree_updated"

    fuzzy_result = await application.handle(
        _request(
            "request_fuzzy_files",
            "workspace.files",
            {"limit": 20, "query": "sap"},
        ),
        emit=emit,
        cancel_event=asyncio.Event(),
    )
    assert isinstance(fuzzy_result, dict)
    fuzzy_paths = fuzzy_result["paths"]
    assert isinstance(fuzzy_paths, list)
    assert "src/app.py" in fuzzy_paths


@pytest.mark.asyncio
async def test_workspace_file_output_is_drained_with_bounded_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(
        ("git", "init", "--quiet", str(tmp_path)),
        check=True,
        capture_output=True,
    )
    for index in range(20):
        (tmp_path / f"long-repository-file-{index:02d}.py").write_text(
            "pass\n",
            encoding="utf-8",
        )

    async def emit(
        _event_type: str,
        _payload: dict[str, JsonValue],
    ) -> None:
        """丢弃有界文件清单事件。"""

    monkeypatch.setattr(command_application, "MAX_COMMAND_OUTPUT_BYTES", 64)
    application = CommandWorkerApplication(tmp_path, environment={})

    with pytest.raises(WorkerMethodError) as captured:
        await application.handle(
            _request("request_files", "workspace.files", {"limit": 20, "query": ""}),
            emit=emit,
            cancel_event=asyncio.Event(),
        )

    assert captured.value.code == "workspace.files_too_large"


@pytest.mark.asyncio
async def test_recent_sessions_are_loaded_only_on_explicit_request(
    tmp_path: Path,
) -> None:
    from rivet.storage.sessions import SessionCheckpoint, SessionStatus, SessionStore

    SessionStore(tmp_path).save(
        SessionCheckpoint(
            session_id="session_recent",
            run_id="run_recent",
            command="ask",
            query="解释",
            status=SessionStatus.COMPLETED,
        )
    )
    events: list[tuple[str, dict[str, JsonValue]]] = []

    async def emit(event_type: str, payload: dict[str, JsonValue]) -> None:
        """捕获按需会话快照。"""
        events.append((event_type, payload))

    application = CommandWorkerApplication(tmp_path)
    result = await application.handle(
        _request("request_sessions", "sessions.list", {"limit": 20}),
        emit=emit,
        cancel_event=asyncio.Event(),
    )

    assert result == {"sessions": ["session_recent"]}
    assert events == [
        (
            "sessions.snapshot",
            {
                "sessions": ["session_recent"],
                "summary": "已加载 1 个近期会话",
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "expected_tail"),
    [
        ("command.read", {"file": "README.md"}, ("read", "README.md")),
        ("command.init", {}, ("init",)),
        ("command.resume", {"session_id": "session_one"}, ("resume", "session_one")),
        ("command.trace", {"run_id": "run_one"}, ("trace", "run_one")),
        ("command.doctor", {}, ("doctor",)),
        ("command.benchmark", {}, ("benchmark",)),
        ("command.config", {}, ("config",)),
        ("command.clean", {}, ("clean",)),
        ("command.verify", {"transaction_id": "tx_one"}, ("verify", "tx_one")),
        ("command.diff", {"transaction_id": "tx_one"}, ("diff", "tx_one")),
        ("command.apply", {"transaction_id": "tx_one"}, ("apply", "tx_one")),
        ("command.abort", {"transaction_id": "tx_one"}, ("abort", "tx_one")),
    ],
)
async def test_published_worker_commands_share_the_formal_cli_parser(
    tmp_path: Path,
    method: str,
    params: dict[str, JsonValue],
    expected_tail: tuple[str, ...],
) -> None:
    arguments: list[tuple[str, ...]] = []

    async def runner(
        argv: tuple[str, ...],
        _emit: EmitEvent,
    ) -> CommandExecution:
        """记录无 shell argv 并返回合法空结果。"""
        arguments.append(argv)
        return CommandExecution(0, b"{}", b"")

    async def emit(
        _event_type: str,
        _payload: dict[str, JsonValue],
    ) -> None:
        """丢弃投影事件。"""

    application = CommandWorkerApplication(tmp_path, runner=runner)
    await application.handle(
        _request("request_command", method, params),
        emit=emit,
        cancel_event=asyncio.Event(),
    )

    assert arguments[0][-len(expected_tail) :] == expected_tail


@pytest.mark.asyncio
async def test_module_commands_use_long_lived_kernel_instead_of_subprocess(
    tmp_path: Path,
) -> None:
    arguments: list[tuple[str, ...]] = []
    events: list[tuple[str, dict[str, JsonValue]]] = []

    async def runner(
        argv: tuple[str, ...],
        _emit: EmitEvent,
    ) -> CommandExecution:
        """记录不应由模块控制调用的子进程。"""
        arguments.append(argv)
        return CommandExecution(0, b"{}", b"")

    async def emit(event_type: str, payload: dict[str, JsonValue]) -> None:
        """捕获模块快照和生命周期事件。"""
        events.append((event_type, payload))

    application = CommandWorkerApplication(tmp_path, runner=runner)
    listed = await application.handle(
        _request("request_modules_list", "command.modules", {"operation": "list"}),
        emit=emit,
        cancel_event=asyncio.Event(),
    )
    woke = await application.handle(
        _request(
            "request_modules_wake",
            "module.operation",
            {"module_id": "context.syntax", "operation": "wake"},
        ),
        emit=emit,
        cancel_event=asyncio.Event(),
    )
    with pytest.raises(WorkerMethodError) as blocked:
        await application.handle(
            _request(
                "request_modules_blocked",
                "module.operation",
                {"module_id": "context.syntax", "operation": "disable"},
            ),
            emit=emit,
            cancel_event=asyncio.Event(),
        )

    assert isinstance(listed, dict)
    assert isinstance(woke, dict)
    assert woke["current_state"] == "ACTIVE"
    assert blocked.value.code == "module.active_dependents"
    assert blocked.value.trace_event_id is not None
    assert arguments == []
    assert any(event_type == "module.operation.completed" for event_type, _ in events)
    blocked_payloads = [
        payload
        for event_type, payload in events
        if event_type == "module.operation.blocked"
    ]
    assert blocked_payloads[-1]["human_message"] == "模块仍有已启用依赖者"
    assert blocked_payloads[-1]["suggested_action"]
    assert blocked_payloads[-1]["blockers"] == ["context.lsp"]
    snapshots = [
        payload for event_type, payload in events if event_type == "modules.snapshot"
    ]
    assert snapshots
    assert isinstance(snapshots[-1]["modules"], list)
    await application.close()
    persisted_trace = (tmp_path / ".rivet" / "trace" / "events.ndjson").read_text(
        encoding="utf-8"
    )
    assert "module.operation.requested" in persisted_trace
    assert "module.operation.completed" in persisted_trace
    assert "request_modules_wake" in persisted_trace


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "error_code"),
    [
        (
            "module.operation",
            {"operation": "reload", "module_id": "context.syntax"},
            "module.operation_invalid",
        ),
        (
            "module.operation",
            {"operation": "wake", "module_id": "../escape"},
            "module.id_invalid",
        ),
        (
            "module.operation",
            {
                "operation": "sleep",
                "module_id": "context.syntax",
                "timeout_seconds": -1,
            },
            "module.timeout_invalid",
        ),
        (
            "module.operation",
            {
                "operation": "enable",
                "module_id": "context.syntax",
                "cascade": True,
            },
            "module.options_invalid",
        ),
        (
            "module.show",
            {"module_id": "context.syntax", "wait": True},
            "module.options_invalid",
        ),
        ("module.show", {}, "module.id_invalid"),
    ],
)
async def test_module_ipc_rejects_unknown_or_malformed_requests(
    tmp_path: Path,
    method: str,
    params: dict[str, JsonValue],
    error_code: str,
) -> None:
    async def emit(_event_type: str, _payload: dict[str, JsonValue]) -> None:
        """丢弃本测试不关心的事件。"""

    application = CommandWorkerApplication(tmp_path)
    try:
        with pytest.raises(WorkerMethodError) as captured:
            await application.handle(
                _request("request_modules_invalid", method, params),
                emit=emit,
                cancel_event=asyncio.Event(),
            )
        assert captured.value.code == error_code
    finally:
        await application.close()


@pytest.mark.asyncio
async def test_module_persistence_survives_tui_disconnect_after_commit(
    tmp_path: Path,
) -> None:
    async def disconnected_emit(
        _event_type: str,
        _payload: dict[str, JsonValue],
    ) -> None:
        """模拟 TUI 在操作过程中断开。"""
        raise BrokenPipeError("fixture disconnected")

    application = CommandWorkerApplication(tmp_path)
    with pytest.raises(BrokenPipeError):
        await application.handle(
            _request(
                "request_modules_disconnect",
                "module.operation",
                {"operation": "disable", "module_id": "context.lsp"},
            ),
            emit=disconnected_emit,
            cancel_event=asyncio.Event(),
        )
    await application.close()

    async def emit(_event_type: str, _payload: dict[str, JsonValue]) -> None:
        """丢弃恢复查询的状态投影。"""

    restarted = CommandWorkerApplication(tmp_path)
    try:
        shown = await restarted.handle(
            _request(
                "request_modules_after_disconnect",
                "module.show",
                {"module_id": "context.lsp"},
            ),
            emit=emit,
            cancel_event=asyncio.Event(),
        )
        assert isinstance(shown, dict)
        module = shown["module"]
        assert isinstance(module, dict)
        assert module["persisted_override"] is False
        assert module["effective_enabled"] is False
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_fix_waits_for_explicit_permission_then_adds_yes(
    tmp_path: Path,
) -> None:
    arguments: list[tuple[str, ...]] = []
    events: list[tuple[str, dict[str, JsonValue]]] = []
    permission_ready = asyncio.Event()

    async def runner(
        argv: tuple[str, ...],
        _emit: EmitEvent,
    ) -> CommandExecution:
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
    async def runner(
        _argv: tuple[str, ...],
        _emit: EmitEvent,
    ) -> CommandExecution:
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

            async def discard(
                _event_type: str,
                _payload: dict[str, JsonValue],
            ) -> None:
                """丢弃探针事件。"""

            return await self._run_subprocess(argv, discard)

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


@pytest.mark.asyncio
async def test_subprocess_trace_is_emitted_before_command_completion(
    tmp_path: Path,
) -> None:
    class ProbeApplication(CommandWorkerApplication):
        """只向测试暴露带实时 Trace 的默认 runner。"""

        async def run_probe(
            self,
            argv: tuple[str, ...],
            emit: EmitEvent,
        ) -> CommandExecution:
            """运行固定测试 argv。"""
            return await self._run_subprocess(argv, emit)

    event_seen = asyncio.Event()
    events: list[str] = []

    async def emit(
        event_type: str,
        _payload: dict[str, JsonValue],
    ) -> None:
        """记录命令仍在运行时到达的投影事件。"""
        events.append(event_type)
        event_seen.set()

    script = (
        "import json,os,time; from pathlib import Path; "
        "p=Path('.rivet/trace/events.ndjson'); p.parent.mkdir(parents=True); "
        "e={'schema_version':1,'sequence':1,'event':"
        "{'event_type':'context.selected','run_id':'run_stream_test',"
        "'payload':{'path':'a.py','stream_id':os.environ['RIVET_STREAM_ID']},"
        "'result_summary':'已选择上下文'}}; "
        "p.write_text(json.dumps(e)+'\\n',encoding='utf-8'); "
        "time.sleep(0.3); print('{}')"
    )
    application = ProbeApplication(
        tmp_path,
        environment={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    running = asyncio.create_task(
        application.run_probe((sys.executable, "-c", script), emit)
    )

    await asyncio.wait_for(event_seen.wait(), timeout=1)
    assert not running.done()
    execution = await running

    assert execution.return_code == 0
    assert events == ["context.selected"]
