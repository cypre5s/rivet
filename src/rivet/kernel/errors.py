"""定义 Kernel 与模块运行时的稳定失败类型。"""

from __future__ import annotations


class KernelError(RuntimeError):
    """作为薄 Kernel 可预期错误的公共基类。"""


class DemandCausalityError(KernelError):
    """表示 Demand 来源、父节点或运行身份不满足因果约束。"""


class DemandJournalError(KernelError):
    """表示 Demand 无法在能力激活前完成耐久写入。"""


class ManifestError(KernelError):
    """表示 Manifest 不可读取或不满足静态契约。"""


class CapabilityConflictError(KernelError):
    """表示多个启用模块声明同一 capability。"""


class CapabilityNotFoundError(KernelError):
    """表示没有启用模块提供请求的 capability。"""


class ModuleDependencyError(KernelError):
    """表示模块依赖缺失、禁用或存在环。"""


class ModuleActivationError(KernelError):
    """表示模块 factory 或显式激活阶段失败。"""


class ResourceScopeClosedError(KernelError):
    """表示关闭后仍尝试向 ResourceScope 注册资源。"""


class ResourceCleanupError(KernelError):
    """表示资源回收已尽力完成但至少一项失败。"""


class ResourceLeakError(KernelError):
    """表示生命周期结束后仍有资源未归零。"""


class ModuleShutdownError(KernelError):
    """表示模块关闭已尽力完成但至少一项失败。"""
