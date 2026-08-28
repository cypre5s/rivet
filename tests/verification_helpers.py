"""建立可真实执行的验证事务和八类补丁 fixture。"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from rivet.contracts.transactions import AcceptanceSpec, TransactionRecord
from rivet.kernel.resources import ResourceScope
from rivet.tools.files import TransactionFileWriter
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.process import ProcessExecutor, ProcessRunner
from rivet.transaction.manager import TransactionManager
from rivet.verify.detector import ProjectConfiguration
from rivet.verify.service import VerificationOutcome, VerificationService
from tests.fixtures.verification.cases import VerificationFixtureCase
from tests.transaction_helpers import initialize_repository, make_manager, run_git


@dataclass(frozen=True, slots=True)
class PreparedVerification:
    """汇总验证结果及后续 apply/清理所需对象。"""

    repository: Path
    manager: TransactionManager
    scope: ResourceScope
    verifying_record: TransactionRecord
    outcome: VerificationOutcome
    runtime_secret: str | None


def _fixture_executor(
    boundary: WorkspaceBoundary,
    scope: ResourceScope,
    environment: Mapping[str, str],
    allowlist: frozenset[str],
) -> ProcessExecutor:
    """仅让受控测试 fixture 显式使用 Phase 5 裸进程原语。"""
    return ProcessRunner(
        boundary,
        scope=scope,
        environment=environment,
        environment_allowlist=allowlist,
        root_kind="transaction",
    )


def _write_base_project(repository: Path) -> None:
    """向初始 Git fixture 增加一个有真实缺陷的小型 Python 项目。"""
    files = {
        "app.py": "def transform(value: int) -> int:\n    return value\n",
        "stable.txt": "stable\n",
        "forbidden.txt": "original\n",
        "check_general.py": (
            "from app import transform\n"
            "raise SystemExit(0 if all(transform(value) == value * 2 "
            "for value in (1, 2, 3)) else 1)\n"
        ),
        "check_target.py": (
            "from app import transform\n"
            "raise SystemExit(0 if transform(2) == 4 else 1)\n"
        ),
        "check_regression.py": (
            "from pathlib import Path\n"
            "from app import transform\n"
            "valid = transform(0) == 0 and "
            "Path('stable.txt').read_text(encoding='utf-8') == 'stable\\n'\n"
            "raise SystemExit(0 if valid else 1)\n"
        ),
        "check_already_passing.py": (
            "from app import transform\n"
            "raise SystemExit(0 if transform(2) == 2 else 1)\n"
        ),
        "check_slow.py": "import time\ntime.sleep(3)\n",
    }
    for relative_path, content in files.items():
        (repository / relative_path).write_text(content, encoding="utf-8")
    run_git(repository, "add", "--", *files)
    run_git(repository, "commit", "-qm", "add verification fixture")


def _acceptance(
    case: VerificationFixtureCase,
    *,
    behavior_verification_commands: tuple[tuple[str, ...], ...] | None = None,
) -> AcceptanceSpec:
    """将每个 fixture 转为冻结命令、范围和预算。"""
    return AcceptanceSpec(
        acceptance_id=f"acceptance_{case.case_id}",
        user_goal=f"修复验证 fixture：{case.case_id}",
        baseline_reproduction=((sys.executable, case.baseline_script),),
        allowed_paths=("app.py", "stable.txt", "secret.txt"),
        forbidden_paths=("forbidden.txt",),
        expected_behaviors=("transform 对任意整数返回两倍值",),
        preserved_behaviors=("零值行为与 stable.txt 保持不变",),
        verification_commands=((sys.executable, case.targeted_script),),
        behavior_verification_commands=(
            ((sys.executable, "check_general.py"),)
            if behavior_verification_commands is None
            else behavior_verification_commands
        ),
        max_wall_seconds=case.max_wall_seconds,
        max_tokens=1_000,
        max_tool_calls=20,
        acceptable_risks=("仅修改 fixture",),
        non_goals=("不访问网络",),
    )


async def run_verification_case(
    tmp_path: Path,
    case: VerificationFixtureCase,
    *,
    cancelled: Callable[[], bool] | None = None,
    project_configuration: ProjectConfiguration | None = None,
    use_production_sandbox: bool = False,
    sandbox_executable: Path | None = None,
    behavior_verification_commands: tuple[tuple[str, ...], ...] | None = None,
) -> PreparedVerification:
    """创建真实 Worktree、写补丁并执行完整验证矩阵。"""
    repository = initialize_repository(tmp_path)
    _write_base_project(repository)
    scope = ResourceScope(f"verify.{case.case_id}")
    manager = make_manager(repository, tmp_path, scope)
    record = await manager.create(transaction_id=f"tx_{case.case_id}")
    await manager.freeze_acceptance(
        record.transaction_id,
        _acceptance(
            case,
            behavior_verification_commands=behavior_verification_commands,
        ),
        confirmed=True,
    )
    writer = TransactionFileWriter(manager.transaction_boundary(record.transaction_id))
    writer.write("app.py", case.implementation)
    runtime_secret: str | None = None
    for relative_path, content in case.extra_changes:
        if content == "__RUNTIME_SECRET__":
            runtime_secret = "sk-" + ("v" * 32)
            content = runtime_secret
        if (manager.worktree_path(record.transaction_id) / relative_path).exists():
            writer.write(relative_path, content)
        else:
            writer.create(relative_path, content)
    await manager.record_patch_set(
        record.transaction_id,
        patch_id=f"patch_{case.case_id}",
    )
    verifying = await manager.begin_verification(record.transaction_id)
    configuration = project_configuration or ProjectConfiguration(
        related=((sys.executable, "check_target.py"),),
        regression=((sys.executable, "check_regression.py"),),
        static=(
            (
                sys.executable,
                "-c",
                "compile(open('app.py', encoding='utf-8').read(), 'app.py', 'exec')",
            ),
        ),
    )
    service = VerificationService(
        manager,
        scope=scope,
        project_configuration=configuration,
        configuration_confirmed=True,
        cancelled=cancelled,
        executor_factory=None if use_production_sandbox else _fixture_executor,
        sandbox_executable=sandbox_executable,
    )
    outcome = await service.verify(record.transaction_id)
    return PreparedVerification(
        repository=repository,
        manager=manager,
        scope=scope,
        verifying_record=verifying,
        outcome=outcome,
        runtime_secret=runtime_secret,
    )
