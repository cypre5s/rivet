"""在真实 Git Worktree 中验证所有观察能力共享事务视图。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from rivet.kernel.resources import ResourceScope
from rivet.tools.files import FileReader, TransactionFileWriter
from rivet.tools.git import GitService
from rivet.tools.process import ProcessRunner
from rivet.tools.search import SearchService
from rivet.tools.workspace import WorkspaceInspector
from tests.transaction_helpers import (
    acceptance_spec,
    initialize_repository,
    make_manager,
)


def _content_hash(repository: Path) -> str:
    """计算主工作区受跟踪 fixture 内容的稳定哈希。"""
    digest = hashlib.sha256()
    for relative_path in ("tracked.txt", "second.txt", "binary.bin"):
        digest.update(relative_path.encode("utf-8"))
        digest.update((repository / relative_path).read_bytes())
    return digest.hexdigest()


@pytest.mark.asyncio
async def test_transaction_tools_share_one_effective_workspace(
    tmp_path: Path,
) -> None:
    repository = initialize_repository(tmp_path)
    main_hash_before = _content_hash(repository)
    scope = ResourceScope("transaction.workspace.view")
    manager = make_manager(repository, tmp_path, scope)
    record = await manager.create(
        acceptance_spec(acceptance_id="acceptance_workspace_view"),
        confirmed=True,
        transaction_id="tx_workspace_view",
    )
    boundary = manager.transaction_boundary(record.transaction_id)
    writer = TransactionFileWriter(boundary)
    writer.write("tracked.txt", "transaction replacement token\nline two\n")
    writer.create("created.txt", "transaction created token\n")
    writer.delete("second.txt")

    reader = FileReader(boundary)
    inspector = WorkspaceInspector(boundary)
    read_runner = ProcessRunner(
        boundary,
        scope=scope,
        root_kind="repository_read_only",
    )
    search = SearchService(boundary, runner=read_runner)
    git_service = GitService(boundary, runner=read_runner)

    whole = reader.read_text("tracked.txt")
    ranged = reader.read_range("tracked.txt", start_line=2, end_line=2)
    listing = inspector.list(".", max_depth=1, max_entries=20)
    text_matches = await search.text("transaction replacement token")
    file_matches = await search.files("*.txt")
    status = await git_service.status()
    diff = await git_service.diff()
    process_result = await ProcessRunner(
        boundary,
        scope=scope,
        root_kind="transaction",
    ).run(
        (
            "/usr/bin/python3",
            "-c",
            "from pathlib import Path; print(Path('tracked.txt').read_text().strip())",
        ),
        timeout_seconds=10,
    )

    assert boundary.effective_root == boundary.transaction_root
    assert whole.content == "transaction replacement token\nline two\n"
    assert ranged.content == "line two\n"
    assert {entry.path for entry in listing.entries} >= {"tracked.txt", "created.txt"}
    assert "second.txt" not in {entry.path for entry in listing.entries}
    assert [match.path for match in text_matches.matches] == ["tracked.txt"]
    assert {match.path for match in file_matches.matches} == {
        "created.txt",
        "tracked.txt",
    }
    assert (
        "tracked.txt" in status and "created.txt" in status and "second.txt" in status
    )
    assert "+transaction replacement token" in diff
    assert process_result.stdout.decode().strip() == (
        "transaction replacement token\nline two"
    )

    assert _content_hash(repository) == main_hash_before
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == "base\n"
    assert (repository / "second.txt").read_text(encoding="utf-8") == "second base\n"
    assert not (repository / "created.txt").exists()

    await manager.abort(record.transaction_id)
    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_workspace_view_metadata_is_json_auditable(tmp_path: Path) -> None:
    repository = initialize_repository(tmp_path)
    scope = ResourceScope("transaction.workspace.metadata")
    manager = make_manager(repository, tmp_path, scope)
    record = await manager.create(
        acceptance_spec(acceptance_id="acceptance_workspace_metadata"),
        confirmed=True,
        transaction_id="tx_workspace_metadata",
    )
    boundary = manager.transaction_boundary(record.transaction_id)

    payload = boundary.workspace_view.as_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert payload == {
        "effective_root": str(boundary.transaction_root),
        "mode": "FIX",
        "repository_root": str(repository.resolve(strict=True)),
        "transaction_id": record.transaction_id,
        "transaction_root": str(boundary.transaction_root),
    }

    await manager.abort(record.transaction_id)
    scope.assert_empty()
    await scope.close()
