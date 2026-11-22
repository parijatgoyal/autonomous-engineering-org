from decimal import Decimal

import pytest

from services.inventory.stock import Inventory, OutOfStockError
from services.notifications.email import OUTBOX
from services.orders.checkout import Order, checkout
from services.pricing.pricing import LineItem


def test_checkout_prices_and_notifies():
    OUTBOX.clear()
    order = Order("o-1", "a@example.com", [LineItem("A", Decimal("10.00"), 1)])
    checkout(order, Inventory({"A": 5}))
    assert order.total == Decimal("10.80")
    assert OUTBOX[-1]["subject"] == "Order o-1 confirmed"


def test_checkout_out_of_stock():
    with pytest.raises(OutOfStockError):
        checkout(Order("o-2", "a@example.com", [LineItem("A", Decimal("1"), 9)]), Inventory({"A": 1}))
