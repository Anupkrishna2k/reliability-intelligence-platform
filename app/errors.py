"""Application error types and the shared error envelope."""

from __future__ import annotations

from typing import Any

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging_config import get_logger, get_request_id

logger = get_logger("app.error")


class OrderNotFoundError(Exception):
    """Raised when an order id does not exist in the store."""

    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        super().__init__(f"order {order_id!r} was not found")


def _envelope(
    code: str,
    message: str,
    request_id: str | None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"error": error, "request_id": request_id}


async def order_not_found_handler(
    request: Request, exc: OrderNotFoundError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content=_envelope(
            "order_not_found",
            f"Order '{exc.order_id}' does not exist.",
            get_request_id(),
            {"order_id": exc.order_id},
        ),
    )


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        content = _envelope(
            str(exc.detail["code"]),
            str(exc.detail.get("message", "")),
            get_request_id(),
            exc.detail.get("details"),
        )
    else:
        content = _envelope("http_error", str(exc.detail), get_request_id())

    if exc.status_code >= 500:
        logger.error(
            "http_exception",
            extra={"http_status": exc.status_code, "path": request.url.path},
        )

    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers=getattr(exc, "headers", None),
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=_envelope(
            "validation_error",
            "Request validation failed.",
            get_request_id(),
            {"errors": _sanitise_errors(exc.errors())},
        ),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "unhandled_exception",
        extra={"http_path": request.url.path, "http_method": request.method},
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_envelope(
            "internal_error",
            "An unexpected error occurred.",
            get_request_id(),
        ),
    )


def _sanitise_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip non-serialisable context (e.g. the raw input value) from errors."""
    return [
        {
            "location": list(error.get("loc", [])),
            "message": str(error.get("msg", "")),
            "type": str(error.get("type", "")),
        }
        for error in errors
    ]
