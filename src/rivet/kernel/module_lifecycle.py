"""提供模块启用、禁用、唤醒与休眠的唯一应用服务。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Literal, Never, Protocol

from pydantic import JsonValue

from rivet.contracts.modules import (
    ActivationPolicy,
    ModuleLifecycleResult,
    ModuleManifest,
    ModuleOperation,
    ModuleOperationSource,
    ModuleOverrideChange,
    ModuleScope,
    ModuleState,
    ModuleStatus,
    SleepPolicy,
)
from rivet.kernel.errors import (
    CapabilityNotFoundError,
    ModuleActivationError,
    ModuleOverridePersistenceError,
    ModuleQuarantinedError,
    SafeModeViolationError,
)
from rivet.kernel.module_runtime import ModuleRuntime


class ModuleOverrideRepository(Protocol):
    """隔离生命周期服务与 SQLite 具体实现。"""

    def set_many(self, changes: tuple[ModuleOverrideChange, ...]) -> None:
        """原子保存一组覆盖。"""
        ...


class ModuleLifecycleEventSink(Protocol):
    """把生命周期事件写入 Trace，并可同时投影到 IPC。"""

    async def emit(
        self,
        event_type: str,
        payload: dict[str, JsonValue],
    ) -> str | None:
        """返回已持久化事件 ID。"""
        ...


class InMemoryModuleOverrideRepository:
    """为纯 Kernel 测试保留无 I/O 的覆盖仓库。"""

    def __init__(self) -> None:
        self.values: dict[tuple[ModuleScope, str], bool] = {}

    def set_many(self, changes: tuple[ModuleOverrideChange, ...]) -> None:
        """以内存原子替换模拟正式存储语义。"""
        updated = dict(self.values)
        for change in changes:
            key = (change.scope, change.module_id)
            if change.enabled is None:
                updated.pop(key, None)
            else:
                updated[key] = change.enabled
        self.values = updated


class ModuleLifecycleError(RuntimeError):
    """携带稳定错误码、阻塞项和恢复动作。"""

    def __init__(
        self,
        *,
        code: str,
        module_id: str,
        current_state: ModuleState,
        requested_operation: ModuleOperation,
        human_message: str,
        blockers: tuple[str, ...] = (),
        retryable: bool = False,
        suggested_action: str,
        trace_event_id: str | None = None,
    ) -> None:
        super().__init__(human_message)
        self.code = code
        self.module_id = module_id
        self.current_state = current_state
        self.requested_operation = requested_operation
        self.human_message = human_message
        self.blockers = blockers
        self.retryable = retryable
        self.suggested_action = suggested_action
        self.trace_event_id = trace_event_id


class ModuleLifecycleService:
    """串行化所有跨模块转换并协调策略、资源、持久化和 Trace。"""

    def __init__(
        self,
        runtime: ModuleRuntime,
        overrides: ModuleOverrideRepository,
        *,
        persisted_overrides: Mapping[str, bool | None] | None = None,
    ) -> None:
        self.runtime = runtime
        self._overrides = overrides
        self._persisted_overrides = dict(persisted_overrides or {})
        self._operation_lock = asyncio.Lock()

    def statuses(self) -> tuple[ModuleStatus, ...]:
        """不激活模块地返回 Manifest、覆盖与实时状态。"""
        return tuple(
            ModuleStatus(
                module_id=snapshot.module_id,
                manifest_default_enabled=snapshot.manifest_default_enabled,
                persisted_override=self._persisted_overrides.get(snapshot.module_id),
                configured_enabled=self.runtime.configured_enabled(snapshot.module_id),
                effective_enabled=snapshot.effective_enabled,
                runtime_state=snapshot.state,
                activation=snapshot.activation,
                scope=ModuleScope(snapshot.scope),
                manual_control=snapshot.manual_control,
                sleep_policy=snapshot.sleep_policy,
                dependencies=snapshot.dependencies,
                dependents=snapshot.dependents,
                provided_capabilities=snapshot.capabilities,
                lease_count=snapshot.lease_count,
                active_resource_count=snapshot.resource_counts.resource_count,
                last_error=snapshot.last_error or snapshot.quarantine_reason,
            )
            for snapshot in self.runtime.snapshots()
        )

    async def enable(
        self,
        module_id: str,
        *,
        with_dependencies: bool = False,
        source: ModuleOperationSource,
        request_id: str,
        event_sink: ModuleLifecycleEventSink | None = None,
    ) -> ModuleLifecycleResult:
        """只持久化启用策略，不创建任何模块实例。"""
        return await self._operate(
            ModuleOperation.ENABLE,
            module_id,
            with_dependencies=with_dependencies,
            source=source,
            request_id=request_id,
            event_sink=event_sink,
        )

    async def disable(
        self,
        module_id: str,
        *,
        cascade: bool = False,
        wait: bool = False,
        timeout_seconds: float = 30,
        confirmed: bool = False,
        source: ModuleOperationSource,
        request_id: str,
        event_sink: ModuleLifecycleEventSink | None = None,
    ) -> ModuleLifecycleResult:
        """安全休眠目标及确认的依赖者后持久化禁用。"""
        return await self._operate(
            ModuleOperation.DISABLE,
            module_id,
            cascade=cascade,
            wait=wait,
            timeout_seconds=timeout_seconds,
            confirmed=confirmed,
            source=source,
            request_id=request_id,
            event_sink=event_sink,
        )

    async def wake(
        self,
        module_id: str,
        *,
        with_dependencies: bool = False,
        source: ModuleOperationSource,
        request_id: str,
        event_sink: ModuleLifecycleEventSink | None = None,
    ) -> ModuleLifecycleResult:
        """经 ModuleRuntime 正式激活路径唤醒模块及依赖。"""
        return await self._operate(
            ModuleOperation.WAKE,
            module_id,
            with_dependencies=with_dependencies,
            source=source,
            request_id=request_id,
            event_sink=event_sink,
        )

    async def sleep(
        self,
        module_id: str,
        *,
        cascade: bool = False,
        wait: bool = False,
        timeout_seconds: float = 30,
        confirmed: bool = False,
        source: ModuleOperationSource,
        request_id: str,
        event_sink: ModuleLifecycleEventSink | None = None,
    ) -> ModuleLifecycleResult:
        """保留启用策略并释放实例、能力和全部模块资源。"""
        return await self._operate(
            ModuleOperation.SLEEP,
            module_id,
            cascade=cascade,
            wait=wait,
            timeout_seconds=timeout_seconds,
            confirmed=confirmed,
            source=source,
            request_id=request_id,
            event_sink=event_sink,
        )

    async def _operate(
        self,
        operation: ModuleOperation,
        module_id: str,
        *,
        with_dependencies: bool = False,
        cascade: bool = False,
        wait: bool = False,
        timeout_seconds: float = 30,
        confirmed: bool = False,
        source: ModuleOperationSource,
        request_id: str,
        event_sink: ModuleLifecycleEventSink | None,
    ) -> ModuleLifecycleResult:
        """在唯一串行边界内执行操作并保证成功或失败都有 Trace。"""
        started = time.monotonic()
        previous_state = self._safe_state(module_id)
        previous_enabled = self._safe_enabled(module_id)
        base_payload = self._event_payload(
            request_id=request_id,
            module_id=module_id,
            operation=operation,
            source=source,
            previous_state=previous_state,
            current_state=previous_state,
            previous_enabled=previous_enabled,
            effective_enabled=previous_enabled,
        )
        await self._emit(event_sink, "module.operation.requested", base_payload)
        async with self._operation_lock:
            await self._emit(event_sink, "module.operation.started", base_payload)
            try:
                self._validate_timeout(module_id, operation, timeout_seconds)
                if operation is ModuleOperation.ENABLE:
                    result = self._enable(module_id, with_dependencies, source)
                elif operation is ModuleOperation.DISABLE:
                    result = await self._disable(
                        module_id,
                        cascade=cascade,
                        wait=wait,
                        timeout_seconds=timeout_seconds,
                        confirmed=confirmed,
                        source=source,
                    )
                elif operation is ModuleOperation.WAKE:
                    result = await self._wake(module_id, with_dependencies, source)
                else:
                    result = await self._sleep(
                        module_id,
                        cascade=cascade,
                        wait=wait,
                        timeout_seconds=timeout_seconds,
                        confirmed=confirmed,
                    )
            except ModuleLifecycleError as error:
                payload = self._event_payload(
                    request_id=request_id,
                    module_id=module_id,
                    operation=operation,
                    source=source,
                    previous_state=previous_state,
                    current_state=error.current_state,
                    previous_enabled=previous_enabled,
                    effective_enabled=self._safe_enabled(module_id),
                    blockers=error.blockers,
                    duration_ms=(time.monotonic() - started) * 1_000,
                    error_code=error.code,
                    human_message=error.human_message,
                    retryable=error.retryable,
                    suggested_action=error.suggested_action,
                )
                event_id = await self._emit(
                    event_sink,
                    "module.operation.blocked"
                    if error.code
                    in {
                        "module.active_dependents",
                        "module.dependency_disabled",
                        "module.lease_blocked",
                        "module.manual_control_denied",
                        "module.required",
                        "module.safe_mode_restricted",
                    }
                    else "module.operation.failed",
                    payload,
                )
                error.trace_event_id = event_id
                raise
            except BaseException as error:
                if isinstance(error, asyncio.CancelledError):
                    code = "module.operation_cancelled"
                    retryable = True
                    action = "刷新模块状态后重试"
                else:
                    code = "module.internal"
                    retryable = False
                    action = "运行 rivet doctor 并检查脱敏 Trace"
                wrapped = ModuleLifecycleError(
                    code=code,
                    module_id=module_id,
                    current_state=self._safe_state(module_id),
                    requested_operation=operation,
                    human_message="模块生命周期操作未完成",
                    retryable=retryable,
                    suggested_action=action,
                )
                payload = self._event_payload(
                    request_id=request_id,
                    module_id=module_id,
                    operation=operation,
                    source=source,
                    previous_state=previous_state,
                    current_state=wrapped.current_state,
                    previous_enabled=previous_enabled,
                    effective_enabled=self._safe_enabled(module_id),
                    duration_ms=(time.monotonic() - started) * 1_000,
                    error_code=code,
                    human_message=wrapped.human_message,
                    retryable=wrapped.retryable,
                    suggested_action=wrapped.suggested_action,
                )
                wrapped.trace_event_id = await self._emit(
                    event_sink, "module.operation.failed", payload
                )
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise wrapped from error

            completed_payload = self._event_payload(
                request_id=request_id,
                module_id=module_id,
                operation=operation,
                source=source,
                previous_state=result.previous_state,
                current_state=result.current_state,
                previous_enabled=result.previous_enabled,
                effective_enabled=result.effective_enabled,
                affected_modules=result.affected_modules,
                blockers=result.blockers,
                duration_ms=(time.monotonic() - started) * 1_000,
            )
            if result.previous_enabled != result.effective_enabled:
                await self._emit(
                    event_sink, "module.enablement.changed", completed_payload
                )
            if result.previous_state is not result.current_state:
                await self._emit(event_sink, "module.state.changed", completed_payload)
            trace_event_id = await self._emit(
                event_sink, "module.operation.completed", completed_payload
            )
            return result.model_copy(update={"trace_event_id": trace_event_id})

    def _enable(
        self,
        module_id: str,
        with_dependencies: bool,
        source: ModuleOperationSource,
    ) -> ModuleLifecycleResult:
        """实现无实例化的幂等策略变更。"""
        self._require_manual(module_id, ModuleOperation.ENABLE)
        targets = (
            self.runtime.dependency_closure(module_id)
            if with_dependencies
            else (module_id,)
        )
        for candidate in targets:
            self._require_manual(candidate, ModuleOperation.ENABLE)
        previous_state = self.runtime.state(module_id)
        previous_enabled = self.runtime.configured_enabled(module_id)
        changed = tuple(
            candidate
            for candidate in targets
            if not self.runtime.configured_enabled(candidate)
        )
        self._persist(changed, True, source, ModuleOperation.ENABLE)
        for candidate in changed:
            self.runtime.set_configured_enabled(candidate, True)
        blockers = tuple(
            f"依赖 {candidate} 当前已禁用"
            for candidate in self.runtime.dependency_closure(module_id)
            if candidate != module_id and not self.runtime.configured_enabled(candidate)
        )
        return ModuleLifecycleResult(
            operation=ModuleOperation.ENABLE,
            module_id=module_id,
            previous_enabled=previous_enabled,
            effective_enabled=self.runtime.effective_enabled(module_id),
            previous_state=previous_state,
            current_state=self.runtime.state(module_id),
            changed=bool(changed),
            affected_modules=changed,
            blockers=blockers,
        )

    async def _disable(
        self,
        module_id: str,
        *,
        cascade: bool,
        wait: bool,
        timeout_seconds: float,
        confirmed: bool,
        source: ModuleOperationSource,
    ) -> ModuleLifecycleResult:
        """先安全释放运行实例，再原子禁用策略。"""
        self._require_disable_allowed(module_id)
        previous_state = self.runtime.state(module_id)
        previous_enabled = self.runtime.configured_enabled(module_id)
        if not previous_enabled:
            return ModuleLifecycleResult(
                operation=ModuleOperation.DISABLE,
                module_id=module_id,
                previous_enabled=False,
                effective_enabled=self.runtime.effective_enabled(module_id),
                previous_state=previous_state,
                current_state=previous_state,
                changed=False,
            )
        dependents = self.runtime.enabled_dependents(module_id)
        if dependents and not cascade:
            self._raise(
                "module.active_dependents",
                module_id,
                ModuleOperation.DISABLE,
                "模块仍有已启用依赖者",
                blockers=dependents,
                suggested_action="使用 --cascade --yes 审查并禁用依赖者",
            )
        if cascade and not confirmed:
            self._raise(
                "module.confirmation_required",
                module_id,
                ModuleOperation.DISABLE,
                "级联禁用需要明确确认",
                blockers=dependents,
                suggested_action="审查受影响模块后传入 --yes",
            )
        ordered = tuple(
            reversed(self.runtime.dependent_closure(module_id))
            if cascade
            else (module_id,)
        )
        for candidate in ordered:
            self._require_disable_allowed(candidate)
        await self._wait_for_leases(
            ordered,
            operation=ModuleOperation.DISABLE,
            wait=wait,
            timeout_seconds=timeout_seconds,
        )
        slept: list[str] = []
        for candidate in ordered:
            state = self.runtime.state(candidate)
            if state not in {ModuleState.ACTIVE, ModuleState.IDLE}:
                continue
            try:
                did_sleep = await self.runtime.sleep_module(candidate)
            except ModuleActivationError as error:
                raise self._cleanup_error(candidate, ModuleOperation.DISABLE) from error
            if not did_sleep:
                self._raise(
                    "module.transition_conflict",
                    candidate,
                    ModuleOperation.DISABLE,
                    "模块无法进入安全休眠状态",
                    suggested_action="刷新 Lease、依赖者和模块状态后重试",
                )
            slept.append(candidate)
        changed = tuple(
            candidate
            for candidate in ordered
            if self.runtime.configured_enabled(candidate)
        )
        self._persist(changed, False, source, ModuleOperation.DISABLE)
        for candidate in changed:
            self.runtime.set_configured_enabled(candidate, False)
        affected = tuple(dict.fromkeys((*slept, *changed)))
        return ModuleLifecycleResult(
            operation=ModuleOperation.DISABLE,
            module_id=module_id,
            previous_enabled=previous_enabled,
            effective_enabled=self.runtime.effective_enabled(module_id),
            previous_state=previous_state,
            current_state=self.runtime.state(module_id),
            changed=bool(affected),
            affected_modules=affected,
        )

    async def _wake(
        self,
        module_id: str,
        with_dependencies: bool,
        source: ModuleOperationSource,
    ) -> ModuleLifecycleResult:
        """校验隔离、Safe Mode 和依赖启用后执行激活。"""
        self._require_manual(module_id, ModuleOperation.WAKE)
        previous_state = self.runtime.state(module_id)
        previous_enabled = self.runtime.configured_enabled(module_id)
        if previous_state is ModuleState.QUARANTINED:
            self._raise(
                "module.quarantined",
                module_id,
                ModuleOperation.WAKE,
                "模块处于崩溃隔离状态",
                suggested_action="检查激活 Trace 并执行安全恢复",
            )
        if previous_state is ModuleState.FAILED:
            self._raise(
                "module.transition_conflict",
                module_id,
                ModuleOperation.WAKE,
                "模块处于 FAILED，不能直接唤醒",
                suggested_action="检查最近错误并重启 Rivet 恢复运行时",
            )
        if (
            self.runtime.safe_mode
            and not self.runtime.manifest(module_id).safe_mode_allowed
        ):
            self._raise(
                "module.safe_mode_restricted",
                module_id,
                ModuleOperation.WAKE,
                "Safe Mode 禁止唤醒该可选模块",
                suggested_action="保持 Safe Mode，或审查配置后关闭 Safe Mode",
            )
        closure = self.runtime.dependency_closure(module_id)
        disabled = tuple(
            candidate
            for candidate in closure
            if not self.runtime.configured_enabled(candidate)
        )
        previous_overrides = {
            candidate: self._persisted_overrides.get(candidate)
            for candidate in disabled
        }
        previous_configured = {
            candidate: self.runtime.configured_enabled(candidate)
            for candidate in disabled
        }
        if disabled and not with_dependencies:
            self._raise(
                "module.dependency_disabled",
                module_id,
                ModuleOperation.WAKE,
                "模块或依赖当前已禁用",
                blockers=disabled,
                suggested_action="先运行 enable --with-dependencies",
            )
        for candidate in disabled:
            self._require_manual(candidate, ModuleOperation.ENABLE)
        if disabled:
            self._persist(disabled, True, source, ModuleOperation.WAKE)
            for candidate in disabled:
                self.runtime.set_configured_enabled(candidate, True)
        try:
            activated = await self.runtime.wake_module(module_id)
        except (ModuleQuarantinedError, SafeModeViolationError) as error:
            self._rollback_enablement(
                disabled, previous_overrides, previous_configured, source
            )
            code = (
                "module.quarantined"
                if isinstance(error, ModuleQuarantinedError)
                else "module.safe_mode_restricted"
            )
            self._raise(
                code,
                module_id,
                ModuleOperation.WAKE,
                "模块无法通过正式激活边界",
                suggested_action="检查模块状态和 Safe Mode 后重试",
            )
        except ModuleActivationError as error:
            self._rollback_enablement(
                disabled, previous_overrides, previous_configured, source
            )
            self._raise(
                "module.transition_conflict",
                module_id,
                ModuleOperation.WAKE,
                "模块激活失败",
                suggested_action="检查脱敏 Trace 与模块最近错误",
            )
            raise AssertionError from error
        affected = tuple(dict.fromkeys((*disabled, *activated)))
        return ModuleLifecycleResult(
            operation=ModuleOperation.WAKE,
            module_id=module_id,
            previous_enabled=previous_enabled,
            effective_enabled=self.runtime.effective_enabled(module_id),
            previous_state=previous_state,
            current_state=self.runtime.state(module_id),
            changed=bool(affected),
            affected_modules=affected,
        )

    async def _sleep(
        self,
        module_id: str,
        *,
        cascade: bool,
        wait: bool,
        timeout_seconds: float,
        confirmed: bool,
    ) -> ModuleLifecycleResult:
        """按逆拓扑释放依赖者，成功后验证状态和资源事实。"""
        self._require_sleep_allowed(module_id)
        previous_state = self.runtime.state(module_id)
        previous_enabled = self.runtime.configured_enabled(module_id)
        dependents = self.runtime.active_dependents(module_id)
        if dependents and not cascade:
            self._raise(
                "module.active_dependents",
                module_id,
                ModuleOperation.SLEEP,
                "模块仍有活动依赖者",
                blockers=dependents,
                suggested_action="等待依赖者结束，或使用 --cascade --yes",
            )
        if cascade and not confirmed:
            self._raise(
                "module.confirmation_required",
                module_id,
                ModuleOperation.SLEEP,
                "级联休眠需要明确确认",
                blockers=dependents,
                suggested_action="审查受影响模块后传入 --yes",
            )
        ordered = tuple(
            reversed(self.runtime.dependent_closure(module_id))
            if cascade
            else (module_id,)
        )
        for candidate in ordered:
            self._require_sleep_allowed(candidate)
        await self._wait_for_leases(
            ordered,
            operation=ModuleOperation.SLEEP,
            wait=wait,
            timeout_seconds=timeout_seconds,
        )
        slept: list[str] = []
        for candidate in ordered:
            state = self.runtime.state(candidate)
            if state not in {ModuleState.ACTIVE, ModuleState.IDLE}:
                continue
            try:
                did_sleep = await self.runtime.sleep_module(candidate)
            except ModuleActivationError as error:
                raise self._cleanup_error(candidate, ModuleOperation.SLEEP) from error
            if not did_sleep:
                self._raise(
                    "module.transition_conflict",
                    candidate,
                    ModuleOperation.SLEEP,
                    "模块无法进入安全休眠状态",
                    suggested_action="刷新 Lease、依赖者和模块状态后重试",
                )
            slept.append(candidate)
        return ModuleLifecycleResult(
            operation=ModuleOperation.SLEEP,
            module_id=module_id,
            previous_enabled=previous_enabled,
            effective_enabled=self.runtime.effective_enabled(module_id),
            previous_state=previous_state,
            current_state=self.runtime.state(module_id),
            changed=bool(slept),
            affected_modules=tuple(slept),
        )

    async def _wait_for_leases(
        self,
        module_ids: tuple[str, ...],
        *,
        operation: ModuleOperation,
        wait: bool,
        timeout_seconds: float,
    ) -> None:
        """等待 Lease 自然释放，超时或未请求等待时失败关闭。"""
        deadline = time.monotonic() + timeout_seconds
        while True:
            blockers = tuple(
                blocker
                for module_id in module_ids
                for blocker in self.runtime.lease_blockers(module_id)
            )
            if not blockers:
                return
            if not wait:
                self._raise(
                    "module.lease_blocked",
                    module_ids[-1],
                    operation,
                    "模块存在活动 Lease",
                    blockers=blockers,
                    retryable=True,
                    suggested_action="等待任务结束，或使用 --wait --timeout 30",
                )
            if time.monotonic() >= deadline:
                self._raise(
                    "module.lease_blocked",
                    module_ids[-1],
                    operation,
                    "等待活动 Lease 释放超时",
                    blockers=blockers,
                    retryable=True,
                    suggested_action="任务结束后重试，不要强制绕过资源边界",
                )
            await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _persist(
        self,
        module_ids: tuple[str, ...],
        enabled: bool,
        source: ModuleOperationSource,
        operation: ModuleOperation,
    ) -> None:
        """原子保存覆盖，失败时不更新运行时策略。"""
        if not module_ids:
            return
        changes = tuple(
            ModuleOverrideChange(
                module_id=module_id,
                scope=self.runtime.manifest(module_id).scope,
                enabled=enabled,
                source=self._persistence_source(source),
            )
            for module_id in module_ids
        )
        try:
            self._overrides.set_many(changes)
        except ModuleOverridePersistenceError as error:
            self._raise(
                "module.persistence_failed",
                module_ids[-1],
                operation,
                "模块启用策略无法原子持久化",
                retryable=True,
                suggested_action="检查 .rivet 状态目录权限与 SQLite 完整性",
            )
            raise AssertionError from error
        for module_id in module_ids:
            self._persisted_overrides[module_id] = enabled

    def _rollback_enablement(
        self,
        module_ids: tuple[str, ...],
        previous_overrides: Mapping[str, bool | None],
        previous_configured: Mapping[str, bool],
        source: ModuleOperationSource,
    ) -> None:
        """激活失败时恢复 wake 附带的启用策略。"""
        if not module_ids:
            return
        changes = tuple(
            ModuleOverrideChange(
                module_id=module_id,
                scope=self.runtime.manifest(module_id).scope,
                enabled=previous_overrides[module_id],
                source=self._persistence_source(source),
            )
            for module_id in module_ids
        )
        try:
            self._overrides.set_many(changes)
        except ModuleOverridePersistenceError as error:
            self._raise(
                "module.persistence_failed",
                module_ids[-1],
                ModuleOperation.WAKE,
                "模块激活失败且启用策略回滚失败",
                suggested_action="停止运行并检查 SQLite 状态后恢复",
            )
            raise AssertionError from error
        for module_id in module_ids:
            previous_override = previous_overrides[module_id]
            if previous_override is None:
                self._persisted_overrides.pop(module_id, None)
            else:
                self._persisted_overrides[module_id] = previous_override
            self.runtime.set_configured_enabled(
                module_id, previous_configured[module_id]
            )

    def _require_manual(self, module_id: str, operation: ModuleOperation) -> None:
        """拒绝用户直接控制内部模块。"""
        manifest = self._manifest_or_raise(module_id, operation)
        if not manifest.manual_control:
            self._raise(
                "module.manual_control_denied",
                module_id,
                operation,
                "该内部模块不允许用户直接控制",
                suggested_action="由 Kernel 根据能力请求管理该模块",
            )

    def _require_disable_allowed(self, module_id: str) -> None:
        """保护 required/eager 常驻模块。"""
        self._require_manual(module_id, ModuleOperation.DISABLE)
        manifest = self.runtime.manifest(module_id)
        if manifest.activation in {ActivationPolicy.REQUIRED, ActivationPolicy.EAGER}:
            self._raise(
                "module.required",
                module_id,
                ModuleOperation.DISABLE,
                "系统必需或 eager 模块不能禁用",
                suggested_action="保持模块启用并检查其依赖用途",
            )

    def _require_sleep_allowed(self, module_id: str) -> None:
        """保护常驻和明确禁止休眠的模块。"""
        self._require_manual(module_id, ModuleOperation.SLEEP)
        manifest = self.runtime.manifest(module_id)
        if (
            manifest.activation in {ActivationPolicy.REQUIRED, ActivationPolicy.EAGER}
            or manifest.sleep_policy is SleepPolicy.NEVER
        ):
            self._raise(
                "module.required",
                module_id,
                ModuleOperation.SLEEP,
                "系统常驻模块不能手动休眠",
                suggested_action="保持模块运行或结束整个 Rivet 进程",
            )

    def _manifest_or_raise(
        self, module_id: str, operation: ModuleOperation
    ) -> ModuleManifest:
        """把 Runtime 的未知模块错误转换为稳定生命周期错误。"""
        try:
            return self.runtime.manifest(module_id)
        except CapabilityNotFoundError:
            raise ModuleLifecycleError(
                code="module.not_found",
                module_id=module_id,
                current_state=ModuleState.DISCOVERED,
                requested_operation=operation,
                human_message="模块不存在",
                suggested_action="运行 rivet modules list 查看有效模块 ID",
            ) from None

    def _raise(
        self,
        code: str,
        module_id: str,
        operation: ModuleOperation,
        message: str,
        *,
        blockers: tuple[str, ...] = (),
        retryable: bool = False,
        suggested_action: str,
    ) -> Never:
        """集中构造不携带原始异常和敏感输入的错误。"""
        raise ModuleLifecycleError(
            code=code,
            module_id=module_id,
            current_state=self._safe_state(module_id),
            requested_operation=operation,
            human_message=message,
            blockers=blockers,
            retryable=retryable,
            suggested_action=suggested_action,
        )

    def _cleanup_error(
        self, module_id: str, operation: ModuleOperation
    ) -> ModuleLifecycleError:
        """构造资源清理失败错误。"""
        return ModuleLifecycleError(
            code="module.resource_cleanup_failed",
            module_id=module_id,
            current_state=self._safe_state(module_id),
            requested_operation=operation,
            human_message="模块资源清理失败",
            retryable=False,
            suggested_action="停止后续操作并检查资源 Trace",
        )

    def _safe_state(self, module_id: str) -> ModuleState:
        """未知 ID 仅用于 Trace 前置字段，不泄露异常。"""
        try:
            return self.runtime.state(module_id)
        except CapabilityNotFoundError:
            return ModuleState.DISCOVERED

    def _safe_enabled(self, module_id: str) -> bool:
        """未知 ID 在 Trace 前置字段中按未启用处理。"""
        try:
            return self.runtime.effective_enabled(module_id)
        except CapabilityNotFoundError:
            return False

    def _validate_timeout(
        self,
        module_id: str,
        operation: ModuleOperation,
        timeout_seconds: float,
    ) -> None:
        """限制等待时间并拒绝 bool、负数和无限值。"""
        if (
            isinstance(timeout_seconds, bool)
            or not 0 <= timeout_seconds <= 300
            or not float(timeout_seconds) < float("inf")
        ):
            self._raise(
                "module.input_invalid",
                module_id,
                operation,
                "模块等待超时必须在 0 到 300 秒之间",
                suggested_action="传入 0 到 300 之间的 --timeout 秒数",
            )

    @staticmethod
    def _persistence_source(
        source: ModuleOperationSource,
    ) -> Literal["cli", "tui", "recovery"]:
        """把只读/自动来源收窄到 SQLite 白名单。"""
        if source is ModuleOperationSource.CLI:
            return "cli"
        if source is ModuleOperationSource.TUI:
            return "tui"
        return "recovery"

    @staticmethod
    async def _emit(
        sink: ModuleLifecycleEventSink | None,
        event_type: str,
        payload: dict[str, JsonValue],
    ) -> str | None:
        """在没有事件接收器的纯 Kernel 测试中保持无 I/O。"""
        return None if sink is None else await sink.emit(event_type, payload)

    @staticmethod
    def _event_payload(
        *,
        request_id: str,
        module_id: str,
        operation: ModuleOperation,
        source: ModuleOperationSource,
        previous_state: ModuleState,
        current_state: ModuleState,
        previous_enabled: bool,
        effective_enabled: bool,
        affected_modules: tuple[str, ...] = (),
        blockers: tuple[str, ...] = (),
        duration_ms: float = 0,
        error_code: str | None = None,
        human_message: str | None = None,
        retryable: bool = False,
        suggested_action: str | None = None,
    ) -> dict[str, JsonValue]:
        """构造固定字段且无用户文件内容的生命周期事件载荷。"""
        return {
            "request_id": request_id,
            "module_id": module_id,
            "operation": operation.value,
            "source": source.value,
            "previous_state": previous_state.value,
            "current_state": current_state.value,
            "state": current_state.value,
            "previous_enabled": previous_enabled,
            "effective_enabled": effective_enabled,
            "affected_modules": list(affected_modules),
            "blockers": list(blockers),
            "duration_ms": round(duration_ms, 3),
            "error_code": error_code,
            "human_message": human_message,
            "retryable": retryable,
            "suggested_action": suggested_action,
        }
