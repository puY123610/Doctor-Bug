"""Small back-office backend used as a demo target repository."""

from .auth import PermissionDenied, User, can_refund, require_role
from .billing import Bill, Coupon, price_order
from .db import Store
from .inventory import InsufficientStock, Inventory
from .orders import PRICE_LIST, Order, OrderService

__all__ = [
    "Bill",
    "Coupon",
    "InsufficientStock",
    "Inventory",
    "Order",
    "OrderService",
    "PRICE_LIST",
    "PermissionDenied",
    "Store",
    "User",
    "can_refund",
    "price_order",
    "require_role",
]
