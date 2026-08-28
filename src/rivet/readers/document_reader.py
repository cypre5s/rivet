"""通过短生命周期 MarkItDown worker 抽取本地文档。"""

from __future__ import annotations

from rivet.contracts.common import SourceSpan
from rivet.contracts.readers import ReaderStatus, SupportLevel

from .base import ReaderContext, ReaderPayload
from .worker_protocol import parse_worker_output, run_reader_worker


class DocumentReader:
    """读取 PDF、Office、HTML 与 EPUB，并隔离解析器崩溃。"""

    reader_id = "reader.document"
    reader_version = "1.0.0"

    async def read(self, context: ReaderContext) -> ReaderPayload:
        """调用禁用插件和网络入口的本地文档 worker。"""
        raw_output = await run_reader_worker(context, mode="document")
        output = parse_worker_output(raw_output)
        warnings = list(output.warnings)
        status = ReaderStatus.SUCCESS
        support_level = SupportLevel.NATIVE
        if not output.content.strip():
            status = ReaderStatus.DEGRADED
            support_level = SupportLevel.FALLBACK
            warnings.append("reader.document.no_extractable_text")
            if context.inspection.detected_format == "pdf":
                warnings.append("reader.pdf.ocr_unavailable")
        elif context.request.enable_ocr:
            warnings.append("reader.document.ocr_not_configured")
            status = ReaderStatus.DEGRADED
        return ReaderPayload(
            status=ReaderStatus.TRUNCATED if output.truncated else status,
            support_level=support_level,
            content=output.content,
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
