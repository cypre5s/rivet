"""从 console module 验证最终极简 CLI 的离线表面。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rivet.cli.parser import OFFICIAL_COMMANDS

REMOVED_COMMANDS = (
    "benchmark",
    "clean",
    "config",
    "doctor",
    "export",
    "modules",
    "plan",
    "read",
    "resume",
    "trace",
)


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }


def _run(
    tmp_path: Path,
    repository: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (
            sys.executable,
            "-m",
            "rivet",
            "--repository",
            str(repository),
            *arguments,
        ),
        cwd=repository,
        env=_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


@pytest.mark.parametrize("command", OFFICIAL_COMMANDS)
def test_every_official_command_has_help(tmp_path: Path, command: str) -> None:
    completed = _run(tmp_path, tmp_path, command, "--help")

    assert completed.returncode == 0
    assert "usage:" in completed.stdout
    assert completed.stderr == ""


@pytest.mark.parametrize("command", REMOVED_COMMANDS)
def test_removed_product_command_is_rejected(tmp_path: Path, command: str) -> None:
    completed = _run(tmp_path, tmp_path, command, "--help")

    assert completed.returncode == 2
    assert "invalid choice" in completed.stderr


def test_init_preview_is_read_only_and_confirmed_init_writes_only_project_config(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    previewed = _run(tmp_path, repository, "--json", "init")
    assert previewed.returncode == 0, previewed.stderr
    assert json.loads(previewed.stdout)["created"] is False
    assert not (repository / ".rivet").exists()

    initialized = _run(tmp_path, repository, "--json", "init", "--yes")
    assert initialized.returncode == 0, initialized.stderr
    assert json.loads(initialized.stdout)["created"] is True
    config_path = repository / ".rivet" / "project.toml"
    assert config_path.is_file()
    assert set(path.name for path in (repository / ".rivet").iterdir()) == {
        "project.toml"
    }
    serialized = config_path.read_text(encoding="utf-8")
    assert "DEEPSEEK_API_KEY" not in serialized
    assert "acceptance = []" in serialized


def test_ask_without_key_is_classified_without_traceback(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    completed = _run(tmp_path, repository, "ask", "解释仓库")

    assert completed.returncode == 3
    assert "DEEPSEEK_API_KEY" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_fix_without_acceptance_fails_before_credential_or_runtime_cost(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    completed = _run(tmp_path, repository, "fix", "修改 tracked.txt", "--yes")

    assert completed.returncode == 4
    assert "verification.acceptance_not_ready" in completed.stderr
    assert "DEEPSEEK_API_KEY" not in completed.stderr
    assert not (repository / ".rivet").exists()


def test_init_rejects_symlink_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "repository-link"
    link.symlink_to(target, target_is_directory=True)

    completed = _run(tmp_path, link, "init")

    assert completed.returncode == 3
    assert "符号链接" in completed.stderr
    assert not (target / ".rivet").exists()
