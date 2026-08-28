"""读取 WAV 原生元数据并隔离其他音视频容器探测。"""

from __future__ import annotations

import importlib.util
import wave

from pydantic import JsonValue

from rivet.contracts.common import SourceSpan
from rivet.contracts.readers import ReaderStatus, SupportLevel

from .base import ReaderContext, ReaderError, ReaderPayload
from .worker_protocol import parse_worker_output, run_reader_worker


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
            output = parse_worker_output(await run_reader_worker(context, mode="media"))
            metadata = output.metadata
            content = output.content
            warnings = list(output.warnings)
        status = ReaderStatus.SUCCESS
        if context.request.enable_transcription:
            if importlib.util.find_spec("faster_whisper") is None:
                warnings.append("reader.media.transcription_unavailable")
            else:
                warnings.append("reader.media.transcription_model_not_configured")
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
