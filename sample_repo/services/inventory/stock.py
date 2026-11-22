"""In-memory stock levels and reservations."""

from __future__ import annotations


class OutOfStockError(Exception):
    pass


class Inventory:
    def __init__(self, levels: dict[str, int] | None = None):
        self._levels = dict(levels or {})

    def available(self, sku: str) -> int:
        return self._levels.get(sku, 0)

    def reserve(self, sku: str, quantity: int) -> None:
        if self.available(sku) < quantity:
            raise OutOfStockError(sku)
        self._levels[sku] -= quantity
