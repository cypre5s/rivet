"""验证所有非 PASSED 补丁都无法污染主工作区。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.contracts.transactions import TransactionState
from rivet.contracts.verification import VerificationStatus
from rivet.transaction.errors import TransactionError
from tests.fixtures.verification.cases import (
    VERIFICATION_CASES,
    VerificationFixtureCase,
)
from tests.transaction_helpers import worktree_digest
from tests.verification_helpers import run_verification_case

FAILED_CASES = tuple(
    case
    for case in VERIFICATION_CASES
    if case.expected_status is not VerificationStatus.PASSED
)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", FAILED_CASES, ids=lambda case: case.case_id)
async def test_failed_or_inconclusive_patch_cannot_apply(
    tmp_path: Path,
    case: VerificationFixtureCase,
) -> None:
    prepared = await run_verification_case(tmp_path, case)
    digest_before = worktree_digest(prepared.repository)

    with pytest.raises(TransactionError, match="VERIFIED"):
        await prepared.manager.apply(prepared.outcome.transaction.transaction_id)

    assert worktree_digest(prepared.repository) == digest_before
    await prepared.manager.abort(prepared.outcome.transaction.transaction_id)
    prepared.scope.assert_empty()
    await prepared.scope.close()


@pytest.mark.asyncio
async def test_passed_patch_applies_and_retains_evidence(tmp_path: Path) -> None:
    prepared = await run_verification_case(tmp_path, VERIFICATION_CASES[0])
    evidence_directory = prepared.outcome.evidence_directory

    applied = await prepared.manager.apply(prepared.outcome.transaction.transaction_id)

    assert applied.state is TransactionState.APPLIED
    assert "return value * 2" in (prepared.repository / "app.py").read_text(
        encoding="utf-8"
    )
    assert evidence_directory.is_dir()
    prepared.scope.assert_empty()
    await prepared.scope.close()


@pytest.mark.asyncio
async def test_tampered_evidence_blocks_previously_passed_patch(tmp_path: Path) -> None:
    prepared = await run_verification_case(tmp_path, VERIFICATION_CASES[0])
    digest_before = worktree_digest(prepared.repository)
    summary_path = prepared.outcome.evidence_directory / "summary.md"
    summary_path.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(TransactionError) as captured:
        await prepared.manager.apply(prepared.outcome.transaction.transaction_id)

    assert captured.value.code == "transaction.evidence_hash_mismatch"
    assert worktree_digest(prepared.repository) == digest_before
    await prepared.manager.abort(prepared.outcome.transaction.transaction_id)
    prepared.scope.assert_empty()
    await prepared.scope.close()
