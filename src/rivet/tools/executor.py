"""执行静态 ToolCatalog，并把 Demand、授权与副作用事实串成单链。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ValidationError

from rivet.contracts.guard import (
    AuthorizationDecision,
    AuthorizationStatus,
    PermissionRequest,
    TaintSource,
)
from rivet.contracts.tools import SideEffectClass, ToolCall
from rivet.kernel.agent_tools import AgentTool, AgentToolValidationError
from rivet.kernel.application import RivetKernel
from rivet.kernel.capability_demand import DemandHandle
from rivet.kernel.module_runtime import (
    CapabilityLease,
    release_capability_leases,
)
from rivet.tools.catalog import TOOL_CATALOG, ToolSpec, tool_spec
from rivet.trace.builder import TraceEventBuilder
from rivet.trace.models import PersistedTraceEvent
from rivet.trace.store import TraceStore

ToolHandler = Callable[[BaseModel, Mapping[str, object]], Awaitable[str]]
MAX_MODEL_OBSERVATION_CHARS = 65_536


class ToolAuthorizer(Protocol):
    async def __call__(self, request: PermissionRequest) -> AuthorizationDecision: ...


class ToolExecutionError(RuntimeError):
    """工具在模型可见边界失败，消息中不得包含原始秘密。"""


class ToolPermissionRequired(ToolExecutionError):
    """交互层必须取得用户决定后重试该调用。"""

    def __init__(self, request: PermissionRequest) -> None:
        super().__init__("工具需要用户显式授权")
        self.request = request


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """绑定一次 Agent 任务的身份、根 Demand 与权限来源。"""

    parent_demand: DemandHandle
    run_id: str
    session_id: str
    transaction_id: str | None = None
    taint_sources: tuple[TaintSource, ...] = (TaintSource.USER_INSTRUCTION,)

    def __post_init__(self) -> None:
        demand_context = self.parent_demand.context
        if (
            demand_context.run_id != self.run_id
            or demand_context.session_id != self.session_id
            or demand_context.transaction_id != self.transaction_id
        ):
            raise ValueError("工具上下文必须与根 Demand 完全一致")


@dataclass(frozen=True, slots=True)
class SideEffectFact:
    """只保存副作用调用的不可逆事实，不持久化原始参数或输出。"""

    operation_id: str
    originating_run_id: str
    operation: str
    arguments_sha256: str
    status: str
    result_sha256: str | None = None
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class UnknownSideEffect:
    """由 STARTED 且无终态推导出的跨进程恢复事实。"""

    operation_id: str
    operation: str
    arguments_sha256: str
    originating_run_id: str


class SideEffectJournal:
    """将 STARTED/SUCCEEDED/FAILED 写入 Trace；UNKNOWN 仅由缺失终态推导。"""

    def __init__(
        self,
        trace: TraceStore,
        *,
        builder: TraceEventBuilder | None = None,
    ) -> None:
        self._trace = trace
        self._builder = builder or TraceEventBuilder()

    async def started(
        self,
        *,
        call: ToolCall,
        arguments_sha256: str,
        context: ToolExecutionContext,
        parent_event_id: str,
    ) -> None:
        await self._write(
            SideEffectFact(
                operation_id=call.tool_call_id,
                originating_run_id=context.run_id,
                operation=call.tool_name,
                arguments_sha256=arguments_sha256,
                status="STARTED",
            ),
            context=context,
            parent_event_id=parent_event_id,
        )

    async def succeeded(
        self,
        *,
        call: ToolCall,
        arguments_sha256: str,
        result: str,
        context: ToolExecutionContext,
        parent_event_id: str,
    ) -> None:
        await self._write(
            SideEffectFact(
                operation_id=call.tool_call_id,
                originating_run_id=context.run_id,
                operation=call.tool_name,
                arguments_sha256=arguments_sha256,
                status="SUCCEEDED",
                result_sha256=_sha256(result.encode("utf-8")),
            ),
            context=context,
            parent_event_id=parent_event_id,
        )

    async def failed(
        self,
        *,
        call: ToolCall,
        arguments_sha256: str,
        error: BaseException,
        context: ToolExecutionContext,
        parent_event_id: str,
    ) -> None:
        await self._write(
            SideEffectFact(
                operation_id=call.tool_call_id,
                originating_run_id=context.run_id,
                operation=call.tool_name,
                arguments_sha256=arguments_sha256,
                status="FAILED",
                error_type=type(error).__name__,
            ),
            context=context,
            parent_event_id=parent_event_id,
        )

    async def operation_started(
        self,
        *,
        operation_id: str,
        operation: str,
        arguments_sha256: str,
        context: ToolExecutionContext,
        parent_event_id: str,
    ) -> None:
        """记录写、进程或 Apply 已越过副作用开始边界。"""
        await self._write(
            SideEffectFact(
                operation_id=operation_id,
                originating_run_id=context.run_id,
                operation=operation,
                arguments_sha256=arguments_sha256,
                status="STARTED",
            ),
            context=context,
            parent_event_id=parent_event_id,
        )

    async def operation_succeeded(
        self,
        *,
        operation_id: str,
        operation: str,
        arguments_sha256: str,
        result: str,
        context: ToolExecutionContext,
        parent_event_id: str,
        originating_run_id: str | None = None,
    ) -> None:
        """记录副作用已完成及脱敏结果摘要哈希。"""
        await self._write(
            SideEffectFact(
                operation_id=operation_id,
                originating_run_id=originating_run_id or context.run_id,
                operation=operation,
                arguments_sha256=arguments_sha256,
                status="SUCCEEDED",
                result_sha256=_sha256(result.encode("utf-8")),
            ),
            context=context,
            parent_event_id=parent_event_id,
        )

    async def operation_failed(
        self,
        *,
        operation_id: str,
        operation: str,
        arguments_sha256: str,
        error: BaseException,
        context: ToolExecutionContext,
        parent_event_id: str,
        originating_run_id: str | None = None,
    ) -> None:
        """记录副作用失败类型，不持久化异常文本。"""
        await self._write(
            SideEffectFact(
                operation_id=operation_id,
                originating_run_id=originating_run_id or context.run_id,
                operation=operation,
                arguments_sha256=arguments_sha256,
                status="FAILED",
                error_type=type(error).__name__,
            ),
            context=context,
            parent_event_id=parent_event_id,
        )

    def unknown_operations(self, *, run_id: str) -> tuple[str, ...]:
        """STARTED 且没有 SUCCEEDED/FAILED 的操作在恢复时视为 UNKNOWN。"""
        return tuple(
            fact.operation_id
            for fact in self._unknown_facts(
                record
                for record in self._trace.events(run_id)
                if record.event.event_type == "side_effect.checkpoint"
            )
        )

    def unknown_for_transaction(
        self,
        *,
        transaction_id: str,
    ) -> tuple[UnknownSideEffect, ...]:
        """跨全部历史 run 推导同一事务尚无终态的副作用。"""
        return self._unknown_facts(
            record
            for record in self._trace.events()
            if record.event.event_type == "side_effect.checkpoint"
            and record.event.transaction_id == transaction_id
        )

    @staticmethod
    def _unknown_facts(
        records: Iterable[PersistedTraceEvent],
    ) -> tuple[UnknownSideEffect, ...]:
        states: dict[tuple[str, str], str] = {}
        started: dict[tuple[str, str], UnknownSideEffect] = {}
        for record in records:
            event = record.event
            operation_id = event.payload.get("operation_id")
            originating_run_id = event.payload.get("originating_run_id")
            operation = event.payload.get("operation")
            arguments_sha256 = event.payload.get("arguments_sha256")
            status = event.payload.get("status")
            if (
                isinstance(operation_id, str)
                and (originating_run_id is None or isinstance(originating_run_id, str))
                and isinstance(operation, str)
                and isinstance(arguments_sha256, str)
                and isinstance(status, str)
            ):
                origin = originating_run_id or event.run_id
                key = (origin, operation_id)
                states[key] = status
                if status == "STARTED":
                    started[key] = UnknownSideEffect(
                        operation_id=operation_id,
                        operation=operation,
                        arguments_sha256=arguments_sha256,
                        originating_run_id=origin,
                    )
        return tuple(
            started[key] for key, status in states.items() if status == "STARTED"
        )

    async def _write(
        self,
        fact: SideEffectFact,
        *,
        context: ToolExecutionContext,
        parent_event_id: str,
    ) -> None:
        await self._trace.emit(
            self._builder.build(
                event_type="side_effect.checkpoint",
                run_id=context.run_id,
                session_id=context.session_id,
                transaction_id=context.transaction_id,
                parent_event_id=parent_event_id,
                result_summary=f"{fact.operation} {fact.status}",
                payload={
                    "arguments_sha256": fact.arguments_sha256,
                    "error_type": fact.error_type,
                    "operation": fact.operation,
                    "operation_id": fact.operation_id,
                    "originating_run_id": fact.originating_run_id,
                    "result_sha256": fact.result_sha256,
                    "status": fact.status,
                },
            )
        )


class CatalogToolExecutor:
    """严格执行 Validate → Demand → Authorize → Acquire → Handler → Release。"""

    def __init__(
        self,
        kernel: RivetKernel,
        *,
        mode: str,
        context: ToolExecutionContext,
        authorizer: ToolAuthorizer,
        handlers: Mapping[str, ToolHandler],
        side_effect_journal: SideEffectJournal | None = None,
    ) -> None:
        if mode not in {"ask", "fix"}:
            raise ValueError("工具目录只支持 ask 或 fix")
        self._kernel = kernel
        self._mode = mode
        self._context = context
        self._authorizer = authorizer
        self._journal = side_effect_journal
        self._handlers = _validated_executor_bindings(TOOL_CATALOG, handlers)
        self._specs = {spec.name: spec for spec in TOOL_CATALOG if mode in spec.modes}

    @property
    def definitions(self):
        return tuple(spec.definition for spec in self._specs.values())

    def agent_tools(self) -> tuple[AgentTool, ...]:
        tools: list[AgentTool] = []
        for spec in self._specs.values():

            async def execute(
                call: ToolCall,
                arguments: BaseModel,
                *,
                expected_name: str = spec.name,
            ) -> str:
                if call.tool_name != expected_name:
                    raise ToolExecutionError("工具名与执行器不匹配")
                return await self.execute(call, prevalidated=arguments)

            tools.append(
                AgentTool.from_call_model(
                    definition=spec.definition,
                    input_model=spec.input_model,
                    executor=execute,
                )
            )
        return tuple(tools)

    async def execute(
        self,
        call: ToolCall,
        *,
        prevalidated: BaseModel | None = None,
    ) -> str:
        spec = self._specs.get(call.tool_name)
        if spec is None:
            try:
                tool_spec(call.tool_name)
            except KeyError:
                raise ToolExecutionError("未知工具") from None
            raise ToolExecutionError(f"工具不允许用于 {self._mode.upper()} 模式")
        arguments = prevalidated or self._validate(spec, call)
        tool_demand = await self._kernel.begin_model_tool_demand(
            call.tool_name,
            parent=self._context.parent_demand,
            reason=f"model requested {call.tool_name}",
            operation_id=call.tool_call_id,
        )
        request = self._permission_request(spec, arguments)
        decision = await self._authorizer(request)
        if decision.status is AuthorizationStatus.PROMPT:
            raise ToolPermissionRequired(request)
        if decision.status is not AuthorizationStatus.ALLOWED:
            raise ToolExecutionError(f"工具授权被拒绝：{decision.code}")

        leases: list[CapabilityLease[object]] = []
        capabilities: dict[str, object] = {}
        try:
            for capability_id in spec.required_capabilities:
                lease = await self._kernel.acquire_required(
                    capability_id,
                    parent=tool_demand,
                    reason=f"{call.tool_name} requires {capability_id}",
                    operation_id=call.tool_call_id,
                )
                leases.append(lease)
                capabilities[capability_id] = lease.capability
            return await self._execute_handler(
                spec,
                call,
                arguments,
                capabilities,
                parent_event_id=tool_demand.event_id,
            )
        finally:
            try:
                await release_capability_leases(leases)
            except BaseException as release_error:
                raise ToolExecutionError("工具能力 Lease 释放失败") from release_error

    async def _execute_handler(
        self,
        spec: ToolSpec,
        call: ToolCall,
        arguments: BaseModel,
        capabilities: Mapping[str, object],
        *,
        parent_event_id: str,
    ) -> str:
        serialized = json.dumps(
            arguments.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        arguments_sha256 = _sha256(serialized)
        checkpointed = spec.side_effect is not SideEffectClass.READ_ONLY
        if checkpointed and self._journal is None:
            raise ToolExecutionError("副作用工具缺少耐久 Journal")
        if checkpointed:
            assert self._journal is not None
            await self._journal.started(
                call=call,
                arguments_sha256=arguments_sha256,
                context=self._context,
                parent_event_id=parent_event_id,
            )
        try:
            result = _bounded_observation(
                await self._handlers[spec.executor](arguments, capabilities)
            )
        except BaseException as error:
            if checkpointed:
                assert self._journal is not None
                await self._journal.failed(
                    call=call,
                    arguments_sha256=arguments_sha256,
                    error=error,
                    context=self._context,
                    parent_event_id=parent_event_id,
                )
            raise
        if checkpointed:
            assert self._journal is not None
            await self._journal.succeeded(
                call=call,
                arguments_sha256=arguments_sha256,
                result=result,
                context=self._context,
                parent_event_id=parent_event_id,
            )
        return result

    @staticmethod
    def _validate(spec: ToolSpec, call: ToolCall) -> BaseModel:
        try:
            return spec.input_model.model_validate(call.arguments)
        except ValidationError as error:
            raise AgentToolValidationError("工具参数未通过本地 Schema") from error

    def _permission_request(
        self,
        spec: ToolSpec,
        arguments: BaseModel,
    ) -> PermissionRequest:
        paths: tuple[str, ...] = ()
        if spec.path_argument is not None:
            value = getattr(arguments, spec.path_argument)
            if value is not None:
                paths = (str(value),)
        return PermissionRequest(
            permission=spec.permission,
            scope=spec.permission_scope,
            reason=f"model tool call: {spec.name}",
            run_id=self._context.run_id,
            transaction_id=self._context.transaction_id,
            paths=paths,
            taint_sources=self._context.taint_sources,
        )


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _validated_executor_bindings(
    catalog: tuple[ToolSpec, ...],
    handlers: Mapping[str, ToolHandler],
) -> dict[str, ToolHandler]:
    """在启动前证明静态目录与 executor registry 一一对应。"""
    duplicate_names = _duplicates(spec.name for spec in catalog)
    if duplicate_names:
        raise ValueError(f"重复工具名：{list(duplicate_names)}")
    duplicate_executors = _duplicates(spec.executor for spec in catalog)
    if duplicate_executors:
        raise ValueError(f"重复工具 executor key：{list(duplicate_executors)}")

    registered = dict(handlers)
    expected = {spec.executor for spec in catalog}
    actual = set(registered)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        raise ValueError(
            f"工具 executor 绑定错配：missing={missing}, unknown_or_extra={unknown}"
        )
    invalid = sorted(
        key for key, handler in registered.items() if not callable(handler)
    )
    if invalid:
        raise ValueError(f"工具 executor handler 不可调用：{invalid}")
    return registered


def _duplicates(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return tuple(sorted(duplicates))


def _bounded_observation(value: str) -> str:
    if len(value) <= MAX_MODEL_OBSERVATION_CHARS:
        return value
    return value[:MAX_MODEL_OBSERVATION_CHARS] + "\n[TRUNCATED]\n"
