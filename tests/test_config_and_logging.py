"""Configuration loading and request-correlation behaviour."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pythonjsonlogger.json import JsonFormatter

from app.config import Settings, get_settings
from app.logging_config import ContextFilter, configure_logging
from app.main import create_app
from tests.conftest import build_settings


# --- configuration -----------------------------------------------------------


def test_defaults_match_documented_values() -> None:
    settings = build_settings()

    assert settings.app_name == "orders-api"
    assert settings.host == "0.0.0.0"
    assert settings.port == 8000
    assert settings.log_level == "INFO"
    assert settings.log_format == "json"
    assert settings.api_prefix == "/api"


def test_environment_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDERS_API_PORT", "9001")
    monkeypatch.setenv("ORDERS_API_ENVIRONMENT", "staging")
    monkeypatch.setenv("ORDERS_API_LOG_LEVEL", "debug")
    monkeypatch.setenv("ORDERS_API_LOG_FORMAT", "TEXT")
    monkeypatch.setenv("ORDERS_API_MAX_PAGE_SIZE", "5")

    settings = Settings(_env_file=None)

    assert settings.port == 9001
    assert settings.environment == "staging"
    assert settings.log_level == "DEBUG"
    assert settings.log_format == "text"
    assert settings.max_page_size == 5


def test_is_production_recognises_production_names() -> None:
    assert build_settings(environment="production").is_production is True
    assert build_settings(environment="PROD").is_production is True
    assert build_settings(environment="test").is_production is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"log_level": "chatty"},
        {"log_format": "xml"},
        {"port": 0},
        {"port": 70000},
    ],
)
def test_invalid_values_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        build_settings(**overrides)


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()

    assert get_settings() is get_settings()

    get_settings.cache_clear()


def test_configure_logging_is_idempotent() -> None:
    import logging

    settings = build_settings()

    configure_logging(settings)
    first = len(logging.getLogger().handlers)
    configure_logging(settings)

    assert first == 1
    assert len(logging.getLogger().handlers) == 1


# --- request correlation -----------------------------------------------------


def test_responses_carry_a_request_id(client: TestClient) -> None:
    assert client.get("/health").headers["x-request-id"]


def test_incoming_request_id_is_reused(client: TestClient) -> None:
    response = client.get("/health", headers={"x-request-id": "trace-abc-123"})

    assert response.headers["x-request-id"] == "trace-abc-123"


def test_request_id_is_returned_in_error_bodies(client: TestClient) -> None:
    response = client.get("/api/orders/ORD-0000-0000", headers={"x-request-id": "trace-xyz"})

    assert response.json()["request_id"] == "trace-xyz"


def test_access_log_is_one_json_record_per_request() -> None:
    """End-to-end check that a request produces a single structured log line."""
    import io
    import json
    import logging

    settings = build_settings(environment="test", log_level="INFO", log_format="json")
    root = logging.getLogger()

    with TestClient(create_app(settings)) as client:
        # create_app configures logging, so capture only after it has run.
        original_handlers = list(root.handlers)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream=stream)
        handler.setFormatter(
            JsonFormatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s "
                "%(service)s %(environment)s %(request_id)s",
                rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
            )
        )
        handler.addFilter(ContextFilter(settings.app_name, settings.environment))
        root.handlers = [handler]
        try:
            client.get("/api/orders", headers={"x-request-id": "trace-123"})
        finally:
            root.handlers = original_handlers

    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    access = [line for line in lines if line["message"] == "http_request"]

    assert len(access) == 1
    record = access[0]
    assert record["level"] == "INFO"
    assert record["logger"] == "app.access"
    assert record["service"] == "orders-api"
    assert record["environment"] == "test"
    assert record["request_id"] == "trace-123"
    assert record["http_method"] == "GET"
    assert record["http_path"] == "/api/orders"
    assert record["http_status"] == 200
    assert record["duration_ms"] >= 0
