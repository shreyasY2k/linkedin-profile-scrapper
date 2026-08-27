"""``PUT`` and ``GET /api/v1/session`` — the caller's own LinkedIn session.

The first real routes on the ``/api/v1`` seam, so they are also the first
request that actually exercises the authentication boundary story 3 attached to
the router. Neither handler declares a security dependency of its own; both
inherit it (see :mod:`app.api.v1`).

===============================================================================
THE ONE RULE
===============================================================================

**The stored cookie value is never returned.** Not by ``GET``, not by ``PUT``'s
confirmation, not under a flag, a query parameter, a debug mode or an error
path. ``GET`` answers presence and last-use validity, which is what a caller
needs to know; answering with the value would make this a credential-disclosure
endpoint that happens to require a token.

Three separate things enforce that, and none of them is "the handler remembers":

1. :class:`app.vault.SessionState` has no field that could carry it.
2. ``response_model=SessionResponse`` makes FastAPI filter the response to
   exactly the declared fields, so a value added to a returned object by some
   later edit is dropped before serialisation rather than published.
3. The plaintext never leaves :mod:`app.vault` on this path — ``state()``
   decrypts to check the row is readable and discards it.

===============================================================================
SUBJECT COMES FROM THE TOKEN
===============================================================================

The vault key is ``claims["sub"]`` from the verified token. The request body is
``extra="forbid"`` precisely so that a caller who sends ``{"li_at": "...",
"subject": "someone-else"}`` gets a 422 rather than a silently ignored field
that looks as though it might have worked.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from app.auth import require_claims
from app.errors import (
    IDP_UNAVAILABLE_RESPONSE,
    ApiError,
    ErrorEnvelope,
    error_responses,
)
from app.linkedin.client import VoyagerClient
from app.vault import SessionState, SessionVault
from app.vault import vault as _process_vault

logger = logging.getLogger(__name__)


def no_store(response: Response) -> None:
    """Mark every response from this router uncacheable.

    These are credential-*status* responses — "this caller has a working
    LinkedIn session" — travelling through host nginx and, deployed, a
    Cloudflare edge. Nothing here varies by anything a cache keys on except the
    bearer token, which caches do not key on, so a cached 200 for one subject
    served to another is a disclosure with no code change required to cause it.

    Story 7 is explicitly a caching story, which makes stating this now cheaper
    than remembering it later.
    """
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(tags=["session"], dependencies=[Depends(no_store)])


def get_vault() -> SessionVault:
    """The process-wide vault, as a dependency.

    A dependency rather than a module global read inside the handler, so the
    API tests can substitute an in-memory store through
    ``app.dependency_overrides`` and run the whole endpoint offline — the suite
    has to pass under ``docker run --network none``.
    """
    return _process_vault


#: Verifies a freshly stored cookie. ``True`` it works, ``False`` LinkedIn
#: refused it, ``None`` we could not tell.
SessionVerifier = Callable[[str], Awaitable[bool | None]]


async def check_stored_session(cookie: str) -> bool | None:
    """One ``me`` call: does this cookie actually work?

    This is the writer that makes ``last_use_ok`` real. Without it the field is
    permanently ``null`` while ``response-schema.md``, the README and this
    module's own field descriptions all promise ``GET`` reports last-use
    validity — a documented value that can never exist.
    ``app/linkedin/client.py:check_session`` was written for exactly this call
    site and says so.

    It is also the answer to the only question a caller actually has after
    pasting a cookie: *did that work?* Finding out at the first profile request
    instead means finding out somewhere the failure looks like a bug.

    **Three outcomes, and the third is not a failure.** ``True`` — LinkedIn
    named the session's owner. ``False`` — LinkedIn refused the session, so the
    caller is told immediately that the value they pasted is dead. ``None`` —
    a throttle, a timeout, a DNS failure, LinkedIn's bot status: none of those
    is evidence about the cookie, and recording ``last_use_ok: false`` for them
    would libel a perfectly good session.

    A **wall in place of an answer counts as a refusal here**, and only here.
    ``me`` asks LinkedIn who is holding this cookie; an authwall in reply to
    that question is about the cookie, and the client classifies it
    ``SESSION_EXPIRED`` accordingly — see the branch in
    ``app/linkedin/client.py:_classify``. The same page on a profile fetch means
    nothing of the sort, which is why the split is by resource and lives there.
    This is what closes the story-4 gap where a dead cookie was stored, verified
    as "could not tell", and only discovered as permanent staleness later.

    This never raises. The credential is already stored by the time it runs, and
    a verification that could fail the request would mean a LinkedIn outage
    losing a cookie the caller pasted correctly. It is best-effort by
    construction, which is what the broad ``except`` at the end is for.

    Note it does no profile fetching — ``me`` describes the session's own
    owner and touches nobody else's data.
    """
    try:
        client = VoyagerClient(cookie)
        await client.check_session()
        return True
    except ApiError as exc:
        if exc.code == "SESSION_EXPIRED":
            logger.info("A freshly stored session was refused by LinkedIn")
            return False
        # RATE_LIMITED, UPSTREAM_CHALLENGE, UPSTREAM_ERROR, PROFILE_NOT_FOUND:
        # facts about LinkedIn or about the network, not about the cookie. A
        # challenge reaching here now means the 999 bot status specifically —
        # a statement about this container's IP — since a wall served in place
        # of `me`'s answer is classified as expiry above.
        logger.info(
            "Could not verify a stored session (%s); leaving validity unknown",
            exc.code,
        )
        return None
    except Exception:
        # Deliberately wide, and only reachable through a bug in this codebase.
        # `logger.exception` puts the traceback where an operator sees it; the
        # caller's stored session is untouched either way.
        logger.exception("Verifying a stored session raised unexpectedly")
        return None


def get_session_verifier() -> SessionVerifier:
    """The verifier, as a dependency, so the suite never reaches LinkedIn.

    Every test overrides this. `tests/test_linkedin_client.py` already proves
    the Voyager call itself against synthetic fixtures; what the API tests need
    to exercise is what this route does with each of the three answers.
    """
    return check_stored_session


class SessionRequest(BaseModel):
    """``PUT`` body. Fixed by ``response-schema.md``: ``{"li_at": "..."}``."""

    # Unknown keys are refused rather than ignored. See the module docstring:
    # the important one to refuse is a caller-supplied subject.
    model_config = ConfigDict(extra="forbid")

    li_at: str = Field(
        description=(
            "Your LinkedIn `li_at` session cookie. Stored encrypted and bound "
            "to your token's subject; never returned by any endpoint."
        ),
        # No `examples=`, deliberately. An example on this field would put a
        # cookie-shaped string in the OpenAPI document, in `/docs`, and in
        # Swagger's pre-filled "Try it out" body — and the story's boundaries
        # say the cookie appears in no OpenAPI example. There is no such thing
        # as a safe example for this field.
    )

    # No length or character constraints here on purpose. Shape validation is
    # `LinkedInSession` in `app/linkedin/client.py` — story 4's, and the same
    # rules that decide what may go into a request header. Duplicating them as
    # pydantic constraints would give two answers to one question, and the
    # pydantic one arrives as a 422 whose detail this API deliberately drops,
    # so the caller would learn less about what they got wrong, not more.


class SessionResponse(BaseModel):
    """Presence and validity. Every field a caller is allowed to see.

    Note what is absent and must stay absent: any field capable of carrying the
    cookie. ``tests/test_session_api.py`` asserts the property name set of this
    schema, so adding one is a deliberate act that fails a test.
    """

    stored: bool = Field(description="Whether a LinkedIn session is stored for you.")
    stored_at: datetime | None = Field(
        default=None, description="When the stored session was supplied."
    )
    last_used_at: datetime | None = Field(
        default=None,
        description="When the stored session was last used against LinkedIn.",
    )
    last_use_ok: bool | None = Field(
        default=None,
        description=(
            "Whether that last use succeeded. `null` means the session has been "
            "stored but not yet used."
        ),
    )

    @classmethod
    def of(cls, state: SessionState) -> "SessionResponse":
        return cls(
            stored=state.stored,
            stored_at=state.stored_at,
            last_used_at=state.last_used_at,
            last_use_ok=state.last_use_ok,
        )


#: Documented failures. `NO_SESSION` and `SESSION_EXPIRED` are both 428 and both
#: reachable from these routes: an empty or malformed cookie on `PUT`, and a row
#: that will not decrypt (or is bound to another subject) on `GET`.
SESSION_ERRORS: dict[int | str, dict[str, Any]] = {
    **error_responses("NO_SESSION", "SESSION_EXPIRED"),
    # Not taxonomy rows — see `app/errors.py`. Documented for the same reason
    # the profile route documents them: a status a route can answer and does not
    # document is a status a client will meet by surprise.
    #
    # 422 in particular has to be stated HERE rather than left to FastAPI. `PUT`
    # takes a request body, so FastAPI generates a 422 of its own referencing
    # `HTTPValidationError` — which is the one shape this API never returns.
    # The handler converts it to the typed envelope at runtime, so an
    # undeclared 422 leaves the published document contradicting the wire.
    422: {
        "model": ErrorEnvelope,
        "description": (
            "`INVALID_REQUEST` — the body is not `{\"li_at\": \"…\"}`: a missing "
            "or non-string `li_at`, or an unexpected field."
        ),
    },
    # Not a taxonomy code — `response-schema.md` has no row for this API's own
    # datastore being down.
    503: {
        "model": ErrorEnvelope,
        "description": "`SERVICE_UNAVAILABLE` — the session store could not be reached.",
    },
    # A 502 from a session route is never about LinkedIn: neither handler's
    # ANSWER depends on it (`PUT` stores first and verifies best-effort, and a
    # verification that cannot reach a verdict leaves `last_use_ok` null rather
    # than failing). It is the authentication boundary.
    #
    # Reused, not restated. A route-level entry replaces the router-level one
    # for the same status, so this has to be here — but writing the sentence a
    # second time is how the two come to say different things.
    **IDP_UNAVAILABLE_RESPONSE,
}


# Both handlers are `async def`, and every vault call goes through
# `asyncio.to_thread`. The vault talks to Postgres through a blocking driver, so
# a bare `async def` would park the event loop on a database round-trip; a plain
# `def` would work too, but `PUT` has to await the LinkedIn verification, and
# having the two handlers disagree about their colour is how someone later adds
# a blocking call to the wrong one.


@router.put(
    "/session",
    response_model=SessionResponse,
    responses=SESSION_ERRORS,
    summary="Store or replace your LinkedIn session",
    description=(
        "Stores the supplied `li_at` encrypted at rest, bound to your token's "
        "subject, replacing any session you had stored. The value is never "
        "returned by this or any other endpoint, and there is no delete "
        "endpoint — overwrite is the whole lifecycle.\n\n"
        "The stored session is then checked against LinkedIn once, so "
        "`last_use_ok` in the response tells you immediately whether the cookie "
        "you supplied actually works. `null` means the check could not reach a "
        "verdict (a throttle, a challenge, a network failure) — it is not a "
        "statement about your cookie. The session is stored either way."
    ),
)
async def put_session(
    payload: SessionRequest,
    claims: dict[str, Any] = Depends(require_claims),
    vault: SessionVault = Depends(get_vault),
    verify: SessionVerifier = Depends(get_session_verifier),
) -> SessionResponse:
    subject = claims["sub"]

    # Stored FIRST, and the ordering is the safety property: a verification
    # that hangs, throws or finds LinkedIn throttling must never cost the
    # caller the credential they just pasted correctly.
    state = await asyncio.to_thread(vault.store, subject, payload.li_at)

    verdict = await verify(payload.li_at)
    if verdict is None:
        # Could not tell. Leave `last_use_ok` null rather than guessing — null
        # means "not yet used", which is exactly true.
        return SessionResponse.of(state)

    # `stored_at` scopes the write, so a concurrent PUT that replaced the row
    # while this check was in flight keeps its own (null) verdict rather than
    # inheriting this one. `None` back means exactly that happened.
    verified = await asyncio.to_thread(
        vault.record_use, subject, ok=verdict, stored_at=state.stored_at
    )
    return SessionResponse.of(verified or state)


@router.get(
    "/session",
    response_model=SessionResponse,
    responses=SESSION_ERRORS,
    summary="Whether you have a LinkedIn session stored",
    description=(
        "Reports presence and last-use validity for your own stored session, "
        "and nothing else. Having no session stored is a successful response "
        "with `stored: false`, not an error."
    ),
)
async def get_session(
    claims: dict[str, Any] = Depends(require_claims),
    vault: SessionVault = Depends(get_vault),
) -> SessionResponse:
    return SessionResponse.of(await asyncio.to_thread(vault.state, claims["sub"]))
