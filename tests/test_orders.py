"""Order listing and detail endpoint behaviour."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import build_settings

ORDERS_PATH = "/api/orders"
EXISTING_ORDER_ID = "ORD-2026-0001"
MISSING_ORDER_ID = "ORD-9999-9999"


def test_list_orders_returns_200(client: TestClient) -> None:
    assert client.get(ORDERS_PATH).status_code == 200


def test_list_orders_is_newest_first(client: TestClient) -> None:
    orders = client.get(ORDERS_PATH).json()["orders"]

    assert [order["order_id"] for order in orders] == sorted(
        (order["order_id"] for order in orders), reverse=True
    )


def test_list_orders_returns_summary_fields(client: TestClient) -> None:
    order = client.get(ORDERS_PATH).json()["orders"][0]

    assert set(order) == {
        "order_id",
        "customer_id",
        "customer_name",
        "status",
        "item_count",
        "total",
        "currency",
        "created_at",
    }


def test_list_orders_reports_pagination(client: TestClient) -> None:
    body = client.get(ORDERS_PATH, params={"limit": 2}).json()

    assert len(body["orders"]) == 2
    assert body["pagination"] == {
        "total": 6,
        "limit": 2,
        "offset": 0,
        "returned": 2,
        "has_more": True,
    }


def test_list_orders_pagination_walks_the_whole_collection(client: TestClient) -> None:
    seen: list[str] = []
    offset = 0

    while True:
        body = client.get(ORDERS_PATH, params={"limit": 2, "offset": offset}).json()
        seen.extend(order["order_id"] for order in body["orders"])
        if not body["pagination"]["has_more"]:
            break
        offset += body["pagination"]["limit"]

    assert len(seen) == body["pagination"]["total"]
    assert len(set(seen)) == len(seen)


def test_list_orders_last_page_has_no_more(client: TestClient) -> None:
    body = client.get(ORDERS_PATH, params={"offset": 5, "limit": 5}).json()

    assert body["pagination"]["has_more"] is False
    assert body["pagination"]["returned"] == 1


def test_list_orders_offset_past_end_returns_empty_page(client: TestClient) -> None:
    body = client.get(ORDERS_PATH, params={"offset": 500}).json()

    assert body["orders"] == []
    assert body["pagination"]["returned"] == 0
    assert body["pagination"]["has_more"] is False


def test_list_orders_filters_by_status(client: TestClient) -> None:
    body = client.get(ORDERS_PATH, params={"status": "shipped"}).json()

    assert body["filters"]["status"] == "shipped"
    assert body["pagination"]["total"] == 1
    assert body["orders"][0]["order_id"] == "ORD-2026-0002"
    assert body["orders"][0]["status"] == "shipped"


def test_list_orders_filters_by_customer_id(client: TestClient) -> None:
    body = client.get(ORDERS_PATH, params={"customer_id": "CUS-1042"}).json()

    assert body["pagination"]["total"] == 2
    assert {order["customer_id"] for order in body["orders"]} == {"CUS-1042"}


def test_list_orders_combines_filters(client: TestClient) -> None:
    body = client.get(
        ORDERS_PATH, params={"customer_id": "CUS-1042", "status": "delivered"}
    ).json()

    assert body["pagination"]["total"] == 1
    assert body["orders"][0]["order_id"] == EXISTING_ORDER_ID


def test_list_orders_unknown_filter_returns_empty_result(client: TestClient) -> None:
    body = client.get(ORDERS_PATH, params={"customer_id": "CUS-0000"}).json()

    assert body["orders"] == []
    assert body["pagination"]["total"] == 0


@pytest.mark.parametrize("status", ["SHIPPED", "not-a-status"])
def test_list_orders_rejects_invalid_status(client: TestClient, status: str) -> None:
    response = client.get(ORDERS_PATH, params={"status": status})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


@pytest.mark.parametrize("limit", [0, -1, "many"])
def test_list_orders_rejects_invalid_limit(client: TestClient, limit: object) -> None:
    assert client.get(ORDERS_PATH, params={"limit": limit}).status_code == 422


def test_list_orders_rejects_negative_offset(client: TestClient) -> None:
    assert client.get(ORDERS_PATH, params={"offset": -5}).status_code == 422


def test_list_orders_clamps_limit_to_configured_maximum() -> None:
    settings = build_settings(environment="test", log_level="WARNING", max_page_size=2)

    with TestClient(create_app(settings)) as client:
        body = client.get(ORDERS_PATH, params={"limit": 100}).json()

    assert body["pagination"]["limit"] == 2
    assert body["pagination"]["returned"] == 2


def test_list_orders_uses_configured_default_page_size() -> None:
    settings = build_settings(environment="test", log_level="WARNING", default_page_size=3)

    with TestClient(create_app(settings)) as client:
        body = client.get(ORDERS_PATH).json()

    assert body["pagination"]["limit"] == 3
    assert body["pagination"]["returned"] == 3


def test_list_orders_honours_custom_api_prefix() -> None:
    settings = build_settings(environment="test", log_level="WARNING", api_prefix="/v2")

    with TestClient(create_app(settings)) as client:
        assert client.get("/v2/orders").status_code == 200
        assert client.get("/api/orders").status_code == 404


def test_get_order_returns_full_detail(client: TestClient) -> None:
    response = client.get(f"{ORDERS_PATH}/{EXISTING_ORDER_ID}")

    assert response.status_code == 200
    order = response.json()
    assert order["order_id"] == EXISTING_ORDER_ID
    assert order["customer_email"] == "dana.whitfield@example.com"
    assert order["shipping_address"]["city"] == "Portland"
    assert order["tracking_number"] == "1Z999AA10123456784"
    assert len(order["items"]) == 2


def test_get_order_calculates_money_totals(client: TestClient) -> None:
    order = client.get(f"{ORDERS_PATH}/{EXISTING_ORDER_ID}").json()

    assert order["item_count"] == 3
    assert order["subtotal"] == 154.99
    assert order["shipping_cost"] == 0.0  # free over $100
    assert order["tax"] == 12.79
    assert order["total"] == 167.78
    assert order["total"] == order["subtotal"] + order["shipping_cost"] + order["tax"]


def test_get_order_includes_full_shipping_address(client: TestClient) -> None:
    order = client.get(f"{ORDERS_PATH}/ORD-2026-0002").json()

    assert order["shipping_address"] == {
        "line1": "17 Harborview Ter",
        "line2": "Apt 412",
        "city": "Seattle",
        "state": "WA",
        "postal_code": "98109",
        "country": "US",
    }


def test_get_order_line_totals_are_consistent(client: TestClient) -> None:
    order = client.get(f"{ORDERS_PATH}/{EXISTING_ORDER_ID}").json()

    for item in order["items"]:
        assert item["line_total"] == round(item["quantity"] * item["unit_price"], 2)
    assert order["subtotal"] == round(
        sum(item["line_total"] for item in order["items"]), 2
    )


def test_get_order_cancelled_order_is_still_retrievable(client: TestClient) -> None:
    order = client.get(f"{ORDERS_PATH}/ORD-2026-0005").json()

    assert order["status"] == "cancelled"
    assert order["notes"]


def test_get_unknown_order_returns_404(client: TestClient) -> None:
    response = client.get(f"{ORDERS_PATH}/{MISSING_ORDER_ID}")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "order_not_found"
    assert body["error"]["details"] == {"order_id": MISSING_ORDER_ID}
    assert body["request_id"]


def test_get_order_is_case_sensitive(client: TestClient) -> None:
    assert client.get(f"{ORDERS_PATH}/{EXISTING_ORDER_ID.lower()}").status_code == 404


def test_orders_are_exposed_in_openapi(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert ORDERS_PATH in paths
    assert f"{ORDERS_PATH}/{{order_id}}" in paths


def test_every_seeded_order_is_retrievable(client: TestClient) -> None:
    listed = client.get(ORDERS_PATH, params={"limit": 100}).json()["orders"]

    for summary in listed:
        response = client.get(f"{ORDERS_PATH}/{summary['order_id']}")
        assert response.status_code == 200
        assert response.json()["total"] == summary["total"]
