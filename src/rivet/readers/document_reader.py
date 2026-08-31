"""通过短生命周期 MarkItDown worker 抽取本地文档。"""

from __future__ import annotations

import shutil

from rivet.contracts.common import SourceSpan
from rivet.contracts.readers import ReaderStatus, SupportLevel

from .base import ReaderContext, ReaderError, ReaderPayload
from .worker_protocol import parse_worker_output, run_reader_worker


class DocumentReader:
    """读取 PDF、Office、HTML 与 EPUB，并隔离解析器崩溃。"""

    reader_id = "reader.document"
    reader_version = "1.0.0"

    async def read(self, context: ReaderContext) -> ReaderPayload:
        """调用禁用插件和网络入口的本地文档 worker。"""
        raw_output = await run_reader_worker(context, mode="document")
        output = parse_worker_output(raw_output)
        content = output.content
        metadata = dict(output.metadata)
        warnings = list(output.warnings)
        status = ReaderStatus.SUCCESS
        support_level = SupportLevel.NATIVE
        if context.request.enable_ocr:
            if context.inspection.detected_format != "pdf":
                warnings.append("reader.document.ocr_not_supported")
                status = ReaderStatus.DEGRADED
            elif shutil.which("tesseract") is None:
                warnings.append("reader.pdf.ocr_unavailable")
                status = ReaderStatus.DEGRADED
            else:
                try:
                    ocr = parse_worker_output(
                        await run_reader_worker(
                            context,
                            mode="pdf_ocr",
                            arguments=(
                                "--max-ocr-pages",
                                str(context.request.max_ocr_pages),
                                "--max-image-pixels",
                                str(context.request.max_image_pixels),
                            ),
                        )
                    )
                except ReaderError:
                    warnings.append("reader.pdf.ocr_failed")
                    status = ReaderStatus.DEGRADED
                else:
                    if ocr.content.strip():
                        content += f"\n## OCR\n{ocr.content}"
                    metadata.update(ocr.metadata)
                    warnings.extend(ocr.warnings)
                    if ocr.warnings:
                        status = ReaderStatus.DEGRADED
        if not content.strip():
            status = ReaderStatus.DEGRADED
            support_level = SupportLevel.FALLBACK
            warnings.append("reader.document.no_extractable_text")
        return ReaderPayload(
            status=ReaderStatus.TRUNCATED if output.truncated else status,
            support_level=support_level,
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
            truncated=output.truncated,
        )
