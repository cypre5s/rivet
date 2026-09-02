"""冻结最终公开命令、参数和隐藏 Worker 入口。"""

from __future__ import annotations

import pytest

from rivet.cli.parser import OFFICIAL_COMMANDS, build_internal_parser, build_parser

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


def test_parser_exposes_exact_public_commands() -> None:
    parser = build_parser()
    help_text = parser.format_help()

    assert OFFICIAL_COMMANDS == (
        "init",
        "ask",
        "fix",
        "diff",
        "verify",
        "apply",
        "abort",
    )
    assert all(command in help_text for command in OFFICIAL_COMMANDS)
    assert all(command not in help_text for command in REMOVED_COMMANDS)
    assert "internal" not in help_text


@pytest.mark.parametrize("command", OFFICIAL_COMMANDS)
def test_every_command_help_returns_success(command: str) -> None:
    with pytest.raises(SystemExit) as captured:
        build_parser().parse_args((command, "--help"))

    assert captured.value.code == 0


def test_fix_preserves_only_explicit_scope_and_confirmation_arguments() -> None:
    arguments = build_parser().parse_args(
        (
            "--repository",
            "/repo",
            "--json",
            "--model",
            "deepseek-v4-flash",
            "fix",
            "修复边界",
            "--yes",
            "--allow-read",
            "tests/test_app.py",
            "--allow-write",
            "src/rivet/app.py",
            "--allow-new",
            "tests/test_new_app.py",
            "--acceptance-sha256",
            "sha256:" + "a" * 64,
            "--base-commit",
            "b" * 40,
        )
    )

    assert arguments.command == "fix"
    assert arguments.task == "修复边界"
    assert arguments.model == "deepseek-v4-flash"
    assert arguments.yes is True
    assert arguments.allow_read == ["tests/test_app.py"]
    assert arguments.allow_write == ["src/rivet/app.py"]
    assert arguments.allow_new == ["tests/test_new_app.py"]
    assert arguments.acceptance_sha256 == "sha256:" + "a" * 64
    assert arguments.base_commit == "b" * 40
    assert not hasattr(arguments, "candidate_only")
    assert not hasattr(arguments, "dirty_policy")
    assert not hasattr(arguments, "safe_mode")


@pytest.mark.parametrize("flag", ("--candidate-only", "--dirty-policy", "--safe-mode"))
def test_removed_fix_flag_is_rejected(flag: str) -> None:
    argv = ["fix", "修复", flag]
    if flag == "--dirty-policy":
        argv.append("snapshot")

    with pytest.raises(SystemExit) as captured:
        build_parser().parse_args(tuple(argv))

    assert captured.value.code == 2


@pytest.mark.parametrize("command", REMOVED_COMMANDS)
def test_removed_command_is_rejected(command: str) -> None:
    with pytest.raises(SystemExit) as captured:
        build_parser().parse_args((command,))

    assert captured.value.code == 2


def test_init_requires_explicit_yes_only_for_writing() -> None:
    assert build_parser().parse_args(("init",)).yes is False
    assert build_parser().parse_args(("init", "--yes")).yes is True


def test_apply_requires_transaction_id() -> None:
    with pytest.raises(SystemExit) as captured:
        build_parser().parse_args(("apply",))

    assert captured.value.code == 2


def test_internal_worker_uses_separate_hidden_parser() -> None:
    arguments = build_internal_parser().parse_args(("worker", "--stdio"))

    assert arguments.command == "internal"
    assert arguments.internal_command == "worker"
    assert arguments.stdio is True
