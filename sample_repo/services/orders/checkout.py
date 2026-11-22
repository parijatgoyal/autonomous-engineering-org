"""Checkout flow: reserve stock, price the order, send confirmation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from services.inventory.stock import Inventory
from services.notifications.email import send_order_confirmation
from services.pricing.pricing import LineItem, calculate_total


@dataclass
class Order:
    order_id: str
    customer_email: str
    items: list[LineItem]
    total: Decimal | None = None


def checkout(order: Order, inventory: Inventory) -> Order:
    for item in order.items:
        inventory.reserve(item.sku, item.quantity)
    order.total = calculate_total(order.items)
    send_order_confirmation(order.customer_email, order.order_id, order.total)
    return order
