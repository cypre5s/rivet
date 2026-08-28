"""定义冻结验收条件、事务状态和补丁集契约。"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from rivet.contracts.common import (
    AcceptanceId,
    ContractModel,
    EvidenceId,
    GitCommit,
    NonEmptyText,
    PatchId,
    RepositoryPath,
    Sha256Digest,
    Timestamp,
    TransactionId,
)

CommandArgument = Annotated[str, StringConstraints(min_length=1, max_length=16_384)]
Command = tuple[CommandArgument, ...]


class TransactionState(StrEnum):
    """列出从创建到应用或终止的完整事务状态。"""

    CREATED = "CREATED"
    SNAPSHOTTED = "SNAPSHOTTED"
    BASELINED = "BASELINED"
    PLANNED = "PLANNED"
    PATCHING = "PATCHING"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"
    ABORTED = "ABORTED"


class AcceptanceSpec(ContractModel):
    """在修改前冻结用户目标、范围、行为与确定性验证预算。"""

    acceptance_id: AcceptanceId
    user_goal: NonEmptyText
    baseline_reproduction: tuple[Command, ...] = Field(min_length=1)
    allowed_paths: tuple[RepositoryPath, ...] = Field(min_length=1)
    forbidden_paths: tuple[RepositoryPath, ...] = ()
    expected_behaviors: tuple[NonEmptyText, ...] = Field(min_length=1)
    preserved_behaviors: tuple[NonEmptyText, ...] = Field(min_length=1)
    verification_commands: tuple[Command, ...] = Field(min_length=1)
    behavior_verification_commands: tuple[Command, ...] = ()
    max_wall_seconds: int = Field(gt=0)
    max_tokens: int = Field(gt=0)
    max_tool_calls: int = Field(gt=0)
    max_cost_usd: Decimal | None = Field(default=None, ge=0)
    acceptable_risks: tuple[NonEmptyText, ...] = ()
    non_goals: tuple[NonEmptyText, ...] = ()

    @model_validator(mode="after")
    def _validate_scope_and_commands(self) -> Self:
        """拒绝范围冲突、重复范围或空命令。"""
        if len(set(self.allowed_paths)) != len(self.allowed_paths):
            raise ValueError("允许路径不得重复")
        if len(set(self.forbidden_paths)) != len(self.forbidden_paths):
            raise ValueError("禁止路径不得重复")
        overlap = set(self.allowed_paths) & set(self.forbidden_paths)
        if overlap:
            raise ValueError("允许路径与禁止路径不得重叠")
        all_commands = (
            *self.baseline_reproduction,
            *self.verification_commands,
            *self.behavior_verification_commands,
        )
        if any(not command for command in all_commands):
            raise ValueError("验收命令不得为空")
        return self


class PatchSet(ContractModel):
    """记录基线、补丁哈希、变更范围和二进制语义。"""

    patch_id: PatchId
    transaction_id: TransactionId
    base_commit: GitCommit
    acceptance_sha256: Sha256Digest
    patch_sha256: Sha256Digest
    changed_files: tuple[RepositoryPath, ...]
    changed_symbols: tuple[str, ...] = ()
    contains_binary_diff: bool = False
    created_at: Timestamp


class TransactionRecord(ContractModel):
    """持久化事务基线、验收哈希和当前状态。"""

    transaction_id: TransactionId
    state: TransactionState
    repository_identity: Sha256Digest
    repository_fingerprint: Sha256Digest
    head_commit: GitCommit
    base_commit: GitCommit
    branch: str | None = Field(default=None, min_length=1, max_length=255)
    detached_head: bool
    dirty: bool
    dirty_snapshot_hash: GitCommit | None = None
    has_submodules: bool
    submodule_status_sha256: Sha256Digest
    git_config_summary: tuple[str, ...] = ()
    acceptance_sha256: Sha256Digest | None = None
    current_patch_id: PatchId | None = None
    evidence_id: EvidenceId | None = None
    evidence_manifest_path: RepositoryPath | None = None
    evidence_manifest_sha256: Sha256Digest | None = None
    created_at: Timestamp
    updated_at: Timestamp

    @model_validator(mode="after")
    def _validate_evidence_attestation(self) -> Self:
        """要求三个证据绑定字段同时存在，并覆盖可交付状态。"""
        evidence_fields = (
            self.evidence_id,
            self.evidence_manifest_path,
            self.evidence_manifest_sha256,
        )
        if any(value is not None for value in evidence_fields) and not all(
            value is not None for value in evidence_fields
        ):
            raise ValueError("事务证据绑定字段必须同时存在")
        if self.state in {
            TransactionState.VERIFIED,
            TransactionState.REJECTED,
            TransactionState.APPLIED,
        } and not all(value is not None for value in evidence_fields):
            raise ValueError("已判定事务必须绑定 Evidence manifest")
        return self
