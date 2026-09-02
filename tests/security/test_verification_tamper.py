"""验证验收、补丁、证据和秘密篡改均失败关闭。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.kernel.resources import ResourceScope
from rivet.tools.files import TransactionFileWriter
from rivet.transaction.errors import TransactionError
from rivet.verify.evidence import EvidenceBundleWriter
from rivet.verify.service import VerificationService
from tests.fixtures.verification.cases import VERIFICATION_CASES
from tests.transaction_helpers import (
    acceptance_spec,
    initialize_repository,
    make_manager,
)
from tests.verification_helpers import run_verification_case


@pytest.mark.asyncio
async def test_acceptance_hash_tamper_stops_before_commands(tmp_path: Path) -> None:
    repository = initialize_repository(tmp_path)
    scope = ResourceScope("verify.acceptance.tamper")
    manager = make_manager(repository, tmp_path, scope)
    record = await manager.create(
        acceptance_spec(),
        confirmed=True,
        transaction_id="tx_acceptance_tamper",
    )
    writer = TransactionFileWriter(manager.transaction_boundary(record.transaction_id))
    writer.write("tracked.txt", "patched\n")
    await manager.record_patch_set(
        record.transaction_id, patch_id="patch_acceptance_tamper"
    )
    await manager.begin_verification(record.transaction_id)
    acceptance_path = (
        tmp_path
        / "state"
        / "transactions"
        / record.transaction_id
        / "acceptance_spec.json"
    )
    acceptance_path.chmod(0o600)
    acceptance_path.write_text("{}\n", encoding="utf-8")
    service = VerificationService(manager, scope=scope)

    with pytest.raises(TransactionError) as captured:
        await service.verify(record.transaction_id)

    assert captured.value.code == "transaction.acceptance_invalid"
    assert not manager.evidence_root(record.transaction_id).exists()
    await manager.abort(record.transaction_id)
    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_secret_is_never_persisted_in_evidence(tmp_path: Path) -> None:
    secret_case = next(
        case for case in VERIFICATION_CASES if case.case_id == "secret_introduced"
    )
    prepared = await run_verification_case(tmp_path, secret_case)
    assert prepared.runtime_secret is not None

    bundle_bytes = b"".join(
        path.read_bytes()
        for path in prepared.outcome.evidence_directory.rglob("*")
        if path.is_file()
    )

    assert prepared.runtime_secret.encode() not in bundle_bytes
    assert EvidenceBundleWriter(prepared.outcome.evidence_directory.parent).verify(
        prepared.outcome.evidence_directory
    )

    await prepared.manager.abort(prepared.outcome.transaction.transaction_id)
    prepared.scope.assert_empty()
    await prepared.scope.close()


@pytest.mark.asyncio
async def test_patch_hash_tamper_stops_before_evidence(tmp_path: Path) -> None:
    repository = initialize_repository(tmp_path)
    scope = ResourceScope("verify.patch.tamper")
    manager = make_manager(repository, tmp_path, scope)
    record = await manager.create(
        acceptance_spec(),
        confirmed=True,
        transaction_id="tx_patch_tamper",
    )
    writer = TransactionFileWriter(manager.transaction_boundary(record.transaction_id))
    writer.write("tracked.txt", "patched\n")
    patch = await manager.record_patch_set(
        record.transaction_id,
        patch_id="patch_tampered",
    )
    await manager.begin_verification(record.transaction_id)
    manager.patch_path(record.transaction_id, patch.patch_id).write_bytes(b"tampered")
    service = VerificationService(manager, scope=scope)

    with pytest.raises(TransactionError) as captured:
        await service.verify(record.transaction_id)

    assert captured.value.code == "transaction.patch_hash_mismatch"
    assert not manager.evidence_root(record.transaction_id).exists()
    await manager.abort(record.transaction_id)
    scope.assert_empty()
    await scope.close()
