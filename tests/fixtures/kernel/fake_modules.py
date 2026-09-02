"""提供可观测生命周期与资源行为的测试模块。"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rivet.kernel.module_api import ModuleActivationContext
    from rivet.kernel.resources import ResourceScope

factory_calls: Counter[str] = Counter()
lifecycle_events: list[str] = []


class RecordingModule:
    """记录激活、休眠和关闭顺序。"""

    def __init__(self, name: str, *, fail_activation: bool = False) -> None:
        self.name = name
        self.fail_activation = fail_activation
        self.activation_count = 0

    async def activate(
        self,
        context: ModuleActivationContext,
        scope: ResourceScope,
    ) -> dict[str, object]:
        """记录显式激活并按配置制造可控失败。"""
        del scope
        self.activation_count += 1
        lifecycle_events.append(f"activate:{self.name}")
        if self.fail_activation:
            raise RuntimeError("可控激活失败")
        return {capability_id: self for capability_id in context.declared_capabilities}

    async def sleep(self) -> None:
        """记录模块进入休眠。"""
        lifecycle_events.append(f"sleep:{self.name}")

    async def shutdown(self) -> None:
        """记录模块关闭。"""
        lifecycle_events.append(f"shutdown:{self.name}")


class ResourceModule(RecordingModule):
    """激活时创建 Task、Process 与临时目录。"""

    def __init__(self) -> None:
        super().__init__("resource")
        self.temporary_directory: Path | None = None
        self.process: asyncio.subprocess.Process | None = None

    async def activate(
        self,
        context: ModuleActivationContext,
        scope: ResourceScope,
    ) -> dict[str, object]:
        """创建由所属 ResourceScope 统一回收的真实资源。"""
        capabilities = await super().activate(context, scope)
        scope.create_task(asyncio.sleep(3_600), description="测试后台任务")
        self.process = await scope.create_process(
            sys.executable,
            "-c",
            "import time; time.sleep(3600)",
            description="测试子进程",
        )
        self.temporary_directory = scope.create_temp_directory(
            description="测试临时目录"
        )
        return capabilities


class FailOnceShutdownModule(RecordingModule):
    """首次关闭失败、第二次可由 Kernel shutdown 完成回收。"""

    def __init__(self) -> None:
        super().__init__("fail_once_shutdown")
        self.shutdown_count = 0

    async def shutdown(self) -> None:
        """记录每次关闭，并只在第一次制造确定性故障。"""
        self.shutdown_count += 1
        lifecycle_events.append("shutdown:fail_once_shutdown")
        if self.shutdown_count == 1:
            raise RuntimeError("可控首次关闭失败")


def reset_observations() -> None:
    """清空跨测试共享的计数和事件。"""
    factory_calls.clear()
    lifecycle_events.clear()


def create_recording_module() -> RecordingModule:
    """创建普通可观测模块。"""
    factory_calls["recording"] += 1
    return RecordingModule("recording")


def create_dependency_module() -> RecordingModule:
    """创建依赖模块。"""
    factory_calls["dependency"] += 1
    return RecordingModule("dependency")


def create_failing_module() -> RecordingModule:
    """创建激活时失败的模块。"""
    factory_calls["failing"] += 1
    return RecordingModule("failing", fail_activation=True)


def create_resource_module() -> ResourceModule:
    """创建会申请真实资源的模块。"""
    factory_calls["resource"] += 1
    return ResourceModule()


def create_fail_once_shutdown_module() -> FailOnceShutdownModule:
    """创建用于验证 Lease 失败后继续清理依赖的模块。"""
    factory_calls["fail_once_shutdown"] += 1
    return FailOnceShutdownModule()
