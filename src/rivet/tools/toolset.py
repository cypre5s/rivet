"""把 Phase 5 的正式工具名绑定到安全工作区原语。"""

from __future__ import annotations

import json
from dataclasses import asdict

from pydantic import BaseModel, ConfigDict, Field

from rivet.contracts.guard import Permission, PermissionScope
from rivet.guard.sandbox import BubblewrapSandbox
from rivet.kernel.resources import ResourceScope
from rivet.tools.files import FileReader, TransactionFileWriter
from rivet.tools.git import GitService
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.process import ProcessExecutor, ProcessRunner
from rivet.tools.registry import (
    RawToolOutput,
    RegisteredTool,
    ToolAuthorizer,
    ToolRegistry,
)
from rivet.tools.search import SearchService
from rivet.tools.workspace import WorkspaceInspector

WORKSPACE_TOOL_NAMES = (
    "workspace.info",
    "workspace.list",
    "file.read_text",
    "file.read_range",
    "file.write_transaction",
    "file.replace_transaction",
    "file.create_transaction",
    "file.delete_transaction",
    "search.text",
    "search.files",
    "git.status",
    "git.diff",
    "git.show",
    "process.run",
)


class _Arguments(BaseModel):
    """统一拒绝模型幻觉出的额外工具参数。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class WorkspaceInfoArguments(_Arguments):
    """表示无参数的工作区概览。"""


class WorkspaceListArguments(_Arguments):
    """限制目录列表的根、深度与条目数。"""

    path: str = "."
    max_depth: int = Field(default=2, ge=0, le=20)
    max_entries: int = Field(default=1_000, ge=1, le=10_000)


class FileReadTextArguments(_Arguments):
    """指定待读取的仓库相对文件。"""

    path: str


class FileReadRangeArguments(_Arguments):
    """指定一基闭区间文本读取。"""

    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class FileWriteArguments(_Arguments):
    """指定事务内覆盖内容。"""

    path: str
    content: str


class FileReplaceArguments(_Arguments):
    """指定事务内带匹配计数的文本替换。"""

    path: str
    old_text: str
    new_text: str
    expected_count: int = Field(default=1, ge=1)


class FileCreateArguments(_Arguments):
    """指定事务内新文件内容。"""

    path: str
    content: str


class FileDeleteArguments(_Arguments):
    """指定事务内待删除文件。"""

    path: str


class SearchTextArguments(_Arguments):
    """指定字面量或正则文本检索。"""

    pattern: str
    paths: list[str] = Field(default_factory=lambda: ["."])
    regex: bool = False
    max_results: int = Field(default=200, ge=1, le=10_000)


class SearchFilesArguments(_Arguments):
    """指定可选 glob 文件检索。"""

    glob: str | None = None
    max_results: int = Field(default=1_000, ge=1, le=100_000)


class GitStatusArguments(_Arguments):
    """表示无参数 Git status。"""


class GitDiffArguments(_Arguments):
    """指定工作树或暂存区 diff 和可选路径。"""

    path: str | None = None
    cached: bool = False


class GitShowArguments(_Arguments):
    """指定 revision 与可选文件。"""

    revision: str = "HEAD"
    path: str | None = None


class ProcessRunArguments(_Arguments):
    """指定无 shell argv、cwd、超时和白名单环境覆盖。"""

    argv: list[str] = Field(min_length=1, max_length=1_024)
    cwd: str = "."
    timeout_seconds: float = Field(gt=0, le=3_600)
    environment: dict[str, str] | None = None


def _json_output(value: object) -> RawToolOutput:
    """把结构化结果稳定序列化为 UTF-8。"""
    return RawToolOutput(
        stdout=json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def build_workspace_tool_registry(
    boundary: WorkspaceBoundary,
    *,
    scope: ResourceScope,
    model_preview_chars: int = 8_192,
    tui_preview_chars: int = 65_536,
    authorizer: ToolAuthorizer | None = None,
    process_executor: ProcessExecutor | None = None,
    read_only: bool = False,
) -> ToolRegistry:
    """构造并按正式 CLI 语义顺序注册全部本地工具。"""
    reader = FileReader(boundary)
    writer = TransactionFileWriter(boundary)
    inspector = WorkspaceInspector(boundary)
    process_runner = process_executor or BubblewrapSandbox(boundary, scope=scope)
    read_only_runner = ProcessRunner(
        boundary,
        scope=scope,
        root_kind="repository_read_only",
    )
    search = SearchService(boundary, runner=read_only_runner)
    git_service = GitService(boundary, runner=read_only_runner)
    registry = ToolRegistry(
        model_preview_chars=model_preview_chars,
        tui_preview_chars=tui_preview_chars,
        authorizer=authorizer,
    )

    async def workspace_info(arguments: BaseModel) -> RawToolOutput:
        WorkspaceInfoArguments.model_validate(arguments.model_dump())
        return _json_output(asdict(await inspector.info()))

    async def workspace_list(arguments: BaseModel) -> RawToolOutput:
        values = WorkspaceListArguments.model_validate(arguments.model_dump())
        return _json_output(asdict(inspector.list(**values.model_dump())))

    async def file_read_text(arguments: BaseModel) -> RawToolOutput:
        values = FileReadTextArguments.model_validate(arguments.model_dump())
        return _json_output(asdict(reader.read_text(values.path)))

    async def file_read_range(arguments: BaseModel) -> RawToolOutput:
        values = FileReadRangeArguments.model_validate(arguments.model_dump())
        return _json_output(
            asdict(
                reader.read_range(
                    values.path,
                    start_line=values.start_line,
                    end_line=values.end_line,
                )
            )
        )

    async def file_write(arguments: BaseModel) -> RawToolOutput:
        values = FileWriteArguments.model_validate(arguments.model_dump())
        writer.write(values.path, values.content)
        return _json_output({"path": values.path, "written": True})

    async def file_replace(arguments: BaseModel) -> RawToolOutput:
        values = FileReplaceArguments.model_validate(arguments.model_dump())
        count = writer.replace(
            values.path,
            values.old_text,
            values.new_text,
            expected_count=values.expected_count,
        )
        return _json_output({"path": values.path, "replacement_count": count})

    async def file_create(arguments: BaseModel) -> RawToolOutput:
        values = FileCreateArguments.model_validate(arguments.model_dump())
        writer.create(values.path, values.content)
        return _json_output({"path": values.path, "created": True})

    async def file_delete(arguments: BaseModel) -> RawToolOutput:
        values = FileDeleteArguments.model_validate(arguments.model_dump())
        writer.delete(values.path)
        return _json_output({"path": values.path, "deleted": True})

    async def search_text(arguments: BaseModel) -> RawToolOutput:
        values = SearchTextArguments.model_validate(arguments.model_dump())
        result = await search.text(
            values.pattern,
            paths=tuple(values.paths),
            regex=values.regex,
            max_results=values.max_results,
        )
        return _json_output(asdict(result))

    async def search_files(arguments: BaseModel) -> RawToolOutput:
        values = SearchFilesArguments.model_validate(arguments.model_dump())
        return _json_output(
            asdict(await search.files(values.glob, max_results=values.max_results))
        )

    async def git_status(arguments: BaseModel) -> RawToolOutput:
        GitStatusArguments.model_validate(arguments.model_dump())
        return RawToolOutput(stdout=(await git_service.status()).encode())

    async def git_diff(arguments: BaseModel) -> RawToolOutput:
        values = GitDiffArguments.model_validate(arguments.model_dump())
        return RawToolOutput(
            stdout=(
                await git_service.diff(path=values.path, cached=values.cached)
            ).encode()
        )

    async def git_show(arguments: BaseModel) -> RawToolOutput:
        values = GitShowArguments.model_validate(arguments.model_dump())
        return RawToolOutput(
            stdout=(await git_service.show(values.revision, path=values.path)).encode()
        )

    async def process_run(arguments: BaseModel) -> RawToolOutput:
        values = ProcessRunArguments.model_validate(arguments.model_dump())
        result = await process_runner.run(
            tuple(values.argv),
            cwd=values.cwd,
            timeout_seconds=values.timeout_seconds,
            environment=values.environment,
        )
        return RawToolOutput(
            stdout=result.stdout,
            stderr=result.stderr,
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            stdout_total_bytes=result.stdout_total_bytes,
            stderr_total_bytes=result.stderr_total_bytes,
            stdout_sha256=result.stdout_sha256,
            stderr_sha256=result.stderr_sha256,
        )

    registrations = (
        (
            "workspace.info",
            "读取工作区和 Git HEAD 概览",
            WorkspaceInfoArguments,
            workspace_info,
            Permission.READ,
            PermissionScope.WORKSPACE,
            None,
        ),
        (
            "workspace.list",
            "受限列出工作区目录",
            WorkspaceListArguments,
            workspace_list,
            Permission.READ,
            PermissionScope.WORKSPACE,
            None,
        ),
        (
            "file.read_text",
            "读取有大小限制的文本文件",
            FileReadTextArguments,
            file_read_text,
            Permission.READ,
            PermissionScope.WORKSPACE,
            None,
        ),
        (
            "file.read_range",
            "读取文本文件的一基行范围",
            FileReadRangeArguments,
            file_read_range,
            Permission.READ,
            PermissionScope.WORKSPACE,
            None,
        ),
        (
            "file.write_transaction",
            "原子覆盖事务文件",
            FileWriteArguments,
            file_write,
            Permission.WRITE,
            PermissionScope.SPECIFIC_PATHS,
            "path",
        ),
        (
            "file.replace_transaction",
            "按预期次数替换事务文本",
            FileReplaceArguments,
            file_replace,
            Permission.WRITE,
            PermissionScope.SPECIFIC_PATHS,
            "path",
        ),
        (
            "file.create_transaction",
            "原子创建事务文件",
            FileCreateArguments,
            file_create,
            Permission.WRITE,
            PermissionScope.SPECIFIC_PATHS,
            "path",
        ),
        (
            "file.delete_transaction",
            "删除事务普通文件",
            FileDeleteArguments,
            file_delete,
            Permission.WRITE,
            PermissionScope.SPECIFIC_PATHS,
            "path",
        ),
        (
            "search.text",
            "使用 ripgrep 搜索文本",
            SearchTextArguments,
            search_text,
            Permission.READ,
            PermissionScope.WORKSPACE,
            None,
        ),
        (
            "search.files",
            "使用 ripgrep 搜索文件名",
            SearchFilesArguments,
            search_files,
            Permission.READ,
            PermissionScope.WORKSPACE,
            None,
        ),
        (
            "git.status",
            "读取 Git 工作树状态",
            GitStatusArguments,
            git_status,
            Permission.READ,
            PermissionScope.WORKSPACE,
            None,
        ),
        (
            "git.diff",
            "读取 Git 补丁",
            GitDiffArguments,
            git_diff,
            Permission.READ,
            PermissionScope.WORKSPACE,
            None,
        ),
        (
            "git.show",
            "读取 Git revision",
            GitShowArguments,
            git_show,
            Permission.READ,
            PermissionScope.WORKSPACE,
            None,
        ),
        (
            "process.run",
            "以 argv 执行受限本地进程",
            ProcessRunArguments,
            process_run,
            Permission.EXECUTE,
            PermissionScope.TRANSACTION,
            None,
        ),
    )
    for (
        name,
        description,
        input_model,
        handler,
        permission,
        permission_scope,
        path_argument,
    ) in registrations:
        if read_only and permission is not Permission.READ:
            continue
        registry.register(
            RegisteredTool.from_model(
                name=name,
                capability_id=name,
                description=description,
                input_model=input_model,
                handler=handler,
                permission=permission,
                permission_scope=permission_scope,
                path_argument=path_argument,
            )
        )
    return registry
