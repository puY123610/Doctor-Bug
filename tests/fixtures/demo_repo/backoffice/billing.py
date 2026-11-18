"""Order pricing: coupons, tax and integer-cent totals."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

TAX_RATE = Decimal("0.06")
_CENT = Decimal("1")


@dataclass(frozen=True)
class Coupon:
    kind: str
    value: int


@dataclass(frozen=True)
class Bill:
    subtotal_cents: int
    discount_cents: int
    tax_cents: int
    total_cents: int


def _round_cents(amount: Decimal) -> int:
    return int(amount.quantize(_CENT, rounding=ROUND_HALF_UP))


def price_order(
    lines: list[tuple[str, int, int]],
    coupons: list[Coupon],
) -> Bill:
    subtotal = sum(unit_price * qty for _sku, unit_price, qty in lines)

    percent = 0
    fixed = 0
    for coupon in coupons:
        if coupon.kind == "percent":
            percent += coupon.value
        elif coupon.kind == "fixed":
            fixed += coupon.value
        else:
            raise ValueError(f"unknown coupon kind: {coupon.kind!r}")

    percent_discount = _round_cents(Decimal(subtotal) * Decimal(percent) / Decimal(100))
    discount = min(percent_discount + fixed, subtotal)
    tax = _round_cents(Decimal(subtotal) * TAX_RATE)
    total = subtotal - discount + tax
    return Bill(subtotal, discount, tax, total)
