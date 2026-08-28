"""定义按需模块 Manifest、生命周期和资源归属契约。"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from rivet.contracts.common import (
    CapabilityId,
    ContractModel,
    ModuleId,
    ResourceId,
    SemVer,
    Timestamp,
)

FactoryPath = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*:[a-z_][a-z0-9_]*$",
        max_length=256,
    ),
]


class ActivationPolicy(StrEnum):
    """区分必需常驻与按需激活策略。"""

    REQUIRED = "required"
    EAGER = "eager"
    ON_DEMAND = "on_demand"


class ModuleScope(StrEnum):
    """区分应用级和仓库工作区级启用覆盖。"""

    APPLICATION = "application"
    WORKSPACE = "workspace"


class SleepPolicy(StrEnum):
    """声明模块是否允许自动或手动释放实例。"""

    AUTOMATIC = "automatic"
    MANUAL = "manual"
    NEVER = "never"


class ModuleOperation(StrEnum):
    """列出统一生命周期服务接受的写操作。"""

    ENABLE = "enable"
    DISABLE = "disable"
    WAKE = "wake"
    SLEEP = "sleep"


class ModuleOperationSource(StrEnum):
    """标识生命周期转换的可信调用来源。"""

    CLI = "cli"
    TUI = "tui"
    KERNEL_AUTO = "kernel_auto"
    SAFE_MODE = "safe_mode"
    RECOVERY = "recovery"
    SHUTDOWN = "shutdown"


class ModuleState(StrEnum):
    """列出可回放的完整模块生命周期状态。"""

    DISCOVERED = "DISCOVERED"
    INACTIVE = "INACTIVE"
    ACTIVATING = "ACTIVATING"
    ACTIVE = "ACTIVE"
    IDLE = "IDLE"
    SLEEPING = "SLEEPING"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"


class ResourceKind(StrEnum):
    """标识必须由 ResourceScope 回收的长期或外部资源。"""

    TASK = "task"
    PROCESS = "process"
    CLIENT = "client"
    CONNECTION = "connection"
    TEMP_DIRECTORY = "temp_directory"
    WORKTREE = "worktree"
    FILE_WATCHER = "file_watcher"
    TIMER = "timer"
    SIDECAR = "sidecar"


class ModuleManifest(ContractModel):
    """描述启动时可静态解析、不导入 factory 的模块元数据。"""

    module_id: ModuleId
    module_version: SemVer
    activation: ActivationPolicy
    factory: FactoryPath
    enabled: bool = True
    safe_mode_allowed: bool = False
    manual_control: bool = True
    scope: ModuleScope = ModuleScope.WORKSPACE
    sleep_policy: SleepPolicy = SleepPolicy.AUTOMATIC
    priority: int = 0
    provides: tuple[CapabilityId, ...] = Field(min_length=1)
    requires: tuple[ModuleId, ...] = ()
    idle_timeout_seconds: int | None = Field(default=300, ge=0)

    @model_validator(mode="after")
    def _validate_unique_edges(self) -> Self:
        """拒绝重复能力、重复依赖和显式自依赖。"""
        if len(set(self.provides)) != len(self.provides):
            raise ValueError("Manifest 不得重复声明 capability")
        if len(set(self.requires)) != len(self.requires):
            raise ValueError("Manifest 不得重复声明依赖")
        if self.module_id in self.requires:
            raise ValueError("Manifest 不得依赖自身")
        if self.activation is ActivationPolicy.REQUIRED and not self.enabled:
            raise ValueError("必需模块不得默认禁用")
        return self


class ModuleLifecycleResult(ContractModel):
    """返回一次生命周期写操作的可审计结果。"""

    operation: ModuleOperation
    module_id: ModuleId
    previous_enabled: bool
    effective_enabled: bool
    previous_state: ModuleState
    current_state: ModuleState
    changed: bool
    affected_modules: tuple[ModuleId, ...] = ()
    blockers: tuple[str, ...] = ()
    trace_event_id: str | None = None


class ModuleStatus(ContractModel):
    """汇总 Manifest、持久化策略与当前进程运行事实。"""

    module_id: ModuleId
    manifest_default_enabled: bool
    persisted_override: bool | None
    configured_enabled: bool
    effective_enabled: bool
    runtime_state: ModuleState
    activation: ActivationPolicy
    scope: ModuleScope
    manual_control: bool
    sleep_policy: SleepPolicy
    dependencies: tuple[ModuleId, ...] = ()
    dependents: tuple[ModuleId, ...] = ()
    provided_capabilities: tuple[CapabilityId, ...] = ()
    lease_count: int = Field(ge=0)
    active_resource_count: int = Field(ge=0)
    last_error: str | None = None


class ModuleOverrideChange(ContractModel):
    """描述一项待原子写入或删除的模块启用覆盖。"""

    module_id: ModuleId
    scope: ModuleScope
    enabled: bool | None
    source: Literal["cli", "tui", "recovery"]


class ResourceRecord(ContractModel):
    """记录资源所属模块与回收状态，不序列化原始句柄。"""

    resource_id: ResourceId
    owner_module_id: ModuleId
    kind: ResourceKind
    created_at: Timestamp
    active: bool
    description: str = Field(default="", max_length=1_024)
