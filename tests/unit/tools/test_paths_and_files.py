"""验证路径解析、文本读取和事务内原子写入。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rivet.tools.errors import PathBoundaryError, WorkspaceToolError
from rivet.tools.files import FileReader, TransactionFileWriter
from rivet.tools.paths import WorkspaceBoundary


def test_text_reader_detects_utf8_bom_and_returns_line_range(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "sample.txt").write_bytes(b"\xef\xbb\xbfalpha\nbeta\ngamma\n")
    reader = FileReader(WorkspaceBoundary(repository))

    whole = reader.read_text("sample.txt")
    selected = reader.read_range("sample.txt", start_line=2, end_line=3)

    assert whole.encoding == "utf-8-sig"
    assert whole.content == "alpha\nbeta\ngamma\n"
    assert selected.content == "beta\ngamma\n"
    assert (selected.start_line, selected.end_line) == (2, 3)


def test_reader_rejects_binary_and_oversized_files(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "binary.bin").write_bytes(b"\x00\x01\x02text")
    (repository / "large.txt").write_text("x" * 20, encoding="utf-8")
    reader = FileReader(WorkspaceBoundary(repository), max_file_bytes=10)

    with pytest.raises(WorkspaceToolError, match="二进制"):
        reader.read_text("binary.bin")
    with pytest.raises(WorkspaceToolError, match="大小上限"):
        reader.read_text("large.txt")


def test_transaction_writer_is_atomic_and_never_changes_main_root(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    (repository / "tracked.txt").write_text("main", encoding="utf-8")
    (transaction / "tracked.txt").write_text("base", encoding="utf-8")
    writer = TransactionFileWriter(WorkspaceBoundary(repository, transaction))

    writer.write("tracked.txt", "updated")
    writer.create("new.txt", "created")
    replacement_count = writer.replace("tracked.txt", "up", "UP")
    writer.delete("new.txt")

    assert replacement_count == 1
    assert (transaction / "tracked.txt").read_text(encoding="utf-8") == "UPdated"
    assert (repository / "tracked.txt").read_text(encoding="utf-8") == "main"
    assert not (transaction / "new.txt").exists()
    assert not tuple(transaction.rglob("*.tmp"))


def test_transaction_writer_preserves_existing_mode(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    target = transaction / "script.sh"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o755)
    writer = TransactionFileWriter(WorkspaceBoundary(repository, transaction))

    writer.write("script.sh", "new")

    assert target.stat().st_mode & 0o777 == 0o755


def test_transaction_root_cannot_be_main_or_nested_inside_main(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    nested = repository / "worktree"
    nested.mkdir()

    with pytest.raises(PathBoundaryError, match="独立"):
        WorkspaceBoundary(repository, repository)
    with pytest.raises(PathBoundaryError, match="独立"):
        WorkspaceBoundary(repository, nested)


def test_atomic_write_fsyncs_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    writer = TransactionFileWriter(WorkspaceBoundary(repository, transaction))
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def record_fsync(file_descriptor: int) -> None:
        fsync_calls.append(file_descriptor)
        real_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)

    writer.create("durable.txt", "content")

    assert len(fsync_calls) >= 2
