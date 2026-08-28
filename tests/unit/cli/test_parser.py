"""冻结正式命令、参数和全局运行选项。"""

from __future__ import annotations

from argparse import Namespace

import pytest

from rivet.cli.parser import OFFICIAL_COMMANDS, build_internal_parser, build_parser


def test_parser_exposes_every_official_command() -> None:
    parser = build_parser()
    help_text = parser.format_help()

    assert OFFICIAL_COMMANDS == (
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
    assert all(command in help_text for command in OFFICIAL_COMMANDS)
    assert "internal" not in help_text


@pytest.mark.parametrize("command", OFFICIAL_COMMANDS)
def test_every_command_help_returns_success(command: str) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as captured:
        parser.parse_args((command, "--help"))

    assert captured.value.code == 0


def test_parser_preserves_structured_arguments() -> None:
    arguments: Namespace = build_parser().parse_args(
        (
            "--repository",
            "/repo",
            "--json",
            "--model",
            "deepseek-v4-flash",
            "fix",
            "修复边界",
            "--yes",
            "--dirty-policy",
            "snapshot",
        )
    )

    assert arguments.command == "fix"
    assert arguments.task == "修复边界"
    assert arguments.model == "deepseek-v4-flash"
    assert arguments.yes is True
    assert arguments.dirty_policy == "snapshot"


def test_apply_requires_transaction_id() -> None:
    with pytest.raises(SystemExit) as captured:
        build_parser().parse_args(("apply",))

    assert captured.value.code == 2


def test_internal_worker_uses_separate_hidden_parser() -> None:
    arguments = build_internal_parser().parse_args(("worker", "--stdio"))

    assert arguments.command == "internal"
    assert arguments.internal_command == "worker"
    assert arguments.stdio is True
