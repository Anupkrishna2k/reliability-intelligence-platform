# Reliability Intelligence Platform

An SRE/DevOps portfolio project. This repository is built in stages; the goal
is to grow a small but realistic service into a fully instrumented, observable,
and safely deployed system.

**Current stage: the Orders API** — the workload that later stages will
instrument, observe, alert on, and deploy.

## What this service is

A read-only HTTP API for customer orders, written in Python with FastAPI. It
exists to be a believable production service: it separates liveness from
readiness, emits one structured JSON log line per request, exposes a consistent
error envelope, and is driven entirely by environment variables so the same
build runs locally and in a container.

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

Interactive API docs are served at `/docs`, and the OpenAPI schema at
`/openapi.json`.

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

## Project layout

```
app/
  main.py            application factory, lifespan, exception handlers
  config.py          environment-driven settings
  logging_config.py  structured JSON logging and request context
  middleware.py      request correlation and access logging
  errors.py          error types and the shared error envelope
  models.py          request/response schemas and money rules
  repository.py      order data access
  seed.py            in-memory fixture data
  routers/
    health.py        /health and /ready
    orders.py        /api/orders endpoints
run.py               entrypoint, binds using environment config
tests/               pytest suite
```

## Not in this stage

Deliberately deferred to later stages: metrics and Prometheus, Grafana,
Alertmanager, Kubernetes, Terraform, AWS, CI/CD, a database, and
authentication.
