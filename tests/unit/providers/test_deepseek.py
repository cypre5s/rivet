"""验证 DeepSeek 离线 HTTP、流聚合、重试和结束原因。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

import httpx
import pytest

from rivet.contracts.messages import AssistantMessage, ProviderOpaqueState, ToolMessage
from rivet.contracts.provider import ModelFinishReason
from rivet.contracts.tools import ToolCall, ToolDefinition
from rivet.kernel.resources import ResourceScope
from rivet.providers.deepseek import DeepSeekProvider
from rivet.providers.errors import (
    ConfigurationError,
    ProviderOutputIncompleteError,
    ProviderProtocolError,
    ProviderRequestError,
    ProviderUnavailableError,
)
from rivet.providers.models import DeepSeekConfig
from tests.fixtures.providers.factories import (
    FIXED_NOW,
    fake_api_key,
    model_request,
)
from tests.fixtures.providers.http_stream import RecordedByteStream

FIXTURE_DIRECTORY = Path("tests/fixtures/providers")
DEEPSEEK_TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
LOCAL_TOOL_CALL_ID_PATTERN = re.compile(r"^call_[a-z0-9][a-z0-9_-]{0,62}$")


def test_default_timeout_allows_reasoning_tool_turns() -> None:
    """推理模型在工具观察后的续写不应被一分钟默认值过早终止。"""
    assert DeepSeekConfig().timeout_seconds == 180.0


def _tool_definition(name: str) -> ToolDefinition:
    """构造带点分本地名称的最小工具契约。"""
    return ToolDefinition(
        name=name,
        description=f"调用 {name}",
        input_schema={"type": "object", "properties": {}},
    )


@pytest.mark.asyncio
async def test_streaming_and_non_streaming_fixtures_are_equivalent() -> None:
    non_stream_document = cast(
        dict[str, object],
        json.loads((FIXTURE_DIRECTORY / "non_stream.json").read_text(encoding="utf-8")),
    )
    sse_bytes = (FIXTURE_DIRECTORY / "stream.sse").read_bytes()

    def non_stream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=non_stream_document, request=request)

    byte_stream = RecordedByteStream(bytes((byte,)) for byte in sse_bytes)

    def stream_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=byte_stream, request=request)

    non_stream_scope = ResourceScope("provider.non_stream")
    stream_scope = ResourceScope("provider.stream")
    non_stream_provider = DeepSeekProvider(
        DeepSeekConfig(),
        scope=non_stream_scope,
        environment={"DEEPSEEK_API_KEY": fake_api_key()},
        transport=httpx.MockTransport(non_stream_handler),
        clock=lambda: FIXED_NOW,
    )
    stream_provider = DeepSeekProvider(
        DeepSeekConfig(),
        scope=stream_scope,
        environment={"DEEPSEEK_API_KEY": fake_api_key()},
        transport=httpx.MockTransport(stream_handler),
        clock=lambda: FIXED_NOW,
    )

    non_stream = await non_stream_provider.complete(model_request(stream=False))
    streamed = await stream_provider.complete(model_request(stream=True))

    assert streamed.message == non_stream.message
    assert streamed.finish_reason is ModelFinishReason.STOP
    assert streamed.usage == non_stream.usage
    assert byte_stream.closed
    await non_stream_scope.close()
    await stream_scope.close()


@pytest.mark.asyncio
async def test_tool_arguments_split_into_twenty_chunks_are_aggregated() -> None:
    arguments = json.dumps({"text": "split-value"}, separators=(",", ":"))
    fragment_count = 20
    fragments = [
        arguments[
            index * len(arguments) // fragment_count : (index + 1)
            * len(arguments)
            // fragment_count
        ]
        for index in range(fragment_count)
    ]
    assert len(fragments) == 20
    chunks: list[bytes] = []
    for index, fragment in enumerate(fragments):
        tool_delta: dict[str, object] = {
            "index": 0,
            "function": {"arguments": fragment},
        }
        if index == 0:
            tool_delta.update(
                {"id": "call_00_qkQ3WypYvo8eZZaS55mT5009", "type": "function"}
            )
            cast(dict[str, object], tool_delta["function"])["name"] = "test_echo"
        document = {
            "id": "fragmented",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "deepseek-v4-pro",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "reasoning_content": "think" if index == 0 else None,
                        "tool_calls": [tool_delta],
                    },
                    "finish_reason": None,
                }
            ],
        }
        chunks.append(b"data: " + json.dumps(document).encode() + b"\n\n")
    finish: dict[str, object] = {
        "id": "fragmented",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "deepseek-v4-pro",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    chunks.extend(
        (b"data: " + json.dumps(finish).encode() + b"\n\n", b"data: [DONE]\n\n")
    )
    scope = ResourceScope("provider.fragment")
    provider = DeepSeekProvider(
        DeepSeekConfig(),
        scope=scope,
        environment={"DEEPSEEK_API_KEY": fake_api_key()},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=RecordedByteStream(chunks), request=request
            )
        ),
        clock=lambda: FIXED_NOW,
    )

    request = model_request(stream=True).model_copy(
        update={"tools": (_tool_definition("test.echo"),)}
    )

    completion = await provider.complete(request)

    assert completion.message.tool_calls[0].arguments == {"text": "split-value"}
    assert completion.message.tool_calls[0].tool_name == "test.echo"
    assert LOCAL_TOOL_CALL_ID_PATTERN.fullmatch(
        completion.message.tool_calls[0].tool_call_id
    )
    await scope.close()


def test_request_body_encodes_dotted_names_without_changing_local_contract() -> None:
    provider = DeepSeekProvider(
        DeepSeekConfig(),
        scope=ResourceScope("provider.tool_alias.body"),
        environment={"DEEPSEEK_API_KEY": fake_api_key()},
    )
    definitions = (
        _tool_definition("workspace.info"),
        _tool_definition("file.read_text"),
    )
    request = model_request(stream=False).model_copy(update={"tools": definitions})

    body = provider.build_request_body(request)

    tools = cast(list[dict[str, object]], body["tools"])
    wire_names = [cast(dict[str, object], tool["function"])["name"] for tool in tools]
    assert wire_names == ["workspace_info", "file_read_text"]
    assert all(
        isinstance(name, str) and DEEPSEEK_TOOL_NAME_PATTERN.fullmatch(name)
        for name in wire_names
    )
    assert [definition.name for definition in definitions] == [
        "workspace.info",
        "file.read_text",
    ]


def test_request_body_disambiguates_colliding_and_overlong_tool_aliases() -> None:
    provider = DeepSeekProvider(
        DeepSeekConfig(),
        scope=ResourceScope("provider.tool_alias.collision"),
        environment={"DEEPSEEK_API_KEY": fake_api_key()},
    )
    definitions = (
        _tool_definition("alpha.beta_gamma"),
        _tool_definition("alpha_beta.gamma"),
        _tool_definition(f"repository.{('very_long_segment_' * 8).rstrip('_')}"),
    )
    request = model_request(stream=False).model_copy(update={"tools": definitions})

    body = provider.build_request_body(request)

    tools = cast(list[dict[str, object]], body["tools"])
    wire_names = [cast(dict[str, object], tool["function"])["name"] for tool in tools]
    assert len(wire_names) == len(set(wire_names)) == 3
    assert all(
        isinstance(name, str) and DEEPSEEK_TOOL_NAME_PATTERN.fullmatch(name)
        for name in wire_names
    )


@pytest.mark.asyncio
async def test_non_streaming_tool_alias_is_restored_to_local_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        request_body = json.loads(request.content)
        tools = cast(list[dict[str, object]], request_body["tools"])
        function = cast(dict[str, object], tools[0]["function"])
        assert function["name"] == "workspace_info"
        return httpx.Response(
            200,
            json={
                "id": "tool_alias_non_stream",
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_00_qkQ3WypYvo8eZZaS55mT5009",
                                    "type": "function",
                                    "function": {
                                        "name": "workspace_info",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
            request=request,
        )

    scope = ResourceScope("provider.tool_alias.non_stream")
    provider = DeepSeekProvider(
        DeepSeekConfig(),
        scope=scope,
        environment={"DEEPSEEK_API_KEY": fake_api_key()},
        transport=httpx.MockTransport(handler),
        clock=lambda: FIXED_NOW,
    )
    request = model_request(stream=False).model_copy(
        update={"tools": (_tool_definition("workspace.info"),)}
    )

    completion = await provider.complete(request)

    assert completion.message.tool_calls[0].tool_name == "workspace.info"
    assert LOCAL_TOOL_CALL_ID_PATTERN.fullmatch(
        completion.message.tool_calls[0].tool_call_id
    )
    await scope.close()


@pytest.mark.asyncio
async def test_unknown_wire_tool_alias_fails_closed() -> None:
    document = {
        "id": "unknown_tool_alias",
        "model": "deepseek-v4-pro",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_alias_unknown",
                            "type": "function",
                            "function": {
                                "name": "unknown_alias",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    scope = ResourceScope("provider.tool_alias.unknown")
    provider = DeepSeekProvider(
        DeepSeekConfig(),
        scope=scope,
        environment={"DEEPSEEK_API_KEY": fake_api_key()},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=document, request=request)
        ),
        clock=lambda: FIXED_NOW,
    )
    request = model_request(stream=False).model_copy(
        update={"tools": (_tool_definition("workspace.info"),)}
    )

    with pytest.raises(ProviderProtocolError, match="Tool Call"):
        await provider.complete(request)

    await scope.close()


def test_request_body_preserves_reasoning_content_for_tool_turn() -> None:
    provider = DeepSeekProvider(
        DeepSeekConfig(),
        scope=ResourceScope("provider.body"),
        environment={"DEEPSEEK_API_KEY": fake_api_key()},
    )
    assistant = AssistantMessage(
        content="",
        tool_calls=(
            ToolCall(
                tool_call_id="call_reasoning_1",
                tool_name="test.echo",
                arguments={"text": "hello"},
            ),
        ),
        opaque_state=ProviderOpaqueState(
            provider_id="deepseek",
            provider_version="1.0.0",
            payload={"reasoning_content": "must-return"},
        ),
        created_at=FIXED_NOW,
    )
    tool_message = ToolMessage(
        tool_call_id="call_reasoning_1",
        content="echo:hello",
        created_at=FIXED_NOW,
    )
    request = model_request(stream=False).model_copy(
        update={
            "messages": (
                *model_request(stream=False).messages,
                assistant,
                tool_message,
            ),
            "tools": (_tool_definition("test.echo"),),
        }
    )

    body = provider.build_request_body(request)

    messages = cast(list[dict[str, object]], body["messages"])
    assert messages[-2]["reasoning_content"] == "must-return"
    assistant_tool_calls = cast(list[dict[str, object]], messages[-2]["tool_calls"])
    assistant_function = cast(dict[str, object], assistant_tool_calls[0]["function"])
    assert assistant_function["name"] == "test_echo"
    assert messages[-1]["tool_call_id"] == "call_reasoning_1"
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"


def test_history_tool_name_missing_from_current_definitions_fails_closed() -> None:
    provider = DeepSeekProvider(
        DeepSeekConfig(),
        scope=ResourceScope("provider.tool_alias.history_missing"),
        environment={"DEEPSEEK_API_KEY": fake_api_key()},
    )
    assistant = AssistantMessage(
        content="",
        tool_calls=(
            ToolCall(
                tool_call_id="call_history_missing",
                tool_name="test.echo",
                arguments={},
            ),
        ),
        created_at=FIXED_NOW,
    )
    request = model_request(stream=False).model_copy(
        update={"messages": (*model_request(stream=False).messages, assistant)}
    )

    with pytest.raises(ProviderRequestError, match="当前工具定义"):
        provider.build_request_body(request)


@pytest.mark.asyncio
async def test_missing_key_fails_before_client_creation() -> None:
    scope = ResourceScope("provider.missing_key")
    provider = DeepSeekProvider(DeepSeekConfig(), scope=scope, environment={})

    with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY"):
        await provider.complete(model_request(stream=False))

    assert scope.counts().open_client_count == 0
    await scope.close()


@pytest.mark.asyncio
async def test_429_uses_retry_after_then_succeeds() -> None:
    calls = 0
    delays: list[float] = []
    success_document = cast(
        dict[str, object],
        json.loads((FIXTURE_DIRECTORY / "non_stream.json").read_text(encoding="utf-8")),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, json=success_document, request=request)

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    scope = ResourceScope("provider.retry")
    provider = DeepSeekProvider(
        DeepSeekConfig(max_attempts=2),
        scope=scope,
        environment={"DEEPSEEK_API_KEY": fake_api_key()},
        transport=httpx.MockTransport(handler),
        sleep=record_sleep,
        clock=lambda: FIXED_NOW,
    )

    completion = await provider.complete(model_request(stream=False))

    assert completion.message.content == "fixture answer"
    assert calls == 2
    assert delays == [0.0]
    await scope.close()


@pytest.mark.asyncio
async def test_400_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, request=request)

    scope = ResourceScope("provider.bad_request")
    provider = DeepSeekProvider(
        DeepSeekConfig(max_attempts=3),
        scope=scope,
        environment={"DEEPSEEK_API_KEY": fake_api_key()},
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProviderRequestError) as captured:
        await provider.complete(model_request(stream=False))

    assert not captured.value.retryable
    assert calls == 1
    await scope.close()


@pytest.mark.asyncio
async def test_length_finish_reason_fails_closed() -> None:
    document = cast(
        dict[str, object],
        json.loads((FIXTURE_DIRECTORY / "non_stream.json").read_text(encoding="utf-8")),
    )
    cast(dict[str, object], cast(list[object], document["choices"])[0])[
        "finish_reason"
    ] = "length"
    scope = ResourceScope("provider.length")
    provider = DeepSeekProvider(
        DeepSeekConfig(),
        scope=scope,
        environment={"DEEPSEEK_API_KEY": fake_api_key()},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=document, request=request)
        ),
    )

    with pytest.raises(ProviderOutputIncompleteError):
        await provider.complete(model_request(stream=False))

    await scope.close()


@pytest.mark.asyncio
async def test_interrupted_stream_releases_response_and_fails_after_budget() -> None:
    sse_bytes = (FIXTURE_DIRECTORY / "stream.sse").read_bytes()
    midpoint = len(sse_bytes) // 2
    stream = RecordedByteStream(
        (sse_bytes[:midpoint], sse_bytes[midpoint:]),
        fail_after_chunks=1,
    )
    scope = ResourceScope("provider.interrupted")
    provider = DeepSeekProvider(
        DeepSeekConfig(max_attempts=1),
        scope=scope,
        environment={"DEEPSEEK_API_KEY": fake_api_key()},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream, request=request)
        ),
    )

    with pytest.raises(ProviderUnavailableError):
        await provider.complete(model_request(stream=True))

    assert stream.closed
    await scope.close()


@pytest.mark.asyncio
async def test_insufficient_system_resource_is_retried() -> None:
    calls = 0
    delays: list[float] = []
    document = cast(
        dict[str, object],
        json.loads((FIXTURE_DIRECTORY / "non_stream.json").read_text(encoding="utf-8")),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        response_document = cast(dict[str, object], json.loads(json.dumps(document)))
        if calls == 1:
            choices = cast(list[object], response_document["choices"])
            cast(dict[str, object], choices[0])["finish_reason"] = (
                "insufficient_system_resource"
            )
        return httpx.Response(200, json=response_document, request=request)

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    scope = ResourceScope("provider.insufficient")
    provider = DeepSeekProvider(
        DeepSeekConfig(max_attempts=2, base_backoff_seconds=0),
        scope=scope,
        environment={"DEEPSEEK_API_KEY": fake_api_key()},
        transport=httpx.MockTransport(handler),
        sleep=record_sleep,
        clock=lambda: FIXED_NOW,
    )

    completion = await provider.complete(model_request(stream=False))

    assert completion.finish_reason is ModelFinishReason.STOP
    assert calls == 2
    assert delays == [0.0]
    await scope.close()
