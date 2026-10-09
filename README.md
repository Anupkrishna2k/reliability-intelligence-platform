# Reliability Intelligence Platform

An SRE/DevOps portfolio project. This repository is built in stages; the goal
is to grow a small but realistic service into a fully instrumented, observable,
and safely deployed system.

**Current stage: local observability** — the Orders API runs as a Docker
Compose stack next to a Prometheus server that scrapes it, with a provisioned
Grafana dashboard on top. This stage adds **controlled failure injection**: a
flag-gated control API that adds artificial latency and HTTP 500s on demand, so
the existing metrics and dashboard can be shown reacting to a realistic
incident. It is disabled by default and refused in production. Alerting and
deployment stages come later.

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
| `GET`  | `/admin/failure`        | Read failure-injection scenario. Only when enabled (see below). |
| `PUT`  | `/admin/failure`        | Configure latency and/or HTTP 500 rate. Only when enabled.    |
| `DELETE`| `/admin/failure`       | Reset failure injection to normal. Only when enabled.         |

Interactive API docs are served at `/docs`, and the OpenAPI schema at
`/openapi.json`. `/metrics` is intentionally hidden from the docs — it exists
for Prometheus, not for API consumers. The `/admin/failure` routes only exist
when failure injection is explicitly enabled; see
[Controlled failure injection](#controlled-failure-injection-local-demos-only).

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

## Running the complete stack with Docker Compose

`docker-compose.yml` runs the whole local stack: the Orders API, a Prometheus
server that scrapes it, and a Grafana dashboard on top of that data.

| Service           | Image                        | Host access                    |
| ----------------- | ---------------------------- | ------------------------------ |
| `reliability-api` | built from `Dockerfile`      | http://localhost:8000          |
| `prometheus`      | `prom/prometheus:v3.5.0`     | http://localhost:9090          |
| `grafana`         | `grafana/grafana:11.6.0`     | http://localhost:3000          |

Start it:

```bash
docker compose up --build -d
```

Verify all three services are up:

```bash
docker compose ps
curl -s http://localhost:8000/health      # API liveness
curl -s http://localhost:9090/-/ready     # Prometheus readiness
curl -s http://localhost:3000/api/health  # Grafana health
```

Stop it:

```bash
docker compose down        # stop containers, keep collected metrics/dashboards
docker compose down -v     # also delete the prometheus and grafana volumes
```

Prometheus only starts after the API reports healthy, and Grafana only starts
after Prometheus reports ready (`depends_on` with `condition: service_healthy`),
so on a fresh `up` the first thing visible in Grafana is already queryable.

## Grafana dashboards

Grafana is exposed at **http://localhost:3000**.

- **Login:** user `admin`, password `admin`
  (**local development only** — change `GF_SECURITY_ADMIN_PASSWORD` in
  `docker-compose.yml` before exposing this stack anywhere else).
- **Sign-up is disabled**, so no one can create an account on your machine.

### Opening the dashboard

1. Go to http://localhost:3000 and log in with `admin` / `admin`.
2. **Dashboards** → **Orders API – Service Overview** (it is provisioned into
   the root folder on every start).

The dashboard is committed to the repository and recreated automatically on
start-up from `grafana/dashboards/orders-api-overview.json`. There is nothing
to click through after a fresh `docker compose up` — the datasource and the
dashboard both come from provisioning files, so the whole setup is reproducible
from git.

### What is on it

| Panel                       | What it shows (query)                                              |
| --------------------------- | ------------------------------------------------------------------ |
| API Readiness               | `orders_api_ready` gauge — 1 while `/ready` passes.                |
| HTTP Request Rate           | `sum(rate(orders_api_http_requests_total[5m]))`.                   |
| Error Rate (4xx + 5xx)      | `4xx|5xx` requests / total requests over 5m.                       |
| P95 Request Latency         | `histogram_quantile(0.95, ...)` of HTTP latency.                   |
| HTTP Requests by Status     | Request rate split by `status_code` label.                         |
| Request Latency Percentiles | p50 / p95 / p99 from the HTTP duration histogram.                  |
| Order Lookups: Success/NF   | `orders_api_order_lookups_total` by `outcome`.                     |
| Request Rate by Route       | Rate per route template (low-cardinality by design).               |
| Application / Build Info    | `orders_api_build_info` version + environment labels.              |
| Prometheus Scrape Target    | `up{job="reliability-api"}` — is the metrics pipeline alive?       |

Only metrics the application already exposes are used — nothing was invented
for the dashboard.

### Generating demo traffic

The request-rate panels read counter deltas, so a single burst of calls shows
up as little more than a spike. To make the dashboard look alive, generate
sustained load for a minute or two:

```bash
# ~3 requests/sec including intentional 404s and a validation error
for s in $(seq 1 120); do
  curl -s -o /dev/null "http://localhost:8000/api/orders"
  curl -s -o /dev/null "http://localhost:8000/api/orders/ORD-2026-000$((s % 6 + 1))"
  curl -s -o /dev/null "http://localhost:8000/api/orders/ORD-NOPE-$s"
  [ $((s % 10)) -eq 0 ] && curl -s -o /dev/null "http://localhost:8000/api/orders?status=not-a-status"
  sleep 0.3
done
```

The `not found` lookups and the `422` show up as healthy red lines — they are
expected traffic, not a broken service.

### How Grafana connects to Prometheus

Exactly like Prometheus connects to the API: no agent, no push, just the
Compose network.

1. `grafana/provisioning/datasources/datasource.yml` declares one Prometheus
   datasource pointing at `http://prometheus:9090` — the Compose service name,
   never `localhost`, because inside the Grafana container `localhost` is
   Grafana itself. It is pinned to `uid: prometheus` so the committed dashboard
   JSON always resolves to it, and `editable: false` keeps it managed.
2. `grafana/provisioning/dashboards/dashboards.yml` registers the
   `grafana/dashboards/` directory as a **file provider**. Every dashboard JSON
   there is loaded at start-up; edits in the browser are discarded in favour of
   the committed file.
3. The Grafana container mounts both directories read-only, so the running
   config is never modified in place — change the files here and run
   `docker compose restart grafana` to apply.

### Grafana troubleshooting

| Symptom | Cause |
| ------- | ----- |
| `admin` login rejected | The volume from an old, differently-configured run; `docker compose down -v` and `up -d`. |
| Datasource listed but "No data" in every panel | No traffic scraped yet in the selected time range; generate demo traffic (above) and refresh. |
| Dashboard missing after edits | The provider reads files on start and re-checks every 30s; `docker compose restart grafana` applies changes. |
| `proxy: Request failed` on the datasource | Grafana cannot reach `http://prometheus:9090`; check `docker compose logs grafana`. |
| Port 3000 already in use | Something else holds the port; stop it or remap `"3000:3000"` in `docker-compose.yml`. |

### Accessing the Orders API

- **Base URL:** http://localhost:8000
- **Interactive docs:** http://localhost:8000/docs
- **OpenAPI schema:** http://localhost:8000/openapi.json
- **Metrics:** http://localhost:8000/metrics

```bash
curl -s "http://localhost:8000/api/orders?limit=2"
curl -s http://localhost:8000/api/orders/ORD-2026-0001
```

### Accessing Prometheus

- **UI:** http://localhost:9090
- **Target health:** http://localhost:9090/targets
- **Graphs:** http://localhost:9090/graph (try `up` or `orders_api_build_info`)
- **Readiness probe:** http://localhost:9090/-/ready

From the terminal:

```bash
# Is the Orders API target up?
curl -s 'http://localhost:9090/api/v1/query?query=up{job="reliability-api"}'

# Full target list, including the scrape URL and last error if any
curl -s http://localhost:9090/api/v1/targets | python3 -m json.tool
```

A healthy target reports `"health": "up"` and
`"scrapeUrl": "http://reliability-api:8000/metrics"`.

### How Prometheus discovers and scrapes the API

There is no agent and no push: Prometheus **pulls**.

1. Compose creates a single network and attaches both services to it. On that
   network, the service name is also a DNS name, so Prometheus resolves
   `reliability-api` to the API container's IP. That is why the target is
   `reliability-api:8000` and **not** `localhost:8000` — inside the Prometheus
   container, `localhost` is the Prometheus container itself.
2. `prometheus/prometheus.yml` declares a `static_configs` scrape job named
   `reliability-api` pointing at `reliability-api:8000` with
   `metrics_path: /metrics`.
3. Every 15 seconds Prometheus issues an HTTP GET to
   `http://reliability-api:8000/metrics`, parses the Prometheus text
   exposition format, and appends the samples to its local TSDB (a Docker
   named volume).
4. Each scrape produces the synthetic `up` series — `1` for a successful
   scrape, `0` for a failed one — which is the standard way to alert on
   "Prometheus cannot reach the target". Target labels (`job`, `service`,
   `env`, `instance`) are attached to every series scraped from that target.
5. The config file is bind-mounted read-only, so editing
   `prometheus/prometheus.yml` and running `docker compose restart prometheus`
   applies changes without rebuilding anything.

### Troubleshooting the stack

```bash
docker compose ps                                     # are both containers healthy?
docker compose logs -f reliability-api                # API logs (JSON, one line per request)
docker compose logs -f prometheus                      # scrape and config errors
curl -s http://localhost:9090/api/v1/targets           # target health + last scrape error
curl -s http://localhost:8000/metrics | head           # is the API exposing metrics at all?
docker compose config -q                               # validate the compose file
docker compose restart prometheus                      # reload prometheus.yml after editing it
docker compose down -v && docker compose up --build -d # clean slate
```

Typical failures:

| Symptom | Cause |
| ------- | ----- |
| Target `down`, error `connection refused` | The API container is not running — check its logs. |
| Target `down`, error `no such host` | The target is not using the service name `reliability-api`. |
| Prometheus `error loading config` | A YAML syntax error in `prometheus/prometheus.yml` — visible in `docker compose logs prometheus`. |
| Port already in use on start | Something else holds `8000` or `9090`; stop it or change the host side of the `ports` mapping. |

## Controlled failure injection (local demos only)

The API can simulate production-like incidents on demand: add artificial
latency, return HTTP 500s at a configurable rate, or both. That lets you show
the Prometheus metrics and Grafana dashboard reacting to a realistic problem
without actually breaking anything.

### Safety

Failure injection is **disabled by default** and **local-development only**:

- The whole feature is gated behind `ORDERS_API_FAILURE_INJECTION_ENABLED`,
  which defaults to `false`. When it is off, the control endpoints do not exist
  (they return `404`) and every request is served exactly as it was before.
- The service **refuses to start** if failure injection is enabled while
  `ORDERS_API_ENVIRONMENT` is `production`/`prod`, so a single environment
  variable cannot expose the control plane on a real deployment.
- Injected behaviour is hard-capped: latency `0–10000 ms` and failure rate
  `0–100 %`. Anything outside that range is rejected with `422`.
- Liveness (`/health`), readiness (`/ready`), the metrics scrape (`/metrics`)
  and the control API itself are always exempt, so the instance stays
  observable and injection can always be switched back off.

The local Docker Compose stack sets `ORDERS_API_ENVIRONMENT=development` and
`ORDERS_API_FAILURE_INJECTION_ENABLED=true` for exactly this reason. A plain
`docker run` of the image keeps its production default and stays safe.

### Enabling it locally

The Compose stack already enables it. For a local `python run.py` session:

```bash
ORDERS_API_FAILURE_INJECTION_ENABLED=true python run.py
```

or set the same variable in `.env`. The control API is then mounted at
`/admin/failure` (configurable with `ORDERS_API_FAILURE_INJECTION_PATH`).

### Control API

| Method   | Path              | Effect                                                |
| -------- | ----------------- | ----------------------------------------------------- |
| `GET`    | `/admin/failure`  | Read the active scenario.                             |
| `PUT`    | `/admin/failure`  | Set `enabled`, `latency_ms`, `failure_rate_percent`.  |
| `DELETE` | `/admin/failure`  | Reset: disabled, zero latency, zero failures.         |

`enabled` defaults to `true`, so a `PUT` that only sets `latency_ms` or
`failure_rate_percent` activates the scenario immediately. `DELETE` is the
one-call reset.

```bash
# Current scenario
curl -s http://localhost:8000/admin/failure

# Add 500 ms of latency to every application request
curl -s -X PUT http://localhost:8000/admin/failure \
  -H 'content-type: application/json' \
  -d '{"enabled": true, "latency_ms": 500, "failure_rate_percent": 0}'

# Fail 30% of requests with HTTP 500 (and keep the latency)
curl -s -X PUT http://localhost:8000/admin/failure \
  -H 'content-type: application/json' \
  -d '{"enabled": true, "latency_ms": 500, "failure_rate_percent": 30}'

# Disable quickly without clearing the values
curl -s -X PUT http://localhost:8000/admin/failure \
  -H 'content-type: application/json' -d '{"enabled": false}'

# Reset everything back to normal
curl -s -X DELETE http://localhost:8000/admin/failure
```

An injected 500 uses the same error envelope as the rest of the API:

```json
{
  "error": {
    "code": "injected_failure",
    "message": "Simulated internal server error (controlled failure injection)."
  },
  "request_id": "a46d84a5547740818b17770d79a6bba3"
}
```

### Restoring normal behaviour

`DELETE /admin/failure` (shown above) disables the scenario *and* clears the
values. If you would rather keep the values but stop injecting, send
`{"enabled": false}`. Restarting the API also resets the scenario, since it is
held in memory only.

### Observing the impact in Grafana

Injected behaviour flows through the instrumentation that already exists — no
separate metrics are defined for it:

- Injected 500s increment
  `orders_api_http_requests_total{status_code="500"}` and light up the **Error
  Rate (4xx + 5xx)**, **HTTP Requests by Status Code** and **P95 Request
  Latency** panels on the *Orders API – Service Overview* dashboard.
- The injected delay raises `orders_api_http_request_duration_seconds`, so the
  **P95 Request Latency** and **Request Latency Percentiles** panels climb by
  roughly the configured amount.
- Every injected failure emits an `injected_failure` warning in the log (with
  the path, method and rate), followed by the usual `http_request` line with
  `http_status: 500`.

A ready-made demo loop:

```bash
# 40% failures + 400 ms latency
curl -s -X PUT http://localhost:8000/admin/failure \
  -H 'content-type: application/json' \
  -d '{"enabled": true, "latency_ms": 400, "failure_rate_percent": 40}'

# send traffic for ~12 seconds
for i in $(seq 1 60); do
  curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" http://localhost:8000/api/orders
  sleep 0.2
done

# back to normal
curl -s -X DELETE http://localhost:8000/admin/failure
```

Watch http://localhost:3000 (Grafana) and http://localhost:9090 (Prometheus)
while it runs. `/health` and `/ready` keep returning `200` throughout, which is
the point: the service is *slow and erroring*, not *down*.

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
| `ORDERS_API_FAILURE_INJECTION_ENABLED` | `false` | Enable the `/admin/failure` control API. Refused in production. |
| `ORDERS_API_FAILURE_INJECTION_PATH` | `/admin/failure` | Control API path when injection is enabled.      |

Invalid values fail fast at start-up rather than at the first request. Enabling
failure injection in a production environment is one such invalid value: the
process refuses to start.

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

Queries to run in the Prometheus UI at http://localhost:9090/graph once the
stack is up (the Grafana dashboard at http://localhost:3000 renders exactly
these — see [Grafana dashboards](#grafana-dashboards) for a ready-made view):

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
docker-compose.yml     local stack: reliability-api + prometheus + grafana
prometheus/
  prometheus.yml       scrape job for the Orders API
grafana/
  provisioning/
    datasources/       Prometheus datasource provisioning (auto-created at start)
    dashboards/        dashboard provider config (filesystem-backed)
  dashboards/
    orders-api-overview.json
                       versioned dashboard JSON, loaded on every start
app/
  main.py            application factory, lifespan, exception handlers
  config.py          environment-driven settings
  logging_config.py  structured JSON logging and request context
  middleware.py      request correlation, access logging and HTTP metrics
  metrics.py         metric definitions, registry and route-template resolver
  failure_injection.py
                     controlled latency/500 scenario and middleware
  errors.py          error types and the shared error envelope
  models.py          request/response schemas and money rules
  repository.py      order data access
  seed.py            in-memory fixture data
  routers/
    health.py        /health and /ready
    orders.py        /api/orders endpoints
    metrics.py       /metrics scrape endpoint
    failure.py       /admin/failure control API (demo only)
run.py               entrypoint, binds using environment config
tests/               pytest suite
```

## Not in this stage

Deliberately deferred to later stages: Alertmanager, Kubernetes, Terraform,
AWS, CI/CD, a database, and authentication.

Prometheus collects and stores the metrics locally and Grafana visualises
them, but nothing alerts on top of that data yet — that is the next milestone.
The dashboard is provisioned from git, so the whole stack can be recreated on a
fresh machine with a single `docker compose up`.

Failure injection here is a deliberately small, flag-gated demo tool. It is not
scheduled chaos engineering, not a production fault-injection framework, and it
adds no new metrics of its own.
