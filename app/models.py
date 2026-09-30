"""Domain and API schemas for the Orders API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

# --- pricing rules -----------------------------------------------------------
# Kept as module constants so the API can describe one consistent money model
# without reaching into configuration or a database.
SHIPPING_FLAT_RATE = 9.99
FREE_SHIPPING_THRESHOLD = 100.0
TAX_RATE = 0.0825


class OrderStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    PROCESSING = "processing"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class PaymentMethod(str, Enum):
    CREDIT_CARD = "credit_card"
    DEBIT_CARD = "debit_card"
    PAYPAL = "paypal"
    BANK_TRANSFER = "bank_transfer"
    STORE_CREDIT = "store_credit"


class FulfilmentChannel(str, Enum):
    WEB = "web"
    MOBILE_APP = "mobile_app"
    PHONE = "phone"
    STORE = "store"


class Address(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line1: str
    line2: str | None = None
    city: str
    state: str
    postal_code: str
    country: str = "US"


class OrderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str
    name: str
    quantity: int = Field(ge=1)
    unit_price: float = Field(ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def line_total(self) -> float:
        return round(self.quantity * self.unit_price, 2)


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    order_id: str
    customer_id: str
    customer_name: str
    customer_email: str
    status: OrderStatus
    items: list[OrderItem] = Field(min_length=1)
    currency: str = "USD"
    payment_method: PaymentMethod
    fulfilment_channel: FulfilmentChannel = FulfilmentChannel.WEB
    shipping_address: Address
    billing_address: Address
    tracking_number: str | None = None
    estimated_delivery: datetime | None = None
    notes: str | None = None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def item_count(self) -> int:
        return sum(item.quantity for item in self.items)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def subtotal(self) -> float:
        return round(sum(item.line_total for item in self.items), 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def shipping_cost(self) -> float:
        if self.subtotal >= FREE_SHIPPING_THRESHOLD:
            return 0.0
        return SHIPPING_FLAT_RATE

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tax(self) -> float:
        return round(self.subtotal * TAX_RATE, 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> float:
        return round(self.subtotal + self.shipping_cost + self.tax, 2)


class OrderSummary(BaseModel):
    """Trimmed order projection used by the list endpoint."""

    model_config = ConfigDict(extra="forbid")

    order_id: str
    customer_id: str
    customer_name: str
    status: OrderStatus
    item_count: int
    total: float
    currency: str
    created_at: datetime


class Pagination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0, description="Total orders matching the filters")
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    returned: int = Field(ge=0)
    has_more: bool


class OrderFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: OrderStatus | None = None
    customer_id: str | None = None


class OrderListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orders: list[OrderSummary]
    pagination: Pagination
    filters: OrderFilters


class HealthResponse(BaseModel):
    """Liveness response. Answers 'is the process running?', nothing more."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    service: str
    version: str
    environment: str
    uptime_seconds: float = Field(ge=0)
    timestamp: datetime


class ReadinessCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: Literal["pass", "fail"]
    detail: str | None = None
    latency_ms: float = Field(ge=0)


class ReadinessResponse(BaseModel):
    """Readiness response. Answers 'can the process serve traffic?'."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "not_ready"]
    service: str
    version: str
    environment: str
    checks: list[ReadinessCheck]
    timestamp: datetime


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, object] | None = None


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorBody
    request_id: str | None = None
