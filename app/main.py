"""Application factory for the Orders API."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Settings, get_settings
from app.errors import (
    OrderNotFoundError,
    http_exception_handler,
    order_not_found_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.logging_config import configure_logging, get_logger
from app.middleware import RequestContextMiddleware
from app.repository import OrderRepository
from app.routers import health, orders

_UTC = timezone.utc

DESCRIPTION = """
Read-only HTTP API for customer orders, used as the workload instrumented by
the Reliability Intelligence Platform.

* `GET /health` - liveness probe, no dependency checks
* `GET /ready` - readiness probe, returns 503 when a dependency check fails
* `GET {api_prefix}/orders` - paginated, filterable order list
* `GET {api_prefix}/orders/{{order_id}}` - full order detail
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = settings or get_settings()

    configure_logging(settings)
    logger = get_logger("app.startup")

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        # Built here rather than at import time so the store is only reported
        # ready once the process has actually finished starting up.
        application.state.repository = OrderRepository()
        application.state.started_at = datetime.now(_UTC)
        logger.info(
            "service_started",
            extra={
                "orders_loaded": application.state.repository.count(),
                "host": settings.host,
                "port": settings.port,
            },
        )
        try:
            yield
        finally:
            logger.info("service_stopping")

    application = FastAPI(
        title="Reliability Intelligence Platform - Orders API",
        description=DESCRIPTION.format(api_prefix=settings.api_prefix),
        version=settings.app_version,
        debug=settings.debug,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "health", "description": "Liveness and readiness probes."},
            {"name": "orders", "description": "Order queries."},
        ],
    )

    application.state.settings = settings

    application.add_middleware(RequestContextMiddleware)
    application.add_exception_handler(OrderNotFoundError, order_not_found_handler)
    application.add_exception_handler(StarletteHTTPException, http_exception_handler)
    application.add_exception_handler(RequestValidationError, validation_exception_handler)
    application.add_exception_handler(Exception, unhandled_exception_handler)

    application.include_router(health.router)
    application.include_router(orders.router, prefix=settings.api_prefix)

    logger.info(
        "application_configured",
        extra={"environment": settings.environment, "log_format": settings.log_format},
    )

    return application


app = create_app()
