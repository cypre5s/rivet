"""从静态 TOML 发现语言服务而不启动任何进程。"""

from __future__ import annotations

import shutil
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import cast


class LspManifestError(RuntimeError):
    """表示语言服务 Manifest 缺失、冲突或字段无效。"""


class LspServerUnavailableError(LspManifestError):
    """表示已知语言服务没有可执行候选。"""


@dataclass(frozen=True, slots=True)
class LspServerManifest:
    """描述一个可按后缀发现和启动的语言 sidecar。"""

    server_id: str
    language_ids: tuple[str, ...]
    suffixes: tuple[str, ...]
    executable_candidates: tuple[str, ...]
    arguments: tuple[str, ...]
    initialization_options: Mapping[str, object]
    idle_timeout_seconds: float = 300.0
    request_timeout_seconds: float = 10.0
    max_restarts: int = 1
    repository_root: Path | None = None

    def __post_init__(self) -> None:
        if (
            not self.server_id
            or not self.language_ids
            or not self.suffixes
            or not self.executable_candidates
            or self.idle_timeout_seconds < 0
            or self.request_timeout_seconds <= 0
            or self.max_restarts < 0
        ):
            raise ValueError("LSP Manifest 字段无效")
        normalized_suffixes = tuple(suffix.casefold() for suffix in self.suffixes)
        if any(not suffix.startswith(".") for suffix in normalized_suffixes):
            raise ValueError("LSP suffix 必须以点开头")
        if len(set(normalized_suffixes)) != len(normalized_suffixes):
            raise ValueError("LSP Manifest 不得重复 suffix")

    def resolve_executable(self) -> Path:
        """按声明顺序解析绝对、仓库相对或 PATH 可执行文件。"""
        for candidate in self.executable_candidates:
            candidate_path = Path(candidate)
            resolved: Path | None = None
            if candidate_path.is_absolute():
                resolved = candidate_path
            elif "/" in candidate or "\\" in candidate:
                if self.repository_root is not None:
                    resolved = self.repository_root / candidate_path
            else:
                located = shutil.which(candidate)
                if located is not None:
                    resolved = Path(located)
            if (
                resolved is not None
                and resolved.is_file()
                and resolved.stat().st_mode & 0o111
            ):
                return resolved.resolve()
        raise LspServerUnavailableError(f"语言服务 {self.server_id} 没有可用可执行文件")

    def command(self) -> tuple[str, ...]:
        """返回固定 executable 加 arguments 的无 shell argv。"""
        return (str(self.resolve_executable()), *self.arguments)

    def language_id_for_path(self, path: str) -> str:
        """把官方后缀映射到 LSP languageId。"""
        suffix = PurePosixPath(path).suffix.casefold()
        known = {
            ".py": "python",
            ".pyi": "python",
            ".ts": "typescript",
            ".tsx": "typescriptreact",
            ".js": "javascript",
            ".jsx": "javascriptreact",
        }
        language_id = known.get(suffix)
        if language_id is None or language_id not in self.language_ids:
            raise LspManifestError(f"语言服务 {self.server_id} 不支持路径 {path}")
        return language_id


class LspManifestRegistry:
    """验证 suffix 唯一提供者并提供确定性发现。"""

    def __init__(self, manifests: tuple[LspServerManifest, ...]) -> None:
        if not manifests:
            raise LspManifestError("至少需要一个 LSP Manifest")
        by_suffix: dict[str, LspServerManifest] = {}
        by_id: dict[str, LspServerManifest] = {}
        for manifest in sorted(manifests, key=lambda item: item.server_id):
            if manifest.server_id in by_id:
                raise LspManifestError(f"重复 LSP server_id：{manifest.server_id}")
            by_id[manifest.server_id] = manifest
            for suffix in manifest.suffixes:
                normalized = suffix.casefold()
                if normalized in by_suffix:
                    raise LspManifestError(f"重复 LSP suffix：{normalized}")
                by_suffix[normalized] = manifest
        self._manifests = tuple(by_id.values())
        self._by_suffix = by_suffix

    @classmethod
    def load_builtin(cls, *, repository_root: Path) -> LspManifestRegistry:
        """读取包内静态 TOML，不导入或启动语言服务。"""
        manifest_root = files("rivet.context.lsp_manifests")
        manifests: list[LspServerManifest] = []
        for resource in sorted(manifest_root.iterdir(), key=lambda item: item.name):
            if not resource.name.endswith(".toml"):
                continue
            with resource.open("rb") as stream:
                raw = cast(dict[str, object], tomllib.load(stream))
            manifests.append(cls._parse(raw, repository_root=repository_root))
        return cls(tuple(manifests))

    @property
    def manifests(self) -> tuple[LspServerManifest, ...]:
        """返回按 server_id 稳定排序的静态清单。"""
        return self._manifests

    def for_path(self, path: str) -> LspServerManifest:
        """按小写后缀返回唯一语言服务。"""
        suffix = PurePosixPath(path).suffix.casefold()
        manifest = self._by_suffix.get(suffix)
        if manifest is None:
            raise LspManifestError(f"没有 LSP Manifest 支持 {path}")
        return manifest

    @staticmethod
    def _parse(raw: dict[str, object], *, repository_root: Path) -> LspServerManifest:
        """严格收窄 TOML 基础字段并冻结集合。"""
        try:
            server_id = raw["server_id"]
            language_ids = raw["language_ids"]
            suffixes = raw["suffixes"]
            executable_candidates = raw["executable_candidates"]
            arguments = raw["arguments"]
            initialization_options = raw.get("initialization_options", {})
            idle_timeout_seconds = raw.get("idle_timeout_seconds", 300.0)
            request_timeout_seconds = raw.get("request_timeout_seconds", 10.0)
            max_restarts = raw.get("max_restarts", 1)
            if not isinstance(server_id, str):
                raise TypeError
            if not all(
                isinstance(value, list)
                and all(isinstance(item, str) for item in cast(list[object], value))
                for value in (
                    language_ids,
                    suffixes,
                    executable_candidates,
                    arguments,
                )
            ):
                raise TypeError
            if not isinstance(initialization_options, dict):
                raise TypeError
            if isinstance(idle_timeout_seconds, bool) or not isinstance(
                idle_timeout_seconds, int | float
            ):
                raise TypeError
            if isinstance(request_timeout_seconds, bool) or not isinstance(
                request_timeout_seconds, int | float
            ):
                raise TypeError
            if isinstance(max_restarts, bool) or not isinstance(max_restarts, int):
                raise TypeError
            return LspServerManifest(
                server_id=server_id,
                language_ids=tuple(cast(list[str], language_ids)),
                suffixes=tuple(cast(list[str], suffixes)),
                executable_candidates=tuple(cast(list[str], executable_candidates)),
                arguments=tuple(cast(list[str], arguments)),
                initialization_options=cast(dict[str, object], initialization_options),
                idle_timeout_seconds=float(idle_timeout_seconds),
                request_timeout_seconds=float(request_timeout_seconds),
                max_restarts=max_restarts,
                repository_root=repository_root.resolve(strict=True),
            )
        except (KeyError, TypeError, ValueError, OSError) as error:
            raise LspManifestError("内置 LSP Manifest 校验失败") from error
