"""定义不会回显命令、环境或文件内容的本地工具错误。"""

from __future__ import annotations


class WorkspaceToolError(RuntimeError):
    """保存工具层稳定错误码、摘要和重试属性。"""

    def __init__(self, code: str, summary: str, *, retryable: bool = False) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary
        self.retryable = retryable

    def __repr__(self) -> str:
        """避免 repr 展开命令参数、路径内容或环境。"""
        return (
            f"{type(self).__name__}(code={self.code!r}, retryable={self.retryable!r})"
        )


class PathBoundaryError(WorkspaceToolError):
    """表示路径越过授权根或命中保护区域。"""


class FileToolError(WorkspaceToolError):
    """表示文件类型、编码、大小或原子操作失败。"""


class ProcessToolError(WorkspaceToolError):
    """表示 argv、cwd 或本地进程启动失败。"""


class SearchToolError(WorkspaceToolError):
    """表示 ripgrep 不可用或输出协议无效。"""


class GitToolError(WorkspaceToolError):
    """表示仓库类型不支持或固定 Git 命令失败。"""
