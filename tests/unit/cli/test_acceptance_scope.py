"""验证 FIX 只接受用户显式确认的最小写范围。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.cli.config import load_config
from rivet.cli.errors import CliSecurityError
from rivet.cli.model_commands import build_acceptance_spec
from rivet.cli.parser import build_parser
from rivet.verify.detector import ProjectDetection, ProjectDetector


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    (root / "src" / "auth").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / ".rivet").mkdir()
    (root / "src" / "auth" / "login.py").write_text(
        "def login():\n    return False\n",
        encoding="utf-8",
    )
    (root / "tests" / "acceptance.py").write_text(
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    (root / ".rivet" / "project.toml").write_text(
        """schema_version = 1

[rivet]
model = "deepseek-v4-flash"

[verification]
acceptance = [["python", "tests/acceptance.py"]]
regression = []
static = []
""",
        encoding="utf-8",
    )
    return root


def _detection(repository: Path) -> ProjectDetection:
    return ProjectDetector().detect(repository)


def _build(repository: Path, paths: tuple[str, ...]):
    return build_acceptance_spec(
        repository,
        "修复登录行为",
        detection=_detection(repository),
        explicit_paths=paths,
        config=load_config(repository, environment={}),
    )


def test_explicit_scope_is_frozen_without_repository_wide_expansion(
    repository: Path,
) -> None:
    specification = _build(repository, ("src/auth/login.py",))

    assert specification.write_scope == ("src/auth/login.py",)
    assert specification.allowed_paths == ("src/auth/login.py",)
    assert specification.read_scope == ("src/auth/login.py",)
    assert specification.allowed_new_paths == ()
    assert specification.scope_source == "explicit"
    assert specification.preserved_behaviors == ()
    assert "tests/acceptance.py" in specification.forbidden_paths
    assert ".rivet/project.toml" in specification.forbidden_paths


def test_scope_cannot_cover_independent_acceptance_or_project_config(
    repository: Path,
) -> None:
    for path in ("tests", ".rivet/project.toml"):
        with pytest.raises(CliSecurityError) as captured:
            _build(repository, (path,))

        assert captured.value.code in {
            "acceptance.oracle_overlap",
            "acceptance.write_scope_invalid",
        }


@pytest.mark.parametrize(
    "path",
    (".git/config", ".env", "credentials.json", "../outside.py"),
)
def test_scope_rejects_protected_or_escaping_paths(
    repository: Path,
    path: str,
) -> None:
    with pytest.raises(CliSecurityError) as captured:
        _build(repository, (path,))

    assert captured.value.code == "acceptance.write_scope_invalid"


def test_headless_fix_without_explicit_scope_fails_closed(repository: Path) -> None:
    with pytest.raises(CliSecurityError) as captured:
        _build(repository, ())

    assert captured.value.code == "acceptance.write_scope_required"


def test_fix_parser_accepts_repeatable_explicit_write_scope() -> None:
    arguments = build_parser().parse_args(
        (
            "fix",
            "修复登录",
            "--yes",
            "--allow-write",
            "src/auth/login.py",
            "--allow-write",
            "tests/test_login.py",
            "--allow-read",
            "tests/acceptance.py",
            "--allow-new",
            "src/auth/generated.py",
        )
    )

    assert arguments.allow_write == ["src/auth/login.py", "tests/test_login.py"]
    assert arguments.allow_read == ["tests/acceptance.py"]
    assert arguments.allow_new == ["src/auth/generated.py"]


def test_new_only_fix_requires_explicit_read_and_new_scopes(repository: Path) -> None:
    specification = build_acceptance_spec(
        repository,
        "新增登录审计模块",
        detection=_detection(repository),
        explicit_paths=(),
        explicit_read_paths=("src/auth/login.py",),
        explicit_new_paths=("src/auth/audit.py",),
        config=load_config(repository, environment={}),
    )

    assert specification.write_scope == ("src/auth/audit.py",)
    assert specification.allowed_paths == ("src/auth/audit.py",)
    assert specification.allowed_new_paths == ("src/auth/audit.py",)
    assert specification.read_scope == ("src/auth/login.py",)

    with pytest.raises(CliSecurityError) as captured:
        _build(repository, ("src/auth/missing.py",))
    assert captured.value.code == "acceptance.write_scope_invalid"
