"""以固定 Git argv 实现仓库指纹、快照、Worktree 和补丁原语。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import stat
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Literal

from rivet.kernel.resources import ResourceScope
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.process import ProcessRunner

from .errors import TransactionError
from .hashing import canonical_json_bytes, sha256_digest
from .models import RepositorySnapshot

MAX_GIT_OUTPUT_BYTES = 64 * 1024 * 1024
GIT_ENVIRONMENT_NAMES = frozenset(
    {
        "GIT_AUTHOR_DATE",
        "GIT_AUTHOR_EMAIL",
        "GIT_AUTHOR_NAME",
        "GIT_COMMITTER_DATE",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_INDEX_FILE",
        "GIT_TERMINAL_PROMPT",
        "LANG",
        "LC_ALL",
        "PATH",
    }
)
CONFIG_KEYS = (
    "core.repositoryformatversion",
    "core.filemode",
    "core.bare",
    "core.logallrefupdates",
    "core.ignorecase",
    "core.symlinks",
    "extensions.worktreeconfig",
)


def _safe_environment() -> dict[str, str]:
    """只向 Git 传递工具查找、UTF-8 和禁网提示相关变量。"""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _decode_path(raw_path: bytes) -> str:
    """将 Git 路径严格收窄到公共契约支持的 UTF-8。"""
    try:
        path = raw_path.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise TransactionError(
            "transaction.path_encoding_unsupported",
            "Git 路径不是受支持的 UTF-8",
        ) from error
    if not path or path.startswith("/") or "\\" in path:
        raise TransactionError("transaction.path_invalid", "Git 返回了无效路径")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise TransactionError("transaction.path_invalid", "Git 路径包含跳转段")
    return path


def parse_nul_paths(raw: bytes) -> tuple[str, ...]:
    """解析 Git `-z` 路径列表并返回稳定去重顺序。"""
    paths = {_decode_path(item) for item in raw.split(b"\0") if item}
    return tuple(sorted(paths))


def parse_porcelain_paths(raw: bytes) -> tuple[str, ...]:
    """解析 porcelain v1 `-z`，同时保留 rename/copy 的两端。"""
    records = raw.split(b"\0")
    paths: set[str] = set()
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise TransactionError(
                "transaction.git_status_invalid",
                "Git porcelain 状态格式无效",
            )
        status = record[:2]
        paths.add(_decode_path(record[3:]))
        if b"R" in status or b"C" in status:
            if index >= len(records) or not records[index]:
                raise TransactionError(
                    "transaction.git_status_invalid",
                    "Git rename 状态缺少来源路径",
                )
            paths.add(_decode_path(records[index]))
            index += 1
    return tuple(sorted(paths))


def _untracked_content_sha256(root: Path, paths: tuple[str, ...]) -> str:
    """哈希未跟踪普通文件、链接和特殊节点，且不跟随链接。"""
    facts: list[dict[str, object]] = []
    for relative_path in paths:
        path = root / relative_path
        try:
            metadata = path.lstat()
        except OSError as error:
            raise TransactionError(
                "transaction.untracked_unreadable",
                "未跟踪路径在指纹计算期间不可读",
            ) from error
        fact: dict[str, object] = {
            "path": relative_path,
            "mode": stat.S_IFMT(metadata.st_mode),
            "permissions": metadata.st_mode & 0o777,
        }
        if stat.S_ISLNK(metadata.st_mode):
            fact["target"] = os.readlink(path)
        elif stat.S_ISREG(metadata.st_mode):
            resolved = path.resolve(strict=True)
            try:
                resolved.relative_to(root)
            except ValueError as error:
                raise TransactionError(
                    "transaction.untracked_escape",
                    "未跟踪文件解析后越过仓库根",
                ) from error
            digest = hashlib.sha256()
            try:
                with resolved.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
            except OSError as error:
                raise TransactionError(
                    "transaction.untracked_unreadable",
                    "未跟踪文件在指纹计算期间不可读",
                ) from error
            fact["content_sha256"] = f"sha256:{digest.hexdigest()}"
        facts.append(fact)
    return sha256_digest(canonical_json_bytes(facts))


class GitBackend:
    """在已发现仓库中执行不经过 shell 的固定 Git 原语。"""

    def __init__(self, repository_root: Path, *, scope: ResourceScope) -> None:
        self.repository_root = repository_root.resolve(strict=True)
        self._scope = scope
        self._main_boundary = WorkspaceBoundary(self.repository_root)
        self._main_runner = self._make_runner(
            self._main_boundary, "repository_read_only"
        )

    @classmethod
    async def discover(
        cls,
        candidate: Path,
        *,
        scope: ResourceScope,
    ) -> GitBackend:
        """从任意仓库子目录发现顶层并拒绝 bare 或非工作树。"""
        resolved_candidate = candidate.resolve(strict=True)
        if not resolved_candidate.is_dir():
            raise TransactionError(
                "transaction.repository_not_directory",
                "事务目标不是目录",
            )
        boundary = WorkspaceBoundary(resolved_candidate)
        runner = ProcessRunner(
            boundary,
            scope=scope,
            max_capture_bytes=1024 * 1024,
            environment=_safe_environment(),
            environment_allowlist=GIT_ENVIRONMENT_NAMES,
            root_kind="repository_read_only",
        )
        root_result = await runner.run(
            ("git", "--no-pager", "rev-parse", "--show-toplevel"),
            cwd=".",
            timeout_seconds=15.0,
        )
        if root_result.returncode != 0 or root_result.timed_out:
            raise TransactionError(
                "transaction.repository_missing",
                "目标不属于 Git 仓库",
            )
        try:
            repository_root = Path(
                root_result.stdout.decode("utf-8", errors="strict").strip()
            ).resolve(strict=True)
            resolved_candidate.relative_to(repository_root)
        except (UnicodeDecodeError, OSError, ValueError) as error:
            raise TransactionError(
                "transaction.repository_root_invalid",
                "Git 返回了无效仓库根",
            ) from error
        backend = cls(repository_root, scope=scope)
        inside = (
            await backend.run_main(("rev-parse", "--is-inside-work-tree"))
        ).strip()
        bare = (await backend.run_main(("rev-parse", "--is-bare-repository"))).strip()
        if inside != b"true" or bare != b"false":
            raise TransactionError(
                "transaction.bare_unsupported",
                "事务只支持非 bare Git 工作树",
            )
        return backend

    async def run_main(
        self,
        arguments: tuple[str, ...],
        *,
        check: bool = True,
        timeout_seconds: float = 30.0,
        environment: Mapping[str, str] | None = None,
    ) -> bytes:
        """在主仓库运行固定 Git 子命令并返回未解码输出。"""
        return await self._run(
            self._main_runner,
            arguments,
            check=check,
            timeout_seconds=timeout_seconds,
            environment=environment,
        )

    async def run_worktree(
        self,
        worktree: Path,
        arguments: tuple[str, ...],
        *,
        check: bool = True,
        timeout_seconds: float = 30.0,
    ) -> bytes:
        """在已授权事务 Worktree 运行固定 Git 子命令。"""
        boundary = WorkspaceBoundary(self.repository_root, worktree)
        runner = self._make_runner(boundary, "transaction")
        return await self._run(
            runner,
            arguments,
            check=check,
            timeout_seconds=timeout_seconds,
        )

    async def inspect(self) -> RepositorySnapshot:
        """采集 HEAD、dirty、submodule、配置和内容敏感指纹。"""
        head = (await self.run_main(("rev-parse", "HEAD"))).decode().strip()
        branch_output = await self.run_main(
            ("symbolic-ref", "--quiet", "--short", "HEAD"), check=False
        )
        branch = branch_output.decode("utf-8", errors="strict").strip() or None
        status = await self.run_main(
            ("status", "--porcelain=v1", "-z", "--untracked-files=all")
        )
        unstaged_diff = await self.run_main(("diff", "--binary", "--no-ext-diff", "--"))
        staged_diff = await self.run_main(
            ("diff", "--cached", "--binary", "--no-ext-diff", "--")
        )
        untracked = parse_nul_paths(
            await self.run_main(("ls-files", "--others", "--exclude-standard", "-z"))
        )
        staged_entries = await self.run_main(("ls-files", "--stage", "-z"))
        has_submodules = any(
            entry.startswith(b"160000 ")
            for entry in staged_entries.split(b"\0")
            if entry
        )
        submodule_status = (
            await self.run_main(("submodule", "status", "--recursive"))
            if has_submodules
            else b""
        )
        config_summary: list[str] = []
        for key in CONFIG_KEYS:
            value = (
                (await self.run_main(("config", "--local", "--get", key), check=False))
                .decode("utf-8", errors="strict")
                .strip()
            )
            config_summary.append(f"{key}={value or '<unset>'}")
        common_dir_text = (
            (await self.run_main(("rev-parse", "--git-common-dir")))
            .decode("utf-8", errors="strict")
            .strip()
        )
        common_dir = Path(common_dir_text)
        if not common_dir.is_absolute():
            common_dir = self.repository_root / common_dir
        identity = sha256_digest(
            canonical_json_bytes(
                {
                    "repository_root": str(self.repository_root),
                    "git_common_dir": str(common_dir.resolve(strict=True)),
                }
            )
        )
        submodule_digest = sha256_digest(submodule_status)
        fingerprint = sha256_digest(
            canonical_json_bytes(
                {
                    "head_commit": head,
                    "branch": branch,
                    "status_sha256": sha256_digest(status),
                    "unstaged_diff_sha256": sha256_digest(unstaged_diff),
                    "staged_diff_sha256": sha256_digest(staged_diff),
                    "untracked_sha256": _untracked_content_sha256(
                        self.repository_root, untracked
                    ),
                    "submodule_status_sha256": submodule_digest,
                    "git_config_summary": config_summary,
                }
            )
        )
        return RepositorySnapshot(
            repository_root=self.repository_root,
            repository_identity=identity,
            repository_fingerprint=fingerprint,
            head_commit=head,
            branch=branch,
            detached_head=branch is None,
            dirty=bool(status),
            status_paths=parse_porcelain_paths(status),
            untracked_paths=untracked,
            has_submodules=has_submodules,
            submodule_status_sha256=submodule_digest,
            git_config_summary=tuple(config_summary),
        )

    async def create_dirty_snapshot(
        self,
        snapshot: RepositorySnapshot,
        *,
        transaction_id: str,
        temporary_index: Path,
        timestamp: datetime,
    ) -> str:
        """用 stash tree 和临时 index 保存 tracked、staged 与 untracked。"""
        stash_output = await self.run_main(
            (
                "-c",
                "user.name=Rivet",
                "-c",
                "user.email=rivet@localhost",
                "stash",
                "create",
                f"Rivet dirty snapshot {transaction_id}",
            )
        )
        stash_commit = stash_output.decode("ascii", errors="strict").strip()
        if stash_commit and (len(stash_commit) != 40 or not stash_commit.isalnum()):
            raise TransactionError(
                "transaction.dirty_snapshot_invalid",
                "Git stash create 返回了无效快照",
            )
        if not snapshot.untracked_paths:
            if not stash_commit:
                raise TransactionError(
                    "transaction.dirty_snapshot_unsupported",
                    "脏状态无法安全生成 tracked 快照",
                )
            return stash_commit
        temporary_index.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary_index.unlink(missing_ok=True)
        environment = {"GIT_INDEX_FILE": str(temporary_index)}
        source_commit = stash_commit or snapshot.head_commit
        try:
            await self.run_main(("read-tree", source_commit), environment=environment)
            for start in range(0, len(snapshot.untracked_paths), 500):
                chunk = snapshot.untracked_paths[start : start + 500]
                await self.run_main(
                    ("add", "--all", "--", *chunk),
                    environment=environment,
                )
            tree = (
                (await self.run_main(("write-tree",), environment=environment))
                .decode("ascii", errors="strict")
                .strip()
            )
            git_date = timestamp.strftime("%Y-%m-%dT%H:%M:%S %z")
            commit_environment = {
                **environment,
                "GIT_AUTHOR_NAME": "Rivet",
                "GIT_AUTHOR_EMAIL": "rivet@localhost",
                "GIT_AUTHOR_DATE": git_date,
                "GIT_COMMITTER_NAME": "Rivet",
                "GIT_COMMITTER_EMAIL": "rivet@localhost",
                "GIT_COMMITTER_DATE": git_date,
            }
            commit = await self.run_main(
                (
                    "commit-tree",
                    tree,
                    "-p",
                    snapshot.head_commit,
                    "-m",
                    f"Rivet dirty snapshot {transaction_id}",
                ),
                environment=commit_environment,
            )
        finally:
            temporary_index.unlink(missing_ok=True)
            with suppress(OSError):
                temporary_index.parent.rmdir()
        return commit.decode("ascii", errors="strict").strip()

    async def add_worktree(self, path: Path, base_commit: str) -> None:
        """从冻结基线创建 detached Worktree。"""
        await self.run_main(
            ("worktree", "add", "--detach", str(path), base_commit),
            timeout_seconds=60.0,
        )

    async def remove_worktree(self, path: Path) -> None:
        """显式强制清理已应用或已 abort 的事务 Worktree。"""
        if path.exists():
            await self.run_main(
                ("worktree", "remove", "--force", str(path)),
                timeout_seconds=60.0,
            )
        await self.run_main(("worktree", "prune", "--expire", "now"))

    async def cleanup_worktree_unscoped(self, path: Path) -> None:
        """在 ResourceScope 关闭期间以有界进程清理 Worktree。"""

        async def run_cleanup(arguments: tuple[str, ...]) -> int:
            """运行一个只属于 Worktree 清理回调的有界 Git 进程。"""
            process = await asyncio.create_subprocess_exec(
                "git",
                "--no-pager",
                *arguments,
                cwd=self.repository_root,
                env=_safe_environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=30.0)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=1.0)
                except TimeoutError:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            return process.returncode or 0

        returncode = await run_cleanup(("worktree", "remove", "--force", str(path)))
        if not path.exists():
            await run_cleanup(("worktree", "prune", "--expire", "now"))
        if returncode != 0 and path.exists():
            raise TransactionError(
                "transaction.worktree_cleanup_failed",
                "退出时无法清理事务 Worktree",
            )

    async def changed_paths(self, worktree: Path) -> tuple[str, ...]:
        """返回事务 Worktree 中 tracked、deleted 和 untracked 路径。"""
        status = await self.run_worktree(
            worktree,
            ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        )
        return parse_porcelain_paths(status)

    async def added_paths(
        self,
        worktree: Path,
        base_commit: str,
    ) -> tuple[str, ...]:
        """返回相对冻结基线新增的文件，包含 intent-to-add 文件。"""
        output = await self.run_worktree(
            worktree,
            (
                "diff",
                "--name-only",
                "--diff-filter=A",
                "-z",
                base_commit,
                "--",
            ),
        )
        return parse_nul_paths(output)

    async def file_at_revision(
        self,
        worktree: Path,
        revision: str,
        path: str,
    ) -> bytes:
        """读取冻结 revision 的单文件内容；不存在时返回空字节。"""
        _decode_path(path.encode("utf-8", errors="strict"))
        return await self.run_worktree(
            worktree,
            ("show", f"{revision}:{path}"),
            check=False,
        )

    async def binary_diff(self, worktree: Path, base_commit: str) -> bytes:
        """将 untracked 标记为 intent-to-add 后生成完整 binary diff。"""
        untracked = parse_nul_paths(
            await self.run_worktree(
                worktree,
                ("ls-files", "--others", "--exclude-standard", "-z"),
            )
        )
        for start in range(0, len(untracked), 500):
            chunk = untracked[start : start + 500]
            await self.run_worktree(
                worktree,
                ("add", "--intent-to-add", "--", *chunk),
            )
        return await self.run_worktree(
            worktree,
            (
                "diff",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                base_commit,
                "--",
            ),
            timeout_seconds=60.0,
        )

    async def apply_patch(
        self,
        patch_path: Path,
        *,
        check_only: bool,
        reverse: bool = False,
    ) -> None:
        """先检查或原子应用已持久化 binary patch。"""
        arguments = ["apply"]
        if check_only:
            arguments.append("--check")
        if reverse:
            arguments.append("--reverse")
        arguments.extend(("--binary", "--whitespace=nowarn", str(patch_path)))
        await self.run_main(tuple(arguments), timeout_seconds=60.0)

    async def apply_patch_to_worktree(
        self,
        worktree: Path,
        patch_path: Path,
        *,
        check_only: bool,
    ) -> None:
        """在验证副本中检查或应用持久化 binary patch。"""
        arguments = ["apply"]
        if check_only:
            arguments.append("--check")
        arguments.extend(("--binary", "--whitespace=nowarn", str(patch_path)))
        await self.run_worktree(
            worktree,
            tuple(arguments),
            timeout_seconds=60.0,
        )

    async def list_worktrees(self) -> tuple[Path, ...]:
        """解析 porcelain Worktree 清单中的规范绝对路径。"""
        output = await self.run_main(("worktree", "list", "--porcelain", "-z"))
        paths: list[Path] = []
        for field in output.split(b"\0"):
            if not field.startswith(b"worktree "):
                continue
            try:
                path = Path(field.removeprefix(b"worktree ").decode("utf-8")).resolve()
            except UnicodeDecodeError as error:
                raise TransactionError(
                    "transaction.worktree_path_invalid",
                    "Git Worktree 路径不是 UTF-8",
                ) from error
            paths.append(path)
        return tuple(sorted(paths))

    async def worktree_head(self, worktree: Path) -> str:
        """返回恢复检查使用的 Worktree HEAD。"""
        return (
            (await self.run_worktree(worktree, ("rev-parse", "HEAD")))
            .decode("ascii", errors="strict")
            .strip()
        )

    def _make_runner(
        self,
        boundary: WorkspaceBoundary,
        root_kind: Literal["transaction", "repository_read_only"],
    ) -> ProcessRunner:
        """构造使用同一 ResourceScope 的安全 Git runner。"""
        return ProcessRunner(
            boundary,
            scope=self._scope,
            max_capture_bytes=MAX_GIT_OUTPUT_BYTES,
            environment=_safe_environment(),
            environment_allowlist=GIT_ENVIRONMENT_NAMES,
            root_kind=root_kind,
        )

    @staticmethod
    async def _run(
        runner: ProcessRunner,
        arguments: tuple[str, ...],
        *,
        check: bool,
        timeout_seconds: float,
        environment: Mapping[str, str] | None = None,
    ) -> bytes:
        """统一处理 Git 退出码、超时和输出上限。"""
        result = await runner.run(
            ("git", "--no-pager", *arguments),
            cwd=".",
            timeout_seconds=timeout_seconds,
            environment=environment,
        )
        if result.timed_out:
            raise TransactionError("transaction.git_timeout", "Git 命令执行超时")
        if result.stdout_truncated or result.stderr_truncated:
            raise TransactionError(
                "transaction.git_output_exceeded",
                "Git 命令输出超过事务上限",
            )
        if check and result.returncode != 0:
            raise TransactionError(
                "transaction.git_command_failed",
                "事务 Git 命令执行失败",
            )
        return result.stdout
