"""验证唯一 NDJSON Trace 不保存秘密明文。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.trace.errors import TraceEventTooLargeError
from rivet.trace.paths import RuntimePaths
from rivet.trace.redaction import SecretRedactor
from rivet.trace.store import TraceStore
from tests.fixtures.trace.events import make_event


@pytest.mark.asyncio
async def test_trace_store_redacts_event_and_environment_secret(tmp_path: Path) -> None:
    secret = "sk-" + ("e" * 32)
    repository = tmp_path / "repo"
    repository.mkdir()
    paths = RuntimePaths.for_repository(
        repository,
        environment={
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
    )
    store = TraceStore(
        paths,
        redactor=SecretRedactor(environment={"DEEPSEEK_API_KEY": secret}),
    )
    await store.start()

    await store.emit(make_event(1, payload={"message": "value=" + secret}))
    await store.close()

    assert secret.encode("utf-8") not in paths.events_path.read_bytes()


@pytest.mark.asyncio
async def test_rejected_oversized_secret_never_reaches_ndjson(tmp_path: Path) -> None:
    secret = "sk-" + ("s" * 32)
    repository = tmp_path / "repo"
    repository.mkdir()
    paths = RuntimePaths.for_repository(
        repository,
        environment={
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
    )
    store = TraceStore(
        paths,
        redactor=SecretRedactor(environment={"DEEPSEEK_API_KEY": secret}),
        max_event_bytes=1_024,
    )
    await store.start()

    with pytest.raises(TraceEventTooLargeError):
        await store.emit(
            make_event(
                1,
                payload={"message": f"{secret}:{'x' * 4_096}"},
            )
        )
    await store.close()

    assert paths.events_path.read_bytes() == b""
