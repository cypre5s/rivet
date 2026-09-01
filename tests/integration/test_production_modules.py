"""验证正式 CLI 模块目录保持静态发现并提供真实运行时状态。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from rivet.cli.runtime import create_cli_kernel
from rivet.contracts.modules import ModuleState
from rivet.kernel.module_runtime import CapabilityLease

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
    probe = (
        "from pathlib import Path; import sys; "
        "from rivet.cli.runtime import create_cli_kernel; "
        f"assert {FACTORY_MODULE!r} not in sys.modules; "
        "create_cli_kernel(Path.cwd(), safe_mode=False); "
        f"assert {FACTORY_MODULE!r} not in sys.modules"
    )
    completed = subprocess.run(
        (sys.executable, "-c", probe),
        cwd=tmp_path,
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

    kernel = create_cli_kernel(tmp_path, safe_mode=False)
    await kernel.start()
    assert kernel.runtime.state("context.syntax") is ModuleState.INACTIVE

    lease = await kernel.acquire("context.search.syntax")
    assert lease.module_id == "context.syntax"
    assert callable(getattr(lease.capability, "retrieve", None))
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


@pytest.mark.asyncio
async def test_every_production_module_returns_a_real_capability(
    tmp_path: Path,
) -> None:
    """生产 Manifest 的 ACTIVE 必须对应可调用服务，而不是 Scope 外壳。"""
    subprocess.run(
        ("git", "init", "-q", "-b", "main"),
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env={
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    )
    subprocess.run(
        ("git", "config", "user.name", "Fixture"),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "config", "user.email", "fixture@example.invalid"),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(
        ("git", "add", "--", "README.md"),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ("git", "commit", "-qm", "initial"),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    kernel = create_cli_kernel(
        tmp_path,
        safe_mode=False,
        provider_base_url="https://api.deepseek.com",
        credential_accessor=lambda name: (
            "fixture" if name == "DEEPSEEK_API_KEY" else None
        ),
    )
    await kernel.start()
    leases: list[CapabilityLease[object]] = []
    expected_types = {
        "provider.chat.completions": "DeepSeekProvider",
        "guard.local_execution": "WorkspaceToolCapabilityService",
        "context.search.lexical": "ProgressiveContext",
        "context.search.syntax": "ProgressiveContext",
        "context.search.lsp": "SemanticContextRetriever",
        "reader.detect": "ReaderCapabilityService",
        "reader.document": "ReaderCapabilityService",
        "reader.image": "ReaderCapabilityService",
        "reader.media": "ReaderCapabilityService",
        "reader.archive.sevenzip": "ReaderCapabilityService",
        "reader.transcription": "ReaderCapabilityService",
        "transaction.worktree": "TransactionManager",
        "verify.deterministic": "VerificationCapabilityService",
    }

    for capability_id, expected_type in expected_types.items():
        lease = await kernel.acquire(capability_id)
        leases.append(lease)
        assert type(lease.capability).__name__ == expected_type
        assert kernel.runtime.state(lease.module_id) is ModuleState.ACTIVE

    for lease in reversed(leases):
        await lease.release()
    await kernel.shutdown()

    assert kernel.runtime.resource_counts().resource_count == 0
    assert not kernel.runtime.journal.path.exists()
