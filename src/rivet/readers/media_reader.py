"""读取 WAV 原生元数据并隔离其他音视频容器探测。"""

from __future__ import annotations

import importlib.util
import os
import wave
from pathlib import Path

from pydantic import JsonValue

from rivet.contracts.common import SourceSpan
from rivet.contracts.readers import ReaderStatus, SupportLevel

from .base import ReaderContext, ReaderError, ReaderPayload
from .worker_protocol import parse_worker_output, run_reader_worker


def _configured_transcription_model() -> Path | None:
    """解析显式模型或用户数据目录中的固定 tiny 模型，绝不自动下载。"""
    configured = os.environ.get("RIVET_TRANSCRIPTION_MODEL_PATH")
    if configured:
        candidate = Path(configured).expanduser()
        cache_root: Path | None = None
    else:
        data_home = os.environ.get("XDG_DATA_HOME")
        root = (
            Path(data_home).expanduser() if data_home else Path.home() / ".local/share"
        )
        candidate = root / "rivet/models/faster-whisper-tiny"
        cache_root = candidate

    def valid_model(path: Path) -> Path | None:
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return None
        if resolved.is_dir() and (resolved / "model.bin").is_file():
            return resolved
        return None

    direct = valid_model(candidate)
    if direct is not None:
        return direct
    if cache_root is None:
        return None
    try:
        reference = (
            (cache_root / "models--Systran--faster-whisper-tiny/refs/main")
            .read_text(encoding="ascii")
            .strip()
        )
    except (OSError, UnicodeError):
        return None
    if len(reference) != 40 or any(
        character not in "0123456789abcdef" for character in reference
    ):
        return None
    return valid_model(
        cache_root / "models--Systran--faster-whisper-tiny/snapshots" / reference
    )


class MediaReader:
    """返回音视频确定性元数据，并显式说明可选转写能力。"""

    reader_id = "reader.media"
    reader_version = "1.0.0"

    async def read(self, context: ReaderContext) -> ReaderPayload:
        """WAV 使用标准库，其余格式使用固定 ffmpeg worker。"""
        metadata: dict[str, JsonValue]
        if context.inspection.detected_format == "wav":
            try:
                with wave.open(str(context.inspection.absolute_path), "rb") as stream:
                    frame_rate = stream.getframerate()
                    frame_count = stream.getnframes()
                    duration = frame_count / frame_rate if frame_rate else 0.0
                    metadata = {
                        "channels": stream.getnchannels(),
                        "sample_width_bytes": stream.getsampwidth(),
                        "sample_rate": frame_rate,
                        "frame_count": frame_count,
                        "duration_seconds": round(duration, 6),
                    }
            except (OSError, EOFError, wave.Error) as error:
                raise ReaderError("reader.media.invalid_wav", "WAV 无法解析") from error
            content = (
                f"audio duration_seconds={metadata['duration_seconds']} "
                f"sample_rate={metadata['sample_rate']} channels={metadata['channels']}\n"
            )
            warnings: list[str] = []
        else:
            output = parse_worker_output(
                await run_reader_worker(
                    context,
                    mode="media",
                    arguments=(
                        "--max-frames",
                        str(context.request.max_video_frames),
                        "--max-image-pixels",
                        str(context.request.max_image_pixels),
                    ),
                )
            )
            metadata = output.metadata
            content = output.content
            warnings = list(output.warnings)
        status = ReaderStatus.SUCCESS
        if "reader.video.pixel_limit_exceeded" in warnings:
            status = ReaderStatus.FAILED
        elif "reader.video.frame_extraction_failed" in warnings:
            status = ReaderStatus.DEGRADED
        duration = metadata.get("duration_seconds")
        if (
            isinstance(duration, (int, float))
            and duration > context.request.max_audio_duration
        ):
            warnings.append("reader.media.duration_limit_exceeded")
            status = ReaderStatus.FAILED
        if context.request.enable_transcription:
            if importlib.util.find_spec("faster_whisper") is None:
                warnings.append("reader.media.transcription_unavailable")
                status = ReaderStatus.DEGRADED
            else:
                model_path = _configured_transcription_model()
                if model_path is None:
                    warnings.append("reader.media.transcription_model_not_configured")
                    status = ReaderStatus.DEGRADED
                else:
                    try:
                        transcription = parse_worker_output(
                            await run_reader_worker(
                                context,
                                mode="transcription",
                                arguments=("--model-path", str(model_path)),
                            )
                        )
                    except ReaderError:
                        warnings.append("reader.media.transcription_failed")
                        status = ReaderStatus.DEGRADED
                    else:
                        content += f"\n## Transcription\n{transcription.content}"
                        metadata.update(transcription.metadata)
                        warnings.extend(transcription.warnings)
                        if transcription.warnings:
                            status = ReaderStatus.DEGRADED
        if context.request.enable_ocr and context.inspection.media_type.startswith(
            "video/"
        ):
            warnings.append("reader.video.keyframe_ocr_unavailable")
            status = ReaderStatus.DEGRADED
        return ReaderPayload(
            status=status,
            support_level=SupportLevel.NATIVE,
            content=content,
            metadata=metadata,
            warnings=tuple(warnings),
            source_spans=(
                SourceSpan(
                    repository_path=context.inspection.source_path,
                    start_line=1,
                    end_line=1,
                ),
            ),
        )
