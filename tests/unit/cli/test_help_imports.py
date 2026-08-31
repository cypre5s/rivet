"""验证帮助入口只构建参数界面，不预热任何运行时子系统。"""

from __future__ import annotations

import os
import subprocess
import sys


def test_help_defers_all_internal_runtime_modules() -> None:
    script = """
import contextlib
import io
import sys

from rivet.cli.application import run_cli

try:
    with contextlib.redirect_stdout(io.StringIO()):
        run_cli(["--help"])
except SystemExit as error:
    if error.code != 0:
        raise

runtime_prefixes = (
    "rivet.cli.config",
    "rivet.cli.doctor",
    "rivet.cli.modules",
    "rivet.cli.runtime",
    "rivet.context",
    "rivet.contracts",
    "rivet.guard",
    "rivet.ipc",
    "rivet.kernel",
    "rivet.modules",
    "rivet.providers",
    "rivet.readers",
    "rivet.storage",
    "rivet.tools",
    "rivet.trace",
    "rivet.transaction",
    "rivet.verify",
)
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in runtime_prefixes)
)
if loaded:
    print("\\n".join(loaded), file=sys.stderr)
    raise SystemExit(1)
"""
    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    }

    completed = subprocess.run(
        (sys.executable, "-c", script),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
