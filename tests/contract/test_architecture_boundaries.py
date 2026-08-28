"""验证 contracts 不依赖 Kernel 或具体能力层。"""

from pathlib import Path

from scripts.verify_architecture import find_architecture_violations


def test_repository_contract_boundaries_have_no_violations() -> None:
    assert find_architecture_violations(Path.cwd()) == ()


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
