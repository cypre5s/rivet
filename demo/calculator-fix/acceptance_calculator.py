"""Rivet 发布演示的独立行为验收 oracle。"""

from decimal import Decimal

from calculator import total_with_tax

CASES = (
    (Decimal("1.00"), Decimal("0.10"), Decimal("1.10")),
    (Decimal("12.34"), Decimal("0.075"), Decimal("13.27")),
    (Decimal("250.00"), Decimal("0.16"), Decimal("290.00")),
)


def main() -> int:
    """用不同于回归测试的数据判定外部可观察行为。"""
    return 0 if all(total_with_tax(a, b) == expected for a, b, expected in CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
