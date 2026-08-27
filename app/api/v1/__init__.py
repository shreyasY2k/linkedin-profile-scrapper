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

from fastapi import APIRouter, Depends, FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match

from app.auth import require_claims
from app.errors import IDP_UNAVAILABLE_RESPONSE, UNAUTHENTICATED_RESPONSE

router = APIRouter(
    prefix="/api/v1",
    # Router-level, so `include_router` merges it onto every route beneath.
    dependencies=[Depends(require_claims)],
    # Makes the 401 body visible in the OpenAPI document story 9 ships as the
    # API documentation, instead of FastAPI's default {"detail": "..."}.
    #
    # The 502 beside it is the other answer this boundary can give, and it is
    # attached here for the same reason the dependency is: every route beneath
    # inherits it, because every route beneath validates a token and every one
    # of them therefore depends on Keycloak being reachable.
    responses={**UNAUTHENTICATED_RESPONSE, **IDP_UNAVAILABLE_RESPONSE},
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

# A later router attaches the same way, e.g.:
#     from app.api.v1 import something
#     router.include_router(something.router)
#
# A route that needs the caller's identity takes it as a dependency:
#     from app.auth import require_claims
#     async def endpoint(claims: dict = Depends(require_claims)) -> ...:
#         subject = claims["sub"]
# Depending on it a second time costs nothing — FastAPI caches a dependency
# within a request — and it is how the verified subject reaches the handler.


# =============================================================================
# THE AUTHENTICATION BOUNDARY HAS A HOLE ONLY ROUTING CAN CLOSE
# =============================================================================
#
# Everything above is inherited by routes that EXIST. Routing happens before any
# dependency runs, so a path with no route never reaches `require_claims` at all
# — and until story 8, `GET /api/v1/nope` with no token answered `404
# NOT_FOUND` while `GET /api/v1/profile` with no token answered `401`.
#
# No data leaks that way, and that is not the problem. The problem is that the
# two answers differ: an unauthenticated caller can sweep the namespace and read
# off which paths are real from the status code alone, which is a map of the
# API's surface handed to somebody who has not proved they may have one. It was
# observed on the deployed host and recorded against this story.
#
# The fix is a route that matches everything left over, carrying the same
# router-level dependency as everything else. Because it is a real route, the
# dependency runs — so an unauthenticated request under `/api/v1` is refused
# before this handler decides anything, whether or not the path was real.

#: The catch-all's path. Also its identity: :func:`_methods_answering` skips the
#: route declaring it, and nothing else may declare it.
UNMATCHED_PATH_ROUTE = "/api/v1/{unmatched_path:path}"

#: Methods the catch-all claims. Every method a caller could use to probe, or
#: the hole simply moves to the ones left out.
UNMATCHED_PATH_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _methods_answering(request: Request) -> set[str]:
    """Which methods a REAL route would answer this path with.

    Matching everything costs one thing worth paying for, and this buys it back:
    a wrong method on a real route (`POST /api/v1/profile`) now reaches this
    catch-all as an ordinary full match, so Starlette never gets to produce its
    own `405 Method Not Allowed` with the `Allow` header a client needs.

    So the same question is asked directly. Each candidate method is tried
    against the app's own routing table — the routers, not a list restated here,
    so a route added later is included without anyone remembering — and a full
    match means that method really is answered at this path. An empty result
    means the path is not real under any method, which is the 404.

    The catch-all itself is skipped by path, or it would match every method and
    report the whole set as allowed.
    """
    scope = dict(request.scope)
    answering: set[str] = set()
    for method in UNMATCHED_PATH_METHODS:
        scope["method"] = method
        for route in request.app.router.routes:
            if getattr(route, "path", None) == UNMATCHED_PATH_ROUTE:
                continue
            match, _ = route.matches(scope)
            if match is Match.FULL:
                answering.add(method)
                break
    return answering


async def answer_unmatched_path(request: Request, unmatched_path: str) -> None:
    """Answer a path under ``/api/v1`` that no real route claims.

    Only reached with a **valid token**: the ``require_claims`` dependency
    :func:`install_unmatched_path_guard` attaches has already refused everyone
    else with the same 401 a real route would give, which is the entire point of
    this route existing. What is left is an authenticated caller, and they are
    told the truth — 405 with `Allow` when they used the wrong method on a real
    path, 404 when the path is not real.

    Both raise Starlette's own exception rather than building a response, so the
    envelope and `Cache-Control: no-store` come from `app/errors.py` exactly as
    they do for a 404 outside this prefix.
    """
    answering = _methods_answering(request)
    if answering:
        raise StarletteHTTPException(405, headers={"Allow": ", ".join(sorted(answering))})
    raise StarletteHTTPException(404)


def install_unmatched_path_guard(application: FastAPI) -> None:
    """Attach the catch-all. **Must be called after every real router.**

    Routing is first-match-wins, so a route registered after this one is
    unreachable. `tests/test_auth.py` asserts the ordering rather than trusting
    the comment, because the symptom of getting it wrong — every request to a
    newly added endpoint answering 404 — points nowhere near the cause.

    Added to the application rather than to ``router`` above for two reasons.
    ``include_router`` leaves a lazy marker carrying no ``path``, and
    :func:`_methods_answering` needs to identify this route by its path to skip
    it; and the test suite mounts probe routes onto ``router`` *after* import,
    which a catch-all sitting inside it would shadow.
    """
    application.add_api_route(
        UNMATCHED_PATH_ROUTE,
        answer_unmatched_path,
        methods=UNMATCHED_PATH_METHODS,
        dependencies=[Depends(require_claims)],
        # Not API surface. It documents nothing a caller can usefully call, and
        # a `{unmatched_path}` entry in the document story 9 ships as the API
        # documentation would read as a real endpoint.
        include_in_schema=False,
    )
