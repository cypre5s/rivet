"""读取 RFC 822 邮件并隔离 Outlook MSG 转换。"""

from __future__ import annotations

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

from pydantic import JsonValue

from rivet.contracts.readers import ReaderStatus, SupportLevel

from .base import ReaderContext, ReaderError, ReaderPayload
from .text_reader import whole_source_span
from .worker_protocol import parse_worker_output, run_reader_worker


def _body_text(message: EmailMessage) -> str:
    """优先选择纯文本正文，并限制单个正文长度。"""
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        content = body.get_content()
    except (LookupError, UnicodeError) as error:
        raise ReaderError(
            "reader.email.body_decode_failed", "邮件正文无法解码"
        ) from error
    return content if isinstance(content, str) else ""


class EmailReader:
    """抽取 EML 头部、正文和附件清单，MSG 使用 MarkItDown worker。"""

    reader_id = "reader.email"
    reader_version = "1.0.0"

    async def read(self, context: ReaderContext) -> ReaderPayload:
        """按检测格式选择标准库 EML 或隔离 MSG 转换。"""
        if context.inspection.detected_format == "msg":
            output = parse_worker_output(
                await run_reader_worker(context, mode="document")
            )
            return ReaderPayload(
                status=ReaderStatus.SUCCESS
                if output.content
                else ReaderStatus.DEGRADED,
                support_level=SupportLevel.NATIVE
                if output.content
                else SupportLevel.FALLBACK,
                content=output.content,
                metadata=output.metadata,
                warnings=output.warnings
                if output.content
                else ("reader.email.msg_no_content",),
                truncated=output.truncated,
            )
        try:
            parsed = BytesParser(policy=policy.default).parsebytes(
                context.inspection.absolute_path.read_bytes()
            )
        except (OSError, ValueError) as error:
            raise ReaderError("reader.email.invalid_eml", "EML 无法解析") from error
        message = parsed
        headers: dict[str, JsonValue] = {
            name: str(message.get(name, ""))[:4_096]
            for name in ("subject", "from", "to", "date", "message-id")
        }
        attachments: list[JsonValue] = []
        for part in message.iter_attachments():
            payload = part.get_payload(decode=True)
            attachments.append(
                {
                    "filename": part.get_filename() or "",
                    "media_type": part.get_content_type(),
                    "size_bytes": len(payload) if isinstance(payload, bytes) else 0,
                }
            )
        body = _body_text(message)
        heading = "\n".join(
            f"{name}: {value}" for name, value in headers.items() if value
        )
        content = f"{heading}\n\n{body}".strip() + "\n"
        source_text = context.inspection.absolute_path.read_text(
            encoding="utf-8", errors="replace"
        )
        return ReaderPayload(
            status=ReaderStatus.SUCCESS,
            support_level=SupportLevel.NATIVE,
            content=content,
            metadata={"headers": headers, "attachments": attachments},
            source_spans=whole_source_span(context.inspection.source_path, source_text),
        )
