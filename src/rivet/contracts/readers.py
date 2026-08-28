"""定义多格式 Reader 的请求、分级支持与不可信输出契约。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, JsonValue

from rivet.contracts.common import (
    ArtifactReference,
    CapabilityId,
    ContractModel,
    MediaType,
    RepositoryPath,
    SemVer,
    Sha256Digest,
    SourceSpan,
)


class ReaderStatus(StrEnum):
    """区分成功、降级、不支持、截断和失败读取。"""

    SUCCESS = "SUCCESS"
    DEGRADED = "DEGRADED"
    UNSUPPORTED = "UNSUPPORTED_CONTENT"
    TRUNCATED = "TRUNCATED"
    FAILED = "FAILED"


class SupportLevel(StrEnum):
    """表示 A 原生抽取、B 通用降级和 C 明确不支持。"""

    NATIVE = "A"
    FALLBACK = "B"
    UNSUPPORTED = "C"


class ReaderRequest(ContractModel):
    """限定 Reader 输入路径、大小、时间与递归预算。"""

    source_path: RepositoryPath
    max_bytes: int = Field(default=50 * 1024 * 1024, gt=0, le=50 * 1024 * 1024)
    timeout_seconds: int = Field(default=30, gt=0)
    max_depth: int = Field(default=3, ge=0, le=3)
    max_output_chars: int = Field(default=1_000_000, gt=0, le=4_000_000)
    max_archive_entries: int = Field(default=1_000, gt=0, le=1_000)
    max_expanded_bytes: int = Field(
        default=200 * 1024 * 1024,
        gt=0,
        le=200 * 1024 * 1024,
    )
    max_compression_ratio: float = Field(default=100.0, gt=1.0, le=1_000.0)
    max_ocr_pages: int = Field(default=100, ge=0, le=100)
    max_video_frames: int = Field(default=20, ge=0, le=20)
    enable_ocr: bool = False
    enable_transcription: bool = False
    preferred_capability: CapabilityId | None = None


class ReaderResult(ContractModel):
    """为任意文件返回结构化状态、内容、来源和警告。"""

    status: ReaderStatus
    source_path: RepositoryPath
    media_type: MediaType
    detected_format: str = Field(min_length=1, max_length=128)
    reader_id: CapabilityId
    reader_version: SemVer
    support_level: SupportLevel
    content: str = Field(default="", max_length=4_000_000)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    artifacts: tuple[ArtifactReference, ...] = ()
    truncated: bool = False
    source_sha256: Sha256Digest
    source_spans: tuple[SourceSpan, ...] = ()
    untrusted: Literal[True] = True
