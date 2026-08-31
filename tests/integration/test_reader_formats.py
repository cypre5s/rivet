"""使用自生成真实 fixture 验证 Reader 支持矩阵与 CLI。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from rivet.contracts.readers import ReaderRequest, ReaderStatus
from rivet.kernel.resources import ResourceScope
from rivet.readers.service import ReaderService

FIXTURE_ROOT = Path("tests/fixtures/files")

FORMAT_CASES = (
    ("sample.txt", "text", "Rivet text"),
    ("sample.json", "json", "$.project"),
    ("sample.yaml", "yaml", "$.project"),
    ("sample.toml", "toml", "$.project"),
    ("sample.xml", "xml", "/root/project"),
    ("sample.csv", "csv", "row[2].value"),
    ("sample.tsv", "tsv", "row[2].value"),
    ("sample.ipynb", "notebook", "Rivet notebook"),
    ("sample.pdf", "pdf", "Rivet PDF"),
    ("sample.docx", "docx", "Rivet DOCX"),
    ("sample.pptx", "pptx", "Rivet PPTX"),
    ("sample.xlsx", "xlsx", "Rivet XLSX"),
    ("sample.xls", "xls", "Rivet XLS"),
    ("sample.html", "html", "Rivet HTML"),
    ("sample.epub", "epub", "Rivet EPUB"),
    ("sample.png", "png", "width="),
    ("sample.jpg", "jpeg", "width="),
    ("sample.webp", "webp", "width="),
    ("sample.gif", "gif", "width="),
    ("sample.bmp", "bmp", "width="),
    ("sample.tiff", "tiff", "width="),
    ("sample.wav", "wav", "duration_seconds"),
    ("sample.mp3", "mp3", "duration"),
    ("sample.m4a", "m4a", "duration"),
    ("sample.flac", "flac", "duration"),
    ("sample.ogg", "ogg", "duration"),
    ("sample.mp4", "mp4", "duration"),
    ("sample.mov", "mov", "duration"),
    ("sample.webm", "webm", "duration"),
    ("sample.mkv", "mkv", "duration"),
    ("sample.zip", "zip", "Rivet archive"),
    ("sample.tar", "tar", "Rivet archive"),
    ("sample.tar.gz", "tar.gz", "Rivet archive"),
    ("sample.7z", "7z", "Rivet archive"),
    ("sample.eml", "eml", "Rivet EML"),
    ("sample.msg", "msg", "Rivet MSG"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "detected_format", "expected"), FORMAT_CASES)
async def test_reader_extracts_supported_format(
    name: str,
    detected_format: str,
    expected: str,
) -> None:
    scope = ResourceScope(f"reader.integration.{detected_format.replace('.', '_')}")
    service = ReaderService(FIXTURE_ROOT, scope=scope)

    result = await service.read(ReaderRequest(source_path=name, timeout_seconds=15))

    assert result.status in {
        ReaderStatus.SUCCESS,
        ReaderStatus.DEGRADED,
        ReaderStatus.TRUNCATED,
    }
    assert result.detected_format == detected_format
    assert expected in result.content
    assert result.source_path == name
    assert result.untrusted is True
    await scope.close()
    scope.assert_empty()


@pytest.mark.asyncio
async def test_unknown_binary_returns_unsupported_metadata() -> None:
    scope = ResourceScope("reader.integration.binary")
    service = ReaderService(FIXTURE_ROOT, scope=scope)

    result = await service.read(ReaderRequest(source_path="sample.bin"))

    assert result.status is ReaderStatus.UNSUPPORTED
    assert result.detected_format == "binary"
    assert "RIVET-BINARY-STRING" in result.content
    size_bytes = result.metadata["size_bytes"]
    assert isinstance(size_bytes, int)
    assert size_bytes > 0
    await scope.close()


@pytest.mark.asyncio
async def test_video_frame_budget_extracts_real_bounded_frame_evidence() -> None:
    """--frames 必须真实解码视频帧，而不只是返回容器元数据。"""
    scope = ResourceScope("reader.integration.video_frames")
    service = ReaderService(FIXTURE_ROOT, scope=scope)

    result = await service.read(
        ReaderRequest(
            source_path="sample.mp4",
            max_video_frames=3,
            timeout_seconds=15,
        )
    )

    frames = result.metadata["extracted_frames"]
    assert isinstance(frames, list)
    assert 1 <= len(frames) <= 3
    for frame in frames:
        assert isinstance(frame, dict)
        size_bytes = frame.get("size_bytes")
        digest = frame.get("sha256")
        assert isinstance(size_bytes, int)
        assert size_bytes > 0
        assert isinstance(digest, str)
        assert digest.startswith("sha256:")
    assert f"video_frames={len(frames)}" in result.content
    await scope.close()
    scope.assert_empty()


def test_rivet_read_json_returns_contract() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rivet",
            "read",
            "sample.pdf",
            "--repository",
            str(FIXTURE_ROOT),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert payload["detected_format"] == "pdf"
    assert payload["untrusted"] is True
    assert "Rivet PDF" in payload["content"]
