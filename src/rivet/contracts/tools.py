"""定义静态工具 Schema 与模型工具调用契约。"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Self

from pydantic import JsonValue, model_validator

from rivet.contracts.common import (
    ContractModel,
    NonEmptyText,
    ToolCallId,
    ToolName,
)

MAX_TOOL_ARGUMENT_BYTES = 65_536


class SideEffectClass(StrEnum):
    """区分只读、事务写入与本地进程执行边界。"""

    READ_ONLY = "READ_ONLY"
    TRANSACTIONAL_WRITE = "TRANSACTIONAL_WRITE"
    LOCAL_PROCESS = "LOCAL_PROCESS"


class ToolDefinition(ContractModel):
    """向模型提供名称、用途和本地仍需验证的 JSON Schema。"""

    name: ToolName
    description: NonEmptyText
    input_schema: dict[str, JsonValue]

    @model_validator(mode="after")
    def _validate_object_schema(self) -> Self:
        """限定工具参数根类型为对象。"""
        if self.input_schema.get("type") != "object":
            raise ValueError("工具输入 Schema 根类型必须是 object")
        return self


class ToolCall(ContractModel):
    """保存已解析但尚未授权执行的结构化工具调用。"""

    tool_call_id: ToolCallId
    tool_name: ToolName
    arguments: dict[str, JsonValue]

    @model_validator(mode="after")
    def _validate_argument_size(self) -> Self:
        """防止超大参数绕过模型上下文预算。"""
        serialized = json.dumps(self.arguments, ensure_ascii=False, sort_keys=True)
        if len(serialized.encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES:
            raise ValueError("工具参数超过契约上限")
        return self
