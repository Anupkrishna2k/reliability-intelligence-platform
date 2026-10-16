"""Environment-based application configuration.

Values are read from real environment variables first, then from a local
``.env`` file, then from the defaults declared below.

Every variable is namespaced with the ``ORDERS_API_`` prefix, e.g.::

    ORDERS_API_PORT=8080
    ORDERS_API_LOG_LEVEL=DEBUG
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

VALID_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
VALID_LOG_FORMATS = {"json", "text"}

#: Upper bounds enforced on failure-injection controls. They keep a demo from
#: accidentally pinning every worker on an absurd delay or a value that is
#: meaningless as a probability.
MAX_FAILURE_LATENCY_MS = 10_000
MAX_FAILURE_RATE_PERCENT = 100.0

#: Upper bound on the latency SLO threshold. A minute is far beyond any healthy
#: read-only JSON API and keeps a typo from silently disabling the latency SLI.
MAX_SLO_LATENCY_THRESHOLD_MS = 60_000.0
#: Longest supported observation window (24 hours). The local stack uses the
#: five-minute default; the cap just stops an accidental huge retention buffer.
MAX_SLO_WINDOW_SECONDS = 86_400

#: Incident detection bounds. The interval is how often the detector evaluates
#: the reliability window; too small a value would spend CPU re-evaluating an
#: unchanged window, so it is capped at one hour. The recovery threshold is the
#: number of consecutive healthy evaluations required before an incident is
#: closed, and the history limit bounds the number of resolved records retained
#: in memory.
MAX_INCIDENT_EVAL_INTERVAL_SECONDS = 3_600.0
MAX_INCIDENT_RECOVERY_EVALUATIONS = 100
MAX_INCIDENT_HISTORY = 100_000
#: Upper bound on the budget-exhaustion multiple at which an availability
#: incident is treated as `critical` rather than `warning`.
MAX_INCIDENT_CRITICAL_BUDGET_CONSUMED = 1_000.0


class Settings(BaseSettings):
    """Runtime configuration for the Orders API."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="ORDERS_API_",
        case_sensitive=False,
        extra="ignore",
    )

    # Service identity
    app_name: str = "orders-api"
    app_version: str = "1.0.0"
    environment: str = "development"
    debug: bool = False

    # HTTP server binding
    host: str = "0.0.0.0"
    port: int = 8000

    # Observability
    log_level: str = "INFO"
    log_format: str = "json"

    # Metrics
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"
    # Probes and scrapes are excluded from the general request metrics by
    # default: they are called on a fixed timer regardless of user traffic and
    # would otherwise dominate request counts and latency percentiles.
    metrics_include_probes: bool = False

    # Failure injection (local development / demo only). Disabled by default,
    # and refused outright in a production environment: the control API and the
    # injected latency/500s must never be reachable on a real deployment.
    failure_injection_enabled: bool = False
    failure_injection_path: str = "/admin/failure"

    # Service-level objectives (SLOs) and the rolling reliability window. These
    # drive the evaluation returned by ``reliability_path`` and the
    # ``orders_api_slo_*`` / ``orders_api_error_budget_*`` gauges. Fractions are
    # ratios in (0, 1); the defaults are a deliberately modest 99.5%
    # availability and 95% latency compliance over the last five minutes.
    slo_availability_target: float = 0.995
    slo_latency_target: float = 0.95
    slo_latency_threshold_ms: float = 500.0
    slo_window_seconds: int = 300
    # An SLO that is still met is flagged ``at_risk`` once less than this
    # fraction of its allowed error budget remains (0.25 = 75% consumed).
    slo_at_risk_budget_remaining: float = 0.25
    reliability_path: str = "/api/reliability"

    # Incident detection and lifecycle. The detector evaluates the reliability
    # window on a timer and opens or closes in-memory incident records from the
    # SLO rules. All incident state is process-local and lost on restart. See
    # the README for the detection rules, severity policy and recovery rule.
    incident_detection_enabled: bool = True
    incident_evaluation_interval_seconds: float = 15.0
    # Consecutive healthy evaluations required before a breached incident is
    # resolved. A small debounce prevents a single noisy window from flapping
    # an incident closed and open again.
    incident_recovery_evaluations: int = 2
    # Availability incidents at or above this consumed-error-budget multiple are
    # `critical`; below it they are `warning`. Default 1.0 means any breached
    # availability SLO (whose budget is exhausted by construction) is critical.
    incident_critical_budget_consumed: float = 1.0
    # Maximum number of resolved incident records retained in memory.
    incident_history_limit: int = 500
    incidents_path: str = "/api/incidents"

    # API behaviour
    api_prefix: str = "/api"
    default_page_size: int = 20
    max_page_size: int = 100

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in VALID_LOG_LEVELS:
            raise ValueError(
                f"log_level must be one of {sorted(VALID_LOG_LEVELS)}, got {value!r}"
            )
        return level

    @field_validator("log_format")
    @classmethod
    def _validate_log_format(cls, value: str) -> str:
        log_format = value.lower()
        if log_format not in VALID_LOG_FORMATS:
            raise ValueError(
                f"log_format must be one of {sorted(VALID_LOG_FORMATS)}, got {value!r}"
            )
        return log_format

    @field_validator("port")
    @classmethod
    def _validate_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError(f"port must be between 1 and 65535, got {value}")
        return value

    @field_validator("metrics_path")
    @classmethod
    def _validate_metrics_path(cls, value: str) -> str:
        path = value.strip()
        if not path.startswith("/"):
            raise ValueError(f"metrics_path must start with '/', got {value!r}")
        return path

    @field_validator("failure_injection_path", "reliability_path", "incidents_path")
    @classmethod
    def _validate_admin_path(cls, value: str, info: ValidationInfo) -> str:
        path = value.strip()
        if not path.startswith("/"):
            raise ValueError(f"{info.field_name} must start with '/', got {value!r}")
        if path == "/":
            raise ValueError(f"{info.field_name} must not be the site root")
        return path

    @field_validator("slo_availability_target", "slo_latency_target")
    @classmethod
    def _validate_slo_target(cls, value: float, info: ValidationInfo) -> float:
        # A target of 1.0 would allow no errors and make the error budget
        # meaningless (a single failure exhausts it), so it is rejected.
        if not 0.0 < value < 1.0:
            raise ValueError(
                f"{info.field_name} must be strictly between 0 and 1, got {value!r}"
            )
        return value

    @field_validator("slo_at_risk_budget_remaining")
    @classmethod
    def _validate_at_risk_remaining(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError(
                "slo_at_risk_budget_remaining must be between 0 and 1, "
                f"got {value!r}"
            )
        return value

    @field_validator("slo_latency_threshold_ms")
    @classmethod
    def _validate_latency_threshold(cls, value: float) -> float:
        if not 0.0 < value <= MAX_SLO_LATENCY_THRESHOLD_MS:
            raise ValueError(
                "slo_latency_threshold_ms must be greater than 0 and at most "
                f"{MAX_SLO_LATENCY_THRESHOLD_MS}, got {value!r}"
            )
        return value

    @field_validator("slo_window_seconds")
    @classmethod
    def _validate_window(cls, value: int) -> int:
        if not 1 <= value <= MAX_SLO_WINDOW_SECONDS:
            raise ValueError(
                "slo_window_seconds must be between 1 and "
                f"{MAX_SLO_WINDOW_SECONDS}, got {value!r}"
            )
        return value

    @field_validator("incident_evaluation_interval_seconds")
    @classmethod
    def _validate_incident_interval(cls, value: float) -> float:
        if not 0.0 < value <= MAX_INCIDENT_EVAL_INTERVAL_SECONDS:
            raise ValueError(
                "incident_evaluation_interval_seconds must be greater than 0 and "
                f"at most {MAX_INCIDENT_EVAL_INTERVAL_SECONDS}, got {value!r}"
            )
        return value

    @field_validator("incident_recovery_evaluations")
    @classmethod
    def _validate_incident_recovery(cls, value: int) -> int:
        if not 1 <= value <= MAX_INCIDENT_RECOVERY_EVALUATIONS:
            raise ValueError(
                "incident_recovery_evaluations must be between 1 and "
                f"{MAX_INCIDENT_RECOVERY_EVALUATIONS}, got {value!r}"
            )
        return value

    @field_validator("incident_critical_budget_consumed")
    @classmethod
    def _validate_incident_critical_budget(cls, value: float) -> float:
        # A threshold below 1.0 would make even an un-exhausted budget critical,
        # which contradicts the documented "exhausted => critical" rule.
        if not 1.0 <= value <= MAX_INCIDENT_CRITICAL_BUDGET_CONSUMED:
            raise ValueError(
                "incident_critical_budget_consumed must be between 1.0 and "
                f"{MAX_INCIDENT_CRITICAL_BUDGET_CONSUMED}, got {value!r}"
            )
        return value

    @field_validator("incident_history_limit")
    @classmethod
    def _validate_incident_history(cls, value: int) -> int:
        if not 1 <= value <= MAX_INCIDENT_HISTORY:
            raise ValueError(
                "incident_history_limit must be between 1 and "
                f"{MAX_INCIDENT_HISTORY}, got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _guard_failure_injection(self) -> "Settings":
        """Refuse to enable failure injection in a production environment.

        This is the hard safety guard behind the default-off feature flag: even
        an explicit ``ORDERS_API_FAILURE_INJECTION_ENABLED=true`` cannot expose
        the control plane on a production deployment.
        """
        if self.failure_injection_enabled and self.is_production:
            raise ValueError(
                "failure_injection_enabled must not be set in a production "
                f"environment (environment={self.environment!r})"
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in {"prod", "production"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
