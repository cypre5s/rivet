"""执行最小 diff/verify/apply/abort 事务命令。"""

from __future__ import annotations

import hashlib
import json
import uuid
from argparse import Namespace
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from rivet.cli.errors import (
    CliConfigurationError,
    CliSecurityError,
    CliVerificationError,
)
from rivet.cli.exit_codes import ExitCode
from rivet.cli.runtime import close_cli_runtime, start_cli_runtime
from rivet.contracts.transactions import TransactionRecord, TransactionState
from rivet.kernel.capability_demand import DemandContext
from rivet.kernel.errors import KernelError
from rivet.kernel.module_runtime import CapabilityLease
from rivet.modules.capabilities import VerificationCapability
from rivet.tools.executor import (
    SideEffectJournal,
    ToolExecutionContext,
    UnknownSideEffect,
)
from rivet.trace.paths import RuntimePaths
from rivet.trace.verification import VerificationTraceJournal
from rivet.transaction.errors import TransactionError
from rivet.transaction.manager import TransactionManager
from rivet.transaction.store import TransactionStore
from rivet.verify.errors import VerificationError
from rivet.verify.evidence_query import EvidenceQueryService


async def run_transaction_command(
    arguments: Namespace,
    *,
    repository: Path,
    environment: Mapping[str, str],
    json_output: bool,
) -> int:
    """按用户 Demand 激活所需能力；diff 不检查 Provider 或沙箱。"""
    command = cast(str, arguments.command)
    store = _store(repository, environment=environment)
    transaction_id = _resolve_transaction_id(
        store,
        cast(str | None, getattr(arguments, "transaction_id", None)),
    )
    runtime = await start_cli_runtime(repository, environment=environment)
    leases: list[CapabilityLease[object]] = []
    manager: TransactionManager | None = None
    suspended = False
    try:
        run_id = f"run_{uuid.uuid4().hex}"
        session_id = f"session_{uuid.uuid4().hex}"
        root = await runtime.kernel.begin_user_demand(
            f"transaction.{command}",
            reason=f"user requested rivet {command}",
            context=DemandContext(
                run_id=run_id,
                session_id=session_id,
                transaction_id=transaction_id,
            ),
            operation_id=f"transaction-command:{command}",
        )
        if command == "diff":
            record, content = _load_patch(store, transaction_id)
            _print_diff(record, content, json_output=json_output)
            return int(ExitCode.SUCCESS)

        transaction_lease = await runtime.kernel.acquire_required(
            "transaction.worktree",
            parent=root,
            reason=f"{command} requires transaction state",
            operation_id=f"transaction-command:{command}",
        )
        leases.append(transaction_lease)
        manager = cast(TransactionManager, transaction_lease.capability)
        checkpoint = SideEffectJournal(runtime.trace, builder=runtime.builder)
        unknown_side_effects = checkpoint.unknown_for_transaction(
            transaction_id=transaction_id
        )

        if command == "verify":
            if unknown_side_effects:
                raise _unknown_side_effect_error(unknown_side_effects)
            existing = store.load_record(transaction_id)
            if existing.state in {
                TransactionState.VERIFIED,
                TransactionState.REJECTED,
                TransactionState.INCONCLUSIVE,
                TransactionState.BLOCKED,
                TransactionState.CANCELLED,
            }:
                _verify_record_evidence(store, existing)
                _print_mapping(
                    cast(
                        dict[str, object],
                        EvidenceQueryService(store).detail(transaction_id),
                    ),
                    json_output=json_output,
                )
                return (
                    int(ExitCode.SUCCESS)
                    if existing.state is TransactionState.VERIFIED
                    else int(ExitCode.VERIFICATION_FAILED)
                )
            if existing.state not in {
                TransactionState.PATCHING,
                TransactionState.VERIFYING,
            }:
                raise CliVerificationError(
                    "verification.transaction_state_invalid",
                    "当前事务尚未形成可验证补丁",
                    "先完成 fix 或查看 rivet diff",
                )
            await manager.recover(transaction_id)
            if existing.state is TransactionState.PATCHING:
                await manager.begin_verification(transaction_id)
            verify_lease = await runtime.kernel.acquire_required(
                "verify.deterministic",
                parent=root,
                reason="user requested independent verification",
                operation_id=f"verify:{transaction_id}",
            )
            leases.append(verify_lease)
            verifier = cast(VerificationCapability, verify_lease.capability)
            verification_trace = VerificationTraceJournal(
                runtime.trace,
                builder=runtime.builder,
            )
            verification_event_id = await verification_trace.started(
                run_id=run_id,
                session_id=session_id,
                transaction_id=transaction_id,
                parent_event_id=verify_lease.demand_handle.event_id,
            )
            try:
                outcome = await verifier.verify(transaction_id)
            except BaseException as error:
                await verification_trace.failed(
                    run_id=run_id,
                    session_id=session_id,
                    transaction_id=transaction_id,
                    parent_event_id=verification_event_id,
                    error=error,
                )
                raise
            await verification_trace.completed(
                run_id=run_id,
                session_id=session_id,
                transaction_id=transaction_id,
                parent_event_id=verification_event_id,
                verdict=outcome.verdict,
                manifest_sha256=outcome.manifest_sha256,
            )
            manager.suspend(transaction_id)
            suspended = True
            payload = cast(
                dict[str, object],
                EvidenceQueryService(store).detail(transaction_id),
            )
            _print_mapping(payload, json_output=json_output)
            return (
                int(ExitCode.SUCCESS)
                if outcome.verdict.passed
                else int(ExitCode.VERIFICATION_FAILED)
            )

        existing = store.load_record(transaction_id)
        if command == "abort" and any(
            fact.operation == "apply" for fact in unknown_side_effects
        ):
            raise CliSecurityError(
                "transaction.apply_recovery_required",
                "事务存在状态未知的 apply，不能 abort",
                f"运行 rivet apply {transaction_id} 完成确定性恢复",
            )
        if existing.state not in {TransactionState.APPLIED, TransactionState.ABORTED}:
            await manager.recover(transaction_id)
        if command == "apply":
            unknown_non_apply = tuple(
                fact for fact in unknown_side_effects if fact.operation != "apply"
            )
            unknown_apply = tuple(
                fact for fact in unknown_side_effects if fact.operation == "apply"
            )
            if unknown_non_apply or len(unknown_apply) > 1:
                raise _unknown_side_effect_error(unknown_side_effects)
            recovering = unknown_apply[0] if unknown_apply else None
            operation_id = (
                recovering.operation_id
                if recovering is not None
                else f"apply_{uuid.uuid4().hex}"
            )
            arguments_sha256 = (
                recovering.arguments_sha256
                if recovering is not None
                else _arguments_sha256({"transaction_id": transaction_id})
            )
            execution_context = ToolExecutionContext(
                parent_demand=root,
                run_id=run_id,
                session_id=session_id,
                transaction_id=transaction_id,
            )
            if recovering is None:
                await checkpoint.operation_started(
                    operation_id=operation_id,
                    operation="apply",
                    arguments_sha256=arguments_sha256,
                    context=execution_context,
                    parent_event_id=root.event_id,
                )
            try:
                record = await manager.apply(transaction_id)
            except BaseException as error:
                await checkpoint.operation_failed(
                    operation_id=operation_id,
                    operation="apply",
                    arguments_sha256=arguments_sha256,
                    error=error,
                    context=execution_context,
                    parent_event_id=root.event_id,
                    originating_run_id=(
                        recovering.originating_run_id
                        if recovering is not None
                        else None
                    ),
                )
                raise
            await checkpoint.operation_succeeded(
                operation_id=operation_id,
                operation="apply",
                arguments_sha256=arguments_sha256,
                result=record.state.value,
                context=execution_context,
                parent_event_id=root.event_id,
                originating_run_id=(
                    recovering.originating_run_id if recovering is not None else None
                ),
            )
        elif command == "abort":
            record = await manager.abort(transaction_id)
        else:
            raise CliConfigurationError(
                "transaction.command_unknown",
                "事务命令未注册",
                "运行 rivet --help",
            )
        _print_record(record, json_output=json_output)
        return int(ExitCode.SUCCESS)
    except TransactionError as error:
        if error.code == "transaction.apply_recovery_required":
            raise CliSecurityError(
                error.code,
                error.summary,
                f"运行 rivet apply {transaction_id} 完成确定性恢复",
            ) from error
        if error.code in {
            "transaction.dirty_repository_rejected",
            "transaction.repository_drift",
            "transaction.patch_drift",
            "transaction.patch_bytes_changed",
        }:
            raise CliSecurityError(
                error.code,
                error.summary,
                "检查仓库状态与事务 Evidence 后再决定是否重试",
            ) from error
        raise CliVerificationError(
            error.code,
            error.summary,
            "运行 rivet diff 检查事务状态",
        ) from error
    except VerificationError as error:
        raise CliVerificationError(
            error.code,
            error.summary,
            "检查 Evidence 与冻结 AcceptanceSpec",
        ) from error
    except KernelError as error:
        raise CliVerificationError(
            "module.transaction_unavailable",
            "事务能力无法安全激活或关闭",
            "检查 Git、bubblewrap 和 XDG 状态目录",
        ) from error
    finally:
        try:
            if manager is not None and not suspended:
                try:
                    record = store.load_record(transaction_id)
                    if record.state not in {
                        TransactionState.APPLIED,
                        TransactionState.ABORTED,
                    }:
                        manager.suspend(transaction_id)
                except TransactionError as error:
                    if error.code not in {
                        "transaction.suspend_terminal",
                        "transaction.worktree_missing",
                        "transaction.suspend_unregistered",
                    }:
                        raise
        finally:
            await close_cli_runtime(runtime, leases)


def transaction_store(
    repository: Path,
    *,
    environment: Mapping[str, str],
) -> TransactionStore:
    """供 CLI 与 IPC 共用的 XDG TransactionStore。"""
    return _store(repository, environment=environment)


def _store(
    repository: Path,
    *,
    environment: Mapping[str, str],
) -> TransactionStore:
    paths = RuntimePaths.for_repository(repository, environment=environment)
    return TransactionStore(
        paths.transactions_root,
        evidence_root=paths.evidence_root,
    )


def _resolve_transaction_id(
    store: TransactionStore,
    transaction_id: str | None,
) -> str:
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


def _print_diff(
    record: TransactionRecord,
    content: bytes,
    *,
    json_output: bool,
) -> None:
    visible = content.decode("utf-8", errors="replace")
    if json_output:
        _print_json(
            {
                "diff": visible,
                "state": record.state.value,
                "transaction_id": record.transaction_id,
            }
        )
    else:
        print(visible, end="")


def _print_record(record: TransactionRecord, *, json_output: bool) -> None:
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
    if json_output:
        _print_json(payload)
        return
    for key, value in payload.items():
        print(f"{key}: {value}")


def _print_json(payload: object) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _arguments_sha256(arguments: Mapping[str, object]) -> str:
    serialized = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(serialized).hexdigest()}"


def _unknown_side_effect_error(
    unknown_side_effects: Sequence[UnknownSideEffect],
) -> CliSecurityError:
    operations = ", ".join(sorted({fact.operation for fact in unknown_side_effects}))
    return CliSecurityError(
        "transaction.side_effect_unknown",
        f"事务存在崩溃后状态未知的副作用：{operations}",
        "只允许重放具备 ApplyIntent 恢复协议的单个 apply；其他情况请审计 Trace 后 abort",
    )
