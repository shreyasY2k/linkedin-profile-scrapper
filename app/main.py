"""Application factory.

The OpenAPI document this produces is the API documentation the README links
to (story 9), so title, version and description are part of the deliverable,
not decoration.

Importing this module imports :mod:`app.config`, which validates the whole
environment. An incomplete environment therefore fails here, at boot, with a
non-zero exit — the process never gets far enough to answer ``/health``.
"""

from __future__ import annotations

import logging
import sys

from fastapi import FastAPI

from app import __version__
from app.api import health
from app.api import v1
# Imported for its side effect and re-exported for callers: `Settings()` runs at
# app.config module scope, so an incomplete environment has already raised by
# the time this line returns. Do not "clean up" as unused.
from app.config import settings as settings  # noqa: F401
from app.errors import install_error_handlers

#: Format carrying the two things `logging.lastResort` does not: when it
#: happened and which logger said it. Without them, `docker compose logs api`
#: shows a bare message with no timestamp — and the README promises an operator
#: can read a rejection reason there, which is only true if this runs.
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging() -> None:
    """Give the root logger a handler, once.

    uvicorn configures its own `uvicorn.*` loggers and leaves the root logger
    alone, so without this every `logger.info` in this codebase is discarded and
    every `logger.warning` reaches stderr only through `logging.lastResort` —
    which emits the bare message, with no timestamp, no level and no logger
    name. That is not something an operator can read.

    There is deliberately no LOG_LEVEL variable. Story 1 fixed that every
    `Settings` field is required and non-blank, so an optional one would be a
    special case in the env contract, and a required one would mean every
    existing `.env` stops booting. INFO is the level; it is not configurable,
    and nothing branches on it.

    Idempotent: a root handler already installed (by uvicorn's `--log-config`,
    by pytest's capture, or by a second `create_app()`) is left alone rather
    than duplicated, which is what would double every line.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(handler)
    root.setLevel(logging.INFO)


API_TITLE = "LinkedIn Profile API"
API_DESCRIPTION = (
    "Accepts a LinkedIn profile URL and returns the profile as structured JSON, "
    "retrieved under the caller's own LinkedIn session."
)


def create_app() -> FastAPI:
    """Build the ASGI application with every router mounted."""
    configure_logging()

    application = FastAPI(
        title=API_TITLE,
        description=API_DESCRIPTION,
        version=__version__,
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Typed error envelope, for every failure path: ApiError, Starlette's own
    # 404/405, request validation, and anything unforeseen. Registration order
    # relative to the routers is irrelevant — Starlette resolves the handler
    # table per request, not at include time — so this sits here for reading
    # order alone.
    install_error_handlers(application)

    # Unversioned operational routes. Deliberately unauthenticated.
    application.include_router(health.router)

    # The versioned seam, carrying bearer validation as a router-level
    # dependency. No routes yet — stories 5-8 fill it in and inherit auth.
    application.include_router(v1.router)

    return application


app = create_app()
