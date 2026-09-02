"""证明 Verify 与 Evidence 只消费事务中持久化的冻结验收事实。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from rivet.cli.config import load_config
from rivet.cli.model_commands import build_acceptance_spec
from rivet.contracts.transactions import AcceptanceSpec, TransactionState
from rivet.contracts.verification import VerificationKind, VerificationStatus
from rivet.kernel.resources import ResourceScope
from rivet.tools.files import TransactionFileWriter
from rivet.trace.paths import RuntimePaths
from rivet.transaction.errors import TransactionError
from rivet.transaction.hashing import acceptance_sha256
from rivet.transaction.manager import TransactionManager
from rivet.transaction.store import TransactionStore
from rivet.verify.detector import ProjectDetector
from rivet.verify.service import VerificationService
from tests.transaction_helpers import acceptance_spec, initialize_repository, run_git
from tests.verification_helpers import fixture_executor


def _toml_argv(command: tuple[str, ...]) -> str:
    """把受控测试 argv 写成 TOML 可接受的字符串数组。"""
    return "[" + ", ".join(json.dumps(argument) for argument in command) + "]"


def _project_configuration(
    *,
    acceptance: tuple[str, ...],
    regression: tuple[str, ...],
) -> str:
    """生成只含最小模型选择和验证命令的真实项目配置。"""
    return (
        "schema_version = 1\n\n"
        "[rivet]\n"
        'model = "deepseek-v4-flash"\n\n'
        "[verification]\n"
        f"acceptance = [{_toml_argv(acceptance)}]\n"
        f"regression = [{_toml_argv(regression)}]\n"
        "static = []\n"
    )


def _install_real_project(repository: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """提交缺陷实现、独立 oracle、回归检查和项目配置。"""
    acceptance = (sys.executable, "check_behavior.py")
    regression = (sys.executable, "check_regression.py")
    files = {
        "app.py": "def transform(value: int) -> int:\n    return value\n",
        "check_behavior.py": (
            "from app import transform\n"
            "raise SystemExit(0 if transform(2) == 4 else 1)\n"
        ),
        "check_regression.py": (
            "from app import transform\n"
            "raise SystemExit(0 if transform(0) == 0 else 1)\n"
        ),
    }
    for relative_path, content in files.items():
        (repository / relative_path).write_text(content, encoding="utf-8")
    configuration_directory = repository / ".rivet"
    configuration_directory.mkdir()
    (configuration_directory / "project.toml").write_text(
        _project_configuration(
            acceptance=acceptance,
            regression=regression,
        ),
        encoding="utf-8",
    )
    run_git(repository, "add", "--", *files, ".rivet/project.toml")
    run_git(repository, "commit", "-qm", "add frozen acceptance fixture")
    return acceptance, regression


@pytest.mark.asyncio
async def test_verify_uses_frozen_acceptance_after_project_config_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置冻结后的内容漂移不能替换命令、Verdict 或 Evidence 哈希。"""
    repository = initialize_repository(tmp_path)
    original_acceptance, original_regression = _install_real_project(repository)
    detection = ProjectDetector().detect(repository)
    specification = build_acceptance_spec(
        repository,
        "让 transform 返回输入的两倍",
        detection=detection,
        explicit_paths=("app.py",),
        config=load_config(repository, environment={}),
    )
    frozen_hash = acceptance_sha256(specification)

    xdg_state_home = tmp_path / "xdg-state"
    xdg_cache_home = tmp_path / "xdg-cache"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_cache_home))
    paths = RuntimePaths.for_repository(repository)
    scope = ResourceScope("verify.frozen.acceptance")
    manager = TransactionManager(repository, scope=scope)
    transaction_id = "tx_frozen_acceptance"

    record = await manager.create(
        specification,
        confirmed=True,
        transaction_id=transaction_id,
    )
    assert {
        path.name for path in (paths.transactions_root / transaction_id).iterdir()
    } == {
        "acceptance_spec.json",
        "acceptance_spec.sha256",
        "record.json",
    }
    writer = TransactionFileWriter(manager.transaction_boundary(transaction_id))
    writer.write(
        "app.py",
        "def transform(value: int) -> int:\n    return value * 2\n",
    )
    patch = await manager.record_patch_set(
        transaction_id,
        patch_id="patch_frozen_acceptance",
    )
    await manager.begin_verification(transaction_id)

    tampered_acceptance = (
        sys.executable,
        "-c",
        "raise SystemExit(91)",
    )
    tampered_regression = (
        sys.executable,
        "-c",
        "raise SystemExit(92)",
    )
    (repository / ".rivet" / "project.toml").write_text(
        _project_configuration(
            acceptance=tampered_acceptance,
            regression=tampered_regression,
        ),
        encoding="utf-8",
    )
    changed_detection = ProjectDetector().detect(repository)
    assert changed_detection.configuration is not None
    assert changed_detection.configuration.acceptance == (tampered_acceptance,)
    assert changed_detection.configuration.regression == (tampered_regression,)
    assert await manager.load_acceptance_spec(transaction_id) == specification

    try:
        outcome = await VerificationService(
            manager,
            scope=scope,
            executor_factory=fixture_executor,
        ).verify(transaction_id)

        assert outcome.verdict.status is VerificationStatus.PASSED
        assert outcome.transaction.state is TransactionState.VERIFIED
        executed = tuple(
            (result.step.kind, result.step.command)
            for result in outcome.verdict.results
            if result.step.kind
            in {
                VerificationKind.BASELINE,
                VerificationKind.BEHAVIOR,
                VerificationKind.REGRESSION,
            }
        )
        assert executed == (
            (VerificationKind.BASELINE, original_acceptance),
            (VerificationKind.BEHAVIOR, original_acceptance),
            (VerificationKind.REGRESSION, original_regression),
        )
        assert all(command != tampered_acceptance for _, command in executed)
        assert all(command != tampered_regression for _, command in executed)

        assert record.acceptance_sha256 == frozen_hash
        assert patch.acceptance_sha256 == frozen_hash
        assert outcome.verdict.acceptance_sha256 == frozen_hash
        assert outcome.manifest.acceptance_sha256 == frozen_hash
        assert outcome.manifest.base_commit == record.base_commit
        assert outcome.manifest.patch_sha256 == patch.patch_sha256

        transaction_directory = paths.transactions_root / transaction_id
        evidence_transaction_directory = paths.evidence_root / transaction_id
        assert outcome.evidence_directory == (
            evidence_transaction_directory / "attempt_0001"
        )
        assert outcome.evidence_directory.is_dir()
        assert not (transaction_directory / "evidence").exists()
        assert not tuple(transaction_directory.rglob("attempt_*"))
        assert (paths.evidence_root / outcome.verdict.evidence_manifest_path).is_file()

        evidence_acceptance = AcceptanceSpec.model_validate_json(
            (outcome.evidence_directory / "acceptance_spec.json").read_bytes()
        )
        assert evidence_acceptance == specification
        matrix = json.loads(
            (outcome.evidence_directory / "matrix.json").read_text(encoding="utf-8")
        )
        matrix_commands = tuple(
            tuple(step["command"])
            for step in matrix["steps"]
            if step["kind"]
            in {
                VerificationKind.BASELINE.value,
                VerificationKind.BEHAVIOR.value,
                VerificationKind.REGRESSION.value,
            }
        )
        assert matrix_commands == (
            original_acceptance,
            original_acceptance,
            original_regression,
        )
        with pytest.raises(TransactionError, match="apply 前发生漂移") as caught:
            await manager.apply(transaction_id)
        assert caught.value.code == "transaction.repository_drift"
        assert manager.store().load_record(transaction_id).state is (
            TransactionState.VERIFIED
        )
    finally:
        await manager.abort(transaction_id)
        scope.assert_empty()
        await scope.close()


@pytest.mark.asyncio
async def test_verify_rejects_config_drift_combined_with_any_other_dirty_path(
    tmp_path: Path,
) -> None:
    """配置例外必须是唯一脏路径，不能掩盖任何代码或数据漂移。"""
    repository = initialize_repository(tmp_path)
    original_acceptance, original_regression = _install_real_project(repository)
    specification = build_acceptance_spec(
        repository,
        "让 transform 返回输入的两倍",
        detection=ProjectDetector().detect(repository),
        explicit_paths=("app.py",),
        config=load_config(repository, environment={}),
    )
    scope = ResourceScope("verify.frozen.acceptance.other-drift")
    manager = TransactionManager(
        repository,
        scope=scope,
        cache_root=tmp_path / "cache" / "rivet",
        state_root=tmp_path / "state" / "transactions",
        evidence_root=tmp_path / "state" / "evidence",
    )
    transaction_id = "tx_frozen_other_drift"
    await manager.create(
        specification,
        confirmed=True,
        transaction_id=transaction_id,
    )
    TransactionFileWriter(manager.transaction_boundary(transaction_id)).write(
        "app.py",
        "def transform(value: int) -> int:\n    return value * 2\n",
    )
    await manager.record_patch_set(
        transaction_id,
        patch_id="patch_frozen_other_drift",
    )
    await manager.begin_verification(transaction_id)

    (repository / ".rivet" / "project.toml").write_text(
        _project_configuration(
            acceptance=original_acceptance,
            regression=original_regression,
        )
        + "# post-freeze configuration edit\n",
        encoding="utf-8",
    )
    (repository / "tracked.txt").write_text(
        "unrelated main-worktree drift\n",
        encoding="utf-8",
    )

    try:
        with pytest.raises(
            TransactionError,
            match="主仓库在验证前发生漂移",
        ) as caught:
            await VerificationService(
                manager,
                scope=scope,
                executor_factory=fixture_executor,
            ).verify(transaction_id)
        assert caught.value.code == "transaction.verification_repository_drift"
        assert not manager.evidence_root(transaction_id).exists()
    finally:
        await manager.abort(transaction_id)
        scope.assert_empty()
        await scope.close()


@pytest.mark.asyncio
async def test_ignored_untracked_project_config_cannot_enter_frozen_transaction(
    tmp_path: Path,
) -> None:
    """`.git/info/exclude` 不能把未跟踪验证配置伪装成干净事实。"""
    repository = initialize_repository(tmp_path)
    acceptance, regression = _install_real_project(repository)
    run_git(repository, "rm", "-q", "--", ".rivet/project.toml")
    run_git(repository, "commit", "-qm", "remove tracked project config")
    exclude = repository / ".git" / "info" / "exclude"
    exclude.write_text(
        exclude.read_text(encoding="utf-8") + "\n/.rivet/project.toml\n",
        encoding="utf-8",
    )
    configuration_directory = repository / ".rivet"
    configuration_directory.mkdir(exist_ok=True)
    (configuration_directory / "project.toml").write_text(
        _project_configuration(
            acceptance=acceptance,
            regression=regression,
        ),
        encoding="utf-8",
    )
    assert run_git(repository, "status", "--short") == ""

    specification = build_acceptance_spec(
        repository,
        "让 transform 返回输入的两倍",
        detection=ProjectDetector().detect(repository),
        explicit_paths=("app.py",),
        config=load_config(repository, environment={}),
    )
    scope = ResourceScope("verify.frozen.acceptance.untracked-config")
    manager = TransactionManager(
        repository,
        scope=scope,
        cache_root=tmp_path / "cache" / "rivet",
        state_root=tmp_path / "state" / "transactions",
        evidence_root=tmp_path / "state" / "evidence",
    )

    with pytest.raises(
        TransactionError,
        match="项目验证配置必须由 Git 跟踪",
    ) as caught:
        await manager.create(
            specification,
            confirmed=True,
            transaction_id="tx_untracked_project_config",
        )

    assert caught.value.code == "transaction.project_config_untracked"
    assert not (tmp_path / "state" / "transactions").exists()
    assert not tuple((tmp_path / "cache").rglob("tx_untracked_project_config"))
    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_call",
    (2, 3),
    ids=("acceptance-hash-write", "frozen-record-write"),
)
async def test_frozen_transaction_publish_failure_is_invisible_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_call: int,
) -> None:
    """任一 staging 文件写入失败都不能暴露半个事务或创建 Worktree。"""
    repository = initialize_repository(tmp_path)
    scope = ResourceScope("transaction.frozen.publish.failure")
    state_root = tmp_path / "state" / "transactions"
    cache_root = tmp_path / "cache" / "rivet"
    manager = TransactionManager(
        repository,
        scope=scope,
        cache_root=cache_root,
        state_root=state_root,
        evidence_root=tmp_path / "state" / "evidence",
    )
    specification = acceptance_spec(acceptance_id="acceptance_atomic_publish_failure")
    base_commit = run_git(repository, "rev-parse", "HEAD").strip()
    transaction_id = "tx_atomic_publish_failure"
    final_directory = state_root / transaction_id
    original_write = TransactionStore._atomic_write  # pyright: ignore[reportPrivateUsage]
    calls = 0

    def fail_selected_staged_file(
        path: Path,
        content: bytes,
        *,
        mode: int,
    ) -> None:
        nonlocal calls
        calls += 1
        assert not final_directory.exists()
        if calls == failure_call:
            raise OSError("injected frozen publication failure")
        original_write(path, content, mode=mode)

    monkeypatch.setattr(
        TransactionStore,
        "_atomic_write",
        staticmethod(fail_selected_staged_file),
    )

    with pytest.raises(TransactionError, match="原子发布") as caught:
        await manager.create(
            specification,
            confirmed=True,
            transaction_id=transaction_id,
            expected_base_commit=base_commit,
        )

    assert caught.value.code == "transaction.frozen_publish_failed"
    assert calls == failure_call
    assert not final_directory.exists()
    assert not tuple(state_root.glob(".*.publish-*"))
    assert not tuple(state_root.rglob("acceptance_spec.json"))
    assert not tuple(cache_root.rglob(transaction_id))
    assert transaction_id not in run_git(repository, "worktree", "list", "--porcelain")
    assert TransactionStore(state_root).record_directories() == ()
    scope.assert_empty()
    await scope.close()


def test_prepare_removes_only_crash_stale_frozen_publication(tmp_path: Path) -> None:
    """下一次启动清理崩溃遗留 staging，但不触碰无关隐藏目录。"""
    state_root = tmp_path / "state" / "transactions"
    staging = state_root / ".tx_crash_stale.publish-deadbeef"
    staging.mkdir(parents=True)
    (staging / "acceptance_spec.json").write_text(
        '{"partial":true',
        encoding="utf-8",
    )
    unrelated = state_root / ".unrelated-state"
    unrelated.mkdir()

    store = TransactionStore(
        state_root,
        evidence_root=tmp_path / "state" / "evidence",
    )
    store.prepare()

    assert not staging.exists()
    assert unrelated.is_dir()
    assert store.record_directories() == (unrelated,)
