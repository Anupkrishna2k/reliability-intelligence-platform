"""Failure-injection control API (local development / demo only).

This router is only mounted when ``Settings.failure_injection_enabled`` is on,
and ``Settings`` refuses to turn that on in a production environment. When the
feature is off these paths do not exist, so there is nothing to disable.

Routes
------
* ``GET``    the current scenario
* ``PUT``    replace the scenario (validated and bounded)
* ``DELETE`` reset to normal behaviour (disabled, zero delay, zero failures)
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.failure_injection import FailureInjector, FailureScenario
from app.logging_config import get_logger
from app.models import FailureConfig, FailureConfigUpdate

logger = get_logger("app.failure_injection.control")


def _to_response(scenario: FailureScenario) -> FailureConfig:
    return FailureConfig(
        enabled=scenario.enabled,
        latency_ms=scenario.latency_ms,
        failure_rate_percent=scenario.failure_rate_percent,
    )


def _injector(request: Request) -> FailureInjector:
    return request.app.state.failure_injector


def build_router(path: str) -> APIRouter:
    """Build the control router mounted at ``path``."""
    router = APIRouter(prefix=path, tags=["failure-injection"])

    @router.get(
        "",
        response_model=FailureConfig,
        summary="Read the active failure-injection scenario",
    )
    async def read_failure_config(request: Request) -> FailureConfig:
        return _to_response(_injector(request).snapshot())

    @router.put(
        "",
        response_model=FailureConfig,
        summary="Configure failure injection",
        description=(
            "Set the artificial latency (0-10000 ms) and/or the HTTP 500 rate "
            "(0-100 percent). Setting both to zero, or `enabled=true` with both "
            "at zero, has no effect on requests."
        ),
    )
    async def configure_failure(
        payload: FailureConfigUpdate, request: Request
    ) -> FailureConfig:
        scenario = _injector(request).configure(
            enabled=payload.enabled,
            latency_ms=payload.latency_ms,
            failure_rate_percent=payload.failure_rate_percent,
        )
        logger.warning(
            "failure_injection_configured",
            extra={
                "enabled": scenario.enabled,
                "latency_ms": scenario.latency_ms,
                "failure_rate_percent": scenario.failure_rate_percent,
            },
        )
        return _to_response(scenario)

    @router.delete(
        "",
        response_model=FailureConfig,
        summary="Reset failure injection to normal behaviour",
    )
    async def reset_failure(request: Request) -> FailureConfig:
        scenario = _injector(request).reset()
        logger.warning(
            "failure_injection_reset",
            extra={"enabled": scenario.enabled},
        )
        return _to_response(scenario)

    return router
