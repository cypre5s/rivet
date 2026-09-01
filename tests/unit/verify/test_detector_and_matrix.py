"""验证项目检测只给候选，确认后才进入 V0-V10 矩阵。"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rivet.contracts.verification import (
    VerificationKind,
    VerificationResult,
    VerificationStatus,
)
from rivet.verify.detector import ProjectDetector, ProjectKind, evidence_readiness
from rivet.verify.errors import VerificationError
from rivet.verify.matrix import build_verification_matrix, compute_verdict
from tests.transaction_helpers import acceptance_spec


def test_detector_reports_all_known_project_kinds_without_running_commands(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='demo'\n", encoding="utf-8"
    )
    (tmp_path / "package.json").write_text('{"scripts":{"test":"x"}}', encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module example.invalid/demo\n", encoding="utf-8")

    detection = ProjectDetector().detect(tmp_path)

    assert detection.kinds == (
        ProjectKind.PYTHON,
        ProjectKind.NODE_BUN,
        ProjectKind.RUST,
        ProjectKind.GO,
    )
    assert {candidate.argv[0] for candidate in detection.candidates} == {
        "bun",
        "bunx",
        "cargo",
        "go",
        "uv",
    }
    assert detection.configuration is None


def test_detector_uses_generic_kind_when_no_marker_exists(tmp_path: Path) -> None:
    detection = ProjectDetector().detect(tmp_path)

    assert detection.kinds == (ProjectKind.GENERIC,)
    assert detection.candidates == ()


def test_project_configuration_is_parsed_but_requires_explicit_confirmation(
    tmp_path: Path,
) -> None:
    configuration_directory = tmp_path / ".rivet"
    configuration_directory.mkdir()
    (configuration_directory / "project.toml").write_text(
        """
schema_version = 1
[verification]
acceptance = [["python", "acceptance.py"]]
targeted = [["python", "target.py"]]
related = [["python", "related.py"]]
regression = [["python", "regression.py"]]
static = [["python", "-m", "compileall", "src"]]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    detection = ProjectDetector().detect(tmp_path)
    specification = acceptance_spec()

    unconfirmed = build_verification_matrix(
        specification,
        project_configuration=detection.configuration,
        configuration_confirmed=False,
    )
    confirmed = build_verification_matrix(
        specification,
        project_configuration=detection.configuration,
        configuration_confirmed=True,
    )

    assert not any(step.command[-1] == "regression.py" for step in unconfirmed.steps)
    assert any(step.command[-1] == "regression.py" for step in confirmed.steps)
    assert any(step.command[-1] == "acceptance.py" for step in confirmed.steps)
    assert {step.kind for step in confirmed.steps} == set(VerificationKind)
    readiness = evidence_readiness(detection)
    assert readiness.ready is True
    assert readiness.acceptance_commands == (("python", "acceptance.py"),)


def test_empty_acceptance_is_not_evidence_ready(tmp_path: Path) -> None:
    configuration_directory = tmp_path / ".rivet"
    configuration_directory.mkdir()
    (configuration_directory / "project.toml").write_text(
        "schema_version = 1\n[verification]\nacceptance = []\n",
        encoding="utf-8",
    )

    readiness = evidence_readiness(ProjectDetector().detect(tmp_path))

    assert readiness.ready is False
    assert readiness.reason == "verification.acceptance 为空"
    assert "candidate-only" not in readiness.next_action


def test_broken_project_configuration_symlink_is_rejected(tmp_path: Path) -> None:
    configuration_directory = tmp_path / ".rivet"
    configuration_directory.mkdir()
    (configuration_directory / "project.toml").symlink_to(tmp_path / "missing.toml")

    with pytest.raises(VerificationError, match="符号链接"):
        ProjectDetector().detect(tmp_path)


def test_verdict_fails_closed_for_required_failure_or_inconclusive() -> None:
    matrix = build_verification_matrix(
        acceptance_spec(
            expected_behavior="修复后命令成功",
        ),
    )
    passed_results = tuple(
        VerificationResult(
            step=step,
            status=VerificationStatus.PASSED,
            exit_code=0,
            duration_ms=0,
        )
        for step in matrix.steps
    )
    decided_at = datetime(2026, 8, 28, tzinfo=UTC)

    passed = compute_verdict(
        transaction_id="tx_matrix",
        acceptance_sha256="sha256:" + ("a" * 64),
        evidence_id="evidence_matrix",
        evidence_manifest_path="evidence/attempt_0001/manifest.json",
        results=passed_results,
        decided_at=decided_at,
    )
    failed_results = list(passed_results)
    failed_index = next(
        index for index, result in enumerate(failed_results) if result.step.required
    )
    failed_results[failed_index] = failed_results[failed_index].model_copy(
        update={"status": VerificationStatus.FAILED, "exit_code": 1}
    )
    failed = compute_verdict(
        transaction_id="tx_matrix",
        acceptance_sha256="sha256:" + ("a" * 64),
        evidence_id="evidence_matrix",
        evidence_manifest_path="evidence/attempt_0001/manifest.json",
        results=tuple(failed_results),
        decided_at=decided_at,
    )
    inconclusive_results = list(passed_results)
    inconclusive_results[failed_index] = inconclusive_results[failed_index].model_copy(
        update={"status": VerificationStatus.INCONCLUSIVE, "exit_code": None}
    )
    inconclusive = compute_verdict(
        transaction_id="tx_matrix",
        acceptance_sha256="sha256:" + ("a" * 64),
        evidence_id="evidence_matrix",
        evidence_manifest_path="evidence/attempt_0001/manifest.json",
        results=tuple(inconclusive_results),
        decided_at=decided_at,
    )

    assert passed.status is VerificationStatus.PASSED
    assert failed.status is VerificationStatus.FAILED
    assert inconclusive.status is VerificationStatus.INCONCLUSIVE
    assert passed.passed is True
    assert failed.passed is False
    assert inconclusive.passed is False


def test_matrix_uses_frozen_commands_without_shell_strings() -> None:
    specification = acceptance_spec()
    matrix = build_verification_matrix(specification)

    assert any(
        step.command == specification.verification_commands[0] for step in matrix.steps
    )
    assert all(isinstance(step.command, tuple) for step in matrix.steps)
    assert not any(
        step.command == (sys.executable + " target.py",) for step in matrix.steps
    )


def test_matrix_rejects_external_use_of_internal_sentinel() -> None:
    specification = acceptance_spec().model_copy(
        update={"verification_commands": (("rivet-internal", "static-unconfigured"),)}
    )

    with pytest.raises(VerificationError, match="保留"):
        build_verification_matrix(specification)
