"""统一正式 CLI 的配置、输出、错误分类和命令分发。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from argparse import Namespace
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from rivet.cli.config import ConfigOverrides, ResolvedConfig, load_config
from rivet.cli.doctor import DoctorSection, doctor_json, inspect_doctor, render_doctor
from rivet.cli.errors import CliConfigurationError, CliError
from rivet.cli.exit_codes import ExitCode
from rivet.cli.modules import module_status_mapping
from rivet.cli.parser import build_internal_parser, build_parser
from rivet.storage.git_exclude import configure_runtime_excludes
from rivet.storage.ownership import SafeCleaner
from rivet.storage.sessions import SessionStore
from rivet.trace.paths import RuntimePaths

PROJECT_CONFIG = """schema_version = 1

[rivet]
safe_mode = false

[verification]
targeted = []
related = []
regression = []
static = []
"""


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
        _print_payload(module_status_mapping(), json_output=json_output)
        return int(ExitCode.SUCCESS)
    if command == "doctor":
        section = cast(DoctorSection, arguments.section)
        report = inspect_doctor(repository, config, section=section)
        print(doctor_json(report) if json_output else render_doctor(report))
        return int(ExitCode.SUCCESS)
    if command == "clean":
        report = SafeCleaner(RuntimePaths.for_repository(repository).cache_root).clean(
            dry_run=cast(bool, arguments.dry_run)
        )
        _print_payload(report.public_mapping(), json_output=json_output)
        return int(ExitCode.SUCCESS)
    if command == "read":
        return asyncio.run(_read_file(arguments, repository, json_output=json_output))
    if command == "trace":
        from rivet.trace.cli import run_trace_command

        return run_trace_command(
            repository=repository,
            run_id=cast(str | None, arguments.run_id),
            json_output=json_output,
        )
    if command == "resume":
        return asyncio.run(_resume(arguments, repository, json_output=json_output))
    if command == "benchmark":
        return _benchmark(arguments, repository)
    if command in {"ask", "plan", "fix"}:
        _require_credential(config)
        from rivet.cli.model_commands import run_model_command

        return asyncio.run(
            run_model_command(
                arguments,
                repository=repository,
                config=config,
                environment=environment,
                json_output=json_output,
            )
        )
    if command in {"verify", "diff", "apply", "abort"}:
        from rivet.cli.transaction_commands import run_transaction_command

        return asyncio.run(
            run_transaction_command(
                arguments,
                repository=repository,
                json_output=json_output,
            )
        )
    raise CliConfigurationError(
        "cli.command_unknown",
        "命令未注册",
        "运行 rivet --help 查看正式命令",
    )


def _initialize(arguments: Namespace) -> int:
    """在明确目录中创建最小、无凭据且可跟踪的项目配置。"""
    path_argument = cast(Path | None, arguments.path)
    repository_argument = cast(Path, arguments.repository)
    target = (path_argument or repository_argument).resolve(strict=False)
    if not target.is_dir():
        raise CliConfigurationError(
            "init.path_invalid",
            "初始化目标必须是已存在目录",
            "创建目录后重试 rivet init",
        )
    runtime_root = target / ".rivet"
    if runtime_root.is_symlink():
        raise CliConfigurationError(
            "init.runtime_symlink",
            ".rivet 不得是符号链接",
            "移除不受信任链接后重试",
        )
    config_path = runtime_root / "project.toml"
    try:
        configure_runtime_excludes(target)
    except ValueError as error:
        raise CliConfigurationError(
            "init.git_exclude_invalid",
            "Git 本地 exclude 无法安全配置",
            "检查 .git/info/exclude 后重试",
        ) from error
    if config_path.exists():
        if config_path.is_symlink() or not config_path.is_file():
            raise CliConfigurationError(
                "init.config_invalid",
                "项目配置路径不是受控普通文件",
                "修复路径后重试",
            )
        print(f"项目已初始化：{config_path}")
        return int(ExitCode.SUCCESS)
    runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path.write_text(PROJECT_CONFIG, encoding="utf-8")
    config_path.chmod(0o600)
    print(f"已创建项目配置：{config_path}")
    return int(ExitCode.SUCCESS)


async def _read_file(
    arguments: Namespace,
    repository: Path,
    *,
    json_output: bool,
) -> int:
    """调用统一 ReaderService 并保证资源域归零。"""
    from rivet.contracts.readers import ReaderRequest, ReaderStatus
    from rivet.kernel.resources import ResourceScope
    from rivet.readers.service import ReaderService

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
    scope = ResourceScope("reader.cli")
    try:
        result = await ReaderService(repository, scope=scope).read(
            ReaderRequest(source_path=source_path)
        )
    finally:
        await scope.close()
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
    json_output: bool,
) -> int:
    """恢复 checkpoint 事实但绝不自动重放 UNKNOWN 写操作。"""
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
    if checkpoint.transaction_id is not None:
        from rivet.cli.errors import CliVerificationError
        from rivet.contracts.transactions import TransactionState
        from rivet.kernel.resources import ResourceScope
        from rivet.transaction.errors import TransactionError
        from rivet.transaction.manager import TransactionManager
        from rivet.transaction.store import TransactionStore

        scope = ResourceScope("cli.resume")
        manager = TransactionManager(repository, scope=scope)
        try:
            await manager.inspect_repository()
            store = TransactionStore(
                RuntimePaths.for_repository(repository).runtime_root / "transactions"
            )
            record = store.load_record(checkpoint.transaction_id)
            if record.state in {
                TransactionState.APPLIED,
                TransactionState.ABORTED,
            }:
                transaction_status = record.state.value
            else:
                await manager.recover(checkpoint.transaction_id)
                manager.suspend(checkpoint.transaction_id)
                transaction_status = "RECOVERED"
        except TransactionError as error:
            raise CliVerificationError(
                error.code,
                error.summary,
                "检查事务 Worktree 与记录后使用 diff 或 abort",
            ) from error
        finally:
            await scope.close()
    payload: dict[str, object] = {
        "command": checkpoint.command,
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
        "run_id": checkpoint.run_id,
        "session_id": checkpoint.session_id,
        "status": checkpoint.status.value,
        "trace_event_count": trace_event_count,
        "trace_status": trace_status,
        "transaction_id": checkpoint.transaction_id,
        "transaction_status": transaction_status,
    }
    _print_payload(payload, json_output=json_output)
    return int(ExitCode.SUCCESS)


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
