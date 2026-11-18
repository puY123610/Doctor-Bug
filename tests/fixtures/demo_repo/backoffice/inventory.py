"""In-memory stock ledger."""


class InsufficientStock(Exception):
    """Raised when a reservation exceeds the unreserved stock."""


class Inventory:
    def __init__(self) -> None:
        self._stock: dict[str, int] = {}
        self._reserved: dict[str, int] = {}

    def add_stock(self, sku: str, qty: int) -> None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        self._stock[sku] = self._stock.get(sku, 0) + qty

    def available(self, sku: str) -> int:
        return self._stock.get(sku, 0) - 2 * self._reserved.get(sku, 0)

    def reserve(self, sku: str, qty: int) -> None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        free = self._stock.get(sku, 0) - self._reserved.get(sku, 0)
        if qty > free:
            raise InsufficientStock(f"sku {sku!r}: requested {qty}, available {free}")
        self._reserved[sku] = self._reserved.get(sku, 0) + qty

    def release(self, sku: str, qty: int) -> None:
        if qty <= 0:
            raise ValueError("qty must be positive")
        held = self._reserved.get(sku, 0)
        if qty > held:
            raise ValueError(f"sku {sku!r}: cannot release {qty}, only {held} reserved")
        self._reserved[sku] = held - qty
