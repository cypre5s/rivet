"""定义模型请求、结束原因、用量与 Provider 中立响应契约。"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from rivet.contracts.common import ContractModel
from rivet.contracts.messages import AssistantMessage, Message
from rivet.contracts.tools import ToolDefinition

ModelIdentifier = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$",
        max_length=128,
    ),
]
ProviderIdentifier = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$",
        max_length=64,
    ),
]


class ModelProviderError(RuntimeError):
    """作为 Kernel 可识别但不依赖具体厂商的调用失败基类。"""


class ThinkingMode(StrEnum):
    """映射 DeepSeek Chat Completions 的 thinking.type。"""

    ENABLED = "enabled"
    DISABLED = "disabled"


class ReasoningEffort(StrEnum):
    """保留用户输入档位，Provider 再执行官方兼容映射。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class ModelFinishReason(StrEnum):
    """列出 Chat Completions 当前正式结束原因。"""

    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    TOOL_CALLS = "tool_calls"
    INSUFFICIENT_SYSTEM_RESOURCE = "insufficient_system_resource"


class TokenUsage(ContractModel):
    """记录 prompt、completion、reasoning、总 token 与可选成本。"""

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_total(self) -> Self:
        """拒绝与 prompt 加 completion 不一致的用量。"""
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            raise ValueError("total_tokens 必须等于 prompt_tokens + completion_tokens")
        if self.reasoning_tokens > self.completion_tokens:
            raise ValueError("reasoning_tokens 不得超过 completion_tokens")
        return self


class ModelRequest(ContractModel):
    """提供 Provider 调用所需的完整历史、工具和预算参数。"""

    model: ModelIdentifier
    messages: tuple[Message, ...] = Field(min_length=1)
    tools: tuple[ToolDefinition, ...] = ()
    stream: bool = True
    thinking: ThinkingMode = ThinkingMode.ENABLED
    reasoning_effort: ReasoningEffort = ReasoningEffort.HIGH
    max_tokens: int = Field(default=8_192, gt=0)

    @model_validator(mode="after")
    def _validate_tools(self) -> Self:
        """拒绝同名工具定义，避免服务端选择歧义。"""
        names = [tool.name for tool in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("ModelRequest 工具名不得重复")
        return self


class ModelResponse(ContractModel):
    """保存本地解析后的助手消息、结束原因和确定用量。"""

    provider_id: ProviderIdentifier
    provider_request_id: str | None = Field(default=None, max_length=256)
    model: ModelIdentifier
    message: AssistantMessage
    finish_reason: ModelFinishReason
    usage: TokenUsage

    @model_validator(mode="after")
    def _validate_tool_finish(self) -> Self:
        """保证 tool_calls 结束原因与结构化调用一致。"""
        has_tool_calls = bool(self.message.tool_calls)
        if (self.finish_reason is ModelFinishReason.TOOL_CALLS) != has_tool_calls:
            raise ValueError("tool_calls 结束原因与助手调用列表不一致")
        return self
