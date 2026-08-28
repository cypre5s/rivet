"""定义工具 Schema、调用、截断输出和分类错误契约。"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Self

from pydantic import Field, JsonValue, model_validator

from rivet.contracts.common import (
    ArtifactReference,
    ContractModel,
    ErrorDetail,
    NonEmptyText,
    Sha256Digest,
    Timestamp,
    ToolCallId,
    ToolName,
)

MAX_TOOL_ARGUMENT_BYTES = 65_536


class ToolExecutionStatus(StrEnum):
    """记录一次工具调用在副作用边界上的耐久状态。"""

    PREPARED = "PREPARED"
    AUTHORIZED = "AUTHORIZED"
    EXECUTING = "EXECUTING"
    RUNNING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


class SideEffectClass(StrEnum):
    """决定中断工具是否允许重放以及需要何种恢复检查。"""

    READ_ONLY = "READ_ONLY"
    IDEMPOTENT_WRITE = "IDEMPOTENT_WRITE"
    TRANSACTIONAL_WRITE = "TRANSACTIONAL_WRITE"
    LOCAL_PROCESS = "LOCAL_PROCESS"
    EXTERNAL_SIDE_EFFECT = "EXTERNAL_SIDE_EFFECT"


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


class ToolOutput(ContractModel):
    """分别保留 stdout/stderr 预览、截断状态和可选完整产物。"""

    stdout: str = Field(default="", max_length=65_536)
    stderr: str = Field(default="", max_length=65_536)
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_sha256: Sha256Digest | None = None
    stderr_sha256: Sha256Digest | None = None
    artifact: ArtifactReference | None = None


class ToolError(ErrorDetail):
    """标记发生在工具校验、授权或执行边界的错误。"""


class ToolResult(ContractModel):
    """记录工具执行的时间边界、结果与错误互斥关系。"""

    tool_call_id: ToolCallId
    tool_name: ToolName
    success: bool
    output: ToolOutput
    error: ToolError | None = None
    started_at: Timestamp
    completed_at: Timestamp

    @model_validator(mode="after")
    def _validate_result_consistency(self) -> Self:
        """禁止成功结果携带错误或失败结果缺少错误。"""
        if self.completed_at < self.started_at:
            raise ValueError("工具完成时间不得早于开始时间")
        if self.success == (self.error is not None):
            raise ValueError("工具成功状态与错误字段不一致")
        return self
