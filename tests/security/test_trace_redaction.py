"""验证 Trace、SQLite 索引和 artifact 均不保存秘密明文。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.trace.paths import RuntimePaths
from rivet.trace.redaction import SecretRedactor
from rivet.trace.store import TraceStore
from tests.fixtures.trace.events import make_event


@pytest.mark.asyncio
async def test_trace_store_redacts_event_and_environment_secret(tmp_path: Path) -> None:
    secret = "sk-" + ("e" * 32)
    paths = RuntimePaths.for_repository(
        tmp_path,
        environment={"XDG_CACHE_HOME": str(tmp_path / "cache")},
    )
    store = TraceStore(
        paths,
        redactor=SecretRedactor(environment={"DEEPSEEK_API_KEY": secret}),
    )
    await store.start()

    await store.emit(make_event(1, payload={"message": "value=" + secret}))
    await store.close()

    assert secret.encode("utf-8") not in paths.events_path.read_bytes()
    assert secret.encode("utf-8") not in paths.database_path.read_bytes()
