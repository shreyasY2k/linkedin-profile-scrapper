"""``GET /api/v1/profile`` — the endpoint CAP-1 is graded on.

Everything the earlier stories built meets here, and this module does nothing
except join them in the right order:

1. **Parse the URL.** :func:`~app.linkedin.client.parse_profile_url` — pure, no
   I/O. A bad URL costs zero LinkedIn quota and zero database round-trips.
2. **Unlock the caller's own session.** :meth:`~app.vault.SessionVault.unlock`,
   keyed on the verified ``sub`` and nothing else. No session, no upstream call.
3. **Fetch.** Six live calls through :class:`~app.linkedin.client.VoyagerClient`.
4. **Record the outcome** against the session, which is what makes
   ``last_use_ok`` on ``GET /api/v1/session`` real for the normal path.
5. **Map** onto ``response-schema.md``, **cache** the answer, and return it.

===============================================================================
THE ORDER IS THE CONTRACT
===============================================================================

Two rows of the story's matrix are about things that must happen *before* an
upstream call, and both are satisfied structurally rather than by remembering:

* *Bad URL* → ``INVALID_URL`` / 400, "rejected before any upstream call".
* *No session* → ``NO_SESSION`` / 428, "before any upstream call".

Step 1 is a pure function and step 2 reads only this service's own datastore, so
by construction nothing reaches LinkedIn until both have passed.
``tests/test_profile_api.py`` asserts the fetcher was never invoked, which is
the only way to prove a negative like this.

===============================================================================
WHY THIS RETURNS A RESPONSE OBJECT RATHER THAN A MODEL
===============================================================================

The central promise of this story is that an unreadable field's key is
**omitted entirely** — not ``null``, not ``[]``. A pydantic response model
serialises a fixed key set, and getting omission out of one means relying on
``exclude_unset`` surviving FastAPI's validate-then-encode round trip. That is
a subtle mechanism guarding the one thing a caller must be able to trust.

So the body is built explicitly and returned as a :class:`JSONResponse`, and
``response_model`` is declared for the OpenAPI document only — which is the
README's API documentation, so it still has to be right.

===============================================================================
STALENESS, AND WHY THE CACHE SITS WHERE IT DOES
===============================================================================

Story 7 added the second way this endpoint can answer 200: when the live
retrieval fails for a reason retrying could fix, the last good record for that
profile is returned with ``stale: true`` and its **original** ``fetched_at``.
The rule and the reasoning live in :mod:`app.cache`; what this module owns is
*where* the fallback is allowed to happen, and that placement is the safety
property:

* The stale-serve boundary opens **after** step 2. A caller who has stored no
  session, or whose stored session will not decrypt, is refused before the cache
  is consulted — so the cache, which is keyed by public id and shared across
  callers, can never be harvested by somebody with no working credential of
  their own. That is structural here, not a rule someone has to remember, and
  ``test_the_session_check_is_outside_the_stale_serve_boundary`` reads this
  function's own syntax tree to keep it that way. (It has to: moving
  ``vault.unlock`` inside the ``try`` changes no observable behaviour, because
  every code it raises is non-retryable and short-circuits the gate anyway. An
  unobservable safety property is one a test has to pin structurally or stop
  claiming.)
* It closes **before** the identity guard, deliberately — see step 4. A response
  naming a different member is a permanent condition, and the one failure inside
  reach of this boundary that must not be softened by it.
* Only a **retryable** failure can be answered from it, and
  :meth:`app.cache.ProfileCache.fallback_for` decides that by reading
  ``ERROR_SPECS``. ``SESSION_EXPIRED`` therefore reaches the caller as a 428
  even when a perfectly good record exists, which is the whole point — with the
  one honest qualification recorded in :mod:`app.cache`, that LinkedIn does not
  always state a refusal as a refusal.
* Nothing on the stale path re-runs mapping or re-derives a field. A cached
  record is republished exactly as it was stored.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth import require_claims
from app.cache import DATASTORE_UNAVAILABLE, UNUSABLE_RECORD, Fallback, ProfileCache
from app.cache import cache as _process_cache
from app.errors import (
    CAUSE_CLIENT_BUG,
    CAUSE_DEADLINE,
    CAUSE_MEMBER_MISMATCH,
    IDP_UNAVAILABLE_DESCRIPTION,
    NO_STORE,
    ApiError,
    ErrorEnvelope,
    error_responses,
)
from app.linkedin.client import (
    DEFAULT_TIMEOUT_SECONDS,
    RawProfile,
    VoyagerClient,
    parse_profile_url,
)
from app.mapping import map_profile
from app.vault import SessionState, SessionVault
from app.api.v1.session import get_vault, no_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["profile"], dependencies=[Depends(no_store)])


#: Takes the caller's cookie and the URL they asked for, returns the raw fetch.
#: A dependency so the whole endpoint is exercisable with no network — the
#: suite has to pass under ``docker run --network none``, and
#: ``tests/test_linkedin_client.py`` already proves the Voyager call itself.
ProfileFetcher = Callable[[str, str], Awaitable[RawProfile]]


#: An overall deadline for the six calls.
#:
#: The client gives each call its own 15s timeout and nothing bounded the whole
#: fetch: one core call plus five concurrent sections is a ~30s worst case, and
#: a transport that ignores its own timeout is unbounded. This is deliberately
#: set ABOVE that worst case, so it never fires on a fetch that is merely slow
#: and only catches a wedged one. It is a backstop, not a budget.
PROFILE_FETCH_DEADLINE_SECONDS = DEFAULT_TIMEOUT_SECONDS * 3


#: A bound on each cache round-trip, read or write.
#:
#: Both run on the default ``to_thread`` executor, which ``vault.unlock`` shares.
#: A Postgres that accepts TCP and then stops answering would otherwise hold the
#: request open indefinitely — *after* the upstream failure was already decided,
#: or *after* a correct 200 body was already built — and occupy an executor
#: thread while doing it, so one wedged backend starves requests that never
#: touched the cache.
#:
#: This is the half that frees the request. ``asyncio.timeout`` cannot cancel
#: work already inside a thread, so the other half is
#: :data:`app.db.CACHE_STATEMENT_TIMEOUT_MS`, which makes Postgres abort the
#: statement and hand the thread back. Set above that ceiling, so the ordinary
#: outcome is the database giving up and being logged as such, and this only
#: fires when even that did not return.
PROFILE_CACHE_DEADLINE_SECONDS = 8.0


async def fetch_via_voyager(cookie: str, url: str) -> RawProfile:
    """One client per request, holding one caller's session and nothing else.

    Constructed per request deliberately: a shared client would hold one
    session, and CAP-4 requires a second user's request to demonstrably use the
    second user's session.

    **This one line joins the endpoint to LinkedIn, and every test stubs it
    out.** Both parameters are `str`, so swapping them type-checks cleanly and
    then puts the profile URL in the `li_at` header and the cookie through
    `parse_profile_url` — failing 100% of real requests while the whole offline
    suite stays green. A review pass proved exactly that mutation survives.
    `tests/test_profile_api.py::test_fetch_via_voyager_*` now executes this
    function against a recording double, so the swap fails a test.
    """
    return await VoyagerClient(cookie).fetch_profile(url)


def get_profile_fetcher() -> ProfileFetcher:
    return fetch_via_voyager


def get_profile_cache() -> ProfileCache:
    """The process-wide response cache, as a dependency.

    A dependency rather than a module global read inside the handler, for the
    same reason as ``get_vault``: the API tests substitute an in-memory store
    through ``app.dependency_overrides`` and run the whole endpoint — stale path
    included — with no Postgres and no network.
    """
    return _process_cache


# --- The documented shape ----------------------------------------------------
#
# These models exist for the OpenAPI document. The handler builds its body by
# hand — see the module docstring — so nothing here can silently re-add a key
# the mapper deliberately omitted.


class Name(BaseModel):
    first: str | None = None
    last: str | None = None
    full: str | None = None


class Location(BaseModel):
    country: str | None = Field(
        default=None, description="ISO 3166-1 alpha-2, upper-cased."
    )
    region: str | None = Field(
        default=None,
        description=(
            "The member's place as LinkedIn names it, e.g. "
            "`Bengaluru, Karnataka` — resolved from the geo entity the core "
            "request already returns, at no extra call. A redundant trailing "
            "country name is trimmed, because `country` has its own field. "
            "`null` when LinkedIn did not deliver a readable place name; that "
            "is an absence, not a failure, and never appears in `partial`."
        ),
    )


class Images(BaseModel):
    profile: str | None = Field(default=None, description="Absolute URL, largest variant.")
    background: str | None = Field(default=None, description="Absolute URL, largest variant.")


class ExperienceEntry(BaseModel):
    title: str | None = None
    company: str | None = None
    company_url: str | None = None
    employment_type: str | None = Field(
        default=None,
        description=(
            "LinkedIn's readable employment type. `null` when the position "
            "states none. **The key is absent** when LinkedIn referenced a "
            "type but delivered no readable name for it — the envelope's "
            "`partial` then contains `experience.employment_type`. The raw URN "
            "is never published: it is an unreadable value, and dressing it as "
            "a readable one is the exact confusion this API exists to avoid."
        ),
    )
    location: str | None = None
    start: str | None = Field(
        default=None,
        description=(
            "`YYYY-MM`, or `YYYY` when LinkedIn stated only a year, or `null`. "
            "The precision is the source's own and is never widened."
        ),
    )
    end: str | None = Field(
        default=None,
        description=(
            "`YYYY-MM`, or `YYYY` when LinkedIn stated only a year, or `null` "
            "**for a current role only**. A finished position whose end date "
            "carries no month renders as `YYYY`, never `null` — `null` here "
            "means the person still holds the role."
        ),
    )
    description: str | None = None


class EducationEntry(BaseModel):
    school: str | None = None
    school_url: str | None = None
    degree: str | None = None
    field_of_study: str | None = None
    start: str | None = Field(default=None, description="`YYYY`, or `null`.")
    end: str | None = Field(default=None, description="`YYYY`, or `null`.")


class CertificationEntry(BaseModel):
    name: str | None = None
    issuer: str | None = None
    issued: str | None = Field(default=None, description="`YYYY-MM`, or `null`.")
    credential_url: str | None = None


class LanguageEntry(BaseModel):
    name: str | None = None
    proficiency: str | None = Field(
        default=None, description="LinkedIn's own enum, verbatim."
    )


class Profile(BaseModel):
    """The profile object from ``response-schema.md``.

    **Every key here may be absent.** A field that could not be retrieved is
    omitted from this object entirely and its name appears in the envelope's
    ``partial``; a field the member genuinely does not have is `null` (scalars)
    or `[]` (arrays) and does **not** appear there. The two are different
    claims and this API never conflates them.
    """

    name: Name | None = None
    headline: str | None = None
    location: Location | None = None
    about: str | None = Field(
        default=None, description="Full summary text, newlines preserved."
    )
    experience: list[ExperienceEntry] | None = Field(
        default=None, description="Ordered most-recent first."
    )
    education: list[EducationEntry] | None = Field(
        default=None, description="Ordered most-recent first."
    )
    skills: list[str] | None = None
    certifications: list[CertificationEntry] | None = None
    languages: list[LanguageEntry] | None = None
    images: Images | None = None


class ProfileEnvelope(BaseModel):
    """The success envelope. Field names are normative — ``response-schema.md``."""

    url: str = Field(
        description=(
            "The canonical `https://www.linkedin.com/in/{public_id}` form of "
            "the profile that was fetched, not the string you supplied."
        )
    )
    public_id: str
    stale: bool = Field(
        description=(
            "`false` when `profile` came from a live retrieval during this "
            "request. `true` when the live call failed for a reason retrying "
            "could fix — a throttle, a challenge, an upstream fault — and the "
            "last good record for this profile was served instead. A `true` "
            "here is always paired with an older `fetched_at`; read them "
            "together."
        )
    )
    fetched_at: datetime = Field(
        description=(
            "When the returned profile was actually read from LinkedIn, not "
            "when this request was served. On a stale response this is the "
            "older timestamp, and it is the only staleness signal there is: "
            "cached records have no expiry and are never evicted, by decision, "
            "so a record of any age is served in preference to a retryable "
            "error."
        )
    )
    partial: list[str] = Field(
        description=(
            "Names that could not be retrieved in this run, and are therefore "
            "absent from `profile`. **Always present**; empty on a complete "
            "answer. A name here means the field **may be incomplete or "
            "unreadable** — it never means the member has none of it, which is "
            "reported as `[]` or `null` on a present key instead.\n\n"
            "Usually a top-level field name (`certifications`). A **dotted "
            "path** such as `experience.employment_type` means that sub-field "
            "was unreadable for at least one entry in that array and is "
            "omitted from those entries; entries where it was readable still "
            "carry it. Any field in the profile object may appear here."
        )
    )
    profile: Profile


#: Every taxonomy code this route can answer, straight from `ERROR_SPECS` so the
#: documented status and the returned status cannot disagree.
#:
#: The 502 needs two things `ERROR_SPECS` cannot say by itself, and both are
#: passed as addenda rather than patched into the result afterwards — FastAPI
#: merges route-level `responses` OVER router-level, so this entry *replaces*
#: `IDP_UNAVAILABLE_RESPONSE` from the router and has to carry its meaning too:
#:
#: * `retryable` is a per-response property here, and the wire value is
#:   authoritative over the contract's table for the member-mismatch case;
#: * a 502 on this route is not always about LinkedIn — the authentication
#:   boundary answers one when the identity provider cannot be reached.
PROFILE_ERRORS: dict[int | str, dict[str, Any]] = {
    **error_responses(
        "INVALID_URL",
        "NO_SESSION",
        "SESSION_EXPIRED",
        "PROFILE_NOT_FOUND",
        "RATE_LIMITED",
        "UPSTREAM_CHALLENGE",
        "UPSTREAM_ERROR",
        addenda={
            502: (
                "**Read `retryable` from the body, not from the table.** A "
                "response that names a different member than the URL asked for "
                "is `UPSTREAM_ERROR` with `retryable: false`: it is a permanent "
                "condition — a vanity URL that now belongs to someone else — "
                "and it is never softened into a stale 200, whatever is cached. "
                "Every other `UPSTREAM_ERROR` here is retryable. "
                + IDP_UNAVAILABLE_DESCRIPTION
            )
        },
    ),
    # Not taxonomy rows — see `app/errors.py`. Documented because a status a
    # route can answer and does not document is one a client meets by surprise.
    422: {
        "model": ErrorEnvelope,
        "description": "`INVALID_REQUEST` — the `url` query parameter is missing.",
    },
    503: {
        "model": ErrorEnvelope,
        "description": "`SERVICE_UNAVAILABLE` — the session store could not be reached.",
    },
}


def _isoformat(moment: datetime) -> str:
    """RFC 3339 in UTC with a ``Z``, at second precision.

    Second precision because that is the granularity ``response-schema.md``
    documents, and because ``fetched_at`` is a staleness signal measured in
    hours — microseconds on it are noise a consumer would have to parse.

    A naive datetime is treated as UTC rather than rejected: everything in this
    codebase stamps timezone-aware, and serialising an offset-less string would
    silently become "some local time" to a consumer.
    """
    if moment.tzinfo is None:  # pragma: no cover - the client stamps aware
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


async def _record_outcome(
    vault: SessionVault, subject: str, state: SessionState, *, ok: bool | None
) -> None:
    """Write how the caller's stored session fared, best-effort.

    Three outcomes, and the third is not a failure — the same reasoning as
    ``PUT /api/v1/session``'s verifier. ``True`` LinkedIn answered under this
    cookie; ``False`` LinkedIn refused it; ``None`` a throttle, a challenge or a
    network fault, none of which is evidence about the cookie. Recording
    ``false`` for those would libel a perfectly good session and send the caller
    to replace a credential that works.

    ``stored_at`` scopes the write, so a concurrent ``PUT`` that replaced the
    session mid-fetch keeps its own verdict rather than inheriting this one.

    This never fails the request. The profile was retrieved; losing the
    bookkeeping is not a reason to withhold it.
    """
    if ok is None:
        return
    if state.stored_at is None:
        # NOT bundled in with the `ok is None` case above, which is a deliberate
        # silence. This one is a surprise: the vault returned a state with no
        # `stored_at` for a session it had just unlocked, so the verdict has
        # nothing to scope itself to and is dropped. Without this line
        # `GET /api/v1/session` would keep reporting stale bookkeeping and
        # nothing anywhere would say why.
        logger.warning(
            "Cannot record a session use verdict: the unlocked session state "
            "carried no stored_at. last_use_ok will stay as it was."
        )
        return
    try:
        await asyncio.to_thread(
            vault.record_use, subject, ok=ok, stored_at=state.stored_at
        )
    except Exception:
        logger.exception("Recording the session use outcome failed; continuing")


async def _cache_fallback(
    cache: ProfileCache, public_id: str, error: ApiError
) -> Fallback:
    """Ask the cache what it can offer, under a deadline. Never raises.

    The deadline is the point: by the time this runs the request's outcome is
    already settled — the caller is getting either a cached 200 or ``error`` —
    so a datastore that has stopped answering must cost them nothing but the
    few seconds it takes to find that out. Timing out is reported as
    ``DATASTORE_UNAVAILABLE`` and not as "no record", because those are
    different operational conditions and this log line is the only place either
    is ever visible.
    """
    try:
        async with asyncio.timeout(PROFILE_CACHE_DEADLINE_SECONDS):
            return await asyncio.to_thread(cache.fallback_for, public_id, error)
    except TimeoutError:
        logger.error(
            "CAP-5 degraded: the cache lookup for %r exceeded %.0fs. Returning "
            "the live %s. The datastore is not answering.",
            public_id,
            PROFILE_CACHE_DEADLINE_SECONDS,
            error.code,
        )
        return Fallback(DATASTORE_UNAVAILABLE)


async def _cache_remember(
    cache: ProfileCache, public_id: str, body: dict[str, Any], fetched_at: datetime
) -> None:
    """Write the answer to the cache, under a deadline. Never raises.

    The profile has already been retrieved and mapped and the caller is owed it,
    so nothing this function does may change what they get — including hanging.
    :meth:`app.cache.ProfileCache.remember` swallows its own failures; this adds
    the one it cannot, a datastore that never replies at all.
    """
    try:
        async with asyncio.timeout(PROFILE_CACHE_DEADLINE_SECONDS):
            await asyncio.to_thread(cache.remember, public_id, body, fetched_at)
    except TimeoutError:
        logger.error(
            "CAP-5 degraded: caching the response for %r exceeded %.0fs, so no "
            "stale answer will exist for it. Returning the live answer anyway.",
            public_id,
            PROFILE_CACHE_DEADLINE_SECONDS,
        )


@router.get(
    "/profile",
    response_model=ProfileEnvelope,
    responses=PROFILE_ERRORS,
    summary="Fetch a LinkedIn profile as structured JSON",
    description=(
        "Retrieves the profile at `url` under **your own** stored LinkedIn "
        "session and returns it in the shape fixed by `response-schema.md`.\n\n"
        "Absent and unreadable are different answers. A section the member "
        "genuinely does not have comes back as `[]`; a section this run could "
        "not retrieve is **omitted** from `profile` and named in `partial`. "
        "Nothing is silently defaulted, so a non-empty `partial` on a 200 is "
        "the signal that extraction degraded without failing.\n\n"
        "Costs six live calls to LinkedIn, and every successful answer is "
        "kept. If the live retrieval fails for a reason retrying could fix "
        "(`RATE_LIMITED`, `UPSTREAM_CHALLENGE`, `UPSTREAM_ERROR`) and this "
        "profile has been fetched before, you get **200 with `stale: true`** "
        "and the `fetched_at` of that earlier retrieval instead of the error. "
        "Records never expire, so a stale answer may be arbitrarily old — "
        "`fetched_at` is how you judge it.\n\n"
        "A failure this API *classifies* as permanent is never masked this way: "
        "`SESSION_EXPIRED`, `NO_SESSION`, `INVALID_URL` and "
        "`PROFILE_NOT_FOUND` reach you as themselves whatever is cached, and so "
        "does a response that names a different member than you asked for — "
        "that one is a `502 UPSTREAM_ERROR` carrying `retryable: false`, which "
        "is the flag to branch on rather than the code.\n\n"
        "**One known gap in that promise.** LinkedIn does not always state a "
        "refusal as a refusal: a dead `li_at` is sometimes answered with a "
        "redirect to an authwall carrying a `200`, which is indistinguishable "
        "from the challenge page a datacenter IP draws with a perfectly good "
        "session. On a profile fetch that classifies as `UPSTREAM_CHALLENGE` — "
        "retryable — so it *is* stale-served. If `stale` has been `true` for "
        "longer than you can explain, re-`PUT` your session before assuming "
        "LinkedIn is the problem: that check asks LinkedIn who owns the cookie, "
        "where the same wall *is* read as a dead session, and it answers "
        "`last_use_ok: false` immediately."
    ),
)
async def get_profile(
    url: str = Query(
        description="A LinkedIn profile URL, in the public `/in/{public-id}` form.",
        examples=["https://www.linkedin.com/in/example"],
    ),
    claims: dict[str, Any] = Depends(require_claims),
    vault: SessionVault = Depends(get_vault),
    fetch: ProfileFetcher = Depends(get_profile_fetcher),
    cache: ProfileCache = Depends(get_profile_cache),
) -> JSONResponse:
    # 1. Pure validation. No quota, no datastore, no session lookup. The parsed
    #    id is KEPT — step 4 checks the answer against it.
    requested_id = parse_profile_url(url)

    subject = claims["sub"]

    # 2. The caller's OWN session. The key is the verified `sub` and there is no
    #    code path here that takes a subject from anywhere else. Raises
    #    NO_SESSION when nothing is stored and SESSION_EXPIRED when the row
    #    cannot be decrypted — both before anything reaches LinkedIn.
    session, state = await asyncio.to_thread(vault.unlock, subject)

    # --- The stale-serve boundary --------------------------------------------
    #
    # Everything that can fail *retryably* happens inside this block, and the
    # block starts HERE — after the session checks above — by construction. The
    # cache is keyed by public id and shared across callers, so a caller with no
    # session or a dead one must never reach it; they were already refused, so
    # they cannot. That is the placement doing the work, not a rule to remember.
    try:
        # 3. Six calls, under one overall deadline. See
        #    PROFILE_FETCH_DEADLINE_SECONDS: the client bounds each call and
        #    nothing bounded the set of them.
        try:
            async with asyncio.timeout(PROFILE_FETCH_DEADLINE_SECONDS):
                raw = await fetch(session.reveal(), url)
        except TimeoutError as exc:
            logger.warning(
                "Profile fetch exceeded the %.0fs deadline; abandoning it",
                PROFILE_FETCH_DEADLINE_SECONDS,
            )
            await _record_outcome(vault, subject, state, ok=None)
            raise ApiError(
                "UPSTREAM_ERROR",
                cause=CAUSE_DEADLINE,
                log_detail=f"fetch exceeded {PROFILE_FETCH_DEADLINE_SECONDS}s",
            ) from exc
        except ApiError as exc:
            # Only a refusal is evidence about the cookie. A throttle or a
            # challenge says nothing about it, so `last_use_ok` is left alone.
            await _record_outcome(
                vault,
                subject,
                state,
                ok=False if exc.code == "SESSION_EXPIRED" else None,
            )
            raise
        except Exception as exc:
            # Anything the fetch raises that is not already typed. Previously
            # this fell straight through to the 500 handler, which meant the
            # cache was never consulted — CAP-5 defeated in one of the exact
            # situations it exists for, since "the transport blew up in a way
            # nobody predicted" is precisely when a caller most wants the last
            # good record rather than an error.
            #
            # Reported as UPSTREAM_ERROR because that is what it is *from the
            # caller's position*: this handler's only unmediated dependency is
            # the fetch, and a bug behind that seam is still an inability to
            # read LinkedIn. It is not softened for us — `logger.exception` puts
            # the full traceback in the log, at ERROR, and CAP-6 is satisfied
            # either way since a typed 502 is no more a naked exception than a
            # typed 500 was. Note `except Exception` does not catch
            # `CancelledError`, so a genuinely cancelled request still unwinds.
            logger.exception(
                "The profile fetch raised %s. Treating it as an upstream failure "
                "so a cached record can still answer; this is a bug here, not at "
                "LinkedIn, and the traceback above is the real one.",
                type(exc).__name__,
            )
            await _record_outcome(vault, subject, state, ok=None)
            raise ApiError(
                "UPSTREAM_ERROR",
                # This codebase raised, not LinkedIn — the same distinction the
                # client draws, drawn here for the same reason: an operator
                # reading a 502 needs to know whose fault it was.
                cause=CAUSE_CLIENT_BUG,
                log_detail=f"the fetch raised {type(exc).__name__}",
            ) from exc
    except ApiError as exc:
        # The last good record wins over a failure retrying could fix, and loses
        # to every failure it could not. `fallback_for` reads `retryable` off
        # ERROR_SPECS, so this line does not restate the taxonomy — see
        # `app/cache.py`.
        fallback = await _cache_fallback(cache, requested_id, exc)
        if fallback.body is None:
            if fallback.reason in (DATASTORE_UNAVAILABLE, UNUSABLE_RECORD):
                # Already logged in detail, at ERROR, by the cache. Repeated
                # here at the point of consequence so that one line in the log
                # says what the CALLER got: a broken cache and an empty one are
                # the same 502 to them and must never be the same line to us.
                logger.error(
                    "Answered %r with %s because the cache could not help (%s), "
                    "not because it had nothing.",
                    requested_id,
                    exc.code,
                    fallback.reason,
                )
            raise
        logger.info(
            "Serving %r from cache after %s: fetched_at=%s",
            requested_id,
            exc.code,
            fallback.body.get("fetched_at"),
        )
        # Returned exactly as it was stored, apart from `stale: true`. No
        # re-mapping, and above all no re-stamping of `fetched_at` — an
        # unbounded cache is only defensible while the timestamp is honest.
        return JSONResponse(content=fallback.body, headers=dict(NO_STORE))

    # LinkedIn answered under this cookie, so the session demonstrably works.
    # Recorded BEFORE the identity guard below, not after: that guard raises,
    # and a request that reached LinkedIn and got an answer must not leave
    # `last_use_ok` untouched just because the answer was about the wrong
    # person. Every other exit from this handler records a verdict; this one
    # used to be the exception, and it was the exception silently.
    await _record_outcome(vault, subject, state, ok=True)

    # 4. The answer must be about the profile that was ASKED for.
    #
    #    **Outside the stale-serve boundary, and non-retryable.** Story 7 could
    #    only do the first half: `UPSTREAM_ERROR` is retryable in `ERROR_SPECS`,
    #    so inside the boundary this would be answered 200-stale whenever a
    #    record existed, and the placement was the only thing preventing that.
    #    Story 8 owns the taxonomy and narrows this raise to `retryable: false`,
    #    which is what actually makes the refusal permanent — the cache gate now
    #    declines it on its own. The placement stays as belt to that braces:
    #    two independent mechanisms, and removing either one still leaves a 502.
    #
    #    A response naming a different member is a *permanent* condition — a
    #    vanity URL that now belongs to somebody else, a redirect, a
    #    substitution — not an upstream hiccup. Under a cache with no expiry,
    #    softening it to a stale 200 would republish the old identity mapping
    #    for ever and never once tell the caller that the URL they are asking
    #    about has stopped meaning what they think. That is the same shape as
    #    hiding a dead session behind cached data, which the spec forbids
    #    outright.
    #
    #    `raw.public_id` is what the rest of this handler builds `url` and
    #    `public_id` from, so without this check a redirect, an upstream
    #    substitution or a client-side bug publishes one person's profile under
    #    a URL that agrees with itself perfectly — and this request would then
    #    cache it. The client has its own guard on the core response's
    #    `publicIdentifier`; this is the endpoint's, against the string the
    #    caller actually typed, and the two are independent on purpose.
    if raw.public_id != requested_id:
        logger.error(
            "Fetch for %r answered with %r — refusing to publish a different "
            "member's profile under the requested URL.",
            requested_id,
            raw.public_id,
        )
        raise ApiError(
            "UPSTREAM_ERROR",
            # The one deliberate deviation from the published table, and it is
            # the wire value that is authoritative: `response-schema.md` marks
            # UPSTREAM_ERROR retryable, and this instance is not. Adding a
            # taxonomy row was the declined alternative — a caller reads
            # `retryable` precisely so they need not read prose.
            retryable=False,
            cause=CAUSE_MEMBER_MISMATCH,
            log_detail="the fetch answered with a different public id than requested",
        )

    # 4. Map. Pure, total, and the only thing that decides `partial`.
    mapped = map_profile(raw)
    if mapped.partial:
        logger.info(
            "Answered %s with %d field(s) reported partial: %s",
            raw.public_id,
            len(mapped.partial),
            ", ".join(mapped.partial),
        )

    body = {
        # Canonical rather than echoed. The caller's string may carry a locale
        # prefix, a `/details/...` sub-path, LinkedIn's tracking query string
        # or arbitrary case; returning it verbatim would also reflect caller
        # input into a response body for no benefit. This form and `public_id`
        # agree with each other by construction.
        "url": f"https://www.linkedin.com/in/{raw.public_id}",
        "public_id": raw.public_id,
        # This is the live path, so it is `false` here and nowhere else: the one
        # place `true` is ever set is `app.cache`, on a body it read back out of
        # the datastore.
        "stale": False,
        "fetched_at": _isoformat(raw.fetched_at),
        "partial": mapped.partial,
        "profile": mapped.profile,
    }

    # Written AFTER the answer is complete, so what is cached is exactly what
    # this request returned — `partial[]` included, mapping never re-run later.
    # `remember` cannot raise: the profile was retrieved and the caller is owed
    # it, and a datastore that will not take the write is not a reason to
    # withhold a 200 that is already correct.
    #
    # The key is `raw.public_id`, which the guard above has just proven equal to
    # `requested_id` — so a later read under the parsed id finds this row, and a
    # response naming a different member never gets in here at all.
    await _cache_remember(cache, raw.public_id, body, raw.fetched_at)

    # Built and returned by hand so an omitted key stays omitted. The
    # `no_store` dependency's header does not survive returning a Response
    # object, so it is set here too — this body is one person's profile data
    # travelling through host nginx and a Cloudflare edge, and nothing a cache
    # keys on distinguishes one caller from another. The same constant covers
    # every error path, in `app/errors.py`.
    return JSONResponse(content=body, headers=dict(NO_STORE))
