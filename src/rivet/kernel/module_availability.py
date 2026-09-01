"""以静态包元数据和 PATH 探测模块激活前提。"""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass

from rivet.contracts.modules import (
    ActivationPolicy,
    ModuleAvailability,
    ModuleManifest,
)


@dataclass(frozen=True, slots=True)
class ModuleAvailabilityReport:
    """保存不导入实现、不启动进程即可得到的可用性事实。"""

    state: ModuleAvailability
    missing_components: tuple[str, ...] = ()
    suggested_action: str | None = None


def probe_module_availability(
    manifest: ModuleManifest,
    *,
    safe_mode: bool,
) -> ModuleAvailabilityReport:
    """按 Safe Mode、Python 包和 executable 的优先级执行轻量探测。"""
    if (
        safe_mode
        and manifest.activation is not ActivationPolicy.REQUIRED
        and not manifest.safe_mode_allowed
    ):
        return ModuleAvailabilityReport(
            state=ModuleAvailability.SAFE_MODE_RESTRICTED,
            suggested_action="保持 Safe Mode，或审查配置后关闭 Safe Mode",
        )

    missing_packages = tuple(
        package
        for package in manifest.required_python_packages
        if not _package_available(package)
    )
    if missing_packages:
        return ModuleAvailabilityReport(
            state=ModuleAvailability.MISSING_DEPENDENCY,
            missing_components=missing_packages,
            suggested_action=_install_action(manifest),
        )

    missing_executables = tuple(
        executable
        for executable in manifest.required_executables
        if shutil.which(executable) is None
    )
    if missing_executables:
        return ModuleAvailabilityReport(
            state=ModuleAvailability.MISSING_EXECUTABLE,
            missing_components=missing_executables,
            suggested_action=(
                "安装系统命令：" + ", ".join(missing_executables) + "，然后重试"
            ),
        )

    return ModuleAvailabilityReport(state=ModuleAvailability.AVAILABLE)


def _package_available(package: str) -> bool:
    """只查询 import spec；缺失父包或损坏 metadata 时按不可用处理。"""
    try:
        return importlib.util.find_spec(package) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _install_action(manifest: ModuleManifest) -> str:
    """返回与项目安装档位一致且不含环境细节的恢复动作。"""
    if manifest.install_extra is not None:
        return f"运行 uv sync --extra {manifest.install_extra} 安装该能力"
    return "安装缺失的 Python 依赖后重试"
