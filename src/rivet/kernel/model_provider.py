"""定义 Kernel 依赖的最小模型 Provider 协议。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from rivet.contracts.provider import ModelRequest, ModelResponse

ModelTextDeltaCallback = Callable[[str], Awaitable[None]]


class ModelProvider(Protocol):
    """把厂商协议隐藏在统一的单轮补全边界之后。"""

    async def complete(
        self,
        request: ModelRequest,
        *,
        on_text_delta: ModelTextDeltaCallback | None = None,
    ) -> ModelResponse:
        """执行一次模型补全并返回已校验的本地响应。"""
        ...
