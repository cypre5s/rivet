"""用 bubblewrap 建立只暴露事务、仓库和只读运行时的命令沙箱。"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from rivet.guard.command_policy import (
    SAFE_COMMAND_ENVIRONMENT,
    CommandPolicy,
)
from rivet.kernel.resources import ResourceScope
from rivet.tools.errors import ProcessToolError
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.process import ProcessRunner, ProcessRunResult

ViolationSink = Callable[["SandboxViolation"], None]
STANDARD_READ_ONLY_ROOTS = (Path("/usr"),)
STANDARD_SYMLINKS = (
    (Path("/bin"), "usr/bin"),
    (Path("/lib"), "usr/lib"),
    (Path("/lib64"), "usr/lib64"),
    (Path("/sbin"), "usr/sbin"),
)
STANDARD_ETC_PATHS = (
    Path("/etc/alternatives"),
    Path("/etc/ld.so.cache"),
    Path("/etc/ld.so.conf"),
    Path("/etc/ld.so.conf.d"),
    Path("/etc/localtime"),
    Path("/etc/nsswitch.conf"),
    Path("/etc/ssl/certs"),
)


class SandboxError(ProcessToolError):
    """表示沙箱不可用或边界构造失败，且绝不触发裸跑。"""


@dataclass(frozen=True, slots=True)
class SandboxViolation:
    """记录不含原始命令和环境内容的沙箱拒绝事实。"""

    code: str
    summary: str


class BubblewrapSandbox:
    """以新用户、PID、挂载和网络命名空间执行一个 argv。"""

    def __init__(
        self,
        boundary: WorkspaceBoundary,
        *,
        scope: ResourceScope,
        executable: Path | None = None,
        environment: Mapping[str, str] | None = None,
        runtime_read_only_paths: tuple[Path, ...] | None = None,
        max_capture_bytes: int = 5 * 1024 * 1024,
        termination_grace_seconds: float = 1.0,
        violation_sink: ViolationSink | None = None,
        command_policy: CommandPolicy | None = None,
    ) -> None:
        self._boundary = boundary
        self._scope = scope
        configured = os.environ.get("RIVET_BWRAP_PATH")
        discovered = shutil.which("bwrap")
        self._executable = Path(executable or configured or discovered or "bwrap")
        source_environment = environment if environment is not None else os.environ
        self._environment = {
            name: value
            for name, value in source_environment.items()
            if name in SAFE_COMMAND_ENVIRONMENT
        }
        self._environment.setdefault("PATH", "/usr/bin:/bin")
        self._environment.setdefault("LANG", "C.UTF-8")
        self._environment.setdefault("LC_ALL", "C.UTF-8")
        self._environment.setdefault("TZ", "UTC")
        self._runtime_paths = runtime_read_only_paths or _default_runtime_paths()
        self._max_capture_bytes = max_capture_bytes
        self._termination_grace_seconds = termination_grace_seconds
        self._violation_sink = violation_sink
        self._command_policy = command_policy or CommandPolicy()

    @property
    def available(self) -> bool:
        """只检查候选二进制是否为可执行普通文件。"""
        return self._executable.is_file() and os.access(self._executable, os.X_OK)

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str = ".",
        timeout_seconds: float,
        environment: Mapping[str, str] | None = None,
    ) -> ProcessRunResult:
        """执行沙箱命令；任一边界不可建立时失败关闭。"""
        try:
            self._command_policy.validate(argv)
            self._command_policy.validate_environment(environment)
        except ProcessToolError as error:
            self._record_violation(error.code, error.summary)
            raise SandboxError(error.code, error.summary) from error
        if not self.available:
            self._record_violation("sandbox.unavailable", "bubblewrap 沙箱不可用")
            raise SandboxError("sandbox.unavailable", "bubblewrap 沙箱不可用")
        working_directory = self._boundary.resolve_transaction(
            cwd,
            require_exists=True,
            require_directory=True,
        )
        relative_cwd = self._boundary.transaction_relative(working_directory)
        sandbox_environment = dict(self._environment)
        if environment is not None:
            sandbox_environment.update(environment)
        wrapped_argv = self.build_argv(
            argv,
            working_directory=working_directory,
            environment=sandbox_environment,
        )
        outer_runner = ProcessRunner(
            self._boundary,
            scope=self._scope,
            max_capture_bytes=self._max_capture_bytes,
            termination_grace_seconds=self._termination_grace_seconds,
            environment=self._environment,
            environment_allowlist=SAFE_COMMAND_ENVIRONMENT,
            root_kind="transaction",
        )
        try:
            result = await outer_runner.run(
                wrapped_argv,
                cwd=".",
                timeout_seconds=timeout_seconds,
            )
        except ProcessToolError as error:
            self._record_violation("sandbox.start_failed", "沙箱进程无法启动")
            raise SandboxError("sandbox.start_failed", "沙箱进程无法启动") from error
        if result.returncode != 0 and result.stderr.lstrip().startswith(b"bwrap:"):
            self._record_violation("sandbox.setup_failed", "沙箱边界建立失败")
            raise SandboxError("sandbox.setup_failed", "沙箱边界建立失败")
        return replace(result, argv=argv, cwd=relative_cwd)

    def build_argv(
        self,
        argv: tuple[str, ...],
        *,
        working_directory: Path,
        environment: Mapping[str, str],
    ) -> tuple[str, ...]:
        """构造默认断网、空 HOME、私有临时目录的确定性 bwrap argv。"""
        if self._boundary.transaction_root is None:
            raise SandboxError("sandbox.transaction_missing", "沙箱缺少事务根")
        arguments = [
            str(self._executable),
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--cap-drop",
            "ALL",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
        ]
        for root in STANDARD_READ_ONLY_ROOTS:
            if root.is_dir():
                arguments.extend(("--ro-bind", str(root), str(root)))
        for link, target in STANDARD_SYMLINKS:
            if link.is_symlink() and link.resolve(strict=False).exists():
                arguments.extend(("--symlink", target, str(link)))
            elif link.exists():
                arguments.extend(("--ro-bind", str(link), str(link)))
        arguments.extend(("--tmpfs", "/tmp", "--tmpfs", "/home"))
        created_directories = {Path("/"), Path("/tmp"), Path("/home"), Path("/usr")}
        _append_directory(arguments, Path("/home/rivet"), created_directories)
        for source in STANDARD_ETC_PATHS:
            if source.exists():
                _append_bind(
                    arguments,
                    source,
                    source,
                    read_only=True,
                    created_directories=created_directories,
                )
        repository_root = self._boundary.repository_root
        transaction_root = self._boundary.transaction_root
        candidates = (*self._runtime_paths, repository_root)
        bound_sources: list[Path] = []
        for source in candidates:
            resolved = source.resolve(strict=False)
            if not source.exists() or any(
                _contains(parent, resolved) for parent in bound_sources
            ):
                continue
            _append_bind(
                arguments,
                source,
                source,
                read_only=True,
                created_directories=created_directories,
            )
            bound_sources.append(resolved)
        _append_bind(
            arguments,
            transaction_root,
            transaction_root,
            read_only=False,
            created_directories=created_directories,
        )
        arguments.extend(("--clearenv", "--setenv", "HOME", "/home/rivet"))
        arguments.extend(("--setenv", "TMPDIR", "/tmp"))
        for name, value in sorted(environment.items()):
            if name in {"HOME", "TMPDIR"}:
                continue
            arguments.extend(("--setenv", name, value))
        arguments.extend(("--chdir", str(working_directory), "--", *argv))
        return tuple(arguments)

    def _record_violation(self, code: str, summary: str) -> None:
        """向可选审计接收器发送已脱敏拒绝事实。"""
        if self._violation_sink is not None:
            self._violation_sink(SandboxViolation(code=code, summary=summary))


def _default_runtime_paths() -> tuple[Path, ...]:
    """只暴露当前 Python 环境，不挂载其余用户 HOME。"""
    candidates = (Path(sys.prefix), Path(sys.base_prefix))
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if candidate.exists() and resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


def _contains(parent: Path, child: Path) -> bool:
    """判断一个已绑定目录是否覆盖另一个候选路径。"""
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _append_directory(
    arguments: list[str],
    directory: Path,
    created_directories: set[Path],
) -> None:
    """按父子顺序创建空命名空间中的挂载目标目录。"""
    missing = [
        parent
        for parent in reversed(directory.parents)
        if parent not in created_directories and parent != Path("/")
    ]
    for parent in (*missing, directory):
        if parent not in created_directories:
            arguments.extend(("--dir", str(parent)))
            created_directories.add(parent)


def _append_bind(
    arguments: list[str],
    source: Path,
    destination: Path,
    *,
    read_only: bool,
    created_directories: set[Path],
) -> None:
    """创建目标父目录并追加只读或读写 bind。"""
    for parent in reversed(destination.parents):
        if parent != Path("/") and parent not in created_directories:
            _append_directory(arguments, parent, created_directories)
    option = "--ro-bind" if read_only else "--bind"
    arguments.extend((option, str(source), str(destination)))
    if source.is_dir():
        created_directories.add(destination)
