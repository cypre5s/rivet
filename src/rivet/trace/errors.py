"""定义 Trace 路径、协议、持久化与关闭失败类型。"""

from __future__ import annotations


class TraceError(RuntimeError):
    """作为 Trace 子系统可预期错误的公共基类。"""


class RuntimePathError(TraceError):
    """表示运行目录或 XDG 配置不安全。"""


class TraceDatabaseError(TraceError):
    """表示 SQLite 配置、迁移或索引操作失败。"""


class TraceWriteError(TraceError):
    """表示事件无法安全进入 append-only Trace。"""


class TraceEventTooLargeError(TraceWriteError):
    """表示事件应改用受限 artifact 保存大输出。"""


class TraceReplayError(TraceError):
    """表示 Trace 无法按确定性规则回放。"""


class UnknownTraceVersionError(TraceReplayError):
    """表示事件协议版本未知且策略要求失败关闭。"""


class CorruptTraceError(TraceReplayError):
    """表示非尾部事件损坏或序列不连续。"""


class TraceShutdownError(TraceError):
    """表示 Writer 未能在有界时间内关闭。"""
