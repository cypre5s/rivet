"""自行实现支持并发请求、服务端请求和有界关闭的 LSP 客户端。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import cast

from rivet.contracts.modules import ResourceKind
from rivet.kernel.resources import ResourceScope
from rivet.tools.process import DEFAULT_ENVIRONMENT_ALLOWLIST

from .lsp_protocol import LspFrameError, LspFrameParser, encode_lsp_message

MAX_STDERR_CAPTURE_BYTES = 64 * 1024


class LspClientError(RuntimeError):
    """作为 LSP 客户端可预期错误的公共基类。"""


class LspProcessExitedError(LspClientError):
    """表示 sidecar 在请求完成前退出。"""


class LspRequestTimeoutError(LspClientError):
    """表示单个 JSON-RPC 请求超出冻结预算。"""


class LspResponseError(LspClientError):
    """表示服务端返回 JSON-RPC error。"""


class LspClient:
    """管理单个 stdio sidecar 的请求关联、通知与协议任务。"""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        repository_root: Path,
        scope: ResourceScope,
        request_timeout_seconds: float,
        termination_grace_seconds: float,
    ) -> None:
        self._process = process
        self._repository_root = repository_root
        self._scope = scope
        self._request_timeout_seconds = request_timeout_seconds
        self._termination_grace_seconds = termination_grace_seconds
        self._request_sequence = 0
        self._pending: dict[int, asyncio.Future[object]] = {}
        self._write_lock = asyncio.Lock()
        self._closing = False
        self._root_uri = repository_root.as_uri()
        self._notifications: list[dict[str, object]] = []
        self._stderr_prefix = bytearray()
        self._stderr_total_bytes = 0
        self._stderr_digest = hashlib.sha256()
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise LspClientError("LSP sidecar 缺少 stdio 管道")
        self._reader_task = scope.create_task(
            self._reader_loop(process.stdout), description="LSP stdout reader"
        )
        self._stderr_task = scope.create_task(
            self._drain_stderr(process.stderr), description="LSP stderr drain"
        )

    @classmethod
    async def start(
        cls,
        argv: tuple[str, ...],
        *,
        repository_root: Path,
        scope: ResourceScope,
        request_timeout_seconds: float = 10.0,
        termination_grace_seconds: float = 1.0,
        environment: Mapping[str, str] | None = None,
    ) -> LspClient:
        """以无 shell argv、白名单环境和新进程组启动 sidecar。"""
        if (
            not argv
            or not argv[0]
            or request_timeout_seconds <= 0
            or termination_grace_seconds <= 0
        ):
            raise ValueError("LSP argv 与时间预算必须有效")
        if any("\x00" in argument for argument in argv):
            raise ValueError("LSP argv 不得包含 NUL")
        root = repository_root.resolve(strict=True)
        source_environment = environment if environment is not None else os.environ
        child_environment = {
            name: value
            for name, value in source_environment.items()
            if name in DEFAULT_ENVIRONMENT_ALLOWLIST
        }
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=root,
                env=child_environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as error:
            raise LspClientError("LSP sidecar 无法启动") from error
        scope.register_process(
            process,
            description="LSP sidecar",
            kind=ResourceKind.SIDECAR,
        )
        return cls(
            process,
            repository_root=root,
            scope=scope,
            request_timeout_seconds=request_timeout_seconds,
            termination_grace_seconds=termination_grace_seconds,
        )

    @property
    def is_running(self) -> bool:
        """指示 sidecar 是否仍有活动进程。"""
        return self._process.returncode is None

    @property
    def notifications(self) -> tuple[dict[str, object], ...]:
        """返回有界保留的服务端通知。"""
        return tuple(self._notifications)

    @property
    def stderr_summary(self) -> dict[str, object]:
        """返回不直接展示正文的 stderr 大小、哈希与截断事实。"""
        return {
            "total_bytes": self._stderr_total_bytes,
            "sha256": f"sha256:{self._stderr_digest.hexdigest()}",
            "truncated": self._stderr_total_bytes > len(self._stderr_prefix),
        }

    async def initialize(
        self,
        repository_root: Path,
        *,
        initialization_options: Mapping[str, object],
    ) -> dict[str, object]:
        """发送 initialize 并在成功后发送 initialized 通知。"""
        root = repository_root.resolve(strict=True)
        self._root_uri = root.as_uri()
        result = await self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "clientInfo": {"name": "rivet", "version": "0.1.0"},
                "rootUri": self._root_uri,
                "capabilities": {
                    "workspace": {
                        "configuration": True,
                        "workspaceFolders": True,
                    },
                    "textDocument": {
                        "definition": {"linkSupport": True},
                        "references": {},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    },
                    "window": {"workDoneProgress": True},
                },
                "initializationOptions": dict(initialization_options),
                "workspaceFolders": [
                    {"uri": self._root_uri, "name": root.name or "repository"}
                ],
                "trace": "off",
            },
        )
        if not isinstance(result, dict):
            raise LspResponseError("initialize 结果必须是对象")
        await self.notify("initialized", {})
        return cast(dict[str, object], result)

    async def did_open(
        self,
        *,
        uri: str,
        language_id: str,
        version: int,
        text: str,
    ) -> None:
        """发送完整正文的 textDocument/didOpen 通知。"""
        await self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id,
                    "version": version,
                    "text": text,
                }
            },
        )

    async def request(self, method: str, params: object) -> object:
        """发送并发安全请求，按 ID 关联响应并执行有界超时。"""
        if self._closing or self._process.returncode is not None:
            raise LspProcessExitedError("LSP sidecar 已退出")
        self._request_sequence += 1
        request_id = self._request_sequence
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            try:
                return await asyncio.wait_for(
                    asyncio.shield(future), timeout=self._request_timeout_seconds
                )
            except TimeoutError as error:
                self._pending.pop(request_id, None)
                with suppress(LspClientError):
                    await self.notify("$/cancelRequest", {"id": request_id})
                raise LspRequestTimeoutError(f"LSP 请求 {method} 超时") from error
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            with suppress(LspClientError):
                await self.notify("$/cancelRequest", {"id": request_id})
            raise
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: object) -> None:
        """发送无需响应的 JSON-RPC 通知。"""
        if self._process.returncode is not None:
            raise LspProcessExitedError("LSP sidecar 已退出")
        await self._write_message(
            {"jsonrpc": "2.0", "method": method, "params": params}
        )

    async def shutdown(self) -> None:
        """优先执行 shutdown/exit，再按 TERM、KILL、wait 回收进程组。"""
        if self._closing:
            return
        if self._process.returncode is None:
            with suppress(LspClientError):
                await self.request("shutdown", None)
                await self.notify("exit", None)
        self._closing = True
        try:
            await asyncio.wait_for(
                self._process.wait(), timeout=self._termination_grace_seconds
            )
        except TimeoutError:
            await self._terminate_process_group()
        await self._finish_tasks()
        self._scope.release_process(self._process)
        self._fail_pending(LspProcessExitedError("LSP sidecar 已关闭"))

    async def abort(self) -> None:
        """跳过协议关闭并立即有界终止崩溃或失联进程。"""
        self._closing = True
        await self._terminate_process_group()
        await self._finish_tasks()
        self._scope.release_process(self._process)
        self._fail_pending(LspProcessExitedError("LSP sidecar 已终止"))

    async def _write_message(self, message: dict[str, object]) -> None:
        """串行写入并把断管稳定分类为进程退出。"""
        stdin = self._process.stdin
        if stdin is None:
            raise LspProcessExitedError("LSP stdin 不可用")
        async with self._write_lock:
            try:
                stdin.write(encode_lsp_message(message))
                await stdin.drain()
            except (BrokenPipeError, ConnectionError, RuntimeError) as error:
                raise LspProcessExitedError("LSP sidecar 写入失败") from error

    async def _reader_loop(self, stdout: asyncio.StreamReader) -> None:
        """持续解析响应、通知和服务端请求，EOF 时唤醒全部等待者。"""
        parser = LspFrameParser()
        try:
            while chunk := await stdout.read(65_536):
                for message in parser.feed(chunk):
                    await self._dispatch(message)
            parser.finish()
            if not self._closing:
                self._fail_pending(LspProcessExitedError("LSP sidecar 意外退出"))
        except (LspFrameError, OSError) as error:
            self._fail_pending(LspProcessExitedError("LSP stdout 协议失败"))
            if not self._closing:
                raise LspClientError("LSP stdout reader 失败") from error

    async def _dispatch(self, message: dict[str, object]) -> None:
        """根据 method/id 形态分发服务端消息。"""
        method = message.get("method")
        request_id = message.get("id")
        if isinstance(method, str) and request_id is not None:
            await self._answer_server_request(request_id, method, message.get("params"))
            return
        if isinstance(method, str):
            if len(self._notifications) >= 1_000:
                self._notifications.pop(0)
            self._notifications.append(message)
            return
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            raise LspFrameError("LSP 响应 ID 必须是整数")
        future = self._pending.get(request_id)
        if future is None or future.done():
            return
        error_value = message.get("error")
        if error_value is not None:
            future.set_exception(LspResponseError("LSP 服务端返回请求错误"))
        elif "result" in message:
            future.set_result(message.get("result"))
        else:
            future.set_exception(LspResponseError("LSP 响应缺少 result 或 error"))

    async def _answer_server_request(
        self, request_id: object, method: str, params: object
    ) -> None:
        """只实现初始化所需的安全客户端能力，未知请求返回 Method not found。"""
        if method == "workspace/configuration":
            item_count = 0
            if isinstance(params, dict):
                raw_items = cast(dict[str, object], params).get("items")
                if isinstance(raw_items, list):
                    item_count = len(cast(list[object], raw_items))
            result: object = [None] * item_count
        elif method in {
            "window/workDoneProgress/create",
            "client/registerCapability",
            "client/unregisterCapability",
        }:
            result = None
        elif method == "workspace/workspaceFolders":
            result = [{"uri": self._root_uri, "name": self._repository_root.name}]
        else:
            await self._write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "Method not found"},
                }
            )
            return
        await self._write_message(
            {"jsonrpc": "2.0", "id": request_id, "result": result}
        )

    async def _drain_stderr(self, stderr: asyncio.StreamReader) -> None:
        """持续 drain stderr，只保存有界前缀和完整哈希。"""
        while chunk := await stderr.read(65_536):
            self._stderr_total_bytes += len(chunk)
            self._stderr_digest.update(chunk)
            remaining = MAX_STDERR_CAPTURE_BYTES - len(self._stderr_prefix)
            if remaining > 0:
                self._stderr_prefix.extend(chunk[:remaining])

    async def _terminate_process_group(self) -> None:
        """按 TERM、有界等待、KILL、wait 终止整个 sidecar 会话。"""
        if self._process.returncode is not None:
            await self._process.wait()
            return
        with suppress(ProcessLookupError):
            os.killpg(self._process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(
                self._process.wait(), timeout=self._termination_grace_seconds
            )
            return
        except TimeoutError:
            pass
        with suppress(ProcessLookupError):
            os.killpg(self._process.pid, signal.SIGKILL)
        await self._process.wait()

    async def _finish_tasks(self) -> None:
        """等待 stdout/stderr drain 完成并吸收已分类异常。"""
        for task in (self._reader_task, self._stderr_task):
            try:
                if not task.done():
                    with suppress(asyncio.CancelledError, LspClientError):
                        await task
                else:
                    with suppress(asyncio.CancelledError, LspClientError):
                        task.result()
            finally:
                if task.done():
                    self._scope.release_task(task)

    def _fail_pending(self, error: LspClientError) -> None:
        """用同一脱敏分类唤醒全部未完成请求。"""
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)
