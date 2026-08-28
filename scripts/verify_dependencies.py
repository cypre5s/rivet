"""检查项目与锁文件中是否引入被禁止的 Agent 依赖。"""

from __future__ import annotations

import re
import sys
import tomllib
from argparse import ArgumentParser
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

FORBIDDEN_EXACT_NAMES = frozenset(
    {
        "anthropic-agent-sdk",
        "autogen",
        "autogen-agentchat",
        "claude-agent-sdk",
        "crewai",
        "langchain",
        "langgraph",
        "llama-index",
        "openai-agents",
        "openai-agents-sdk",
        "openhands-ai",
        "pyautogen",
    }
)
FORBIDDEN_NAME_PREFIXES = (
    "autogen-",
    "crewai-",
    "langchain-",
    "langgraph-",
    "llama-index-",
    "openai-agents-",
    "openhands-",
)
DEPENDENCY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def normalize_dependency_name(name: str) -> str:
    """按 Python 包名规则归一化依赖名。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def is_forbidden_dependency(name: str) -> bool:
    """判断依赖是否属于被禁止的 Agent 框架家族。"""
    normalized_name = normalize_dependency_name(name)
    return normalized_name in FORBIDDEN_EXACT_NAMES or normalized_name.startswith(
        FORBIDDEN_NAME_PREFIXES
    )


def _as_mapping(value: object) -> Mapping[str, object]:
    """将 TOML 节点收窄为安全的映射类型。"""
    if isinstance(value, dict):
        return cast(dict[str, object], value)
    return {}


def _dependency_names(requirements: object) -> set[str]:
    """从 PEP 508 字符串列表提取包名。"""
    if not isinstance(requirements, list):
        return set()

    names: set[str] = set()
    for requirement in cast(list[object], requirements):
        if not isinstance(requirement, str):
            continue
        match = DEPENDENCY_NAME_PATTERN.match(requirement.strip())
        if match is not None:
            names.add(normalize_dependency_name(match.group(0)))
    return names


def collect_declared_dependencies(pyproject_path: Path) -> set[str]:
    """收集项目、可选组和开发组中的直接依赖。"""
    payload = cast(
        dict[str, object], tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    )
    project = _as_mapping(payload.get("project"))
    names = _dependency_names(project.get("dependencies"))

    optional_groups = _as_mapping(project.get("optional-dependencies"))
    for requirements in optional_groups.values():
        names.update(_dependency_names(requirements))

    dependency_groups = _as_mapping(payload.get("dependency-groups"))
    for requirements in dependency_groups.values():
        names.update(_dependency_names(requirements))

    tool = _as_mapping(payload.get("tool"))
    uv_settings = _as_mapping(tool.get("uv"))
    names.update(_dependency_names(uv_settings.get("dev-dependencies")))
    return names


def collect_locked_dependencies(lock_path: Path) -> set[str]:
    """收集 uv 锁文件中的全部传递依赖。"""
    if not lock_path.exists():
        return set()

    payload = cast(
        dict[str, object], tomllib.loads(lock_path.read_text(encoding="utf-8"))
    )
    packages = payload.get("package")
    if not isinstance(packages, list):
        return set()

    names: set[str] = set()
    for package in cast(list[object], packages):
        name = _as_mapping(package).get("name")
        if isinstance(name, str):
            names.add(normalize_dependency_name(name))
    return names


def find_forbidden_dependencies(
    pyproject_path: Path, lock_path: Path
) -> tuple[str, ...]:
    """返回直接或传递引入的被禁止依赖。"""
    dependency_names = collect_declared_dependencies(pyproject_path)
    dependency_names.update(collect_locked_dependencies(lock_path))
    return tuple(
        sorted(name for name in dependency_names if is_forbidden_dependency(name))
    )


def _build_parser() -> ArgumentParser:
    """构造可在 CI 中稳定调用的参数解析器。"""
    parser = ArgumentParser(description="检查被禁止的 Agent 框架依赖")
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """执行依赖黑名单检查并返回适合 CI 的退出码。"""
    arguments = _build_parser().parse_args(argv)
    pyproject_path = cast(Path, arguments.pyproject)
    lock_path = cast(Path, arguments.lock)

    if not pyproject_path.is_file():
        print(f"依赖检查失败：找不到 {pyproject_path}", file=sys.stderr)
        return 2

    forbidden = find_forbidden_dependencies(pyproject_path, lock_path)
    if forbidden:
        print("依赖检查失败：发现被禁止的 Agent 依赖", file=sys.stderr)
        for dependency_name in forbidden:
            print(f"- {dependency_name}", file=sys.stderr)
        return 1

    print("依赖检查通过：未发现被禁止的 Agent 依赖")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
