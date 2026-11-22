from decimal import Decimal

import pytest

from services.pricing.pricing import LineItem, calculate_total, subtotal


def test_subtotal_sums_line_items():
    items = [LineItem("A", Decimal("10.00"), 2), LineItem("B", Decimal("5.50"), 1)]
    assert subtotal(items) == Decimal("25.50")


def test_total_includes_tax():
    assert calculate_total([LineItem("A", Decimal("100.00"), 1)]) == Decimal("108.00")


def test_rejects_non_positive_quantity():
    with pytest.raises(ValueError):
        subtotal([LineItem("A", Decimal("1.00"), 0)])
