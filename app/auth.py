"""Bearer-token validation against the Keycloak realm's published JWKS.

Attached once, at the ``/api/v1`` router (see :mod:`app.api.v1`), so stories
5-8 cannot ship an unprotected endpoint by forgetting a decorator. ``/health``
is outside that router and stays open: the container healthcheck has no token.

What "validate" means here, in order:

1. The ``Authorization`` header parses as a bearer credential.
2. The JWS header names an algorithm on :data:`ALLOWED_ALGORITHMS` and a
   ``kid`` we hold a *signature* key for.
3. The signature verifies against that key.
4. Only then are claims read: ``iss``, ``aud``, ``exp``/``iat``, and Keycloak's
   ``typ``.

Nothing is trusted before step 3. The header *is* read before the signature is
checked, because JWS gives no other way to choose a key — but only ``alg`` and
``kid`` are taken from it, both are used solely to select a key, and neither
can make an invalid signature verify.

Every failure lands on the same 401 ``UNAUTHENTICATED`` envelope with the same
message. The specific reason is logged, never returned.
"""

from __future__ import annotations

import binascii
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable

import jwt
from fastapi import Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.errors import ApiError, unauthenticated

logger = logging.getLogger(__name__)


#: Cap on any attacker-controlled fragment reaching a log line.
LOGGABLE_MAX_CHARS = 120


def _loggable(value: Any) -> str:
    """Render an attacker-controlled value safe to put in a log record.

    ``repr`` escapes newlines and control characters, so a `kid` containing
    ``\n2026-08-27 WARNING ...`` cannot forge a second log record; the length
    cap stops a megabyte-long one from flooding the log. Both matter because
    every value passed here came out of a token this process has just refused
    to trust.
    """
    rendered = repr(str(value))
    if len(rendered) > LOGGABLE_MAX_CHARS:
        rendered = rendered[:LOGGABLE_MAX_CHARS] + "...(truncated)"
    return rendered


# --- Realm endpoints ---------------------------------------------------------
#
# Built from the two sides of the identity provider (see app/config.py):
# JWKS is fetched over the compose network, the issuer is the external name the
# caller minted through. Both bases are trailing-slash-normalised by Settings.

REALM_PATH = f"/realms/{settings.keycloak_realm}"

#: Where this process fetches signing keys. In-network, never proxied.
JWKS_URL = f"{settings.keycloak_server_url}{REALM_PATH}/protocol/openid-connect/certs"

#: The exact string a token's ``iss`` claim must equal. Not a prefix match.
EXPECTED_ISSUER = f"{settings.keycloak_issuer_url}{REALM_PATH}"

#: Where a caller mints, derived from the same issuer the validator enforces.
#: Used in the OpenAPI description of the bearer scheme below, so the document
#: story 9 ships names a mint URL that cannot drift from the accepted `iss`.
TOKEN_URL = f"{EXPECTED_ISSUER}/protocol/openid-connect/token"

#: The audience a token must carry. The realm export adds an audience mapper to
#: the client precisely so that real tokens carry this rather than Keycloak's
#: default ``account`` — see the story's Design Notes.
EXPECTED_AUDIENCE = settings.keycloak_client_id


# --- Algorithm policy --------------------------------------------------------

#: Asymmetric signature algorithms only.
#:
#: This list is the defence against the two classic JWT forgeries, and both are
#: closed by omission rather than by a special case:
#:
#: * ``none`` — an unsigned token. Absent here, so PyJWT refuses it.
#: * ``HS256`` — an HMAC forged with the realm's *public* key, which is
#:   published in the JWKS and therefore known to everyone. Absent here, so a
#:   token claiming ``alg: HS256`` never reaches verification.
#:
#: Keycloak's realm default is RS256 and the export pins the client to it; the
#: rest are listed so rotating the realm to another asymmetric algorithm is a
#: Keycloak-side change rather than a code change.
ALLOWED_ALGORITHMS: tuple[str, ...] = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
)

#: Claims that must be *present*, beyond being correct when present. Without
#: this, a token omitting ``exp`` would validate forever, and one omitting
#: ``aud`` would skip the audience check entirely rather than fail it.
REQUIRED_CLAIMS: tuple[str, ...] = ("exp", "iat", "iss", "aud", "sub")

#: Keycloak stamps the token type into a payload claim: ``Bearer`` on an access
#: token, ``ID`` on an ID token, ``Refresh`` on a refresh token. Checking it
#: refuses the well-known confusion where an ID token — same issuer, same
#: signature, and ``aud`` equal to the client id, so otherwise indistinguishable
#: — is presented as an access token.
EXPECTED_TOKEN_TYPE = "Bearer"

#: Tolerance for clock skew between Keycloak and this container, in seconds.
#: Small on purpose: they run on the same host.
CLOCK_SKEW_LEEWAY_SECONDS = 10


# --- JWKS cache --------------------------------------------------------------

JWKS_TTL_SECONDS = 600.0
JWKS_MIN_REFRESH_INTERVAL_SECONDS = 30.0
JWKS_FETCH_TIMEOUT_SECONDS = 5.0

#: Hard cap on the JWKS body. A realm's key set is a few kilobytes; anything
#: larger is a misconfiguration or a hostile response, and an unbounded
#: ``read()`` would let either one exhaust this container's memory.
JWKS_MAX_BYTES = 1_048_576


class SigningKeyUnavailable(Exception):
    """No usable signature key for this ``kid`` — unknown, or JWKS unreachable."""


def _fetch_jwks_over_http(url: str) -> dict[str, Any]:
    """GET the JWKS document. stdlib only, by dependency budget.

    One GET against a service on the compose network does not justify adding an
    HTTP client to the runtime surface, and the call is made off the event loop
    (the dependency below is a plain ``def``, which FastAPI runs in a
    threadpool), so blocking here blocks nothing else.
    """
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=JWKS_FETCH_TIMEOUT_SECONDS) as response:
        # Read one byte past the cap so an oversized body is detected rather
        # than silently truncated into unparseable JSON.
        body = response.read(JWKS_MAX_BYTES + 1)
    if len(body) > JWKS_MAX_BYTES:
        raise ValueError(f"JWKS body exceeded {JWKS_MAX_BYTES} bytes")
    return json.loads(body.decode("utf-8"))


class JwksCache:
    """Signature keys for one realm, cached with a refresh floor.

    Three behaviours are load-bearing:

    *Unknown ``kid`` triggers at most one refetch per
    :data:`JWKS_MIN_REFRESH_INTERVAL_SECONDS`.* Refetching is necessary — that
    is how a realm key rotation is picked up without a restart — but doing it
    unconditionally turns any attacker with a random ``kid`` into an
    amplification pump aimed at Keycloak. The floor bounds it.

    *A failed or empty fetch keeps the keys already held.* A Keycloak blip, or
    a realm mid-rotation briefly publishing nothing usable, should not
    invalidate tokens that are still perfectly good.

    *The network call happens outside the lock.* The refresh slot is claimed
    under the lock, so concurrent misses still produce a single fetch, but a
    slow Keycloak cannot serialise every authenticated request behind a 5s
    timeout.

    Note what this does NOT do: it never fetches from a URL derived from the
    token. The ``jku``/``x5u`` headers are ignored entirely — the only JWKS
    location is the one built from configuration at import time. An unknown
    ``kid`` is a rejection, never an invitation to go and trust a new key.
    """

    def __init__(
        self,
        url: str,
        *,
        fetcher: Callable[[str], dict[str, Any]] = _fetch_jwks_over_http,
        ttl_seconds: float = JWKS_TTL_SECONDS,
        min_refresh_interval_seconds: float = JWKS_MIN_REFRESH_INTERVAL_SECONDS,
    ) -> None:
        self._url = url
        self._fetch = fetcher
        self._ttl = ttl_seconds
        self._min_refresh_interval = min_refresh_interval_seconds
        self._lock = threading.Lock()
        self._keys: dict[str, jwt.PyJWK] = {}
        # `None`, not 0.0. `time.monotonic()`'s reference point is undefined —
        # on a platform where it counts from process or boot start it is a
        # small number, so `now - 0.0` would exceed neither the TTL nor the
        # refresh floor and the very first fetch would be suppressed. Every
        # legitimate token would then 401 for the first 30 seconds of uptime,
        # on some platforms only. `None` means "never", unambiguously.
        self._loaded_at: float | None = None
        self._attempted_at: float | None = None

    def signing_key(self, kid: str) -> jwt.PyJWK:
        """Return the signature key for ``kid``, refreshing when warranted."""
        with self._lock:
            key = self._keys.get(kid)
            claimed = self._claim_refresh(kid, time.monotonic())

        if not claimed:
            if key is None:
                raise SigningKeyUnavailable(kid)
            return key

        # Outside the lock on purpose: see the class docstring. Other threads
        # meanwhile see the refresh slot already claimed and serve from cache
        # rather than piling onto the same network call.
        document = self._fetch_document()

        with self._lock:
            if document is not None:
                self._install(document, time.monotonic())
            key = self._keys.get(kid)

        if key is None:
            raise SigningKeyUnavailable(kid)
        return key

    def _claim_refresh(self, kid: str, now: float) -> bool:
        """Decide whether *this* caller performs the fetch. Under the lock."""
        stale = self._loaded_at is None or (now - self._loaded_at) >= self._ttl
        unknown_kid = kid not in self._keys
        # The floor governs *every* refresh, not just the unknown-kid one. If
        # it governed only that, a Keycloak outage would leave the cache
        # permanently stale and make each request pay a fresh 5s timeout.
        may_refetch = (
            self._attempted_at is None
            or (now - self._attempted_at) >= self._min_refresh_interval
        )

        if not ((stale or unknown_kid) and may_refetch):
            return False

        self._attempted_at = now
        return True

    def _fetch_document(self) -> dict[str, Any] | None:
        """Fetch the JWKS, or ``None`` if it could not be read. No lock held.

        The except clause is deliberately wide. A JWKS fetch failing must never
        become a 500 — the matrix says 401, never 500 — and the ways this can
        fail are open-ended: transport errors, TLS errors, a body that is not
        JSON, a body that is not UTF-8, an oversized body. Catching
        ``Exception`` here trades a precise clause for a guarantee that holds.
        """
        try:
            document = self._fetch(self._url)
        except Exception as exc:
            # Keep whatever we already hold: an outage must not invalidate
            # tokens that are still signed by a key we already trust.
            logger.error("JWKS fetch from %s failed: %s", self._url, _loggable(exc))
            return None

        if not isinstance(document, dict):
            # A JWKS that parsed as a list or a bare string. `.get` on it would
            # be an AttributeError, and an AttributeError here is a 500.
            logger.error(
                "JWKS at %s is %s, not a JSON object", self._url, type(document).__name__
            )
            return None
        return document

    def _install(self, document: dict[str, Any], now: float) -> None:
        """Adopt a fetched key set. Under the lock."""
        keys = _signature_keys(document)
        if not keys:
            # Not merely "nothing to add" — the existing keys must SURVIVE. A
            # realm mid-rotation can briefly publish a document this filter
            # empties, and replacing the cache with {} would 401 every caller
            # until the next refresh window.
            logger.error("JWKS at %s contained no usable signature key", self._url)
            return

        self._keys = keys
        self._loaded_at = now
        logger.info("Loaded %d signature key(s) from %s", len(keys), self._url)


def _signature_keys(document: dict[str, Any]) -> dict[str, jwt.PyJWK]:
    """Parse a JWKS document into ``kid -> key``, keeping only signature keys.

    Keycloak publishes an encryption key (``use: "enc"``, ``alg: "RSA-OAEP"``)
    alongside the signing key in the same document. Filtering on ``use`` and on
    :data:`ALLOWED_ALGORITHMS` keeps a key that exists for a different purpose
    from ever being handed to signature verification.

    ``alg`` is OPTIONAL in RFC 7517, so an entry that omits it is kept and left
    for :func:`jwt.decode` to constrain — the same permissive treatment ``use``
    already gets. Only an entry that *states* an algorithm outside the policy is
    dropped. Being stricter here would silently discard a perfectly good key
    from any issuer that does not annotate its JWKS, and the resulting symptom —
    every token 401ing with "no published signature key" — points nowhere near
    the cause.
    """
    keys: dict[str, jwt.PyJWK] = {}
    for entry in document.get("keys") or []:
        if not isinstance(entry, dict):
            continue
        kid = entry.get("kid")
        if not isinstance(kid, str) or not kid:
            continue
        if entry.get("use") not in (None, "sig"):
            continue
        if entry.get("alg") is not None and entry.get("alg") not in ALLOWED_ALGORITHMS:
            continue
        try:
            keys[kid] = jwt.PyJWK.from_dict(entry)
        except (jwt.PyJWTError, binascii.Error, ValueError, TypeError, KeyError) as exc:
            # PyJWKError is only the documented failure. Malformed base64url in
            # `n`/`e` surfaces as binascii.Error or InvalidKeyError, and a
            # non-string member as TypeError — none of which is a reason to
            # 500. One bad entry is skipped; the rest of the set still loads.
            logger.warning(
                "Skipping unusable JWKS entry kid=%s: %s", _loggable(kid), _loggable(exc)
            )
    return keys


#: Process-wide cache. Replaced wholesale by the test suite so the matrix runs
#: entirely offline against a locally generated key.
jwks_cache = JwksCache(JWKS_URL)


# --- The dependency ----------------------------------------------------------

#: ``auto_error=False`` so a missing or non-bearer header arrives here as
#: ``None`` and leaves through the typed envelope. With ``auto_error=True``
#: FastAPI would answer first, with its own ``{"detail": ...}`` body — which is
#: not the shape ``response-schema.md`` fixes.
bearer_scheme = HTTPBearer(
    scheme_name="KeycloakBearer",
    description=(
        "Access token from the realm's client_credentials lane: "
        f"POST {TOKEN_URL} with grant_type=client_credentials."
    ),
    auto_error=False,
)


def require_claims(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
) -> dict[str, Any]:
    """Verify the bearer token and return its claims.

    Declared ``def``, not ``async def``: FastAPI runs a sync dependency in a
    threadpool, so the blocking JWKS fetch on a cache miss cannot stall the
    event loop, and no async HTTP client is needed to avoid that.

    Routes in stories 5-8 read the caller's identity with
    ``claims: dict = Depends(require_claims)`` and take ``claims["sub"]`` — the
    Keycloak subject CAP-4 binds a stored session to.
    """
    if credentials is None:
        # Covers both "no Authorization header at all" and a header whose
        # scheme is not Bearer (`Authorization: potato`).
        raise _reject("no bearer credentials in the Authorization header", presented=False)

    token = credentials.credentials
    if not token:
        raise _reject("empty bearer credential", presented=False)

    try:
        header = jwt.get_unverified_header(token)
    except (jwt.PyJWTError, binascii.Error, ValueError, TypeError) as exc:
        # `Bearer not.a.jwt` lands here. The clause is wider than PyJWTError
        # because the header is attacker-controlled bytes: malformed base64url
        # surfaces as binascii.Error, and a header that parses to a list rather
        # than an object as TypeError. Neither may become a 500.
        #
        # Note this reads the header only — no claim from this token is
        # trusted, and it is never decoded unverified.
        raise _reject(f"unparseable JWS header: {_loggable(exc)}") from exc

    if not isinstance(header, dict):
        raise _reject("JWS header is not a JSON object")

    algorithm = header.get("alg")
    if algorithm not in ALLOWED_ALGORITHMS:
        raise _reject(f"disallowed algorithm {_loggable(algorithm)}")

    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise _reject("token carries no kid, so no signing key can be selected")

    try:
        key = jwks_cache.signing_key(kid)
    except SigningKeyUnavailable:
        # A foreign realm's token dies here: correctly signed, but by a key this
        # realm's JWKS has never published.
        raise _reject(f"no published signature key for kid={_loggable(kid)}") from None

    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=list(ALLOWED_ALGORITHMS),
            audience=EXPECTED_AUDIENCE,
            issuer=EXPECTED_ISSUER,
            leeway=CLOCK_SKEW_LEEWAY_SECONDS,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
                "require": list(REQUIRED_CLAIMS),
            },
        )
    except (jwt.PyJWTError, TypeError, ValueError, AttributeError) as exc:
        # Expired, wrong issuer, wrong audience, bad signature, missing required
        # claim — one exception family, one response.
        #
        # TypeError is NOT hypothetical: PyJWT compares `exp`/`iat`/`nbf` with
        # arithmetic, so a token carrying `"exp": []` or `"exp": {}` raises
        # TypeError rather than the DecodeError the surrounding code implies,
        # and that is a 500 on a request the matrix says must be a 401.
        raise _reject(f"{type(exc).__name__}: {_loggable(exc)}") from exc

    if not isinstance(claims, dict):  # pragma: no cover - PyJWT always returns a dict
        raise _reject("token payload is not a JSON object")

    token_type = claims.get("typ")
    if token_type != EXPECTED_TOKEN_TYPE:
        raise _reject(f"token type {_loggable(token_type)} is not an access token")

    # PyJWT 2.13 does reject a future `iat` (ImmatureSignatureError), so the
    # bound below is belt-and-braces against a version that stops doing so —
    # it has changed before. What PyJWT does NOT check is the claim's TYPE: it
    # coerces with `int(payload["iat"])`, so `"iat": "1787818576"` (a string)
    # and `"iat": true` both validate happily. Requiring a real number closes
    # that, and makes the `iat` entry in REQUIRED_CLAIMS load-bearing rather
    # than decorative.
    issued_at = claims.get("iat")
    if not isinstance(issued_at, (int, float)) or isinstance(issued_at, bool):
        raise _reject(f"iat is not a numeric timestamp: {_loggable(issued_at)}")
    if math.isnan(issued_at) or issued_at > time.time() + CLOCK_SKEW_LEEWAY_SECONDS:
        raise _reject("token was issued in the future")

    return claims


def _reject(detail: str, *, presented: bool = True) -> ApiError:
    """Build the 401, logging the real reason where only an operator sees it."""
    logger.warning("Rejected request: %s", detail)
    return unauthenticated(
        log_detail=detail,
        # RFC 6750: the `error` parameter belongs on a rejected *credential*.
        # Omitted when nothing was presented, so the header does not imply the
        # caller sent something wrong when they sent nothing at all.
        www_authenticate='Bearer error="invalid_token"' if presented else "Bearer",
    )
