"""验证 fix 的候选写范围来自任务而不是仓库顶层清单。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rivet.cli.errors import CliConfigurationError
from rivet.cli.model_commands import (
    _authorizer,  # pyright: ignore[reportPrivateUsage]
    resolve_task_acceptance_scope,
)
from rivet.cli.parser import build_parser
from rivet.contracts.guard import (
    AuthorizationStatus,
    Permission,
    PermissionRequest,
    PermissionScope,
)
from rivet.guard.permissions import GuardPolicy


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    (root / "src" / "auth").mkdir(parents=True)
    (root / "tests").mkdir()
    for relative in (
        "src/auth/login.py",
        "src/auth/unrelated.py",
        "tests/test_login.py",
        "README.md",
    ):
        (root / relative).write_text(f"{relative}\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "--", ".")
    _git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "initial",
    )
    return root


def test_task_scope_selects_named_target_and_corresponding_test(
    repository: Path,
) -> None:
    scope = resolve_task_acceptance_scope(
        repository,
        "修复 src/auth/login.py 的登录问题",
        explicit_paths=(),
    )

    assert scope.write_scope == ("src/auth/login.py", "tests/test_login.py")
    assert scope.allowed_new_paths == ()
    assert "src/auth/unrelated.py" not in scope.write_scope
    assert "README.md" not in scope.write_scope


def test_explicit_scope_can_allow_one_new_test_but_not_its_directory(
    repository: Path,
) -> None:
    scope = resolve_task_acceptance_scope(
        repository,
        "补充回归测试",
        explicit_paths=("tests/test_login_regression.py",),
    )

    assert scope.write_scope == ("tests/test_login_regression.py",)
    assert scope.allowed_new_paths == ("tests/test_login_regression.py",)
    assert "tests" not in scope.write_scope


@pytest.mark.parametrize(
    "path",
    (
        ".git/config",
        ".env",
        "credentials.json",
        "src/auth/link.py",
        "../outside.py",
    ),
)
def test_explicit_scope_rejects_protected_or_escaping_paths(
    repository: Path,
    tmp_path: Path,
    path: str,
) -> None:
    (tmp_path / "outside.py").write_text("outside\n", encoding="utf-8")
    (repository / "src" / "auth" / "link.py").symlink_to(tmp_path / "outside.py")

    with pytest.raises(CliConfigurationError) as captured:
        resolve_task_acceptance_scope(
            repository,
            "修改显式文件",
            explicit_paths=(path,),
        )

    assert captured.value.code == "acceptance.write_scope_invalid"


def test_headless_fix_without_task_paths_fails_closed(repository: Path) -> None:
    with pytest.raises(CliConfigurationError) as captured:
        resolve_task_acceptance_scope(
            repository,
            "修复登录行为但没有声明文件",
            explicit_paths=(),
        )

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
        )
    )

    assert arguments.allow_write == ["src/auth/login.py", "tests/test_login.py"]


@pytest.mark.parametrize(
    "path",
    ("src/auth/unrelated.py", "README.md", "tests/arbitrary.py"),
)
def test_guard_denies_every_path_outside_frozen_file_scope(path: str) -> None:
    authorize = _authorizer(
        GuardPolicy(headless=True),
        approved=True,
        allowed_paths=("src/auth/login.py", "tests/test_login.py"),
    )
    request = PermissionRequest(
        permission=Permission.WRITE,
        scope=PermissionScope.SPECIFIC_PATHS,
        reason="执行冻结任务",
        run_id="run_scope",
        transaction_id="tx_scope",
        paths=(path,),
    )

    decision = authorize(request)

    assert decision.status is AuthorizationStatus.DENIED
    assert decision.code == "guard.acceptance_scope_denied"
