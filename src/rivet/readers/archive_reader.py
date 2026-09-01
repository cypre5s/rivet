"""以内存扫描和严格预算读取 ZIP、TAR、TGZ 与 7z。"""

from __future__ import annotations

import io
import stat
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from pydantic import JsonValue

from rivet.contracts.common import SourceSpan
from rivet.contracts.readers import ReaderStatus, SupportLevel

from .base import ReaderContext, ReaderError, ReaderPayload
from .worker_protocol import parse_worker_output, run_reader_worker

MAX_MEMBER_PREVIEW_BYTES = 1_000_000


@dataclass(slots=True)
class _ArchiveState:
    """累计跨嵌套归档的条目、展开大小和文本区段。"""

    entry_count: int = 0
    expanded_bytes: int = 0
    sections: list[str] = field(default_factory=list)


def _safe_name(name: str) -> PurePosixPath:
    """拒绝跨平台路径穿越、绝对路径、空段和 NUL。"""
    if "\\" in name or "\x00" in name:
        raise ReaderError("reader.archive.path_forbidden", "归档路径无效")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or not path.parts
        or path.parts[0].endswith(":")
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ReaderError("reader.archive.path_forbidden", "归档路径越界")
    return path


def _add_budget(
    context: ReaderContext,
    state: _ArchiveState,
    *,
    expanded_bytes: int,
) -> None:
    """累计条目与展开大小并执行冻结上限。"""
    state.entry_count += 1
    state.expanded_bytes += expanded_bytes
    if state.entry_count > context.request.max_archive_entries:
        raise ReaderError("reader.archive.entry_limit_exceeded", "归档条目数超限")
    if state.expanded_bytes > context.request.max_expanded_bytes:
        raise ReaderError("reader.archive.expanded_size_exceeded", "归档展开大小超限")


def _append_preview(state: _ArchiveState, name: str, content: bytes) -> None:
    """只将小型 UTF-8 无 NUL成员加入不可信正文。"""
    if len(content) > MAX_MEMBER_PREVIEW_BYTES or b"\x00" in content:
        return
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return
    state.sections.append(f"## {name}\n{text[:65_536]}")


def _scan_nested(
    content: bytes,
    *,
    name: str,
    depth: int,
    context: ReaderContext,
    state: _ArchiveState,
) -> bool:
    """识别嵌套 ZIP/TAR 并在递归前执行深度门禁。"""
    lowered = name.casefold()
    is_zip = content.startswith(b"PK\x03\x04") and lowered.endswith(".zip")
    is_tar = lowered.endswith((".tar", ".tar.gz", ".tgz"))
    if not is_zip and not is_tar:
        return False
    if depth >= context.request.max_depth:
        raise ReaderError("reader.archive.depth_exceeded", "归档递归深度超限")
    if is_zip:
        _scan_zip(io.BytesIO(content), depth=depth + 1, context=context, state=state)
    else:
        _scan_tar(io.BytesIO(content), depth=depth + 1, context=context, state=state)
    return True


def _scan_zip(
    source: io.BytesIO,
    *,
    depth: int,
    context: ReaderContext,
    state: _ArchiveState,
) -> None:
    """预检 ZIP 路径、链接、大小和压缩比后读取成员。"""
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as error:
        raise ReaderError("reader.archive.invalid_zip", "ZIP 无法解析") from error
    with archive:
        for member in archive.infolist():
            name = _safe_name(member.filename)
            mode = (member.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ReaderError(
                    "reader.archive.symlink_forbidden", "ZIP 符号链接禁止"
                )
            _add_budget(context, state, expanded_bytes=member.file_size)
            if member.compress_size == 0:
                ratio = float("inf") if member.file_size else 1.0
            else:
                ratio = member.file_size / member.compress_size
            if ratio > context.request.max_compression_ratio:
                raise ReaderError(
                    "reader.archive.compression_ratio_exceeded", "ZIP 压缩比超限"
                )
            if member.is_dir():
                continue
            try:
                content = archive.read(member)
            except (OSError, RuntimeError, zipfile.BadZipFile) as error:
                raise ReaderError(
                    "reader.archive.invalid_zip", "ZIP 成员无法读取"
                ) from error
            if not _scan_nested(
                content,
                name=name.as_posix(),
                depth=depth,
                context=context,
                state=state,
            ):
                _append_preview(state, name.as_posix(), content)


def _scan_tar(
    source: io.BytesIO,
    *,
    depth: int,
    context: ReaderContext,
    state: _ArchiveState,
) -> None:
    """流式检查 TAR 节点类型，并拒绝全部链接和设备节点。"""
    try:
        with tarfile.open(fileobj=source, mode="r:*") as archive:
            for member in archive:
                name = _safe_name(member.name)
                if member.issym() or member.islnk():
                    raise ReaderError(
                        "reader.archive.symlink_forbidden", "TAR 链接禁止"
                    )
                if member.isdev() or member.isfifo():
                    raise ReaderError(
                        "reader.archive.special_file_forbidden", "TAR 特殊节点禁止"
                    )
                _add_budget(context, state, expanded_bytes=member.size)
                if not member.isfile():
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ReaderError("reader.archive.invalid_tar", "TAR 成员无法读取")
                content = extracted.read()
                if not _scan_nested(
                    content,
                    name=name.as_posix(),
                    depth=depth,
                    context=context,
                    state=state,
                ):
                    _append_preview(state, name.as_posix(), content)
    except (OSError, tarfile.TarError) as error:
        raise ReaderError("reader.archive.invalid_tar", "TAR 无法解析") from error


class ArchiveReader:
    """在不写出 ZIP/TAR 内容的前提下安全递归读取。"""

    reader_id = "reader.archive"
    reader_version = "1.0.0"

    async def read(self, context: ReaderContext) -> ReaderPayload:
        """对标准 ZIP/TAR 执行内存预检，不依赖任何可选包。"""
        state = _ArchiveState()
        source_bytes = context.inspection.absolute_path.read_bytes()
        if context.inspection.detected_format == "zip":
            _scan_zip(io.BytesIO(source_bytes), depth=0, context=context, state=state)
        elif context.inspection.detected_format in {"tar", "tar.gz"}:
            _scan_tar(io.BytesIO(source_bytes), depth=0, context=context, state=state)
        else:
            raise ReaderError("reader.archive.format_unknown", "归档格式未注册")
        if state.expanded_bytes / max(context.inspection.size_bytes, 1) > (
            context.request.max_compression_ratio
        ):
            raise ReaderError(
                "reader.archive.compression_ratio_exceeded", "归档总压缩比超限"
            )
        content = "\n\n".join(state.sections)
        if content:
            content += "\n"
        metadata: dict[str, JsonValue] = {
            "entry_count": state.entry_count,
            "expanded_bytes": state.expanded_bytes,
            "recursion_limit": context.request.max_depth,
        }
        return ReaderPayload(
            status=ReaderStatus.SUCCESS,
            support_level=SupportLevel.NATIVE,
            content=content,
            metadata=metadata,
            source_spans=(
                SourceSpan(
                    repository_path=context.inspection.source_path,
                    start_line=1,
                    end_line=1,
                ),
            ),
        )


class SevenZipReader:
    """只在 archive extra 可用时通过隔离 worker 读取 7z。"""

    reader_id = "reader.archive.sevenzip"
    reader_version = "1.0.0"

    async def read(self, context: ReaderContext) -> ReaderPayload:
        """把不可信 7z 解析限制在短生命周期子进程。"""
        output = parse_worker_output(
            await run_reader_worker(
                context,
                mode="sevenzip",
                arguments=(
                    "--max-entries",
                    str(context.request.max_archive_entries),
                    "--max-expanded-bytes",
                    str(context.request.max_expanded_bytes),
                    "--max-ratio",
                    str(context.request.max_compression_ratio),
                ),
            )
        )
        return ReaderPayload(
            status=(
                ReaderStatus.TRUNCATED if output.truncated else ReaderStatus.SUCCESS
            ),
            support_level=SupportLevel.NATIVE,
            content=output.content,
            metadata=output.metadata,
            warnings=output.warnings,
            source_spans=(
                SourceSpan(
                    repository_path=context.inspection.source_path,
                    start_line=1,
                    end_line=1,
                ),
            ),
            truncated=output.truncated,
        )
