"""加载并物化 Phase 14 的固定功能评测仓库。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

EXPECTED_FAMILY_COUNTS = {
    "config": 4,
    "cross_file": 3,
    "documentation": 3,
    "javascript": 2,
    "python": 8,
    "typescript": 4,
}
FIXED_GIT_ENVIRONMENT = {
    "GIT_AUTHOR_DATE": "2026-08-28T08:00:00+08:00",
    "GIT_COMMITTER_DATE": "2026-08-28T08:00:00+08:00",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}
FUNCTIONAL_VERIFIER_PATH = ".rivet-benchmark/verifier.py"


@dataclass(frozen=True, slots=True)
class FunctionalTask:
    """描述一个固定任务、允许范围和离线提案标签。"""

    task_id: str
    family: str
    category: str
    task: str
    gold_files: tuple[str, ...]
    supporting_files: tuple[str, ...]
    proposal: Literal["correct", "flawed"]

    @property
    def marker(self) -> str:
        """返回可安全进入源码标识符的任务标记。"""
        return self.task_id.replace("-", "_")

    @property
    def broken_value(self) -> str:
        """返回基线中的唯一错误值。"""
        return f"broken:{self.task_id}"

    @property
    def fixed_value(self) -> str:
        """返回独立 verifier 冻结的目标值。"""
        return f"fixed:{self.task_id}"


@dataclass(frozen=True, slots=True)
class MaterializedFixture:
    """保存一次物化后的基线事实与 verifier 哈希。"""

    task: FunctionalTask
    repository: Path
    base_commit: str
    verifier_sha256: str
    original_hashes: dict[str, str]


@dataclass(frozen=True, slots=True)
class FunctionalVerdict:
    """保存独立 verifier 的功能、范围和变更事实。"""

    passed: bool
    behavior_passed: bool
    scope_passed: bool
    preservation_passed: bool
    changed_files: tuple[str, ...]


def load_functional_tasks() -> tuple[FunctionalTask, ...]:
    """严格加载 24 个原创任务并验证类别配额。"""
    path = Path(__file__).parents[1] / "benchmarks" / "functional" / "tasks.json"
    raw_document = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw_document, dict):
        raise ValueError("功能评测清单根节点无效")
    document = cast(dict[str, object], raw_document)
    if (
        set(document) != {"schema_version", "tasks"}
        or document.get("schema_version") != 1
    ):
        raise ValueError("功能评测清单协议无效")
    raw_tasks = document.get("tasks")
    if not isinstance(raw_tasks, list):
        raise ValueError("功能评测任务列表无效")
    tasks: list[FunctionalTask] = []
    for raw_task in cast(list[object], raw_tasks):
        if not isinstance(raw_task, dict):
            raise ValueError("功能评测任务无效")
        item = cast(dict[str, object], raw_task)
        if set(item) != {
            "category",
            "family",
            "gold_files",
            "proposal",
            "supporting_files",
            "task",
            "task_id",
        }:
            raise ValueError("功能评测任务字段无效")
        proposal = item["proposal"]
        if proposal not in {"correct", "flawed"}:
            raise ValueError("功能评测提案标签无效")
        tasks.append(
            FunctionalTask(
                task_id=_required_text(item["task_id"]),
                family=_required_text(item["family"]),
                category=_required_text(item["category"]),
                task=_required_text(item["task"]),
                gold_files=_path_tuple(item["gold_files"]),
                supporting_files=_path_tuple(item["supporting_files"]),
                proposal=cast(Literal["correct", "flawed"], proposal),
            )
        )
    identifiers = tuple(task.task_id for task in tasks)
    family_counts = {
        family: sum(task.family == family for task in tasks)
        for family in EXPECTED_FAMILY_COUNTS
    }
    if (
        len(tasks) != 24
        or len(set(identifiers)) != len(identifiers)
        or family_counts != EXPECTED_FAMILY_COUNTS
    ):
        raise ValueError("功能评测任务数量、ID 或类别配额无效")
    return tuple(tasks)


def materialize_functional_task(
    task: FunctionalTask,
    root: Path,
) -> MaterializedFixture:
    """创建内容、时间和 Git 基线都固定的临时仓库。"""
    repository = root / task.task_id
    repository.mkdir(parents=True)
    original_hashes: dict[str, str] = {}
    for relative_path in (*task.gold_files, *task.supporting_files):
        content = (
            _gold_content(task, relative_path)
            if relative_path in task.gold_files
            else _supporting_content(task, relative_path)
        )
        _write_fixture_file(repository, relative_path, content)
        original_hashes[relative_path] = _content_sha256(content)
    for index in range(12):
        relative_path = f"noise/package_{index:02d}/unrelated_{index:02d}.py"
        content = (
            f'NOISE_ID = "noise-{index:02d}"\nNOISE_VALUE = "unrelated:{index:02d}"\n'
        )
        _write_fixture_file(repository, relative_path, content)
        original_hashes[relative_path] = _content_sha256(content)
    verifier_content = _verifier_content(task)
    _write_fixture_file(repository, FUNCTIONAL_VERIFIER_PATH, verifier_content)
    original_hashes[FUNCTIONAL_VERIFIER_PATH] = _content_sha256(verifier_content)
    _write_fixture_file(repository, ".gitignore", ".rivet/*\n")
    original_hashes[".gitignore"] = _content_sha256(".rivet/*\n")
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "add", "--", *sorted(original_hashes))
    _git(
        repository,
        "-c",
        "user.name=Rivet Benchmark",
        "-c",
        "user.email=benchmark@example.invalid",
        "commit",
        "-qm",
        "固定基线",
    )
    base_commit = _git_output(repository, "rev-parse", "HEAD").strip()
    verifier_payload = {
        "allowed_paths": list(task.gold_files),
        "base_commit": base_commit,
        "expected": {path: task.fixed_value for path in task.gold_files},
        "preserved_hashes": {
            path: digest
            for path, digest in sorted(original_hashes.items())
            if path not in task.gold_files
        },
        "schema_version": 1,
        "task_id": task.task_id,
    }
    verifier_sha256 = hashlib.sha256(
        json.dumps(
            verifier_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return MaterializedFixture(
        task=task,
        repository=repository,
        base_commit=base_commit,
        verifier_sha256=f"sha256:{verifier_sha256}",
        original_hashes=original_hashes,
    )


def apply_recorded_proposal(task: FunctionalTask, target_root: Path) -> None:
    """应用固定离线提案；错误提案用于验证失败关闭而非伪装成功。"""
    selected_paths = (
        task.gold_files if task.proposal == "correct" else task.gold_files[:1]
    )
    replacement = (
        task.fixed_value
        if task.proposal == "correct" or len(task.gold_files) > 1
        else f"almost:{task.task_id}"
    )
    for relative_path in selected_paths:
        path = target_root / relative_path
        content = path.read_text(encoding="utf-8")
        if content.count(task.broken_value) != 1:
            raise ValueError("评测提案的基线标记不唯一")
        path.write_text(
            content.replace(task.broken_value, replacement),
            encoding="utf-8",
        )


def verify_functional_task(
    fixture: MaterializedFixture,
    target_root: Path,
) -> FunctionalVerdict:
    """独立于提案执行器检查目标行为、范围和保留文件。"""
    changed_files = tuple(
        path
        for path in _git_output(
            target_root,
            "diff",
            "--name-only",
            "--diff-filter=ACMRTUXB",
            fixture.base_commit,
            "--",
        ).splitlines()
        if path
    )
    behavior_passed = all(
        (target_root / path).read_text(encoding="utf-8").count(fixture.task.fixed_value)
        == 1
        and fixture.task.broken_value
        not in (target_root / path).read_text(encoding="utf-8")
        for path in fixture.task.gold_files
    )
    scope_passed = bool(changed_files) and set(changed_files).issubset(
        fixture.task.gold_files
    )
    preservation_passed = all(
        _content_sha256((target_root / path).read_text(encoding="utf-8")) == digest
        for path, digest in fixture.original_hashes.items()
        if path not in fixture.task.gold_files
    )
    return FunctionalVerdict(
        passed=behavior_passed and scope_passed and preservation_passed,
        behavior_passed=behavior_passed,
        scope_passed=scope_passed,
        preservation_passed=preservation_passed,
        changed_files=tuple(sorted(changed_files)),
    )


def _gold_content(task: FunctionalTask, relative_path: str) -> str:
    """按扩展名生成包含唯一目标符号的最小源码或配置。"""
    suffix = Path(relative_path).suffix
    if suffix == ".py":
        return (
            f'TASK_ID = "{task.task_id}"\n\n\n'
            f"def {task.marker}() -> str:\n"
            f'    return "{task.broken_value}"\n'
        )
    if suffix == ".ts":
        return (
            f'export const TASK_ID = "{task.task_id}"\n\n'
            f"export function {task.marker}(): string {{\n"
            f'  return "{task.broken_value}"\n'
            "}\n"
        )
    if suffix == ".js":
        return (
            f'export const TASK_ID = "{task.task_id}"\n\n'
            f"export function {task.marker}() {{\n"
            f'  return "{task.broken_value}"\n'
            "}\n"
        )
    if suffix == ".json":
        return (
            json.dumps(
                {"task_id": task.task_id, "value": task.broken_value},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    return f'task_id = "{task.task_id}"\nvalue = "{task.broken_value}"\n'


def _supporting_content(task: FunctionalTask, relative_path: str) -> str:
    """生成不可修改的测试或需求证据。"""
    suffix = Path(relative_path).suffix
    if suffix == ".md":
        return (
            f"# {task.task_id} 冻结需求\n\n"
            f"目标值必须为 `{task.fixed_value}`，不得修改本文件。\n"
        )
    if suffix == ".py":
        return f'TASK_ID = "{task.task_id}"\nEXPECTED_VALUE = "{task.fixed_value}"\n'
    if suffix in {".ts", ".js"}:
        return (
            f'export const taskId = "{task.task_id}"\n'
            f'export const expectedValue = "{task.fixed_value}"\n'
        )
    return f"{task.task_id}={task.fixed_value}\n"


def _verifier_content(task: FunctionalTask) -> str:
    """生成独立于补丁提案的固定行为检查器。"""
    return (
        "from pathlib import Path\n\n"
        f"EXPECTED_VALUE = {task.fixed_value!r}\n"
        f"BROKEN_VALUE = {task.broken_value!r}\n"
        f"TARGET_PATHS = {task.gold_files!r}\n\n"
        "valid = all(\n"
        "    (content := Path(path).read_text(encoding='utf-8')).count("
        "EXPECTED_VALUE) == 1\n"
        "    and BROKEN_VALUE not in content\n"
        "    for path in TARGET_PATHS\n"
        ")\n"
        "raise SystemExit(0 if valid else 1)\n"
    )


def _required_text(value: object) -> str:
    """验证清单中的必填文本。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("功能评测文本字段无效")
    return value


def _path_tuple(value: object) -> tuple[str, ...]:
    """验证清单中的仓库相对路径列表。"""
    if not isinstance(value, list) or not value:
        raise ValueError("功能评测路径列表无效")
    paths = tuple(_required_text(item) for item in cast(list[object], value))
    if len(paths) != len(set(paths)) or any(
        Path(path).is_absolute() or ".." in Path(path).parts for path in paths
    ):
        raise ValueError("功能评测路径越界或重复")
    return paths


def _write_fixture_file(repository: Path, relative_path: str, content: str) -> None:
    """只在新建临时仓库内写入固定 fixture。"""
    target = repository / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _content_sha256(content: str) -> str:
    """返回固定文本哈希。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _git(repository: Path, *arguments: str) -> None:
    """执行固定、无交互且不回显内容的 Git 命令。"""
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=False,
        capture_output=True,
        env=_git_environment(),
        timeout=20,
    )
    if completed.returncode != 0:
        raise RuntimeError("功能评测 Git 命令失败")


def _git_output(repository: Path, *arguments: str) -> str:
    """读取固定 Git 命令的 UTF-8 输出。"""
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=False,
        capture_output=True,
        env=_git_environment(),
        timeout=20,
    )
    if completed.returncode != 0:
        raise RuntimeError("功能评测 Git 读取失败")
    return completed.stdout.decode("utf-8", errors="strict")


def _git_environment() -> dict[str, str]:
    """构造不携带凭据且固定提交时间的 Git 环境。"""
    return {
        **FIXED_GIT_ENVIRONMENT,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
