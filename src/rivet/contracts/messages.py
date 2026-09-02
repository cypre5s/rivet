"""定义单次模型循环消息与不进入普通 TUI 文本的 Provider 状态。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints

from rivet.contracts.common import ContractModel, SemVer, Timestamp, ToolCallId
from rivet.contracts.tools import ToolCall

MessageContent = Annotated[str, StringConstraints(min_length=1, max_length=1_000_000)]


class ProviderOpaqueState(ContractModel):
    """封装只可回传 Provider、不作为助手文本展示的状态。"""

    provider_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9._-]+$")
    provider_version: SemVer
    payload: JsonValue


class SystemMessage(ContractModel):
    """表示系统边界与稳定约束。"""

    role: Literal["system"] = "system"
    content: MessageContent
    created_at: Timestamp


class UserMessage(ContractModel):
    """表示用户当前明确提供的任务或说明。"""

    role: Literal["user"] = "user"
    content: MessageContent
    created_at: Timestamp


class AssistantMessage(ContractModel):
    """表示模型文本、工具调用和可选不透明回传状态。"""

    role: Literal["assistant"] = "assistant"
    content: str = Field(default="", max_length=1_000_000)
    tool_calls: tuple[ToolCall, ...] = ()
    opaque_state: ProviderOpaqueState | None = None
    created_at: Timestamp


class ToolMessage(ContractModel):
    """表示已脱敏、已限额的工具观察文本。"""

    role: Literal["tool"] = "tool"
    tool_call_id: ToolCallId
    content: MessageContent
    created_at: Timestamp


Message = Annotated[
    SystemMessage | UserMessage | AssistantMessage | ToolMessage,
    Field(discriminator="role"),
]
