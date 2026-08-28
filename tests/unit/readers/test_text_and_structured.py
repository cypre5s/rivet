"""验证文本、结构化数据与 Notebook 的安全抽取。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rivet.contracts.readers import ReaderRequest, ReaderResult, ReaderStatus
from rivet.kernel.resources import ResourceScope
from rivet.readers.service import ReaderService


async def _read(tmp_path: Path, name: str, content: bytes) -> ReaderResult:
    """写入单个 fixture 并返回 Reader 结果。"""
    (tmp_path / name).write_bytes(content)
    scope = ResourceScope(f"reader.unit.{name.replace('.', '_')}")
    service = ReaderService(tmp_path, scope=scope)
    result = await service.read(ReaderRequest(source_path=name, max_output_chars=2_000))
    scope.assert_empty()
    await scope.close()
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("sample.json", b'{\n  "user": {"name": "Rivet"}\n}\n', "$.user.name"),
        ("sample.jsonl", b'{"id": 1}\n{"id": "Rivet"}\n', "$[2].id"),
        ("sample.yaml", b"user:\n  name: Rivet\n", "$.user.name"),
        ("sample.toml", b'[user]\nname = "Rivet"\n', "$.user.name"),
        ("sample.xml", b"<root><name>Rivet</name></root>\n", "/root/name"),
        ("sample.csv", b"name,value\nproject,Rivet\n", "row[2].value"),
        ("sample.tsv", b"name\tvalue\nproject\tRivet\n", "row[2].value"),
    ],
)
async def test_structured_reader_preserves_object_paths(
    tmp_path: Path,
    name: str,
    content: bytes,
    expected: str,
) -> None:
    result = await _read(tmp_path, name, content)

    assert result.status is ReaderStatus.SUCCESS
    assert expected in result.content
    assert "Rivet" in result.content
    assert result.source_spans


@pytest.mark.asyncio
async def test_text_reader_rejects_nul_without_inventing_content(
    tmp_path: Path,
) -> None:
    result = await _read(tmp_path, "broken.txt", b"before\x00after")

    assert result.status is ReaderStatus.FAILED
    assert result.content == ""
    assert "reader.text.nul_detected" in result.warnings


@pytest.mark.asyncio
async def test_notebook_limits_outputs_and_records_cell_metadata(
    tmp_path: Path,
) -> None:
    notebook: dict[str, object] = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": ["Rivet note"]},
            {
                "cell_type": "code",
                "execution_count": 7,
                "metadata": {},
                "source": ["print('Rivet code')"],
                "outputs": [
                    {
                        "output_type": "stream",
                        "name": "stdout",
                        "text": ["x" * 10_000],
                    }
                ],
            },
        ],
    }
    result = await _read(
        tmp_path,
        "sample.ipynb",
        json.dumps(notebook).encode("utf-8"),
    )

    assert result.status is ReaderStatus.TRUNCATED
    assert "cell[1] markdown" in result.content
    assert "execution_count=7" in result.content
    assert result.metadata["cell_count"] == 2
    assert result.truncated is True
