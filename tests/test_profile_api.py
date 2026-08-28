"""``GET /api/v1/profile`` end to end — real token, real router, stubbed client.

The endpoint CAP-1 is graded on, so this file exercises the whole chain the
request actually travels: token validation, the router-level auth boundary, the
vault, the error envelope, and the mapper. Only two things are substituted, and
both for the same reason — the suite has to pass under
``docker run --network none``:

* the **session store**, replaced with an in-memory one;
* the **fetcher**, replaced with a recorder that returns a
  :class:`~app.linkedin.client.RawProfile` built from the same synthetic
  fixtures ``tests/test_mapping.py`` uses.

Everything else is the shipping code. Overriding ``require_claims`` would
delete the half of the wiring worth testing.

===============================================================================
THE GAP THAT MADE THIS FILE'S STUBBING DANGEROUS
===============================================================================

Stubbing the fetcher means the one function joining this endpoint to LinkedIn —
:func:`app.api.v1.profile.fetch_via_voyager` — was executed by **no test at
all**. A review pass rewrote it as ``VoyagerClient(url).fetch_profile(cookie)``,
arguments swapped, and the entire suite stayed green while 100% of real
requests would have failed: the profile URL would go into the ``li_at`` header
and the cookie through ``parse_profile_url``. Both parameters are ``str``, so
nothing about the types objects.

``test_fetch_via_voyager_*`` below closes that by executing the real function
against a recording double. Any test that stubs a seam should assume the seam
itself is untested until something asserts otherwise.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import re
import textwrap
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import auth
from app.api.v1 import profile as profile_routes
from app.api.v1 import session as session_routes
from app.cache import ProfileCache
from app.errors import CAUSE_MALFORMED_BODY, CAUSE_MEMBER_MISMATCH, ApiError
from app.linkedin.client import RawProfile
from app.main import create_app
from app.mapping.profile import CONTRACT_FIELDS, EMPLOYMENT_TYPE_PATH
from app.vault import SessionVault
from tests.support import (
    COOKIE,
    FETCHED_AT,
    FULL_SECTIONS,
    OTHER_COOKIE,
    PROFILE_URL,
    PUBLIC_ID,
    SPARSE_SECTIONS,
    SUBJECT_A,
    SUBJECT_B,
    InMemoryProfileCacheStore,
    InMemoryStore,
    RecordingFetcher,
    bearer,
    failed_section,
    make_token,
    raw_profile,
)

PROFILE_PATH = "/api/v1/profile"

#: RFC 3339, UTC, second precision — the shape `response-schema.md` documents.
FETCHED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

#: The complete fixture's baseline `partial`. See `tests/test_mapping.py`:
#: LinkedIn names no readable employment type, on real profiles too.
BASELINE_PARTIAL = [EMPLOYMENT_TYPE_PATH]


class RecordingFetch:
    """Stands in for the six live Voyager calls.

    Records **both** arguments. Recording only the cookie was itself a
    verification gap: a review pass hardcoded a different profile URL into the
    fetch call and every test stayed green.
    """

    def __init__(self, result: RawProfile | None = None) -> None:
        self.result = result if result is not None else raw_profile()
        self.raises: Exception | None = None
        self.delay: float = 0.0
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, cookie: str, url: str) -> RawProfile:
        self.calls.append((cookie, url))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return self.result

    @property
    def cookies(self) -> list[str]:
        return [cookie for cookie, _ in self.calls]

    @property
    def urls(self) -> list[str]:
        return [url for _, url in self.calls]


@pytest.fixture(name="store")
def _store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture(name="vault")
def _vault(store: InMemoryStore) -> SessionVault:
    return SessionVault(store, Fernet(Fernet.generate_key()))


@pytest.fixture(name="fetch")
def _fetch() -> RecordingFetch:
    return RecordingFetch()


@pytest.fixture(name="cache_store")
def _cache_store() -> InMemoryProfileCacheStore:
    return InMemoryProfileCacheStore()


@pytest.fixture(name="cache")
def _cache(cache_store: InMemoryProfileCacheStore) -> ProfileCache:
    """The real cache over an in-memory store.

    Substituted rather than left as the process-wide one for the same reason the
    vault is: the shipping instance talks to Postgres, and this suite has to
    pass under `docker run --network none`. Left unsubstituted every cache
    operation would fail, be swallowed as designed, and quietly turn every
    stale-serve assertion below into a test of nothing.
    """
    return ProfileCache(cache_store)


@pytest.fixture(name="client")
def _client(
    vault: SessionVault,
    fetch: RecordingFetch,
    cache: ProfileCache,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setattr(
        auth, "jwks_cache", auth.JwksCache(auth.JWKS_URL, fetcher=RecordingFetcher())
    )
    application = create_app()
    application.dependency_overrides[session_routes.get_vault] = lambda: vault
    application.dependency_overrides[profile_routes.get_profile_fetcher] = lambda: fetch
    application.dependency_overrides[profile_routes.get_profile_cache] = lambda: cache
    application.dependency_overrides[session_routes.get_session_verifier] = (
        lambda: _never_verified
    )
    yield TestClient(application, raise_server_exceptions=False)
    application.dependency_overrides.clear()


async def _never_verified(cookie: str) -> bool | None:
    return None


def token_for(subject: str) -> dict[str, str]:
    return bearer(make_token(sub=subject))


def store_session(vault: SessionVault, subject: str, cookie: str = COOKIE) -> None:
    vault.store(subject, cookie)


def get(client: TestClient, subject: str = SUBJECT_A, url: str = PROFILE_URL) -> Any:
    return client.get(PROFILE_PATH, params={"url": url}, headers=token_for(subject))


# --- C1: the one line joining this endpoint to LinkedIn -----------------------


class RecordingVoyagerClient:
    """A `VoyagerClient` double that records what each half of the call got."""

    instances: list["RecordingVoyagerClient"] = []

    def __init__(self, cookie: str, **kwargs: Any) -> None:
        self.constructed_with = cookie
        self.fetched: list[str] = []
        RecordingVoyagerClient.instances.append(self)

    async def fetch_profile(self, url: str) -> RawProfile:
        self.fetched.append(url)
        return raw_profile()


@pytest.fixture(name="voyager_double")
def _voyager_double(monkeypatch: pytest.MonkeyPatch) -> Iterator[type]:
    RecordingVoyagerClient.instances = []
    monkeypatch.setattr(profile_routes, "VoyagerClient", RecordingVoyagerClient)
    yield RecordingVoyagerClient
    RecordingVoyagerClient.instances = []


def test_fetch_via_voyager_puts_the_cookie_in_the_client_and_the_url_in_the_fetch(
    voyager_double: type,
) -> None:
    """The mutation that survived 661 green tests.

    Swapping these two arguments type-checks perfectly and then sends the
    profile URL as the `li_at` cookie and the cookie through
    `parse_profile_url`. Every real request fails; nothing else in the suite
    notices, because everything else stubs this function out.
    """
    asyncio.run(profile_routes.fetch_via_voyager(COOKIE, PROFILE_URL))

    (client,) = voyager_double.instances
    assert client.constructed_with == COOKIE, "the cookie must reach the constructor"
    assert client.fetched == [PROFILE_URL], "the URL must reach fetch_profile"


def test_fetch_via_voyager_builds_one_client_per_call(voyager_double: type) -> None:
    """CAP-4: a shared client would hold one session for every caller."""
    asyncio.run(profile_routes.fetch_via_voyager(COOKIE, PROFILE_URL))
    asyncio.run(profile_routes.fetch_via_voyager(OTHER_COOKIE, PROFILE_URL))

    assert [c.constructed_with for c in voyager_double.instances] == [
        COOKIE,
        OTHER_COOKIE,
    ]


def test_the_default_fetcher_dependency_is_the_real_one() -> None:
    """Otherwise the test above proves something the endpoint does not use."""
    assert profile_routes.get_profile_fetcher() is profile_routes.fetch_via_voyager


# --- Matrix: complete profile -------------------------------------------------


def test_a_complete_profile_returns_the_documented_envelope(
    client: TestClient, vault: SessionVault
) -> None:
    store_session(vault, SUBJECT_A)

    response = get(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["url"] == f"https://www.linkedin.com/in/{PUBLIC_ID}"
    assert body["public_id"] == PUBLIC_ID
    assert body["stale"] is False
    assert FETCHED_AT_RE.match(body["fetched_at"]), body["fetched_at"]
    assert body["partial"] == BASELINE_PARTIAL
    assert body["profile"]["name"]["full"] == "Ada Placeholder"
    assert body["profile"]["location"] == {"country": "ZZ", "region": "Placeholder City"}


def test_the_envelope_carries_exactly_the_contracts_top_level_keys(
    client: TestClient, vault: SessionVault
) -> None:
    store_session(vault, SUBJECT_A)

    body = get(client).json()

    assert set(body) == {"url", "public_id", "stale", "fetched_at", "partial", "profile"}


def test_partial_is_always_present_even_when_nothing_degraded(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """`response-schema.md` says always present, `[]` on a complete answer.

    Omitting it when empty would make "nothing degraded" and "this response
    predates the field" indistinguishable.
    """
    store_session(vault, SUBJECT_A)
    fetch.result = raw_profile(
        core_fixture="voyager_sparse_core.json", sections=SPARSE_SECTIONS
    )

    body = get(client).json()

    assert body["partial"] == []


def test_the_url_is_canonicalised_rather_than_echoed(
    client: TestClient, vault: SessionVault
) -> None:
    """A locale prefix, a sub-path and a tracking query all name one profile."""
    store_session(vault, SUBJECT_A)

    body = get(
        client,
        url="https://in.linkedin.com/en/in/Ada-Placeholder/details/experience?trk=share",
    ).json()

    assert body["url"] == f"https://www.linkedin.com/in/{PUBLIC_ID}"
    assert body["public_id"] == PUBLIC_ID


def test_the_response_is_never_cacheable(
    client: TestClient, vault: SessionVault
) -> None:
    """One person's profile data, behind host nginx and a Cloudflare edge."""
    store_session(vault, SUBJECT_A)

    assert get(client).headers["cache-control"] == "no-store"


# --- C2: the URL the caller asked for is the URL that is fetched --------------


def test_the_url_the_caller_supplied_is_the_url_that_reaches_the_fetcher(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """A review pass hardcoded a different profile URL here and nothing failed."""
    store_session(vault, SUBJECT_A)
    asked = "https://www.linkedin.com/in/ada-placeholder?trk=public_profile"

    get(client, url=asked)

    assert fetch.calls == [(COOKIE, asked)]


def test_two_requests_for_two_profiles_fetch_two_profiles(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    store_session(vault, SUBJECT_A)

    get(client, url="https://www.linkedin.com/in/ada-placeholder")
    get(client, url="https://www.linkedin.com/in/ada-placeholder/details/skills")

    assert fetch.urls == [
        "https://www.linkedin.com/in/ada-placeholder",
        "https://www.linkedin.com/in/ada-placeholder/details/skills",
    ]


def test_the_callers_own_session_is_what_reaches_linkedin(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """CAP-4, at the endpoint that matters: two callers, two cookies."""
    vault.store(SUBJECT_A, COOKIE)
    vault.store(SUBJECT_B, OTHER_COOKIE)

    get(client, SUBJECT_A)
    get(client, SUBJECT_B)

    assert fetch.cookies == [COOKIE, OTHER_COOKIE]


def test_the_cookie_never_appears_in_the_response(
    client: TestClient, vault: SessionVault
) -> None:
    store_session(vault, SUBJECT_A)

    response = get(client)

    haystack = response.text + repr(dict(response.headers))
    assert COOKIE not in haystack


def test_a_successful_fetch_records_the_session_as_working(
    client: TestClient, vault: SessionVault
) -> None:
    """This route is `record_use`'s main production caller."""
    store_session(vault, SUBJECT_A)

    get(client)

    assert vault.state(SUBJECT_A).last_use_ok is True


def test_a_failure_to_record_the_outcome_never_costs_the_caller_the_profile(
    client: TestClient, vault: SessionVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The profile was retrieved; losing the bookkeeping is not a reason to withhold it."""
    store_session(vault, SUBJECT_A)

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the session store fell over")

    monkeypatch.setattr(vault, "record_use", explode)

    response = get(client)

    assert response.status_code == 200, response.text
    assert response.json()["profile"]["name"]["full"] == "Ada Placeholder"


# --- C3: fetched_at is the FETCH's time, not the response's -------------------


def test_fetched_at_is_the_timestamp_the_fetch_reported(
    client: TestClient, vault: SessionVault
) -> None:
    """Not merely RFC 3339-shaped.

    `response-schema.md` calls this the caller's only staleness signal and
    story 7 depends on it completely — yet replacing it with
    `datetime.now(timezone.utc)` left the whole suite green, because the only
    assertion was a regex on its shape.
    """
    store_session(vault, SUBJECT_A)

    body = get(client).json()

    assert body["fetched_at"] == "2026-08-27T09:00:00Z"
    assert body["fetched_at"] == FETCHED_AT.isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def test_an_hours_old_fetch_timestamp_is_reported_as_it_was(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """The case stale-serve turns on: a timestamp that is not close to `now`."""
    old = datetime.now(timezone.utc) - timedelta(hours=9, minutes=30)
    fetch.result = raw_profile(fetched_at=old)
    store_session(vault, SUBJECT_A)

    body = get(client).json()

    assert body["fetched_at"] == old.isoformat(timespec="seconds").replace("+00:00", "Z")
    assert body["stale"] is False, "an old timestamp on a LIVE fetch is not staleness"


def test_a_non_utc_fetch_timestamp_is_normalised_rather_than_reinterpreted(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    moment = datetime(2026, 8, 27, 14, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    fetch.result = raw_profile(fetched_at=moment)
    store_session(vault, SUBJECT_A)

    assert get(client).json()["fetched_at"] == "2026-08-27T09:00:00Z"


# --- Matrix: sparse profile ---------------------------------------------------


def test_a_sparse_profile_reports_empty_sections_not_partial_ones(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    store_session(vault, SUBJECT_A)
    fetch.result = raw_profile(
        core_fixture="voyager_sparse_core.json", sections=SPARSE_SECTIONS
    )

    body = get(client).json()

    assert body["partial"] == []
    assert body["profile"]["education"] == []
    assert body["profile"]["skills"] == []
    assert body["profile"]["headline"] is None
    assert body["profile"]["experience"][0]["end"] is None


# --- Matrix: degraded but successful -----------------------------------------


def test_an_unreadable_section_is_omitted_and_named_in_partial(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    store_session(vault, SUBJECT_A)
    fetch.result = raw_profile(
        overrides={"certifications": failed_section("certifications")}
    )

    response = get(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert "certifications" in body["partial"]
    # The key must be ABSENT from the serialised JSON, not null. This is the
    # assertion the handler returns a hand-built response to protect: a
    # pydantic response model would put the key back as `null`.
    assert "certifications" not in body["profile"]
    assert body["profile"]["skills"], "the rest of the profile still comes back"


def test_an_unreadable_sub_field_is_omitted_from_the_entry_and_named_by_path(
    client: TestClient, vault: SessionVault
) -> None:
    """A1: a raw URN is never published in a field a caller reads as a label."""
    store_session(vault, SUBJECT_A)

    body = get(client).json()

    assert EMPLOYMENT_TYPE_PATH in body["partial"]
    assert "employment_type" not in body["profile"]["experience"][0]
    assert "urn:li:fsd_employmentType" not in json.dumps(body)


def test_a_fully_degraded_fetch_is_still_a_200(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    store_session(vault, SUBJECT_A)
    fetch.result = raw_profile(
        overrides={name: failed_section(name) for name in FULL_SECTIONS}
    )

    response = get(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted(body["partial"]) == sorted(FULL_SECTIONS)
    assert body["profile"]["name"]["full"] == "Ada Placeholder"


# --- B2: the answer must be about the profile that was asked for --------------


def test_a_fetch_that_answers_with_a_different_member_is_refused(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """`url` and `public_id` are rebuilt from the FETCH, so they would agree.

    Without this check a redirect or an upstream substitution publishes one
    person's profile under another person's URL, in a response that is
    internally consistent and completely wrong — and story 7 would cache it.
    """
    store_session(vault, SUBJECT_A)
    fetch.result = raw_profile(public_id="someone-else")

    response = get(client)

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "UPSTREAM_ERROR"
    assert "someone-else" not in response.text


# --- D6: the six calls are bounded overall ------------------------------------


def test_a_wedged_fetch_is_abandoned_rather_than_held_open(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client bounds each call; nothing bounded the set of them."""
    store_session(vault, SUBJECT_A)
    monkeypatch.setattr(profile_routes, "PROFILE_FETCH_DEADLINE_SECONDS", 0.05)
    fetch.delay = 5.0

    response = get(client)

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "UPSTREAM_ERROR"
    # A deadline says nothing about the cookie, so the verdict is left alone.
    assert vault.state(SUBJECT_A).last_use_ok is None


def test_the_deadline_is_above_the_clients_own_worst_case() -> None:
    """A backstop, not a budget: it must never fire on a merely slow fetch."""
    from app.linkedin.client import DEFAULT_TIMEOUT_SECONDS

    # One core call, then five concurrent sections: two sequential timeouts.
    assert profile_routes.PROFILE_FETCH_DEADLINE_SECONDS > DEFAULT_TIMEOUT_SECONDS * 2


# --- Matrix: failures ---------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/company/example",
        "https://example.invalid/in/ada",
        "https://linkedin.com.evil.invalid/in/ada",
        "not a url at all",
        "",
    ],
)
def test_a_bad_url_is_rejected_before_any_upstream_call(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch, url: str
) -> None:
    store_session(vault, SUBJECT_A)

    response = get(client, url=url)

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_URL"
    assert fetch.calls == [], "a malformed URL must cost zero LinkedIn quota"


def test_a_bad_url_never_echoes_the_callers_string_back(
    client: TestClient, vault: SessionVault
) -> None:
    store_session(vault, SUBJECT_A)
    marker = "reflected-marker-9f2c"

    response = get(client, url=f"https://example.invalid/{marker}")

    assert marker not in response.text


def test_no_stored_session_is_428_before_any_upstream_call(
    client: TestClient, fetch: RecordingFetch
) -> None:
    """The story's own acceptance criterion, literally."""
    response = get(client)

    assert response.status_code == 428, response.text
    assert response.json()["error"]["code"] == "NO_SESSION"
    assert response.json()["error"]["retryable"] is False
    assert fetch.calls == []


def test_one_callers_session_is_not_another_callers(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """B has stored nothing, and A having a session does not lend B one."""
    store_session(vault, SUBJECT_A)

    response = get(client, SUBJECT_B)

    assert response.status_code == 428, response.text
    assert response.json()["error"]["code"] == "NO_SESSION"
    assert fetch.calls == []


def test_a_dead_session_aborts_the_whole_fetch_rather_than_degrading(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    store_session(vault, SUBJECT_A)
    fetch.raises = ApiError("SESSION_EXPIRED", log_detail="test")

    response = get(client)

    assert response.status_code == 428, response.text
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"
    # And the caller is told, on the session endpoint, why their next request
    # will fail too.
    assert vault.state(SUBJECT_A).last_use_ok is False


@pytest.mark.parametrize(
    "code,status",
    [
        ("SESSION_EXPIRED", 428),
        ("PROFILE_NOT_FOUND", 404),
        ("RATE_LIMITED", 429),
        ("UPSTREAM_CHALLENGE", 502),
        ("UPSTREAM_ERROR", 502),
    ],
)
def test_every_code_the_fetch_can_raise_wears_its_documented_status(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch, code: str, status: int
) -> None:
    """The five taxonomy codes reachable by raising from the fetch, not all eight.

    The name is scoped deliberately. ``response-schema.md``'s taxonomy has eight
    codes and three of them cannot be driven through this harness, because they
    are decided **before** any fetch happens and driving them from
    ``fetch.raises`` would assert a path that does not exist:

    * ``UNAUTHENTICATED`` — ``require_claims`` on the router rejects the request
      before the route body runs. ``tests/test_auth.py`` owns the whole matrix.
    * ``INVALID_URL`` — raised by URL parsing before a session is even loaded.
    * ``NO_SESSION`` — raised by the route itself when the vault is empty, which
      is exactly what ``test_no_stored_session_is_428_before_any_upstream_call``
      asserts, including that no upstream call is made.

    ``SESSION_EXPIRED`` **is** reachable here and belongs in the list;
    ``test_a_dead_session_aborts_the_whole_fetch_rather_than_degrading`` covers
    the same code with the extra ``last_use_ok`` bookkeeping assertion.
    """
    store_session(vault, SUBJECT_A)
    fetch.raises = ApiError(code, log_detail="test")

    response = get(client)

    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize("code", ["RATE_LIMITED", "UPSTREAM_CHALLENGE", "UPSTREAM_ERROR"])
def test_a_failure_that_says_nothing_about_the_cookie_does_not_libel_it(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch, code: str
) -> None:
    """A throttle is a fact about LinkedIn, not about the caller's session."""
    store_session(vault, SUBJECT_A)
    fetch.raises = ApiError(code, log_detail="test")

    get(client)

    assert vault.state(SUBJECT_A).last_use_ok is None


def test_a_rate_limit_propagates_retry_after(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    store_session(vault, SUBJECT_A)
    fetch.raises = ApiError("RATE_LIMITED", headers={"Retry-After": "120"})

    response = get(client)

    assert response.status_code == 429
    assert response.headers["retry-after"] == "120"
    assert response.json()["error"]["retryable"] is True


def test_an_unexpected_exception_in_the_fetch_is_typed_and_never_naked(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CAP-6: no unhandled exception reaches the client, ever.

    It used to become a 500, which also meant the cache was never consulted —
    CAP-5 defeated in one of the exact situations it exists for, since "the
    transport blew up in a way nobody predicted" is precisely when a caller most
    wants the last good record. It is now a typed 502 raised INSIDE the
    stale-serve boundary. CAP-6 is satisfied either way; what changed is that a
    cached record can now answer.

    The bug is not softened for *us*: the traceback still reaches the log.
    """
    store_session(vault, SUBJECT_A)
    fetch.raises = RuntimeError("a bug nobody predicted")

    with caplog.at_level(logging.ERROR, logger="app.api.v1.profile"):
        response = get(client)

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "UPSTREAM_ERROR"
    assert "a bug nobody predicted" not in response.text
    assert "RuntimeError" in caplog.text
    assert "a bug nobody predicted" in caplog.text, "the real cause must be logged"


def test_an_unexpected_exception_can_still_be_answered_from_the_cache(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """The point of the change above: CAP-5 covers the failures nobody predicted."""
    store_session(vault, SUBJECT_A)
    fresh = get(client).json()
    fetch.raises = RuntimeError("a bug nobody predicted")

    response = get(client)

    assert response.status_code == 200, response.text
    assert response.json() == {**fresh, "stale": True}


def test_an_unexpected_exception_leaves_the_session_verdict_alone(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """A bug here is not evidence that the caller's cookie stopped working."""
    store_session(vault, SUBJECT_A)
    fetch.raises = RuntimeError("boom")

    get(client)

    assert vault.state(SUBJECT_A).last_use_ok is None


def test_a_missing_url_parameter_wears_the_typed_envelope(
    client: TestClient, vault: SessionVault
) -> None:
    store_session(vault, SUBJECT_A)

    response = client.get(PROFILE_PATH, headers=token_for(SUBJECT_A))

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


# --- Story 7: stale-serve, through the whole route ----------------------------
#
# `tests/test_cache.py` proves the rule — retryable and a record exists, and
# nothing else. What can only be proved here is that the route *applies* it, and
# where: after the session checks, before the mapper, and on the timeout path
# too. Deleting the fallback entirely leaves every test in that file green.


def test_a_successful_fetch_is_written_to_the_cache(
    client: TestClient, vault: SessionVault, cache_store: InMemoryProfileCacheStore
) -> None:
    store_session(vault, SUBJECT_A)

    body = get(client).json()

    assert PUBLIC_ID in cache_store.rows
    assert json.loads(cache_store.rows[PUBLIC_ID].body) == body
    assert cache_store.rows[PUBLIC_ID].fetched_at == FETCHED_AT


@pytest.mark.parametrize("code", ["RATE_LIMITED", "UPSTREAM_CHALLENGE", "UPSTREAM_ERROR"])
def test_a_retryable_failure_after_a_good_fetch_serves_the_record(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch, code: str
) -> None:
    """CAP-5, and the story's first acceptance criterion.

    With retrieval forced to fail, a previously fetched profile still returns
    200 with its full payload, `stale: true`, and the ORIGINAL fetch timestamp.
    """
    store_session(vault, SUBJECT_A)
    fresh = get(client).json()
    fetch.raises = ApiError(code, log_detail="test")

    response = get(client)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stale"] is True
    assert body["fetched_at"] == fresh["fetched_at"] == "2026-08-27T09:00:00Z"
    assert body == {**fresh, "stale": True}


def test_a_stale_response_reports_the_older_fetch_time_not_this_request(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """`fetched_at` is what makes staleness actionable; re-stamping it is the one
    change that would make an unbounded cache dishonest."""
    long_ago = datetime.now(timezone.utc) - timedelta(days=400)
    fetch.result = raw_profile(fetched_at=long_ago)
    store_session(vault, SUBJECT_A)
    get(client)
    fetch.raises = ApiError("RATE_LIMITED", log_detail="test")

    body = get(client).json()

    assert body["stale"] is True
    assert body["fetched_at"] == long_ago.isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def test_a_stale_response_carries_the_contracts_top_level_keys(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """A caller must not have to branch on whether an answer was live."""
    store_session(vault, SUBJECT_A)
    get(client)
    fetch.raises = ApiError("UPSTREAM_ERROR", log_detail="test")

    body = get(client).json()

    assert set(body) == {"url", "public_id", "stale", "fetched_at", "partial", "profile"}


def test_a_stale_record_keeps_the_partial_it_was_stored_with(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """Served verbatim: the omitted key stays omitted and `partial` is not re-decided."""
    store_session(vault, SUBJECT_A)
    fetch.result = raw_profile(
        overrides={"certifications": failed_section("certifications")}
    )
    get(client)
    fetch.raises = ApiError("RATE_LIMITED", log_detail="test")

    body = get(client).json()

    assert body["stale"] is True
    assert "certifications" in body["partial"]
    assert "certifications" not in body["profile"]


def test_a_live_success_replaces_the_record_and_is_never_stale(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """Live always wins when it works — the cache is a fallback, not a shortcut."""
    store_session(vault, SUBJECT_A)
    get(client)
    fetch.result = raw_profile(core_fixture="voyager_sparse_core.json", sections=SPARSE_SECTIONS)

    body = get(client).json()

    assert body["stale"] is False
    assert body["partial"] == [], "the replacement record is the new one, not the old"
    assert fetch.calls, "a cached record must not suppress the live call"


def test_a_retryable_failure_with_nothing_cached_is_still_the_typed_error(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    store_session(vault, SUBJECT_A)
    fetch.raises = ApiError("RATE_LIMITED", log_detail="test")

    response = get(client)

    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "RATE_LIMITED"


def test_an_expired_session_is_428_even_with_a_record_cached(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """The story's second acceptance criterion, and the reason this is safe.

    Hiding a dead credential behind cached data would report success forever
    about a session that stopped working.
    """
    store_session(vault, SUBJECT_A)
    get(client)
    fetch.raises = ApiError("SESSION_EXPIRED", log_detail="test")

    response = get(client)

    assert response.status_code == 428, response.text
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"
    assert "Ada Placeholder" not in response.text


def test_a_caller_with_no_session_never_reaches_the_cache(
    client: TestClient,
    vault: SessionVault,
    fetch: RecordingFetch,
    cache_store: InMemoryProfileCacheStore,
) -> None:
    """The cache is keyed by profile and shared, so this is what keeps it safe.

    Not merely "B is refused": the record is never even looked up, which is what
    makes "no working credential, no cached data" structural rather than a rule
    someone has to remember.
    """
    store_session(vault, SUBJECT_A)
    get(client)
    cache_store.loads.clear()

    response = get(client, SUBJECT_B)

    assert response.status_code == 428, response.text
    assert response.json()["error"]["code"] == "NO_SESSION"
    assert cache_store.loads == []
    assert "Ada Placeholder" not in response.text


def test_a_second_caller_with_their_own_session_is_served_the_shared_record(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """The cache holds public profile data, so it is global rather than per-caller.

    Safe only because of the test above: the session check precedes the lookup.
    """
    vault.store(SUBJECT_A, COOKIE)
    vault.store(SUBJECT_B, OTHER_COOKIE)
    get(client, SUBJECT_A)
    fetch.raises = ApiError("UPSTREAM_CHALLENGE", log_detail="test")

    response = get(client, SUBJECT_B)

    assert response.status_code == 200, response.text
    assert response.json()["stale"] is True


@pytest.mark.parametrize(
    "code,status", [("PROFILE_NOT_FOUND", 404), ("SESSION_EXPIRED", 428)]
)
def test_a_permanent_failure_is_never_masked_by_a_cached_record(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch, code: str, status: int
) -> None:
    """A deleted profile is not stale data, and a dead session is not a 200."""
    store_session(vault, SUBJECT_A)
    get(client)
    fetch.raises = ApiError(code, log_detail="test")

    response = get(client)

    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code


def test_a_bad_url_is_still_a_400_after_that_profile_was_cached(
    client: TestClient, vault: SessionVault
) -> None:
    """`INVALID_URL` is decided before anything else and has no cache key anyway."""
    store_session(vault, SUBJECT_A)
    get(client)

    response = get(client, url="https://example.invalid/in/ada-placeholder")

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "INVALID_URL"


def test_a_wedged_fetch_falls_back_to_the_record_too(
    client: TestClient,
    vault: SessionVault,
    fetch: RecordingFetch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline raises its `UPSTREAM_ERROR` on its own path, inside the boundary."""
    store_session(vault, SUBJECT_A)
    get(client)
    monkeypatch.setattr(profile_routes, "PROFILE_FETCH_DEADLINE_SECONDS", 0.05)
    fetch.delay = 5.0

    response = get(client)

    assert response.status_code == 200, response.text
    assert response.json()["stale"] is True


def test_a_cache_write_failure_never_costs_the_caller_the_live_answer(
    client: TestClient, vault: SessionVault, cache_store: InMemoryProfileCacheStore
) -> None:
    """The story's fifth acceptance criterion. The profile was retrieved; it is owed."""
    store_session(vault, SUBJECT_A)
    cache_store.fail_writes = True

    response = get(client)

    assert response.status_code == 200, response.text
    assert response.json()["stale"] is False
    assert response.json()["profile"]["name"]["full"] == "Ada Placeholder"


def test_a_cache_read_failure_leaves_the_upstream_error_as_it_was(
    client: TestClient,
    vault: SessionVault,
    fetch: RecordingFetch,
    cache_store: InMemoryProfileCacheStore,
) -> None:
    """A 503 about our own datastore would tell the caller less than the 429 did."""
    store_session(vault, SUBJECT_A)
    get(client)
    cache_store.fail_reads = True
    fetch.raises = ApiError("RATE_LIMITED", log_detail="test")

    response = get(client)

    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "RATE_LIMITED"


def test_a_stale_response_is_no_more_cacheable_than_a_live_one(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """Returning a Response object drops the dependency's header; this path builds its own.

    Asserting the header ALONE was hollow: it is equally true of the 429 this
    route would return with the fallback deleted, so the test passed whether or
    not the thing it is named after happened. The status and the flag are what
    make it a test of the stale path.
    """
    store_session(vault, SUBJECT_A)
    get(client)
    fetch.raises = ApiError("RATE_LIMITED", log_detail="test")

    response = get(client)

    assert response.status_code == 200, response.text
    assert response.json()["stale"] is True
    assert response.headers["cache-control"] == "no-store"


# --- The identity guard: a decision, and it is 502 ----------------------------


def test_a_refusal_to_publish_a_different_member_is_not_cached(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch,
    cache_store: InMemoryProfileCacheStore,
) -> None:
    """The guard runs before the write, so a substituted profile never gets in."""
    store_session(vault, SUBJECT_A)
    fetch.result = raw_profile(public_id="someone-else")

    response = get(client)

    assert response.status_code == 502, response.text
    assert cache_store.rows == {}


def test_a_different_member_is_still_a_502_when_a_record_exists(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """The decision this story owed, pinned in the case that actually decides it.

    The guard raises `UPSTREAM_ERROR`, which `ERROR_SPECS` marks retryable, so
    inside the stale-serve boundary this would be a 200. It is deliberately
    outside it: a response naming a different member is a PERMANENT condition —
    a vanity URL that now belongs to someone else — and under a cache with no
    expiry, softening it would republish the old identity mapping for ever
    without ever telling the caller their URL has stopped meaning what they
    think.

    The existing test above runs with an EMPTY cache and so passes whatever this
    case does. That is the gap; this is the test that closes it.
    """
    store_session(vault, SUBJECT_A)
    get(client)  # a good record now exists for the requested id
    fetch.result = raw_profile(public_id="someone-else")

    response = get(client)

    assert response.status_code == 502, response.text
    assert response.json()["error"]["code"] == "UPSTREAM_ERROR"
    assert "Ada Placeholder" not in response.text, "no stale body may leak out here"


def test_a_different_member_is_reported_as_a_permanent_failure(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """`retryable: false` on the wire, which is what a client actually branches on.

    A deliberate deviation from `response-schema.md`, made with the author: the
    table marks `UPSTREAM_ERROR` retryable and this one is not, because the
    condition is permanent and a retry cannot change it. The published table has
    one row per code; the wire has one flag per response, and the flag is
    authoritative.
    """
    store_session(vault, SUBJECT_A)
    fetch.result = raw_profile(public_id="someone-else")

    body = get(client).json()

    assert body["error"]["code"] == "UPSTREAM_ERROR"
    assert body["error"]["retryable"] is False
    assert set(body["error"]) == {"code", "message", "retryable"}


def test_the_mismatch_is_refused_by_the_taxonomy_and_not_only_by_its_placement(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """The client-side guard's refusal, which arrives from INSIDE the boundary.

    Story 7 could only move the endpoint's own guard outside the stale-serve
    `try`. The client raises its twin during the fetch, so that one lands in the
    `except ApiError` and is offered to the cache like any other failure — and
    with a record present it used to come back as a 200. What refuses it now is
    the narrowed flag, and this is the row that shows it: same code, record in
    the cache, still a 502.

    `tests/test_linkedin_client.py::test_the_member_mismatch_refusal_is_not_retryable`
    pins that the real client raises exactly this; here it is the route's half.
    """
    store_session(vault, SUBJECT_A)
    get(client)  # a good record now exists for the requested id
    fetch.raises = ApiError(
        "UPSTREAM_ERROR",
        retryable=False,
        cause=CAUSE_MEMBER_MISMATCH,
        log_detail="core response named a different member",
    )

    response = get(client)

    assert response.status_code == 502, response.text
    assert response.json()["error"]["retryable"] is False
    assert "Ada Placeholder" not in response.text, "no stale body may leak out here"


def test_the_cause_is_operator_only_and_never_reaches_the_caller(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """`cause` names an internal classification branch. It joins `log_detail`."""
    store_session(vault, SUBJECT_A)
    fetch.raises = ApiError(
        "UPSTREAM_ERROR", cause=CAUSE_MALFORMED_BODY, log_detail="internal reason"
    )

    response = get(client)

    assert set(response.json()["error"]) == {"code", "message", "retryable"}
    assert CAUSE_MALFORMED_BODY not in response.text
    assert "internal reason" not in response.text


def test_a_different_member_still_records_that_the_session_worked(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    """LinkedIn answered under this cookie. The cookie is fine; the answer is not.

    This path used to skip `_record_outcome` entirely, so a request that reached
    LinkedIn and got an answer left `last_use_ok` untouched — the only exit from
    this handler that recorded nothing, and it did so silently.
    """
    store_session(vault, SUBJECT_A)
    fetch.result = raw_profile(public_id="someone-else")

    get(client)

    assert vault.state(SUBJECT_A).last_use_ok is True


# --- The placement of the boundary, pinned structurally -----------------------


def _handler_tree() -> ast.FunctionDef:
    source = textwrap.dedent(inspect.getsource(profile_routes.get_profile))
    # The route decorator is part of the source and references names this module
    # does not have; parsing is fine, it is only evaluation that would not be.
    tree = ast.parse(source)
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_profile"
    )


def _calls_within(node: ast.AST) -> set[str]:
    """Every `receiver.attribute` named under `node`.

    References rather than calls, deliberately: the blocking vault and cache
    operations are *handed* to `asyncio.to_thread` rather than called directly,
    so looking only for `ast.Call` would find neither and report every placement
    as safe.
    """
    return {
        f"{child.value.id}.{child.attr}"
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name)
    }


def _stale_serve_try() -> ast.Try:
    """The `try` whose handler consults the cache."""
    return next(
        node
        for node in ast.walk(_handler_tree())
        if isinstance(node, ast.Try)
        and any("_cache_fallback" in ast.dump(handler) for handler in node.handlers)
    )


def test_the_session_check_is_outside_the_stale_serve_boundary() -> None:
    """The safety property the module docstring claims, made falsifiable.

    The cache is keyed by public id and shared across callers, so it is safe
    only because a caller with no session — or a dead one — is refused BEFORE it
    is consulted. That is meant to be structural.

    It cannot be tested behaviourally: moving `vault.unlock` inside the `try`
    changes no observable output, because every code it raises is non-retryable
    and `fallback_for` short-circuits on those anyway. A reviewer made exactly
    that move and 816 tests stayed green. So either the property is pinned
    against the handler's own syntax, or the second layer is a claim with
    nothing behind it and should stop being made.
    """
    boundary = _stale_serve_try()

    inside = set()
    for statement in boundary.body:
        inside |= _calls_within(statement)

    assert "vault.unlock" not in inside, (
        "vault.unlock has moved inside the stale-serve boundary. The cache is "
        "shared across callers and must not be reachable before the session "
        "check; nothing about the response would change, which is why this is "
        "asserted structurally."
    )
    # And it really is called, somewhere — otherwise the assertion above is
    # satisfied by the session check having been deleted altogether.
    assert "vault.unlock" in _calls_within(_handler_tree())


def test_the_identity_guard_is_outside_the_stale_serve_boundary() -> None:
    """The other half of the placement decision, pinned the same way.

    `raw.public_id != requested_id` raises a *retryable* code for a permanent
    condition, so moving it inside the boundary silently converts a 502 into a
    stale 200. `test_a_different_member_is_still_a_502_when_a_record_exists`
    catches that behaviourally; this says which structure produces it.
    """
    boundary = _stale_serve_try()
    handler = _handler_tree()

    def compares_public_id(node: ast.AST) -> bool:
        return any(
            isinstance(child, ast.Compare)
            and "requested_id" in ast.dump(child)
            and "public_id" in ast.dump(child)
            for child in ast.walk(node)
        )

    assert any(compares_public_id(node) for node in handler.body), (
        "the identity guard is not in the handler's top-level body"
    )
    assert not any(compares_public_id(node) for node in boundary.body), (
        "the identity guard has moved inside the stale-serve boundary, which "
        "turns a permanent member-substitution into an indefinite stale 200"
    )


def test_the_default_cache_dependency_is_the_process_wide_one() -> None:
    """Otherwise every test above proves something the endpoint does not use."""
    from app.cache import cache as process_cache

    assert profile_routes.get_profile_cache() is process_cache


# --- B3: error responses are uncacheable too ----------------------------------


@pytest.mark.parametrize(
    "case",
    ["no_session", "bad_url", "no_token", "not_found", "rate_limited", "missing_param",
     "unknown_path", "server_error"],
)
def test_every_error_response_is_uncacheable(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch, case: str
) -> None:
    """These bodies are caller-specific and this service sits behind three caches.

    A `428 NO_SESSION` cached and replayed to a second caller tells somebody
    with a perfectly good session to go and store one; a cached `401` locks out
    a valid token for the life of the entry. The success path set the header and
    the error paths did not.
    """
    if case == "no_session":
        response = get(client)
    elif case == "bad_url":
        store_session(vault, SUBJECT_A)
        response = get(client, url="https://example.invalid/in/ada")
    elif case == "no_token":
        response = client.get(PROFILE_PATH, params={"url": PROFILE_URL})
    elif case == "missing_param":
        store_session(vault, SUBJECT_A)
        response = client.get(PROFILE_PATH, headers=token_for(SUBJECT_A))
    elif case == "unknown_path":
        response = client.get("/api/v1/nothing-here", headers=token_for(SUBJECT_A))
    elif case == "server_error":
        store_session(vault, SUBJECT_A)
        fetch.raises = RuntimeError("boom")
        response = get(client)
    else:
        store_session(vault, SUBJECT_A)
        fetch.raises = ApiError(
            "PROFILE_NOT_FOUND" if case == "not_found" else "RATE_LIMITED"
        )
        response = get(client)

    assert response.status_code >= 400, response.text
    assert response.headers.get("cache-control") == "no-store", response.status_code


def test_a_401_keeps_its_www_authenticate_header_alongside_no_store(
    client: TestClient,
) -> None:
    """Adding one header must not drop the one RFC 6750 requires."""
    response = client.get(PROFILE_PATH, params={"url": PROFILE_URL})

    assert response.headers["www-authenticate"].startswith("Bearer")
    assert response.headers["cache-control"] == "no-store"


# --- The authentication boundary ---------------------------------------------


def test_no_token_is_401_and_reaches_nothing(
    client: TestClient, vault: SessionVault, fetch: RecordingFetch
) -> None:
    store_session(vault, SUBJECT_A)

    response = client.get(PROFILE_PATH, params={"url": PROFILE_URL})

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert fetch.calls == []


def test_the_handler_takes_the_verified_subject_from_the_token() -> None:
    """Not the auth boundary — that is `app/api/v1/__init__.py`'s router
    dependency, and `tests/test_auth.py` is what proves deleting it fails.

    This asserts the narrower thing its name now says: the handler reads the
    caller's identity from `require_claims`, so the vault key can only ever be
    a verified `sub`. An earlier version of this test was named as though it
    checked the boundary and stayed green when the boundary was deleted.
    """
    route = next(
        route
        for route in profile_routes.router.routes
        if getattr(route, "path", None) == "/profile"
    )

    names = {
        dependency.call.__name__
        for dependency in route.dependant.dependencies
        if dependency.call is not None
    }
    assert "require_claims" in names


def test_the_profile_router_declares_no_security_dependency_of_its_own() -> None:
    """Authentication is inherited from `/api/v1`, which is the whole point.

    A router that declared its own would be a router someone could later mount
    somewhere unprotected without noticing.
    """
    names = {
        dependency.dependency.__name__  # type: ignore[union-attr]
        for dependency in profile_routes.router.dependencies
        if dependency.dependency is not None
    }
    assert "require_claims" not in names
    assert names == {"no_store"}


# --- C4: the OpenAPI success schema is the README's API documentation ---------


def _resolve(document: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Follow one `$ref` into `components.schemas`."""
    ref = schema.get("$ref")
    if ref is None:
        return schema
    name = ref.rsplit("/", 1)[-1]
    return document["components"]["schemas"][name]


def test_the_documented_200_body_matches_the_body_the_service_actually_sends(
    client: TestClient, vault: SessionVault
) -> None:
    """`/docs` is the API documentation CAP-3 and CAP-8 are graded on.

    Renaming `public_id` to `publicId` in the model, or deleting `partial` from
    it, left the whole suite green while `/docs` described a body the service
    never sends — missing the one field the entire contract rests on.
    """
    store_session(vault, SUBJECT_A)
    body = get(client).json()
    document = client.get("/openapi.json").json()

    operation = document["paths"][PROFILE_PATH]["get"]
    schema = _resolve(
        document, operation["responses"]["200"]["content"]["application/json"]["schema"]
    )

    assert set(schema["properties"]) == set(body)
    assert "partial" in schema["properties"]


def test_the_documented_profile_object_names_every_contract_field(
    client: TestClient,
) -> None:
    document = client.get("/openapi.json").json()

    envelope = _resolve(
        document,
        document["paths"][PROFILE_PATH]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"],
    )
    profile = _resolve(document, envelope["properties"]["profile"])

    assert set(profile["properties"]) == set(CONTRACT_FIELDS)


def test_the_route_documents_every_status_it_can_answer(client: TestClient) -> None:
    document = client.get("/openapi.json").json()

    documented = set(document["paths"][PROFILE_PATH]["get"]["responses"])

    assert {"200", "400", "401", "404", "422", "428", "429", "502", "503"} <= documented


def test_the_documented_url_parameter_is_required(client: TestClient) -> None:
    """`/docs` is what an evaluator reads; an optional-looking `url` misleads."""
    document = client.get("/openapi.json").json()

    parameters = document["paths"][PROFILE_PATH]["get"]["parameters"]
    url_parameter = next(p for p in parameters if p["name"] == "url")
    assert url_parameter["required"] is True


def test_no_openapi_example_anywhere_looks_like_a_cookie(client: TestClient) -> None:
    document = client.get("/openapi.json").text

    assert "li_at=" not in document
    assert COOKIE not in document
