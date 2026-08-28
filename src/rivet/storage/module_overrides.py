"""在现有 SQLite 状态库中保存模块启用策略覆盖。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from rivet.contracts.modules import (
    ModuleManifest,
    ModuleOverrideChange,
    ModuleScope,
)
from rivet.kernel.errors import ModuleOverridePersistenceError
from rivet.trace.database import TraceDatabase
from rivet.trace.errors import TraceDatabaseError


class ModuleOverrideStoreError(ModuleOverridePersistenceError):
    """表示模块覆盖无法安全读取或原子持久化。"""


class SQLiteModuleOverrideStore:
    """复用 Trace SQLite migration，并保持连接按操作有界。"""

    def __init__(self, database_path: Path, repository: Path) -> None:
        self._database_path = database_path.absolute()
        normalized_repository = str(repository.resolve(strict=True)).encode("utf-8")
        self.workspace_id = hashlib.sha256(normalized_repository).hexdigest()

    def load(self, manifests: tuple[ModuleManifest, ...]) -> dict[str, bool | None]:
        """按 Manifest scope 返回覆盖，缺失值保留为 None。"""
        database = self._open_database()
        try:
            try:
                rows = database.module_overrides(self.workspace_id)
            except TraceDatabaseError as error:
                raise ModuleOverrideStoreError("模块启用覆盖无法读取") from error
        finally:
            database.close()
        return {
            manifest.module_id: rows.get(
                (
                    manifest.scope.value,
                    manifest.module_id,
                )
            )
            for manifest in manifests
        }

    def set_many(self, changes: tuple[ModuleOverrideChange, ...]) -> None:
        """在单个 SQLite 事务中写入全部策略变化。"""
        rows = tuple(
            (
                change.scope.value,
                "" if change.scope is ModuleScope.APPLICATION else self.workspace_id,
                change.module_id,
                change.enabled,
                change.source,
            )
            for change in changes
        )
        database = self._open_database()
        try:
            try:
                database.update_module_overrides(rows)
            except TraceDatabaseError as error:
                raise ModuleOverrideStoreError("模块启用覆盖无法写入") from error
        finally:
            database.close()

    def _open_database(self) -> TraceDatabase:
        """拒绝符号链接状态路径并把底层错误收窄为存储错误。"""
        if self._database_path.is_symlink() or self._database_path.parent.is_symlink():
            raise ModuleOverrideStoreError("模块状态数据库路径不得是符号链接")
        database = TraceDatabase(self._database_path)
        try:
            database.open()
        except TraceDatabaseError as error:
            database.close()
            raise ModuleOverrideStoreError("模块状态数据库无法打开") from error
        return database
