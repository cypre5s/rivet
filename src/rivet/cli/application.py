"""统一正式 CLI 的配置、输出、错误分类和命令分发。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from argparse import Namespace
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rivet.cli.errors import (
    CliConfigurationError,
    CliError,
    CliSecurityError,
    CliVerificationError,
)
from rivet.cli.exit_codes import ExitCode
from rivet.cli.parser import build_internal_parser, build_parser

if TYPE_CHECKING:
    from rivet.cli.config import ConfigOverrides, ResolvedConfig
    from rivet.storage.sessions import SessionCheckpoint, SessionStatus
    from rivet.verify.detector import EvidenceReadiness, ProjectDetection


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    """解析、执行并把所有普通失败转换为稳定退出码。"""
    selected_argv = tuple(sys.argv[1:] if argv is None else argv)
    internal = bool(selected_argv and selected_argv[0] == "internal")
    parser = build_internal_parser() if internal else build_parser()
    arguments = parser.parse_args(selected_argv[1:] if internal else selected_argv)
    selected_environment = os.environ if environment is None else environment
    debug = cast(bool, arguments.debug)
    try:
        command = cast(str | None, arguments.command)
        if command is None:
            if cast(bool, arguments.headless):
                parser.print_help()
                return int(ExitCode.SUCCESS)
            return _launch_tui(_repository(arguments))
        if command == "init":
            return _initialize(arguments)
        repository = _repository(arguments)
        if command == "internal":
            return asyncio.run(_worker(arguments, repository))
        from rivet.cli.config import load_config

        config = load_config(
            repository,
            environment=selected_environment,
            overrides=_overrides(arguments),
        )
        return _dispatch(
            arguments,
            repository=repository,
            config=config,
            environment=selected_environment,
        )
    except KeyboardInterrupt:
        print("[cli.cancelled] 用户取消", file=sys.stderr)
        return int(ExitCode.USER_CANCELLED)
    except CliError as error:
        if debug:
            raise
        _print_cli_error(error, json_output=_json_output(arguments))
        return int(error.exit_code)
    except Exception:
        if debug:
            raise
        print(
            "[cli.internal_error] Rivet 发生已脱敏内部错误\n"
            "下一步：使用 --debug 在本机查看堆栈或检查 Trace",
            file=sys.stderr,
        )
        return int(ExitCode.INTERNAL_ERROR)


def _dispatch(
    arguments: Namespace,
    *,
    repository: Path,
    config: ResolvedConfig,
    environment: Mapping[str, str],
) -> int:
    """把已校验配置交给唯一命令实现。"""
    command = cast(str, arguments.command)
    json_output = _json_output(arguments)
    if command == "config":
        payload = config.public_mapping()
        if cast(bool, arguments.show_sources):
            payload["sources"] = config.sources
        _print_payload(payload, json_output=json_output)
        return int(ExitCode.SUCCESS)
    if command == "modules":
        from rivet.cli.modules import run_module_command

        return asyncio.run(
            run_module_command(
                arguments,
                repository=repository,
                safe_mode=config.safe_mode,
                json_output=json_output,
            )
        )
    if command == "doctor":
        from rivet.cli.doctor import (
            DoctorSection,
            doctor_json,
            inspect_doctor,
            render_doctor,
        )

        section = cast(DoctorSection, arguments.section)
        report = inspect_doctor(repository, config, section=section)
        print(doctor_json(report) if json_output else render_doctor(report))
        return int(ExitCode.SUCCESS)
    if command == "clean":
        from rivet.storage.ownership import SafeCleaner
        from rivet.trace.paths import RuntimePaths

        report = SafeCleaner(RuntimePaths.for_repository(repository).cache_root).clean(
            dry_run=cast(bool, arguments.dry_run)
        )
        _print_payload(report.public_mapping(), json_output=json_output)
        return int(ExitCode.SUCCESS)
    if command == "read":
        return asyncio.run(
            _read_file(
                arguments,
                repository,
                safe_mode=config.safe_mode,
                json_output=json_output,
            )
        )
    if command == "trace":
        from rivet.trace.cli import run_trace_command

        return run_trace_command(
            repository=repository,
            run_id=cast(str | None, arguments.run_id),
            json_output=json_output,
        )
    if command == "export":
        from rivet.export.service import ExportError, ExportService

        try:
            result = ExportService(repository, environment=environment).export(
                cast(str, arguments.kind),
                cast(Path | None, arguments.path),
            )
        except ExportError as error:
            raise CliConfigurationError(
                error.code,
                error.summary,
                "选择有效来源和仓库内尚不存在的目标路径",
            ) from error
        _print_payload(
            {
                "kind": result.kind,
                "path": str(result.path),
                "sha256": result.sha256,
                "source_id": result.source_id,
            },
            json_output=json_output,
        )
        return int(ExitCode.SUCCESS)
    if command == "resume":
        return asyncio.run(
            _resume(
                arguments,
                repository,
                config=config,
                environment=environment,
                json_output=json_output,
            )
        )
    if command == "benchmark":
        return _benchmark(arguments, repository)
    if command in {"ask", "plan", "fix"}:
        preflight_detection = None
        if command == "fix" and not cast(
            bool, getattr(arguments, "candidate_only", False)
        ):
            preflight_detection = _require_evidence_readiness(repository)
        _require_credential(config)
        from rivet.cli.model_commands import run_model_command

        return asyncio.run(
            run_model_command(
                arguments,
                repository=repository,
                config=config,
                environment=environment,
                json_output=json_output,
                preflight_detection=preflight_detection,
            )
        )
    if command in {"verify", "diff", "apply", "abort"}:
        from rivet.cli.transaction_commands import run_transaction_command

        return asyncio.run(
            run_transaction_command(
                arguments,
                repository=repository,
                json_output=json_output,
                safe_mode=config.safe_mode,
            )
        )
    raise CliConfigurationError(
        "cli.command_unknown",
        "命令未注册",
        "运行 rivet --help 查看正式命令",
    )


def _initialize(arguments: Namespace) -> int:
    """只读检测项目，并只在显式确认后写入无凭据配置。"""
    from rivet.storage.git_exclude import configure_runtime_excludes
    from rivet.verify.detector import ProjectDetector, evidence_readiness
    from rivet.verify.errors import VerificationError

    path_argument = cast(Path | None, arguments.path)
    repository_argument = cast(Path, arguments.repository)
    candidate = path_argument or repository_argument
    if candidate.is_symlink() or not candidate.is_dir():
        raise CliConfigurationError(
            "init.path_invalid",
            "初始化目标必须是已存在普通目录且不能是符号链接",
            "创建目录后重试 rivet init",
        )
    target = candidate.resolve(strict=True)
    try:
        detection = ProjectDetector().detect(target)
    except VerificationError as error:
        raise CliConfigurationError(
            error.code,
            error.summary,
            "修复现有 .rivet/project.toml 后重试",
        ) from error
    readiness = evidence_readiness(detection)
    runtime_root = target / ".rivet"
    if runtime_root.is_symlink():
        raise CliConfigurationError(
            "init.runtime_symlink",
            ".rivet 不得是符号链接",
            "移除不受信任链接后重试",
        )
    config_path = runtime_root / "project.toml"
    if config_path.exists():
        if config_path.is_symlink() or not config_path.is_file():
            raise CliConfigurationError(
                "init.config_invalid",
                "项目配置路径不是受控普通文件",
                "修复路径后重试",
            )
        if cast(bool, arguments.yes):
            _configure_project_excludes(target, configure_runtime_excludes)
        _print_payload(
            _init_payload(
                config_path=config_path,
                created=False,
                confirmed=True,
                readiness=readiness,
            ),
            json_output=_json_output(arguments),
        )
        return int(ExitCode.SUCCESS)
    if not cast(bool, arguments.yes):
        _print_payload(
            _init_payload(
                config_path=config_path,
                created=False,
                confirmed=False,
                readiness=readiness,
            ),
            json_output=_json_output(arguments),
        )
        return int(ExitCode.SUCCESS)
    _configure_project_excludes(target, configure_runtime_excludes)
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path.write_text(_project_config_text(detection), encoding="utf-8")
    config_path.chmod(0o600)
    created_readiness = evidence_readiness(ProjectDetector().detect(target))
    _print_payload(
        _init_payload(
            config_path=config_path,
            created=True,
            confirmed=True,
            readiness=created_readiness,
        ),
        json_output=_json_output(arguments),
    )
    return int(ExitCode.SUCCESS)


def _configure_project_excludes(
    target: Path,
    configure: Callable[[Path], bool],
) -> None:
    """只在确认写配置的路径更新 Git 私有 exclude。"""
    try:
        configure(target)
    except ValueError as error:
        raise CliConfigurationError(
            "init.git_exclude_invalid",
            "Git 本地 exclude 无法安全配置",
            "检查 .git/info/exclude 后重试",
        ) from error


def _require_evidence_readiness(repository: Path) -> ProjectDetection:
    """在凭据检查和模型费用之前拒绝没有独立验收的 FIX。"""
    from rivet.verify.detector import ProjectDetector, evidence_readiness
    from rivet.verify.errors import VerificationError

    try:
        detection = ProjectDetector().detect(repository)
    except VerificationError as error:
        raise CliConfigurationError(
            error.code,
            error.summary,
            "修复 .rivet/project.toml 后重新运行 FIX",
        ) from error
    readiness = evidence_readiness(detection)
    if not readiness.ready:
        raise CliVerificationError(
            "verification.acceptance_not_ready",
            f"FIX 尚无独立验收门禁：{readiness.reason}",
            f"{readiness.next_action}；或显式使用 --candidate-only 只生成不可 Apply 的候选",
        )
    return detection


def _project_config_text(detection: ProjectDetection) -> str:
    """把未执行候选写成严格 argv；独立 acceptance 永远不自动推断。"""
    grouped: dict[str, list[list[str]]] = {
        "targeted": [],
        "related": [],
        "regression": [],
        "static": [],
    }
    for candidate in detection.candidates:
        if candidate.category in grouped:
            grouped[candidate.category].append(list(candidate.argv))
    lines = [
        "schema_version = 1",
        "",
        "[rivet]",
        "safe_mode = false",
        "",
        "[verification]",
        "# 必须由用户提供独立于模型输出的行为验收 argv。",
        "acceptance = []",
    ]
    for name in ("targeted", "related", "regression", "static"):
        encoded = json.dumps(grouped[name], ensure_ascii=False, separators=(",", ":"))
        lines.append(f"{name} = {encoded}")
    return "\n".join(lines) + "\n"


def _init_payload(
    *,
    config_path: Path,
    created: bool,
    confirmed: bool,
    readiness: EvidenceReadiness,
) -> dict[str, object]:
    """返回 TUI/headless 共用的无秘密初始化检测结果。"""
    return {
        "acceptance_ready": readiness.ready,
        "config_path": str(config_path),
        "confirmed": confirmed,
        "created": created,
        "detected_kinds": [kind.value for kind in readiness.kinds],
        "next_action": (
            readiness.next_action
            if confirmed
            else "检测结果尚未写入；审查候选后运行 rivet init --yes"
        ),
        "reason": readiness.reason,
        "suggestions": [
            {
                "argv": list(candidate.argv),
                "category": candidate.category,
                "kind": candidate.kind.value,
                "reason": candidate.reason,
            }
            for candidate in readiness.suggestions
        ],
    }


async def _read_file(
    arguments: Namespace,
    repository: Path,
    *,
    safe_mode: bool,
    json_output: bool,
) -> int:
    """调用统一 ReaderService 并保证资源域归零。"""
    from rivet.cli.runtime import create_cli_kernel, shutdown_cli_kernel
    from rivet.contracts.readers import ReaderRequest, ReaderStatus
    from rivet.kernel.errors import (
        KernelError,
        ModuleUnavailableError,
        SafeModeViolationError,
    )
    from rivet.kernel.module_runtime import CapabilityLease
    from rivet.modules.capabilities import ReaderCapability
    from rivet.tools.paths import WorkspaceBoundary

    source_argument = cast(Path, arguments.file)
    source = (
        source_argument.resolve(strict=True)
        if source_argument.is_absolute()
        else (repository / source_argument).resolve(strict=True)
    )
    try:
        source_path = source.relative_to(repository).as_posix()
    except ValueError as error:
        raise CliConfigurationError(
            "read.path_outside_repository",
            "读取路径不属于授权仓库",
            "选择仓库内文件后重试",
        ) from error
    boundary = WorkspaceBoundary(repository)
    kernel = create_cli_kernel(repository, safe_mode=safe_mode)
    leases: list[CapabilityLease[object]] = []
    try:
        await kernel.start()
        detection_lease = await kernel.acquire("reader.detect")
        leases.append(detection_lease)
        detector = cast(ReaderCapability, detection_lease.capability)
        inspection = detector.detect(
            boundary.resolve_repository(source_path, require_file=True),
            source_path=source_path,
        )
        requested_capability = (
            "reader.transcription"
            if cast(bool, arguments.transcribe)
            and inspection.capability_id == "reader.media"
            else inspection.capability_id
        )
        lease = await kernel.acquire(requested_capability)
        leases.append(lease)
        reader = cast(ReaderCapability, lease.capability)
        result = await reader.read(
            ReaderRequest(
                source_path=source_path,
                timeout_seconds=cast(int, arguments.timeout),
                max_output_chars=cast(int, arguments.max_output_chars),
                max_ocr_pages=cast(int, arguments.max_ocr_pages),
                max_image_pixels=cast(int, arguments.max_image_pixels),
                max_video_frames=cast(int, arguments.frames),
                max_audio_duration=cast(int, arguments.max_audio_duration),
                enable_ocr=cast(bool, arguments.ocr),
                enable_transcription=cast(bool, arguments.transcribe),
            )
        )
    except SafeModeViolationError as error:
        raise CliSecurityError(
            "module.safe_mode_denied",
            "Safe Mode 不允许激活该 Reader 模块",
            "改用受支持的基础格式，或审查配置后关闭 Safe Mode",
        ) from error
    except ModuleUnavailableError as error:
        missing = ", ".join(error.missing_components) or error.availability
        raise CliConfigurationError(
            "module.reader_unavailable",
            f"Reader 模块缺少激活前提：{missing}",
            error.suggested_action or "运行 rivet modules 和 rivet doctor 检查能力",
        ) from error
    except KernelError as error:
        raise CliConfigurationError(
            "module.reader_unavailable",
            "Reader 模块无法安全激活",
            "运行 rivet modules 和 rivet doctor 检查模块状态",
        ) from error
    finally:
        await shutdown_cli_kernel(kernel, leases)
    if json_output:
        print(result.model_dump_json())
    else:
        print(result.content, end="" if result.content.endswith("\n") else "\n")
    return (
        int(ExitCode.SUCCESS)
        if result.status is not ReaderStatus.FAILED
        else int(ExitCode.VERIFICATION_FAILED)
    )


async def _resume(
    arguments: Namespace,
    repository: Path,
    *,
    config: ResolvedConfig,
    environment: Mapping[str, str],
    json_output: bool,
) -> int:
    """续跑安全 Agent 阶段，并拒绝自动重放结果未知的工具调用。"""
    from rivet.contracts.tools import SideEffectClass
    from rivet.storage.sessions import (
        SessionStage,
        SessionStatus,
        SessionStore,
        ToolRecoveryStatus,
    )
    from rivet.trace.paths import RuntimePaths

    session_id = cast(str, arguments.session_id)
    try:
        checkpoint = SessionStore(repository).resume(session_id)
    except KeyError as error:
        raise CliConfigurationError(
            "resume.session_missing",
            "会话 checkpoint 不存在",
            "使用已保存的 session_id 或查看 Trace",
        ) from error
    except ValueError as error:
        raise CliConfigurationError(
            "resume.checkpoint_invalid",
            "会话 checkpoint 无法验证",
            "保留现场并检查本地状态完整性",
        ) from error
    resumable_statuses = {
        SessionStatus.INTERRUPTED,
        SessionStatus.CANCELLED,
        SessionStatus.FAILED,
    }
    if (
        checkpoint.stage is SessionStage.AGENT_LOOP
        and checkpoint.status in resumable_statuses
        and checkpoint.model is not None
        and checkpoint.messages
    ):
        unresolved_calls = _unresolved_tool_calls(checkpoint)
        unsafe_pending = tuple(
            tool
            for tool in checkpoint.pending_tools
            if tool.status is ToolRecoveryStatus.RUNNING
            or (
                tool.status is ToolRecoveryStatus.UNKNOWN
                and not (
                    tool.side_effect_class is SideEffectClass.READ_ONLY
                    and tool.next_action == "RETRY"
                )
            )
        )
        if unsafe_pending or unresolved_calls:
            raise CliConfigurationError(
                "resume.tool_outcome_unknown",
                "会话含结果未知的工具调用，不能自动重放",
                "检查 Trace 和事务 Diff 后 abort，或重新发起明确任务",
            )
        _require_credential(config)
        from rivet.cli.model_commands import run_model_command

        return await run_model_command(
            arguments,
            repository=repository,
            config=config,
            environment=environment,
            json_output=json_output,
            resume_checkpoint=checkpoint,
        )
    if (
        checkpoint.stage is SessionStage.PATCH_FINALIZATION
        and checkpoint.status
        in resumable_statuses
        | {SessionStatus.RUNNING, SessionStatus.READY_FOR_VERIFICATION}
        and checkpoint.transaction_id is not None
    ):
        from rivet.cli.model_commands import run_model_command

        return await run_model_command(
            arguments,
            repository=repository,
            config=config,
            environment=environment,
            json_output=json_output,
            resume_checkpoint=checkpoint,
        )
    if (
        checkpoint.stage is SessionStage.VERIFICATION
        and checkpoint.status in resumable_statuses
        and checkpoint.transaction_id is not None
    ):
        from rivet.cli.transaction_commands import run_transaction_command

        exit_code = await run_transaction_command(
            Namespace(
                command="verify",
                transaction_id=checkpoint.transaction_id,
            ),
            repository=repository,
            json_output=json_output,
            safe_mode=config.safe_mode,
        )
        SessionStore(repository).save(
            checkpoint.model_copy(
                update={
                    "stage": SessionStage.TERMINAL,
                    "status": _verification_session_status(
                        repository,
                        checkpoint.transaction_id,
                    ),
                }
            )
        )
        return exit_code
    trace_status = "MISSING"
    trace_event_count = 0
    from rivet.trace.errors import TraceError
    from rivet.trace.replay import TraceReplayer

    events_path = RuntimePaths.for_repository(repository).events_path
    try:
        if events_path.is_file():
            replay = TraceReplayer(events_path).replay(checkpoint.run_id)
            trace_status = "REPLAYED"
            trace_event_count = replay.state.event_count
    except TraceError:
        trace_status = "INVALID"
    transaction_status: str | None = None
    transaction_recovery: str | None = None
    evidence_id: str | None = None
    verification_status: str | None = None
    if checkpoint.transaction_id is not None:
        from rivet.cli.errors import CliVerificationError
        from rivet.cli.runtime import create_cli_kernel, shutdown_cli_kernel
        from rivet.contracts.transactions import TransactionState
        from rivet.kernel.errors import KernelError
        from rivet.kernel.module_runtime import CapabilityLease
        from rivet.transaction.errors import TransactionError
        from rivet.transaction.manager import TransactionManager
        from rivet.transaction.store import TransactionStore

        kernel = create_cli_kernel(repository, safe_mode=config.safe_mode)
        leases: list[CapabilityLease[object]] = []
        try:
            await kernel.start()
            transaction_lease = await kernel.acquire("transaction.worktree")
            leases.append(transaction_lease)
            manager = cast(TransactionManager, transaction_lease.capability)
            store = TransactionStore(
                RuntimePaths.for_repository(repository).runtime_root / "transactions"
            )
            record = store.load_record(checkpoint.transaction_id)
            if record.current_patch_id is not None and record.evidence_id is not None:
                patch, _ = store.load_patch(
                    record.transaction_id,
                    record.current_patch_id,
                )
                verdict = store.verify_record_evidence(
                    record,
                    expected_patch_sha256=patch.patch_sha256,
                )
                evidence_id = verdict.evidence_id
                verification_status = verdict.status.value
            transaction_status = record.state.value
            if record.state in {
                TransactionState.APPLIED,
                TransactionState.ABORTED,
            }:
                transaction_recovery = "NOT_REQUIRED"
            else:
                await manager.recover(checkpoint.transaction_id)
                manager.suspend(checkpoint.transaction_id)
                transaction_recovery = "RECOVERED"
        except TransactionError as error:
            raise CliVerificationError(
                error.code,
                error.summary,
                "检查事务 Worktree 与记录后使用 diff 或 abort",
            ) from error
        except KernelError as error:
            raise CliVerificationError(
                "module.transaction_unavailable",
                "事务恢复能力无法安全激活",
                "运行 rivet modules 和 rivet doctor 检查能力策略",
            ) from error
        finally:
            await shutdown_cli_kernel(kernel, leases)
    payload: dict[str, object] = {
        "command": checkpoint.command,
        "evidence_id": evidence_id,
        "pending_tools": [
            {
                "next_action": tool.next_action,
                "status": tool.status.value,
                "tool_call_id": tool.tool_call_id,
                "tool_name": tool.tool_name,
            }
            for tool in checkpoint.pending_tools
        ],
        "provider_state_restored": checkpoint.provider_state is not None,
        "resumable": False,
        "run_id": checkpoint.run_id,
        "session_id": checkpoint.session_id,
        "status": checkpoint.status.value,
        "trace_event_count": trace_event_count,
        "trace_status": trace_status,
        "transaction_id": checkpoint.transaction_id,
        "transaction_recovery": transaction_recovery,
        "transaction_status": transaction_status,
        "verification_status": verification_status,
    }
    _print_payload(payload, json_output=json_output)
    return int(ExitCode.SUCCESS)


def _verification_session_status(
    repository: Path,
    transaction_id: str,
) -> SessionStatus:
    """把独立验证后的事务事实原样投影到 Session。"""
    from rivet.storage.sessions import SessionStatus
    from rivet.transaction.store import TransactionStore

    record = TransactionStore(repository / ".rivet" / "transactions").load_record(
        transaction_id
    )
    return SessionStatus(record.state.value)


def _unresolved_tool_calls(checkpoint: SessionCheckpoint) -> tuple[str, ...]:
    """从消息历史推导没有 ToolMessage 回执的调用，防止副作用重放。"""
    from rivet.contracts.messages import AssistantMessage, ToolMessage
    from rivet.contracts.tools import SideEffectClass
    from rivet.storage.sessions import ToolRecoveryStatus

    completed = {
        message.tool_call_id
        for message in checkpoint.messages
        if isinstance(message, ToolMessage)
    }
    recoverable = {
        tool.tool_call_id
        for tool in checkpoint.pending_tools
        if (
            tool.status
            in {
                ToolRecoveryStatus.PREPARED,
                ToolRecoveryStatus.AUTHORIZED,
            }
            or (
                tool.status in {ToolRecoveryStatus.COMPLETED, ToolRecoveryStatus.FAILED}
                and tool.result_text is not None
            )
            or (
                tool.status is ToolRecoveryStatus.UNKNOWN
                and tool.side_effect_class is SideEffectClass.READ_ONLY
                and tool.next_action == "RETRY"
            )
        )
    }
    return tuple(
        call.tool_call_id
        for message in checkpoint.messages
        if isinstance(message, AssistantMessage)
        for call in message.tool_calls
        if call.tool_call_id not in completed and call.tool_call_id not in recoverable
    )


def _benchmark(arguments: Namespace, repository: Path) -> int:
    """通过 argv 启动仓库内评测脚本，不使用 shell。"""
    script = Path(__file__).resolve().parents[3] / "scripts" / "run_benchmark.py"
    if not script.is_file():
        raise CliConfigurationError(
            "benchmark.assets_missing",
            "当前安装不包含评测资源",
            "在 Rivet 源码仓库运行 benchmark",
        )
    completed = subprocess.run(
        (sys.executable, str(script), "--suite", cast(str, arguments.suite)),
        cwd=repository,
        check=False,
    )
    return completed.returncode


async def _worker(arguments: Namespace, repository: Path) -> int:
    """只允许版本化 stdio Worker 内部入口。"""
    if cast(str | None, arguments.internal_command) != "worker" or not cast(
        bool, arguments.stdio
    ):
        raise CliConfigurationError(
            "ipc.worker_arguments_invalid",
            "internal worker 只支持 --stdio",
            "由 Rivet TUI 启动 Worker",
        )
    from rivet.ipc.worker import run_stdio_worker

    return await run_stdio_worker(repository)


def _launch_tui(repository: Path) -> int:
    """启动前台 TUI 并分类本地依赖问题。"""
    from rivet.tui_launcher import TuiLaunchError, launch_tui

    try:
        return launch_tui(repository)
    except TuiLaunchError as error:
        raise CliConfigurationError(
            "tui.unavailable",
            str(error),
            "修复 TUI 依赖或使用 rivet --headless",
        ) from error


def _require_credential(config: ResolvedConfig) -> None:
    """在创建网络客户端或运行状态前执行凭据存在性门禁。"""
    if not config.credential_configured:
        raise CliConfigurationError(
            "provider.api_key_missing",
            "缺少 DEEPSEEK_API_KEY 环境变量",
            "设置已轮换的新凭据后重试",
        )


def _repository(arguments: Namespace) -> Path:
    """解析普通目录且拒绝符号链接仓库根。"""
    candidate = cast(Path, arguments.repository)
    if candidate.is_symlink() or not candidate.is_dir():
        raise CliConfigurationError(
            "cli.repository_invalid",
            "仓库路径必须是已存在普通目录",
            "使用 --repository 指定受控目录",
        )
    return candidate.resolve(strict=True)


def _overrides(arguments: Namespace) -> ConfigOverrides:
    """只提取正式全局配置选项。"""
    from rivet.cli.config import ConfigOverrides

    return ConfigOverrides(
        model=cast(str | None, arguments.model),
        base_url=cast(str | None, arguments.base_url),
        max_rounds=cast(int | None, arguments.max_rounds),
        max_total_tokens=cast(int | None, arguments.max_total_tokens),
        max_cost_usd=cast(str | None, arguments.max_cost_usd),
        safe_mode=cast(bool | None, arguments.safe_mode),
    )


def _json_output(arguments: Namespace) -> bool:
    """兼容全局或子命令位置的 JSON 选项。"""
    return bool(getattr(arguments, "json_output", False))


def _print_payload(payload: Mapping[str, object], *, json_output: bool) -> None:
    """为机器和人类输出同一事实映射。"""
    if json_output:
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    for key, value in payload.items():
        print(f"{key}: {value}")


def _print_cli_error(error: CliError, *, json_output: bool) -> None:
    """输出分类错误且不包含原始异常、输入或凭据值。"""
    if json_output:
        print(
            json.dumps(
                {
                    "error": {
                        "code": error.code,
                        "next_action": error.next_action,
                        "summary": error.summary,
                    },
                    "schema_version": 1,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return
    print(
        f"[{error.code}] {error.summary}\n下一步：{error.next_action}",
        file=sys.stderr,
    )
