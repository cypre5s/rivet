"""验证 CLI、环境、项目、用户和默认配置的唯一优先级。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from rivet.cli.config import ConfigOverrides, load_config, save_user_config
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
        "models": "default",
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


def test_project_configuration_rejects_symlink_runtime_directory(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    external = tmp_path / "external"
    repository.mkdir()
    external.mkdir()
    (external / "project.toml").write_text(
        "schema_version = 1\n[rivet]\nsafe_mode = true\n",
        encoding="utf-8",
    )
    (repository / ".rivet").symlink_to(external, target_is_directory=True)

    with pytest.raises(CliConfigurationError, match="符号链接"):
        load_config(repository, environment={})


def test_configuration_exposes_a_deduplicated_model_catalog(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    xdg_config = tmp_path / "config"
    _write_configuration(
        xdg_config / "rivet" / "config.toml",
        """schema_version = 1
[rivet]
model = "team-reasoner"
models = ["team-chat", "team-reasoner"]
""",
    )

    resolved = load_config(
        repository,
        environment={"XDG_CONFIG_HOME": str(xdg_config)},
    )

    assert resolved.model == "team-reasoner"
    assert resolved.models == ("team-chat", "team-reasoner")
    assert resolved.public_mapping()["models"] == ["team-chat", "team-reasoner"]
    assert resolved.sources["models"] == "user"


def test_selected_model_is_available_even_when_a_stronger_layer_selects_it(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    xdg_config = tmp_path / "config"
    _write_configuration(
        xdg_config / "rivet" / "config.toml",
        """schema_version = 1
[rivet]
models = ["team-chat", "team-reasoner"]
""",
    )

    resolved = load_config(
        repository,
        environment={
            "XDG_CONFIG_HOME": str(xdg_config),
            "RIVET_MODEL": "emergency-model",
        },
    )

    assert resolved.models == ("emergency-model", "team-chat", "team-reasoner")


def test_user_configuration_is_written_atomically_without_credentials(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    xdg_config = tmp_path / "config"
    environment = {
        "XDG_CONFIG_HOME": str(xdg_config),
        "DEEPSEEK_API_KEY": "must-remain-in-memory",
    }

    path = save_user_config(
        {
            "base_url": "https://gateway.example.test/v1",
            "max_cost_usd": "1.25",
            "max_rounds": 12,
            "max_total_tokens": 24_000,
            "model": "team-reasoner",
            "models": ["team-chat", "team-reasoner"],
            "safe_mode": True,
        },
        environment=environment,
    )

    serialized = path.read_text(encoding="utf-8")
    assert path.stat().st_mode & 0o777 == 0o600
    assert "team-chat" in serialized
    assert "must-remain-in-memory" not in serialized
    assert "api_key" not in serialized.casefold()
    assert not list(path.parent.glob("*.tmp"))
    resolved = load_config(repository, environment=environment)
    assert resolved.model == "team-reasoner"
    assert resolved.models == ("team-chat", "team-reasoner")
    assert resolved.max_cost_usd == Decimal("1.25")


def test_user_configuration_rejects_secrets_and_symlink_targets(
    tmp_path: Path,
) -> None:
    xdg_config = tmp_path / "config"
    environment = {"XDG_CONFIG_HOME": str(xdg_config)}

    with pytest.raises(CliConfigurationError, match="凭据"):
        save_user_config({"api_key": "never-write-this"}, environment=environment)

    target = tmp_path / "outside.toml"
    target.write_text("outside\n", encoding="utf-8")
    config_path = xdg_config / "rivet" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.symlink_to(target)
    with pytest.raises(CliConfigurationError, match="符号链接"):
        save_user_config({"model": "safe-model"}, environment=environment)
    assert target.read_text(encoding="utf-8") == "outside\n"


def test_configuration_rejects_models_that_downstream_contracts_cannot_run(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    xdg_config = tmp_path / "config"
    _write_configuration(
        xdg_config / "rivet" / "config.toml",
        """schema_version = 1
[rivet]
model = "valid-model"
models = ["valid-model", "Bad Model"]
""",
    )

    with pytest.raises(CliConfigurationError, match="models"):
        load_config(
            repository,
            environment={"XDG_CONFIG_HOME": str(xdg_config)},
        )
