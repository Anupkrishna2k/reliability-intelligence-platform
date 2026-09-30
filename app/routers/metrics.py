"""Prometheus scrape endpoint.

Excluded from the OpenAPI schema: it exists for Prometheus, not API consumers.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.metrics import Metrics

DEFAULT_METRICS_PATH = "/metrics"


def build_router(path: str = DEFAULT_METRICS_PATH) -> APIRouter:
    """Build the metrics router, mounted at ``path``."""
    router = APIRouter(tags=["metrics"])

    @router.get(
        path,
        include_in_schema=False,
        summary="Prometheus metrics",
        description=(
            "Exposes this instance's metrics in the Prometheus text format. "
            "Intended to be scraped, not called by hand."
        ),
    )
    async def metrics(request: Request) -> Response:
        application_metrics: Metrics = request.app.state.metrics
        payload, content_type = application_metrics.render()
        return Response(content=payload, media_type=content_type)

    return router
