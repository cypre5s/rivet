"""验证 ResourceScope 的归属、计数和有界回收。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

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
