"""定义脚本调用方可依赖的稳定退出码。"""

from enum import IntEnum


class ExitCode(IntEnum):
    """区分成功、用户输入、配置、验证、安全和内部失败。"""

    SUCCESS = 0
    USAGE = 2
    CONFIGURATION = 3
    VERIFICATION_FAILED = 4
    SECURITY_DENIED = 5
    PROVIDER_FAILED = 6
    INTERNAL_ERROR = 70
    USER_CANCELLED = 130
