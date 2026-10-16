"""Prometheus instrumentation for the Orders API.

Design notes
------------
* Every app instance owns a **private** ``CollectorRegistry`` rather than using
  the global default. This keeps metric state scoped to the app (important for
  tests and for running more than one app in a process) and makes the exposed
  set explicit instead of whatever happens to be registered globally.
* **Label cardinality is bounded by construction.** Every label value is
  normalised through a whitelist before it reaches a metric, so a hostile or
  buggy caller cannot create an unbounded number of time series. Identifiers
  (``order_id``, ``customer_id``, ``request_id``, timestamps) are never used
  as labels; the request path is recorded as its *route template*
  (``/api/orders/{order_id}``) instead.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from app.config import Settings

if TYPE_CHECKING:
    from app.models import ErrorBudget, ReliabilityResponse

#: Label values for the ``slo`` dimension on the reliability gauges. Two
#: bounded values keep the reliability export to a handful of series.
SLO_AVAILABILITY = "availability"
SLO_LATENCY = "latency"
SLO_VALUES = (SLO_AVAILABILITY, SLO_LATENCY)

#: Prometheus renders a ``float('nan')`` gauge as ``NaN``. It is used for
#: "insufficient data" so a dashboard shows a gap rather than a false 0/100%.
_NAN = float("nan")

# Prometheus metric names cannot contain dashes, so the service name is
# spelled out rather than derived from settings.app_name.
METRIC_NAMESPACE = "orders_api"

#: Route label used when no route template matches, e.g. a scanner hitting a
#: random URL. Collapsing these into one series is what stops 404 traffic from
#: exploding cardinality.
UNMATCHED_ROUTE = "unmatched"

#: Incident label vocabularies. Incident *ids* are never labels: they are
#: unbounded, so only the bounded type/severity dimensions are exported.
INCIDENT_TYPE_AVAILABILITY = "availability"
INCIDENT_TYPE_LATENCY = "latency"
INCIDENT_TYPE_VALUES = (INCIDENT_TYPE_AVAILABILITY, INCIDENT_TYPE_LATENCY)
INCIDENT_SEVERITY_WARNING = "warning"
INCIDENT_SEVERITY_CRITICAL = "critical"
INCIDENT_SEVERITY_VALUES = (INCIDENT_SEVERITY_WARNING, INCIDENT_SEVERITY_CRITICAL)
UNKNOWN_INCIDENT_TYPE = "unknown"
UNKNOWN_INCIDENT_SEVERITY = "unknown"

#: Incident durations run from seconds to hours, so the buckets span that range.
INCIDENT_DURATION_BUCKETS = (
    1.0,
    5.0,
    15.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
    1800.0,
    3600.0,
    7200.0,
    86400.0,
)

#: Buckets are in seconds. Chosen around the latency range a read-only JSON API
#: should sit in, so a regression shows up as a p95/p99 shift rather than
#: hiding in the +Inf bucket.
HTTP_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
LOOKUP_DURATION_BUCKETS = (0.0005, 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.5)
ORDER_COUNT_BUCKETS = (0, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000)

#: Bounded label vocabularies. Anything outside these is folded into the
#: catch-all value rather than creating a new series.
KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
OTHER_METHOD = "OTHER"
UNKNOWN_STATUS = "unknown"
UNKNOWN_OUTCOME = "unknown"
OTHER_FILTER = "other"
NO_FILTER = "none"

LOOKUP_SUCCESS = "success"
LOOKUP_NOT_FOUND = "not_found"


def _normalise_method(method: str | None) -> str:
    """Upper-case and whitelist the HTTP method."""
    value = (method or "").upper()
    return value if value in KNOWN_METHODS else OTHER_METHOD


def _normalise_status(status_code: Any) -> str:
    """Clamp the status code to the valid HTTP range."""
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return UNKNOWN_STATUS
    return str(code) if 100 <= code <= 599 else UNKNOWN_STATUS


def _normalise_outcome(outcome: str) -> str:
    return outcome if outcome in (LOOKUP_SUCCESS, LOOKUP_NOT_FOUND) else UNKNOWN_OUTCOME


def _normalise_filter(status_filter: str | None, allowed: Iterable[str]) -> str:
    """Map a status filter onto a bounded label value."""
    if not status_filter:
        return NO_FILTER
    return status_filter if status_filter in set(allowed) else OTHER_FILTER


def _normalise_incident_type(incident_type: str) -> str:
    return incident_type if incident_type in INCIDENT_TYPE_VALUES else UNKNOWN_INCIDENT_TYPE


def _normalise_incident_severity(severity: str) -> str:
    return severity if severity in INCIDENT_SEVERITY_VALUES else UNKNOWN_INCIDENT_SEVERITY


_PATH_PARAM = re.compile(r"\{([^{}:]+)(?::([^{}]+))?\}")


def _template_to_regex(template: str) -> re.Pattern[str]:
    """Compile a route template like ``/api/orders/{order_id}`` to a regex."""
    parts: list[str] = []
    position = 0
    for match in _PATH_PARAM.finditer(template):
        parts.append(re.escape(template[position : match.start()]))
        name, converter = match.group(1).strip(), match.group(2)
        # A `path` converter may span slashes; a plain parameter may not.
        parts.append(f"(?P<{name}>{'.+' if converter == 'path' else '[^/]+'})")
        position = match.end()
    parts.append(re.escape(template[position:]))
    return re.compile("^" + "".join(parts) + "$")


class RouteTemplateResolver:
    """Resolves a concrete request path to its low-cardinality route template.

    Templates are taken from the application's OpenAPI document so the metric
    always agrees with the documented API surface, without depending on router
    internals. A path that matches nothing resolves to ``None`` and callers
    substitute :data:`UNMATCHED_ROUTE`.
    """

    def __init__(self, templates: Iterable[str]) -> None:
        # Static templates are tried first so `/api/orders` wins over a
        # hypothetical `/api/{something}`.
        ordered = sorted(templates, key=lambda t: (t.count("{"), -len(t)))
        self._patterns: list[tuple[re.Pattern[str], str]] = [
            (_template_to_regex(template), template) for template in ordered
        ]

    def resolve(self, path: str) -> str | None:
        """Return the matching route template, or ``None`` if there is no match."""
        for pattern, template in self._patterns:
            if pattern.match(path):
                return template
        return None


class Metrics:
    """The metric objects for one application instance."""

    def __init__(self, settings: Settings) -> None:
        self.registry = CollectorRegistry()

        self.http_requests_total = Counter(
            f"{METRIC_NAMESPACE}_http_requests_total",
            "Total HTTP requests handled, by method, route template and status code.",
            ["method", "route", "status_code"],
            registry=self.registry,
        )
        self.http_request_duration_seconds = Histogram(
            f"{METRIC_NAMESPACE}_http_request_duration_seconds",
            "HTTP request latency in seconds, by method and route template.",
            ["method", "route"],
            buckets=HTTP_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.order_lookups_total = Counter(
            f"{METRIC_NAMESPACE}_order_lookups_total",
            "Single-order lookups by outcome ('success' or 'not_found').",
            ["outcome"],
            registry=self.registry,
        )
        self.order_lookup_duration_seconds = Histogram(
            f"{METRIC_NAMESPACE}_order_lookup_duration_seconds",
            "Order lookup latency in seconds, excluding HTTP overhead.",
            buckets=LOOKUP_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.orders_listed_total = Counter(
            f"{METRIC_NAMESPACE}_orders_listed_total",
            "Order list queries by requested status filter.",
            ["status_filter"],
            registry=self.registry,
        )
        self.orders_returned = Histogram(
            f"{METRIC_NAMESPACE}_orders_returned",
            "Number of orders returned per list query.",
            buckets=ORDER_COUNT_BUCKETS,
            registry=self.registry,
        )
        self.orders_matched = Histogram(
            f"{METRIC_NAMESPACE}_orders_matched",
            "Number of orders matching the filters per list query.",
            buckets=ORDER_COUNT_BUCKETS,
            registry=self.registry,
        )
        self.ready = Gauge(
            f"{METRIC_NAMESPACE}_ready",
            "Readiness as last reported by GET /ready (1 ready, 0 not ready).",
            registry=self.registry,
        )
        self.build_info = Gauge(
            f"{METRIC_NAMESPACE}_build_info",
            "Build metadata, always 1. Labels carry the version and environment.",
            ["version", "environment"],
            registry=self.registry,
        )

        # --- reliability / SLOs -------------------------------------------
        # These gauges are computed from the rolling reliability window. They
        # are refreshed on every Prometheus scrape (and when the reliability
        # endpoint is called), so scraping always sees current values without
        # recomputing on the request hot path.
        self.slo_target = Gauge(
            f"{METRIC_NAMESPACE}_slo_target",
            "Configured SLO target ratio, by SLO name ('availability' or 'latency').",
            ["slo"],
            registry=self.registry,
        )
        self.slo_observed = Gauge(
            f"{METRIC_NAMESPACE}_slo_observed",
            "Observed SLI ratio over the rolling window, by SLO name. NaN when insufficient data.",
            ["slo"],
            registry=self.registry,
        )
        self.slo_compliant = Gauge(
            f"{METRIC_NAMESPACE}_slo_compliant",
            "1 when the observed SLI meets the SLO target, 0 when breached, NaN when insufficient data.",
            ["slo"],
            registry=self.registry,
        )
        self.error_budget_allowed_fraction = Gauge(
            f"{METRIC_NAMESPACE}_error_budget_allowed_fraction",
            "Allowed failure fraction (1 - target) of the error budget, by SLO name.",
            ["slo"],
            registry=self.registry,
        )
        self.error_budget_observed_failure_fraction = Gauge(
            f"{METRIC_NAMESPACE}_error_budget_observed_failure_fraction",
            "Observed failure fraction (1 - observed SLI), by SLO name.",
            ["slo"],
            registry=self.registry,
        )
        self.error_budget_consumed_fraction = Gauge(
            f"{METRIC_NAMESPACE}_error_budget_consumed_fraction",
            "Fraction of the allowed error budget consumed; may exceed 1.0, by SLO name.",
            ["slo"],
            registry=self.registry,
        )
        self.error_budget_remaining_fraction = Gauge(
            f"{METRIC_NAMESPACE}_error_budget_remaining_fraction",
            "Remaining error budget fraction, floored at 0.0, by SLO name.",
            ["slo"],
            registry=self.registry,
        )
        self.error_budget_exhausted = Gauge(
            f"{METRIC_NAMESPACE}_error_budget_exhausted",
            "1 when the error budget is consumed/exceeded, 0 otherwise, by SLO name.",
            ["slo"],
            registry=self.registry,
        )

        # --- incidents -----------------------------------------------------
        # Low-cardinality only: incident ids, timestamps and other unbounded
        # values are deliberately absent.
        self.incidents_active = Gauge(
            f"{METRIC_NAMESPACE}_incidents_active",
            "Number of incidents that are not yet resolved.",
            registry=self.registry,
        )
        self.incident_detections_total = Counter(
            f"{METRIC_NAMESPACE}_incident_detections_total",
            "Incidents opened by the detector, by incident type and severity.",
            ["type", "severity"],
            registry=self.registry,
        )
        self.incident_resolutions_total = Counter(
            f"{METRIC_NAMESPACE}_incident_resolutions_total",
            "Incidents resolved, by incident type.",
            ["type"],
            registry=self.registry,
        )
        self.incident_duration_seconds = Histogram(
            f"{METRIC_NAMESPACE}_incident_duration_seconds",
            "Time from detection to resolution for resolved incidents, by type.",
            ["type"],
            buckets=INCIDENT_DURATION_BUCKETS,
            registry=self.registry,
        )

        # Version and environment are constant per deployment, so this is one
        # bounded series rather than a per-request label.
        self.build_info.labels(
            version=settings.app_version, environment=settings.environment
        ).set(1)
        self.ready.set(0)
        self.incidents_active.set(0)

        # Targets are known at construction, so they are exported immediately;
        # observed/budget values wait for real observations and read as NaN
        # (insufficient data) until then.
        self.slo_target.labels(slo=SLO_AVAILABILITY).set(settings.slo_availability_target)
        self.slo_target.labels(slo=SLO_LATENCY).set(settings.slo_latency_target)
        self.error_budget_allowed_fraction.labels(slo=SLO_AVAILABILITY).set(
            1.0 - settings.slo_availability_target
        )
        self.error_budget_allowed_fraction.labels(slo=SLO_LATENCY).set(
            1.0 - settings.slo_latency_target
        )
        for name in SLO_VALUES:
            self.slo_observed.labels(slo=name).set(_NAN)
            self.slo_compliant.labels(slo=name).set(_NAN)
            self.error_budget_observed_failure_fraction.labels(slo=name).set(_NAN)
            self.error_budget_consumed_fraction.labels(slo=name).set(_NAN)
            self.error_budget_remaining_fraction.labels(slo=name).set(_NAN)
            self.error_budget_exhausted.labels(slo=name).set(_NAN)

        self._status_filter_labels = _status_filter_values()

    # --- recording ---------------------------------------------------------

    def record_http_request(
        self, method: str | None, route: str, status_code: Any, duration_seconds: float
    ) -> None:
        """Record one served request."""
        safe_method = _normalise_method(method)
        self.http_requests_total.labels(
            method=safe_method, route=route, status_code=_normalise_status(status_code)
        ).inc()
        self.http_request_duration_seconds.labels(
            method=safe_method, route=route
        ).observe(duration_seconds)

    def record_order_lookup(self, outcome: str, duration_seconds: float) -> None:
        """Record one single-order lookup."""
        self.order_lookups_total.labels(outcome=_normalise_outcome(outcome)).inc()
        self.order_lookup_duration_seconds.observe(duration_seconds)

    def record_order_list(
        self, status_filter: str | None, returned: int, matched: int
    ) -> None:
        """Record one order list query."""
        self.orders_listed_total.labels(
            status_filter=_normalise_filter(status_filter, self._status_filter_labels)
        ).inc()
        self.orders_returned.observe(returned)
        self.orders_matched.observe(matched)

    def set_ready(self, is_ready: bool) -> None:
        """Publish the outcome of the most recent readiness check."""
        self.ready.set(1 if is_ready else 0)

    def update_reliability(self, snapshot: "ReliabilityResponse") -> None:
        """Publish a reliability evaluation onto the SLO and budget gauges.

        Absent ("insufficient data") values are published as ``NaN`` so a
        dashboard or alert sees a gap instead of a misleading 0 or 100%.
        """
        self._publish_sli(
            SLO_AVAILABILITY,
            snapshot.availability.observed,
            snapshot.availability.status,
            snapshot.error_budget,
        )
        self._publish_sli(
            SLO_LATENCY,
            snapshot.latency.observed,
            snapshot.latency.status,
            snapshot.latency_error_budget,
        )

    def _publish_sli(
        self,
        name: str,
        observed: float | None,
        status: str,
        budget: "ErrorBudget",
    ) -> None:
        sufficient = observed is not None and budget.sufficient_data
        self.slo_observed.labels(slo=name).set(_NAN if observed is None else observed)
        if not sufficient:
            # Both statuses below "meeting_target" (at_risk) and "breached"
            # count as not compliant, which is what an alert wants.
            self.slo_compliant.labels(slo=name).set(_NAN)
        else:
            self.slo_compliant.labels(slo=name).set(1.0 if status != "breached" else 0.0)

        self.error_budget_allowed_fraction.labels(slo=name).set(
            budget.allowed_failure_fraction
        )
        self._set_optional(
            self.error_budget_observed_failure_fraction, name, budget.observed_failure_fraction
        )
        self._set_optional(
            self.error_budget_consumed_fraction, name, budget.consumed_fraction
        )
        self._set_optional(
            self.error_budget_remaining_fraction, name, budget.remaining_fraction
        )
        self.error_budget_exhausted.labels(slo=name).set(
            _NAN if not sufficient else (1.0 if budget.exhausted else 0.0)
        )

    @staticmethod
    def _set_optional(gauge: Gauge, name: str, value: float | None) -> None:
        gauge.labels(slo=name).set(_NAN if value is None else value)

    # --- incidents ---------------------------------------------------------

    def record_incident_detection(self, incident_type: str, severity: str) -> None:
        """Count one newly opened incident."""
        self.incident_detections_total.labels(
            type=_normalise_incident_type(incident_type),
            severity=_normalise_incident_severity(severity),
        ).inc()

    def record_incident_resolution(self, incident_type: str, duration_seconds: float) -> None:
        """Count and time one resolved incident."""
        safe_type = _normalise_incident_type(incident_type)
        self.incident_resolutions_total.labels(type=safe_type).inc()
        self.incident_duration_seconds.labels(type=safe_type).observe(
            max(0.0, float(duration_seconds))
        )

    def set_active_incidents(self, count: int) -> None:
        """Publish the current number of unresolved incidents."""
        self.incidents_active.set(max(0, int(count)))

    # --- exposition --------------------------------------------------------

    def render(self) -> tuple[bytes, str]:
        """Render the registry in the Prometheus text exposition format."""
        return generate_latest(self.registry), CONTENT_TYPE_LATEST


def _status_filter_values() -> tuple[str, ...]:
    """Status filter values accepted by the list endpoint."""
    from app.models import OrderStatus

    return tuple(status.value for status in OrderStatus)
