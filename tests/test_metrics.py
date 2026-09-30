"""Prometheus instrumentation behaviour.

Metrics are asserted by scraping ``/metrics`` and parsing the exposition text,
so these tests verify exactly what a Prometheus server would collect.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST
from prometheus_client.parser import text_string_to_metric_families

from app.config import Settings
from app.main import create_app
from tests.conftest import build_settings

METRICS_PATH = "/metrics"
ORDERS_PATH = "/api/orders"
DETAIL_PATH = f"{ORDERS_PATH}/ORD-2026-0001"
MISSING_PATH = f"{ORDERS_PATH}/ORD-9999-9999"

Labels = tuple[tuple[str, str], ...]
Samples = dict[tuple[str, Labels], float]

#: Label names the service is allowed to expose. Identifiers such as order_id,
#: customer_id and request_id are deliberately absent.
ALLOWED_LABEL_NAMES = {
    "method",
    "route",
    "status_code",
    "outcome",
    "status_filter",
    "version",
    "environment",
    "le",  # histogram bucket boundary
}

#: Metric family names. Prometheus strips the `_total` suffix from a counter's
#: family name (it reappears on the samples), so `..._requests_total` is
#: defined here as `..._requests`.
EXPECTED_METRIC_FAMILIES = {
    "orders_api_http_requests",
    "orders_api_http_request_duration_seconds",
    "orders_api_order_lookups",
    "orders_api_order_lookup_duration_seconds",
    "orders_api_orders_listed",
    "orders_api_orders_returned",
    "orders_api_orders_matched",
    "orders_api_ready",
    "orders_api_build_info",
}

#: Sample names that must be present in the exposition output.
EXPECTED_SAMPLE_NAMES = {
    "orders_api_http_requests_total",
    "orders_api_http_request_duration_seconds_count",
    "orders_api_order_lookups_total",
    "orders_api_order_lookup_duration_seconds_count",
    "orders_api_ready",
    "orders_api_build_info",
}


def scrape(client: TestClient, path: str = METRICS_PATH) -> Samples:
    """Fetch and parse the Prometheus exposition format."""
    response = client.get(path)
    assert response.status_code == 200
    return {
        (sample.name, tuple(sorted(sample.labels.items()))): sample.value
        for family in text_string_to_metric_families(response.text)
        for sample in family.samples
    }


def families(client: TestClient, path: str = METRICS_PATH) -> set[str]:
    """Names of the metric families currently exposed."""
    response = client.get(path)
    assert response.status_code == 200
    return {family.name for family in text_string_to_metric_families(response.text)}


def value(samples: Samples, name: str, **labels: str) -> float:
    """Read one sample, failing loudly if it is not exposed."""
    key = (name, tuple(sorted(labels.items())))
    assert key in samples, f"{name}{dict(labels)} is not exposed"
    return samples[key]


def value_or_zero(samples: Samples, name: str, **labels: str) -> float:
    """Read one sample, treating an absent series as zero.

    Prometheus only exposes a label combination once it has been observed, so
    "not present" and "never happened" are the same thing.
    """
    return samples.get((name, tuple(sorted(labels.items()))), 0.0)


def series_for(samples: Samples, name: str) -> list[Labels]:
    return [labels for sample_name, labels in samples if sample_name == name]


def series_routes(samples: Samples, name: str) -> set[str]:
    return {
        dict(labels)["route"] for labels in series_for(samples, name) if "route" in dict(labels)
    }


@pytest.fixture
def metrics_client() -> Iterator[TestClient]:
    """A client on a dedicated settings object for these tests."""
    settings = build_settings(environment="test", log_level="WARNING")
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def app_client(**overrides: object) -> TestClient:
    settings = build_settings(environment="test", log_level="WARNING", **overrides)
    return TestClient(create_app(settings))


# --- endpoint ----------------------------------------------------------------


def test_metrics_endpoint_returns_200(metrics_client: TestClient) -> None:
    assert metrics_client.get(METRICS_PATH).status_code == 200


def test_metrics_endpoint_uses_prometheus_content_type(metrics_client: TestClient) -> None:
    content_type = metrics_client.get(METRICS_PATH).headers["content-type"]

    assert content_type == CONTENT_TYPE_LATEST


def test_metrics_endpoint_exposes_expected_metrics(metrics_client: TestClient) -> None:
    assert EXPECTED_METRIC_FAMILIES <= families(metrics_client)


def test_metrics_endpoint_exposes_expected_sample_names(metrics_client: TestClient) -> None:
    metrics_client.get(DETAIL_PATH)
    metrics_client.get(ORDERS_PATH)

    exposed = {name for name, _ in scrape(metrics_client)}

    assert EXPECTED_SAMPLE_NAMES <= exposed


def test_metrics_endpoint_is_hidden_from_openapi(metrics_client: TestClient) -> None:
    assert METRICS_PATH not in metrics_client.get("/openapi.json").json()["paths"]


def test_build_info_publishes_version_and_environment(metrics_client: TestClient) -> None:
    info = value(
        scrape(metrics_client), "orders_api_build_info", version="1.0.0", environment="test"
    )

    assert info == 1.0


# --- HTTP request metrics ----------------------------------------------------


def test_api_request_increments_request_counter(metrics_client: TestClient) -> None:
    before = value_or_zero(
        scrape(metrics_client),
        "orders_api_http_requests_total",
        method="GET",
        route=ORDERS_PATH,
        status_code="200",
    )

    metrics_client.get(ORDERS_PATH)

    after = value(
        scrape(metrics_client),
        "orders_api_http_requests_total",
        method="GET",
        route=ORDERS_PATH,
        status_code="200",
    )
    assert after == before + 1


def test_request_counter_records_status_code(metrics_client: TestClient) -> None:
    metrics_client.get(ORDERS_PATH)
    metrics_client.get(MISSING_PATH)
    samples = scrape(metrics_client)

    assert value(
        samples, "orders_api_http_requests_total", method="GET", route=ORDERS_PATH, status_code="200"
    ) == 1.0
    assert value(
        samples,
        "orders_api_http_requests_total",
        method="GET",
        route=f"{ORDERS_PATH}/{{order_id}}",
        status_code="404",
    ) == 1.0


def test_request_duration_histogram_observes_each_request(metrics_client: TestClient) -> None:
    metrics_client.get(ORDERS_PATH)
    metrics_client.get(ORDERS_PATH)
    samples = scrape(metrics_client)

    assert value(
        samples,
        "orders_api_http_request_duration_seconds_count",
        method="GET",
        route=ORDERS_PATH,
    ) == 2.0
    assert value(
        samples, "orders_api_http_request_duration_seconds_sum", method="GET", route=ORDERS_PATH
    ) > 0


def test_histogram_exposes_buckets(metrics_client: TestClient) -> None:
    metrics_client.get(ORDERS_PATH)
    samples = scrape(metrics_client)

    assert value(
        samples,
        "orders_api_http_request_duration_seconds_bucket",
        method="GET",
        route=ORDERS_PATH,
        le="0.005",
    ) >= 0


# --- label cardinality -------------------------------------------------------


def test_path_is_recorded_as_route_template_not_raw_path(metrics_client: TestClient) -> None:
    metrics_client.get(DETAIL_PATH)
    samples = scrape(metrics_client)

    assert value(
        samples,
        "orders_api_http_requests_total",
        method="GET",
        route=f"{ORDERS_PATH}/{{order_id}}",
        status_code="200",
    ) == 1.0
    assert "ORD-2026-0001" not in {label for _, labels in samples for _, label in labels}


def test_many_distinct_order_ids_produce_a_single_series(metrics_client: TestClient) -> None:
    """The classic cardinality trap: an identifier used as a label."""
    for index in range(50):
        metrics_client.get(f"{ORDERS_PATH}/ORD-SCAN-{index}")

    matching = [
        labels
        for labels in series_for(scrape(metrics_client), "orders_api_http_requests_total")
        if dict(labels).get("route") == f"{ORDERS_PATH}/{{order_id}}"
    ]
    assert len(matching) == 1
    assert dict(matching[0])["status_code"] == "404"


def test_unmatched_paths_collapse_into_one_series(metrics_client: TestClient) -> None:
    for index in range(20):
        metrics_client.get(f"/scanner/probe/{index}")

    samples = scrape(metrics_client)
    unmatched = [
        labels
        for labels in series_for(samples, "orders_api_http_requests_total")
        if dict(labels).get("route") == "unmatched"
    ]
    assert len(unmatched) == 1
    assert dict(unmatched[0])["status_code"] == "404"


def test_exposed_labels_are_all_low_cardinality(metrics_client: TestClient) -> None:
    metrics_client.get(DETAIL_PATH)
    metrics_client.get(MISSING_PATH)
    metrics_client.get(ORDERS_PATH)
    metrics_client.get(METRICS_PATH)

    exposed = {name for _, labels in scrape(metrics_client) for name, _ in labels}

    assert exposed <= ALLOWED_LABEL_NAMES
    assert not exposed & {"order_id", "customer_id", "request_id", "timestamp", "path"}


def test_customer_id_is_not_used_as_a_label(metrics_client: TestClient) -> None:
    metrics_client.get(f"{ORDERS_PATH}?customer_id=CUS-1042")

    exposed = {value for _, labels in scrape(metrics_client) for _, value in labels}
    assert "CUS-1042" not in exposed


def test_request_id_is_not_used_as_a_label(metrics_client: TestClient) -> None:
    metrics_client.get(ORDERS_PATH, headers={"x-request-id": "unique-trace-id"})

    exposed = {value for _, labels in scrape(metrics_client) for _, value in labels}
    assert "unique-trace-id" not in exposed


# --- probe exclusion ---------------------------------------------------------


@pytest.mark.parametrize("path", ["/health", "/ready", METRICS_PATH])
def test_probes_and_scrape_are_excluded_from_request_metrics(
    metrics_client: TestClient, path: str
) -> None:
    metrics_client.get(path)

    assert path not in series_routes(scrape(metrics_client), "orders_api_http_requests_total")


def test_excluding_probes_does_not_hide_traffic_metrics(metrics_client: TestClient) -> None:
    metrics_client.get("/health")
    metrics_client.get("/ready")
    metrics_client.get(METRICS_PATH)
    metrics_client.get(ORDERS_PATH)

    assert series_routes(scrape(metrics_client), "orders_api_http_requests_total") == {ORDERS_PATH}


def test_probes_can_be_included_on_request() -> None:
    with app_client(metrics_include_probes=True) as client:
        client.get("/health")
        samples = scrape(client)

    assert value(
        samples, "orders_api_http_requests_total", method="GET", route="/health", status_code="200"
    ) == 1.0


# --- order metrics -----------------------------------------------------------


def test_successful_lookup_increments_success_counter(metrics_client: TestClient) -> None:
    before = value_or_zero(scrape(metrics_client), "orders_api_order_lookups_total", outcome="success")

    metrics_client.get(DETAIL_PATH)

    after = scrape(metrics_client)
    assert value(after, "orders_api_order_lookups_total", outcome="success") == before + 1
    assert value_or_zero(after, "orders_api_order_lookups_total", outcome="not_found") == 0.0


def test_failed_lookup_increments_not_found_counter(metrics_client: TestClient) -> None:
    response = metrics_client.get(MISSING_PATH)
    assert response.status_code == 404

    samples = scrape(metrics_client)
    assert value(samples, "orders_api_order_lookups_total", outcome="not_found") == 1.0
    assert value_or_zero(samples, "orders_api_order_lookups_total", outcome="success") == 0.0


def test_lookup_outcomes_are_counted_separately(metrics_client: TestClient) -> None:
    metrics_client.get(DETAIL_PATH)
    metrics_client.get(DETAIL_PATH)
    metrics_client.get(MISSING_PATH)
    samples = scrape(metrics_client)

    assert value(samples, "orders_api_order_lookups_total", outcome="success") == 2.0
    assert value(samples, "orders_api_order_lookups_total", outcome="not_found") == 1.0
    # Total attempts are the sum of the outcomes, so no separate counter is
    # needed to answer "how many lookups happened?".
    assert sum(
        value(samples, "orders_api_order_lookups_total", **dict(labels))
        for labels in series_for(samples, "orders_api_order_lookups_total")
    ) == 3.0


def test_lookup_duration_is_recorded(metrics_client: TestClient) -> None:
    metrics_client.get(DETAIL_PATH)
    samples = scrape(metrics_client)

    assert value(samples, "orders_api_order_lookup_duration_seconds_count") == 1.0
    assert value(samples, "orders_api_order_lookup_duration_seconds_sum") > 0


def test_list_query_is_counted_without_a_filter(metrics_client: TestClient) -> None:
    metrics_client.get(ORDERS_PATH)

    assert value(scrape(metrics_client), "orders_api_orders_listed_total", status_filter="none") == 1.0


def test_list_query_is_counted_per_status_filter(metrics_client: TestClient) -> None:
    metrics_client.get(f"{ORDERS_PATH}?status=shipped")
    metrics_client.get(f"{ORDERS_PATH}?status=delivered")
    samples = scrape(metrics_client)

    assert value(samples, "orders_api_orders_listed_total", status_filter="shipped") == 1.0
    assert value(samples, "orders_api_orders_listed_total", status_filter="delivered") == 1.0


def test_invalid_filter_does_not_reach_the_metrics(client: TestClient) -> None:
    client.get(f"{ORDERS_PATH}?status=not-a-status")

    # The family is still declared, but no series was ever created for the
    # rejected query, so nothing is counted.
    exposed = {name for name, _ in scrape(client)}
    assert "orders_api_orders_listed_total" not in exposed


def test_list_query_records_returned_and_matched_counts(metrics_client: TestClient) -> None:
    metrics_client.get(f"{ORDERS_PATH}?status=shipped")
    samples = scrape(metrics_client)

    assert value(samples, "orders_api_orders_returned_count") == 1.0
    assert value(samples, "orders_api_orders_returned_sum") == 1.0
    assert value(samples, "orders_api_orders_matched_count") == 1.0


# --- readiness ---------------------------------------------------------------


def test_readiness_gauge_reports_ready(metrics_client: TestClient) -> None:
    assert metrics_client.get("/ready").status_code == 200

    assert value(scrape(metrics_client), "orders_api_ready") == 1.0


def test_readiness_gauge_reports_not_ready(metrics_client: TestClient) -> None:
    metrics_client.app.state.repository._loaded = False

    assert metrics_client.get("/ready").status_code == 503
    assert value(scrape(metrics_client), "orders_api_ready") == 0.0


# --- toggling ----------------------------------------------------------------


def test_metrics_endpoint_absent_when_disabled() -> None:
    with app_client(metrics_enabled=False) as client:
        assert client.get(METRICS_PATH).status_code == 404


def test_nothing_is_recorded_when_disabled() -> None:
    with app_client(metrics_enabled=False) as client:
        client.get(ORDERS_PATH)
        assert client.get(METRICS_PATH).status_code == 404


def test_disabling_metrics_preserves_api_behaviour() -> None:
    with app_client(metrics_enabled=False) as client:
        assert client.get(ORDERS_PATH).status_code == 200
        assert client.get(DETAIL_PATH).status_code == 200
        assert client.get(MISSING_PATH).status_code == 404
        assert client.get("/health").status_code == 200


def test_metrics_path_is_configurable() -> None:
    with app_client(metrics_path="/internal/metrics") as client:
        assert client.get("/internal/metrics").status_code == 200
        assert client.get(METRICS_PATH).status_code == 404


def test_configured_metrics_path_is_still_excluded_from_request_metrics() -> None:
    custom_path = "/internal/metrics"
    with app_client(metrics_path=custom_path) as client:
        client.get(custom_path)
        samples = scrape(client, custom_path)

    assert custom_path not in series_routes(samples, "orders_api_http_requests_total")


def test_invalid_metrics_path_is_rejected() -> None:
    with pytest.raises(Exception):
        Settings(_env_file=None, metrics_path="metrics")


# --- registry isolation ------------------------------------------------------


def test_each_app_has_its_own_registry() -> None:
    with app_client() as first, app_client() as second:
        first.get(ORDERS_PATH)
        second.get(DETAIL_PATH)

        assert value(
            scrape(first),
            "orders_api_http_requests_total",
            method="GET",
            route=ORDERS_PATH,
            status_code="200",
        ) == 1.0
        assert (
            value_or_zero(
                scrape(first),
                "orders_api_http_requests_total",
                method="GET",
                route=f"{ORDERS_PATH}/{{order_id}}",
                status_code="200",
            )
            == 0.0
        )
        assert value(
            scrape(second),
            "orders_api_http_requests_total",
            method="GET",
            route=f"{ORDERS_PATH}/{{order_id}}",
            status_code="200",
        ) == 1.0


# --- behaviour preservation --------------------------------------------------


def test_existing_json_formats_are_unchanged(metrics_client: TestClient) -> None:
    order = metrics_client.get(DETAIL_PATH).json()
    assert order["order_id"] == "ORD-2026-0001"
    assert order["total"] == 167.78

    listing = metrics_client.get(ORDERS_PATH).json()
    assert set(listing) == {"orders", "pagination", "filters"}

    error = metrics_client.get(MISSING_PATH).json()
    assert error["error"]["code"] == "order_not_found"


def test_request_id_header_still_returned(metrics_client: TestClient) -> None:
    response = metrics_client.get(ORDERS_PATH, headers={"x-request-id": "trace-42"})

    assert response.headers["x-request-id"] == "trace-42"
