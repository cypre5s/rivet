"""提供不导入具体能力实现的生产模块生命周期工厂。"""

from __future__ import annotations

from rivet.kernel.resources import ResourceScope


class ScopedCapabilityModule:
    """只持有 ModuleRuntime 注入的资源域，具体服务由编排层按需构造。"""

    def __init__(self) -> None:
        self._scope: ResourceScope | None = None

    @property
    def resource_scope(self) -> ResourceScope:
        """拒绝在未激活或已关闭后取得资源域。"""
        if self._scope is None:
            raise RuntimeError("模块资源域当前不可用")
        return self._scope

    async def activate(self, scope: ResourceScope) -> None:
        """接收由 ModuleRuntime 创建并负责回收的资源域。"""
        if self._scope is not None:
            raise RuntimeError("模块不得重复激活")
        self._scope = scope

    async def sleep(self) -> None:
        """丢弃资源域引用，真实资源由 ModuleRuntime 随后关闭。"""
        self._scope = None

    async def shutdown(self) -> None:
        """丢弃资源域引用，保持关闭幂等。"""
        self._scope = None


def create_provider_module() -> ScopedCapabilityModule:
    """构造模型 Provider 生命周期模块。"""
    return ScopedCapabilityModule()


def create_context_module() -> ScopedCapabilityModule:
    """构造上下文检索生命周期模块。"""
    return ScopedCapabilityModule()


def create_reader_module() -> ScopedCapabilityModule:
    """构造文件读取生命周期模块。"""
    return ScopedCapabilityModule()


def create_transaction_module() -> ScopedCapabilityModule:
    """构造隔离事务生命周期模块。"""
    return ScopedCapabilityModule()


def create_verify_module() -> ScopedCapabilityModule:
    """构造确定性验证生命周期模块。"""
    return ScopedCapabilityModule()


def create_guard_module() -> ScopedCapabilityModule:
    """构造本地权限与工具生命周期模块。"""
    return ScopedCapabilityModule()
