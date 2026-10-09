"""Inbound request correlation, access logging and HTTP metrics.

All three happen here, in one pass over the response, so a request is not
walked twice just to observe it. Implemented as raw ASGI middleware so the
correlation id is bound in the same task as the endpoint, which keeps it
visible to both the access log and anything logged inside the request handler.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, MutableMapping, cast

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import Settings
from app.failure_injection import FailureInjector
from app.logging_config import (
    get_logger,
    get_request_id,
    new_request_id,
    reset_request_id,
    set_request_id,
)
from app.metrics import UNMATCHED_ROUTE, Metrics, RouteTemplateResolver

REQUEST_ID_HEADER = "x-request-id"

#: Paths that are never counted as served application traffic. See
#: `Settings.metrics_include_probes` for the rationale.
PROBE_PATHS = ("/health", "/ready")

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class RequestContextMiddleware:
    """Assign a correlation id to every request, then log and measure it."""

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        settings: Settings,
        route_resolver: RouteTemplateResolver,
    ) -> None:
        self.app = app
        self.settings = settings
        self.route_resolver = route_resolver
        self.logger = get_logger("app.access")
        self._excluded = {*PROBE_PATHS, settings.metrics_path}
        if settings.failure_injection_enabled:
            # The failure-injection control plane is operational tooling, not
            # application traffic, so it is excluded just like the metrics path.
            self._excluded.add(settings.failure_injection_path)

    def _should_record(self, path: str) -> bool:
        """Decide whether this path counts as application traffic."""
        if not self.settings.metrics_enabled:
            return False
        if self.settings.metrics_include_probes:
            return True
        return path not in self._excluded

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._incoming_request_id(scope) or new_request_id()
        path = scope.get("path", "/")

        status_code = 500
        token = set_request_id(request_id)
        started_at = time.perf_counter()

        async def send_wrapper(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (REQUEST_ID_HEADER.encode(), request_id.encode()),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_seconds = time.perf_counter() - started_at
            self.logger.info(
                "http_request",
                extra={
                    "http_method": scope.get("method", "-"),
                    "http_path": path,
                    "http_query": (scope.get("query_string") or b"").decode("utf-8", "replace"),
                    "http_status": status_code,
                    "duration_ms": round(duration_seconds * 1000, 3),
                    "client_ip": self._client_ip(scope),
                },
            )
            if self._should_record(path):
                # The route template, never the concrete path: /api/orders/X-1
                # must not become its own time series.
                route = self.route_resolver.resolve(path) or UNMATCHED_ROUTE
                self._metrics(scope).record_http_request(
                    scope.get("method"), route, status_code, duration_seconds
                )
            reset_request_id(token)

    @staticmethod
    def _metrics(scope: Scope) -> Metrics:
        application: FastAPI = scope["app"]
        return cast(Metrics, application.state.metrics)

    @staticmethod
    def _incoming_request_id(scope: Scope) -> str | None:
        for key, value in scope.get("headers", []):
            if key.decode("latin-1").lower() == REQUEST_ID_HEADER:
                request_id = value.decode("latin-1").strip()
                if request_id:
                    return request_id[:128]
        return None

    @staticmethod
    def _client_ip(scope: Scope) -> str:
        client = scope.get("client")
        if not client:
            return "-"
        return str(client[0])


class FailureInjectionMiddleware:
    """Apply a configured artificial delay and/or HTTP 500 to each request.

    This sits *inside* :class:`RequestContextMiddleware`, so an injected delay
    or failure is observed by the existing access log and HTTP metrics exactly
    like a real slow request or server error would be — no separate counters.

    Probes, the metrics scrape and the control API itself are exempt, so the
    instance stays observable and the injection can always be turned back off.
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        settings: Settings,
        injector: FailureInjector,
    ) -> None:
        self.app = app
        self.injector = injector
        self.logger = get_logger("app.failure_injection")
        self._exempt = {
            *PROBE_PATHS,
            settings.metrics_path,
            settings.failure_injection_path,
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.injector.feature_enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")
        if path in self._exempt:
            await self.app(scope, receive, send)
            return

        delay_seconds = self.injector.delay_seconds()
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

        if self.injector.should_fail():
            scenario = self.injector.snapshot()
            self.logger.warning(
                "injected_failure",
                extra={
                    "http_method": scope.get("method", "-"),
                    "http_path": path,
                    "http_status": 500,
                    "failure_rate_percent": scenario.failure_rate_percent,
                },
            )
            response = JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "injected_failure",
                        "message": "Simulated internal server error "
                        "(controlled failure injection).",
                    },
                    "request_id": get_request_id(),
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
