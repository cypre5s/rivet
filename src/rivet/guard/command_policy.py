"""在进入沙箱前拒绝删除、历史破坏、提权和网络命令。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from rivet.tools.errors import ProcessToolError

SAFE_COMMAND_ENVIRONMENT = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "PATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONHASHSEED",
        "TERM",
        "TMPDIR",
        "TZ",
    }
)
DENIED_PROGRAMS = frozenset(
    {
        "chmod",
        "chown",
        "curl",
        "dd",
        "doas",
        "ftp",
        "mount",
        "nc",
        "ncat",
        "netcat",
        "pkexec",
        "rm",
        "rmdir",
        "scp",
        "shred",
        "ssh",
        "sudo",
        "telnet",
        "umount",
        "wget",
    }
)
SHELL_PROGRAMS = frozenset({"ash", "bash", "dash", "fish", "ksh", "sh", "zsh"})
DANGEROUS_GIT_SUBCOMMANDS = frozenset(
    {
        "checkout",
        "clean",
        "commit",
        "gc",
        "merge",
        "pull",
        "push",
        "rebase",
        "reset",
        "restore",
        "switch",
        "worktree",
    }
)


class CommandPolicy:
    """校验模型控制的 argv 与显式环境覆盖。"""

    def validate(self, argv: tuple[str, ...]) -> None:
        """拒绝可绕开事务、网络和历史边界的程序。"""
        if not argv or not argv[0]:
            raise ProcessToolError("guard.command_denied", "命令被安全策略拒绝")
        program = Path(argv[0]).name.lower()
        denied = program in DENIED_PROGRAMS
        denied = denied or (program in SHELL_PROGRAMS and "-c" in argv[1:])
        denied = denied or (
            program == "git"
            and any(
                argument.lower() in DANGEROUS_GIT_SUBCOMMANDS for argument in argv[1:]
            )
        )
        if denied:
            raise ProcessToolError("guard.command_denied", "命令被安全策略拒绝")

    def validate_environment(self, environment: Mapping[str, str] | None) -> None:
        """环境覆盖只允许与执行确定性相关的非秘密字段。"""
        if environment is None:
            return
        if set(environment) - SAFE_COMMAND_ENVIRONMENT:
            raise ProcessToolError("guard.environment_denied", "命令环境被安全策略拒绝")
