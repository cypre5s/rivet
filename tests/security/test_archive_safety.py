"""验证归档路径、链接、压缩比和递归预算失败关闭。"""

from __future__ import annotations

import io
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from rivet.contracts.readers import ReaderRequest, ReaderResult, ReaderStatus
from rivet.kernel.resources import ResourceScope
from rivet.readers.service import ReaderService


async def _read_archive(tmp_path: Path, name: str) -> ReaderResult:
    """读取一个恶意归档并确保资源域归零。"""
    scope = ResourceScope(f"reader.security.{name.replace('.', '_')}")
    service = ReaderService(tmp_path, scope=scope)
    result = await service.read(ReaderRequest(source_path=name, max_depth=2))
    scope.assert_empty()
    await scope.close()
    return result


@pytest.mark.asyncio
async def test_zip_slip_is_rejected_without_writing(tmp_path: Path) -> None:
    archive_path = tmp_path / "slip.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escaped.txt", "blocked")

    result = await _read_archive(tmp_path, "slip.zip")

    assert result.status is ReaderStatus.FAILED
    assert "reader.archive.path_forbidden" in result.warnings
    assert not (tmp_path.parent / "escaped.txt").exists()


@pytest.mark.asyncio
async def test_zip_symlink_is_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "symlink.zip"
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(link, "../../outside")

    result = await _read_archive(tmp_path, "symlink.zip")

    assert result.status is ReaderStatus.FAILED
    assert "reader.archive.symlink_forbidden" in result.warnings


@pytest.mark.asyncio
async def test_tar_symlink_is_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "symlink.tar"
    with tarfile.open(archive_path, "w") as archive:
        member = tarfile.TarInfo("link")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside"
        archive.addfile(member)

    result = await _read_archive(tmp_path, "symlink.tar")

    assert result.status is ReaderStatus.FAILED
    assert "reader.archive.symlink_forbidden" in result.warnings


@pytest.mark.asyncio
async def test_high_compression_ratio_is_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(
        archive_path, "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        archive.writestr("zeros.bin", b"0" * (2 * 1024 * 1024))

    result = await _read_archive(tmp_path, "bomb.zip")

    assert result.status is ReaderStatus.FAILED
    assert "reader.archive.compression_ratio_exceeded" in result.warnings


def _nested_zip(depth: int) -> bytes:
    """生成固定深度的内存嵌套 ZIP。"""
    payload = b"Rivet leaf"
    name = "leaf.txt"
    for index in range(depth):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(name, payload)
        payload = stream.getvalue()
        name = f"nested-{index}.zip"
    return payload


@pytest.mark.asyncio
async def test_recursive_archive_depth_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "recursive.zip").write_bytes(_nested_zip(4))

    result = await _read_archive(tmp_path, "recursive.zip")

    assert result.status is ReaderStatus.FAILED
    assert "reader.archive.depth_exceeded" in result.warnings
