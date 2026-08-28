"""管理按需 LSP 会话、文档、空闲关闭和单次崩溃恢复。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TypeVar

from rivet.kernel.resources import ResourceScope
from rivet.tools.files import FileReader
from rivet.tools.paths import WorkspaceBoundary

from .lsp_client import LspClient, LspClientError, LspProcessExitedError
from .lsp_manifest import LspServerManifest
from .lsp_models import (
    LspDocumentSymbol,
    LspLocation,
    LspPosition,
    parse_document_symbols,
    parse_locations,
)

T = TypeVar("T")


class LspSidecarError(RuntimeError):
    """作为 LSP 会话编排失败的公共基类。"""


class LspRestartLimitError(LspSidecarError):
    """表示 sidecar 崩溃后已用完唯一重启机会。"""


class LspSidecar:
    """只在第一次语义请求时启动并在空闲预算后彻底关闭进程。"""

    def __init__(
        self,
        manifest: LspServerManifest,
        *,
        repository_root: Path,
        scope: ResourceScope,
    ) -> None:
        self._manifest = manifest
        self._repository_root = repository_root.resolve(strict=True)
        self._scope = scope
        self._boundary = WorkspaceBoundary(self._repository_root)
        self._reader = FileReader(self._boundary, max_file_bytes=2 * 1024 * 1024)
        self._client: LspClient | None = None
        self._operation_lock = asyncio.Lock()
        self._idle_task: asyncio.Task[None] | None = None
        self._document_generation: dict[str, int] = {}
        self._generation = 0
        self.start_count = 0
        self.restart_count = 0

    @property
    def is_running(self) -> bool:
        """指示当前是否有活动 sidecar 进程。"""
        return self._client is not None and self._client.is_running

    async def definition(
        self, path: str, position: LspPosition
    ) -> tuple[LspLocation, ...]:
        """请求跨文件 Definition 并拒绝仓库外 URI。"""
        result = await self._run_document_request(
            path,
            "textDocument/definition",
            lambda uri: {"textDocument": {"uri": uri}, "position": position.to_json()},
        )
        return parse_locations(result, self._repository_root)

    async def references(
        self, path: str, position: LspPosition
    ) -> tuple[LspLocation, ...]:
        """请求包含声明的跨文件 References 并稳定排序。"""
        result = await self._run_document_request(
            path,
            "textDocument/references",
            lambda uri: {
                "textDocument": {"uri": uri},
                "position": position.to_json(),
                "context": {"includeDeclaration": True},
            },
        )
        return parse_locations(result, self._repository_root)

    async def document_symbols(self, path: str) -> tuple[LspDocumentSymbol, ...]:
        """请求当前文档的层级符号。"""
        result = await self._run_document_request(
            path,
            "textDocument/documentSymbol",
            lambda uri: {"textDocument": {"uri": uri}},
        )
        return parse_document_symbols(result)

    async def close(self) -> None:
        """幂等取消空闲计时并优先协议关闭 sidecar。"""
        await self._cancel_idle()
        async with self._operation_lock:
            await self._close_client(graceful=True)

    async def _run_document_request(
        self,
        path: str,
        method: str,
        build_params: Callable[[str], object],
    ) -> object:
        """串行执行请求，崩溃时最多重启一次并重新 didOpen。"""
        await self._cancel_idle()
        async with self._operation_lock:
            while True:
                try:
                    client = await self._ensure_started()
                    uri = await self._ensure_open(client, path)
                    result = await client.request(method, build_params(uri))
                    self._schedule_idle()
                    return result
                except LspProcessExitedError as error:
                    await self._close_client(graceful=False)
                    if self.restart_count >= self._manifest.max_restarts:
                        raise LspRestartLimitError(
                            "LSP sidecar 崩溃后最多重启一次，现已明确停止"
                        ) from error
                    self.restart_count += 1

    async def _ensure_started(self) -> LspClient:
        """惰性启动并完成 initialize/initialized 握手。"""
        if self._client is not None and self._client.is_running:
            return self._client
        client = await LspClient.start(
            self._manifest.command(),
            repository_root=self._repository_root,
            scope=self._scope,
            request_timeout_seconds=self._manifest.request_timeout_seconds,
        )
        self._client = client
        self._generation += 1
        self.start_count += 1
        try:
            await client.initialize(
                self._repository_root,
                initialization_options=self._manifest.initialization_options,
            )
        except LspClientError:
            await self._close_client(graceful=False)
            raise
        return client

    async def _ensure_open(self, client: LspClient, path: str) -> str:
        """读取授权文本并在每个 sidecar generation 只发送一次 didOpen。"""
        absolute_path = self._boundary.resolve_repository(path, require_file=True)
        relative_path = self._boundary.repository_relative(absolute_path)
        uri = absolute_path.as_uri()
        if self._document_generation.get(relative_path) == self._generation:
            return uri
        read = self._reader.read_text(relative_path)
        await client.did_open(
            uri=uri,
            language_id=self._manifest.language_id_for_path(relative_path),
            version=1,
            text=read.content,
        )
        self._document_generation[relative_path] = self._generation
        return uri

    def _schedule_idle(self) -> None:
        """每次成功请求后重置清单指定的空闲关闭计时。"""
        if self._idle_task is not None and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = self._scope.create_task(
            self._sleep_after_idle(), description="LSP idle timer"
        )

    async def _sleep_after_idle(self) -> None:
        """达到空闲预算后在操作锁内关闭当前 generation。"""
        await asyncio.sleep(self._manifest.idle_timeout_seconds)
        async with self._operation_lock:
            await self._close_client(graceful=True)

    async def _cancel_idle(self) -> None:
        """取消并等待非当前空闲任务。"""
        task = self._idle_task
        self._idle_task = None
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _close_client(self, *, graceful: bool) -> None:
        """关闭并丢弃当前 client，保留重启所需的文档路径状态。"""
        client = self._client
        self._client = None
        if client is None:
            return
        if graceful:
            await client.shutdown()
        else:
            await client.abort()
