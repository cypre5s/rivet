"""定义按需模块 Manifest、生命周期和资源归属契约。"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from rivet.contracts.common import (
    CapabilityId,
    ModuleId,
)

FactoryPath = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*:[a-z_][a-z0-9_]*$",
        max_length=256,
    ),
]


class ModuleState(StrEnum):
    """列出单次进程内按需模块的最小生命周期状态。"""

    INACTIVE = "INACTIVE"
    ACTIVATING = "ACTIVATING"
    ACTIVE = "ACTIVE"
    FAILED = "FAILED"


class ResourceKind(StrEnum):
    """标识必须由 ResourceScope 回收的长期或外部资源。"""

    TASK = "task"
    PROCESS = "process"
    CLIENT = "client"
    CONNECTION = "connection"
    TEMP_DIRECTORY = "temp_directory"
    WORKTREE = "worktree"


class ModuleManifest(BaseModel):
    """仅描述惰性 factory、提供能力和静态依赖图。"""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        validate_default=True,
        hide_input_in_errors=True,
    )

    module_id: ModuleId
    factory: FactoryPath
    provides: tuple[CapabilityId, ...] = Field(min_length=1)
    requires: tuple[ModuleId, ...] = ()

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
