"""用八类真实补丁验证七类 Evidence 最终判定。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from rivet.contracts.transactions import AcceptanceSpec, TransactionState
from rivet.contracts.verification import VerificationKind, VerificationStatus
from rivet.kernel.resources import ResourceScope
from rivet.tools.files import TransactionFileWriter
from rivet.verify.service import VerificationService
from tests.fixtures.verification.cases import (
    VERIFICATION_CASES,
    VerificationFixtureCase,
)
from tests.transaction_helpers import (
    acceptance_spec,
    initialize_repository,
    make_manager,
)
from tests.verification_helpers import fixture_executor, run_verification_case


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
    expected_state = {
        VerificationStatus.PASSED: TransactionState.VERIFIED,
        VerificationStatus.FAILED: TransactionState.REJECTED,
        VerificationStatus.INCONCLUSIVE: TransactionState.INCONCLUSIVE,
        VerificationStatus.BLOCKED: TransactionState.BLOCKED,
        VerificationStatus.CANCELLED: TransactionState.CANCELLED,
    }[case.expected_status]
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
    behavior = next(
        result for result in results if result.step.kind is VerificationKind.BEHAVIOR
    )

    assert baseline.status is VerificationStatus.PASSED
    assert baseline.exit_code == 1
    assert behavior.status is VerificationStatus.PASSED
    assert behavior.exit_code == 0

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
    assert prepared.outcome.transaction.state is TransactionState.CANCELLED

    await prepared.manager.abort(prepared.outcome.transaction.transaction_id)
    prepared.scope.assert_empty()
    await prepared.scope.close()


@pytest.mark.asyncio
async def test_missing_required_tool_produces_blocked_verdict(
    tmp_path: Path,
) -> None:
    prepared = await run_verification_case(
        tmp_path,
        VERIFICATION_CASES[0],
        verification_commands=(("rivet-command-that-does-not-exist",),),
    )

    assert prepared.outcome.verdict.status is VerificationStatus.BLOCKED
    regression = next(
        result
        for result in prepared.outcome.verdict.results
        if result.step.kind is VerificationKind.REGRESSION
    )
    assert regression.status is VerificationStatus.BLOCKED
    assert "缺少可执行文件" in regression.stderr_summary
    resource = next(
        result
        for result in prepared.outcome.verdict.results
        if result.step.kind is VerificationKind.RESOURCE
    )
    assert resource.status is VerificationStatus.BLOCKED
    assert prepared.outcome.transaction.state is TransactionState.BLOCKED

    await prepared.manager.abort(prepared.outcome.transaction.transaction_id)
    prepared.scope.assert_empty()
    await prepared.scope.close()


@pytest.mark.asyncio
async def test_preserved_behavior_without_regression_evidence_is_inconclusive(
    tmp_path: Path,
) -> None:
    prepared = await run_verification_case(
        tmp_path,
        VERIFICATION_CASES[0],
        verification_commands=(),
    )
    binding = next(
        result
        for result in prepared.outcome.verdict.results
        if result.step.kind is VerificationKind.BINDING
    )

    assert binding.status is VerificationStatus.INCONCLUSIVE
    assert prepared.outcome.verdict.status is VerificationStatus.INCONCLUSIVE
    assert prepared.outcome.transaction.state is TransactionState.INCONCLUSIVE

    await prepared.manager.abort(prepared.outcome.transaction.transaction_id)
    prepared.scope.assert_empty()
    await prepared.scope.close()


@pytest.mark.asyncio
async def test_scope_rejects_new_file_not_declared_in_allowed_new_paths(
    tmp_path: Path,
) -> None:
    repository = initialize_repository(tmp_path)
    scope = ResourceScope("verify.scope.new-file")
    manager = make_manager(repository, tmp_path, scope)
    specification = acceptance_spec(
        acceptance_id="acceptance_scope_new_file"
    ).model_copy(
        update={
            "allowed_paths": ("tracked.txt", "unexpected.py"),
            "write_scope": ("tracked.txt", "unexpected.py"),
            "allowed_new_paths": (),
        }
    )
    record = await manager.create(
        specification,
        confirmed=True,
        transaction_id="tx_scope_new_file",
    )
    writer = TransactionFileWriter(manager.transaction_boundary(record.transaction_id))
    writer.create("unexpected.py", "print('unexpected')\n")
    await manager.record_patch_set(record.transaction_id, patch_id="patch_scope_new")
    await manager.begin_verification(record.transaction_id)

    outcome = await VerificationService(
        manager,
        scope=scope,
        executor_factory=fixture_executor,
    ).verify(record.transaction_id)
    scope_result = next(
        result
        for result in outcome.verdict.results
        if result.step.kind is VerificationKind.SCOPE
    )

    assert scope_result.status is VerificationStatus.FAILED
    assert outcome.transaction.state is TransactionState.REJECTED

    await manager.abort(record.transaction_id)
    scope.assert_empty()
    await scope.close()


def test_acceptance_requires_an_independent_behavior_verifier() -> None:
    payload = acceptance_spec().model_dump(mode="json")
    payload["behavior_verification_commands"] = []

    with pytest.raises(ValidationError, match="behavior_verification_commands"):
        AcceptanceSpec.model_validate(payload)


@pytest.mark.asyncio
async def test_verification_accepts_separate_clean_runtime_scope(
    tmp_path: Path,
) -> None:
    verification_scope = ResourceScope("verify.separate.runtime")
    prepared = await run_verification_case(
        tmp_path,
        VERIFICATION_CASES[0],
        verification_scope=verification_scope,
    )
    resource = next(
        result
        for result in prepared.outcome.verdict.results
        if result.step.kind is VerificationKind.RESOURCE
    )

    assert prepared.outcome.verdict.status is VerificationStatus.PASSED
    assert resource.status is VerificationStatus.PASSED
    verification_scope.assert_empty()

    await prepared.manager.abort(prepared.outcome.transaction.transaction_id)
    prepared.scope.assert_empty()
    await verification_scope.close()
    await prepared.scope.close()
