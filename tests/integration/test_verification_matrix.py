"""用八类真实补丁验证 V0-V10 最终判定。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.contracts.transactions import TransactionState
from rivet.contracts.verification import VerificationKind, VerificationStatus
from rivet.transaction.errors import TransactionError
from rivet.verify.detector import ProjectConfiguration
from tests.fixtures.verification.cases import (
    VERIFICATION_CASES,
    VerificationFixtureCase,
)
from tests.verification_helpers import run_verification_case


@pytest.mark.asyncio
@pytest.mark.parametrize("case", VERIFICATION_CASES, ids=lambda case: case.case_id)
async def test_verification_matrix_classifies_eight_patch_fixtures(
    tmp_path: Path,
    case: VerificationFixtureCase,
) -> None:
    prepared = await run_verification_case(tmp_path, case)
    outcome = prepared.outcome

    assert outcome.verdict.status is case.expected_status
    assert outcome.verdict.passed is (case.expected_status is VerificationStatus.PASSED)
    assert {result.step.kind for result in outcome.verdict.results} == set(
        VerificationKind
    )
    expected_state = (
        TransactionState.VERIFIED
        if case.expected_status is VerificationStatus.PASSED
        else TransactionState.REJECTED
    )
    assert outcome.transaction.state is expected_state
    assert outcome.evidence_directory.is_dir()
    assert not tuple((tmp_path / "cache" / "rivet" / "verification").rglob("attempt_*"))

    await prepared.manager.abort(outcome.transaction.transaction_id)
    prepared.scope.assert_empty()
    await prepared.scope.close()


@pytest.mark.asyncio
async def test_correct_fix_records_failed_baseline_and_passed_patched_checks(
    tmp_path: Path,
) -> None:
    case = VERIFICATION_CASES[0]
    prepared = await run_verification_case(tmp_path, case)
    results = prepared.outcome.verdict.results

    baseline = next(
        result for result in results if result.step.kind is VerificationKind.BASELINE
    )
    reproduction = next(
        result
        for result in results
        if result.step.kind is VerificationKind.REPRODUCTION
    )

    assert baseline.status is VerificationStatus.PASSED
    assert baseline.exit_code == 1
    assert reproduction.status is VerificationStatus.PASSED
    assert reproduction.exit_code == 0

    await prepared.manager.abort(prepared.outcome.transaction.transaction_id)
    prepared.scope.assert_empty()
    await prepared.scope.close()


@pytest.mark.asyncio
async def test_user_cancellation_produces_cancelled_verdict(tmp_path: Path) -> None:
    prepared = await run_verification_case(
        tmp_path,
        VERIFICATION_CASES[0],
        cancelled=lambda: True,
    )

    assert prepared.outcome.verdict.status is VerificationStatus.CANCELLED
    assert prepared.outcome.transaction.state is TransactionState.REJECTED

    await prepared.manager.abort(prepared.outcome.transaction.transaction_id)
    prepared.scope.assert_empty()
    await prepared.scope.close()


@pytest.mark.asyncio
async def test_missing_required_tool_produces_inconclusive_verdict(
    tmp_path: Path,
) -> None:
    prepared = await run_verification_case(
        tmp_path,
        VERIFICATION_CASES[0],
        project_configuration=ProjectConfiguration(
            regression=(("rivet-command-that-does-not-exist",),),
        ),
    )

    assert prepared.outcome.verdict.status is VerificationStatus.INCONCLUSIVE
    environment = next(
        result
        for result in prepared.outcome.verdict.results
        if result.step.kind is VerificationKind.ENVIRONMENT
    )
    assert environment.status is VerificationStatus.INCONCLUSIVE

    await prepared.manager.abort(prepared.outcome.transaction.transaction_id)
    prepared.scope.assert_empty()
    await prepared.scope.close()


@pytest.mark.asyncio
async def test_old_test_only_cannot_release_hardcoded_patch(tmp_path: Path) -> None:
    case = VerificationFixtureCase(
        case_id="old_test_trap",
        implementation="def transform(value: int) -> int:\n    return 4\n",
        expected_status=VerificationStatus.INCONCLUSIVE,
        baseline_script="check_target.py",
        targeted_script="check_target.py",
    )
    python_command = ("python", "check_target.py")
    prepared = await run_verification_case(
        tmp_path,
        case,
        behavior_verification_commands=(),
        project_configuration=ProjectConfiguration(
            related=(python_command,),
            regression=(python_command,),
            static=(("python", "-m", "compileall", "-q", "app.py"),),
        ),
    )

    acceptance = next(
        result
        for result in prepared.outcome.verdict.results
        if result.step.kind is VerificationKind.ACCEPTANCE
    )
    assert prepared.outcome.verdict.status is VerificationStatus.INCONCLUSIVE
    assert prepared.outcome.verdict.passed is False
    assert acceptance.status is VerificationStatus.INCONCLUSIVE
    assert prepared.outcome.transaction.state is TransactionState.REJECTED
    with pytest.raises(TransactionError, match="只有 VERIFIED"):
        await prepared.manager.apply(prepared.outcome.transaction.transaction_id)

    await prepared.manager.abort(prepared.outcome.transaction.transaction_id)
    prepared.scope.assert_empty()
    await prepared.scope.close()
