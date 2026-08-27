"""The typed error envelope from ``response-schema.md``.

This story implements exactly one code — ``UNAUTHENTICATED`` — but in the shape
story 8 will adopt for the whole taxonomy. The generalisation story 8 performs
is adding rows to :data:`ERROR_SPECS`; it should not have to change this
module's structure, the wire shape, or any call site written against it.

The wire shape is fixed and is not negotiable per code::

    {"error": {"code": "...", "message": "...", "retryable": false}}

``retryable`` exists so a client can decide without parsing prose, which is why
it lives in the spec table next to the status rather than being inferred from
the status class.
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


#: The taxonomy, keyed by code. Story 8 adds the remaining seven rows from
#: ``response-schema.md``; nothing else about this module needs to change.
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
}


class ApiError(Exception):
    """An outcome the API states in the typed envelope rather than raising into.

    Carrying the code (not the status) as the identity keeps call sites honest:
    a route says *what went wrong*, and this module owns what that means on the
    wire.
    """

    def __init__(
        self,
        code: str,
        *,
        message: str | None = None,
        headers: Mapping[str, str] | None = None,
        log_detail: str | None = None,
    ) -> None:
        if code not in ERROR_SPECS:
            raise KeyError(f"{code!r} is not in ERROR_SPECS — add it to the taxonomy first")
        self.code = code
        self.spec = ERROR_SPECS[code]
        self.message = message or self.spec.message
        self.headers = dict(headers) if headers else None
        #: Operator-facing reason. Deliberately NOT part of the response body.
        self.log_detail = log_detail
        super().__init__(f"{code}: {self.log_detail or self.message}")

    def to_response(self) -> JSONResponse:
        body = ErrorEnvelope(
            error=ErrorDetail(
                code=self.code,
                message=self.message,
                retryable=self.spec.retryable,
            )
        )
        return JSONResponse(
            status_code=self.spec.status_code,
            content=body.model_dump(),
            headers=self.headers,
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
# `ERROR_SPECS` above is the spec table and nothing else — story 8 fills in its
# remaining seven rows. But a response leaving in FastAPI's default
# `{"detail": ...}` shape breaks the wire contract just as badly as a wrong
# code does, and three of them can happen today without any route existing:
# a 404 for an unknown path, a 405 for a wrong method, and a 500 for a bug.
# Stories 5-8 add the fourth the moment they declare a query parameter: a
# 422 from request validation.
#
# So the shape is guaranteed here, separately from the taxonomy. These are
# NOT taxonomy rows and must not be treated as if they were: story 8 replaces
# each with a real code from `response-schema.md` (`INVALID_URL` for the
# validation case, `UPSTREAM_ERROR` for the failure case) and deletes what it
# supersedes. What it must NOT do is delete the fallback itself.
FALLBACK_CODES: dict[int, str] = {
    400: "BAD_REQUEST",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    422: "INVALID_REQUEST",
}
FALLBACK_CODE = "INTERNAL_ERROR"

#: What a caller is told when the failure has no code yet. Never the exception
#: text: on the 500 path that is a stack-trace fragment, and on the 422 path it
#: is a pydantic dump that can echo a submitted `li_at` cookie straight back
#: into the response body.
FALLBACK_MESSAGES: dict[int, str] = {
    400: "The request could not be understood.",
    404: "No such resource.",
    405: "That method is not allowed on this resource.",
    422: "The request failed validation.",
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
    itself was wrong and repeating it verbatim cannot help. Story 8 overrides
    this per code — `RATE_LIMITED` is a 429 that *is* retryable — which is
    exactly why :class:`ErrorSpec` carries the flag explicitly and this
    fallback only guesses for codes that have no spec row yet.
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
        headers=dict(headers) if headers else None,
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
    """Render Starlette's own 404/405/redirect-slash errors as the envelope."""
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover
        raise TypeError(f"http_exception_handler received {type(exc).__name__}")
    # `exc.detail` is written by this codebase or by Starlette, never by the
    # caller, so it is safe to surface — but only when it is a plain string.
    message = exc.detail if isinstance(exc.detail, str) and exc.detail else None
    return envelope(exc.status_code, message=message, headers=exc.headers)


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render request-validation failures as the envelope.

    The pydantic error list is deliberately dropped from the body rather than
    summarised into it. `PUT /api/v1/session` takes a `li_at` cookie in its
    body (story 5), and a validation error report echoes the offending input —
    which would put a live LinkedIn session cookie in a response body and in
    every log that captures it. The detail is logged at INFO instead.
    """
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        raise TypeError(f"validation_exception_handler received {type(exc).__name__}")
    logger.info("Request validation failed at %s", request.url.path)
    return envelope(422)


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
    application.add_exception_handler(ApiError, api_error_handler)
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
