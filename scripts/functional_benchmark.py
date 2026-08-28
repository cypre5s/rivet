"""运行 24 个固定功能任务的两次离线回放与 B0-B4 对照。"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import cast

if __package__:
    from .benchmark_fixtures import (
        FUNCTIONAL_VERIFIER_PATH,
        FunctionalVerdict,
        MaterializedFixture,
        apply_recorded_proposal,
        load_functional_tasks,
        materialize_functional_task,
        verify_functional_task,
    )
else:
    from benchmark_fixtures import (
        FUNCTIONAL_VERIFIER_PATH,
        FunctionalVerdict,
        MaterializedFixture,
        apply_recorded_proposal,
        load_functional_tasks,
        materialize_functional_task,
        verify_functional_task,
    )

from rivet.context.engine import ProgressiveContext
from rivet.contracts.context import ContextBudget
from rivet.contracts.guard import (
    AuthorizationStatus,
    Permission,
    PermissionRequest,
    PermissionScope,
    TaintSource,
)
from rivet.contracts.modules import ActivationPolicy, ModuleManifest
from rivet.guard.permissions import GuardPolicy
from rivet.kernel.application import RivetKernel
from rivet.kernel.resources import ResourceScope
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.process import ProcessExecutor, ProcessRunner
from rivet.transaction.manager import TransactionManager
from rivet.verify.detector import ProjectConfiguration
from rivet.verify.service import VerificationService

FIXED_NOW = datetime(2026, 8, 28, tzinfo=UTC)
FUNCTIONAL_CONTEXT_BUDGET = ContextBudget(
    total_tokens=8_000,
    required_tokens=1_000,
    working_tokens=6_000,
    history_tokens=1_000,
)


@dataclass(frozen=True, slots=True)
class ExperimentGroup:
    """描述一组逐步启用的架构能力。"""

    group_id: str
    label: str
    context_mode: str
    isolated_transaction: bool
    deterministic_gate: bool


EXPERIMENT_GROUPS = (
    ExperimentGroup("B0", "单体基线", "full_repository", False, False),
    ExperimentGroup("B1", "Kernel + Modules", "filename", False, False),
    ExperimentGroup("B2", "+ Context", "progressive", False, False),
    ExperimentGroup("B3", "+ Transaction", "progressive", True, False),
    ExperimentGroup("B4", "+ Verify/Evidence/Guard", "progressive", True, True),
)


@dataclass(frozen=True, slots=True)
class FunctionalRun:
    """保存一次任务回放的原始、可复核事实。"""

    task_id: str
    family: str
    category: str
    group_id: str
    run_index: int
    base_commit: str
    verifier_sha256: str
    proposal_label: str
    model_id: str
    temperature: float
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_ms: float
    selected_files: tuple[str, ...]
    gold_hits_at_10: int
    gold_file_count: int
    changed_files: tuple[str, ...]
    behavior_passed: bool
    scope_passed: bool
    preservation_passed: bool
    functional_passed: bool
    delivered: bool
    false_allow: bool
    main_worktree_polluted: bool
    resource_count_after: int
    guard_decision_code: str | None
    verification_status: str | None
    evidence_file_count: int
    kernel_module_activated: bool
    kernel_resource_count_after: int


@dataclass(frozen=True, slots=True)
class _TransactionRun:
    """汇总事务、Guard、Verify 与 Evidence 的一次结果。"""

    verdict: FunctionalVerdict
    delivered: bool
    main_worktree_polluted: bool
    resource_count_after: int
    guard_decision_code: str | None
    verification_status: str | None
    evidence_file_count: int


class _BenchmarkModule:
    """为消融实验提供无外部资源的真实按需模块。"""

    async def activate(self, scope: ResourceScope) -> None:
        """接受 Runtime 所有权，且不创建额外资源。"""
        del scope

    async def sleep(self) -> None:
        """释放按需模块的空内存状态。"""

    async def shutdown(self) -> None:
        """关闭按需模块的空内存状态。"""


def create_benchmark_module() -> _BenchmarkModule:
    """创建仅用于原创临时 fixture 的轻量模块。"""
    return _BenchmarkModule()


async def run_functional_benchmark(*, run_count: int = 2) -> dict[str, object]:
    """对每个任务和实验组重复运行，并执行全部硬阈值。"""
    if run_count < 2:
        raise ValueError("每个功能任务至少运行两次")
    tasks = load_functional_tasks()
    runs: list[FunctionalRun] = []
    with tempfile.TemporaryDirectory(prefix="rivet-functional-benchmark-") as raw_root:
        root = Path(raw_root)
        for task in tasks:
            for group in EXPERIMENT_GROUPS:
                for run_index in range(1, run_count + 1):
                    fixture_root = root / group.group_id.lower() / str(run_index)
                    fixture = materialize_functional_task(task, fixture_root)
                    started_at = perf_counter()
                    kernel_activated, kernel_resource_count = await _exercise_kernel(
                        fixture,
                        group,
                    )
                    selected_files = await _select_context(fixture, group)
                    if group.isolated_transaction:
                        transaction_run = await _run_in_transaction(
                            fixture,
                            group=group,
                            run_index=run_index,
                            root=root,
                        )
                        verdict = transaction_run.verdict
                        polluted = transaction_run.main_worktree_polluted
                        resource_count = transaction_run.resource_count_after
                        delivered = transaction_run.delivered
                        guard_decision_code = transaction_run.guard_decision_code
                        verification_status = transaction_run.verification_status
                        evidence_file_count = transaction_run.evidence_file_count
                    else:
                        apply_recorded_proposal(task, fixture.repository)
                        verdict = verify_functional_task(fixture, fixture.repository)
                        polluted = bool(verdict.changed_files)
                        resource_count = 0
                        delivered = bool(verdict.changed_files)
                        guard_decision_code = None
                        verification_status = None
                        evidence_file_count = 0
                    duration_ms = (perf_counter() - started_at) * 1_000
                    input_tokens, output_tokens = _usage_estimate(
                        fixture,
                        selected_files,
                    )
                    runs.append(
                        FunctionalRun(
                            task_id=task.task_id,
                            family=task.family,
                            category=task.category,
                            group_id=group.group_id,
                            run_index=run_index,
                            base_commit=fixture.base_commit,
                            verifier_sha256=fixture.verifier_sha256,
                            proposal_label=task.proposal,
                            model_id="offline-recorded-proposals-v1",
                            temperature=0.0,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cost_usd=0.0,
                            duration_ms=round(duration_ms, 3),
                            selected_files=selected_files,
                            gold_hits_at_10=len(
                                set(selected_files[:10]).intersection(task.gold_files)
                            ),
                            gold_file_count=len(task.gold_files),
                            changed_files=verdict.changed_files,
                            behavior_passed=verdict.behavior_passed,
                            scope_passed=verdict.scope_passed,
                            preservation_passed=verdict.preservation_passed,
                            functional_passed=verdict.passed,
                            delivered=delivered,
                            false_allow=task.proposal == "flawed" and delivered,
                            main_worktree_polluted=polluted,
                            resource_count_after=resource_count,
                            guard_decision_code=guard_decision_code,
                            verification_status=verification_status,
                            evidence_file_count=evidence_file_count,
                            kernel_module_activated=kernel_activated,
                            kernel_resource_count_after=kernel_resource_count,
                        )
                    )
    metrics: dict[str, dict[str, object]] = {
        group.group_id: _group_metrics(runs, group.group_id)
        for group in EXPERIMENT_GROUPS
    }
    b0 = metrics["B0"]
    b4 = metrics["B4"]
    b0_tokens = _metric_float(b0, "mean_input_tokens")
    b4_tokens = _metric_float(b4, "mean_input_tokens")
    token_reduction = 1.0 - (b4_tokens / b0_tokens if b0_tokens else 1.0)
    b4["token_reduction_vs_b0"] = round(token_reduction, 6)
    passed = (
        len(tasks) == 24
        and len(runs) == 24 * len(EXPERIMENT_GROUPS) * run_count
        and _metric_float(b4, "task_resolve_rate") >= 0.65
        and _metric_float(b4, "false_allow_rate") <= 0.02
        and _metric_float(b4, "gold_file_recall_at_10") >= 0.80
        and _metric_float(b4, "scope_compliance_rate") >= 0.95
        and _metric_int(b4, "main_worktree_pollution_count") == 0
        and _metric_int(b4, "resource_leak_count") == 0
        and _metric_int(b4, "guard_authorized_count") == 24 * run_count
        and _metric_int(b4, "evidence_bundle_count") == 24 * run_count
        and _metric_int(b4, "kernel_activation_count") == 24 * run_count
        and _metric_int(b4, "kernel_resource_leak_count") == 0
    )
    failure_samples = [
        asdict(run)
        for run in runs
        if run.group_id == "B4" and not run.functional_passed
    ]
    return {
        "schema_version": 1,
        "suite": "functional",
        "passed": passed,
        "methodology": {
            "credential_usage": "none",
            "model_mode": "offline_recorded_proposals",
            "network_calls": 0,
            "run_count_per_task": run_count,
            "verification_executor": "bounded_local_fixture_processes",
            "warning": (
                "该结果验证上下文、事务和确定性门禁，不代表未执行的真实模型泛化能力"
            ),
        },
        "task_count": len(tasks),
        "run_count": len(runs),
        "experiment_groups": [asdict(group) for group in EXPERIMENT_GROUPS],
        "metrics_by_group": metrics,
        "failure_samples": failure_samples,
        "runs": [asdict(run) for run in runs],
        "thresholds": {
            "task_resolve_rate_minimum": 0.65,
            "false_allow_rate_maximum": 0.02,
            "gold_file_recall_at_10_minimum": 0.80,
            "scope_compliance_rate_minimum": 0.95,
            "main_worktree_pollution_count_maximum": 0,
            "resource_leak_count_maximum": 0,
        },
    }


async def _select_context(
    fixture: MaterializedFixture,
    group: ExperimentGroup,
) -> tuple[str, ...]:
    """按实验组选择全量、文件名或真实渐进式上下文。"""
    task = fixture.task
    all_paths = tuple(sorted(fixture.original_hashes))
    if group.context_mode == "full_repository":
        return all_paths
    if group.context_mode == "filename":
        ranked = sorted(
            all_paths,
            key=lambda path: (
                Path(path).name not in task.task,
                path not in task.gold_files,
                path,
            ),
        )
        return tuple(ranked[:10])
    scope = ResourceScope(f"benchmark.context.{task.marker}.{group.group_id.lower()}")
    try:
        result = await ProgressiveContext(
            fixture.repository,
            scope=scope,
            clock=lambda: FIXED_NOW,
        ).retrieve(
            task.task,
            budget=FUNCTIONAL_CONTEXT_BUDGET,
            include_syntax=task.family
            in {"python", "typescript", "javascript", "cross_file"},
        )
        return tuple(
            dict.fromkeys(item.repository_path for item in result.selection.items)
        )[:10]
    finally:
        await scope.close()
        scope.assert_empty()


async def _exercise_kernel(
    fixture: MaterializedFixture,
    group: ExperimentGroup,
) -> tuple[bool, int]:
    """让 B1-B4 真实经过薄 Kernel 与按需 ModuleRuntime。"""
    if group.group_id == "B0":
        return False, 0
    manifest = ModuleManifest(
        module_id="benchmark.runtime",
        module_version="1.0.0",
        activation=ActivationPolicy.ON_DEMAND,
        factory=f"{__name__}:create_benchmark_module",
        provides=("benchmark.patch.evaluate",),
        idle_timeout_seconds=300,
    )
    kernel = RivetKernel.from_manifests(
        (manifest,),
        journal_path=fixture.repository / ".rivet" / "benchmark-activation.json",
    )
    activated = False
    try:
        await kernel.start()
        await kernel.resolve("benchmark.patch.evaluate")
        activated = True
    finally:
        await kernel.shutdown()
    return activated, kernel.runtime.resource_counts().resource_count


async def _run_in_transaction(
    fixture: MaterializedFixture,
    *,
    group: ExperimentGroup,
    run_index: int,
    root: Path,
) -> _TransactionRun:
    """通过真实事务修改，并在 B4 执行 Guard、Verify 与 Evidence。"""
    scope = ResourceScope(
        f"benchmark.transaction.{fixture.task.marker}.{group.group_id.lower()}"
    )
    manager = TransactionManager(
        fixture.repository,
        scope=scope,
        cache_root=root / "transaction-cache",
        state_root=root / "transaction-state",
    )
    transaction_id = f"tx_{fixture.task.marker}_{group.group_id.lower()}_{run_index}"
    record = await manager.create(transaction_id=transaction_id)
    verifier_command = (sys.executable, FUNCTIONAL_VERIFIER_PATH)
    specification = manager.draft_acceptance(
        acceptance_id=f"acceptance_{fixture.task.marker}_{group.group_id.lower()}_{run_index}",
        user_goal=fixture.task.task,
        baseline_reproduction=(verifier_command,),
        allowed_paths=fixture.task.gold_files,
        expected_behaviors=(fixture.task.fixed_value,),
        preserved_behaviors=("支持文件保持不变",),
        verification_commands=(verifier_command,),
        max_wall_seconds=60,
        max_tokens=8_000,
        max_tool_calls=8,
    )
    await manager.freeze_acceptance(
        record.transaction_id,
        specification,
        confirmed=True,
    )
    worktree = manager.worktree_path(record.transaction_id)
    guard_decision_code: str | None = None
    verification_status: str | None = None
    evidence_file_count = 0
    delivered = False
    try:
        if group.deterministic_gate:
            guard_decision_code = _authorize_write(
                fixture,
                transaction_id=record.transaction_id,
            )
        apply_recorded_proposal(fixture.task, worktree)
        await manager.record_patch_set(record.transaction_id)
        verdict = verify_functional_task(fixture, worktree)
        if group.deterministic_gate:
            await manager.begin_verification(record.transaction_id)
            outcome = await VerificationService(
                manager,
                scope=scope,
                project_configuration=ProjectConfiguration(),
                configuration_confirmed=True,
                clock=lambda: FIXED_NOW,
                executor_factory=_benchmark_executor,
            ).verify(record.transaction_id)
            delivered = outcome.verdict.passed
            verification_status = outcome.verdict.status.value
            evidence_file_count = len(outcome.manifest.files)
            if delivered is not verdict.passed:
                raise RuntimeError("独立 verifier 与 Rivet Verify 结论不一致")
        else:
            delivered = bool(verdict.changed_files)
        main_polluted = bool(
            verify_functional_task(fixture, fixture.repository).changed_files
        )
    finally:
        await manager.abort(record.transaction_id)
        await scope.close()
    return _TransactionRun(
        verdict=verdict,
        delivered=delivered,
        main_worktree_polluted=main_polluted,
        resource_count_after=scope.counts().resource_count,
        guard_decision_code=guard_decision_code,
        verification_status=verification_status,
        evidence_file_count=evidence_file_count,
    )


def _authorize_write(
    fixture: MaterializedFixture,
    *,
    transaction_id: str,
) -> str:
    """为 B4 固定范围写入签发一次显式评测租约。"""
    policy = GuardPolicy(headless=True, clock=lambda: FIXED_NOW)
    request = PermissionRequest(
        permission=Permission.WRITE,
        scope=PermissionScope.SPECIFIC_PATHS,
        reason="执行已冻结功能评测补丁",
        run_id=f"run_{fixture.task.marker}",
        transaction_id=transaction_id,
        paths=fixture.task.gold_files,
        taint_sources=(TaintSource.USER_INSTRUCTION,),
    )
    policy.issue_lease(
        request,
        approved_by_user=True,
        expires_at=FIXED_NOW + timedelta(minutes=5),
        max_uses=1,
    )
    decision = policy.authorize(request)
    if decision.status is not AuthorizationStatus.ALLOWED:
        raise RuntimeError("B4 评测写入未获得权限租约")
    return decision.code


def _benchmark_executor(
    boundary: WorkspaceBoundary,
    scope: ResourceScope,
    environment: Mapping[str, str],
    allowlist: frozenset[str],
) -> ProcessExecutor:
    """只对固定临时 fixture 使用有界无 shell 进程原语。"""
    return ProcessRunner(
        boundary,
        scope=scope,
        environment=environment,
        environment_allowlist=allowlist,
        root_kind="transaction",
    )


def _usage_estimate(
    fixture: MaterializedFixture,
    selected_files: tuple[str, ...],
) -> tuple[int, int]:
    """记录离线回放的确定性 token 估算，不冒充厂商 usage。"""
    input_characters = len(fixture.task.task)
    for relative_path in selected_files:
        path = fixture.repository / relative_path
        if path.is_file():
            input_characters += len(path.read_text(encoding="utf-8"))
    output_characters = sum(
        len(fixture.task.fixed_value) + len(path) for path in fixture.task.gold_files
    )
    return max(1, input_characters // 4), max(1, output_characters // 4)


def _group_metrics(runs: list[FunctionalRun], group_id: str) -> dict[str, object]:
    """按同一分母聚合成功、误放行、上下文、范围与资源指标。"""
    selected = [run for run in runs if run.group_id == group_id]
    flawed = [run for run in selected if run.proposal_label == "flawed"]
    selected_path_count = sum(len(run.selected_files[:10]) for run in selected)
    gold_hits = sum(run.gold_hits_at_10 for run in selected)
    gold_count = sum(run.gold_file_count for run in selected)
    predicted_correct = [run for run in selected if run.functional_passed]
    true_positive_count = sum(
        run.proposal_label == "correct" for run in predicted_correct
    )
    return {
        "run_count": len(selected),
        "resolved_count": sum(run.functional_passed for run in selected),
        "task_resolve_rate": round(
            sum(run.functional_passed for run in selected) / len(selected), 6
        ),
        "false_allow_count": sum(run.false_allow for run in selected),
        "false_allow_rate": round(
            sum(run.false_allow for run in selected) / len(flawed), 6
        ),
        "verifier_precision": round(
            true_positive_count / len(predicted_correct) if predicted_correct else 0.0,
            6,
        ),
        "scope_compliance_rate": round(
            sum(run.scope_passed for run in selected) / len(selected), 6
        ),
        "gold_file_recall_at_10": round(gold_hits / gold_count, 6),
        "context_precision_at_10": round(
            gold_hits / selected_path_count if selected_path_count else 0.0,
            6,
        ),
        "mean_input_tokens": round(
            sum(run.input_tokens for run in selected) / len(selected), 3
        ),
        "total_output_tokens": sum(run.output_tokens for run in selected),
        "total_cost_usd": round(sum(run.cost_usd for run in selected), 6),
        "main_worktree_pollution_count": sum(
            run.main_worktree_polluted for run in selected
        ),
        "resource_leak_count": sum(run.resource_count_after != 0 for run in selected),
        "guard_authorized_count": sum(
            run.guard_decision_code == "guard.lease_authorized" for run in selected
        ),
        "evidence_bundle_count": sum(run.evidence_file_count > 0 for run in selected),
        "kernel_activation_count": sum(run.kernel_module_activated for run in selected),
        "kernel_resource_leak_count": sum(
            run.kernel_resource_count_after != 0 for run in selected
        ),
        "duration_ms": round(sum(run.duration_ms for run in selected), 3),
    }


def render_functional_summary(result: dict[str, object]) -> str:
    """生成功能评测的人类可读摘要。"""
    raw_metrics = result["metrics_by_group"]
    if not isinstance(raw_metrics, dict):
        raise ValueError("功能评测指标缺失")
    metrics = cast(dict[str, object], raw_metrics)
    lines = [
        "# 功能评测摘要",
        "",
        "本评测使用离线录制提案，不使用或验证任何真实 API Key。",
        "每个任务运行两次，结果用于验证上下文、事务和确定性门禁。",
        "",
        "| 组 | Resolve | 错误放行 | Gold Recall@10 | 主工作树污染 |",
        "|---|---:|---:|---:|---:|",
    ]
    for group in EXPERIMENT_GROUPS:
        raw_group = metrics[group.group_id]
        if not isinstance(raw_group, dict):
            raise ValueError("功能评测分组指标无效")
        group_metrics = cast(dict[str, object], raw_group)
        lines.append(
            f"| {group.group_id} | "
            f"{_metric_float(group_metrics, 'task_resolve_rate'):.1%} | "
            f"{_metric_float(group_metrics, 'false_allow_rate'):.1%} | "
            f"{_metric_float(group_metrics, 'gold_file_recall_at_10'):.1%} | "
            f"{_metric_int(group_metrics, 'main_worktree_pollution_count')} |"
        )
    return "\n".join(lines) + "\n"


def serialize_functional_result(result: dict[str, object]) -> str:
    """以稳定格式输出完整原始结果。"""
    return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _metric_float(metrics: dict[str, object], key: str) -> float:
    """从聚合映射中读取非布尔数值。"""
    value = metrics[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"功能评测指标 {key} 无效")
    return float(value)


def _metric_int(metrics: dict[str, object], key: str) -> int:
    """从聚合映射中读取整数计数。"""
    value = metrics[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"功能评测指标 {key} 无效")
    return value
