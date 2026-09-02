"""验证薄 Kernel 边界与被删除子系统不会回流。"""

from pathlib import Path
from typing import get_args

import pytest

from rivet.cli.parser import OFFICIAL_COMMANDS
from rivet.tools.paths import WorkspaceMode
from scripts.verify_architecture import find_architecture_violations

REMOVED_SOURCE_PATHS = (
    "src/rivet/readers",
    "src/rivet/export",
    "src/rivet/context/budget.py",
    "src/rivet/context/engine.py",
    "src/rivet/context/inventory.py",
    "src/rivet/context/lsp_client.py",
    "src/rivet/context/lsp_manifest.py",
    "src/rivet/context/lsp_sidecar.py",
    "src/rivet/context/semantic.py",
    "src/rivet/context/syntax.py",
    "src/rivet/contracts/context.py",
    "src/rivet/contracts/readers.py",
    "src/rivet/cli/doctor.py",
    "src/rivet/cli/modules.py",
    "src/rivet/storage",
    "src/rivet/guard/doctor.py",
    "src/rivet/trace/database.py",
    "src/rivet/trace/artifacts.py",
    "src/rivet/trace/reducer.py",
    "src/rivet/trace/replay.py",
    "src/rivet/trace/cli.py",
    "tools/lsp",
)
REMOVED_COMMANDS = frozenset(
    {
        "benchmark",
        "clean",
        "config",
        "doctor",
        "export",
        "modules",
        "plan",
        "read",
        "resume",
        "trace",
    }
)


def test_repository_contract_boundaries_have_no_violations() -> None:
    assert find_architecture_violations(Path.cwd()) == ()


def test_removed_subsystems_are_absent_from_product_source() -> None:
    repository = Path.cwd()

    for relative_path in REMOVED_SOURCE_PATHS:
        path = repository / relative_path
        if path.is_dir():
            assert not any(
                child.is_file()
                and "__pycache__" not in child.parts
                and "node_modules" not in child.parts
                for child in path.rglob("*")
            )
        else:
            assert not path.exists()
    assert {
        path.name for path in (repository / "src" / "rivet" / "context").glob("*.py")
    } == {"__init__.py", "lexical.py"}


def test_public_cli_contains_only_final_commands() -> None:
    assert OFFICIAL_COMMANDS == (
        "init",
        "ask",
        "fix",
        "diff",
        "verify",
        "apply",
        "abort",
    )
    assert REMOVED_COMMANDS.isdisjoint(OFFICIAL_COMMANDS)


def test_workspace_mode_has_no_plan_variant() -> None:
    assert get_args(WorkspaceMode) == ("ASK", "FIX")


def test_contract_importing_kernel_is_rejected(tmp_path: Path) -> None:
    contract_directory = tmp_path / "src" / "rivet" / "contracts"
    contract_directory.mkdir(parents=True)
    (contract_directory / "invalid.py").write_text(
        "from rivet.kernel import agent_loop\n",
        encoding="utf-8",
    )

    violations = find_architecture_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].rule_id == "contracts.reverse_dependency"


def test_kernel_importing_concrete_transaction_is_rejected(tmp_path: Path) -> None:
    kernel_directory = tmp_path / "src" / "rivet" / "kernel"
    kernel_directory.mkdir(parents=True)
    (kernel_directory / "invalid.py").write_text(
        "from rivet.transaction import manager\n",
        encoding="utf-8",
    )

    violations = find_architecture_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].rule_id == "kernel.concrete_dependency"


def test_trace_importing_provider_is_rejected(tmp_path: Path) -> None:
    trace_directory = tmp_path / "src" / "rivet" / "trace"
    trace_directory.mkdir(parents=True)
    (trace_directory / "invalid.py").write_text(
        "from rivet.providers import deepseek\n",
        encoding="utf-8",
    )

    violations = find_architecture_violations(tmp_path)

    assert len(violations) == 1
    assert violations[0].rule_id == "trace.concrete_dependency"


@pytest.mark.parametrize(
    ("symbol", "source"),
    (
        (
            "_ActivationPermit",
            "from rivet.kernel.module_runtime import _ActivationPermit\n",
        ),
        (
            "_acquire",
            "async def bypass(runtime, permit):\n"
            "    return await runtime._acquire(permit)\n",
        ),
        (
            "_kernel_required",
            "from rivet.kernel.capability_demand import CapabilityDemand\n"
            "def bypass(parent):\n"
            "    return CapabilityDemand._kernel_required(parent)\n",
        ),
    ),
)
def test_business_source_cannot_touch_private_activation_qualification(
    tmp_path: Path,
    symbol: str,
    source: str,
) -> None:
    tools_directory = tmp_path / "src" / "rivet" / "tools"
    tools_directory.mkdir(parents=True)
    (tools_directory / "bypass.py").write_text(source, encoding="utf-8")

    violations = find_architecture_violations(tmp_path)

    assert any(
        violation.rule_id == "kernel.activation_bypass" and symbol in violation.summary
        for violation in violations
    )
