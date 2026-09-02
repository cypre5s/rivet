"""构造极简公开 CLI 与隐藏 IPC Worker 入口。"""

from __future__ import annotations

from argparse import SUPPRESS, ArgumentParser
from importlib.metadata import version
from pathlib import Path

OFFICIAL_COMMANDS = (
    "init",
    "ask",
    "fix",
    "diff",
    "verify",
    "apply",
    "abort",
)


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="rivet",
        description="Demand-driven、Evidence-gated 的本地 Coding Agent",
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
        help="不启动 TUI；未给子命令时显示帮助",
    )
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--max-rounds", type=int)
    parser.add_argument("--max-total-tokens", type=int)
    parser.add_argument("--max-cost-usd")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="建立独立 Evidence oracle")
    init_parser.add_argument("path", nargs="?", type=Path)
    init_parser.add_argument(
        "--yes",
        action="store_true",
        help="写入只读检测建议；acceptance 仍须由用户填写",
    )
    _add_runtime_aliases(init_parser)

    ask_parser = subparsers.add_parser("ask", help="只读询问仓库")
    ask_parser.add_argument("query")
    _add_runtime_aliases(ask_parser)

    fix_parser = subparsers.add_parser("fix", help="隔离修改并独立验证")
    fix_parser.add_argument("task")
    fix_parser.add_argument(
        "--yes",
        action="store_true",
        help="确认显示的 AcceptanceSpec 和限定写范围",
    )
    fix_parser.add_argument(
        "--allow-read",
        action="append",
        default=[],
        metavar="PATH",
        help="允许只读调查的现有仓库相对文件或目录；可重复",
    )
    fix_parser.add_argument(
        "--allow-write",
        action="append",
        default=[],
        metavar="PATH",
        help="允许修改的现有仓库相对文件或目录；可重复",
    )
    fix_parser.add_argument(
        "--allow-new",
        action="append",
        default=[],
        metavar="PATH",
        help="允许新建的仓库相对文件或目录；可重复",
    )
    fix_parser.add_argument(
        "--acceptance-sha256",
        help="确认前一次只读提案返回的 AcceptanceSpec 哈希",
    )
    fix_parser.add_argument(
        "--base-commit",
        help="确认前一次只读调查绑定的 Git 基线提交",
    )
    _add_runtime_aliases(fix_parser)

    for command, help_text in (
        ("diff", "查看隔离事务补丁"),
        ("verify", "重新独立验证事务"),
    ):
        child = subparsers.add_parser(command, help=help_text)
        child.add_argument("transaction_id", nargs="?")
        _add_runtime_aliases(child)

    for command, help_text in (
        ("apply", "显式应用 VERIFIED 补丁"),
        ("abort", "终止并清理事务"),
    ):
        child = subparsers.add_parser(command, help=help_text)
        child.add_argument("transaction_id")
        _add_runtime_aliases(child)
    return parser


def build_internal_parser() -> ArgumentParser:
    """构造不出现在公开帮助中的版本化 Worker 入口。"""
    parser = ArgumentParser(prog="rivet internal")
    parser.set_defaults(
        command="internal",
        debug=False,
        headless=True,
        json_output=False,
    )
    subparsers = parser.add_subparsers(dest="internal_command", required=True)
    worker = subparsers.add_parser("worker")
    worker.add_argument("--stdio", action="store_true", required=True)
    worker.add_argument("--repository", type=Path, default=Path.cwd())
    return parser


def _add_runtime_aliases(parser: ArgumentParser) -> None:
    """允许仓库和 JSON 选项位于子命令之后。"""
    parser.add_argument("--repository", type=Path, default=SUPPRESS)
    parser.add_argument(
        "--json", action="store_true", dest="json_output", default=SUPPRESS
    )
