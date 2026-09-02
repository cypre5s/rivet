"""管理模块拥有的异步任务、进程、连接和临时资源。"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Protocol, TypeVar

from rivet.contracts.modules import ResourceKind
from rivet.kernel.errors import (
    ResourceCleanupError,
    ResourceLeakError,
    ResourceScopeClosedError,
)

T = TypeVar("T")
Cleanup = Callable[[], Awaitable[None]]
WorktreeCleanup = Callable[[Path], Awaitable[None]]


class AsyncCloseable(Protocol):
    """描述具有异步关闭原语的客户端。"""

    def aclose(self) -> Awaitable[None]:
        """关闭客户端。"""
        ...


class SyncCloseable(Protocol):
    """描述具有同步关闭原语的连接。"""

    def close(self) -> None:
        """关闭连接。"""
        ...


@dataclass(frozen=True, slots=True)
class ResourceCounts:
    """汇总生命周期门禁使用的各类活动资源数量。"""

    active_task_count: int = 0
    active_process_count: int = 0
    open_client_count: int = 0
    open_connection_count: int = 0
    temporary_directory_count: int = 0
    temporary_worktree_count: int = 0
    resource_count: int = 0

    def __add__(self, other: ResourceCounts) -> ResourceCounts:
        """合并多个模块 scope 的资源计数。"""
        return ResourceCounts(
            active_task_count=self.active_task_count + other.active_task_count,
            active_process_count=(
                self.active_process_count + other.active_process_count
            ),
            open_client_count=self.open_client_count + other.open_client_count,
            open_connection_count=(
                self.open_connection_count + other.open_connection_count
            ),
            temporary_directory_count=(
                self.temporary_directory_count + other.temporary_directory_count
            ),
            temporary_worktree_count=(
                self.temporary_worktree_count + other.temporary_worktree_count
            ),
            resource_count=self.resource_count + other.resource_count,
        )


@dataclass(slots=True)
class _OwnedResource:
    """保存不对外序列化的真实句柄清理动作。"""

    resource_id: str
    kind: ResourceKind
    cleanup: Cleanup
    is_active: Callable[[], bool]
    description: str
    handle: object | None = None


class ResourceScope:
    """强制每项长期资源属于唯一模块并支持幂等回收。"""

    def __init__(
        self,
        owner_module_id: str,
        *,
        process_terminate_timeout_seconds: float = 2.0,
    ) -> None:
        self.owner_module_id = owner_module_id
        self._process_terminate_timeout_seconds = process_terminate_timeout_seconds
        self._sequence = count(1)
        self._resources: dict[str, _OwnedResource] = {}
        self._closed = False

    def create_task(
        self,
        coroutine: Coroutine[object, object, T],
        *,
        description: str,
    ) -> asyncio.Task[T]:
        """创建并登记 Task，完成后自动移除记录。"""
        self._ensure_open()
        task = asyncio.create_task(coroutine)

        async def cleanup() -> None:
            if task is asyncio.current_task():
                return
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        resource_id = self._register(
            ResourceKind.TASK,
            cleanup,
            is_active=lambda: not task.done(),
            description=description,
            handle=task,
        )

        def discard_finished(_task: asyncio.Task[T]) -> None:
            self._resources.pop(resource_id, None)

        task.add_done_callback(discard_finished)
        return task

    def release_task(self, task: asyncio.Task[object]) -> None:
        """在已结束任务被等待后同步移除其资源登记。"""
        if not task.done():
            raise ResourceCleanupError("活动任务不得提前移除资源登记")
        for resource_id, resource in tuple(self._resources.items()):
            if resource.handle is task:
                self._resources.pop(resource_id, None)
                return

    async def create_process(
        self,
        program: str,
        *arguments: str,
        description: str,
    ) -> asyncio.subprocess.Process:
        """使用无 shell 参数数组启动并登记子进程。"""
        self._ensure_open()
        process = await asyncio.create_subprocess_exec(
            program,
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.register_process(process, description=description)
        return process

    def register_process(
        self,
        process: asyncio.subprocess.Process,
        *,
        description: str,
    ) -> asyncio.subprocess.Process:
        """登记已有进程并按 TERM、有界等待、KILL、wait 回收。"""
        self._ensure_open()

        async def cleanup() -> None:
            if process.returncode is not None:
                await process.wait()
                return
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=self._process_terminate_timeout_seconds
                )
            except TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

        self._register(
            ResourceKind.PROCESS,
            cleanup,
            is_active=lambda: process.returncode is None,
            description=description,
            handle=process,
        )
        return process

    def release_process(self, process: asyncio.subprocess.Process) -> None:
        """在已 wait 的进程退出后移除其资源登记。"""
        if process.returncode is None:
            raise ResourceCleanupError("活动进程不得提前移除资源登记")
        for resource_id, resource in tuple(self._resources.items()):
            if resource.handle is process:
                self._resources.pop(resource_id, None)
                return

    def register_client(
        self, client: AsyncCloseable, *, description: str
    ) -> AsyncCloseable:
        """登记具有 aclose 的 HTTP 或模型客户端。"""
        self._ensure_open()

        async def cleanup() -> None:
            await client.aclose()

        self._register(
            ResourceKind.CLIENT,
            cleanup,
            is_active=lambda: True,
            description=description,
        )
        return client

    def register_connection(
        self, connection: SyncCloseable, *, description: str
    ) -> SyncCloseable:
        """登记具有 close 的数据库或流连接。"""
        self._ensure_open()

        async def cleanup() -> None:
            connection.close()

        self._register(
            ResourceKind.CONNECTION,
            cleanup,
            is_active=lambda: True,
            description=description,
        )
        return connection

    def create_temp_directory(
        self,
        *,
        root: Path | None = None,
        description: str,
    ) -> Path:
        """创建并登记只由当前 scope 回收的临时目录。"""
        self._ensure_open()
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(dir=root))

        async def cleanup() -> None:
            if directory.exists():
                shutil.rmtree(directory)

        self._register(
            ResourceKind.TEMP_DIRECTORY,
            cleanup,
            is_active=directory.exists,
            description=description,
        )
        return directory

    def register_worktree(
        self,
        path: Path,
        *,
        cleanup: WorktreeCleanup,
        description: str,
    ) -> Path:
        """登记 Worktree，并把安全移除策略留给事务模块提供。"""
        self._ensure_open()

        async def cleanup_worktree() -> None:
            await cleanup(path)

        self._register(
            ResourceKind.WORKTREE,
            cleanup_worktree,
            is_active=path.exists,
            description=description,
            handle=path,
        )
        return path

    def release_worktree(self, path: Path) -> None:
        """在 Worktree 已清理后同步移除其资源登记。"""
        if path.exists():
            raise ResourceCleanupError("仍存在的 Worktree 不得提前移除资源登记")
        resolved = path.resolve(strict=False)
        for resource_id, resource in tuple(self._resources.items()):
            if resource.kind is not ResourceKind.WORKTREE:
                continue
            handle = resource.handle
            if isinstance(handle, Path) and handle.resolve(strict=False) == resolved:
                self._resources.pop(resource_id, None)
                return

    def transfer_persisted_worktree(self, path: Path) -> None:
        """把仍存在但已有持久化所有者的 Worktree 移交给恢复层。"""
        resolved = path.resolve(strict=False)
        for resource_id, resource in tuple(self._resources.items()):
            if resource.kind is not ResourceKind.WORKTREE:
                continue
            handle = resource.handle
            if isinstance(handle, Path) and handle.resolve(strict=False) == resolved:
                if not path.is_dir():
                    raise ResourceCleanupError("持久化 Worktree 必须仍为目录")
                self._resources.pop(resource_id, None)
                return
        raise ResourceCleanupError("待移交 Worktree 未登记到当前资源域")

    def counts(self) -> ResourceCounts:
        """返回当前仍登记且活动的资源计数。"""
        active_resources = tuple(
            resource for resource in self._resources.values() if resource.is_active()
        )
        return ResourceCounts(
            active_task_count=sum(
                resource.kind is ResourceKind.TASK for resource in active_resources
            ),
            active_process_count=sum(
                resource.kind is ResourceKind.PROCESS for resource in active_resources
            ),
            open_client_count=sum(
                resource.kind is ResourceKind.CLIENT for resource in active_resources
            ),
            open_connection_count=sum(
                resource.kind is ResourceKind.CONNECTION
                for resource in active_resources
            ),
            temporary_directory_count=sum(
                resource.kind is ResourceKind.TEMP_DIRECTORY
                for resource in active_resources
            ),
            temporary_worktree_count=sum(
                resource.kind is ResourceKind.WORKTREE for resource in active_resources
            ),
            resource_count=len(self._resources),
        )

    async def close(self) -> None:
        """按逆注册顺序尽力清理全部资源并保持幂等。"""
        if self._closed and not self._resources:
            return
        self._closed = True
        first_error: Exception | None = None
        for resource_id, resource in reversed(tuple(self._resources.items())):
            try:
                await resource.cleanup()
            except Exception as error:
                if first_error is None:
                    first_error = error
            else:
                self._resources.pop(resource_id, None)
        if first_error is not None:
            raise ResourceCleanupError(
                f"模块 {self.owner_module_id} 至少一项资源清理失败"
            ) from first_error

    def assert_empty(self) -> None:
        """在生命周期门禁处拒绝任何残留资源。"""
        counts = self.counts()
        if counts.resource_count:
            raise ResourceLeakError(
                f"模块 {self.owner_module_id} 仍有 {counts.resource_count} 项资源"
            )

    def _register(
        self,
        kind: ResourceKind,
        cleanup: Cleanup,
        *,
        is_active: Callable[[], bool],
        description: str,
        handle: object | None = None,
    ) -> str:
        """生成局部稳定 ID 并保存真实清理动作。"""
        self._ensure_open()
        owner_slug = self.owner_module_id.replace(".", "_")[:48]
        resource_id = f"resource_{owner_slug}_{next(self._sequence)}"
        self._resources[resource_id] = _OwnedResource(
            resource_id=resource_id,
            kind=kind,
            cleanup=cleanup,
            is_active=is_active,
            description=description,
            handle=handle,
        )
        return resource_id

    def _ensure_open(self) -> None:
        """拒绝关闭后的资源注册。"""
        if self._closed:
            raise ResourceScopeClosedError(
                f"模块 {self.owner_module_id} 的 ResourceScope 已关闭"
            )
