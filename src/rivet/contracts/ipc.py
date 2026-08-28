"""定义 Python Worker 与 TUI 之间的版本化 NDJSON 契约。"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from rivet.contracts.common import (
    ContractModel,
    ErrorDetail,
    EventId,
    EventType,
    RequestId,
)

IPC_PROTOCOL_VERSION = 1


class IpcRequest(ContractModel):
    """表示带请求 ID、方法和严格 JSON 参数的 Worker 请求。"""

    message_type: Literal["request"] = "request"
    protocol_version: Literal[1] = IPC_PROTOCOL_VERSION
    request_id: RequestId
    method: EventType
    params: dict[str, JsonValue] = Field(default_factory=dict)


class IpcResponse(ContractModel):
    """表示与请求一一关联的成功结果或分类错误。"""

    message_type: Literal["response"] = "response"
    protocol_version: Literal[1] = IPC_PROTOCOL_VERSION
    request_id: RequestId
    ok: bool
    result: JsonValue | None = None
    error: ErrorDetail | None = None

    @model_validator(mode="after")
    def _validate_response(self) -> Self:
        """禁止成功响应携带错误或失败响应缺少错误。"""
        if self.ok == (self.error is not None):
            raise ValueError("IPC 响应状态与错误字段不一致")
        return self


class IpcEvent(ContractModel):
    """表示无请求响应关系、按 sequence 排序的实时事件。"""

    message_type: Literal["event"] = "event"
    protocol_version: Literal[1] = IPC_PROTOCOL_VERSION
    event_id: EventId
    event_type: EventType
    sequence: int = Field(ge=0)
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class IpcCancel(ContractModel):
    """表示带独立操作 ID 的在途请求取消消息。"""

    message_type: Literal["cancel"] = "cancel"
    protocol_version: Literal[1] = IPC_PROTOCOL_VERSION
    request_id: RequestId
    target_request_id: RequestId

    @model_validator(mode="after")
    def _validate_target(self) -> Self:
        """取消操作自身不得成为取消目标。"""
        if self.request_id == self.target_request_id:
            raise ValueError("取消请求不得以自身为目标")
        return self


IpcMessage = Annotated[
    IpcRequest | IpcResponse | IpcEvent | IpcCancel,
    Field(discriminator="message_type"),
]
