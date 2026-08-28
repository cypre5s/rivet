"""验证 TUI 无终端 smoke 仍展示完整工作台状态。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast


def test_tui_smoke_model_contains_required_panels() -> None:
    repository = Path(__file__).parents[2]
    completed = subprocess.run(
        ("bun", "run", "smoke"),
        cwd=repository / "tui",
        capture_output=True,
        timeout=10,
        check=False,
        env={"PATH": "/home/lbx/.local/bin:/usr/bin:/bin", "NO_COLOR": "1"},
    )

    payload = cast(dict[str, object], json.loads(completed.stdout.decode()))
    assert completed.returncode == 0, completed.stderr.decode()
    assert payload["layout"] == "three-column"
    assert payload["no_color"] is True
    assert payload["inspector_tabs"] == [
        "Plan",
        "Context",
        "Diff",
        "Verify",
        "Evidence",
        "Modules",
    ]
    assert payload["permission_visible"] is True
    assert payload["command_methods"] == [
        "command.ask",
        "command.plan",
        "command.fix",
        "command.verify",
        "command.diff",
        "command.apply",
    ]
