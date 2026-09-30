"""Read access to the order store.

The repository is the only place the rest of the app reads orders from, so the
in-memory implementation used in this stage can be swapped for a database
without touching the routers.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.models import Order, OrderStatus
from app.seed import seed_orders


class OrderRepository:
    """In-memory, read-only store of orders."""

    def __init__(self, orders: Sequence[Order] | None = None) -> None:
        self._loaded = False
        source = list(orders) if orders is not None else list(seed_orders())
        self._orders: dict[str, Order] = {order.order_id: order for order in source}
        self._sorted_ids: list[str] = sorted(self._orders, reverse=True)
        self._loaded = True

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def count(self) -> int:
        return len(self._orders)

    def get_order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def list_orders(
        self,
        *,
        status: OrderStatus | None = None,
        customer_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Order], int]:
        """Return a page of orders and the total number of matches.

        Results are newest first, which is what a fulfilment or support agent
        needs when triaging a customer report.
        """
        matches = [
            self._orders[order_id]
            for order_id in self._sorted_ids
            if (status is None or self._orders[order_id].status == status)
            and (customer_id is None or self._orders[order_id].customer_id == customer_id)
        ]
        return matches[offset : offset + limit], len(matches)

    def to_summary(self, order: Order) -> dict[str, object]:
        """Project an order down to the fields the list endpoint returns."""
        return {
            "order_id": order.order_id,
            "customer_id": order.customer_id,
            "customer_name": order.customer_name,
            "status": order.status,
            "item_count": order.item_count,
            "total": order.total,
            "currency": order.currency,
            "created_at": order.created_at,
        }
