"""编排路径授权、格式检测、Reader 激活和结构化降级。"""

from __future__ import annotations

from pathlib import Path

from rivet.contracts.readers import (
    ReaderRequest,
    ReaderResult,
    ReaderStatus,
    SupportLevel,
)
from rivet.kernel.resources import ResourceScope
from rivet.tools.paths import WorkspaceBoundary

from .base import ReaderContext, ReaderError, ReaderPayload
from .detection import detect_file
from .registry import ReaderRegistry


class ReaderService:
    """确保任意已授权普通文件得到显式且不可信的 ReaderResult。"""

    def __init__(
        self,
        repository_root: Path,
        *,
        scope: ResourceScope,
        registry: ReaderRegistry | None = None,
    ) -> None:
        self._boundary = WorkspaceBoundary(repository_root)
        self._repository_root = self._boundary.repository_root
        self._scope = scope
        self._registry = registry or ReaderRegistry.load_builtin()

    @property
    def active_reader_ids(self) -> tuple[str, ...]:
        """返回此次服务实际激活过的 Reader。"""
        return self._registry.active_reader_ids

    async def read(self, request: ReaderRequest) -> ReaderResult:
        """读取一个来源并将所有预期解析失败转换为稳定状态。"""
        absolute_path = self._boundary.resolve_repository(
            request.source_path,
            require_file=True,
        )
        inspection = detect_file(absolute_path, source_path=request.source_path)
        if inspection.size_bytes > request.max_bytes:
            payload = ReaderPayload(
                status=ReaderStatus.FAILED,
                support_level=SupportLevel.FALLBACK,
                metadata={"size_bytes": inspection.size_bytes},
                warnings=("reader.file.size_exceeded",),
            )
            reader_id = inspection.capability_id
            reader_version = "1.0.0"
        else:
            reader = self._registry.resolve(inspection)
            reader_id = reader.reader_id
            reader_version = reader.reader_version
            try:
                payload = await reader.read(
                    ReaderContext(
                        inspection=inspection,
                        request=request,
                        scope=self._scope,
                        repository_root=self._repository_root,
                    )
                )
            except ReaderError as error:
                payload = ReaderPayload(
                    status=ReaderStatus.FAILED,
                    support_level=SupportLevel.FALLBACK,
                    warnings=(error.code,),
                )
            except (OSError, UnicodeError, ValueError) as error:
                del error
                payload = ReaderPayload(
                    status=ReaderStatus.FAILED,
                    support_level=SupportLevel.FALLBACK,
                    warnings=("reader.parse.failed",),
                )
        content = payload.content
        truncated = payload.truncated
        status = payload.status
        warnings = list(inspection.warnings) + list(payload.warnings)
        if len(content) > request.max_output_chars:
            content = content[: request.max_output_chars]
            truncated = True
            status = ReaderStatus.TRUNCATED
            warnings.append("reader.output.truncated")
        metadata = {
            "size_bytes": inspection.size_bytes,
            "magic_hex": inspection.magic_hex,
            "capability_id": inspection.capability_id,
            **payload.metadata,
        }
        return ReaderResult(
            status=status,
            source_path=request.source_path,
            media_type=inspection.media_type,
            detected_format=inspection.detected_format,
            reader_id=reader_id,
            reader_version=reader_version,
            support_level=payload.support_level,
            content=content,
            metadata=metadata,
            warnings=tuple(dict.fromkeys(warnings)),
            truncated=truncated,
            source_sha256=inspection.source_sha256,
            source_spans=payload.source_spans,
        )
