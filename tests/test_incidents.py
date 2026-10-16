"""Incident detection and lifecycle behaviour.

The detector is exercised directly against a reliability tracker and a wall
clock the test controls, so every incident transition is deterministic and no
test depends on real elapsed time. The HTTP and Prometheus surface is then
tested through ``TestClient`` exactly as an operator would see it.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.incidents import (
    RULE_AVAILABILITY_BREACH,
    RULE_AVAILABILITY_BUDGET_EXHAUSTED,
    RULE_LATENCY_BREACH,
    IncidentDetector,
    IncidentStore,
)
from app.main import create_app
from app.models import (
    IncidentEvidence,
    IncidentSeverity,
    IncidentStatus,
    IncidentTrigger,
    IncidentType,
)
from app.reliability import STATUS_BREACHED, ReliabilityTracker
from tests.conftest import build_settings
from tests.test_metrics import scrape, value, value_or_zero

ORDERS_PATH = "/api/orders"
INCIDENTS_PATH = "/api/incidents"
CONTROL_PATH = "/admin/failure"

UTC = timezone.utc


# --- deterministic helpers ---------------------------------------------------


class FakeClock:
    """A monotonic-style clock advanced explicitly by the test."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeWallClock:
    """A wall clock advanced explicitly by the test."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


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
    *,
    successes: int = 0,
    failures: int = 0,
    duration_ms: float = 10.0,
) -> None:
    """Record a run of successful/failed requests at the current clock time."""
    for _ in range(successes):
        tracker.record(route=ORDERS_PATH, status_code=200, duration_seconds=duration_ms / 1000.0)
    for _ in range(failures):
        tracker.record(route=ORDERS_PATH, status_code=500, duration_seconds=duration_ms / 1000.0)


def make_detector(
    tracker: ReliabilityTracker, **overrides: object
) -> tuple[IncidentStore, IncidentDetector, FakeWallClock]:
    wall = FakeWallClock()
    history_limit = int(overrides.pop("history_limit", 500))
    store = IncidentStore(history_limit=history_limit)
    detector = IncidentDetector(
        tracker=tracker, store=store, wall_clock=wall, **overrides  # type: ignore[arg-type]
    )
    return store, detector, wall


def make_evidence(at: datetime) -> IncidentEvidence:
    return IncidentEvidence(
        observed_at=at,
        window_seconds=300,
        eligible_requests=100,
        failed_requests=5,
        observed_availability=0.95,
        availability_target=0.995,
        observed_latency=1.0,
        latency_target=0.95,
        latency_threshold_ms=500.0,
        error_budget_consumed=10.0,
        error_budget_exhausted=True,
        summary="seeded evidence",
    )


def seed_incident(
    store: IncidentStore,
    *,
    at: datetime | None = None,
    incident_type: IncidentType = IncidentType.AVAILABILITY,
    severity: IncidentSeverity = IncidentSeverity.CRITICAL,
):
    moment = at or datetime(2026, 1, 1, tzinfo=UTC)
    return store.create(
        incident_type=incident_type,
        severity=severity,
        detected_at=moment,
        triggers=[
            IncidentTrigger(
                rule=RULE_AVAILABILITY_BREACH,
                severity=severity,
                detected_at=moment,
            )
        ],
        evidence=make_evidence(moment),
    )


# --- detection rules ---------------------------------------------------------


def test_no_incident_when_reliability_data_is_insufficient() -> None:
    tracker, _ = make_tracker()
    store, detector, _ = make_detector(tracker)

    result = detector.evaluate_once()

    assert result.changed is False
    assert store.active_count() == 0
    assert store.counts().total == 0


def test_availability_breach_creates_critical_incident() -> None:
    tracker, _ = make_tracker()
    store, detector, wall = make_detector(tracker)
    feed(tracker, successes=95, failures=5)

    result = detector.evaluate_once()

    assert len(result.created) == 1
    incident = result.created[0]
    assert incident.type is IncidentType.AVAILABILITY
    assert incident.severity is IncidentSeverity.CRITICAL
    assert incident.status is IncidentStatus.OPEN
    assert incident.detected_at == wall.now
    assert incident.updated_at == wall.now
    assert incident.resolved_at is None
    assert {trigger.rule for trigger in incident.triggers} == {
        RULE_AVAILABILITY_BREACH,
        RULE_AVAILABILITY_BUDGET_EXHAUSTED,
    }
    assert incident.evidence.observed_availability == pytest.approx(0.95)
    assert incident.evidence.availability_target == 0.995
    assert incident.evidence.error_budget_exhausted is True
    assert store.active_count() == 1


def test_latency_breach_creates_warning_incident() -> None:
    tracker, _ = make_tracker()
    store, detector, _ = make_detector(tracker)
    feed(tracker, successes=100, duration_ms=600.0)

    result = detector.evaluate_once()

    assert len(result.created) == 1
    incident = result.created[0]
    assert incident.type is IncidentType.LATENCY
    assert incident.severity is IncidentSeverity.WARNING
    assert [trigger.rule for trigger in incident.triggers] == [RULE_LATENCY_BREACH]
    assert incident.evidence.observed_latency == 0.0
    assert incident.evidence.latency_target == 0.95


def test_multiple_rules_create_distinct_incidents() -> None:
    tracker, _ = make_tracker()
    store, detector, _ = make_detector(tracker)
    # Slow and failing: both the availability and latency SLOs are breached.
    feed(tracker, failures=100, duration_ms=600.0)

    detector.evaluate_once()

    types = {incident.type for incident in store.active()}
    assert types == {IncidentType.AVAILABILITY, IncidentType.LATENCY}
    assert store.active_count() == 2


def test_availability_severity_threshold_is_configurable() -> None:
    # target 0.995 -> allowed failure 0.005. 1/100 failures => consumed 2.0.
    mild_tracker, _ = make_tracker()
    mild_store, mild_detector, _ = make_detector(
        mild_tracker, critical_budget_consumed=5.0
    )
    feed(mild_tracker, successes=99, failures=1)
    mild_detector.evaluate_once()
    assert mild_store.active_incident(IncidentType.AVAILABILITY).severity is IncidentSeverity.WARNING

    # 5/100 failures => consumed 10.0, at or above the threshold -> critical.
    deep_tracker, _ = make_tracker()
    deep_store, deep_detector, _ = make_detector(
        deep_tracker, critical_budget_consumed=5.0
    )
    feed(deep_tracker, successes=95, failures=5)
    deep_detector.evaluate_once()
    assert deep_store.active_incident(IncidentType.AVAILABILITY).severity is IncidentSeverity.CRITICAL


# --- lifecycle ---------------------------------------------------------------


def test_repeated_evaluation_does_not_duplicate_active_incident() -> None:
    tracker, _ = make_tracker()
    store, detector, wall = make_detector(tracker)
    feed(tracker, successes=95, failures=5)

    first = detector.evaluate_once()
    wall.advance(15)
    second = detector.evaluate_once()

    assert len(first.created) == 1
    assert second.created == ()
    assert len(second.updated) == 1
    assert store.active_count() == 1
    incident = store.active()[0]
    assert incident.detection_count == 2
    assert incident.detected_at == first.created[0].detected_at
    assert incident.updated_at == wall.now


def test_added_rule_updates_existing_active_incident() -> None:
    tracker, _ = make_tracker()
    store, detector, wall = make_detector(tracker)
    # Exactly at target: the budget is exhausted but availability is at_risk,
    # so only the budget rule fires.
    feed(tracker, successes=199, failures=1)
    assert tracker.snapshot().availability.status != STATUS_BREACHED
    first = detector.evaluate_once()
    assert [trigger.rule for trigger in first.created[0].triggers] == [
        RULE_AVAILABILITY_BUDGET_EXHAUSTED
    ]

    # One more failure pushes availability below target: the breach rule now
    # fires too, and it must update the existing incident, not create a new one.
    wall.advance(15)
    feed(tracker, failures=1)
    second = detector.evaluate_once()

    assert second.created == ()
    assert len(second.updated) == 1
    incident = store.active()[0]
    assert {trigger.rule for trigger in incident.triggers} == {
        RULE_AVAILABILITY_BREACH,
        RULE_AVAILABILITY_BUDGET_EXHAUSTED,
    }
    # The trigger that fired first keeps its original detection time.
    budget_trigger = next(
        trigger
        for trigger in incident.triggers
        if trigger.rule == RULE_AVAILABILITY_BUDGET_EXHAUSTED
    )
    assert budget_trigger.detected_at == first.created[0].detected_at


def test_recovery_resolves_incident_after_configured_evaluations() -> None:
    tracker, clock = make_tracker()
    store, detector, wall = make_detector(tracker, recovery_evaluations=2)
    feed(tracker, successes=95, failures=5)
    detector.evaluate_once()
    detected_at = store.active()[0].detected_at

    # Clear the window and feed healthy traffic.
    clock.advance(301)
    feed(tracker, successes=50)
    wall.advance(30)

    first = detector.evaluate_once()
    assert first.resolved == ()
    assert store.active_count() == 1

    wall.advance(15)
    second = detector.evaluate_once()
    assert len(second.resolved) == 1
    assert store.active_count() == 0

    resolved = second.resolved[0]
    assert resolved.status is IncidentStatus.RESOLVED
    assert resolved.resolved_at == wall.now
    assert resolved.duration_seconds == pytest.approx(
        (wall.now - detected_at).total_seconds()
    )


def test_recovery_debounce_resets_while_still_breached() -> None:
    tracker, clock = make_tracker()
    store, detector, wall = make_detector(tracker, recovery_evaluations=2)
    feed(tracker, successes=95, failures=5)
    detector.evaluate_once()

    clock.advance(301)
    feed(tracker, successes=50)
    detector.evaluate_once()  # streak = 1

    # A new failure resets the healthy streak, so the incident must persist.
    feed(tracker, failures=5)
    wall.advance(15)
    detector.evaluate_once()

    assert store.active_count() == 1
    assert store.active()[0].status is not IncidentStatus.RESOLVED


def test_insufficient_data_does_not_resolve_active_incident() -> None:
    tracker, clock = make_tracker()
    store, detector, wall = make_detector(tracker, recovery_evaluations=1)
    feed(tracker, successes=95, failures=5)
    detector.evaluate_once()

    # No traffic at all in the window: the outcome is insufficient_data.
    clock.advance(301)
    wall.advance(30)
    for _ in range(5):
        result = detector.evaluate_once()

    assert result.resolved == ()
    assert store.active_count() == 1


def test_resolved_history_is_bounded() -> None:
    tracker, clock = make_tracker()
    store, detector, wall = make_detector(tracker, recovery_evaluations=1, history_limit=2)
    for index in range(4):
        feed(tracker, failures=5)
        detector.evaluate_once()
        clock.advance(301)
        feed(tracker, successes=50)
        detector.evaluate_once()
        # Next incident starts from an empty window.
        clock.advance(301)
        assert store.active_count() == 0 or index < 3

    all_incidents, total = store.list()
    assert total <= 3  # 2 resolved retained + at most 1 active
    resolved = [i for i in all_incidents if i.status is IncidentStatus.RESOLVED]
    assert len(resolved) <= 2


# --- API ---------------------------------------------------------------------


def incidents_client(**overrides: object) -> TestClient:
    overrides.setdefault("incident_detection_enabled", False)
    settings = build_settings(environment="test", log_level="WARNING", **overrides)
    return TestClient(create_app(settings))


def test_incidents_endpoint_is_empty_and_well_formed() -> None:
    with incidents_client() as client:
        response = client.get(INCIDENTS_PATH)

    assert response.status_code == 200
    body = response.json()
    assert body["incidents"] == []
    assert body["pagination"] == {
        "total": 0,
        "limit": 50,
        "offset": 0,
        "returned": 0,
        "has_more": False,
    }
    assert body["counts"]["total"] == 0
    assert body["counts"]["active"] == 0


def test_incidents_endpoints_are_documented() -> None:
    with incidents_client() as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert INCIDENTS_PATH in paths
    assert f"{INCIDENTS_PATH}/{{incident_id}}" in paths
    assert f"{INCIDENTS_PATH}/{{incident_id}}/acknowledge" in paths


def test_incident_detail_and_invalid_id() -> None:
    with incidents_client() as client:
        incident = seed_incident(client.app.state.incidents)

        found = client.get(f"{INCIDENTS_PATH}/{incident.incident_id}")
        missing = client.get(f"{INCIDENTS_PATH}/INC-999999")

    assert found.status_code == 200
    assert found.json()["incident_id"] == incident.incident_id
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "incident_not_found"


def test_incident_list_filters_and_pagination() -> None:
    with incidents_client() as client:
        store = client.app.state.incidents
        seed_incident(store, incident_type=IncidentType.AVAILABILITY)
        seed_incident(
            store,
            incident_type=IncidentType.LATENCY,
            severity=IncidentSeverity.WARNING,
        )

        all_body = client.get(INCIDENTS_PATH).json()
        availability = client.get(f"{INCIDENTS_PATH}?type=availability").json()
        warnings = client.get(f"{INCIDENTS_PATH}?severity=warning").json()
        paged = client.get(f"{INCIDENTS_PATH}?limit=1&offset=0").json()

    assert all_body["pagination"]["total"] == 2
    assert availability["pagination"]["total"] == 1
    assert availability["incidents"][0]["type"] == "availability"
    assert warnings["pagination"]["total"] == 1
    assert warnings["incidents"][0]["severity"] == "warning"
    assert paged["pagination"]["returned"] == 1
    assert paged["pagination"]["has_more"] is True


def test_acknowledge_incident_lifecycle() -> None:
    with incidents_client() as client:
        incident = seed_incident(client.app.state.incidents)

        first = client.post(f"{INCIDENTS_PATH}/{incident.incident_id}/acknowledge")
        second = client.post(f"{INCIDENTS_PATH}/{incident.incident_id}/acknowledge")

    assert first.status_code == 200
    assert first.json()["status"] == "acknowledged"
    assert first.json()["acknowledged_at"] is not None
    # Idempotent: a second acknowledgement does not change the record.
    assert second.status_code == 200
    assert second.json()["acknowledged_at"] == first.json()["acknowledged_at"]


def test_acknowledge_unknown_incident_is_404() -> None:
    with incidents_client() as client:
        response = client.post(f"{INCIDENTS_PATH}/INC-999999/acknowledge")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "incident_not_found"


def test_acknowledge_resolved_incident_conflicts() -> None:
    with incidents_client() as client:
        store = client.app.state.incidents
        incident = seed_incident(store)
        store.resolve(incident.incident_id, resolved_at=datetime.now(UTC))

        response = client.post(f"{INCIDENTS_PATH}/{incident.incident_id}/acknowledge")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "incident_resolved"


def test_incidents_api_exposes_no_create_or_delete() -> None:
    with incidents_client() as client:
        assert client.post(INCIDENTS_PATH, json={}).status_code in (404, 405)
        assert client.delete(INCIDENTS_PATH).status_code in (404, 405)
        incident = seed_incident(client.app.state.incidents)
        assert client.delete(f"{INCIDENTS_PATH}/{incident.incident_id}").status_code in (404, 405)


def test_detected_incident_is_visible_through_the_api() -> None:
    with incidents_client(failure_injection_enabled=True) as client:
        client.put(CONTROL_PATH, json={"enabled": True, "failure_rate_percent": 100})
        for _ in range(5):
            client.get(ORDERS_PATH)
        client.delete(CONTROL_PATH)

        client.app.state.incident_detector.evaluate_once()
        body = client.get(INCIDENTS_PATH).json()

    assert body["counts"]["active"] == 1
    assert body["incidents"][0]["type"] == "availability"
    assert body["incidents"][0]["severity"] == "critical"
    assert body["incidents"][0]["evidence"]["error_budget_exhausted"] is True


def test_incident_endpoints_are_excluded_from_request_metrics() -> None:
    with incidents_client() as client:
        seed_incident(client.app.state.incidents)
        client.get(INCIDENTS_PATH)
        routes = {
            dict(labels).get("route")
            for name, labels in scrape(client)
            if name == "orders_api_http_requests_total"
        }

    assert INCIDENTS_PATH not in routes


# --- Prometheus metrics ------------------------------------------------------


def test_incident_metrics_are_exported() -> None:
    with incidents_client(failure_injection_enabled=True) as client:
        client.put(CONTROL_PATH, json={"enabled": True, "failure_rate_percent": 100})
        for _ in range(5):
            client.get(ORDERS_PATH)
        client.delete(CONTROL_PATH)
        client.app.state.incident_detector.evaluate_once()
        samples = scrape(client)

    assert value(samples, "orders_api_incidents_active") == 1.0
    assert (
        value(
            samples,
            "orders_api_incident_detections_total",
            type="availability",
            severity="critical",
        )
        == 1.0
    )
    assert value_or_zero(samples, "orders_api_incident_resolutions_total", type="availability") == 0.0


def test_resolution_metrics_are_recorded() -> None:
    tracker, clock = make_tracker()
    from app.metrics import Metrics

    settings = build_settings(environment="test")
    metrics = Metrics(settings)
    store, detector, wall = make_detector(tracker, metrics=metrics, recovery_evaluations=1)
    feed(tracker, successes=95, failures=5)
    detector.evaluate_once()

    clock.advance(301)
    feed(tracker, successes=50)
    wall.advance(20)
    detector.evaluate_once()

    payload, _ = metrics.render()
    text = payload.decode()
    assert 'orders_api_incident_resolutions_total{type="availability"} 1.0' in text
    assert "orders_api_incident_duration_seconds_count" in text


def test_incident_metrics_do_not_expose_incident_ids() -> None:
    with incidents_client() as client:
        incident = seed_incident(client.app.state.incidents)
        client.get(INCIDENTS_PATH)
        exposed = {val for _, labels in scrape(client) for _, val in labels}

    assert incident.incident_id not in exposed


# --- detector scheduling -----------------------------------------------------


def test_detector_runs_automatically_and_stops_cleanly() -> None:
    with incidents_client(
        incident_detection_enabled=True,
        incident_evaluation_interval_seconds=0.02,
        incident_recovery_evaluations=1,
        failure_injection_enabled=True,
    ) as client:
        detector = client.app.state.incident_detector
        assert detector.is_running is True

        client.put(CONTROL_PATH, json={"enabled": True, "failure_rate_percent": 100})
        for _ in range(5):
            client.get(ORDERS_PATH)

        deadline = time.monotonic() + 3.0
        while client.app.state.incidents.active_count() == 0 and time.monotonic() < deadline:
            time.sleep(0.05)

        assert client.app.state.incidents.active_count() == 1

    # On lifespan shutdown the supervised task is cancelled and awaited.
    assert detector.is_running is False


def test_detector_disabled_does_not_run() -> None:
    with incidents_client(
        incident_detection_enabled=False, failure_injection_enabled=True
    ) as client:
        detector = client.app.state.incident_detector
        assert detector.is_running is False

        client.put(CONTROL_PATH, json={"enabled": True, "failure_rate_percent": 100})
        for _ in range(5):
            client.get(ORDERS_PATH)
        time.sleep(0.2)

        assert client.app.state.incidents.active_count() == 0


# --- configuration -----------------------------------------------------------


def test_incident_defaults() -> None:
    settings = build_settings()

    assert settings.incident_detection_enabled is True
    assert settings.incident_evaluation_interval_seconds == 15.0
    assert settings.incident_recovery_evaluations == 2
    assert settings.incident_critical_budget_consumed == 1.0
    assert settings.incident_history_limit == 500
    assert settings.incidents_path == "/api/incidents"


def test_incidents_path_is_configurable() -> None:
    with incidents_client(incidents_path="/internal/incidents") as client:
        assert client.get("/internal/incidents").status_code == 200
        assert client.get(INCIDENTS_PATH).status_code == 404


@pytest.mark.parametrize(
    "overrides",
    [
        {"incident_evaluation_interval_seconds": 0},
        {"incident_evaluation_interval_seconds": -1},
        {"incident_evaluation_interval_seconds": 10_000},
        {"incident_recovery_evaluations": 0},
        {"incident_recovery_evaluations": 1_000},
        {"incident_critical_budget_consumed": 0.5},
        {"incident_critical_budget_consumed": 10_000},
        {"incident_history_limit": 0},
        {"incidents_path": "api/incidents"},
        {"incidents_path": "/"},
    ],
)
def test_invalid_incident_settings_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        build_settings(**overrides)
