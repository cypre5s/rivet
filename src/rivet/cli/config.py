"""加载项目内最小模型配置和少量运行时覆盖。"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from rivet.cli.errors import CliConfigurationError
from rivet.providers.models import DeepSeekConfig, DeepSeekModel

DEFAULT_MODEL = DeepSeekModel.V4_FLASH.value
DEFAULT_MODELS = tuple(model.value for model in DeepSeekModel)


@dataclass(frozen=True, slots=True)
class ConfigOverrides:
    """命令行允许覆盖的有限非秘密运行参数。"""

    model: str | None = None
    base_url: str | None = None
    max_rounds: int | None = None
    max_total_tokens: int | None = None
    max_cost_usd: str | None = None


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    """Provider 与 AgentLoop 需要的完整、无秘密配置。"""

    model: str
    models: tuple[str, ...]
    base_url: str
    max_rounds: int
    max_total_tokens: int
    max_cost_usd: Decimal | None
    credential_configured: bool
    sources: dict[str, str]

    def public_mapping(self) -> dict[str, object]:
        return {
            "base_url": self.base_url,
            "credential_configured": self.credential_configured,
            "max_cost_usd": (
                str(self.max_cost_usd) if self.max_cost_usd is not None else None
            ),
            "max_rounds": self.max_rounds,
            "max_total_tokens": self.max_total_tokens,
            "model": self.model,
            "models": list(self.models),
        }


def load_config(
    repository: Path,
    *,
    environment: Mapping[str, str],
    overrides: ConfigOverrides | None = None,
) -> ResolvedConfig:
    """按 default < project < environment < CLI 合并最小配置。"""
    values: dict[str, object] = {
        "model": DEFAULT_MODEL,
        "base_url": DeepSeekConfig().base_url,
        "max_rounds": 24,
        "max_total_tokens": 128_000,
        "max_cost_usd": None,
    }
    sources = {key: "default" for key in values}
    _merge(values, sources, _project_values(repository), "project")
    env_values: dict[str, object] = {}
    for env_name, field in (
        ("RIVET_MODEL", "model"),
        ("RIVET_BASE_URL", "base_url"),
        ("RIVET_MAX_ROUNDS", "max_rounds"),
        ("RIVET_MAX_TOTAL_TOKENS", "max_total_tokens"),
        ("RIVET_MAX_COST_USD", "max_cost_usd"),
    ):
        if env_name in environment:
            env_values[field] = environment[env_name]
    _merge(values, sources, env_values, "environment")
    selected = overrides or ConfigOverrides()
    cli_values = {
        key: value
        for key, value in {
            "model": selected.model,
            "base_url": selected.base_url,
            "max_rounds": selected.max_rounds,
            "max_total_tokens": selected.max_total_tokens,
            "max_cost_usd": selected.max_cost_usd,
        }.items()
        if value is not None
    }
    _merge(values, sources, cli_values, "cli")

    model = _bounded_text(values["model"], "model", maximum=128)
    base_url = _bounded_text(values["base_url"], "base_url", maximum=2_048)
    try:
        DeepSeekConfig(base_url=base_url)
    except ValueError as error:
        raise _invalid("base_url") from error
    max_rounds = _positive_int(values["max_rounds"], "max_rounds", maximum=256)
    max_total_tokens = _positive_int(
        values["max_total_tokens"], "max_total_tokens", maximum=10_000_000
    )
    max_cost_usd = _optional_decimal(values["max_cost_usd"])
    models = tuple(dict.fromkeys((model, *DEFAULT_MODELS)))
    return ResolvedConfig(
        model=model,
        models=models,
        base_url=base_url,
        max_rounds=max_rounds,
        max_total_tokens=max_total_tokens,
        max_cost_usd=max_cost_usd,
        credential_configured=bool(environment.get("DEEPSEEK_API_KEY", "").strip()),
        sources=sources,
    )


def _project_values(repository: Path) -> dict[str, object]:
    path = repository.resolve(strict=True) / ".rivet" / "project.toml"
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise CliConfigurationError(
            "config.project_invalid",
            "项目配置必须是仓库内普通文件",
            "修复 .rivet/project.toml 后重试",
        )
    try:
        if path.stat().st_size > 256 * 1024:
            raise ValueError("too large")
        document = tomllib.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError) as error:
        raise CliConfigurationError(
            "config.project_invalid",
            "项目配置无法安全解析",
            "修复 .rivet/project.toml 后重试",
        ) from error
    raw_rivet = document.get("rivet", {})
    if not isinstance(raw_rivet, dict):
        raise CliConfigurationError(
            "config.project_invalid",
            "[rivet] 只允许配置 model",
            "删除外围运行时配置后重试",
        )
    rivet_values = cast(dict[str, object], raw_rivet)
    if set(rivet_values) - {"model"}:
        raise CliConfigurationError(
            "config.project_invalid",
            "[rivet] 只允许配置 model",
            "删除外围运行时配置后重试",
        )
    return {"model": rivet_values["model"]} if "model" in rivet_values else {}


def _merge(
    values: dict[str, object],
    sources: dict[str, str],
    incoming: Mapping[str, object],
    source: str,
) -> None:
    for key, value in incoming.items():
        if key not in values:
            continue
        values[key] = value
        sources[key] = source


def _bounded_text(value: object, field: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
    ):
        raise _invalid(field)
    return value


def _positive_int(value: object, field: str, *, maximum: int) -> int:
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError as error:
            raise _invalid(field) from error
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise _invalid(field)
    return value


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise _invalid("max_cost_usd") from error
    if not result.is_finite() or result < 0:
        raise _invalid("max_cost_usd")
    return result


def _invalid(field: str) -> CliConfigurationError:
    return CliConfigurationError(
        "config.value_invalid",
        f"运行配置字段无效：{field}",
        "修复项目配置、环境变量或命令行参数后重试",
    )
