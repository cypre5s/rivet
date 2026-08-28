"""在 Git 本地 exclude 中隔离 Rivet 运行产物并保留项目配置。"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

RIVET_EXCLUDE_BLOCK = """# Rivet local runtime
.rivet/*
!.rivet/project.toml
"""
MAX_EXCLUDE_BYTES = 1024 * 1024


def configure_runtime_excludes(repository: Path) -> bool:
    """幂等更新仓库私有 exclude；非 Git 目录返回 False。"""
    root = repository.resolve(strict=True)
    git_common = _git_path(root, ("rev-parse", "--git-common-dir"))
    exclude_path = _git_path(root, ("rev-parse", "--git-path", "info/exclude"))
    if git_common is None or exclude_path is None:
        return False
    common_root = (
        git_common if git_common.is_absolute() else (root / git_common)
    ).resolve(strict=True)
    unresolved_target = (
        exclude_path if exclude_path.is_absolute() else root / exclude_path
    ).absolute()
    if unresolved_target.is_symlink():
        raise ValueError("Git exclude 不得是符号链接")
    try:
        target = unresolved_target.parent.resolve(strict=True) / unresolved_target.name
    except OSError as error:
        raise ValueError("Git exclude 父目录无法验证") from error
    try:
        target.relative_to(common_root)
    except ValueError as error:
        raise ValueError("Git exclude 路径越过 common directory") from error
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if target.exists() and (
            not target.is_file() or target.stat().st_size > MAX_EXCLUDE_BYTES
        ):
            raise ValueError("Git exclude 不是有界普通文件")
        current = target.read_text(encoding="utf-8") if target.exists() else ""
    except (OSError, UnicodeError) as error:
        raise ValueError("Git exclude 无法读取") from error
    if _contains_block(current):
        return True
    separator = "" if not current or current.endswith("\n") else "\n"
    content = f"{current}{separator}{RIVET_EXCLUDE_BLOCK}"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=".rivet-exclude.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, target)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return True


def _contains_block(content: str) -> bool:
    """要求两个有效规则均存在，不依赖注释文本。"""
    rules = {
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    return {".rivet/*", "!.rivet/project.toml"}.issubset(rules)


def _git_path(repository: Path, arguments: tuple[str, ...]) -> Path | None:
    """用固定 Git argv 读取元数据路径且不回显 stderr。"""
    try:
        completed = subprocess.run(
            ("git", *arguments),
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            },
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value or "\x00" in value:
        return None
    return Path(value)
