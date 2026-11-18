"""Tests for coupon, tax and total calculation."""

from backoffice.billing import Coupon, price_order


def test_price_without_coupons():
    bill = price_order([("cable", 700, 3)], [])
    assert bill.subtotal_cents == 2100
    assert bill.discount_cents == 0
    assert bill.tax_cents == 126
    assert bill.total_cents == 2226


def test_tax_on_discounted_total():
    bill = price_order([("keyboard", 10000, 1)], [Coupon("percent", 10)])
    assert bill.subtotal_cents == 10000
    assert bill.discount_cents == 1000
    assert bill.tax_cents == 540
    assert bill.total_cents == 9540


def test_total_with_percent_and_fixed_coupon():
    bill = price_order(
        [("monitor", 20000, 1)],
        [Coupon("percent", 10), Coupon("fixed", 500)],
    )
    assert bill.discount_cents == 2500
    assert bill.tax_cents == 1050
    assert bill.total_cents == 18550


def test_percent_coupon_applied_to_subtotal_before_fixed():
    bill = price_order(
        [("bundle", 10000, 1)],
        [Coupon("fixed", 500), Coupon("percent", 10)],
    )
    assert bill.subtotal_cents == 10000
    assert bill.discount_cents == 1500


def test_discount_capped_at_subtotal():
    bill = price_order([("bundle", 3000, 1)], [Coupon("fixed", 10000)])
    assert bill.discount_cents == 3000


def test_tax_rounds_half_up():
    bill = price_order([("sample", 75, 1)], [])
    assert bill.subtotal_cents == 75
    assert bill.tax_cents == 5
    assert bill.total_cents == 80
