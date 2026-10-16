"""Automatic incident detection and lifecycle management.

This module turns the rolling reliability evaluation in :mod:`app.reliability`
into incident records. It is pure Python plus an optional, supervised asyncio
background loop, so it can be unit-tested deterministically against a fake
clock without a server and without real sleeping.

Detection rules
---------------
The detector evaluates the *same* :class:`~app.reliability.ReliabilityTracker`
the rest of the service uses; it never recomputes SLIs or budgets.

===========================  ==============  ==========  ========================
Rule                         Incident type   Severity    Fires when
===========================  ==============  ==========  ========================
``availability_slo_breach``  availability    critical*   availability status is
                                                         ``breached``
``availability_error_budget_  availability   critical*   the availability error
exhausted``                                              budget is exhausted
``latency_slo_breach``       latency         warning     latency status is
                                                         ``breached``
===========================  ==============  ==========  ========================

* Both availability rules resolve to the same severity: ``critical`` once the
consumed error-budget fraction reaches
``incident_critical_budget_consumed`` (default ``1.0``, i.e. the budget is
exhausted), otherwise ``warning``. With the default it is always critical
because a breach exhausts the budget by construction; a ``warning`` availability
incident is only possible if an operator deliberately raises that threshold to
reserve ``critical`` for deeper degradation.

Error-budget exhaustion is listed as its own rule rather than folded into the
breach rule, because the two are *almost* but not exactly equivalent: a breach
(`observed < target`) always exhausts the budget, but the budget is also
exhausted at the single point `observed == target`, where the status is
``at_risk`` rather than ``breached``. The availability incident therefore
records both triggers, and a later degradation merely *adds* the breach trigger
to the incident that already exists.

Identity and deduplication
--------------------------
An incident's identity is its :class:`~app.models.IncidentType`. At most one
active incident per type may exist, so repeated evaluations of the same breach
update the existing record (refreshing evidence and bumping ``updated_at``)
instead of opening a new one. No incident is created while the window has
``insufficient_data``.

Recovery
--------
A type's incident resolves once none of that type's rules is firing and the
window has been healthy (with sufficient data) for
``incident_recovery_evaluations`` consecutive evaluations. The debounce stops a
single noisy window from flapping an incident closed and straight back open.
Insufficient data never creates and never resolves an incident: without an
evaluation there is no evidence to act on.

Storage
-------
Incidents live in process memory (an ordered dict) and are **lost on restart**.
The store is a thin, self-contained class so it can be replaced by a database
later without touching the detector or the API.
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from app.logging_config import get_logger
from app.metrics import Metrics
from app.models import (
    RULE_AVAILABILITY_BREACH,
    RULE_AVAILABILITY_BUDGET_EXHAUSTED,
    RULE_LATENCY_BREACH,
    Incident,
    IncidentCounts,
    IncidentEvidence,
    IncidentSeverity,
    IncidentStatus,
    IncidentTrigger,
    IncidentType,
    ReliabilityResponse,
)
from app.reliability import (
    STATUS_BREACHED,
    STATUS_INSUFFICIENT_DATA,
    ReliabilityTracker,
)

UTC = timezone.utc
logger = get_logger("app.incidents")

#: Severity ordering used to collapse several firing rules into one severity.
_SEVERITY_RANK = {
    IncidentSeverity.WARNING: 1,
    IncidentSeverity.CRITICAL: 2,
}

#: Default cap on retained resolved incidents when no explicit limit is given.
DEFAULT_HISTORY_LIMIT = 500

#: Human-readable description of each rule, retained for documentation and
#: echoed nowhere else so there is a single source of truth for the policy.
RULE_DEFINITIONS: dict[str, str] = {
    RULE_AVAILABILITY_BREACH: (
        "Availability over the rolling window is below the configured target."
    ),
    RULE_AVAILABILITY_BUDGET_EXHAUSTED: (
        "The availability error budget is consumed (consumed fraction >= 1.0), "
        "which always accompanies a breach and also covers the exact-target "
        "boundary where the status is 'at_risk' rather than 'breached'."
    ),
    RULE_LATENCY_BREACH: (
        "Latency compliance over the rolling window is below the configured "
        "target."
    ),
}

_INCIDENT_TYPES = (IncidentType.AVAILABILITY, IncidentType.LATENCY)


@dataclass(frozen=True)
class RuleOutcome:
    """The result of evaluating one detection rule against a snapshot."""

    rule: str
    incident_type: IncidentType
    severity: IncidentSeverity
    triggered: bool


@dataclass(frozen=True)
class EvaluationResult:
    """The incidents created, updated and resolved by one evaluation."""

    created: tuple[Incident, ...] = ()
    updated: tuple[Incident, ...] = ()
    resolved: tuple[Incident, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.resolved)


class IncidentStore:
    """Thread-safe, in-memory incident store.

    A single ordered dictionary holds every incident (active and resolved) so
    lookups by id are O(1) and listing is in detection order. A second index
    maps each incident type to its active record to enforce the one-active-per-
    type invariant. Resolved records are evicted oldest-first once the history
    limit is reached; active records are never evicted.
    """

    def __init__(self, *, history_limit: int = DEFAULT_HISTORY_LIMIT) -> None:
        self._lock = threading.Lock()
        self._incidents: OrderedDict[str, Incident] = OrderedDict()
        self._active_by_type: dict[IncidentType, str] = {}
        self._sequence = 0
        self._history_limit = max(1, int(history_limit))

    # --- lifecycle ---------------------------------------------------------

    def create(
        self,
        *,
        incident_type: IncidentType,
        severity: IncidentSeverity,
        detected_at: datetime,
        triggers: Sequence[IncidentTrigger],
        evidence: IncidentEvidence,
    ) -> Incident:
        """Open a new incident for ``incident_type``.

        The caller is responsible for the one-active-per-type invariant; the
        detector checks :meth:`active_incident` before calling this.
        """
        with self._lock:
            self._sequence += 1
            incident = Incident(
                incident_id=f"INC-{self._sequence:06d}",
                type=incident_type,
                severity=severity,
                status=IncidentStatus.OPEN,
                detected_at=detected_at,
                updated_at=detected_at,
                triggers=list(triggers),
                evidence=evidence,
                detection_count=1,
            )
            self._incidents[incident.incident_id] = incident
            self._active_by_type[incident_type] = incident.incident_id
            return incident

    def update(
        self,
        incident_id: str,
        *,
        severity: IncidentSeverity,
        updated_at: datetime,
        triggers: Sequence[IncidentTrigger],
        evidence: IncidentEvidence,
    ) -> Incident:
        """Refresh an active incident with new triggers and evidence."""
        with self._lock:
            current = self._incidents[incident_id]
            updated = current.model_copy(
                update={
                    "severity": severity,
                    "updated_at": updated_at,
                    "triggers": list(triggers),
                    "evidence": evidence,
                    "detection_count": current.detection_count + 1,
                }
            )
            self._incidents[incident_id] = updated
            return updated

    def resolve(self, incident_id: str, *, resolved_at: datetime) -> Incident:
        """Mark an active incident resolved and record its duration."""
        with self._lock:
            current = self._incidents[incident_id]
            duration = max(0.0, (resolved_at - current.detected_at).total_seconds())
            resolved = current.model_copy(
                update={
                    "status": IncidentStatus.RESOLVED,
                    "resolved_at": resolved_at,
                    "updated_at": resolved_at,
                    "duration_seconds": round(duration, 3),
                }
            )
            self._incidents[incident_id] = resolved
            self._active_by_type.pop(current.type, None)
            self._evict_history()
            return resolved

    def acknowledge(self, incident_id: str, *, at: datetime) -> Incident:
        """Acknowledge an open incident. Idempotent for an acknowledged one."""
        with self._lock:
            current = self._incidents[incident_id]
            if current.status is not IncidentStatus.OPEN:
                return current
            acknowledged = current.model_copy(
                update={
                    "status": IncidentStatus.ACKNOWLEDGED,
                    "acknowledged_at": at,
                    "updated_at": at,
                }
            )
            self._incidents[incident_id] = acknowledged
            return acknowledged

    # --- queries -----------------------------------------------------------

    def get(self, incident_id: str) -> Incident | None:
        with self._lock:
            return self._incidents.get(incident_id)

    def active_incident(self, incident_type: IncidentType) -> Incident | None:
        with self._lock:
            incident_id = self._active_by_type.get(incident_type)
            return self._incidents.get(incident_id) if incident_id else None

    def active(self) -> list[Incident]:
        with self._lock:
            ids = list(self._active_by_type.values())
            return [self._incidents[incident_id] for incident_id in ids]

    def active_count(self) -> int:
        with self._lock:
            return len(self._active_by_type)

    def list(
        self,
        *,
        status: IncidentStatus | None = None,
        severity: IncidentSeverity | None = None,
        incident_type: IncidentType | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[list[Incident], int]:
        """Return incidents newest-first plus the total matching the filters."""
        with self._lock:
            records = list(self._incidents.values())

        records = [
            incident
            for incident in records
            if (status is None or incident.status is status)
            and (severity is None or incident.severity is severity)
            and (incident_type is None or incident.type is incident_type)
        ]
        # Newest first; the id breaks ties so paging is stable.
        records.sort(
            key=lambda incident: (incident.detected_at, incident.incident_id),
            reverse=True,
        )
        total = len(records)
        if limit is not None:
            records = records[offset : offset + limit]
        else:
            records = records[offset:]
        return records, total

    def counts(self) -> IncidentCounts:
        with self._lock:
            records = list(self._incidents.values())

        by_status = {status.value: 0 for status in IncidentStatus}
        by_severity = {severity.value: 0 for severity in IncidentSeverity}
        active = 0
        for incident in records:
            by_status[incident.status.value] += 1
            by_severity[incident.severity.value] += 1
            if incident.status is not IncidentStatus.RESOLVED:
                active += 1
        return IncidentCounts(
            total=len(records),
            active=active,
            by_status=by_status,
            by_severity=by_severity,
        )

    def _evict_history(self) -> None:
        """Drop the oldest resolved records once the history limit is exceeded."""
        resolved = [
            incident_id
            for incident_id, incident in self._incidents.items()
            if incident.status is IncidentStatus.RESOLVED
        ]
        excess = len(resolved) - self._history_limit
        for incident_id in resolved[:excess]:
            del self._incidents[incident_id]


class IncidentDetector:
    """Evaluates reliability rules and maintains incident records.

    The detector is deliberately synchronous in :meth:`evaluate_once` so it can
    be driven from a test or a request context. :meth:`run` wraps it in a simple
    sleep loop, started and stopped by the application lifespan; the task is
    always supervised (cancelled and awaited on shutdown), never fire-and-forget
    from a request handler.
    """

    def __init__(
        self,
        *,
        tracker: ReliabilityTracker,
        store: IncidentStore,
        metrics: Metrics | None = None,
        recovery_evaluations: int = 2,
        interval_seconds: float = 15.0,
        critical_budget_consumed: float = 1.0,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._tracker = tracker
        self._store = store
        self._metrics = metrics
        self._recovery_evaluations = max(1, int(recovery_evaluations))
        self._interval_seconds = float(interval_seconds)
        self._critical_budget_consumed = float(critical_budget_consumed)
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._healthy_streak: dict[IncidentType, int] = {}
        self._task: asyncio.Task[None] | None = None

    # --- execution ---------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Whether the supervised background loop is currently executing."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the periodic evaluator as a supervised asyncio task.

        Must be called from within a running event loop (the app lifespan does
        this). Calling it twice is a no-op while a loop is already running.
        """
        if self.is_running:
            return
        self._task = asyncio.create_task(self.run(), name="incident-detector")

    async def stop(self) -> None:
        """Cancel and await the periodic evaluator; safe to call repeatedly."""
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def run(self) -> None:
        """Sleep-loop that evaluates the reliability window every interval."""
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                self.evaluate_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive, logged not raised
                logger.exception("incident_evaluation_failed")

    # --- detection ---------------------------------------------------------

    def evaluate_once(self, *, now: datetime | None = None) -> EvaluationResult:
        """Evaluate the reliability window once and reconcile incidents."""
        moment = self._wall_clock() if now is None else now
        snapshot = self._tracker.snapshot()
        sufficient = _has_sufficient_data(snapshot)
        outcomes = self._evaluate_rules(snapshot, sufficient=sufficient)
        evidence = _build_evidence(snapshot, moment) if sufficient else None

        created: list[Incident] = []
        updated: list[Incident] = []
        resolved: list[Incident] = []

        for incident_type in _INCIDENT_TYPES:
            firing = [o for o in outcomes if o.incident_type is incident_type and o.triggered]
            active = self._store.active_incident(incident_type)

            if firing and evidence is not None:
                severity = max(
                    (outcome.severity for outcome in firing),
                    key=lambda value: _SEVERITY_RANK[value],
                )
                trigger_list = _merge_triggers(active, firing, moment)
                if active is None:
                    incident = self._store.create(
                        incident_type=incident_type,
                        severity=severity,
                        detected_at=moment,
                        triggers=trigger_list,
                        evidence=evidence,
                    )
                    created.append(incident)
                    self._on_detected(incident)
                else:
                    incident = self._store.update(
                        active.incident_id,
                        severity=severity,
                        updated_at=moment,
                        triggers=trigger_list,
                        evidence=evidence,
                    )
                    updated.append(incident)
                self._healthy_streak[incident_type] = 0
                continue

            # No rule firing for this type.
            if active is None:
                self._healthy_streak.pop(incident_type, None)
                continue
            if not sufficient:
                # No data means no evidence to justify resolving; leave it open.
                continue

            streak = self._healthy_streak.get(incident_type, 0) + 1
            self._healthy_streak[incident_type] = streak
            if streak >= self._recovery_evaluations:
                incident = self._store.resolve(active.incident_id, resolved_at=moment)
                resolved.append(incident)
                self._healthy_streak.pop(incident_type, None)
                self._on_resolved(incident)

        self._publish_active_count()
        return EvaluationResult(
            created=tuple(created),
            updated=tuple(updated),
            resolved=tuple(resolved),
        )

    def _evaluate_rules(
        self, snapshot: ReliabilityResponse, *, sufficient: bool
    ) -> list[RuleOutcome]:
        availability = snapshot.availability
        latency = snapshot.latency
        budget = snapshot.error_budget

        availability_severity = self._availability_severity(snapshot)

        return [
            RuleOutcome(
                rule=RULE_AVAILABILITY_BREACH,
                incident_type=IncidentType.AVAILABILITY,
                severity=availability_severity,
                triggered=sufficient and availability.status == STATUS_BREACHED,
            ),
            RuleOutcome(
                rule=RULE_AVAILABILITY_BUDGET_EXHAUSTED,
                incident_type=IncidentType.AVAILABILITY,
                severity=availability_severity,
                triggered=sufficient and budget.exhausted,
            ),
            RuleOutcome(
                rule=RULE_LATENCY_BREACH,
                incident_type=IncidentType.LATENCY,
                severity=IncidentSeverity.WARNING,
                triggered=sufficient and latency.status == STATUS_BREACHED,
            ),
        ]

    def _availability_severity(self, snapshot: ReliabilityResponse) -> IncidentSeverity:
        consumed = snapshot.error_budget.consumed_fraction
        if consumed is not None and consumed >= self._critical_budget_consumed:
            return IncidentSeverity.CRITICAL
        return IncidentSeverity.WARNING

    # --- side effects ------------------------------------------------------

    def _on_detected(self, incident: Incident) -> None:
        if self._metrics is not None:
            self._metrics.record_incident_detection(
                incident.type.value, incident.severity.value
            )
        logger.warning(
            "incident_detected",
            extra={
                "incident_id": incident.incident_id,
                "incident_type": incident.type.value,
                "incident_severity": incident.severity.value,
                "incident_status": incident.status.value,
                "incident_rules": [trigger.rule for trigger in incident.triggers],
            },
        )

    def _on_resolved(self, incident: Incident) -> None:
        if self._metrics is not None:
            self._metrics.record_incident_resolution(
                incident.type.value, incident.duration_seconds or 0.0
            )
        logger.warning(
            "incident_resolved",
            extra={
                "incident_id": incident.incident_id,
                "incident_type": incident.type.value,
                "incident_severity": incident.severity.value,
                "incident_duration_seconds": incident.duration_seconds,
            },
        )

    def _publish_active_count(self) -> None:
        if self._metrics is not None:
            self._metrics.set_active_incidents(self._store.active_count())


# --- module helpers ----------------------------------------------------------


def _has_sufficient_data(snapshot: ReliabilityResponse) -> bool:
    return (
        snapshot.status != STATUS_INSUFFICIENT_DATA
        and snapshot.availability.sufficient_data
        and snapshot.latency.sufficient_data
    )


def _merge_triggers(
    active: Incident | None,
    firing: Sequence[RuleOutcome],
    moment: datetime,
) -> list[IncidentTrigger]:
    """Build the trigger list for an incident, preserving first-seen times."""
    existing = {trigger.rule: trigger for trigger in active.triggers} if active else {}
    triggers = [
        IncidentTrigger(
            rule=outcome.rule,
            severity=outcome.severity,
            detected_at=(
                existing[outcome.rule].detected_at
                if outcome.rule in existing
                else moment
            ),
        )
        for outcome in firing
    ]
    triggers.sort(key=lambda trigger: trigger.rule)
    return triggers


def _build_evidence(snapshot: ReliabilityResponse, moment: datetime) -> IncidentEvidence:
    availability = snapshot.availability
    latency = snapshot.latency
    budget = snapshot.error_budget
    return IncidentEvidence(
        observed_at=moment,
        window_seconds=snapshot.evaluation_window_seconds,
        eligible_requests=availability.eligible_requests,
        failed_requests=availability.failed_requests,
        observed_availability=availability.observed,
        availability_target=availability.target,
        observed_latency=latency.observed,
        latency_target=latency.target,
        latency_threshold_ms=latency.latency_threshold_ms,
        error_budget_consumed=budget.consumed_fraction,
        error_budget_exhausted=budget.exhausted,
        summary=_summarise(snapshot),
    )


def _summarise(snapshot: ReliabilityResponse) -> str:
    availability = snapshot.availability
    latency = snapshot.latency
    budget = snapshot.error_budget
    observed_availability = (
        "n/a" if availability.observed is None else f"{availability.observed:.4f}"
    )
    observed_latency = "n/a" if latency.observed is None else f"{latency.observed:.4f}"
    consumed = "n/a" if budget.consumed_fraction is None else f"{budget.consumed_fraction:.2f}x"
    return (
        f"availability {observed_availability} vs target {availability.target:.4f} "
        f"(budget consumed {consumed}, exhausted={budget.exhausted}); "
        f"latency {observed_latency} vs target {latency.target:.4f} at "
        f"{latency.latency_threshold_ms:.0f}ms over {snapshot.evaluation_window_seconds}s"
    )
