"""执行隔离 V0-V10 矩阵并发布程序化 EvidenceBundle。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rivet.contracts.transactions import TransactionRecord
from rivet.contracts.verification import (
    EvidenceManifest,
    Verdict,
    VerificationKind,
    VerificationResult,
    VerificationStatus,
    VerificationStep,
)
from rivet.guard.sandbox import BubblewrapSandbox
from rivet.kernel.errors import ResourceCleanupError
from rivet.kernel.resources import ResourceCounts, ResourceScope
from rivet.tools.errors import ProcessToolError
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.process import ProcessExecutor
from rivet.trace.redaction import SecretRedactor
from rivet.transaction.errors import TransactionError
from rivet.transaction.hashing import canonical_json_bytes, sha256_digest
from rivet.transaction.manager import TransactionManager
from rivet.transaction.models import TransactionVerificationContext

from .detector import ProjectConfiguration, ProjectDetection, ProjectDetector
from .errors import VerificationError
from .evidence import EvidenceBundleWriter
from .matrix import build_verification_matrix, compute_verdict
from .security import SecurityFinding, SecurityScanReport, scan_added_content

Clock = Callable[[], datetime]
CancellationCheck = Callable[[], bool]
VerificationExecutorFactory = Callable[
    [WorkspaceBoundary, ResourceScope, Mapping[str, str], frozenset[str]],
    ProcessExecutor,
]
MAX_COMMAND_CAPTURE_BYTES = 1024 * 1024
MAX_RESULT_SUMMARY_CHARS = 4_096
MAX_SECURITY_SCAN_BYTES = 32 * 1024 * 1024
EXECUTION_KINDS = frozenset(
    {
        VerificationKind.BASELINE,
        VerificationKind.REPRODUCTION,
        VerificationKind.TARGETED,
        VerificationKind.RELATED,
        VerificationKind.REGRESSION,
        VerificationKind.STATIC,
    }
)
LOG_PATHS = {
    VerificationKind.ENVIRONMENT: "static_checks.log",
    VerificationKind.BASELINE: "baseline.log",
    VerificationKind.REPRODUCTION: "focused_tests.log",
    VerificationKind.TARGETED: "focused_tests.log",
    VerificationKind.RELATED: "regression_tests.log",
    VerificationKind.REGRESSION: "regression_tests.log",
    VerificationKind.STATIC: "static_checks.log",
    VerificationKind.SCOPE: "scope_check.json",
    VerificationKind.SECRET: "secret_scan.json",
    VerificationKind.ACCEPTANCE: "acceptance_check.json",
    VerificationKind.RESOURCE: "resource_check.json",
}


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """汇总已落证据、已写回事务状态的最终验证结果。"""

    verdict: Verdict
    transaction: TransactionRecord
    manifest: EvidenceManifest
    evidence_directory: Path
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class _StepDraft:
    """在证据日志哈希确定前暂存单步执行事实。"""

    step: VerificationStep
    status: VerificationStatus
    exit_code: int | None
    duration_ms: int
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False


def _verification_environment() -> tuple[dict[str, str], frozenset[str]]:
    """构造不含 HOME 和凭据的最小确定性命令环境。"""
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
    }
    return environment, frozenset(environment)


def _scope_matches(path: str, scope_path: str) -> bool:
    """把允许和禁止路径解释为文件或目录前缀。"""
    return path == scope_path or path.startswith(f"{scope_path}/")


def _aggregate_status(statuses: tuple[VerificationStatus, ...]) -> VerificationStatus:
    """按最终 Verdict 相同优先级汇总一组依赖结果。"""
    if any(status is VerificationStatus.CANCELLED for status in statuses):
        return VerificationStatus.CANCELLED
    if any(status is VerificationStatus.FAILED for status in statuses):
        return VerificationStatus.FAILED
    if any(
        status in {VerificationStatus.INCONCLUSIVE, VerificationStatus.BLOCKED}
        for status in statuses
    ):
        return VerificationStatus.INCONCLUSIVE
    return VerificationStatus.PASSED


class VerificationService:
    """只依据冻结事实和命令退出状态生成最终 Verdict。"""

    def __init__(
        self,
        manager: TransactionManager,
        *,
        scope: ResourceScope,
        project_configuration: ProjectConfiguration | None = None,
        configuration_confirmed: bool = False,
        clock: Clock | None = None,
        cancelled: CancellationCheck | None = None,
        reverse_patch_description: str | None = None,
        executor_factory: VerificationExecutorFactory | None = None,
        sandbox_executable: Path | None = None,
    ) -> None:
        self._manager = manager
        self._scope = scope
        self._project_configuration = project_configuration
        self._configuration_confirmed = configuration_confirmed
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cancelled = cancelled or (lambda: False)
        self._reverse_patch_description = reverse_patch_description
        self._executor_factory = executor_factory
        self._sandbox_executable = sandbox_executable
        self._redactor = SecretRedactor(environment={})

    async def verify(self, transaction_id: str) -> VerificationOutcome:
        """隔离执行矩阵、原子写证据，再将程序化 Verdict 交给事务。"""
        context = await self._manager.verification_context(transaction_id)
        acceptance_hash = context.record.acceptance_sha256
        if acceptance_hash is None:
            raise VerificationError(
                "verification.acceptance_missing",
                "验证上下文缺少冻结验收哈希",
            )
        detection = ProjectDetector().detect(context.worktree)
        configuration = self._project_configuration or detection.configuration
        matrix = build_verification_matrix(
            context.acceptance,
            project_configuration=configuration,
            configuration_confirmed=self._configuration_confirmed,
        )
        evidence_writer = EvidenceBundleWriter(
            self._manager.evidence_root(transaction_id),
            clock=self._clock,
        )
        attempt_name = evidence_writer.next_attempt_name()
        evidence_id = f"evidence_{uuid.uuid4().hex}"
        evidence_manifest_path = f"evidence/{attempt_name}/manifest.json"
        deadline = time.monotonic() + context.acceptance.max_wall_seconds
        baseline: Path | None = None
        candidate: Path | None = None
        baseline_error: str | None = None
        candidate_error: str | None = None
        cleanup_errors: list[str] = []
        drafts: list[_StepDraft] = []
        scope_report: dict[str, object] = {}
        security_report: SecurityScanReport | None = None
        acceptance_report: dict[str, object] = {}
        resource_report: dict[str, object] = {}
        candidate_signature_before: dict[str, str] | None = None
        candidate_signature_after: dict[str, str] | None = None

        try:
            try:
                baseline = await self._manager.create_verification_baseline(
                    transaction_id,
                    attempt_name,
                )
            except TransactionError as error:
                baseline_error = error.code
            try:
                candidate = await self._manager.create_verification_candidate(
                    transaction_id,
                    attempt_name,
                )
                candidate_signature_before = self._path_signatures(
                    candidate,
                    context.patch.changed_files,
                )
            except TransactionError as error:
                candidate_error = error.code

            for step in matrix.steps:
                if step.kind is VerificationKind.ENVIRONMENT:
                    drafts.append(self._environment_result(step, matrix.steps))
                elif step.kind is VerificationKind.BASELINE:
                    drafts.append(
                        await self._execute_step(
                            step,
                            root=baseline,
                            unavailable_code=baseline_error,
                            deadline=deadline,
                            record_only=True,
                            repository_root=context.repository_root,
                        )
                    )
                elif step.kind in EXECUTION_KINDS:
                    drafts.append(
                        await self._execute_step(
                            step,
                            root=candidate,
                            unavailable_code=candidate_error,
                            deadline=deadline,
                            record_only=False,
                            repository_root=context.repository_root,
                        )
                    )
                elif step.kind is VerificationKind.SCOPE:
                    draft, scope_report = self._scope_result(step, context)
                    drafts.append(draft)
                elif step.kind is VerificationKind.SECRET:
                    draft, security_report = self._security_result(
                        step,
                        context,
                        candidate,
                    )
                    drafts.append(draft)
                elif step.kind is VerificationKind.ACCEPTANCE:
                    draft, acceptance_report = self._acceptance_result(
                        step,
                        context,
                        tuple(drafts),
                    )
                    drafts.append(draft)
                elif step.kind is VerificationKind.RESOURCE:
                    continue
            if candidate is not None:
                candidate_signature_after = self._path_signatures(
                    candidate,
                    context.patch.changed_files,
                )
        finally:
            for worktree in (candidate, baseline):
                if worktree is None:
                    continue
                try:
                    await self._manager.cleanup_verification_worktree(worktree)
                except (ResourceCleanupError, TransactionError) as error:
                    cleanup_errors.append(
                        error.code
                        if isinstance(error, TransactionError)
                        else "resource.worktree_cleanup_failed"
                    )

        resource_step = next(
            step for step in matrix.steps if step.kind is VerificationKind.RESOURCE
        )
        resource_draft, resource_report = await self._resource_result(
            resource_step,
            transaction_id=transaction_id,
            cleanup_errors=tuple(cleanup_errors),
            candidate_signature_before=candidate_signature_before,
            candidate_signature_after=candidate_signature_after,
        )
        drafts.append(resource_draft)
        ordered_drafts = self._order_drafts(matrix.steps, tuple(drafts))
        log_payloads = self._build_log_payloads(
            ordered_drafts,
            scope_report=scope_report,
            security_report=security_report,
            acceptance_report=acceptance_report,
            resource_report=resource_report,
        )
        results = tuple(
            self._finalize_result(draft, log_payloads) for draft in ordered_drafts
        )
        verdict = compute_verdict(
            transaction_id=transaction_id,
            acceptance_sha256=acceptance_hash,
            evidence_id=evidence_id,
            evidence_manifest_path=evidence_manifest_path,
            results=results,
            decided_at=self._now(),
        )
        payloads = self._evidence_payloads(
            context=context,
            detection=detection,
            matrix_steps=matrix.steps,
            verdict=verdict,
            log_payloads=log_payloads,
        )
        bundle = evidence_writer.write(
            transaction_id=transaction_id,
            acceptance_sha256=acceptance_hash,
            files=payloads,
            evidence_id=evidence_id,
            attempt_name=attempt_name,
        )
        verified_manifest = evidence_writer.verify(bundle.directory)
        transaction = await self._manager.record_verdict(verdict)
        return VerificationOutcome(
            verdict=verdict,
            transaction=transaction,
            manifest=verified_manifest,
            evidence_directory=bundle.directory,
            manifest_sha256=bundle.manifest_sha256,
        )

    def _environment_result(
        self,
        step: VerificationStep,
        steps: tuple[VerificationStep, ...],
    ) -> _StepDraft:
        """检查所有实际 argv 的首个程序是否可执行。"""
        environment, _ = _verification_environment()
        executables = sorted(
            {
                candidate.command[0]
                for candidate in steps
                if candidate.command[0] != "rivet-internal"
            }
        )
        missing = tuple(
            executable
            for executable in executables
            if not self._is_executable(executable, environment["PATH"])
        )
        status = (
            VerificationStatus.INCONCLUSIVE if missing else VerificationStatus.PASSED
        )
        return _StepDraft(
            step=step,
            status=status,
            exit_code=None if missing else 0,
            duration_ms=0,
            stdout="验证命令环境可用" if not missing else "",
            stderr=("缺少可执行文件：" + ", ".join(missing) if missing else ""),
        )

    async def _execute_step(
        self,
        step: VerificationStep,
        *,
        root: Path | None,
        unavailable_code: str | None,
        deadline: float,
        record_only: bool,
        repository_root: Path,
    ) -> _StepDraft:
        """在指定验证副本中执行一个有界 argv。"""
        started = time.monotonic()
        if self._cancelled():
            return _StepDraft(
                step=step,
                status=VerificationStatus.CANCELLED,
                exit_code=None,
                duration_ms=0,
                stderr="验证已取消",
            )
        if step.command[0] == "rivet-internal":
            return _StepDraft(
                step=step,
                status=VerificationStatus.PASSED,
                exit_code=0,
                duration_ms=0,
                stdout="该可选验证组未配置，未执行候选命令",
            )
        if root is None:
            return _StepDraft(
                step=step,
                status=VerificationStatus.INCONCLUSIVE,
                exit_code=None,
                duration_ms=0,
                stderr=f"验证 Worktree 不可用：{unavailable_code or 'unknown'}",
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _StepDraft(
                step=step,
                status=VerificationStatus.INCONCLUSIVE,
                exit_code=None,
                duration_ms=self._duration_ms(started),
                stderr="验证总墙钟预算已耗尽",
            )
        environment, allowlist = _verification_environment()
        boundary = WorkspaceBoundary(repository_root, root)
        if self._executor_factory is None:
            runner: ProcessExecutor = BubblewrapSandbox(
                boundary,
                scope=self._scope,
                executable=self._sandbox_executable,
                max_capture_bytes=MAX_COMMAND_CAPTURE_BYTES,
                environment=environment,
            )
        else:
            runner = self._executor_factory(
                boundary,
                self._scope,
                environment,
                allowlist,
            )
        try:
            completed = await runner.run(
                step.command,
                cwd=".",
                timeout_seconds=min(float(step.timeout_seconds), remaining),
            )
        except ProcessToolError as error:
            return _StepDraft(
                step=step,
                status=VerificationStatus.INCONCLUSIVE,
                exit_code=None,
                duration_ms=self._duration_ms(started),
                stderr=f"命令无法启动：{error.code}",
            )
        stdout = self._redact_bytes(completed.stdout)
        stderr = self._redact_bytes(completed.stderr)
        if completed.timed_out:
            status = VerificationStatus.INCONCLUSIVE
        elif record_only:
            status = VerificationStatus.PASSED
        else:
            status = (
                VerificationStatus.PASSED
                if completed.returncode == 0
                else VerificationStatus.FAILED
            )
        return _StepDraft(
            step=step,
            status=status,
            exit_code=completed.returncode,
            duration_ms=self._duration_ms(started),
            stdout=stdout,
            stderr=stderr,
            output_truncated=(completed.stdout_truncated or completed.stderr_truncated),
        )

    def _scope_result(
        self,
        step: VerificationStep,
        context: TransactionVerificationContext,
    ) -> tuple[_StepDraft, dict[str, object]]:
        """检查每个变更路径只落在允许范围且不命中禁止范围。"""
        changed = context.patch.changed_files
        outside = tuple(
            path
            for path in changed
            if not any(
                _scope_matches(path, allowed)
                for allowed in context.acceptance.allowed_paths
            )
        )
        forbidden = tuple(
            path
            for path in changed
            if any(
                _scope_matches(path, denied)
                for denied in context.acceptance.forbidden_paths
            )
        )
        status = (
            VerificationStatus.PASSED
            if changed and not outside and not forbidden
            else VerificationStatus.FAILED
        )
        report: dict[str, object] = {
            "status": status.value,
            "changed_files": list(changed),
            "outside_allowed_paths": list(outside),
            "forbidden_paths_changed": list(forbidden),
        }
        return (
            _StepDraft(
                step=step,
                status=status,
                exit_code=0 if status is VerificationStatus.PASSED else 1,
                duration_ms=0,
                stdout="补丁范围通过" if status is VerificationStatus.PASSED else "",
                stderr="补丁超出冻结范围"
                if status is VerificationStatus.FAILED
                else "",
            ),
            report,
        )

    def _security_result(
        self,
        step: VerificationStep,
        context: TransactionVerificationContext,
        candidate: Path | None,
    ) -> tuple[_StepDraft, SecurityScanReport]:
        """扫描候选补丁当前内容，缺失副本时返回不确定。"""
        if candidate is None:
            report = SecurityScanReport(
                status=VerificationStatus.INCONCLUSIVE,
                findings=(
                    SecurityFinding(
                        rule_id="candidate_missing",
                        location="verification-worktree",
                    ),
                ),
                scanned_files=0,
                scanned_bytes=0,
            )
        else:
            try:
                content = self._changed_content(
                    candidate,
                    context.patch.changed_files,
                )
            except VerificationError:
                report = SecurityScanReport(
                    status=VerificationStatus.INCONCLUSIVE,
                    findings=(
                        SecurityFinding(
                            rule_id="content_unreadable",
                            location="changed-content",
                        ),
                    ),
                    scanned_files=0,
                    scanned_bytes=0,
                )
            else:
                report = scan_added_content(
                    content,
                    max_total_bytes=MAX_SECURITY_SCAN_BYTES,
                )
        return (
            _StepDraft(
                step=step,
                status=report.status,
                exit_code=0 if report.status is VerificationStatus.PASSED else 1,
                duration_ms=0,
                stdout="秘密与危险模式扫描通过" if not report.findings else "",
                stderr=("秘密或危险模式扫描未通过" if report.findings else ""),
            ),
            report,
        )

    def _acceptance_result(
        self,
        step: VerificationStep,
        context: TransactionVerificationContext,
        prior_drafts: tuple[_StepDraft, ...],
    ) -> tuple[_StepDraft, dict[str, object]]:
        """把每条预期和保持行为绑定到已执行步骤。"""
        baseline = tuple(
            draft
            for draft in prior_drafts
            if draft.step.kind is VerificationKind.BASELINE
        )
        expected = tuple(
            draft
            for draft in prior_drafts
            if draft.step.kind
            in {VerificationKind.REPRODUCTION, VerificationKind.TARGETED}
        )
        preserved = tuple(
            draft
            for draft in prior_drafts
            if draft.step.kind
            in {VerificationKind.RELATED, VerificationKind.REGRESSION}
            and draft.step.required
        )
        dependency_status = _aggregate_status(
            tuple(draft.status for draft in (*baseline, *expected, *preserved))
        )
        baseline_reproduced = any(
            draft.status is VerificationStatus.PASSED
            and draft.exit_code not in {None, 0}
            for draft in baseline
        )
        overlap = set(context.acceptance.expected_behaviors) & set(
            context.acceptance.preserved_behaviors
        )
        if dependency_status is not VerificationStatus.PASSED:
            status = dependency_status
        elif not baseline_reproduced or overlap:
            status = VerificationStatus.FAILED
        else:
            status = VerificationStatus.PASSED
        expected_ids = [draft.step.step_id for draft in expected]
        preserved_ids = [draft.step.step_id for draft in preserved]
        report: dict[str, object] = {
            "status": status.value,
            "baseline_reproduced": baseline_reproduced,
            "expected_behavior_bindings": {
                behavior: expected_ids
                for behavior in context.acceptance.expected_behaviors
            },
            "preserved_behavior_bindings": {
                behavior: preserved_ids
                for behavior in context.acceptance.preserved_behaviors
            },
            "conflicting_descriptions": sorted(overlap),
        }
        return (
            _StepDraft(
                step=step,
                status=status,
                exit_code=0 if status is VerificationStatus.PASSED else 1,
                duration_ms=0,
                stdout="验收条目已绑定通过证据"
                if status is VerificationStatus.PASSED
                else "",
                stderr="验收条目缺少通过证据或基线未复现"
                if status is not VerificationStatus.PASSED
                else "",
            ),
            report,
        )

    async def _resource_result(
        self,
        step: VerificationStep,
        *,
        transaction_id: str,
        cleanup_errors: tuple[str, ...],
        candidate_signature_before: dict[str, str] | None,
        candidate_signature_after: dict[str, str] | None,
    ) -> tuple[_StepDraft, dict[str, object]]:
        """复核冻结事务未漂移、验证副本未自改且资源只剩事务 Worktree。"""
        integrity_error: str | None = None
        try:
            await self._manager.verification_context(transaction_id)
        except TransactionError as error:
            integrity_error = error.code
        counts = self._scope.counts()
        counts_payload = self._resource_counts_payload(counts)
        candidate_unchanged = (
            candidate_signature_before is not None
            and candidate_signature_before == candidate_signature_after
        )
        transient_empty = (
            counts.active_task_count == 0
            and counts.active_process_count == 0
            and counts.open_client_count == 0
            and counts.open_connection_count == 0
            and counts.temporary_directory_count == 0
            and counts.temporary_worktree_count == 1
            and counts.resource_count == 1
        )
        if cleanup_errors:
            status = VerificationStatus.INCONCLUSIVE
        elif (
            integrity_error is not None
            or not candidate_unchanged
            or not transient_empty
        ):
            status = VerificationStatus.FAILED
        else:
            status = VerificationStatus.PASSED
        report: dict[str, object] = {
            "status": status.value,
            "cleanup_errors": list(cleanup_errors),
            "transaction_integrity_error": integrity_error,
            "candidate_changed_during_verification": not candidate_unchanged,
            "resource_counts": counts_payload,
        }
        return (
            _StepDraft(
                step=step,
                status=status,
                exit_code=0 if status is VerificationStatus.PASSED else 1,
                duration_ms=0,
                stdout="资源与事务完整性通过"
                if status is VerificationStatus.PASSED
                else "",
                stderr="资源清理或事务完整性检查未通过"
                if status is not VerificationStatus.PASSED
                else "",
            ),
            report,
        )

    def _build_log_payloads(
        self,
        drafts: tuple[_StepDraft, ...],
        *,
        scope_report: Mapping[str, object],
        security_report: SecurityScanReport | None,
        acceptance_report: Mapping[str, object],
        resource_report: Mapping[str, object],
    ) -> dict[str, bytes]:
        """把命令输出合并到固定日志，并把内部检查写为 JSON。"""
        log_parts: dict[str, list[str]] = {
            "baseline.log": [],
            "focused_tests.log": [],
            "regression_tests.log": [],
            "static_checks.log": [],
        }
        for draft in drafts:
            log_path = LOG_PATHS[draft.step.kind]
            if log_path not in log_parts:
                continue
            command_json = json.dumps(
                draft.step.command,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            text = (
                f"step_id={draft.step.step_id}\n"
                f"kind={draft.step.kind.value}\n"
                f"required={str(draft.step.required).lower()}\n"
                f"status={draft.status.value}\n"
                f"exit_code={draft.exit_code}\n"
                f"duration_ms={draft.duration_ms}\n"
                f"argv={command_json}\n"
                f"stdout:\n{draft.stdout}\n"
                f"stderr:\n{draft.stderr}\n"
            )
            log_parts[log_path].append(self._redactor.redact_text(text))
        payloads = {
            name: ("\n".join(parts).rstrip() + "\n").encode("utf-8")
            for name, parts in log_parts.items()
        }
        payloads["scope_check.json"] = self._json_bytes(scope_report)
        payloads["secret_scan.json"] = self._json_bytes(
            security_report.model_dump(mode="json")
            if security_report is not None
            else {"status": VerificationStatus.INCONCLUSIVE.value}
        )
        payloads["acceptance_check.json"] = self._json_bytes(acceptance_report)
        payloads["resource_check.json"] = self._json_bytes(resource_report)
        return payloads

    def _finalize_result(
        self,
        draft: _StepDraft,
        log_payloads: Mapping[str, bytes],
    ) -> VerificationResult:
        """绑定最终日志路径和哈希后构造严格结果契约。"""
        log_path = LOG_PATHS[draft.step.kind]
        log_content = log_payloads[log_path]
        return VerificationResult(
            step=draft.step,
            status=draft.status,
            exit_code=draft.exit_code,
            duration_ms=draft.duration_ms,
            stdout_summary=draft.stdout[:MAX_RESULT_SUMMARY_CHARS],
            stderr_summary=draft.stderr[:MAX_RESULT_SUMMARY_CHARS],
            output_truncated=draft.output_truncated,
            log_path=log_path,
            log_sha256=sha256_digest(log_content),
        )

    def _evidence_payloads(
        self,
        *,
        context: TransactionVerificationContext,
        detection: ProjectDetection,
        matrix_steps: tuple[VerificationStep, ...],
        verdict: Verdict,
        log_payloads: Mapping[str, bytes],
    ) -> dict[str, bytes]:
        """生成 EvidenceBundle 的固定最小集合和附加审计事实。"""
        acceptance_hash = context.record.acceptance_sha256
        if acceptance_hash is None:
            raise VerificationError(
                "verification.acceptance_missing",
                "证据生成缺少 AcceptanceSpec 哈希",
            )
        failed_steps = [
            result.step.step_id
            for result in verdict.results
            if result.status is not VerificationStatus.PASSED
        ]
        risk_lines = [
            "# 风险报告",
            "",
            f"- 最终状态：{verdict.status.value}",
            f"- 原始补丁哈希：`{context.patch.patch_sha256}`",
            f"- 未通过步骤：{', '.join(failed_steps) if failed_steps else '无'}",
            "- 验证命令在隔离副本运行；操作系统级网络沙箱由 Phase 11 接入。",
            "- 可接受风险：",
            *[f"  - {risk}" for risk in context.acceptance.acceptable_risks],
            "- 明确非目标：",
            *[f"  - {goal}" for goal in context.acceptance.non_goals],
            "",
        ]
        summary = (
            "# 验证摘要\n\n"
            f"事务 `{context.record.transaction_id}` 的确定性结论为 "
            f"`{verdict.status.value}`。\n"
            "该结论完全由 required 步骤状态计算，未使用模型完成声明。\n"
        )
        detection_payload = {
            "kinds": [kind.value for kind in detection.kinds],
            "candidates": [
                {
                    "kind": candidate.kind.value,
                    "category": candidate.category,
                    "argv": list(candidate.argv),
                    "reason": candidate.reason,
                    "executed": False,
                }
                for candidate in detection.candidates
            ],
            "configuration_present": detection.configuration is not None,
            "configuration_confirmed": self._configuration_confirmed,
        }
        reverse_payload = {
            "advisory_only": True,
            "provided": self._reverse_patch_description is not None,
            "description": self._reverse_patch_description,
            "changes_verdict": False,
        }
        payloads = dict(log_payloads)
        payloads.update(
            {
                "acceptance_spec.json": self._json_bytes(
                    context.acceptance.model_dump(mode="json")
                ),
                "acceptance_spec.sha256": f"{acceptance_hash}\n".encode("ascii"),
                "patch.diff": context.patch_path.read_bytes(),
                "changed_files.json": self._json_bytes(
                    {"changed_files": list(context.patch.changed_files)}
                ),
                "changed_symbols.json": self._json_bytes(
                    {"changed_symbols": list(context.patch.changed_symbols)}
                ),
                "risk_report.md": "\n".join(risk_lines).encode("utf-8"),
                "matrix.json": self._json_bytes(
                    {"steps": [step.model_dump(mode="json") for step in matrix_steps]}
                ),
                "verdict.json": self._json_bytes(verdict.model_dump(mode="json")),
                "summary.md": summary.encode("utf-8"),
                "project_detection.json": self._json_bytes(detection_payload),
                "reverse_patch_check.json": self._json_bytes(reverse_payload),
            }
        )
        return payloads

    @staticmethod
    def _order_drafts(
        steps: tuple[VerificationStep, ...],
        drafts: tuple[_StepDraft, ...],
    ) -> tuple[_StepDraft, ...]:
        """拒绝缺失、重复或额外结果并恢复矩阵冻结顺序。"""
        by_id = {draft.step.step_id: draft for draft in drafts}
        if len(by_id) != len(drafts) or set(by_id) != {step.step_id for step in steps}:
            raise VerificationError(
                "verification.result_matrix_mismatch",
                "验证结果与冻结矩阵不一致",
            )
        return tuple(by_id[step.step_id] for step in steps)

    @staticmethod
    def _is_executable(program: str, path_environment: str) -> bool:
        """处理绝对程序路径和 PATH 中程序名。"""
        candidate = Path(program)
        if candidate.is_absolute():
            return candidate.is_file() and os.access(candidate, os.X_OK)
        return shutil.which(program, path=path_environment) is not None

    def _changed_content(
        self,
        root: Path,
        changed_files: tuple[str, ...],
    ) -> dict[str, bytes]:
        """不跟随链接且有界读取当前补丁路径。"""
        content: dict[str, bytes] = {}
        total = 0
        resolved_root = root.resolve(strict=True)
        for relative_path in changed_files:
            path = resolved_root / relative_path
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise VerificationError(
                    "verification.changed_content_unreadable",
                    "补丁内容不可读",
                ) from error
            if stat.S_ISLNK(metadata.st_mode):
                raw = os.fsencode(os.readlink(path))
            elif stat.S_ISREG(metadata.st_mode):
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(resolved_root)
                    with resolved.open("rb") as stream:
                        raw = stream.read(MAX_SECURITY_SCAN_BYTES + 1)
                except (OSError, ValueError) as error:
                    raise VerificationError(
                        "verification.changed_content_unreadable",
                        "补丁内容越界或不可读",
                    ) from error
            else:
                raise VerificationError(
                    "verification.changed_content_type",
                    "补丁路径不是普通文件或链接",
                )
            total += len(raw)
            content[relative_path] = raw
            if total > MAX_SECURITY_SCAN_BYTES:
                break
        return content

    def _path_signatures(
        self,
        root: Path,
        changed_files: tuple[str, ...],
    ) -> dict[str, str]:
        """只指纹补丁声明路径，检测验证命令自修改。"""
        signatures: dict[str, str] = {}
        resolved_root = root.resolve(strict=True)
        for relative_path in changed_files:
            path = resolved_root / relative_path
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                signatures[relative_path] = "missing"
                continue
            except OSError:
                signatures[relative_path] = "unreadable"
                continue
            digest = hashlib.sha256()
            digest.update(str(stat.S_IFMT(metadata.st_mode)).encode("ascii"))
            digest.update(str(metadata.st_mode & 0o777).encode("ascii"))
            if stat.S_ISLNK(metadata.st_mode):
                digest.update(os.fsencode(os.readlink(path)))
            elif stat.S_ISREG(metadata.st_mode):
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(resolved_root)
                    with resolved.open("rb") as stream:
                        while chunk := stream.read(1024 * 1024):
                            digest.update(chunk)
                except (OSError, ValueError):
                    signatures[relative_path] = "unreadable"
                    continue
            else:
                signatures[relative_path] = "unsupported"
                continue
            signatures[relative_path] = f"sha256:{digest.hexdigest()}"
        return signatures

    def _redact_bytes(self, content: bytes) -> str:
        """把有界输出解码为 UTF-8 并执行固定秘密脱敏。"""
        return self._redactor.redact_text(content.decode("utf-8", errors="replace"))

    @staticmethod
    def _resource_counts_payload(counts: ResourceCounts) -> dict[str, int]:
        """把 ResourceCounts 转为稳定 JSON 字段。"""
        return {
            "active_task_count": counts.active_task_count,
            "active_process_count": counts.active_process_count,
            "open_client_count": counts.open_client_count,
            "open_connection_count": counts.open_connection_count,
            "temporary_directory_count": counts.temporary_directory_count,
            "temporary_worktree_count": counts.temporary_worktree_count,
            "resource_count": counts.resource_count,
        }

    @staticmethod
    def _json_bytes(payload: object) -> bytes:
        """生成带换行的规范 JSON 证据。"""
        return canonical_json_bytes(payload) + b"\n"

    @staticmethod
    def _duration_ms(started: float) -> int:
        """把单调时钟差转换为非负毫秒。"""
        return max(0, int((time.monotonic() - started) * 1_000))

    def _now(self) -> datetime:
        """读取可注入且必须带时区的 Verdict 时间。"""
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise VerificationError(
                "verification.clock_naive",
                "验证时钟必须带时区",
            )
        return value
