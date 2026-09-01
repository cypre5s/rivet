"""定义真实 capability 模块的激活上下文与生命周期协议。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from rivet.contracts.common import CapabilityId, ModuleId
from rivet.kernel.resources import ResourceScope

CredentialAccessor = Callable[[str], str | None]


def _empty_dependencies() -> Mapping[CapabilityId, object]:
    """返回不可变空依赖映射，避免跨模块共享可变状态。"""
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ModuleActivationContext:
    """向模块提供仓库身份和经过约束的初始化依赖。"""

    repository: Path
    safe_mode: bool
    provider_base_url: str | None = None
    credential_accessor: CredentialAccessor | None = None
    module_id: ModuleId | None = None
    declared_capabilities: tuple[CapabilityId, ...] = ()
    dependencies: Mapping[CapabilityId, object] = field(
        default_factory=_empty_dependencies
    )

    def bind(
        self,
        module_id: ModuleId,
        declared_capabilities: tuple[CapabilityId, ...],
        dependencies: Mapping[CapabilityId, object],
    ) -> ModuleActivationContext:
        """为一次激活冻结模块 ID 与真实依赖 capability。"""
        return replace(
            self,
            module_id=module_id,
            declared_capabilities=declared_capabilities,
            dependencies=MappingProxyType(dict(dependencies)),
        )


@runtime_checkable
class ModuleInstance(Protocol):
    """要求构造无副作用，activate 返回 Manifest 声明的真实能力。"""

    async def activate(
        self,
        context: ModuleActivationContext,
        scope: ResourceScope,
    ) -> Mapping[CapabilityId, object]:
        """显式启动模块、登记资源并返回真实 capability mapping。"""
        ...

    async def sleep(self) -> None:
        """释放模块自身不由 scope 表示的内存状态。"""
        ...

    async def shutdown(self) -> None:
        """执行程序退出所需的模块级关闭动作。"""
        ...


ModuleFactory = Callable[[], ModuleInstance]
