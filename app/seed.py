"""In-memory order data.

This stage has no database, so the service serves a small fixed dataset that is
materialised at start-up. It is replaced by a real repository in a later stage;
the rest of the app only depends on :class:`~app.repository.OrderRepository`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import (
    Address,
    FulfilmentChannel,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
)

_UTC = timezone.utc


def _days_ago(days: int, hours: int = 0) -> datetime:
    return datetime.now(_UTC) - timedelta(days=days, hours=hours)


def _in(days: int) -> datetime:
    return datetime.now(_UTC) + timedelta(days=days)


def _address(
    line1: str,
    city: str,
    state: str,
    postal_code: str,
    line2: str | None = None,
) -> Address:
    return Address(
        line1=line1,
        line2=line2,
        city=city,
        state=state,
        postal_code=postal_code,
        country="US",
    )


_SEED_ORDERS: tuple[Order, ...] = (
    Order(
        order_id="ORD-2026-0001",
        customer_id="CUS-1042",
        customer_name="Dana Whitfield",
        customer_email="dana.whitfield@example.com",
        status=OrderStatus.DELIVERED,
        items=[
            OrderItem(sku="KB-87-BLK", name="Mechanical Keyboard 87", quantity=1, unit_price=129.99),
            OrderItem(sku="USB-C-2M", name="USB-C Cable 2m", quantity=2, unit_price=12.5),
        ],
        currency="USD",
        payment_method=PaymentMethod.CREDIT_CARD,
        fulfilment_channel=FulfilmentChannel.WEB,
        shipping_address=_address("4821 Ridgeview Dr", "Portland", "OR", "97209"),
        billing_address=_address("4821 Ridgeview Dr", "Portland", "OR", "97209"),
        tracking_number="1Z999AA10123456784",
        estimated_delivery=_days_ago(2),
        created_at=_days_ago(6),
        updated_at=_days_ago(2),
    ),
    Order(
        order_id="ORD-2026-0002",
        customer_id="CUS-1187",
        customer_name="Marcus Delacroix",
        customer_email="marcus.d@example.com",
        status=OrderStatus.SHIPPED,
        items=[
            OrderItem(sku="MON-27-4K", name="27\" 4K Monitor", quantity=1, unit_price=449.0),
            OrderItem(sku="ARM-DUAL", name="Dual Monitor Arm", quantity=1, unit_price=79.95),
        ],
        currency="USD",
        payment_method=PaymentMethod.PAYPAL,
        fulfilment_channel=FulfilmentChannel.MOBILE_APP,
        shipping_address=_address("17 Harborview Ter", "Seattle", "WA", "98109", line2="Apt 412"),
        billing_address=_address("17 Harborview Ter", "Seattle", "WA", "98109", line2="Apt 412"),
        tracking_number="9400111899560001234567",
        estimated_delivery=_in(1),
        created_at=_days_ago(3),
        updated_at=_days_ago(1, 4),
    ),
    Order(
        order_id="ORD-2026-0003",
        customer_id="CUS-1042",
        customer_name="Dana Whitfield",
        customer_email="dana.whitfield@example.com",
        status=OrderStatus.PROCESSING,
        items=[
            OrderItem(sku="DOCK-TB4", name="Thunderbolt 4 Dock", quantity=1, unit_price=289.99),
        ],
        currency="USD",
        payment_method=PaymentMethod.CREDIT_CARD,
        fulfilment_channel=FulfilmentChannel.WEB,
        shipping_address=_address("4821 Ridgeview Dr", "Portland", "OR", "97209"),
        billing_address=_address("4821 Ridgeview Dr", "Portland", "OR", "97209"),
        estimated_delivery=_in(4),
        notes="Leave with front desk.",
        created_at=_days_ago(1, 6),
        updated_at=_days_ago(0, 5),
    ),
    Order(
        order_id="ORD-2026-0004",
        customer_id="CUS-2205",
        customer_name="Priya Raghunathan",
        customer_email="priya.raghunathan@example.com",
        status=OrderStatus.PENDING,
        items=[
            OrderItem(sku="HDPH-NC-ANC", name="ANC Over-Ear Headphones", quantity=1, unit_price=349.0),
            OrderItem(sku="CAB-HDMI-3", name="HDMI 2.1 Cable 3m", quantity=2, unit_price=14.99),
            OrderItem(sku="ADP-USB-C-65", name="65W USB-C Charger", quantity=1, unit_price=39.95),
        ],
        currency="USD",
        payment_method=PaymentMethod.DEBIT_CARD,
        fulfilment_channel=FulfilmentChannel.WEB,
        shipping_address=_address("900 Camino Del Rio", "San Jose", "CA", "95126", line2="Suite 210"),
        billing_address=_address("900 Camino Del Rio", "San Jose", "CA", "95126", line2="Suite 210"),
        estimated_delivery=_in(6),
        created_at=_days_ago(0, 8),
        updated_at=_days_ago(0, 8),
    ),
    Order(
        order_id="ORD-2026-0005",
        customer_id="CUS-2205",
        customer_name="Priya Raghunathan",
        customer_email="priya.raghunathan@example.com",
        status=OrderStatus.CANCELLED,
        items=[
            OrderItem(sku="WRLSS-M2-DUAL", name="Dual-Band Mesh Wi-Fi (2-pack)", quantity=1, unit_price=199.99),
        ],
        currency="USD",
        payment_method=PaymentMethod.STORE_CREDIT,
        fulfilment_channel=FulfilmentChannel.PHONE,
        shipping_address=_address("900 Camino Del Rio", "San Jose", "CA", "95126", line2="Suite 210"),
        billing_address=_address("900 Camino Del Rio", "San Jose", "CA", "95126", line2="Suite 210"),
        notes="Cancelled at customer request - ordered wrong variant.",
        created_at=_days_ago(9),
        updated_at=_days_ago(7),
    ),
    Order(
        order_id="ORD-2026-0006",
        customer_id="CUS-3310",
        customer_name="Tomás Ferreira",
        customer_email="tomas.ferreira@example.com",
        status=OrderStatus.CONFIRMED,
        items=[
            OrderItem(sku="NAS-2BAY", name="2-Bay NAS Enclosure", quantity=1, unit_price=529.0),
            OrderItem(sku="HDD-NAS-8T", name="NAS HDD 8TB", quantity=2, unit_price=159.0),
        ],
        currency="USD",
        payment_method=PaymentMethod.BANK_TRANSFER,
        fulfilment_channel=FulfilmentChannel.WEB,
        shipping_address=_address("733 Congress Ave", "Austin", "TX", "78701"),
        billing_address=_address("1200 Commerce St", "Austin", "TX", "78701"),
        estimated_delivery=_in(8),
        created_at=_days_ago(0, 2),
        updated_at=_days_ago(0, 1),
    ),
)


def seed_orders() -> tuple[Order, ...]:
    """Return the fixture orders backing the read-only API."""
    return _SEED_ORDERS
