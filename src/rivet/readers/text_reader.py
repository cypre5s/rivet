"""以明确编码和 NUL 边界读取文本文件。"""

from __future__ import annotations

from rivet.contracts.common import SourceSpan
from rivet.contracts.readers import ReaderStatus, SupportLevel

from .base import ReaderContext, ReaderError, ReaderPayload


def decode_text_bytes(content: bytes) -> tuple[str, str]:
    """按 BOM、UTF-8、CP1252 顺序解码并拒绝二进制 NUL。"""
    if content.startswith(b"\xff\xfe"):
        return content[2:].decode("utf-16-le", errors="strict"), "utf-16-le"
    if content.startswith(b"\xfe\xff"):
        return content[2:].decode("utf-16-be", errors="strict"), "utf-16-be"
    if content.startswith(b"\xef\xbb\xbf"):
        return content[3:].decode("utf-8", errors="strict"), "utf-8-sig"
    if b"\x00" in content:
        raise ReaderError("reader.text.nul_detected", "文本来源包含 NUL")
    try:
        return content.decode("utf-8", errors="strict"), "utf-8"
    except UnicodeDecodeError:
        try:
            return content.decode("cp1252", errors="strict"), "cp1252"
        except UnicodeDecodeError as error:
            raise ReaderError(
                "reader.text.encoding_unknown", "无法确定文本编码"
            ) from error


def whole_source_span(source_path: str, content: str) -> tuple[SourceSpan, ...]:
    """为文本结果建立覆盖全部来源行的保守区间。"""
    line_count = max(1, content.count("\n") + (not content.endswith("\n")))
    return (
        SourceSpan(
            repository_path=source_path,
            start_line=1,
            end_line=int(line_count),
        ),
    )


class TextReader:
    """读取常见源码、配置片段和普通文本。"""

    reader_id = "reader.text"
    reader_version = "1.0.0"

    async def read(self, context: ReaderContext) -> ReaderPayload:
        """在文件大小门禁后解码完整文本并记录来源行。"""
        content_bytes = context.inspection.absolute_path.read_bytes()
        text, encoding = decode_text_bytes(content_bytes)
        return ReaderPayload(
            status=ReaderStatus.SUCCESS,
            support_level=SupportLevel.NATIVE,
            content=text,
            metadata={
                "encoding": encoding,
                "line_count": max(1, len(text.splitlines())),
            },
            source_spans=whole_source_span(context.inspection.source_path, text),
        )
