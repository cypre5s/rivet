"""为未知二进制返回有限 strings 与明确不支持状态。"""

from __future__ import annotations

import re

from rivet.contracts.readers import ReaderStatus, SupportLevel

from .base import ReaderContext, ReaderPayload

MAX_STRING_COUNT = 100
MAX_PREVIEW_BYTES = 256 * 1024


class BinaryFallbackReader:
    """只提供可验证元数据和有限可打印字符串。"""

    reader_id = "reader.binary"
    reader_version = "1.0.0"

    async def read(self, context: ReaderContext) -> ReaderPayload:
        """扫描有界前缀，不将随机字节标记为可信正文。"""
        with context.inspection.absolute_path.open("rb") as stream:
            preview = stream.read(MAX_PREVIEW_BYTES)
        strings = [
            match.decode("ascii", errors="strict")[:1_024]
            for match in re.findall(rb"[\x20-\x7e]{4,}", preview)[:MAX_STRING_COUNT]
        ]
        content = "\n".join(strings)
        if content:
            content += "\n"
        return ReaderPayload(
            status=ReaderStatus.UNSUPPORTED,
            support_level=SupportLevel.UNSUPPORTED,
            content=content,
            metadata={
                "strings_count": len(strings),
                "preview_bytes": len(preview),
            },
            warnings=("reader.binary.unsupported_content",),
        )
