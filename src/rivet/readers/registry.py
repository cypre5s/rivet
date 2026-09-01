"""从静态描述符按需导入唯一 Reader 实现。"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import cast

from rivet.contracts.common import CapabilityId

from .base import FileInspection, Reader, ReaderError


@dataclass(frozen=True, slots=True)
class ReaderDescriptor:
    """保存启动时可解析且不导入实现的 Reader Manifest。"""

    capability_id: CapabilityId
    factory: str
    priority: int


BUILTIN_DESCRIPTORS = (
    ReaderDescriptor("reader.text", "rivet.readers.text_reader:TextReader", 100),
    ReaderDescriptor(
        "reader.structured", "rivet.readers.structured_reader:StructuredReader", 100
    ),
    ReaderDescriptor(
        "reader.notebook", "rivet.readers.notebook_reader:NotebookReader", 100
    ),
    ReaderDescriptor(
        "reader.document", "rivet.readers.document_reader:DocumentReader", 100
    ),
    ReaderDescriptor("reader.image", "rivet.readers.image_reader:ImageReader", 100),
    ReaderDescriptor("reader.media", "rivet.readers.media_reader:MediaReader", 100),
    ReaderDescriptor(
        "reader.archive", "rivet.readers.archive_reader:ArchiveReader", 100
    ),
    ReaderDescriptor(
        "reader.archive.sevenzip",
        "rivet.readers.archive_reader:SevenZipReader",
        100,
    ),
    ReaderDescriptor("reader.email", "rivet.readers.email_reader:EmailReader", 100),
    ReaderDescriptor(
        "reader.binary", "rivet.readers.binary_reader:BinaryFallbackReader", 0
    ),
)


class ReaderRegistry:
    """验证 capability 唯一提供者并延迟构造命中的 Reader。"""

    def __init__(self, descriptors: tuple[ReaderDescriptor, ...]) -> None:
        if not descriptors:
            raise ReaderError("reader.registry.empty", "Reader Registry 不得为空")
        by_capability: dict[str, ReaderDescriptor] = {}
        for descriptor in sorted(
            descriptors, key=lambda item: (-item.priority, item.capability_id)
        ):
            if descriptor.capability_id in by_capability:
                raise ReaderError(
                    "reader.registry.duplicate_capability",
                    f"重复 Reader capability：{descriptor.capability_id}",
                )
            by_capability[descriptor.capability_id] = descriptor
        self._descriptors = tuple(by_capability.values())
        self._by_capability = by_capability
        self._active: dict[str, Reader] = {}
        self._activation_order: list[str] = []

    @classmethod
    def load_builtin(cls) -> ReaderRegistry:
        """加载只包含静态字符串的内置描述符。"""
        return cls(BUILTIN_DESCRIPTORS)

    @property
    def descriptors(self) -> tuple[ReaderDescriptor, ...]:
        """返回按优先级和 capability 稳定排序的描述符。"""
        return self._descriptors

    @property
    def active_reader_ids(self) -> tuple[str, ...]:
        """按首次激活顺序返回已构造 Reader。"""
        return tuple(self._activation_order)

    def resolve(self, inspection: FileInspection) -> Reader:
        """按检测 capability 激活唯一实现并缓存轻量实例。"""
        capability_id = inspection.capability_id
        active = self._active.get(capability_id)
        if active is not None:
            return active
        descriptor = self._by_capability.get(capability_id)
        if descriptor is None:
            raise ReaderError(
                "reader.registry.capability_missing", "检测结果没有 Reader 提供者"
            )
        module_name, separator, attribute_name = descriptor.factory.partition(":")
        if not separator or not module_name or not attribute_name:
            raise ReaderError("reader.registry.factory_invalid", "Reader factory 无效")
        try:
            factory = getattr(import_module(module_name), attribute_name)
            reader = cast(Reader, factory())
        except (AttributeError, ImportError, TypeError) as error:
            raise ReaderError(
                "reader.registry.activation_failed", "Reader 无法激活"
            ) from error
        if reader.reader_id != capability_id:
            raise ReaderError(
                "reader.registry.identity_mismatch", "Reader ID 与 capability 不一致"
            )
        self._active[capability_id] = reader
        self._activation_order.append(capability_id)
        return reader
