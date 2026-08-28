"""定义验证步骤、结果、程序化 Verdict 和 Evidence Manifest。"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from rivet.contracts.common import (
    ContractModel,
    EvidenceId,
    RepositoryPath,
    Sha256Digest,
    SummaryText,
    Timestamp,
    TransactionId,
    VerificationStepId,
)
from rivet.contracts.transactions import Command


class VerificationKind(StrEnum):
    """标识从环境检查到资源完整性的确定性验证层。"""

    ENVIRONMENT = "V0_ENVIRONMENT"
    BASELINE = "V1_BASELINE"
    REPRODUCTION = "V2_REPRODUCTION"
    TARGETED = "V3_TARGETED"
    RELATED = "V4_RELATED"
    REGRESSION = "V5_REGRESSION"
    STATIC = "V6_STATIC"
    SCOPE = "V7_SCOPE"
    SECRET = "V8_SECRET"
    ACCEPTANCE = "V9_ACCEPTANCE"
    RESOURCE = "V10_RESOURCE"


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
    acceptance_sha256: Sha256Digest
    status: VerificationStatus
    passed: bool
    results: tuple[VerificationResult, ...]
    decided_at: Timestamp

    @model_validator(mode="after")
    def _validate_passed_flag(self) -> Self:
        """保证只有 PASSED 状态可序列化为 passed=true。"""
        if self.passed != (self.status is VerificationStatus.PASSED):
            raise ValueError("Verdict.passed 必须由程序化状态决定")
        return self


class EvidenceFile(ContractModel):
    """记录证据文件的相对路径、大小和内容哈希。"""

    path: RepositoryPath
    sha256: Sha256Digest
    size_bytes: int = Field(ge=0)


class EvidenceManifest(ContractModel):
    """绑定证据包、事务、验收哈希与不可重复的文件清单。"""

    evidence_id: EvidenceId
    transaction_id: TransactionId
    acceptance_sha256: Sha256Digest
    files: tuple[EvidenceFile, ...]
    created_at: Timestamp

    @model_validator(mode="after")
    def _validate_unique_files(self) -> Self:
        """拒绝证据清单中的重复路径。"""
        paths = [evidence_file.path for evidence_file in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("证据清单不得包含重复路径")
        return self
