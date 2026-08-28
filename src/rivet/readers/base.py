"""定义 Reader 实现与编排之间的轻量内部契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import JsonValue

from rivet.contracts.common import CapabilityId, MediaType, Sha256Digest, SourceSpan
from rivet.contracts.readers import ReaderRequest, ReaderStatus, SupportLevel
from rivet.kernel.resources import ResourceScope


class ReaderError(RuntimeError):
    """表示可安全转换为结构化失败结果的 Reader 错误。"""

    def __init__(self, code: str, summary: str) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary


@dataclass(frozen=True, slots=True)
class FileInspection:
    """保存单次检测产生的格式、来源和哈希事实。"""

    source_path: str
    absolute_path: Path
    size_bytes: int
    source_sha256: Sha256Digest
    media_type: MediaType
    detected_format: str
    capability_id: CapabilityId
    magic_hex: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReaderContext:
    """向唯一 Reader 提供已授权来源、预算和资源域。"""

    inspection: FileInspection
    request: ReaderRequest
    scope: ResourceScope
    repository_root: Path


@dataclass(frozen=True, slots=True)
class ReaderPayload:
    """保存 Reader 私有实现返回、尚未补齐公共来源字段的内容。"""

    status: ReaderStatus
    support_level: SupportLevel
    content: str = ""
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    source_spans: tuple[SourceSpan, ...] = ()
    truncated: bool = False


class Reader(Protocol):
    """描述可由 Registry 延迟构造的异步 Reader。"""

    reader_id: CapabilityId
    reader_version: str

    async def read(self, context: ReaderContext) -> ReaderPayload:
        """读取单个已检测来源并返回有界载荷。"""
        ...
