"""Application factory.

The OpenAPI document this produces is the API documentation the README links
to (story 9), so title, version and description are part of the deliverable,
not decoration.

Importing this module imports :mod:`app.config`, which validates the whole
environment. An incomplete environment therefore fails here, at boot, with a
non-zero exit — the process never gets far enough to answer ``/health``.
"""

from __future__ import annotations

from fastapi import FastAPI

from app import __version__
from app.api import health
from app.api import v1
from app.config import settings

API_TITLE = "LinkedIn Profile API"
API_DESCRIPTION = (
    "Accepts a LinkedIn profile URL and returns the profile as structured JSON, "
    "retrieved under the caller's own LinkedIn session."
)


def create_app() -> FastAPI:
    """Build the ASGI application with every router mounted."""
    # Touch the settings object so a broken environment is a boot failure with
    # a clear traceback rather than a surprise on the first request.
    _ = settings.keycloak_realm

    application = FastAPI(
        title=API_TITLE,
        description=API_DESCRIPTION,
        version=__version__,
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Unversioned operational routes.
    application.include_router(health.router)

    # The versioned seam. No routes yet — stories 5-8 fill it in.
    application.include_router(v1.router)

    return application


app = create_app()
