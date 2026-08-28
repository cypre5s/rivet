"""只读识别项目类型，并将配置命令保持为待确认候选。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from .errors import VerificationError

MAX_PROJECT_CONFIG_BYTES = 256 * 1024
MAX_COMMANDS_PER_GROUP = 64
MAX_ARGUMENTS_PER_COMMAND = 128
MAX_ARGUMENT_LENGTH = 16_384


class ProjectKind(StrEnum):
    """列出 1.0 项目检测器支持的生态。"""

    PYTHON = "python"
    NODE_BUN = "node_bun"
    RUST = "rust"
    GO = "go"
    GENERIC = "generic"


@dataclass(frozen=True, slots=True)
class ProjectCommandCandidate:
    """保存尚未获准执行的检测器命令建议。"""

    kind: ProjectKind
    category: str
    argv: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class ProjectConfiguration:
    """保存 `.rivet/project.toml` 中四类显式 argv。"""

    targeted: tuple[tuple[str, ...], ...] = ()
    related: tuple[tuple[str, ...], ...] = ()
    regression: tuple[tuple[str, ...], ...] = ()
    static: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectDetection:
    """汇总稳定项目类型、命令候选和未执行配置。"""

    kinds: tuple[ProjectKind, ...]
    candidates: tuple[ProjectCommandCandidate, ...]
    configuration: ProjectConfiguration | None


class ProjectDetector:
    """根据根目录标记文件生成建议，不启动任何子进程。"""

    def detect(self, repository_root: Path) -> ProjectDetection:
        """识别 Python、Node/Bun、Rust、Go 或通用项目。"""
        root = repository_root.resolve(strict=True)
        if not root.is_dir():
            raise VerificationError(
                "verification.project_root_invalid",
                "项目检测根不是目录",
            )
        kinds: list[ProjectKind] = []
        candidates: list[ProjectCommandCandidate] = []
        if self._has_any(root, ("pyproject.toml", "setup.py", "requirements.txt")):
            kinds.append(ProjectKind.PYTHON)
            candidates.extend(
                (
                    self._candidate(
                        ProjectKind.PYTHON, "regression", ("uv", "run", "pytest", "-q")
                    ),
                    self._candidate(
                        ProjectKind.PYTHON,
                        "static",
                        ("uv", "run", "ruff", "check", "."),
                    ),
                    self._candidate(
                        ProjectKind.PYTHON, "static", ("uv", "run", "basedpyright")
                    ),
                )
            )
        if self._has_any(root, ("package.json", "bun.lock", "bun.lockb")):
            kinds.append(ProjectKind.NODE_BUN)
            candidates.extend(
                (
                    self._candidate(
                        ProjectKind.NODE_BUN, "regression", ("bun", "test")
                    ),
                    self._candidate(
                        ProjectKind.NODE_BUN, "static", ("bunx", "tsc", "--noEmit")
                    ),
                )
            )
        if (root / "Cargo.toml").is_file():
            kinds.append(ProjectKind.RUST)
            candidates.extend(
                (
                    self._candidate(ProjectKind.RUST, "regression", ("cargo", "test")),
                    self._candidate(
                        ProjectKind.RUST,
                        "static",
                        ("cargo", "clippy", "--", "-D", "warnings"),
                    ),
                )
            )
        if (root / "go.mod").is_file():
            kinds.append(ProjectKind.GO)
            candidates.extend(
                (
                    self._candidate(
                        ProjectKind.GO, "regression", ("go", "test", "./...")
                    ),
                    self._candidate(ProjectKind.GO, "static", ("go", "vet", "./...")),
                )
            )
        if not kinds:
            kinds.append(ProjectKind.GENERIC)
        configuration = self._load_configuration(root)
        return ProjectDetection(
            kinds=tuple(kinds),
            candidates=tuple(candidates),
            configuration=configuration,
        )

    @staticmethod
    def _has_any(root: Path, names: tuple[str, ...]) -> bool:
        """判断任一项目标记是否为普通文件。"""
        return any((root / name).is_file() for name in names)

    @staticmethod
    def _candidate(
        kind: ProjectKind,
        category: str,
        argv: tuple[str, ...],
    ) -> ProjectCommandCandidate:
        """构造带明确未执行原因的命令候选。"""
        return ProjectCommandCandidate(
            kind=kind,
            category=category,
            argv=argv,
            reason="由项目标记推断，需写入配置并显式确认后执行",
        )

    def _load_configuration(self, root: Path) -> ProjectConfiguration | None:
        """严格解析项目配置，但不把其中命令升级为执行权限。"""
        path = root / ".rivet" / "project.toml"
        if path.is_symlink():
            raise VerificationError(
                "verification.project_config_invalid",
                "项目验证配置不得是符号链接",
            )
        if not path.exists():
            return None
        if not path.is_file():
            raise VerificationError(
                "verification.project_config_invalid",
                "项目验证配置必须是仓库内普通文件",
            )
        try:
            if path.stat().st_size > MAX_PROJECT_CONFIG_BYTES:
                raise VerificationError(
                    "verification.project_config_too_large",
                    "项目验证配置超过大小上限",
                )
            payload = tomllib.loads(path.read_text(encoding="utf-8", errors="strict"))
        except VerificationError:
            raise
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
            raise VerificationError(
                "verification.project_config_invalid",
                "项目验证配置无法解析",
            ) from error
        if set(payload) - {"schema_version", "rivet", "verification"}:
            raise VerificationError(
                "verification.project_config_unknown_field",
                "项目验证配置包含未知顶层字段",
            )
        if payload.get("schema_version") != 1:
            raise VerificationError(
                "verification.project_config_version",
                "项目验证配置版本不受支持",
            )
        raw_verification = payload.get("verification", {})
        if not isinstance(raw_verification, dict):
            raise VerificationError(
                "verification.project_config_invalid",
                "verification 必须是 TOML 表",
            )
        verification = cast(dict[str, object], raw_verification)
        allowed_groups = {"targeted", "related", "regression", "static"}
        if set(verification) - allowed_groups:
            raise VerificationError(
                "verification.project_config_unknown_field",
                "项目验证配置包含未知命令组",
            )
        return ProjectConfiguration(
            targeted=self._parse_commands(verification.get("targeted", [])),
            related=self._parse_commands(verification.get("related", [])),
            regression=self._parse_commands(verification.get("regression", [])),
            static=self._parse_commands(verification.get("static", [])),
        )

    @staticmethod
    def _parse_commands(raw_commands: object) -> tuple[tuple[str, ...], ...]:
        """只接受有界字符串 argv 数组，拒绝 shell 字符串。"""
        if not isinstance(raw_commands, list):
            raise VerificationError(
                "verification.project_commands_invalid",
                "项目验证命令组格式或数量无效",
            )
        command_items = cast(list[object], raw_commands)
        if len(command_items) > MAX_COMMANDS_PER_GROUP:
            raise VerificationError(
                "verification.project_commands_invalid",
                "项目验证命令组格式或数量无效",
            )
        commands: list[tuple[str, ...]] = []
        for raw_command in command_items:
            if not isinstance(raw_command, list):
                raise VerificationError(
                    "verification.project_command_invalid",
                    "项目验证命令必须是非空有界 argv",
                )
            arguments = cast(list[object], raw_command)
            if (
                not arguments
                or len(arguments) > MAX_ARGUMENTS_PER_COMMAND
                or any(
                    not isinstance(argument, str)
                    or not argument
                    or "\x00" in argument
                    or len(argument) > MAX_ARGUMENT_LENGTH
                    for argument in arguments
                )
            ):
                raise VerificationError(
                    "verification.project_command_invalid",
                    "项目验证命令必须是非空有界 argv",
                )
            commands.append(tuple(cast(list[str], arguments)))
        if any(command[0] == "rivet-internal" for command in commands):
            raise VerificationError(
                "verification.internal_command_reserved",
                "项目配置不得使用内部保留程序名",
            )
        return tuple(commands)
