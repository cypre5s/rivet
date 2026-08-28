"""用真实 Git、ripgrep 和原创仓库验证渐进检索闭环。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from rivet.context.engine import ProgressiveContext
from rivet.contracts.context import ContextBudget, ContextLevel
from rivet.kernel.resources import ResourceScope
from scripts.context_fixtures import (
    load_context_cases,
    materialize_context_case,
)

BUDGET_8K = ContextBudget(
    total_tokens=8_000,
    required_tokens=1_000,
    working_tokens=6_000,
    history_tokens=1_000,
)
NOW = datetime(2026, 8, 28, tzinfo=UTC)


@pytest.mark.asyncio
async def test_twelve_fixtures_reach_gold_coverage(tmp_path: Path) -> None:
    covered = 0
    gold_count = 0
    for case in load_context_cases()[:12]:
        repository = materialize_context_case(case, tmp_path)
        scope = ResourceScope(f"context.{case.case_id.replace('-', '_')}")
        engine = ProgressiveContext(repository, scope=scope, clock=lambda: NOW)

        result = await engine.retrieve(
            case.task,
            budget=BUDGET_8K,
            include_syntax=case.include_syntax,
        )

        selected_paths = {item.repository_path for item in result.selection.items}
        covered += len(selected_paths.intersection(case.gold_files))
        gold_count += len(case.gold_files)
        assert all(item.reason for item in result.selection.items)
        assert all(
            item.retrieval_level <= ContextLevel.SYNTAX
            for item in result.selection.items
        )
        await scope.close()
        scope.assert_empty()

    assert covered / gold_count >= 0.70


@pytest.mark.asyncio
async def test_inventory_respects_ignores_and_never_reads_large_binary(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (repository / ".env.example").write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
    (repository / "ignored").mkdir()
    (repository / "ignored" / "secret.py").write_text(
        "TARGET_NEVER_READ = True\n", encoding="utf-8"
    )
    (repository / "safe.py").write_text("TARGET = True\n", encoding="utf-8")
    (repository / "large.bin").write_bytes(b"TARGET" + (b"\x00" * 600_000))
    scope = ResourceScope("context.ignore")
    engine = ProgressiveContext(repository, scope=scope, clock=lambda: NOW)

    result = await engine.retrieve("TARGET", budget=BUDGET_8K)

    inventory_paths = {entry.path for entry in result.snapshot.entries}
    selected_paths = {item.repository_path for item in result.selection.items}
    assert "ignored/secret.py" not in inventory_paths
    assert ".env.example" in inventory_paths
    assert "large.bin" in inventory_paths
    assert "large.bin" not in selected_paths
    assert selected_paths == {"safe.py"}
    await scope.close()


@pytest.mark.asyncio
async def test_same_query_and_snapshot_have_stable_order(tmp_path: Path) -> None:
    case = load_context_cases()[0]
    repository = materialize_context_case(case, tmp_path)
    first_scope = ResourceScope("context.stable.first")
    second_scope = ResourceScope("context.stable.second")

    first = await ProgressiveContext(
        repository, scope=first_scope, clock=lambda: NOW
    ).retrieve(case.task, budget=BUDGET_8K, include_syntax=True)
    second = await ProgressiveContext(
        repository, scope=second_scope, clock=lambda: NOW
    ).retrieve(case.task, budget=BUDGET_8K, include_syntax=True)

    assert first.snapshot.repository_sha256 == second.snapshot.repository_sha256
    assert [item.repository_path for item in first.selection.items] == [
        item.repository_path for item in second.selection.items
    ]
    assert first.selection == second.selection
    await first_scope.close()
    await second_scope.close()


@pytest.mark.asyncio
async def test_context_auto_escalates_syntax_only_when_lexical_is_ambiguous(
    tmp_path: Path,
) -> None:
    focused = tmp_path / "focused"
    focused.mkdir()
    (focused / "target.py").write_text(
        "def unique_handler():\n    return 'unique_token'\n",
        encoding="utf-8",
    )
    focused_scope = ResourceScope("context.auto.focused")
    focused_result = await ProgressiveContext(
        focused, scope=focused_scope, clock=lambda: NOW
    ).retrieve("unique_handler target.py", budget=BUDGET_8K, include_syntax=None)

    ambiguous = tmp_path / "ambiguous"
    ambiguous.mkdir()
    for index in range(6):
        (ambiguous / f"handler_{index}.py").write_text(
            f"def shared_handler_{index}():\n    return 'shared_token'\n",
            encoding="utf-8",
        )
    ambiguous_scope = ResourceScope("context.auto.ambiguous")
    ambiguous_result = await ProgressiveContext(
        ambiguous, scope=ambiguous_scope, clock=lambda: NOW
    ).retrieve("shared_handler", budget=BUDGET_8K, include_syntax=None)

    assert focused_result.syntax_activated is False
    assert all(
        item.retrieval_level < ContextLevel.SYNTAX
        for item in focused_result.selection.items
    )
    assert ambiguous_result.syntax_activated is True
    assert any(
        item.retrieval_level is ContextLevel.SYNTAX
        for item in ambiguous_result.selection.items
    )
    await focused_scope.close()
    await ambiguous_scope.close()


@pytest.mark.asyncio
async def test_context_content_hash_refreshes_after_change_and_delete(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "refresh"
    repository.mkdir()
    source = repository / "target.py"
    source.write_text("REFRESH_TOKEN = 'old'\n", encoding="utf-8")
    scope = ResourceScope("context.refresh")
    engine = ProgressiveContext(repository, scope=scope, clock=lambda: NOW)
    first = await engine.retrieve("REFRESH_TOKEN", budget=BUDGET_8K)
    first_item = next(
        item for item in first.selection.items if item.repository_path == "target.py"
    )

    source.write_text("REFRESH_TOKEN = 'new'\n", encoding="utf-8")
    second = await engine.retrieve("REFRESH_TOKEN", budget=BUDGET_8K)
    second_item = next(
        item for item in second.selection.items if item.repository_path == "target.py"
    )
    source.unlink()
    third = await engine.retrieve("REFRESH_TOKEN", budget=BUDGET_8K)

    assert first_item.content_sha256 != second_item.content_sha256
    assert first_item.freshness == first_item.content_sha256
    assert second_item.freshness == second_item.content_sha256
    assert "new" in second_item.content
    assert all(item.repository_path != "target.py" for item in third.selection.items)
    await scope.close()
