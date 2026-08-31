"""在短生命周期子进程内调用文档、图片、媒体和 7z 解析器。"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

from pydantic import JsonValue


class ConversionResult(Protocol):
    """描述 MarkItDown 转换结果的稳定最小字段。"""

    text_content: str
    title: str | None


class ImageFrameMetadata(Protocol):
    """描述 Pillow 多帧插件公开的帧计数字段。"""

    n_frames: int


class TranscriptionSegment(Protocol):
    """描述 faster-whisper 惰性片段的稳定最小字段。"""

    start: float
    end: float
    text: str


class TranscriptionInfo(Protocol):
    """描述 faster-whisper 返回的语言检测字段。"""

    language: str
    language_probability: float


class WhisperModelInstance(Protocol):
    """描述 faster-whisper 模型实例所需的惰性转录入口。"""

    def transcribe(
        self, audio: str, **options: object
    ) -> tuple[Iterable[TranscriptionSegment], TranscriptionInfo]: ...


class RenderedImage(Protocol):
    """描述 PDFium 转出的 Pillow 图片生命周期。"""

    def save(self, path: Path) -> None: ...

    def close(self) -> None: ...


class PdfBitmap(Protocol):
    """描述 PDFium 位图的转换与释放入口。"""

    def to_pil(self) -> RenderedImage: ...

    def close(self) -> None: ...


class PdfPage(Protocol):
    """描述单页渲染所需的稳定 PDFium API。"""

    def get_size(self) -> tuple[float, float]: ...

    def render(self, *, scale: float) -> PdfBitmap: ...

    def close(self) -> None: ...


class PdfDocument(Protocol):
    """描述 PDFium 文档的索引和释放入口。"""

    def __len__(self) -> int: ...

    def __getitem__(self, index: int) -> PdfPage: ...

    def close(self) -> None: ...


def _safe_source(source_text: str) -> Path:
    """只允许 worker 当前目录内的普通来源文件。"""
    lexical = Path(source_text)
    if lexical.is_absolute() or ".." in lexical.parts or "\x00" in source_text:
        raise ValueError("来源路径越界")
    root = Path.cwd().resolve(strict=True)
    source = (root / lexical).resolve(strict=True)
    source.relative_to(root)
    if not source.is_file():
        raise ValueError("来源不是普通文件")
    return source


def _document(source: Path) -> dict[str, JsonValue]:
    """禁用插件并调用最窄的本地 MarkItDown API。"""
    from markitdown import MarkItDown

    converter = MarkItDown(enable_plugins=False)
    result = cast(ConversionResult, converter.convert_local(source))
    metadata: dict[str, JsonValue] = {
        "converter": "markitdown",
        "title": result.title,
    }
    return {
        "ok": True,
        "content": result.text_content,
        "metadata": metadata,
        "warnings": [],
        "truncated": False,
    }


def _image(source: Path, *, max_frames: int) -> dict[str, JsonValue]:
    """以 Pillow 读取有界像素和帧元数据，不解码任意嵌入脚本。"""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = 100_000_000
    with Image.open(source) as image:
        width, height = image.size
        try:
            frame_count = cast(ImageFrameMetadata, image).n_frames
        except AttributeError:
            frame_count = 1
        inspected_frames = min(frame_count, max_frames)
        metadata: dict[str, JsonValue] = {
            "width": width,
            "height": height,
            "mode": image.mode,
            "format": image.format,
            "frame_count": frame_count,
            "inspected_frames": inspected_frames,
        }
        content = (
            f"image width={width} height={height} mode={image.mode} "
            f"frames={frame_count}\n"
        )
        return {
            "ok": True,
            "content": content,
            "metadata": metadata,
            "warnings": (
                ["reader.image.frame_limit_reached"]
                if frame_count > inspected_frames
                else []
            ),
            "truncated": frame_count > inspected_frames,
        }


def _duration_seconds(stderr: str) -> float | None:
    """从 ffmpeg 稳定 Duration 行解析秒数。"""
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if match is None:
        return None
    hours, minutes, seconds = match.groups()
    return round(int(hours) * 3600 + int(minutes) * 60 + float(seconds), 6)


def _media(
    source: Path,
    *,
    max_frames: int,
    max_image_pixels: int,
) -> dict[str, JsonValue]:
    """使用随 wheel 固定的 ffmpeg 二进制读取容器元数据。"""
    imageio_ffmpeg = importlib.import_module("imageio_ffmpeg")
    get_executable = cast(Callable[[], str], imageio_ffmpeg.__dict__["get_ffmpeg_exe"])
    executable = get_executable()
    completed = subprocess.run(
        [executable, "-hide_banner", "-i", str(source)],
        check=False,
        capture_output=True,
        timeout=20,
    )
    stderr = completed.stderr.decode("utf-8", errors="replace")[:256_000]
    duration = _duration_seconds(stderr)
    stream_summaries = [
        line.strip()[:1_024] for line in stderr.splitlines() if "Stream #" in line
    ][:32]
    if duration is None and not stream_summaries:
        raise ValueError("媒体容器无法探测")
    metadata: dict[str, JsonValue] = {
        "duration_seconds": duration,
        "streams": cast(JsonValue, stream_summaries),
        "probe": "imageio-ffmpeg",
    }
    content = f"media duration={duration} seconds streams={len(stream_summaries)}\n"
    warnings: list[str] = []
    video_stream = next(
        (summary for summary in stream_summaries if " Video:" in summary),
        None,
    )
    if video_stream is not None:
        dimensions = re.search(r"(?:^|\s)(\d{2,6})x(\d{2,6})(?:\s|[\[,])", video_stream)
        if dimensions is None:
            metadata["extracted_frames"] = []
            warnings.append("reader.video.dimensions_unknown")
        else:
            width, height = (int(value) for value in dimensions.groups())
            metadata["video_width"] = width
            metadata["video_height"] = height
            if width * height > max_image_pixels:
                metadata["extracted_frames"] = []
                warnings.append("reader.video.pixel_limit_exceeded")
            elif max_frames == 0:
                metadata["extracted_frames"] = []
                content += "video_frames=0\n"
            else:
                with tempfile.TemporaryDirectory(
                    prefix="rivet-video-frames-"
                ) as directory:
                    output_pattern = str(Path(directory) / "frame-%03d.jpg")
                    frame_rate = (
                        max_frames / duration
                        if isinstance(duration, (int, float)) and duration > 0
                        else 1.0
                    )
                    extracted = subprocess.run(
                        [
                            executable,
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-i",
                            str(source),
                            "-vf",
                            (
                                f"fps={frame_rate:.8f},"
                                "scale=1280:720:force_original_aspect_ratio=decrease"
                            ),
                            "-frames:v",
                            str(max_frames),
                            "-q:v",
                            "3",
                            "-y",
                            output_pattern,
                        ],
                        check=False,
                        capture_output=True,
                        timeout=20,
                    )
                    frame_evidence: list[dict[str, JsonValue]] = []
                    if extracted.returncode == 0:
                        for index, frame_path in enumerate(
                            sorted(Path(directory).glob("frame-*.jpg")),
                            start=1,
                        ):
                            frame_bytes = frame_path.read_bytes()
                            frame_evidence.append(
                                {
                                    "index": index,
                                    "sha256": f"sha256:{hashlib.sha256(frame_bytes).hexdigest()}",
                                    "size_bytes": len(frame_bytes),
                                }
                            )
                    if not frame_evidence:
                        warnings.append("reader.video.frame_extraction_failed")
                    metadata["extracted_frames"] = cast(JsonValue, frame_evidence)
                    content += f"video_frames={len(frame_evidence)}\n"
    payload: dict[str, JsonValue] = {
        "ok": True,
        "content": content,
        "metadata": metadata,
        "warnings": cast(JsonValue, warnings),
        "truncated": False,
    }
    return payload


def _transcription(source: Path, *, model_path_text: str) -> dict[str, JsonValue]:
    """只加载已配置的本地 faster-whisper 模型并执行有界 CPU 转录。"""
    model_path = Path(model_path_text)
    if not model_path.is_absolute():
        raise ValueError("转录模型路径必须是绝对路径")
    model_path = model_path.resolve(strict=True)
    if not model_path.is_dir() or not (model_path / "model.bin").is_file():
        raise ValueError("转录模型未配置")
    module = importlib.import_module("faster_whisper")
    model_type = cast(Callable[..., object], module.__dict__["WhisperModel"])
    model = cast(
        WhisperModelInstance,
        model_type(
            str(model_path),
            device="cpu",
            compute_type="int8",
            cpu_threads=4,
            num_workers=1,
            local_files_only=True,
        ),
    )
    raw_segments, info = model.transcribe(
        str(source),
        beam_size=1,
        best_of=1,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    lines: list[str] = []
    for segment in raw_segments:
        text = segment.text.strip()
        if text:
            lines.append(f"[{segment.start:.2f}-{segment.end:.2f}] {text}")
    content = "\n".join(lines)
    if content:
        content += "\n"
    metadata: dict[str, JsonValue] = {
        "transcription_engine": "faster-whisper",
        "transcription_model": model_path.name,
        "transcription_language": info.language,
        "transcription_language_probability": round(
            float(info.language_probability), 6
        ),
        "transcription_segments": len(lines),
    }
    return {
        "ok": True,
        "content": content,
        "metadata": metadata,
        "warnings": [] if lines else ["reader.media.transcription_empty"],
        "truncated": False,
    }


def _pdf_ocr(
    source: Path,
    *,
    max_pages: int,
    max_image_pixels: int,
) -> dict[str, JsonValue]:
    """用 PDFium 有界渲染页面，并交给本地 Tesseract 识别。"""
    executable = shutil.which("tesseract")
    if executable is None:
        raise ValueError("OCR 引擎未配置")
    pdfium = importlib.import_module("pypdfium2")
    document_type = cast(Callable[[Path], PdfDocument], pdfium.__dict__["PdfDocument"])
    document = document_type(source)
    page_count = len(document)
    inspected_pages = min(page_count, max_pages)
    sections: list[str] = []
    warnings: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="rivet-pdf-ocr-") as directory:
            for index in range(inspected_pages):
                page = document[index]
                try:
                    width, height = page.get_size()
                    scale = min(
                        2.0,
                        math.sqrt(max_image_pixels / max(width * height, 1.0)),
                    )
                    bitmap = page.render(scale=max(scale, 0.1))
                    try:
                        image = bitmap.to_pil()
                        try:
                            image_path = Path(directory) / f"page-{index + 1:03d}.png"
                            image.save(image_path)
                            completed = subprocess.run(
                                [executable, str(image_path), "stdout"],
                                check=False,
                                capture_output=True,
                                timeout=20,
                            )
                            if completed.returncode != 0:
                                warnings.append("reader.pdf.ocr_page_failed")
                                continue
                            text = completed.stdout.decode(
                                "utf-8", errors="replace"
                            ).strip()
                            if text:
                                sections.append(f"### Page {index + 1}\n{text}")
                        finally:
                            image.close()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
    finally:
        document.close()
    if page_count > inspected_pages:
        warnings.append("reader.pdf.ocr_page_limit_reached")
    if not sections:
        warnings.append("reader.pdf.ocr_empty")
    content = "\n\n".join(sections)
    if content:
        content += "\n"
    metadata: dict[str, JsonValue] = {
        "ocr_engine": "tesseract",
        "ocr_pages": inspected_pages,
        "pdf_pages": page_count,
    }
    return {
        "ok": True,
        "content": content,
        "metadata": metadata,
        "warnings": cast(JsonValue, warnings),
        "truncated": page_count > inspected_pages,
    }


def _safe_archive_name(name: str) -> PurePosixPath:
    """拒绝 7z 绝对路径、反斜杠、NUL 和跳转段。"""
    if "\\" in name or "\x00" in name:
        raise ValueError("7z 路径无效")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("7z 路径越界")
    return path


def _seven_zip(
    source: Path,
    *,
    max_entries: int,
    max_expanded_bytes: int,
    max_ratio: float,
) -> dict[str, JsonValue]:
    """预检 7z 清单和预算后只向临时目录展开。"""
    import py7zr

    source_size = max(source.stat().st_size, 1)
    with py7zr.SevenZipFile(source, mode="r") as archive:
        entries = archive.list()
        if len(entries) > max_entries:
            raise ValueError("7z 条目超限")
        expanded_bytes = sum(entry.uncompressed for entry in entries)
        if expanded_bytes > max_expanded_bytes:
            raise ValueError("7z 展开大小超限")
        if expanded_bytes / source_size > max_ratio:
            raise ValueError("7z 压缩比超限")
        if any(entry.is_symlink for entry in entries):
            raise ValueError("7z 符号链接禁止")
        names = [_safe_archive_name(entry.filename) for entry in entries]
        with tempfile.TemporaryDirectory(prefix="rivet-reader-7z-") as directory:
            extraction_root = Path(directory).resolve(strict=True)
            archive.extractall(path=extraction_root)
            sections: list[str] = []
            for name in names:
                extracted = (extraction_root / Path(*name.parts)).resolve(strict=True)
                extracted.relative_to(extraction_root)
                if extracted.is_symlink():
                    raise ValueError("7z 符号链接禁止")
                if not extracted.is_file() or extracted.stat().st_size > 1_000_000:
                    continue
                content_bytes = extracted.read_bytes()
                if b"\x00" in content_bytes:
                    continue
                try:
                    text = content_bytes.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    continue
                sections.append(f"## {name.as_posix()}\n{text[:65_536]}")
    content = "\n\n".join(sections)
    if content:
        content += "\n"
    metadata: dict[str, JsonValue] = {
        "entry_count": len(entries),
        "expanded_bytes": expanded_bytes,
    }
    return {
        "ok": True,
        "content": content,
        "metadata": metadata,
        "warnings": [],
        "truncated": False,
    }


def _build_parser() -> argparse.ArgumentParser:
    """构造无自由命令参数的固定 worker CLI。"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=(
            "document",
            "image",
            "media",
            "pdf_ocr",
            "sevenzip",
            "transcription",
        ),
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--max-frames", type=int, default=20)
    parser.add_argument("--max-image-pixels", type=int, default=40_000_000)
    parser.add_argument("--max-entries", type=int, default=1_000)
    parser.add_argument("--max-expanded-bytes", type=int, default=200 * 1024 * 1024)
    parser.add_argument("--max-ratio", type=float, default=100.0)
    parser.add_argument("--max-ocr-pages", type=int, default=100)
    parser.add_argument("--model-path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """执行一种固定解析模式并保证 stdout 只含一个 JSON 对象。"""
    arguments = _build_parser().parse_args(argv)
    try:
        source = _safe_source(cast(str, arguments.source))
        mode = cast(str, arguments.mode)
        if mode == "document":
            payload = _document(source)
        elif mode == "image":
            payload = _image(source, max_frames=cast(int, arguments.max_frames))
        elif mode == "media":
            payload = _media(
                source,
                max_frames=cast(int, arguments.max_frames),
                max_image_pixels=cast(int, arguments.max_image_pixels),
            )
        elif mode == "pdf_ocr":
            payload = _pdf_ocr(
                source,
                max_pages=cast(int, arguments.max_ocr_pages),
                max_image_pixels=cast(int, arguments.max_image_pixels),
            )
        elif mode == "transcription":
            model_path = cast(str | None, arguments.model_path)
            if model_path is None:
                raise ValueError("转录模型路径缺失")
            payload = _transcription(source, model_path_text=model_path)
        elif mode == "sevenzip":
            payload = _seven_zip(
                source,
                max_entries=cast(int, arguments.max_entries),
                max_expanded_bytes=cast(int, arguments.max_expanded_bytes),
                max_ratio=cast(float, arguments.max_ratio),
            )
        else:
            raise AssertionError("参数解析器不得产生未知模式")
    except Exception:
        print(
            json.dumps(
                {"ok": False, "error_code": "reader.worker.parse_failed"},
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
