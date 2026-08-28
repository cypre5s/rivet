"""定义 Phase 10 必须区分的八类补丁结果。"""

from __future__ import annotations

from dataclasses import dataclass

from rivet.contracts.verification import VerificationStatus


@dataclass(frozen=True, slots=True)
class VerificationFixtureCase:
    """描述一个补丁实现、附加变更和预期程序化结论。"""

    case_id: str
    implementation: str
    expected_status: VerificationStatus
    extra_changes: tuple[tuple[str, str], ...] = ()
    baseline_script: str = "check_general.py"
    targeted_script: str = "check_target.py"
    max_wall_seconds: int = 20


VERIFICATION_CASES = (
    VerificationFixtureCase(
        case_id="correct_fix",
        implementation="def transform(value: int) -> int:\n    return value * 2\n",
        expected_status=VerificationStatus.PASSED,
    ),
    VerificationFixtureCase(
        case_id="hardcoded_test_value",
        implementation="def transform(value: int) -> int:\n    return 4\n",
        expected_status=VerificationStatus.FAILED,
    ),
    VerificationFixtureCase(
        case_id="target_passes_regression_fails",
        implementation="def transform(value: int) -> int:\n    return value * 2\n",
        extra_changes=(("stable.txt", "broken\n"),),
        expected_status=VerificationStatus.FAILED,
    ),
    VerificationFixtureCase(
        case_id="forbidden_path_changed",
        implementation="def transform(value: int) -> int:\n    return value * 2\n",
        extra_changes=(("forbidden.txt", "changed\n"),),
        expected_status=VerificationStatus.FAILED,
    ),
    VerificationFixtureCase(
        case_id="secret_introduced",
        implementation="def transform(value: int) -> int:\n    return value * 2\n",
        extra_changes=(("secret.txt", "__RUNTIME_SECRET__"),),
        expected_status=VerificationStatus.FAILED,
    ),
    VerificationFixtureCase(
        case_id="target_timeout",
        implementation="def transform(value: int) -> int:\n    return value * 2\n",
        targeted_script="check_slow.py",
        max_wall_seconds=1,
        expected_status=VerificationStatus.INCONCLUSIVE,
    ),
    VerificationFixtureCase(
        case_id="acceptance_and_test_conflict",
        implementation="def transform(value: int) -> int:\n    return value * 2\n",
        baseline_script="check_already_passing.py",
        expected_status=VerificationStatus.FAILED,
    ),
    VerificationFixtureCase(
        case_id="correct_alternative",
        implementation="def transform(value: int) -> int:\n    return value + value\n",
        expected_status=VerificationStatus.PASSED,
    ),
)
