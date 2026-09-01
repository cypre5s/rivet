"""验证 Reader 以魔数优先识别格式并诚实处理伪扩展名。"""

from pathlib import Path

from rivet.readers.detection import detect_file


def test_magic_detects_pdf_before_extension(tmp_path: Path) -> None:
    source = tmp_path / "renamed.bin"
    source.write_bytes(b"%PDF-1.4\nfixture")

    inspection = detect_file(source, source_path="renamed.bin")

    assert inspection.detected_format == "pdf"
    assert inspection.media_type == "application/pdf"
    assert inspection.capability_id == "reader.document"


def test_text_disguised_as_pdf_uses_text_reader_with_warning(tmp_path: Path) -> None:
    source = tmp_path / "fake.pdf"
    source.write_text("Rivet plain text\n", encoding="utf-8")

    inspection = detect_file(source, source_path="fake.pdf")

    assert inspection.detected_format == "text"
    assert inspection.capability_id == "reader.text"
    assert "reader.detect.extension_mismatch" in inspection.warnings


def test_unknown_binary_has_hash_magic_and_binary_capability(tmp_path: Path) -> None:
    source = tmp_path / "sample.unknown"
    source.write_bytes(b"\x00\xffRIVET-STRING\x00\x01")

    inspection = detect_file(source, source_path="sample.unknown")

    assert inspection.detected_format == "binary"
    assert inspection.capability_id == "reader.binary"
    assert inspection.source_sha256.startswith("sha256:")
    assert inspection.magic_hex.startswith("00ff")


def test_core_archives_and_optional_sevenzip_have_distinct_capabilities(
    tmp_path: Path,
) -> None:
    zip_source = tmp_path / "sample.zip"
    zip_source.write_bytes(b"PK\x03\x04fixture")
    sevenzip_source = tmp_path / "sample.7z"
    sevenzip_source.write_bytes(b"7z\xbc\xaf'\x1cfixture")

    assert (
        detect_file(zip_source, source_path="sample.zip").capability_id
        == "reader.archive"
    )
    assert (
        detect_file(sevenzip_source, source_path="sample.7z").capability_id
        == "reader.archive.sevenzip"
    )
