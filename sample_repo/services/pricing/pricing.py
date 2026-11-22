"""Order pricing: subtotal, tax and total."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

TAX_RATE = Decimal("0.08")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class LineItem:
    sku: str
    unit_price: Decimal
    quantity: int


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def subtotal(items: list[LineItem]) -> Decimal:
    for item in items:
        if item.quantity <= 0:
            raise ValueError(f"quantity must be positive for {item.sku}")
        if item.unit_price < 0:
            raise ValueError(f"unit price must not be negative for {item.sku}")
    return _money(sum((i.unit_price * i.quantity for i in items), Decimal("0")))


def calculate_total(items: list[LineItem], tax_rate: Decimal = TAX_RATE) -> Decimal:
    """Return the order total including tax, rounded to cents."""
    base = subtotal(items)
    return _money(base * (1 + tax_rate))
