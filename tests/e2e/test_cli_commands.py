"""从 console module 验证 Phase 13 的离线正式命令体验。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

OFFICIAL_COMMANDS = (
    "init",
    "ask",
    "read",
    "plan",
    "fix",
    "verify",
    "diff",
    "apply",
    "abort",
    "trace",
    "resume",
    "modules",
    "doctor",
    "benchmark",
    "config",
    "clean",
)


def _environment(tmp_path: Path) -> dict[str, str]:
    """构造不携带用户凭据的 CLI 测试环境。"""
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(Path(__file__).parents[2] / "src"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }


def _run(
    tmp_path: Path,
    repository: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    """运行隔离环境中的真实 Python 模块入口。"""
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


def test_init_config_modules_doctor_and_clean_are_offline(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    initialized = _run(tmp_path, repository, "init")
    configured = _run(tmp_path, repository, "--json", "config")
    modules = _run(tmp_path, repository, "--json", "modules")
    doctor = _run(tmp_path, repository, "--json", "doctor")
    cleaned = _run(tmp_path, repository, "--json", "clean", "--dry-run")

    assert initialized.returncode == 0
    project_config = repository / ".rivet" / "project.toml"
    assert project_config.is_file()
    assert "DEEPSEEK_API_KEY" not in project_config.read_text(encoding="utf-8")
    for completed in (configured, modules, doctor, cleaned):
        assert completed.returncode == 0, completed.stderr
        cast(dict[str, object], json.loads(completed.stdout))
    module_payload = cast(dict[str, object], json.loads(modules.stdout))
    module_summary = cast(dict[str, object], module_payload["summary"])
    assert module_payload["source"] == "module_runtime"
    assert module_summary["active"] == 0
    assert "credential_value" not in configured.stdout


def test_model_command_without_key_is_classified_without_traceback(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    completed = _run(tmp_path, repository, "ask", "解释仓库")

    assert completed.returncode == 3
    assert "DEEPSEEK_API_KEY" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_init_rejects_symlink_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "repository-link"
    link.symlink_to(target, target_is_directory=True)

    completed = _run(tmp_path, link, "init")

    assert completed.returncode == 3
    assert "符号链接" in completed.stderr
    assert not (target / ".rivet").exists()
