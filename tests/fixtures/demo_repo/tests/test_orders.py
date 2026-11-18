"""Tests for checkout, cancellation, refund and storage integration."""

import pytest

from backoffice.auth import PermissionDenied, User
from backoffice.billing import Coupon
from backoffice.db import Store
from backoffice.inventory import InsufficientStock, Inventory
from backoffice.orders import OrderService


@pytest.fixture
def service():
    return OrderService(Inventory(), Store())


def test_checkout_creates_order_and_saves(service):
    service.inventory.add_stock("keyboard", 10)
    user = User("u-sales", ("sales",))

    order = service.checkout(user, [("keyboard", 2)])

    assert order.subtotal_cents == 9000
    assert order.discount_cents == 0
    assert order.tax_cents == 540
    assert order.total_cents == 9540
    record = service.store.get(order.order_id)
    assert record is not None
    assert record["user_id"] == "u-sales"
    assert record["status"] == "created"
    assert record["total_cents"] == 9540
    assert record["lines"] == [("keyboard", 2)]


def test_checkout_total_with_coupon(service):
    service.inventory.add_stock("keyboard", 10)
    user = User("u-sales", ("sales",))

    order = service.checkout(
        user,
        [("keyboard", 2)],
        [Coupon("percent", 10), Coupon("fixed", 300)],
    )

    assert order.subtotal_cents == 9000
    assert order.discount_cents == 1200
    assert order.tax_cents == 468
    assert order.total_cents == 8268


def test_stock_after_single_checkout(service):
    service.inventory.add_stock("mouse", 10)
    user = User("u-sales", ("sales",))

    service.checkout(user, [("mouse", 3)])

    assert service.inventory.available("mouse") == 7


def test_insufficient_stock_rolls_back(service):
    service.inventory.add_stock("keyboard", 5)
    service.inventory.add_stock("monitor", 1)
    user = User("u-sales", ("sales",))

    with pytest.raises(InsufficientStock):
        service.checkout(user, [("keyboard", 2), ("monitor", 3)])

    assert service.inventory.available("keyboard") == 5
    assert service.inventory.available("monitor") == 1
    order = service.checkout(user, [("keyboard", 2), ("monitor", 1)])
    assert order.total_cents > 0


def test_cancel_releases_reservation(service):
    service.inventory.add_stock("mouse", 4)
    user = User("u-sales", ("sales",))
    order = service.checkout(user, [("mouse", 3)])

    service.cancel(order.order_id)

    assert service.inventory.available("mouse") == 4
    assert service.store.get(order.order_id)["status"] == "cancelled"


def test_refund_rejected_for_support_role(service):
    service.inventory.add_stock("cable", 5)
    support = User("u-support", ("support",))
    order = service.checkout(support, [("cable", 1)])

    with pytest.raises(PermissionDenied):
        service.refund(support, order.order_id)

    assert service.store.get(order.order_id)["status"] == "created"


def test_refund_marks_order_refunded(service):
    service.inventory.add_stock("cable", 5)
    sales = User("u-sales", ("sales",))
    finance = User("u-finance", ("finance",))
    order = service.checkout(sales, [("cable", 1)])

    service.refund(finance, order.order_id)

    assert service.store.get(order.order_id)["status"] == "refunded"


def test_full_checkout_flow(service):
    service.inventory.add_stock("keyboard", 6)
    support = User("u-support", ("support",))

    order = service.checkout(
        support,
        [("keyboard", 2)],
        [Coupon("percent", 10)],
    )

    record = service.store.get(order.order_id)
    assert record["status"] == "created"
    # keyboard 4500 x 2 = 9000; percent 10% -> 900; tax 6% of 8100 = 486.
    assert order.subtotal_cents == 9000
    assert order.total_cents == 8586
    assert record["total_cents"] == order.total_cents
    assert service.inventory.available("keyboard") == 4

    with pytest.raises(PermissionDenied):
        service.refund(support, order.order_id)

    finance = User("u-finance", ("finance",))
    service.refund(finance, order.order_id)
    assert service.store.get(order.order_id)["status"] == "refunded"
