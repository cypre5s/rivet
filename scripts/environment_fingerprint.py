"""采集可复现且不包含凭据值的环境指纹。"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from argparse import ArgumentParser
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast


def _command_version(command: str, arguments: Sequence[str]) -> str | None:
    """仅返回工具版本首行，不读取用户配置或凭据。"""
    executable = shutil.which(command)
    if executable is None:
        return None
    completed = subprocess.run(
        [executable, *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    output = completed.stdout.strip() or completed.stderr.strip()
    return output.splitlines()[0] if output else None


def collect_environment_fingerprint() -> dict[str, object]:
    """收集 Phase 0 需要的系统、运行时和工具状态。"""
    try:
        os_release = platform.freedesktop_os_release()
    except OSError:
        os_release = {}
    return {
        "schema_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "os": {
            "name": os_release.get("NAME", platform.system()),
            "version": os_release.get("VERSION_ID", platform.release()),
            "kernel": platform.release(),
            "architecture": platform.machine(),
        },
        "python": platform.python_version(),
        "tools": {
            "uv": _command_version("uv", ["--version"]),
            "git": _command_version("git", ["--version"]),
            "ripgrep": _command_version("rg", ["--version"]),
            "bun": _command_version("bun", ["--version"]),
            "bubblewrap": _command_version("bwrap", ["--version"]),
        },
        "deepseek_api_key_configured": bool(os.environ.get("DEEPSEEK_API_KEY")),
    }


def _build_parser() -> ArgumentParser:
    """构造支持本地未跟踪报告的参数解析器。"""
    parser = ArgumentParser(description="采集 Rivet 开发环境指纹")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """输出环境指纹，并确保目标目录只在显式请求时创建。"""
    arguments = _build_parser().parse_args(argv)
    output_path = cast(Path | None, arguments.output)
    serialized = json.dumps(
        collect_environment_fingerprint(), ensure_ascii=False, indent=2, sort_keys=True
    )
    if output_path is None:
        print(serialized)
        return 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(f"{serialized}\n", encoding="utf-8")
    print(f"环境指纹已写入 {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
