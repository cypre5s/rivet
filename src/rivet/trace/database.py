"""维护 Trace 的 SQLite 索引、状态快照与幂等 schema migration。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from rivet.trace.errors import TraceDatabaseError
from rivet.trace.models import LocatedTraceEvent, TraceState

CURRENT_SCHEMA_VERSION = 2
ALLOWED_PRAGMAS = frozenset({"journal_mode", "foreign_keys", "busy_timeout"})

MIGRATION_VERSION_1 = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    ended_at TEXT,
    UNIQUE (run_id, session_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    run_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    transaction_id TEXT,
    parent_event_id TEXT,
    byte_offset INTEGER NOT NULL CHECK (byte_offset >= 0),
    byte_length INTEGER NOT NULL CHECK (byte_length > 0),
    FOREIGN KEY (run_id, session_id) REFERENCES runs(run_id, session_id),
    FOREIGN KEY (parent_event_id) REFERENCES events(event_id)
);
CREATE INDEX IF NOT EXISTS events_run_sequence_idx
    ON events(run_id, sequence);
CREATE INDEX IF NOT EXISTS events_transaction_sequence_idx
    ON events(transaction_id, sequence);
CREATE TABLE IF NOT EXISTS module_states (
    run_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    PRIMARY KEY (run_id, module_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id),
    FOREIGN KEY (source_event_id) REFERENCES events(event_id)
);
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id),
    FOREIGN KEY (source_event_id) REFERENCES events(event_id)
);
CREATE TABLE IF NOT EXISTS run_metrics (
    run_id TEXT PRIMARY KEY,
    snapshot_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
"""

MIGRATION_VERSION_2 = """
CREATE TABLE IF NOT EXISTS module_overrides (
    scope TEXT NOT NULL CHECK (scope IN ('application', 'workspace')),
    workspace_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    updated_at TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('cli', 'tui', 'recovery')),
    PRIMARY KEY (scope, workspace_id, module_id)
);
CREATE INDEX IF NOT EXISTS module_overrides_workspace_idx
    ON module_overrides(workspace_id, module_id);
"""


class TraceDatabase:
    """封装单 Writer 使用的同步 SQLite 连接与确定性索引更新。"""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._connection: sqlite3.Connection | None = None

    def open(self) -> None:
        """显式打开连接、配置 WAL/外键/超时并迁移。"""
        if self._connection is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            connection = sqlite3.connect(self.path, isolation_level=None)
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA synchronous = FULL")
        except sqlite3.Error as error:
            raise TraceDatabaseError("SQLite 无法打开或配置") from error
        self._connection = connection
        self.migrate()

    def migrate(self) -> None:
        """重复执行只应用尚未记录的 migration。"""
        connection = self._require_connection()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
            current_version = int(row[0]) if row is not None else 0
            if current_version > CURRENT_SCHEMA_VERSION:
                raise TraceDatabaseError(
                    f"SQLite schema 版本 {current_version} 高于当前支持版本"
                )
            if current_version < 1:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in MIGRATION_VERSION_1.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(
                        """
                        INSERT INTO schema_migrations(version, applied_at)
                        VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                        """
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            if current_version < 2:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in MIGRATION_VERSION_2.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(
                        """
                        INSERT INTO schema_migrations(version, applied_at)
                        VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                        """
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except (sqlite3.Error, ValueError) as error:
            raise TraceDatabaseError("SQLite schema migration 失败") from error

    def append_events(self, located_events: tuple[LocatedTraceEvent, ...]) -> None:
        """在单事务中追加事件索引与模块/事务快照。"""
        if not located_events:
            return
        connection = self._require_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for located_event in located_events:
                self._insert_event(connection, located_event)
            connection.commit()
        except (sqlite3.Error, TraceDatabaseError) as error:
            connection.rollback()
            if isinstance(error, TraceDatabaseError):
                raise
            raise TraceDatabaseError("SQLite 事件索引追加失败") from error

    def rebuild_indexes(
        self,
        located_events: tuple[LocatedTraceEvent, ...],
        states: tuple[TraceState, ...],
    ) -> None:
        """以 NDJSON 事实源重建全部可派生索引与快照。"""
        connection = self._require_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for table_name in (
                "run_metrics",
                "module_states",
                "transactions",
                "events",
                "runs",
                "sessions",
            ):
                connection.execute(f"DELETE FROM {table_name}")
            for located_event in located_events:
                self._insert_event(connection, located_event)
            for state in states:
                self._upsert_metric(connection, state)
            connection.commit()
        except (sqlite3.Error, TraceDatabaseError) as error:
            connection.rollback()
            if isinstance(error, TraceDatabaseError):
                raise
            raise TraceDatabaseError("SQLite 索引重建失败") from error

    def update_metrics(self, states: tuple[TraceState, ...]) -> None:
        """持久化受影响 run 的 reducer 快照。"""
        if not states:
            return
        connection = self._require_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for state in states:
                self._upsert_metric(connection, state)
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise TraceDatabaseError("SQLite 指标快照更新失败") from error

    def event_run_id(self, event_id: str) -> str | None:
        """查询已持久化父事件所属 run。"""
        row = (
            self._require_connection()
            .execute("SELECT run_id FROM events WHERE event_id = ?", (event_id,))
            .fetchone()
        )
        return str(row[0]) if row is not None else None

    def event_count(self) -> int:
        """返回已索引事件数。"""
        row = (
            self._require_connection().execute("SELECT COUNT(*) FROM events").fetchone()
        )
        return int(row[0]) if row is not None else 0

    def schema_version(self) -> int:
        """返回已应用 migration 的最高版本。"""
        row = (
            self._require_connection()
            .execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
            .fetchone()
        )
        return int(row[0]) if row is not None else 0

    def migration_count(self) -> int:
        """返回 migration 记录数，用于幂等验收。"""
        row = (
            self._require_connection()
            .execute("SELECT COUNT(*) FROM schema_migrations")
            .fetchone()
        )
        return int(row[0]) if row is not None else 0

    def pragma(self, name: str) -> str | int:
        """只允许查询固定 pragma，避免拼接任意 SQL。"""
        if name not in ALLOWED_PRAGMAS:
            raise ValueError(f"不允许查询 PRAGMA {name}")
        row = self._require_connection().execute(f"PRAGMA {name}").fetchone()
        if row is None:
            raise TraceDatabaseError(f"PRAGMA {name} 未返回值")
        value = row[0]
        return int(value) if isinstance(value, int) else str(value)

    def table_names(self) -> set[str]:
        """列出非 SQLite 内部表。"""
        rows = (
            self._require_connection()
            .execute(
                """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
            )
            .fetchall()
        )
        return {str(row[0]) for row in rows}

    def module_overrides(self, workspace_id: str) -> dict[tuple[str, str], bool]:
        """读取应用级与指定工作区的模块启用覆盖。"""
        rows = (
            self._require_connection()
            .execute(
                """
                SELECT scope, module_id, enabled
                FROM module_overrides
                WHERE (scope = 'application' AND workspace_id = '')
                   OR (scope = 'workspace' AND workspace_id = ?)
                ORDER BY scope, module_id
                """,
                (workspace_id,),
            )
            .fetchall()
        )
        return {
            (str(scope), str(module_id)): bool(enabled)
            for scope, module_id, enabled in rows
        }

    def update_module_overrides(
        self,
        changes: tuple[tuple[str, str, str, bool | None, str], ...],
    ) -> None:
        """原子写入或删除一组生命周期启用覆盖。"""
        if not changes:
            return
        connection = self._require_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for scope, workspace_id, module_id, enabled, source in changes:
                if enabled is None:
                    connection.execute(
                        """
                        DELETE FROM module_overrides
                        WHERE scope = ? AND workspace_id = ? AND module_id = ?
                        """,
                        (scope, workspace_id, module_id),
                    )
                    continue
                connection.execute(
                    """
                    INSERT INTO module_overrides(
                        scope, workspace_id, module_id, enabled, updated_at, source
                    ) VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?)
                    ON CONFLICT(scope, workspace_id, module_id) DO UPDATE SET
                        enabled = excluded.enabled,
                        updated_at = excluded.updated_at,
                        source = excluded.source
                    """,
                    (scope, workspace_id, module_id, int(enabled), source),
                )
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise TraceDatabaseError("模块启用覆盖写入失败") from error

    def close(self) -> None:
        """幂等关闭 SQLite 连接。"""
        connection = self._connection
        self._connection = None
        if connection is not None:
            connection.close()

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        located_event: LocatedTraceEvent,
    ) -> None:
        """插入单个事件及其可派生上下文快照。"""
        record = located_event.record
        event = record.event
        timestamp = event.timestamp.isoformat()
        connection.execute(
            """
            INSERT INTO sessions(session_id, created_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (event.session_id, timestamp, timestamp),
        )
        run_status = self._run_status(event.event_type)
        ended_at = (
            timestamp if run_status in {"COMPLETED", "FAILED", "CANCELLED"} else None
        )
        connection.execute(
            """
            INSERT INTO runs(
                run_id, session_id, status, started_at, updated_at, ended_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status = CASE
                    WHEN excluded.status = 'RUNNING' THEN runs.status
                    ELSE excluded.status
                END,
                updated_at = excluded.updated_at,
                ended_at = COALESCE(excluded.ended_at, runs.ended_at)
            """,
            (
                event.run_id,
                event.session_id,
                run_status,
                timestamp,
                timestamp,
                ended_at,
            ),
        )
        existing_session = connection.execute(
            "SELECT session_id FROM runs WHERE run_id = ?", (event.run_id,)
        ).fetchone()
        if existing_session is None or str(existing_session[0]) != event.session_id:
            raise TraceDatabaseError("同一 run_id 不得跨 session")
        if event.parent_event_id is not None:
            parent_row = connection.execute(
                "SELECT run_id FROM events WHERE event_id = ?",
                (event.parent_event_id,),
            ).fetchone()
            if parent_row is None or str(parent_row[0]) != event.run_id:
                raise TraceDatabaseError("父事件必须已存在且属于同一 run")
        connection.execute(
            """
            INSERT INTO events(
                sequence, event_id, event_type, timestamp, run_id, session_id,
                transaction_id, parent_event_id, byte_offset, byte_length
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.sequence,
                event.event_id,
                event.event_type,
                timestamp,
                event.run_id,
                event.session_id,
                event.transaction_id,
                event.parent_event_id,
                located_event.byte_offset,
                located_event.byte_length,
            ),
        )
        module_id = event.payload.get("module_id")
        module_state = event.payload.get("state")
        if (
            event.event_type.startswith("module.")
            and isinstance(module_id, str)
            and isinstance(module_state, str)
        ):
            connection.execute(
                """
                INSERT INTO module_states(
                    run_id, module_id, state, updated_at, source_event_id
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id, module_id) DO UPDATE SET
                    state = excluded.state,
                    updated_at = excluded.updated_at,
                    source_event_id = excluded.source_event_id
                """,
                (
                    event.run_id,
                    module_id,
                    module_state.upper(),
                    timestamp,
                    event.event_id,
                ),
            )
        transaction_state = event.payload.get("transaction_state")
        if event.transaction_id is not None and isinstance(transaction_state, str):
            connection.execute(
                """
                INSERT INTO transactions(
                    transaction_id, run_id, state, updated_at, source_event_id
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(transaction_id) DO UPDATE SET
                    state = excluded.state,
                    updated_at = excluded.updated_at,
                    source_event_id = excluded.source_event_id
                """,
                (
                    event.transaction_id,
                    event.run_id,
                    transaction_state.upper(),
                    timestamp,
                    event.event_id,
                ),
            )

    @staticmethod
    def _run_status(event_type: str) -> str:
        """从稳定终止事件推导 run 状态。"""
        if event_type == "run.completed":
            return "COMPLETED"
        if event_type == "run.failed":
            return "FAILED"
        if event_type == "run.cancelled":
            return "CANCELLED"
        return "RUNNING"

    @staticmethod
    def _upsert_metric(connection: sqlite3.Connection, state: TraceState) -> None:
        """写入确定性排序的 reducer JSON 快照。"""
        connection.execute(
            """
            INSERT INTO run_metrics(run_id, snapshot_json, updated_at)
            VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            ON CONFLICT(run_id) DO UPDATE SET
                snapshot_json = excluded.snapshot_json,
                updated_at = excluded.updated_at
            """,
            (
                state.run_id,
                json.dumps(
                    state.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )

    def _require_connection(self) -> sqlite3.Connection:
        """拒绝在显式 open 之前或 close 之后访问数据库。"""
        if self._connection is None:
            raise TraceDatabaseError("SQLite 连接尚未打开")
        return self._connection
