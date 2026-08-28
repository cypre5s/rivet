"""定义 Agent Loop 的状态、预算、任务和确定性终止结果。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from rivet.contracts.common import RunId, SessionId
from rivet.contracts.messages import Message
from rivet.contracts.provider import ReasoningEffort, ThinkingMode, TokenUsage


class AgentLoopState(StrEnum):
    """列出自主循环可观测的全部阶段。"""

    RECEIVE = "receive"
    UNDERSTAND = "understand"
    GATHER_CONTEXT = "gather_context"
    PLAN = "plan"
    PREPARE = "prepare"
    MODEL_CALL = "model_call"
    PARSE_TOOL_CALLS = "parse_tool_calls"
    AUTHORIZE = "authorize"
    EXECUTE_TOOLS = "execute_tools"
    OBSERVE = "observe"
    EVALUATE = "evaluate"
    VERIFY = "verify"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentTerminationReason(StrEnum):
    """给出不依赖模型自报完成的稳定终止原因。"""

    FINAL_ANSWER = "final_answer"
    USER_CANCELLED = "user_cancelled"
    MAX_ROUNDS_EXCEEDED = "max_rounds_exceeded"
    MAX_TOOL_CALLS_EXCEEDED = "max_tool_calls_exceeded"
    WALL_TIME_EXCEEDED = "wall_time_exceeded"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    COST_BUDGET_EXCEEDED = "cost_budget_exceeded"
    REPEATED_ACTION = "repeated_action"
    NO_PROGRESS = "no_progress"
    UNKNOWN_TOOL = "unknown_tool"
    TOOL_VALIDATION_FAILED = "tool_validation_failed"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    PROVIDER_FAILED = "provider_failed"
    CONTEXT_FAILED = "context_failed"
    INVALID_MODEL_RESPONSE = "invalid_model_response"


@dataclass(frozen=True, slots=True)
class AgentLoopConfig:
    """集中保存每次运行冻结的循环与费用预算。"""

    max_rounds: int = 24
    max_tool_calls: int = 64
    max_wall_seconds: float = 900.0
    max_total_tokens: int = 128_000
    max_cost_usd: Decimal | None = None
    max_consecutive_repeats: int = 3
    max_no_progress_rounds: int = 4
    stream: bool = True
    thinking: ThinkingMode = ThinkingMode.ENABLED
    reasoning_effort: ReasoningEffort = ReasoningEffort.HIGH
    max_completion_tokens: int = 8_192

    def __post_init__(self) -> None:
        """拒绝无法安全执行或永不终止的预算。"""
        positive_values = (
            self.max_rounds,
            self.max_tool_calls,
            self.max_wall_seconds,
            self.max_total_tokens,
            self.max_consecutive_repeats,
            self.max_no_progress_rounds,
            self.max_completion_tokens,
        )
        if any(value <= 0 for value in positive_values):
            raise ValueError("Agent Loop 预算必须大于零")
        if self.max_cost_usd is not None and self.max_cost_usd < 0:
            raise ValueError("Agent Loop 费用预算不得为负数")


@dataclass(frozen=True, slots=True)
class AgentTask:
    """保存一次循环不可变的身份、历史和模型选择。"""

    run_id: RunId
    session_id: SessionId
    messages: tuple[Message, ...]
    model: str
    initial_round_count: int = 0
    initial_tool_call_count: int = 0
    initial_prompt_tokens: int = 0
    initial_completion_tokens: int = 0
    initial_reasoning_tokens: int = 0
    initial_cost_usd: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        """要求任务至少包含一条消息和明确模型。"""
        if not self.messages:
            raise ValueError("AgentTask 至少需要一条消息")
        if not self.model:
            raise ValueError("AgentTask 必须指定模型")
        counters = (
            self.initial_round_count,
            self.initial_tool_call_count,
            self.initial_prompt_tokens,
            self.initial_completion_tokens,
            self.initial_reasoning_tokens,
        )
        if any(value < 0 for value in counters) or self.initial_cost_usd < 0:
            raise ValueError("AgentTask 恢复计数不得为负数")


@dataclass(frozen=True, slots=True)
class AgentLoopResult:
    """返回最终状态、事实计数、历史和可选回答。"""

    state: AgentLoopState
    state_history: tuple[AgentLoopState, ...]
    termination_reason: AgentTerminationReason
    messages: tuple[Message, ...]
    answer: str | None
    round_count: int
    tool_call_count: int
    usage: TokenUsage
