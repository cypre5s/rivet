"""用脚本 Provider 和真实 Pydantic 工具验证自主循环终止规则。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, JsonValue

from rivet.contracts.messages import (
    AssistantMessage,
    ProviderOpaqueState,
    SystemMessage,
    UserMessage,
)
from rivet.contracts.provider import (
    ModelFinishReason,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)
from rivet.contracts.tools import ToolCall
from rivet.kernel.agent_loop import AgentLoop
from rivet.kernel.agent_models import (
    AgentCompletionStatus,
    AgentLoopConfig,
    AgentLoopState,
    AgentTask,
    AgentTaskMode,
    AgentTerminationReason,
)
from rivet.kernel.agent_tools import AgentTool
from rivet.kernel.resources import ResourceScope
from rivet.providers.deepseek import DeepSeekProvider
from rivet.providers.models import DeepSeekConfig
from tests.fixtures.providers.factories import fake_api_key
from tests.fixtures.providers.http_stream import BlockingByteStream

NOW = datetime(2026, 8, 28, tzinfo=UTC)


class EchoArguments(BaseModel):
    """拒绝额外字段的测试工具参数。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    text: str


class WriteArguments(BaseModel):
    """为修改后 Context 刷新测试提供严格写参数。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    content: str


class ScriptedProvider:
    """按固定顺序返回响应并记录每轮请求。"""

    def __init__(self, responses: Sequence[ModelResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def complete(
        self,
        request: ModelRequest,
        *,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> ModelResponse:
        """返回下一条录制响应。"""
        self.requests.append(request)
        response = self._responses.pop(0)
        if on_text_delta is not None and response.message.content:
            midpoint = max(1, len(response.message.content) // 2)
            await on_text_delta(response.message.content[:midpoint])
            await on_text_delta(response.message.content[midpoint:])
        return response


def _response(
    *,
    content: str = "",
    tool_calls: tuple[ToolCall, ...] = (),
    reasoning: str | None = None,
    finish_reason: ModelFinishReason = ModelFinishReason.STOP,
    total_tokens: int = 2,
    cost_usd: Decimal | None = None,
) -> ModelResponse:
    opaque = (
        ProviderOpaqueState(
            provider_id="deepseek",
            provider_version="1.0.0",
            payload={"reasoning_content": reasoning},
        )
        if reasoning is not None
        else None
    )
    return ModelResponse(
        provider_id="deepseek",
        model="deepseek-v4-pro",
        message=AssistantMessage(
            content=content,
            tool_calls=tool_calls,
            opaque_state=opaque,
            created_at=NOW,
        ),
        finish_reason=finish_reason,
        usage=TokenUsage(
            prompt_tokens=1,
            completion_tokens=total_tokens - 1,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
        ),
    )


def _task(mode: AgentTaskMode = AgentTaskMode.ASK) -> AgentTask:
    return AgentTask(
        run_id="run_agent_test",
        session_id="session_agent_test",
        messages=(UserMessage(content="do it", created_at=NOW),),
        model="deepseek-v4-pro",
        mode=mode,
    )


def _tool_call(
    arguments: dict[str, JsonValue] | None = None,
    *,
    name: str = "test.echo",
    call_id: str = "call_echo_1",
) -> ToolCall:
    return ToolCall(
        tool_call_id=call_id,
        tool_name=name,
        arguments=arguments or {"text": "hello"},
    )


async def _echo(arguments: BaseModel) -> str:
    """返回已验证参数。"""
    validated = EchoArguments.model_validate(arguments.model_dump())
    return f"echo:{validated.text}"


def _echo_tool() -> AgentTool:
    return AgentTool.from_model(
        name="test.echo",
        description="回显文本",
        input_model=EchoArguments,
        executor=_echo,
    )


async def _constant_observation(arguments: BaseModel) -> str:
    """忽略不同的有效参数并制造无新增证据场景。"""
    EchoArguments.model_validate(arguments.model_dump())
    return "same-observation"


def _constant_tool() -> AgentTool:
    return AgentTool.from_model(
        name="test.echo",
        description="返回固定观察",
        input_model=EchoArguments,
        executor=_constant_observation,
    )


@pytest.mark.asyncio
async def test_final_answer_completes_without_tools() -> None:
    provider = ScriptedProvider((_response(content="done"),))
    loop = AgentLoop(provider, tools=(), clock=lambda: NOW)

    result = await loop.run(_task())

    assert result.state is AgentLoopState.COMPLETE
    assert result.termination_reason is AgentTerminationReason.FINAL_ANSWER
    assert result.answer == "done"
    assert result.round_count == 1
    assert result.state_history == (
        AgentLoopState.RECEIVE,
        AgentLoopState.UNDERSTAND,
        AgentLoopState.GATHER_CONTEXT,
        AgentLoopState.PLAN,
        AgentLoopState.PREPARE,
        AgentLoopState.MODEL_CALL,
        AgentLoopState.PARSE_TOOL_CALLS,
        AgentLoopState.EVALUATE,
        AgentLoopState.COMPLETE,
    )


@pytest.mark.asyncio
async def test_agent_loop_forwards_provider_text_deltas_before_final_answer() -> None:
    deltas: list[str] = []

    async def collect_delta(delta: str) -> None:
        deltas.append(delta)

    result = await AgentLoop(
        ScriptedProvider((_response(content="逐步生成回答"),)),
        tools=(),
        text_delta_callback=collect_delta,
    ).run(_task())

    assert deltas == ["逐步生", "成回答"]
    assert result.answer == "逐步生成回答"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    (
        (AgentTaskMode.ASK, AgentCompletionStatus.ANSWERED),
        (AgentTaskMode.PLAN, AgentCompletionStatus.PLANNED),
        (AgentTaskMode.FIX, AgentCompletionStatus.READY_FOR_VERIFICATION),
    ),
)
async def test_provider_stop_has_command_specific_non_verified_status(
    mode: AgentTaskMode,
    expected: AgentCompletionStatus,
) -> None:
    provider = ScriptedProvider(
        (_response(content="已经修复，全部测试通过，verified done。"),)
    )

    result = await AgentLoop(provider, tools=(), clock=lambda: NOW).run(_task(mode))

    assert result.completion_status is expected
    assert all(state.value != "verify" for state in result.state_history)
    assert result.completion_status.value != "VERIFIED"


@pytest.mark.asyncio
async def test_context_gatherer_runs_inside_kernel_before_first_model_call() -> None:
    provider = ScriptedProvider((_response(content="done"),))
    gathered = SystemMessage(content="受限仓库上下文", created_at=NOW)

    async def gather_context(_task: AgentTask) -> tuple[SystemMessage, ...]:
        """返回由正式 Context 能力选择的测试消息。"""
        return (gathered,)

    loop = AgentLoop(
        provider,
        tools=(),
        context_gatherer=gather_context,
        clock=lambda: NOW,
    )

    result = await loop.run(_task())

    assert result.state is AgentLoopState.COMPLETE
    assert provider.requests[0].messages[-1] == gathered


@pytest.mark.asyncio
async def test_transaction_write_refreshes_context_before_next_model_call() -> None:
    current = {"content": "old content"}

    async def write(arguments: BaseModel) -> str:
        values = WriteArguments.model_validate(arguments.model_dump())
        current["content"] = values.content
        return "written"

    async def gather_context(_task: AgentTask) -> tuple[SystemMessage, ...]:
        return (SystemMessage(content=current["content"], created_at=NOW),)

    provider = ScriptedProvider(
        (
            _response(
                tool_calls=(
                    ToolCall(
                        tool_call_id="call_refresh_context",
                        tool_name="file.write_transaction",
                        arguments={"content": "new content"},
                    ),
                ),
                finish_reason=ModelFinishReason.TOOL_CALLS,
            ),
            _response(content="done"),
        )
    )
    tool = AgentTool.from_model(
        name="file.write_transaction",
        description="事务写入",
        input_model=WriteArguments,
        executor=write,
    )

    result = await AgentLoop(
        provider,
        tools=(tool,),
        context_gatherer=gather_context,
        clock=lambda: NOW,
    ).run(_task(AgentTaskMode.FIX))

    assert result.state is AgentLoopState.COMPLETE
    assert any(
        message.content == "new content" for message in provider.requests[1].messages
    )


@pytest.mark.asyncio
async def test_parallel_writes_keep_tool_responses_contiguous_and_refresh_once() -> (
    None
):
    current = {"content": "old content"}
    gather_count = 0

    async def write(arguments: BaseModel) -> str:
        values = WriteArguments.model_validate(arguments.model_dump())
        current["content"] = values.content
        return f"written:{values.content}"

    async def gather_context(_task: AgentTask) -> tuple[SystemMessage, ...]:
        nonlocal gather_count
        gather_count += 1
        return (SystemMessage(content=current["content"], created_at=NOW),)

    provider = ScriptedProvider(
        (
            _response(
                tool_calls=(
                    ToolCall(
                        tool_call_id="call_write_first",
                        tool_name="file.write_transaction",
                        arguments={"content": "first content"},
                    ),
                    ToolCall(
                        tool_call_id="call_write_second",
                        tool_name="file.write_transaction",
                        arguments={"content": "final content"},
                    ),
                ),
                finish_reason=ModelFinishReason.TOOL_CALLS,
            ),
            _response(content="done"),
        )
    )
    tool = AgentTool.from_model(
        name="file.write_transaction",
        description="事务写入",
        input_model=WriteArguments,
        executor=write,
    )

    result = await AgentLoop(
        provider,
        tools=(tool,),
        context_gatherer=gather_context,
        clock=lambda: NOW,
    ).run(_task(AgentTaskMode.FIX))

    assert result.state is AgentLoopState.COMPLETE
    assert gather_count == 2
    follow_up = provider.requests[1].messages
    assert [message.role for message in follow_up[-4:]] == [
        "assistant",
        "tool",
        "tool",
        "system",
    ]
    assert [message.content for message in follow_up[-3:]] == [
        "written:first content",
        "written:final content",
        "final content",
    ]


@pytest.mark.asyncio
async def test_context_gatherer_failure_closes_before_provider_call() -> None:
    provider = ScriptedProvider((_response(content="unused"),))

    async def gather_context(_task: AgentTask) -> tuple[SystemMessage, ...]:
        """模拟上下文能力稳定失败。"""
        raise RuntimeError("fixture failure")

    result = await AgentLoop(
        provider,
        tools=(),
        context_gatherer=gather_context,
        clock=lambda: NOW,
    ).run(_task())

    assert result.state is AgentLoopState.FAILED
    assert result.termination_reason is AgentTerminationReason.CONTEXT_FAILED
    assert provider.requests == []


@pytest.mark.asyncio
async def test_resumed_budget_exhaustion_stops_before_context_and_provider() -> None:
    provider = ScriptedProvider((_response(content="unused"),))
    context_called = False

    async def gather_context(_task: AgentTask) -> tuple[SystemMessage, ...]:
        """记录预算预检是否错误进入了上下文阶段。"""
        nonlocal context_called
        context_called = True
        return ()

    task = AgentTask(
        run_id="run_agent_budget_resume",
        session_id="session_agent_budget_resume",
        messages=(UserMessage(content="continue", created_at=NOW),),
        model="deepseek-v4-pro",
        initial_prompt_tokens=10,
    )
    result = await AgentLoop(
        provider,
        tools=(),
        config=AgentLoopConfig(max_total_tokens=10),
        context_gatherer=gather_context,
        clock=lambda: NOW,
    ).run(task)

    assert result.termination_reason is AgentTerminationReason.TOKEN_BUDGET_EXCEEDED
    assert context_called is False
    assert provider.requests == []


@pytest.mark.asyncio
async def test_thinking_tool_turn_roundtrips_reasoning_and_observation() -> None:
    call = _tool_call()
    provider = ScriptedProvider(
        (
            _response(
                tool_calls=(call,),
                reasoning="keep this reasoning",
                finish_reason=ModelFinishReason.TOOL_CALLS,
            ),
            _response(content="finished"),
        )
    )
    loop = AgentLoop(provider, tools=(_echo_tool(),), clock=lambda: NOW)

    result = await loop.run(_task())

    assert result.state is AgentLoopState.COMPLETE
    assert result.tool_call_count == 1
    assert len(provider.requests) == 2
    assistant = provider.requests[1].messages[-2]
    assert isinstance(assistant, AssistantMessage)
    assert assistant.opaque_state is not None
    assert assistant.opaque_state.payload == {
        "reasoning_content": "keep this reasoning"
    }
    assert provider.requests[1].messages[-1].role == "tool"


@pytest.mark.asyncio
async def test_multiple_tool_calls_execute_in_kernel_order() -> None:
    provider = ScriptedProvider(
        (
            _response(
                tool_calls=(
                    _tool_call({"text": "first"}, call_id="call_echo_first"),
                    _tool_call({"text": "second"}, call_id="call_echo_second"),
                ),
                finish_reason=ModelFinishReason.TOOL_CALLS,
            ),
            _response(content="finished"),
        )
    )
    loop = AgentLoop(provider, tools=(_echo_tool(),), clock=lambda: NOW)

    result = await loop.run(_task())

    assert result.state is AgentLoopState.COMPLETE
    assert result.tool_call_count == 2
    assert provider.requests[1].messages[-2].role == "tool"
    assert provider.requests[1].messages[-2].content == "echo:first"
    assert provider.requests[1].messages[-1].content == "echo:second"


@pytest.mark.asyncio
async def test_unknown_tool_is_intercepted() -> None:
    provider = ScriptedProvider(
        (
            _response(
                tool_calls=(_tool_call(name="missing.tool"),),
                finish_reason=ModelFinishReason.TOOL_CALLS,
            ),
        )
    )
    loop = AgentLoop(provider, tools=(_echo_tool(),), clock=lambda: NOW)

    result = await loop.run(_task())

    assert result.state is AgentLoopState.FAILED
    assert result.termination_reason is AgentTerminationReason.UNKNOWN_TOOL


@pytest.mark.asyncio
async def test_extra_tool_argument_is_rejected_by_local_pydantic() -> None:
    provider = ScriptedProvider(
        (
            _response(
                tool_calls=(_tool_call({"text": "hello", "unexpected": True}),),
                finish_reason=ModelFinishReason.TOOL_CALLS,
            ),
        )
    )
    loop = AgentLoop(provider, tools=(_echo_tool(),), clock=lambda: NOW)

    result = await loop.run(_task())

    assert result.state is AgentLoopState.FAILED
    assert result.termination_reason is AgentTerminationReason.TOOL_VALIDATION_FAILED


@pytest.mark.asyncio
async def test_three_identical_tool_calls_terminate_repeated_action() -> None:
    responses = tuple(
        _response(
            tool_calls=(_tool_call(call_id=f"call_echo_{index}"),),
            finish_reason=ModelFinishReason.TOOL_CALLS,
        )
        for index in range(1, 4)
    )
    provider = ScriptedProvider(responses)
    loop = AgentLoop(
        provider,
        tools=(_echo_tool(),),
        config=AgentLoopConfig(max_consecutive_repeats=3),
        clock=lambda: NOW,
    )

    result = await loop.run(_task())

    assert result.state is AgentLoopState.FAILED
    assert result.termination_reason is AgentTerminationReason.REPEATED_ACTION
    assert result.tool_call_count == 2


@pytest.mark.asyncio
async def test_token_budget_fails_closed_before_tool_execution() -> None:
    provider = ScriptedProvider((_response(content="large", total_tokens=20),))
    loop = AgentLoop(
        provider,
        tools=(),
        config=AgentLoopConfig(max_total_tokens=10),
        clock=lambda: NOW,
    )

    result = await loop.run(_task())

    assert result.state is AgentLoopState.FAILED
    assert result.termination_reason is AgentTerminationReason.TOKEN_BUDGET_EXCEEDED


@pytest.mark.asyncio
async def test_cost_budget_fails_closed() -> None:
    provider = ScriptedProvider(
        (_response(content="costly", cost_usd=Decimal("0.02")),)
    )
    loop = AgentLoop(
        provider,
        tools=(),
        config=AgentLoopConfig(max_cost_usd=Decimal("0.01")),
        clock=lambda: NOW,
    )

    result = await loop.run(_task())

    assert result.termination_reason is AgentTerminationReason.COST_BUDGET_EXCEEDED


@pytest.mark.asyncio
async def test_cost_budget_fails_closed_when_provider_cost_is_unknown() -> None:
    provider = ScriptedProvider((_response(content="unknown cost"),))
    loop = AgentLoop(
        provider,
        tools=(),
        config=AgentLoopConfig(max_cost_usd=Decimal("1")),
        clock=lambda: NOW,
    )

    result = await loop.run(_task())

    assert result.termination_reason is AgentTerminationReason.COST_BUDGET_EXCEEDED


@pytest.mark.asyncio
async def test_round_budget_stops_before_another_model_call() -> None:
    provider = ScriptedProvider(
        (
            _response(
                tool_calls=(_tool_call(),),
                finish_reason=ModelFinishReason.TOOL_CALLS,
            ),
        )
    )
    loop = AgentLoop(
        provider,
        tools=(_echo_tool(),),
        config=AgentLoopConfig(max_rounds=1),
        clock=lambda: NOW,
    )

    result = await loop.run(_task())

    assert result.termination_reason is AgentTerminationReason.MAX_ROUNDS_EXCEEDED
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_tool_budget_stops_before_excess_execution() -> None:
    provider = ScriptedProvider(
        (
            _response(
                tool_calls=(
                    _tool_call({"text": "first"}, call_id="call_echo_first"),
                    _tool_call({"text": "second"}, call_id="call_echo_second"),
                ),
                finish_reason=ModelFinishReason.TOOL_CALLS,
            ),
        )
    )
    loop = AgentLoop(
        provider,
        tools=(_echo_tool(),),
        config=AgentLoopConfig(max_tool_calls=1),
        clock=lambda: NOW,
    )

    result = await loop.run(_task())

    assert result.termination_reason is AgentTerminationReason.MAX_TOOL_CALLS_EXCEEDED
    assert result.tool_call_count == 1


@pytest.mark.asyncio
async def test_wall_budget_stops_before_provider_call() -> None:
    provider = ScriptedProvider((_response(content="unused"),))
    monotonic_values = iter((0.0, 2.0))
    loop = AgentLoop(
        provider,
        tools=(),
        config=AgentLoopConfig(max_wall_seconds=1),
        clock=lambda: NOW,
        monotonic=lambda: next(monotonic_values),
    )

    result = await loop.run(_task())

    assert result.termination_reason is AgentTerminationReason.WALL_TIME_EXCEEDED
    assert provider.requests == []


@pytest.mark.asyncio
async def test_repeated_observation_terminates_no_progress() -> None:
    provider = ScriptedProvider(
        tuple(
            _response(
                tool_calls=(
                    _tool_call(
                        {"text": f"value-{index}"},
                        call_id=f"call_echo_progress_{index}",
                    ),
                ),
                finish_reason=ModelFinishReason.TOOL_CALLS,
            )
            for index in range(1, 4)
        )
    )
    loop = AgentLoop(
        provider,
        tools=(_constant_tool(),),
        config=AgentLoopConfig(max_no_progress_rounds=2),
        clock=lambda: NOW,
    )

    result = await loop.run(_task())

    assert result.termination_reason is AgentTerminationReason.NO_PROGRESS
    assert result.tool_call_count == 3


@pytest.mark.asyncio
async def test_pre_cancelled_task_never_calls_provider() -> None:
    provider = ScriptedProvider((_response(content="unused"),))
    cancel_event = asyncio.Event()
    cancel_event.set()
    loop = AgentLoop(provider, tools=(), clock=lambda: NOW)

    result = await loop.run(_task(), cancel_event=cancel_event)

    assert result.state is AgentLoopState.CANCELLED
    assert result.termination_reason is AgentTerminationReason.USER_CANCELLED
    assert provider.requests == []


@pytest.mark.asyncio
async def test_user_cancel_interrupts_running_tool() -> None:
    tool_started = asyncio.Event()
    tool_cancelled = asyncio.Event()

    async def blocking_tool(arguments: BaseModel) -> str:
        """阻塞至循环取消，并记录协程收到取消。"""
        EchoArguments.model_validate(arguments.model_dump())
        tool_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            tool_cancelled.set()
        return "unreachable"

    tool = AgentTool.from_model(
        name="test.echo",
        description="阻塞测试工具",
        input_model=EchoArguments,
        executor=blocking_tool,
    )
    provider = ScriptedProvider(
        (
            _response(
                tool_calls=(_tool_call(),),
                finish_reason=ModelFinishReason.TOOL_CALLS,
            ),
        )
    )
    cancel_event = asyncio.Event()
    loop = AgentLoop(provider, tools=(tool,), clock=lambda: NOW)

    running = asyncio.create_task(loop.run(_task(), cancel_event=cancel_event))
    await tool_started.wait()
    cancel_event.set()
    result = await running

    assert result.state is AgentLoopState.CANCELLED
    assert tool_cancelled.is_set()


@pytest.mark.asyncio
async def test_user_cancel_releases_provider_http_stream() -> None:
    stream = BlockingByteStream()
    scope = ResourceScope("provider.cancel")
    provider = DeepSeekProvider(
        DeepSeekConfig(max_attempts=1),
        scope=scope,
        environment={"DEEPSEEK_API_KEY": fake_api_key()},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=stream, request=request)
        ),
    )
    cancel_event = asyncio.Event()
    loop = AgentLoop(provider, tools=(), clock=lambda: NOW)

    running = asyncio.create_task(loop.run(_task(), cancel_event=cancel_event))
    await stream.started.wait()
    cancel_event.set()
    result = await running

    assert result.state is AgentLoopState.CANCELLED
    assert result.termination_reason is AgentTerminationReason.USER_CANCELLED
    assert stream.closed
    await scope.close()
