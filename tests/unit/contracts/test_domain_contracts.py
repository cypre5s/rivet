"""验证最小模块、事务和 Evidence 契约。"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from rivet.contracts import CONTRACT_MODELS
from rivet.contracts.modules import ModuleManifest
from rivet.contracts.transactions import AcceptanceSpec
from rivet.contracts.verification import (
    Verdict,
    VerificationKind,
    VerificationResult,
    VerificationStatus,
    VerificationStep,
)


def test_module_manifest_surface_is_minimal_and_rejects_duplicate_capabilities() -> (
    None
):
    assert set(ModuleManifest.model_fields) == {
        "module_id",
        "factory",
        "provides",
        "requires",
    }
    with pytest.raises(ValidationError):
        ModuleManifest(
            module_id="context.lexical",
            factory="rivet.modules.factories:create_lexical_module",
            provides=("context.search.lexical", "context.search.lexical"),
        )


def test_removed_reader_and_progressive_context_contracts_are_not_exported() -> None:
    names = {model.__name__ for model in CONTRACT_MODELS}

    assert names.isdisjoint(
        {
            "ContextBudget",
            "ContextItem",
            "ContextSelection",
            "ReaderRequest",
            "ReaderResult",
        }
    )


def test_acceptance_spec_rejects_allowed_and_forbidden_overlap() -> None:
    with pytest.raises(ValidationError):
        AcceptanceSpec(
            acceptance_id="acceptance_example",
            user_goal="修复超时",
            baseline_reproduction=(("uv", "run", "pytest"),),
            allowed_paths=("src/rivet/cli.py",),
            write_scope=("src/rivet/cli.py",),
            forbidden_paths=("src/rivet/cli.py",),
            expected_behaviors=("超时时返回分类错误",),
            preserved_behaviors=("普通请求仍成功",),
            verification_commands=(("uv", "run", "pytest"),),
            behavior_verification_commands=(("uv", "run", "pytest", "acceptance"),),
            max_wall_seconds=120,
            max_tokens=1_000,
            max_tool_calls=10,
        )


def test_verdict_rejects_forged_passed_flag() -> None:
    step = VerificationStep(
        step_id="verification_contract",
        kind=VerificationKind.BEHAVIOR,
        name="契约失败步骤",
        required=True,
        command=("pytest",),
        timeout_seconds=30,
    )
    result = VerificationResult(
        step=step,
        status=VerificationStatus.FAILED,
        exit_code=1,
        duration_ms=1,
    )

    with pytest.raises(ValidationError):
        Verdict(
            transaction_id="tx_example",
            base_commit="a" * 40,
            acceptance_sha256="sha256:" + ("c" * 64),
            patch_sha256="sha256:" + ("e" * 64),
            evidence_id="evidence_example",
            evidence_manifest_path="tx_example/attempt_0001/manifest.json",
            status=VerificationStatus.FAILED,
            passed=True,
            results=(result,),
            decided_at=datetime(2026, 8, 28, tzinfo=UTC),
        )


def test_verdict_preserves_blocked_status() -> None:
    step = VerificationStep(
        step_id="verification_blocked",
        kind=VerificationKind.RESOURCE,
        name="环境阻塞步骤",
        required=True,
        command=("missing-tool",),
        timeout_seconds=30,
    )
    result = VerificationResult(
        step=step,
        status=VerificationStatus.BLOCKED,
        duration_ms=1,
    )

    verdict = Verdict(
        transaction_id="tx_blocked",
        base_commit="b" * 40,
        acceptance_sha256="sha256:" + ("d" * 64),
        patch_sha256="sha256:" + ("f" * 64),
        evidence_id="evidence_blocked",
        evidence_manifest_path="tx_blocked/attempt_0001/manifest.json",
        status=VerificationStatus.BLOCKED,
        passed=False,
        results=(result,),
        decided_at=datetime(2026, 8, 28, tzinfo=UTC),
    )

    assert verdict.status is VerificationStatus.BLOCKED
