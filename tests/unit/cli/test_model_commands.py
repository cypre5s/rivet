"""用脚本 Provider 验证 CLI 模型命令的离线编排。"""

from __future__ import annotations

import json
import os
import subprocess
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import rivet.cli.model_commands as model_commands
from rivet.cli.config import ResolvedConfig
from rivet.cli.errors import CliProviderError
from rivet.cli.exit_codes import ExitCode
from rivet.contracts.messages import (
    AssistantMessage,
    ProviderOpaqueState,
    SystemMessage,
    UserMessage,
)
from rivet.contracts.provider import (
    ModelFinishReason,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)
from rivet.contracts.tools import ToolCall
from rivet.kernel.resources import ResourceScope
from rivet.storage.sessions import (
    SessionCheckpoint,
    SessionStage,
    SessionStatus,
    SessionStore,
)
from rivet.tools.files import TransactionFileWriter
from rivet.transaction.manager import TransactionManager
from rivet.transaction.store import TransactionStore

NOW = datetime(2026, 8, 28, tzinfo=UTC)


def _git(repository: Path, *arguments: str) -> None:
    """运行固定测试 Git 命令。"""
    subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        env={
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    )


def _repository(tmp_path: Path) -> Path:
    """创建一个干净且可形成补丁的 Git 仓库。"""
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Fixture")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "--", "tracked.txt")
    _git(repository, "commit", "-qm", "initial")
    return repository


def _config() -> ResolvedConfig:
    """返回不携带凭据值的已解析配置。"""
    return ResolvedConfig(
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        max_rounds=5,
        max_total_tokens=4_000,
        max_cost_usd=None,
        safe_mode=False,
        credential_configured=True,
        sources={
            "base_url": "default",
            "max_cost_usd": "default",
            "max_rounds": "default",
            "max_total_tokens": "default",
            "model": "default",
            "safe_mode": "default",
        },
    )


def test_behavior_verifier_files_are_frozen_out_of_patch_scope(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    (repository / ".rivet").mkdir(parents=True)
    (repository / ".rivet" / "project.toml").write_text(
        "schema_version = 1\n",
        encoding="utf-8",
    )
    (repository / "check_behavior.py").write_text(
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    (repository / "acceptance").mkdir()
    (repository / "acceptance" / "cases.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    executable = repository / "verify-behavior"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (repository / "verify-link").symlink_to("verify-behavior")
    (repository / "acceptance_module.py").write_text(
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )

    protected = model_commands.resolve_behavior_verifier_paths(
        repository,
        (
            ("python", "check_behavior.py", "acceptance"),
            ("./verify-behavior",),
            ("./verify-link",),
            ("python", "-m", "acceptance_module"),
            ("python", "-c", "print('ok')"),
        ),
    )

    assert protected == (
        ".rivet/project.toml",
        "acceptance",
        "acceptance_module.py",
        "check_behavior.py",
        "verify-behavior",
        "verify-link",
    )


def _response(
    content: str,
    *,
    tool_calls: tuple[ToolCall, ...] = (),
    finish_reason: ModelFinishReason = ModelFinishReason.STOP,
) -> ModelResponse:
    """构造带 opaque 状态且 usage 自洽的响应。"""
    return ModelResponse(
        provider_id="deepseek",
        model="deepseek-v4-pro",
        message=AssistantMessage(
            content=content,
            tool_calls=tool_calls,
            opaque_state=ProviderOpaqueState(
                provider_id="deepseek",
                provider_version="1.0.0",
                payload={"reasoning_content": "opaque"},
            ),
            created_at=NOW,
        ),
        finish_reason=finish_reason,
        usage=TokenUsage(
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
        ),
    )


def _install_provider(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[ModelResponse],
) -> list[ModelRequest]:
    """注入只返回本地录制响应的 Provider 原语。"""
    requests: list[ModelRequest] = []

    class ScriptedProvider:
        """按顺序消费响应，不创建客户端或访问网络。"""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.requests: list[ModelRequest] = []

        async def complete(self, request: ModelRequest) -> ModelResponse:
            """记录请求并返回下一个响应。"""
            self.requests.append(request)
            requests.append(request)
            return responses.pop(0)

    def create_provider(**_kwargs: object) -> ScriptedProvider:
        """在正式惰性 Provider 边界注入录制原语。"""
        return ScriptedProvider()

    monkeypatch.setattr(model_commands, "_create_provider", create_provider)
    return requests


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["ask", "plan"])
async def test_read_only_model_commands_persist_trace_and_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    repository = _repository(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    _install_provider(monkeypatch, [_response("离线回答")])

    exit_code = await model_commands.run_model_command(
        Namespace(command=command, query="解释仓库"),
        repository=repository,
        config=_config(),
        environment={"DEEPSEEK_API_KEY": "fixture-provider-value"},
        json_output=True,
    )
    payload = json.loads(capsys.readouterr().out)
    checkpoint = SessionStore(repository).load(payload["session_id"])

    assert exit_code == 0
    assert payload["answer"] == "离线回答"
    assert checkpoint.status is (
        SessionStatus.ANSWERED if command == "ask" else SessionStatus.PLANNED
    )
    assert checkpoint.provider_state == {"reasoning_content": "opaque"}
    assert (repository / ".rivet" / "trace" / "events.ndjson").is_file()


@pytest.mark.asyncio
async def test_fix_records_patch_and_fails_closed_when_verification_cannot_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = _repository(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    _install_provider(
        monkeypatch,
        [
            _response(
                "",
                tool_calls=(
                    ToolCall(
                        tool_call_id="call_fix_write",
                        tool_name="file.write_transaction",
                        arguments={"content": "patched\n", "path": "tracked.txt"},
                    ),
                ),
                finish_reason=ModelFinishReason.TOOL_CALLS,
            ),
            _response("修改已完成"),
        ],
    )

    exit_code = await model_commands.run_model_command(
        Namespace(
            command="fix",
            dirty_policy="reject",
            task="修改 tracked.txt",
            yes=True,
        ),
        repository=repository,
        config=_config(),
        environment={"DEEPSEEK_API_KEY": "fixture-provider-value"},
        json_output=True,
    )
    payload = json.loads(capsys.readouterr().out)
    store = TransactionStore(repository / ".rivet" / "transactions")
    record = store.load_record(payload["transaction_id"])
    checkpoint_paths = tuple((repository / ".rivet" / "sessions").glob("*.json"))
    checkpoint = SessionStore(repository).load(checkpoint_paths[0].stem)

    assert exit_code == 4
    assert payload["status"] != "PASSED"
    assert record.current_patch_id is not None
    assert record.state.value == "INCONCLUSIVE"
    assert checkpoint.status is SessionStatus.INCONCLUSIVE
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == "base\n"


@pytest.mark.asyncio
async def test_formal_agent_uses_context_reader_and_semantic_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = _repository(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    requests = _install_provider(monkeypatch, [_response("上下文回答")])

    exit_code = await model_commands.run_model_command(
        Namespace(command="ask", query="解释 tracked.txt"),
        repository=repository,
        config=_config(),
        environment={"DEEPSEEK_API_KEY": "fixture-provider-value"},
        json_output=True,
    )
    capsys.readouterr()

    assert exit_code == 0
    assert {tool.name for tool in requests[0].tools} >= {
        "reader.read",
        "context.search.semantic",
    }
    assert any(
        message.role == "user"
        and "tracked.txt" in message.content
        and "不可信数据" in message.content
        for message in requests[0].messages
    )
    checkpoint_path = next((repository / ".rivet" / "sessions").glob("*.json"))
    checkpoint = SessionStore(repository).load(checkpoint_path.stem)
    assert not any(
        "RIVET_UNTRUSTED_REPOSITORY_CONTEXT" in message.content
        for message in checkpoint.messages
    )
    trace_lines = (repository / ".rivet" / "trace" / "events.ndjson").read_text(
        encoding="utf-8"
    )
    assert '"event_type":"context.selected"' in trace_lines
    assert '"module_id":"context.syntax"' in trace_lines


@pytest.mark.asyncio
async def test_resume_continues_saved_history_and_preserves_budget_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = _repository(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    requests = _install_provider(monkeypatch, [_response("续跑完成")])
    checkpoint = SessionCheckpoint(
        session_id="session_model_resume",
        run_id="run_model_resume",
        command="ask",
        query="继续解释 tracked.txt",
        status=SessionStatus.INTERRUPTED,
        stage=SessionStage.AGENT_LOOP,
        model="deepseek-v4-pro",
        messages=(
            SystemMessage(content="保持只读", created_at=NOW),
            UserMessage(content="继续解释 tracked.txt", created_at=NOW),
        ),
        round_count=2,
        prompt_tokens=10,
        completion_tokens=5,
    )
    SessionStore(repository).save(checkpoint)

    exit_code = await model_commands.run_model_command(
        Namespace(command="resume", session_id=checkpoint.session_id, yes=False),
        repository=repository,
        config=_config(),
        environment={"DEEPSEEK_API_KEY": "fixture-provider-value"},
        json_output=True,
        resume_checkpoint=checkpoint,
    )
    payload = json.loads(capsys.readouterr().out)
    resumed = SessionStore(repository).load(checkpoint.session_id)

    assert exit_code == 0
    assert payload["resumed"] is True
    assert payload["run_id"] == checkpoint.run_id
    assert requests[0].messages[0] == checkpoint.messages[0]
    assert resumed.status is SessionStatus.ANSWERED
    assert resumed.stage is SessionStage.TERMINAL
    assert resumed.round_count == 3
    assert resumed.prompt_tokens == 11
    assert resumed.completion_tokens == 6
    assert resumed.messages[-1].role == "assistant"


@pytest.mark.asyncio
async def test_provider_failure_checkpoint_remains_agent_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    class FailingProvider:
        """稳定模拟一次可恢复 Provider 故障。"""

        async def complete(self, _request: ModelRequest) -> ModelResponse:
            """在任何网络接触前返回 Kernel 可识别错误。"""
            raise ModelProviderError("fixture unavailable")

    def create_failing_provider(**_kwargs: object) -> FailingProvider:
        """在惰性 Provider 边界返回稳定失败原语。"""
        return FailingProvider()

    monkeypatch.setattr(model_commands, "_create_provider", create_failing_provider)

    with pytest.raises(CliProviderError):
        await model_commands.run_model_command(
            Namespace(command="ask", query="解释 tracked.txt"),
            repository=repository,
            config=_config(),
            environment={"DEEPSEEK_API_KEY": "fixture-provider-value"},
            json_output=True,
        )

    checkpoint_path = next((repository / ".rivet" / "sessions").glob("*.json"))
    checkpoint = SessionStore(repository).load(checkpoint_path.stem)
    assert checkpoint.status is SessionStatus.FAILED
    assert checkpoint.stage is SessionStage.AGENT_LOOP
    assert checkpoint.messages
    assert checkpoint.termination_reason == "provider_failed"


@pytest.mark.asyncio
async def test_patch_finalization_resume_skips_provider_and_finishes_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = _repository(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    prepare_scope = ResourceScope("resume.patch.prepare")
    prepare_manager = TransactionManager(repository, scope=prepare_scope)
    record = await prepare_manager.create(transaction_id="tx_resume_patch")
    specification = prepare_manager.draft_acceptance(
        acceptance_id="acceptance_resume_patch",
        user_goal="修改 tracked.txt",
        baseline_reproduction=(("git", "status", "--short"),),
        allowed_paths=("tracked.txt",),
        expected_behaviors=("tracked.txt 已修改",),
        preserved_behaviors=("主工作区不变",),
        verification_commands=(("git", "diff", "--check"),),
        behavior_verification_commands=(("rivet-missing-behavior-verifier",),),
        max_wall_seconds=60,
        max_tokens=2_000,
        max_tool_calls=10,
    )
    await prepare_manager.freeze_acceptance(
        record.transaction_id,
        specification,
        confirmed=True,
    )
    TransactionFileWriter(
        prepare_manager.transaction_boundary(record.transaction_id)
    ).write("tracked.txt", "resumed patch\n")
    prepare_manager.suspend(record.transaction_id)
    prepare_scope.assert_empty()
    await prepare_scope.close()
    checkpoint = SessionCheckpoint(
        session_id="session_resume_patch",
        run_id="run_resume_patch",
        transaction_id=record.transaction_id,
        command="fix",
        query="修改 tracked.txt",
        status=SessionStatus.INTERRUPTED,
        stage=SessionStage.PATCH_FINALIZATION,
        model="deepseek-v4-pro",
        messages=(
            UserMessage(content="修改 tracked.txt", created_at=NOW),
            AssistantMessage(content="隔离修改已完成", created_at=NOW),
        ),
        round_count=2,
        tool_call_count=1,
    )
    SessionStore(repository).save(checkpoint)

    def forbidden_provider(**_kwargs: object) -> object:
        """证明模型阶段完成后恢复不会再次产生 API 调用。"""
        raise AssertionError("PATCH_FINALIZATION 不得创建 Provider")

    monkeypatch.setattr(model_commands, "_create_provider", forbidden_provider)

    exit_code = await model_commands.run_model_command(
        Namespace(command="resume", session_id=checkpoint.session_id, yes=True),
        repository=repository,
        config=_config(),
        environment={},
        json_output=True,
        resume_checkpoint=checkpoint,
    )
    capsys.readouterr()
    resumed = SessionStore(repository).load(checkpoint.session_id)
    transaction = TransactionStore(repository / ".rivet" / "transactions").load_record(
        record.transaction_id
    )
    trace = (repository / ".rivet" / "trace" / "events.ndjson").read_text(
        encoding="utf-8"
    )

    assert exit_code == int(ExitCode.VERIFICATION_FAILED)
    assert resumed.stage is SessionStage.TERMINAL
    assert resumed.status is SessionStatus.BLOCKED
    assert transaction.state.value == "BLOCKED"
    assert transaction.current_patch_id is not None
    assert '"module_id":"provider.deepseek"' not in trace

    cleanup_scope = ResourceScope("resume.patch.cleanup")
    await TransactionManager(repository, scope=cleanup_scope).abort(
        record.transaction_id
    )
    cleanup_scope.assert_empty()
    await cleanup_scope.close()
