"""提供双重显式开关保护的 DeepSeek 在线 smoke。"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from rivet.contracts.messages import UserMessage
from rivet.contracts.provider import ModelRequest, ThinkingMode
from rivet.kernel.resources import ResourceScope
from rivet.providers.deepseek import DeepSeekProvider
from rivet.providers.models import DeepSeekConfig, DeepSeekModel

LIVE_ENABLED = os.environ.get("RIVET_LIVE_TEST") == "1"
ROTATION_CONFIRMED = os.environ.get("RIVET_ROTATED_KEY_CONFIRMED") == "1"

pytestmark = pytest.mark.skipif(
    not (LIVE_ENABLED and ROTATION_CONFIRMED),
    reason="需要显式启用 live 且确认凭据已轮换",
)


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_chat_completion_smoke() -> None:
    scope = ResourceScope("provider.live")
    provider = DeepSeekProvider(DeepSeekConfig(max_attempts=1), scope=scope)
    request = ModelRequest(
        model=DeepSeekModel.V4_FLASH,
        messages=(
            UserMessage(
                content="Reply with exactly: ok",
                created_at=datetime.now(UTC),
            ),
        ),
        stream=False,
        thinking=ThinkingMode.DISABLED,
        max_tokens=8,
    )

    completion = await provider.complete(request)

    assert completion.message.content.strip()
    await scope.close()
