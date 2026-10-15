"""Service-level indicators, objectives and error budgets.

This module is pure calculation. It holds no FastAPI or Prometheus types, so it
can be unit-tested against a fake clock with no running server.

Measurement model
-----------------
The reliability layer observes the *same* requests the HTTP metrics observe, at
the same point in the middleware and with the same route-template labels. It
does **not** read the cumulative Prometheus counters back: a rolling window
cannot be reconstructed from cumulative counters in-process, and scraping a
local TSDB is out of scope. Instead each eligible request is appended to a
bounded, time-stamped ring (a ``deque``) and old samples are evicted as the
window slides. That makes every calculation deterministic, exact, and testable
without a real clock.

Policy
------
* A request is a **failure** when its status code is ``5xx``.
* ``1xx``-``4xx`` responses (including ``4xx``) are **successes** for
  availability. A ``4xx`` is a client error, not a service failure; counting it
  as one would let a broken client burn the service's error budget.
* Administrative routes (health, readiness, metrics, the failure-injection
  control plane, this reliability endpoint) and unmatched/scanner paths are
  excluded from the calculation entirely.
* Availability is ``successes / eligible`` and latency compliance is
  ``within_threshold / eligible`` over the window.
* Latency uses the request's exact duration, so compliance at the configured
  threshold is exact for any threshold. The HTTP histogram's buckets were
  inspected when choosing this: the default 500 ms threshold happens to be a
  bucket boundary, but not every configuration would be, and the tracker's
  exact durations avoid the interpolation error a bucket-only calculation
  would introduce.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from app.models import (
    AvailabilitySli,
    ErrorBudget,
    LatencySli,
    ReliabilityResponse,
)

UTC = timezone.utc

#: Status vocabulary. Kept as plain strings so they survive JSON round-trips.
STATUS_MEETING_TARGET = "meeting_target"
STATUS_AT_RISK = "at_risk"
STATUS_BREACHED = "breached"
STATUS_INSUFFICIENT_DATA = "insufficient_data"

#: Severity ordering used to collapse the two SLIs into one headline status.
_STATUS_SEVERITY = {
    STATUS_INSUFFICIENT_DATA: 0,
    STATUS_MEETING_TARGET: 1,
    STATUS_AT_RISK: 2,
    STATUS_BREACHED: 3,
}

STATUS_DEFINITIONS: dict[str, str] = {
    STATUS_MEETING_TARGET: (
        "Observed indicator meets or exceeds the target and more than the "
        "at-risk share of the error budget remains."
    ),
    STATUS_AT_RISK: (
        "Observed indicator still meets the target, but the remaining error "
        "budget has fallen to or below the configured at-risk fraction "
        "(default: 25%). The SLO is not yet breached, but a small amount of "
        "additional failure would breach it."
    ),
    STATUS_BREACHED: "Observed indicator is below the configured target.",
    STATUS_INSUFFICIENT_DATA: (
        "No eligible requests were observed in the window, so no indicator can "
        "be computed. This is never reported as 100%."
    ),
}

#: Default hard cap on retained samples, so a runaway client cannot grow the
#: buffer without bound inside one long window.
DEFAULT_MAX_EVENTS = 200_000


@dataclass(frozen=True)
class ReliabilityEvent:
    """One observed request inside the rolling window."""

    at: float
    is_failure: bool
    duration_seconds: float


def _ratio(value: float) -> float:
    """Round a computed ratio to a stable number of decimals for reporting."""
    return round(value, 10)


def _status(observed: float, target: float, remaining: float, at_risk_below: float) -> str:
    """Map an observation onto the status vocabulary.

    ``breached`` when the target is missed; otherwise ``at_risk`` when the
    remaining error budget is at or below the at-risk fraction; otherwise
    ``meeting_target``. The at-risk rule is a documented, single threshold on
    the *same* error budget the rest of the module reports — not an invented
    separate score.
    """
    if observed < target:
        return STATUS_BREACHED
    if remaining <= at_risk_below:
        return STATUS_AT_RISK
    return STATUS_MEETING_TARGET


def _error_budget(slo: str, target: float, observed: float) -> ErrorBudget:
    """Build the error budget for an observed indicator."""
    allowed = 1.0 - target
    observed_failure = 1.0 - observed
    consumed = observed_failure / allowed
    remaining = max(0.0, min(1.0, 1.0 - consumed))
    return ErrorBudget(
        slo=slo,  # type: ignore[arg-type]
        target=target,
        observed=_ratio(observed),
        allowed_failure_fraction=_ratio(allowed),
        observed_failure_fraction=_ratio(observed_failure),
        consumed_fraction=_ratio(consumed),
        remaining_fraction=_ratio(remaining),
        exhausted=consumed >= 1.0,
        sufficient_data=True,
    )


def _insufficient_budget(slo: str, target: float) -> ErrorBudget:
    """An error budget for which there is not yet enough data to compute."""
    return ErrorBudget(
        slo=slo,  # type: ignore[arg-type]
        target=target,
        observed=None,
        allowed_failure_fraction=_ratio(1.0 - target),
        observed_failure_fraction=None,
        consumed_fraction=None,
        remaining_fraction=None,
        exhausted=False,
        sufficient_data=False,
    )


def _notes(at_risk_below: float) -> list[str]:
    return [
        "Availability counts HTTP 5xx as failures; 1xx-4xx responses count as "
        "successful requests (a 4xx is a client error, not a service failure).",
        "Health, readiness, metrics, failure-control and reliability endpoints "
        "are excluded from reliability calculations, as are unmatched paths.",
        "With no eligible requests in the window the result is "
        "'insufficient_data', never 100%.",
        "Error-budget 'consumed_fraction' can exceed 1.0 when more failures "
        "occurred than the budget allowed; 'remaining_fraction' is floored at "
        "0.0 and 'exhausted' marks the overrun.",
        f"at_risk means the target is still met but the remaining error budget "
        f"is at or below {at_risk_below:.0%}.",
    ]


class ReliabilityTracker:
    """Rolling-window reliability recorder and evaluator."""

    def __init__(
        self,
        *,
        availability_target: float,
        latency_target: float,
        latency_threshold_ms: float,
        window_seconds: int,
        at_risk_budget_remaining: float = 0.25,
        excluded_routes: Iterable[str] = (),
        clock: Callable[[], float] = time.monotonic,
        max_events: int = DEFAULT_MAX_EVENTS,
    ) -> None:
        self.availability_target = availability_target
        self.latency_target = latency_target
        self.latency_threshold_seconds = latency_threshold_ms / 1000.0
        self.latency_threshold_ms = latency_threshold_ms
        self.window_seconds = window_seconds
        self.at_risk_budget_remaining = at_risk_budget_remaining
        self._excluded = frozenset(excluded_routes)
        self._clock = clock
        self._max_events = max_events
        self._events: deque[ReliabilityEvent] = deque()
        self._lock = threading.Lock()

    # --- recording ---------------------------------------------------------

    def record(
        self,
        *,
        route: str,
        status_code: int,
        duration_seconds: float,
        at: float | None = None,
    ) -> bool:
        """Record one eligible request. Returns False if it was excluded.

        ``at`` is exposed for deterministic tests; production callers omit it
        and the injected clock is used.
        """
        if route in self._excluded:
            return False

        timestamp = self._clock() if at is None else at
        try:
            code = int(status_code)
        except (TypeError, ValueError):
            code = 500

        event = ReliabilityEvent(
            at=timestamp,
            is_failure=code >= 500,
            duration_seconds=float(duration_seconds),
        )
        with self._lock:
            self._events.append(event)
            self._evict(timestamp)
        return True

    def _evict(self, now: float) -> None:
        """Drop samples that have fallen out of the window (lock held)."""
        cutoff = now - self.window_seconds
        events = self._events
        while events and events[0].at < cutoff:
            events.popleft()
        while len(events) > self._max_events:
            events.popleft()

    # --- evaluation --------------------------------------------------------

    def snapshot(self, *, now: float | None = None) -> ReliabilityResponse:
        """Evaluate the current window and return the full reliability view."""
        moment = self._clock() if now is None else now
        with self._lock:
            self._evict(moment)
            events = tuple(self._events)

        total = len(events)
        if total == 0:
            return self._empty_snapshot()

        failures = sum(1 for event in events if event.is_failure)
        successful = total - failures
        observed_availability = successful / total

        within = sum(
            1
            for event in events
            if event.duration_seconds <= self.latency_threshold_seconds
        )
        observed_latency = within / total

        availability_budget = _error_budget(
            "availability", self.availability_target, observed_availability
        )
        latency_budget = _error_budget(
            "latency", self.latency_target, observed_latency
        )

        availability_status = _status(
            observed_availability,
            self.availability_target,
            availability_budget.remaining_fraction or 0.0,
            self.at_risk_budget_remaining,
        )
        latency_status = _status(
            observed_latency,
            self.latency_target,
            latency_budget.remaining_fraction or 0.0,
            self.at_risk_budget_remaining,
        )

        return ReliabilityResponse(
            service="",
            generated_at=datetime.now(UTC),
            evaluation_window_seconds=self.window_seconds,
            status=self._overall(availability_status, latency_status),
            availability=AvailabilitySli(
                target=self.availability_target,
                observed=_ratio(observed_availability),
                status=availability_status,  # type: ignore[arg-type]
                window_seconds=self.window_seconds,
                eligible_requests=total,
                successful_requests=successful,
                failed_requests=failures,
                sufficient_data=True,
            ),
            latency=LatencySli(
                target=self.latency_target,
                observed=_ratio(observed_latency),
                status=latency_status,  # type: ignore[arg-type]
                window_seconds=self.window_seconds,
                eligible_requests=total,
                within_threshold_requests=within,
                latency_threshold_ms=self.latency_threshold_ms,
                sufficient_data=True,
            ),
            error_budget=availability_budget,
            latency_error_budget=latency_budget,
            status_definitions=dict(STATUS_DEFINITIONS),
            notes=_notes(self.at_risk_budget_remaining),
        )

    def _empty_snapshot(self) -> ReliabilityResponse:
        return ReliabilityResponse(
            service="",
            generated_at=datetime.now(UTC),
            evaluation_window_seconds=self.window_seconds,
            status=STATUS_INSUFFICIENT_DATA,
            availability=AvailabilitySli(
                target=self.availability_target,
                observed=None,
                status=STATUS_INSUFFICIENT_DATA,
                window_seconds=self.window_seconds,
                eligible_requests=0,
                successful_requests=0,
                failed_requests=0,
                sufficient_data=False,
            ),
            latency=LatencySli(
                target=self.latency_target,
                observed=None,
                status=STATUS_INSUFFICIENT_DATA,
                window_seconds=self.window_seconds,
                eligible_requests=0,
                within_threshold_requests=0,
                latency_threshold_ms=self.latency_threshold_ms,
                sufficient_data=False,
            ),
            error_budget=_insufficient_budget("availability", self.availability_target),
            latency_error_budget=_insufficient_budget("latency", self.latency_target),
            status_definitions=dict(STATUS_DEFINITIONS),
            notes=_notes(self.at_risk_budget_remaining),
        )

    @staticmethod
    def _overall(first: str, second: str) -> str:
        return max(
            (first, second),
            key=lambda status: _STATUS_SEVERITY[status],
        )
