"""把九个模型工具映射到最小本地开发原语。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import cast

from pydantic import BaseModel

from rivet.modules.capabilities import GuardCapability, LexicalContextCapability
from rivet.tools.catalog import (
    ContextSearchArguments,
    FileCreateArguments,
    FileDeleteArguments,
    FileReadArguments,
    FileReplaceArguments,
    FileWriteArguments,
    GitDiffArguments,
    ProcessRunArguments,
    WorkspaceInfoArguments,
)
from rivet.tools.errors import PathBoundaryError
from rivet.tools.executor import ToolHandler
from rivet.tools.files import FileReader, TransactionFileWriter
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.workspace import WorkspaceInspector
from rivet.transaction.manager import TransactionManager


class WorkspaceToolHandlers:
    """任务级 handler 集；构造阶段不启动模块或进程。"""

    def __init__(
        self,
        boundary: WorkspaceBoundary,
        *,
        transaction_id: str | None = None,
        read_scope: tuple[str, ...] = (),
    ) -> None:
        self._boundary = boundary
        self._transaction_id = transaction_id
        self._read_scope = read_scope

    def mapping(self) -> Mapping[str, ToolHandler]:
        return {
            "workspace_info": self.workspace_info,
            "context_search": self.context_search,
            "file_read": self.file_read,
            "file_write": self.file_write,
            "file_replace": self.file_replace,
            "file_create": self.file_create,
            "file_delete": self.file_delete,
            "process_run": self.process_run,
            "git_diff": self.git_diff,
        }

    async def workspace_info(
        self,
        arguments: BaseModel,
        capabilities: Mapping[str, object],
    ) -> str:
        del capabilities
        cast(WorkspaceInfoArguments, arguments)
        return _json(asdict(await WorkspaceInspector(self._boundary).info()))

    async def context_search(
        self,
        arguments: BaseModel,
        capabilities: Mapping[str, object],
    ) -> str:
        selected = cast(ContextSearchArguments, arguments)
        context = cast(
            LexicalContextCapability,
            capabilities["context.search.lexical"],
        )
        result = await context.search(
            self._boundary,
            selected.query,
            max_results=selected.max_results,
            paths=self._existing_read_scope(),
        )
        return _json(asdict(result))

    async def file_read(
        self,
        arguments: BaseModel,
        capabilities: Mapping[str, object],
    ) -> str:
        del capabilities
        selected = cast(FileReadArguments, arguments)
        self._require_read_path(selected.path)
        reader = FileReader(self._boundary)
        if (selected.start_line is None) != (selected.end_line is None):
            raise ValueError("start_line 与 end_line 必须同时提供")
        result = (
            reader.read_text(selected.path)
            if selected.start_line is None
            else reader.read_range(
                selected.path,
                start_line=selected.start_line,
                end_line=cast(int, selected.end_line),
            )
        )
        return _json(asdict(result))

    async def file_write(
        self,
        arguments: BaseModel,
        capabilities: Mapping[str, object],
    ) -> str:
        del capabilities
        selected = cast(FileWriteArguments, arguments)
        TransactionFileWriter(self._boundary).write(selected.path, selected.content)
        return _json({"path": selected.path, "status": "WRITTEN"})

    async def file_replace(
        self,
        arguments: BaseModel,
        capabilities: Mapping[str, object],
    ) -> str:
        del capabilities
        selected = cast(FileReplaceArguments, arguments)
        count = TransactionFileWriter(self._boundary).replace(
            selected.path,
            selected.old_text,
            selected.new_text,
            expected_count=selected.expected_count,
        )
        return _json({"path": selected.path, "replaced": count, "status": "REPLACED"})

    async def file_create(
        self,
        arguments: BaseModel,
        capabilities: Mapping[str, object],
    ) -> str:
        del capabilities
        selected = cast(FileCreateArguments, arguments)
        TransactionFileWriter(self._boundary).create(selected.path, selected.content)
        return _json({"path": selected.path, "status": "CREATED"})

    async def file_delete(
        self,
        arguments: BaseModel,
        capabilities: Mapping[str, object],
    ) -> str:
        del capabilities
        selected = cast(FileDeleteArguments, arguments)
        TransactionFileWriter(self._boundary).delete(selected.path)
        return _json({"path": selected.path, "status": "DELETED"})

    async def process_run(
        self,
        arguments: BaseModel,
        capabilities: Mapping[str, object],
    ) -> str:
        selected = cast(ProcessRunArguments, arguments)
        guard = cast(
            GuardCapability,
            capabilities["guard.local_execution"],
        )
        runner = guard.create_process_runner(self._boundary)
        result = await runner.run(
            tuple(selected.argv),
            cwd=selected.cwd,
            timeout_seconds=selected.timeout_seconds,
            environment=selected.environment,
        )
        return _json(
            {
                "argv": list(result.argv),
                "cwd": result.cwd,
                "returncode": result.returncode,
                "stderr": result.stderr.decode("utf-8", errors="replace"),
                "stderr_sha256": result.stderr_sha256,
                "stderr_truncated": result.stderr_truncated,
                "stdout": result.stdout.decode("utf-8", errors="replace"),
                "stdout_sha256": result.stdout_sha256,
                "stdout_truncated": result.stdout_truncated,
                "timed_out": result.timed_out,
            }
        )

    async def git_diff(
        self,
        arguments: BaseModel,
        capabilities: Mapping[str, object],
    ) -> str:
        selected = cast(GitDiffArguments, arguments)
        transaction_id = self._transaction_id
        if transaction_id is None:
            raise RuntimeError("git_diff 必须绑定事务")
        manager = cast(
            TransactionManager,
            capabilities["transaction.worktree"],
        )
        return await manager.candidate_diff(transaction_id, path=selected.path)

    def _existing_read_scope(self) -> tuple[str, ...]:
        """只向 Context 暴露冻结范围中当前真实存在的路径。"""
        if not self._read_scope:
            return (".",)
        existing = tuple(
            path
            for path in self._read_scope
            if self._boundary.resolve_repository(path, require_exists=False).exists()
        )
        if not existing:
            raise PathBoundaryError(
                "workspace.read_scope_empty",
                "冻结读范围中没有可调查的现有路径",
            )
        return existing

    def _require_read_path(self, requested: str) -> None:
        """拒绝 file_read 越过冻结的显式调查范围。"""
        if not self._read_scope:
            return
        resolved = self._boundary.resolve_repository(requested, require_exists=True)
        relative = self._boundary.repository_relative(resolved)
        if not any(_path_covers(allowed, relative) for allowed in self._read_scope):
            raise PathBoundaryError(
                "workspace.read_scope_denied",
                "读取路径不在冻结调查范围内",
            )


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _path_covers(allowed: str, requested: str) -> bool:
    return requested == allowed or requested.startswith(f"{allowed}/")
