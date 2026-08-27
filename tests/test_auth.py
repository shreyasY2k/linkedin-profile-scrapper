"""The story-3 edge-case matrix, as tests.

| Scenario         | Input / State                          | Expected             |
|------------------|----------------------------------------|----------------------|
| Valid token      | `Authorization: Bearer <valid>`        | route runs, `sub` up |
| No header        | none                                   | 401 UNAUTHENTICATED  |
| Malformed header | `potato` / `Bearer not.a.jwt`          | 401, never 500       |
| Expired token    | `exp` in the past                      | 401 UNAUTHENTICATED  |
| Foreign realm    | signed by another key / another issuer  | 401 UNAUTHENTICATED  |
| Wrong audience   | valid realm token for another client    | 401 UNAUTHENTICATED  |
| Liveness         | `GET /health`, no token                | 200 {"status":"ok"}  |

Every token in this file is signed by a key generated here, in-process. The
suite makes no network call and needs no running Keycloak — which is the point:
it must be able to fail on a laptop with the stack down.

The row the matrix names but this file cannot cover is minting: "evaluator
mints a token" is an integration fact about a running realm, verified with the
`curl` in the story's Verification block and documented in the README.
"""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient

from app import auth
from app.api.v1 import router as v1_router
from app.auth import require_claims
from app.config import settings
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
REALM_EXPORT = REPO_ROOT / "deploy" / "keycloak" / "realm-linkedin.json"

#: Declares `Depends(require_claims)` itself, and reads the claims.
PROBE_PATH = "/api/v1/__auth_probe__"

#: Declares NOTHING. Its only protection is the router-level dependency in
#: `app/api/v1/__init__.py`, which makes it the one route in this file that can
#: observe whether that dependency exists. Every other probe here would keep
#: passing with the router-level `dependencies=[...]` deleted, because each
#: names `require_claims` in its own signature — which is precisely the
#: regression the module docstring of `app/api/v1/__init__.py` claims is
#: impossible, and precisely what stories 5-8 would ship by omission.
INHERITED_PATH = "/api/v1/__inherits_auth__"

SUBJECT = "615225e6-fb6a-4d02-a323-7b1fe4b6e88b"


# --- Local signing material --------------------------------------------------
#
# Two keypairs: the one the "realm" publishes, and one that stands in for a
# foreign realm — correctly signed, entirely legitimate, and not ours.

_REALM_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_FOREIGN_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

REALM_KID = "realm-signing-key"
FOREIGN_KID = "foreign-signing-key"


def _public_jwk(private_key: rsa.RSAPrivateKey, kid: str, **overrides: Any) -> dict[str, Any]:
    entry = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    entry.update({"kid": kid, "use": "sig", "alg": "RS256"})
    entry.update(overrides)
    return entry


REALM_JWK = _public_jwk(_REALM_KEY, REALM_KID)

#: Shaped exactly like Keycloak's: the signing key next to an encryption key
#: that must never be handed to signature verification.
REALM_JWKS = {
    "keys": [
        REALM_JWK,
        _public_jwk(_FOREIGN_KEY, "realm-encryption-key", use="enc", alg="RSA-OAEP"),
    ]
}


class RecordingFetcher:
    """A JWKS fetcher that counts calls, so refetch policy is observable."""

    def __init__(self, document: dict[str, Any] | None = None) -> None:
        self.document = document if document is not None else REALM_JWKS
        self.urls: list[str] = []
        self.error: Exception | None = None

    def __call__(self, url: str) -> dict[str, Any]:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.document

    @property
    def calls(self) -> int:
        return len(self.urls)


@pytest.fixture(name="fetcher")
def _fetcher(monkeypatch: pytest.MonkeyPatch) -> RecordingFetcher:
    """Point the process-wide cache at the local key set, offline."""
    fetcher = RecordingFetcher()
    monkeypatch.setattr(
        auth, "jwks_cache", auth.JwksCache(auth.JWKS_URL, fetcher=fetcher)
    )
    return fetcher


@pytest.fixture(name="client")
def _client(fetcher: RecordingFetcher) -> TestClient:
    """A probe route under `/api/v1`, exercised through the real dependency.

    Reuses story 1's probe-route pattern (`tests/test_health.py`) rather than
    inventing public surface: `response-schema.md` names the only three real
    routes and they belong to stories 5-8.

    `raise_server_exceptions=False` matters — it turns an unhandled exception
    into a 500 *response*, so "never a 500" is an assertion that can fail
    rather than an error that aborts the test.
    """
    probe = APIRouter()

    @probe.get("/__auth_probe__")
    async def _probe(claims: dict = Depends(require_claims)) -> dict[str, str]:
        return {"sub": claims["sub"]}

    @probe.get("/__inherits_auth__")
    async def _inherits() -> dict[str, bool]:
        # No dependency, no parameters, no claims. If this answers 200 without
        # a token, the boundary is not on the router.
        return {"reached": True}

    # Snapshot/restore rather than filtering by path: since FastAPI 0.141
    # `include_router` leaves a lazy `_IncludedRouter` marker that carries no
    # `path`, so a path-based filter silently leaks the probe into every later
    # test in the session.
    saved = list(v1_router.routes)
    v1_router.include_router(probe)
    try:
        yield TestClient(create_app(), raise_server_exceptions=False)
    finally:
        v1_router.routes[:] = saved


_ABSENT = object()


def make_token(
    *,
    key: rsa.RSAPrivateKey = _REALM_KEY,
    kid: str | None = REALM_KID,
    algorithm: str = "RS256",
    **claim_overrides: Any,
) -> str:
    """A token shaped like a real Keycloak client_credentials access token.

    Claim defaults mirror one minted against the running realm on 2026-08-27.
    Pass `claim=_ABSENT` to omit a claim entirely.
    """
    now = int(time.time())
    claims: dict[str, Any] = {
        "exp": now + 300,
        "iat": now,
        "iss": auth.EXPECTED_ISSUER,
        "aud": auth.EXPECTED_AUDIENCE,
        "sub": SUBJECT,
        "typ": "Bearer",
        "azp": settings.keycloak_client_id,
        "scope": "profile email",
    }
    claims.update(claim_overrides)
    claims = {name: value for name, value in claims.items() if value is not _ABSENT}

    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(claims, key, algorithm=algorithm, headers=headers)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _b64(part: dict[str, Any]) -> str:
    """One base64url JWS segment, unpadded — for hand-built forgeries."""
    raw = json.dumps(part, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def assert_unauthenticated(response: Any) -> None:
    """The typed envelope from `response-schema.md`, in full."""
    assert response.status_code == 401, response.text
    assert response.json() == {
        "error": {
            "code": "UNAUTHENTICATED",
            "message": "Missing or invalid bearer token.",
            "retryable": False,
        }
    }
    # RFC 6750 requires the challenge on a 401 from a bearer resource.
    assert response.headers["www-authenticate"].startswith("Bearer")


# --- The boundary: auth is inherited from the router, not declared per route --
#
# These four are the highest-value tests in the file. Deleting
# `dependencies=[Depends(require_claims)]` from `app/api/v1/__init__.py` must
# fail here and nowhere else, because nothing else in the suite would notice.


def test_a_route_declaring_no_dependency_still_requires_a_token(
    client: TestClient,
) -> None:
    """Stories 5-8 cannot ship an unprotected endpoint by forgetting one."""
    assert_unauthenticated(client.get(INHERITED_PATH))


def test_a_route_declaring_no_dependency_is_reachable_with_a_token(
    client: TestClient,
) -> None:
    """The negative above must be failing for the right reason."""
    response = client.get(INHERITED_PATH, headers=bearer(make_token()))

    assert response.status_code == 200, response.text
    assert response.json() == {"reached": True}


def test_a_route_declaring_no_dependency_rejects_a_bad_token(
    client: TestClient,
) -> None:
    """Inherited validation is the real thing, not a presence check."""
    assert_unauthenticated(
        client.get(INHERITED_PATH, headers=bearer(make_token(key=_FOREIGN_KEY)))
    )


def test_the_v1_router_carries_the_dependency_itself() -> None:
    """Structural sibling of the three above, so a failure names the cause.

    The behavioural tests prove the boundary holds; this one says *where* it is
    supposed to live, so moving it onto every individual route — which would
    keep those three green while reintroducing exactly the omission risk the
    router placement exists to remove — is still caught.
    """
    dependencies = [
        dependency.dependency for dependency in (v1_router.dependencies or [])
    ]

    assert require_claims in dependencies, (
        "app/api/v1/__init__.py must attach require_claims at router level"
    )


# --- Matrix: the happy path --------------------------------------------------


def test_valid_token_reaches_the_route_with_its_subject(client: TestClient) -> None:
    response = client.get(PROBE_PATH, headers=bearer(make_token()))

    assert response.status_code == 200, response.text
    assert response.json() == {"sub": SUBJECT}


def test_a_second_request_reuses_the_cached_key_set(
    client: TestClient, fetcher: RecordingFetcher
) -> None:
    """The common path must not hit Keycloak once per request."""
    for _ in range(3):
        assert client.get(PROBE_PATH, headers=bearer(make_token())).status_code == 200

    assert fetcher.calls == 1


def test_jwks_is_fetched_from_the_in_network_url(
    client: TestClient, fetcher: RecordingFetcher
) -> None:
    """Keys come from KEYCLOAK_SERVER_URL, never from the issuer name."""
    client.get(PROBE_PATH, headers=bearer(make_token()))

    assert fetcher.urls == [
        f"{settings.keycloak_server_url}/realms/{settings.keycloak_realm}"
        "/protocol/openid-connect/certs"
    ]


# --- Matrix: no header, malformed header -------------------------------------


def test_no_authorization_header_is_the_typed_401(client: TestClient) -> None:
    response = client.get(PROBE_PATH)

    assert_unauthenticated(response)
    # Nothing was presented, so nothing was invalid.
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    [
        "potato",
        "Bearer not.a.jwt",
        "Bearer ",
        "Bearer",
        "Basic dXNlcjpwYXNz",
        "Bearer aaa.bbb.ccc",
        "Bearer " + "." * 10,
        "Bearer " + base64.urlsafe_b64encode(b"{}").decode(),
        f"Bearer {make_token()} {make_token()}",
    ],
)
def test_malformed_authorization_header_is_401_never_500(
    client: TestClient, header: str
) -> None:
    response = client.get(PROBE_PATH, headers={"Authorization": header})

    assert response.status_code != 500, response.text
    assert_unauthenticated(response)


# --- Matrix: expired ---------------------------------------------------------


def test_expired_token_is_401(client: TestClient) -> None:
    now = int(time.time())
    token = make_token(iat=now - 7200, exp=now - 3600)

    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(token)))


def test_a_token_expiring_within_the_leeway_still_works(client: TestClient) -> None:
    """Clock skew between two containers on one host must not reject."""
    now = int(time.time())
    token = make_token(exp=now - 1)

    assert client.get(PROBE_PATH, headers=bearer(token)).status_code == 200


def test_a_token_expiring_just_outside_the_leeway_is_401(client: TestClient) -> None:
    """Pins the leeway from the other side.

    With only "`now - 1` accepted" and "`now - 3600` rejected" on record, any
    leeway between one second and an hour passes both — including one large
    enough to keep an expired token alive for the whole evaluation window.
    """
    now = int(time.time())
    token = make_token(exp=now - (auth.CLOCK_SKEW_LEEWAY_SECONDS + 5))

    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(token)))


def test_the_leeway_is_small(client: TestClient) -> None:
    """Both containers run on one host; this is skew tolerance, not a grace period."""
    assert 0 < auth.CLOCK_SKEW_LEEWAY_SECONDS <= 60


def test_a_token_issued_in_the_future_is_401(client: TestClient) -> None:
    """Pins the behaviour, wherever it comes from.

    PyJWT 2.13 rejects this itself; earlier versions did not, and the pin can
    move. Asserting it here means a PyJWT upgrade that relaxed it would fail a
    test rather than silently widen what this API accepts.
    """
    now = int(time.time())
    token = make_token(iat=now + 86400, exp=now + 172800)

    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(token)))


@pytest.mark.parametrize("issued_at", ["1787818576", True, float("nan")])
def test_an_iat_that_is_not_a_number_is_401(client: TestClient, issued_at: object) -> None:
    """PyJWT coerces `iat` with `int(...)`, so a string or a bool validates.

    This is what the explicit check in `require_claims` catches and PyJWT does
    not — and it is why `iat` in REQUIRED_CLAIMS is load-bearing: the check
    below it reads a claim that must therefore be present and genuinely numeric.
    """
    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(make_token(iat=issued_at))))


def test_an_iat_within_the_leeway_is_still_accepted(client: TestClient) -> None:
    """The check above must be skew tolerance, not a rejection of every token."""
    token = make_token(iat=int(time.time()) + 1)

    assert client.get(PROBE_PATH, headers=bearer(token)).status_code == 200


def test_a_token_without_exp_is_401(client: TestClient) -> None:
    """Absent `exp` must fail closed, not validate forever."""
    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(make_token(exp=_ABSENT))))


def test_a_token_without_iat_is_401(client: TestClient) -> None:
    """`iat` is in REQUIRED_CLAIMS and nothing else enforced it.

    Removing it from that tuple left the whole suite green, which made the
    entry decorative. It is load-bearing: the future-`iat` check above reads a
    claim that must therefore be present.
    """
    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(make_token(iat=_ABSENT))))


@pytest.mark.parametrize("claim", sorted(auth.REQUIRED_CLAIMS))
def test_every_required_claim_is_actually_required(
    client: TestClient, claim: str
) -> None:
    """One test per entry, so no entry can become decorative again."""
    assert_unauthenticated(
        client.get(PROBE_PATH, headers=bearer(make_token(**{claim: _ABSENT})))
    )


def test_a_token_not_yet_valid_is_401(client: TestClient) -> None:
    now = int(time.time())
    token = make_token(nbf=now + 3600, iat=now + 3600, exp=now + 7200)

    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(token)))


# --- Matrix: foreign realm ---------------------------------------------------


def test_a_correctly_signed_token_from_another_issuer_is_401(client: TestClient) -> None:
    """Same key, same audience — only `iss` differs. Must still be rejected."""
    token = make_token(iss="https://evil.example.test/realms/linkedin")

    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(token)))


def test_an_issuer_that_merely_starts_with_ours_is_401(client: TestClient) -> None:
    """`iss` is an exact match, never a prefix match."""
    token = make_token(iss=f"{auth.EXPECTED_ISSUER}-evil")

    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(token)))


def test_a_token_signed_by_a_foreign_key_is_401(client: TestClient) -> None:
    """Unknown `kid`: the key is real, it is simply not one this realm publishes."""
    token = make_token(key=_FOREIGN_KEY, kid=FOREIGN_KID)

    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(token)))


def test_a_foreign_key_reusing_our_kid_is_401(client: TestClient) -> None:
    """The `kid` selects a key; it never authenticates one."""
    token = make_token(key=_FOREIGN_KEY, kid=REALM_KID)

    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(token)))


def test_an_unknown_kid_is_not_fetched_and_trusted(
    client: TestClient, fetcher: RecordingFetcher
) -> None:
    """A `jku` header pointing anywhere must be ignored entirely."""
    token = jwt.encode(
        {"iss": auth.EXPECTED_ISSUER, "aud": auth.EXPECTED_AUDIENCE, "sub": SUBJECT,
         "typ": "Bearer", "iat": int(time.time()), "exp": int(time.time()) + 300},
        _FOREIGN_KEY,
        algorithm="RS256",
        headers={"kid": FOREIGN_KID, "jku": "https://evil.example.test/certs"},
    )

    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(token)))
    # One refetch is legitimate (a rotated realm key looks exactly like this).
    # Fetching the attacker's URL is not.
    assert all("evil.example.test" not in url for url in fetcher.urls)


def test_a_token_with_no_kid_is_401(client: TestClient) -> None:
    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(make_token(kid=None))))


# --- Matrix: wrong audience --------------------------------------------------


def test_a_token_minted_for_another_client_is_401(client: TestClient) -> None:
    token = make_token(aud="some-other-client", azp="some-other-client")

    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(token)))


def test_keycloaks_default_account_audience_is_401(client: TestClient) -> None:
    """The exact trap the realm's audience mapper exists to avoid.

    Without the mapper a bare client_credentials token carries `aud: account`.
    Rejecting it here is what makes the mapper load-bearing rather than
    decorative — and stops anyone "fixing" a failure by dropping the check.
    """
    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(make_token(aud="account"))))


def test_a_token_without_aud_is_401(client: TestClient) -> None:
    """Absent `aud` must fail the check, not skip it."""
    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(make_token(aud=_ABSENT))))


def test_a_multi_audience_token_including_ours_is_accepted(client: TestClient) -> None:
    """`aud` is legitimately a list; ours being in it is what matters."""
    token = make_token(aud=["account", auth.EXPECTED_AUDIENCE])

    assert client.get(PROBE_PATH, headers=bearer(token)).status_code == 200


# --- Forgery ----------------------------------------------------------------


def test_an_unsigned_token_is_401(client: TestClient) -> None:
    """`alg: none`, hand-built because PyJWT will not encode one."""
    now = int(time.time())
    token = ".".join(
        [
            _b64({"alg": "none", "typ": "JWT", "kid": REALM_KID}),
            _b64(
                {
                    "iss": auth.EXPECTED_ISSUER,
                    "aud": auth.EXPECTED_AUDIENCE,
                    "sub": SUBJECT,
                    "typ": "Bearer",
                    "iat": now,
                    "exp": now + 300,
                }
            ),
            "",
        ]
    )

    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(token)))


def test_an_hmac_forgery_using_the_published_public_key_is_401(
    client: TestClient,
) -> None:
    """The classic key confusion: the "secret" is the JWKS, which is public.

    Assembled by hand because PyJWT refuses to *encode* an HMAC from a PEM key
    — a good guard, and precisely the wrong thing to rely on here. The claim
    under test is that the *verifier* refuses it, so the forgery has to be
    built the way an attacker would build it.
    """
    import hashlib
    import hmac

    from cryptography.hazmat.primitives import serialization

    public_pem = _REALM_KEY.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    now = int(time.time())
    signing_input = ".".join(
        _b64(part)
        for part in (
            {"alg": "HS256", "typ": "JWT", "kid": REALM_KID},
            {
                "iss": auth.EXPECTED_ISSUER,
                "aud": auth.EXPECTED_AUDIENCE,
                "sub": SUBJECT,
                "typ": "Bearer",
                "iat": now,
                "exp": now + 300,
            },
        )
    )
    signature = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    token = f"{signing_input}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"

    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(token)))


def test_an_id_token_is_not_accepted_as_an_access_token(client: TestClient) -> None:
    """Same issuer, same signature, `aud` equal to the client id — only `typ` differs."""
    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(make_token(typ="ID"))))


def test_a_token_without_sub_is_401(client: TestClient) -> None:
    """Stories 5-8 bind stored sessions to `sub`; it may never be absent."""
    assert_unauthenticated(client.get(PROBE_PATH, headers=bearer(make_token(sub=_ABSENT))))


# --- Failing closed ----------------------------------------------------------


def test_an_unreachable_jwks_rejects_rather_than_500(
    client: TestClient, fetcher: RecordingFetcher
) -> None:
    """Keycloak down must never mean "let it through", and never mean a 500."""
    fetcher.error = OSError("connection refused")

    response = client.get(PROBE_PATH, headers=bearer(make_token()))

    assert response.status_code != 500, response.text
    assert_unauthenticated(response)


#: A cache that refreshes on every call: TTL and refresh floor both zero.
#:
#: Both are needed. With the default 600s TTL a second `signing_key()` for a
#: `kid` already held never refreshes at all, so a test that swaps the
#: fetcher's document between two calls proves nothing — the second document is
#: never fetched. That is exactly how the empty-key-set guard below stayed
#: untested while looking tested.
def _always_refreshing(fetcher: RecordingFetcher) -> auth.JwksCache:
    return auth.JwksCache(
        auth.JWKS_URL,
        fetcher=fetcher,
        ttl_seconds=0,
        min_refresh_interval_seconds=0,
    )


def test_keys_already_held_survive_a_failed_refresh() -> None:
    """A Keycloak blip must not invalidate tokens signed by a known key."""
    fetcher = RecordingFetcher()
    cache = _always_refreshing(fetcher)

    assert cache.signing_key(REALM_KID) is not None

    fetcher.error = OSError("connection refused")
    assert cache.signing_key(REALM_KID) is not None
    assert fetcher.calls == 2, "the refresh under test never happened"


def test_keys_already_held_survive_an_empty_key_set() -> None:
    """The `if not keys: return` guard, which nothing else observes.

    Distinct from the failed-fetch case above: here the fetch SUCCEEDS and
    returns a document this filter empties — a realm mid-rotation, or one whose
    only published key is an encryption key. Replacing the cache with `{}` would
    401 every caller until the next refresh window, for a document that was
    never an error.
    """
    fetcher = RecordingFetcher()
    cache = _always_refreshing(fetcher)

    assert cache.signing_key(REALM_KID) is not None

    fetcher.document = {"keys": []}
    assert cache.signing_key(REALM_KID) is not None

    fetcher.document = {"keys": [_public_jwk(_FOREIGN_KEY, "enc-only", use="enc", alg="RSA-OAEP")]}
    assert cache.signing_key(REALM_KID) is not None

    assert fetcher.calls == 3, "the refreshes under test never happened"


@pytest.mark.parametrize("process_start_clock", [0.0, 0.5, 3.25, 29.0])
def test_the_first_fetch_happens_however_the_monotonic_clock_starts(
    monkeypatch: pytest.MonkeyPatch, process_start_clock: float
) -> None:
    """`time.monotonic()`'s reference point is undefined by the language.

    On a platform where it counts from process or boot start, a cache
    initialised to `0.0` computes `now - 0.0` as a small number — smaller than
    both the TTL and the 30s refresh floor — and suppresses the very first
    fetch. Every legitimate token then 401s for the first half-minute of
    uptime, on some platforms and not others. Sentinels must mean "never".
    """
    monkeypatch.setattr(auth.time, "monotonic", lambda: process_start_clock)
    fetcher = RecordingFetcher()
    cache = auth.JwksCache(auth.JWKS_URL, fetcher=fetcher)

    assert cache.signing_key(REALM_KID) is not None
    assert fetcher.calls == 1


def test_the_fetch_does_not_hold_the_cache_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """One slow Keycloak must not serialise every authenticated request.

    Asserted by having the fetcher try to take the lock itself. Under the old
    fetch-inside-the-lock arrangement this deadlocks; the non-blocking acquire
    turns that into a clean failure rather than a hung suite.
    """
    cache = auth.JwksCache(auth.JWKS_URL, fetcher=lambda url: REALM_JWKS)
    acquired: list[bool] = []

    def fetch_while_probing_the_lock(url: str) -> dict:
        got = cache._lock.acquire(blocking=False)
        acquired.append(got)
        if got:
            cache._lock.release()
        return REALM_JWKS

    monkeypatch.setattr(cache, "_fetch", fetch_while_probing_the_lock)

    assert cache.signing_key(REALM_KID) is not None
    assert acquired == [True], "the cache lock was held across the network fetch"


# --- The JWKS cache itself ---------------------------------------------------


def test_an_encryption_key_is_never_offered_for_signature_verification() -> None:
    """Keycloak publishes an RSA-OAEP `use: enc` key in the same document."""
    keys = auth._signature_keys(REALM_JWKS)

    assert set(keys) == {REALM_KID}


def test_a_jwks_entry_omitting_alg_is_kept() -> None:
    """`alg` is OPTIONAL in RFC 7517, and `use` is already handled permissively.

    Dropping such a key would 401 every token from any issuer that does not
    annotate its JWKS, with a symptom — "no published signature key" — that
    points nowhere near the cause. `jwt.decode`'s `algorithms=` list is what
    constrains it, and that constraint is unchanged.
    """
    entry = {name: value for name, value in REALM_JWK.items() if name != "alg"}

    assert set(auth._signature_keys({"keys": [entry]})) == {REALM_KID}


@pytest.mark.parametrize(
    "entry",
    [
        {"kty": "RSA", "kid": "bad", "use": "sig", "alg": "RS256", "n": "!!!not-base64!!!", "e": "AQAB"},
        {"kty": "RSA", "kid": "bad", "use": "sig", "alg": "RS256", "n": 12345, "e": "AQAB"},
        {"kty": "RSA", "kid": "bad", "use": "sig", "alg": "RS256"},
        {"kty": "nonsense", "kid": "bad", "use": "sig", "alg": "RS256"},
        {"kid": "bad", "use": "sig", "alg": "RS256"},
    ],
)
def test_a_malformed_jwks_entry_is_skipped_not_raised(entry: dict) -> None:
    """`PyJWK.from_dict` raises well outside `PyJWKError` on malformed material.

    InvalidKeyError, binascii.Error and TypeError all reach this loop from a
    JWKS document, and every one of them would otherwise be a 500 on a request
    the matrix says must be a 401. The good keys in the same document must
    still load.
    """
    keys = auth._signature_keys({"keys": [entry, REALM_JWK]})

    assert set(keys) == {REALM_KID}


@pytest.mark.parametrize("document", [[], "not-a-document", 42, None, {"keys": "nope"}])
def test_a_jwks_that_is_not_an_object_rejects_rather_than_500(
    client: TestClient, fetcher: RecordingFetcher, document: object
) -> None:
    """`document.get(...)` on a parsed list is an AttributeError, i.e. a 500."""
    fetcher.document = document

    response = client.get(PROBE_PATH, headers=bearer(make_token()))

    assert response.status_code != 500, response.text
    assert_unauthenticated(response)


@pytest.mark.parametrize("broken", ["exp", "iat", "nbf"])
@pytest.mark.parametrize("value", [[], {}, "not-a-number", None])
def test_a_non_numeric_time_claim_rejects_rather_than_500(
    client: TestClient, broken: str, value: object
) -> None:
    """PyJWT does arithmetic on these, so a list or dict raises TypeError.

    TypeError is not a `PyJWTError`, so it escaped the handler and became a 500
    on a request that must be a 401 — reachable by anyone who can post a header.
    """
    response = client.get(PROBE_PATH, headers=bearer(make_token(**{broken: value})))

    assert response.status_code != 500, response.text
    assert_unauthenticated(response)


def test_an_oversized_jwks_body_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unbounded `read()` lets a hostile or broken response exhaust memory."""
    assert auth.JWKS_MAX_BYTES <= 8 * 1024 * 1024

    class _Response:
        def read(self, size: int | None = None) -> bytes:
            return b"x" * (size or auth.JWKS_MAX_BYTES + 1)

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(auth.urllib.request, "urlopen", lambda *a, **k: _Response())

    with pytest.raises(ValueError, match="exceeded"):
        auth._fetch_jwks_over_http(auth.JWKS_URL)


def test_a_hostile_kid_cannot_forge_a_log_record() -> None:
    """`kid` is attacker-controlled and reaches a log line."""
    forged = auth._loggable("abc\n2026-08-27 ERROR app.auth: authorised\n" + "x" * 500)

    assert "\n" not in forged
    assert len(forged) <= auth.LOGGABLE_MAX_CHARS + len("...(truncated)")


def test_an_unknown_kid_refetches_at_most_once_per_interval() -> None:
    """Bounded, or any random `kid` becomes an amplifier aimed at Keycloak."""
    fetcher = RecordingFetcher()
    cache = auth.JwksCache(
        auth.JWKS_URL, fetcher=fetcher, min_refresh_interval_seconds=3600
    )

    for _ in range(10):
        with pytest.raises(auth.SigningKeyUnavailable):
            cache.signing_key("never-published")

    assert fetcher.calls == 1


def test_a_rotated_realm_key_is_picked_up_without_a_restart() -> None:
    """The reason an unknown `kid` refetches at all."""
    fetcher = RecordingFetcher(document={"keys": [REALM_JWK]})
    cache = auth.JwksCache(auth.JWKS_URL, fetcher=fetcher, min_refresh_interval_seconds=0)

    assert cache.signing_key(REALM_KID) is not None

    rotated = _public_jwk(_FOREIGN_KEY, "rotated-key")
    fetcher.document = {"keys": [REALM_JWK, rotated]}

    assert cache.signing_key("rotated-key") is not None


# --- /health is untouched ----------------------------------------------------


def test_health_still_answers_without_a_token(client: TestClient) -> None:
    """The container healthcheck has no token, and never will."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_ignores_a_broken_token(client: TestClient) -> None:
    response = client.get("/health", headers={"Authorization": "Bearer garbage"})

    assert response.status_code == 200


# --- The OpenAPI document ----------------------------------------------------


def test_openapi_advertises_bearer_on_v1_and_not_on_health(client: TestClient) -> None:
    document = client.get("/openapi.json").json()

    scheme = document["components"]["securitySchemes"]["KeycloakBearer"]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"

    assert document["paths"][PROBE_PATH]["get"]["security"] == [{"KeycloakBearer": []}]
    assert "security" not in document["paths"]["/health"]["get"]


def test_openapi_documents_the_typed_401_body(client: TestClient) -> None:
    """Story 9 ships this document as the API documentation."""
    document = client.get("/openapi.json").json()
    responses = document["paths"][PROBE_PATH]["get"]["responses"]

    schema_ref = responses["401"]["content"]["application/json"]["schema"]["$ref"]
    assert schema_ref.endswith("/ErrorEnvelope")

    envelope = document["components"]["schemas"]["ErrorEnvelope"]
    detail = document["components"]["schemas"]["ErrorDetail"]
    assert set(envelope["properties"]) == {"error"}
    assert set(detail["properties"]) == {"code", "message", "retryable"}


# --- Every failure path wears the envelope -----------------------------------
#
# `response-schema.md` fixes one body shape for every error at every status.
# FastAPI's defaults do not honour it, and three of these are reachable today
# without any route existing at all.


def _assert_envelope(response: Any, status: int, retryable: bool) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}, body
    assert set(body["error"]) == {"code", "message", "retryable"}, body
    assert body["error"]["retryable"] is retryable
    assert isinstance(body["error"]["code"], str) and body["error"]["code"]
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_an_unknown_path_is_the_envelope_not_fastapis_detail(
    client: TestClient,
) -> None:
    _assert_envelope(client.get("/no/such/path"), 404, retryable=False)


def test_a_wrong_method_is_the_envelope(client: TestClient) -> None:
    response = client.post(PROBE_PATH, headers=bearer(make_token()))

    _assert_envelope(response, 405, retryable=False)
    # Starlette's own `Allow` header must survive being re-rendered.
    assert "allow" in {name.lower() for name in response.headers}


def test_a_validation_failure_is_the_envelope(fetcher: RecordingFetcher) -> None:
    """Stories 5-8 hit this the moment they declare a query parameter."""
    probe = APIRouter()

    @probe.get("/__validated__")
    async def _validated(count: int) -> dict[str, int]:
        return {"count": count}

    saved = list(v1_router.routes)
    v1_router.include_router(probe)
    try:
        client = TestClient(create_app(), raise_server_exceptions=False)
        response = client.get(
            "/api/v1/__validated__", params={"count": "not-an-int"},
            headers=bearer(make_token()),
        )
    finally:
        v1_router.routes[:] = saved

    _assert_envelope(response, 422, retryable=False)


def test_a_validation_failure_does_not_echo_the_submitted_value(
    fetcher: RecordingFetcher,
) -> None:
    """Story 5's `PUT /api/v1/session` body is a live LinkedIn session cookie.

    FastAPI's default 422 reports the offending input back to the caller. Doing
    that for that route would put an `li_at` in a response body and in every
    log that captures one.
    """
    probe = APIRouter()

    @probe.put("/__secretish__")
    async def _secretish(payload: dict[str, int]) -> dict[str, bool]:
        return {"ok": True}

    saved = list(v1_router.routes)
    v1_router.include_router(probe)
    try:
        client = TestClient(create_app(), raise_server_exceptions=False)
        response = client.put(
            "/api/v1/__secretish__",
            json={"li_at": "AQEDA-super-secret-cookie-value"},
            headers=bearer(make_token()),
        )
    finally:
        v1_router.routes[:] = saved

    assert response.status_code == 422, response.text
    assert "super-secret-cookie-value" not in response.text


def test_an_unhandled_exception_is_a_typed_500_not_a_naked_one(
    fetcher: RecordingFetcher,
) -> None:
    """CAP-6: no unhandled exception reaches the client."""
    probe = APIRouter()

    @probe.get("/__boom__")
    async def _boom() -> dict[str, str]:
        raise RuntimeError("a bug nobody predicted")

    saved = list(v1_router.routes)
    v1_router.include_router(probe)
    try:
        client = TestClient(create_app(), raise_server_exceptions=False)
        response = client.get("/api/v1/__boom__", headers=bearer(make_token()))
    finally:
        v1_router.routes[:] = saved

    _assert_envelope(response, 500, retryable=True)
    assert "a bug nobody predicted" not in response.text
    assert "Traceback" not in response.text


def test_the_fallback_codes_are_not_mistaken_for_taxonomy_rows() -> None:
    """`response-schema.md`'s codes are the taxonomy; these are not among them.

    Keeping the two sets disjoint is what stops the fallback from quietly
    becoming the taxonomy — from a 422 shaped like `INVALID_URL` being counted
    as the real `INVALID_URL`, which story 8 must actually route.

    Story 4 filled `ERROR_SPECS` out to the full schema table, so the row count
    is no longer pinned here; `tests/test_linkedin_client.py` asserts the table
    against a hand-transcribed copy of the spec instead. What is pinned here is
    that the fallback set stayed *outside* it.
    """
    from app.errors import ERROR_SPECS, FALLBACK_CODE, FALLBACK_CODES

    assert "UNAUTHENTICATED" in ERROR_SPECS
    assert not set(FALLBACK_CODES.values()) & set(ERROR_SPECS)
    assert FALLBACK_CODE not in ERROR_SPECS


def test_the_error_handler_refuses_a_type_it_cannot_render() -> None:
    """`assert isinstance(...)` is stripped by `python -O`; a raise is not."""
    import asyncio

    from app.errors import api_error_handler

    with pytest.raises(TypeError):
        asyncio.run(api_error_handler(None, ValueError("not an ApiError")))


# --- Configuration wiring ----------------------------------------------------


def test_the_issuer_comes_from_the_external_url_not_the_in_network_one() -> None:
    """The story-1 deferred finding, asserted.

    Deriving `iss` from KEYCLOAK_SERVER_URL would reject every token minted
    through nginx while every unit test that shared one variable passed.
    """
    assert auth.EXPECTED_ISSUER == (
        f"{settings.keycloak_issuer_url}/realms/{settings.keycloak_realm}"
    )
    assert not auth.EXPECTED_ISSUER.startswith(settings.keycloak_server_url)


#: Every field declared `RequiredBaseUrl`. Both must normalise, not just the
#: one that happened to get a test: reverting either to a plain
#: `RequiredSetting` left the whole suite green.
BASE_URL_FIELDS = ("keycloak_server_url", "keycloak_issuer_url")


def _settings_with(**overrides: str) -> Any:
    from app.config import Settings

    from tests.conftest import REQUIRED_ENV

    values = {name.lower(): value for name, value in REQUIRED_ENV.items()}
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize("field", BASE_URL_FIELDS)
@pytest.mark.parametrize("suffix", ["/", "//", "///", "/  ", "  /"])
def test_a_trailing_slash_in_a_base_url_cannot_break_validation(
    field: str, suffix: str
) -> None:
    """`iss` is an exact string match, so one stray slash 401s every token.

    And it does so while `/health`, the realm and the token endpoint all stay
    green — the symptom is nowhere near the typo.
    """
    settings = _settings_with(**{field: "https://base.example.test" + suffix})

    assert getattr(settings, field) == "https://base.example.test"


@pytest.mark.parametrize("field", BASE_URL_FIELDS)
@pytest.mark.parametrize("blank", ["/", "//", " / ", "///"])
def test_a_base_url_of_only_slashes_is_rejected(field: str, blank: str) -> None:
    """Normalising `/` yields `""`, which `min_length=1` has already waved past.

    Without the explicit raise the service boots with an empty base URL and
    builds `"/realms/linkedin"` as its issuer — a value nothing can ever match.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as caught:
        _settings_with(**{field: blank})

    assert field in str(caught.value)


@pytest.mark.parametrize("field", BASE_URL_FIELDS)
def test_a_base_url_without_a_trailing_slash_is_untouched(field: str) -> None:
    """The normaliser must not eat anything else."""
    assert getattr(
        _settings_with(**{field: "https://base.example.test/auth"}),
        field,
    ) == "https://base.example.test/auth"


def test_only_asymmetric_algorithms_are_accepted() -> None:
    assert "none" not in auth.ALLOWED_ALGORITHMS
    assert not any(algorithm.startswith("HS") for algorithm in auth.ALLOWED_ALGORITHMS)


# --- The committed realm export ----------------------------------------------


def _env_example_values() -> dict[str, str]:
    path = REPO_ROOT / ".env.example"
    assert path.is_file(), f"{path} is missing — the env contract is unverified"

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.strip())
        if match:
            values[match.group(1)] = match.group(2)
    return values


def _realm_export() -> dict[str, Any]:
    assert REALM_EXPORT.is_file(), (
        f"{REALM_EXPORT} is missing — `docker compose up` would produce an empty realm"
    )
    return json.loads(REALM_EXPORT.read_text(encoding="utf-8"))


def _api_client(export: dict[str, Any]) -> dict[str, Any]:
    matches = [c for c in export["clients"] if c["clientId"] == "${KEYCLOAK_CLIENT_ID}"]
    assert matches, "realm export has no client templated from KEYCLOAK_CLIENT_ID"
    return matches[0]


def _compose_keycloak_environment() -> set[str]:
    """Variable names the compose `keycloak` service puts in its environment."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    keycloak = compose.split("\n  keycloak:\n", 1)[1].split("\n  api:\n", 1)[0]
    return set(re.findall(r"^\s+([A-Z_][A-Z0-9_]*):", keycloak, flags=re.MULTILINE))


def test_the_realm_export_is_templated_from_the_env_contract() -> None:
    """The DATABASE_URL lesson from story 1, applied to the realm.

    Hardcoding `realm` and `clientId` here while the API reads KEYCLOAK_REALM
    and KEYCLOAK_CLIENT_ID from `.env` means a `.env` edit alone yields a stack
    that boots entirely healthy and 401s every token — because the realm the
    API asks for is not the realm that exists. `.env` is the single source of
    truth; the export is templated from it and Keycloak substitutes at import.
    """
    export = _realm_export()

    assert export["realm"] == "${KEYCLOAK_REALM}"
    assert _api_client(export)["clientId"] == "${KEYCLOAK_CLIENT_ID}"


def test_every_placeholder_in_the_export_is_supplied_by_compose() -> None:
    """A placeholder nothing substitutes becomes a realm literally named `${...}`."""
    raw = REALM_EXPORT.read_text(encoding="utf-8")
    referenced = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}", raw))
    documented = _env_example_values()
    supplied = _compose_keycloak_environment()

    assert referenced, "the export templates nothing — is it hardcoded again?"
    for name in sorted(referenced):
        assert name in supplied, f"{name} is templated in the export but not passed to keycloak"
        assert name in documented, f"{name} is templated in the export but absent from .env.example"


#: Realm-export keys whose *string* value is a live secret in a real export.
#:
#: `publicKey` and `certificate` are deliberately NOT here: both are public by
#: definition, neither can ever hold a `${ENV_VAR}` placeholder, and requiring
#: one would fail on the first valid re-export with a message pointing at the
#: wrong problem — which teaches whoever hits it to loosen the check rather
#: than fix it. `credentials` is handled separately below because in a real
#: export it is a LIST of `{type, value}` objects, so a string test against the
#: key itself never fires.
SECRET_BEARING_KEYS = frozenset(
    {
        "secret",
        "password",
        "clientSecret",
        "adminPassword",
        "keyPassword",
        "storePassword",
        "privateKey",
    }
)

#: What a secret-bearing value is allowed to be: a placeholder Keycloak
#: substitutes at import, and nothing else.
PLACEHOLDER = re.compile(r"^\$\{[A-Z_][A-Z0-9_]*\}$")


def _secret_bearing_entries(node: Any, path: str = "") -> list[tuple[str, Any]]:
    """Every (path, value) in the export that must be a placeholder."""
    found: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if key in SECRET_BEARING_KEYS:
                found.append((here, value))
            elif key == "credentials":
                # A list of {type, value}; only `value` is the secret.
                for index, credential in enumerate(value if isinstance(value, list) else []):
                    if isinstance(credential, dict) and "value" in credential:
                        found.append((f"{here}[{index}].value", credential["value"]))
            found.extend(_secret_bearing_entries(value, here))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_secret_bearing_entries(value, f"{path}[{index}]"))
    return found


def test_public_key_material_is_not_required_to_be_a_placeholder() -> None:
    """Pins the narrowing itself, which no export currently exercises.

    `publicKey` and `certificate` are public by definition and can never hold a
    `${ENV_VAR}` placeholder. Including them would make the scan above fail on
    the first valid re-export, with a message pointing at the wrong problem —
    which teaches whoever hits it to loosen the check rather than fix it. The
    export has neither key today, so only this assertion keeps them out.
    """
    assert not {"publicKey", "certificate"} & SECRET_BEARING_KEYS


def test_a_literal_secret_anywhere_in_the_export_is_caught() -> None:
    """Positive control: the scan must be capable of failing.

    Including the list form, because a real Keycloak export writes a client
    secret as `credentials: [{"type": "secret", "value": "..."}]` — a shape a
    string test against the `credentials` key itself never sees.
    """
    planted = {
        "clients": [
            {"clientId": "x", "secret": "a-real-literal-secret"},
            {"clientId": "y", "credentials": [{"type": "secret", "value": "another"}]},
        ]
    }

    values = [value for _, value in _secret_bearing_entries(planted)]

    assert "a-real-literal-secret" in values
    assert "another" in values
    assert not any(PLACEHOLDER.match(str(value)) for value in values)


def test_the_realm_export_carries_no_secret() -> None:
    """The one place a client secret could silently reach git.

    Every credential-bearing key in the whole document must hold an env
    placeholder, not a value — so this stays honest if a later story adds a
    second client, an SMTP block, or a keystore reference.
    """
    export = _realm_export()

    assert _api_client(export)["secret"] == "${KEYCLOAK_CLIENT_SECRET}", (
        "the realm export must carry the placeholder Keycloak substitutes at "
        "import, never a literal secret"
    )

    for where, value in _secret_bearing_entries(export):
        assert isinstance(value, str) and PLACEHOLDER.match(value), (
            f"{where} = {value!r} — secret-bearing keys must hold a ${{ENV_VAR}} "
            "placeholder, never a literal"
        )


def test_the_realm_client_is_service_account_only() -> None:
    """CAP-3: two curl commands, no browser redirect anywhere in the flow."""
    client = _api_client(_realm_export())

    assert client["serviceAccountsEnabled"] is True
    assert client["publicClient"] is False
    for browser_flow in (
        "standardFlowEnabled",
        "implicitFlowEnabled",
        "directAccessGrantsEnabled",
    ):
        assert client[browser_flow] is False, f"{browser_flow} would invite a redirect flow"
    assert client["redirectUris"] == []


def test_the_realm_client_carries_an_audience_mapper() -> None:
    """Without it every legitimate token carries `aud: account` and is rejected."""
    client = _api_client(_realm_export())

    mappers = [
        mapper
        for mapper in client.get("protocolMappers", [])
        if mapper["protocolMapper"] == "oidc-audience-mapper"
    ]
    assert len(mappers) == 1, "exactly one audience mapper, or `aud` becomes ambiguous"

    config = mappers[0]["config"]
    # Templated from the same variable as `clientId`, so the mapper cannot come
    # to name a client that no longer exists.
    assert config["included.client.audience"] == "${KEYCLOAK_CLIENT_ID}"
    assert config["access.token.claim"] == "true"


def test_the_realm_signs_with_an_algorithm_the_validator_accepts() -> None:
    """The two halves must agree, and nothing else makes them.

    Setting the realm or the client to HS256 produces a Keycloak that mints
    tokens this API categorically refuses — an entirely green stack in which
    every request 401s, with the cause two files away from the symptom.
    """
    export = _realm_export()
    client = _api_client(export)

    realm_algorithm = export["defaultSignatureAlgorithm"]
    client_algorithm = client["attributes"]["access.token.signed.response.alg"]

    assert realm_algorithm in auth.ALLOWED_ALGORITHMS, realm_algorithm
    assert client_algorithm in auth.ALLOWED_ALGORITHMS, client_algorithm


def test_compose_imports_the_realm_export() -> None:
    """A committed export nothing mounts is a file, not a realm."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "--import-realm" in compose
    # The DIRECTORY, not the file inside it. Bind-mounting a single file whose
    # source has been renamed makes Docker create an empty directory at the
    # source path, and Keycloak then boots healthy with no realm at all.
    assert "./deploy/keycloak:/opt/keycloak/data/import:ro" in compose
    assert "realm-linkedin.json:/opt/keycloak" not in compose
    assert REALM_EXPORT.parent.is_dir()
