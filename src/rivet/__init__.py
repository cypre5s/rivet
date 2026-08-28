"""提供 Rivet 的最小可安装入口。"""

from argparse import ArgumentParser
from collections.abc import Sequence
from importlib.metadata import version

__version__ = version("rivet")


def main(argv: Sequence[str] | None = None) -> None:
    """解析基础元信息参数；业务命令由后续阶段接入。"""
    parser = ArgumentParser(
        prog="rivet",
        description="可靠、可审计的本地编程智能体",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.parse_args(argv)
