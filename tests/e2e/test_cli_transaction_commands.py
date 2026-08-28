"""验证跨 CLI 生命周期的事务 diff、恢复与 abort。"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest

from rivet.cli.application import run_cli
from rivet.cli.transaction_commands import run_transaction_command
from rivet.kernel.resources import ResourceScope
from rivet.storage.git_exclude import configure_runtime_excludes
from rivet.storage.sessions import SessionCheckpoint, SessionStatus, SessionStore
from rivet.tools.files import TransactionFileWriter
from rivet.transaction.manager import TransactionManager
from tests.transaction_helpers import acceptance_spec, initialize_repository


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
