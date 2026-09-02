"""验证 Agent 框架依赖黑名单。"""

import tomllib
from pathlib import Path
from typing import cast

from scripts.verify_dependencies import find_forbidden_dependencies

REMOVED_RUNTIME_DEPENDENCIES = frozenset(
    {
        "defusedxml",
        "faster-whisper",
        "imageio-ffmpeg",
        "markitdown",
        "pillow",
        "py7zr",
        "pyyaml",
        "tree-sitter",
        "tree-sitter-javascript",
        "tree-sitter-python",
        "tree-sitter-typescript",
    }
)


def test_runtime_dependency_surface_is_exactly_httpx_and_pydantic() -> None:
    document = cast(
        dict[str, object],
        tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8")),
    )
    project = cast(dict[str, object], document["project"])
    dependencies = cast(list[str], project["dependencies"])
    names = {item.split(">", 1)[0].split("=", 1)[0] for item in dependencies}

    assert names == {"httpx", "pydantic"}
    assert "optional-dependencies" not in project
    assert REMOVED_RUNTIME_DEPENDENCIES.isdisjoint(names)


def test_reader_and_syntax_dependencies_are_absent_from_lock() -> None:
    lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    packages = cast(list[dict[str, object]], lock["package"])
    names = {cast(str, package["name"]) for package in packages}

    assert REMOVED_RUNTIME_DEPENDENCIES.isdisjoint(names)


def test_forbidden_direct_dependency_is_reported(tmp_path: Path) -> None:
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(
        '[project]\nname = "sample"\nversion = "0.1.0"\n'
        'dependencies = ["langchain>=1"]\n',
        encoding="utf-8",
    )

    assert find_forbidden_dependencies(pyproject_path, tmp_path / "uv.lock") == (
        "langchain",
    )


def test_forbidden_transitive_dependency_is_reported(tmp_path: Path) -> None:
    pyproject_path = tmp_path / "pyproject.toml"
    lock_path = tmp_path / "uv.lock"
    pyproject_path.write_text(
        '[project]\nname = "sample"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    lock_path.write_text(
        'version = 1\n[[package]]\nname = "llama-index-core"\nversion = "1.0"\n',
        encoding="utf-8",
    )

    assert find_forbidden_dependencies(pyproject_path, lock_path) == (
        "llama-index-core",
    )
