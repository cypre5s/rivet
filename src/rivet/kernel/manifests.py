"""从静态 TOML 加载模块 Manifest，且不导入 factory。"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from rivet.contracts.modules import ModuleManifest
from rivet.kernel.errors import ManifestError


class ManifestLoader:
    """执行确定性文件读取、容器归一化与严格契约校验。"""

    def load_paths(self, paths: Iterable[Path]) -> tuple[ModuleManifest, ...]:
        """按路径排序加载并拒绝重复模块 ID。"""
        manifests: list[ModuleManifest] = []
        module_paths: dict[str, Path] = {}
        for path in sorted((candidate.resolve() for candidate in paths), key=str):
            manifest = self._load_path(path)
            previous_path = module_paths.get(manifest.module_id)
            if previous_path is not None:
                raise ManifestError(
                    f"模块 {manifest.module_id} 在 {previous_path.name} "
                    f"与 {path.name} 重复声明"
                )
            module_paths[manifest.module_id] = path
            manifests.append(manifest)
        return tuple(manifests)

    def load_directory(self, directory: Path) -> tuple[ModuleManifest, ...]:
        """只读取目录第一层按名称排序的 TOML 文件。"""
        resolved_directory = directory.resolve()
        if not resolved_directory.is_dir():
            raise ManifestError(f"Manifest 目录不存在：{resolved_directory}")
        return self.load_paths(resolved_directory.glob("*.toml"))

    def _load_path(self, path: Path) -> ModuleManifest:
        """解析单个 TOML，并把数组显式冻结为契约元组。"""
        if path.suffix != ".toml" or not path.is_file():
            raise ManifestError(f"Manifest 必须是 TOML 文件：{path.name}")
        try:
            raw_document = cast(
                dict[str, object],
                tomllib.loads(path.read_text(encoding="utf-8")),
            )
            normalized = dict(raw_document)
            for field_name in ("provides", "requires"):
                value = normalized.get(field_name)
                if isinstance(value, list):
                    normalized[field_name] = tuple(cast(list[object], value))
            return ModuleManifest.model_validate(normalized)
        except (
            OSError,
            UnicodeError,
            ValueError,
            tomllib.TOMLDecodeError,
            ValidationError,
        ) as error:
            raise ManifestError(f"Manifest {path.name} 校验失败") from error
