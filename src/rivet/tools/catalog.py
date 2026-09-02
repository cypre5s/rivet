"""定义不会导入执行器或创建资源的静态模型工具目录。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from rivet.contracts.guard import Permission, PermissionScope
from rivet.contracts.tools import SideEffectClass, ToolDefinition


class ToolArguments(BaseModel):
    """所有模型工具都拒绝未声明参数和隐式类型转换。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class WorkspaceInfoArguments(ToolArguments):
    """``workspace_info`` 不接受参数。"""


class ContextSearchArguments(ToolArguments):
    query: str = Field(min_length=1, max_length=4_096)
    max_results: int = Field(default=8, ge=1, le=32)


class FileReadArguments(ToolArguments):
    path: str = Field(min_length=1, max_length=4_096)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class FileWriteArguments(ToolArguments):
    path: str = Field(min_length=1, max_length=4_096)
    content: str = Field(max_length=4_000_000)


class FileReplaceArguments(ToolArguments):
    path: str = Field(min_length=1, max_length=4_096)
    old_text: str = Field(min_length=1, max_length=1_000_000)
    new_text: str = Field(max_length=1_000_000)
    expected_count: int = Field(default=1, ge=1, le=10_000)


class FileCreateArguments(ToolArguments):
    path: str = Field(min_length=1, max_length=4_096)
    content: str = Field(max_length=4_000_000)


class FileDeleteArguments(ToolArguments):
    path: str = Field(min_length=1, max_length=4_096)


class ProcessRunArguments(ToolArguments):
    argv: list[str] = Field(min_length=1, max_length=128)
    cwd: str = Field(default=".", min_length=1, max_length=4_096)
    timeout_seconds: float = Field(default=120.0, gt=0, le=900)
    environment: dict[str, str] | None = None


class GitDiffArguments(ToolArguments):
    path: str | None = Field(default=None, min_length=1, max_length=4_096)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """绑定静态协议、权限和真正执行时才请求的能力。"""

    name: str
    description: str
    input_model: type[ToolArguments]
    side_effect: SideEffectClass
    permission: Permission
    permission_scope: PermissionScope
    required_capabilities: tuple[str, ...]
    modes: frozenset[str]
    executor: str
    path_argument: str | None = None

    def __post_init__(self) -> None:
        if not self.executor or self.executor.strip() != self.executor:
            raise ValueError("ToolSpec executor key 不得为空或包含边界空白")
        if self.permission_scope is PermissionScope.SPECIFIC_PATHS:
            if self.path_argument is None:
                raise ValueError("具体路径权限必须声明 path_argument")
        elif self.path_argument is not None:
            raise ValueError("非路径权限不得声明 path_argument")

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=cast(
                dict[str, JsonValue], self.input_model.model_json_schema()
            ),
        )


_ASK_AND_FIX = frozenset({"ask", "fix"})
_FIX_ONLY = frozenset({"fix"})

TOOL_CATALOG: tuple[ToolSpec, ...] = (
    ToolSpec(
        "workspace_info",
        "读取当前仓库根、Git HEAD 和分支概览",
        WorkspaceInfoArguments,
        SideEffectClass.READ_ONLY,
        Permission.READ,
        PermissionScope.WORKSPACE,
        (),
        _ASK_AND_FIX,
        executor="workspace_info",
    ),
    ToolSpec(
        "context_search",
        "仅在回答依赖仓库事实时执行有界词法搜索",
        ContextSearchArguments,
        SideEffectClass.READ_ONLY,
        Permission.READ,
        PermissionScope.WORKSPACE,
        ("context.search.lexical",),
        _ASK_AND_FIX,
        executor="context_search",
    ),
    ToolSpec(
        "file_read",
        "读取授权范围内的有界 UTF-8 文本；FIX 仅可读取冻结 read_scope，二进制内容会被拒绝",
        FileReadArguments,
        SideEffectClass.READ_ONLY,
        Permission.READ,
        PermissionScope.SPECIFIC_PATHS,
        (),
        _ASK_AND_FIX,
        executor="file_read",
        path_argument="path",
    ),
    ToolSpec(
        "file_write",
        "原子覆盖隔离事务中的文本文件",
        FileWriteArguments,
        SideEffectClass.TRANSACTIONAL_WRITE,
        Permission.WRITE,
        PermissionScope.SPECIFIC_PATHS,
        ("transaction.worktree", "guard.local_execution"),
        _FIX_ONLY,
        executor="file_write",
        path_argument="path",
    ),
    ToolSpec(
        "file_replace",
        "按预期次数替换隔离事务中的文本",
        FileReplaceArguments,
        SideEffectClass.TRANSACTIONAL_WRITE,
        Permission.WRITE,
        PermissionScope.SPECIFIC_PATHS,
        ("transaction.worktree", "guard.local_execution"),
        _FIX_ONLY,
        executor="file_replace",
        path_argument="path",
    ),
    ToolSpec(
        "file_create",
        "在隔离事务中原子创建文本文件",
        FileCreateArguments,
        SideEffectClass.TRANSACTIONAL_WRITE,
        Permission.WRITE,
        PermissionScope.SPECIFIC_PATHS,
        ("transaction.worktree", "guard.local_execution"),
        _FIX_ONLY,
        executor="file_create",
        path_argument="path",
    ),
    ToolSpec(
        "file_delete",
        "删除隔离事务中的普通文件",
        FileDeleteArguments,
        SideEffectClass.TRANSACTIONAL_WRITE,
        Permission.WRITE,
        PermissionScope.SPECIFIC_PATHS,
        ("transaction.worktree", "guard.local_execution"),
        _FIX_ONLY,
        executor="file_delete",
        path_argument="path",
    ),
    ToolSpec(
        "process_run",
        "在隔离事务和 Bubblewrap 边界内以 argv 执行本地进程",
        ProcessRunArguments,
        SideEffectClass.LOCAL_PROCESS,
        Permission.EXECUTE,
        PermissionScope.TRANSACTION,
        ("transaction.worktree", "guard.local_execution"),
        _FIX_ONLY,
        executor="process_run",
    ),
    ToolSpec(
        "git_diff",
        "读取隔离事务相对基线的完整补丁",
        GitDiffArguments,
        SideEffectClass.READ_ONLY,
        Permission.READ,
        PermissionScope.TRANSACTION,
        ("transaction.worktree",),
        _FIX_ONLY,
        executor="git_diff",
    ),
)


def catalog_for_mode(mode: str) -> tuple[ToolSpec, ...]:
    """按 ASK/FIX 返回稳定子集，未知模式失败关闭。"""
    if mode not in {"ask", "fix"}:
        raise ValueError("工具目录只支持 ask 或 fix")
    return tuple(spec for spec in TOOL_CATALOG if mode in spec.modes)


def tool_spec(name: str) -> ToolSpec:
    """返回唯一目录项；未知名称不触发任何实现导入。"""
    for spec in TOOL_CATALOG:
        if spec.name == name:
            return spec
    raise KeyError(name)
