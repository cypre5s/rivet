"""验证跨 CLI 生命周期的事务 diff、恢复与 abort。"""

from __future__ import annotations

import asyncio
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import rivet.cli.model_commands as model_commands
from rivet.cli.application import run_cli
from rivet.cli.exit_codes import ExitCode
from rivet.cli.transaction_commands import run_transaction_command
from rivet.contracts.messages import AssistantMessage, UserMessage
from rivet.contracts.tools import ToolCall
from rivet.contracts.transactions import TransactionState
from rivet.kernel.resources import ResourceScope
from rivet.storage.git_exclude import configure_runtime_excludes
from rivet.storage.sessions import (
    SessionCheckpoint,
    SessionStage,
    SessionStatus,
    SessionStore,
)
from rivet.tools.files import TransactionFileWriter
from rivet.transaction.manager import TransactionManager
from rivet.transaction.store import TransactionStore
from tests.transaction_helpers import (
    acceptance_spec,
    initialize_repository,
    passed_verdict,
)


@pytest.mark.asyncio
async def test_suspended_transaction_can_diff_and_abort_across_managers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = initialize_repository(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert configure_runtime_excludes(repository) is True
    scope = ResourceScope("transaction.cli.prepare")
    manager = TransactionManager(repository, scope=scope)
    record = await manager.create(transaction_id="tx_cli_resume")
    await manager.freeze_acceptance(
        record.transaction_id,
        acceptance_spec(),
        confirmed=True,
    )
    writer = TransactionFileWriter(manager.transaction_boundary(record.transaction_id))
    writer.write("tracked.txt", "cli patched\n")
    await manager.record_patch_set(record.transaction_id, patch_id="patch_cli_resume")

    manager.suspend(record.transaction_id)
    scope.assert_empty()
    await scope.close()

    diff_code = await run_transaction_command(
        Namespace(command="diff", transaction_id=record.transaction_id),
        repository=repository,
        json_output=False,
    )
    diff_output = capsys.readouterr().out
    abort_code = await run_transaction_command(
        Namespace(command="abort", transaction_id=record.transaction_id),
        repository=repository,
        json_output=True,
    )

    assert diff_code == 0
    assert "cli patched" in diff_output
    assert abort_code == 0
    assert not manager.worktree_path(record.transaction_id).exists()


def test_cli_resume_loads_provider_state_without_replaying_tools(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    SessionStore(repository).save(
        SessionCheckpoint(
            session_id="session_cli_resume",
            run_id="run_cli_resume",
            command="ask",
            query="解释",
            status=SessionStatus.COMPLETED,
            provider_state={"reasoning_content": "opaque"},
        )
    )

    exit_code = run_cli(
        (
            "--repository",
            str(repository),
            "--json",
            "resume",
            "session_cli_resume",
        ),
        environment={"XDG_CONFIG_HOME": str(tmp_path / "config")},
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert '"provider_state_restored":true' in output
    assert "opaque" not in output


def test_cli_resume_terminal_fix_restores_verified_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """终态恢复只投影重新验真的 Evidence，而不是遗失验证上下文。"""
    repository = initialize_repository(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert configure_runtime_excludes(repository) is True

    async def prepare() -> tuple[str, str]:
        scope = ResourceScope("resume.verified.prepare")
        manager = TransactionManager(repository, scope=scope)
        record = await manager.create(transaction_id="tx_resume_verified")
        await manager.freeze_acceptance(
            record.transaction_id,
            acceptance_spec(acceptance_id="acceptance_resume_verified"),
            confirmed=True,
        )
        TransactionFileWriter(
            manager.transaction_boundary(record.transaction_id)
        ).write("tracked.txt", "verified resume\n")
        await manager.record_patch_set(
            record.transaction_id,
            patch_id="patch_resume_verified",
        )
        verifying = await manager.begin_verification(record.transaction_id)
        verified = await manager.record_verdict(passed_verdict(verifying, manager))
        manager.suspend(record.transaction_id)
        scope.assert_empty()
        await scope.close()
        assert verified.evidence_id is not None
        return verified.transaction_id, verified.evidence_id

    transaction_id, evidence_id = asyncio.run(prepare())
    SessionStore(repository).save(
        SessionCheckpoint(
            session_id="session_resume_verified",
            run_id="run_resume_verified",
            transaction_id=transaction_id,
            command="fix",
            query="修改 tracked.txt",
            status=SessionStatus.VERIFIED,
            stage=SessionStage.TERMINAL,
        )
    )

    exit_code = run_cli(
        (
            "--repository",
            str(repository),
            "--json",
            "resume",
            "session_resume_verified",
        ),
        environment={"XDG_CONFIG_HOME": str(tmp_path / "config")},
    )
    output = capsys.readouterr().out

    assert exit_code == int(ExitCode.SUCCESS)
    assert f'"evidence_id":"{evidence_id}"' in output
    assert '"transaction_status":"VERIFIED"' in output
    assert '"verification_status":"PASSED"' in output


def test_cli_resume_dispatches_saved_history_back_to_agent_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    now = datetime(2026, 8, 28, tzinfo=UTC)
    checkpoint = SessionCheckpoint(
        session_id="session_cli_continue",
        run_id="run_cli_continue",
        command="ask",
        query="继续解释",
        status=SessionStatus.INTERRUPTED,
        stage=SessionStage.AGENT_LOOP,
        model="deepseek-v4-pro",
        messages=(UserMessage(content="继续解释", created_at=now),),
    )
    SessionStore(repository).save(checkpoint)
    captured: list[SessionCheckpoint] = []

    async def fake_run_model_command(
        _arguments: Namespace,
        *,
        repository: Path,
        config: object,
        environment: object,
        json_output: bool,
        resume_checkpoint: SessionCheckpoint | None = None,
    ) -> int:
        """确认正式 resume 分发携带经过校验的 checkpoint。"""
        del repository, config, environment, json_output
        if resume_checkpoint is None:
            raise AssertionError("缺少续跑 checkpoint")
        captured.append(resume_checkpoint)
        print('{"resumed":true}')
        return 0

    monkeypatch.setattr(
        model_commands,
        "run_model_command",
        fake_run_model_command,
    )

    exit_code = run_cli(
        (
            "--repository",
            str(repository),
            "--json",
            "resume",
            checkpoint.session_id,
        ),
        environment={
            "DEEPSEEK_API_KEY": "fixture-provider-value",
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
        },
    )

    assert exit_code == 0
    assert captured == [checkpoint]
    assert '"resumed":true' in capsys.readouterr().out


def test_cli_resume_refuses_unanswered_tool_call(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    now = datetime(2026, 8, 28, tzinfo=UTC)
    checkpoint = SessionCheckpoint(
        session_id="session_cli_unknown_tool",
        run_id="run_cli_unknown_tool",
        command="ask",
        query="继续解释",
        status=SessionStatus.INTERRUPTED,
        model="deepseek-v4-pro",
        messages=(
            UserMessage(content="继续解释", created_at=now),
            AssistantMessage(
                tool_calls=(
                    ToolCall(
                        tool_call_id="call_cli_unknown",
                        tool_name="file.read_text",
                        arguments={"path": "tracked.txt"},
                    ),
                ),
                created_at=now,
            ),
        ),
    )
    SessionStore(repository).save(checkpoint)

    exit_code = run_cli(
        (
            "--repository",
            str(repository),
            "resume",
            checkpoint.session_id,
        ),
        environment={
            "DEEPSEEK_API_KEY": "fixture-provider-value",
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
        },
    )

    assert exit_code == 3
    assert "不能自动重放" in capsys.readouterr().err


def test_cli_resume_verification_stage_runs_real_matrix_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = initialize_repository(tmp_path)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert configure_runtime_excludes(repository) is True

    async def prepare() -> str:
        """构造崩溃在验证开始前的持久化事务与 checkpoint。"""
        scope = ResourceScope("resume.verification.prepare")
        manager = TransactionManager(repository, scope=scope)
        record = await manager.create(transaction_id="tx_resume_verification")
        specification = manager.draft_acceptance(
            acceptance_id="acceptance_resume_verification",
            user_goal="修改 tracked.txt",
            baseline_reproduction=(("git", "status", "--short"),),
            allowed_paths=("tracked.txt",),
            expected_behaviors=("tracked.txt 已修改",),
            preserved_behaviors=("主工作区不变",),
            verification_commands=(("git", "diff", "--check"),),
            behavior_verification_commands=(("rivet-missing-verifier",),),
            max_wall_seconds=60,
            max_tokens=2_000,
            max_tool_calls=10,
        )
        await manager.freeze_acceptance(
            record.transaction_id,
            specification,
            confirmed=True,
        )
        TransactionFileWriter(
            manager.transaction_boundary(record.transaction_id)
        ).write(
            "tracked.txt",
            "resume verification\n",
        )
        await manager.record_patch_set(record.transaction_id)
        manager.suspend(record.transaction_id)
        scope.assert_empty()
        await scope.close()
        SessionStore(repository).save(
            SessionCheckpoint(
                session_id="session_resume_verification",
                run_id="run_resume_verification",
                transaction_id=record.transaction_id,
                command="fix",
                query="修改 tracked.txt",
                status=SessionStatus.RUNNING,
                stage=SessionStage.VERIFICATION,
            )
        )
        return record.transaction_id

    transaction_id = asyncio.run(prepare())
    exit_code = run_cli(
        (
            "--repository",
            str(repository),
            "--json",
            "resume",
            "session_resume_verification",
        ),
        environment={"XDG_CONFIG_HOME": str(tmp_path / "config")},
    )
    captured = capsys.readouterr()
    resumed = SessionStore(repository).load("session_resume_verification")
    transaction = TransactionStore(repository / ".rivet" / "transactions").load_record(
        transaction_id
    )

    assert exit_code == int(ExitCode.VERIFICATION_FAILED), captured.err
    assert resumed.stage is SessionStage.TERMINAL, captured.err
    assert resumed.status is SessionStatus.BLOCKED
    assert transaction.state is TransactionState.BLOCKED

    abort_code = run_cli(
        (
            "--repository",
            str(repository),
            "--json",
            "abort",
            transaction_id,
        ),
        environment={"XDG_CONFIG_HOME": str(tmp_path / "config")},
    )
    assert abort_code == int(ExitCode.SUCCESS)
