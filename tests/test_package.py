"""验证 Phase 0 的最小可安装包。"""

import subprocess
import sys

import rivet


def test_package_exposes_version() -> None:
    assert rivet.__version__ == "0.1.0"


def test_module_help_returns_success() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "rivet", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "Demand-driven、Evidence-gated 的本地 Coding Agent" in completed.stdout
    assert completed.stderr == ""
