"""验证 Reader doctor 区分必需解析原语与可选系统能力。"""

from __future__ import annotations

import json
import subprocess
import sys

from rivet.readers.doctor import ReaderDoctor


def test_reader_doctor_keeps_optional_profiles_out_of_core_readiness() -> None:
    report = ReaderDoctor().inspect()

    required = [item for item in report.components if item.required]
    optional = [item for item in report.components if not item.required]
    assert report.ready is True
    assert required == []
    assert {item.component_id for item in optional} >= {
        "reader.document.markitdown",
        "reader.image.pillow",
        "reader.media.ffmpeg",
        "reader.archive.py7zr",
        "reader.ocr.tesseract",
        "reader.transcription.whisper",
    }


def test_rivet_doctor_readers_json_is_machine_readable() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "rivet", "doctor", "--section", "readers", "--json"],
        check=False,
        capture_output=True,
        text=True,
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert payload["ready"] is True
    assert payload["limits"]["max_archive_entries"] == 1_000
