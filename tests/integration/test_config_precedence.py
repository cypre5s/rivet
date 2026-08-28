"""验证 CLI、环境、项目、用户和默认配置的唯一优先级。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.cli.config import ConfigOverrides, load_config
from rivet.cli.errors import CliConfigurationError


def _write_configuration(path: Path, body: str) -> None:
    """建立父目录并写入测试 TOML。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_configuration_precedence_is_cli_env_project_user_default(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    xdg_config = tmp_path / "config"
    _write_configuration(
        xdg_config / "rivet" / "config.toml",
        """schema_version = 1
[rivet]
model = "user-model"
max_rounds = 5
max_total_tokens = 5000
""",
    )
    _write_configuration(
        repository / ".rivet" / "project.toml",
        """schema_version = 1
[rivet]
model = "project-model"
max_rounds = 7
""",
    )
    environment = {
        "XDG_CONFIG_HOME": str(xdg_config),
        "RIVET_MODEL": "environment-model",
        "RIVET_MAX_ROUNDS": "9",
    }

    resolved = load_config(
        repository,
        environment=environment,
        overrides=ConfigOverrides(model="cli-model"),
    )

    assert resolved.model == "cli-model"
    assert resolved.max_rounds == 9
    assert resolved.max_total_tokens == 5000
    assert resolved.sources == {
        "base_url": "default",
        "max_cost_usd": "default",
        "max_rounds": "environment",
        "max_total_tokens": "user",
        "model": "cli",
        "safe_mode": "default",
    }


def test_configuration_rejects_secret_fields_and_invalid_environment(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _write_configuration(
        repository / ".rivet" / "project.toml",
        """schema_version = 1
[rivet]
api_key = ""
""",
    )

    with pytest.raises(CliConfigurationError, match="凭据"):
        load_config(repository, environment={})

    (repository / ".rivet" / "project.toml").unlink()
    with pytest.raises(CliConfigurationError, match="RIVET_MAX_ROUNDS"):
        load_config(repository, environment={"RIVET_MAX_ROUNDS": "not-an-int"})


def test_configuration_only_reports_credential_presence(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    credential = "provider-value-that-must-not-appear"

    resolved = load_config(
        repository,
        environment={"DEEPSEEK_API_KEY": credential},
    )
    public = resolved.public_mapping()

    assert public["credential_configured"] is True
    assert credential not in repr(resolved)
    assert credential not in str(public)
