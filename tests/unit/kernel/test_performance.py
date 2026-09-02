"""验证静态 Kernel 与五模块目录不会偷跑重型实现。"""

from __future__ import annotations

import os
import subprocess
import sys


def test_kernel_and_catalog_import_without_heavy_factories() -> None:
    probe = (
        "import sys; "
        "import rivet.kernel.application; import rivet.modules.catalog; "
        "blocked=('rivet.modules.factories','rivet.providers.deepseek',"
        "'rivet.context.engine','rivet.transaction.manager','rivet.verify.service'); "
        "raise SystemExit(1 if any(name in sys.modules for name in blocked) else 0)"
    )

    completed = subprocess.run(
        (sys.executable, "-c", probe),
        check=False,
        capture_output=True,
        env={
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        },
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
