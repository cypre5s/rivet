"""验证 ResourceScope 的归属、计数和有界回收。"""

from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from pathlib import Path

import pytest

from rivet.kernel.errors import (
    ResourceCleanupError,
    ResourceLeakError,
    ResourceScopeClosedError,
)
from rivet.kernel.resources import ResourceScope


class AsyncClient:
    """模拟具有异步关闭接口的客户端。"""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        """记录异步关闭。"""
        self.closed = True


class Connection:
    """模拟同步连接关闭接口。"""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        """记录同步关闭。"""
        self.closed = True


@pytest.mark.asyncio
async def test_scope_closes_task_client_connection_and_temp_directory(
    tmp_path: Path,
) -> None:
    scope = ResourceScope("test.resource")
    client = AsyncClient()
    connection = Connection()
    task = scope.create_task(asyncio.sleep(3_600), description="后台任务")
    directory = scope.create_temp_directory(root=tmp_path, description="临时目录")
    scope.register_client(client, description="HTTP 客户端")
    scope.register_connection(connection, description="数据库连接")

    counts_before = scope.counts()
    await scope.close()

    assert counts_before.active_task_count == 1
    assert counts_before.open_client_count == 1
    assert counts_before.open_connection_count == 1
    assert counts_before.temporary_directory_count == 1
    assert task.cancelled()
    assert client.closed
    assert connection.closed
    assert not directory.exists()
    assert scope.counts().resource_count == 0


@pytest.mark.asyncio
async def test_scope_runs_worktree_cleanup_once(tmp_path: Path) -> None:
    scope = ResourceScope("test.resource")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    cleanup_calls = 0

    async def cleanup(path: Path) -> None:
        """模拟由事务模块提供的安全 Worktree 清理。"""
        nonlocal cleanup_calls
        cleanup_calls += 1
        path.rmdir()

    scope.register_worktree(worktree, cleanup=cleanup, description="测试 worktree")

    await scope.close()
    await scope.close()

    assert cleanup_calls == 1
    assert not worktree.exists()
    assert scope.counts().temporary_worktree_count == 0


@pytest.mark.asyncio
async def test_scope_releases_only_removed_worktree(tmp_path: Path) -> None:
    scope = ResourceScope("transaction.worktree.release")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    async def cleanup(path: Path) -> None:
        """为意外关闭保留可执行清理动作。"""
        path.rmdir()

    scope.register_worktree(worktree, cleanup=cleanup, description="事务 Worktree")

    with pytest.raises(ResourceCleanupError, match="仍存在"):
        scope.release_worktree(worktree)
    worktree.rmdir()
    scope.release_worktree(worktree)

    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_scope_releases_only_finished_tasks_and_waited_processes() -> None:
    scope = ResourceScope("process.release")
    task = scope.create_task(asyncio.sleep(3_600), description="测试读取任务")
    with pytest.raises(ResourceCleanupError, match="活动任务"):
        scope.release_task(task)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    scope.release_task(task)

    process = await scope.create_process(
        sys.executable,
        "-c",
        "import time; time.sleep(3600)",
        description="测试 sidecar",
    )

    with pytest.raises(ResourceCleanupError, match="活动进程"):
        scope.release_process(process)
    process.terminate()
    await process.wait()
    scope.release_process(process)

    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_scope_cleanup_failure_continues_and_closed_scope_rejects_registration(
    tmp_path: Path,
) -> None:
    class FailingConnection:
        """在关闭时制造可控资源故障。"""

        def close(self) -> None:
            """抛出固定关闭故障。"""
            raise RuntimeError("fixture close failure")

    scope = ResourceScope("test.cleanup_failure")
    scope.register_connection(FailingConnection(), description="失败连接")
    directory = scope.create_temp_directory(root=tmp_path, description="仍需清理")

    with pytest.raises(ResourceLeakError):
        scope.assert_empty()
    with pytest.raises(ResourceCleanupError, match="至少一项"):
        await scope.close()

    assert not directory.exists()
    assert scope.counts().resource_count == 1
    with pytest.raises(ResourceLeakError):
        scope.assert_empty()
    with pytest.raises(ResourceCleanupError, match="至少一项"):
        await scope.close()
    with pytest.raises(ResourceScopeClosedError):
        scope.create_temp_directory(description="关闭后禁止")


@pytest.mark.asyncio
async def test_worktree_transfer_requires_registered_existing_directory(
    tmp_path: Path,
) -> None:
    scope = ResourceScope("test.worktree_transfer")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    async def cleanup(path: Path) -> None:
        """为测试提供正常清理回调。"""
        path.rmdir()

    scope.register_worktree(worktree, cleanup=cleanup, description="持久事务")
    scope.transfer_persisted_worktree(worktree)
    scope.assert_empty()
    with pytest.raises(ResourceCleanupError, match="未登记"):
        scope.transfer_persisted_worktree(worktree)
    await scope.close()


@pytest.mark.asyncio
async def test_missing_worktree_directory_is_rejected(
    tmp_path: Path,
) -> None:
    scope = ResourceScope("test.invalid_resources")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    async def cleanup(_path: Path) -> None:
        """保留目录以测试移交时的二次检查。"""

    scope.register_worktree(worktree, cleanup=cleanup, description="待移交")
    worktree.rmdir()
    with pytest.raises(ResourceCleanupError, match="必须仍为目录"):
        scope.transfer_persisted_worktree(worktree)
    await scope.close()
