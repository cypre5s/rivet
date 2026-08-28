"""实现 Provider 中立、预算受控且可确定终止的 Agent Loop。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from rivet.contracts.messages import Message, ToolMessage
from rivet.contracts.provider import (
    ModelFinishReason,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)
from rivet.contracts.tools import ToolCall
from rivet.kernel.agent_models import (
    AgentCompletionStatus,
    AgentLoopConfig,
    AgentLoopResult,
    AgentLoopState,
    AgentTask,
    AgentTerminationReason,
)
from rivet.kernel.agent_tools import AgentTool, AgentToolValidationError
from rivet.kernel.model_provider import ModelProvider

Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]
ContextGatherer = Callable[[AgentTask], Awaitable[tuple[Message, ...]]]


@dataclass(frozen=True, slots=True)
class AgentProgress:
    """提供模型响应与工具观察后的可持久化运行事实。"""

    messages: tuple[Message, ...]
    round_count: int
    tool_call_count: int
    usage: TokenUsage


ProgressCallback = Callable[[AgentProgress], Awaitable[None]]


class _RunCancelled(Exception):
    """在内部竞速中传播用户取消，不跨越 Kernel 公共边界。"""


def _may_change_workspace(tool_name: str) -> bool:
    """识别会使此前 Context 失效的事务写入或本地进程。"""
    return tool_name in {
        "file.write_transaction",
        "file.replace_transaction",
        "file.create_transaction",
        "file.delete_transaction",
        "process.run",
    }


class AgentLoop:
    """逐轮调用模型，并由本地规则决定工具执行和终止。"""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        tools: tuple[AgentTool, ...],
        config: AgentLoopConfig | None = None,
        context_gatherer: ContextGatherer | None = None,
        progress_callback: ProgressCallback | None = None,
        clock: Clock | None = None,
        monotonic: MonotonicClock = time.monotonic,
    ) -> None:
        self._provider = provider
        self._config = config or AgentLoopConfig()
        self._context_gatherer = context_gatherer
        self._progress_callback = progress_callback
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self._tools = {tool.definition.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("Agent Loop 工具名不得重复")

    async def run(
        self,
        task: AgentTask,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> AgentLoopResult:
        """运行至本地成功条件或任一失败关闭条件。"""
        messages: list[Message] = list(task.messages)
        round_count = task.initial_round_count
        tool_call_count = task.initial_tool_call_count
        prompt_tokens = task.initial_prompt_tokens
        completion_tokens = task.initial_completion_tokens
        reasoning_tokens = task.initial_reasoning_tokens
        total_cost = task.initial_cost_usd
        started_at = self._monotonic()
        last_action_fingerprint: str | None = None
        consecutive_repeats = 0
        observation_fingerprints: set[str] = set()
        no_progress_rounds = 0
        state_history: list[AgentLoopState] = []

        def transition(state: AgentLoopState) -> None:
            """只记录真实发生且不与上一项重复的状态变化。"""
            if not state_history or state_history[-1] is not state:
                state_history.append(state)

        def result(
            state: AgentLoopState,
            reason: AgentTerminationReason,
            *,
            answer: str | None = None,
        ) -> AgentLoopResult:
            transition(state)
            usage = TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                reasoning_tokens=reasoning_tokens,
                cost_usd=total_cost if total_cost else None,
            )
            return AgentLoopResult(
                state=state,
                state_history=tuple(state_history),
                termination_reason=reason,
                completion_status=(
                    {
                        "ASK": AgentCompletionStatus.ANSWERED,
                        "PLAN": AgentCompletionStatus.PLANNED,
                        "FIX": AgentCompletionStatus.READY_FOR_VERIFICATION,
                    }[task.mode.value]
                    if state is AgentLoopState.COMPLETE
                    else (
                        AgentCompletionStatus.CANCELLED
                        if state is AgentLoopState.CANCELLED
                        else AgentCompletionStatus.FAILED
                    )
                ),
                messages=tuple(messages),
                answer=answer,
                round_count=round_count,
                tool_call_count=tool_call_count,
                usage=usage,
            )

        transition(AgentLoopState.RECEIVE)
        transition(AgentLoopState.UNDERSTAND)
        transition(AgentLoopState.GATHER_CONTEXT)
        if round_count >= self._config.max_rounds:
            return result(
                AgentLoopState.FAILED,
                AgentTerminationReason.MAX_ROUNDS_EXCEEDED,
            )
        if prompt_tokens + completion_tokens >= self._config.max_total_tokens:
            return result(
                AgentLoopState.FAILED,
                AgentTerminationReason.TOKEN_BUDGET_EXCEEDED,
            )
        if (
            self._config.max_cost_usd is not None
            and total_cost >= self._config.max_cost_usd
        ):
            return result(
                AgentLoopState.FAILED,
                AgentTerminationReason.COST_BUDGET_EXCEEDED,
            )
        if self._context_gatherer is not None:
            if cancel_event is not None and cancel_event.is_set():
                return result(
                    AgentLoopState.CANCELLED,
                    AgentTerminationReason.USER_CANCELLED,
                )
            try:
                gathered = await self._gather_context_with_limits(
                    task,
                    cancel_event=cancel_event,
                    timeout_seconds=self._config.max_wall_seconds,
                )
            except _RunCancelled:
                return result(
                    AgentLoopState.CANCELLED,
                    AgentTerminationReason.USER_CANCELLED,
                )
            except TimeoutError:
                return result(
                    AgentLoopState.FAILED,
                    AgentTerminationReason.WALL_TIME_EXCEEDED,
                )
            except Exception:
                return result(
                    AgentLoopState.FAILED,
                    AgentTerminationReason.CONTEXT_FAILED,
                )
            messages.extend(gathered)
        transition(AgentLoopState.PLAN)
        while True:
            transition(AgentLoopState.PREPARE)
            if cancel_event is not None and cancel_event.is_set():
                return result(
                    AgentLoopState.CANCELLED,
                    AgentTerminationReason.USER_CANCELLED,
                )
            elapsed = self._monotonic() - started_at
            if elapsed >= self._config.max_wall_seconds:
                return result(
                    AgentLoopState.FAILED,
                    AgentTerminationReason.WALL_TIME_EXCEEDED,
                )
            if round_count >= self._config.max_rounds:
                return result(
                    AgentLoopState.FAILED,
                    AgentTerminationReason.MAX_ROUNDS_EXCEEDED,
                )
            if prompt_tokens + completion_tokens >= self._config.max_total_tokens:
                return result(
                    AgentLoopState.FAILED,
                    AgentTerminationReason.TOKEN_BUDGET_EXCEEDED,
                )
            if (
                self._config.max_cost_usd is not None
                and total_cost >= self._config.max_cost_usd
            ):
                return result(
                    AgentLoopState.FAILED,
                    AgentTerminationReason.COST_BUDGET_EXCEEDED,
                )

            request = ModelRequest(
                model=task.model,
                messages=tuple(messages),
                tools=tuple(tool.definition for tool in self._tools.values()),
                stream=self._config.stream,
                thinking=self._config.thinking,
                reasoning_effort=self._config.reasoning_effort,
                max_tokens=self._config.max_completion_tokens,
            )
            try:
                transition(AgentLoopState.MODEL_CALL)
                response = await self._complete_with_limits(
                    request,
                    cancel_event=cancel_event,
                    timeout_seconds=self._config.max_wall_seconds - elapsed,
                )
            except _RunCancelled:
                return result(
                    AgentLoopState.CANCELLED,
                    AgentTerminationReason.USER_CANCELLED,
                )
            except TimeoutError:
                return result(
                    AgentLoopState.FAILED,
                    AgentTerminationReason.WALL_TIME_EXCEEDED,
                )
            except ModelProviderError:
                return result(
                    AgentLoopState.FAILED,
                    AgentTerminationReason.PROVIDER_FAILED,
                )

            transition(AgentLoopState.PARSE_TOOL_CALLS)
            round_count += 1
            prompt_tokens += response.usage.prompt_tokens
            completion_tokens += response.usage.completion_tokens
            reasoning_tokens += response.usage.reasoning_tokens
            if response.usage.cost_usd is not None:
                total_cost += response.usage.cost_usd
            if prompt_tokens + completion_tokens > self._config.max_total_tokens:
                return result(
                    AgentLoopState.FAILED,
                    AgentTerminationReason.TOKEN_BUDGET_EXCEEDED,
                )
            if self._config.max_cost_usd is not None and (
                response.usage.cost_usd is None
                or total_cost > self._config.max_cost_usd
            ):
                return result(
                    AgentLoopState.FAILED,
                    AgentTerminationReason.COST_BUDGET_EXCEEDED,
                )

            messages.append(response.message)
            await self._save_progress(
                messages,
                round_count=round_count,
                tool_call_count=tool_call_count,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                reasoning_tokens=reasoning_tokens,
                total_cost=total_cost,
            )
            if response.finish_reason is ModelFinishReason.STOP:
                transition(AgentLoopState.EVALUATE)
                return result(
                    AgentLoopState.COMPLETE,
                    AgentTerminationReason.FINAL_ANSWER,
                    answer=response.message.content,
                )
            if response.finish_reason is not ModelFinishReason.TOOL_CALLS:
                return result(
                    AgentLoopState.FAILED,
                    AgentTerminationReason.INVALID_MODEL_RESPONSE,
                )

            round_made_progress = False
            for call in response.message.tool_calls:
                transition(AgentLoopState.AUTHORIZE)
                fingerprint = self._tool_fingerprint(call)
                if fingerprint == last_action_fingerprint:
                    consecutive_repeats += 1
                else:
                    last_action_fingerprint = fingerprint
                    consecutive_repeats = 1
                if consecutive_repeats >= self._config.max_consecutive_repeats:
                    return result(
                        AgentLoopState.FAILED,
                        AgentTerminationReason.REPEATED_ACTION,
                    )
                if tool_call_count >= self._config.max_tool_calls:
                    return result(
                        AgentLoopState.FAILED,
                        AgentTerminationReason.MAX_TOOL_CALLS_EXCEEDED,
                    )
                tool = self._tools.get(call.tool_name)
                if tool is None:
                    return result(
                        AgentLoopState.FAILED,
                        AgentTerminationReason.UNKNOWN_TOOL,
                    )
                try:
                    transition(AgentLoopState.EXECUTE_TOOLS)
                    remaining_seconds = self._config.max_wall_seconds - (
                        self._monotonic() - started_at
                    )
                    observation = await self._execute_with_limits(
                        tool,
                        call,
                        cancel_event=cancel_event,
                        timeout_seconds=remaining_seconds,
                    )
                except _RunCancelled:
                    return result(
                        AgentLoopState.CANCELLED,
                        AgentTerminationReason.USER_CANCELLED,
                    )
                except TimeoutError:
                    return result(
                        AgentLoopState.FAILED,
                        AgentTerminationReason.WALL_TIME_EXCEEDED,
                    )
                except AgentToolValidationError:
                    return result(
                        AgentLoopState.FAILED,
                        AgentTerminationReason.TOOL_VALIDATION_FAILED,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return result(
                        AgentLoopState.FAILED,
                        AgentTerminationReason.TOOL_EXECUTION_FAILED,
                    )
                tool_call_count += 1
                safe_observation = observation or "（工具未返回文本）"
                observation_fingerprint = hashlib.sha256(
                    safe_observation.encode("utf-8")
                ).hexdigest()
                if observation_fingerprint not in observation_fingerprints:
                    observation_fingerprints.add(observation_fingerprint)
                    round_made_progress = True
                messages.append(
                    ToolMessage(
                        tool_call_id=call.tool_call_id,
                        content=safe_observation,
                        created_at=self._clock(),
                    )
                )
                if self._context_gatherer is not None and _may_change_workspace(
                    call.tool_name
                ):
                    try:
                        refreshed = await self._gather_context_with_limits(
                            task,
                            cancel_event=cancel_event,
                            timeout_seconds=max(
                                0.001,
                                self._config.max_wall_seconds
                                - (self._monotonic() - started_at),
                            ),
                        )
                    except _RunCancelled:
                        return result(
                            AgentLoopState.CANCELLED,
                            AgentTerminationReason.USER_CANCELLED,
                        )
                    except TimeoutError:
                        return result(
                            AgentLoopState.FAILED,
                            AgentTerminationReason.WALL_TIME_EXCEEDED,
                        )
                    except Exception:
                        return result(
                            AgentLoopState.FAILED,
                            AgentTerminationReason.CONTEXT_FAILED,
                        )
                    messages.extend(refreshed)
                await self._save_progress(
                    messages,
                    round_count=round_count,
                    tool_call_count=tool_call_count,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    reasoning_tokens=reasoning_tokens,
                    total_cost=total_cost,
                )
                transition(AgentLoopState.OBSERVE)
            transition(AgentLoopState.EVALUATE)
            if round_made_progress:
                no_progress_rounds = 0
            else:
                no_progress_rounds += 1
                if no_progress_rounds >= self._config.max_no_progress_rounds:
                    return result(
                        AgentLoopState.FAILED,
                        AgentTerminationReason.NO_PROGRESS,
                    )

    async def _save_progress(
        self,
        messages: list[Message],
        *,
        round_count: int,
        tool_call_count: int,
        prompt_tokens: int,
        completion_tokens: int,
        reasoning_tokens: int,
        total_cost: Decimal,
    ) -> None:
        """在模型响应和观察边界等待上层完成原子 checkpoint。"""
        if self._progress_callback is None:
            return
        await self._progress_callback(
            AgentProgress(
                messages=tuple(messages),
                round_count=round_count,
                tool_call_count=tool_call_count,
                usage=TokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                    reasoning_tokens=reasoning_tokens,
                    cost_usd=total_cost if total_cost else None,
                ),
            )
        )

    async def _complete_with_limits(
        self,
        request: ModelRequest,
        *,
        cancel_event: asyncio.Event | None,
        timeout_seconds: float,
    ) -> ModelResponse:
        """让墙钟超时和用户取消都能取消正在进行的 HTTP 调用。"""
        provider_task = asyncio.create_task(self._provider.complete(request))
        cancel_task = (
            asyncio.create_task(cancel_event.wait())
            if cancel_event is not None
            else None
        )
        waiters: set[asyncio.Task[object]] = {provider_task}
        if cancel_task is not None:
            waiters.add(cancel_task)
        try:
            done, _ = await asyncio.wait(
                waiters,
                timeout=max(0.0, timeout_seconds),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if provider_task in done:
                return await provider_task
            provider_task.cancel()
            await asyncio.gather(provider_task, return_exceptions=True)
            if cancel_task is not None and cancel_task in done:
                raise _RunCancelled
            raise TimeoutError
        finally:
            if not provider_task.done():
                provider_task.cancel()
                await asyncio.gather(provider_task, return_exceptions=True)
            if cancel_task is not None:
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)

    async def _gather_context_with_limits(
        self,
        task: AgentTask,
        *,
        cancel_event: asyncio.Event | None,
        timeout_seconds: float,
    ) -> tuple[Message, ...]:
        """让上下文获取与模型和工具共享取消、墙钟失败关闭语义。"""
        if self._context_gatherer is None:
            return ()

        async def invoke_gatherer() -> tuple[Message, ...]:
            """把通用 Awaitable 收窄为 asyncio 可调度协程。"""
            if self._context_gatherer is None:
                return ()
            return await self._context_gatherer(task)

        gather_task = asyncio.create_task(invoke_gatherer())
        cancel_task = (
            asyncio.create_task(cancel_event.wait())
            if cancel_event is not None
            else None
        )
        waiters: set[asyncio.Task[object]] = {gather_task}
        if cancel_task is not None:
            waiters.add(cancel_task)
        try:
            done, _ = await asyncio.wait(
                waiters,
                timeout=max(0.0, timeout_seconds),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if gather_task in done:
                return await gather_task
            if cancel_task is not None and cancel_task in done:
                raise _RunCancelled
            raise TimeoutError
        finally:
            if not gather_task.done():
                gather_task.cancel()
                await asyncio.gather(gather_task, return_exceptions=True)
            if cancel_task is not None:
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)

    async def _execute_with_limits(
        self,
        tool: AgentTool,
        call: ToolCall,
        *,
        cancel_event: asyncio.Event | None,
        timeout_seconds: float,
    ) -> str:
        """让用户取消和总墙钟预算也覆盖工具执行阶段。"""
        tool_task = asyncio.create_task(tool.execute(call))
        cancel_task = (
            asyncio.create_task(cancel_event.wait())
            if cancel_event is not None
            else None
        )
        waiters: set[asyncio.Task[object]] = {tool_task}
        if cancel_task is not None:
            waiters.add(cancel_task)
        try:
            done, _ = await asyncio.wait(
                waiters,
                timeout=max(0.0, timeout_seconds),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if tool_task in done:
                return await tool_task
            if cancel_task is not None and cancel_task in done:
                raise _RunCancelled
            raise TimeoutError
        finally:
            if not tool_task.done():
                tool_task.cancel()
                await asyncio.gather(tool_task, return_exceptions=True)
            if cancel_task is not None:
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)

    @staticmethod
    def _tool_fingerprint(call: ToolCall) -> str:
        """忽略厂商 call id，对工具名和规范化参数生成动作指纹。"""
        payload = json.dumps(
            {"arguments": call.arguments, "tool_name": call.tool_name},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
