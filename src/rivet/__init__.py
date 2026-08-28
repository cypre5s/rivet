"""提供 Rivet 的最小可安装入口。"""

from argparse import ArgumentParser
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path
from typing import cast

__version__ = version("rivet")


def main(argv: Sequence[str] | None = None) -> None:
    """解析基础元信息与当前已接通的 headless 子命令。"""
    parser = ArgumentParser(
        prog="rivet",
        description="可靠、可审计的本地编程智能体",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command")
    trace_parser = subparsers.add_parser("trace", help="回放结构化执行轨迹")
    trace_parser.add_argument("run_id", nargs="?")
    trace_parser.add_argument("--json", action="store_true", dest="json_output")
    trace_parser.add_argument("--repository", type=Path, default=Path.cwd())
    arguments = parser.parse_args(argv)
    command = cast(str | None, arguments.command)
    if command == "trace":
        from rivet.trace.cli import run_trace_command

        exit_code = run_trace_command(
            repository=cast(Path, arguments.repository),
            run_id=cast(str | None, arguments.run_id),
            json_output=cast(bool, arguments.json_output),
        )
        if exit_code:
            raise SystemExit(exit_code)
