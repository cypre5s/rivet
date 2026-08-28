"""验证正式 CLI 模块目录保持静态发现并提供真实运行时状态。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from rivet.cli.runtime import create_cli_kernel, module_scope
from rivet.contracts.modules import ModuleState

FACTORY_MODULE = "rivet.modules.factories"


def test_agent_capability_module_does_not_import_heavy_implementations() -> None:
    script = (
        "import sys; import rivet.cli.agent_capabilities; "
        "blocked=('rivet.context.engine','rivet.context.semantic',"
        "'rivet.readers.service','rivet.providers.deepseek'); "
        "raise SystemExit(1 if any(name in sys.modules for name in blocked) else 0)"
    )

    completed = subprocess.run(
        (sys.executable, "-c", script),
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


@pytest.mark.asyncio
async def test_production_kernel_defers_optional_factory_and_reports_live_state(
    tmp_path: Path,
) -> None:
    sys.modules.pop(FACTORY_MODULE, None)
    kernel = create_cli_kernel(tmp_path, safe_mode=False)

    assert FACTORY_MODULE not in sys.modules
    await kernel.start()
    assert FACTORY_MODULE not in sys.modules
    assert kernel.runtime.state("context.syntax") is ModuleState.INACTIVE

    lease = await kernel.acquire_lease("context.search.syntax")
    assert module_scope(lease.instance).owner_module_id == "context.syntax"
    snapshots = kernel.runtime.snapshots()

    assert kernel.runtime.state("context.syntax") is ModuleState.ACTIVE
    assert (
        next(
            item for item in snapshots if item.module_id == "context.syntax"
        ).lease_count
        == 1
    )
    await lease.release()
    await kernel.shutdown()
    assert kernel.runtime.resource_counts().resource_count == 0
    assert not kernel.runtime.journal.path.exists()
