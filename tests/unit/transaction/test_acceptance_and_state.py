"""验证 AcceptanceSpec 规范哈希和事务状态迁移门禁。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rivet.contracts.transactions import TransactionState
from rivet.kernel.resources import ResourceScope
from rivet.transaction.errors import TransactionError
from rivet.transaction.hashing import acceptance_sha256, canonical_json_bytes
from rivet.transaction.manager import TransactionManager
from rivet.transaction.state_machine import validate_transition
from tests.transaction_helpers import acceptance_spec


def test_acceptance_hash_is_stable_across_json_whitespace() -> None:
    specification = acceptance_spec()
    canonical = canonical_json_bytes(specification.model_dump(mode="json"))
    reparsed = json.loads(json.dumps(specification.model_dump(mode="json"), indent=4))

    assert acceptance_sha256(specification) == acceptance_sha256(
        specification.model_validate_json(json.dumps(reparsed))
    )
    assert b"\n" not in canonical


def test_state_machine_accepts_only_adjacent_verified_path() -> None:
    assert (
        validate_transition(TransactionState.BASELINED, TransactionState.PLANNED)
        is TransactionState.PLANNED
    )
    assert (
        validate_transition(TransactionState.VERIFYING, TransactionState.VERIFIED)
        is TransactionState.VERIFIED
    )

    with pytest.raises(TransactionError, match="状态迁移"):
        validate_transition(TransactionState.BASELINED, TransactionState.VERIFIED)


def test_terminal_state_is_idempotent_but_cannot_reopen() -> None:
    assert (
        validate_transition(TransactionState.APPLIED, TransactionState.APPLIED)
        is TransactionState.APPLIED
    )

    with pytest.raises(TransactionError, match="状态迁移"):
        validate_transition(TransactionState.APPLIED, TransactionState.PATCHING)


def test_acceptance_draft_is_pure_and_does_not_create_repository(
    tmp_path: Path,
) -> None:
    missing_repository = tmp_path / "missing"
    scope = ResourceScope("transaction.draft")
    manager = TransactionManager(missing_repository, scope=scope)

    draft = manager.draft_acceptance(
        acceptance_id="acceptance_draft",
        user_goal="修复 fixture",
        baseline_reproduction=(("pytest",),),
        allowed_paths=("src",),
        expected_behaviors=("缺陷被修复",),
        preserved_behaviors=("旧测试通过",),
        verification_commands=(("pytest",),),
        max_wall_seconds=60,
        max_tokens=1_000,
        max_tool_calls=10,
    )

    assert draft.acceptance_id == "acceptance_draft"
    assert not missing_repository.exists()
    scope.assert_empty()
