"""Structured application logging.

Emits one JSON object per log line so records can be shipped and queried by a
log pipeline. Every record carries the service name, the deployment
environment, and (for anything inside a request) the correlation id.
"""

from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any

from pythonjsonlogger.json import JsonFormatter

from app.config import Settings

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

JSON_LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s %(message)s "
    "%(service)s %(environment)s %(request_id)s"
)

TEXT_LOG_FORMAT = (
    "%(asctime)s %(levelname)-8s %(name)s "
    "[service=%(service)s env=%(environment)s request_id=%(request_id)s] "
    "%(message)s"
)

LOG_FORMAT_RENAMES = {
    "asctime": "timestamp",
    "levelname": "level",
    "name": "logger",
}


def new_request_id() -> str:
    """Generate a new correlation id for an inbound request."""
    return uuid.uuid4().hex


def set_request_id(request_id: str) -> Any:
    """Bind a correlation id to the current context, returning a reset token."""
    return _request_id.set(request_id)


def reset_request_id(token: Any) -> None:
    """Restore the previous correlation id."""
    _request_id.reset(token)


def get_request_id() -> str | None:
    """Return the correlation id bound to the current context, if any."""
    return _request_id.get()


class ContextFilter(logging.Filter):
    """Stamp service/environment/request context onto every record."""

    def __init__(self, service: str, environment: str) -> None:
        super().__init__()
        self._service = service
        self._environment = environment

    def filter(self, record: logging.LogRecord) -> bool:
        record.service = self._service
        record.environment = self._environment
        record.request_id = get_request_id() or "-"
        return True


def configure_logging(settings: Settings) -> None:
    """Install the application logging configuration on the root logger.

    Safe to call more than once: previously installed handlers are replaced so
    repeated configuration never duplicates log lines.
    """
    level = getattr(logging, settings.log_level, logging.INFO)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        JsonFormatter(JSON_LOG_FORMAT, rename_fields=LOG_FORMAT_RENAMES)
        if settings.log_format == "json"
        else logging.Formatter(TEXT_LOG_FORMAT)
    )
    handler.addFilter(ContextFilter(settings.app_name, settings.environment))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    root.addHandler(handler)
    root.setLevel(level)

    # Route uvicorn through our handler so its output is structured too, and
    # drop its access log: RequestContextMiddleware already emits one JSON
    # record per request with timing and status.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True

    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str) -> logging.Logger:
    """Return a module logger."""
    return logging.getLogger(name)
