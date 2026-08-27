"""集中构造不含真实凭据的 Provider 测试输入。"""

from __future__ import annotations

from datetime import UTC, datetime

from rivet.contracts.messages import UserMessage
from rivet.contracts.provider import (
    ModelRequest,
    ReasoningEffort,
    ThinkingMode,
)
from rivet.providers.models import DeepSeekModel

FIXED_NOW = datetime(2026, 8, 28, tzinfo=UTC)


def fake_api_key() -> str:
    """返回只能用于离线 Mock 的合成凭据。"""
    return "sk-" + ("p" * 32)


def model_request(*, stream: bool) -> ModelRequest:
    """构造覆盖 thinking 和 reasoning effort 的固定请求。"""
    return ModelRequest(
        model=DeepSeekModel.V4_PRO,
        messages=(UserMessage(content="hello", created_at=FIXED_NOW),),
        stream=stream,
        thinking=ThinkingMode.ENABLED,
        reasoning_effort=ReasoningEffort.HIGH,
        max_tokens=256,
    )
