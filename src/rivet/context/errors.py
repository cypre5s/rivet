"""定义渐进式上下文检索的稳定失败类型。"""


class ContextError(RuntimeError):
    """作为上下文能力可预期错误的公共基类。"""


class ContextInventoryError(ContextError):
    """表示仓库清单无法完整、确定地建立。"""


class ContextBudgetError(ContextError):
    """表示上下文预算无法容纳必须保留的信息。"""


class ContextSyntaxError(ContextError):
    """表示已请求的语法解析能力不可用或不支持。"""
