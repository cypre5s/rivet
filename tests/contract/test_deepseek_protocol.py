"""验证 DeepSeek wire schema、模型选择和本地 Tool 参数解析。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rivet.contracts.messages import UserMessage
from rivet.contracts.provider import ModelRequest, ReasoningEffort, ThinkingMode
from rivet.providers.errors import ProviderProtocolError
from rivet.providers.models import DeepSeekModel
from rivet.providers.protocol import parse_non_streaming_response

NOW = datetime(2026, 8, 28, tzinfo=UTC)


def test_current_models_and_request_options_match_official_protocol() -> None:
    request = ModelRequest(
        model=DeepSeekModel.V4_FLASH,
        messages=(UserMessage(content="test", created_at=NOW),),
        stream=False,
        thinking=ThinkingMode.DISABLED,
        reasoning_effort=ReasoningEffort.MAX,
        max_tokens=128,
    )

    assert request.model == "deepseek-v4-flash"
    assert request.thinking is ThinkingMode.DISABLED


def test_recorded_non_stream_response_parses_strictly() -> None:
    document = json.loads(
        Path("tests/fixtures/providers/non_stream.json").read_text(encoding="utf-8")
    )

    completion = parse_non_streaming_response(document, created_at=NOW)

    assert completion.message.content == "fixture answer"
    assert completion.usage.reasoning_tokens == 2


@pytest.mark.parametrize(
    "arguments",
    ("{invalid", "[]"),
)
def test_invalid_or_non_object_tool_arguments_are_rejected(arguments: str) -> None:
    document = {
        "id": "bad_tool",
        "model": "deepseek-v4-pro",
        "object": "chat.completion",
        "created": 1,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_bad_1",
                            "type": "function",
                            "function": {
                                "name": "test.echo",
                                "arguments": arguments,
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    with pytest.raises(ProviderProtocolError, match="arguments"):
        parse_non_streaming_response(document, created_at=NOW)
