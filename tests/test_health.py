"""Liveness and readiness probe behaviour."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_health_returns_200(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_reports_service_identity(client: TestClient) -> None:
    body = client.get("/health").json()

    assert body["service"] == "orders-api"
    assert body["version"] == "1.0.0"
    assert body["environment"] == "test"
    assert body["uptime_seconds"] >= 0
    assert body["timestamp"]


def test_health_does_not_check_dependencies(client: TestClient) -> None:
    """Liveness must stay answerable even if a dependency is broken."""
    client.app.state.repository._loaded = False

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_does_not_require_the_order_store(client: TestClient) -> None:
    del client.app.state.repository

    assert client.get("/health").status_code == 200


def test_ready_returns_200_when_service_can_serve(client: TestClient) -> None:
    response = client.get("/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["service"] == "orders-api"


def test_ready_reports_each_check(client: TestClient) -> None:
    checks = client.get("/ready").json()["checks"]

    assert [check["name"] for check in checks] == ["order_store"]
    assert checks[0]["status"] == "pass"
    assert checks[0]["latency_ms"] >= 0
    assert "orders available" in checks[0]["detail"]


def test_ready_returns_503_when_order_store_is_not_loaded(client: TestClient) -> None:
    client.app.state.repository._loaded = False

    response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"][0]["status"] == "fail"


def test_ready_returns_503_when_order_store_is_empty(settings: Settings) -> None:
    from app.repository import OrderRepository

    with TestClient(create_app(settings)) as test_client:
        test_client.app.state.repository = OrderRepository(orders=[])

        response = test_client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


@pytest.mark.parametrize("path", ["/health", "/ready"])
def test_probes_are_never_cached(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert "no-store" in response.headers.get("cache-control", "")


def test_probes_appear_in_openapi(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert "/health" in paths
    assert "/ready" in paths
