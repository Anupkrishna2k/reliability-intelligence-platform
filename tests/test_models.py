"""Money and enum rules on the order model."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import (
    Address,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_ADDRESS = Address(
    line1="1 Test St", city="Testville", state="TS", postal_code="00001"
)


def _order(*items: OrderItem) -> Order:
    return Order(
        order_id="ORD-TEST-0001",
        customer_id="CUS-0001",
        customer_name="Test Customer",
        customer_email="test@example.com",
        status=OrderStatus.PENDING,
        items=list(items),
        payment_method=PaymentMethod.CREDIT_CARD,
        shipping_address=_ADDRESS,
        billing_address=_ADDRESS,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_line_total_multiplies_quantity_by_price() -> None:
    item = OrderItem(sku="A", name="A", quantity=3, unit_price=10.005)

    assert item.line_total == 30.02


def test_shipping_is_flat_rate_below_threshold() -> None:
    order = _order(OrderItem(sku="A", name="A", quantity=1, unit_price=42.0))

    assert order.subtotal == 42.0
    assert order.shipping_cost == 9.99
    assert order.tax == 3.47
    assert order.total == 55.46


def test_shipping_is_free_at_threshold() -> None:
    order = _order(OrderItem(sku="A", name="A", quantity=1, unit_price=100.0))

    assert order.shipping_cost == 0.0
    assert order.total == 108.25


def test_shipping_is_free_above_threshold() -> None:
    order = _order(OrderItem(sku="A", name="A", quantity=1, unit_price=250.0))

    assert order.shipping_cost == 0.0


def test_item_count_sums_quantities_not_lines() -> None:
    order = _order(
        OrderItem(sku="A", name="A", quantity=2, unit_price=5.0),
        OrderItem(sku="B", name="B", quantity=3, unit_price=5.0),
    )

    assert len(order.items) == 2
    assert order.item_count == 5


def test_money_fields_are_rounded_to_cents() -> None:
    order = _order(OrderItem(sku="A", name="A", quantity=3, unit_price=19.99))

    assert order.subtotal == 59.97
    assert order.shipping_cost == 9.99
    assert order.tax == 4.95
    assert order.total == 74.91


def test_computed_fields_are_serialised() -> None:
    payload = _order(OrderItem(sku="A", name="A", quantity=1, unit_price=10.0)).model_dump(
        mode="json"
    )

    assert payload["subtotal"] == 10.0
    assert payload["item_count"] == 1
    assert payload["shipping_cost"] == 9.99
    assert payload["tax"] == 0.83
    assert payload["total"] == 20.82


def test_rejects_empty_order() -> None:
    with pytest.raises(ValueError):
        _order()


@pytest.mark.parametrize("quantity", [0, -1])
def test_rejects_non_positive_quantity(quantity: int) -> None:
    with pytest.raises(ValueError):
        OrderItem(sku="A", name="A", quantity=quantity, unit_price=1.0)


def test_rejects_negative_price() -> None:
    with pytest.raises(ValueError):
        OrderItem(sku="A", name="A", quantity=1, unit_price=-1.0)


def test_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        OrderItem(sku="A", name="A", quantity=1, unit_price=1.0, discount=0.5)
