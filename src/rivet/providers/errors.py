"""定义不泄露响应体或凭据的 Provider 分类错误。"""

from __future__ import annotations

from rivet.contracts.provider import ModelProviderError


class ProviderError(ModelProviderError):
    """保存稳定错误码、摘要与可重试标记。"""

    def __init__(self, code: str, summary: str, *, retryable: bool) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary
        self.retryable = retryable

    def __repr__(self) -> str:
        """只展示稳定码和重试属性，不展开底层请求。"""
        return (
            f"{type(self).__name__}(code={self.code!r}, retryable={self.retryable!r})"
        )


class ConfigurationError(ProviderError):
    """表示本地 Provider 配置缺失或无效。"""


class CredentialError(ProviderError):
    """表示服务端拒绝认证，且不携带原始凭据。"""


class ProviderRequestError(ProviderError):
    """表示 400/402/422 等不可盲目重试请求错误。"""


class ProviderRateLimitError(ProviderError):
    """表示 429，并保存脱敏后的退避秒数。"""

    def __init__(self, retry_after_seconds: float | None) -> None:
        super().__init__(
            "provider.rate_limited",
            "模型服务触发速率限制",
            retryable=True,
        )
        self.retry_after_seconds = retry_after_seconds


class ProviderUnavailableError(ProviderError):
    """表示网络、5xx 或推理资源不足。"""


class ProviderProtocolError(ProviderError):
    """表示响应 JSON、SSE 或 Tool Call 不满足本地契约。"""


class ProviderOutputIncompleteError(ProviderError):
    """表示 length 或内容过滤导致结果不可作为完成。"""
