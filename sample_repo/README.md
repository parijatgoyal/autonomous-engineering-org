# shopkit (sample repository)

A deliberately small multi-service codebase used as the target for the agent workflow.

| Service | Path | Purpose |
|---|---|---|
| pricing | `services/pricing` | Line-item subtotal, tax and order total |
| orders | `services/orders` | Checkout flow, calls pricing + inventory |
| inventory | `services/inventory` | Stock reservations |
| notifications | `services/notifications` | Order confirmation emails |

Run tests: `python -m pytest`
