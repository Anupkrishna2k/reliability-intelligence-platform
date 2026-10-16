"""Domain and API schemas for the Orders API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.config import MAX_FAILURE_LATENCY_MS, MAX_FAILURE_RATE_PERCENT

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


class FailureConfig(BaseModel):
    """Active failure-injection scenario, as returned by the control API."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(description="Whether the scenario is currently active.")
    latency_ms: int = Field(
        ge=0,
        le=MAX_FAILURE_LATENCY_MS,
        description="Artificial delay added to each request, in milliseconds.",
    )
    failure_rate_percent: float = Field(
        ge=0,
        le=MAX_FAILURE_RATE_PERCENT,
        description="Probability that a request is answered with HTTP 500, in percent.",
    )


class FailureConfigUpdate(BaseModel):
    """Request body for configuring the failure-injection scenario."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, description="Turn the scenario on or off.")
    latency_ms: int = Field(
        default=0,
        ge=0,
        le=MAX_FAILURE_LATENCY_MS,
        description="Artificial delay added to each request, in milliseconds.",
    )
    failure_rate_percent: float = Field(
        default=0.0,
        ge=0,
        le=MAX_FAILURE_RATE_PERCENT,
        description="Probability that a request is answered with HTTP 500, in percent.",
    )


#: Reliability status vocabulary, shared by the availability and latency SLIs.
#: ``insufficient_data`` is a first-class outcome: an empty window must never be
#: reported as 100% availability.
ReliabilityStatus = Literal["meeting_target", "at_risk", "breached", "insufficient_data"]


class AvailabilitySli(BaseModel):
    """Availability indicator over the rolling observation window."""

    model_config = ConfigDict(extra="forbid")

    target: float = Field(
        gt=0, lt=1, description="Configured availability SLO (ratio)."
    )
    observed: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Observed availability (successful / eligible), or null if insufficient data.",
    )
    status: ReliabilityStatus
    window_seconds: int = Field(ge=1)
    eligible_requests: int = Field(ge=0)
    successful_requests: int = Field(ge=0)
    failed_requests: int = Field(ge=0, description="Eligible requests answered with HTTP 5xx.")
    sufficient_data: bool


class LatencySli(BaseModel):
    """Latency-compliance indicator over the rolling observation window."""

    model_config = ConfigDict(extra="forbid")

    target: float = Field(gt=0, lt=1, description="Configured latency-compliance SLO (ratio).")
    observed: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description=(
            "Fraction of eligible requests that completed within the threshold, "
            "or null if insufficient data."
        ),
    )
    status: ReliabilityStatus
    window_seconds: int = Field(ge=1)
    eligible_requests: int = Field(ge=0)
    within_threshold_requests: int = Field(ge=0)
    latency_threshold_ms: float = Field(gt=0)
    sufficient_data: bool


class ErrorBudget(BaseModel):
    """Error budget derived from one SLO.

    ``consumed_fraction`` is deliberately unbounded: a value above ``1.0`` means
    the budget has been exceeded, and ``exhausted`` is the explicit flag for
    that. ``remaining_fraction`` is floored at ``0.0`` so a dashboard never
    shows a misleading negative remainder; over-consumption is visible through
    ``consumed_fraction`` instead.
    """

    model_config = ConfigDict(extra="forbid")

    slo: Literal["availability", "latency"]
    target: float = Field(gt=0, lt=1)
    observed: float | None = Field(default=None, ge=0, le=1)
    allowed_failure_fraction: float = Field(ge=0, lt=1)
    observed_failure_fraction: float | None = Field(default=None, ge=0, le=1)
    consumed_fraction: float | None = Field(
        default=None, ge=0, description="Observed/allowed failure fraction; may exceed 1."
    )
    remaining_fraction: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="max(0, 1 - consumed_fraction); floored, never negative.",
    )
    exhausted: bool
    sufficient_data: bool


class ReliabilityResponse(BaseModel):
    """Full reliability evaluation for the rolling observation window."""

    model_config = ConfigDict(extra="forbid")

    service: str
    generated_at: datetime
    evaluation_window_seconds: int = Field(ge=1)
    status: ReliabilityStatus = Field(
        description="Most severe status across availability and latency."
    )
    availability: AvailabilitySli
    latency: LatencySli
    error_budget: ErrorBudget = Field(description="Availability error budget.")
    latency_error_budget: ErrorBudget
    status_definitions: dict[str, str]
    notes: list[str]


# --- incidents ---------------------------------------------------------------
# An incident is a lifecycle record opened by the detector when a reliability
# rule fires, updated while it keeps firing, and resolved when the rule has been
# clear for a documented number of consecutive evaluations. Incidents are held
# in process memory only: they are lost on restart and easy to move to a store.


class IncidentType(str, Enum):
    """Coarse incident identity. At most one active incident per type."""

    AVAILABILITY = "availability"
    LATENCY = "latency"


class IncidentSeverity(str, Enum):
    WARNING = "warning"
    CRITICAL = "critical"


class IncidentStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


#: The detection rules that can contribute a trigger to an incident. Kept as
#: plain strings so they survive JSON round-trips and read clearly in evidence.
RULE_AVAILABILITY_BREACH = "availability_slo_breach"
RULE_AVAILABILITY_BUDGET_EXHAUSTED = "availability_error_budget_exhausted"
RULE_LATENCY_BREACH = "latency_slo_breach"


class IncidentTrigger(BaseModel):
    """One rule that is currently firing on an incident."""

    model_config = ConfigDict(extra="forbid")

    rule: str = Field(description="Detection rule id, e.g. 'availability_slo_breach'.")
    severity: IncidentSeverity
    detected_at: datetime = Field(
        description="When this rule first fired on the incident."
    )


class IncidentEvidence(BaseModel):
    """Bounded, structured evidence captured from the reliability evaluation.

    Only aggregate numbers from the rolling window are kept - never logs, request
    payloads or identifiers - so the record is cheap to store and safe to expose.
    """

    model_config = ConfigDict(extra="forbid")

    observed_at: datetime = Field(description="When this evidence was captured.")
    window_seconds: int = Field(ge=1, description="Rolling observation window.")
    eligible_requests: int = Field(ge=0)
    failed_requests: int = Field(ge=0)
    observed_availability: float | None = Field(default=None, ge=0, le=1)
    availability_target: float = Field(gt=0, lt=1)
    observed_latency: float | None = Field(default=None, ge=0, le=1)
    latency_target: float = Field(gt=0, lt=1)
    latency_threshold_ms: float = Field(gt=0)
    error_budget_consumed: float | None = Field(
        default=None, ge=0, description="Consumed availability budget; may exceed 1.0."
    )
    error_budget_exhausted: bool
    summary: str = Field(description="Short human-readable summary of the evidence.")


class Incident(BaseModel):
    """A detected reliability incident and its lifecycle state."""

    model_config = ConfigDict(extra="forbid")

    incident_id: str
    type: IncidentType
    severity: IncidentSeverity
    status: IncidentStatus
    detected_at: datetime = Field(description="First time the incident was detected.")
    updated_at: datetime = Field(description="Last time the incident was updated.")
    triggers: list[IncidentTrigger] = Field(
        description="Rules currently firing, with the time each started."
    )
    evidence: IncidentEvidence
    detection_count: int = Field(
        ge=1, description="Number of evaluations that found the incident active."
    )
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    duration_seconds: float | None = Field(
        default=None, ge=0, description="detected_at to resolved_at, when resolved."
    )


class IncidentCounts(BaseModel):
    """Summary counts across all retained incidents."""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    active: int = Field(ge=0, description="Incidents that are not resolved.")
    by_status: dict[str, int]
    by_severity: dict[str, int]


class IncidentPagination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    returned: int = Field(ge=0)
    has_more: bool


class IncidentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incidents: list[Incident]
    pagination: IncidentPagination
    counts: IncidentCounts


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, object] | None = None


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorBody
    request_id: str | None = None
