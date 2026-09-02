"""统一极简 CLI 的分发、错误和 Evidence readiness 门禁。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from argparse import Namespace
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rivet.cli.errors import (
    CliConfigurationError,
    CliError,
    CliVerificationError,
)
from rivet.cli.exit_codes import ExitCode
from rivet.cli.parser import build_internal_parser, build_parser

if TYPE_CHECKING:
    from rivet.cli.config import ConfigOverrides, ResolvedConfig
    from rivet.verify.detector import EvidenceReadiness, ProjectDetection


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> int:
    """执行公开命令，并把普通失败转换成稳定退出码。"""
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
            "下一步：使用 --debug 在本机查看堆栈或检查 XDG Trace",
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
    command = cast(str, arguments.command)
    json_output = _json_output(arguments)
    if command in {"ask", "fix"}:
        detection = (
            _require_evidence_readiness(repository) if command == "fix" else None
        )
        _require_credential(config)
        from rivet.cli.model_commands import run_model_command

        return asyncio.run(
            run_model_command(
                arguments,
                repository=repository,
                config=config,
                environment=environment,
                json_output=json_output,
                preflight_detection=detection,
            )
        )
    if command in {"verify", "diff", "apply", "abort"}:
        from rivet.cli.transaction_commands import run_transaction_command

        return asyncio.run(
            run_transaction_command(
                arguments,
                repository=repository,
                environment=environment,
                json_output=json_output,
            )
        )
    raise CliConfigurationError(
        "cli.command_unknown",
        "命令未注册",
        "运行 rivet --help 查看正式命令",
    )


def _initialize(arguments: Namespace) -> int:
    """只读检测项目，并仅在显式确认后写 project.toml。"""
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
    config_directory = target / ".rivet"
    config_path = config_directory / "project.toml"
    if config_directory.is_symlink() or config_path.is_symlink():
        raise CliConfigurationError(
            "init.config_invalid",
            ".rivet/project.toml 不得经过符号链接",
            "移除不受信任链接后重试",
        )
    if config_path.exists():
        if not config_path.is_file():
            raise CliConfigurationError(
                "init.config_invalid",
                "项目配置路径不是普通文件",
                "修复路径后重试",
            )
        _print_payload(
            _init_payload(config_path, False, True, readiness),
            json_output=_json_output(arguments),
        )
        return int(ExitCode.SUCCESS)
    if not cast(bool, arguments.yes):
        _print_payload(
            _init_payload(config_path, False, False, readiness),
            json_output=_json_output(arguments),
        )
        return int(ExitCode.SUCCESS)
    config_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path.write_text(_project_config_text(detection), encoding="utf-8")
    config_path.chmod(0o600)
    created_readiness = evidence_readiness(ProjectDetector().detect(target))
    _print_payload(
        _init_payload(config_path, True, True, created_readiness),
        json_output=_json_output(arguments),
    )
    return int(ExitCode.SUCCESS)


def _require_evidence_readiness(repository: Path) -> ProjectDetection:
    """在检查凭据和产生模型费用前要求独立 acceptance。"""
    from rivet.verify.detector import ProjectDetector, evidence_readiness
    from rivet.verify.errors import VerificationError

    try:
        detection = ProjectDetector().detect(repository)
    except VerificationError as error:
        raise CliConfigurationError(
            error.code,
            error.summary,
            "修复 .rivet/project.toml 后重新运行 fix",
        ) from error
    readiness = evidence_readiness(detection)
    if not readiness.ready:
        raise CliVerificationError(
            "verification.acceptance_not_ready",
            f"FIX 尚无独立验收门禁：{readiness.reason}",
            readiness.next_action,
        )
    return detection


def _project_config_text(detection: ProjectDetection) -> str:
    """写入未执行候选；独立 acceptance 永远不自动推断。"""
    grouped: dict[str, list[list[str]]] = {"regression": [], "static": []}
    for candidate in detection.candidates:
        if candidate.category in grouped:
            grouped[candidate.category].append(list(candidate.argv))
    return "\n".join(
        (
            "schema_version = 1",
            "",
            "[rivet]",
            'model = "deepseek-v4-flash"',
            "",
            "[verification]",
            "# 必须由用户提供独立于模型输出的行为验收 argv。",
            "acceptance = []",
            "regression = "
            + json.dumps(
                grouped["regression"], ensure_ascii=False, separators=(",", ":")
            ),
            "static = "
            + json.dumps(grouped["static"], ensure_ascii=False, separators=(",", ":")),
            "",
        )
    )


def _init_payload(
    config_path: Path,
    created: bool,
    confirmed: bool,
    readiness: EvidenceReadiness,
) -> dict[str, object]:
    return {
        "acceptance_ready": readiness.ready,
        "config_path": str(config_path),
        "confirmed": confirmed,
        "created": created,
        "detected_kinds": [kind.value for kind in readiness.kinds],
        "next_action": (
            readiness.next_action
            if confirmed
            else "检测结果尚未写入；审查后运行 rivet init --yes"
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


async def _worker(arguments: Namespace, repository: Path) -> int:
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
    if not config.credential_configured:
        raise CliConfigurationError(
            "provider.api_key_missing",
            "缺少 DEEPSEEK_API_KEY 环境变量",
            "设置有效凭据后重试",
        )


def _repository(arguments: Namespace) -> Path:
    candidate = cast(Path, arguments.repository)
    if candidate.is_symlink() or not candidate.is_dir():
        raise CliConfigurationError(
            "cli.repository_invalid",
            "仓库路径必须是已存在普通目录",
            "使用 --repository 指定受控目录",
        )
    return candidate.resolve(strict=True)


def _overrides(arguments: Namespace) -> ConfigOverrides:
    from rivet.cli.config import ConfigOverrides

    return ConfigOverrides(
        model=cast(str | None, arguments.model),
        base_url=cast(str | None, arguments.base_url),
        max_rounds=cast(int | None, arguments.max_rounds),
        max_total_tokens=cast(int | None, arguments.max_total_tokens),
        max_cost_usd=cast(str | None, arguments.max_cost_usd),
    )


def _json_output(arguments: Namespace) -> bool:
    return bool(getattr(arguments, "json_output", False))


def _print_payload(payload: Mapping[str, object], *, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
        return
    for key, value in payload.items():
        print(f"{key}: {value}")


def _print_cli_error(error: CliError, *, json_output: bool) -> None:
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
