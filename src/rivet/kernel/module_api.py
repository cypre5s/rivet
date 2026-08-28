"""定义模块 factory 与显式生命周期的最小进程内协议。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from rivet.kernel.resources import ResourceScope


@runtime_checkable
class ModuleInstance(Protocol):
    """要求构造无副作用，资源仅在 activate 中注册。"""

    async def activate(self, scope: ResourceScope) -> None:
        """显式启动模块并将所有外部资源登记到 scope。"""
        ...

    async def sleep(self) -> None:
        """释放模块自身不由 scope 表示的内存状态。"""
        ...

    async def shutdown(self) -> None:
        """执行程序退出所需的模块级关闭动作。"""
        ...


@runtime_checkable
class ScopedModuleInstance(ModuleInstance, Protocol):
    """向编排层暴露由 ModuleRuntime 独占管理的资源域。"""

    @property
    def resource_scope(self) -> ResourceScope:
        """返回仅在模块处于活动期时有效的资源域。"""
        ...


ModuleFactory = Callable[[], ModuleInstance]
