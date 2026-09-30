# syntax=docker/dockerfile:1

# --- build stage: resolve dependencies into a self-contained venv -------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

COPY requirements.txt ./

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


# --- runtime stage ------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    ORDERS_API_HOST=0.0.0.0 \
    ORDERS_API_PORT=8000 \
    ORDERS_API_ENVIRONMENT=production \
    ORDERS_API_LOG_FORMAT=json

# Run unprivileged: a container escape should not start as root.
RUN groupadd --system app \
    && useradd --system --gid app --no-log-init --create-home app

COPY --from=builder --chown=app:app /opt/venv /opt/venv

WORKDIR /srv/app

COPY --chown=app:app app ./app
COPY --chown=app:app run.py ./run.py

USER app

EXPOSE 8000

# Liveness only: readiness is an orchestrator concern, not Docker's.
# Uses the interpreter already in the image, so no curl install is needed.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]

CMD ["python", "run.py"]
