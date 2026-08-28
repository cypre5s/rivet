"""检查 bubblewrap 可执行文件而不尝试降低沙箱要求。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class SandboxDoctorReport:
    """描述沙箱是否可作为写入和执行任务的硬依赖。"""

    ready: bool
    status: Literal["AVAILABLE", "MISSING", "NOT_EXECUTABLE", "UNUSABLE"]
    executable: str | None
    required: bool
    next_action: str

    def to_json(self) -> str:
        """稳定输出机器可读 Doctor 结果。"""
        return json.dumps(
            asdict(self), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


class SandboxDoctor:
    """定位显式或 PATH 中的 bubblewrap，并报告失败关闭影响。"""

    def __init__(self, *, executable: Path | None = None) -> None:
        configured = os.environ.get("RIVET_BWRAP_PATH")
        discovered = shutil.which("bwrap")
        self._candidate = Path(executable or configured or discovered or "bwrap")

    def inspect(self) -> SandboxDoctorReport:
        """检查文件属性并执行不接触用户数据的最小命名空间探测。"""
        if not self._candidate.is_file():
            return SandboxDoctorReport(
                ready=False,
                status="MISSING",
                executable=None,
                required=True,
                next_action="安装 bubblewrap；写入和执行任务将保持失败关闭",
            )
        resolved = self._candidate.resolve()
        if not os.access(resolved, os.X_OK):
            return SandboxDoctorReport(
                ready=False,
                status="NOT_EXECUTABLE",
                executable=str(resolved),
                required=True,
                next_action="修复 bubblewrap 执行权限后重试",
            )
        try:
            completed = subprocess.run(
                (
                    str(resolved),
                    "--die-with-parent",
                    "--new-session",
                    "--unshare-all",
                    "--ro-bind",
                    "/",
                    "/",
                    "--",
                    "/usr/bin/true",
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        if completed is None or completed.returncode != 0:
            return SandboxDoctorReport(
                ready=False,
                status="UNUSABLE",
                executable=str(resolved),
                required=True,
                next_action="检查内核用户命名空间与 bubblewrap 配置",
            )
        return SandboxDoctorReport(
            ready=True,
            status="AVAILABLE",
            executable=str(resolved),
            required=True,
            next_action="沙箱可用",
        )
