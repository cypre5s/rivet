"""把模型工具定义、本地 Pydantic 校验和执行函数绑定。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel, JsonValue, ValidationError

from rivet.contracts.tools import ToolCall, ToolDefinition

ToolExecutor = Callable[[BaseModel], Awaitable[str]]


class AgentToolValidationError(ValueError):
    """表示模型参数没有通过工具自己的本地 Schema。"""


@dataclass(frozen=True, slots=True)
class AgentTool:
    """保存向模型公开的 Schema 和仅在本地运行的执行器。"""

    definition: ToolDefinition
    input_model: type[BaseModel]
    executor: ToolExecutor

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

    async def execute(self, call: ToolCall) -> str:
        """先执行本地 Schema 校验，再调用工具实现。"""
        try:
            arguments = self.input_model.model_validate(call.arguments)
        except ValidationError as error:
            raise AgentToolValidationError("工具参数未通过本地 Schema") from error
        return await self.executor(arguments)
