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
from rivet.contracts.messages import (
    AssistantMessage,
    ProviderOpaqueState,
)
from rivet.contracts.provider import (
    ModelFinishReason,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)
from rivet.contracts.tools import ToolCall
from rivet.storage.sessions import SessionStatus, SessionStore
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
) -> None:
    """注入只返回本地录制响应的 Provider 原语。"""

    class ScriptedProvider:
        """按顺序消费响应，不创建客户端或访问网络。"""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.requests: list[ModelRequest] = []

        async def complete(self, request: ModelRequest) -> ModelResponse:
            """记录请求并返回下一个响应。"""
            self.requests.append(request)
            return responses.pop(0)

    monkeypatch.setattr(model_commands, "DeepSeekProvider", ScriptedProvider)


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
    assert checkpoint.status is SessionStatus.COMPLETED
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
    assert record.state.value == "REJECTED"
    assert checkpoint.status is SessionStatus.FAILED
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == "base\n"
