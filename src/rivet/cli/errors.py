"""保存不会把原始异常或凭据暴露给普通 CLI 的分类错误。"""

from __future__ import annotations

from rivet.cli.exit_codes import ExitCode


class CliError(RuntimeError):
    """携带稳定错误码、下一步和进程退出码。"""

    def __init__(
        self,
        code: str,
        summary: str,
        next_action: str,
        *,
        exit_code: ExitCode,
    ) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary
        self.next_action = next_action
        self.exit_code = exit_code


class CliConfigurationError(CliError):
    """表示配置文件、环境变量或本地依赖无效。"""

    def __init__(self, code: str, summary: str, next_action: str) -> None:
        super().__init__(
            code,
            summary,
            next_action,
            exit_code=ExitCode.CONFIGURATION,
        )


class CliVerificationError(CliError):
    """表示确定性验证未通过或无法得出通过结论。"""

    def __init__(self, code: str, summary: str, next_action: str) -> None:
        super().__init__(
            code,
            summary,
            next_action,
            exit_code=ExitCode.VERIFICATION_FAILED,
        )


class CliSecurityError(CliError):
    """表示权限、范围或 ownership 门禁拒绝操作。"""

    def __init__(self, code: str, summary: str, next_action: str) -> None:
        super().__init__(
            code,
            summary,
            next_action,
            exit_code=ExitCode.SECURITY_DENIED,
        )


class CliProviderError(CliError):
    """表示已配置 Provider 调用失败。"""

    def __init__(self, code: str, summary: str, next_action: str) -> None:
        super().__init__(
            code,
            summary,
            next_action,
            exit_code=ExitCode.PROVIDER_FAILED,
        )


class CliCancellationError(CliError):
    """表示用户取消了当前 Agent 或工具运行。"""

    def __init__(self, code: str, summary: str, next_action: str) -> None:
        super().__init__(
            code,
            summary,
            next_action,
            exit_code=ExitCode.USER_CANCELLED,
        )
