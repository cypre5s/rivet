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


def test_module_lifecycle_cli_persists_policy_and_uses_stable_exit_codes(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    listed = _run(tmp_path, repository, "modules", "list", "--json")
    shown = _run(
        tmp_path,
        repository,
        "modules",
        "show",
        "context.lsp",
        "--json",
    )
    blocked = _run(
        tmp_path,
        repository,
        "modules",
        "disable",
        "context.syntax",
        "--json",
    )
    disabled = _run(
        tmp_path,
        repository,
        "modules",
        "disable",
        "context.lsp",
        "--json",
    )
    persisted = _run(
        tmp_path,
        repository,
        "modules",
        "show",
        "context.lsp",
        "--json",
    )
    refused_wake = _run(
        tmp_path,
        repository,
        "modules",
        "wake",
        "context.lsp",
        "--json",
    )
    enabled = _run(
        tmp_path,
        repository,
        "modules",
        "enable",
        "context.lsp",
        "--json",
    )
    woken = _run(
        tmp_path,
        repository,
        "modules",
        "wake",
        "context.lsp",
        "--json",
    )
    slept = _run(
        tmp_path,
        repository,
        "modules",
        "sleep",
        "context.lsp",
        "--json",
    )
    disabled_again = _run(
        tmp_path,
        repository,
        "modules",
        "disable",
        "context.lsp",
        "--json",
    )
    missing = _run(
        tmp_path,
        repository,
        "modules",
        "show",
        "unknown.module",
        "--json",
    )
    invalid_timeout = _run(
        tmp_path,
        repository,
        "modules",
        "sleep",
        "context.lsp",
        "--timeout",
        "-1",
    )

    assert listed.returncode == 0, listed.stderr
    assert shown.returncode == 0, shown.stderr
    assert blocked.returncode == 5
    assert "module.active_dependents" in blocked.stderr
    assert disabled.returncode == 0, disabled.stderr
    persisted_module = cast(
        dict[str, object],
        cast(dict[str, object], json.loads(persisted.stdout))["module"],
    )
    assert persisted_module["persisted_override"] is False
    assert persisted_module["effective_enabled"] is False
    assert refused_wake.returncode == 5
    assert "module.dependency_disabled" in refused_wake.stderr
    assert enabled.returncode == 0, enabled.stderr
    assert cast(dict[str, object], json.loads(enabled.stdout))["current_state"] == (
        "INACTIVE"
    )
    assert woken.returncode == 0, woken.stderr
    assert cast(dict[str, object], json.loads(woken.stdout))["current_state"] == (
        "ACTIVE"
    )
    assert slept.returncode == 0, slept.stderr
    assert disabled_again.returncode == 0, disabled_again.stderr
    assert missing.returncode == 3
    assert "module.not_found" in missing.stderr
    assert invalid_timeout.returncode == 2
    assert "module.input_invalid" in invalid_timeout.stderr


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
