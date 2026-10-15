"""Reliability evaluation endpoint.

Read-only view of the rolling SLI/SLO/error-budget evaluation. The calculation
itself lives in :mod:`app.reliability`; this router only resolves the running
tracker, refreshes the exported gauges and returns the structured response.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.models import ReliabilityResponse
from app.reliability import ReliabilityTracker

DEFAULT_RELIABILITY_PATH = "/api/reliability"


def build_router(path: str = DEFAULT_RELIABILITY_PATH) -> APIRouter:
    """Build the reliability router mounted at ``path``."""
    router = APIRouter(tags=["reliability"])

    @router.get(
        path,
        response_model=ReliabilityResponse,
        summary="Evaluate SLIs, SLOs and the error budget",
        description=(
            "Evaluates availability and latency indicators over the rolling "
            "observation window, compares them with the configured SLOs, and "
            "reports the error budget. With no eligible traffic in the window "
            "the result status is 'insufficient_data', never a false 100%."
        ),
    )
    async def get_reliability(request: Request) -> ReliabilityResponse:
        settings = request.app.state.settings
        tracker: ReliabilityTracker = request.app.state.reliability

        snapshot = tracker.snapshot().model_copy(update={"service": settings.app_name})

        # Refresh the exported gauges so a direct call and a Prometheus scrape
        # always agree on the current evaluation.
        request.app.state.metrics.update_reliability(snapshot)
        return snapshot

    return router
