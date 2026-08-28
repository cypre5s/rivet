"""贯通正式 Kernel、SQLite、Trace 与模块生命周期 CLI。"""

from __future__ import annotations

import json
import uuid
from argparse import Namespace
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from rivet.cli.errors import CliModuleError
from rivet.cli.exit_codes import ExitCode
from rivet.cli.runtime import create_cli_kernel
from rivet.contracts.modules import (
    ModuleLifecycleResult,
    ModuleOperation,
    ModuleOperationSource,
    ModuleState,
    ModuleStatus,
)
from rivet.ipc.worker import EmitEvent
from rivet.kernel.application import RivetKernel
from rivet.kernel.module_lifecycle import (
    ModuleLifecycleError,
    ModuleLifecycleEventSink,
)
from rivet.trace.builder import TraceEventBuilder
from rivet.trace.paths import RuntimePaths
from rivet.trace.store import TraceStore


class ModuleTraceSink(ModuleLifecycleEventSink):
    """持久化生命周期 Trace，并可把同一事实投影到 TUI。"""

    def __init__(
        self,
        trace: TraceStore,
        *,
        emit: EmitEvent | None = None,
    ) -> None:
        suffix = uuid.uuid4().hex
        self._trace = trace
        self._builder = TraceEventBuilder()
        self._run_id = f"run_module_{suffix}"
        self._session_id = f"session_module_{suffix}"
        self._emit_ipc = emit

    async def emit(
        self,
        event_type: str,
        payload: dict[str, JsonValue],
    ) -> str:
        """先写入脱敏事实源，再发布同一载荷到 IPC。"""
        event = self._builder.build(
            event_type=event_type,
            run_id=self._run_id,
            session_id=self._session_id,
            payload=payload,
            input_summary=(
                f"模块操作 {payload.get('operation', 'unknown')}"
                if event_type == "module.operation.requested"
                else None
            ),
            result_summary=(
                "模块生命周期操作已完成"
                if event_type == "module.operation.completed"
                else None
            ),
        )
        await self._trace.emit(event)
        if self._emit_ipc is not None:
            with suppress(Exception):
                await self._emit_ipc(event_type, payload)
        return event.event_id


class ModuleCommandController:
    """让 CLI 与长驻 TUI Worker 共用同一生命周期应用入口。"""

    def __init__(self, repository: Path, *, safe_mode: bool) -> None:
        self.repository = repository
        self.kernel: RivetKernel = create_cli_kernel(repository, safe_mode=safe_mode)
        self._trace: TraceStore | None = None
        self._started = False

    async def start(self) -> None:
        """只启动 required/eager 模块，不激活任何可选模块。"""
        if self._started:
            return
        await self.kernel.start()
        self._started = True

    async def close(self) -> None:
        """关闭 Trace 与 Runtime，并执行资源归零门禁。"""
        first_error: BaseException | None = None
        if self._trace is not None:
            try:
                await self._trace.close()
            except BaseException as error:
                first_error = error
        try:
            await self.kernel.shutdown()
        except BaseException as error:
            if first_error is None:
                first_error = error
        self._started = False
        if first_error is not None:
            raise first_error

    def list_mapping(self) -> dict[str, object]:
        """返回不会触发 factory 的完整模块状态列表。"""
        return module_status_mapping(self.kernel.module_lifecycle.statuses())

    def show_mapping(self, module_id: str) -> dict[str, object]:
        """返回单模块详情，未知 ID 使用正式错误。"""
        status = next(
            (
                item
                for item in self.kernel.module_lifecycle.statuses()
                if item.module_id == module_id
            ),
            None,
        )
        if status is None:
            raise ModuleLifecycleError(
                code="module.not_found",
                module_id=module_id,
                current_state=ModuleState.DISCOVERED,
                requested_operation=ModuleOperation.ENABLE,
                human_message="模块不存在",
                suggested_action="运行 rivet modules list 查看有效模块 ID",
            )
        return {
            "module": module_status_public_mapping(status),
            "schema_version": 1,
            "source": "module_runtime",
        }

    async def operate(
        self,
        operation: str,
        module_id: str,
        *,
        source: ModuleOperationSource,
        request_id: str,
        with_dependencies: bool = False,
        cascade: bool = False,
        wait: bool = False,
        timeout_seconds: float = 30,
        confirmed: bool = False,
        emit: EmitEvent | None = None,
    ) -> dict[str, object]:
        """把已校验选项交给唯一 ModuleLifecycleService。"""
        sink = await self._trace_sink(emit=emit)
        service = self.kernel.module_lifecycle
        if operation == "enable":
            result = await service.enable(
                module_id,
                with_dependencies=with_dependencies,
                source=source,
                request_id=request_id,
                event_sink=sink,
            )
        elif operation == "disable":
            result = await service.disable(
                module_id,
                cascade=cascade,
                wait=wait,
                timeout_seconds=timeout_seconds,
                confirmed=confirmed,
                source=source,
                request_id=request_id,
                event_sink=sink,
            )
        elif operation == "wake":
            result = await service.wake(
                module_id,
                with_dependencies=with_dependencies,
                source=source,
                request_id=request_id,
                event_sink=sink,
            )
        elif operation == "sleep":
            result = await service.sleep(
                module_id,
                cascade=cascade,
                wait=wait,
                timeout_seconds=timeout_seconds,
                confirmed=confirmed,
                source=source,
                request_id=request_id,
                event_sink=sink,
            )
        else:
            raise CliModuleError(
                "module.operation_unknown",
                "模块操作不存在",
                "使用 list/show/enable/disable/wake/sleep",
                exit_code=ExitCode.USAGE,
            )
        return module_result_mapping(result)

    async def _trace_sink(self, *, emit: EmitEvent | None) -> ModuleTraceSink:
        """仅在写操作发生时创建 Trace Store。"""
        if self._trace is None:
            self._trace = TraceStore(RuntimePaths.for_repository(self.repository))
            await self._trace.start()
        return ModuleTraceSink(self._trace, emit=emit)


def module_status_public_mapping(status: ModuleStatus) -> dict[str, object]:
    """把严格状态契约转换为前后端共享的 JSON 字段。"""
    return {
        "activation": status.activation.value,
        "active_resource_count": status.active_resource_count,
        "active_resources": status.active_resource_count,
        "configured_enabled": status.configured_enabled,
        "dependencies": list(status.dependencies),
        "dependents": list(status.dependents),
        "effective_enabled": status.effective_enabled,
        "last_error": status.last_error,
        "lease_count": status.lease_count,
        "leases": status.lease_count,
        "manifest_default_enabled": status.manifest_default_enabled,
        "manual_control": status.manual_control,
        "module_id": status.module_id,
        "persisted_override": status.persisted_override,
        "provided_capabilities": list(status.provided_capabilities),
        "runtime_state": status.runtime_state.value,
        "scope": status.scope.value,
        "sleep_policy": status.sleep_policy.value,
    }


def module_status_mapping(statuses: tuple[ModuleStatus, ...]) -> dict[str, object]:
    """汇总模块状态，不触发额外激活。"""
    modules = [module_status_public_mapping(status) for status in statuses]
    return {
        "modules": modules,
        "schema_version": 1,
        "source": "module_runtime",
        "summary": {
            "active": sum(
                status.runtime_state in {ModuleState.ACTIVE, ModuleState.IDLE}
                for status in statuses
            ),
            "disabled": sum(not status.configured_enabled for status in statuses),
            "quarantined": sum(
                status.runtime_state is ModuleState.QUARANTINED for status in statuses
            ),
            "resource_count": sum(status.active_resource_count for status in statuses),
            "total": len(statuses),
        },
    }


def module_result_mapping(result: ModuleLifecycleResult) -> dict[str, object]:
    """返回机器可读且不含内部异常的操作结果。"""
    return {
        "affected_modules": list(result.affected_modules),
        "blockers": list(result.blockers),
        "changed": result.changed,
        "current_state": result.current_state.value,
        "effective_enabled": result.effective_enabled,
        "module_id": result.module_id,
        "operation": result.operation.value,
        "previous_enabled": result.previous_enabled,
        "previous_state": result.previous_state.value,
        "schema_version": 1,
        "trace_event_id": result.trace_event_id,
    }


async def load_module_status_mapping(
    repository: Path,
    *,
    safe_mode: bool,
) -> dict[str, object]:
    """启动正式 Kernel，读取真实状态并执行资源归零关闭。"""
    controller = ModuleCommandController(repository, safe_mode=safe_mode)
    try:
        await controller.start()
        return controller.list_mapping()
    finally:
        await controller.close()


async def run_module_command(
    arguments: Namespace,
    *,
    repository: Path,
    safe_mode: bool,
    json_output: bool,
) -> int:
    """执行正式 modules 子命令并稳定映射生命周期错误。"""
    controller = ModuleCommandController(repository, safe_mode=safe_mode)
    try:
        await controller.start()
        command = cast(str | None, getattr(arguments, "module_command", None)) or "list"
        if command == "list":
            payload = controller.list_mapping()
        elif command == "show":
            payload = controller.show_mapping(cast(str, arguments.module_id))
        else:
            payload = await controller.operate(
                command,
                cast(str, arguments.module_id),
                source=ModuleOperationSource.CLI,
                request_id=f"request_module_{uuid.uuid4().hex}",
                with_dependencies=bool(getattr(arguments, "with_dependencies", False)),
                cascade=bool(getattr(arguments, "cascade", False)),
                wait=bool(getattr(arguments, "wait", False)),
                timeout_seconds=float(getattr(arguments, "timeout", 30.0)),
                confirmed=bool(getattr(arguments, "yes", False)),
            )
        _print_module_payload(payload, json_output=json_output)
        return int(ExitCode.SUCCESS)
    except ModuleLifecycleError as error:
        raise _cli_error(error) from error
    finally:
        await controller.close()


def _print_module_payload(payload: Mapping[str, object], *, json_output: bool) -> None:
    """机器模式输出稳定 JSON，人类模式输出紧凑事实。"""
    if json_output:
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    modules = payload.get("modules")
    if isinstance(modules, list):
        module_items = cast(list[object], modules)
        print("ID\tENABLED\tSTATE\tSCOPE\tPOLICY\tLEASES\tRESOURCES\tDEPENDENCIES")
        for raw_module in module_items:
            if not isinstance(raw_module, dict):
                continue
            module = cast(dict[str, object], raw_module)
            dependencies = module.get("dependencies")
            rendered_dependencies = (
                ",".join(str(item) for item in cast(list[object], dependencies))
                if isinstance(dependencies, list)
                else ""
            )
            print(
                "\t".join(
                    (
                        str(module.get("module_id", "")),
                        str(module.get("effective_enabled", False)).lower(),
                        str(module.get("runtime_state", "UNKNOWN")),
                        str(module.get("scope", "")),
                        str(module.get("activation", "")),
                        str(module.get("lease_count", 0)),
                        str(module.get("active_resource_count", 0)),
                        rendered_dependencies or "-",
                    )
                )
            )
        return
    module = payload.get("module")
    if isinstance(module, dict):
        for key, value in cast(dict[str, object], module).items():
            print(f"{key}: {value}")
        return
    print(f"模块：{payload.get('module_id', '')}")
    print(f"操作：{payload.get('operation', '')}")
    print(f"状态：{payload.get('current_state', '')}")
    print(f"已启用：{str(payload.get('effective_enabled', False)).lower()}")
    print(f"发生变化：{str(payload.get('changed', False)).lower()}")
    affected = payload.get("affected_modules")
    if isinstance(affected, list) and affected:
        affected_items = cast(list[object], affected)
        print(f"受影响模块：{', '.join(str(item) for item in affected_items)}")
    if trace_event_id := payload.get("trace_event_id"):
        print(f"Trace：{trace_event_id}")


def _cli_error(error: ModuleLifecycleError) -> CliModuleError:
    """按公开模块退出码区分不存在、策略、阻塞、资源和持久化。"""
    if error.code == "module.not_found":
        exit_code = ExitCode.CONFIGURATION
    elif error.code == "module.input_invalid":
        exit_code = ExitCode.USAGE
    elif error.code in {
        "module.manual_control_denied",
        "module.required",
        "module.safe_mode_restricted",
        "module.confirmation_required",
    }:
        exit_code = ExitCode.VERIFICATION_FAILED
    elif error.code in {
        "module.active_dependents",
        "module.dependency_disabled",
        "module.disabled",
        "module.lease_blocked",
        "module.quarantined",
        "module.transition_conflict",
    }:
        exit_code = ExitCode.SECURITY_DENIED
    elif error.code == "module.resource_cleanup_failed":
        exit_code = ExitCode.PROVIDER_FAILED
    elif error.code == "module.persistence_failed":
        exit_code = ExitCode.MODULE_PERSISTENCE_FAILED
    else:
        exit_code = ExitCode.MODULE_INTERNAL_ERROR
    blocker_suffix = f"；阻塞：{', '.join(error.blockers)}" if error.blockers else ""
    return CliModuleError(
        error.code,
        f"{error.human_message}{blocker_suffix}",
        error.suggested_action,
        exit_code=exit_code,
    )
