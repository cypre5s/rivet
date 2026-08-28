"""提供 Rivet 的可安装入口与版本。"""

from collections.abc import Sequence
from importlib.metadata import version

__version__ = version("rivet")


def main(argv: Sequence[str] | None = None) -> int:
    """延迟导入正式 CLI，保持包导入无业务副作用。"""
    from rivet.cli.application import run_cli

    return run_cli(argv)
