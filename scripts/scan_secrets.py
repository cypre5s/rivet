"""为项目统一门禁名称提供凭据扫描入口。"""

from pathlib import Path
from runpy import run_path

if __name__ == "__main__":
    run_path(str(Path(__file__).with_name("verify_secrets.py")), run_name="__main__")
