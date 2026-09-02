"""开发期离线评测：词法检索、隔离候选与独立 oracle。"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter

if __package__:
    from .benchmark_fixtures import (
        FUNCTIONAL_VERIFIER_PATH,
        apply_recorded_proposal,
        load_functional_tasks,
        materialize_functional_task,
        verify_functional_task,
    )
else:
    from benchmark_fixtures import (
        FUNCTIONAL_VERIFIER_PATH,
        apply_recorded_proposal,
        load_functional_tasks,
        materialize_functional_task,
        verify_functional_task,
    )

from rivet.context.lexical import LexicalContext
from rivet.kernel.resources import ResourceScope
from rivet.transaction.manager import TransactionManager


@dataclass(frozen=True, slots=True)
class FunctionalRun:
    task_id: str
    proposal_label: str
    run_index: int
    selected_files: tuple[str, ...]
    gold_hits_at_10: int
    gold_file_count: int
    changed_files: tuple[str, ...]
    oracle_passed: bool
    accepted: bool
    false_allow: bool
    main_worktree_polluted: bool
    resource_count_after: int
    duration_ms: float


async def run_functional_benchmark(
    *,
    run_count: int = 2,
    task_limit: int | None = None,
) -> dict[str, object]:
    """回放固定提案；结果只代表离线架构门禁，不代表模型能力。"""
    if run_count <= 0:
        raise ValueError("run_count 必须大于零")
    all_tasks = load_functional_tasks()
    if task_limit is not None and not 1 <= task_limit <= len(all_tasks):
        raise ValueError("task_limit 超出固定任务数量")
    tasks = all_tasks if task_limit is None else all_tasks[:task_limit]
    runs: list[FunctionalRun] = []
    with tempfile.TemporaryDirectory(prefix="rivet-functional-benchmark-") as raw:
        root = Path(raw)
        for task in tasks:
            for run_index in range(1, run_count + 1):
                fixture = materialize_functional_task(
                    task,
                    root / "fixtures" / f"run_{run_index}",
                )
                started_at = perf_counter()
                scope = ResourceScope(f"benchmark.{task.marker}.run_{run_index}")
                lexical = LexicalContext(fixture.repository, scope=scope)
                result = await lexical.search(
                    " ".join((task.task, task.marker, *task.gold_files)),
                    max_results=10,
                )
                selected_files = tuple(match.path for match in result.matches)
                manager = TransactionManager(
                    fixture.repository,
                    scope=scope,
                    cache_root=(root / "cache" / task.task_id / f"run_{run_index}"),
                    state_root=(root / "state" / task.task_id / f"run_{run_index}"),
                )
                specification = manager.draft_acceptance(
                    acceptance_id=f"acceptance_{task.marker}_{run_index}",
                    user_goal=task.task,
                    baseline_reproduction=((sys.executable, FUNCTIONAL_VERIFIER_PATH),),
                    allowed_paths=task.gold_files,
                    expected_behaviors=("独立 oracle 接受正确提案",),
                    preserved_behaviors=("非目标文件哈希保持不变",),
                    verification_commands=(),
                    behavior_verification_commands=(
                        (sys.executable, FUNCTIONAL_VERIFIER_PATH),
                    ),
                    max_wall_seconds=60,
                    max_tokens=1_000,
                    max_tool_calls=10,
                )
                record = await manager.create(
                    specification,
                    confirmed=True,
                    transaction_id=f"tx_{task.marker}_{run_index}",
                )
                candidate_root = manager.transaction_boundary(
                    record.transaction_id
                ).effective_root
                apply_recorded_proposal(task, candidate_root)
                patch_set = await manager.record_patch_set(record.transaction_id)
                oracle = verify_functional_task(fixture, candidate_root)
                polluted = _main_worktree_polluted(
                    fixture.repository,
                    fixture.original_hashes,
                )
                accepted = oracle.passed
                await manager.abort(record.transaction_id)
                resources = scope.counts().resource_count
                await scope.close()
                scope.assert_empty()
                runs.append(
                    FunctionalRun(
                        task_id=task.task_id,
                        proposal_label=task.proposal,
                        run_index=run_index,
                        selected_files=selected_files,
                        gold_hits_at_10=len(
                            set(selected_files[:10]).intersection(task.gold_files)
                        ),
                        gold_file_count=len(task.gold_files),
                        changed_files=patch_set.changed_files,
                        oracle_passed=oracle.passed,
                        accepted=accepted,
                        false_allow=task.proposal == "flawed" and accepted,
                        main_worktree_polluted=polluted,
                        resource_count_after=resources,
                        duration_ms=round(
                            (perf_counter() - started_at) * 1_000,
                            3,
                        ),
                    )
                )

    total_gold = sum(run.gold_file_count for run in runs)
    total_hits = sum(run.gold_hits_at_10 for run in runs)
    metrics = {
        "accept_count": sum(run.accepted for run in runs),
        "false_allow_count": sum(run.false_allow for run in runs),
        "gold_file_recall_at_10": round(total_hits / total_gold, 6),
        "main_worktree_pollution_count": sum(
            run.main_worktree_polluted for run in runs
        ),
        "resource_leak_count": sum(run.resource_count_after != 0 for run in runs),
    }
    passed = (
        metrics["false_allow_count"] == 0
        and metrics["gold_file_recall_at_10"] >= 0.8
        and metrics["main_worktree_pollution_count"] == 0
        and metrics["resource_leak_count"] == 0
        and all(run.oracle_passed == (run.proposal_label == "correct") for run in runs)
    )
    return {
        "schema_version": 1,
        "suite": "functional",
        "passed": passed,
        "task_count": len(tasks),
        "run_count": len(runs),
        "repetitions_per_task": run_count,
        "methodology": {
            "credential_usage": "none",
            "model_mode": "offline_recorded_proposals",
            "network_calls": 0,
            "warning": "结果只验证词法检索、隔离补丁与独立 oracle",
        },
        "metrics": metrics,
        "runs": [asdict(run) for run in runs],
    }


def _main_worktree_polluted(
    repository: Path,
    original_hashes: dict[str, str],
) -> bool:
    for relative_path, expected in original_hashes.items():
        path = repository / relative_path
        if not path.is_file():
            return True
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            return True
    return False
