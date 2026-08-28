"""检测 Reader 必需 Python 原语与可选系统能力。"""

from __future__ import annotations

import importlib.util
import json
import shutil
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class ReaderDoctorItem:
    """保存单个 Reader 依赖的可用性与安装建议。"""

    component_id: str
    available: bool
    required: bool
    source: str | None
    next_action: str | None


@dataclass(frozen=True, slots=True)
class ReaderDoctorReport:
    """汇总 Reader 核心就绪状态和冻结安全预算。"""

    ready: bool
    components: tuple[ReaderDoctorItem, ...]
    limits: dict[str, int]

    def to_json(self) -> str:
        """返回稳定字段顺序的机器可读 JSON。"""
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


class ReaderDoctor:
    """只探测包清单和 executable，不启动任何解析器。"""

    def inspect(self) -> ReaderDoctorReport:
        """区分影响基础支持的必需项和按需增强项。"""
        package_components = (
            ("reader.document.markitdown", "markitdown", True),
            ("reader.image.pillow", "PIL", True),
            ("reader.archive.py7zr", "py7zr", True),
            ("reader.media.ffmpeg", "imageio_ffmpeg", True),
            ("reader.transcription.whisper", "faster_whisper", False),
        )
        items: list[ReaderDoctorItem] = []
        for component_id, package_name, required in package_components:
            available = importlib.util.find_spec(package_name) is not None
            items.append(
                ReaderDoctorItem(
                    component_id=component_id,
                    available=available,
                    required=required,
                    source="python-package" if available else None,
                    next_action=(
                        None
                        if available
                        else (
                            "安装项目锁定依赖"
                            if required
                            else "按需安装 rivet[transcription] 并配置本地模型"
                        )
                    ),
                )
            )
        executable_components = (
            ("reader.ocr.tesseract", "tesseract", False),
            ("reader.pdf.poppler", "pdftoppm", False),
            ("reader.media.ffprobe", "ffprobe", False),
        )
        for component_id, executable_name, required in executable_components:
            executable = shutil.which(executable_name)
            items.append(
                ReaderDoctorItem(
                    component_id=component_id,
                    available=executable is not None,
                    required=required,
                    source=executable,
                    next_action=(
                        None
                        if executable is not None
                        else f"安装 {executable_name} 后显式启用对应增强能力"
                    ),
                )
            )
        components = tuple(sorted(items, key=lambda item: item.component_id))
        return ReaderDoctorReport(
            ready=all(item.available for item in components if item.required),
            components=components,
            limits={
                "max_file_bytes": 50 * 1024 * 1024,
                "max_archive_entries": 1_000,
                "max_archive_depth": 3,
                "max_expanded_bytes": 200 * 1024 * 1024,
                "max_ocr_pages": 100,
                "max_video_frames": 20,
            },
        )
