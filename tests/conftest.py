"""Shared pytest fixtures.

Each test gets its own app instance built from an explicit ``Settings`` object
so tests never depend on - or mutate - the developer's environment.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def build_settings(**overrides: object) -> Settings:
    """Build settings that ignore any local .env file."""
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


@pytest.fixture
def settings() -> Settings:
    return build_settings(
        app_name="orders-api",
        app_version="1.0.0",
        environment="test",
        log_level="WARNING",
        log_format="json",
        default_page_size=20,
        max_page_size=100,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client
