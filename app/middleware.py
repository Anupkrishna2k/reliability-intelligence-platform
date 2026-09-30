"""Inbound request correlation and access logging (pure ASGI middleware)."""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, MutableMapping

from app.logging_config import (
    get_logger,
    new_request_id,
    reset_request_id,
    set_request_id,
)

REQUEST_ID_HEADER = "x-request-id"

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class RequestContextMiddleware:
    """Assign a correlation id to every request and log the response.

    Implemented as raw ASGI middleware so the correlation id is bound in the
    same task as the endpoint, which keeps it visible to both the access log
    and anything logged inside the request handler.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app
        self.logger = get_logger("app.access")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._incoming_request_id(scope) or new_request_id()

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
            duration_ms = round((time.perf_counter() - started_at) * 1000, 3)
            self.logger.info(
                "http_request",
                extra={
                    "http_method": scope.get("method", "-"),
                    "http_path": scope.get("path", "-"),
                    "http_query": (scope.get("query_string") or b"").decode("utf-8", "replace"),
                    "http_status": status_code,
                    "duration_ms": duration_ms,
                    "client_ip": self._client_ip(scope),
                },
            )
            reset_request_id(token)

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
