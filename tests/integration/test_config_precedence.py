"""验证项目内最小配置与有限运行时覆盖。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.cli.config import ConfigOverrides, load_config
from rivet.cli.errors import CliConfigurationError


def _write_project_config(repository: Path, body: str) -> None:
    directory = repository / ".rivet"
    directory.mkdir()
    (directory / "project.toml").write_text(body, encoding="utf-8")


def test_configuration_precedence_is_default_project_environment_cli(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _write_project_config(
        repository,
        'schema_version = 1\n[rivet]\nmodel = "project-model"\n',
    )

    resolved = load_config(
        repository,
        environment={
            "RIVET_MODEL": "environment-model",
            "RIVET_MAX_ROUNDS": "9",
            "RIVET_MAX_TOTAL_TOKENS": "5000",
        },
        overrides=ConfigOverrides(model="cli-model"),
    )

    assert resolved.model == "cli-model"
    assert resolved.max_rounds == 9
    assert resolved.max_total_tokens == 5_000
    assert resolved.sources == {
        "base_url": "default",
        "max_cost_usd": "default",
        "max_rounds": "environment",
        "max_total_tokens": "environment",
        "model": "cli",
    }


@pytest.mark.parametrize(
    "body",
    (
        'schema_version = 1\n[rivet]\napi_key = "forbidden"\n',
        "schema_version = 1\n[rivet]\nsafe_mode = true\n",
        "schema_version = 1\n[rivet]\nmax_rounds = 4\n",
    ),
)
def test_project_configuration_rejects_every_non_model_field(
    tmp_path: Path,
    body: str,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _write_project_config(repository, body)

    with pytest.raises(CliConfigurationError) as captured:
        load_config(repository, environment={})

    assert captured.value.code == "config.project_invalid"


def test_runtime_environment_is_validated_and_never_persisted(tmp_path: Path) -> None:
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
    assert not (repository / ".rivet").exists()
    with pytest.raises(CliConfigurationError, match="max_rounds"):
        load_config(repository, environment={"RIVET_MAX_ROUNDS": "invalid"})


def test_project_configuration_rejects_symlink_file(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    external = tmp_path / "external.toml"
    repository.mkdir()
    (repository / ".rivet").mkdir()
    external.write_text('[rivet]\nmodel = "external"\n', encoding="utf-8")
    (repository / ".rivet" / "project.toml").symlink_to(external)

    with pytest.raises(CliConfigurationError) as captured:
        load_config(repository, environment={})

    assert captured.value.code == "config.project_invalid"


def test_selected_model_is_first_in_static_model_catalog(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    resolved = load_config(
        repository,
        environment={"RIVET_MODEL": "team-reasoner"},
    )

    assert resolved.models[0] == "team-reasoner"
    assert len(resolved.models) == len(set(resolved.models))
