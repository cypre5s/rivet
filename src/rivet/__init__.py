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
    doctor_parser = subparsers.add_parser("doctor", help="检测本地运行依赖")
    doctor_parser.add_argument("--json", action="store_true", dest="json_output")
    doctor_parser.add_argument("--repository", type=Path, default=Path.cwd())
    doctor_parser.add_argument(
        "--section",
        choices=("lsp", "readers"),
        default="lsp",
    )
    read_parser = subparsers.add_parser("read", help="安全读取本地文件")
    read_parser.add_argument("file", type=Path)
    read_parser.add_argument("--json", action="store_true", dest="json_output")
    read_parser.add_argument("--repository", type=Path, default=Path.cwd())
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
    elif command == "doctor":
        section = cast(str, arguments.section)
        if section == "readers":
            from rivet.readers.doctor import ReaderDoctor

            reader_report = ReaderDoctor().inspect()
            if cast(bool, arguments.json_output):
                print(reader_report.to_json())
            else:
                for component in reader_report.components:
                    state = "可用" if component.available else "缺失"
                    requirement = "必需" if component.required else "可选"
                    print(f"{component.component_id}: {state} ({requirement})")
            if not reader_report.ready:
                raise SystemExit(1)
        else:
            from rivet.context.lsp_doctor import LspDoctor
            from rivet.context.lsp_manifest import LspManifestRegistry

            lsp_report = LspDoctor(
                LspManifestRegistry.load_builtin(
                    repository_root=cast(Path, arguments.repository)
                )
            ).inspect()
            if cast(bool, arguments.json_output):
                print(lsp_report.to_json())
            else:
                for server in lsp_report.servers:
                    state = "可用" if server.available else "缺失"
                    print(f"{server.server_id}: {state}")
            if not lsp_report.ready:
                raise SystemExit(1)
    elif command == "read":
        import asyncio

        from rivet.contracts.readers import ReaderRequest, ReaderResult, ReaderStatus
        from rivet.kernel.resources import ResourceScope
        from rivet.readers.service import ReaderService

        repository = cast(Path, arguments.repository).resolve(strict=True)
        source_argument = cast(Path, arguments.file)
        source = (
            source_argument.resolve(strict=True)
            if source_argument.is_absolute()
            else (repository / source_argument).resolve(strict=True)
        )
        try:
            source_path = source.relative_to(repository).as_posix()
        except ValueError as error:
            raise SystemExit("读取路径不属于授权仓库") from error

        async def run_reader() -> ReaderResult:
            """执行一次读取并保证 CLI 资源域关闭。"""
            scope = ResourceScope("reader.cli")
            try:
                return await ReaderService(repository, scope=scope).read(
                    ReaderRequest(source_path=source_path)
                )
            finally:
                await scope.close()

        result = asyncio.run(run_reader())
        if cast(bool, arguments.json_output):
            print(result.model_dump_json())
        else:
            print(result.content, end="" if result.content.endswith("\n") else "\n")
        if result.status is ReaderStatus.FAILED:
            raise SystemExit(1)
