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
                    "status": "ANSWERED",
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
    answered = next(
        payload for event_type, payload in events if event_type == "agent.answered"
    )
    assert answered["status"] == "ANSWERED"
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
    assert payload["models"] == ["deepseek-v4-pro", "deepseek-v4-flash"]
    assert payload["base_url"] == "https://api.deepseek.com"
    assert payload["max_rounds"] == 24
    assert payload["max_total_tokens"] == 128_000
    assert isinstance(payload["sources"], dict)
    assert "configured-placeholder" not in serialized
    assert "api_key" not in serialized.casefold()


@pytest.mark.asyncio
async def test_tui_runtime_configuration_updates_models_and_session_key_without_echo(
    tmp_path: Path,
) -> None:
    caller_environment = {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    events: list[tuple[str, dict[str, JsonValue]]] = []

    async def emit(event_type: str, payload: dict[str, JsonValue]) -> None:
        events.append((event_type, payload))

    application = CommandWorkerApplication(
        tmp_path,
        environment=caller_environment,
    )
    secret = "fixture-session-value-that-must-never-be-echoed"
    result = await application.handle(
        _request(
            "request_config_update",
            "config.update",
            {
                "api_key_action": "replace",
                "api_key": secret,
                "base_url": "https://gateway.example.test/v1",
                "max_cost_usd": "2.50",
                "max_rounds": 18,
                "max_total_tokens": 64_000,
                "model": "reasoner-large",
                "models": ["chat-fast", "reasoner-large"],
                "safe_mode": True,
            },
        ),
        emit=emit,
        cancel_event=asyncio.Event(),
    )

    serialized = json.dumps({"result": result, "events": events})
    assert isinstance(result, dict)
    assert result["credential_configured"] is True
    assert result["model"] == "reasoner-large"
    assert result["models"] == ["chat-fast", "reasoner-large"]
    assert result["base_url"] == "https://gateway.example.test/v1"
    assert secret not in serialized
    assert "api_key" not in serialized.casefold()
    assert caller_environment == {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    persisted = (tmp_path / "config" / "rivet" / "config.toml").read_text(
        encoding="utf-8"
    )
    assert secret not in persisted
    assert "reasoner-large" in persisted
    assert any(event_type == "config.updated" for event_type, _ in events)
    assert application.ready_payload()["credential_configured"] is True
    restarted = CommandWorkerApplication(tmp_path, environment=caller_environment)
    restarted_payload = restarted.ready_payload()
    assert restarted_payload["model"] == "reasoner-large"
    assert restarted_payload["models"] == ["chat-fast", "reasoner-large"]
    assert restarted_payload["credential_configured"] is False


@pytest.mark.asyncio
async def test_tui_runtime_configuration_can_clear_only_the_session_key(
    tmp_path: Path,
) -> None:
    async def emit(
        _event_type: str,
        _payload: dict[str, JsonValue],
    ) -> None:
        return None

    application = CommandWorkerApplication(
        tmp_path,
        environment={
            "DEEPSEEK_API_KEY": "existing-session-value",
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
        },
    )
    result = await application.handle(
        _request(
            "request_config_clear",
            "config.update",
            {"api_key_action": "clear"},
        ),
        emit=emit,
        cancel_event=asyncio.Event(),
    )

    assert isinstance(result, dict)
    assert result["credential_configured"] is False
    assert application.ready_payload()["credential_configured"] is False


@pytest.mark.asyncio
async def test_tui_session_key_reaches_the_cli_subprocess_without_entering_argv(
    tmp_path: Path,
) -> None:
    class ProbeApplication(CommandWorkerApplication):
        """只向测试暴露默认无 shell 子进程 runner。"""

        async def run_probe(self) -> CommandExecution:
            async def discard(
                _event_type: str,
                _payload: dict[str, JsonValue],
            ) -> None:
                return None

            return await self._run_subprocess(
                (
                    sys.executable,
                    "-c",
                    "import os; print('configured' if os.environ.get("
                    "'DEEPSEEK_API_KEY') else 'missing')",
                ),
                discard,
            )

    async def emit(
        _event_type: str,
        _payload: dict[str, JsonValue],
    ) -> None:
        return None

    secret = "fixture-session-subprocess-value"
    application = ProbeApplication(
        tmp_path,
        environment={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
        },
    )
    await application.handle(
        _request(
            "request_config_subprocess",
            "config.update",
            {"api_key_action": "replace", "api_key": secret},
        ),
        emit=emit,
        cancel_event=asyncio.Event(),
    )

    execution = await application.run_probe()

    assert execution.return_code == 0
    assert execution.stdout == b"configured\n"
    assert secret not in execution.stdout.decode()


@pytest.mark.asyncio
async def test_tui_runtime_configuration_rejects_invalid_models_without_secret_leak(
    tmp_path: Path,
) -> None:
    async def emit(
        _event_type: str,
        _payload: dict[str, JsonValue],
    ) -> None:
        return None

    application = CommandWorkerApplication(
        tmp_path,
        environment={"XDG_CONFIG_HOME": str(tmp_path / "config")},
    )
    secret = "fixture-invalid-request-value"
    with pytest.raises(WorkerMethodError) as captured:
        await application.handle(
            _request(
                "request_config_invalid",
                "config.update",
                {
                    "api_key_action": "replace",
                    "api_key": secret,
                    "model": "missing-from-list",
                    "models": ["valid-model", "valid-model"],
                },
            ),
            emit=emit,
            cancel_event=asyncio.Event(),
        )

    assert captured.value.code == "config.update_invalid"
    assert secret not in str(captured.value)
    assert not (tmp_path / "config" / "rivet" / "config.toml").exists()

    with pytest.raises(WorkerMethodError) as invalid_key:
        await application.handle(
            _request(
                "request_config_invalid_key",
                "config.update",
                {
                    "api_key_action": "replace",
                    "api_key": "fixture value with spaces",
                },
            ),
            emit=emit,
            cancel_event=asyncio.Event(),
        )
    assert invalid_key.value.code == "config.update_invalid"

    with pytest.raises(WorkerMethodError) as invalid_command_model:
        await application.handle(
            _request(
                "request_command_invalid_model",
                "command.ask",
                {"model": "Bad Model", "query": "解释"},
            ),
            emit=emit,
            cancel_event=asyncio.Event(),
        )
    assert invalid_command_model.value.code == "config.model_invalid"


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
async def test_recent_transactions_are_loaded_only_on_explicit_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """事务选择器只接收经过事务记录校验的近期状态。"""
    from rivet.kernel.resources import ResourceScope
    from rivet.storage.git_exclude import configure_runtime_excludes
    from rivet.transaction.manager import TransactionManager
    from tests.transaction_helpers import initialize_repository

    repository = initialize_repository(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert configure_runtime_excludes(repository) is True
    scope = ResourceScope("ipc.transactions.list")
    manager = TransactionManager(repository, scope=scope)
    first = await manager.create(transaction_id="tx_history_first")
    manager.suspend(first.transaction_id)
    second = await manager.create(transaction_id="tx_history_second")
    manager.suspend(second.transaction_id)
    await scope.close()
    events: list[tuple[str, dict[str, JsonValue]]] = []

    async def emit(event_type: str, payload: dict[str, JsonValue]) -> None:
        events.append((event_type, payload))

    application = CommandWorkerApplication(repository)
    result = await application.handle(
        _request("request_transactions", "transactions.list", {"limit": 20}),
        emit=emit,
        cancel_event=asyncio.Event(),
    )

    assert isinstance(result, dict)
    assert result == {
        "transactions": [
            {
                "evidence_id": None,
                "state": "BASELINED",
                "transaction_id": "tx_history_second",
            },
            {
                "evidence_id": None,
                "state": "BASELINED",
                "transaction_id": "tx_history_first",
            },
        ]
    }
    assert events == [
        (
            "transactions.snapshot",
            {
                "summary": "已加载 2 个近期事务",
                "transactions": result["transactions"],
            },
        )
    ]


@pytest.mark.asyncio
async def test_resume_projects_verified_evidence_back_to_tui(tmp_path: Path) -> None:
    """恢复终态 fix 时必须恢复 Evidence 和独立验证状态。"""
    events: list[tuple[str, dict[str, JsonValue]]] = []

    async def runner(
        _argv: tuple[str, ...],
        _emit: EmitEvent,
    ) -> CommandExecution:
        return CommandExecution(
            0,
            json.dumps(
                {
                    "command": "fix",
                    "evidence_id": "evidence_resume_verified",
                    "session_id": "session_resume_verified",
                    "status": "VERIFIED",
                    "transaction_id": "tx_resume_verified",
                    "transaction_status": "VERIFIED",
                    "verification_status": "PASSED",
                }
            ).encode(),
            b"",
        )

    async def emit(event_type: str, payload: dict[str, JsonValue]) -> None:
        events.append((event_type, payload))

    application = CommandWorkerApplication(tmp_path, runner=runner)
    await application.handle(
        _request(
            "request_resume_verified",
            "command.resume",
            {"session_id": "session_resume_verified"},
        ),
        emit=emit,
        cancel_event=asyncio.Event(),
    )

    assert (
        "verification.completed",
        {"status": "PASSED", "summary": "验证已完成"},
    ) in events
    assert (
        "evidence.published",
        {
            "evidence_id": "evidence_resume_verified",
            "summary": "证据已发布",
        },
    ) in events


@pytest.mark.asyncio
async def test_resume_read_only_session_does_not_claim_verification(
    tmp_path: Path,
) -> None:
    """恢复普通 ask 终态不得把会话状态伪装成补丁验证结论。"""
    events: list[tuple[str, dict[str, JsonValue]]] = []

    async def runner(
        _argv: tuple[str, ...],
        _emit: EmitEvent,
    ) -> CommandExecution:
        return CommandExecution(
            0,
            json.dumps(
                {
                    "command": "ask",
                    "session_id": "session_resume_answered",
                    "status": "ANSWERED",
                }
            ).encode(),
            b"",
        )

    async def emit(event_type: str, payload: dict[str, JsonValue]) -> None:
        events.append((event_type, payload))

    application = CommandWorkerApplication(tmp_path, runner=runner)
    await application.handle(
        _request(
            "request_resume_answered",
            "command.resume",
            {"session_id": "session_resume_answered"},
        ),
        emit=emit,
        cancel_event=asyncio.Event(),
    )

    assert not any(event_type == "verification.completed" for event_type, _ in events)
    assert not any(event_type == "evidence.published" for event_type, _ in events)


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
async def test_read_enhancement_options_reach_the_formal_cli_parser(
    tmp_path: Path,
) -> None:
    """TUI Reader 的受限增强参数必须逐项传给正式 CLI。"""
    arguments: list[tuple[str, ...]] = []

    async def runner(
        argv: tuple[str, ...],
        _emit: EmitEvent,
    ) -> CommandExecution:
        arguments.append(argv)
        return CommandExecution(0, b"{}", b"")

    async def emit(
        _event_type: str,
        _payload: dict[str, JsonValue],
    ) -> None:
        pass

    application = CommandWorkerApplication(tmp_path, runner=runner)
    await application.handle(
        _request(
            "request_read_enhanced",
            "command.read",
            {
                "file": "samples/video.mp4",
                "frames": 8,
                "max_audio_duration": 600,
                "max_image_pixels": 2_000_000,
                "max_ocr_pages": 12,
                "max_output_chars": 5_000,
                "ocr": True,
                "timeout": 15,
                "transcribe": True,
            },
        ),
        emit=emit,
        cancel_event=asyncio.Event(),
    )

    assert arguments[0][-16:] == (
        "read",
        "samples/video.mp4",
        "--ocr",
        "--transcribe",
        "--frames",
        "8",
        "--max-ocr-pages",
        "12",
        "--max-image-pixels",
        "2000000",
        "--max-audio-duration",
        "600",
        "--max-output-chars",
        "5000",
        "--timeout",
        "15",
    )


@pytest.mark.asyncio
async def test_failed_read_is_projected_as_a_structured_reader_event(
    tmp_path: Path,
) -> None:
    """Reader 的领域失败仍应抵达 TUI，而不是退化为 IPC 异常。"""
    events: list[tuple[str, dict[str, JsonValue]]] = []

    async def runner(
        _argv: tuple[str, ...],
        _emit: EmitEvent,
    ) -> CommandExecution:
        return CommandExecution(
            4,
            json.dumps(
                {
                    "content": "",
                    "detected_format": "image",
                    "metadata": {"height": 500, "width": 640},
                    "reader_id": "reader.image",
                    "source_path": "samples/large.png",
                    "status": "FAILED",
                    "support_level": "A",
                    "truncated": False,
                    "warnings": ["reader.image.pixel_limit_exceeded"],
                }
            ).encode(),
            b"",
        )

    async def emit(event_type: str, payload: dict[str, JsonValue]) -> None:
        events.append((event_type, payload))

    application = CommandWorkerApplication(tmp_path, runner=runner)
    result = await application.handle(
        _request(
            "request_read_failed",
            "command.read",
            {"file": "samples/large.png", "max_image_pixels": 10},
        ),
        emit=emit,
        cancel_event=asyncio.Event(),
    )

    assert isinstance(result, dict)
    assert result["status"] == "FAILED"
    reader_event = next(
        payload for event_type, payload in events if event_type == "reader.completed"
    )
    assert reader_event["status"] == "FAILED"
    assert "pixel_limit_exceeded" in str(reader_event["summary"])


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
                    "answer": "已经修复，全部测试通过",
                    "evidence_id": "evidence_one",
                    "model_status": "READY_FOR_VERIFICATION",
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
    event_types = [event_type for event_type, _ in events]
    assert "agent.completed" not in event_types
    assert event_types.index("agent.patch_ready") < event_types.index(
        "verification.completed"
    )


@pytest.mark.asyncio
async def test_resume_fix_waits_for_explicit_permission_then_adds_yes(
    tmp_path: Path,
) -> None:
    """恢复 fix 也必须经 TUI 授权，并把批准传给正式 CLI。"""
    from rivet.storage.sessions import SessionCheckpoint, SessionStatus, SessionStore

    session_id = "session_resume_fix"
    SessionStore(tmp_path).save(
        SessionCheckpoint(
            session_id=session_id,
            run_id="run_resume_fix",
            command="fix",
            query="继续隔离修复",
            status=SessionStatus.FAILED,
        )
    )
    arguments: list[tuple[str, ...]] = []
    events: list[tuple[str, dict[str, JsonValue]]] = []
    permission_ready = asyncio.Event()

    async def runner(
        argv: tuple[str, ...],
        _emit: EmitEvent,
    ) -> CommandExecution:
        arguments.append(argv)
        return CommandExecution(
            4,
            json.dumps(
                {
                    "answer": "补丁验证未通过",
                    "evidence_id": "evidence_resume_fix",
                    "model_status": "READY_FOR_VERIFICATION",
                    "status": "REJECTED",
                    "transaction_id": "tx_resume_fix",
                }
            ).encode(),
            b"",
        )

    async def emit(event_type: str, payload: dict[str, JsonValue]) -> None:
        events.append((event_type, payload))
        if event_type == "permission.requested":
            permission_ready.set()

    application = CommandWorkerApplication(tmp_path, runner=runner)
    resume_task = asyncio.create_task(
        application.handle(
            _request(
                "request_resume_fix",
                "command.resume",
                {"session_id": session_id},
            ),
            emit=emit,
            cancel_event=asyncio.Event(),
        )
    )
    await asyncio.wait_for(permission_ready.wait(), timeout=1)
    permission = next(
        payload
        for event_type, payload in events
        if event_type == "permission.requested"
    )
    await application.handle(
        _request(
            "request_resume_fix_permission",
            "permission.resolve",
            {"approved": True, "request_id": permission["request_id"]},
        ),
        emit=emit,
        cancel_event=asyncio.Event(),
    )

    result = await resume_task

    assert arguments[0][-3:] == ("resume", session_id, "--yes")
    assert isinstance(result, dict)
    assert result["status"] == "REJECTED"


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
