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
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from app.config import Settings

# Prometheus metric names cannot contain dashes, so the service name is
# spelled out rather than derived from settings.app_name.
METRIC_NAMESPACE = "orders_api"

#: Route label used when no route template matches, e.g. a scanner hitting a
#: random URL. Collapsing these into one series is what stops 404 traffic from
#: exploding cardinality.
UNMATCHED_ROUTE = "unmatched"

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

        # Version and environment are constant per deployment, so this is one
        # bounded series rather than a per-request label.
        self.build_info.labels(
            version=settings.app_version, environment=settings.environment
        ).set(1)
        self.ready.set(0)

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

    # --- exposition --------------------------------------------------------

    def render(self) -> tuple[bytes, str]:
        """Render the registry in the Prometheus text exposition format."""
        return generate_latest(self.registry), CONTENT_TYPE_LATEST


def _status_filter_values() -> tuple[str, ...]:
    """Status filter values accepted by the list endpoint."""
    from app.models import OrderStatus

    return tuple(status.value for status in OrderStatus)
