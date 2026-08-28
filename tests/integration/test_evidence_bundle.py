"""验证 EvidenceBundle 完整、不可覆盖且可独立复核。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from rivet.verify import evidence as evidence_module
from rivet.verify.errors import VerificationError
from rivet.verify.evidence import MANDATORY_EVIDENCE_FILES, EvidenceBundleWriter
from tests.fixtures.verification.cases import VERIFICATION_CASES
from tests.verification_helpers import run_verification_case


def _payloads() -> dict[str, bytes]:
    """构造不含 manifest 的最小完整证据载荷。"""
    return {name: f"fixture:{name}\n".encode() for name in MANDATORY_EVIDENCE_FILES}


def test_attempt_directories_never_overwrite_previous_evidence(tmp_path: Path) -> None:
    writer = EvidenceBundleWriter(
        tmp_path / "evidence",
        clock=lambda: datetime(2026, 8, 28, tzinfo=UTC),
    )

    first = writer.write(
        transaction_id="tx_evidence",
        acceptance_sha256="sha256:" + ("a" * 64),
        files=_payloads(),
    )
    second = writer.write(
        transaction_id="tx_evidence",
        acceptance_sha256="sha256:" + ("a" * 64),
        files=_payloads(),
    )

    assert first.directory.name == "attempt_0001"
    assert second.directory.name == "attempt_0002"
    assert first.directory.is_dir()
    assert writer.verify(first.directory) == first.manifest
    assert writer.verify(second.directory) == second.manifest


def test_manifest_detects_modified_or_extra_file(tmp_path: Path) -> None:
    writer = EvidenceBundleWriter(tmp_path / "evidence")
    bundle = writer.write(
        transaction_id="tx_tamper",
        acceptance_sha256="sha256:" + ("b" * 64),
        files=_payloads(),
    )
    (bundle.directory / "patch.diff").write_bytes(b"tampered")

    with pytest.raises(VerificationError, match="哈希"):
        writer.verify(bundle.directory)

    (bundle.directory / "patch.diff").write_bytes(_payloads()["patch.diff"])
    (bundle.directory / "unexpected.log").write_text("extra", encoding="utf-8")
    with pytest.raises(VerificationError, match="文件清单"):
        writer.verify(bundle.directory)


def test_atomic_publish_failure_leaves_no_partial_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = EvidenceBundleWriter(tmp_path / "evidence")

    def fail_publish(_source: Path, _target: Path) -> None:
        """模拟最终目录 rename 失败。"""
        raise OSError("injected")

    monkeypatch.setattr(evidence_module.os, "rename", fail_publish)

    with pytest.raises(VerificationError) as captured:
        writer.write(
            transaction_id="tx_atomic_failure",
            acceptance_sha256="sha256:" + ("c" * 64),
            files=_payloads(),
        )

    assert captured.value.code == "verification.evidence_publish_failed"
    assert tuple((tmp_path / "evidence").iterdir()) == ()


@pytest.mark.asyncio
async def test_service_writes_required_auditable_files(tmp_path: Path) -> None:
    prepared = await run_verification_case(tmp_path, VERIFICATION_CASES[0])
    directory = prepared.outcome.evidence_directory
    names = {path.name for path in directory.iterdir()}

    assert names >= MANDATORY_EVIDENCE_FILES
    assert {"manifest.json", "verdict.json", "summary.md", "matrix.json"} <= names
    assert prepared.outcome.manifest == EvidenceBundleWriter(directory.parent).verify(
        directory
    )

    await prepared.manager.abort(prepared.outcome.transaction.transaction_id)
    prepared.scope.assert_empty()
    await prepared.scope.close()
