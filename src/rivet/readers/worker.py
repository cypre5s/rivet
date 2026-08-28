"""在短生命周期子进程内调用文档、图片、媒体和 7z 解析器。"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import subprocess
import tempfile
from collections.abc import Callable, Sequence
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


def _media(source: Path) -> dict[str, JsonValue]:
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
    return {
        "ok": True,
        "content": content,
        "metadata": metadata,
        "warnings": [],
        "truncated": False,
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
    parser.add_argument("--mode", choices=("document", "image", "media", "sevenzip"))
    parser.add_argument("--source", required=True)
    parser.add_argument("--max-frames", type=int, default=20)
    parser.add_argument("--max-entries", type=int, default=1_000)
    parser.add_argument("--max-expanded-bytes", type=int, default=200 * 1024 * 1024)
    parser.add_argument("--max-ratio", type=float, default=100.0)
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
            payload = _media(source)
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
