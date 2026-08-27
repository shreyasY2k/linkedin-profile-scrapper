"""``PUT`` and ``GET /api/v1/session``, end to end against a real token.

The first routes on the ``/api/v1`` seam, so this file is also the first proof
that the authentication boundary story 3 attached to the router is exercised by
a real request rather than only by a probe route.

Tokens are signed by the key `tests/test_auth.py` generates in-process, so the
whole file runs with no Keycloak, no Postgres and no network — which is what
``docker build --target test && docker run --network none`` requires.

The single assertion this file exists for, repeated against every path: the
stored cookie value never appears in a response body, a header, or a log line.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import auth
from app.api.v1 import session as session_routes
from app.main import create_app
from app.vault import SessionVault
from tests.test_auth import RecordingFetcher, bearer, make_token
from tests.test_vault import COOKIE, OTHER_COOKIE, InMemoryStore

SESSION_PATH = "/api/v1/session"

#: Two different Keycloak subjects. `make_token` defaults to the first.
SUBJECT_A = "615225e6-fb6a-4d02-a323-7b1fe4b6e88b"
SUBJECT_B = "9f2c1d84-0a77-4a15-bd0e-1c7a3f5b2e40"


@pytest.fixture(name="store")
def _store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture(name="vault")
def _vault(store: InMemoryStore) -> SessionVault:
    return SessionVault(store, Fernet(Fernet.generate_key()))


class RecordingVerifier:
    """Stands in for the one `me` call `PUT` makes against LinkedIn.

    The suite never reaches the network, so the verifier is always overridden.
    What these tests exercise is what the route DOES with each of the three
    answers — `True`, `False`, and "could not tell" — not the Voyager call
    itself, which `tests/test_linkedin_client.py` covers offline already.
    """

    def __init__(self, verdict: bool | None = True) -> None:
        self.verdict = verdict
        self.cookies: list[str] = []
        self.raises: Exception | None = None

    async def __call__(self, cookie: str) -> bool | None:
        self.cookies.append(cookie)
        if self.raises is not None:
            raise self.raises
        return self.verdict


@pytest.fixture(name="verifier")
def _verifier() -> RecordingVerifier:
    return RecordingVerifier()


@pytest.fixture(name="client")
def _client(
    vault: SessionVault,
    verifier: RecordingVerifier,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """The real app, with the real dependency chain and a fake datastore.

    Only the *store* is substituted. Token validation, the router-level
    dependency, the vault's encryption, the response model and the error
    envelope are all the shipping ones — the point of this file is the wiring,
    and overriding `require_claims` would delete the half of it worth testing.

    `TestClient` is deliberately NOT used as a context manager: Starlette runs
    the lifespan only then, and the lifespan is what bootstraps the Postgres
    schema. Entering it here would make every test in this file need a database.
    """
    monkeypatch.setattr(
        auth, "jwks_cache", auth.JwksCache(auth.JWKS_URL, fetcher=RecordingFetcher())
    )
    application = create_app()
    application.dependency_overrides[session_routes.get_vault] = lambda: vault
    application.dependency_overrides[session_routes.get_session_verifier] = (
        lambda: verifier
    )
    yield TestClient(application, raise_server_exceptions=False)
    application.dependency_overrides.clear()


def token_for(subject: str) -> dict[str, str]:
    return bearer(make_token(sub=subject))


def assert_never_leaks(response: Any, *secrets: str) -> None:
    """No secret in the body, in any header, or in the reason phrase."""
    haystack = response.text + repr(dict(response.headers))
    for secret in secrets:
        assert secret not in haystack, response.text


# --- Matrix: store, replace, read --------------------------------------------


def test_storing_a_session_confirms_presence_without_echoing_the_value(
    client: TestClient,
) -> None:
    response = client.put(
        SESSION_PATH, json={"li_at": COOKIE}, headers=token_for(SUBJECT_A)
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stored"] is True
    assert body["stored_at"]
    assert_never_leaks(response, COOKIE)


def test_the_response_is_never_cacheable(client: TestClient) -> None:
    """Credential status, behind nginx and a Cloudflare edge.

    Nothing here varies by anything a cache keys on except the bearer token,
    and caches do not key on that — so a cached 200 for one subject served to
    another is a disclosure needing no code change to cause it. Story 7 is
    explicitly a caching story, which makes saying so now cheaper than
    remembering it later.
    """
    headers = token_for(SUBJECT_A)

    for response in (
        client.put(SESSION_PATH, json={"li_at": COOKIE}, headers=headers),
        client.get(SESSION_PATH, headers=headers),
    ):
        assert response.headers["cache-control"] == "no-store", response.request.method


def test_the_stored_value_actually_reached_the_vault(
    client: TestClient, vault: SessionVault
) -> None:
    """The leak assertions everywhere else must be failing for the right reason."""
    client.put(SESSION_PATH, json={"li_at": COOKIE}, headers=token_for(SUBJECT_A))

    assert vault.unlock(SUBJECT_A)[0].reveal() == COOKIE


def test_reading_presence_after_storing(client: TestClient) -> None:
    client.put(SESSION_PATH, json={"li_at": COOKIE}, headers=token_for(SUBJECT_A))

    response = client.get(SESSION_PATH, headers=token_for(SUBJECT_A))

    assert response.status_code == 200, response.text
    assert response.json()["stored"] is True
    assert_never_leaks(response, COOKIE)


def test_nothing_stored_is_a_200_saying_so_not_an_error(client: TestClient) -> None:
    """The matrix is explicit: "no session" is a state, not a failure."""
    response = client.get(SESSION_PATH, headers=token_for(SUBJECT_A))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "stored": False,
        "stored_at": None,
        "last_used_at": None,
        "last_use_ok": None,
    }


def test_putting_again_replaces_outright(
    client: TestClient, vault: SessionVault, store: InMemoryStore
) -> None:
    client.put(SESSION_PATH, json={"li_at": COOKIE}, headers=token_for(SUBJECT_A))
    response = client.put(
        SESSION_PATH, json={"li_at": OTHER_COOKIE}, headers=token_for(SUBJECT_A)
    )

    assert response.status_code == 200, response.text
    assert list(store.rows) == [SUBJECT_A], "overwrite must not create a second row"
    assert vault.unlock(SUBJECT_A)[0].reveal() == OTHER_COOKIE
    assert_never_leaks(response, COOKIE, OTHER_COOKIE)


def test_there_is_no_delete_endpoint(client: TestClient) -> None:
    """Overwrite is the entire lifecycle in the Must tier, by decision."""
    response = client.delete(SESSION_PATH, headers=token_for(SUBJECT_A))

    assert response.status_code == 405, response.text
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


# --- Matrix: subject isolation ------------------------------------------------


def test_a_second_subject_cannot_see_the_first_subjects_state(
    client: TestClient,
) -> None:
    """CAP-4: whether A has a session at all must be invisible to B."""
    client.put(SESSION_PATH, json={"li_at": COOKIE}, headers=token_for(SUBJECT_A))

    response = client.get(SESSION_PATH, headers=token_for(SUBJECT_B))

    assert response.status_code == 200, response.text
    assert response.json()["stored"] is False
    assert_never_leaks(response, COOKIE)


def test_each_subject_reads_back_its_own_session(
    client: TestClient, vault: SessionVault
) -> None:
    client.put(SESSION_PATH, json={"li_at": COOKIE}, headers=token_for(SUBJECT_A))
    client.put(SESSION_PATH, json={"li_at": OTHER_COOKIE}, headers=token_for(SUBJECT_B))

    assert client.get(SESSION_PATH, headers=token_for(SUBJECT_A)).json()["stored"]
    assert client.get(SESSION_PATH, headers=token_for(SUBJECT_B)).json()["stored"]
    assert vault.unlock(SUBJECT_A)[0].reveal() == COOKIE
    assert vault.unlock(SUBJECT_B)[0].reveal() == OTHER_COOKIE


def test_a_subject_in_the_body_cannot_redirect_the_write(
    client: TestClient, store: InMemoryStore
) -> None:
    """The vault key comes from the verified token, never from a request field.

    A silently ignored `subject` field would look to a caller as though it might
    have worked; `extra="forbid"` makes it a refusal instead.
    """
    response = client.put(
        SESSION_PATH,
        json={"li_at": COOKIE, "subject": SUBJECT_B},
        headers=token_for(SUBJECT_A),
    )

    assert response.status_code == 422, response.text
    assert not store.rows
    assert_never_leaks(response, COOKIE)


# --- Matrix: no token ---------------------------------------------------------


@pytest.mark.parametrize("method", ["get", "put"])
def test_no_token_is_the_inherited_401(client: TestClient, method: str) -> None:
    """Neither route declares auth; both must have it anyway."""
    response = getattr(client, method)(
        SESSION_PATH, **({"json": {"li_at": COOKIE}} if method == "put" else {})
    )

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_a_foreign_realm_token_cannot_store_a_session(
    client: TestClient, store: InMemoryStore
) -> None:
    from tests.test_auth import _FOREIGN_KEY

    response = client.put(
        SESSION_PATH,
        json={"li_at": COOKIE},
        headers=bearer(make_token(key=_FOREIGN_KEY)),
    )

    assert response.status_code == 401, response.text
    assert not store.rows
    assert_never_leaks(response, COOKIE)


# --- Matrix: malformed cookie -------------------------------------------------


@pytest.mark.parametrize(
    "cookie",
    ["", "   ", "AQEDA\nInjected: header", "AQEDA\x00null", "A" * 5000],
)
def test_a_malformed_cookie_is_a_typed_4xx_and_stores_nothing(
    client: TestClient, store: InMemoryStore, cookie: str
) -> None:
    response = client.put(
        SESSION_PATH, json={"li_at": cookie}, headers=token_for(SUBJECT_A)
    )

    assert response.status_code == 428, response.text
    assert response.json()["error"]["code"] in {"NO_SESSION", "SESSION_EXPIRED"}
    assert response.json()["error"]["retryable"] is False
    assert not store.rows
    assert_never_leaks(response, cookie.strip() or "unreachable-sentinel")


@pytest.mark.parametrize(
    "payload",
    [{}, {"li_at": None}, {"li_at": 42}, {"li_at": ["a"]}, {"li_at": {"a": 1}}],
)
def test_a_body_of_the_wrong_shape_is_a_422_that_echoes_nothing(
    client: TestClient, store: InMemoryStore, payload: dict
) -> None:
    """FastAPI's default 422 reports the offending input back to the caller.

    On this route that input is a live LinkedIn session, which is why
    `app/errors.py` drops the pydantic detail rather than summarising it.
    """
    response = client.put(SESSION_PATH, json=payload, headers=token_for(SUBJECT_A))

    assert response.status_code == 422, response.text
    assert set(response.json()) == {"error"}
    assert not store.rows


def test_a_422_does_not_echo_a_submitted_cookie(
    client: TestClient, store: InMemoryStore
) -> None:
    """The specific leak the envelope's validation handler exists to stop."""
    response = client.put(
        SESSION_PATH,
        # `li_at` is present and valid; the *extra* key is what fails validation,
        # so the pydantic error report is built from a body containing a cookie.
        json={"li_at": COOKIE, "unexpected": "x"},
        headers=token_for(SUBJECT_A),
    )

    assert response.status_code == 422, response.text
    assert_never_leaks(response, COOKIE)
    assert not store.rows


# --- Matrix: wrong key --------------------------------------------------------


def test_a_row_written_under_another_key_surfaces_as_a_typed_428(
    client: TestClient, store: InMemoryStore
) -> None:
    """Key rotated, old row still in the table. Never a 500, never a silent false."""
    client.put(SESSION_PATH, json={"li_at": COOKIE}, headers=token_for(SUBJECT_A))
    # Everything else about the app stays as it is; only the key changes.
    rotated = SessionVault(store, Fernet(Fernet.generate_key()))
    client.app.dependency_overrides[session_routes.get_vault] = lambda: rotated

    response = client.get(SESSION_PATH, headers=token_for(SUBJECT_A))

    assert response.status_code == 428, response.text
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"
    assert_never_leaks(response, COOKIE)


# --- The value never leaves, on any path --------------------------------------


def test_no_response_on_any_path_contains_the_stored_value(
    client: TestClient,
) -> None:
    """One sweep over every request shape this story can produce."""
    headers = token_for(SUBJECT_A)
    responses = [
        client.put(SESSION_PATH, json={"li_at": COOKIE}, headers=headers),
        client.get(SESSION_PATH, headers=headers),
        client.get(SESSION_PATH, headers=token_for(SUBJECT_B)),
        client.put(SESSION_PATH, json={"li_at": COOKIE}, headers=headers),
        client.get(SESSION_PATH),
        client.post(SESSION_PATH, json={"li_at": COOKIE}, headers=headers),
        client.get(f"{SESSION_PATH}?reveal=true&debug=1", headers=headers),
        client.get("/openapi.json"),
    ]

    for response in responses:
        assert_never_leaks(response, COOKIE)


def test_a_query_parameter_cannot_turn_the_value_on(client: TestClient) -> None:
    """There is no flag, and asking for one must not change the answer."""
    headers = token_for(SUBJECT_A)
    client.put(SESSION_PATH, json={"li_at": COOKIE}, headers=headers)

    plain = client.get(SESSION_PATH, headers=headers).json()
    with_flags = client.get(
        f"{SESSION_PATH}?reveal=true&include=li_at&debug=1", headers=headers
    ).json()

    assert plain.keys() == with_flags.keys()
    assert set(plain) == {"stored", "stored_at", "last_used_at", "last_use_ok"}


def test_the_shipping_response_model_has_no_field_for_the_value() -> None:
    """The type is the guarantee. Adding a field here has to fail a test."""
    assert set(session_routes.SessionResponse.model_fields) == {
        "stored",
        "stored_at",
        "last_used_at",
        "last_use_ok",
    }


def test_the_response_model_filters_a_value_a_handler_returns_anyway() -> None:
    """The third guard from the module docstring, exercised rather than assumed.

    `response_model=SessionResponse` is what makes "the value is never returned"
    survive a later edit that puts one on the object a handler hands back. Built
    on a throwaway app so the assertion is about FastAPI's filtering of *this*
    model, not about the session routes happening to behave.
    """
    from fastapi import FastAPI

    class Leaky(session_routes.SessionResponse):
        li_at: str = COOKIE

    application = FastAPI()

    @application.get("/probe", response_model=session_routes.SessionResponse)
    def _probe() -> Any:
        return Leaky(stored=True)

    response = TestClient(application).get("/probe")

    assert response.status_code == 200, response.text
    assert "li_at" not in response.json()
    assert COOKIE not in response.text


def test_nothing_is_logged_that_contains_the_cookie(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """`docker compose logs api | grep -F "$COOKIE"` must find nothing."""
    with caplog.at_level(logging.DEBUG):
        client.put(SESSION_PATH, json={"li_at": COOKIE}, headers=token_for(SUBJECT_A))
        client.get(SESSION_PATH, headers=token_for(SUBJECT_A))
        client.put(SESSION_PATH, json={"li_at": "AQEDA\nbroken"}, headers=token_for(SUBJECT_A))
        client.put(SESSION_PATH, json={"li_at": COOKIE, "extra": 1}, headers=token_for(SUBJECT_A))

    assert COOKIE not in caplog.text


# --- The generated document ---------------------------------------------------


def test_the_session_routes_are_documented(client: TestClient) -> None:
    """Story 9 ships this document as the README's API documentation."""
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths[SESSION_PATH]) >= {"get", "put"}
    for method in ("get", "put"):
        assert paths[SESSION_PATH][method]["security"] == [
            {auth.SECURITY_SCHEME_NAME: []}
        ]


def test_the_documented_response_schema_has_no_field_for_the_value(
    client: TestClient,
) -> None:
    """The contract a consumer reads must promise presence and nothing more."""
    schemas = client.get("/openapi.json").json()["components"]["schemas"]

    assert set(schemas["SessionResponse"]["properties"]) == {
        "stored",
        "stored_at",
        "last_used_at",
        "last_use_ok",
    }


def test_the_request_schema_carries_no_example_cookie(client: TestClient) -> None:
    """An example on `li_at` would put a cookie-shaped string in `/docs`.

    Swagger pre-fills the "Try it out" body from it, so an example is not merely
    documentation — it is a value a reader will send.
    """
    schema = client.get("/openapi.json").json()["components"]["schemas"][
        "SessionRequest"
    ]["properties"]["li_at"]

    assert "example" not in schema
    assert "examples" not in schema


def test_the_documented_errors_are_the_ones_these_routes_can_answer(
    client: TestClient,
) -> None:
    """A documented status a route never returns is worse than none at all."""
    responses = client.get("/openapi.json").json()["paths"][SESSION_PATH]["put"][
        "responses"
    ]

    assert "401" in responses and "428" in responses
    for status in ("401", "428"):
        ref = responses[status]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("/ErrorEnvelope")
    # Both 428 codes share one status, so both meanings must be named in it.
    description = responses["428"]["description"]
    assert "NO_SESSION" in description and "SESSION_EXPIRED" in description


# --- PUT verifies the cookie it just stored -----------------------------------
#
# Without this, `last_use_ok` is permanently null while `response-schema.md`,
# the README and the field's own description all promise GET reports last-use
# validity — a documented value that can never exist. It is also the answer to
# the only question a caller has after pasting a cookie: did that work?


def test_storing_a_working_session_reports_that_it_works(
    client: TestClient, verifier: RecordingVerifier
) -> None:
    verifier.verdict = True

    body = client.put(
        SESSION_PATH, json={"li_at": COOKIE}, headers=token_for(SUBJECT_A)
    ).json()

    assert body["stored"] is True
    assert body["last_use_ok"] is True
    assert body["last_used_at"] is not None
    assert verifier.cookies == [COOKIE]


def test_storing_a_dead_session_says_so_immediately_and_still_stores_it(
    client: TestClient, vault: SessionVault, verifier: RecordingVerifier
) -> None:
    """Surfaced, never healed — and never at the cost of the credential.

    A caller who pastes a dead cookie learns it here rather than at their first
    profile request, where the failure looks like a bug in the service.
    """
    verifier.verdict = False

    response = client.put(
        SESSION_PATH, json={"li_at": COOKIE}, headers=token_for(SUBJECT_A)
    )

    assert response.status_code == 200, response.text
    assert response.json()["stored"] is True
    assert response.json()["last_use_ok"] is False
    # Stored regardless: a verdict is not a reason to throw the value away.
    assert vault.unlock(SUBJECT_A)[0].reveal() == COOKIE
    assert_never_leaks(response, COOKIE)


def test_an_inconclusive_check_leaves_validity_unknown(
    client: TestClient, vault: SessionVault, verifier: RecordingVerifier
) -> None:
    """A throttle or a challenge is not evidence about the cookie.

    Recording `last_use_ok: false` for a LinkedIn outage would libel a perfectly
    good session, and would tell the caller to replace something that works.
    """
    verifier.verdict = None

    body = client.put(
        SESSION_PATH, json={"li_at": COOKIE}, headers=token_for(SUBJECT_A)
    ).json()

    assert body["stored"] is True
    assert body["last_use_ok"] is None
    assert vault.unlock(SUBJECT_A)[0].reveal() == COOKIE


def test_a_verifier_that_raises_never_costs_the_caller_the_credential(
    client: TestClient, vault: SessionVault, verifier: RecordingVerifier
) -> None:
    """Stored FIRST. That ordering is the safety property, not an accident."""
    verifier.raises = RuntimeError("LinkedIn fell over mid-check")

    response = client.put(
        SESSION_PATH, json={"li_at": COOKIE}, headers=token_for(SUBJECT_A)
    )

    assert response.status_code == 500, response.text
    # The request failed, but the session the caller supplied is safely stored
    # and their next GET will find it.
    assert vault.unlock(SUBJECT_A)[0].reveal() == COOKIE
    assert_never_leaks(response, COOKIE)


def test_the_real_verifier_never_raises(caplog: pytest.LogCaptureFixture) -> None:
    """The shipping verifier absorbs everything: it runs after the store.

    Asserted against the real function rather than the fake, because the fake is
    what every other test in this file uses and would happily hide a verifier
    that propagated.
    """
    import asyncio

    from app.linkedin import client as linkedin_client

    def exploding_transport(url, headers, timeout):
        raise linkedin_client.TransportError("no network")

    original = linkedin_client.urllib_transport
    linkedin_client.urllib_transport = exploding_transport
    try:
        with caplog.at_level(logging.INFO):
            verdict = asyncio.run(session_routes.check_stored_session(COOKIE))
    finally:
        linkedin_client.urllib_transport = original

    assert verdict is None, "a network failure is not a verdict about the cookie"


def test_the_verifier_reports_false_only_for_a_refused_session() -> None:
    """`SESSION_EXPIRED` is the one code that is evidence about the cookie."""
    import asyncio

    from app.errors import ApiError
    from app.linkedin.client import VoyagerClient

    async def refuse(self):
        raise ApiError("SESSION_EXPIRED", log_detail="LinkedIn refused the session")

    async def throttle(self):
        raise ApiError("RATE_LIMITED", log_detail="throttled")

    original = VoyagerClient.check_session
    try:
        VoyagerClient.check_session = refuse  # type: ignore[method-assign]
        assert asyncio.run(session_routes.check_stored_session(COOKIE)) is False

        VoyagerClient.check_session = throttle  # type: ignore[method-assign]
        assert asyncio.run(session_routes.check_stored_session(COOKIE)) is None
    finally:
        VoyagerClient.check_session = original  # type: ignore[method-assign]


def test_a_malformed_cookie_is_never_sent_to_linkedin(
    client: TestClient, verifier: RecordingVerifier
) -> None:
    """Validation runs before the store, and the store before the check."""
    client.put(SESSION_PATH, json={"li_at": "AQEDA\nbroken"}, headers=token_for(SUBJECT_A))

    assert verifier.cookies == [], "a cookie that can never work still spent quota"


def test_a_verdict_for_a_session_replaced_mid_check_is_discarded(
    client: TestClient, vault: SessionVault
) -> None:
    """The race, through the real route.

    The verifier replaces the caller's session while its own check is in
    flight — exactly what a concurrent `PUT` does — and the verdict it then
    returns belongs to a cookie that is no longer stored.
    """

    async def replace_then_condemn(cookie: str) -> bool:
        vault.store(SUBJECT_A, OTHER_COOKIE)
        return False

    client.app.dependency_overrides[session_routes.get_session_verifier] = (
        lambda: replace_then_condemn
    )

    body = client.put(
        SESSION_PATH, json={"li_at": COOKIE}, headers=token_for(SUBJECT_A)
    ).json()

    assert body["last_use_ok"] is None, "a stale verdict reached the new session"
    assert vault.state(SUBJECT_A).last_use_ok is None


# --- The datastore failing is a typed 503, not a naked 500 --------------------


def test_a_datastore_failure_is_the_typed_envelope(client: TestClient) -> None:
    """A Postgres hiccup must not tell a caller their request was the problem."""
    from app.db import DatastoreUnavailable

    class BrokenStore:
        def upsert(self, subject: str, ciphertext: bytes):
            raise DatastoreUnavailable("OperationalError")

        def fetch(self, subject: str):
            raise DatastoreUnavailable("OperationalError")

        def mark_use(self, subject: str, **kwargs: Any):
            raise DatastoreUnavailable("OperationalError")

    broken = SessionVault(BrokenStore(), Fernet(Fernet.generate_key()))
    client.app.dependency_overrides[session_routes.get_vault] = lambda: broken

    for response in (
        client.get(SESSION_PATH, headers=token_for(SUBJECT_A)),
        client.put(SESSION_PATH, json={"li_at": COOKIE}, headers=token_for(SUBJECT_A)),
    ):
        assert response.status_code == 503, response.text
        body = response.json()
        assert body["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert body["error"]["retryable"] is True
        assert_never_leaks(response, COOKIE)


def test_the_datastore_code_is_not_mistaken_for_a_taxonomy_row() -> None:
    """`response-schema.md` has no row for this API's own datastore being down."""
    from app.errors import ERROR_SPECS, FALLBACK_CODES

    assert FALLBACK_CODES[503] == "SERVICE_UNAVAILABLE"
    assert "SERVICE_UNAVAILABLE" not in ERROR_SPECS


# --- Two subjects, which is what makes CAP-4 demonstrable ---------------------


def test_the_realm_ships_two_distinct_service_account_clients() -> None:
    """One client means one service-account user, and therefore ONE `sub`.

    Per-caller isolation is then real in the code and impossible to show in a
    running stack — and two people sharing the documented credentials silently
    overwrite each other's stored cookie. The second client exists to be a
    second subject and nothing else; the first stays THE evaluator lane.
    """
    from tests.test_auth import _realm_export

    clients = _realm_export()["clients"]
    ids = [c["clientId"] for c in clients]

    assert len(clients) == 2, ids
    assert ids[0] == "${KEYCLOAK_CLIENT_ID}", "the evaluator lane must stay first"
    assert len(set(ids)) == 2

    for client in clients:
        assert client["serviceAccountsEnabled"] is True
        assert client["publicClient"] is False
        # Both must mint tokens the API accepts, which means BOTH audience
        # mappers name the API's client id — not their own.
        mappers = [
            m for m in client["protocolMappers"]
            if m["protocolMapper"] == "oidc-audience-mapper"
        ]
        assert len(mappers) == 1, client["clientId"]
        assert mappers[0]["config"]["included.client.audience"] == "${KEYCLOAK_CLIENT_ID}"


def test_the_second_lane_gets_its_own_row(client: TestClient, vault: SessionVault) -> None:
    """What two subjects buys, asserted end to end over HTTP.

    `SUBJECT_A` and `SUBJECT_B` stand in for the two service-account users the
    realm now creates. This is the CAP-4 proof the README's two-lane section
    walks a human through against the running stack.
    """
    client.put(SESSION_PATH, json={"li_at": COOKIE}, headers=token_for(SUBJECT_A))

    assert client.get(SESSION_PATH, headers=token_for(SUBJECT_B)).json()["stored"] is False

    client.put(SESSION_PATH, json={"li_at": OTHER_COOKIE}, headers=token_for(SUBJECT_B))

    assert vault.unlock(SUBJECT_A)[0].reveal() == COOKIE
    assert vault.unlock(SUBJECT_B)[0].reveal() == OTHER_COOKIE


# --- An ambiguous credential is refused, not resolved -------------------------


def test_two_authorization_headers_are_rejected(client: TestClient) -> None:
    """Starlette's `get` returns the first and drops the rest, silently.

    That is a request-smuggling shape: an intermediary that reads the last
    header and a backend that reads the first disagree about who the caller is,
    and neither log records the disagreement.
    """
    good = make_token(sub=SUBJECT_A)
    other = make_token(sub=SUBJECT_B)

    response = client.get(
        SESSION_PATH,
        headers=[("Authorization", f"Bearer {good}"), ("Authorization", f"Bearer {other}")],
    )

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
