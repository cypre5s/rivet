"""把模型工具定义、本地 Pydantic 校验和执行函数绑定。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel, JsonValue, ValidationError

from rivet.contracts.tools import ToolCall, ToolDefinition

ToolExecutor = Callable[[BaseModel], Awaitable[str]]
ToolCallExecutor = Callable[[ToolCall, BaseModel], Awaitable[str]]


class AgentToolValidationError(ValueError):
    """表示模型参数没有通过工具自己的本地 Schema。"""


class AgentToolRejectedError(RuntimeError):
    """表示单次工具调用被本地边界拒绝，但 Agent 可以修正后继续。"""

    def __init__(self, code: str, summary: str, *, retryable: bool = True) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class AgentTool:
    """保存向模型公开的 Schema 和仅在本地运行的执行器。"""

    definition: ToolDefinition
    input_model: type[BaseModel]
    executor: ToolExecutor | None = None
    call_executor: ToolCallExecutor | None = None

    def __post_init__(self) -> None:
        """要求普通执行器与需要 call 身份的执行器二选一。"""
        if (self.executor is None) == (self.call_executor is None):
            raise ValueError("AgentTool 必须且只能配置一个执行器")

    @classmethod
    def from_model(
        cls,
        *,
        name: str,
        description: str,
        input_model: type[BaseModel],
        executor: ToolExecutor,
    ) -> AgentTool:
        """从严格 Pydantic 模型生成 JSON Schema。"""
        if input_model.model_config.get("extra") != "forbid":
            raise ValueError("AgentTool 输入模型必须配置 extra='forbid'")
        schema = cast(dict[str, JsonValue], input_model.model_json_schema())
        return cls(
            definition=ToolDefinition(
                name=name,
                description=description,
                input_schema=schema,
            ),
            input_model=input_model,
            executor=executor,
        )

    @classmethod
    def from_call_model(
        cls,
        *,
        definition: ToolDefinition,
        input_model: type[BaseModel],
        executor: ToolCallExecutor,
    ) -> AgentTool:
        """绑定需要 Tool Call 身份和审计上下文的执行器。"""
        if input_model.model_config.get("extra") != "forbid":
            raise ValueError("AgentTool 输入模型必须配置 extra='forbid'")
        return cls(
            definition=definition,
            input_model=input_model,
            call_executor=executor,
        )

    async def execute(self, call: ToolCall) -> str:
        """先执行本地 Schema 校验，再调用工具实现。"""
        try:
            arguments = self.input_model.model_validate(call.arguments)
        except ValidationError as error:
            raise AgentToolValidationError("工具参数未通过本地 Schema") from error
        if self.call_executor is not None:
            return await self.call_executor(call, arguments)
        if self.executor is None:
            raise RuntimeError("AgentTool 执行器不变量被破坏")
        return await self.executor(arguments)
