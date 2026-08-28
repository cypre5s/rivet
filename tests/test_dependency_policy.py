"""验证 Agent 框架依赖黑名单。"""

from pathlib import Path

from scripts.verify_dependencies import find_forbidden_dependencies


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
