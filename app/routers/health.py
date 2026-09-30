"""Liveness and readiness endpoints.

These are deliberately cheap and dependency-free in the liveness case, because
a failing liveness probe restarts the process while a failing readiness probe
only removes it from load-balancer rotation. Keeping them separate is what
lets a platform tell "crashed" apart from "not able to serve yet".
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response, status

from app.models import HealthResponse, ReadinessCheck, ReadinessResponse
from app.repository import OrderRepository

router = APIRouter(tags=["health"])

_UTC = timezone.utc


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    description=(
        "Reports whether the process is up. Intentionally performs no "
        "dependency checks so a slow dependency cannot trigger a restart."
    ),
)
async def health(request: Request, response: Response) -> HealthResponse:
    settings = request.app.state.settings
    started_at = getattr(request.app.state, "started_at", None)

    # Probes are polled constantly and must never be served from a cache.
    response.headers["Cache-Control"] = "no-store"

    uptime_seconds = (
        max(0.0, (datetime.now(_UTC) - started_at).total_seconds())
        if started_at is not None
        else 0.0
    )

    return HealthResponse(
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        uptime_seconds=round(uptime_seconds, 3),
        timestamp=datetime.now(_UTC),
    )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    description=(
        "Reports whether the service can serve traffic. Returns 503 while any "
        "dependency check fails, which keeps the instance out of rotation "
        "without restarting it."
    ),
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def ready(request: Request, response: Response) -> ReadinessResponse:
    settings = request.app.state.settings
    checks = [await _check_order_store(request.app.state.repository)]

    response.headers["Cache-Control"] = "no-store"

    all_passed = all(check.status == "pass" for check in checks)
    if not all_passed:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ready" if all_passed else "not_ready",
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        checks=checks,
        timestamp=datetime.now(_UTC),
    )


async def _check_order_store(repository: OrderRepository) -> ReadinessCheck:
    started_at = time.perf_counter()
    loaded = repository.is_loaded
    order_count = repository.count() if loaded else 0
    latency_ms = round((time.perf_counter() - started_at) * 1000, 3)

    return ReadinessCheck(
        name="order_store",
        status="pass" if loaded and order_count > 0 else "fail",
        detail=f"{order_count} orders available" if loaded else "order store not initialised",
        latency_ms=latency_ms,
    )
