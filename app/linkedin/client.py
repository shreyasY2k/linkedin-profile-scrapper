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

from app.errors import (
    CAUSE_BAD_REQUEST,
    CAUSE_CLIENT_BUG,
    CAUSE_GONE,
    CAUSE_MALFORMED_BODY,
    CAUSE_MEMBER_MISMATCH,
    CAUSE_TRANSPORT,
    CAUSE_UNEXPECTED_STATUS,
    ApiError,
)

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

#: Asks the core request to deliver the referenced entities inline instead of
#: only their URNs. **Verified live on 2026-08-27**: with this, ``included``
#: carries ``com.linkedin.common.Geo`` entities joined from
#: ``Profile.geoLocation["*geo"]``, each with a ``defaultLocalizedName``
#: (measured: ``"Bengaluru, Karnataka, India"`` and a country-level ``"India"``).
#:
#: This is the whole reason ``location.region`` is populated, and it costs
#: **nothing**: it is a query parameter on a request the fetch already makes, so
#: the per-profile budget is still six calls. Resolving the geo URN with a
#: seventh call was the alternative, and it is Ask First.
#:
#: The id is version-pinned (`-77`) and therefore brittle by construction —
#: LinkedIn revises these without notice. :meth:`VoyagerClient._fetch_core`
#: falls back to the undecorated request when it is refused, because a nicety
#: must never take down the fetch it decorates.
CORE_DECORATION_ID = "com.linkedin.voyager.dash.deco.identity.profile.FullProfile-77"

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

#: Failures that are facts about the ACCOUNT, not about one sub-resource.
#:
#: A section that hits one of these aborts the whole fetch instead of degrading
#: into ``partial[]``. The reasoning is that all three are account-wide by
#: construction — if the session died, it died for all six calls; if LinkedIn
#: is throttling, it is throttling the account — so a 200 carrying whichever
#: sections happened to land first is not a partial answer, it is a wrong one.
#: And story 7 would cache it, so the lie would outlive the condition.
#:
#: Everything else (404, a malformed envelope, an unexpected status) really can
#: be specific to one sub-resource, and those still degrade.
SYSTEMIC_CODES = frozenset({"SESSION_EXPIRED", "RATE_LIMITED", "UPSTREAM_CHALLENGE"})

# --- What an UPSTREAM_ERROR actually was ---------------------------------------
#
# `_classify` collapses a 400, a 410, an unexpected status and an unparseable
# body into one `UPSTREAM_ERROR`, and it is right to: the caller can do nothing
# different with any of them, and `response-schema.md` has one row for "any
# other upstream failure". But *this module* can do something different with
# them, and used to be unable to — `_fetch_core` retried the core undecorated
# for every one, so a LinkedIn outage cost a second doomed call against an
# account that was already failing.
#
# The vocabulary itself lives in `app/errors.py` beside `ERROR_SPECS`, and is
# validated there: a cause is compared for MEMBERSHIP below, so an unregistered
# one would silently mean "not that case" rather than failing. Imported here
# because these are the values this module raises with.

#: The causes that look like a refused decoration, and therefore the *only*
#: ones that earn the seventh call.
#:
#: Deliberately a whitelist. `_fetch_core` used to retry on the code alone,
#: which meant every cause bought a second call; the boundary this story works
#: under says the retry may be narrowed but never widened, and a whitelist is
#: what makes adding a cause an explicit act rather than a side effect of
#: classifying something new as ``UPSTREAM_ERROR``.
#:
#: `CAUSE_CLIENT_BUG` is the one worth naming as absent. It means a function in
#: THIS file raised, which is not evidence that LinkedIn sent anything at all —
#: so retrying it would spend a live call against the account to run the same
#: bug a second time.
DECORATION_RETRY_CAUSES = frozenset(
    {CAUSE_BAD_REQUEST, CAUSE_GONE, CAUSE_MALFORMED_BODY}
)

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
#:
#: A value that parses but is not *positive* is dropped too. `Retry-After: 0`
#: propagated verbatim tells a throttled caller to retry immediately, which is
#: the one instruction guaranteed to make throttling worse.
RETRY_AFTER_RE = re.compile(r"^\d{1,7}$")

#: How many redirects one request may follow before the transport gives up.
#: Lower than urllib's default of 10: a Voyager call that needs more than a
#: couple of hops is being walked somewhere, and each hop is another chance to
#: hand the session cookie to a host that should not have it.
MAX_REDIRECTS = 4

#: Characters that may never appear in a value this module writes into a
#: header. CR and LF are header injection; `;` and `"` would break out of the
#: cookie value into another cookie or terminate a quoted one; the rest are
#: control characters `http.client` refuses outright.
#:
#: The refusal matters more than it looks. `http.client` raises `ValueError`
#: with the offending header value INSIDE the message, so a cookie carrying a
#: newline would put itself into an exception string, and a `ValueError` from
#: the transport becomes a *retryable* UPSTREAM_ERROR — which story 7 would
#: then stale-serve forever, for a cookie that can never work. Rejecting at
#: construction turns an unfixable retry loop into one accurate 428.
_HEADER_UNSAFE_RE = re.compile(r'[\x00-\x20\x7f;",\\]')

#: A real ``li_at`` is around 150 characters. The cap is generous rather than
#: tight because the value is opaque and LinkedIn may lengthen it; what it
#: stops is a megabyte of junk being assembled into a request header.
MAX_COOKIE_LENGTH = 4096

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

#: A URL scheme, per RFC 3986. Used only to decide whether a pasted string is
#: missing one; the scheme itself is still checked against http(s) afterwards.
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


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
        cleaned = (value or "").strip() if isinstance(value, str) else ""
        if not cleaned:
            # NO_SESSION, not SESSION_EXPIRED. The matrix requires a caller who
            # has stored nothing to be told something different from a caller
            # whose stored cookie LinkedIn has stopped accepting: the first
            # needs to supply a session, the second to supply a *new* one.
            raise ApiError(
                "NO_SESSION",
                log_detail="empty LinkedIn session cookie supplied to the client",
            )
        # SESSION_EXPIRED rather than NO_SESSION for a value that is present
        # but unusable. A session *was* supplied, so "you have not stored one"
        # would be wrong; and the remedy this code states — supply a new one —
        # is exactly right. Crucially it is `retryable: false`, so story 7 does
        # not stale-serve a caller forever over a cookie that can never work.
        #
        # The detail deliberately says nothing about WHICH character offended:
        # that sentence would be built from the cookie.
        if len(cleaned) > MAX_COOKIE_LENGTH:
            raise ApiError(
                "SESSION_EXPIRED",
                log_detail=f"session cookie is {len(cleaned)} characters, over the cap",
            )
        if _HEADER_UNSAFE_RE.search(cleaned):
            raise ApiError(
                "SESSION_EXPIRED",
                log_detail="session cookie contains a character illegal in a header",
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


class CrossHostRedirect(TransportError):
    """A redirect pointed off LinkedIn, and following it would leak the cookie."""


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


def is_linkedin_host(host: str) -> bool:
    """Whether ``host`` is linkedin.com or a subdomain of it.

    A *suffix* check anchored on a leading dot, never ``startswith`` and never
    a bare ``in``: ``linkedin.com.example.test`` is not LinkedIn, and treating
    it as such is how a redirect walks the session cookie off to an attacker.
    """
    host = (host or "").lower()
    return host == LINKEDIN_HOST or host.endswith("." + LINKEDIN_HOST)


class LinkedInRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuses to follow a redirect that leaves LinkedIn.

    This exists because of a specific, verified stdlib behaviour: ``urllib``
    follows redirects automatically and **forwards headers the caller set
    manually to the redirect target**. It strips only content headers
    (``Content-Length``, ``Content-Type``, ``Transfer-Encoding``); a manually
    supplied ``Cookie`` header survives the hop and is handed to the new host
    verbatim.

    LinkedIn answers an unauthenticated request with a redirect, and a redirect
    target is not something this codebase controls. Without this handler, the
    module's central claim — that the session cookie is written in exactly one
    place and goes to exactly one host — is false, and the failure is silent:
    the request succeeds, and the cookie is simply also somewhere else.

    ``max_redirections`` is lowered from urllib's 10 to :data:`MAX_REDIRECTS`.
    """

    max_redirections = MAX_REDIRECTS

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        try:
            host = urllib.parse.urlsplit(newurl).hostname or ""
        except ValueError:
            host = ""
        if not is_linkedin_host(host):
            # Raised, not returned-as-None: returning None makes urllib treat
            # the redirect as a final response and hand the *wall page* back as
            # if it were data. This must be loud.
            raise CrossHostRedirect(
                f"refused a {code} redirect to non-LinkedIn host {host!r}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener() -> urllib.request.OpenerDirector:
    """One opener, carrying the redirect policy. Replaced wholesale in tests."""
    return urllib.request.build_opener(LinkedInRedirectHandler())


#: Module-level so the redirect policy cannot be bypassed by calling
#: ``urlopen`` directly — there is no other opener in this module.
_OPENER = _build_opener()


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
        with _OPENER.open(request, timeout=timeout) as response:
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
    except CrossHostRedirect:
        # Already the right type and already says the right thing. Re-raised
        # explicitly so the OSError clause below cannot reword it.
        raise
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
    #
    # Detected by matching a scheme, not by looking for `//` anywhere in the
    # string: a schemeless URL whose query or fragment happens to contain `//`
    # (`linkedin.com/in/ada#a//b`) would be left alone by the naive test and
    # then rejected as "scheme '' is not http(s)" — an error message pointing
    # at something the caller did not do.
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif not _SCHEME_RE.match(candidate):
        candidate = "https://" + candidate

    try:
        parts = urllib.parse.urlsplit(candidate)
    except ValueError as exc:
        raise _invalid_url(f"unparseable URL: {type(exc).__name__}") from exc

    if parts.scheme not in ("http", "https"):
        raise _invalid_url(f"scheme {parts.scheme!r} is not http(s)")

    host = (parts.hostname or "").lower()
    if not is_linkedin_host(host):
        # Explicitly logged: `linkedin.com.evil.test` reaching a fetch would be
        # an SSRF, and this is the line that stops it. The same predicate
        # governs redirects, so the two cannot drift apart.
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
    # Lower-cased, because LinkedIn treats public ids case-insensitively and
    # this string becomes an identity downstream. Without it `/in/Ada` and
    # `/in/ada` are two different people to this service: two story-7 cache
    # keys, two six-call fetches of the same person, and two `public_id`
    # values in responses that describe one profile.
    #
    # `.lower()` rather than `.casefold()`: casefold rewrites characters
    # (`ß` -> `ss`), and this is an identifier that has to survive round-tripping
    # into a URL, not a string being compared for linguistic equality.
    return public_id.lower()


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


def is_collection_envelope(payload: Mapping[str, Any]) -> bool:
    """Whether ``payload`` is a normalized collection response at all.

    The distinction this draws is the one the whole story turns on. A 200
    carrying a body with no ``data``, or a ``data`` with no ``*elements``, is
    **unreadable** — the shape changed, or something other than a collection
    came back. Reading zero elements out of it and reporting "this section is
    empty" tells a caller, in `response-schema.md`'s own terms, that the
    profile *has none* of something we simply could not read.

    So a malformed envelope is a failure, never an empty result.
    """
    data = payload.get("data")
    return isinstance(data, Mapping) and isinstance(data.get("*elements"), list)


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
    #: How many of the ``*elements`` URNs actually resolved against
    #: ``included``. Normally equal to ``element_count``; when it is lower,
    #: entities were referenced and not delivered, and the difference is a
    #: silently dropped entry rather than an absent one. Recorded so story 6
    #: reports the shortfall instead of shortening the list without saying so.
    resolved_count: int | None = None
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
            if section.ok and _looks_truncated(section)
        ]

    @property
    def unresolved_sections(self) -> list[str]:
        """Sections that referenced entities ``included`` did not deliver.

        A third kind of incompleteness, distinct from failed and from
        truncated: the call worked, the list is the right length, and some of
        the entries are simply missing from the payload.
        """
        return [
            name
            for name, section in self.sections.items()
            if section.ok
            and section.resolved_count is not None
            and section.element_count is not None
            and section.resolved_count < section.element_count
        ]


def _looks_truncated(section: SectionFetch) -> bool:
    """Whether ``section`` is missing entries the first page did not carry.

    Two signals, and the second is not paranoia:

    * ``paging.total`` exceeds what came back. The direct case.
    * ``total`` is **absent** and exactly :data:`SECTION_PAGE_SIZE` elements
      came back. A response that fills the page precisely is what truncation
      looks like when the total is not reported, and calling it complete is a
      coin flip on a real person's history. Treating it as truncated can only
      cost a caller an unnecessary caveat; the other error publishes a partial
      career as a whole one.
    """
    if section.element_count is None:
        return False
    if section.reported_total is not None:
        return section.reported_total > section.element_count
    return section.element_count >= SECTION_PAGE_SIZE


# --- The client --------------------------------------------------------------


class VoyagerClient:
    """Retrieves raw profile JSON under one LinkedIn session.

    One instance per session, constructed per request in story 5 from the
    caller's stored cookie. It holds no cache and no state beyond the session
    and a call counter.

    ``transport`` is injectable so that the whole edge-case matrix is testable
    offline. The default is the stdlib one; the test suite never installs a
    transport that can reach a network.

    ``None`` means "the module's transport", resolved **here rather than in the
    signature**. A default argument is bound once, when this ``def`` executes,
    so ``transport=urllib_transport`` captured the function object at import and
    a test replacing :data:`urllib_transport` on the module changed nothing —
    silently, since the substitute was simply never called. One test did exactly
    that and had been reaching the real linkedin.com from inside the offline
    suite ever since. Resolving at construction makes the module attribute the
    single answer to "what does this client call", which is what every reader
    already assumed.
    """

    def __init__(
        self,
        cookie: str,
        *,
        transport: Transport | None = None,
        jsessionid: str = DEFAULT_JSESSIONID,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._session = LinkedInSession(cookie)
        self._transport = urllib_transport if transport is None else transport
        # Same header-safety rules as the cookie, different failure type, and
        # the difference is deliberate. The cookie is caller data, so a bad one
        # is a 428 the caller can act on. The CSRF token is a code-level
        # argument with a safe default that no caller supplies — a bad one is a
        # bug in this repository, and a bug should be loud and unhandled here
        # rather than dressed up as a session problem for someone else to
        # debug. It is validated all the same because it lands in the same
        # header and would inject just as happily.
        if not isinstance(jsessionid, str) or not jsessionid.strip():
            raise ValueError("jsessionid must be a non-empty string")
        if len(jsessionid) > MAX_COOKIE_LENGTH or _HEADER_UNSAFE_RE.search(jsessionid):
            raise ValueError("jsessionid contains a character illegal in a header")
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
        # Stamped here, not after the sections return. `fetched_at` means "when
        # this data was read from LinkedIn" and it is, under unbounded
        # stale-serve, the caller's ONLY staleness signal — so it has to be the
        # read time, not the time the last of six concurrent calls happened to
        # finish. Timezone-aware always: a naive timestamp serialises without
        # an offset and silently becomes "some local time" to a consumer.
        fetched_at = datetime.now(timezone.utc)

        profile = self._core_profile(core, public_id)
        profile_urn = profile.get("entityUrn")
        if not isinstance(profile_urn, str) or not profile_urn:
            raise self._upstream_error(
                CORE_RESOURCE,
                "Profile entity carried no entityUrn",
                cause=CAUSE_MALFORMED_BODY,
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
            fetched_at=fetched_at,
        )

    # -- Fan-out -------------------------------------------------------------

    def _core_profile(
        self, core: Mapping[str, Any], public_id: str
    ) -> dict[str, Any]:
        """The ``Profile`` this query asked for — not merely the first one seen.

        Two checks, and both guard the single worst thing this system can do,
        which is to answer (and, through story 7, cache indefinitely) one
        person's profile under another person's URL.

        *Resolved through* ``*elements``, not scanned out of ``included``.
        ``included`` is an unordered pool that can carry entities the query did
        not ask about; "the first thing in it shaped like a Profile" is a
        coincidence, not an identity. The URN in ``*elements`` is the response's
        own statement of what it returned.

        *Cross-checked against the requested public id.* If the entity names a
        different person, this fails loudly rather than serving them. See the
        note in the deferred-work log: a profile that has changed its vanity
        URL could in principle answer under an old id, and this would refuse
        it. Refusing to answer is recoverable; answering with the wrong human
        being is not.

        **And the refusal is not retryable.** ``UPSTREAM_ERROR`` is retryable in
        ``response-schema.md``, and until story 8 this guard inherited that —
        which meant the refusal was raised *inside* the stale-serve boundary and
        answered with a cached 200 whenever a record existed. So the one guard
        against publishing the wrong human being was, in exactly the case where
        it mattered, disabled by the code it raised. A response naming a
        different member is permanent: a vanity URL that now belongs to somebody
        else does not stop belonging to them because the caller asked twice.
        The endpoint's own guard in ``app/api/v1/profile.py`` narrows the same
        way, and the two are independent on purpose.
        """
        elements = resolve_elements(core)
        profile = next(
            (
                entity
                for entity in elements
                if str(entity.get("$type", "")).endswith(PROFILE_TYPE_SUFFIX)
            ),
            None,
        )
        if profile is None:
            raise self._not_found(
                public_id, "no Profile entity resolved from the core *elements"
            )

        identifier = profile.get("publicIdentifier")
        if isinstance(identifier, str) and identifier.lower() != public_id:
            logger.error(
                "Core lookup for %s returned publicIdentifier %s — refusing to "
                "answer with a different member's profile.",
                _loggable_public_id(public_id),
                _loggable_public_id(identifier),
            )
            raise self._upstream_error(
                CORE_RESOURCE,
                "core response named a different member",
                cause=CAUSE_MEMBER_MISMATCH,
                retryable=False,
            )
        return profile

    async def _fetch_core(self, public_id: str) -> dict[str, Any]:
        """The one call whose failure aborts the whole fetch.

        Made **decorated** first (:data:`CORE_DECORATION_ID`), which is what
        delivers the ``Geo`` entities ``location.region`` is built from, at no
        extra call.

        The decoration id is version-pinned and LinkedIn revises these without
        notice, so a refusal falls back to the plain request and the profile
        comes back without a region. The fallback is deliberately narrow — an
        ``UPSTREAM_ERROR`` whose ``cause`` is one of
        :data:`DECORATION_RETRY_CAUSES`, which is what a rejected or withdrawn
        decoration looks like (400, 410, an unparseable body).

        It must NOT retry a systemic failure. An expired session, a throttle or
        a challenge is a fact about the account: a second call cannot succeed,
        and spending one against an account LinkedIn is already throttling makes
        the condition worse. Nor a 404 — that is a statement about the member,
        not about the decoration. So the seven-call case is exactly "the
        decoration broke", and the ordinary paths still cost six.

        The ``cause`` test is what makes that last sentence true rather than
        approximately true. Story 4 could only test the *code*, and
        ``UPSTREAM_ERROR`` also covers a 500, a 503 and a connection reset —
        none of which the decoration had anything to do with, and each of which
        bought a second doomed call. That was recorded as this story's to fix,
        and it narrows the retry rather than widening it.
        """
        try:
            return await self._fetch_core_once(public_id, decorated=True)
        except ApiError as exc:
            if exc.code != "UPSTREAM_ERROR" or exc.cause not in DECORATION_RETRY_CAUSES:
                raise
            logger.warning(
                "Decorated core request failed (%s); retrying undecorated. "
                "location.region will be absent. If this persists, "
                "CORE_DECORATION_ID in app/linkedin/client.py is out of date.",
                exc.log_detail or exc.code,
            )
        return await self._fetch_core_once(public_id, decorated=False)

    async def _fetch_core_once(
        self, public_id: str, *, decorated: bool
    ) -> dict[str, Any]:
        """One core request, decorated or not, validated the same way either way."""
        parameters: dict[str, Any] = {"q": "memberIdentity", "memberIdentity": public_id}
        if decorated:
            parameters["decorationId"] = CORE_DECORATION_ID
        query = urllib.parse.urlencode(parameters)
        payload = await self._request(f"{CORE_RESOURCE}?{query}", resource=CORE_RESOURCE)

        if not is_collection_envelope(payload):
            # A 200 that is not a collection at all. UPSTREAM_ERROR, not
            # PROFILE_NOT_FOUND: "we could not read this" and "this person does
            # not exist" are different claims and only one of them is true.
            raise self._upstream_error(
                CORE_RESOURCE,
                "response was not a normalized collection envelope",
                cause=CAUSE_MALFORMED_BODY,
            )

        if not element_urns(payload):
            # A well-formed id that does not exist answers 200 with an empty
            # elements list rather than a 404. Reporting that as an empty
            # profile would be the worst possible outcome: a confident,
            # well-formed answer about a person who is not there.
            raise self._not_found(public_id, "core response listed zero elements")
        return payload

    async def _fetch_sections(self, profile_urn: str) -> dict[str, SectionFetch]:
        """Fan the five section calls out concurrently.

        ``return_exceptions=True`` because a systemic failure raised by one
        section must not leave the other four running unobserved. Every result
        is collected, then the systemic ones are re-raised.
        """
        names = list(SECTION_RESOURCES)
        results = await asyncio.gather(
            *(self._fetch_section(name, profile_urn) for name in names),
            return_exceptions=True,
        )

        sections: dict[str, SectionFetch] = {}
        systemic: ApiError | None = None
        for name, result in zip(names, results):
            if isinstance(result, SectionFetch):
                sections[name] = result
            elif isinstance(result, ApiError):
                # First one wins; they will almost always be the same code,
                # because the condition is account-wide by definition.
                systemic = systemic or result
            elif isinstance(result, BaseException):
                # A bug in this module, not an upstream failure — but CAP-6
                # still forbids it reaching a caller naked.
                systemic = systemic or self._upstream_error(
                    SECTION_RESOURCES[name],
                    f"section task raised {type(result).__name__}: {self._safe(result)}",
                    cause=CAUSE_CLIENT_BUG,
                )

        if systemic is not None:
            raise systemic
        return sections

    async def _fetch_section(self, name: str, profile_urn: str) -> SectionFetch:
        """One sub-resource.

        Raises only for a **systemic** failure — see :data:`SYSTEMIC_CODES`.
        Everything else is a recorded outcome, so the other four sections still
        return and story 6 reports this one in ``partial[]``.
        """
        resource = SECTION_RESOURCES[name]
        query = urllib.parse.urlencode(
            {"q": "viewee", "profileUrn": profile_urn, "count": SECTION_PAGE_SIZE}
        )
        try:
            payload = await self._request(f"{resource}?{query}", resource=resource)
        except ApiError as exc:
            if exc.code in SYSTEMIC_CODES:
                # An expired session, a throttle, or a challenge is a fact
                # about the ACCOUNT, not about this sub-resource. The other
                # four are failing for the same reason at the same moment, and
                # answering 200 with half a profile would be a cheerful lie —
                # one story 7 would then cache and keep serving. Abort.
                logger.warning(
                    "Section %s (%s) hit a systemic %s; aborting the fetch",
                    name, resource, exc.code,
                )
                raise
            # A genuinely per-section failure. The typed code is preserved
            # rather than flattened: story 6 wants to know a section is missing
            # because the endpoint was withdrawn, not merely that it is missing.
            logger.warning(
                "Section %s (%s) failed: %s", name, resource, exc.log_detail or exc.code
            )
            return SectionFetch(
                name=name, resource=resource, ok=False, error_code=exc.code
            )

        if not is_collection_envelope(payload):
            # A 200 whose body is not a collection is UNREADABLE, not empty.
            # Counting zero elements out of it and letting story 6 map that to
            # `[]` states that the profile has none of something that was never
            # read — the precise absent-versus-unreadable error this story
            # exists to prevent.
            logger.warning(
                "Section %s (%s) returned 200 with a malformed envelope", name, resource
            )
            return SectionFetch(
                name=name, resource=resource, ok=False, error_code="UPSTREAM_ERROR"
            )

        urns = element_urns(payload)
        resolved = resolve_elements(payload)
        if len(resolved) < len(urns):
            logger.warning(
                "Section %s (%s) referenced %d elements but included only %d",
                name, resource, len(urns), len(resolved),
            )

        return SectionFetch(
            name=name,
            resource=resource,
            ok=True,
            payload=payload,
            # Recorded independently of `ok`. Zero here means "LinkedIn said
            # zero", which is emphatically not the same as "not retrievable".
            element_count=len(urns),
            reported_total=reported_total(payload),
            resolved_count=len(resolved),
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
            raise self._upstream_error(
                resource,
                f"transport failed: {self._safe(exc)}",
                cause=CAUSE_TRANSPORT,
            )
        except ApiError:
            raise
        except Exception as exc:
            # A transport that raises something unexpected must not become a
            # naked 500. CAP-6 permits no unhandled exception to reach a caller.
            raise self._upstream_error(
                resource,
                f"transport raised {type(exc).__name__}: {self._safe(exc)}",
                cause=CAUSE_TRANSPORT,
            )

        try:
            return self._classify(response, resource=resource)
        except ApiError:
            raise
        except Exception as exc:
            # Classification is inside the guarantee too, and not
            # hypothetically: `json.loads` raises `RecursionError` on a deeply
            # nested body — reachable well under MAX_BODY_BYTES, since nesting
            # costs one byte per level — and `RecursionError` is not in the
            # narrow clause `_classify` catches. Guarding only the transport
            # call left a body LinkedIn could never send but an attacker-
            # positioned proxy could, turning into a naked 500.
            raise self._upstream_error(
                resource,
                f"classifying the response raised {type(exc).__name__}: {self._safe(exc)}",
                # NOT `malformed-body`, which is in the retry whitelist. What
                # happened here is that `_classify` itself threw — a bug in this
                # file, or a body crafted to make it throw — and neither is a
                # refused decoration. Tagging it as a bad body bought the second
                # doomed call the whitelist exists to prevent.
                cause=CAUSE_CLIENT_BUG,
            )

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
        4. **A challenge on ``me`` is not a challenge.** See the branch — it is
           the one place the resource, not the response, decides the code.
        """
        status = response.status

        if status == 429:
            raise self._rate_limited(response, resource)

        if status in (401, 403):
            # The cookie was presented and refused. Not NO_SESSION — one was
            # supplied — and not UPSTREAM_ERROR, because the caller can fix it.
            #
            # THIS MUST STAY ABOVE THE CHALLENGE CHECK. A dead `li_at` is most
            # often signalled by LinkedIn bouncing the request to the login
            # page, so the landing URL matches a wall marker and the body is
            # HTML — every signal a challenge has. Classifying on that first
            # turns the commonest expiry into UPSTREAM_CHALLENGE, which is
            # `retryable: true`, which story 7 stale-serves unboundedly. The
            # caller would then be fed ever-older cached data forever and never
            # once told to store a new cookie. An explicit refusal outranks the
            # scenery it was delivered with.
            raise ApiError(
                "SESSION_EXPIRED",
                log_detail=f"{resource} returned {status}; LinkedIn refused the session",
            )

        if status == LINKEDIN_BOT_STATUS:
            # Deliberately NOT split by resource the way the wall below is. 999
            # means "we think you are a bot": it is a statement about where the
            # request came from, decided at the edge before any session is
            # considered, and it arrives identically for a brand-new cookie and
            # a dead one. Reporting it as SESSION_EXPIRED would send a caller to
            # replace a credential that works, which is the same class of lie as
            # the one this story is fixing, pointed the other way.
            raise self._challenge(resource, f"status {LINKEDIN_BOT_STATUS}")

        challenge = self._challenge_reason(response)
        if challenge is not None:
            if resource == ME_RESOURCE:
                # `me` IS NOT LIKE A PROFILE FETCH, and this asymmetry is the
                # whole justification for the branch.
                #
                # A wall on a profile URL says nothing about the cookie:
                # LinkedIn serves that page to perfectly healthy sessions coming
                # from a datacenter IP, which is what this service is. So a
                # challenge there stays retryable and story 7 absorbs it.
                #
                # `me` describes the session's OWN OWNER. There is no third
                # party involved, no profile whose visibility could be the
                # explanation — the only question the request asks is "who is
                # holding this cookie", and a wall in place of that answer is
                # evidence about the cookie itself. Verified live: a
                # deliberately dead `li_at` answers `me` with a 200 redirect to
                # `/authwall`, which the previous classification reported as
                # `UPSTREAM_CHALLENGE`. Retryable — so `PUT /api/v1/session`
                # recorded "could not tell" and the caller was never told the
                # value they had just pasted was already dead.
                logger.info(
                    "Voyager me answered with a wall (%s); treating it as a dead "
                    "session rather than a challenge",
                    self._safe(challenge),
                )
                raise ApiError(
                    "SESSION_EXPIRED",
                    log_detail=f"{ME_RESOURCE}: {self._safe(challenge)}",
                )
            raise self._challenge(resource, challenge)

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
            raise self._upstream_error(
                resource, "endpoint returned 410 Gone", cause=CAUSE_GONE
            )

        if status == 400:
            # Split out of the `!= 200` case below solely to carry the cause: a
            # 400 is LinkedIn refusing the request as malformed, and the only
            # thing this client ever sends that LinkedIn could call malformed is
            # a decoration id it has withdrawn.
            raise self._upstream_error(
                resource, "returned 400 Bad Request", cause=CAUSE_BAD_REQUEST
            )

        if status != 200:
            raise self._upstream_error(
                resource,
                f"unexpected status {status}",
                cause=CAUSE_UNEXPECTED_STATUS,
            )

        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._upstream_error(
                resource,
                f"body claimed JSON but did not parse: {type(exc).__name__}",
                cause=CAUSE_MALFORMED_BODY,
            ) from exc

        if not isinstance(payload, dict):
            raise self._upstream_error(
                resource,
                f"payload is {type(payload).__name__}, not an object",
                cause=CAUSE_MALFORMED_BODY,
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

        if not response.is_json:
            # Reached only for statuses that are not an explicit refusal — 401
            # and 403 are already gone by the time this runs — so a non-JSON
            # body here really is a wall rather than the rendering of a "no".
            return f"content-type {response.content_type.split(';')[0].strip()!r} is not JSON"
        return None

    def _rate_limited(self, response: VoyagerResponse, resource: str) -> ApiError:
        """Throttled. Propagate ``Retry-After`` only when it is a sane integer."""
        headers: dict[str, str] = {}
        retry_after = response.headers.get("retry-after", "").strip()
        if RETRY_AFTER_RE.match(retry_after) and int(retry_after) > 0:
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
        self,
        resource: str,
        detail: str,
        *,
        code: str = "UPSTREAM_ERROR",
        cause: str | None = None,
        retryable: bool | None = None,
    ) -> ApiError:
        message = f"{resource}: {self._safe(detail)}"
        logger.warning("Voyager %s", message)
        return ApiError(code, log_detail=message, cause=cause, retryable=retryable)

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
        ``*miniProfile`` **reference** and the identifier lives on the entity
        that reference names, inside ``included``.

        The reference is resolved rather than scanned past. Scanning
        ``included`` for the first entity carrying any ``publicIdentifier``
        happens to work when the array holds one entity and silently returns
        *somebody else* when it holds more — and this function is the entire
        basis of the live check's safety property, that the one permitted live
        fetch cannot be aimed at a third party. "Usually the right person" is
        not a safety property.
        """
        data = payload.get("data")
        if not isinstance(data, Mapping):
            return None

        # `data` may carry it directly on some shapes; that is unambiguous.
        identifier = data.get("publicIdentifier")
        if isinstance(identifier, str) and identifier:
            return identifier

        reference = data.get("*miniProfile")
        if not isinstance(reference, str) or not reference:
            return None

        included = payload.get("included")
        if not isinstance(included, list):
            return None
        for entity in included:
            if not isinstance(entity, Mapping):
                continue
            if entity.get("entityUrn") != reference:
                continue
            identifier = entity.get("publicIdentifier")
            if isinstance(identifier, str) and identifier:
                return identifier
        return None


def _loggable_public_id(public_id: str) -> str:
    """A public id is caller-controlled; `repr` stops it forging a log record."""
    rendered = repr(public_id)
    return rendered if len(rendered) <= 64 else rendered[:64] + "...'"
