"""Checkout, cancellation and refund orchestration."""

import uuid
from dataclasses import dataclass

from .auth import PermissionDenied, User, can_refund
from .billing import Coupon, price_order
from .db import Store
from .inventory import Inventory

PRICE_LIST: dict[str, int] = {
    "keyboard": 4500,
    "mouse": 1800,
    "monitor": 22000,
    "cable": 700,
}


@dataclass(frozen=True)
class Order:
    order_id: str
    user_id: str
    lines: tuple[tuple[str, int], ...]
    subtotal_cents: int
    discount_cents: int
    tax_cents: int
    total_cents: int
    status: str = "created"


class OrderService:
    def __init__(
        self,
        inventory: Inventory,
        store: Store,
        price_list: dict[str, int] | None = None,
    ) -> None:
        self.inventory = inventory
        self.store = store
        self.price_list = dict(PRICE_LIST if price_list is None else price_list)

    def checkout(
        self,
        user: User,
        cart: list[tuple[str, int]],
        coupons: list[Coupon] | tuple[Coupon, ...] = (),
    ) -> Order:
        if not cart:
            raise ValueError("cart must not be empty")

        lines = [(sku, self.price_list[sku], qty) for sku, qty in cart]

        reserved: list[tuple[str, int]] = []
        try:
            for sku, qty in cart:
                self.inventory.reserve(sku, qty)
                reserved.append((sku, qty))
        except Exception:
            for sku, qty in reserved:
                self.inventory.release(sku, qty)
            raise

        bill = price_order(lines, list(coupons))
        order = Order(
            order_id=uuid.uuid4().hex,
            user_id=user.user_id,
            lines=tuple((sku, qty) for sku, qty in cart),
            subtotal_cents=bill.subtotal_cents,
            discount_cents=bill.discount_cents,
            tax_cents=bill.tax_cents,
            total_cents=bill.total_cents,
        )
        self.store.save(order)
        return order

    def cancel(self, order_id: str) -> None:
        record = self.store.get(order_id)
        if record is None:
            raise KeyError(f"unknown order: {order_id}")
        for sku, qty in record["lines"]:
            self.inventory.release(sku, qty)
        self.store.update_status(order_id, "cancelled")

    def refund(self, user: User, order_id: str) -> None:
        if not can_refund(user):
            raise PermissionDenied(f"user {user.user_id!r} cannot issue refunds")
        if self.store.get(order_id) is None:
            raise KeyError(f"unknown order: {order_id}")
        self.store.update_status(order_id, "refunded")
