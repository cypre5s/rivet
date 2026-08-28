"""加载并在临时目录中物化原创 Context 基准样本。"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import cast


@dataclass(frozen=True, slots=True)
class SemanticFixture:
    """描述跨文件语义操作与零基查询位置。"""

    operation: str
    document_path: str
    line: int
    character: int


@dataclass(frozen=True, slots=True)
class ContextFixtureCase:
    """描述单个任务、Gold 文件和小型仓库内容。"""

    case_id: str
    task: str
    include_syntax: bool
    gold_files: tuple[str, ...]
    files: dict[str, str]
    recent_paths: tuple[str, ...] = ()
    relevant_files: tuple[str, ...] = ()
    semantic: SemanticFixture | None = None


def load_context_cases() -> tuple[ContextFixtureCase, ...]:
    """从固定 JSON 加载二十个可重复检索样本。"""
    fixture_path = (
        Path(__file__).parents[1] / "tests" / "fixtures" / "context" / "cases.json"
    )
    payload = cast(list[dict[str, object]], json.loads(fixture_path.read_text()))
    cases: list[ContextFixtureCase] = []
    for item in payload:
        raw_semantic = item.get("semantic")
        semantic: SemanticFixture | None = None
        if isinstance(raw_semantic, dict):
            semantic_mapping = cast(dict[str, object], raw_semantic)
            semantic = SemanticFixture(
                operation=cast(str, semantic_mapping["operation"]),
                document_path=cast(str, semantic_mapping["document_path"]),
                line=cast(int, semantic_mapping["line"]),
                character=cast(int, semantic_mapping["character"]),
            )
        gold_files = tuple(cast(list[str], item["gold_files"]))
        cases.append(
            ContextFixtureCase(
                case_id=cast(str, item["case_id"]),
                task=cast(str, item["task"]),
                include_syntax=cast(bool, item["include_syntax"]),
                gold_files=gold_files,
                files=cast(dict[str, str], item["files"]),
                recent_paths=tuple(cast(list[str], item.get("recent_paths", []))),
                relevant_files=tuple(
                    cast(list[str], item.get("relevant_files", list(gold_files)))
                ),
                semantic=semantic,
            )
        )
    return tuple(cases)


def materialize_context_case(case: ContextFixtureCase, root: Path) -> Path:
    """创建带两次确定性提交的小型 Git 仓库。"""
    repository = root / case.case_id
    repository.mkdir(parents=True)
    for relative_path, content in case.files.items():
        target = repository / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "add", "--", *sorted(case.files)], cwd=repository, check=True
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Context Fixture",
            "-c",
            "user.email=context@example.invalid",
            "commit",
            "-qm",
            "基线",
        ],
        cwd=repository,
        check=True,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_DATE": "2026-08-27T00:00:00+08:00",
            "GIT_COMMITTER_DATE": "2026-08-27T00:00:00+08:00",
        },
    )
    for relative_path in case.recent_paths:
        target = repository / relative_path
        target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    if case.recent_paths:
        subprocess.run(
            ["git", "add", "--", *case.recent_paths], cwd=repository, check=True
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Context Fixture",
                "-c",
                "user.email=context@example.invalid",
                "commit",
                "-qm",
                "最近修改",
            ],
            cwd=repository,
            check=True,
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_AUTHOR_DATE": "2026-08-28T00:00:00+08:00",
                "GIT_COMMITTER_DATE": "2026-08-28T00:00:00+08:00",
            },
        )
    return repository
