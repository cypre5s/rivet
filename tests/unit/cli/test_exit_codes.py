"""冻结脚本调用可依赖的退出码。"""

from rivet.cli.exit_codes import ExitCode


def test_exit_codes_are_stable_and_distinct() -> None:
    assert ExitCode.SUCCESS == 0
    assert ExitCode.USAGE == 2
    assert ExitCode.CONFIGURATION == 3
    assert ExitCode.VERIFICATION_FAILED == 4
    assert ExitCode.SECURITY_DENIED == 5
    assert ExitCode.PROVIDER_FAILED == 6
    assert ExitCode.INTERNAL_ERROR == 70
    assert ExitCode.USER_CANCELLED == 130
    assert len({int(exit_code) for exit_code in ExitCode}) == len(ExitCode)
