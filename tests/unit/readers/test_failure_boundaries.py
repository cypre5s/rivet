"""验证损坏输入和冻结预算均以结构化结果失败关闭。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from rivet.contracts.readers import ReaderRequest, ReaderStatus
from rivet.kernel.resources import ResourceScope
from rivet.readers.service import ReaderService


async def _read(
    tmp_path: Path,
    *,
    name: str,
    content: bytes,
    max_bytes: int = 50 * 1024 * 1024,
) -> tuple[ReaderStatus, str, tuple[str, ...]]:
    """读取一个临时坏样本并在返回前验证资源归零。"""
    (tmp_path / name).write_bytes(content)
    scope = ResourceScope(f"reader.failure.{name.replace('.', '_')}")
    service = ReaderService(tmp_path, scope=scope)
    result = await service.read(
        ReaderRequest(source_path=name, max_bytes=max_bytes, timeout_seconds=5)
    )
    scope.assert_empty()
    await scope.close()
    return result.status, result.content, result.warnings


def test_reader_request_rejects_archive_depth_above_frozen_limit() -> None:
    with pytest.raises(ValidationError):
        ReaderRequest(source_path="sample.zip", max_depth=4)


@pytest.mark.asyncio
async def test_file_size_limit_fails_without_reading_content(tmp_path: Path) -> None:
    status, content, warnings = await _read(
        tmp_path,
        name="large.txt",
        content=b"Rivet oversized fixture",
        max_bytes=4,
    )

    assert status is ReaderStatus.FAILED
    assert content == ""
    assert "reader.file.size_exceeded" in warnings


@pytest.mark.asyncio
async def test_corrupt_png_fails_without_inventing_content(tmp_path: Path) -> None:
    status, content, warnings = await _read(
        tmp_path,
        name="broken.png",
        content=b"\x89PNG\r\n\x1a\ncorrupt",
    )

    assert status is ReaderStatus.FAILED
    assert content == ""
    assert "reader.worker.parse_failed" in warnings


@pytest.mark.asyncio
async def test_corrupt_zip_fails_without_inventing_content(tmp_path: Path) -> None:
    status, content, warnings = await _read(
        tmp_path,
        name="broken.zip",
        content=b"PK\x03\x04corrupt",
    )

    assert status is ReaderStatus.FAILED
    assert content == ""
    assert "reader.archive.invalid_zip" in warnings
