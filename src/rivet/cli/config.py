"""按 CLI、环境、项目、用户和默认值解析非秘密配置。"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from rivet.cli.errors import CliConfigurationError
from rivet.providers.models import DeepSeekConfig, DeepSeekModel

CONFIG_FIELDS = (
    "model",
    "base_url",
    "max_rounds",
    "max_total_tokens",
    "max_cost_usd",
    "safe_mode",
)
ENVIRONMENT_FIELDS = {
    "RIVET_MODEL": "model",
    "RIVET_BASE_URL": "base_url",
    "RIVET_MAX_ROUNDS": "max_rounds",
    "RIVET_MAX_TOTAL_TOKENS": "max_total_tokens",
    "RIVET_MAX_COST_USD": "max_cost_usd",
    "RIVET_SAFE_MODE": "safe_mode",
}
SECRET_FIELD_PARTS = ("api_key", "credential", "password", "secret", "token")
MAX_CONFIG_BYTES = 256 * 1024
DEFAULT_VALUES: dict[str, object] = {
    "model": DeepSeekModel.V4_PRO.value,
    "base_url": "https://api.deepseek.com",
    "max_rounds": 24,
    "max_total_tokens": 128_000,
    "max_cost_usd": None,
    "safe_mode": False,
}


@dataclass(frozen=True, slots=True)
class ConfigOverrides:
    """保存命令行显式提供、未提供时为 None 的配置覆盖。"""

    model: str | None = None
    base_url: str | None = None
    max_rounds: int | None = None
    max_total_tokens: int | None = None
    max_cost_usd: str | Decimal | None = None
    safe_mode: bool | None = None


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    """保存已经校验的有效值、来源和仅布尔形式的凭据状态。"""

    model: str
    base_url: str
    max_rounds: int
    max_total_tokens: int
    max_cost_usd: Decimal | None
    safe_mode: bool
    credential_configured: bool
    sources: dict[str, str]

    def __repr__(self) -> str:
        """只展示非秘密字段，凭据只展示是否存在。"""
        return f"ResolvedConfig({self.public_mapping()!r})"

    def public_mapping(self) -> dict[str, object]:
        """返回可安全用于 config 和 Doctor 输出的稳定映射。"""
        return {
            "base_url": self.base_url,
            "credential_configured": self.credential_configured,
            "max_cost_usd": (
                str(self.max_cost_usd) if self.max_cost_usd is not None else None
            ),
            "max_rounds": self.max_rounds,
            "max_total_tokens": self.max_total_tokens,
            "model": self.model,
            "safe_mode": self.safe_mode,
        }


@dataclass(frozen=True, slots=True)
class _ValidatedValues:
    """保存已完成跨层类型与范围校验的配置字段。"""

    model: str
    base_url: str
    max_rounds: int
    max_total_tokens: int
    max_cost_usd: Decimal | None
    safe_mode: bool


def load_config(
    repository: Path,
    *,
    environment: Mapping[str, str] | None = None,
    overrides: ConfigOverrides | None = None,
) -> ResolvedConfig:
    """解析唯一优先级，并在任何秘密字段进入结果前拒绝。"""
    root = repository.resolve(strict=True)
    selected_environment = os.environ if environment is None else environment
    user_path = _user_config_path(selected_environment)
    user_values = _load_document(user_path, project=False)
    project_values = _load_document(root / ".rivet" / "project.toml", project=True)
    environment_values = _environment_values(selected_environment)
    cli_values = _override_values(overrides or ConfigOverrides())
    values = dict(DEFAULT_VALUES)
    sources = {field: "default" for field in CONFIG_FIELDS}
    for source_name, layer in (
        ("user", user_values),
        ("project", project_values),
        ("environment", environment_values),
        ("cli", cli_values),
    ):
        for field, value in layer.items():
            values[field] = value
            sources[field] = source_name
    normalized = _validate_values(values)
    credential = selected_environment.get("DEEPSEEK_API_KEY")
    return ResolvedConfig(
        model=normalized.model,
        base_url=normalized.base_url,
        max_rounds=normalized.max_rounds,
        max_total_tokens=normalized.max_total_tokens,
        max_cost_usd=normalized.max_cost_usd,
        safe_mode=normalized.safe_mode,
        credential_configured=bool(credential and credential.strip()),
        sources=dict(sorted(sources.items())),
    )


def _user_config_path(environment: Mapping[str, str]) -> Path:
    """解析 XDG 用户配置路径并拒绝相对目录。"""
    configured = environment.get("XDG_CONFIG_HOME")
    root = Path(configured) if configured else Path.home() / ".config"
    if not root.is_absolute():
        raise CliConfigurationError(
            "config.xdg_path_invalid",
            "XDG_CONFIG_HOME 必须是绝对路径",
            "设置绝对 XDG_CONFIG_HOME 后重试",
        )
    return root / "rivet" / "config.toml"


def _load_document(path: Path, *, project: bool) -> dict[str, object]:
    """读取有界 TOML，只提取 `[rivet]` 非秘密字段。"""
    if project and path.parent.is_symlink():
        raise CliConfigurationError(
            "config.file_invalid",
            "项目配置目录不能是符号链接",
            "替换为仓库内受控 .rivet 目录",
        )
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise CliConfigurationError(
            "config.file_invalid",
            "配置路径必须是普通文件且不能是符号链接",
            "替换为受控普通 TOML 文件",
        )
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise CliConfigurationError(
                "config.file_too_large",
                "配置文件超过大小上限",
                "精简配置文件后重试",
            )
        document = cast(
            dict[str, object], tomllib.loads(path.read_text(encoding="utf-8"))
        )
    except CliConfigurationError:
        raise
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise CliConfigurationError(
            "config.file_invalid",
            "配置文件无法解析",
            "检查 UTF-8 与 TOML 语法",
        ) from error
    allowed_top = {"schema_version", "rivet"}
    if project:
        allowed_top.add("verification")
    if set(document) - allowed_top or document.get("schema_version") != 1:
        raise CliConfigurationError(
            "config.schema_invalid",
            "配置协议版本或顶层字段无效",
            "使用 schema_version = 1 和已公布字段",
        )
    raw_values = document.get("rivet", {})
    if not isinstance(raw_values, dict):
        raise CliConfigurationError(
            "config.section_invalid",
            "[rivet] 必须是 TOML 表",
            "修正 [rivet] 配置结构",
        )
    values = cast(dict[str, object], raw_values)
    _reject_secret_fields(values)
    unknown = set(values) - set(CONFIG_FIELDS)
    if unknown:
        raise CliConfigurationError(
            "config.field_unknown",
            "[rivet] 包含未知配置字段",
            "删除未知字段后重试",
        )
    return dict(values)


def _reject_secret_fields(values: Mapping[str, object]) -> None:
    """拒绝任何看起来可能承载凭据的配置键。"""
    if any(
        key not in CONFIG_FIELDS
        and any(part in key.lower() for part in SECRET_FIELD_PARTS)
        for key in values
    ):
        raise CliConfigurationError(
            "config.credential_forbidden",
            "凭据不得写入 Rivet TOML 配置",
            "撤销该凭据并改用 DEEPSEEK_API_KEY 环境变量",
        )


def _environment_values(environment: Mapping[str, str]) -> dict[str, object]:
    """只读取已公布的非秘密 RIVET_ 配置变量。"""
    values: dict[str, object] = {}
    for name, field in ENVIRONMENT_FIELDS.items():
        if name not in environment:
            continue
        raw = environment[name]
        try:
            if field in {"max_rounds", "max_total_tokens"}:
                values[field] = int(raw)
            elif field == "safe_mode":
                values[field] = _parse_boolean(raw)
            else:
                values[field] = raw
        except ValueError as error:
            raise CliConfigurationError(
                "config.environment_invalid",
                f"环境变量 {name} 的值无效",
                f"修正或移除 {name}",
            ) from error
    return values


def _parse_boolean(value: str) -> bool:
    """只接受无歧义的布尔文本。"""
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("布尔值无效")


def _override_values(overrides: ConfigOverrides) -> dict[str, object]:
    """移除未显式提供的命令行字段。"""
    return {
        field: value
        for field in CONFIG_FIELDS
        if (value := getattr(overrides, field)) is not None
    }


def _validate_values(values: Mapping[str, object]) -> _ValidatedValues:
    """规范数值、URL 和模型名并返回可构造结果。"""
    model = values["model"]
    base_url = values["base_url"]
    max_rounds = values["max_rounds"]
    max_total_tokens = values["max_total_tokens"]
    safe_mode = values["safe_mode"]
    if not isinstance(model, str) or not model.strip() or len(model) > 128:
        raise _value_error("model")
    if not isinstance(base_url, str):
        raise _value_error("base_url")
    try:
        DeepSeekConfig(base_url=base_url)
    except ValueError as error:
        raise _value_error("base_url") from error
    if (
        not isinstance(max_rounds, int)
        or isinstance(max_rounds, bool)
        or not 1 <= max_rounds <= 128
    ):
        raise _value_error("max_rounds")
    if (
        not isinstance(max_total_tokens, int)
        or isinstance(max_total_tokens, bool)
        or not 1 <= max_total_tokens <= 10_000_000
    ):
        raise _value_error("max_total_tokens")
    if not isinstance(safe_mode, bool):
        raise _value_error("safe_mode")
    raw_cost = values["max_cost_usd"]
    try:
        max_cost = None if raw_cost is None else Decimal(str(raw_cost))
    except (InvalidOperation, ValueError) as error:
        raise _value_error("max_cost_usd") from error
    if max_cost is not None and (not max_cost.is_finite() or max_cost < 0):
        raise _value_error("max_cost_usd")
    return _ValidatedValues(
        model=model.strip(),
        base_url=base_url,
        max_rounds=max_rounds,
        max_total_tokens=max_total_tokens,
        max_cost_usd=max_cost,
        safe_mode=safe_mode,
    )


def _value_error(field: str) -> CliConfigurationError:
    """构造不包含原始值的统一配置错误。"""
    return CliConfigurationError(
        "config.value_invalid",
        f"配置字段 {field} 的值无效",
        "按 rivet config --help 修正配置",
    )
