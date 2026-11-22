"""Outbound email (stubbed: records messages instead of sending)."""

from __future__ import annotations

from decimal import Decimal

OUTBOX: list[dict] = []


def send_order_confirmation(to: str, order_id: str, total: Decimal) -> None:
    OUTBOX.append({"to": to, "subject": f"Order {order_id} confirmed", "total": str(total)})
