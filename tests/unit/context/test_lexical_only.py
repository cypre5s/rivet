from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rivet.context.lexical import LexicalContext
from rivet.kernel.resources import ResourceScope


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "rivet@example.invalid")
    _git(root, "config", "user.name", "Rivet Test")
    (root / "src").mkdir()
    (root / "src" / "ports.py").write_text(
        "def parse_port(value: str) -> int:\n    return int(value)\n",
        encoding="utf-8",
    )
    (root / "src" / "unrelated.py").write_text(
        "def noop():\n    return None\n",
        encoding="utf-8",
    )
    (root / ".env").write_text("SECRET=never-read\n", encoding="utf-8")
    _git(root, "add", "src")
    _git(root, "commit", "-qm", "fixture")
    return root


@pytest.mark.asyncio
async def test_lexical_search_is_bounded_ranked_and_excludes_secrets(
    repository: Path,
) -> None:
    scope = ResourceScope("context.lexical.test")
    try:
        result = await LexicalContext(repository, scope=scope).search("parse_port")
    finally:
        await scope.close()
    scope.assert_empty()

    assert result.status == "MATCH"
    assert result.matches[0].path == "src/ports.py"
    assert "parse_port" in result.matches[0].content
    assert all(match.path != ".env" for match in result.matches)


@pytest.mark.asyncio
async def test_lexical_search_returns_explicit_no_match(repository: Path) -> None:
    scope = ResourceScope("context.lexical.no-match")
    try:
        result = await LexicalContext(repository, scope=scope).search(
            "symbol_that_does_not_exist_anywhere"
        )
    finally:
        await scope.close()
    scope.assert_empty()

    assert result.status == "NO_MATCH"
    assert result.matches == ()
