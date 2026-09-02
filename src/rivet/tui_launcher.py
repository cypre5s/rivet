"""检测固定版本的 Bun 与 TUI 资源，并以前台子进程启动界面。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

SUPPORTED_BUN_MAJOR = 1
SUPPORTED_BUN_MINOR = 4
TUI_ENVIRONMENT_NAMES = frozenset(
    {
        "COLORTERM",
        "DEEPSEEK_API_KEY",
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "PATH",
        "RIVET_BASE_URL",
        "RIVET_BWRAP_PATH",
        "RIVET_MAX_COST_USD",
        "RIVET_MAX_ROUNDS",
        "RIVET_MAX_TOTAL_TOKENS",
        "RIVET_MODEL",
        "RIVET_MODELS",
        "TERM",
        "TZ",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    }
)


class TuiLaunchError(RuntimeError):
    """表示本地 TUI 依赖不可用，且错误文本不包含环境变量值。"""


def build_tui_environment(
    repository: Path,
    *,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """只向 Bun 传递终端、XDG 和明确允许的 Provider 环境变量。"""
    source_environment = os.environ if source is None else source
    environment = {
        name: value
        for name, value in source_environment.items()
        if name in TUI_ENVIRONMENT_NAMES
    }
    resolved_repository = repository.resolve(strict=True)
    environment["RIVET_REPOSITORY"] = str(resolved_repository)
    environment["RIVET_PYTHON"] = sys.executable
    environment["RIVET_WORKER_COMMAND_JSON"] = json.dumps(
        [
            sys.executable,
            "-m",
            "rivet",
            "internal",
            "worker",
            "--stdio",
            "--repository",
            str(resolved_repository),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return environment


def launch_tui(
    repository: Path,
    *,
    environment: Mapping[str, str] | None = None,
    tui_directory: Path | None = None,
) -> int:
    """校验运行时版本后启动 TUI，并原样返回前台进程退出码。"""
    bun = shutil.which("bun", path=(environment or os.environ).get("PATH"))
    if bun is None:
        raise TuiLaunchError("未找到 Bun 1.4.x；可继续使用 rivet --headless")
    root = (
        tui_directory.resolve(strict=True)
        if tui_directory is not None
        else Path(__file__).resolve().parents[2] / "tui"
    )
    entrypoint = root / "src" / "main.tsx"
    lockfile = root / "bun.lock"
    if not entrypoint.is_file() or not lockfile.is_file():
        raise TuiLaunchError(
            "未找到已安装的 Rivet TUI 资源；可继续使用 rivet --headless"
        )
    child_environment = build_tui_environment(repository, source=environment)
    try:
        version_result = subprocess.run(
            (bun, "--version"),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=child_environment,
        )
    except OSError as error:
        raise TuiLaunchError("无法启动 Bun；可继续使用 rivet --headless") from error
    version_text = version_result.stdout.strip()
    if version_result.returncode != 0 or not _supports_bun(version_text):
        raise TuiLaunchError("Rivet TUI 需要 Bun 1.4.x；可继续使用 rivet --headless")
    try:
        completed = subprocess.run(
            (bun, "run", "src/main.tsx"),
            cwd=root,
            env=child_environment,
            check=False,
        )
    except OSError as error:
        raise TuiLaunchError("Rivet TUI 启动失败") from error
    return completed.returncode


def _supports_bun(value: str) -> bool:
    """只接受项目冻结的 Bun 1.4.x 版本族。"""
    parts = value.split(".")
    if len(parts) < 2:
        return False
    try:
        major, minor = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return major == SUPPORTED_BUN_MAJOR and minor == SUPPORTED_BUN_MINOR
