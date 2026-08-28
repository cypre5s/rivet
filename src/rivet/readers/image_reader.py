"""隔离 Pillow 元数据读取并按需调用本地 Tesseract。"""

from __future__ import annotations

import shutil

from rivet.contracts.common import SourceSpan
from rivet.contracts.readers import ReaderStatus, SupportLevel
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.process import ProcessRunner

from .base import ReaderContext, ReaderPayload
from .worker_protocol import parse_worker_output, run_reader_worker


class ImageReader:
    """读取图片尺寸、帧和模式，OCR 缺失时明确降级。"""

    reader_id = "reader.image"
    reader_version = "1.0.0"

    async def read(self, context: ReaderContext) -> ReaderPayload:
        """在 worker 内使用 Pillow，并只在显式请求时启动 OCR。"""
        raw_output = await run_reader_worker(
            context,
            mode="image",
            arguments=("--max-frames", str(context.request.max_video_frames)),
        )
        output = parse_worker_output(raw_output)
        content = output.content
        warnings = list(output.warnings)
        status = ReaderStatus.TRUNCATED if output.truncated else ReaderStatus.SUCCESS
        if context.request.enable_ocr:
            executable = shutil.which("tesseract")
            if executable is None:
                warnings.append("reader.image.ocr_unavailable")
                status = ReaderStatus.DEGRADED
            else:
                runner = ProcessRunner(
                    WorkspaceBoundary(context.repository_root),
                    scope=context.scope,
                    max_capture_bytes=context.request.max_output_chars,
                    root_kind="repository_read_only",
                )
                completed = await runner.run(
                    (
                        executable,
                        context.inspection.source_path,
                        "stdout",
                    ),
                    cwd=".",
                    timeout_seconds=float(context.request.timeout_seconds),
                )
                if completed.returncode == 0 and not completed.timed_out:
                    ocr_text = completed.stdout.decode("utf-8", errors="replace")
                    if ocr_text.strip():
                        content += f"\n## OCR\n{ocr_text}"
                else:
                    warnings.append("reader.image.ocr_failed")
                    status = ReaderStatus.DEGRADED
        return ReaderPayload(
            status=status,
            support_level=SupportLevel.NATIVE,
            content=content,
            metadata=output.metadata,
            warnings=tuple(warnings),
            source_spans=(
                SourceSpan(
                    repository_path=context.inspection.source_path,
                    start_line=1,
                    end_line=1,
                ),
            ),
            truncated=output.truncated,
        )
