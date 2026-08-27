"""使用真实文件、ripgrep 和 Git 验证工作区工具。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rivet.kernel.resources import ResourceScope
from rivet.tools.errors import GitToolError
from rivet.tools.git import GitService
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.process import ProcessRunner
from rivet.tools.search import SearchService
from rivet.tools.workspace import WorkspaceInspector


def _run_git(repository: Path, *arguments: str) -> None:
    """以参数数组创建固定 Git fixture。"""
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
    )


@pytest.mark.asyncio
async def test_list_read_and_search_support_unusual_filenames(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    names = (
        "name with space.txt",
        "quote'file.txt",
        'double"quote.txt',
        "semi;colon.txt",
        "line\nbreak.txt",
    )
    for name in names:
        (repository / name).write_text(f"needle in {name}", encoding="utf-8")
    boundary = WorkspaceBoundary(repository)
    inspector = WorkspaceInspector(boundary)
    scope = ResourceScope("tools.search")
    runner = ProcessRunner(boundary, scope=scope, root_kind="repository_read_only")
    search = SearchService(boundary, runner=runner)

    listing = inspector.list(".", max_depth=1, max_entries=20)
    text_results = await search.text("needle", max_results=20)
    file_results = await search.files("*.txt", max_results=20)

    assert {entry.path for entry in listing.entries} == set(names)
    assert {match.path for match in text_results.matches} == set(names)
    assert {match.path for match in file_results.matches} == set(names)
    assert all(match.line_number == 1 for match in text_results.matches)
    assert all(match.column_number == 1 for match in text_results.matches)
    await scope.close()


@pytest.mark.asyncio
async def test_search_never_returns_sensitive_file_content(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "safe.txt").write_text("needle", encoding="utf-8")
    (repository / ".env").write_text("needle", encoding="utf-8")
    (repository / "credentials.json").write_text("needle", encoding="utf-8")
    boundary = WorkspaceBoundary(repository)
    scope = ResourceScope("tools.search.secrets")
    search = SearchService(
        boundary,
        runner=ProcessRunner(
            boundary,
            scope=scope,
            root_kind="repository_read_only",
        ),
    )

    text_results = await search.text("needle")
    file_results = await search.files()

    assert {match.path for match in text_results.matches} == {"safe.txt"}
    assert {match.path for match in file_results.matches} == {"safe.txt"}
    await scope.close()


@pytest.mark.asyncio
async def test_git_status_diff_show_and_detached_head(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _run_git(repository, "init", "-q")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    _run_git(repository, "add", "tracked.txt")
    _run_git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "initial",
    )
    _run_git(repository, "checkout", "--detach", "-q")
    (repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
    boundary = WorkspaceBoundary(repository)
    inspector = WorkspaceInspector(boundary)
    scope = ResourceScope("tools.git")
    git_service = GitService(
        boundary,
        runner=ProcessRunner(boundary, scope=scope, root_kind="repository_read_only"),
    )

    info = await inspector.info()
    status = await git_service.status()
    diff = await git_service.diff(path="tracked.txt")
    shown = await git_service.show("HEAD")

    assert info.is_git_repository
    assert info.detached_head
    assert "tracked.txt" in status
    assert "-base" in diff and "+changed" in diff
    assert "initial" in shown
    await scope.close()


@pytest.mark.asyncio
async def test_git_rejects_non_repository_and_bare_repository(tmp_path: Path) -> None:
    ordinary = tmp_path / "ordinary"
    bare = tmp_path / "bare.git"
    ordinary.mkdir()
    bare.mkdir()
    _run_git(bare, "init", "--bare", "-q")

    ordinary_scope = ResourceScope("tools.git.ordinary")
    bare_scope = ResourceScope("tools.git.bare")
    ordinary_boundary = WorkspaceBoundary(ordinary)
    bare_boundary = WorkspaceBoundary(bare)
    ordinary_git = GitService(
        ordinary_boundary,
        runner=ProcessRunner(
            ordinary_boundary,
            scope=ordinary_scope,
            root_kind="repository_read_only",
        ),
    )
    bare_git = GitService(
        bare_boundary,
        runner=ProcessRunner(
            bare_boundary,
            scope=bare_scope,
            root_kind="repository_read_only",
        ),
    )

    with pytest.raises(GitToolError, match="Git 仓库"):
        await ordinary_git.status()
    with pytest.raises(GitToolError, match="bare"):
        await bare_git.status()

    await ordinary_scope.close()
    await bare_scope.close()
