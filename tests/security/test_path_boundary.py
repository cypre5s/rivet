"""验证路径穿越、symlink 逃逸和受保护文件均失败关闭。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.tools.errors import PathBoundaryError
from rivet.tools.files import FileReader, TransactionFileWriter
from rivet.tools.paths import WorkspaceBoundary


def test_absolute_and_parent_traversal_are_rejected(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    boundary = WorkspaceBoundary(repository)

    with pytest.raises(PathBoundaryError):
        boundary.resolve_repository("../../outside")
    with pytest.raises(PathBoundaryError):
        boundary.resolve_repository(str(tmp_path / "outside"))


def test_symlink_to_outside_is_rejected_for_read_and_write(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    outside = tmp_path / "outside"
    repository.mkdir()
    transaction.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    (repository / "read-link").symlink_to(outside / "secret.txt")
    (transaction / "write-link").symlink_to(outside / "secret.txt")
    boundary = WorkspaceBoundary(repository, transaction)

    with pytest.raises(PathBoundaryError):
        FileReader(boundary).read_text("read-link")
    with pytest.raises(PathBoundaryError):
        TransactionFileWriter(boundary).write("write-link", "overwrite")
    assert (outside / "secret.txt").read_text(encoding="utf-8") == "outside"


@pytest.mark.parametrize(
    "protected_path",
    (
        ".git/config",
        ".rivet/evidence/result.json",
        ".env",
        ".env.local",
        "credentials.json",
        "id_rsa",
    ),
)
def test_protected_paths_cannot_be_written(tmp_path: Path, protected_path: str) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    writer = TransactionFileWriter(WorkspaceBoundary(repository, transaction))

    with pytest.raises(PathBoundaryError):
        writer.create(protected_path, "forbidden")


def test_sensitive_file_cannot_be_read(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".env").write_text("DEEPSEEK_API_KEY=hidden", encoding="utf-8")

    with pytest.raises(PathBoundaryError):
        FileReader(WorkspaceBoundary(repository)).read_text(".env")


def test_write_without_transaction_authorization_is_rejected(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    writer = TransactionFileWriter(WorkspaceBoundary(repository))

    with pytest.raises(PathBoundaryError, match="事务"):
        writer.create("main-pollution.txt", "forbidden")

    assert not (repository / "main-pollution.txt").exists()
