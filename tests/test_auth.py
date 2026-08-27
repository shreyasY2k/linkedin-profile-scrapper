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
import logging
import re
import time
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Depends
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import auth
from app import errors as app_errors
from app.api import v1
from app.api.v1 import router as v1_router
from app.auth import require_claims
from app.config import settings
from app.errors import (
    CAUSE_MALFORMED_BODY,
    CAUSE_MEMBER_MISMATCH,
    IDP_UNAVAILABLE_DESCRIPTION,
    ApiError,
    unauthenticated,
)
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


#: `sub` values that must be refused, and WHICH layer is expected to refuse
#: each one. The second half is the point of the table.
#:
#: Without it, five of these eight pass whether or not the guard in
#: `require_claims` exists at all — pinned PyJWT 2.13 defaults `verify_sub` to
#: True and its `_validate_sub` rejects a non-string `sub` before the guard
#: runs. A parametrised "all eight are 401" test therefore looks like eight
#: assertions about our code and is really three, and deleting the three checks
#: that ARE ours would still leave it green for the other five.
#:
#: So each case names the log fragment the refusing layer emits. PyJWT's own
#: rejections surface through `_reject` as `InvalidSubjectError`; ours name the
#: specific rule.
UNUSABLE_SUBJECTS = [
    # Refused by PyJWT, before our guard is reached. Kept in the table so a
    # PyJWT downgrade that stops refusing them fails HERE rather than silently
    # widening what this API accepts.
    (1, "InvalidSubjectError"),
    (True, "InvalidSubjectError"),
    ([], "InvalidSubjectError"),
    ({}, "InvalidSubjectError"),
    # `null` never even reaches the subject validator: PyJWT's required-claims
    # check reads `payload.get(claim) is None`, so a present-but-null `sub` is
    # indistinguishable from an absent one and dies as a missing claim.
    (None, "MissingRequiredClaimError"),
    # Refused by us, and by nothing else. PyJWT accepts all three.
    ("", "sub is blank"),
    ("   ", "sub is blank"),
    ("x" * 256, "over the cap"),
    ("has\x00a-nul", "control character"),
    ("has\na-newline", "control character"),
]


@pytest.mark.parametrize(("subject", "reason"), UNUSABLE_SUBJECTS)
def test_a_sub_that_is_not_a_usable_subject_is_401(
    client: TestClient, caplog: pytest.LogCaptureFixture, subject: object, reason: str
) -> None:
    """`require` checks that `sub` is PRESENT, never what it is.

    Story 5 makes `sub` the PRIMARY KEY of the session vault. A blank one pools
    every such caller into a single shared row, which is CAP-4's isolation
    inverted; one containing NUL is a 500 on the database write, because
    Postgres `text` cannot store it; an unbounded one is an unbounded key.
    None of that is something a route should have to remember to check.

    The `reason` assertion is what keeps this honest about which layer did the
    refusing — see `UNUSABLE_SUBJECTS`.
    """
    with caplog.at_level(logging.WARNING, logger="app.auth"):
        response = client.get(PROBE_PATH, headers=bearer(make_token(sub=subject)))

    assert_unauthenticated(response)
    assert reason in caplog.text, (
        f"expected {reason!r} to appear in the rejection log; got: {caplog.text}"
    )


def test_the_blank_and_control_character_rules_are_ours_alone() -> None:
    """The three cases above that PyJWT does NOT catch, asserted directly.

    Proves the claim in `UNUSABLE_SUBJECTS` rather than asserting it in a
    comment: if a future PyJWT starts refusing these too, this test says so and
    the guard can be reconsidered deliberately.
    """
    from jwt.exceptions import InvalidSubjectError

    for accepted in ("", "   ", "x" * 256, "has\x00a-nul", "has\na-newline"):
        # `_validate_sub` is what `verify_sub` runs. It must NOT object to any
        # of these — which is precisely why `require_claims` checks them itself.
        jwt.PyJWT()._validate_sub({"sub": accepted})

    # And it MUST object to the one it owns, or the pragma'd branch in
    # `require_claims` is wrong about who refuses first.
    with pytest.raises(InvalidSubjectError):
        jwt.PyJWT()._validate_sub({"sub": 1})


def test_an_ordinary_subject_is_still_accepted(client: TestClient) -> None:
    """The check above must be a type guard, not a rejection of every token."""
    response = client.get(PROBE_PATH, headers=bearer(make_token()))

    assert response.status_code == 200, response.text
    assert response.json() == {"sub": SUBJECT}


# --- Failing closed ----------------------------------------------------------


def test_an_unreachable_jwks_refuses_the_request_without_blaming_the_token(
    client: TestClient, fetcher: RecordingFetcher
) -> None:
    """Keycloak down must never mean "let it through", and never mean a 500.

    Nor a 401. The token here is perfectly valid — this is the story-8 matrix
    row — and it was never checked: the keys to check it against could not be
    read. A 401 tells its holder to stop asking, which is a claim about their
    credential that this service is in no position to make. It is a retryable
    502 about the identity provider, and the request is still refused.
    """
    fetcher.error = OSError("connection refused")

    response = client.get(PROBE_PATH, headers=bearer(make_token()))

    _assert_envelope(response, 502, retryable=True)
    assert response.json()["error"]["code"] == "UPSTREAM_ERROR"
    # Never the reason, the URL, or anything else about the realm's topology.
    assert "connection refused" not in response.text
    assert settings.keycloak_server_url not in response.text


def test_an_unreachable_jwks_still_refuses_while_the_refresh_floor_holds(
    client: TestClient, fetcher: RecordingFetcher
) -> None:
    """The second request, inside the 30s floor, must answer the same way.

    The floor suppresses the refetch, so this request reaches the "no fetch
    happened" branch with an empty cache rather than the "the fetch failed"
    one. Both mean Keycloak, and reading only the first would leave a 401 —
    the exact bug — for every request in the half-minute after the first
    failure, which is most of them.
    """
    fetcher.error = OSError("connection refused")

    first = client.get(PROBE_PATH, headers=bearer(make_token()))
    second = client.get(PROBE_PATH, headers=bearer(make_token()))

    assert fetcher.calls == 1, "the refresh floor did not suppress the second fetch"
    _assert_envelope(first, 502, retryable=True)
    _assert_envelope(second, 502, retryable=True)


def test_an_unknown_kid_stays_a_401_when_a_refresh_fails_underneath_it() -> None:
    """Keys held + a refetch that comes back unusable + an unknown `kid` = 401.

    The discriminator is "do we hold a key set we could have checked this kid
    against", and an earlier revision also required the refresh THIS CALL made
    to have succeeded. With keys held and a failing refetch, an unknown kid then
    answered 502 — and 401 for the identical token thirty seconds later, once
    the refresh floor stopped it refetching. `response-schema.md`'s matrix says
    an unknown signing key is a 401 and must not regress into a 502; a verdict
    that depends on which side of a refresh window a request lands on is not a
    verdict at all.
    """
    fetcher = RecordingFetcher()
    cache = _always_refreshing(fetcher)

    assert cache.signing_key(REALM_KID) is not None
    fetcher.error = OSError("connection refused")

    with pytest.raises(auth.SigningKeyUnavailable):
        cache.signing_key("never-published")
    assert fetcher.calls == 2, "the failing refresh under test never happened"


def test_the_answer_for_an_unknown_kid_does_not_change_across_the_refresh_floor() -> None:
    """The timing dependency, stated as the property it violates.

    Same cache, same token, one call that refetches and one the floor
    suppresses. Both must raise the same class, or the same request gets two
    different HTTP statuses depending on when it arrives.
    """
    fetcher = RecordingFetcher()
    cache = auth.JwksCache(auth.JWKS_URL, fetcher=fetcher, min_refresh_interval_seconds=3600)

    assert cache.signing_key(REALM_KID) is not None
    fetcher.error = OSError("connection refused")

    with pytest.raises(Exception) as refreshing:
        cache.signing_key("never-published")
    with pytest.raises(Exception) as suppressed:
        cache.signing_key("never-published")

    assert fetcher.calls == 1, "the floor must have suppressed the second fetch"
    assert type(refreshing.value) is auth.SigningKeyUnavailable
    assert type(refreshing.value) is type(suppressed.value)


def test_a_key_set_that_arrived_still_refuses_an_unknown_kid_as_a_401(
    client: TestClient, fetcher: RecordingFetcher
) -> None:
    """The other half of the split, asserted beside it so neither drifts.

    The realm ANSWERED and does not publish this key. That is a statement about
    the token, and it must not regress into a 502 — a foreign realm's token
    would then be told to try again.
    """
    token = make_token(key=_FOREIGN_KEY, kid=FOREIGN_KID)

    response = client.get(PROBE_PATH, headers=bearer(token))

    assert fetcher.calls >= 1, "the realm must actually have been consulted"
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
    """`document.get(...)` on a parsed list is an AttributeError, i.e. a 500.

    Answered the same way as an unreachable Keycloak, and for the same reason:
    a key set that did not arrive in a usable form is a fact about the realm,
    and no token was checked against it. A misconfigured or mid-deploy realm
    telling every valid caller their credential is bad is the failure this
    split exists to prevent, and it does not become less wrong because the
    connection succeeded.
    """
    fetcher.document = document

    response = client.get(PROBE_PATH, headers=bearer(make_token()))

    _assert_envelope(response, 502, retryable=True)
    assert response.json()["error"]["code"] == "UPSTREAM_ERROR"


def test_a_realm_publishing_no_usable_signature_key_is_not_the_callers_fault(
    client: TestClient, fetcher: RecordingFetcher
) -> None:
    """A document that this filter empties — a realm mid-rotation.

    It parses, it is well formed, and it names no key any token could be
    verified against. Nothing has been decided about the token.
    """
    fetcher.document = {
        "keys": [_public_jwk(_FOREIGN_KEY, "enc-only", use="enc", alg="RSA-OAEP")]
    }

    response = client.get(PROBE_PATH, headers=bearer(make_token()))

    _assert_envelope(response, 502, retryable=True)


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


def test_openapi_advertises_client_credentials_on_v1_and_nothing_on_health(
    client: TestClient,
) -> None:
    """Story 5 replaced story 3's `http`/`bearer` scheme, deliberately.

    A bare bearer scheme renders in `/docs` as a box to paste an already-minted
    token into. Declaring the flow gives the Authorize button a token endpoint
    to mint from, which is what an evaluator will reach for first. The half of
    story 3's assertion that must NOT weaken is the second one: `/health`
    carries no security and never will.
    """
    document = client.get("/openapi.json").json()

    scheme = document["components"]["securitySchemes"][auth.SECURITY_SCHEME_NAME]
    assert scheme["type"] == "oauth2"
    assert set(scheme["flows"]) == {"clientCredentials"}

    assert document["paths"][PROBE_PATH]["get"]["security"] == [
        {auth.SECURITY_SCHEME_NAME: []}
    ]
    assert "security" not in document["paths"]["/health"]["get"]


def test_the_advertised_token_url_is_the_external_issuer(client: TestClient) -> None:
    """Swagger runs in a browser; it cannot resolve the compose service name.

    `KEYCLOAK_SERVER_URL` is where THIS PROCESS fetches JWKS. Advertising it as
    the token URL would give `/docs` an Authorize button that can never
    connect — and the two settings exist separately precisely so that cannot
    happen by accident. Building the token URL from `EXPECTED_ISSUER` also means
    a token Swagger can obtain is necessarily a token this API accepts, because
    `iss` is checked against the same string.
    """
    document = client.get("/openapi.json").json()
    scheme = document["components"]["securitySchemes"][auth.SECURITY_SCHEME_NAME]

    token_url = scheme["flows"]["clientCredentials"]["tokenUrl"]

    assert token_url == (
        f"{settings.keycloak_issuer_url}"
        f"/realms/{settings.keycloak_realm}/protocol/openid-connect/token"
    )
    assert token_url == auth.TOKEN_URL
    assert not token_url.startswith(settings.keycloak_server_url)


def test_the_realm_client_allows_the_docs_page_to_mint() -> None:
    """The Authorize button posts from the browser, so Keycloak needs an origin.

    Without a web origin on the client, Keycloak's token endpoint omits the
    CORS headers and the browser discards the response — the button appears,
    the mint fails, and nothing in either log says why. `*` rather than a list
    of hosts because the same export serves `http://127.0.0.1:8000/docs` and the
    deployed HTTPS name, and this client is confidential: an origin without the
    client secret still gets nothing.
    """
    origins = _api_client(_realm_export())["webOrigins"]

    assert origins, (
        "the client needs a web origin or /docs' Authorize button cannot mint"
    )


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


def test_the_fallback_covers_every_status_reachable_without_a_raise_site() -> None:
    """Story 8 dropped the 400 row and had to put it back. That is the test.

    The reasoning for dropping it — "every 400 this API answers is now
    `INVALID_URL`" — was true of every 400 *this codebase raises* and false of
    the ones it does not: FastAPI's own body-read guard raises
    `HTTPException(400, ...)` before any of our code runs. "Nothing raises this
    status" is precisely the argument the fallback set exists to distrust, and
    a row is not superseded by a taxonomy code merely because one path now has
    one.
    """
    from app.errors import FALLBACK_CODES, FALLBACK_MESSAGES

    assert set(FALLBACK_CODES) == {400, 404, 405, 422, 503}
    assert set(FALLBACK_MESSAGES) == set(FALLBACK_CODES)


def test_an_unparseable_body_is_a_typed_400_and_not_an_internal_error(
    fetcher: RecordingFetcher,
) -> None:
    """The regression itself, driven through a real route.

    FastAPI reads the body before any dependency runs, so bytes that are not
    UTF-8 become `HTTPException(400, "There was an error parsing the body")`
    from `fastapi/routing.py`. Without a 400 row that rendered
    `code: "INTERNAL_ERROR"` — telling a caller their own malformed request was
    a bug in this service, at a status that says the opposite.

    Asserted on the wire rather than against the constant, because the constant
    is not what was broken: the routing of a status to it was.
    """
    client = TestClient(create_app(), raise_server_exceptions=False)

    response = client.put(
        "/api/v1/session",
        content=b"\xff\xfe\xff",
        headers={**bearer(make_token()), "Content-Type": "application/json"},
    )

    _assert_envelope(response, 400, retryable=False)
    assert response.json()["error"]["code"] == "BAD_REQUEST"
    # And never FastAPI's own text, which names a framework internal.
    assert "parsing the body" not in response.text


def test_a_curated_message_wins_over_starlettes_own_detail(client: TestClient) -> None:
    """`FALLBACK_MESSAGES` was dead code that looked alive.

    Starlette always populates `detail` and the handler preferred it, so every
    curated message was unreachable — including the 400 one, whose whole point
    is not to hand a caller "There was an error parsing the body".
    """
    from app.errors import FALLBACK_MESSAGES

    assert client.get("/no/such/path").json()["error"]["message"] == (
        FALLBACK_MESSAGES[404]
    )
    assert client.post(PROBE_PATH, headers=bearer(make_token())).json()["error"][
        "message"
    ] == FALLBACK_MESSAGES[405]


# --- Every taxonomy code, on the wire -----------------------------------------
#
# `tests/test_linkedin_client.py` pins ERROR_SPECS against a hand-transcribed
# copy of `response-schema.md`. That says the TABLE is right and nothing about
# what a caller receives — four codes had their status asserted over HTTP and
# four did not, and `retryable` was asserted on the wire for none of them.
#
# This closes the composition: hand-transcribed spec == ERROR_SPECS (there) and
# ERROR_SPECS == what comes back over HTTP (here), so the wire agrees with the
# published contract by two independent steps rather than by assumption. It is
# derived from ERROR_SPECS deliberately — a second hand transcription here would
# be a second thing to keep in step, and the first one is already the pin.


def _raise_over_http(error: ApiError) -> Any:
    """Provoke `error` from a real route and return the client's response."""
    probe = APIRouter()

    @probe.get("/__taxonomy__")
    async def _raiser() -> None:
        raise error

    saved = list(v1_router.routes)
    v1_router.include_router(probe)
    try:
        client = TestClient(create_app(), raise_server_exceptions=False)
        return client.get("/api/v1/__taxonomy__", headers=bearer(make_token()))
    finally:
        v1_router.routes[:] = saved


@pytest.mark.parametrize("code", sorted(app_errors.ERROR_SPECS))
def test_every_taxonomy_code_wears_the_envelope_on_the_wire(
    fetcher: RecordingFetcher, code: str
) -> None:
    """All eight, and `retryable` among them. The story's third criterion."""
    spec = app_errors.ERROR_SPECS[code]

    response = _raise_over_http(ApiError(code))

    _assert_envelope(response, spec.status_code, retryable=spec.retryable)
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["message"] == spec.message
    # Every error is caller-specific and none of them may be cached by the
    # nginx / load balancer / Cloudflare chain in front of this service.
    assert response.headers["cache-control"] == "no-store"
    if spec.status_code == 401:
        # RFC 6750 requires the challenge on every 401 from a bearer resource,
        # so it belongs to the row rather than to the one helper that used to
        # remember it — `ApiError("UNAUTHENTICATED")` raised by a route that
        # never heard of `unauthenticated()` is still conformant.
        assert response.headers["www-authenticate"] == "Bearer"


def test_a_call_site_with_a_more_specific_challenge_still_wins(
    fetcher: RecordingFetcher,
) -> None:
    """The default must not overwrite `error="invalid_token"`.

    RFC 6750 puts that parameter on a *presented and rejected* credential, and
    `unauthenticated()` sets it for exactly that case. A default that clobbered
    it would make every 401 claim a token was presented.
    """
    response = _raise_over_http(
        unauthenticated(log_detail="test", www_authenticate='Bearer error="invalid_token"')
    )

    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'


def test_the_documented_502_addendum_reaches_the_generated_document(
    client: TestClient,
) -> None:
    """`error_responses(addenda=...)` exists because a caller reads only this.

    FastAPI merges route-level `responses` OVER router-level, so the profile
    route's own 502 entry REPLACES the router's `IDP_UNAVAILABLE_RESPONSE` —
    and the IdP meaning of a 502 was documented nowhere a caller looks. It is
    composed into both routes now, and this is what says so.
    """
    paths = client.get("/openapi.json").json()["paths"]
    profile = paths["/api/v1/profile"]["get"]["responses"]["502"]["description"]
    session = paths["/api/v1/session"]["get"]["responses"]["502"]["description"]

    assert IDP_UNAVAILABLE_DESCRIPTION in profile
    assert IDP_UNAVAILABLE_DESCRIPTION in session
    # And the route-specific half is still there beside it.
    assert "retryable: false" in profile


def test_documenting_a_status_no_code_produces_is_refused() -> None:
    """The reason `addenda` is a parameter rather than a caller mutating a dict.

    `PROFILE_ERRORS[502]["description"] += ...` at module scope raises KeyError
    at IMPORT the moment `UPSTREAM_ERROR` is dropped from the call above it — a
    broken deploy for an edit to a docstring. Here the same mistake is a clear
    KeyError from the function that knows which statuses exist.
    """
    with pytest.raises(KeyError):
        app_errors.error_responses("INVALID_URL", addenda={502: "no 502 here"})


def test_a_narrowed_error_reports_its_own_retryability_not_its_codes(
    fetcher: RecordingFetcher,
) -> None:
    """The deliberate contract deviation, asserted where a client reads it.

    `response-schema.md` marks `UPSTREAM_ERROR` retryable. A member-mismatch
    returns that code with `retryable: false`, and the wire value is the
    authoritative one — which is exactly why a client is told to branch on the
    flag rather than on the code.
    """
    response = _raise_over_http(
        ApiError("UPSTREAM_ERROR", retryable=False, cause=CAUSE_MEMBER_MISMATCH)
    )

    _assert_envelope(response, 502, retryable=False)
    assert response.json()["error"]["code"] == "UPSTREAM_ERROR"


def test_the_operator_only_fields_never_reach_the_body(
    fetcher: RecordingFetcher,
) -> None:
    """`cause` joins `log_detail` on the operator side of the line.

    Both name internals — a resource path, a classification branch — and the
    envelope has exactly three keys. A fourth appearing here would be a contract
    break; a value leaking into `message` would be an information leak.
    """
    response = _raise_over_http(
        ApiError(
            "UPSTREAM_ERROR",
            cause=CAUSE_MALFORMED_BODY,
            log_detail="identity/dash/profiles: secret-ish internal detail",
        )
    )

    assert set(response.json()["error"]) == {"code", "message", "retryable"}
    assert CAUSE_MALFORMED_BODY not in response.text
    assert "secret-ish internal detail" not in response.text


def test_a_raise_site_cannot_make_a_permanent_failure_retryable() -> None:
    """The override narrows and only narrows.

    Widening is what hides a permanent failure behind a cached 200 — telling a
    caller whose session is dead to try again is the bug this codebase has
    already had to fix once. The story's boundaries put that change behind Ask
    First; this raise is what makes that mean something at runtime rather than
    in a document.
    """
    for code in ("SESSION_EXPIRED", "NO_SESSION", "PROFILE_NOT_FOUND", "INVALID_URL"):
        with pytest.raises(ValueError):
            ApiError(code, retryable=True)

    # Narrowing an already-retryable code is the permitted direction.
    assert ApiError("UPSTREAM_ERROR", retryable=False).retryable is False
    # And an untouched instance still answers with its code's default.
    assert ApiError("UPSTREAM_ERROR").retryable is True
    assert ApiError("SESSION_EXPIRED").retryable is False


#: The exact raise sites that may set `retryable` per instance, as
#: ``(file, enclosing function)``.
#:
#: The story's design note: the override is reachable only from named raise
#: sites, never a general escape hatch. Two guards need it — the client's and
#: the endpoint's, both refusing to publish a different member — plus the
#: builder that forwards it for the first of those.
#:
#: Pinned per SITE rather than per file. A grep for the literal per file cannot
#: see a third override added to a file already on the list, and
#: `_upstream_error` is reachable from eleven call sites inside the one file
#: that legitimately forwards it — so file membership would have waved through
#: exactly the drift this exists to catch.
RETRYABLE_OVERRIDE_SITES = {
    ("app/linkedin/client.py", "_core_profile"),
    # The builder that forwards the keyword to `ApiError` for the guard above.
    ("app/linkedin/client.py", "_upstream_error"),
    ("app/api/v1/profile.py", "get_profile"),
}

#: Callables whose `retryable=` keyword is a taxonomy override. `ErrorDetail`
#: and `envelope` also take one, and theirs is the rendering of a decision
#: already made rather than the making of one.
OVERRIDE_CALLEES = {"ApiError", "_upstream_error"}


def _override_sites() -> set[tuple[str, str]]:
    """Every `(file, function)` that passes `retryable=` to a raise-site builder."""
    import ast

    found: set[tuple[str, str]] = set()
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        relative = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
                callee = call.func
                name = (
                    callee.attr if isinstance(callee, ast.Attribute)
                    else callee.id if isinstance(callee, ast.Name)
                    else ""
                )
                if name not in OVERRIDE_CALLEES:
                    continue
                if any(keyword.arg == "retryable" for keyword in call.keywords):
                    found.add((relative, node.name))
    return found


def test_the_retryable_override_is_only_used_where_it_was_argued_for() -> None:
    sites = _override_sites()

    assert sites == RETRYABLE_OVERRIDE_SITES, (
        f"the set of raise sites narrowing `retryable` changed: "
        f"added={sorted(sites - RETRYABLE_OVERRIDE_SITES)}, "
        f"removed={sorted(RETRYABLE_OVERRIDE_SITES - sites)}. It exists for the "
        "two member-mismatch guards and is not a general way to disagree with "
        "response-schema.md — argue the case, then add the site here."
    )


def test_the_override_pin_would_notice_a_new_site() -> None:
    """The pin above passes vacuously if the walk finds nothing. It does not."""
    assert _override_sites(), "the AST walk matched no call site at all"
    assert ("app/api/v1/profile.py", "get_profile") in _override_sites()


def test_an_unregistered_cause_is_refused_at_the_raise_site() -> None:
    """`cause` is compared for MEMBERSHIP, so a typo means "not that case".

    `DECORATION_RETRY_CAUSES` is a whitelist and `_fetch_core` tests against it.
    A free-form string would let `"malformed_body"` — one character out — drop a
    case out of the retry silently, and the symptom (a missing
    `location.region`, occasionally) points nowhere near the cause. Validated
    exactly the way `code` is validated against `ERROR_SPECS`.
    """
    with pytest.raises(KeyError):
        ApiError("UPSTREAM_ERROR", cause="malformed_body")
    with pytest.raises(KeyError):
        ApiError("UPSTREAM_ERROR", cause="something-new")

    assert ApiError("UPSTREAM_ERROR", cause=CAUSE_MALFORMED_BODY).cause == (
        CAUSE_MALFORMED_BODY
    )
    assert ApiError("UPSTREAM_ERROR").cause is None


def test_every_cause_the_app_raises_is_registered() -> None:
    """The constants and the set they are validated against cannot drift apart."""
    for name, value in vars(app_errors).items():
        if name.startswith("CAUSE_"):
            assert value in app_errors.ERROR_CAUSES, name
    assert len(app_errors.ERROR_CAUSES) == sum(
        1 for name in vars(app_errors) if name.startswith("CAUSE_")
    )


# --- The authentication boundary covers paths that do not exist ---------------


UNMATCHED_PATH = "/api/v1/__no_such_route__"


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
def test_an_unmatched_v1_path_without_a_token_is_401_not_404(
    client: TestClient, method: str
) -> None:
    """The story's matrix row, and it is about enumeration rather than data.

    Routing runs before dependencies, so a path with no route never reached
    `require_claims` at all and answered 404 while a real path answered 401.
    Nothing leaks that way — but the difference between the two answers is a
    map of this API's surface, readable from the status code by somebody who
    has not authenticated. Observed on the deployed host.
    """
    assert_unauthenticated(client.request(method, UNMATCHED_PATH))


def test_a_real_route_and_an_absent_one_are_indistinguishable_without_a_token(
    client: TestClient,
) -> None:
    """The property, stated directly: same status, same body, same headers.

    Asserting the two responses are equal is what makes this a test of
    indistinguishability rather than of one status code.
    """
    absent = client.get(UNMATCHED_PATH)
    real = client.get(PROBE_PATH)

    assert absent.status_code == real.status_code == 401
    assert absent.json() == real.json()
    assert absent.headers["www-authenticate"] == real.headers["www-authenticate"]


def test_a_bad_token_on_an_unmatched_path_is_refused_the_same_way(
    client: TestClient,
) -> None:
    """Inherited validation, not a presence check — the catch-all is a real route."""
    assert_unauthenticated(
        client.get(UNMATCHED_PATH, headers=bearer(make_token(key=_FOREIGN_KEY)))
    )


def test_an_authenticated_caller_still_gets_a_real_404(client: TestClient) -> None:
    """The other half of the requirement: real 404s must survive.

    An authenticated caller has proved they may know the shape of this API, and
    telling them a path does not exist is the honest answer. Turning every 404
    under `/api/v1` into a 401 would break `PROFILE_NOT_FOUND`, which is a real
    404 from a real route.
    """
    _assert_envelope(
        client.get(UNMATCHED_PATH, headers=bearer(make_token())), 404, retryable=False
    )


def test_a_wrong_method_on_a_real_route_survives_the_catch_all(
    client: TestClient,
) -> None:
    """The cost of matching everything, bought back deliberately.

    A catch-all claiming every method turns `POST /api/v1/profile` into an
    ordinary full match, so Starlette never produces the 405 and `Allow` a
    client needs. `_methods_answering` asks the routing table the same question
    directly, and this is what says it answers correctly.
    """
    response = client.post(PROBE_PATH, headers=bearer(make_token()))

    _assert_envelope(response, 405, retryable=False)
    assert "GET" in response.headers["allow"]


def test_a_path_outside_the_versioned_seam_is_untouched(client: TestClient) -> None:
    """`/health` is open by construction and unknown paths outside `/api/v1`
    are still an ordinary 404 — the guard is scoped to the prefix it protects."""
    _assert_envelope(client.get("/no/such/path"), 404, retryable=False)
    assert client.get("/health").status_code == 200


def test_the_catch_all_is_the_last_route_or_it_shadows_the_real_ones() -> None:
    """First match wins, so a route registered after this one is unreachable.

    The symptom of getting that wrong is every request to a newly added
    endpoint answering 404, which points nowhere near the cause — so the
    ordering is asserted rather than left to the comment beside it.
    """
    routes = create_app().router.routes
    guards = [
        index
        for index, route in enumerate(routes)
        if getattr(route, "path", None) in v1.UNMATCHED_PATH_ROUTES
    ]

    assert len(guards) == len(v1.UNMATCHED_PATH_ROUTES), (
        "each catch-all path must be declared exactly once"
    )
    assert guards == list(range(len(routes) - len(guards), len(routes))), (
        "the catch-all routes must be last, or they shadow real ones"
    )


def test_the_catch_all_is_not_published_as_api_surface(client: TestClient) -> None:
    """It documents nothing callable, and would read as a real endpoint."""
    paths = client.get("/openapi.json").json()["paths"]

    assert not set(v1.UNMATCHED_PATH_ROUTES) & set(paths)
    assert not any("unmatched" in path for path in paths)


@pytest.mark.parametrize("method", ["TRACE", "CONNECT", "PROPFIND", "WHATEVER"])
def test_no_verb_short_circuits_past_the_boundary(
    client: TestClient, method: str
) -> None:
    """A declared method set is a hole, not a courtesy.

    Any verb outside it fell through to Starlette's own 405 — whose `Allow`
    header then names exactly which methods the real route answers.
    `TRACE /api/v1/profile` answered `Allow: GET` while `TRACE` on an absent
    path answered the catch-all's full declared list, which is the distinction
    this route exists to remove, restored by any verb nobody thought to declare.
    Invented verbs included, hence `WHATEVER`.
    """
    real = client.request(method, PROBE_PATH)
    absent = client.request(method, UNMATCHED_PATH)

    assert real.status_code == absent.status_code == 401
    assert real.json() == absent.json()
    assert "allow" not in {name.lower() for name in real.headers}


def test_the_catch_all_matches_on_path_alone(client: TestClient) -> None:
    """The mechanism behind the row above, so a revert is named where it happens."""
    assert issubclass(v1.AnyMethodRoute, APIRoute)

    routes = [
        route
        for route in create_app().router.routes
        if getattr(route, "path", None) in v1.UNMATCHED_PATH_ROUTES
    ]

    assert routes and all(isinstance(route, v1.AnyMethodRoute) for route in routes)


def test_a_trailing_slash_on_a_real_route_still_redirects(client: TestClient) -> None:
    """The regression this route introduced, reinstated.

    `GET /api/v1/profile/` was a `307` to the canonical path before story 8 and
    became a hard `404`, because the catch-all full-matches and Starlette's
    `redirect_slashes` never runs. A client-visible break for a request that
    used to work is not an acceptable side effect of an error-shape change.
    """
    response = client.get(
        PROBE_PATH + "/", headers=bearer(make_token()), follow_redirects=False
    )

    assert response.status_code == 307, response.text
    assert response.headers["location"].endswith(PROBE_PATH)


def test_a_trailing_slash_on_an_absent_route_is_still_a_404(
    client: TestClient,
) -> None:
    """The redirect must not invent a destination that does not exist either."""
    _assert_envelope(
        client.get(
            UNMATCHED_PATH + "/", headers=bearer(make_token()), follow_redirects=False
        ),
        404,
        retryable=False,
    )


def test_the_versioned_prefix_itself_is_not_a_redirect(client: TestClient) -> None:
    """`/api/v1` has no separating slash, so a `{path:path}` route misses it.

    It fell through to `redirect_slashes` and answered `307` without a token —
    the one status the uniformity is supposed to remove, reachable by dropping
    a character.
    """
    response = client.get("/api/v1", follow_redirects=False)

    assert response.status_code == 401, response.text
    assert_unauthenticated(response)


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


#: Keycloak stores `name` and `description` on a client in `varchar(255)`.
#:
#: Not a style rule. A longer value aborts the ENTIRE realm import with
#: `ERROR: value too long for type character varying(255)`, Keycloak exits
#: non-zero, and `docker compose up -d --wait` fails with the cause in the
#: keycloak log and nothing at all in the API's — which is exactly how a
#: well-meant paragraph of explanation in an export takes the whole stack down.
#: Found the hard way while adding the second client.
KEYCLOAK_VARCHAR_LIMIT = 255

#: Fields on a client that land in one of those columns.
LENGTH_LIMITED_CLIENT_FIELDS = ("name", "description")


@pytest.mark.parametrize("field", LENGTH_LIMITED_CLIENT_FIELDS)
def test_no_client_field_overflows_keycloaks_column(field: str) -> None:
    """Explanation belongs in .env.example and the story, which have room."""
    for client in _realm_export()["clients"]:
        value = client.get(field, "")
        assert len(value) <= KEYCLOAK_VARCHAR_LIMIT, (
            f"{client['clientId']}.{field} is {len(value)} characters; anything "
            f"over {KEYCLOAK_VARCHAR_LIMIT} aborts the realm import and the "
            "whole stack fails to come up"
        )


def test_the_web_origins_are_scoped_to_real_origins() -> None:
    """`*` would open CORS on the token endpoint to every origin on the web.

    The client is confidential, so an origin without the secret still gets
    nothing — but a wildcard is a larger surface than this needs, and the only
    two origins that exist are the local `/docs` and the deployed name.
    """
    origins = _api_client(_realm_export())["webOrigins"]

    assert origins, "the client needs a web origin or /docs' Authorize button cannot mint"
    assert "*" not in origins, "scope CORS to the origins that actually exist"
    assert "http://127.0.0.1:8000" in origins
