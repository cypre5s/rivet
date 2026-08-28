"""通过受控 ownership marker 清理可重建资源。"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

MARKER_NAME = ".rivet-owner.json"
MAX_MARKER_BYTES = 16 * 1024
RESOURCE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")


class OwnershipKind(StrEnum):
    """区分可清理缓存与必须保留的持久状态。"""

    TEMPORARY = "TEMPORARY"
    WORKTREE_ORPHAN = "WORKTREE_ORPHAN"
    SIDECAR_CACHE = "SIDECAR_CACHE"
    SESSION = "SESSION"
    EVIDENCE = "EVIDENCE"


REMOVABLE_KINDS = frozenset(
    {
        OwnershipKind.TEMPORARY,
        OwnershipKind.WORKTREE_ORPHAN,
        OwnershipKind.SIDECAR_CACHE,
    }
)


@dataclass(frozen=True, slots=True)
class CleanReport:
    """返回确定排序的候选、已删除与保留路径。"""

    candidates: tuple[Path, ...]
    removed: tuple[Path, ...]
    preserved: tuple[Path, ...]

    def public_mapping(self) -> dict[str, object]:
        """生成 CLI 可直接 JSON 序列化的报告。"""
        return {
            "candidates": [str(path) for path in self.candidates],
            "removed": [str(path) for path in self.removed],
            "preserved": [str(path) for path in self.preserved],
        }


def write_ownership(
    directory: Path,
    *,
    kind: OwnershipKind,
    resource_id: str,
) -> Path:
    """在已存在普通目录中原子写入不含用户数据的所有权标记。"""
    if not RESOURCE_ID_PATTERN.fullmatch(resource_id):
        raise ValueError("ownership resource_id 无效")
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("ownership 目标必须是普通目录")
    marker = directory / MARKER_NAME
    if marker.is_symlink():
        raise ValueError("ownership marker 不得是符号链接")
    content = (
        json.dumps(
            {
                "kind": kind.value,
                "owner": "rivet",
                "resource_id": resource_id,
                "schema_version": 1,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=directory,
        prefix=".rivet-owner.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, marker)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return marker


class SafeCleaner:
    """仅扫描给定根的直接子目录且绝不跟随符号链接。"""

    def __init__(self, cache_root: Path) -> None:
        self._cache_root = cache_root.absolute()

    def clean(self, *, dry_run: bool = False) -> CleanReport:
        """删除标记为可重建的直接子目录，其他路径全部保留。"""
        if not self._cache_root.exists():
            return CleanReport((), (), ())
        if self._cache_root.is_symlink() or not self._cache_root.is_dir():
            raise ValueError("clean 根必须是普通目录")
        candidates: list[Path] = []
        preserved: list[Path] = []
        for child in sorted(self._cache_root.iterdir(), key=lambda path: path.name):
            if child.is_symlink() or not child.is_dir():
                preserved.append(child)
                continue
            kind = self._read_kind(child)
            if kind in REMOVABLE_KINDS:
                candidates.append(child)
            else:
                preserved.append(child)
        removed: list[Path] = []
        if not dry_run:
            for candidate in candidates:
                self._remove_candidate(candidate)
                removed.append(candidate)
        return CleanReport(tuple(candidates), tuple(removed), tuple(preserved))

    def _read_kind(self, directory: Path) -> OwnershipKind | None:
        """严格读取 marker，损坏或未知标记一律失败关闭为保留。"""
        marker = directory / MARKER_NAME
        if marker.is_symlink() or not marker.is_file():
            return None
        try:
            if marker.stat().st_size > MAX_MARKER_BYTES:
                return None
            raw_document = cast(object, json.loads(marker.read_bytes()))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw_document, dict):
            return None
        document = cast(dict[str, object], raw_document)
        if set(document) != {
            "kind",
            "owner",
            "resource_id",
            "schema_version",
        }:
            return None
        resource_id = document.get("resource_id")
        if (
            document.get("schema_version") != 1
            or document.get("owner") != "rivet"
            or not isinstance(resource_id, str)
            or not RESOURCE_ID_PATTERN.fullmatch(resource_id)
        ):
            return None
        try:
            return OwnershipKind(cast(str, document.get("kind")))
        except (TypeError, ValueError):
            return None

    def _remove_candidate(self, candidate: Path) -> None:
        """删除前再次校验父级、链接和 marker，缩小竞态窗口。"""
        if candidate.parent != self._cache_root or candidate.is_symlink():
            raise ValueError("clean 候选路径越界")
        if self._read_kind(candidate) not in REMOVABLE_KINDS:
            raise ValueError("clean 候选 ownership 已变化")
        shutil.rmtree(candidate)
