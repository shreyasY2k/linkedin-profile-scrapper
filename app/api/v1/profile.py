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
5. **Map** onto ``response-schema.md`` and answer.

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
STALENESS
===============================================================================

``stale`` is ``false`` and ``fetched_at`` is the live fetch time, always. There
is no cache in this story; story 7 owns stale-serve and is the only thing that
may ever set ``stale`` to ``true``. Both fields exist here because the envelope
shape is fixed by ``response-schema.md``, not by what this story happens to
implement.
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
from app.errors import NO_STORE, ApiError, ErrorEnvelope, error_responses
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
            "request. Always `false` today — cached stale-serve is not built yet."
        )
    )
    fetched_at: datetime = Field(
        description=(
            "When the returned profile was actually read from LinkedIn, not "
            "when this request was served."
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
PROFILE_ERRORS: dict[int | str, dict[str, Any]] = {
    **error_responses(
        "INVALID_URL",
        "NO_SESSION",
        "SESSION_EXPIRED",
        "PROFILE_NOT_FOUND",
        "RATE_LIMITED",
        "UPSTREAM_CHALLENGE",
        "UPSTREAM_ERROR",
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
        "Costs six live calls to LinkedIn. There is no cache yet, so `stale` "
        "is always `false` and `fetched_at` is the time of this fetch."
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

    # 3. Six calls, under one overall deadline. See
    #    PROFILE_FETCH_DEADLINE_SECONDS: the client bounds each call and nothing
    #    bounded the set of them.
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
            log_detail=f"fetch exceeded {PROFILE_FETCH_DEADLINE_SECONDS}s",
        ) from exc
    except ApiError as exc:
        # Only a refusal is evidence about the cookie. A throttle or a
        # challenge says nothing about it, so `last_use_ok` is left alone.
        await _record_outcome(
            vault, subject, state, ok=False if exc.code == "SESSION_EXPIRED" else None
        )
        raise

    # 4. The answer must be about the profile that was ASKED for.
    #
    #    `raw.public_id` is what the rest of this handler builds `url` and
    #    `public_id` from, so without this check a redirect, an upstream
    #    substitution or a client-side bug publishes one person's profile under
    #    a URL that agrees with itself perfectly — and story 7 would cache it.
    #    The client has its own guard on the core response's
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
            log_detail="the fetch answered with a different public id than requested",
        )

    await _record_outcome(vault, subject, state, ok=True)

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
        # Story 7 owns stale-serve. Until it lands this is a live fetch or it
        # is an error; there is no third state to report.
        "stale": False,
        "fetched_at": _isoformat(raw.fetched_at),
        "partial": mapped.partial,
        "profile": mapped.profile,
    }

    # Built and returned by hand so an omitted key stays omitted. The
    # `no_store` dependency's header does not survive returning a Response
    # object, so it is set here too — this body is one person's profile data
    # travelling through host nginx and a Cloudflare edge, and nothing a cache
    # keys on distinguishes one caller from another. The same constant covers
    # every error path, in `app/errors.py`.
    return JSONResponse(content=body, headers=dict(NO_STORE))
