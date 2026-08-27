"""提供可解释、分级且按需升级的仓库上下文能力。"""

from .engine import ProgressiveContext, ProgressiveContextResult

__all__ = ["ProgressiveContext", "ProgressiveContextResult"]
