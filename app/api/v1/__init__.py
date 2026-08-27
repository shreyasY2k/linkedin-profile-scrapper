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

import logging
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import RedirectResponse, Response
from starlette.routing import Match

from app.auth import require_claims
from app.errors import IDP_UNAVAILABLE_RESPONSE, UNAUTHENTICATED_RESPONSE

logger = logging.getLogger(__name__)

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
# ONE ERROR ENVELOPE UNDER /api/v1, INCLUDING FOR PATHS THAT DO NOT EXIST
# =============================================================================
#
# Everything above is inherited by routes that EXIST. Routing happens before any
# dependency runs, so a path with no route never reaches `require_claims` at all
# — and until story 8, `GET /api/v1/nope` with no token answered `404
# NOT_FOUND` while `GET /api/v1/profile` with no token answered `401`.
#
# WHAT THIS IS AND IS NOT.
#
# It is not enumeration resistance, and claiming otherwise would be worse than
# not doing it: `/openapi.json` is unauthenticated and publishes every route on
# this service, which story 9 ships as the API documentation and must keep
# public. Anyone who wants the route list can read it there in one request. A
# 401 here also does not hide much on its own — `PUT /api/v1/session` with an
# unparseable body still answers 400 where an absent path answers 401, because
# FastAPI reads the body before the dependency runs. That leak is known,
# accepted, and recorded in the deferred-work log rather than papered over.
#
# What it IS: one error envelope, and one status, for every request under this
# prefix that has not authenticated. A caller who forgot their token gets the
# same answer whatever they asked for, which is the answer that tells them the
# useful thing; a 404 in its place says "that route is not there" to somebody
# whose actual problem is that they sent no credential. The uniformity is worth
# having on its own terms, and it removes the cheapest of several ways to tell
# real paths from absent ones without pretending to remove them all.
#
# The mechanism is a route that matches everything left over, carrying the same
# dependency as everything else. Because it is a real route, the dependency
# runs — so an unauthenticated request under `/api/v1` is refused before this
# handler decides anything, whether or not the path was real.

#: The catch-all's paths. Also its identity: :func:`_methods_answering` skips
#: any route declaring one of these, and nothing else may declare them.
#:
#: Two, because ``/api/v1`` itself is not matched by a ``{path:path}`` converter
#: that requires the separating slash — it fell through to Starlette's
#: `redirect_slashes` and answered a `307` to unauthenticated callers, which is
#: the one status the whole point of this route is to not have.
UNMATCHED_PATH_ROUTE = "/api/v1/{unmatched_path:path}"
UNMATCHED_PREFIX_ROUTE = "/api/v1"
UNMATCHED_PATH_ROUTES = (UNMATCHED_PREFIX_ROUTE, UNMATCHED_PATH_ROUTE)

#: Methods probed when computing `Allow`. NOT the set the catch-all accepts —
#: see :class:`AnyMethodRoute`, which accepts every method there is.
PROBED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

#: Cap on a caller-controlled path reaching a log line, matching the treatment
#: `app/auth.py` and `app/linkedin/client.py` give their own untrusted values.
LOGGABLE_PATH_MAX_CHARS = 120


class AnyMethodRoute(APIRoute):
    """A route that matches on path alone, whatever the method.

    A route declares a method *set*, and Starlette answers `405` for a path that
    matches with a method that does not. For this route that is a hole rather
    than a courtesy: a verb outside the declared set short-circuits past
    `require_claims` into Starlette's own 405, whose `Allow` header then names
    exactly which methods each real route answers. `TRACE /api/v1/profile`
    answered `Allow: GET` while `TRACE /api/v1/__nope__` answered the full
    declared list — the distinction this route exists to remove, restored by any
    verb nobody thought to declare, including invented ones.

    So the method check is dropped: a partial match (path yes, method no) is
    promoted to a full one. Nothing is lost, because the `405` a real route
    would have produced is reconstructed deliberately in
    :func:`answer_unmatched_path`, for callers who have authenticated.
    """

    def matches(self, scope: Any) -> tuple[Match, dict[str, Any]]:
        match, child_scope = super().matches(scope)
        if match is Match.PARTIAL:
            # Starlette populates `child_scope` fully before the method check,
            # so promoting is safe: the endpoint and path params are already in
            # it. PARTIAL means "path matched, method did not", and this route
            # has no opinion about methods.
            return Match.FULL, child_scope
        return match, child_scope

    async def handle(self, scope: Any, receive: Any, send: Any) -> None:
        # `Route.handle` checks the method a SECOND time and raises its own 405
        # with an `Allow` header built from the declared set. Overriding
        # `matches` alone therefore changed nothing observable — the 405 simply
        # moved from the router to the route, `Allow` still named the declared
        # methods, and the leak survived. Both checks have to go.
        await self.app(scope, receive, send)


def _loggable_path(path: str) -> str:
    """A request path is caller-controlled; `repr` stops it forging a record."""
    rendered = repr(path)
    if len(rendered) > LOGGABLE_PATH_MAX_CHARS:
        rendered = rendered[:LOGGABLE_PATH_MAX_CHARS] + "...(truncated)"
    return rendered


def _full_match(request: Request, path: str, method: str) -> bool:
    """Whether a REAL route answers ``path`` with ``method``.

    Asked of the app's own routing table rather than of a list restated here, so
    a route added later is accounted for without anyone remembering to. The
    catch-all's own routes are skipped by path — they match everything, and
    would report every method at every path as real.
    """
    scope = dict(request.scope)
    scope["method"] = method
    scope["path"] = path
    scope["raw_path"] = path.encode("utf-8")
    for route in request.app.router.routes:
        if getattr(route, "path", None) in UNMATCHED_PATH_ROUTES:
            continue
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return True
    return False


def _methods_answering(request: Request, path: str) -> set[str]:
    """Which methods a real route would answer ``path`` with.

    An empty result means the path is not real under any method, which is the
    404. A non-empty one is the `Allow` header of a 405 — the one Starlette
    would have produced had this route not matched first.
    """
    return {method for method in PROBED_METHODS if _full_match(request, path, method)}


def _redirect_target(request: Request, path: str) -> str | None:
    """The trailing-slash variant of ``path`` that a real route answers.

    Starlette redirects `/api/v1/profile/` to `/api/v1/profile` when nothing
    matches, and this route matching first took that away — an authenticated
    `GET /api/v1/profile/` went from a `307` to a hard `404`, a client-visible
    break for a request that used to work. Reinstated here rather than
    documented as a loss: the redirect is what the router would have done, and
    it is not this route's business to change it.
    """
    alternative = path[:-1] if path.endswith("/") else path + "/"
    if not alternative or alternative in UNMATCHED_PATH_ROUTES:
        return None
    if not _full_match(request, alternative, request.method):
        return None
    return str(request.url.replace(path=alternative))


async def answer_unmatched_path(request: Request) -> Response:
    """Answer a path under ``/api/v1`` that no real route claims.

    Only reached with a **valid token**: the ``require_claims`` dependency
    :func:`install_unmatched_path_guard` attaches has already refused everyone
    else with the same 401 a real route would give, which is the entire point of
    this route existing. What is left is an authenticated caller, and they are
    told exactly what the router would have told them — a `307` to the
    trailing-slash variant that does exist, a `405` with `Allow` for the wrong
    method on a real path, or a `404`.

    The two errors raise Starlette's own exception rather than building a
    response, so the status, the `Cache-Control: no-store` header and the
    envelope shape are `app/errors.py`'s throughout; the message and code come
    from :data:`~app.errors.FALLBACK_MESSAGES` and
    :data:`~app.errors.FALLBACK_CODES` there, keyed on the status raised here.

    The probe is logged. An operator debugging "why is everything 401" is
    pointed at the rejection log by the README, and an *authenticated* sweep of
    the namespace produced no line at all — so the one caller who got through
    the boundary and is guessing at paths was the only one invisible.
    """
    path = request.url.path
    method = request.method
    loggable = _loggable_path(path)

    redirect = _redirect_target(request, path)
    if redirect is not None:
        logger.info("Redirecting %s %s to its canonical path", method, loggable)
        return RedirectResponse(redirect, status_code=307)

    answering = _methods_answering(request, path)
    if answering:
        logger.info(
            "%s %s exists but answers only %s", method, loggable, sorted(answering)
        )
        raise StarletteHTTPException(405, headers={"Allow": ", ".join(sorted(answering))})

    logger.info("No route under the versioned prefix answers %s %s", method, loggable)
    raise StarletteHTTPException(404)


def install_unmatched_path_guard(application: FastAPI) -> None:
    """Attach the catch-all. **Must be called after every real router.**

    Routing is first-match-wins, so a route registered after this one is
    unreachable. `tests/test_auth.py` asserts the ordering rather than trusting
    the comment, because the symptom of getting it wrong — every request to a
    newly added endpoint answering 404 — points nowhere near the cause.

    Added to the application rather than to ``router`` above for two reasons.
    ``include_router`` leaves a lazy marker carrying no ``path``, and the
    matching helpers need to identify these routes by path to skip them; and the
    test suite mounts probe routes onto ``router`` *after* import, which a
    catch-all sitting inside it would shadow.
    """
    for path in UNMATCHED_PATH_ROUTES:
        application.router.routes.append(
            AnyMethodRoute(
                path,
                answer_unmatched_path,
                # Declared for readability only — `AnyMethodRoute.matches`
                # ignores the set and answers every verb.
                methods=list(PROBED_METHODS),
                dependencies=[Depends(require_claims)],
                # Not API surface. It documents nothing a caller can usefully
                # call, and a `{unmatched_path}` entry in the document story 9
                # ships as the API documentation would read as a real endpoint.
                include_in_schema=False,
            )
        )
