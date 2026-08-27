"""The story-4 edge-case matrix, as tests. No network, ever.

| Scenario           | Expected                                                  |
|--------------------|-----------------------------------------------------------|
| Known profile      | raw JSON for the core entity plus all five sections        |
| Empty section      | preserved as an empty *result*, distinct from a failure    |
| Malformed URL      | `INVALID_URL` / 400, before any network call               |
| Expired cookie     | `SESSION_EXPIRED` / 428, distinct from a missing cookie    |
| Unknown profile    | `PROFILE_NOT_FOUND` / 404, not an auth failure             |
| Throttled          | `RATE_LIMITED` / 429, retryable, `Retry-After` propagated  |
| Challenge          | `UPSTREAM_CHALLENGE` / 502, by content type, not status    |
| Endpoint withdrawn | `UPSTREAM_ERROR` / 502, logged loudly, never "empty"       |

**About the fixtures.** Everything in `tests/fixtures/` is synthetic —
hand-written to mirror entity shapes measured against the live API on
2026-08-27, and populated with an invented person ("Ada Placeholder") on
`.invalid` domains. The real captured payloads were kept deliberately outside
this repository. The repository is public and a captured payload is a real
person's personal data, so no fixture is derived from one by copying;
`test_no_fixture_carries_a_secret_or_a_real_person` enforces that rather than
trusting it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from app.errors import ERROR_SPECS, ApiError
from app.linkedin import client as voyager
from app.linkedin.client import (
    CORE_RESOURCE,
    SECTION_RESOURCES,
    LinkedInSession,
    RawProfile,
    TransportError,
    VoyagerClient,
    VoyagerResponse,
    parse_profile_url,
    resolve_elements,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: A cookie value that exists to be hunted for. Distinctive enough that a
#: substring search for it cannot match anything incidental, so every "the
#: cookie never appears in X" assertion in this file is a real assertion.
SENTINEL_COOKIE = "synthetic-session-value-DO-NOT-LOG-4f2a9c11"

PROFILE_URL = "https://www.linkedin.com/in/ada-placeholder"
PUBLIC_ID = "ada-placeholder"
PROFILE_URN = "urn:li:fsd_profile:SYNTHETIC-ada-placeholder"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


#: Which fixture answers which resource on the happy path.
SECTION_FIXTURES = {
    "experience": "voyager_experience.json",
    "education": "voyager_education.json",
    "skills": "voyager_skills.json",
    "certifications": "voyager_certifications.json",
    "languages": "voyager_languages.json",
}


# --- A transport that cannot reach a network ---------------------------------


@dataclass(frozen=True)
class Recorded:
    url: str
    headers: dict[str, str]
    timeout: float


def json_response(
    payload: Any, *, status: int = 200, url: str = "https://www.linkedin.com/voyager/api/x"
) -> VoyagerResponse:
    return VoyagerResponse(
        status=status,
        url=url,
        headers={"content-type": "application/vnd.linkedin.normalized+json+2.1"},
        body=json.dumps(payload).encode("utf-8"),
    )


def html_response(
    *, status: int = 200, url: str = "https://www.linkedin.com/authwall"
) -> VoyagerResponse:
    return VoyagerResponse(
        status=status,
        url=url,
        headers={"content-type": "text/html; charset=utf-8"},
        body=(FIXTURES / "linkedin_authwall.html").read_bytes(),
    )


def status_response(
    status: int, *, headers: dict[str, str] | None = None, body: bytes = b"{}"
) -> VoyagerResponse:
    merged = {"content-type": "application/json"}
    merged.update(headers or {})
    return VoyagerResponse(
        status=status,
        url="https://www.linkedin.com/voyager/api/x",
        headers=merged,
        body=body,
    )


class FakeTransport:
    """Routes by path fragment. Records every request it is handed.

    Nothing in this suite can reach LinkedIn: the client takes its transport as
    an argument, and every test installs this one. The recording is what makes
    the "six calls, these six URLs, these headers, and the cookie in no other
    field" assertions possible.
    """

    def __init__(self, routes: list[tuple[str, Any]]) -> None:
        self.routes = routes
        self.calls: list[Recorded] = []

    def __call__(
        self, url: str, headers: dict[str, str], timeout: float
    ) -> VoyagerResponse:
        self.calls.append(Recorded(url=url, headers=dict(headers), timeout=timeout))
        for marker, outcome in self.routes:
            if marker in url:
                if isinstance(outcome, Exception):
                    raise outcome
                if callable(outcome):
                    return outcome(url, headers)
                return outcome
        raise AssertionError(f"test transport has no route for {url!r}")

    @property
    def urls(self) -> list[str]:
        return [call.url for call in self.calls]


def happy_routes() -> list[tuple[str, Any]]:
    """The full six-call happy path. Longest markers first: `identity/dash/profiles`
    is a prefix of nothing else, but `me` would match half the section paths, so
    routing is by the distinctive resource fragment in every case."""
    routes: list[tuple[str, Any]] = []
    for name, resource in SECTION_RESOURCES.items():
        routes.append((resource, json_response(load_fixture(SECTION_FIXTURES[name]))))
    routes.append((CORE_RESOURCE + "?", json_response(load_fixture("voyager_core.json"))))
    routes.append(("/api/me", json_response(load_fixture("voyager_me.json"))))
    return routes


def override(name: str, outcome: Any) -> list[tuple[str, Any]]:
    """Happy path with one resource's answer replaced."""
    resource = CORE_RESOURCE + "?" if name == "core" else SECTION_RESOURCES[name]
    routes = [(marker, out) for marker, out in happy_routes() if marker != resource]
    return [(resource, outcome), *routes]


def make_client(routes: list[tuple[str, Any]] | None = None, **kwargs: Any) -> VoyagerClient:
    transport = FakeTransport(routes if routes is not None else happy_routes())
    client = VoyagerClient(SENTINEL_COOKIE, transport=transport, **kwargs)
    # Attached for assertions; the client itself never reads it.
    client.transport = transport  # type: ignore[attr-defined]
    return client


def fetch(client: VoyagerClient, url: str = PROFILE_URL) -> RawProfile:
    return asyncio.run(client.fetch_profile(url))


def expect_api_error(callable_: Callable[[], Any]) -> ApiError:
    """Run and require an `ApiError` — never a raw exception (CAP-6)."""
    try:
        callable_()
    except ApiError as exc:
        return exc
    except Exception as exc:  # pragma: no cover - the failure this guards
        raise AssertionError(f"a raw {type(exc).__name__} escaped: {exc}") from exc
    raise AssertionError("expected an ApiError, but the call succeeded")


# --- Known profile: the happy path ------------------------------------------


def test_a_known_profile_returns_the_core_entity_and_all_five_sections() -> None:
    client = make_client()

    profile = fetch(client)

    assert profile.public_id == PUBLIC_ID
    assert profile.profile_urn == PROFILE_URN
    assert set(profile.sections) == set(SECTION_RESOURCES)
    assert all(section.ok for section in profile.sections.values())
    assert profile.failed_sections == []


def test_the_payloads_are_returned_raw_and_unmodified() -> None:
    """Retrieval only. Any reshaping toward `response-schema.md` is story 6's."""
    client = make_client()

    profile = fetch(client)

    assert profile.core == load_fixture("voyager_core.json")
    for name, fixture in SECTION_FIXTURES.items():
        assert profile.sections[name].payload == load_fixture(fixture), name
    # The core entity is selected out of `included`, not rebuilt from it.
    assert profile.profile == load_fixture("voyager_core.json")["included"][0]


def test_a_fetch_costs_exactly_six_live_calls() -> None:
    """One core plus five sections. The `me` check is deliberately not among them.

    The story puts "any change that increases the number of live calls per
    profile" behind Ask First. That boundary is only enforceable if the number
    is measured, which is what the counter on the choke point is for.
    """
    client = make_client()

    profile = fetch(client)

    assert profile.call_count == 6
    assert len(client.transport.calls) == 6  # type: ignore[attr-defined]


def test_the_reported_call_count_is_this_fetchs_and_not_the_clients_total() -> None:
    """Found by the live check, which is the only place the two ever differed.

    Story 5's flow is validate-then-fetch on one client, so the instance
    counter is already at 1 by the time a fetch starts. Reporting the
    cumulative total would make a six-call fetch read as seven and quietly
    break the only measurement the Ask-First boundary on call count rests on.
    """
    client = make_client()

    asyncio.run(client.check_session())
    profile = fetch(client)

    assert profile.call_count == 6
    assert client.call_count == 7


def test_the_six_requests_are_the_verified_endpoint_map() -> None:
    client = make_client()

    fetch(client)
    urls = client.transport.urls  # type: ignore[attr-defined]

    assert any(
        url.startswith(
            voyager.VOYAGER_BASE
            + "identity/dash/profiles?q=memberIdentity&memberIdentity=ada-placeholder"
        )
        for url in urls
    ), urls
    for resource in SECTION_RESOURCES.values():
        expected = f"{voyager.VOYAGER_BASE}{resource}?q=viewee&profileUrn="
        assert any(url.startswith(expected) for url in urls), resource


def test_every_section_asks_for_a_full_page() -> None:
    """The default page size is 20, and it silently truncates.

    Measured: the developer's own profile has 33 skills and the default request
    returned 20 of them with a 200 and no error. `count=100` returned all 33 in
    the same single call — so this is a correctness fix, not an optimisation,
    and it costs nothing against the per-profile call budget.
    """
    client = make_client()

    fetch(client)
    urls = client.transport.urls  # type: ignore[attr-defined]

    section_urls = [url for url in urls if "identity/dash/profiles?" not in url]
    assert len(section_urls) == len(SECTION_RESOURCES)
    for url in section_urls:
        assert f"count={voyager.SECTION_PAGE_SIZE}" in url, url
    assert voyager.SECTION_PAGE_SIZE > 20


def test_the_profile_urn_is_url_encoded_into_the_section_query() -> None:
    """`urn:li:fsd_profile:...` carries colons; an unencoded one is a 400."""
    client = make_client()

    fetch(client)

    section_urls = [
        url
        for url in client.transport.urls  # type: ignore[attr-defined]
        if "profilePositions" in url
    ]
    assert section_urls
    assert "urn%3Ali%3Afsd_profile%3A" in section_urls[0], section_urls[0]
    assert PROFILE_URN not in section_urls[0]


def test_the_dead_endpoints_are_never_requested() -> None:
    """410 Gone and 403, verified 2026-08-27.

    These are exactly what an implementation written from remembered API lore
    would call, so this test is aimed at a future edit rather than at today's
    code.
    """
    client = make_client()

    fetch(client)

    for url in client.transport.urls:  # type: ignore[attr-defined]
        assert "profileView" not in url, url
        assert "/voyager/api/graphql" not in url, url
        # `identity/profiles/{id}` is dead; `identity/dash/profiles` is not.
        assert "identity/profiles/" not in url, url


def test_the_request_headers_are_the_verified_shape() -> None:
    client = make_client()

    fetch(client)
    headers = client.transport.calls[0].headers  # type: ignore[attr-defined]

    assert headers["accept"] == "application/vnd.linkedin.normalized+json+2.1"
    assert headers["x-restli-protocol-version"] == "2.0.0"
    assert headers["user-agent"].startswith("Mozilla/5.0")
    assert "accept-encoding" not in headers, "urllib cannot decompress a gzip body"


def test_the_csrf_token_and_the_jsessionid_cookie_carry_the_same_value() -> None:
    """LinkedIn checks the header against the cookie, not against a real session.

    A mismatch answers 403, which is indistinguishable from an expired session
    from the outside — so this pairing is asserted rather than assumed.
    """
    client = make_client(jsessionid="ajax:1234567890123456789")

    fetch(client)
    headers = client.transport.calls[0].headers  # type: ignore[attr-defined]

    assert headers["csrf-token"] == "ajax:1234567890123456789"
    assert 'JSESSIONID="ajax:1234567890123456789"' in headers["cookie"]


def test_the_five_sections_are_fetched_concurrently() -> None:
    """Five sequential calls would be five round trips of latency, per profile.

    Asserted with a barrier rather than with timing: all five section requests
    must be in flight simultaneously for the barrier to release, so a
    sequential implementation deadlocks and fails on the timeout instead of
    being merely slower on a fast machine.
    """
    barrier = threading.Barrier(len(SECTION_RESOURCES))

    def gated(fixture: str) -> Callable[[str, dict[str, str]], VoyagerResponse]:
        def handler(url: str, headers: dict[str, str]) -> VoyagerResponse:
            barrier.wait(timeout=10)
            return json_response(load_fixture(fixture))

        return handler

    routes: list[tuple[str, Any]] = [
        (SECTION_RESOURCES[name], gated(fixture))
        for name, fixture in SECTION_FIXTURES.items()
    ]
    routes.append((CORE_RESOURCE + "?", json_response(load_fixture("voyager_core.json"))))

    profile = fetch(make_client(routes))

    assert all(section.ok for section in profile.sections.values())


def test_elements_order_survives_the_join() -> None:
    """`*elements` is authoritative for order; `included` is an unordered pool.

    `response-schema.md` requires experience "ordered most-recent first", and
    that ordering is LinkedIn's — it cannot be recovered by sorting on
    `dateRange` once a current role with no end date is in the list. The
    experience fixture stores `included` in reverse on purpose, so an
    implementation that reads `included` directly fails here.
    """
    payload = load_fixture("voyager_experience.json")

    resolved = resolve_elements(payload)

    assert [entity["entityUrn"] for entity in resolved] == payload["data"]["*elements"]
    assert [entity["title"] for entity in resolved] == [
        "Synthetic Fixture Engineer",
        "Placeholder Developer",
    ]
    assert resolved[0]["entityUrn"] != payload["included"][0]["entityUrn"]


def test_the_join_survives_a_malformed_envelope() -> None:
    """A shape change upstream must degrade, not raise into a 500."""
    assert resolve_elements({}) == []
    assert resolve_elements({"data": {}, "included": []}) == []
    assert resolve_elements({"data": {"*elements": "not-a-list"}, "included": []}) == []
    assert resolve_elements({"data": {"*elements": ["urn:missing"]}, "included": []}) == []


# --- Empty section: never conflated with a failure ---------------------------


def test_an_empty_section_is_a_success_with_zero_elements() -> None:
    """Measured: `profileLanguages` gave 0 elements, then 3, minutes apart.

    A zero-length section is therefore not evidence that the profile has none.
    The client records "the call succeeded" and "it returned nothing" as two
    separate facts so that story 6 can map the first to `[]` and a *failure* to
    `partial[]` — getting this wrong publishes a confident falsehood about a
    real person.
    """
    client = make_client(
        override("languages", json_response(load_fixture("voyager_empty_section.json")))
    )

    profile = fetch(client)
    languages = profile.sections["languages"]

    assert languages.ok is True
    assert languages.element_count == 0
    assert languages.payload is not None
    assert languages.error_code is None
    assert "languages" not in profile.failed_sections


def test_a_failed_section_records_no_element_count_at_all() -> None:
    """`None`, not 0 — the third state that keeps the two cases separable."""
    client = make_client(override("languages", status_response(500)))

    languages = fetch(client).sections["languages"]

    assert languages.ok is False
    assert languages.element_count is None
    assert languages.payload is None
    assert languages.error_code == "UPSTREAM_ERROR"


def test_a_truncated_section_is_visible_as_truncated() -> None:
    """Only the first page is fetched, and a 200 does not reveal that.

    A profile with more positions than the page size returns a short list with
    no error at all. Presenting that as "these are their roles" is the same
    class of falsehood as calling an unreadable section empty, so the shortfall
    is recorded rather than left for story 6 to fail to notice.
    """
    payload = load_fixture("voyager_experience.json")
    payload["data"]["paging"]["total"] = 9
    client = make_client(override("experience", json_response(payload)))

    profile = fetch(client)
    experience = profile.sections["experience"]

    assert experience.ok is True
    assert experience.element_count == 2
    assert experience.reported_total == 9
    assert profile.truncated_sections == ["experience"]


def test_a_complete_section_is_not_reported_as_truncated() -> None:
    profile = fetch(make_client())

    assert profile.truncated_sections == []
    assert profile.sections["experience"].reported_total == 2


def test_a_total_that_is_not_stated_is_none_not_zero() -> None:
    """The core collection omits `paging.total`; `None` means "not stated"."""
    assert voyager.reported_total(load_fixture("voyager_core.json")) is None
    assert voyager.reported_total({"data": {"paging": {"total": "9"}}}) is None
    assert voyager.reported_total({"data": {"paging": {"total": True}}}) is None
    assert voyager.reported_total(load_fixture("voyager_skills.json")) == 3


def test_element_counts_are_read_from_elements_not_from_included() -> None:
    client = make_client()

    profile = fetch(client)

    assert profile.sections["experience"].element_count == 2
    assert profile.sections["skills"].element_count == 3
    assert profile.sections["certifications"].element_count == 1


# --- Malformed URL -----------------------------------------------------------


VALID_URLS = [
    ("https://www.linkedin.com/in/ada-placeholder", "ada-placeholder"),
    ("https://www.linkedin.com/in/ada-placeholder/", "ada-placeholder"),
    ("http://linkedin.com/in/ada-placeholder", "ada-placeholder"),
    ("www.linkedin.com/in/ada-placeholder", "ada-placeholder"),
    ("linkedin.com/in/ada-placeholder", "ada-placeholder"),
    ("https://in.linkedin.com/in/ada-placeholder", "ada-placeholder"),
    ("https://uk.linkedin.com/in/ada-placeholder", "ada-placeholder"),
    ("https://www.linkedin.com/in/ada-placeholder?trk=public_profile", "ada-placeholder"),
    ("https://www.linkedin.com/in/ada-placeholder#experience", "ada-placeholder"),
    ("https://www.linkedin.com/in/ada-placeholder/details/experience/", "ada-placeholder"),
    ("https://www.linkedin.com/en/in/ada-placeholder", "ada-placeholder"),
    ("  https://www.linkedin.com/in/ada-placeholder  ", "ada-placeholder"),
    # Percent-encoded non-ASCII. Real public ids carry these, and an ASCII-only
    # allowlist would reject a legitimate profile as malformed.
    ("https://www.linkedin.com/in/%C3%A5da-pl%C3%A5ceholder", "åda-plåceholder"),
]


@pytest.mark.parametrize("url,expected", VALID_URLS)
def test_a_profile_url_yields_its_public_id(url: str, expected: str) -> None:
    assert parse_profile_url(url) == expected


INVALID_URLS = [
    ("", "empty string"),
    ("   ", "whitespace only"),
    ("not a url", "not a URL at all"),
    ("https://www.linkedin.com/company/example-robotics", "a company page"),
    ("https://www.linkedin.com/jobs/view/1234567890", "a job post"),
    ("https://www.linkedin.com/in/", "no public id"),
    ("https://www.linkedin.com/", "the site root"),
    ("https://example.invalid/in/ada-placeholder", "another host entirely"),
    # The SSRF shape: a host that merely *starts* with the expected one.
    ("https://linkedin.com.example.invalid/in/ada", "a lookalike host"),
    ("ftp://www.linkedin.com/in/ada-placeholder", "a non-http scheme"),
    ("javascript:alert(1)", "a javascript URL"),
    # `%2F` decodes to a path separator; checking before decoding would miss it.
    ("https://www.linkedin.com/in/ada%2F..%2Fadmin", "an encoded path separator"),
    ("https://www.linkedin.com/in/ada%00placeholder", "an encoded null byte"),
    ("https://www.linkedin.com/in/" + "a" * 101, "an absurdly long public id"),
]


@pytest.mark.parametrize("url,reason", INVALID_URLS)
def test_a_malformed_url_is_rejected_as_invalid_url(url: str, reason: str) -> None:
    error = expect_api_error(lambda: parse_profile_url(url))

    assert error.code == "INVALID_URL", reason
    assert error.spec.status_code == 400
    assert error.spec.retryable is False


@pytest.mark.parametrize("url,reason", INVALID_URLS)
def test_a_malformed_url_costs_zero_live_calls(url: str, reason: str) -> None:
    """"Rejected before any network call" is the acceptance criterion, so the
    absence of a request is what must be asserted — not merely the error code."""
    client = make_client()

    error = expect_api_error(lambda: fetch(client, url))

    assert error.code == "INVALID_URL", reason
    assert client.transport.calls == []  # type: ignore[attr-defined]


def test_the_rejected_url_is_not_echoed_into_the_response_body() -> None:
    """A caller-supplied string reflected into a response body is a stored-XSS
    primitive the moment anything renders it."""
    error = expect_api_error(
        lambda: parse_profile_url("https://evil.invalid/<script>alert(1)</script>")
    )

    assert "<script>" not in error.to_response().body.decode("utf-8")


# --- Missing versus expired session ------------------------------------------


def test_an_absent_cookie_is_no_session_not_session_expired() -> None:
    """The matrix requires these to be distinguishable, and the caller's remedy
    differs: supply a session, versus supply a *new* one."""
    for empty in ("", "   ", "\t\n"):
        error = expect_api_error(lambda: VoyagerClient(empty, transport=FakeTransport([])))
        assert error.code == "NO_SESSION"
        assert error.spec.status_code == 428

    assert ERROR_SPECS["NO_SESSION"] != ERROR_SPECS["SESSION_EXPIRED"]


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_cookie_is_session_expired(status: int) -> None:
    client = make_client(override("core", status_response(status)))

    error = expect_api_error(lambda: fetch(client))

    assert error.code == "SESSION_EXPIRED"
    assert error.spec.status_code == 428
    assert error.spec.retryable is False


def test_session_expiry_is_not_reported_as_a_profile_problem() -> None:
    """A dead cookie must never look like "that profile does not exist"."""
    client = make_client(override("core", status_response(401)))

    error = expect_api_error(lambda: fetch(client))

    assert error.code != "PROFILE_NOT_FOUND"
    assert error.code != "UNAUTHENTICATED"  # that code is the *bearer* token's


def test_check_session_returns_the_sessions_own_public_identifier() -> None:
    """How the live check finds the developer's own profile without guessing."""
    client = make_client()

    assert asyncio.run(client.check_session()) == PUBLIC_ID
    assert client.call_count == 1


def test_check_session_reports_a_200_that_names_nobody_as_expiry() -> None:
    client = make_client([("/api/me", json_response({"data": {}, "included": []}))])

    error = expect_api_error(lambda: asyncio.run(client.check_session()))

    assert error.code == "SESSION_EXPIRED"


# --- Unknown profile ----------------------------------------------------------


def test_a_core_response_with_zero_elements_is_profile_not_found() -> None:
    """LinkedIn answers a well-formed but unknown id with 200 and no elements.

    Treating that as an empty profile would be the worst available outcome: a
    confident, well-formed answer about a person who is not there.
    """
    empty_core = {
        "data": {"*elements": [], "paging": {"count": 10, "start": 0, "links": []}},
        "included": [],
    }
    client = make_client(override("core", json_response(empty_core)))

    error = expect_api_error(lambda: fetch(client))

    assert error.code == "PROFILE_NOT_FOUND"
    assert error.spec.status_code == 404
    assert error.spec.retryable is False


def test_a_core_response_with_no_profile_entity_is_profile_not_found() -> None:
    """Elements listed, but nothing in `included` resolves to a Profile."""
    orphaned = {
        "data": {"*elements": ["urn:li:fsd_profile:ghost"], "paging": {}},
        "included": [],
    }
    client = make_client(override("core", json_response(orphaned)))

    assert expect_api_error(lambda: fetch(client)).code == "PROFILE_NOT_FOUND"


def test_an_upstream_404_is_profile_not_found() -> None:
    client = make_client(override("core", status_response(404)))

    error = expect_api_error(lambda: fetch(client))

    assert error.code == "PROFILE_NOT_FOUND"


def test_a_missing_profile_is_never_reported_as_an_auth_failure() -> None:
    client = make_client(override("core", status_response(404)))

    error = expect_api_error(lambda: fetch(client))

    assert error.spec.status_code == 404
    assert error.code not in ("SESSION_EXPIRED", "NO_SESSION", "UNAUTHENTICATED")


# --- Throttling ---------------------------------------------------------------


def test_a_429_is_rate_limited_and_retryable() -> None:
    client = make_client(override("core", status_response(429)))

    error = expect_api_error(lambda: fetch(client))

    assert error.code == "RATE_LIMITED"
    assert error.spec.status_code == 429
    assert error.spec.retryable is True


def test_retry_after_is_propagated_when_upstream_sends_one() -> None:
    client = make_client(
        override("core", status_response(429, headers={"retry-after": "120"}))
    )

    error = expect_api_error(lambda: fetch(client))

    assert error.headers == {"Retry-After": "120"}
    assert error.to_response().headers["retry-after"] == "120"


@pytest.mark.parametrize(
    "value",
    [
        "soon",
        "Wed, 21 Oct 2026 07:28:00 GMT",  # legal HTTP-date; LinkedIn never sends it
        "12\r\nX-Injected: yes",  # header injection through a propagated value
        "999999999999999999",
        "-5",
    ],
)
def test_an_unparseable_retry_after_is_dropped_rather_than_propagated(
    value: str,
) -> None:
    """Echoing an unvalidated upstream header into our own response is how a
    header-injection bug is imported wholesale."""
    client = make_client(
        override("core", status_response(429, headers={"retry-after": value}))
    )

    error = expect_api_error(lambda: fetch(client))

    assert error.code == "RATE_LIMITED"
    assert error.headers is None
    assert "x-injected" not in {k.lower() for k in error.to_response().headers}


def test_a_throttled_section_degrades_and_records_its_code() -> None:
    """Story 6 needs to know *why* a section is missing, not merely that it is."""
    client = make_client(override("skills", status_response(429)))

    profile = fetch(client)

    assert profile.sections["skills"].ok is False
    assert profile.sections["skills"].error_code == "RATE_LIMITED"
    assert profile.failed_sections == ["skills"]


# --- Challenge ----------------------------------------------------------------


def test_an_html_body_with_a_200_is_a_challenge_not_a_success() -> None:
    """"Detected by content type, not by status alone" — this is that row.

    The authwall arrives at the end of a redirect chain urllib follows
    silently, so the status on the page it lands on is 200. Classifying on
    status alone reads this as a successful fetch.
    """
    client = make_client(
        override("core", html_response(status=200, url="https://www.linkedin.com/voyager/api/x"))
    )

    error = expect_api_error(lambda: fetch(client))

    assert error.code == "UPSTREAM_CHALLENGE"
    assert error.spec.status_code == 502
    assert error.spec.retryable is True


def test_a_redirect_to_the_authwall_is_a_challenge() -> None:
    client = make_client(
        override("core", html_response(url="https://www.linkedin.com/authwall?trk=x"))
    )

    assert expect_api_error(lambda: fetch(client)).code == "UPSTREAM_CHALLENGE"


@pytest.mark.parametrize(
    "final_url",
    [
        "https://www.linkedin.com/checkpoint/challenge/verify",
        "https://www.linkedin.com/uas/login?goback=x",
        "https://www.linkedin.com/login",
    ],
)
def test_any_wall_destination_is_a_challenge(final_url: str) -> None:
    """Destination alone, with no other signal to fall back on.

    The response here is a *200 carrying valid JSON* — every other check passes
    — so this fails unless the final URL after redirects is actually inspected.
    """
    client = make_client(
        override("core", json_response(load_fixture("voyager_core.json"), url=final_url))
    )

    assert expect_api_error(lambda: fetch(client)).code == "UPSTREAM_CHALLENGE"


def test_linkedins_own_999_status_is_a_challenge() -> None:
    """999 is not an HTTP status. It means "we think you are a bot"."""
    client = make_client(override("core", status_response(999)))

    assert expect_api_error(lambda: fetch(client)).code == "UPSTREAM_CHALLENGE"


def test_a_challenge_is_not_reported_as_a_missing_profile() -> None:
    client = make_client(override("core", html_response()))

    error = expect_api_error(lambda: fetch(client))

    assert error.code != "PROFILE_NOT_FOUND"
    assert error.spec.retryable is True, "story 7's stale-serve keys off this"


# --- Endpoint withdrawn --------------------------------------------------------


def test_a_410_on_the_core_is_an_upstream_error_not_an_empty_profile() -> None:
    """`identity/profiles/{id}/profileView` is already 410. The dash endpoints
    can follow, and the failure mode to prevent is a silently empty profile."""
    client = make_client(override("core", status_response(410)))

    error = expect_api_error(lambda: fetch(client))

    assert error.code == "UPSTREAM_ERROR"
    assert error.spec.status_code == 502
    assert error.spec.retryable is True


def test_a_410_is_logged_loudly_enough_to_notice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """WARNING would be lost in the noise of ordinary section failures.

    This is the one upstream change that silently invalidates the endpoint map
    the whole story is built on, so it logs at ERROR and names the file to fix.
    """
    client = make_client(override("skills", status_response(410)))

    with caplog.at_level(logging.DEBUG, logger="app.linkedin.client"):
        profile = fetch(client)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a withdrawn endpoint must not be logged below ERROR"
    assert "410" in errors[0].getMessage()
    assert "app/linkedin/client.py" in errors[0].getMessage()
    assert profile.sections["skills"].error_code == "UPSTREAM_ERROR"


# --- Degrade versus abort -------------------------------------------------------


def test_a_section_failure_does_not_abort_the_fetch() -> None:
    """`response-schema.md` has a place to say "this field was unreadable"
    (`partial[]`) and none to say "the request died because languages 500'd"."""
    client = make_client(override("languages", status_response(503)))

    profile = fetch(client)

    assert profile.failed_sections == ["languages"]
    assert all(
        profile.sections[name].ok for name in SECTION_RESOURCES if name != "languages"
    )
    assert profile.core == load_fixture("voyager_core.json")


def test_every_section_failing_still_returns_the_core_profile() -> None:
    routes = [(resource, status_response(500)) for resource in SECTION_RESOURCES.values()]
    routes.append((CORE_RESOURCE + "?", json_response(load_fixture("voyager_core.json"))))

    profile = fetch(make_client(routes))

    assert sorted(profile.failed_sections) == sorted(SECTION_RESOURCES)
    assert profile.profile["publicIdentifier"] == PUBLIC_ID


def test_a_core_failure_aborts_the_whole_fetch() -> None:
    client = make_client(override("core", status_response(500)))

    error = expect_api_error(lambda: fetch(client))

    assert error.code == "UPSTREAM_ERROR"
    # The core call is made alone, so nothing else was spent on a doomed fetch.
    assert len(client.transport.calls) == 1  # type: ignore[attr-defined]


def test_a_degraded_fetch_is_logged_with_the_section_names(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = make_client(override("education", status_response(500)))

    with caplog.at_level(logging.DEBUG, logger="app.linkedin.client"):
        fetch(client)

    assert any("education" in record.getMessage() for record in caplog.records)


# --- Transport and payload failures ---------------------------------------------


def test_a_transport_failure_is_an_upstream_error() -> None:
    client = make_client(override("core", TransportError("connection reset by peer")))

    error = expect_api_error(lambda: fetch(client))

    assert error.code == "UPSTREAM_ERROR"


def test_a_transport_raising_something_unexpected_is_still_typed() -> None:
    """CAP-6: no unhandled exception reaches the client, including from here."""
    client = make_client(override("core", RuntimeError("a bug nobody predicted")))

    error = expect_api_error(lambda: fetch(client))

    assert error.code == "UPSTREAM_ERROR"
    assert "a bug nobody predicted" not in error.to_response().body.decode("utf-8")


def test_a_body_that_claims_json_but_is_not_is_an_upstream_error() -> None:
    client = make_client(
        override(
            "core",
            VoyagerResponse(
                status=200,
                url="https://www.linkedin.com/voyager/api/x",
                headers={"content-type": "application/json"},
                body=b"{not json at all",
            ),
        )
    )

    assert expect_api_error(lambda: fetch(client)).code == "UPSTREAM_ERROR"


def test_a_json_array_where_an_object_belongs_is_an_upstream_error() -> None:
    client = make_client(override("core", json_response([1, 2, 3])))

    assert expect_api_error(lambda: fetch(client)).code == "UPSTREAM_ERROR"


# --- The cookie never leaks -------------------------------------------------------


def test_the_session_object_refuses_to_render_itself() -> None:
    session = LinkedInSession(SENTINEL_COOKIE)

    assert SENTINEL_COOKIE not in repr(session)
    assert SENTINEL_COOKIE not in str(session)
    assert SENTINEL_COOKIE not in f"{session}"
    assert SENTINEL_COOKIE not in "%s" % (session,)
    assert SENTINEL_COOKIE not in f"{session!r}"
    # And the value is still readable through the one greppable accessor.
    assert session.reveal() == SENTINEL_COOKIE


def test_the_cookie_reaches_the_cookie_header_and_no_other_field() -> None:
    client = make_client()

    fetch(client)

    for call in client.transport.calls:  # type: ignore[attr-defined]
        assert SENTINEL_COOKIE not in call.url
        assert call.headers["cookie"].startswith(f"li_at={SENTINEL_COOKIE};")
        leaked = {
            name: value
            for name, value in call.headers.items()
            if name != "cookie" and SENTINEL_COOKIE in value
        }
        assert not leaked, leaked


#: Every failure path, as (route override, description). Each is driven with the
#: sentinel cookie and inspected for a leak.
LEAK_PATHS = [
    ("core", status_response(401), "refused session"),
    ("core", status_response(404), "unknown profile"),
    ("core", status_response(429, headers={"retry-after": "30"}), "throttled"),
    ("core", status_response(410), "withdrawn endpoint"),
    ("core", status_response(500), "upstream error"),
    ("core", html_response(), "challenge"),
    ("core", TransportError(f"failed while sending {SENTINEL_COOKIE}"), "transport"),
    ("core", RuntimeError(f"boom with {SENTINEL_COOKIE}"), "unexpected transport bug"),
    ("languages", status_response(500), "degraded section"),
]


@pytest.mark.parametrize("resource,outcome,description", LEAK_PATHS)
def test_the_cookie_appears_in_no_log_no_error_and_no_response_body(
    caplog: pytest.LogCaptureFixture, resource: str, outcome: Any, description: str
) -> None:
    """The absolute constraint, exercised on every path that can fail.

    Two of the cases deliberately put the cookie *inside the exception message*
    the transport raises — which is exactly what a real HTTP client does when
    it renders a failed request — so the redaction is proved rather than
    assumed.
    """
    client = make_client(override(resource, outcome))

    with caplog.at_level(logging.DEBUG):
        try:
            profile = fetch(client)
        except ApiError as exc:
            assert SENTINEL_COOKIE not in str(exc), description
            assert SENTINEL_COOKIE not in repr(exc), description
            assert SENTINEL_COOKIE not in (exc.log_detail or ""), description
            assert SENTINEL_COOKIE not in exc.message, description
            body = exc.to_response().body.decode("utf-8")
            assert SENTINEL_COOKIE not in body, description
        else:
            assert SENTINEL_COOKIE not in repr(profile), description

    assert SENTINEL_COOKIE not in caplog.text, description


def test_the_raw_profile_does_not_carry_the_cookie_anywhere() -> None:
    """Story 7 caches this object. Anything on it reaches a datastore."""
    profile = fetch(make_client())

    assert SENTINEL_COOKIE not in repr(profile)
    assert SENTINEL_COOKIE not in json.dumps(profile.core)


def test_the_cookie_name_is_handled_only_inside_the_client() -> None:
    """Automates the story's `grep -rIn "li_at" app/` verification command.

    A second place that touches the cookie is a second place it can be logged,
    stored unencrypted, or echoed into an error body — and the audit that would
    catch it is a grep nobody runs twice.
    """
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in sorted((REPO_ROOT / "app").rglob("*.py"))
        if "li_at" in path.read_text(encoding="utf-8")
    ]

    assert offenders == ["app/linkedin/client.py"], offenders


def test_the_client_never_reads_configuration() -> None:
    """The session is an argument, not a setting.

    Story 5 constructs this client from the *caller's* stored cookie. A client
    that could reach `app.config` could fall back to the developer's own
    session and serve one user's request under another user's identity — which
    is precisely the CAP-4 violation that would be hardest to notice.
    """
    source = (REPO_ROOT / "app" / "linkedin" / "client.py").read_text(encoding="utf-8")

    assert "app.config" not in source
    assert "import settings" not in source


# --- The taxonomy agrees with response-schema.md ----------------------------------


#: Code -> (status, retryable), transcribed from `response-schema.md`.
SCHEMA_TABLE = {
    "INVALID_URL": (400, False),
    "UNAUTHENTICATED": (401, False),
    "NO_SESSION": (428, False),
    "SESSION_EXPIRED": (428, False),
    "PROFILE_NOT_FOUND": (404, False),
    "RATE_LIMITED": (429, True),
    "UPSTREAM_CHALLENGE": (502, True),
    "UPSTREAM_ERROR": (502, True),
}


def test_the_error_table_matches_the_response_schema_exactly() -> None:
    """A second transcription of the spec table, so drift fails a test.

    Written out by hand rather than derived from `ERROR_SPECS`, which would
    make it agree with the code by construction and assert nothing.
    """
    assert set(ERROR_SPECS) == set(SCHEMA_TABLE)
    for code, (status, retryable) in SCHEMA_TABLE.items():
        assert ERROR_SPECS[code].status_code == status, code
        assert ERROR_SPECS[code].retryable is retryable, code


def test_no_error_message_leaks_an_upstream_detail() -> None:
    """A Voyager error body can echo the request, and the request has the cookie."""
    for code, spec in ERROR_SPECS.items():
        assert spec.message, code
        assert "linkedin.com" not in spec.message.lower(), code


# --- Fixture safety ----------------------------------------------------------------


FIXTURE_FILES = sorted(p for p in FIXTURES.iterdir() if p.is_file())

#: The only person who appears in any fixture, and she does not exist.
SYNTHETIC_IDENTIFIERS = {"ada-placeholder", "Ada", "Placeholder"}

#: Substrings that would mean a real capture leaked into the repository.
FORBIDDEN_IN_FIXTURES = [
    "li_at",  # a cookie name, and therefore possibly a cookie value beside it
    "JSESSIONID",
    "AQED",  # every real li_at value observed starts with this
    "licdn.com",  # LinkedIn's real media CDN: signed URLs identify a real member
    "urn:li:member:9",  # the shape of a real member id; synthetics use 900000001
]


def test_fixtures_exist_and_are_parseable() -> None:
    """Guards every assertion below from passing vacuously on an empty directory."""
    json_fixtures = [p for p in FIXTURE_FILES if p.suffix == ".json"]

    assert len(json_fixtures) >= 8, [p.name for p in FIXTURE_FILES]
    for path in json_fixtures:
        json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_no_fixture_carries_a_secret_or_a_real_person(path: Path) -> None:
    """The repository is public and a captured payload is personal data.

    Real payloads were captured during development and kept deliberately
    outside this repository; every fixture here was written by hand to mirror
    their *shape*. This test is what keeps that true after the next edit.
    """
    text = path.read_text(encoding="utf-8")

    for forbidden in FORBIDDEN_IN_FIXTURES:
        # `urn:li:member:9` is the synthetic prefix too, so exempt the exact
        # synthetic id rather than weakening the check for everything.
        haystack = text.replace("urn:li:member:900000001", "")
        assert forbidden not in haystack, f"{path.name} contains {forbidden!r}"

    assert "https://www.linkedin.com/in/" not in text
    # Every host named by a fixture is unresolvable by construction.
    for marker in ("http://", "https://"):
        for chunk in text.split(marker)[1:]:
            host = chunk.split("/", 1)[0].strip('", ')
            assert host.endswith(".invalid"), f"{path.name} names host {host!r}"


def test_the_only_person_in_the_fixtures_is_invented() -> None:
    core = load_fixture("voyager_core.json")["included"][0]

    assert core["publicIdentifier"] in SYNTHETIC_IDENTIFIERS
    assert core["firstName"] in SYNTHETIC_IDENTIFIERS
    assert core["lastName"] in SYNTHETIC_IDENTIFIERS
    # Fields a real capture carries that no synthetic fixture has any reason to.
    for private in ("emailAddress", "phoneNumbers", "birthDateOn", "address"):
        assert private not in core, private


# --- The fixtures still mirror the measured shapes ---------------------------------


def test_the_core_fixture_carries_every_field_story_6_needs() -> None:
    """Measured on the dev profile, 2026-08-27. A fixture that drifts from this
    lets story 6 be written against a shape LinkedIn does not send."""
    profile = load_fixture("voyager_core.json")["included"][0]

    for field in (
        "firstName", "lastName", "headline", "summary", "publicIdentifier",
        "profilePicture", "backgroundPicture", "experienceCardUrn", "educationCardUrn",
    ):
        assert field in profile, field
    assert "geoUrn" in profile["geoLocation"]
    assert "countryCode" in profile["location"]
    # `location` is thin: a country code and a geo URN, no human-readable region.
    # `response-schema.md` wants `{country, region}`; resolving that URN is
    # story 6's problem, and this is where it discovers the problem exists.
    assert "region" not in profile["location"]


def test_experience_dates_carry_month_precision_and_education_does_not() -> None:
    """The precision falls out of the source exactly as the schema requires.

    `response-schema.md` fixes YYYY-MM for experience and YYYY for education.
    Widening either — into a timestamp, or a month invented for an education —
    is a claim about the source that the source does not make.
    """
    position = resolve_elements(load_fixture("voyager_experience.json"))[0]
    education = resolve_elements(load_fixture("voyager_education.json"))[0]

    assert set(position["dateRange"]["start"]) >= {"month", "year"}
    assert set(education["dateRange"]["start"]) & {"month"} == set()
    assert "year" in education["dateRange"]["start"]


def test_a_current_role_has_no_end_at_all() -> None:
    """Absent, not null — and `response-schema.md` maps it to `end: null`."""
    current = resolve_elements(load_fixture("voyager_experience.json"))[0]

    assert "end" not in current["dateRange"]


def test_a_certification_carries_a_start_and_no_end() -> None:
    """Measured: `Certification.dateRange` has `start` only. `response-schema.md`
    accordingly gives certifications an `issued` and no end date."""
    certification = resolve_elements(load_fixture("voyager_certifications.json"))[0]

    assert set(certification["dateRange"]["start"]) >= {"month", "year"}
    assert "end" not in certification["dateRange"]
    for field in ("name", "authority", "url", "licenseNumber"):
        assert field in certification, field


def test_skills_and_languages_carry_the_measured_fields() -> None:
    skill = resolve_elements(load_fixture("voyager_skills.json"))[0]
    language = resolve_elements(load_fixture("voyager_languages.json"))[0]

    assert set(skill) >= {"name", "entityUrn"}
    assert set(language) >= {"name", "proficiency", "entityUrn"}
