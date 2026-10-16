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
    IncidentNotFoundError,
    OrderNotFoundError,
    http_exception_handler,
    incident_not_found_handler,
    order_not_found_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.failure_injection import FailureInjector
from app.incidents import IncidentDetector, IncidentStore
from app.logging_config import configure_logging, get_logger
from app.metrics import UNMATCHED_ROUTE, Metrics, RouteTemplateResolver
from app.middleware import FailureInjectionMiddleware, RequestContextMiddleware
from app.reliability import ReliabilityTracker
from app.repository import OrderRepository
from app.routers import failure, health, incidents, metrics, orders, reliability

_UTC = timezone.utc

DESCRIPTION = """
Read-only HTTP API for customer orders, used as the workload instrumented by
the Reliability Intelligence Platform.

* `GET /health` - liveness probe, no dependency checks
* `GET /ready` - readiness probe, returns 503 when a dependency check fails
* `GET {api_prefix}/orders` - paginated, filterable order list
* `GET {api_prefix}/orders/{{order_id}}` - full order detail
* `GET {api_prefix}/reliability` - rolling SLI/SLO and error-budget evaluation
* `GET {api_prefix}/incidents` - incident list, detail and acknowledgement
* `GET /metrics` - Prometheus metrics for scraping
"""


def _incident_route_templates(incidents_path: str) -> tuple[str, str, str]:
    """Route templates for the incident API, as resolved by the middleware.

    The list route is a fixed path; the detail and acknowledge routes carry an
    ``{incident_id}`` placeholder. All three are administrative and must be
    excluded from reliability and request metrics.
    """
    return (
        incidents_path,
        f"{incidents_path}/{{incident_id}}",
        f"{incidents_path}/{{incident_id}}/acknowledge",
    )


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

        # The detector is supervised by this lifespan: its asyncio task is
        # created on startup and cancelled and awaited on shutdown, so it never
        # outlives the application and never runs uncontrolled.
        detector: IncidentDetector = application.state.incident_detector
        if settings.incident_detection_enabled:
            detector.start()
            logger.info(
                "incident_detection_started",
                extra={
                    "interval_seconds": settings.incident_evaluation_interval_seconds,
                    "recovery_evaluations": settings.incident_recovery_evaluations,
                },
            )
        else:
            logger.info("incident_detection_disabled")

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
            await detector.stop()
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
            {
                "name": "failure-injection",
                "description": (
                    "Local-development only. Controlled latency and HTTP 500 "
                    "injection, absent unless explicitly enabled."
                ),
            },
            {
                "name": "reliability",
                "description": (
                    "Rolling service-level indicators measured against the "
                    "configured objectives, with the derived error budget."
                ),
            },
            {
                "name": "incidents",
                "description": (
                    "Automatically detected reliability incidents and their "
                    "lifecycle. Incidents are created and resolved by the "
                    "detector; the API only lists, reads and acknowledges them."
                ),
            },
        ],
    )

    application.state.settings = settings
    application.state.metrics = Metrics(settings)
    application.state.failure_injector = FailureInjector(settings)
    # Administrative routes are never part of user-facing reliability: probes,
    # scrapes, the failure-control plane, the reliability read-out and the
    # incident API itself are excluded, along with unmatched (unrouted) paths.
    application.state.reliability = ReliabilityTracker(
        availability_target=settings.slo_availability_target,
        latency_target=settings.slo_latency_target,
        latency_threshold_ms=settings.slo_latency_threshold_ms,
        window_seconds=settings.slo_window_seconds,
        at_risk_budget_remaining=settings.slo_at_risk_budget_remaining,
        excluded_routes={
            "/health",
            "/ready",
            settings.metrics_path,
            settings.failure_injection_path,
            settings.reliability_path,
            *_incident_route_templates(settings.incidents_path),
            UNMATCHED_ROUTE,
        },
    )
    # Incident state is process-local and rebuilt empty on every start; the
    # detector is created here so tests can drive it directly, and started by
    # the lifespan below.
    application.state.incidents = IncidentStore(
        history_limit=settings.incident_history_limit
    )
    application.state.incident_detector = IncidentDetector(
        tracker=application.state.reliability,
        store=application.state.incidents,
        metrics=application.state.metrics,
        recovery_evaluations=settings.incident_recovery_evaluations,
        interval_seconds=settings.incident_evaluation_interval_seconds,
        critical_budget_consumed=settings.incident_critical_budget_consumed,
    )

    application.include_router(health.router)
    application.include_router(orders.router, prefix=settings.api_prefix)
    application.include_router(reliability.build_router(settings.reliability_path))
    application.include_router(incidents.build_router(settings.incidents_path))

    if settings.failure_injection_enabled:
        application.include_router(failure.build_router(settings.failure_injection_path))
        logger.warning(
            "failure_injection_enabled",
            extra={"control_path": settings.failure_injection_path},
        )
    else:
        logger.info("failure_injection_disabled")

    # Route templates come from the documented API surface, so metrics always
    # agree with the OpenAPI schema and carry no per-request path values.
    route_resolver = RouteTemplateResolver(application.openapi()["paths"].keys())

    # Failure injection runs *inside* the request-context middleware: the delay
    # and any injected 500 are then logged and measured by the existing
    # instrumentation rather than by a parallel set of metrics.
    application.add_middleware(
        FailureInjectionMiddleware,
        settings=settings,
        injector=application.state.failure_injector,
    )
    application.add_middleware(
        RequestContextMiddleware, settings=settings, route_resolver=route_resolver
    )

    application.add_exception_handler(OrderNotFoundError, order_not_found_handler)
    application.add_exception_handler(IncidentNotFoundError, incident_not_found_handler)
    application.add_exception_handler(StarletteHTTPException, http_exception_handler)
    application.add_exception_handler(RequestValidationError, validation_exception_handler)
    application.add_exception_handler(Exception, unhandled_exception_handler)

    if settings.metrics_enabled:
        application.include_router(metrics.build_router(settings.metrics_path))
    else:
        logger.info("metrics_disabled")

    logger.info(
        "application_configured",
        extra={
            "environment": settings.environment,
            "log_format": settings.log_format,
            "metrics_enabled": settings.metrics_enabled,
        },
    )

    return application


app = create_app()
