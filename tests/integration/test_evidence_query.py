"""验证历史 Evidence 查询始终先复核哈希，日志保持显式惰性加载。"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from rivet.transaction.errors import TransactionError
from rivet.transaction.store import TransactionStore
from rivet.verify.evidence_query import EvidenceQueryService
from tests.fixtures.verification.cases import VERIFICATION_CASES
from tests.verification_helpers import run_verification_case


@pytest.mark.asyncio
async def test_evidence_detail_and_log_are_hash_verified_and_complete(
    tmp_path: Path,
) -> None:
    prepared = await run_verification_case(tmp_path, VERIFICATION_CASES[0])
    transaction_id = prepared.outcome.transaction.transaction_id
    service = EvidenceQueryService(
        TransactionStore(tmp_path / "state" / "transactions")
    )

    detail = service.detail(transaction_id)
    results = cast(list[dict[str, object]], detail["verification_results"])
    files = cast(list[dict[str, object]], detail["files"])

    assert detail["evidence_verified"] is True
    assert detail["verdict_status"] == "PASSED"
    assert detail["apply_eligible"] is True
    assert detail["base_commit"] == prepared.outcome.verdict.base_commit
    assert detail["acceptance_sha256"]
    assert detail["patch_sha256"]
    assert detail["manifest_sha256"]
    assert isinstance(results, list)
    assert {cast(str, result["kind"]) for result in results} == {
        "BASELINE",
        "BEHAVIOR",
        "REGRESSION",
        "SCOPE",
        "SECRET",
        "BINDING",
        "RESOURCE",
    }
    assert all("argv" in result and "duration_ms" in result for result in results)
    assert isinstance(files, list)
    assert {cast(str, item["path"]) for item in files} >= {
        "verdict.json",
        "matrix.json",
    }

    log = service.log(transaction_id)
    assert log["transaction_id"] == transaction_id
    assert log["step_id"]
    assert log["log_sha256"]
    assert isinstance(log["content"], str)

    manifest_path = prepared.outcome.evidence_directory / "matrix.json"
    manifest_path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(TransactionError, match="哈希"):
        service.detail(transaction_id)

    await prepared.manager.abort(transaction_id)
    prepared.scope.assert_empty()
    await prepared.scope.close()
