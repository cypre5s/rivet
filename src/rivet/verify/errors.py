"""定义验证矩阵与证据持久化的稳定错误。"""


class VerificationError(RuntimeError):
    """携带不暴露原始输出的验证错误码。"""

    def __init__(self, code: str, summary: str) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary
