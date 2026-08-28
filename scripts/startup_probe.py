"""观测最小 CLI 帮助路径的网络与子进程审计事件。"""

from __future__ import annotations

import json
import sys

AUDIT_COUNTS = {"network_connections": 0, "subprocess_starts": 0}


def _audit_hook(event_name: str, _arguments: tuple[object, ...]) -> None:
    """只统计启动路径中的外联和子进程事件，不记录参数。"""
    if event_name == "socket.connect":
        AUDIT_COUNTS["network_connections"] += 1
    elif event_name == "subprocess.Popen":
        AUDIT_COUNTS["subprocess_starts"] += 1


def main() -> int:
    """运行最小帮助路径并将计数以固定标记写入 stderr。"""
    sys.addaudithook(_audit_hook)
    from rivet import main as rivet_main

    exit_code = 0
    try:
        rivet_main(["--help"])
    except SystemExit as error:
        exit_code = error.code if isinstance(error.code, int) else 1
    print(
        f"RIVET_STARTUP_AUDIT={json.dumps(AUDIT_COUNTS, sort_keys=True)}",
        file=sys.stderr,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
