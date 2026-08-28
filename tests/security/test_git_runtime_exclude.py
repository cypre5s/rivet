"""验证运行状态不污染 Git，而 project.toml 仍可被跟踪。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from rivet.storage.git_exclude import configure_runtime_excludes


def _git(repository: Path, *arguments: str) -> str:
    """运行固定测试 Git 命令。"""
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        env={
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    )
    return completed.stdout


def test_runtime_is_ignored_but_project_config_remains_visible(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")

    assert configure_runtime_excludes(repository) is True
    assert configure_runtime_excludes(repository) is True
    runtime = repository / ".rivet"
    runtime.mkdir()
    (runtime / "state.sqlite3").write_text("runtime", encoding="utf-8")
    (runtime / "project.toml").write_text("schema_version = 1\n", encoding="utf-8")

    status = _git(repository, "status", "--short", "--untracked-files=all")
    exclude = (repository / ".git" / "info" / "exclude").read_text(encoding="utf-8")

    assert status == "?? .rivet/project.toml\n"
    assert exclude.count(".rivet/*") == 1
    assert exclude.count("!.rivet/project.toml") == 1


def test_non_git_directory_is_not_modified(tmp_path: Path) -> None:
    assert configure_runtime_excludes(tmp_path) is False
    assert not (tmp_path / ".git").exists()


def test_runtime_exclude_rejects_symlink_target(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    exclude = repository / ".git" / "info" / "exclude"
    external = tmp_path / "external-exclude"
    external.write_text("user rule\n", encoding="utf-8")
    exclude.unlink()
    exclude.symlink_to(external)

    with pytest.raises(ValueError, match="符号链接"):
        configure_runtime_excludes(repository)

    assert external.read_text(encoding="utf-8") == "user rule\n"
