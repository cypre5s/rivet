"""实现按需连接、可重试且不会泄露凭据的 DeepSeek Provider。"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime

import httpx

from rivet.contracts.messages import (
    Message,
    SystemMessage,
    ToolMessage,
    UserMessage,
)
from rivet.contracts.provider import (
    ModelFinishReason,
    ModelRequest,
    ModelResponse,
    ReasoningEffort,
)
from rivet.contracts.tools import ToolCall, ToolDefinition
from rivet.kernel.model_provider import ModelTextDeltaCallback
from rivet.kernel.resources import ResourceScope
from rivet.providers.errors import (
    ConfigurationError,
    CredentialError,
    ProviderError,
    ProviderOutputIncompleteError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderUnavailableError,
)
from rivet.providers.models import DeepSeekConfig
from rivet.providers.protocol import (
    DeepSeekStreamAccumulator,
    parse_non_streaming_response,
)
from rivet.providers.sse import SSEDecoder

Environment = Mapping[str, str]
Sleeper = Callable[[float], Awaitable[None]]
Clock = Callable[[], datetime]


class DeepSeekProvider:
    """适配 OpenAI-compatible Chat Completions，不参与 Agent 决策。"""

    def __init__(
        self,
        config: DeepSeekConfig,
        *,
        scope: ResourceScope,
        environment: Environment | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleeper = asyncio.sleep,
        clock: Clock | None = None,
    ) -> None:
        self._config = config
        self._scope = scope
        self._environment = environment if environment is not None else os.environ
        self._transport = transport
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(UTC))
        self._client: httpx.AsyncClient | None = None

    def __repr__(self) -> str:
        """只展示非秘密 Provider 身份和根地址。"""
        return (
            f"{type(self).__name__}(base_url={self._config.base_url!r}, "
            f"client_created={self._client is not None!r})"
        )

    def build_request_body(self, request: ModelRequest) -> dict[str, object]:
        """把本地消息与工具契约转换为 DeepSeek JSON 请求。"""
        return self._build_request_body(request)

    def _build_request_body(
        self,
        request: ModelRequest,
    ) -> dict[str, object]:
        """以内部 snake_case 名称原样序列化定义与历史调用。"""
        tool_names = frozenset(definition.name for definition in request.tools)
        body: dict[str, object] = {
            "model": request.model,
            "messages": [
                self._message(message, tool_names=tool_names)
                for message in request.messages
            ],
            "stream": request.stream,
            "max_tokens": request.max_tokens,
            "thinking": {"type": request.thinking.value},
            "reasoning_effort": self._reasoning_effort(request.reasoning_effort),
        }
        if request.tools:
            body["tools"] = [self._tool_definition(tool) for tool in request.tools]
        if request.stream:
            body["stream_options"] = {"include_usage": True}
        return body

    async def complete(
        self,
        request: ModelRequest,
        *,
        on_text_delta: ModelTextDeltaCallback | None = None,
    ) -> ModelResponse:
        """执行一次补全，并只重试明确可恢复的失败。"""
        client = self._get_client()
        for attempt_index in range(self._config.max_attempts):
            retry_after: float | None = None
            emitted_text = False

            async def publish_text_delta(delta: str) -> None:
                nonlocal emitted_text
                emitted_text = True
                if on_text_delta is not None:
                    await on_text_delta(delta)

            try:
                response = await self._complete_once(
                    client,
                    request,
                    on_text_delta=(
                        publish_text_delta if on_text_delta is not None else None
                    ),
                )
                self._ensure_usable(response)
                return response
            except ProviderRateLimitError as error:
                provider_error: ProviderError = error
                retry_after = error.retry_after_seconds
            except ProviderError as error:
                provider_error = error
            except httpx.TransportError:
                provider_error = ProviderUnavailableError(
                    "provider.network_unavailable",
                    "模型服务网络连接失败",
                    retryable=True,
                )
            if emitted_text or (
                not provider_error.retryable
                or attempt_index + 1 >= self._config.max_attempts
            ):
                raise provider_error from None
            await self._sleep(
                retry_after
                if retry_after is not None
                else self._backoff_seconds(attempt_index)
            )
        raise AssertionError("重试循环必须返回或抛出分类错误")

    def _get_client(self) -> httpx.AsyncClient:
        """首次调用时读取 Key、创建客户端并登记生命周期。"""
        if self._client is not None:
            return self._client
        credential_value = self._environment.get("DEEPSEEK_API_KEY")
        if credential_value is None or not credential_value.strip():
            raise ConfigurationError(
                "provider.api_key_missing",
                "缺少 DEEPSEEK_API_KEY 环境变量",
                retryable=False,
            )
        normalized_credential = credential_value.strip()
        client = httpx.AsyncClient(
            base_url=self._config.base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {normalized_credential}"},
            timeout=httpx.Timeout(self._config.timeout_seconds),
            transport=self._transport,
        )
        self._scope.register_client(client, description="DeepSeek HTTP 客户端")
        self._client = client
        return client

    async def _complete_once(
        self,
        client: httpx.AsyncClient,
        request: ModelRequest,
        *,
        on_text_delta: ModelTextDeltaCallback | None,
    ) -> ModelResponse:
        """执行单次 HTTP 请求并解析流式或非流式响应。"""
        tool_names = frozenset(definition.name for definition in request.tools)
        body = self._build_request_body(request)
        if request.stream:
            return await self._complete_stream(
                client,
                body,
                tool_names=tool_names,
                on_text_delta=on_text_delta,
            )
        response = await client.post("chat/completions", json=body)
        self._raise_for_status(response)
        try:
            document: object = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ProviderProtocolError(
                "provider.response_json_invalid",
                "模型响应不是有效 JSON",
                retryable=False,
            ) from error
        return parse_non_streaming_response(
            document,
            created_at=self._clock(),
            tool_names=tool_names,
        )

    async def _complete_stream(
        self,
        client: httpx.AsyncClient,
        body: dict[str, object],
        *,
        tool_names: frozenset[str],
        on_text_delta: ModelTextDeltaCallback | None,
    ) -> ModelResponse:
        """在响应上下文中增量解码 SSE，并确保取消时释放连接。"""
        decoder = SSEDecoder()
        accumulator = DeepSeekStreamAccumulator(tool_names=tool_names)
        async with client.stream("POST", "chat/completions", json=body) as response:
            self._raise_for_status(response)
            async for chunk in response.aiter_bytes():
                for event_data in decoder.feed(chunk):
                    delta = accumulator.consume(event_data)
                    if delta and on_text_delta is not None:
                        await on_text_delta(delta)
            for event_data in decoder.finalize():
                delta = accumulator.consume(event_data)
                if delta and on_text_delta is not None:
                    await on_text_delta(delta)
        return accumulator.response(created_at=self._clock())

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        """只依据状态码分类，不把响应体放入错误。"""
        status = response.status_code
        if 200 <= status < 300:
            return
        if status == 401:
            raise CredentialError(
                "provider.credential_rejected",
                "模型服务拒绝 API 凭据",
                retryable=False,
            )
        if status == 429:
            raise ProviderRateLimitError(
                DeepSeekProvider._retry_after(response.headers.get("Retry-After"))
            )
        if status >= 500:
            raise ProviderUnavailableError(
                "provider.service_unavailable",
                "模型服务暂时不可用",
                retryable=True,
            )
        if 400 <= status < 500:
            raise ProviderRequestError(
                "provider.request_rejected",
                "模型服务拒绝请求参数",
                retryable=False,
            )
        raise ProviderProtocolError(
            "provider.http_status_invalid",
            "模型服务返回无法识别的 HTTP 状态",
            retryable=False,
        )

    @staticmethod
    def _ensure_usable(response: ModelResponse) -> None:
        """把不完整输出和临时资源不足转换为失败关闭错误。"""
        if response.finish_reason is ModelFinishReason.INSUFFICIENT_SYSTEM_RESOURCE:
            raise ProviderUnavailableError(
                "provider.insufficient_system_resource",
                "模型服务推理资源暂时不足",
                retryable=True,
            )
        if response.finish_reason is ModelFinishReason.LENGTH:
            raise ProviderOutputIncompleteError(
                "provider.output_length_exceeded",
                "模型输出达到长度上限，不能作为完整结果",
                retryable=False,
            )
        if response.finish_reason is ModelFinishReason.CONTENT_FILTER:
            raise ProviderOutputIncompleteError(
                "provider.output_filtered",
                "模型输出被内容策略截断，不能作为完整结果",
                retryable=False,
            )

    @staticmethod
    def _message(
        message: Message,
        *,
        tool_names: frozenset[str],
    ) -> dict[str, object]:
        """序列化消息并只回传当前 Provider 的不透明思考状态。"""
        if isinstance(message, (SystemMessage, UserMessage)):
            return {"role": message.role, "content": message.content}
        if isinstance(message, ToolMessage):
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }
        serialized: dict[str, object] = {
            "role": "assistant",
            "content": message.content,
        }
        if message.tool_calls:
            serialized["tool_calls"] = [
                DeepSeekProvider._tool_call(call, tool_names=tool_names)
                for call in message.tool_calls
            ]
        opaque_state = message.opaque_state
        if opaque_state is not None and opaque_state.provider_id == "deepseek":
            payload = opaque_state.payload
            if isinstance(payload, dict):
                reasoning = payload.get("reasoning_content")
                if isinstance(reasoning, str):
                    serialized["reasoning_content"] = reasoning
        return serialized

    @staticmethod
    def _tool_call(
        call: ToolCall,
        *,
        tool_names: frozenset[str],
    ) -> dict[str, object]:
        """把本地 Tool Call 转回兼容协议格式。"""
        if call.tool_name not in tool_names:
            raise ProviderRequestError(
                "provider.tool_name_unknown",
                "历史 Tool Call 名称不在当前工具定义中",
                retryable=False,
            )
        return {
            "id": call.tool_call_id,
            "type": "function",
            "function": {
                "name": call.tool_name,
                "arguments": json.dumps(
                    call.arguments,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            },
        }

    @staticmethod
    def _tool_definition(
        definition: ToolDefinition,
    ) -> dict[str, object]:
        """把本地工具定义包装为 function tool。"""
        return {
            "type": "function",
            "function": {
                "name": definition.name,
                "description": definition.description,
                "parameters": definition.input_schema,
            },
        }

    @staticmethod
    def _reasoning_effort(effort: ReasoningEffort) -> str:
        """把扩展档位收敛到 DeepSeek 当前接受的 low/high。"""
        return "low" if effort is ReasoningEffort.LOW else "high"

    def _backoff_seconds(self, attempt_index: int) -> float:
        """计算有上限且无需随机状态的指数退避。"""
        return min(
            self._config.max_backoff_seconds,
            self._config.base_backoff_seconds * (2**attempt_index),
        )

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        """只接受非负秒数，忽略日期形式和畸形值。"""
        if value is None:
            return None
        try:
            seconds = float(value)
        except ValueError:
            return None
        return seconds if seconds >= 0 else None
