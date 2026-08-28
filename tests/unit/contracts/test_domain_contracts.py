"""验证模块、上下文、Reader、事务和验证契约。"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from rivet.contracts.context import ContextBudget, ContextItem, ContextLevel
from rivet.contracts.modules import ActivationPolicy, ModuleManifest
from rivet.contracts.readers import (
    ReaderResult,
    ReaderStatus,
    SupportLevel,
)
from rivet.contracts.transactions import AcceptanceSpec
from rivet.contracts.verification import (
    Verdict,
    VerificationKind,
    VerificationResult,
    VerificationStatus,
    VerificationStep,
)


def test_module_manifest_rejects_duplicate_capabilities() -> None:
    with pytest.raises(ValidationError):
        ModuleManifest(
            module_id="context.lexical",
            module_version="1.0.0",
            activation=ActivationPolicy.ON_DEMAND,
            factory="rivet.context.lexical:create_module",
            provides=("context.search.lexical", "context.search.lexical"),
        )


def test_module_manifest_rejects_unknown_activation_policy() -> None:
    with pytest.raises(ValidationError):
        ModuleManifest.model_validate(
            {
                "schema_version": 1,
                "module_id": "context.lexical",
                "module_version": "1.0.0",
                "activation": "automatic",
                "factory": "rivet.context.lexical:create_module",
                "provides": ("context.search.lexical",),
            }
        )


def test_context_budget_rejects_overcommit() -> None:
    with pytest.raises(ValidationError):
        ContextBudget(
            total_tokens=100,
            required_tokens=50,
            working_tokens=40,
            history_tokens=20,
        )


def test_context_item_records_source_reason_and_cost() -> None:
    item = ContextItem(
        context_item_id="context_example",
        repository_path="src/rivet/cli.py",
        content="def main(): ...",
        reason="命中用户指定的 main 符号",
        retrieval_level=ContextLevel.LEXICAL,
        content_sha256="sha256:" + ("a" * 64),
        token_estimate=8,
        selected_at=datetime(2026, 8, 28, tzinfo=UTC),
    )

    assert item.reason.startswith("命中")
    assert item.token_estimate == 8


def test_reader_result_is_explicitly_untrusted_and_structured() -> None:
    result = ReaderResult(
        status=ReaderStatus.SUCCESS,
        source_path="docs/input.txt",
        media_type="text/plain",
        detected_format="text",
        reader_id="reader.text",
        reader_version="1.0.0",
        support_level=SupportLevel.NATIVE,
        content="文档内容",
        source_sha256="sha256:" + ("b" * 64),
    )

    assert result.untrusted is True
    assert result.metadata == {}
    assert result.warnings == ()


def test_acceptance_spec_rejects_allowed_and_forbidden_overlap() -> None:
    with pytest.raises(ValidationError):
        AcceptanceSpec(
            acceptance_id="acceptance_example",
            user_goal="修复超时",
            baseline_reproduction=(("uv", "run", "pytest"),),
            allowed_paths=("src/rivet/cli.py",),
            forbidden_paths=("src/rivet/cli.py",),
            expected_behaviors=("超时时返回分类错误",),
            preserved_behaviors=("普通请求仍成功",),
            verification_commands=(("uv", "run", "pytest"),),
            max_wall_seconds=120,
            max_tokens=1000,
            max_tool_calls=10,
        )


def test_verdict_rejects_forged_passed_flag() -> None:
    step = VerificationStep(
        step_id="verification_contract",
        kind=VerificationKind.TARGETED,
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
            acceptance_sha256="sha256:" + ("c" * 64),
            evidence_id="evidence_example",
            evidence_manifest_path="evidence/attempt_0001/manifest.json",
            status=VerificationStatus.FAILED,
            passed=True,
            results=(result,),
            decided_at=datetime(2026, 8, 28, tzinfo=UTC),
        )


def test_verdict_preserves_blocked_status() -> None:
    step = VerificationStep(
        step_id="verification_blocked",
        kind=VerificationKind.ENVIRONMENT,
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
        acceptance_sha256="sha256:" + ("d" * 64),
        evidence_id="evidence_blocked",
        evidence_manifest_path="evidence/attempt_0001/manifest.json",
        status=VerificationStatus.BLOCKED,
        passed=False,
        results=(result,),
        decided_at=datetime(2026, 8, 28, tzinfo=UTC),
    )

    assert verdict.status is VerificationStatus.BLOCKED
