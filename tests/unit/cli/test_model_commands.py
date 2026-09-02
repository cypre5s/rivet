"""验证最小 ASK/FIX CLI 的提案、Demand 与 Evidence 闭环。"""

from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from rivet.cli.config import ResolvedConfig
from rivet.cli.errors import CliSecurityError, CliVerificationError
from rivet.cli.model_commands import build_acceptance_spec, run_model_command
from rivet.cli.transaction_commands import run_transaction_command
from rivet.contracts.messages import AssistantMessage
from rivet.contracts.provider import (
    ModelFinishReason,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)
from rivet.contracts.tools import ToolCall
from rivet.contracts.transactions import TransactionState
from rivet.kernel.module_api import ModuleActivationContext
from rivet.kernel.resources import ResourceScope
from rivet.trace.paths import RuntimePaths
from rivet.transaction.hashing import acceptance_sha256
from rivet.transaction.store import TransactionStore
from rivet.verify.detector import ProjectDetector

NOW = datetime(2026, 9, 2, tzinfo=UTC)


class ScriptedProvider:
    def __init__(self, responses: Sequence[ModelResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def complete(
        self,
        request: ModelRequest,
        *,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> ModelResponse:
        self.requests.append(request)
        response = self._responses.pop(0)
        if on_text_delta is not None and response.message.content:
            await on_text_delta(response.message.content)
        return response


def _provider_factory(
    provider: ScriptedProvider,
) -> Callable[[ModuleActivationContext, ResourceScope], ScriptedProvider]:
    def create(
        context: ModuleActivationContext,
        scope: ResourceScope,
    ) -> ScriptedProvider:
        del context, scope
        return provider

    return create


def _response(
    *,
    content: str = "",
    tool_calls: tuple[ToolCall, ...] = (),
    finish_reason: ModelFinishReason = ModelFinishReason.STOP,
) -> ModelResponse:
    return ModelResponse(
        provider_id="deepseek",
        model="deepseek-v4-flash",
        message=AssistantMessage(
            content=content,
            tool_calls=tool_calls,
            created_at=NOW,
        ),
        finish_reason=finish_reason,
        usage=TokenUsage(
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            cost_usd=Decimal("0.001"),
        ),
    )


def _investigation_provider() -> ScriptedProvider:
    return ScriptedProvider(
        (
            _response(
                tool_calls=(
                    ToolCall(
                        tool_call_id="call_context_search",
                        tool_name="context_search",
                        arguments={"query": "answer", "max_results": 4},
                    ),
                ),
                finish_reason=ModelFinishReason.TOOL_CALLS,
            ),
            _response(content="根因位于 calc.py，建议只修改该文件。"),
        )
    )


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "DEEPSEEK_API_KEY": "test-key-not-real",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }


def _config(*, credential: bool = True) -> ResolvedConfig:
    return ResolvedConfig(
        model="deepseek-v4-flash",
        models=("deepseek-v4-flash",),
        base_url="https://api.deepseek.com",
        max_rounds=8,
        max_total_tokens=16_000,
        max_cost_usd=Decimal("1"),
        credential_configured=credential,
        sources={"model": "test"},
    )


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=repository, check=True)
    subprocess.run(
        ("git", "config", "user.email", "test@example.invalid"),
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ("git", "config", "user.name", "Rivet Test"),
        cwd=repository,
        check=True,
    )
    (repository / "calc.py").write_text(
        "def answer():\n    return 1\n", encoding="utf-8"
    )
    config = repository / ".rivet" / "project.toml"
    config.parent.mkdir()
    config.write_text(
        """schema_version = 1
[rivet]
model = "deepseek-v4-flash"
[verification]
acceptance = [["python", "-c", "from calc import answer; assert answer() == 2"]]
regression = []
static = []
""",
        encoding="utf-8",
    )
    subprocess.run(("git", "add", "."), cwd=repository, check=True)
    subprocess.run(("git", "commit", "-qm", "baseline"), cwd=repository, check=True)
    return repository


def _fix_arguments(
    *,
    yes: bool,
    allow_write: list[str],
    allow_read: list[str] | None = None,
    allow_new: list[str] | None = None,
    acceptance_hash: str | None = None,
    base_commit: str | None = None,
) -> Namespace:
    return Namespace(
        acceptance_sha256=acceptance_hash,
        base_commit=base_commit,
        command="fix",
        task="让 answer 返回 2",
        yes=yes,
        allow_read=allow_read or [],
        allow_write=allow_write,
        allow_new=allow_new or [],
    )


@pytest.mark.asyncio
async def test_unconfirmed_fix_without_scope_is_in_memory_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    environment = _environment(tmp_path)
    detection = ProjectDetector().detect(repository)
    provider = _investigation_provider()
    from rivet.modules import factories

    monkeypatch.setattr(
        factories,
        "_create_deepseek_provider",
        _provider_factory(provider),
    )

    code = await run_model_command(
        _fix_arguments(yes=False, allow_write=[]),
        repository=repository,
        config=_config(),
        environment=environment,
        json_output=True,
        preflight_detection=detection,
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["transaction_created"] is False
    assert payload["scope"] == []
    assert "calc.py" in payload["investigation"]
    assert len(provider.requests) == 2
    paths = RuntimePaths.for_repository(repository, environment=environment)
    assert paths.events_path.is_file()
    assert not paths.transactions_root.exists()
    assert not paths.worktrees_root.exists()
    assert {definition.name for definition in provider.requests[0].tools} == {
        "workspace_info",
        "context_search",
        "file_read",
    }


@pytest.mark.asyncio
async def test_unconfirmed_complete_proposal_has_no_transaction(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    environment = _environment(tmp_path)
    detection = ProjectDetector().detect(repository)
    provider = _investigation_provider()
    from rivet.modules import factories

    monkeypatch.setattr(
        factories,
        "_create_deepseek_provider",
        _provider_factory(provider),
    )

    code = await run_model_command(
        _fix_arguments(yes=False, allow_write=["calc.py"]),
        repository=repository,
        config=_config(),
        environment=environment,
        json_output=True,
        preflight_detection=detection,
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["confirmed"] is False
    assert payload["transaction_created"] is False
    assert payload["acceptance"]["write_scope"] == ["calc.py"]
    assert set(payload) == {
        "acceptance",
        "acceptance_sha256",
        "base_commit",
        "confirmed",
        "investigation",
        "next_action",
        "run_id",
        "transaction_created",
    }
    assert payload["acceptance_sha256"] == acceptance_sha256(
        build_acceptance_spec(
            repository,
            "让 answer 返回 2",
            detection=detection,
            explicit_paths=("calc.py",),
            config=_config(),
        )
    )
    paths = RuntimePaths.for_repository(repository, environment=environment)
    assert not paths.transactions_root.exists()
    assert not paths.evidence_root.exists()


@pytest.mark.asyncio
async def test_unconfirmed_fix_requires_actual_read_tool_before_proposal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    environment = _environment(tmp_path)
    provider = ScriptedProvider((_response(content="我猜问题位于 calc.py"),))
    from rivet.modules import factories

    monkeypatch.setattr(
        factories,
        "_create_deepseek_provider",
        _provider_factory(provider),
    )

    with pytest.raises(CliVerificationError, match="只读仓库调查"):
        await run_model_command(
            _fix_arguments(yes=False, allow_write=["calc.py"]),
            repository=repository,
            config=_config(),
            environment=environment,
            json_output=True,
            preflight_detection=ProjectDetector().detect(repository),
        )

    paths = RuntimePaths.for_repository(repository, environment=environment)
    assert not paths.transactions_root.exists()
    assert not paths.worktrees_root.exists()
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_confirmed_fix_rejects_unbound_acceptance_before_transaction(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    environment = _environment(tmp_path)

    with pytest.raises(CliSecurityError, match="确认令牌"):
        await run_model_command(
            _fix_arguments(
                yes=True,
                allow_write=["calc.py"],
                acceptance_hash="sha256:" + "0" * 64,
                base_commit="0" * 40,
            ),
            repository=repository,
            config=_config(),
            environment=environment,
            json_output=True,
            preflight_detection=ProjectDetector().detect(repository),
        )

    paths = RuntimePaths.for_repository(repository, environment=environment)
    assert not paths.transactions_root.exists()
    assert not paths.worktrees_root.exists()


@pytest.mark.asyncio
async def test_confirmed_fix_rejects_proposal_base_drift_without_transaction(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    environment = _environment(tmp_path)
    detection = ProjectDetector().detect(repository)
    specification = build_acceptance_spec(
        repository,
        "让 answer 返回 2",
        detection=detection,
        explicit_paths=("calc.py",),
        config=_config(),
    )

    with pytest.raises(CliSecurityError, match="Git 基线"):
        await run_model_command(
            _fix_arguments(
                yes=True,
                allow_write=["calc.py"],
                acceptance_hash=acceptance_sha256(specification),
                base_commit="0" * 40,
            ),
            repository=repository,
            config=_config(),
            environment=environment,
            json_output=True,
            preflight_detection=detection,
        )

    paths = RuntimePaths.for_repository(repository, environment=environment)
    assert not paths.transactions_root.exists()
    assert not paths.worktrees_root.exists()


def test_acceptance_rejects_write_scope_covering_oracle(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    detection = ProjectDetector().detect(repository)
    with pytest.raises(CliSecurityError, match="验收文件"):
        build_acceptance_spec(
            repository,
            "修改测试和实现",
            detection=detection,
            explicit_paths=(".rivet",),
            config=_config(),
        )


@pytest.mark.asyncio
async def test_confirmed_fix_requires_explicit_scope(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with pytest.raises(CliSecurityError, match="最小写范围"):
        await run_model_command(
            _fix_arguments(yes=True, allow_write=[]),
            repository=repository,
            config=_config(),
            environment=_environment(tmp_path),
            json_output=True,
            preflight_detection=ProjectDetector().detect(repository),
        )


@pytest.mark.asyncio
async def test_ask_activates_provider_only_after_user_demand(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    environment = _environment(tmp_path)
    provider = ScriptedProvider((_response(content="这是一个示例仓库"),))
    from rivet.modules import factories

    monkeypatch.setattr(
        factories,
        "_create_deepseek_provider",
        _provider_factory(provider),
    )
    code = await run_model_command(
        Namespace(command="ask", query="这是什么项目？"),
        repository=repository,
        config=_config(),
        environment=environment,
        json_output=True,
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["answer"] == "这是一个示例仓库"
    assert provider.requests[0].tools
    paths = RuntimePaths.for_repository(repository, environment=environment)
    events = [json.loads(line) for line in paths.events_path.read_text().splitlines()]
    demand_sources = [
        item["event"]["payload"]["demand_source"]
        for item in events
        if item["event"]["event_type"] == "demand.created"
    ]
    assert demand_sources == ["USER_EXPLICIT", "KERNEL_REQUIRED"]
    assert not paths.transactions_root.exists()


@pytest.mark.asyncio
async def test_fix_verifies_then_explicit_apply_changes_main_workspace(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    environment = _environment(tmp_path)
    provider = ScriptedProvider(
        (
            _response(
                tool_calls=(
                    ToolCall(
                        tool_call_id="call_read_allowed",
                        tool_name="file_read",
                        arguments={"path": "calc.py"},
                    ),
                    ToolCall(
                        tool_call_id="call_read_forbidden_oracle",
                        tool_name="file_read",
                        arguments={"path": ".rivet/project.toml"},
                    ),
                ),
                finish_reason=ModelFinishReason.TOOL_CALLS,
            ),
            _response(
                tool_calls=(
                    ToolCall(
                        tool_call_id="call_replace",
                        tool_name="file_replace",
                        arguments={
                            "path": "calc.py",
                            "old_text": "return 1",
                            "new_text": "return 2",
                            "expected_count": 1,
                        },
                    ),
                ),
                finish_reason=ModelFinishReason.TOOL_CALLS,
            ),
            _response(
                tool_calls=(
                    ToolCall(
                        tool_call_id="call_diff_path",
                        tool_name="git_diff",
                        arguments={"path": "calc.py"},
                    ),
                ),
                finish_reason=ModelFinishReason.TOOL_CALLS,
            ),
            _response(content="候选补丁已生成，等待独立验证。"),
        )
    )
    from rivet.modules import factories

    monkeypatch.setattr(
        factories,
        "_create_deepseek_provider",
        _provider_factory(provider),
    )
    detection = ProjectDetector().detect(repository)
    specification = build_acceptance_spec(
        repository,
        "让 answer 返回 2",
        detection=detection,
        explicit_paths=("calc.py",),
        config=_config(),
    )
    base_commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    code = await run_model_command(
        _fix_arguments(
            yes=True,
            allow_write=["calc.py"],
            acceptance_hash=acceptance_sha256(specification),
            base_commit=base_commit,
        ),
        repository=repository,
        config=_config(),
        environment=environment,
        json_output=True,
        preflight_detection=detection,
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["state"] == "VERIFIED"
    assert payload["evidence_verified"] is True
    assert "workspace.read_scope_denied" in provider.requests[1].messages[-1].content
    assert "return 2" in provider.requests[-1].messages[-1].content
    assert "return 1" in (repository / "calc.py").read_text(encoding="utf-8")
    transaction_id = payload["transaction_id"]
    runtime_paths = RuntimePaths.for_repository(repository, environment=environment)
    trace_events = [
        json.loads(line)["event"]
        for line in runtime_paths.events_path.read_text(encoding="utf-8").splitlines()
    ]
    verification_started = next(
        event for event in trace_events if event["event_type"] == "verification.started"
    )
    verification_completed = next(
        event
        for event in trace_events
        if event["event_type"] == "verification.completed"
    )
    rejected_read = next(
        event
        for event in trace_events
        if event["event_type"] == "tool.failed"
        and event["payload"]["operation_id"] == "call_read_forbidden_oracle"
    )
    assert rejected_read["payload"]["error_code"] == "workspace.read_scope_denied"
    assert rejected_read["payload"]["error_type"] == "PathBoundaryError"
    verification_demand = next(
        event
        for event in trace_events
        if event["event_id"] == verification_started["parent_event_id"]
    )
    assert verification_demand["event_type"] == "demand.created"
    assert verification_demand["payload"]["demand_source"] == "KERNEL_REQUIRED"
    assert verification_demand["payload"]["capability_id"] == "verify.deterministic"
    assert verification_completed["parent_event_id"] == verification_started["event_id"]
    assert verification_completed["payload"]["passed"] is True
    assert {
        result["kind"] for result in verification_completed["payload"]["results"]
    } == {
        "BASELINE",
        "BEHAVIOR",
        "SCOPE",
        "SECRET",
        "BINDING",
        "RESOURCE",
    }
    assert any(
        event["event_type"] == "module.activated"
        and event["payload"]["module_id"] == "guard.sandbox"
        and event["parent_event_id"] == verification_demand["event_id"]
        for event in trace_events
    )
    store = TransactionStore(
        runtime_paths.transactions_root,
        evidence_root=runtime_paths.evidence_root,
    )
    assert store.load_record(transaction_id).state is TransactionState.VERIFIED

    apply_code = await run_transaction_command(
        Namespace(command="apply", transaction_id=transaction_id),
        repository=repository,
        environment=environment,
        json_output=True,
    )
    apply_payload = json.loads(capsys.readouterr().out)
    assert apply_code == 0
    assert apply_payload["state"] == "APPLIED"
    assert "return 2" in (repository / "calc.py").read_text(encoding="utf-8")
