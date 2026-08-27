"""使用 argv、环境白名单和进程组实现有界本地执行。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal

from rivet.kernel.resources import ResourceScope
from rivet.tools.errors import ProcessToolError
from rivet.tools.paths import WorkspaceBoundary

DEFAULT_ENVIRONMENT_ALLOWLIST = frozenset(
    {"LANG", "LC_ALL", "LC_CTYPE", "PATH", "TERM", "TMPDIR", "TZ"}
)


@dataclass(frozen=True, slots=True)
class ProcessRunResult:
    """保存有界字节输出、完整流哈希和退出事实。"""

    argv: tuple[str, ...]
    cwd: str
    returncode: int | None
    timed_out: bool
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    stdout_total_bytes: int
    stderr_total_bytes: int
    stdout_sha256: str
    stderr_sha256: str


@dataclass(frozen=True, slots=True)
class _CapturedBytes:
    """保存一个已持续 drain 的输出流。"""

    content: bytes
    total_bytes: int
    sha256: str

    @property
    def truncated(self) -> bool:
        """指示捕获前缀是否短于真实流。"""
        return len(self.content) < self.total_bytes


class ProcessRunner:
    """不经过 shell 启动进程，并对整个新会话执行有界终止。"""

    def __init__(
        self,
        boundary: WorkspaceBoundary,
        *,
        scope: ResourceScope,
        max_capture_bytes: int = 5 * 1024 * 1024,
        termination_grace_seconds: float = 1.0,
        environment: Mapping[str, str] | None = None,
        environment_allowlist: frozenset[str] = DEFAULT_ENVIRONMENT_ALLOWLIST,
        root_kind: Literal["transaction", "repository_read_only"] = "transaction",
    ) -> None:
        if max_capture_bytes <= 0 or termination_grace_seconds <= 0:
            raise ValueError("进程输出和终止预算必须大于零")
        self._boundary = boundary
        self._scope = scope
        self._max_capture_bytes = max_capture_bytes
        self._termination_grace_seconds = termination_grace_seconds
        source_environment = environment if environment is not None else os.environ
        self._environment = {
            name: value
            for name, value in source_environment.items()
            if name in environment_allowlist
        }
        self._environment_allowlist = environment_allowlist
        self._root_kind = root_kind

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str = ".",
        timeout_seconds: float,
        environment: Mapping[str, str] | None = None,
    ) -> ProcessRunResult:
        """执行单个 argv 命令，超时或取消时回收同一进程组。"""
        self._validate_argv(argv)
        if timeout_seconds <= 0:
            raise ProcessToolError("process.timeout_invalid", "进程超时必须大于零")
        if self._root_kind == "transaction":
            working_directory = self._boundary.resolve_transaction(
                cwd,
                require_exists=True,
                require_directory=True,
            )
            relative_cwd = self._boundary.transaction_relative(working_directory)
        else:
            working_directory = self._boundary.resolve_repository(
                cwd, require_directory=True
            )
            relative_cwd = self._boundary.repository_relative(working_directory)
        child_environment = dict(self._environment)
        if environment is not None:
            unexpected = set(environment) - self._environment_allowlist
            if unexpected:
                raise ProcessToolError(
                    "process.environment_denied", "进程环境包含未授权变量"
                )
            child_environment.update(environment)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=working_directory,
                env=child_environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as error:
            raise ProcessToolError(
                "process.start_failed", "本地进程无法启动"
            ) from error
        self._scope.register_process(process, description="本地工具进程")
        if process.stdout is None or process.stderr is None:
            await self._terminate_tree(process)
            raise ProcessToolError("process.pipe_missing", "本地进程输出管道缺失")
        stdout_task = self._scope.create_task(
            self._read_stream(process.stdout), description="进程 stdout drain"
        )
        stderr_task = self._scope.create_task(
            self._read_stream(process.stderr), description="进程 stderr drain"
        )
        timed_out = False
        try:
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            except TimeoutError:
                timed_out = True
                await self._terminate_tree(process)
        except asyncio.CancelledError:
            await self._terminate_tree(process)
            raise
        finally:
            if process.returncode is None:
                await self._terminate_tree(process)
        stdout_capture, stderr_capture = await asyncio.gather(stdout_task, stderr_task)
        return ProcessRunResult(
            argv=argv,
            cwd=relative_cwd,
            returncode=process.returncode,
            timed_out=timed_out,
            stdout=stdout_capture.content,
            stderr=stderr_capture.content,
            stdout_truncated=stdout_capture.truncated,
            stderr_truncated=stderr_capture.truncated,
            stdout_total_bytes=stdout_capture.total_bytes,
            stderr_total_bytes=stderr_capture.total_bytes,
            stdout_sha256=f"sha256:{stdout_capture.sha256}",
            stderr_sha256=f"sha256:{stderr_capture.sha256}",
        )

    async def _read_stream(self, reader: asyncio.StreamReader) -> _CapturedBytes:
        """持续 drain 全部输出，但只在内存保留固定前缀。"""
        captured = bytearray()
        total_bytes = 0
        digest = hashlib.sha256()
        while chunk := await reader.read(65_536):
            total_bytes += len(chunk)
            digest.update(chunk)
            remaining = self._max_capture_bytes - len(captured)
            if remaining > 0:
                captured.extend(chunk[:remaining])
        return _CapturedBytes(bytes(captured), total_bytes, digest.hexdigest())

    async def _terminate_tree(self, process: asyncio.subprocess.Process) -> None:
        """依次向新会话发送 TERM、等待、KILL、wait。"""
        if process.returncode is not None:
            await process.wait()
            return
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(
                process.wait(), timeout=self._termination_grace_seconds
            )
            return
        except TimeoutError:
            pass
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()

    @staticmethod
    def _validate_argv(argv: tuple[str, ...]) -> None:
        """拒绝 shell 字符串形态、空程序和超大/NUL 参数。"""
        if not argv or not argv[0]:
            raise ProcessToolError("process.argv_invalid", "进程 argv 必须包含程序")
        if len(argv) > 1_024:
            raise ProcessToolError("process.argv_too_many", "进程 argv 参数过多")
        if any("\x00" in argument or len(argument) > 16_384 for argument in argv):
            raise ProcessToolError("process.argv_invalid", "进程 argv 参数无效")
