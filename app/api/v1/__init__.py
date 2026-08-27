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

# Story 5: PUT|GET /api/v1/session. Imported after `router` exists, because
# `session` imports nothing from this module and this module must be importable
# on its own for `tests/test_auth.py`'s structural assertions.
#
# Note what the session router does NOT declare: any security dependency of its
# own. It inherits the one above, which is the whole point of the placement.
from app.api.v1 import session as session_routes  # noqa: E402

router.include_router(session_routes.router)

# Story 6: GET /api/v1/profile — the endpoint CAP-1 is graded on.
#
# It imports `get_vault` and `no_store` from `app.api.v1.session`, so that the
# two routers share one vault dependency and one cache marker — overriding
# `session.get_vault` in a test therefore covers both routes. That is an
# ordinary module import and Python resolves it whichever order these two lines
# are in; there is no ordering constraint here to preserve.
#
# Like the session router, it declares no security dependency at the ROUTER
# level: authentication is the one attached to `router` above, and that is the
# whole point of the placement. The handler separately takes `require_claims`
# as a dependency because it needs the verified `sub` — depending on it twice
# costs nothing, since FastAPI caches a dependency within a request.
from app.api.v1 import profile as profile_routes  # noqa: E402

router.include_router(profile_routes.router)

# Stories 7-8 attach theirs the same way, e.g.:
#     from app.api.v1 import something
#     router.include_router(something.router)
#
# A route that needs the caller's identity takes it as a dependency:
#     from app.auth import require_claims
#     async def endpoint(claims: dict = Depends(require_claims)) -> ...:
#         subject = claims["sub"]
# Depending on it a second time costs nothing — FastAPI caches a dependency
# within a request — and it is how the verified subject reaches the handler.
