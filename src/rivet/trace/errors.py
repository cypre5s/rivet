"""定义 Trace 路径、协议、持久化与关闭失败类型。"""

from __future__ import annotations


class TraceError(RuntimeError):
    """作为 Trace 子系统可预期错误的公共基类。"""


class RuntimePathError(TraceError):
    """表示运行目录或 XDG 配置不安全。"""


class TraceWriteError(TraceError):
    """表示事件无法安全进入 append-only Trace。"""


class TraceEventTooLargeError(TraceWriteError):
    """表示事件应改用受限 artifact 保存大输出。"""


class CorruptTraceError(TraceError):
    """表示完整事件损坏，或序列与父链不连续。"""


class TraceShutdownError(TraceError):
    """表示 Writer 未能在有界时间内关闭。"""
