"""定义 DeepSeek 当前模型名与不含密钥的 Provider 配置。"""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DeepSeekModel(StrEnum):
    """列出 2026 年当前正式 Chat Completions 模型。"""

    V4_PRO = "deepseek-v4-pro"
    V4_FLASH = "deepseek-v4-flash"


class DeepSeekConfig(BaseModel):
    """只保存非秘密连接、重试与超时配置。"""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        hide_input_in_errors=True,
    )

    base_url: str = "https://api.deepseek.com"
    timeout_seconds: float = Field(default=180.0, gt=0, le=600)
    max_attempts: int = Field(default=3, ge=1, le=8)
    base_backoff_seconds: float = Field(default=0.5, ge=0, le=60)
    max_backoff_seconds: float = Field(default=30.0, ge=0, le=300)

    @model_validator(mode="after")
    def _validate_url_and_backoff(self) -> Self:
        """只接受 HTTP(S) 根地址并保证退避上限合理。"""
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("DeepSeek base_url 必须是 HTTP(S) 绝对地址")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("DeepSeek base_url 不得携带凭据")
        if parsed.query or parsed.fragment:
            raise ValueError("DeepSeek base_url 不得包含 query 或 fragment")
        if self.max_backoff_seconds < self.base_backoff_seconds:
            raise ValueError("max_backoff_seconds 不得小于 base_backoff_seconds")
        return self
