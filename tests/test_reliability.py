"""SLI, SLO and error-budget behaviour.

The calculation layer is tested directly against a fake clock so the rolling
window is deterministic and no test depends on real elapsed time. The HTTP and
Prometheus surface is then tested through ``TestClient`` and a ``/metrics``
scrape, which is exactly what an operator and Prometheus would see.
"""

from __future__ import annotations

import math
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app
from app.reliability import (
    STATUS_AT_RISK,
    STATUS_BREACHED,
    STATUS_INSUFFICIENT_DATA,
    STATUS_MEETING_TARGET,
    ReliabilityTracker,
)
from tests.conftest import build_settings
from tests.test_metrics import scrape, value, value_or_zero

ORDERS_PATH = "/api/orders"
RELIABILITY_PATH = "/api/reliability"
CONTROL_PATH = "/admin/failure"


# --- deterministic helpers ---------------------------------------------------


class FakeClock:
    """A monotonic-style clock advanced explicitly by the test."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_tracker(**overrides: object) -> tuple[ReliabilityTracker, FakeClock]:
    clock = FakeClock()
    params: dict[str, object] = {
        "availability_target": 0.995,
        "latency_target": 0.95,
        "latency_threshold_ms": 500.0,
        "window_seconds": 300,
        "at_risk_budget_remaining": 0.25,
        "clock": clock,
    }
    params.update(overrides)
    return ReliabilityTracker(**params), clock  # type: ignore[arg-type]


def feed(
    tracker: ReliabilityTracker,
    clock: FakeClock,
    *,
    successes: int = 0,
    failures: int = 0,
    duration_ms: float = 10.0,
    route: str = ORDERS_PATH,
) -> None:
    """Record a run of successful/failed requests at the clock's current time."""
    for _ in range(successes):
        tracker.record(
            route=route, status_code=200, duration_seconds=duration_ms / 1000.0
        )
    for _ in range(failures):
        tracker.record(
            route=route, status_code=500, duration_seconds=duration_ms / 1000.0
        )


# --- availability ------------------------------------------------------------


def test_empty_window_is_insufficient_not_full_availability() -> None:
    tracker, _ = make_tracker()

    snapshot = tracker.snapshot()

    assert snapshot.status == STATUS_INSUFFICIENT_DATA
    assert snapshot.availability.observed is None
    assert snapshot.availability.sufficient_data is False
    assert snapshot.latency.observed is None
    assert snapshot.error_budget.sufficient_data is False
    assert snapshot.error_budget.consumed_fraction is None


def test_all_successful_requests_meet_target() -> None:
    tracker, clock = make_tracker()
    feed(tracker, clock, successes=100)

    snapshot = tracker.snapshot()

    assert snapshot.availability.observed == 1.0
    assert snapshot.availability.status == STATUS_MEETING_TARGET
    assert snapshot.availability.failed_requests == 0
    assert snapshot.error_budget.consumed_fraction == 0.0
    assert snapshot.error_budget.remaining_fraction == 1.0
    assert snapshot.error_budget.exhausted is False


def test_5xx_counts_as_failure() -> None:
    tracker, clock = make_tracker()
    feed(tracker, clock, successes=98, failures=2)

    snapshot = tracker.snapshot()

    assert snapshot.availability.observed == pytest.approx(0.98)
    assert snapshot.availability.failed_requests == 2
    assert snapshot.availability.successful_requests == 98
    assert snapshot.availability.status == STATUS_BREACHED


def test_4xx_is_not_a_service_failure() -> None:
    """A client error must not consume the service's error budget."""
    tracker, clock = make_tracker()
    for _ in range(10):
        tracker.record(route=ORDERS_PATH, status_code=404, duration_seconds=0.01)
    for _ in range(10):
        tracker.record(route=ORDERS_PATH, status_code=422, duration_seconds=0.01)

    snapshot = tracker.snapshot()

    assert snapshot.availability.observed == 1.0
    assert snapshot.availability.failed_requests == 0
    assert snapshot.availability.status == STATUS_MEETING_TARGET


def test_excluded_routes_are_not_recorded() -> None:
    tracker, clock = make_tracker(excluded_routes={RELIABILITY_PATH})
    assert tracker.record(route=RELIABILITY_PATH, status_code=500, duration_seconds=1.0) is False
    assert tracker.record(route=ORDERS_PATH, status_code=200, duration_seconds=0.01) is True

    snapshot = tracker.snapshot()

    assert snapshot.availability.eligible_requests == 1
    assert snapshot.availability.observed == 1.0


# --- latency -----------------------------------------------------------------


def test_latency_compliance_counts_requests_within_threshold() -> None:
    tracker, clock = make_tracker()
    for _ in range(9):
        tracker.record(route=ORDERS_PATH, status_code=200, duration_seconds=0.1)
    tracker.record(route=ORDERS_PATH, status_code=200, duration_seconds=0.9)

    snapshot = tracker.snapshot()

    assert snapshot.latency.observed == pytest.approx(0.9)
    assert snapshot.latency.within_threshold_requests == 9
    assert snapshot.latency.latency_threshold_ms == 500.0


def test_latency_threshold_boundary_is_inclusive() -> None:
    tracker, clock = make_tracker(latency_threshold_ms=500.0)
    tracker.record(route=ORDERS_PATH, status_code=200, duration_seconds=0.5)
    tracker.record(route=ORDERS_PATH, status_code=200, duration_seconds=0.500001)

    snapshot = tracker.snapshot()

    assert snapshot.latency.within_threshold_requests == 1
    assert snapshot.latency.observed == pytest.approx(0.5)


def test_latency_breach_when_requests_exceed_threshold() -> None:
    tracker, clock = make_tracker()
    for _ in range(10):
        tracker.record(route=ORDERS_PATH, status_code=200, duration_seconds=0.6)

    snapshot = tracker.snapshot()

    assert snapshot.latency.observed == 0.0
    assert snapshot.latency.status == STATUS_BREACHED
    assert snapshot.latency_error_budget.exhausted is True


# --- statuses ----------------------------------------------------------------


def test_at_risk_when_meeting_target_but_budget_mostly_consumed() -> None:
    # target 0.995, allowed failure fraction 0.005.
    # 3 / 800 failures = 0.00375 -> consumed 0.75, remaining 0.25.
    tracker, clock = make_tracker()
    feed(tracker, clock, successes=797, failures=3)

    snapshot = tracker.snapshot()

    assert snapshot.availability.observed == pytest.approx(0.99625)
    assert snapshot.availability.status == STATUS_AT_RISK
    assert snapshot.error_budget.remaining_fraction == pytest.approx(0.25)
    assert snapshot.error_budget.exhausted is False


def test_meeting_target_when_budget_healthy() -> None:
    tracker, clock = make_tracker()
    feed(tracker, clock, successes=798, failures=2)

    snapshot = tracker.snapshot()

    assert snapshot.availability.status == STATUS_MEETING_TARGET
    assert snapshot.error_budget.remaining_fraction == pytest.approx(0.5)


def test_breached_when_target_missed() -> None:
    tracker, clock = make_tracker()
    feed(tracker, clock, successes=795, failures=5)

    snapshot = tracker.snapshot()

    assert snapshot.availability.observed == pytest.approx(0.99375)
    assert snapshot.availability.status == STATUS_BREACHED


def test_overall_status_is_the_most_severe() -> None:
    tracker, clock = make_tracker()
    # Availability breached, latency fine.
    feed(tracker, clock, successes=90, failures=10, duration_ms=10.0)

    snapshot = tracker.snapshot()

    assert snapshot.availability.status == STATUS_BREACHED
    assert snapshot.latency.status == STATUS_MEETING_TARGET
    assert snapshot.status == STATUS_BREACHED


# --- error budget ------------------------------------------------------------


def test_error_budget_consumed_and_remaining() -> None:
    tracker, clock = make_tracker()
    feed(tracker, clock, successes=996, failures=4)

    budget = tracker.snapshot().error_budget

    # allowed 0.005, observed failure 0.004 -> consumed 0.8, remaining 0.2.
    assert budget.allowed_failure_fraction == pytest.approx(0.005)
    assert budget.observed_failure_fraction == pytest.approx(0.004)
    assert budget.consumed_fraction == pytest.approx(0.8)
    assert budget.remaining_fraction == pytest.approx(0.2)
    assert budget.exhausted is False


def test_error_budget_exhausted_at_exactly_target() -> None:
    tracker, clock = make_tracker()
    # 4 / 800 = 0.005 = allowed -> consumed exactly 1.0.
    feed(tracker, clock, successes=796, failures=4)

    budget = tracker.snapshot().error_budget

    assert budget.consumed_fraction == pytest.approx(1.0)
    assert budget.remaining_fraction == 0.0
    assert budget.exhausted is True


def test_error_budget_overrun_is_represented_not_negative() -> None:
    tracker, clock = make_tracker()
    feed(tracker, clock, successes=98, failures=2)

    budget = tracker.snapshot().error_budget

    assert budget.consumed_fraction == pytest.approx(4.0)
    # Remaining is floored at zero rather than reporting -300%.
    assert budget.remaining_fraction == 0.0
    assert budget.exhausted is True


# --- rolling window and lifecycle --------------------------------------------


def test_samples_leave_the_window() -> None:
    tracker, clock = make_tracker(window_seconds=300)
    feed(tracker, clock, successes=5)

    clock.advance(301)

    snapshot = tracker.snapshot()
    assert snapshot.availability.eligible_requests == 0
    assert snapshot.status == STATUS_INSUFFICIENT_DATA


def test_sample_at_the_window_edge_is_retained() -> None:
    tracker, clock = make_tracker(window_seconds=300)
    tracker.record(route=ORDERS_PATH, status_code=200, duration_seconds=0.01)

    clock.advance(300)
    assert tracker.snapshot().availability.eligible_requests == 1

    clock.advance(1)
    assert tracker.snapshot().availability.eligible_requests == 0


def test_window_only_counts_recent_traffic() -> None:
    tracker, clock = make_tracker(window_seconds=300)
    feed(tracker, clock, successes=10)  # old, all successful

    clock.advance(301)
    feed(tracker, clock, successes=5, failures=5)  # current, half failed

    snapshot = tracker.snapshot()

    assert snapshot.availability.eligible_requests == 10
    assert snapshot.availability.observed == pytest.approx(0.5)


def test_restart_starts_with_no_data() -> None:
    """A fresh tracker (container restart) must not inherit stale success."""
    first, clock = make_tracker()
    feed(first, clock, successes=1000)

    restarted, _ = make_tracker()
    snapshot = restarted.snapshot()

    assert snapshot.status == STATUS_INSUFFICIENT_DATA
    assert snapshot.availability.observed is None


def test_status_definitions_are_exposed() -> None:
    tracker, _ = make_tracker()
    definitions = tracker.snapshot().status_definitions

    assert set(definitions) == {
        STATUS_MEETING_TARGET,
        STATUS_AT_RISK,
        STATUS_BREACHED,
        STATUS_INSUFFICIENT_DATA,
    }
    assert all(definition for definition in definitions.values())


# --- configuration -----------------------------------------------------------


def test_slo_defaults() -> None:
    settings = build_settings()

    assert settings.slo_availability_target == 0.995
    assert settings.slo_latency_target == 0.95
    assert settings.slo_latency_threshold_ms == 500.0
    assert settings.slo_window_seconds == 300
    assert settings.reliability_path == "/api/reliability"


def test_slo_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDERS_API_SLO_AVAILABILITY_TARGET", "0.99")
    monkeypatch.setenv("ORDERS_API_SLO_LATENCY_THRESHOLD_MS", "250")
    monkeypatch.setenv("ORDERS_API_SLO_WINDOW_SECONDS", "60")

    settings = Settings(_env_file=None)

    assert settings.slo_availability_target == 0.99
    assert settings.slo_latency_threshold_ms == 250.0
    assert settings.slo_window_seconds == 60


@pytest.mark.parametrize(
    "overrides",
    [
        {"slo_availability_target": 0.0},
        {"slo_availability_target": 1.0},
        {"slo_availability_target": -0.1},
        {"slo_latency_target": 0.0},
        {"slo_latency_target": 1.5},
        {"slo_latency_threshold_ms": 0},
        {"slo_latency_threshold_ms": 120_000},
        {"slo_window_seconds": 0},
        {"slo_window_seconds": 200_000},
        {"slo_at_risk_budget_remaining": -0.1},
        {"slo_at_risk_budget_remaining": 1.1},
        {"reliability_path": "api/reliability"},
        {"reliability_path": "/"},
    ],
)
def test_invalid_slo_settings_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        build_settings(**overrides)


# --- API ---------------------------------------------------------------------


def reliability_client(**overrides: object) -> TestClient:
    settings = build_settings(environment="test", log_level="WARNING", **overrides)
    return TestClient(create_app(settings))


def test_reliability_endpoint_reports_insufficient_data_with_no_traffic() -> None:
    with reliability_client() as client:
        response = client.get(RELIABILITY_PATH)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == STATUS_INSUFFICIENT_DATA
    assert body["service"] == "orders-api"
    assert body["availability"]["observed"] is None
    assert body["error_budget"]["sufficient_data"] is False
    assert "meeting_target" in body["status_definitions"]


def test_reliability_endpoint_reflects_normal_traffic() -> None:
    with reliability_client() as client:
        for _ in range(5):
            client.get(ORDERS_PATH)
        body = client.get(RELIABILITY_PATH).json()

    assert body["availability"]["observed"] == 1.0
    assert body["availability"]["status"] == STATUS_MEETING_TARGET
    assert body["availability"]["eligible_requests"] == 5


def test_reliability_endpoint_is_documented() -> None:
    with reliability_client() as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert RELIABILITY_PATH in paths
    assert "get" in paths[RELIABILITY_PATH]


def test_reliability_path_is_configurable() -> None:
    with reliability_client(reliability_path="/internal/reliability") as client:
        assert client.get("/internal/reliability").status_code == 200
        assert client.get(RELIABILITY_PATH).status_code == 404


def test_reliability_endpoint_is_not_counted_as_application_traffic() -> None:
    with reliability_client() as client:
        client.get(RELIABILITY_PATH)
        routes = {
            dict(labels).get("route")
            for name, labels in scrape(client)
            if name == "orders_api_http_requests_total"
        }

    assert RELIABILITY_PATH not in routes


def test_reliability_endpoint_schema_is_stable() -> None:
    with reliability_client() as client:
        body = client.get(RELIABILITY_PATH).json()

    assert set(body) == {
        "service",
        "generated_at",
        "evaluation_window_seconds",
        "status",
        "availability",
        "latency",
        "error_budget",
        "latency_error_budget",
        "status_definitions",
        "notes",
    }
    assert set(body["error_budget"]) == {
        "slo",
        "target",
        "observed",
        "allowed_failure_fraction",
        "observed_failure_fraction",
        "consumed_fraction",
        "remaining_fraction",
        "exhausted",
        "sufficient_data",
    }


# --- Prometheus gauges -------------------------------------------------------


def test_slo_targets_are_exported_before_any_traffic() -> None:
    with reliability_client() as client:
        samples = scrape(client)

    assert value(samples, "orders_api_slo_target", slo="availability") == 0.995
    assert value(samples, "orders_api_slo_target", slo="latency") == 0.95


def test_slo_observed_is_nan_before_any_traffic() -> None:
    with reliability_client() as client:
        samples = scrape(client)

    observed = value(samples, "orders_api_slo_observed", slo="availability")
    compliant = value(samples, "orders_api_slo_compliant", slo="availability")

    assert math.isnan(observed)
    assert math.isnan(compliant)


def test_slo_gauges_reflect_traffic_on_scrape() -> None:
    with reliability_client() as client:
        for _ in range(4):
            client.get(ORDERS_PATH)
        samples = scrape(client)

    assert value(samples, "orders_api_slo_observed", slo="availability") == 1.0
    assert value(samples, "orders_api_slo_compliant", slo="availability") == 1.0
    assert value(samples, "orders_api_error_budget_remaining_fraction", slo="availability") == 1.0


def test_error_budget_gauges_track_failures() -> None:
    with reliability_client(failure_injection_enabled=True) as client:
        for _ in range(9):
            client.get(ORDERS_PATH)
        client.put(CONTROL_PATH, json={"enabled": True, "failure_rate_percent": 100})
        client.get(ORDERS_PATH)
        client.delete(CONTROL_PATH)
        samples = scrape(client)

    assert value(samples, "orders_api_slo_compliant", slo="availability") == 0.0
    assert value(samples, "orders_api_error_budget_exhausted", slo="availability") == 1.0
    assert value_or_zero(
        samples, "orders_api_error_budget_remaining_fraction", slo="availability"
    ) == 0.0


# --- interaction with failure injection --------------------------------------


def test_injected_failures_breach_availability() -> None:
    with reliability_client(failure_injection_enabled=True) as client:
        client.put(CONTROL_PATH, json={"enabled": True, "failure_rate_percent": 100})
        for _ in range(5):
            client.get(ORDERS_PATH)
        body = client.get(RELIABILITY_PATH).json()
        client.delete(CONTROL_PATH)

    assert body["availability"]["failed_requests"] == 5
    assert body["availability"]["observed"] == 0.0
    assert body["availability"]["status"] == STATUS_BREACHED
    assert body["error_budget"]["exhausted"] is True


def test_injected_latency_breaches_latency_slo() -> None:
    with reliability_client(failure_injection_enabled=True) as client:
        client.put(CONTROL_PATH, json={"enabled": True, "latency_ms": 650, "failure_rate_percent": 0})
        client.get(ORDERS_PATH)
        client.get(ORDERS_PATH)
        body = client.get(RELIABILITY_PATH).json()
        client.delete(CONTROL_PATH)

    assert body["latency"]["within_threshold_requests"] == 0
    assert body["latency"]["observed"] == 0.0
    assert body["latency"]["status"] == STATUS_BREACHED


def test_disable_injection_restores_normal_reliability() -> None:
    with reliability_client(failure_injection_enabled=True) as client:
        client.put(CONTROL_PATH, json={"enabled": True, "failure_rate_percent": 100})
        client.get(ORDERS_PATH)
        client.delete(CONTROL_PATH)
        for _ in range(4):
            client.get(ORDERS_PATH)
        body = client.get(RELIABILITY_PATH).json()

    # 1 failure out of 5 requests in the window; no longer a full breach.
    assert body["availability"]["failed_requests"] == 1
    assert body["availability"]["status"] == STATUS_BREACHED  # 0.8 < 0.995


# --- regression --------------------------------------------------------------


def test_reliability_metrics_do_not_leak_identifiers() -> None:
    with reliability_client() as client:
        client.get(f"{ORDERS_PATH}?customer_id=CUS-1042")
        exposed = {val for _, labels in scrape(client) for _, val in labels}

    assert "CUS-1042" not in exposed
