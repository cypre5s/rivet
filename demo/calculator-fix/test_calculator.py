"""以标准库断言演示含税总价的正常值和边界值。"""

from decimal import Decimal

from calculator import total_with_tax

CASES = (
    (Decimal("100.00"), Decimal("0.07"), Decimal("107.00")),
    (Decimal("0.00"), Decimal("0.20"), Decimal("0.00")),
    (Decimal("19.99"), Decimal("0.00"), Decimal("19.99")),
)


def main() -> int:
    """运行固定样例并以退出码表达结果。"""
    for subtotal, rate, expected in CASES:
        actual = total_with_tax(subtotal, rate)
        if actual != expected:
            print(f"FAIL: expected {expected}, got {actual}")
            return 1
    print(f"PASS: {len(CASES)} tax cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
