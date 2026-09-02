"""定义验证步骤、结果、程序化 Verdict 和 Evidence Manifest。"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from rivet.contracts.common import (
    ContractModel,
    EvidenceId,
    GitCommit,
    RepositoryPath,
    Sha256Digest,
    SummaryText,
    Timestamp,
    TransactionId,
    VerificationStepId,
)
from rivet.contracts.transactions import Command


class VerificationKind(StrEnum):
    """标识独立 Evidence 所需的七类真实验证事实。"""

    BASELINE = "BASELINE"
    BEHAVIOR = "BEHAVIOR"
    REGRESSION = "REGRESSION"
    SCOPE = "SCOPE"
    SECRET = "SECRET"
    BINDING = "BINDING"
    RESOURCE = "RESOURCE"


class VerificationStatus(StrEnum):
    """区分通过、失败、不确定、外部阻塞与取消状态。"""

    PASSED = "PASSED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class VerificationStep(ContractModel):
    """定义固定 argv、时间预算和必需性的验证步骤。"""

    step_id: VerificationStepId
    kind: VerificationKind
    name: SummaryText
    required: bool = True
    command: Command
    timeout_seconds: int = Field(gt=0)


class VerificationResult(ContractModel):
    """保存单步退出码、耗时、截断摘要与原始日志引用。"""

    step: VerificationStep
    status: VerificationStatus
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)
    stdout_summary: str = Field(default="", max_length=65_536)
    stderr_summary: str = Field(default="", max_length=65_536)
    output_truncated: bool = False
    log_path: RepositoryPath | None = None
    log_sha256: Sha256Digest | None = None


class Verdict(ContractModel):
    """使 passed 只能与程序化 PASSED 状态一致，拒绝自由伪造。"""

    transaction_id: TransactionId
    base_commit: GitCommit
    acceptance_sha256: Sha256Digest
    patch_sha256: Sha256Digest
    evidence_id: EvidenceId
    evidence_manifest_path: RepositoryPath
    status: VerificationStatus
    passed: bool
    results: tuple[VerificationResult, ...] = Field(min_length=1)
    decided_at: Timestamp

    @model_validator(mode="after")
    def _validate_passed_flag(self) -> Self:
        """按 required 结果重算状态，并拒绝重复步骤或自由结论。"""
        step_ids = [result.step.step_id for result in self.results]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Verdict.results 不得包含重复步骤")
        required_statuses = tuple(
            result.status for result in self.results if result.step.required
        )
        if not required_statuses:
            raise ValueError("Verdict 必须包含 required 验证结果")
        if any(status is VerificationStatus.CANCELLED for status in required_statuses):
            computed = VerificationStatus.CANCELLED
        elif any(status is VerificationStatus.FAILED for status in required_statuses):
            computed = VerificationStatus.FAILED
        elif any(status is VerificationStatus.BLOCKED for status in required_statuses):
            computed = VerificationStatus.BLOCKED
        elif any(
            status is VerificationStatus.INCONCLUSIVE for status in required_statuses
        ):
            computed = VerificationStatus.INCONCLUSIVE
        else:
            computed = VerificationStatus.PASSED
        if self.status is not computed:
            raise ValueError("Verdict.status 必须由 required 结果程序化计算")
        if self.passed != (computed is VerificationStatus.PASSED):
            raise ValueError("Verdict.passed 必须由程序化状态决定")
        return self


class EvidenceFile(ContractModel):
    """记录证据文件的相对路径、大小和内容哈希。"""

    path: RepositoryPath
    sha256: Sha256Digest
    size_bytes: int = Field(ge=0)


class EvidenceManifest(ContractModel):
    """把证据包绑定到同一基线、验收条件和候选补丁。"""

    evidence_id: EvidenceId
    transaction_id: TransactionId
    base_commit: GitCommit
    acceptance_sha256: Sha256Digest
    patch_sha256: Sha256Digest
    patch_redacted: bool = False
    files: tuple[EvidenceFile, ...]
    created_at: Timestamp

    @model_validator(mode="after")
    def _validate_unique_files(self) -> Self:
        """拒绝证据清单中的重复路径。"""
        paths = [evidence_file.path for evidence_file in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("证据清单不得包含重复路径")
        return self
