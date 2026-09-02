"""验证五个生产模块只在耐久 Demand 后返回真实能力。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rivet.contracts.modules import ModuleManifest, ModuleState
from rivet.kernel.application import RivetKernel
from rivet.kernel.capability_demand import DemandContext, InMemoryDemandJournal
from rivet.kernel.errors import ModuleActivationError
from rivet.kernel.module_api import ModuleActivationContext
from rivet.kernel.module_events import InMemoryModuleLifecycleSink
from rivet.kernel.resources import ResourceScope
from rivet.modules.catalog import BUILTIN_MODULE_MANIFESTS
from rivet.modules.factories import ContextCapabilityService
from rivet.tools.catalog import ContextSearchArguments, FileReadArguments
from rivet.tools.errors import PathBoundaryError
from rivet.tools.handlers import WorkspaceToolHandlers
from rivet.tools.paths import WorkspaceBoundary

FACTORY_MODULE = "rivet.modules.factories"


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        env={
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    )


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Fixture")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(repository, "add", "--", "README.md")
    _git(repository, "commit", "-qm", "initial")
    return repository


def _kernel(
    repository: Path,
) -> tuple[RivetKernel, InMemoryDemandJournal, InMemoryModuleLifecycleSink]:
    journal = InMemoryDemandJournal()
    sink = InMemoryModuleLifecycleSink()
    kernel = RivetKernel.from_manifests(
        BUILTIN_MODULE_MANIFESTS,
        demand_journal=journal,
        lifecycle_sink=sink,
        activation_context=ModuleActivationContext(
            repository=repository,
            provider_base_url="https://api.deepseek.com",
            credential_accessor=lambda name: (
                "fixture" if name == "DEEPSEEK_API_KEY" else None
            ),
        ),
    )
    return kernel, journal, sink


def test_catalog_contains_exactly_five_core_modules() -> None:
    assert set(ModuleManifest.model_fields) == {
        "module_id",
        "factory",
        "provides",
        "requires",
    }
    assert [manifest.module_id for manifest in BUILTIN_MODULE_MANIFESTS] == [
        "provider.deepseek",
        "context.lexical",
        "transaction.git",
        "guard.sandbox",
        "verify.matrix",
    ]
    assert all(
        set(manifest.model_fields_set)
        <= {"module_id", "factory", "provides", "requires"}
        for manifest in BUILTIN_MODULE_MANIFESTS
    )


def test_catalog_import_does_not_import_factory_or_heavy_implementations() -> None:
    script = (
        "import sys; import rivet.modules.catalog; "
        "blocked=('rivet.modules.factories','rivet.providers.deepseek',"
        "'rivet.context.engine','rivet.transaction.manager','rivet.verify.service'); "
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
async def test_start_keeps_every_production_module_inactive(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    kernel, journal, sink = _kernel(repository)

    await kernel.start()

    assert journal.records == ()
    assert sink.activation_events == []
    assert all(
        snapshot.state is ModuleState.INACTIVE for snapshot in kernel.snapshots()
    )
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_every_production_module_returns_real_capability_after_demand(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    kernel, journal, sink = _kernel(repository)
    await kernel.start()
    root = await kernel.begin_user_demand(
        "production-test",
        reason="集成测试枚举真实核心能力",
        context=DemandContext(
            run_id="run_production_modules",
            session_id="session_production_modules",
        ),
    )
    expected_types = {
        "provider.chat.completions": "DeepSeekProvider",
        "context.search.lexical": "ContextCapabilityService",
        "transaction.worktree": "TransactionManager",
        "guard.local_execution": "GuardCapabilityService",
        "verify.deterministic": "VerificationCapabilityService",
    }

    for capability_id, expected_type in expected_types.items():
        lease = await kernel.acquire_required(
            capability_id,
            parent=root,
            reason=f"集成测试请求 {capability_id}",
            operation_id=f"test:{capability_id}",
        )
        assert type(lease.capability).__name__ == expected_type
        assert kernel.state(lease.module_id) is ModuleState.ACTIVE
        assert (
            kernel.snapshots()[
                [item.module_id for item in kernel.snapshots()].index(lease.module_id)
            ].activated_by_demand_id
            == lease.demand_handle.demand_id
        )
        await lease.release()
        assert kernel.state(lease.module_id) is ModuleState.INACTIVE

    assert len(journal.records) == 1 + len(expected_types)
    assert {event.module_id for event in sink.activation_events} == {
        "provider.deepseek",
        "context.lexical",
        "transaction.git",
        "guard.sandbox",
        "verify.matrix",
    }
    assert kernel.resource_counts().resource_count == 0
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_fix_context_search_observes_candidate_worktree_only(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    (repository / "README.md").write_text("main_old_token\n", encoding="utf-8")
    _git(repository, "add", "--", "README.md")
    _git(repository, "commit", "-qm", "old value")
    worktree = tmp_path / "candidate"
    _git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "README.md").write_text("candidate_new_token\n", encoding="utf-8")

    scope = ResourceScope("context.lexical")
    capability = ContextCapabilityService(scope)
    fix_boundary = WorkspaceBoundary(
        repository,
        worktree,
        transaction_id="tx_context",
        mode="FIX",
    )
    fix_handler = WorkspaceToolHandlers(fix_boundary)
    fix_payload = json.loads(
        await fix_handler.context_search(
            ContextSearchArguments(query="candidate_new_token"),
            {"context.search.lexical": capability},
        )
    )
    main_payload = json.loads(
        await WorkspaceToolHandlers(
            WorkspaceBoundary(repository, mode="ASK")
        ).context_search(
            ContextSearchArguments(query="candidate_new_token"),
            {"context.search.lexical": capability},
        )
    )

    assert fix_payload["status"] == "MATCH"
    assert "candidate_new_token" in fix_payload["matches"][0]["content"]
    assert "main_old_token" not in fix_payload["matches"][0]["content"]
    assert main_payload["status"] == "NO_MATCH"
    await scope.close()


@pytest.mark.asyncio
async def test_fix_handlers_enforce_frozen_read_scope(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "allowed.py").write_text("allowed_marker = 1\n", encoding="utf-8")
    (repository / "outside.py").write_text("outside_marker = 2\n", encoding="utf-8")
    _git(repository, "add", "--", "allowed.py", "outside.py")
    _git(repository, "commit", "-qm", "add scoped sources")

    scope = ResourceScope("context.lexical.scoped")
    capability = ContextCapabilityService(scope)
    handlers = WorkspaceToolHandlers(
        WorkspaceBoundary(repository, mode="ASK"),
        read_scope=("allowed.py",),
    )
    allowed = json.loads(
        await handlers.file_read(FileReadArguments(path="allowed.py"), {})
    )
    with pytest.raises(PathBoundaryError, match="冻结调查范围"):
        await handlers.file_read(FileReadArguments(path="outside.py"), {})
    search = json.loads(
        await handlers.context_search(
            ContextSearchArguments(query="outside_marker"),
            {"context.search.lexical": capability},
        )
    )

    assert "allowed_marker" in allowed["content"]
    assert search["status"] == "NO_MATCH"
    await scope.close()
    scope.assert_empty()


@pytest.mark.asyncio
async def test_guard_activation_fails_closed_when_bubblewrap_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    monkeypatch.setenv("RIVET_BWRAP_PATH", str(tmp_path / "missing-bwrap"))
    kernel, _, _ = _kernel(repository)
    await kernel.start()
    root = await kernel.begin_user_demand(
        "guard-unavailable",
        reason="验证受管写入在沙箱缺失时失败关闭",
        context=DemandContext(
            run_id="run_guard_unavailable",
            session_id="session_guard_unavailable",
        ),
    )

    with pytest.raises(ModuleActivationError, match="guard.sandbox"):
        await kernel.acquire_required(
            "guard.local_execution",
            parent=root,
            reason="受管写入需要 bubblewrap",
            operation_id="test:guard-unavailable",
        )

    assert kernel.state("guard.sandbox") is ModuleState.FAILED
    assert kernel.resource_counts().resource_count == 0
    await kernel.shutdown()
