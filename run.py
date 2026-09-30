"""Entrypoint: `python run.py` (also the container's default command).

Binds the server using the environment-driven configuration so the same image
runs locally and in a container without a hardcoded port.
"""

from __future__ import annotations

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()

    # log_config=None keeps our structured logging in place; uvicorn's own
    # access log is disabled because the middleware emits a richer one.
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        reload=settings.debug and not settings.is_production,
    )


if __name__ == "__main__":
    main()
