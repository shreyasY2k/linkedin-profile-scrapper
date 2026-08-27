"""The typed error envelope from ``response-schema.md``.

Story 3 implemented exactly one code — ``UNAUTHENTICATED`` — in the shape the
whole taxonomy would adopt. Story 4 adds the seven upstream rows, which is
precisely the generalisation that was predicted: rows in :data:`ERROR_SPECS`,
and nothing about this module's structure, the wire shape, or any call site
written against it changed. The table is now complete against
``response-schema.md`` and story 8 invented no new rows either: what it changed
is *what is classified as what*, and one thing about this module.

The wire shape is fixed and is not negotiable per code::

    {"error": {"code": "...", "message": "...", "retryable": false}}

``retryable`` exists so a client can decide without parsing prose, which is why
it lives in the spec table next to the status rather than being inferred from
the status class.

The one thing story 8 changed here: **``retryable`` is a property of the
response, not of the code.** :data:`ERROR_SPECS` still fixes every code's
default and remains a byte-for-byte transcription of the contract's table, but a
named raise site may *narrow* its own instance — see :class:`ApiError`. One
condition needs it, and the alternative was a new contract row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ErrorDetail(BaseModel):
    """The inner object. Field names are normative — see ``response-schema.md``."""

    code: str = Field(
        description="Stable machine-readable code from the response-schema table.",
        examples=["UNAUTHENTICATED"],
    )
    message: str = Field(description="Human-readable explanation. Never contains a secret.")
    retryable: bool = Field(
        description="Whether repeating the identical request could succeed."
    )


class ErrorEnvelope(BaseModel):
    """Every error body this API returns, at every status code."""

    error: ErrorDetail


@dataclass(frozen=True)
class ErrorSpec:
    """The status and retryability the schema table fixes for one code.

    Frozen and table-driven so that the status/retryable pairing lives in one
    place. A handler that could choose its own status per call site is how a
    taxonomy drifts out of agreement with its own documentation.
    """

    status_code: int
    retryable: bool
    message: str


#: The taxonomy, keyed by code. This is ``response-schema.md``'s table,
#: transcribed — status and ``retryable`` are copied from it, not chosen here,
#: and a row that disagrees with that file is a bug in this one.
ERROR_SPECS: dict[str, ErrorSpec] = {
    "UNAUTHENTICATED": ErrorSpec(
        status_code=401,
        retryable=False,
        # One message for every rejection reason on purpose. Distinguishing
        # "expired" from "wrong audience" from "unknown signing key" would hand
        # an attacker a free oracle for probing the realm's configuration; the
        # specific reason is logged server-side instead, where the operator can
        # read it and the caller cannot.
        message="Missing or invalid bearer token.",
    ),
    # --- Request-shaped failures, decided before any network call -----------
    "INVALID_URL": ErrorSpec(
        status_code=400,
        retryable=False,
        message="Not a parseable LinkedIn profile URL.",
    ),
    # --- LinkedIn session state ---------------------------------------------
    #
    # Two codes, one status, and collapsing them would destroy the only thing
    # the caller can act on: NO_SESSION means "store one", SESSION_EXPIRED
    # means "the one you stored is dead, store another". A single code would
    # tell a caller who just supplied a cookie to supply it again.
    "NO_SESSION": ErrorSpec(
        status_code=428,
        retryable=False,
        message="No LinkedIn session is stored for this caller.",
    ),
    "SESSION_EXPIRED": ErrorSpec(
        status_code=428,
        retryable=False,
        message="Stored LinkedIn session is no longer valid.",
    ),
    # --- Upstream outcomes ---------------------------------------------------
    "PROFILE_NOT_FOUND": ErrorSpec(
        status_code=404,
        retryable=False,
        # Deliberately ambiguous between "does not exist" and "not visible to
        # this session". LinkedIn does not reliably distinguish the two, and
        # asserting either would be a claim this service cannot support.
        message="Profile does not exist or is not visible to this session.",
    ),
    "RATE_LIMITED": ErrorSpec(
        status_code=429,
        # The one retryable 4xx, which is exactly why `ErrorSpec` carries the
        # flag explicitly instead of deriving it from the status class.
        retryable=True,
        message="LinkedIn throttled this request.",
    ),
    "UPSTREAM_CHALLENGE": ErrorSpec(
        status_code=502,
        retryable=True,
        message="LinkedIn served a challenge or authwall instead of data.",
    ),
    "UPSTREAM_ERROR": ErrorSpec(
        status_code=502,
        retryable=True,
        # Never the upstream's own message. A Voyager error body can echo the
        # request, and the request carries the session cookie.
        message="LinkedIn could not be read.",
    ),
}


# --- What an UPSTREAM_ERROR (or any collapsed code) actually was ----------------
#
# `response-schema.md` has one row for "any other upstream failure", and it is
# right to: a caller can do nothing different with a 400, a 410, an unexpected
# status or an unparseable body. This service can, and `cause` is how it tells
# them apart WITHOUT adding rows the contract does not have. Operator-only, like
# `log_detail`, and never in a body.
#
# Declared here rather than in `app/linkedin/client.py` for one reason: a cause
# is compared for membership (`DECORATION_RETRY_CAUSES`), so a one-character
# drift would silently drop a case out of a whitelist and nothing would say so.
# Validated in `ApiError` exactly the way `code` is validated against
# `ERROR_SPECS` — an unknown cause is a KeyError at the raise site, not a
# mystery six weeks later.

#: LinkedIn refused the request as malformed. What a withdrawn or revised
#: ``decorationId`` looks like.
CAUSE_BAD_REQUEST = "bad-request"
#: The endpoint is gone. Also what a withdrawn decoration looks like.
CAUSE_GONE = "gone"
#: A 200 whose body is not the collection envelope the client can read — an
#: unparseable body, a non-object payload, a missing ``*elements``.
CAUSE_MALFORMED_BODY = "malformed-body"
#: Any other status. A 500 or a 503 is a fact about LinkedIn, not about the
#: request that was sent.
CAUSE_UNEXPECTED_STATUS = "unexpected-status"
#: The call never completed: DNS, TLS, connection reset, timeout.
CAUSE_TRANSPORT = "transport"
#: The response was readable and describes a different member than the one
#: asked for. The one cause that is also **not retryable**.
CAUSE_MEMBER_MISMATCH = "member-mismatch"
#: **This codebase** raised, not LinkedIn. A classifier that threw, a fan-out
#: task that died. Kept distinct because it is emphatically not evidence that
#: anything about the request was wrong, and must never buy a retry.
CAUSE_CLIENT_BUG = "client-bug"
#: The identity provider could not be reached to validate a token.
CAUSE_IDP_UNREACHABLE = "idp-unreachable"
#: The profile fetch as a whole exceeded its deadline.
CAUSE_DEADLINE = "deadline"

#: Every value ``ApiError.cause`` may take. A raise site inventing one fails.
ERROR_CAUSES: frozenset[str] = frozenset(
    {
        CAUSE_BAD_REQUEST,
        CAUSE_GONE,
        CAUSE_MALFORMED_BODY,
        CAUSE_UNEXPECTED_STATUS,
        CAUSE_TRANSPORT,
        CAUSE_MEMBER_MISMATCH,
        CAUSE_CLIENT_BUG,
        CAUSE_IDP_UNREACHABLE,
        CAUSE_DEADLINE,
    }
)


#: Every error this API returns is uncacheable, and that is a correctness rule
#: rather than a performance one.
#:
#: These responses are **caller-specific**. `428 NO_SESSION` means "*you* have
#: not stored a session"; `401 UNAUTHENTICATED` means "*your* token is bad".
#: Nothing about them varies by anything a shared cache keys on except the
#: bearer token, and caches do not key on Authorization. Deployed, this service
#: sits behind host nginx, an OCI load balancer and a Cloudflare edge — so one
#: caller's 428 cached and replayed to a second caller would tell somebody with
#: a perfectly good session to go and store one, and a cached 401 would lock out
#: a valid token for the life of the entry.
#:
#: The success path sets this on its own response. Setting it HERE rather than
#: per route is what makes it true for the paths nobody enumerated: an ApiError
#: raised in a dependency, Starlette's own 404 and 405, a validation 422, and
#: the last-resort 500 all render through this module and inherit it.
NO_STORE = {"Cache-Control": "no-store"}

#: RFC 6750 requires a challenge on a 401 from a bearer-token resource, so it is
#: a property of the ``UNAUTHENTICATED`` row rather than of one helper that
#: happens to remember it. `ApiError` applies it to every 401 it renders; a call
#: site with something more specific to say (``error="invalid_token"``, on a
#: credential that was actually presented and rejected) passes its own and wins.
WWW_AUTHENTICATE = "WWW-Authenticate"
DEFAULT_CHALLENGE = "Bearer"


def _uncacheable(headers: Mapping[str, str] | None) -> dict[str, str]:
    """``headers`` plus ``Cache-Control: no-store``.

    An explicit `Cache-Control` from a call site wins — there is no such call
    site today, and if one appears it should be a deliberate act rather than
    something this helper silently overrules.
    """
    merged = dict(headers) if headers else {}
    if not any(name.lower() == "cache-control" for name in merged):
        merged.update(NO_STORE)
    return merged


class ApiError(Exception):
    """An outcome the API states in the typed envelope rather than raising into.

    Carrying the code (not the status) as the identity keeps call sites honest:
    a route says *what went wrong*, and this module owns what that means on the
    wire.

    ===========================================================================
    ``retryable`` IS A PROPERTY OF THE RESPONSE, NOT OF THE CODE
    ===========================================================================

    :data:`ERROR_SPECS` still fixes the **default** for every code, and that
    default is ``response-schema.md``'s column, untouched. What story 8 adds is
    a per-*instance* override, because one real condition needs it: a response
    naming a different member than the caller asked for is an ``UPSTREAM_ERROR``
    — the table marks that retryable — and it is emphatically not. A vanity URL
    that now belongs to somebody else does not stop belonging to them because
    you asked twice, and under an unbounded cache `retryable: true` means the
    stale record is republished for ever while nobody is ever told the URL
    changed meaning.

    Three rules keep this from becoming a hole in the taxonomy:

    * **It can only narrow.** Making a non-retryable code retryable is refused
      here, loudly, because that is the direction that hides a permanent
      failure behind a cached 200 — the exact bug this codebase has already had
      to fix once. The story's Boundaries put that change behind Ask First; this
      raise is what makes "behind Ask First" mean something at runtime.
    * **Every gate reads the effective value.** :attr:`retryable`, never
      ``.spec.retryable``. An override that some gate does not consult changes
      the body and not the behaviour, which is worse than no override at all —
      ``tests/test_cache.py`` greps for that mistake.
    * **It is used only at named raise sites**, pinned by a test, so it stays a
      reclassification of two specific guards rather than a general escape
      hatch a later call site can reach for.

    ``cause`` is the other half of the same story. ``_classify`` in
    :mod:`app.linkedin.client` collapses a 400, a 410, an unexpected status and
    an unparseable body into one ``UPSTREAM_ERROR``; ``cause`` says which,
    so the decorated-core retry can fire for a refused decoration and not for a
    LinkedIn outage. Like ``log_detail`` it is **operator-only** and never
    reaches a client-facing body — and like ``code`` it is checked against a
    closed set (:data:`ERROR_CAUSES`), because it is compared for membership and
    a typo would otherwise drop a case out of a whitelist in silence.
    """

    def __init__(
        self,
        code: str,
        *,
        message: str | None = None,
        headers: Mapping[str, str] | None = None,
        log_detail: str | None = None,
        retryable: bool | None = None,
        cause: str | None = None,
    ) -> None:
        if code not in ERROR_SPECS:
            raise KeyError(f"{code!r} is not in ERROR_SPECS — add it to the taxonomy first")
        self.code = code
        self.spec = ERROR_SPECS[code]
        if retryable and not self.spec.retryable:
            raise ValueError(
                f"{code} is non-retryable in response-schema.md and a raise site "
                "may not make it retryable. Telling a caller to repeat a request "
                "that cannot succeed is how a permanent failure — a dead session "
                "above all — ends up hidden behind a stale cached answer."
            )
        if cause is not None and cause not in ERROR_CAUSES:
            raise KeyError(
                f"{cause!r} is not in ERROR_CAUSES. A cause is compared for "
                "membership — `DECORATION_RETRY_CAUSES` is a whitelist — so an "
                "unregistered one would silently mean 'not that case'."
            )
        self.message = message or self.spec.message
        self.headers = dict(headers) if headers else None
        if self.spec.status_code == 401:
            # RFC 6750, applied to the row rather than remembered per call site.
            # `ApiError("UNAUTHENTICATED")` raised anywhere — including by a
            # route that never heard of `unauthenticated()` — is a conformant
            # 401, and a caller that reads the challenge to decide whether to
            # re-authenticate gets one every time.
            self.headers = self.headers or {}
            if not any(name.lower() == WWW_AUTHENTICATE.lower() for name in self.headers):
                self.headers[WWW_AUTHENTICATE] = DEFAULT_CHALLENGE
        #: ``None`` means "whatever the table says". Read through
        #: :attr:`retryable`, never directly.
        self._retryable_override = retryable
        #: Operator-facing reason. Deliberately NOT part of the response body.
        self.log_detail = log_detail
        #: Operator-facing discriminator within one code. Also NOT in the body.
        self.cause = cause
        detail = self.log_detail or self.message
        super().__init__(f"{code}[{cause}]: {detail}" if cause else f"{code}: {detail}")

    @property
    def retryable(self) -> bool:
        """Whether repeating *this* request could succeed. **The only gate.**

        Anything that decides behaviour on retryability reads this. The code's
        default lives on :attr:`spec` and is the input to this answer, not the
        answer.
        """
        if self._retryable_override is None:
            return self.spec.retryable
        return self._retryable_override

    def to_response(self) -> JSONResponse:
        body = ErrorEnvelope(
            error=ErrorDetail(
                code=self.code,
                message=self.message,
                # The effective value, so the wire and the cache gate can never
                # disagree about whether the caller should try again.
                retryable=self.retryable,
            )
        )
        return JSONResponse(
            status_code=self.spec.status_code,
            content=body.model_dump(),
            headers=_uncacheable(self.headers),
        )


def unauthenticated(
    *,
    log_detail: str,
    www_authenticate: str = "Bearer",
) -> ApiError:
    """Build the 401 every authentication failure lands on.

    A *raiser* rather than a raise so the call site reads ``raise
    unauthenticated(...)`` and static analysis still sees the control flow.

    ``WWW-Authenticate`` is required by RFC 6750 on a 401 from a bearer-token
    resource. It carries the scheme and, when a token was actually presented
    and rejected, ``error="invalid_token"`` — both are constants, so nothing
    about the specific failure leaks through this header either.
    """
    return ApiError(
        "UNAUTHENTICATED",
        headers={"WWW-Authenticate": www_authenticate},
        log_detail=log_detail,
    )


# --- Responses this story does not have a taxonomy code for ------------------
#
# `ERROR_SPECS` above is the spec table and nothing else. But a response
# leaving in FastAPI's default `{"detail": ...}` shape breaks the wire contract
# just as badly as a wrong code does, and three of them can happen without any
# route existing: a 404 for an unknown path, a 405 for a wrong method, and a
# 500 for a bug. Stories 5-8 add the fourth the moment they declare a query
# parameter: a 422 from request validation.
#
# So the shape is guaranteed here, separately from the taxonomy. These are
# NOT taxonomy rows and must not be treated as if they were: story 8 routes
# each reachable case to a real code from `response-schema.md` (`INVALID_URL`
# for the profile-URL validation case, `UPSTREAM_ERROR` for the failure case)
# and deletes what it supersedes. What it must NOT do is delete the fallback
# itself — a path with no route at all can still 404, and that 404 still has
# to wear the envelope.
#
# Story 8 dropped no row, and the 400 is why. It looked superseded — every 400
# this API *raises* is `INVALID_URL` from the profile route — and removing it was
# reproduced as a live regression: FastAPI raises `HTTPException(400, "There was
# an error parsing the body")` from its own body-read guard
# (`fastapi/routing.py`), so `PUT /api/v1/session` with an unparseable body
# rendered `code: "INTERNAL_ERROR"` at a 400. That is the fallback's whole job —
# statuses reachable without any route or any raise site of ours agreeing to
# them — and "nothing raises this" is exactly the reasoning it exists to
# distrust. Every row here stays until something proves the status unreachable,
# which is a proof about a framework's internals rather than about this code.
FALLBACK_CODES: dict[int, str] = {
    # FastAPI's body-read guard, not ours. See above.
    400: "BAD_REQUEST",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    422: "INVALID_REQUEST",
    # Story 5: this API's OWN datastore is unreachable. `response-schema.md`
    # has no row for that — every code in its table is a statement about the
    # request or about LinkedIn — and inventing one would put the wire contract
    # and `ERROR_SPECS` out of agreement. A 503 in the fallback set says the
    # true thing without pretending to be taxonomy: the service is temporarily
    # unable to answer, and `retryable` derives to True.
    503: "SERVICE_UNAVAILABLE",
}
FALLBACK_CODE = "INTERNAL_ERROR"

#: What a caller is told when the failure has no code yet. Never the exception
#: text: on the 500 path that is a stack-trace fragment, and on the 422 path it
#: is a pydantic dump that can echo a submitted LinkedIn session cookie back
#: into the response body.
FALLBACK_MESSAGES: dict[int, str] = {
    # Deliberately ours rather than FastAPI's "There was an error parsing the
    # body", which names a framework internal to a caller who cannot act on it.
    400: "The request could not be understood.",
    404: "No such resource.",
    405: "That method is not allowed on this resource.",
    422: "The request failed validation.",
    # Never the driver's message: a psycopg error can quote the failing
    # statement, and the upsert's parameters carry the stored ciphertext.
    503: "The service could not reach its datastore. Try again shortly.",
}
FALLBACK_MESSAGE = "The server failed to handle this request."


def envelope(
    status_code: int,
    *,
    code: str | None = None,
    message: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Render any status as the typed envelope.

    ``retryable`` is derived rather than chosen: a 5xx means the server failed
    and repeating the identical request may well work, a 4xx means the request
    itself was wrong and repeating it verbatim cannot help.

    **This guess is only ever used for the fallback codes**, which have no spec
    row and cannot have one — see :data:`FALLBACK_CODES`. A taxonomy code never
    comes through here: it renders through :meth:`ApiError.to_response`, which
    reads the effective per-response value. That is why the guess can be as
    crude as a status comparison and why `RATE_LIMITED`, a 429 that *is*
    retryable, is not a counter-example to it.
    """
    body = ErrorEnvelope(
        error=ErrorDetail(
            code=code or FALLBACK_CODES.get(status_code, FALLBACK_CODE),
            message=message or FALLBACK_MESSAGES.get(status_code, FALLBACK_MESSAGE),
            retryable=status_code >= 500,
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(),
        headers=_uncacheable(headers),
    )


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render :class:`ApiError` as the typed envelope.

    Typed as ``Exception`` because that is the signature Starlette's handler
    registry declares. The narrowing is raised, not asserted: `python -O`
    strips `assert`, and under it a bare assert would fall through to an
    `AttributeError` — a 500 — instead of failing where the mistake is.
    """
    if not isinstance(exc, ApiError):  # pragma: no cover - registry mismatch
        raise TypeError(f"api_error_handler received {type(exc).__name__}, not ApiError")
    return exc.to_response()


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render Starlette's own 400/404/405/redirect-slash errors as the envelope.

    **A curated message wins over ``exc.detail``.** Starlette always populates
    `detail` — with "Not Found", "Method Not Allowed", or FastAPI's "There was
    an error parsing the body" — so preferring it made every entry in
    :data:`FALLBACK_MESSAGES` dead code that looked alive. Worse, the one that
    reached a caller most often named a framework internal at a status the
    caller was supposed to be able to act on.

    So a status this module has written a message for uses that message, and
    `detail` is the fallback for the statuses it has not — which is what keeps
    an unforeseen `HTTPException(501)` from losing the only description it has.
    `detail` is written by this codebase or by Starlette and never by the
    caller, so surfacing it there is safe; it is used only when it is a plain
    string.
    """
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover
        raise TypeError(f"http_exception_handler received {type(exc).__name__}")
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail else None
    message = None if exc.status_code in FALLBACK_MESSAGES else detail
    return envelope(exc.status_code, message=message, headers=exc.headers)


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render request-validation failures as the envelope.

    The pydantic error list is deliberately dropped from the body rather than
    summarised into it. `PUT /api/v1/session` takes a LinkedIn session cookie
    in its body (story 5), and a validation error report echoes the offending
    input — which would put a live session cookie in a response body and in
    every log that captures it. The detail is logged at INFO instead.
    """
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        raise TypeError(f"validation_exception_handler received {type(exc).__name__}")
    logger.info("Request validation failed at %s", request.url.path)
    return envelope(422)


async def datastore_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render :class:`app.db.DatastoreUnavailable` as a typed 503.

    Without this, any Postgres hiccup during a session request becomes a 500
    whose code is ``INTERNAL_ERROR`` — which tells a caller their request was
    the problem when it was not, and tells them nothing about whether retrying
    helps. A 503 says both.

    The exception carries only the psycopg exception's *class name*; the message
    was already logged at the store. Nothing about the statement or its
    parameters reaches the caller.
    """
    logger.error("Datastore unavailable at %s: %s", request.url.path, exc)
    return envelope(503)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort: a bug becomes a typed 500, never a naked one.

    CAP-6 says no unhandled exception reaches the client. This is what makes
    that true for the exceptions nobody predicted — which is, by definition,
    the set that matters. The traceback goes to the log, and nothing about it
    goes to the caller.
    """
    logger.exception("Unhandled exception at %s", request.url.path)
    return envelope(500)


def install_error_handlers(application: FastAPI) -> None:
    """Wire the envelope into an app.

    Called from :func:`app.main.create_app`. Without it an ``ApiError`` raised
    in a dependency becomes a naked 500, which is the one thing CAP-6 forbids —
    so :mod:`tests.test_auth` asserts the wiring rather than trusting it, for
    every one of these handlers.
    """
    # Imported here rather than at module scope: `app.db` imports `app.config`,
    # and `app.errors` must stay importable by anything, including the config
    # tests that deliberately run with a broken environment.
    from app.db import DatastoreUnavailable

    application.add_exception_handler(ApiError, api_error_handler)
    application.add_exception_handler(DatastoreUnavailable, datastore_unavailable_handler)
    application.add_exception_handler(StarletteHTTPException, http_exception_handler)
    application.add_exception_handler(RequestValidationError, validation_exception_handler)
    # Starlette routes bare `Exception` to ServerErrorMiddleware rather than to
    # the normal handler table, which is why it is registered separately from
    # the three above and why it still re-raises after responding (so the
    # traceback reaches the server log).
    application.add_exception_handler(Exception, unhandled_exception_handler)


#: OpenAPI ``responses`` fragment for routes that can answer 401.
#:
#: Attached at the ``/api/v1`` router level so the generated document — which
#: is the README's API documentation — shows the real error body rather than
#: FastAPI's default ``{"detail": "..."}``.
UNAUTHENTICATED_RESPONSE: dict[int | str, dict[str, Any]] = {
    401: {
        "model": ErrorEnvelope,
        "description": "Missing, malformed, expired, wrong-issuer or wrong-audience token.",
    }
}


#: The other answer the authentication boundary can give, and the reason story 8
#: exists: *this service* could not reach the identity provider.
#:
#: That used to be a 401, which tells a caller holding a perfectly good token to
#: stop asking — a statement about their credential that this service is in no
#: position to make when it could not read the realm's key set. It is a 502 with
#: `retryable: true`, and it is reachable from every route under `/api/v1`,
#: which is why it is attached at the router beside the 401 rather than
#: enumerated per route.
#: The prose, separately, because a route-level `responses` entry for 502
#: REPLACES the router-level one rather than merging with it — FastAPI merges by
#: status key. A route that documents its own 502 must therefore restate this
#: meaning, and restating it by hand is how the two drift apart.
IDP_UNAVAILABLE_DESCRIPTION = (
    "A 502 is also how the authentication boundary answers when the identity "
    "provider could not be reached to validate the token: retryable, and the "
    "token is not being refused."
)

IDP_UNAVAILABLE_RESPONSE: dict[int | str, dict[str, Any]] = {
    502: {"model": ErrorEnvelope, "description": IDP_UNAVAILABLE_DESCRIPTION}
}


def error_responses(
    *codes: str, addenda: Mapping[int, str] | None = None
) -> dict[int | str, dict[str, Any]]:
    """OpenAPI ``responses`` fragment for the taxonomy codes a route can answer.

    Built from :data:`ERROR_SPECS` rather than written per route, so the status
    a route documents and the status it actually returns cannot disagree — the
    generated document is the README's API documentation, and a documented 403
    for a route that answers 428 is worse than no documentation at all.

    Codes sharing a status are merged into one entry with both meanings listed,
    because OpenAPI keys responses by status and the two 428s are exactly that
    case.

    Both failure modes are loud, and neither used to be. A code absent from the
    taxonomy raised a bare ``KeyError`` naming nothing useful; and calling this
    with no codes returned ``{}``, so a route asking to document its failures
    silently documented none — the exact outcome the helper exists to prevent,
    reached by a typo.

    ``addenda`` appends prose to the entry for one status, for the thing the
    taxonomy cannot say by itself: that a status means something on THIS route
    which the code's own message does not cover. It is a parameter rather than a
    caller mutating the returned dict in place, because a status keyed into a
    result and mutated at module scope raises ``KeyError`` at import the moment
    the code producing it is dropped from the call above — a broken deploy for
    an edit to a docstring. Here the same mistake is a ``KeyError`` naming the
    status, raised from the one function that knows which statuses exist.
    """
    if not codes:
        raise ValueError(
            "error_responses() needs at least one code — an empty result would "
            "silently document a route as having no failure modes"
        )

    merged: dict[int | str, dict[str, Any]] = {}
    for code in codes:
        if code not in ERROR_SPECS:
            raise KeyError(
                f"{code!r} is not in ERROR_SPECS. Every code this API documents "
                "must come from the table in response-schema.md; add the row "
                "there and here before documenting it on a route."
            )
        spec = ERROR_SPECS[code]
        entry = merged.setdefault(
            spec.status_code, {"model": ErrorEnvelope, "description": ""}
        )
        line = f"`{code}` — {spec.message}"
        entry["description"] = (
            f"{entry['description']} {line}".strip() if entry["description"] else line
        )

    for status, addendum in (addenda or {}).items():
        if status not in merged:
            raise KeyError(
                f"there is no {status} entry to append to — the codes given "
                f"({', '.join(codes)}) produce {sorted(merged)}. A route cannot "
                "document a meaning for a status it does not answer."
            )
        merged[status]["description"] = (
            f"{merged[status]['description']} {addendum}".strip()
        )
    return merged
