"""解析仓库 `.rivet` 状态目录与 XDG 可重建缓存目录。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from rivet.trace.errors import RuntimePathError


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """集中描述 Trace 和状态存储使用的所有本地路径。"""

    repository_root: Path
    runtime_root: Path
    database_path: Path
    events_path: Path
    artifacts_root: Path
    cache_root: Path
    module_database_path: Path

    @classmethod
    def for_repository(
        cls,
        repository_root: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> RuntimePaths:
        """只解析路径，不在构造阶段创建目录。"""
        resolved_repository = repository_root.resolve()
        selected_environment = os.environ if environment is None else environment
        xdg_cache_value = selected_environment.get("XDG_CACHE_HOME")
        if xdg_cache_value:
            xdg_cache_root = Path(xdg_cache_value)
            if not xdg_cache_root.is_absolute():
                raise RuntimePathError("XDG_CACHE_HOME 必须是绝对路径")
        else:
            xdg_cache_root = Path.home() / ".cache"
        xdg_state_value = selected_environment.get("XDG_STATE_HOME")
        if xdg_state_value:
            xdg_state_root = Path(xdg_state_value)
            if not xdg_state_root.is_absolute():
                raise RuntimePathError("XDG_STATE_HOME 必须是绝对路径")
        else:
            xdg_state_root = Path.home() / ".local" / "state"
        runtime_root = resolved_repository / ".rivet"
        return cls(
            repository_root=resolved_repository,
            runtime_root=runtime_root,
            database_path=runtime_root / "state.sqlite3",
            events_path=runtime_root / "trace" / "events.ndjson",
            artifacts_root=runtime_root / "artifacts",
            cache_root=xdg_cache_root.resolve() / "rivet",
            module_database_path=(
                xdg_state_root.resolve() / "rivet" / "module-state.sqlite3"
            ),
        )

    def prepare(self) -> None:
        """显式创建私有运行目录，并拒绝不存在的仓库根。"""
        if not self.repository_root.is_dir():
            raise RuntimePathError(f"仓库目录不存在：{self.repository_root}")
        for directory in (
            self.runtime_root,
            self.database_path.parent,
            self.events_path.parent,
            self.artifacts_root,
            self.cache_root,
            self.module_database_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
