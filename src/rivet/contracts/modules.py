"""定义按需模块 Manifest、生命周期和资源归属契约。"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

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
    ON_DEMAND = "on_demand"


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
        return self


class ResourceRecord(ContractModel):
    """记录资源所属模块与回收状态，不序列化原始句柄。"""

    resource_id: ResourceId
    owner_module_id: ModuleId
    kind: ResourceKind
    created_at: Timestamp
    active: bool
    description: str = Field(default="", max_length=1_024)
