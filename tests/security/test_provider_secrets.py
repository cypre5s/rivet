"""验证 Provider 凭据不会进入错误、repr 或 Trace。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from rivet.kernel.resources import ResourceScope
from rivet.providers.deepseek import DeepSeekProvider
from rivet.providers.errors import CredentialError
from rivet.providers.models import DeepSeekConfig
from rivet.trace.builder import TraceEventBuilder
from rivet.trace.paths import RuntimePaths
from rivet.trace.redaction import SecretRedactor
from rivet.trace.store import TraceStore
from tests.fixtures.providers.factories import fake_api_key, model_request


@pytest.mark.asyncio
async def test_key_does_not_enter_error_repr_or_trace(tmp_path: Path) -> None:
    key = fake_api_key()
    scope = ResourceScope("provider.secret")
    provider = DeepSeekProvider(
        DeepSeekConfig(max_attempts=1),
        scope=scope,
        environment={"DEEPSEEK_API_KEY": key},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, request=request)
        ),
    )

    with pytest.raises(CredentialError) as captured:
        await provider.complete(model_request(stream=False))

    assert key not in str(captured.value)
    assert key not in repr(captured.value)
    assert key not in repr(provider)
    paths = RuntimePaths.for_repository(
        tmp_path,
        environment={"XDG_CACHE_HOME": str(tmp_path / "cache")},
    )
    redactor = SecretRedactor(environment={"DEEPSEEK_API_KEY": key})
    trace = TraceStore(paths, redactor=redactor)
    await trace.start()
    event = TraceEventBuilder(
        redactor=redactor,
        event_id_factory=lambda: "event_provider_error",
    ).build(
        event_type="provider.failed",
        run_id="run_provider_test",
        session_id="session_provider_test",
        payload={"error": str(captured.value)},
    )
    await trace.emit(event)
    await trace.close()

    assert key.encode() not in paths.events_path.read_bytes()
    await scope.close()
