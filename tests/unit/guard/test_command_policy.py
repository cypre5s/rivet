"""验证危险命令与环境变量在进入沙箱前即被拒绝。"""

from __future__ import annotations

import pytest

from rivet.guard.command_policy import CommandPolicy
from rivet.tools.errors import ProcessToolError


@pytest.mark.parametrize(
    "argv",
    (
        ("rm", "-rf", "."),
        ("shred", "secret.txt"),
        ("git", "reset", "--hard"),
        ("git", "clean", "-fdx"),
        ("git", "push", "--force"),
        ("bash", "-c", "touch escaped"),
        ("curl", "https://example.com"),
        ("wget", "https://example.com"),
        ("ssh", "example.com"),
        ("sudo", "true"),
    ),
)
def test_dangerous_or_network_command_is_denied(argv: tuple[str, ...]) -> None:
    with pytest.raises(ProcessToolError, match="安全策略") as captured:
        CommandPolicy().validate(argv)

    assert captured.value.code == "guard.command_denied"


@pytest.mark.parametrize(
    "argv",
    (
        ("python", "-m", "pytest", "-q"),
        ("python", "-c", "print('bounded by sandbox')"),
        ("git", "status", "--short"),
        ("ruff", "check", "."),
    ),
)
def test_expected_development_command_is_allowed(argv: tuple[str, ...]) -> None:
    CommandPolicy().validate(argv)


def test_sensitive_environment_override_is_denied() -> None:
    with pytest.raises(ProcessToolError) as captured:
        CommandPolicy().validate_environment(
            {"PATH": "/usr/bin", "DEEPSEEK_API_KEY": "never-forward"}
        )

    assert captured.value.code == "guard.environment_denied"
