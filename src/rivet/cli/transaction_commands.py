"""把 diff、verify、apply 与 abort 接到持久化事务事实。"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from typing import cast

from rivet.cli.errors import (
    CliConfigurationError,
    CliSecurityError,
    CliVerificationError,
)
from rivet.cli.exit_codes import ExitCode
from rivet.cli.runtime import create_cli_kernel, module_scope, shutdown_cli_kernel
from rivet.contracts.transactions import TransactionRecord, TransactionState
from rivet.kernel.errors import KernelError, SafeModeViolationError
from rivet.kernel.module_runtime import ModuleLease
from rivet.trace.paths import RuntimePaths
from rivet.transaction.errors import TransactionError
from rivet.transaction.store import TransactionStore
from rivet.verify.errors import VerificationError


async def run_transaction_command(
    arguments: Namespace,
    *,
    repository: Path,
    json_output: bool,
    safe_mode: bool = False,
) -> int:
    """执行一个事务命令并保证临时资源按状态清理或移交。"""
    kernel = create_cli_kernel(repository, safe_mode=safe_mode)
    leases: list[ModuleLease] = []
    manager = None
    suspended = False
    registered_transaction_id: str | None = None
    try:
        await kernel.start()
        transaction_lease = await kernel.acquire_lease("transaction.worktree")
        leases.append(transaction_lease)
        scope = module_scope(transaction_lease.instance)
        from rivet.transaction.manager import TransactionManager

        manager = TransactionManager(repository, scope=scope)
        await manager.inspect_repository()
        store = _store(repository)
        transaction_id = _resolve_transaction_id(
            store,
            cast(str | None, getattr(arguments, "transaction_id", None)),
        )
        command = cast(str, arguments.command)
        if command == "diff":
            record, content = _load_patch(store, transaction_id)
            if json_output:
                _print_json(
                    {
                        "diff": content.decode("utf-8", errors="replace"),
                        "state": record.state.value,
                        "transaction_id": record.transaction_id,
                    }
                )
            else:
                print(content.decode("utf-8", errors="replace"), end="")
            return int(ExitCode.SUCCESS)
        if command == "verify":
            verify_lease = await kernel.acquire_lease("verify.deterministic")
            leases.append(verify_lease)
            from rivet.verify.detector import ProjectDetector
            from rivet.verify.service import VerificationService

            record = store.load_record(transaction_id)
            if record.state is TransactionState.VERIFIED:
                _verify_record_evidence(store, record)
                _print_record(record, json_output=json_output)
                return int(ExitCode.SUCCESS)
            if record.state in {
                TransactionState.REJECTED,
                TransactionState.INCONCLUSIVE,
                TransactionState.BLOCKED,
                TransactionState.CANCELLED,
            }:
                _verify_record_evidence(store, record)
                _print_record(record, json_output=json_output)
                return int(ExitCode.VERIFICATION_FAILED)
            if record.state not in {
                TransactionState.PATCHING,
                TransactionState.VERIFYING,
            }:
                raise CliVerificationError(
                    "verification.transaction_state_invalid",
                    "当前事务尚未形成可验证补丁",
                    "先完成 fix 或查看 rivet diff",
                )
            await manager.recover(transaction_id)
            registered_transaction_id = transaction_id
            if record.state is TransactionState.PATCHING:
                await manager.begin_verification(transaction_id)
            detection = ProjectDetector().detect(repository)
            outcome = await VerificationService(
                manager,
                scope=module_scope(verify_lease.instance),
                project_configuration=detection.configuration,
                configuration_confirmed=detection.configuration is not None,
            ).verify(transaction_id)
            manager.suspend(transaction_id)
            suspended = True
            payload: dict[str, object] = {
                "evidence_id": outcome.verdict.evidence_id,
                "manifest_sha256": outcome.manifest_sha256,
                "passed": outcome.verdict.passed,
                "status": outcome.verdict.status.value,
                "transaction_id": transaction_id,
            }
            _print_mapping(payload, json_output=json_output)
            return (
                int(ExitCode.SUCCESS)
                if outcome.verdict.passed
                else int(ExitCode.VERIFICATION_FAILED)
            )
        if command == "apply":
            record = await manager.apply(transaction_id)
            _print_record(record, json_output=json_output)
            return int(ExitCode.SUCCESS)
        if command == "abort":
            record = await manager.abort(transaction_id)
            _print_record(record, json_output=json_output)
            return int(ExitCode.SUCCESS)
        raise CliConfigurationError(
            "transaction.command_unknown",
            "事务命令未注册",
            "运行 rivet --help",
        )
    except TransactionError as error:
        if error.code in {
            "transaction.dirty_repository_rejected",
            "transaction.repository_drift",
            "transaction.patch_drift",
            "transaction.patch_bytes_changed",
        }:
            raise CliSecurityError(
                error.code,
                error.summary,
                "检查仓库状态和事务证据后再决定是否重试",
            ) from error
        raise CliVerificationError(
            error.code,
            error.summary,
            "运行 rivet diff/trace 检查事务状态",
        ) from error
    except VerificationError as error:
        raise CliVerificationError(
            error.code,
            error.summary,
            "检查 Evidence 和冻结验收条件",
        ) from error
    except SafeModeViolationError as error:
        raise CliSecurityError(
            "module.safe_mode_denied",
            "Safe Mode 不允许执行事务命令",
            "保持只读操作，或审查配置后关闭 Safe Mode",
        ) from error
    except KernelError as error:
        raise CliVerificationError(
            "module.transaction_unavailable",
            "事务模块无法安全激活或关闭",
            "运行 rivet modules 和 rivet doctor 检查模块状态",
        ) from error
    finally:
        try:
            if (
                manager is not None
                and not suspended
                and registered_transaction_id is not None
            ):
                try:
                    manager.suspend(registered_transaction_id)
                except TransactionError as error:
                    if error.code not in {
                        "transaction.suspend_terminal",
                        "transaction.worktree_missing",
                    }:
                        raise
        finally:
            await shutdown_cli_kernel(kernel, leases)


def _store(repository: Path) -> TransactionStore:
    """返回与 TransactionManager 默认路径完全一致的只读 Store。"""
    return TransactionStore(
        RuntimePaths.for_repository(repository).runtime_root / "transactions"
    )


def _resolve_transaction_id(
    store: TransactionStore,
    transaction_id: str | None,
) -> str:
    """显式 ID 优先；缺省时选择 updated_at 最新的有效事务。"""
    if transaction_id is not None:
        store.load_record(transaction_id)
        return transaction_id
    records: list[TransactionRecord] = []
    for directory in store.record_directories():
        try:
            records.append(store.load_record(directory.name))
        except TransactionError:
            continue
    if not records:
        raise CliConfigurationError(
            "transaction.none",
            "仓库没有可用事务",
            "先运行 rivet fix 或显式提供 TX_ID",
        )
    return max(
        records,
        key=lambda record: (record.updated_at, record.transaction_id),
    ).transaction_id


def _load_patch(
    store: TransactionStore,
    transaction_id: str,
) -> tuple[TransactionRecord, bytes]:
    """复核记录已绑定当前补丁后返回完整 binary diff。"""
    record = store.load_record(transaction_id)
    if record.current_patch_id is None:
        raise CliVerificationError(
            "transaction.patch_missing",
            "事务尚无已记录补丁",
            "继续 fix 或使用 rivet abort",
        )
    _, content = store.load_patch(transaction_id, record.current_patch_id)
    return record, content


def _verify_record_evidence(
    store: TransactionStore,
    record: TransactionRecord,
) -> None:
    """复核当前补丁与已判定 Evidence 的完整绑定。"""
    if record.current_patch_id is None:
        raise CliVerificationError(
            "transaction.patch_missing",
            "已判定事务缺少当前补丁",
            "保留现场并检查事务记录",
        )
    patch, _ = store.load_patch(record.transaction_id, record.current_patch_id)
    store.verify_record_evidence(
        record,
        expected_patch_sha256=patch.patch_sha256,
    )


def _print_record(record: TransactionRecord, *, json_output: bool) -> None:
    """展示不含仓库绝对路径和用户内容的事务状态。"""
    _print_mapping(
        {
            "evidence_id": record.evidence_id,
            "patch_id": record.current_patch_id,
            "state": record.state.value,
            "transaction_id": record.transaction_id,
        },
        json_output=json_output,
    )


def _print_mapping(payload: dict[str, object], *, json_output: bool) -> None:
    """同时支持稳定 JSON 与简洁人类输出。"""
    if json_output:
        _print_json(payload)
        return
    for key, value in payload.items():
        print(f"{key}: {value}")


def _print_json(payload: object) -> None:
    """输出稳定紧凑 JSON。"""
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
