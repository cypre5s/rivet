"""提供可复现演示中的含税总价计算。"""

from decimal import Decimal


def total_with_tax(subtotal: Decimal, rate: Decimal) -> Decimal:
    """返回保留两位小数的含税金额。"""
    return (subtotal + rate).quantize(Decimal("0.01"))
