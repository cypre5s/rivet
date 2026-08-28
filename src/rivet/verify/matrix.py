"""从冻结验收条件生成 V0-V10，并纯函数计算 Verdict。"""

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

from .detector import ProjectConfiguration
from .errors import VerificationError


@dataclass(frozen=True, slots=True)
class VerificationMatrix:
    """保存按 V0-V10 和原命令顺序冻结的步骤。"""

    steps: tuple[VerificationStep, ...]


def _unique_commands(commands: tuple[Command, ...]) -> tuple[Command, ...]:
    """保留首次出现顺序去重同组 argv。"""
    return tuple(dict.fromkeys(commands))


def build_verification_matrix(
    specification: AcceptanceSpec,
    *,
    project_configuration: ProjectConfiguration | None = None,
    configuration_confirmed: bool = False,
) -> VerificationMatrix:
    """只将冻结命令和已确认项目命令编入矩阵。"""
    configuration = (
        project_configuration
        if configuration_confirmed and project_configuration is not None
        else ProjectConfiguration()
    )
    timeout_seconds = min(specification.max_wall_seconds, 3_600)
    steps: list[VerificationStep] = []

    external_commands = (
        *specification.baseline_reproduction,
        *specification.verification_commands,
        *configuration.targeted,
        *configuration.related,
        *configuration.regression,
        *configuration.static,
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
    ) -> None:
        """为同类命令生成稳定且全局唯一的步骤 ID。"""
        for index, command in enumerate(_unique_commands(commands), start=1):
            steps.append(
                VerificationStep(
                    step_id=f"verification_{kind.value.lower()}_{index:03d}",
                    kind=kind,
                    name=name,
                    required=required,
                    command=command,
                    timeout_seconds=timeout_seconds,
                )
            )

    add(
        VerificationKind.ENVIRONMENT,
        "验证命令运行环境",
        (("rivet-internal", "environment"),),
        required=True,
    )
    add(
        VerificationKind.BASELINE,
        "记录修改前基线复现",
        specification.baseline_reproduction,
        required=True,
    )
    add(
        VerificationKind.REPRODUCTION,
        "确认补丁解决基线问题",
        specification.baseline_reproduction,
        required=True,
    )
    add(
        VerificationKind.TARGETED,
        "运行目标测试",
        (*specification.verification_commands, *configuration.targeted),
        required=True,
    )
    related = configuration.related or (("rivet-internal", "related-unconfigured"),)
    add(
        VerificationKind.RELATED,
        "运行相关测试",
        related,
        required=bool(configuration.related),
    )
    regression = configuration.regression or specification.verification_commands
    add(
        VerificationKind.REGRESSION,
        "运行回归测试",
        regression,
        required=True,
    )
    static = configuration.static or (("rivet-internal", "static-unconfigured"),)
    add(
        VerificationKind.STATIC,
        "运行静态检查与构建",
        static,
        required=bool(configuration.static),
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
        VerificationKind.ACCEPTANCE,
        "绑定验收条目与执行证据",
        (("rivet-internal", "acceptance"),),
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
    acceptance_sha256: str,
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
    elif any(
        status in {VerificationStatus.INCONCLUSIVE, VerificationStatus.BLOCKED}
        for status in required_statuses
    ):
        status = VerificationStatus.INCONCLUSIVE
    else:
        status = VerificationStatus.PASSED
    return Verdict(
        transaction_id=transaction_id,
        acceptance_sha256=acceptance_sha256,
        evidence_id=evidence_id,
        evidence_manifest_path=evidence_manifest_path,
        status=status,
        passed=status is VerificationStatus.PASSED,
        results=results,
        decided_at=decided_at,
    )
