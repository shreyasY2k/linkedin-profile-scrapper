"""The Voyager client: a LinkedIn profile URL in, raw normalized JSON out.

Retrieval only. Nothing here reshapes a payload toward ``response-schema.md`` —
that is story 6's job, and it works from what this module returns.

===============================================================================
THE REQUEST SHAPE — verified live against LinkedIn on 2026-08-27
===============================================================================

This block is documentation of measured fact, not of remembered API lore.
Story 9's "approach" section is written from it, so keep it accurate.

Base: ``https://www.linkedin.com/voyager/api/``

Authentication is a cookie pair plus a header that must agree with one half of
it::

    cookie:      li_at={session}; JSESSIONID="{J}"
    csrf-token:  {J}

``J`` may be *any* value, as long as the header and the cookie carry the same
one — LinkedIn checks the two against each other, not against a session it
issued. ``ajax:0000000000000000000`` is verified working. The quotes around the
``JSESSIONID`` cookie value are part of the wire format and are required.

Three more headers, all required in practice::

    accept:                     application/vnd.linkedin.normalized+json+2.1
    x-restli-protocol-version:  2.0.0
    user-agent:                 <a browser UA; a default urllib UA draws a challenge>

``accept-encoding`` is deliberately NOT sent: :mod:`urllib` does not decompress
a response, so asking for gzip would hand this module a body it cannot parse.

The endpoint map, all verified 200:

===================  ==========================================================
Purpose              Path under the base
===================  ==========================================================
Session check        ``me``
Core profile         ``identity/dash/profiles?q=memberIdentity&memberIdentity={public_id}``
Experience           ``identity/dash/profilePositions?q=viewee&profileUrn={urlencoded entityUrn}``
Education            ``identity/dash/profileEducations?q=viewee&profileUrn={…}``
Skills               ``identity/dash/profileSkills?q=viewee&profileUrn={…}``
Certifications       ``identity/dash/profileCertifications?q=viewee&profileUrn={…}``
Languages            ``identity/dash/profileLanguages?q=viewee&profileUrn={…}``
===================  ==========================================================

Every section request also carries ``&count=100``. The default page size is 20,
which silently truncated a 33-skill profile to 20 with a 200 and no error —
see :data:`SECTION_PAGE_SIZE`. It is the same one request either way.

**Dead. Do not reintroduce from memory** — every one of these is what a
plausible-looking implementation written from documentation would use:

* ``identity/profiles/{id}/profileView`` → **410 Gone**
* ``identity/profiles/{id}`` → **410 Gone**
* ``graphql`` without a ``queryId`` → **403**

===============================================================================
THE NORMALIZED ENVELOPE
===============================================================================

Every response is ``{"data": {...}, "included": [...]}``. ``data`` holds
*references* — ``*elements`` is a list of URN strings, not of objects — and the
entities themselves live flat in ``included``, joined by ``entityUrn``. Reading
``data`` expecting nested objects returns nothing and looks like an empty
profile. :func:`resolve_elements` performs the join.

===============================================================================
COST
===============================================================================

Six live calls per profile: one core, then five sections. The core ``Profile``
carries only ``experienceCardUrn`` / ``educationCardUrn`` pointers, so the
sections cannot be had from it. Every call goes through :meth:`VoyagerClient._request`
so that this cost is countable (:attr:`RawProfile.call_count`) and so that
rate-limit and challenge detection has exactly one place to live.

There is deliberately **no retry**. A retry would multiply the per-profile call
count against an account with a real quota, and the failures worth retrying —
throttling, challenges — are precisely the ones an immediate retry makes worse.
Story 7's stale-serve is the recovery mechanism, not this module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from app.errors import ApiError

logger = logging.getLogger(__name__)


# --- Wire constants ----------------------------------------------------------

VOYAGER_BASE = "https://www.linkedin.com/voyager/api/"

#: Hosts a profile URL may name. Exact match or subdomain — LinkedIn serves
#: country variants such as ``in.linkedin.com`` and ``uk.linkedin.com``.
LINKEDIN_HOST = "linkedin.com"

#: Any value works, provided the cookie and the ``csrf-token`` header carry the
#: same one. Fixed rather than random so a captured request is reproducible and
#: so tests can assert the pairing exactly.
DEFAULT_JSESSIONID = "ajax:0000000000000000000"

#: A real browser UA. LinkedIn answers urllib's default UA with a challenge
#: page, which this module would then correctly — and unhelpfully — report as
#: ``UPSTREAM_CHALLENGE`` on every single request.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT_SECONDS = 15.0

#: Hard cap on a response body. A profile payload is tens of kilobytes; a
#: multi-megabyte body is either a challenge page or a hostile response, and an
#: unbounded ``read()`` would let either exhaust this container.
MAX_BODY_BYTES = 8 * 1024 * 1024

#: The five sub-resources, keyed by the name ``response-schema.md`` uses for
#: the corresponding profile field. Story 6 puts these keys straight into
#: ``partial[]`` when a section fails, so the naming is contract, not taste.
SECTION_RESOURCES: Mapping[str, str] = {
    "experience": "identity/dash/profilePositions",
    "education": "identity/dash/profileEducations",
    "skills": "identity/dash/profileSkills",
    "certifications": "identity/dash/profileCertifications",
    "languages": "identity/dash/profileLanguages",
}

CORE_RESOURCE = "identity/dash/profiles"
ME_RESOURCE = "me"

#: Elements requested per section. **Verified live on 2026-08-27**, and not an
#: optimisation — a correctness fix for data loss that was already happening.
#:
#: The default page size is 20. The developer's own profile has 33 skills, so
#: the default request returned 20 of them with a 200 and no error of any kind:
#: a truncated list presented as a complete one. `count=100` returned all 33 in
#: the SAME single call, so this costs nothing against the "no change that
#: increases the number of live calls per profile" boundary — it is one request
#: either way.
#:
#: 100 rather than higher because it is the value that was actually tested. A
#: profile with more than 100 of something is still truncated, and still says
#: so through `SectionFetch.reported_total`.
SECTION_PAGE_SIZE = 100

#: ``$type`` suffix identifying the core entity inside ``included``.
PROFILE_TYPE_SUFFIX = "identity.profile.Profile"

#: Path fragments that mean LinkedIn answered with a wall instead of data. A
#: challenge usually arrives as a *redirect* that urllib follows, so the final
#: URL is the reliable signal — the status on the page it lands on is 200.
CHALLENGE_PATH_MARKERS = (
    "/authwall",
    "/checkpoint",
    "/uas/login",
    "/login",
    "/challenge",
)

#: Status LinkedIn returns to clients it has decided are bots. Not a real HTTP
#: status; it is theirs, and it means "go away", not "server error".
LINKEDIN_BOT_STATUS = 999

#: `Retry-After` is only propagated in its delta-seconds form. The HTTP-date
#: form is legal but LinkedIn does not send it, and echoing an unvalidated
#: upstream header into our own response is how a header-injection bug starts.
RETRY_AFTER_RE = re.compile(r"^\d{1,7}$")

#: ``/in/{public-id}``, with an optional locale prefix (``/en/in/x``) and any
#: trailing sub-path (``/in/x/details/experience``) that LinkedIn's own UI adds.
PROFILE_PATH_RE = re.compile(
    r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?in/(?P<public_id>[^/]+)(?:/.*)?$",
    re.IGNORECASE,
)

#: What survives as a public id after percent-decoding. Deliberately a
#: blocklist of structure rather than an allowlist of characters: public ids
#: contain non-ASCII letters on plenty of real profiles, and an ASCII-only
#: allowlist would reject them as malformed.
_ILLEGAL_PUBLIC_ID_RE = re.compile(r"[\s/\\?#\x00-\x1f\x7f]")
MAX_PUBLIC_ID_LENGTH = 100


# --- The session -------------------------------------------------------------


class LinkedInSession:
    """A ``li_at`` cookie value that refuses to render itself.

    The constraint from the story is absolute: the cookie value appears in no
    log, trace, exception message, test output or error body. Holding it in a
    bare ``str`` makes that a rule every future call site has to remember. This
    class makes it a property of the value itself — ``repr`` and ``str`` both
    return a placeholder, so an f-string, a ``logger.info("%s", session)``, a
    pytest assertion dump and a traceback that happens to include a frame local
    all print the placeholder rather than the secret.

    Reading the real value requires :meth:`reveal`, which is greppable. There
    are two call sites: building the cookie header, and building the redaction
    list.
    """

    __slots__ = ("_value",)

    PLACEHOLDER = "<linkedin-session redacted>"

    def __init__(self, value: str) -> None:
        cleaned = (value or "").strip()
        if not cleaned:
            # NO_SESSION, not SESSION_EXPIRED. The matrix requires a caller who
            # has stored nothing to be told something different from a caller
            # whose stored cookie LinkedIn has stopped accepting: the first
            # needs to supply a session, the second to supply a *new* one.
            raise ApiError(
                "NO_SESSION",
                log_detail="empty LinkedIn session cookie supplied to the client",
            )
        self._value = cleaned

    def reveal(self) -> str:
        """Return the cookie value. The only way to read it; grep for callers."""
        return self._value

    def __repr__(self) -> str:  # pragma: no cover - exercised via assertions
        return self.PLACEHOLDER

    def __str__(self) -> str:  # pragma: no cover - exercised via assertions
        return self.PLACEHOLDER


# --- Transport ---------------------------------------------------------------


class TransportError(Exception):
    """The request never produced an HTTP response: DNS, TLS, timeout, reset.

    Distinct from an HTTP error status, which arrives as a
    :class:`VoyagerResponse` and is classified from its status and body.
    """


@dataclass(frozen=True)
class VoyagerResponse:
    """One HTTP response, reduced to what classification actually reads.

    ``url`` is the *final* URL after redirects, which is how a challenge is
    detected: the redirect to ``/authwall`` is followed, so the status is 200
    and only the landing URL and content type give it away.
    """

    status: int
    url: str
    #: Lower-cased header names. LinkedIn's casing is not stable.
    headers: Mapping[str, str]
    body: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    @property
    def is_json(self) -> bool:
        """Whether the body claims to be JSON.

        Matches ``application/json`` and LinkedIn's
        ``application/vnd.linkedin.normalized+json+2.1`` alike. An HTML
        challenge page fails this even when it arrives with a 200.
        """
        media_type = self.content_type.split(";", 1)[0].strip().lower()
        return media_type.endswith("json") or "+json" in media_type


#: A transport takes (url, headers, timeout) and returns a response, or raises
#: :class:`TransportError`. Synchronous by design — it is called through
#: ``asyncio.to_thread``, so the fan-out is concurrent without an async HTTP
#: client entering the runtime dependency set.
Transport = Callable[[str, Mapping[str, str], float], VoyagerResponse]


def _read_capped(stream: Any) -> bytes:
    """Read at most :data:`MAX_BODY_BYTES`, detecting overrun rather than truncating."""
    body = stream.read(MAX_BODY_BYTES + 1)
    if len(body) > MAX_BODY_BYTES:
        raise TransportError(f"response body exceeded {MAX_BODY_BYTES} bytes")
    return body


def _lower_headers(raw: Any) -> dict[str, str]:
    return {str(name).lower(): str(value) for name, value in raw.items()}


def urllib_transport(
    url: str, headers: Mapping[str, str], timeout: float
) -> VoyagerResponse:
    """Perform one GET with the standard library.

    stdlib rather than ``httpx`` because the story's boundaries put a new
    runtime dependency behind "Ask First", and :mod:`app.auth` already
    establishes the house pattern of a plain ``urllib`` call for outbound HTTP.
    Concurrency comes from ``asyncio.to_thread`` in the client instead of from
    an async client.

    An HTTP error status is *not* an exception here: ``urllib`` raises
    :class:`urllib.error.HTTPError` for anything >= 400, and that object is
    itself a readable response. Converting it back into a
    :class:`VoyagerResponse` is what lets the single classifier below see 429s
    and 410s as data rather than as transport failures.
    """
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return VoyagerResponse(
                status=response.status,
                url=response.geturl(),
                headers=_lower_headers(response.headers),
                body=_read_capped(response),
            )
    except urllib.error.HTTPError as exc:
        try:
            body = _read_capped(exc)
        except Exception:  # pragma: no cover - a body-less error response
            body = b""
        return VoyagerResponse(
            status=exc.code,
            url=exc.url or url,
            headers=_lower_headers(exc.headers) if exc.headers else {},
            body=body,
        )
    except urllib.error.URLError as exc:
        # `exc.reason` is a socket error or an SSL error. Neither contains a
        # request header, so neither can carry the cookie — but the client
        # redacts everything it logs anyway.
        raise TransportError(f"{type(exc).__name__}: {exc.reason}") from exc
    except (TimeoutError, OSError, ValueError) as exc:
        raise TransportError(f"{type(exc).__name__}: {exc}") from exc


# --- URL parsing -------------------------------------------------------------


def parse_profile_url(url: str) -> str:
    """Return the public id in ``url``, or raise ``INVALID_URL``.

    Runs before any network call, which is the whole point: a malformed URL
    must cost zero LinkedIn quota. Accepts what a human actually pastes —
    a missing scheme, a country subdomain, a locale prefix, a trailing slash,
    the tracking query string LinkedIn's share button appends, and the
    ``/details/...`` sub-paths its own UI links to.

    Rejects anything that is not a profile URL on a LinkedIn host: company
    pages, job posts, search results, and any other host entirely.
    """
    if not isinstance(url, str) or not url.strip():
        raise _invalid_url("no URL supplied")

    candidate = url.strip()
    # A bare `www.linkedin.com/in/x` parses with an empty netloc and the whole
    # thing in `path`, which would then fail the host check for the wrong
    # reason. Give it a scheme so the host check judges the actual host.
    if "//" not in candidate.split("?", 1)[0]:
        candidate = "https://" + candidate

    try:
        parts = urllib.parse.urlsplit(candidate)
    except ValueError as exc:
        raise _invalid_url(f"unparseable URL: {type(exc).__name__}") from exc

    if parts.scheme not in ("http", "https"):
        raise _invalid_url(f"scheme {parts.scheme!r} is not http(s)")

    host = (parts.hostname or "").lower()
    if host != LINKEDIN_HOST and not host.endswith("." + LINKEDIN_HOST):
        # Explicitly logged: `linkedin.com.evil.test` reaching a fetch would be
        # an SSRF, and this is the line that stops it.
        raise _invalid_url(f"host {host!r} is not a LinkedIn host")

    match = PROFILE_PATH_RE.match(parts.path or "/")
    if match is None:
        raise _invalid_url(f"path {parts.path!r} is not a /in/{{public-id}} profile path")

    public_id = urllib.parse.unquote(match.group("public_id")).strip()
    if not public_id:
        raise _invalid_url("empty public id")
    if len(public_id) > MAX_PUBLIC_ID_LENGTH:
        raise _invalid_url(f"public id is {len(public_id)} characters")
    if _ILLEGAL_PUBLIC_ID_RE.search(public_id):
        # Decoding happens before this check on purpose: `%2F` and `%00` are
        # exactly how a path separator or a null byte would be smuggled past a
        # check performed on the still-encoded form.
        raise _invalid_url("public id contains an illegal character")
    return public_id


def _invalid_url(detail: str) -> ApiError:
    """Build the 400. The caller's URL is never echoed into the response body."""
    logger.info("Rejected profile URL: %s", detail)
    return ApiError("INVALID_URL", log_detail=detail)


# --- Envelope helpers --------------------------------------------------------


def resolve_elements(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Join ``data["*elements"]`` against ``included`` and return the entities.

    This is the join the normalized envelope requires and that most Voyager
    examples get wrong. ``data["*elements"]`` is an ordered list of URN strings
    and is authoritative for *order*; ``included`` is an unordered pool of
    entities keyed by ``entityUrn``.

    Returning entities in ``*elements`` order matters downstream:
    ``response-schema.md`` requires experience and education "ordered
    most-recent first", and that ordering is LinkedIn's, carried by this list —
    it is not recoverable by sorting on ``dateRange`` once a current role with
    no end date is in the mix.

    Reshaping nothing: the dicts returned are the payload's own objects.
    """
    data = payload.get("data")
    included = payload.get("included")
    # `list`, not `Sequence`: a JSON array is a list, and `Sequence` would
    # also accept the string LinkedIn returns on a malformed response — which
    # would then be iterated character by character rather than rejected.
    if not isinstance(data, Mapping) or not isinstance(included, list):
        return []

    by_urn: dict[str, dict[str, Any]] = {}
    for entity in included:
        if isinstance(entity, dict):
            urn = entity.get("entityUrn")
            if isinstance(urn, str):
                by_urn[urn] = entity

    urns = data.get("*elements")
    if not isinstance(urns, list):
        return []

    resolved: list[dict[str, Any]] = []
    for urn in urns:
        entity = by_urn.get(urn) if isinstance(urn, str) else None
        if entity is not None:
            resolved.append(entity)
    return resolved


def element_urns(payload: Mapping[str, Any]) -> list[str]:
    """The raw ``*elements`` URN list, which is what "how many" really means.

    Counted from ``data``, never from ``len(included)``: ``included`` can carry
    entities the elements list does not reference, and a section whose join
    fails would otherwise look empty rather than broken.
    """
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return []
    urns = data.get("*elements")
    if not isinstance(urns, list):
        return []
    return [urn for urn in urns if isinstance(urn, str)]


def reported_total(payload: Mapping[str, Any]) -> int | None:
    """``data.paging.total`` — how many elements LinkedIn says exist.

    ``None`` when the response does not state one, which is normal: the core
    profile collection omits ``total`` entirely, and only the sections carry it.
    ``None`` therefore means "not stated", never "zero".

    Recorded because the client reads only the first page. A profile with more
    positions than the page size returns a truncated list with a 200, and
    without this the truncation is invisible — story 6 would publish "these are
    their roles" about a list that is missing some. Comparing this against
    ``element_count`` is what lets it say otherwise. Following the pages would
    multiply the per-profile call count, which the story puts behind Ask First.
    """
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    paging = data.get("paging")
    if not isinstance(paging, Mapping):
        return None
    total = paging.get("total")
    if isinstance(total, bool) or not isinstance(total, int):
        return None
    return total


def find_entity(payload: Mapping[str, Any], type_suffix: str) -> dict[str, Any] | None:
    """First entity in ``included`` whose ``$type`` ends with ``type_suffix``."""
    included = payload.get("included")
    if not isinstance(included, list):
        return None
    for entity in included:
        if isinstance(entity, dict) and str(entity.get("$type", "")).endswith(type_suffix):
            return entity
    return None


# --- Results -----------------------------------------------------------------


@dataclass(frozen=True)
class SectionFetch:
    """One sub-resource's outcome — success and size recorded *separately*.

    This separation is the point of the dataclass, and it is not defensive
    programming. Measured behaviour on 2026-08-27: ``profileLanguages``
    returned **0 elements** and, minutes later on an identical request, **3
    elements** — both with a 200 and no error. So "zero elements" is not
    evidence that the profile has none.

    Story 6 therefore maps ``ok=True, element_count=0`` to ``[]`` ("the profile
    has none") and ``ok=False`` to an omitted key plus an entry in ``partial[]``
    ("we could not read it"). Collapsing the two into one signal publishes a
    confident falsehood about a real person's profile.
    """

    #: The ``response-schema.md`` field name this section populates.
    name: str
    #: Voyager path, kept for the log line that explains a failure.
    resource: str
    #: Whether the call returned a parseable payload. Independent of size.
    ok: bool
    #: The raw envelope, exactly as received. ``None`` only when ``ok`` is False.
    payload: dict[str, Any] | None = None
    #: ``len(data["*elements"])``. ``None`` when the call failed — which is a
    #: third state, distinct from 0, and deliberately not defaulted to it.
    element_count: int | None = None
    #: ``data.paging.total``, when the response states one. Only the first page
    #: is retrieved, so ``total > element_count`` means the list is truncated —
    #: which a 200 does not otherwise reveal. ``None`` means "not stated".
    reported_total: int | None = None
    #: Taxonomy code from ``response-schema.md`` when ``ok`` is False.
    error_code: str | None = None


@dataclass(frozen=True)
class RawProfile:
    """Everything one fetch retrieved. Raw, unmapped, story 6's input."""

    #: The URL as supplied by the caller, unmodified.
    url: str
    public_id: str
    #: ``urn:li:fsd_profile:...`` — the key every section call is made against.
    profile_urn: str
    #: The core envelope, exactly as received.
    core: dict[str, Any]
    #: The ``Profile`` entity resolved out of ``core["included"]``. Selected,
    #: not reshaped: it is the payload's own object.
    profile: dict[str, Any]
    sections: Mapping[str, SectionFetch]
    #: Live calls THIS fetch spent — six on the happy path. Not the client's
    #: cumulative total: a client that validated its session first has already
    #: spent one, and the boundary "no change that increases the number of live
    #: calls per profile" is about the fetch, not about the process.
    call_count: int
    fetched_at: datetime

    @property
    def failed_sections(self) -> list[str]:
        """Section names story 6 must report in ``partial[]``."""
        return [name for name, section in self.sections.items() if not section.ok]

    @property
    def truncated_sections(self) -> list[str]:
        """Sections where LinkedIn says there are more than this page returned.

        A separate state from both "failed" and "empty": the call succeeded and
        the elements returned are real, but the list is incomplete. Story 6
        must not present one of these as the whole of a person's history.
        """
        return [
            name
            for name, section in self.sections.items()
            if section.ok
            and section.reported_total is not None
            and section.element_count is not None
            and section.reported_total > section.element_count
        ]


# --- The client --------------------------------------------------------------


class VoyagerClient:
    """Retrieves raw profile JSON under one LinkedIn session.

    One instance per session, constructed per request in story 5 from the
    caller's stored cookie. It holds no cache and no state beyond the session
    and a call counter.

    ``transport`` is injectable so that the whole edge-case matrix is testable
    offline. The default is the stdlib one; the test suite never installs a
    transport that can reach a network.
    """

    def __init__(
        self,
        cookie: str,
        *,
        transport: Transport = urllib_transport,
        jsessionid: str = DEFAULT_JSESSIONID,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._session = LinkedInSession(cookie)
        self._transport = transport
        self._jsessionid = jsessionid
        self._user_agent = user_agent
        self._timeout = timeout
        #: Live calls made by this instance. Read by tests to prove the fetch
        #: costs six and not seven.
        self.call_count = 0

    # -- Public API ----------------------------------------------------------

    async def check_session(self) -> str:
        """Return the session owner's own ``publicIdentifier``, via ``me``.

        Two uses. Story 5 validates a freshly stored cookie with it — one cheap
        call that distinguishes "expired" from "works" without touching anyone
        else's profile. And the live check identifies the *developer's own*
        profile with it, so that the one permitted live fetch cannot
        accidentally be aimed at a third party.
        """
        payload = await self._request(ME_RESOURCE, resource=ME_RESOURCE)
        identifier = self._public_identifier(payload)
        if identifier is None:
            # A 200 from `me` that names nobody is a session LinkedIn accepted
            # without recognising — treat it as dead rather than as an upstream
            # fault, since the caller's remedy is the same either way and
            # SESSION_EXPIRED is the one that tells them so.
            logger.warning("Voyager me returned 200 with no publicIdentifier")
            raise ApiError(
                "SESSION_EXPIRED",
                log_detail="me returned no publicIdentifier",
            )
        return identifier

    async def fetch_profile(self, url: str) -> RawProfile:
        """Resolve ``url`` and retrieve the core profile plus all five sections.

        Six live calls. The core call is made first and alone, because every
        section request needs the ``entityUrn`` it returns; the five sections
        then fan out concurrently.

        Failure is asymmetric on purpose. The core failing aborts the fetch —
        without the profile entity there is no profile to report. A *section*
        failing degrades: it is recorded on its :class:`SectionFetch` and the
        other four still return, because ``response-schema.md`` has a place to
        say "this field was not retrievable" (``partial[]``) and no place to say
        "the whole request died because languages 500'd".
        """
        public_id = parse_profile_url(url)

        # Snapshot, so the number reported below is what THIS fetch spent. The
        # instance counter is cumulative — `check_session()` runs on the same
        # client in story 5's validate-then-fetch flow, and reporting 7 for a
        # six-call fetch would make the "no change increases the live-call
        # count" boundary unenforceable in exactly the case worth watching.
        calls_before = self.call_count

        core = await self._fetch_core(public_id)
        profile = find_entity(core, PROFILE_TYPE_SUFFIX)
        if profile is None:
            raise self._not_found(public_id, "core response carried no Profile entity")

        profile_urn = profile.get("entityUrn")
        if not isinstance(profile_urn, str) or not profile_urn:
            raise self._upstream_error(
                CORE_RESOURCE, "Profile entity carried no entityUrn"
            )

        sections = await self._fetch_sections(profile_urn)

        failed = [name for name, section in sections.items() if not section.ok]
        if failed:
            logger.warning(
                "Profile %s retrieved with %d of %d sections unreadable: %s",
                _loggable_public_id(public_id),
                len(failed),
                len(SECTION_RESOURCES),
                ", ".join(sorted(failed)),
            )

        return RawProfile(
            url=url,
            public_id=public_id,
            profile_urn=profile_urn,
            core=core,
            profile=profile,
            sections=sections,
            call_count=self.call_count - calls_before,
            # The moment the data was actually read from LinkedIn, which is
            # what `fetched_at` means on the wire — not when a response was
            # served, and on a stale-serve those differ by design.
            fetched_at=datetime.now(timezone.utc),
        )

    # -- Fan-out -------------------------------------------------------------

    async def _fetch_core(self, public_id: str) -> dict[str, Any]:
        """The one call whose failure aborts the whole fetch."""
        query = urllib.parse.urlencode(
            {"q": "memberIdentity", "memberIdentity": public_id}
        )
        payload = await self._request(f"{CORE_RESOURCE}?{query}", resource=CORE_RESOURCE)

        if not element_urns(payload):
            # A well-formed id that does not exist answers 200 with an empty
            # elements list rather than a 404. Reporting that as an empty
            # profile would be the worst possible outcome: a confident,
            # well-formed answer about a person who is not there.
            raise self._not_found(public_id, "core response listed zero elements")
        return payload

    async def _fetch_sections(self, profile_urn: str) -> dict[str, SectionFetch]:
        """Fan the five section calls out concurrently.

        ``return_exceptions`` is not used: :meth:`_fetch_section` already
        converts every failure into a recorded :class:`SectionFetch`, so a
        raised exception here would be a bug in this module rather than an
        upstream failure, and swallowing it would hide it.
        """
        names = list(SECTION_RESOURCES)
        results = await asyncio.gather(
            *(self._fetch_section(name, profile_urn) for name in names)
        )
        return dict(zip(names, results))

    async def _fetch_section(self, name: str, profile_urn: str) -> SectionFetch:
        """One sub-resource. Never raises — a failure is a recorded outcome."""
        resource = SECTION_RESOURCES[name]
        query = urllib.parse.urlencode(
            {"q": "viewee", "profileUrn": profile_urn, "count": SECTION_PAGE_SIZE}
        )
        try:
            payload = await self._request(f"{resource}?{query}", resource=resource)
        except ApiError as exc:
            # The typed code is preserved rather than flattened: story 6 wants
            # to know that languages failed *because of throttling* and not
            # because the endpoint was withdrawn, and story 8 decides from it
            # whether a partial answer is honest.
            logger.warning(
                "Section %s (%s) failed: %s", name, resource, exc.log_detail or exc.code
            )
            return SectionFetch(
                name=name, resource=resource, ok=False, error_code=exc.code
            )

        return SectionFetch(
            name=name,
            resource=resource,
            ok=True,
            payload=payload,
            # Recorded independently of `ok`. Zero here means "LinkedIn said
            # zero", which is emphatically not the same as "not retrievable".
            element_count=len(element_urns(payload)),
            reported_total=reported_total(payload),
        )

    # -- The single choke point ----------------------------------------------

    async def _request(self, path: str, *, resource: str) -> dict[str, Any]:
        """Every outbound call to LinkedIn goes through here. No exceptions.

        Concentrating them buys three things the story requires: the live-call
        count is knowable, rate-limit and challenge detection have exactly one
        implementation, and there is a single place where the cookie is written
        into a header — so a proof that it never leaks is a proof about one
        function rather than about a codebase.
        """
        url = VOYAGER_BASE + path
        self.call_count += 1

        try:
            # `to_thread`, not an async HTTP client: this keeps the five
            # section calls genuinely concurrent while the runtime dependency
            # set stays exactly what story 1 pinned.
            response = await asyncio.to_thread(
                self._transport, url, self._headers(), self._timeout
            )
        except TransportError as exc:
            raise self._upstream_error(resource, f"transport failed: {self._safe(exc)}")
        except Exception as exc:  # pragma: no cover - a broken transport
            # A transport that raises something unexpected must not become a
            # naked 500. CAP-6 permits no unhandled exception to reach a caller.
            raise self._upstream_error(
                resource, f"transport raised {type(exc).__name__}: {self._safe(exc)}"
            )

        return self._classify(response, resource=resource)

    def _classify(self, response: VoyagerResponse, *, resource: str) -> dict[str, Any]:
        """Turn one HTTP response into a payload or a typed error.

        Order matters and is argued, top to bottom:

        1. **429 first.** A throttling response can arrive as an HTML page, and
           checking content type before status would report the more alarming
           ``UPSTREAM_CHALLENGE`` for the ordinary, retryable case.
        2. **Challenge by destination, then by content type.** The matrix is
           explicit that a challenge is "detected by content type, not by
           status alone" — because the authwall arrives as a *200* at the end
           of a redirect chain urllib follows silently.
        3. **410 loudly.** An endpoint being withdrawn is the failure mode this
           whole story exists because of, and the one that must never be
           mistaken for an empty profile.
        """
        status = response.status
        challenge = self._challenge_reason(response)

        if status == 429:
            raise self._rate_limited(response, resource)

        if status == LINKEDIN_BOT_STATUS:
            raise self._challenge(resource, f"status {LINKEDIN_BOT_STATUS}")

        if challenge is not None:
            raise self._challenge(resource, challenge)

        if status in (401, 403):
            # The cookie was presented and refused. Not NO_SESSION — one was
            # supplied — and not UPSTREAM_ERROR, because the caller can fix it.
            raise ApiError(
                "SESSION_EXPIRED",
                log_detail=f"{resource} returned {status}; LinkedIn refused the session",
            )

        if status == 404:
            raise ApiError(
                "PROFILE_NOT_FOUND",
                log_detail=f"{resource} returned 404",
            )

        if status == 410:
            # The specific catastrophe the Code Map warns about: `profileView`
            # is already 410, and the dash endpoints can follow. ERROR, not
            # WARNING — this is the line that should wake someone up, and the
            # symptom it prevents is a profile that silently reports empty.
            logger.error(
                "Voyager endpoint %s is GONE (410). The endpoint map in "
                "app/linkedin/client.py is out of date and must be re-verified.",
                resource,
            )
            raise self._upstream_error(resource, "endpoint returned 410 Gone")

        if status != 200:
            raise self._upstream_error(resource, f"unexpected status {status}")

        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._upstream_error(
                resource, f"body claimed JSON but did not parse: {type(exc).__name__}"
            ) from exc

        if not isinstance(payload, dict):
            raise self._upstream_error(
                resource, f"payload is {type(payload).__name__}, not an object"
            )
        return payload

    def _challenge_reason(self, response: VoyagerResponse) -> str | None:
        """Why this response looks like a wall rather than data, or ``None``.

        Two independent signals, because either alone misses real cases: the
        redirect destination catches the authwall that answers 200, and the
        content type catches a challenge served in place at the requested URL.
        """
        try:
            path = urllib.parse.urlsplit(response.url).path.lower()
        except ValueError:  # pragma: no cover - urllib built this URL
            path = ""
        for marker in CHALLENGE_PATH_MARKERS:
            if path.startswith(marker) or path.startswith("/voyager" + marker):
                return f"redirected to {marker}"

        if response.status == 200 and not response.is_json:
            return f"content-type {response.content_type.split(';')[0].strip()!r} is not JSON"
        return None

    def _rate_limited(self, response: VoyagerResponse, resource: str) -> ApiError:
        """Throttled. Propagate ``Retry-After`` only when it is a sane integer."""
        headers: dict[str, str] = {}
        retry_after = response.headers.get("retry-after", "").strip()
        if RETRY_AFTER_RE.match(retry_after):
            headers["Retry-After"] = retry_after
        elif retry_after:
            logger.info("Ignoring unparseable Retry-After from %s", resource)
        logger.warning("Voyager throttled %s", resource)
        return ApiError(
            "RATE_LIMITED",
            headers=headers or None,
            log_detail=f"{resource} returned 429",
        )

    # -- Headers and redaction ------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """The verified header set. The one place the cookie value is written.

        The ``JSESSIONID`` cookie and the ``csrf-token`` header carry the same
        value by construction here rather than by two call sites agreeing — a
        mismatch is a 403 that looks exactly like an expired session, which is
        an hour of debugging aimed at the wrong thing.
        """
        return {
            "cookie": f'li_at={self._session.reveal()}; JSESSIONID="{self._jsessionid}"',
            "csrf-token": self._jsessionid,
            "accept": "application/vnd.linkedin.normalized+json+2.1",
            "x-restli-protocol-version": "2.0.0",
            "user-agent": self._user_agent,
            "x-li-lang": "en_US",
        }

    def _safe(self, value: Any) -> str:
        """Render a value for a log line with the session redacted out of it.

        :class:`LinkedInSession` already stops the cookie reaching a log
        through an f-string. This closes the other route: a value that came
        back *from* LinkedIn, or an exception message built from a request that
        contained the header. Belt and braces, on the single path that logs.
        """
        text = str(value)
        secret = self._session.reveal()
        if secret and secret in text:
            text = text.replace(secret, LinkedInSession.PLACEHOLDER)
        return text[:300]

    # -- Error builders -------------------------------------------------------

    def _upstream_error(
        self, resource: str, detail: str, *, code: str = "UPSTREAM_ERROR"
    ) -> ApiError:
        message = f"{resource}: {self._safe(detail)}"
        logger.warning("Voyager %s", message)
        return ApiError(code, log_detail=message)

    def _challenge(self, resource: str, detail: str) -> ApiError:
        message = f"{resource}: {self._safe(detail)}"
        logger.warning("Voyager challenge — %s", message)
        return ApiError("UPSTREAM_CHALLENGE", log_detail=message)

    def _not_found(self, public_id: str, detail: str) -> ApiError:
        logger.info(
            "Profile %s not found: %s", _loggable_public_id(public_id), detail
        )
        return ApiError("PROFILE_NOT_FOUND", log_detail=detail)

    @staticmethod
    def _public_identifier(payload: Mapping[str, Any]) -> str | None:
        """Dig ``publicIdentifier`` out of a ``me`` response.

        ``me`` is normalized like everything else: ``data`` holds a
        ``*miniProfile`` reference and the identifier lives on the entity in
        ``included``. Both are checked because the shape of ``me`` was not
        re-verified as carefully as the six that matter.
        """
        data = payload.get("data")
        if isinstance(data, Mapping):
            identifier = data.get("publicIdentifier")
            if isinstance(identifier, str) and identifier:
                return identifier
        included = payload.get("included")
        if isinstance(included, list):
            for entity in included:
                if isinstance(entity, Mapping):
                    identifier = entity.get("publicIdentifier")
                    if isinstance(identifier, str) and identifier:
                        return identifier
        return None


def _loggable_public_id(public_id: str) -> str:
    """A public id is caller-controlled; `repr` stops it forging a log record."""
    rendered = repr(public_id)
    return rendered if len(rendered) <= 64 else rendered[:64] + "...'"
