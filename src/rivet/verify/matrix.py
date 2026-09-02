"""从冻结验收条件生成七类验证事实，并纯函数计算 Verdict。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rivet.contracts.transactions import AcceptanceSpec, Command
from rivet.contracts.verification import (
    Verdict,
    VerificationKind,
    VerificationResult,
    VerificationStatus,
    VerificationStep,
)

from .errors import VerificationError


@dataclass(frozen=True, slots=True)
class VerificationMatrix:
    """保存按七类事实和原命令顺序冻结的步骤。"""

    steps: tuple[VerificationStep, ...]


def _unique_commands(commands: tuple[Command, ...]) -> tuple[Command, ...]:
    """保留首次出现顺序去重同组 argv。"""
    return tuple(dict.fromkeys(commands))


def build_verification_matrix(
    specification: AcceptanceSpec,
) -> VerificationMatrix:
    """只把 AcceptanceSpec 中已经冻结的命令编入矩阵。"""
    timeout_seconds = min(specification.max_wall_seconds, 3_600)
    steps: list[VerificationStep] = []

    external_commands = (
        *specification.baseline_reproduction,
        *specification.verification_commands,
        *specification.behavior_verification_commands,
    )
    if any(command[0] == "rivet-internal" for command in external_commands):
        raise VerificationError(
            "verification.internal_command_reserved",
            "外部验证命令不得使用内部保留程序名",
        )

    def add(
        kind: VerificationKind,
        name: str,
        commands: tuple[Command, ...],
        *,
        required: bool,
        namespace: str = "",
    ) -> None:
        """为同类命令生成稳定且全局唯一的步骤 ID。"""
        namespace_part = f"_{namespace}" if namespace else ""
        for index, command in enumerate(_unique_commands(commands), start=1):
            steps.append(
                VerificationStep(
                    step_id=(
                        f"verification_{kind.value.lower()}{namespace_part}_{index:03d}"
                    ),
                    kind=kind,
                    name=name,
                    required=required,
                    command=command,
                    timeout_seconds=timeout_seconds,
                )
            )

    add(
        VerificationKind.BASELINE,
        "记录修改前基线复现",
        specification.baseline_reproduction,
        required=True,
    )
    add(
        VerificationKind.BEHAVIOR,
        "运行独立行为验收",
        specification.behavior_verification_commands,
        required=True,
    )
    add(
        VerificationKind.REGRESSION,
        "运行已冻结回归与质量检查",
        specification.verification_commands,
        required=bool(specification.verification_commands),
    )
    add(
        VerificationKind.SCOPE,
        "检查补丁范围",
        (("rivet-internal", "scope"),),
        required=True,
    )
    add(
        VerificationKind.SECRET,
        "扫描秘密与危险新增内容",
        (("rivet-internal", "secret"),),
        required=True,
    )
    add(
        VerificationKind.BINDING,
        "绑定验收条目与执行证据",
        (("rivet-internal", "binding"),),
        required=True,
    )
    add(
        VerificationKind.RESOURCE,
        "检查资源与 Worktree 完整性",
        (("rivet-internal", "resource"),),
        required=True,
    )
    return VerificationMatrix(steps=tuple(steps))


def compute_verdict(
    *,
    transaction_id: str,
    base_commit: str,
    acceptance_sha256: str,
    patch_sha256: str,
    evidence_id: str,
    evidence_manifest_path: str,
    results: tuple[VerificationResult, ...],
    decided_at: datetime,
) -> Verdict:
    """按取消、失败、不确定、通过的固定优先级计算结论。"""
    if not results:
        raise VerificationError(
            "verification.results_empty",
            "验证结果不能为空",
        )
    step_ids = [result.step.step_id for result in results]
    if len(step_ids) != len(set(step_ids)):
        raise VerificationError(
            "verification.result_step_duplicate",
            "验证结果包含重复步骤",
        )
    required_statuses = tuple(
        result.status for result in results if result.step.required
    )
    if any(status is VerificationStatus.CANCELLED for status in required_statuses):
        status = VerificationStatus.CANCELLED
    elif any(status is VerificationStatus.FAILED for status in required_statuses):
        status = VerificationStatus.FAILED
    elif any(status is VerificationStatus.BLOCKED for status in required_statuses):
        status = VerificationStatus.BLOCKED
    elif any(status is VerificationStatus.INCONCLUSIVE for status in required_statuses):
        status = VerificationStatus.INCONCLUSIVE
    else:
        status = VerificationStatus.PASSED
    return Verdict(
        transaction_id=transaction_id,
        base_commit=base_commit,
        acceptance_sha256=acceptance_sha256,
        patch_sha256=patch_sha256,
        evidence_id=evidence_id,
        evidence_manifest_path=evidence_manifest_path,
        status=status,
        passed=status is VerificationStatus.PASSED,
        results=results,
        decided_at=decided_at,
    )
