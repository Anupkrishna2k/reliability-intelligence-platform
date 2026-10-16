"""Incident query and lifecycle endpoints.

Read-only listing and lookup, plus a single documented mutation:
acknowledgement. Incidents themselves are created and resolved solely by the
detector (see :mod:`app.incidents`); the API never lets a caller create, delete
or arbitrarily edit one.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.errors import IncidentNotFoundError
from app.incidents import IncidentStore
from app.models import (
    Incident,
    IncidentListResponse,
    IncidentPagination,
    IncidentSeverity,
    IncidentStatus,
    IncidentType,
)

DEFAULT_INCIDENTS_PATH = "/api/incidents"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

_UTC = timezone.utc


def _store(request: Request) -> IncidentStore:
    return request.app.state.incidents


def build_router(path: str = DEFAULT_INCIDENTS_PATH) -> APIRouter:
    """Build the incidents router mounted at ``path``."""
    router = APIRouter(tags=["incidents"])

    @router.get(
        path,
        response_model=IncidentListResponse,
        summary="List incidents",
        description=(
            "Lists incident records newest first, with optional filters by "
            "status, severity and type, and limit/offset paging. The response "
            "also carries summary counts. Records are held in memory and are "
            "lost when the process restarts."
        ),
    )
    async def list_incidents(
        request: Request,
        status_filter: IncidentStatus | None = Query(
            default=None, alias="status", description="Only incidents in this state."
        ),
        severity: IncidentSeverity | None = Query(
            default=None, description="Only incidents of this severity."
        ),
        incident_type: IncidentType | None = Query(
            default=None, alias="type", description="Only incidents of this type."
        ),
        limit: int = Query(
            default=DEFAULT_PAGE_SIZE,
            ge=1,
            le=MAX_PAGE_SIZE,
            description="Maximum number of incidents to return.",
        ),
        offset: int = Query(default=0, ge=0, description="Number of incidents to skip."),
    ) -> IncidentListResponse:
        store = _store(request)
        incidents, total = store.list(
            status=status_filter,
            severity=severity,
            incident_type=incident_type,
            limit=limit,
            offset=offset,
        )
        return IncidentListResponse(
            incidents=incidents,
            pagination=IncidentPagination(
                total=total,
                limit=limit,
                offset=offset,
                returned=len(incidents),
                has_more=offset + len(incidents) < total,
            ),
            counts=store.counts(),
        )

    @router.get(
        path + "/{incident_id}",
        response_model=Incident,
        summary="Get an incident",
        description="Returns one incident, including its triggers and evidence.",
        responses={
            status.HTTP_404_NOT_FOUND: {"description": "No incident has that id."}
        },
    )
    async def get_incident(incident_id: str, request: Request) -> Incident:
        incident = _store(request).get(incident_id)
        if incident is None:
            raise IncidentNotFoundError(incident_id)
        return incident

    @router.post(
        path + "/{incident_id}/acknowledge",
        response_model=Incident,
        summary="Acknowledge an incident",
        description=(
            "Marks an incident as acknowledged so operators can see it has been "
            "picked up. Acknowledgement does not resolve the incident: the "
            "detector closes it when the reliability condition recovers. "
            "Acknowledging an already-acknowledged incident is idempotent; a "
            "resolved incident cannot be acknowledged."
        ),
        responses={
            status.HTTP_404_NOT_FOUND: {"description": "No incident has that id."},
            status.HTTP_409_CONFLICT: {
                "description": "The incident is already resolved."
            },
        },
    )
    async def acknowledge_incident(incident_id: str, request: Request) -> Incident:
        store = _store(request)
        incident = store.get(incident_id)
        if incident is None:
            raise IncidentNotFoundError(incident_id)
        if incident.status is IncidentStatus.RESOLVED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "incident_resolved",
                    "message": f"Incident '{incident_id}' is resolved and cannot be acknowledged.",
                },
            )
        return store.acknowledge(incident_id, at=datetime.now(_UTC))

    return router
