"""定义事务创建、冻结、补丁和应用的稳定错误。"""


class TransactionError(RuntimeError):
    """携带可供上层分类的事务错误码。"""

    def __init__(self, code: str, summary: str) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary
