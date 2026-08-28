"""验证确定性验证器在 bubblewrap 缺失时不会裸跑测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.contracts.verification import VerificationStatus
from tests.fixtures.verification.cases import VERIFICATION_CASES
from tests.verification_helpers import run_verification_case


@pytest.mark.asyncio
async def test_verification_without_sandbox_is_inconclusive_not_passed(
    tmp_path: Path,
) -> None:
    correct = next(case for case in VERIFICATION_CASES if case.case_id == "correct_fix")

    prepared = await run_verification_case(
        tmp_path,
        correct,
        use_production_sandbox=True,
        sandbox_executable=tmp_path / "missing-bwrap",
    )

    assert prepared.outcome.verdict.status is VerificationStatus.INCONCLUSIVE
    assert not prepared.outcome.verdict.passed
    assert any(
        "sandbox.unavailable" in result.stderr_summary
        for result in prepared.outcome.verdict.results
    )
    await prepared.manager.abort(prepared.outcome.transaction.transaction_id)
    prepared.scope.assert_empty()
    await prepared.scope.close()
