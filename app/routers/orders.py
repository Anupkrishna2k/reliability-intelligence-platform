"""Order query endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, status

from app.config import Settings
from app.errors import OrderNotFoundError
from app.logging_config import get_logger
from app.models import (
    Order,
    OrderFilters,
    OrderListResponse,
    OrderStatus,
    OrderSummary,
    Pagination,
)
from app.repository import OrderRepository

logger = get_logger("app.orders")

router = APIRouter(prefix="/orders", tags=["orders"])


def get_repository(request: Request) -> OrderRepository:
    """Resolve the order store attached to the running app."""
    return request.app.state.repository


def get_app_settings(request: Request) -> Settings:
    """Resolve the settings attached to the running app."""
    return request.app.state.settings


@router.get(
    "",
    response_model=OrderListResponse,
    summary="List orders",
    description=(
        "Returns orders newest first, optionally filtered by status or "
        "customer. Results are paginated."
    ),
)
async def list_orders(
    repository: OrderRepository = Depends(get_repository),
    settings: Settings = Depends(get_app_settings),
    status_filter: OrderStatus | None = Query(
        default=None,
        alias="status",
        description="Only return orders in this state.",
    ),
    customer_id: str | None = Query(
        default=None,
        description="Only return orders for this customer id.",
    ),
    limit: int | None = Query(
        default=None,
        ge=1,
        description="Page size. Defaults to the configured page size.",
    ),
    offset: int = Query(default=0, ge=0, description="Number of orders to skip."),
) -> OrderListResponse:
    page_size = settings.default_page_size if limit is None else min(limit, settings.max_page_size)

    orders, total = repository.list_orders(
        status=status_filter,
        customer_id=customer_id,
        limit=page_size,
        offset=offset,
    )

    logger.info(
        "orders_listed",
        extra={
            "orders_returned": len(orders),
            "orders_matched": total,
            "filter_status": status_filter.value if status_filter else None,
            "filter_customer_id": customer_id,
            "page_limit": page_size,
            "page_offset": offset,
        },
    )

    return OrderListResponse(
        orders=[OrderSummary(**repository.to_summary(order)) for order in orders],
        pagination=Pagination(
            total=total,
            limit=page_size,
            offset=offset,
            returned=len(orders),
            has_more=offset + len(orders) < total,
        ),
        filters=OrderFilters(status=status_filter, customer_id=customer_id),
    )


@router.get(
    "/{order_id}",
    response_model=Order,
    summary="Get an order",
    description="Returns the full order, including line items and money totals.",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "No order exists with that id.",
        }
    },
)
async def get_order(order_id: str, repository: OrderRepository = Depends(get_repository)) -> Order:
    order = repository.get_order(order_id)
    if order is None:
        logger.warning("order_not_found", extra={"order_id": order_id})
        raise OrderNotFoundError(order_id)

    logger.info("order_fetched", extra={"order_id": order.order_id, "order_status": order.status.value})
    return order
