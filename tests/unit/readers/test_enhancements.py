"""验证 Reader 增强能力只在本地依赖已配置时执行真实工作。"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from rivet.contracts.readers import ReaderRequest, ReaderStatus
from rivet.kernel.resources import ResourceScope
from rivet.readers import document_reader as document_reader_module
from rivet.readers import media_reader as media_reader_module
from rivet.readers import worker as reader_worker
from rivet.readers.base import FileInspection, ReaderContext
from rivet.readers.document_reader import DocumentReader
from rivet.readers.media_reader import MediaReader


def _context(
    root: Path,
    *,
    name: str,
    detected_format: str,
    media_type: str,
    ocr: bool = False,
    transcribe: bool = False,
) -> ReaderContext:
    source = root / name
    source.write_bytes(b"fixture")
    return ReaderContext(
        inspection=FileInspection(
            source_path=name,
            absolute_path=source,
            size_bytes=source.stat().st_size,
            source_sha256=f"sha256:{'1' * 64}",
            media_type=media_type,
            detected_format=detected_format,
            capability_id=(
                "reader.document" if detected_format == "pdf" else "reader.media"
            ),
            magic_hex="00",
        ),
        request=ReaderRequest(
            source_path=name,
            enable_ocr=ocr,
            enable_transcription=transcribe,
        ),
        scope=ResourceScope(f"reader.enhancement.{detected_format}"),
        repository_root=root,
    )


def _configure_cached_model(tmp_path: Path) -> Path:
    revision = "a" * 40
    model_root = (
        tmp_path
        / "rivet/models/faster-whisper-tiny"
        / "models--Systran--faster-whisper-tiny"
    )
    snapshot = model_root / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"model")
    (model_root / "refs").mkdir()
    (model_root / "refs/main").write_text(revision, encoding="ascii")
    return snapshot


@pytest.mark.asyncio
async def test_configured_local_model_produces_real_transcription(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _configure_cached_model(tmp_path)
    monkeypatch.delenv("RIVET_TRANSCRIPTION_MODEL_PATH", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    calls: list[str] = []

    async def fake_worker(
        context: ReaderContext,
        *,
        mode: str,
        arguments: tuple[str, ...] = (),
    ) -> dict[str, object]:
        del context
        calls.append(mode)
        if mode == "media":
            return {
                "ok": True,
                "content": "media duration=1.0 seconds streams=1\n",
                "metadata": {"duration_seconds": 1.0},
                "warnings": [],
                "truncated": False,
            }
        assert arguments == ("--model-path", str(model.resolve()))
        return {
            "ok": True,
            "content": "hello from rivet\n",
            "metadata": {
                "transcription_language": "en",
                "transcription_segments": 1,
            },
            "warnings": [],
            "truncated": False,
        }

    monkeypatch.setattr(media_reader_module, "run_reader_worker", fake_worker)
    context = _context(
        tmp_path,
        name="sample.mp3",
        detected_format="mp3",
        media_type="audio/mpeg",
        transcribe=True,
    )

    result = await MediaReader().read(context)

    assert result.status is ReaderStatus.SUCCESS
    assert calls == ["media", "transcription"]
    assert "## Transcription\nhello from rivet" in result.content
    assert result.metadata["transcription_language"] == "en"
    assert "reader.media.transcription_model_not_configured" not in result.warnings
    await context.scope.close()


@pytest.mark.asyncio
async def test_pdf_ocr_renders_pages_and_appends_recognized_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_which(_: str) -> str:
        return "/ocr"

    monkeypatch.setattr(document_reader_module.shutil, "which", fake_which)

    async def fake_worker(
        context: ReaderContext,
        *,
        mode: str,
        arguments: tuple[str, ...] = (),
    ) -> dict[str, object]:
        del context
        calls.append(mode)
        if mode == "document":
            return {
                "ok": True,
                "content": "embedded text\n",
                "metadata": {"converter": "markitdown"},
                "warnings": [],
                "truncated": False,
            }
        assert arguments == (
            "--max-ocr-pages",
            "100",
            "--max-image-pixels",
            "40000000",
        )
        return {
            "ok": True,
            "content": "recognized page text\n",
            "metadata": {"ocr_pages": 1, "ocr_engine": "tesseract"},
            "warnings": [],
            "truncated": False,
        }

    monkeypatch.setattr(document_reader_module, "run_reader_worker", fake_worker)
    context = _context(
        tmp_path,
        name="sample.pdf",
        detected_format="pdf",
        media_type="application/pdf",
        ocr=True,
    )

    result = await DocumentReader().read(context)

    assert result.status is ReaderStatus.SUCCESS
    assert calls == ["document", "pdf_ocr"]
    assert "## OCR\nrecognized page text" in result.content
    assert result.metadata["ocr_pages"] == 1
    assert "reader.document.ocr_not_configured" not in result.warnings
    await context.scope.close()


def test_transcription_worker_loads_only_configured_local_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "speech.mp3"
    source.write_bytes(b"audio")
    model_path = tmp_path / "model"
    model_path.mkdir()
    (model_path / "model.bin").write_bytes(b"model")
    constructor_options: dict[str, object] = {}
    transcription_options: dict[str, object] = {}

    class Segment:
        start = 0.25
        end = 1.5
        text = " local speech "

    class Info:
        language = "en"
        language_probability = 0.875

    class WhisperModel:
        def __init__(self, path: str, **options: object) -> None:
            assert path == str(model_path)
            constructor_options.update(options)

        def transcribe(
            self, audio: str, **options: object
        ) -> tuple[Iterable[Segment], Info]:
            assert audio == str(source)
            transcription_options.update(options)
            return (Segment(),), Info()

    def fake_import(name: str) -> object:
        assert name == "faster_whisper"
        return SimpleNamespace(WhisperModel=WhisperModel)

    monkeypatch.setattr(reader_worker.importlib, "import_module", fake_import)
    monkeypatch.chdir(tmp_path)

    exit_code = reader_worker.main(
        (
            "--mode",
            "transcription",
            "--source",
            source.name,
            "--model-path",
            str(model_path),
        )
    )

    payload = cast(dict[str, object], json.loads(capsys.readouterr().out))
    assert exit_code == 0
    assert payload["content"] == "[0.25-1.50] local speech\n"
    assert payload["warnings"] == []
    assert constructor_options == {
        "device": "cpu",
        "compute_type": "int8",
        "cpu_threads": 4,
        "num_workers": 1,
        "local_files_only": True,
    }
    assert transcription_options["beam_size"] == 1
    assert transcription_options["vad_filter"] is True


def test_pdf_ocr_worker_obeys_page_limit_and_releases_render_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-fixture")
    closed: list[str] = []

    class Image:
        def save(self, path: Path) -> None:
            path.write_bytes(b"png")

        def close(self) -> None:
            closed.append("image")

    class Bitmap:
        def to_pil(self) -> Image:
            return Image()

        def close(self) -> None:
            closed.append("bitmap")

    class Page:
        def get_size(self) -> tuple[float, float]:
            return 100.0, 100.0

        def render(self, *, scale: float) -> Bitmap:
            assert scale == 2.0
            return Bitmap()

        def close(self) -> None:
            closed.append("page")

    class Document:
        def __init__(self, path: Path) -> None:
            assert path == source

        def __len__(self) -> int:
            return 2

        def __getitem__(self, index: int) -> Page:
            assert index == 0
            return Page()

        def close(self) -> None:
            closed.append("document")

    def fake_import(name: str) -> object:
        assert name == "pypdfium2"
        return SimpleNamespace(PdfDocument=Document)

    def fake_run(
        argv: list[str],
        *,
        check: bool,
        capture_output: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[bytes]:
        assert argv[0] == "/ocr"
        assert argv[-1] == "stdout"
        assert check is False
        assert capture_output is True
        assert timeout == 20
        return subprocess.CompletedProcess(argv, 0, b"recognized text\n", b"")

    def fake_which(_: str) -> str:
        return "/ocr"

    monkeypatch.setattr(reader_worker.importlib, "import_module", fake_import)
    monkeypatch.setattr(reader_worker.shutil, "which", fake_which)
    monkeypatch.setattr(reader_worker.subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)

    exit_code = reader_worker.main(
        (
            "--mode",
            "pdf_ocr",
            "--source",
            source.name,
            "--max-ocr-pages",
            "1",
        )
    )

    payload = cast(dict[str, object], json.loads(capsys.readouterr().out))
    metadata = cast(dict[str, object], payload["metadata"])
    assert exit_code == 0
    assert payload["content"] == "### Page 1\nrecognized text\n"
    assert payload["warnings"] == ["reader.pdf.ocr_page_limit_reached"]
    assert metadata["ocr_pages"] == 1
    assert metadata["pdf_pages"] == 2
    assert closed == ["image", "bitmap", "page", "document"]
