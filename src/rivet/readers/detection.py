"""使用魔数、容器特征和文本探测识别 Reader capability。"""

from __future__ import annotations

import hashlib
import mimetypes
import zipfile
from pathlib import Path, PurePosixPath

from .base import FileInspection

PROBE_BYTES = 8 * 1024

TEXT_EXTENSIONS = frozenset(
    {
        ".c",
        ".cfg",
        ".conf",
        ".cpp",
        ".css",
        ".go",
        ".ini",
        ".java",
        ".js",
        ".jsx",
        ".log",
        ".md",
        ".py",
        ".pyi",
        ".rs",
        ".rst",
        ".sh",
        ".sql",
        ".text",
        ".ts",
        ".tsx",
        ".txt",
    }
)
STRUCTURED_FORMATS = {
    ".json": ("json", "application/json"),
    ".jsonl": ("jsonl", "application/x-ndjson"),
    ".yaml": ("yaml", "application/yaml"),
    ".yml": ("yaml", "application/yaml"),
    ".toml": ("toml", "application/toml"),
    ".xml": ("xml", "application/xml"),
    ".csv": ("csv", "text/csv"),
    ".tsv": ("tsv", "text/tab-separated-values"),
}
DOCUMENT_FORMATS = {
    ".pdf": ("pdf", "application/pdf"),
    ".docx": (
        "docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ".pptx": (
        "pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    ".xlsx": (
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    ".xls": ("xls", "application/vnd.ms-excel"),
    ".html": ("html", "text/html"),
    ".htm": ("html", "text/html"),
    ".epub": ("epub", "application/epub+zip"),
}
IMAGE_FORMATS = {
    ".png": ("png", "image/png"),
    ".jpg": ("jpeg", "image/jpeg"),
    ".jpeg": ("jpeg", "image/jpeg"),
    ".webp": ("webp", "image/webp"),
    ".gif": ("gif", "image/gif"),
    ".bmp": ("bmp", "image/bmp"),
    ".tif": ("tiff", "image/tiff"),
    ".tiff": ("tiff", "image/tiff"),
}
MEDIA_FORMATS = {
    ".wav": ("wav", "audio/wav"),
    ".mp3": ("mp3", "audio/mpeg"),
    ".m4a": ("m4a", "audio/mp4"),
    ".flac": ("flac", "audio/flac"),
    ".ogg": ("ogg", "audio/ogg"),
    ".mp4": ("mp4", "video/mp4"),
    ".mov": ("mov", "video/quicktime"),
    ".webm": ("webm", "video/webm"),
    ".mkv": ("mkv", "video/x-matroska"),
}
ARCHIVE_FORMATS = {
    ".zip": ("zip", "application/zip"),
    ".tar": ("tar", "application/x-tar"),
    ".tgz": ("tar.gz", "application/gzip"),
    ".gz": ("tar.gz", "application/gzip"),
    ".7z": ("7z", "application/x-7z-compressed"),
}
EMAIL_FORMATS = {
    ".eml": ("eml", "message/rfc822"),
    ".msg": ("msg", "application/vnd.ms-outlook"),
}


def _sha256(path: Path) -> str:
    """流式计算完整来源哈希而不把文件整体载入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _looks_like_text(probe: bytes) -> bool:
    """接受 UTF BOM 或低控制字符比例的 UTF-8 文本。"""
    if probe.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")):
        return True
    if b"\x00" in probe:
        return False
    try:
        probe.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    if not probe:
        return True
    controls = sum(byte < 32 and byte not in b"\t\n\r\f\b" for byte in probe)
    return controls / len(probe) <= 0.02


def _magic_format(probe: bytes) -> tuple[str, str, str] | None:
    """返回强魔数确定的格式、MIME 和 capability。"""
    signatures = (
        (b"%PDF-", "pdf", "application/pdf", "reader.document"),
        (b"\x89PNG\r\n\x1a\n", "png", "image/png", "reader.image"),
        (b"\xff\xd8\xff", "jpeg", "image/jpeg", "reader.image"),
        (b"GIF87a", "gif", "image/gif", "reader.image"),
        (b"GIF89a", "gif", "image/gif", "reader.image"),
        (b"BM", "bmp", "image/bmp", "reader.image"),
        (b"II*\x00", "tiff", "image/tiff", "reader.image"),
        (b"MM\x00*", "tiff", "image/tiff", "reader.image"),
        (b"fLaC", "flac", "audio/flac", "reader.media"),
        (b"OggS", "ogg", "audio/ogg", "reader.media"),
        (b"ID3", "mp3", "audio/mpeg", "reader.media"),
        (
            b"7z\xbc\xaf'\x1c",
            "7z",
            "application/x-7z-compressed",
            "reader.archive.sevenzip",
        ),
        (
            b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
            "ole",
            "application/x-ole-storage",
            "reader.binary",
        ),
    )
    for signature, detected_format, media_type, capability_id in signatures:
        if probe.startswith(signature):
            return detected_format, media_type, capability_id
    if probe.startswith(b"RIFF") and probe[8:12] == b"WAVE":
        return "wav", "audio/wav", "reader.media"
    if probe.startswith(b"RIFF") and probe[8:12] == b"WEBP":
        return "webp", "image/webp", "reader.image"
    if len(probe) >= 12 and probe[4:8] == b"ftyp":
        major_brand = probe[8:12]
        detected_format = "mov" if major_brand == b"qt  " else "mp4"
        media_type = "video/quicktime" if detected_format == "mov" else "video/mp4"
        return detected_format, media_type, "reader.media"
    if probe.startswith(b"\x1aE\xdf\xa3"):
        return "matroska", "video/x-matroska", "reader.media"
    if len(probe) >= 262 and probe[257:262] == b"ustar":
        return "tar", "application/x-tar", "reader.archive"
    if probe.startswith(b"PK\x03\x04"):
        return "zip", "application/zip", "reader.archive"
    if probe.startswith(b"\x1f\x8b"):
        return "tar.gz", "application/gzip", "reader.archive"
    return None


def _zip_document_format(path: Path, suffix: str) -> tuple[str, str, str] | None:
    """只查看 ZIP 名称区分 Office 与 EPUB 容器。"""
    if suffix not in {".docx", ".pptx", ".xlsx", ".epub"}:
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            names = frozenset(archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return None
    required = {
        ".docx": "word/document.xml",
        ".pptx": "ppt/presentation.xml",
        ".xlsx": "xl/workbook.xml",
        ".epub": "META-INF/container.xml",
    }[suffix]
    if required not in names:
        return None
    detected_format, media_type = DOCUMENT_FORMATS[suffix]
    return detected_format, media_type, "reader.document"


def detect_file(path: Path, *, source_path: str) -> FileInspection:
    """对普通文件执行有界探测并返回完整来源哈希。"""
    absolute_path = path.resolve(strict=True)
    if not absolute_path.is_file():
        raise ValueError("Reader 来源必须是普通文件")
    size_bytes = absolute_path.stat().st_size
    with absolute_path.open("rb") as stream:
        probe = stream.read(PROBE_BYTES)
    suffixes = tuple(
        suffix.casefold() for suffix in PurePosixPath(source_path).suffixes
    )
    suffix = suffixes[-1] if suffixes else ""
    extension_format: tuple[str, str] | None = None
    for mapping in (
        STRUCTURED_FORMATS,
        DOCUMENT_FORMATS,
        IMAGE_FORMATS,
        MEDIA_FORMATS,
        ARCHIVE_FORMATS,
        EMAIL_FORMATS,
    ):
        if suffix in mapping:
            extension_format = mapping[suffix]
            break
    if suffixes[-2:] == (".tar", ".gz"):
        extension_format = ("tar.gz", "application/gzip")
    warnings: list[str] = []
    magic = _magic_format(probe)
    zip_document = _zip_document_format(absolute_path, suffix)
    if zip_document is not None:
        detected_format, media_type, capability_id = zip_document
    elif magic is not None:
        detected_format, media_type, capability_id = magic
        if detected_format == "ole" and suffix == ".xls":
            detected_format, media_type, capability_id = (
                "xls",
                "application/vnd.ms-excel",
                "reader.document",
            )
        elif detected_format == "ole" and suffix == ".msg":
            detected_format, media_type, capability_id = (
                "msg",
                "application/vnd.ms-outlook",
                "reader.email",
            )
        elif detected_format == "mp4" and suffix == ".m4a":
            detected_format, media_type = "m4a", "audio/mp4"
        elif detected_format == "matroska" and suffix == ".webm":
            detected_format, media_type = "webm", "video/webm"
        elif detected_format == "matroska":
            detected_format = "mkv"
    elif _looks_like_text(probe):
        if suffix == ".ipynb":
            detected_format, media_type, capability_id = (
                "notebook",
                "application/x-ipynb+json",
                "reader.notebook",
            )
        elif suffix in STRUCTURED_FORMATS:
            detected_format, media_type = STRUCTURED_FORMATS[suffix]
            capability_id = "reader.structured"
        elif suffix in {".html", ".htm"}:
            detected_format, media_type, capability_id = (
                "html",
                "text/html",
                "reader.document",
            )
        elif suffix == ".eml":
            detected_format, media_type, capability_id = (
                "eml",
                "message/rfc822",
                "reader.email",
            )
        else:
            detected_format = "text"
            media_type = mimetypes.guess_type(source_path)[0] or "text/plain"
            capability_id = "reader.text"
            if extension_format is not None or suffix in IMAGE_FORMATS:
                warnings.append("reader.detect.extension_mismatch")
    elif suffix in TEXT_EXTENSIONS:
        detected_format = "text"
        media_type = mimetypes.guess_type(source_path)[0] or "text/plain"
        capability_id = "reader.text"
        warnings.append("reader.detect.signature_unconfirmed")
    elif suffix in MEDIA_FORMATS and magic is None:
        detected_format, media_type = MEDIA_FORMATS[suffix]
        capability_id = "reader.media"
        warnings.append("reader.detect.signature_unconfirmed")
    else:
        detected_format, media_type, capability_id = (
            "binary",
            "application/octet-stream",
            "reader.binary",
        )
        if extension_format is not None:
            warnings.append("reader.detect.extension_mismatch")
    return FileInspection(
        source_path=source_path,
        absolute_path=absolute_path,
        size_bytes=size_bytes,
        source_sha256=_sha256(absolute_path),
        media_type=media_type,
        detected_format=detected_format,
        capability_id=capability_id,
        magic_hex=probe[:16].hex(),
        warnings=tuple(warnings),
    )
