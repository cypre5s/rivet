"""验证 Reader capability 唯一映射和重解析器延迟加载。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rivet.contracts.readers import ReaderRequest
from rivet.kernel.resources import ResourceScope
from rivet.readers.registry import ReaderRegistry
from rivet.readers.service import ReaderService

EXPECTED_CAPABILITIES = {
    "reader.text",
    "reader.structured",
    "reader.notebook",
    "reader.document",
    "reader.image",
    "reader.media",
    "reader.archive",
    "reader.email",
    "reader.binary",
}
HEAVY_PREFIXES = ("markitdown", "PIL", "faster_whisper", "py7zr")


def test_builtin_registry_has_one_descriptor_per_capability() -> None:
    registry = ReaderRegistry.load_builtin()

    assert {
        item.capability_id for item in registry.descriptors
    } == EXPECTED_CAPABILITIES
    assert len(registry.descriptors) == len(EXPECTED_CAPABILITIES)
    assert registry.active_reader_ids == ()


@pytest.mark.asyncio
async def test_text_read_does_not_import_heavy_readers(tmp_path: Path) -> None:
    source = tmp_path / "sample.txt"
    source.write_text("Rivet text\n", encoding="utf-8")
    before = set(sys.modules)
    scope = ResourceScope("reader.text.lazy")
    service = ReaderService(tmp_path, scope=scope)

    result = await service.read(ReaderRequest(source_path="sample.txt"))

    imported = set(sys.modules) - before
    assert result.content == "Rivet text\n"
    assert not any(name.startswith(HEAVY_PREFIXES) for name in imported)
    assert service.active_reader_ids == ("reader.text",)
    scope.assert_empty()
    await scope.close()
