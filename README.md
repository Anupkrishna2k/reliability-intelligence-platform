# Reliability Intelligence Platform

An SRE/DevOps portfolio project. This repository is built in stages; the goal
is to grow a small but realistic service into a fully instrumented, observable,
and safely deployed system.

**Current stage: the Orders API** — the workload that later stages will
instrument, observe, alert on, and deploy.

## What this service is

A read-only HTTP API for customer orders, written in Python with FastAPI. It
exists to be a believable production service: it separates liveness from
readiness, emits one structured JSON log line per request, exposes Prometheus
metrics, returns a consistent error envelope, and is driven entirely by
environment variables so the same build runs locally and in a container.

There is no database at this stage. Orders are served from a small in-memory
fixture loaded at start-up, behind a repository interface so a real store can
be swapped in later without touching the HTTP layer.

### Endpoints

| Method | Path                    | Purpose                                                       |
| ------ | ----------------------- | ------------------------------------------------------------- |
| `GET`  | `/health`               | Liveness. Answers "is the process up?" — no dependency checks. |
| `GET`  | `/ready`                | Readiness. Answers "can it serve traffic?" — `503` when not.   |
| `GET`  | `/api/orders`           | Paginated, filterable order list, newest first.                |
| `GET`  | `/api/orders/{order_id}`| Full order detail including line items and money totals.       |
| `GET`  | `/metrics`              | Prometheus metrics, for scraping.                             |

Interactive API docs are served at `/docs`, and the OpenAPI schema at
`/openapi.json`. `/metrics` is intentionally hidden from the docs — it exists
for Prometheus, not for API consumers.

#### Why liveness and readiness are separate

A failing **liveness** probe means the process is wedged and should be
restarted. A failing **readiness** probe means the process is fine but cannot
serve yet, and should simply be pulled out of rotation. Conflating them turns a
slow dependency into a restart loop, so `/health` deliberately touches nothing
outside the process while `/ready` runs dependency checks and returns `503`.

### Filtering and pagination

`GET /api/orders` accepts:

| Parameter     | Type                                    | Default | Notes                                    |
| ------------- | --------------------------------------- | ------- | ---------------------------------------- |
| `status`      | `pending`, `confirmed`, `processing`, `shipped`, `delivered`, `cancelled` | none | Exact, lowercase match       |
| `customer_id` | string                                  | none     | Exact match                             |
| `limit`       | int                                     | `20`    | Clamped to `ORDERS_API_MAX_PAGE_SIZE`    |
| `offset`      | int                                     | `0`     | Use with `limit` to page through results |

## Running locally

Requires Python 3.12+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env    # optional: every value already has a working default
python run.py
```

The service listens on **http://localhost:8000**.

For a live-reload development loop:

```bash
ORDERS_API_DEBUG=true python run.py
```

### Running the tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Running with Docker

```bash
docker build -t orders-api:0.1.0 .
docker run --rm -p 8000:8000 orders-api:0.1.0
```

The image is built in two stages and runs as an unprivileged user. Verify it
with:

```bash
docker run -d --name orders-api -p 8000:8000 orders-api:0.1.0
curl http://localhost:8000/health
docker logs -f orders-api
```

To override configuration at runtime, pass environment variables:

```bash
docker run --rm -p 8000:8000 \
  -e ORDERS_API_ENVIRONMENT=production \
  -e ORDERS_API_LOG_LEVEL=DEBUG \
  orders-api:0.1.0
```

## Configuration

All settings are read from environment variables, falling back to `.env` and
then to the defaults below. Every variable is prefixed with `ORDERS_API_`.

| Variable                       | Default       | Description                                            |
| ------------------------------ | ------------- | ------------------------------------------------------ |
| `ORDERS_API_APP_NAME`          | `orders-api`  | Service name reported by probes and logs.              |
| `ORDERS_API_APP_VERSION`       | `1.0.0`       | Service version.                                       |
| `ORDERS_API_ENVIRONMENT`       | `development` | Deployment environment name.                           |
| `ORDERS_API_DEBUG`             | `false`       | Debug mode and live reload outside production.         |
| `ORDERS_API_HOST`              | `0.0.0.0`     | Bind address.                                          |
| `ORDERS_API_PORT`              | `8000`        | Bind port.                                             |
| `ORDERS_API_LOG_LEVEL`         | `INFO`        | `CRITICAL`/`ERROR`/`WARNING`/`INFO`/`DEBUG`.           |
| `ORDERS_API_LOG_FORMAT`        | `json`        | `json` for machine parsing, `text` for humans.         |
| `ORDERS_API_API_PREFIX`        | `/api`        | Prefix for the orders routes.                          |
| `ORDERS_API_DEFAULT_PAGE_SIZE` | `20`          | Page size when `limit` is omitted.                     |
| `ORDERS_API_MAX_PAGE_SIZE`     | `100`         | Upper bound applied to `limit`.                        |
| `ORDERS_API_METRICS_ENABLED`   | `true`        | Set to `false` to remove `/metrics` and record nothing. |
| `ORDERS_API_METRICS_PATH`      | `/metrics`    | Scrape endpoint path.                                  |
| `ORDERS_API_METRICS_INCLUDE_PROBES` | `false`  | Count `/health` and `/ready` as application traffic.   |

Invalid values fail fast at start-up rather than at the first request.

## Example API calls

```bash
# Liveness
curl -s http://localhost:8000/health

# Readiness
curl -s http://localhost:8000/ready

# First page of orders
curl -s "http://localhost:8000/api/orders"

# Filter by status, page through results
curl -s "http://localhost:8000/api/orders?status=shipped&limit=5&offset=0"

# All orders for one customer
curl -s "http://localhost:8000/api/orders?customer_id=CUS-1042"

# Full order detail
curl -s http://localhost:8000/api/orders/ORD-2026-0001

# Unknown order -> 404 with a structured error body
curl -s http://localhost:8000/api/orders/ORD-9999-9999

# Trace a request end to end using your own correlation id
curl -s -H "x-request-id: my-trace-id" http://localhost:8000/api/orders

# Prometheus metrics
curl -s http://localhost:8000/metrics
```

### Sample responses

`GET /health`

```json
{
  "status": "ok",
  "service": "orders-api",
  "version": "1.0.0",
  "environment": "development",
  "uptime_seconds": 7.094,
  "timestamp": "2026-09-30T06:55:30.773830Z"
}
```

`GET /ready`

```json
{
  "status": "ready",
  "service": "orders-api",
  "version": "1.0.0",
  "environment": "development",
  "checks": [
    {
      "name": "order_store",
      "status": "pass",
      "detail": "6 orders available",
      "latency_ms": 0.003
    }
  ],
  "timestamp": "2026-09-30T06:55:30.781696Z"
}
```

`GET /api/orders?limit=1`

```json
{
  "orders": [
    {
      "order_id": "ORD-2026-0006",
      "customer_id": "CUS-3310",
      "customer_name": "Tomás Ferreira",
      "status": "confirmed",
      "item_count": 3,
      "total": 916.88,
      "currency": "USD",
      "created_at": "2026-09-30T04:55:23.633609Z"
    }
  ],
  "pagination": {
    "total": 6,
    "limit": 1,
    "offset": 0,
    "returned": 1,
    "has_more": true
  },
  "filters": {
    "status": null,
    "customer_id": null
  }
}
```

`GET /api/orders/ORD-9999-9999` — `404`

```json
{
  "error": {
    "code": "order_not_found",
    "message": "Order 'ORD-9999-9999' does not exist.",
    "details": { "order_id": "ORD-9999-9999" }
  },
  "request_id": "a46d84a5547740818b17770d79a6bba3"
}
```

## Logging

Every log line is a single JSON object on stdout, carrying the service name,
the environment, and the request correlation id — so a request can be followed
across every line it produced. Errors additionally return that `request_id` in
the response body.

```json
{"timestamp": "2026-09-30 12:25:33,715", "level": "INFO", "logger": "app.access", "message": "http_request", "service": "orders-api", "environment": "development", "request_id": "a46d84a5547740818b17770d79a6bba3", "http_method": "GET", "http_path": "/api/orders", "http_query": "status=shipped", "http_status": 200, "duration_ms": 0.863, "client_ip": "127.0.0.1"}
```

If a caller supplies an `x-request-id`, it is reused and echoed back in the
response headers; otherwise the service generates one.

Set `ORDERS_API_LOG_FORMAT=text` for a human-readable format during debugging.

## Metrics

The service exposes Prometheus metrics on `GET /metrics` in the standard text
exposition format, ready to be scraped:

```bash
curl -s http://localhost:8000/metrics
```

```bash
# Just the order lookup counters
curl -s http://localhost:8000/metrics | grep orders_api_order_lookups_total

# Latency percentiles for the order detail endpoint
curl -s http://localhost:8000/metrics \
  | grep orders_api_http_request_duration_seconds_bucket
```

In a container:

```bash
docker run --rm -p 8000:8000 orders-api:0.2.0
curl -s http://localhost:8000/metrics
```

### Why application metrics matter for SRE

Logs tell you what happened to one request; metrics tell you what is happening
to *everyone, right now*. That difference is the whole point of instrumenting
an application.

A log line is high-cardinality and expensive to keep: you cannot store every
request log for a year, and you cannot compute a p95 latency across a billion
rows. Metrics are pre-aggregated on the server, so a single time series
summarises every request that ever matched it. That makes them the only
practical way to answer the questions an on-call engineer actually has:

- **Is the service healthy right now?** A 5xx rate that is climbing from 0.1%
  to 4% is visible in one graph, and nowhere in the logs.
- **Is a deploy to blame?** A version label on the metric lets you compare
  before and after, which turns "it feels slow" into a decision.
- **Is it *me* or a dependency?** A latency histogram per route separates
  framework time from store time, so a slow `/api/orders` points at the data
  layer rather than the app.
- **Should we roll back?** Alerts and SLOs are written against these series, so
  instrumentation is a hard prerequisite for the alerting and dashboards in
  later milestones.

The concrete payoff: an order lookup that starts failing at 3% will not page
anyone from logs, because nobody is reading them at 3am. It will page
immediately from `orders_api_order_lookups_total{outcome="not_found"}`.

### Metrics exposed

**HTTP request metrics**

| Metric                                  | Type      | Labels                    | What it answers                                       |
| --------------------------------------- | --------- | ------------------------- | ----------------------------------------------------- |
| `orders_api_http_requests_total`        | Counter   | `method`, `route`, `status_code` | Request volume and error mix per endpoint.      |
| `orders_api_http_request_duration_seconds` | Histogram | `method`, `route`         | Latency distribution (p50/p95/p99) per endpoint.     |

**Order application metrics**

| Metric                                  | Type      | Labels             | What it answers                                        |
| --------------------------------------- | --------- | ------------------ | ------------------------------------------------------ |
| `orders_api_order_lookups_total`        | Counter   | `outcome`          | Lookups by `success` / `not_found`; sum is total attempts. |
| `orders_api_order_lookup_duration_seconds` | Histogram | —                | Order store latency without HTTP overhead.             |
| `orders_api_orders_listed_total`        | Counter   | `status_filter`    | How the list endpoint is being queried.                 |
| `orders_api_orders_returned`            | Histogram | —                  | How many orders each list query actually returns.      |
| `orders_api_orders_matched`             | Histogram | —                  | How many orders matched the filters (catches a broken filter). |
| `orders_api_ready`                      | Gauge     | —                  | Readiness as last reported by `/ready` (1/0).           |
| `orders_api_build_info`                 | Gauge     | `version`, `environment` | Which build is answering. Always `1`.            |

Example output after a few requests:

```
orders_api_http_requests_total{method="GET",route="/api/orders",status_code="200"} 2.0
orders_api_http_requests_total{method="GET",route="/api/orders/{order_id}",status_code="404"} 1.0
orders_api_order_lookups_total{outcome="success"} 2.0
orders_api_order_lookups_total{outcome="not_found"} 1.0
orders_api_ready 1.0
orders_api_build_info{environment="production",version="1.0.0"} 1.0
```

Useful queries once a Prometheus server exists:

```promql
# Error ratio on the order detail endpoint
sum(rate(orders_api_http_requests_total{route="/api/orders/{order_id}",status_code=~"5.."}[5m]))
  / sum(rate(orders_api_http_requests_total{route="/api/orders/{order_id}"}[5m]))

# p95 latency for order lookups
histogram_quantile(0.95, sum(rate(orders_api_http_request_duration_seconds_bucket{route="/api/orders/{order_id}"}[5m])) by (le))

# Share of lookups that could not be served
sum(rate(orders_api_order_lookups_total{outcome="not_found"}[5m]))
  / sum(rate(orders_api_order_lookups_total[5m]))
```

### Cardinality: the rule that keeps this affordable

Every label is chosen so the number of time series stays small and predictable.
A single unbounded label can take down a Prometheus server, so the following are
**never** used as labels:

- `order_id`, `customer_id`, `request_id`, or any other unique identifier
- raw request paths, query strings, or timestamps

Instead:

- The path is recorded as its **route template**, so `/api/orders/ORD-1` and
  `/api/orders/ORD-2` share one series `/api/orders/{order_id}`. A thousand
  distinct order ids produce exactly one series.
- Paths that match no route are recorded as `unmatched`, so background scanner
  traffic cannot invent a new series per URL.
- Methods, outcomes, and status filters are whitelisted; anything unexpected is
  folded into a catch-all value rather than creating a new series.
- `version` and `environment` are constant per deployment, so
  `orders_api_build_info` is a single series.

The rule of thumb: a label's values must be enumerable in advance. If you cannot
list them, they do not belong in a label.

### Probes and scrapes are not application traffic

`/health`, `/ready` and `/metrics` are **excluded** from
`orders_api_http_requests_total` by default. They are called on a fixed timer by
the orchestrator and by Prometheus, regardless of how much real traffic the
service is serving. Counting them would mean the "requests per second" number is
really "probes per second", and that average latency is dominated by trivially
cheap probe requests — which is exactly how a real latency regression gets
hidden.

Readiness is not invisible, though: `/ready` publishes its outcome to the
one-series `orders_api_ready` gauge, because "is this instance serving?" is too
important a signal to lose. Set `ORDERS_API_METRICS_INCLUDE_PROBES=true` to count
probes as traffic if you would rather see them all in one place.

Metrics can be turned off entirely with `ORDERS_API_METRICS_ENABLED=false`,
which removes the `/metrics` route and stops all recording.

## Project layout

```
app/
  main.py            application factory, lifespan, exception handlers
  config.py          environment-driven settings
  logging_config.py  structured JSON logging and request context
  middleware.py      request correlation, access logging and HTTP metrics
  metrics.py         metric definitions, registry and route-template resolver
  errors.py          error types and the shared error envelope
  models.py          request/response schemas and money rules
  repository.py      order data access
  seed.py            in-memory fixture data
  routers/
    health.py        /health and /ready
    orders.py        /api/orders endpoints
    metrics.py       /metrics scrape endpoint
run.py               entrypoint, binds using environment config
tests/               pytest suite
```

## Not in this stage

Deliberately deferred to later stages: the Prometheus server and `prometheus.yml`,
Grafana, Alertmanager, Kubernetes, Terraform, AWS, CI/CD, a database, and
authentication.

This milestone only instruments the application. There is nothing here that
*collects* or *stores* the metrics yet, so the numbers reset every time the
process restarts and are not visible outside the container.
