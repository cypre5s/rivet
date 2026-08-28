"""构造正式命令、全局选项和严格 argparse 输入边界。"""

from __future__ import annotations

from argparse import SUPPRESS, ArgumentParser
from importlib.metadata import version
from pathlib import Path

OFFICIAL_COMMANDS = (
    "init",
    "ask",
    "read",
    "plan",
    "fix",
    "verify",
    "diff",
    "apply",
    "abort",
    "trace",
    "resume",
    "modules",
    "doctor",
    "benchmark",
    "config",
    "clean",
)


def build_parser() -> ArgumentParser:
    """返回同时支持 TUI 默认入口和全部 headless 子命令的解析器。"""
    parser = ArgumentParser(
        prog="rivet",
        description="可靠、可审计的本地编程智能体",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {version('rivet')}"
    )
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="不启动 TUI，仅使用命令行接口",
    )
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--max-rounds", type=int)
    parser.add_argument("--max-total-tokens", type=int)
    parser.add_argument("--max-cost-usd")
    parser.add_argument("--safe-mode", action="store_true", default=None)
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="初始化项目配置")
    init_parser.add_argument("path", nargs="?", type=Path)
    _add_runtime_aliases(init_parser)

    ask_parser = subparsers.add_parser("ask", help="只读询问仓库")
    ask_parser.add_argument("query")
    _add_runtime_aliases(ask_parser)

    read_parser = subparsers.add_parser("read", help="安全读取本地文件")
    read_parser.add_argument("file", type=Path)
    _add_runtime_aliases(read_parser)

    plan_parser = subparsers.add_parser("plan", help="生成可验证计划")
    plan_parser.add_argument("task")
    _add_runtime_aliases(plan_parser)

    fix_parser = subparsers.add_parser("fix", help="在隔离事务中修复代码")
    fix_parser.add_argument("task")
    fix_parser.add_argument("--yes", action="store_true")
    fix_parser.add_argument(
        "--dirty-policy",
        choices=("reject", "snapshot"),
        default="reject",
    )
    _add_runtime_aliases(fix_parser)

    for command, description in (
        ("verify", "验证事务补丁"),
        ("diff", "查看事务补丁"),
    ):
        command_parser = subparsers.add_parser(command, help=description)
        command_parser.add_argument("transaction_id", nargs="?")
        _add_runtime_aliases(command_parser)

    apply_parser = subparsers.add_parser("apply", help="显式应用已验证补丁")
    apply_parser.add_argument("transaction_id")
    _add_runtime_aliases(apply_parser)

    abort_parser = subparsers.add_parser("abort", help="终止并清理事务")
    abort_parser.add_argument("transaction_id")
    _add_runtime_aliases(abort_parser)

    trace_parser = subparsers.add_parser("trace", help="回放结构化执行轨迹")
    trace_parser.add_argument("run_id", nargs="?")
    _add_runtime_aliases(trace_parser)

    resume_parser = subparsers.add_parser("resume", help="恢复持久化会话")
    resume_parser.add_argument("session_id")
    resume_parser.add_argument(
        "--yes",
        action="store_true",
        help="重新批准中断 fix 的事务写入与验证",
    )
    _add_runtime_aliases(resume_parser)

    modules_parser = subparsers.add_parser("modules", help="查看或控制按需模块")
    _add_runtime_aliases(modules_parser)
    module_subparsers = modules_parser.add_subparsers(dest="module_command")
    module_list_parser = module_subparsers.add_parser("list", help="列出模块状态")
    _add_runtime_aliases(module_list_parser)
    module_show_parser = module_subparsers.add_parser("show", help="查看模块详情")
    module_show_parser.add_argument("module_id")
    _add_runtime_aliases(module_show_parser)
    module_enable_parser = module_subparsers.add_parser("enable", help="持久化启用模块")
    module_enable_parser.add_argument("module_id")
    module_enable_parser.add_argument("--with-dependencies", action="store_true")
    _add_runtime_aliases(module_enable_parser)
    module_wake_parser = module_subparsers.add_parser("wake", help="唤醒模块")
    module_wake_parser.add_argument("module_id")
    module_wake_parser.add_argument("--with-dependencies", action="store_true")
    _add_runtime_aliases(module_wake_parser)
    for operation, description in (
        ("sleep", "安全休眠模块"),
        ("disable", "安全休眠并持久化禁用模块"),
    ):
        lifecycle_parser = module_subparsers.add_parser(operation, help=description)
        lifecycle_parser.add_argument("module_id")
        lifecycle_parser.add_argument("--cascade", action="store_true")
        lifecycle_parser.add_argument("--wait", action="store_true")
        lifecycle_parser.add_argument("--timeout", type=float, default=30.0)
        lifecycle_parser.add_argument("--yes", action="store_true")
        _add_runtime_aliases(lifecycle_parser)

    doctor_parser = subparsers.add_parser("doctor", help="检测本地运行依赖")
    doctor_parser.add_argument(
        "--section",
        choices=("all", "core", "tui", "sandbox", "readers", "lsp", "provider"),
        default="all",
    )
    _add_runtime_aliases(doctor_parser)

    benchmark_parser = subparsers.add_parser("benchmark", help="运行本地评测套件")
    benchmark_parser.add_argument(
        "--suite",
        choices=(
            "context-smoke",
            "context-full",
            "security",
            "functional",
            "faults",
            "performance",
            "all",
        ),
        default="context-smoke",
    )
    _add_runtime_aliases(benchmark_parser)

    config_parser = subparsers.add_parser("config", help="查看非秘密有效配置")
    config_parser.add_argument("--show-sources", action="store_true")
    _add_runtime_aliases(config_parser)

    clean_parser = subparsers.add_parser("clean", help="清理 Rivet 自有临时资源")
    clean_parser.add_argument("--dry-run", action="store_true")
    _add_runtime_aliases(clean_parser)

    return parser


def build_internal_parser() -> ArgumentParser:
    """构造不进入公开帮助的版本化 Worker 入口。"""
    parser = ArgumentParser(prog="rivet internal")
    parser.set_defaults(
        command="internal",
        debug=False,
        headless=True,
        json_output=False,
    )
    subparsers = parser.add_subparsers(dest="internal_command", required=True)
    worker_parser = subparsers.add_parser("worker")
    worker_parser.add_argument("--stdio", action="store_true", required=True)
    worker_parser.add_argument("--repository", type=Path, default=Path.cwd())
    return parser


def _add_runtime_aliases(parser: ArgumentParser) -> None:
    """兼容把仓库和 JSON 选项放在子命令后的常见写法。"""
    parser.add_argument("--repository", type=Path, default=SUPPRESS)
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=SUPPRESS,
    )
