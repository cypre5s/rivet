"""校验 DeepSeek wire JSON 并聚合流式文本、思考与 Tool Call。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from rivet.contracts.messages import AssistantMessage, ProviderOpaqueState
from rivet.contracts.provider import (
    ModelFinishReason,
    ModelResponse,
    TokenUsage,
)
from rivet.contracts.tools import ToolCall
from rivet.providers.errors import ProviderProtocolError


class _WireModel(BaseModel):
    """允许官方增加无关字段，但严格校验 Rivet 使用的字段。"""

    model_config = ConfigDict(
        extra="ignore",
        strict=True,
        hide_input_in_errors=True,
    )


class WireFunctionCall(_WireModel):
    """描述非流式 function 调用。"""

    name: str
    arguments: str


class WireToolCall(_WireModel):
    """描述非流式 Tool Call。"""

    id: str
    type: Literal["function"]
    function: WireFunctionCall


class WireAssistantMessage(_WireModel):
    """描述 DeepSeek assistant 消息。"""

    role: Literal["assistant"]
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[WireToolCall] | None = None


class WireChoice(_WireModel):
    """描述非流式唯一 choice。"""

    index: int
    message: WireAssistantMessage
    finish_reason: str


class WireCompletionTokenDetails(_WireModel):
    """描述 completion 中的 reasoning token。"""

    reasoning_tokens: int = Field(default=0, ge=0)


class WireUsage(_WireModel):
    """描述官方 usage 字段。"""

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    completion_tokens_details: WireCompletionTokenDetails | None = None


class WireChatCompletion(_WireModel):
    """描述非流式 Chat Completion 响应。"""

    id: str
    model: str
    choices: list[WireChoice] = Field(min_length=1)
    usage: WireUsage


class WireFunctionDelta(_WireModel):
    """描述可分片的 function 名与 arguments。"""

    name: str | None = None
    arguments: str | None = None


class WireToolCallDelta(_WireModel):
    """描述按 index 聚合的流式 Tool Call 分片。"""

    index: int = Field(ge=0)
    id: str | None = None
    type: Literal["function"] | None = None
    function: WireFunctionDelta | None = None


class WireDelta(_WireModel):
    """描述单个流式 assistant 增量。"""

    role: Literal["assistant"] | None = None
    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[WireToolCallDelta] | None = None


class WireChunkChoice(_WireModel):
    """描述单个流式 choice。"""

    index: int
    delta: WireDelta
    finish_reason: str | None = None


class WireChatChunk(_WireModel):
    """描述 SSE data 中的 Chat Completion chunk。"""

    id: str | None = None
    model: str | None = None
    choices: list[WireChunkChoice]
    usage: WireUsage | None = None


def _protocol_error(code: str, summary: str) -> ProviderProtocolError:
    """构造不包含 wire 输入内容的协议错误。"""
    return ProviderProtocolError(code, summary, retryable=False)


def _finish_reason(value: str) -> ModelFinishReason:
    """拒绝未知结束原因。"""
    try:
        return ModelFinishReason(value)
    except ValueError as error:
        raise _protocol_error(
            "provider.finish_reason_unknown", "模型返回未知 finish_reason"
        ) from error


def _usage(wire_usage: WireUsage) -> TokenUsage:
    """把 wire usage 转换为严格本地契约。"""
    reasoning_tokens = (
        wire_usage.completion_tokens_details.reasoning_tokens
        if wire_usage.completion_tokens_details is not None
        else 0
    )
    try:
        return TokenUsage(
            prompt_tokens=wire_usage.prompt_tokens,
            completion_tokens=wire_usage.completion_tokens,
            total_tokens=wire_usage.total_tokens,
            reasoning_tokens=reasoning_tokens,
        )
    except ValidationError as error:
        raise _protocol_error(
            "provider.usage_invalid", "模型 usage 字段不一致"
        ) from error


def _arguments(arguments: str) -> dict[str, JsonValue]:
    """先解析 JSON，再要求根节点为 object。"""
    try:
        raw_arguments: object = json.loads(arguments)
    except json.JSONDecodeError as error:
        raise _protocol_error(
            "provider.tool_arguments_json_invalid",
            "模型 Tool arguments 不是有效 JSON",
        ) from error
    if not isinstance(raw_arguments, dict):
        raise _protocol_error(
            "provider.tool_arguments_not_object",
            "模型 Tool arguments 根节点必须是 object",
        )
    return cast(dict[str, JsonValue], raw_arguments)


def _tool_call(call: WireToolCall) -> ToolCall:
    """构造仍需业务 Schema 校验的本地 ToolCall。"""
    try:
        return ToolCall(
            tool_call_id=call.id,
            tool_name=call.function.name,
            arguments=_arguments(call.function.arguments),
        )
    except ValidationError as error:
        raise _protocol_error(
            "provider.tool_call_contract_invalid",
            "模型 Tool Call 不满足本地基础契约",
        ) from error


def _assistant_message(
    *,
    content: str | None,
    reasoning_content: str | None,
    tool_calls: tuple[ToolCall, ...],
    created_at: datetime,
) -> AssistantMessage:
    """把 reasoning_content 放入默认不展示的不透明状态。"""
    opaque_state = (
        ProviderOpaqueState(
            provider_id="deepseek",
            provider_version="1.0.0",
            payload={"reasoning_content": reasoning_content},
        )
        if reasoning_content is not None
        else None
    )
    return AssistantMessage(
        content=content or "",
        tool_calls=tool_calls,
        opaque_state=opaque_state,
        created_at=created_at,
    )


def parse_non_streaming_response(
    document: object,
    *,
    created_at: datetime,
) -> ModelResponse:
    """严格解析非流式响应的唯一 index=0 choice。"""
    try:
        response = WireChatCompletion.model_validate(document)
    except ValidationError as error:
        raise _protocol_error(
            "provider.response_schema_invalid", "模型响应不满足 Chat Completion 契约"
        ) from error
    choices = [choice for choice in response.choices if choice.index == 0]
    if len(choices) != 1:
        raise _protocol_error(
            "provider.choice_invalid", "模型响应必须包含唯一 index=0 choice"
        )
    choice = choices[0]
    calls = tuple(_tool_call(call) for call in choice.message.tool_calls or ())
    try:
        return ModelResponse(
            provider_id="deepseek",
            provider_request_id=response.id,
            model=response.model,
            message=_assistant_message(
                content=choice.message.content,
                reasoning_content=choice.message.reasoning_content,
                tool_calls=calls,
                created_at=created_at,
            ),
            finish_reason=_finish_reason(choice.finish_reason),
            usage=_usage(response.usage),
        )
    except ValidationError as error:
        raise _protocol_error(
            "provider.response_contract_invalid",
            "模型响应与本地结束原因契约不一致",
        ) from error


@dataclass(slots=True)
class _ToolCallFragments:
    """按 index 累积流式 Tool Call 字段。"""

    tool_call_id: str = ""
    name: str = ""
    arguments: str = ""


def _merge_once(current: str, fragment: str | None) -> str:
    """忽略服务端重复完整字段，否则拼接真实分片。"""
    if not fragment:
        return current
    if not current:
        return fragment
    if fragment == current:
        return current
    return current + fragment


class DeepSeekStreamAccumulator:
    """聚合 SSE JSON 中的 content、reasoning、usage 与多个 Tool Call。"""

    def __init__(self) -> None:
        self._provider_request_id: str | None = None
        self._model: str | None = None
        self._content_parts: list[str] = []
        self._reasoning_parts: list[str] = []
        self._reasoning_seen = False
        self._tool_calls: dict[int, _ToolCallFragments] = {}
        self._finish_reason: ModelFinishReason | None = None
        self._usage: TokenUsage | None = None
        self.done = False

    def consume(self, data: str) -> None:
        """消费单个 SSE data 字段；`[DONE]` 只标记终止。"""
        if self.done:
            raise _protocol_error(
                "provider.sse_after_done", "模型在 [DONE] 后继续发送数据"
            )
        if data == "[DONE]":
            self.done = True
            return
        try:
            chunk = WireChatChunk.model_validate_json(data)
        except ValidationError as error:
            raise _protocol_error(
                "provider.chunk_schema_invalid", "模型 SSE chunk 不满足契约"
            ) from error
        if chunk.id is not None:
            self._provider_request_id = chunk.id
        if chunk.model is not None:
            self._model = chunk.model
        if chunk.usage is not None:
            self._usage = _usage(chunk.usage)
        for choice in chunk.choices:
            if choice.index != 0:
                continue
            delta = choice.delta
            if delta.content is not None:
                self._content_parts.append(delta.content)
            if delta.reasoning_content is not None:
                self._reasoning_seen = True
                self._reasoning_parts.append(delta.reasoning_content)
            for tool_delta in delta.tool_calls or ():
                fragments = self._tool_calls.setdefault(
                    tool_delta.index, _ToolCallFragments()
                )
                fragments.tool_call_id = _merge_once(
                    fragments.tool_call_id, tool_delta.id
                )
                if tool_delta.function is not None:
                    fragments.name = _merge_once(
                        fragments.name, tool_delta.function.name
                    )
                    if tool_delta.function.arguments is not None:
                        fragments.arguments += tool_delta.function.arguments
            if choice.finish_reason is not None:
                self._finish_reason = _finish_reason(choice.finish_reason)

    def response(self, *, created_at: datetime) -> ModelResponse:
        """在收到 [DONE] 后构造与非流式相同的本地响应。"""
        if not self.done:
            raise ProviderProtocolError(
                "provider.stream_interrupted",
                "模型 SSE 在 [DONE] 前中断",
                retryable=True,
            )
        if self._finish_reason is None or self._usage is None or self._model is None:
            raise _protocol_error(
                "provider.stream_incomplete", "模型 SSE 缺少结束原因、usage 或 model"
            )
        calls: list[ToolCall] = []
        if self._tool_calls:
            expected_indexes = list(range(len(self._tool_calls)))
            if sorted(self._tool_calls) != expected_indexes:
                raise _protocol_error(
                    "provider.tool_index_gap", "模型流式 Tool Call index 不连续"
                )
            for index in expected_indexes:
                fragments = self._tool_calls[index]
                if not fragments.tool_call_id or not fragments.name:
                    raise _protocol_error(
                        "provider.tool_fragment_incomplete",
                        "模型流式 Tool Call 缺少 id 或 name",
                    )
                try:
                    calls.append(
                        ToolCall(
                            tool_call_id=fragments.tool_call_id,
                            tool_name=fragments.name,
                            arguments=_arguments(fragments.arguments),
                        )
                    )
                except ValidationError as error:
                    raise _protocol_error(
                        "provider.tool_call_contract_invalid",
                        "模型流式 Tool Call 不满足本地契约",
                    ) from error
        try:
            return ModelResponse(
                provider_id="deepseek",
                provider_request_id=self._provider_request_id,
                model=self._model,
                message=_assistant_message(
                    content="".join(self._content_parts),
                    reasoning_content=(
                        "".join(self._reasoning_parts) if self._reasoning_seen else None
                    ),
                    tool_calls=tuple(calls),
                    created_at=created_at,
                ),
                finish_reason=self._finish_reason,
                usage=self._usage,
            )
        except ValidationError as error:
            raise _protocol_error(
                "provider.stream_contract_invalid",
                "模型流式响应与本地结束原因契约不一致",
            ) from error
