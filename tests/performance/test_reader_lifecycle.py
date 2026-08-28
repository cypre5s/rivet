"""验证 Reader 重依赖、sidecar 和临时资源的生命周期。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rivet.contracts.readers import ReaderRequest
from rivet.kernel.resources import ResourceScope
from rivet.readers.service import ReaderService

FIXTURE_ROOT = Path("tests/fixtures/files")
HEAVY_PREFIXES = ("markitdown", "PIL", "faster_whisper", "py7zr")


@pytest.mark.asyncio
async def test_plain_text_keeps_heavy_modules_and_processes_at_zero() -> None:
    before = set(sys.modules)
    scope = ResourceScope("reader.lifecycle.text")
    service = ReaderService(FIXTURE_ROOT, scope=scope)

    await service.read(ReaderRequest(source_path="sample.txt"))

    imported = set(sys.modules) - before
    assert not any(name.startswith(HEAVY_PREFIXES) for name in imported)
    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_document_sidecar_releases_process_and_parent_imports() -> None:
    before = set(sys.modules)
    scope = ResourceScope("reader.lifecycle.document")
    service = ReaderService(FIXTURE_ROOT, scope=scope)

    result = await service.read(
        ReaderRequest(source_path="sample.docx", timeout_seconds=15)
    )

    imported = set(sys.modules) - before
    assert "Rivet DOCX" in result.content
    assert not any(name.startswith(("markitdown", "PIL")) for name in imported)
    scope.assert_empty()
    await scope.close()
