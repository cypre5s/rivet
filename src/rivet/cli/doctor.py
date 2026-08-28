"""聚合核心、TUI、沙箱、Reader、LSP 与 Provider 的只读诊断。"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

from rivet.cli.config import ResolvedConfig
from rivet.context.lsp_doctor import LspDoctor
from rivet.context.lsp_manifest import LspManifestRegistry
from rivet.guard.doctor import SandboxDoctor
from rivet.readers.doctor import ReaderDoctor

DoctorSection = Literal["all", "core", "tui", "sandbox", "readers", "lsp", "provider"]
DoctorStatus = Literal[
    "AVAILABLE", "MISSING", "NOT_EXECUTABLE", "UNSUPPORTED", "UNUSABLE"
]


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """保存一个不会携带秘密值的诊断事实。"""

    check_id: str
    section: str
    status: DoctorStatus
    required: bool
    detail: str
    next_action: str | None = None

    @property
    def available(self) -> bool:
        """返回该项是否满足当前能力要求。"""
        return self.status == "AVAILABLE"


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """汇总所选分区并区分核心就绪与可选能力缺失。"""

    ready: bool
    status: Literal["READY", "DEGRADED"]
    checks: tuple[DoctorCheck, ...]

    def public_mapping(self) -> dict[str, object]:
        """生成稳定 JSON 映射。"""
        payload: dict[str, object] = {
            "checks": [asdict(check) for check in self.checks],
            "ready": self.ready,
            "schema_version": 1,
            "status": self.status,
        }
        lsp_checks = tuple(check for check in self.checks if check.section == "lsp")
        if lsp_checks:
            payload["servers"] = [
                {
                    "available": check.available,
                    "executable": check.detail if check.available else None,
                    "next_action": check.next_action,
                    "server_id": check.check_id.removeprefix("lsp."),
                }
                for check in lsp_checks
            ]
        reader_checks = tuple(
            check for check in self.checks if check.section == "readers"
        )
        if reader_checks:
            payload["components"] = [
                {
                    "available": check.available,
                    "component_id": check.check_id,
                    "next_action": check.next_action,
                    "required": check.required,
                    "source": check.detail if check.available else None,
                }
                for check in reader_checks
            ]
            payload["limits"] = ReaderDoctor().inspect().limits
        return payload


def inspect_doctor(
    repository: Path,
    config: ResolvedConfig,
    *,
    section: DoctorSection = "all",
) -> DoctorReport:
    """执行不联网、不读取凭据值且有界的本地诊断。"""
    checks: list[DoctorCheck] = []
    selected = (
        {"core", "tui", "sandbox", "readers", "lsp", "provider"}
        if section == "all"
        else {section}
    )
    if "core" in selected:
        checks.extend(_core_checks())
    if "tui" in selected:
        checks.extend(_tui_checks())
    if "sandbox" in selected:
        checks.append(_sandbox_check())
    if "readers" in selected:
        checks.extend(_reader_checks())
    if "lsp" in selected:
        checks.extend(_lsp_checks(repository))
    if "provider" in selected:
        checks.append(
            DoctorCheck(
                check_id="provider.deepseek_api_key",
                section="provider",
                status=("AVAILABLE" if config.credential_configured else "MISSING"),
                required=False,
                detail=(
                    "DEEPSEEK_API_KEY 已配置"
                    if config.credential_configured
                    else "DEEPSEEK_API_KEY 未配置"
                ),
                next_action=(
                    None
                    if config.credential_configured
                    else "通过环境变量提供已轮换的 DEEPSEEK_API_KEY"
                ),
            )
        )
    ordered = tuple(sorted(checks, key=lambda check: check.check_id))
    if section == "all":
        ready = all(
            check.available
            for check in ordered
            if check.required and check.section in {"core", "readers"}
        )
    elif section in {"lsp", "provider", "tui"}:
        ready = bool(ordered) and all(check.available for check in ordered)
    else:
        ready = all(check.available for check in ordered if check.required)
    degraded = any(not check.available for check in ordered if check.required)
    return DoctorReport(
        ready=ready,
        status="DEGRADED" if degraded else "READY",
        checks=ordered,
    )


def render_doctor(report: DoctorReport) -> str:
    """生成人类可读且不包含环境变量值的诊断文本。"""
    lines = [f"Rivet Doctor: {report.status}"]
    for check in report.checks:
        requirement = "required" if check.required else "optional"
        lines.append(
            f"- {check.check_id}: {check.status} ({requirement}) — {check.detail}"
        )
        if check.next_action is not None:
            lines.append(f"  下一步：{check.next_action}")
    return "\n".join(lines)


def doctor_json(report: DoctorReport) -> str:
    """以紧凑稳定格式序列化诊断报告。"""
    return json.dumps(
        report.public_mapping(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _core_checks() -> tuple[DoctorCheck, ...]:
    """检查目标操作系统、架构、Python 与基础命令。"""
    system = platform.system()
    architecture = platform.machine().lower()
    distro = _linux_distribution()
    python_supported = sys.version_info[:2] == (3, 13)
    checks = [
        DoctorCheck(
            "core.os",
            "core",
            "AVAILABLE" if system == "Linux" else "UNSUPPORTED",
            True,
            f"{system} {distro}".strip(),
            None if system == "Linux" else "使用 Ubuntu 24.04 LTS",
        ),
        DoctorCheck(
            "core.architecture",
            "core",
            (
                "AVAILABLE"
                if architecture in {"x86_64", "amd64", "aarch64", "arm64"}
                else "UNSUPPORTED"
            ),
            True,
            architecture or "unknown",
            None,
        ),
        DoctorCheck(
            "core.python",
            "core",
            "AVAILABLE" if python_supported else "UNSUPPORTED",
            True,
            platform.python_version(),
            None if python_supported else "通过 uv 使用 CPython 3.13.x",
        ),
    ]
    for check_id, executable, required in (
        ("core.uv", "uv", True),
        ("core.git", "git", True),
        ("core.ripgrep", "rg", True),
    ):
        discovered = shutil.which(executable)
        checks.append(
            DoctorCheck(
                check_id,
                "core",
                "AVAILABLE" if discovered is not None else "MISSING",
                required,
                discovered or f"未找到 {executable}",
                None if discovered is not None else f"安装 {executable} 后重试",
            )
        )
    return tuple(checks)


def _tui_checks() -> tuple[DoctorCheck, ...]:
    """检查 Bun 1.4.x 与本地 OpenTUI 锁定资源。"""
    bun = shutil.which("bun")
    version = _command_version((bun, "--version")) if bun is not None else None
    bun_ready = version is not None and version.startswith("1.4.")
    source_root = Path(__file__).resolve().parents[3]
    package_path = (
        source_root / "tui" / "node_modules" / "@opentui" / "core" / "package.json"
    )
    open_tui_version = _package_version(package_path)
    return (
        DoctorCheck(
            "tui.bun",
            "tui",
            "AVAILABLE" if bun_ready else ("MISSING" if bun is None else "UNSUPPORTED"),
            False,
            version or "未找到 Bun 1.4.x",
            None if bun_ready else "安装项目固定的 Bun 1.4.x",
        ),
        DoctorCheck(
            "tui.opentui",
            "tui",
            "AVAILABLE" if open_tui_version is not None else "MISSING",
            False,
            open_tui_version or "未找到本地 OpenTUI 资源",
            None
            if open_tui_version is not None
            else "在 tui 目录执行 bun install --frozen-lockfile",
        ),
    )


def _sandbox_check() -> DoctorCheck:
    """把 bubblewrap 的失败关闭事实合并进统一格式。"""
    report = SandboxDoctor().inspect()
    status: DoctorStatus = report.status
    return DoctorCheck(
        "sandbox.bubblewrap",
        "sandbox",
        status,
        True,
        report.executable or report.status,
        None if report.ready else report.next_action,
    )


def _reader_checks() -> tuple[DoctorCheck, ...]:
    """复用 Reader Doctor 并保留必需/可选区别。"""
    report = ReaderDoctor().inspect()
    return tuple(
        DoctorCheck(
            item.component_id,
            "readers",
            "AVAILABLE" if item.available else "MISSING",
            item.required,
            item.source or "不可用",
            item.next_action,
        )
        for item in report.components
    )


def _lsp_checks(repository: Path) -> tuple[DoctorCheck, ...]:
    """检查内置 LSP Manifest 的候选可执行文件但不启动服务。"""
    report = LspDoctor(
        LspManifestRegistry.load_builtin(repository_root=repository)
    ).inspect()
    return tuple(
        DoctorCheck(
            f"lsp.{item.server_id}",
            "lsp",
            "AVAILABLE" if item.available else "MISSING",
            False,
            item.executable or "不可用",
            item.next_action,
        )
        for item in report.servers
    )


def _linux_distribution() -> str:
    """只读取公开系统标识，不依赖额外发行版包。"""
    path = Path("/etc/os-release")
    try:
        values = {
            key: value.strip().strip('"')
            for line in path.read_text(encoding="utf-8").splitlines()
            if "=" in line
            for key, value in (line.split("=", 1),)
        }
    except OSError:
        return ""
    return values.get("PRETTY_NAME", values.get("ID", ""))


def _command_version(argv: tuple[str, ...]) -> str | None:
    """以最小环境和短超时读取版本文本。"""
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            env={
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            },
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _package_version(path: Path) -> str | None:
    """有界读取 npm package version，不执行包代码。"""
    try:
        if path.is_symlink() or path.stat().st_size > 64 * 1024:
            return None
        raw_document: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw_document, dict):
        return None
    document = cast(dict[str, object], raw_document)
    version = document.get("version")
    return version if isinstance(version, str) else None
