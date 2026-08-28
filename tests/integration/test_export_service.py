"""验证 Evidence、Trace、Session 导出真实落盘且受路径与脱敏约束。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rivet.export.service import ExportError, ExportService
from rivet.storage.sessions import SessionCheckpoint, SessionStatus, SessionStore
from rivet.verify.evidence import MANDATORY_EVIDENCE_FILES, EvidenceBundleWriter


def _session(repository: Path, secret: str) -> None:
    SessionStore(repository).save(
        SessionCheckpoint(
            session_id="session_export_fixture",
            run_id="run_export_fixture",
            command="ask",
            query=f"不得泄露 {secret}",
            status=SessionStatus.ANSWERED,
        )
    )


def _evidence(repository: Path, secret: str) -> None:
    root = repository / ".rivet" / "transactions" / "tx_export" / "evidence"
    payloads = {
        name: (f"fixture {name} {secret}\n").encode()
        for name in MANDATORY_EVIDENCE_FILES
    }
    EvidenceBundleWriter(root).write(
        transaction_id="tx_export",
        acceptance_sha256="sha256:" + ("a" * 64),
        files=payloads,
        evidence_id="evidence_export_fixture",
    )


@pytest.mark.parametrize("kind", ("session", "evidence"))
def test_export_writes_real_redacted_file_with_hash(
    tmp_path: Path,
    kind: str,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    secret = "sk-" + ("x" * 32)
    _session(repository, secret)
    _evidence(repository, secret)
    service = ExportService(repository, environment={"DEEPSEEK_API_KEY": secret})

    result = service.export(kind, Path(f".rivet/exports/{kind}.json"))

    assert result.path.is_file()
    assert result.sha256.startswith("sha256:")
    content = result.path.read_text(encoding="utf-8")
    assert secret not in content
    assert json.loads(content)["kind"] == kind

    with pytest.raises(ExportError) as captured:
        service.export(kind, Path(f".rivet/exports/{kind}.json"))
    assert captured.value.code == "export.destination_exists"


@pytest.mark.parametrize(
    "destination",
    (Path("../outside.json"), Path(".git/export.json"), Path("/tmp/export.json")),
)
def test_export_rejects_destination_escape(
    tmp_path: Path,
    destination: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _session(repository, "safe")

    with pytest.raises(ExportError) as captured:
        ExportService(repository, environment={}).export("session", destination)

    assert captured.value.code == "export.destination_invalid"
