"""注册工具与 capability，并统一校验、Trace 和双视图结果。"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from pydantic import BaseModel, JsonValue, ValidationError

from rivet.contracts.common import RunId, SessionId, TransactionId
from rivet.contracts.tools import (
    ToolCall,
    ToolDefinition,
    ToolError,
    ToolOutput,
    ToolResult,
)
from rivet.kernel.agent_tools import AgentTool
from rivet.tools.errors import WorkspaceToolError
from rivet.trace.builder import TraceEventBuilder
from rivet.trace.models import OutputCapture
from rivet.trace.redaction import SecretRedactor
from rivet.trace.store import TraceStore

Clock = Callable[[], datetime]
ToolHandler = Callable[[BaseModel], Awaitable["RawToolOutput"]]


def _utc_now() -> datetime:
    """返回工具结果所需的 UTC 时间。"""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class RawToolOutput:
    """保存 handler 的原始有界字节与上游截断事实。"""

    stdout: bytes = b""
    stderr: bytes = b""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_total_bytes: int | None = None
    stderr_total_bytes: int | None = None
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """绑定模型定义、唯一 capability、本地输入模型与 handler。"""

    definition: ToolDefinition
    capability_id: str
    input_model: type[BaseModel]
    handler: ToolHandler

    @classmethod
    def from_model(
        cls,
        *,
        name: str,
        capability_id: str,
        description: str,
        input_model: type[BaseModel],
        handler: ToolHandler,
    ) -> RegisteredTool:
        """从拒绝额外字段的 Pydantic 模型生成工具定义。"""
        if input_model.model_config.get("extra") != "forbid":
            raise ValueError("工具输入模型必须配置 extra='forbid'")
        schema = cast(dict[str, JsonValue], input_model.model_json_schema())
        return cls(
            definition=ToolDefinition(
                name=name,
                description=description,
                input_schema=schema,
            ),
            capability_id=capability_id,
            input_model=input_model,
            handler=handler,
        )


@dataclass(frozen=True, slots=True)
class ToolInvocationContext:
    """提供 Trace 关联身份和可选事务身份。"""

    run_id: RunId
    session_id: SessionId
    trace: TraceStore
    transaction_id: TransactionId | None = None


@dataclass(frozen=True, slots=True)
class ToolInvocationView:
    """同时返回结构化结果、模型短视图、TUI 长视图和 artifact。"""

    result: ToolResult
    model_text: str
    tui_text: str
    output_capture: OutputCapture


class ToolRegistry:
    """拒绝重复工具/capability，并对每次调用执行统一审计。"""

    def __init__(
        self,
        *,
        model_preview_chars: int = 8_192,
        tui_preview_chars: int = 65_536,
        clock: Clock = _utc_now,
        event_builder: TraceEventBuilder | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        if model_preview_chars <= 0 or tui_preview_chars < model_preview_chars:
            raise ValueError("工具视图预算必须为正且 TUI 不小于模型视图")
        self._tools: dict[str, RegisteredTool] = {}
        self._capabilities: dict[str, str] = {}
        self._model_preview_chars = model_preview_chars
        self._tui_preview_chars = tui_preview_chars
        self._clock = clock
        self._event_builder = event_builder or TraceEventBuilder(clock=clock)
        self._redactor = redactor or SecretRedactor()

    @property
    def names(self) -> tuple[str, ...]:
        """按注册顺序返回稳定工具名。"""
        return tuple(self._tools)

    @property
    def capabilities(self) -> tuple[str, ...]:
        """按注册顺序返回唯一 capability。"""
        return tuple(self._capabilities)

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        """返回可直接提供给模型的工具定义。"""
        return tuple(tool.definition for tool in self._tools.values())

    def register(self, tool: RegisteredTool) -> None:
        """原子拒绝重复名称或多个工具绑定同一 capability。"""
        name = tool.definition.name
        if name in self._tools:
            raise ValueError(f"工具名重复：{name}")
        if tool.capability_id in self._capabilities:
            raise ValueError(f"capability 重复绑定：{tool.capability_id}")
        self._tools[name] = tool
        self._capabilities[tool.capability_id] = name

    def resolve_capability(self, capability_id: str) -> RegisteredTool:
        """依据唯一 capability 返回已注册工具。"""
        tool_name = self._capabilities.get(capability_id)
        if tool_name is None:
            raise WorkspaceToolError("tool.capability_missing", "capability 未绑定工具")
        return self._tools[tool_name]

    def agent_tools(
        self,
        *,
        context: ToolInvocationContext,
    ) -> tuple[AgentTool, ...]:
        """把审计注册表适配为 Agent Loop 可执行工具。"""
        adapted: list[AgentTool] = []
        for registered in self._tools.values():

            async def execute_call(
                call: ToolCall,
                arguments: BaseModel,
                *,
                registered_tool: RegisteredTool = registered,
            ) -> str:
                """保留 call id，并让 Registry 继续承担二次校验和 Trace。"""
                if call.tool_name != registered_tool.definition.name:
                    raise WorkspaceToolError("tool.name_mismatch", "工具适配名称不一致")
                validated_call = call.model_copy(
                    update={"arguments": arguments.model_dump()}
                )
                view = await self.invoke(validated_call, context=context)
                return view.model_text

            adapted.append(
                AgentTool.from_call_model(
                    definition=registered.definition,
                    input_model=registered.input_model,
                    executor=execute_call,
                )
            )
        return tuple(adapted)

    async def invoke(
        self,
        call: ToolCall,
        *,
        context: ToolInvocationContext,
    ) -> ToolInvocationView:
        """校验并执行工具，任何失败都返回脱敏的 ToolResult。"""
        started_at = self._clock()
        registered = self._tools.get(call.tool_name)
        capability_id = registered.capability_id if registered is not None else None
        started_event = self._event_builder.build(
            event_type="tool.started",
            run_id=context.run_id,
            session_id=context.session_id,
            transaction_id=context.transaction_id,
            input_summary=f"调用 {call.tool_name}",
            payload={
                "tool_name": call.tool_name,
                "capability_id": capability_id,
                "argument_names": cast(JsonValue, sorted(call.arguments)),
            },
        )
        await context.trace.emit(started_event)

        success = False
        tool_error: ToolError | None = None
        try:
            if registered is None:
                raise WorkspaceToolError("tool.unknown", "工具未注册")
            try:
                arguments = registered.input_model.model_validate(call.arguments)
            except ValidationError as error:
                raise WorkspaceToolError(
                    "tool.validation_failed", "工具参数未通过本地 Schema"
                ) from error
            raw_output = await registered.handler(arguments)
            success = True
        except WorkspaceToolError as error:
            raw_output = RawToolOutput(stderr=error.summary.encode("utf-8"))
            tool_error = ToolError(
                code=error.code,
                summary=error.summary,
                next_action="检查参数、工作区边界或本地依赖后重试",
                retryable=error.retryable,
                run_id=context.run_id,
                session_id=context.session_id,
                transaction_id=context.transaction_id,
            )
        except Exception:
            raw_output = RawToolOutput(stderr="工具内部错误".encode())
            tool_error = ToolError(
                code="tool.internal_error",
                summary="工具执行发生已脱敏的内部错误",
                next_action="查看关联 Trace 并检查工具实现",
                retryable=False,
                run_id=context.run_id,
                session_id=context.session_id,
                transaction_id=context.transaction_id,
            )

        completed_at = self._clock()
        stdout_text = self._redactor.redact_text(self._render_bytes(raw_output.stdout))
        stderr_text = self._redactor.redact_text(self._render_bytes(raw_output.stderr))
        model_text = self._combined_view(
            stdout_text, stderr_text, limit=self._model_preview_chars
        )
        tui_text = self._combined_view(
            stdout_text, stderr_text, limit=self._tui_preview_chars
        )
        stdout_total = raw_output.stdout_total_bytes or len(raw_output.stdout)
        stderr_total = raw_output.stderr_total_bytes or len(raw_output.stderr)
        model_stdout = stdout_text[: self._model_preview_chars]
        model_stderr = stderr_text[: self._model_preview_chars]
        stdout_truncated = raw_output.stdout_truncated or len(stdout_text) > len(
            model_stdout
        )
        stderr_truncated = raw_output.stderr_truncated or len(stderr_text) > len(
            model_stderr
        )
        event_type = "tool.completed" if success else "tool.failed"
        duration_ms = max(0.0, (completed_at - started_at).total_seconds() * 1_000)
        completed_event = self._event_builder.build(
            event_type=event_type,
            run_id=context.run_id,
            session_id=context.session_id,
            transaction_id=context.transaction_id,
            parent_event_id=started_event.event_id,
            result_summary="工具执行成功" if success else "工具执行失败",
            payload={
                "tool_name": call.tool_name,
                "capability_id": capability_id,
                "success": success,
                "duration_ms": duration_ms,
                "stdout_bytes": stdout_total,
                "stderr_bytes": stderr_total,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
            },
        )
        output_capture = context.trace.capture_output(
            run_id=context.run_id,
            event_id=completed_event.event_id,
            stdout=stdout_text,
            stderr=stderr_text,
        )
        stdout_sha256 = raw_output.stdout_sha256 or self._digest(raw_output.stdout)
        stderr_sha256 = raw_output.stderr_sha256 or self._digest(raw_output.stderr)
        output = ToolOutput(
            stdout=model_stdout,
            stderr=model_stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            stdout_sha256=stdout_sha256,
            stderr_sha256=stderr_sha256,
            artifact=output_capture.stdout.artifact,
        )
        result = ToolResult(
            tool_call_id=call.tool_call_id,
            tool_name=call.tool_name,
            success=success,
            output=output,
            error=tool_error,
            started_at=started_at,
            completed_at=completed_at,
        )
        await context.trace.emit(completed_event)
        return ToolInvocationView(result, model_text, tui_text, output_capture)

    @staticmethod
    def _render_bytes(content: bytes) -> str:
        """UTF-8 无损展示；二进制改用可逆 base64 文本。"""
        try:
            return content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return "base64:" + base64.b64encode(content).decode("ascii")

    @staticmethod
    def _combined_view(stdout: str, stderr: str, *, limit: int) -> str:
        """为两个流生成非空、有截断标记的单一视图。"""
        sections: list[str] = []
        if stdout:
            sections.append(stdout)
        if stderr:
            sections.append(f"[stderr]\n{stderr}")
        combined = "\n".join(sections) or "（工具未返回文本）"
        if len(combined) > limit:
            return combined[:limit] + "\n[TRUNCATED]\n"
        return combined

    @staticmethod
    def _digest(content: bytes) -> str:
        """生成 ToolOutput 使用的带算法前缀哈希。"""
        return f"sha256:{hashlib.sha256(content).hexdigest()}"
