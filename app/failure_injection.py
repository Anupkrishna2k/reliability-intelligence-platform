"""Controlled failure injection for local development and demos.

The injector holds a single active :class:`FailureScenario` per application
instance: an optional artificial delay and an optional HTTP 500 probability.
The scenario is controlled at runtime through the control API in
``app/routers/failure.py`` and applied by ``FailureInjectionMiddleware``.

Safety
------
The whole feature is gated behind ``Settings.failure_injection_enabled``
(default ``False``), and ``Settings`` refuses to enable it in a production
environment. When the gate is closed the control routes are never mounted and
the middleware passes every request straight through, so the service behaves
exactly as it did before failure injection existed.

This module deliberately defines **no new metrics**. Injected delays and 500s
flow through the existing HTTP request counter, duration histogram and access
log, so a demo exercises the real instrumentation rather than a parallel one.
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass

from app.config import (
    MAX_FAILURE_LATENCY_MS,
    MAX_FAILURE_RATE_PERCENT,
    Settings,
)


@dataclass(frozen=True)
class FailureScenario:
    """One immutable view of the failure settings."""

    enabled: bool = False
    latency_ms: int = 0
    failure_rate_percent: float = 0.0

    @property
    def has_latency(self) -> bool:
        return self.enabled and self.latency_ms > 0

    @property
    def has_failure_rate(self) -> bool:
        return self.enabled and self.failure_rate_percent > 0


class FailureInjector:
    """Thread-safe holder for the active failure scenario.

    A lock is used because the control endpoint can write while worker threads
    read the scenario for in-flight requests.
    """

    def __init__(self, settings: Settings) -> None:
        self._feature_enabled = settings.failure_injection_enabled
        self._lock = threading.Lock()
        self._scenario = FailureScenario()

    @property
    def feature_enabled(self) -> bool:
        """Whether failure injection is allowed at all for this instance."""
        return self._feature_enabled

    def snapshot(self) -> FailureScenario:
        """Return the current scenario."""
        with self._lock:
            return self._scenario

    def configure(
        self, *, enabled: bool, latency_ms: int, failure_rate_percent: float
    ) -> FailureScenario:
        """Validate and store a new scenario, returning it."""
        self._validate(latency_ms, failure_rate_percent)
        scenario = FailureScenario(
            enabled=enabled,
            latency_ms=int(latency_ms),
            failure_rate_percent=float(failure_rate_percent),
        )
        with self._lock:
            self._scenario = scenario
        return scenario

    def reset(self) -> FailureScenario:
        """Disable the scenario and return the service to normal behaviour."""
        return self.configure(enabled=False, latency_ms=0, failure_rate_percent=0.0)

    def delay_seconds(self) -> float:
        """Artificial delay to apply to the current request, in seconds."""
        scenario = self.snapshot()
        return scenario.latency_ms / 1000.0 if scenario.has_latency else 0.0

    def should_fail(self) -> bool:
        """Decide whether the current request should be answered with HTTP 500."""
        scenario = self.snapshot()
        if not scenario.has_failure_rate:
            return False
        if scenario.failure_rate_percent >= MAX_FAILURE_RATE_PERCENT:
            return True
        return random.random() * MAX_FAILURE_RATE_PERCENT < scenario.failure_rate_percent

    @staticmethod
    def _validate(latency_ms: int, failure_rate_percent: float) -> None:
        """Enforce the same limits as the API schema, as defence in depth."""
        if not 0 <= latency_ms <= MAX_FAILURE_LATENCY_MS:
            raise ValueError(
                f"latency_ms must be between 0 and {MAX_FAILURE_LATENCY_MS}, "
                f"got {latency_ms!r}"
            )
        if not 0 <= failure_rate_percent <= MAX_FAILURE_RATE_PERCENT:
            raise ValueError(
                "failure_rate_percent must be between 0 and "
                f"{MAX_FAILURE_RATE_PERCENT}, got {failure_rate_percent!r}"
            )
