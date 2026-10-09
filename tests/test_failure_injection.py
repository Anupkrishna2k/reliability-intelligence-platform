"""Controlled failure injection: latency, HTTP 500s, safety and instrumentation.

These tests exercise the feature both through its HTTP control API and by
scraping ``/metrics``, so they verify exactly what an operator would do in a
demo and exactly what Prometheus would collect.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from tests.conftest import build_settings
from tests.test_metrics import scrape, value, value_or_zero

ORDERS_PATH = "/api/orders"
CONTROL_PATH = "/admin/failure"


def injection_client(**overrides: object) -> TestClient:
    """A client with failure injection explicitly enabled (test environment)."""
    settings = build_settings(
        environment="test",
        log_level="WARNING",
        failure_injection_enabled=True,
        **overrides,
    )
    return TestClient(create_app(settings))


def plain_client(**overrides: object) -> TestClient:
    """A client with the feature flag at its default (disabled)."""
    settings = build_settings(environment="test", log_level="WARNING", **overrides)
    return TestClient(create_app(settings))


@pytest.fixture
def enabled_client() -> Iterator[TestClient]:
    with injection_client() as client:
        yield client


class _RecordingHandler(logging.Handler):
    """Collect log records without touching the root logger's handlers."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


# --- default behaviour / safety guard ----------------------------------------


def test_control_api_absent_when_disabled() -> None:
    with plain_client() as client:
        assert client.get(CONTROL_PATH).status_code == 404
        assert client.put(CONTROL_PATH, json={"latency_ms": 100}).status_code == 404
        assert client.delete(CONTROL_PATH).status_code == 404


def test_control_path_hidden_from_openapi_when_disabled() -> None:
    with plain_client() as client:
        assert CONTROL_PATH not in client.get("/openapi.json").json()["paths"]


def test_default_behaviour_is_unchanged_when_disabled() -> None:
    with plain_client() as client:
        started = time.perf_counter()
        response = client.get(ORDERS_PATH)
        elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 0.5


def test_injector_is_ignored_when_feature_disabled() -> None:
    """Belt and braces: even a configured scenario does nothing while off."""
    with plain_client() as client:
        client.app.state.failure_injector.configure(
            enabled=True, latency_ms=500, failure_rate_percent=100
        )

        started = time.perf_counter()
        response = client.get(ORDERS_PATH)
        elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 0.3


def test_enabling_injection_in_production_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_settings(environment="production", failure_injection_enabled=True)


def test_control_path_must_start_with_slash() -> None:
    with pytest.raises(ValidationError):
        build_settings(failure_injection_path="admin/failure")


# --- control API -------------------------------------------------------------


def test_control_api_reports_idle_scenario_initially(enabled_client: TestClient) -> None:
    response = enabled_client.get(CONTROL_PATH)

    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "latency_ms": 0,
        "failure_rate_percent": 0.0,
    }


def test_control_path_is_exposed_when_enabled(enabled_client: TestClient) -> None:
    assert CONTROL_PATH in enabled_client.get("/openapi.json").json()["paths"]


def test_control_path_is_configurable() -> None:
    with injection_client(failure_injection_path="/internal/failure") as client:
        assert client.get("/internal/failure").status_code == 200
        assert client.get(CONTROL_PATH).status_code == 404


# --- artificial latency ------------------------------------------------------


def test_latency_is_injected(enabled_client: TestClient) -> None:
    enabled_client.put(
        CONTROL_PATH,
        json={"enabled": True, "latency_ms": 200, "failure_rate_percent": 0},
    )

    started = time.perf_counter()
    response = enabled_client.get(ORDERS_PATH)
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed >= 0.18


def test_latency_is_not_injected_when_scenario_disabled(enabled_client: TestClient) -> None:
    enabled_client.put(
        CONTROL_PATH,
        json={"enabled": False, "latency_ms": 500, "failure_rate_percent": 0},
    )

    assert enabled_client.app.state.failure_injector.delay_seconds() == 0.0

    started = time.perf_counter()
    response = enabled_client.get(ORDERS_PATH)
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed < 0.3


# --- HTTP 500 injection ------------------------------------------------------


def test_http_500_is_injected_at_full_rate(enabled_client: TestClient) -> None:
    enabled_client.put(CONTROL_PATH, json={"failure_rate_percent": 100})

    response = enabled_client.get(ORDERS_PATH)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "injected_failure"
    assert response.json()["request_id"]


def test_zero_failure_rate_never_fails(enabled_client: TestClient) -> None:
    enabled_client.put(CONTROL_PATH, json={"failure_rate_percent": 0})

    for _ in range(20):
        assert enabled_client.get(ORDERS_PATH).status_code == 200


# --- instrumentation ---------------------------------------------------------


def test_injected_500_is_counted_by_existing_request_metrics(
    enabled_client: TestClient,
) -> None:
    enabled_client.put(CONTROL_PATH, json={"failure_rate_percent": 100})
    enabled_client.get(ORDERS_PATH)

    samples = scrape(enabled_client)

    assert value(
        samples,
        "orders_api_http_requests_total",
        method="GET",
        route=ORDERS_PATH,
        status_code="500",
    ) == 1.0
    assert value_or_zero(
        samples,
        "orders_api_http_requests_total",
        method="GET",
        route=ORDERS_PATH,
        status_code="200",
    ) == 0.0


def test_injected_latency_is_observed_by_the_histogram(
    enabled_client: TestClient,
) -> None:
    enabled_client.put(CONTROL_PATH, json={"latency_ms": 200, "failure_rate_percent": 0})
    enabled_client.get(ORDERS_PATH)

    samples = scrape(enabled_client)

    assert value(
        samples,
        "orders_api_http_request_duration_seconds_count",
        method="GET",
        route=ORDERS_PATH,
    ) == 1.0
    assert (
        value(
            samples,
            "orders_api_http_request_duration_seconds_sum",
            method="GET",
            route=ORDERS_PATH,
        )
        >= 0.18
    )


def test_injected_failure_is_logged(enabled_client: TestClient) -> None:
    handler = _RecordingHandler()
    logger = logging.getLogger("app.failure_injection")
    logger.addHandler(handler)
    try:
        enabled_client.put(CONTROL_PATH, json={"failure_rate_percent": 100})
        enabled_client.get(ORDERS_PATH)
    finally:
        logger.removeHandler(handler)

    injected = [record for record in handler.records if record.getMessage() == "injected_failure"]
    assert len(injected) == 1
    assert injected[0].http_path == ORDERS_PATH
    assert injected[0].http_status == 500


def test_control_requests_are_not_application_traffic(enabled_client: TestClient) -> None:
    enabled_client.put(CONTROL_PATH, json={"failure_rate_percent": 0})

    routes = {
        dict(labels).get("route")
        for name, labels in scrape(enabled_client)
        if name == "orders_api_http_requests_total"
    }
    assert CONTROL_PATH not in routes


# --- validation --------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"latency_ms": -1},
        {"latency_ms": 10_001},
        {"latency_ms": "soon"},
        {"failure_rate_percent": -0.1},
        {"failure_rate_percent": 100.1},
        {"failure_rate_percent": "often"},
        {"unexpected_field": 1},
    ],
)
def test_invalid_settings_are_rejected(enabled_client: TestClient, payload: dict) -> None:
    assert enabled_client.put(CONTROL_PATH, json=payload).status_code == 422


# --- reset / disable ---------------------------------------------------------


def test_delete_resets_to_normal_behaviour(enabled_client: TestClient) -> None:
    enabled_client.put(
        CONTROL_PATH,
        json={"enabled": True, "latency_ms": 500, "failure_rate_percent": 100},
    )

    reset = enabled_client.delete(CONTROL_PATH)

    assert reset.status_code == 200
    assert reset.json() == {
        "enabled": False,
        "latency_ms": 0,
        "failure_rate_percent": 0.0,
    }

    started = time.perf_counter()
    response = enabled_client.get(ORDERS_PATH)
    elapsed = time.perf_counter() - started
    assert response.status_code == 200
    assert elapsed < 0.3


def test_put_can_disable_while_keeping_values(enabled_client: TestClient) -> None:
    enabled_client.put(CONTROL_PATH, json={"enabled": True, "failure_rate_percent": 100})
    enabled_client.put(CONTROL_PATH, json={"enabled": False, "failure_rate_percent": 100})

    assert enabled_client.get(ORDERS_PATH).status_code == 200
    assert enabled_client.app.state.failure_injector.should_fail() is False


# --- health and readiness stay usable ----------------------------------------


def test_probes_metrics_and_control_survive_injection(enabled_client: TestClient) -> None:
    enabled_client.put(
        CONTROL_PATH,
        json={"enabled": True, "latency_ms": 300, "failure_rate_percent": 100},
    )

    assert enabled_client.get("/health").status_code == 200
    assert enabled_client.get("/ready").status_code == 200
    assert enabled_client.get("/metrics").status_code == 200
    assert enabled_client.get(CONTROL_PATH).status_code == 200
