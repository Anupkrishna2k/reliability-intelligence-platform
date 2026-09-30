"""Environment-based application configuration.

Values are read from real environment variables first, then from a local
``.env`` file, then from the defaults declared below.

Every variable is namespaced with the ``ORDERS_API_`` prefix, e.g.::

    ORDERS_API_PORT=8080
    ORDERS_API_LOG_LEVEL=DEBUG
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

VALID_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
VALID_LOG_FORMATS = {"json", "text"}


class Settings(BaseSettings):
    """Runtime configuration for the Orders API."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="ORDERS_API_",
        case_sensitive=False,
        extra="ignore",
    )

    # Service identity
    app_name: str = "orders-api"
    app_version: str = "1.0.0"
    environment: str = "development"
    debug: bool = False

    # HTTP server binding
    host: str = "0.0.0.0"
    port: int = 8000

    # Observability
    log_level: str = "INFO"
    log_format: str = "json"

    # Metrics
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"
    # Probes and scrapes are excluded from the general request metrics by
    # default: they are called on a fixed timer regardless of user traffic and
    # would otherwise dominate request counts and latency percentiles.
    metrics_include_probes: bool = False

    # API behaviour
    api_prefix: str = "/api"
    default_page_size: int = 20
    max_page_size: int = 100

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in VALID_LOG_LEVELS:
            raise ValueError(
                f"log_level must be one of {sorted(VALID_LOG_LEVELS)}, got {value!r}"
            )
        return level

    @field_validator("log_format")
    @classmethod
    def _validate_log_format(cls, value: str) -> str:
        log_format = value.lower()
        if log_format not in VALID_LOG_FORMATS:
            raise ValueError(
                f"log_format must be one of {sorted(VALID_LOG_FORMATS)}, got {value!r}"
            )
        return log_format

    @field_validator("port")
    @classmethod
    def _validate_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError(f"port must be between 1 and 65535, got {value}")
        return value

    @field_validator("metrics_path")
    @classmethod
    def _validate_metrics_path(cls, value: str) -> str:
        path = value.strip()
        if not path.startswith("/"):
            raise ValueError(f"metrics_path must start with '/', got {value!r}")
        return path

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in {"prod", "production"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
