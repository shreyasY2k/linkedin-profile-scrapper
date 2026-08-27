"""The ``/api/v1`` seam — and the authentication boundary.

The prefix is fixed by ``response-schema.md`` (``GET /api/v1/profile``,
``PUT|GET /api/v1/session``); carving it out in story 1 meant stories 5-8
attach routers here instead of renegotiating the layout under deadline.

Story 3 attaches bearer validation to *this router*, not to individual routes.
That placement is the whole point: a route added below inherits authentication
whether or not its author remembered it, so an unprotected endpoint cannot be
shipped by omission. Anything that must stay open — ``/health`` — lives outside
this router by construction, not by an exception carved into it.

``tests/test_auth.py`` mounts a probe route that declares no dependency of its
own, precisely so that deleting the ``dependencies=`` line below fails a test.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import require_claims
from app.errors import UNAUTHENTICATED_RESPONSE

router = APIRouter(
    prefix="/api/v1",
    # Router-level, so `include_router` merges it onto every route beneath.
    dependencies=[Depends(require_claims)],
    # Makes the 401 body visible in the OpenAPI document story 9 ships as the
    # API documentation, instead of FastAPI's default {"detail": "..."}.
    responses=UNAUTHENTICATED_RESPONSE,
)

# Stories 5-8 attach their routers here, e.g.:
#     from app.api.v1 import profile
#     router.include_router(profile.router)
#
# A route that needs the caller's identity takes it as a dependency:
#     from app.auth import require_claims
#     async def endpoint(claims: dict = Depends(require_claims)) -> ...:
#         subject = claims["sub"]
# Depending on it a second time costs nothing — FastAPI caches a dependency
# within a request — and it is how the verified subject reaches the handler.
