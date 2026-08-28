"""供崩溃恢复测试随机终止的独立 Trace 写入进程。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from rivet.trace.paths import RuntimePaths
from rivet.trace.store import TraceStore
from tests.fixtures.trace.events import make_event


async def _run(repository_root: Path) -> None:
    """逐事件确认持久化，以便父进程选择确定终止点。"""
    paths = RuntimePaths.for_repository(
        repository_root,
        environment={"XDG_CACHE_HOME": str(repository_root / "cache")},
    )
    store = TraceStore(paths)
    await store.start()
    for sequence in range(1, 10_001):
        await store.emit(make_event(sequence))
        print(sequence, flush=True)


if __name__ == "__main__":
    asyncio.run(_run(Path(sys.argv[1]).resolve()))
