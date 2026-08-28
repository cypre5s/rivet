"""验证功能评测清单、固定基线和独立 verifier。"""

from __future__ import annotations

from pathlib import Path

from scripts.benchmark_fixtures import (
    FUNCTIONAL_VERIFIER_PATH,
    apply_recorded_proposal,
    load_functional_tasks,
    materialize_functional_task,
    verify_functional_task,
)


def test_functional_manifest_has_required_family_counts() -> None:
    tasks = load_functional_tasks()

    assert len(tasks) == 24
    assert sum(task.family == "python" for task in tasks) == 8
    assert sum(task.family in {"typescript", "javascript"} for task in tasks) == 6
    assert sum(task.family == "config" for task in tasks) == 4
    assert sum(task.family == "documentation" for task in tasks) == 3
    assert sum(task.family == "cross_file" for task in tasks) == 3
    assert sum(task.proposal == "correct" for task in tasks) == 18


def test_materialized_base_commit_and_verifier_are_deterministic(
    tmp_path: Path,
) -> None:
    task = load_functional_tasks()[0]

    first = materialize_functional_task(task, tmp_path / "first")
    second = materialize_functional_task(task, tmp_path / "second")

    assert first.base_commit == second.base_commit
    assert first.verifier_sha256 == second.verifier_sha256
    assert (first.repository / FUNCTIONAL_VERIFIER_PATH).is_file()


def test_independent_verifier_accepts_correct_and_rejects_flawed_proposal(
    tmp_path: Path,
) -> None:
    tasks = load_functional_tasks()
    correct = next(task for task in tasks if task.proposal == "correct")
    flawed = next(task for task in tasks if task.proposal == "flawed")
    correct_fixture = materialize_functional_task(correct, tmp_path / "correct")
    flawed_fixture = materialize_functional_task(flawed, tmp_path / "flawed")

    apply_recorded_proposal(correct, correct_fixture.repository)
    apply_recorded_proposal(flawed, flawed_fixture.repository)

    assert verify_functional_task(correct_fixture, correct_fixture.repository).passed
    assert not verify_functional_task(
        flawed_fixture,
        flawed_fixture.repository,
    ).passed
