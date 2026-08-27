"""The story-7 stale-serve matrix, as tests.

| Scenario              | State                              | Expected                          |
|-----------------------|------------------------------------|-----------------------------------|
| First fetch           | nothing cached                     | nothing to fall back to           |
| Repeat fetch          | record exists, live works          | record replaced, live wins        |
| Throttled with cache  | `RATE_LIMITED`, record exists      | the record, `stale: true`         |
| Challenge with cache  | `UPSTREAM_CHALLENGE`, record exists| the record, `stale: true`         |
| Upstream error        | `UPSTREAM_ERROR`, record exists    | the record, `stale: true`         |
| Retryable, no record  | `RATE_LIMITED`, nothing cached     | the typed error, unchanged        |
| Expired session       | `SESSION_EXPIRED`, record exists   | **never** the record              |
| No session            | `NO_SESSION`, record exists        | **never** the record              |
| Not found             | `PROFILE_NOT_FOUND`, record exists | **never** the record              |
| Very old record       | record from long ago               | served, with its old `fetched_at` |
| Partial record        | stored body carried `partial[]`    | the same `partial[]`              |
| Cache write fails     | the datastore rejects the write    | no raise; the caller keeps its 200 |

Everything here runs against an in-memory store and needs no Postgres, no
network and no running stack — ``docker run --network none`` is one of the
story's verification commands.

===============================================================================
WHY THE NON-RETRYABLE HALF IS THE HALF THAT MATTERS
===============================================================================

The cheerful rows above are the feature. The rows that say **never** are the
reason the feature is safe, and they are asserted here against *every* code in
``ERROR_SPECS`` rather than against the three anyone thought to name — so a code
added or reclassified later cannot quietly join the served set. Serving a cached
profile to a caller whose LinkedIn session has died would report success,
forever, about a credential that stopped working; that is the exact failure this
project has already had to fix once.

===============================================================================
AND WHY THE SQL IS CHECKED STRUCTURALLY, IN THE DEFAULT SUITE
===============================================================================

The cache is the one thing in this codebase that can be **completely broken and
completely silent**. Five separate mutations each left the whole suite green and
would have shipped as "the cache never writes and never serves", with CAP-5 gone
and one log line as the only trace:

  (a) renaming ``fetched_at`` in the cache DDL only, so both statements name a
      column that does not exist;
  (b) ``body text`` → ``body jsonb``, the exact change the DDL comment says
      would break key-order round-tripping and reject a NUL escape;
  (c) ``timestamptz`` → ``timestamp``;
  (d) the load statement selecting FROM the *session* relation;
  (e) the save statement inserting INTO the *session* relation.

None of them is caught by asserting on SQL *strings*, and the tests that
actually execute the statements live in the opt-in ``test_postgres_live.py``,
which is skipped by default. The gap is structural: everywhere else a bad
statement surfaces to the caller as a typed 503, but ``remember`` and
``fallback_for`` swallow everything by design, so on this path nothing surfaces
at all.

*The schema-resolution section below closes it without a database.* It parses
the tables ``bootstrap`` actually creates and resolves every statement in
``db.CACHE_STATEMENTS`` against them — relation and column names both — so (a),
(d) and (e) fail here, in the default suite. (b) and (c) are caught by the
column-type assertions, which mirror ``test_the_ciphertext_column_is_bytea``
because both are documented as load-bearing for the same kind of reason.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app import cache as cache_module
from app import db
from app.cache import (
    DATASTORE_UNAVAILABLE,
    ENVELOPE_VERSION,
    NO_RECORD,
    NOT_RETRYABLE,
    REQUIRED_ENVELOPE_KEYS,
    SERVED,
    UNUSABLE_RECORD,
    ProfileCache,
)
from app.db import CacheRow
from app.errors import ERROR_SPECS, ApiError
from tests.support import (
    PROFILE_URL,
    PUBLIC_ID,
    InMemoryProfileCacheStore,
    RecordingConnection,
)

FETCHED_AT = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)

#: The three codes ``response-schema.md`` marks retryable. Written out by hand
#: rather than derived, so this file disagrees loudly with `ERROR_SPECS` if the
#: taxonomy changes — which is the moment somebody has to think about whether
#: the new answer is one that may be served from a cache.
RETRYABLE_CODES = {"RATE_LIMITED", "UPSTREAM_CHALLENGE", "UPSTREAM_ERROR"}

#: The codes that must reach the caller as themselves, whatever is cached.
NEVER_SERVED_CODES = sorted(set(ERROR_SPECS) - RETRYABLE_CODES)


def envelope(
    *,
    public_id: str = PUBLIC_ID,
    fetched_at: datetime = FETCHED_AT,
    partial: list[str] | None = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A response body in the shape `app/api/v1/profile.py` actually builds."""
    return {
        "url": f"https://www.linkedin.com/in/{public_id}",
        "public_id": public_id,
        "stale": False,
        "fetched_at": fetched_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "partial": partial if partial is not None else [],
        "profile": profile
        if profile is not None
        else {"name": {"full": "Ada Placeholder"}},
    }


@pytest.fixture(name="store")
def _store() -> InMemoryProfileCacheStore:
    return InMemoryProfileCacheStore()


@pytest.fixture(name="cache")
def _cache(store: InMemoryProfileCacheStore) -> ProfileCache:
    return ProfileCache(store)


def remember(cache: ProfileCache, body: dict[str, Any]) -> None:
    cache.remember(body["public_id"], body, FETCHED_AT)


def error(code: str) -> ApiError:
    return ApiError(code, log_detail="test")


def row_for(
    body: dict[str, Any] | str,
    *,
    public_id: str = PUBLIC_ID,
    version: int = ENVELOPE_VERSION,
    fetched_at: datetime = FETCHED_AT,
) -> CacheRow:
    """A stored row built by hand, for the rows the cache must refuse."""
    return CacheRow(
        public_id=public_id,
        body=body if isinstance(body, str) else json.dumps(body),
        envelope_version=version,
        fetched_at=fetched_at,
    )


# --- Matrix: first fetch, repeat fetch ---------------------------------------


def test_nothing_is_served_when_nothing_was_ever_stored(cache: ProfileCache) -> None:
    fallback = cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED"))

    assert fallback.body is None
    assert fallback.reason == NO_RECORD


def test_a_stored_record_comes_back(cache: ProfileCache) -> None:
    remember(cache, envelope())

    served = cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED"))

    assert served.reason == SERVED
    assert served.body is not None
    assert served.body["public_id"] == PUBLIC_ID
    assert served.body["url"] == PROFILE_URL


def test_one_profiles_record_is_not_another_profiles(cache: ProfileCache) -> None:
    """The key is the public id, and there is no path that ignores it."""
    remember(cache, envelope())

    assert cache.fallback_for("someone-else", error("RATE_LIMITED")).body is None


def test_a_second_successful_fetch_replaces_the_record(
    cache: ProfileCache, store: InMemoryProfileCacheStore
) -> None:
    """Live always wins when it works, and what it leaves behind is the new answer."""
    remember(cache, envelope(profile={"name": {"full": "Old Answer"}}))
    later = FETCHED_AT + timedelta(days=2)
    cache.remember(
        PUBLIC_ID,
        envelope(fetched_at=later, profile={"name": {"full": "New Answer"}}),
        later,
    )

    served = cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED"))

    assert served.body is not None
    assert served.body["profile"]["name"]["full"] == "New Answer"
    assert len(store.rows) == 1, "there is one record per profile, not a history"


# --- Matrix: the three retryable failures ------------------------------------


@pytest.mark.parametrize("code", sorted(RETRYABLE_CODES))
def test_a_retryable_failure_with_a_record_is_answered_from_the_cache(
    cache: ProfileCache, code: str
) -> None:
    remember(cache, envelope())

    served = cache.fallback_for(PUBLIC_ID, error(code))

    assert served.body is not None
    assert served.body["stale"] is True
    assert served.body["profile"]["name"]["full"] == "Ada Placeholder"


@pytest.mark.parametrize("code", sorted(RETRYABLE_CODES))
def test_a_retryable_failure_with_nothing_cached_lets_the_error_stand(
    cache: ProfileCache, code: str
) -> None:
    """There is nothing to fall back to, so the typed error is the answer."""
    assert cache.fallback_for(PUBLIC_ID, error(code)).body is None


def test_the_served_record_carries_the_original_fetch_time(cache: ProfileCache) -> None:
    """The acceptance criterion, and the whole reason an unbounded cache is honest.

    `fetched_at` is when LinkedIn was read, never when this request was served.
    Re-stamping it here would make a week-old record indistinguishable from a
    fresh one, which is the single change that would make stale-serve dishonest.
    """
    remember(cache, envelope())

    served = cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED"))

    assert served.body is not None
    assert served.body["fetched_at"] == "2026-08-27T09:00:00Z"


# --- Matrix: the failures that must NEVER be served from the cache ------------


@pytest.mark.parametrize("code", NEVER_SERVED_CODES)
def test_a_non_retryable_failure_is_never_answered_from_the_cache(
    cache: ProfileCache, code: str
) -> None:
    """Every code in the taxonomy that is not retryable, not a hand-picked few.

    `SESSION_EXPIRED` is the one that matters most: a caller whose LinkedIn
    session has died must be told so, and a 200 carrying somebody's profile does
    not tell them. `PROFILE_NOT_FOUND` matters for a different reason — a
    deleted or hidden profile is not stale data, it is gone.
    """
    remember(cache, envelope())

    fallback = cache.fallback_for(PUBLIC_ID, error(code))

    assert fallback.body is None
    assert fallback.reason == NOT_RETRYABLE


def test_a_non_retryable_failure_does_not_even_read_the_datastore(
    cache: ProfileCache, store: InMemoryProfileCacheStore
) -> None:
    """Not merely "the record is not returned": it is not looked up at all.

    A lookup whose result is discarded is one edit away from being returned.
    """
    remember(cache, envelope())

    cache.fallback_for(PUBLIC_ID, error("SESSION_EXPIRED"))

    assert store.loads == []


def test_the_retryable_flag_comes_from_the_taxonomy_rather_than_a_second_list() -> None:
    """The rule reads `ERROR_SPECS`, so story 8 cannot reclassify a code past it.

    A hand-written list of servable codes here would keep serving a code the
    taxonomy had just declared permanent, with nothing failing to say so.
    """
    assert {code for code, spec in ERROR_SPECS.items() if spec.retryable} == (
        RETRYABLE_CODES
    ), "response-schema.md's retryable column changed; re-read this file's docstring"


def test_there_is_no_public_way_to_read_the_cache_past_the_retryable_gate() -> None:
    """The module says the rule lives in exactly one place; this makes that true.

    `recall` used to be public, which meant the gate was a convention rather
    than a structure — one call site away from a cached profile reaching a
    caller whose session had died.
    """
    public = {
        name
        for name in vars(ProfileCache)
        if not name.startswith("_") and callable(getattr(ProfileCache, name))
    }

    assert public == {"remember", "fallback_for"}


# --- Matrix: unbounded, partial, and served exactly as stored -----------------


def test_a_record_of_any_age_is_served(cache: ProfileCache) -> None:
    """Unbounded by decision: no TTL, no eviction, no age limit."""
    ancient = datetime(2019, 1, 1, tzinfo=timezone.utc)
    cache.remember(PUBLIC_ID, envelope(fetched_at=ancient), ancient)

    served = cache.fallback_for(PUBLIC_ID, error("UPSTREAM_CHALLENGE"))

    assert served.body is not None
    assert served.body["fetched_at"] == "2019-01-01T00:00:00Z"
    assert served.body["stale"] is True


def test_a_partial_record_is_served_with_the_same_partial(cache: ProfileCache) -> None:
    """Stored verbatim. Serving it must not re-run mapping or re-decide `partial`."""
    stored = envelope(partial=["certifications", "experience.employment_type"])
    del stored["profile"]["name"]  # the omitted-key half of the same contract
    stored["profile"]["skills"] = ["python"]
    remember(cache, stored)

    served = cache.fallback_for(PUBLIC_ID, error("UPSTREAM_ERROR"))

    assert served.body is not None
    assert served.body["partial"] == ["certifications", "experience.employment_type"]
    assert "name" not in served.body["profile"], "an omitted key must stay omitted"


def test_stale_is_the_only_key_the_cache_changes(cache: ProfileCache) -> None:
    """"Exactly as it was stored" asserted key by key, not just field by field."""
    stored = envelope(partial=["languages"])
    remember(cache, stored)

    served = cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED"))

    assert served.body == {**stored, "stale": True}
    assert set(served.body) == set(stored), "no key is added or dropped on the way out"


def test_the_stored_document_is_not_mutated_by_serving_it(cache: ProfileCache) -> None:
    """Two stale serves of one record must produce the same answer."""
    remember(cache, envelope())

    first = cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED"))
    second = cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED"))

    assert first == second


def test_the_body_round_trips_byte_for_byte(
    cache: ProfileCache, store: InMemoryProfileCacheStore
) -> None:
    """The store holds a string, so this is stronger than dict equality.

    Key order, unicode and number formatting all survive, which is what makes
    "served exactly as it was stored" a literal claim rather than a semantic one.
    """
    stored = envelope(profile={"name": {"full": "Ada Plaçéholder"}, "skills": ["ml"]})
    remember(cache, stored)

    (_, document, _, _) = store.written[0]

    assert json.loads(document) == stored
    assert list(json.loads(document)) == list(stored)
    assert "Ada Plaçéholder" in document, "unicode is stored as itself, not escaped"


def test_what_is_written_is_what_was_asked_to_be_written(
    cache: ProfileCache, store: InMemoryProfileCacheStore
) -> None:
    """The public id, the document, the version and the fetch time, in place."""
    cache.remember(PUBLIC_ID, envelope(), FETCHED_AT)

    row = store.rows[PUBLIC_ID]
    assert store.written == [(PUBLIC_ID, row.body, ENVELOPE_VERSION, FETCHED_AT)]
    assert row.fetched_at == FETCHED_AT


# --- The stored timestamp and the body's own must agree -----------------------


@pytest.mark.parametrize(
    "supplied",
    [
        datetime(2026, 8, 27, 14, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        datetime(2026, 8, 27, 9, 0),  # naive
    ],
)
def test_the_stored_fetch_time_is_normalised_to_utc(
    cache: ProfileCache, store: InMemoryProfileCacheStore, supplied: datetime
) -> None:
    """The README documents a psql staleness check; it must agree with the body.

    A non-UTC or naive value written into a `timestamptz` column makes the
    column and the body's own `fetched_at` string report different times, so the
    documented check disagrees with the response it is checking.
    """
    cache.remember(PUBLIC_ID, envelope(), supplied)

    stored = store.rows[PUBLIC_ID].fetched_at
    assert stored.tzinfo is not None
    assert stored == datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)


# --- The record never moves backwards in time --------------------------------


def test_an_older_fetch_does_not_replace_a_newer_record(
    cache: ProfileCache, store: InMemoryProfileCacheStore
) -> None:
    """Two concurrent fetches finish in LinkedIn's order, not the caller's.

    Without the guard the slower fetch's older body overwrites the faster
    fetch's newer one, and the record's `fetched_at` goes *down* — a last-good
    record that is not the last good one.
    """
    newer = FETCHED_AT
    older = FETCHED_AT - timedelta(hours=6)
    cache.remember(PUBLIC_ID, envelope(fetched_at=newer, profile={"n": "new"}), newer)

    assert cache.remember(
        PUBLIC_ID, envelope(fetched_at=older, profile={"n": "old"}), older
    ) is True, "losing a race is not a failure"

    served = cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED"))
    assert served.body is not None
    assert served.body["profile"] == {"n": "new"}
    assert store.rows[PUBLIC_ID].fetched_at == newer


def test_an_identical_fetch_time_still_rewrites(
    cache: ProfileCache, store: InMemoryProfileCacheStore
) -> None:
    """`<=`, not `<`: re-storing the same fetch is a write, not a refusal."""
    cache.remember(PUBLIC_ID, envelope(profile={"n": "first"}), FETCHED_AT)
    cache.remember(PUBLIC_ID, envelope(profile={"n": "second"}), FETCHED_AT)

    assert json.loads(store.rows[PUBLIC_ID].body)["profile"] == {"n": "second"}


# --- Matrix: a write that fails never costs the caller the answer -------------


def test_a_rejected_write_does_not_raise(
    cache: ProfileCache, store: InMemoryProfileCacheStore
) -> None:
    """The profile was retrieved; losing the bookkeeping is not a reason to withhold it."""
    store.fail_writes = True

    assert cache.remember(PUBLIC_ID, envelope(), FETCHED_AT) is False


def test_an_unserialisable_body_is_refused_rather_than_raising(
    cache: ProfileCache, store: InMemoryProfileCacheStore
) -> None:
    """A mapper bug is a bug, and it still must not fail a request that worked."""
    body = envelope()
    body["profile"] = {"name": object()}

    assert cache.remember(PUBLIC_ID, body, FETCHED_AT) is False
    assert store.written == []


def test_a_read_that_fails_lets_the_real_upstream_error_stand(
    cache: ProfileCache, store: InMemoryProfileCacheStore
) -> None:
    """A 503 about *our* datastore would be a worse answer than the 429 it replaced."""
    remember(cache, envelope())
    store.fail_reads = True

    fallback = cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED"))

    assert fallback.body is None
    assert fallback.reason == DATASTORE_UNAVAILABLE


def test_a_failure_while_validating_a_record_does_not_escape(
    cache: ProfileCache, store: InMemoryProfileCacheStore
) -> None:
    """The parse is inside the guard, not after it.

    It used to sit outside, so a `TypeError` raised by the very code written to
    make a bad row safe escaped `fallback_for` uncaught and turned a caller's
    real 429 into a 500 — the one thing the "never raises" contract exists to
    prevent, reached through its own enforcement.
    """
    store.rows[PUBLIC_ID] = CacheRow(
        public_id=PUBLIC_ID,
        body=None,  # type: ignore[arg-type]  # `json.loads(None)` raises TypeError
        envelope_version=ENVELOPE_VERSION,
        fetched_at=FETCHED_AT,
    )

    fallback = cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED"))

    assert fallback.body is None
    assert fallback.reason == DATASTORE_UNAVAILABLE


# --- A broken cache must never look like an empty one -------------------------
#
# Nothing about a cache failure ever reaches a caller — that is the design, and
# it is also what makes a cache that has NEVER worked indistinguishable, in
# operation, from one that simply has nothing in it yet. The log is the only
# place the difference can exist, so the difference has to be in the log.


def levels_for(caplog: pytest.LogCaptureFixture) -> set[str]:
    return {record.levelname for record in caplog.records}


def test_a_swallowed_write_failure_is_logged_at_error(
    cache: ProfileCache,
    store: InMemoryProfileCacheStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store.fail_writes = True

    with caplog.at_level(logging.DEBUG, logger="app.cache"):
        cache.remember(PUBLIC_ID, envelope(), FETCHED_AT)

    assert levels_for(caplog) == {"ERROR"}
    assert PUBLIC_ID in caplog.text
    assert "CAP-5" in caplog.text, "the log must name what stopped working"


def test_a_swallowed_read_failure_is_logged_at_error(
    cache: ProfileCache,
    store: InMemoryProfileCacheStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store.fail_reads = True

    with caplog.at_level(logging.DEBUG, logger="app.cache"):
        cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED"))

    assert levels_for(caplog) == {"ERROR"}
    assert "CAP-5" in caplog.text


def test_an_empty_cache_is_reported_differently_from_a_broken_one(
    cache: ProfileCache,
    store: InMemoryProfileCacheStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole point: an operator must be able to tell these two apart.

    Both produce the identical 502 for the caller. If they also produced the
    identical log line, a cache that never once worked would be invisible.
    """
    with caplog.at_level(logging.DEBUG, logger="app.cache"):
        empty = cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED"))
    empty_text, empty_levels = caplog.text, levels_for(caplog)

    caplog.clear()
    store.fail_reads = True
    with caplog.at_level(logging.DEBUG, logger="app.cache"):
        broken = cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED"))

    assert empty.body is broken.body is None, "same answer to the caller"
    assert empty.reason != broken.reason, "different answer to the code"
    assert empty_levels == {"INFO"} and levels_for(caplog) == {"ERROR"}
    assert empty_text != caplog.text, "different answer to an operator"


@pytest.mark.parametrize(
    "reason",
    [SERVED, NOT_RETRYABLE, NO_RECORD, DATASTORE_UNAVAILABLE, UNUSABLE_RECORD],
)
def test_every_outcome_has_its_own_reason(reason: str) -> None:
    """Five distinct operational conditions, five distinct values, no overlap."""
    everything = [SERVED, NOT_RETRYABLE, NO_RECORD, DATASTORE_UNAVAILABLE, UNUSABLE_RECORD]

    assert len(set(everything)) == len(everything)
    assert reason in everything


# --- A stored row that cannot be trusted is treated as absent -----------------


def test_a_row_returned_for_a_different_key_is_refused(
    cache: ProfileCache, store: InMemoryProfileCacheStore
) -> None:
    """The dropped-`WHERE` mutation, at the layer that can actually catch it.

    Every other guard here compares the body against the ROW'S OWN key, which
    catches "this document disagrees with the row it sits in" and cannot catch
    "the store handed back a row for a different key" — in that case the row's
    key, the body's id and each other agree perfectly. So the requested id is
    threaded down and compared independently of the SQL, which is the story-5
    lesson: a defence that restates the statement is no defence against the
    statement being wrong.
    """
    store.answer_with = row_for(envelope(public_id="someone-else"), public_id="someone-else")

    fallback = cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED"))

    assert fallback.body is None
    assert fallback.reason == UNUSABLE_RECORD


def test_a_record_naming_a_different_member_is_refused(
    cache: ProfileCache, store: InMemoryProfileCacheStore
) -> None:
    """The worst failure this API has, with a longer fuse than the live one.

    Story 4's review caught the live path answering a request for one member
    with another member's profile. A mis-keyed cache row is the same bug,
    except it would be served to every later caller too.
    """
    store.rows[PUBLIC_ID] = row_for(envelope(public_id="someone-else"))

    assert cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED")).body is None


@pytest.mark.parametrize(
    "document", ["not json at all", '"a string"', "[1, 2, 3]", "null"]
)
def test_an_unusable_record_is_treated_as_no_record(
    cache: ProfileCache, store: InMemoryProfileCacheStore, document: str
) -> None:
    store.rows[PUBLIC_ID] = row_for(document)

    fallback = cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED"))

    assert fallback.body is None
    assert fallback.reason == UNUSABLE_RECORD


def test_nothing_in_this_module_ever_removes_a_record(
    cache: ProfileCache, store: InMemoryProfileCacheStore
) -> None:
    """Refusing a row is not evicting it. Unbounded is the author's decision.

    Every refusal above returns `None`; none of them may also delete. A row the
    current build cannot use is one the next live fetch replaces.
    """
    store.rows[PUBLIC_ID] = row_for("not json at all")

    cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED"))

    assert PUBLIC_ID in store.rows, "the unusable row is ignored, not deleted"


# --- Records outlive the shape they were written in ---------------------------


def test_a_record_missing_an_envelope_key_is_not_republished(
    cache: ProfileCache, store: InMemoryProfileCacheStore
) -> None:
    """The concrete bug: a body seeded without `partial` came back as a 200 without it.

    `response-schema.md` makes `partial` always-present precisely so that
    "nothing in it" and "this response predates the field" stay distinguishable.
    Republishing a body that lacks it destroys exactly that distinction — and
    because records are never evicted, it would do so for ever.
    """
    stored = envelope()
    del stored["partial"]
    store.rows[PUBLIC_ID] = row_for(stored)

    fallback = cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED"))

    assert fallback.body is None
    assert fallback.reason == UNUSABLE_RECORD


def test_a_record_carrying_an_unknown_envelope_key_is_not_republished(
    cache: ProfileCache, store: InMemoryProfileCacheStore
) -> None:
    stored = envelope()
    stored["cached_by"] = "some future build"
    store.rows[PUBLIC_ID] = row_for(stored)

    assert cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED")).body is None


def test_a_record_written_under_an_older_envelope_version_is_ignored(
    cache: ProfileCache, store: InMemoryProfileCacheStore
) -> None:
    """Braces to the key set's belt, for shape changes *inside* `profile`.

    Records survive every later deploy, so a body whose sub-shape changed is
    still wrong even when its top-level keys are right. Bumping the version
    makes every older row invisible without deleting one.
    """
    store.rows[PUBLIC_ID] = row_for(envelope(), version=ENVELOPE_VERSION - 1)

    fallback = cache.fallback_for(PUBLIC_ID, error("RATE_LIMITED"))

    assert fallback.body is None
    assert fallback.reason == UNUSABLE_RECORD
    assert PUBLIC_ID in store.rows, "ignored, never evicted"


def test_the_envelope_version_is_bumped_whenever_the_envelope_changes() -> None:
    """A snapshot, so changing the shape without bumping the version fails HERE.

    Otherwise `ENVELOPE_VERSION` is a constant nobody remembers to touch, and
    the guard it powers protects nothing. If this test fails because you changed
    the envelope: bump the version, then update this line.
    """
    assert (ENVELOPE_VERSION, sorted(REQUIRED_ENVELOPE_KEYS)) == (
        1,
        ["fetched_at", "partial", "profile", "public_id", "stale", "url"],
    )


def test_the_required_key_set_is_the_envelope_the_route_actually_builds() -> None:
    """Two hand-written key lists would drift; this pins them together."""
    from app.api.v1.profile import ProfileEnvelope

    assert set(ProfileEnvelope.model_fields) == REQUIRED_ENVELOPE_KEYS


# --- The schema, resolved against the statements, with no database ------------
#
# See this module's docstring: five real mutations shipped a silently dead cache
# past 816 green tests. Everything below runs by default.


def _tables() -> dict[str, dict[str, str]]:
    """`{relation: {column: type}}`, parsed from what `bootstrap` creates."""
    tables: dict[str, dict[str, str]] = {}
    for statement in db.BOOTSTRAP_STATEMENTS:
        match = re.search(
            r"CREATE TABLE IF NOT EXISTS\s+([\w.]+)\s*\((.+)\)", statement, re.S
        )
        if match is None:
            continue
        columns = {}
        for line in match.group(2).split(","):
            parts = line.split()
            if len(parts) >= 2:
                columns[parts[0]] = parts[1]
        tables[match.group(1)] = columns
    return tables


#: Everything in these statements that is a word but not a name.
_SQL_WORDS = {
    "INSERT", "INTO", "VALUES", "ON", "CONFLICT", "DO", "UPDATE", "SET", "WHERE",
    "RETURNING", "SELECT", "FROM", "AND", "OR", "NOT", "NULL", "EXCLUDED", "AS",
    "DELETE", "IS", "IN", "BY", "ORDER", "LIMIT",
}


def _relations_named(statement: str) -> set[str]:
    """Every relation the statement reads or writes.

    `DO UPDATE SET` is not a relation reference, so a bare keyword capture is
    dropped rather than special-cased — the same filter also covers whatever
    the next statement's shape turns out to be.
    """
    found = re.findall(r"(?:INSERT INTO|FROM|UPDATE|JOIN)\s+([\w.]+)", statement)
    return {name for name in found if name.upper() not in _SQL_WORDS}


def _columns_named(statement: str, relations: set[str]) -> set[str]:
    """Every identifier the statement uses as a column name.

    Relation names are stripped first — both the qualified `app.profile_cache`
    and the bare `profile_cache` an `ON CONFLICT ... WHERE` clause uses — and
    then the `EXCLUDED.` qualifier, so what is left is column names and SQL
    words. The assertion that the result is non-empty is load-bearing: a
    stripping bug that removed everything would make this check vacuous.
    """
    # `%s` first, or every placeholder leaves a bare `s` behind.
    text = statement.replace("%s", " ")
    for relation in sorted(relations, key=len, reverse=True):
        text = text.replace(relation, " ")
        text = text.replace(relation.split(".")[-1], " ")
    text = text.replace(f"{db.SCHEMA}.", " ")
    text = re.sub(r"\bEXCLUDED\.", " ", text)
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))
    return {word for word in words if word.upper() not in _SQL_WORDS}


@pytest.mark.parametrize("statement", db.CACHE_STATEMENTS)
def test_every_cache_statement_names_the_cache_relation(statement: str) -> None:
    """Mutations (d) and (e): a statement pointed at the SESSION relation.

    Both shipped green. The cache silently never writes and never serves, CAP-5
    is gone, and nothing anywhere says so — because `remember` and
    `fallback_for` swallow the error by design.
    """
    assert _relations_named(statement) == {db.CACHE_RELATION}, statement


@pytest.mark.parametrize("statement", db.CACHE_STATEMENTS)
def test_every_cache_statement_resolves_against_the_schema_bootstrap_creates(
    statement: str,
) -> None:
    """Mutation (a): `fetched_at` renamed in the DDL only.

    Both statements then name a column that does not exist, every cache
    operation fails, and the suite stayed green because nothing offline executed
    them and the executing tests are opt-in.
    """
    tables = _tables()
    assert db.CACHE_RELATION in tables, "bootstrap does not create the cache table"
    available = set(tables[db.CACHE_RELATION])

    named = _columns_named(statement, _relations_named(statement))

    assert named, "the identifier stripping removed everything; this check is vacuous"
    assert named <= available, (
        f"{sorted(named - available)} is not a column of {db.CACHE_RELATION}; "
        f"it has {sorted(available)}"
    )


def test_the_cache_columns_list_is_columns_that_exist() -> None:
    """`_CACHE_COLUMNS` feeds both statements and `_to_cache_row`'s unpacking."""
    available = set(_tables()[db.CACHE_RELATION])

    named = {name.strip() for name in db._CACHE_COLUMNS.split(",")}

    assert named <= available
    assert named == available, "a column the statements never select is dead weight"


def test_the_body_column_is_text_and_not_jsonb() -> None:
    """Mutation (b), and the DDL comment says exactly why it matters.

    `jsonb` reorders keys — breaking "served exactly as it was stored" — and
    cannot hold a `\\u0000` escape at all, so one NUL in a member's `about` text
    turns every cache write for that profile into a logged failure.
    """
    assert _tables()[db.CACHE_RELATION]["body"] == "text"


def test_the_fetch_time_column_keeps_its_timezone() -> None:
    """Mutation (c). A naive column makes `fetched_at` "some local time"."""
    assert _tables()[db.CACHE_RELATION]["fetched_at"] == "timestamptz"


def test_the_cache_table_is_keyed_by_public_id() -> None:
    """One record per profile, replaced outright — a property of the schema."""
    ddl = next(s for s in db.BOOTSTRAP_STATEMENTS if db.CACHE_RELATION in s)

    assert re.search(r"public_id\s+text\s+PRIMARY KEY", ddl), ddl


def test_the_bootstrap_creates_the_cache_relation() -> None:
    """A table created in one schema and queried in another fails at runtime only."""
    created = " ".join(db.BOOTSTRAP_STATEMENTS)

    assert db.CACHE_RELATION in created
    assert db.CACHE_RELATION.startswith(f"{db.SCHEMA}.")


def test_no_cache_column_holds_session_or_subject_state() -> None:
    """This cache holds public profile data only — never a credential.

    It is keyed by profile and shared across callers, which is exactly why it
    must never learn anything about who fetched a record.
    """
    columns = set(_tables()[db.CACHE_RELATION])

    for forbidden in ("subject", "ciphertext", "session", "li_at", "cookie"):
        assert not any(forbidden in column for column in columns), forbidden


# --- No expiry, anywhere, in anything ----------------------------------------


def test_nothing_in_the_cache_schema_or_its_statements_bounds_a_records_age() -> None:
    """Unbounded is a decision in SPEC.md, not an oversight for someone to fix.

    Checked across every cache statement AND the DDL, not just the load: an age
    bound smuggled into the save's `ON CONFLICT` clause, or into a statement
    added later, is the same TTL by another route. `db.CACHE_STATEMENTS` is the
    enumeration this iterates, so a new statement left out of it is the gap.
    """
    surfaces = list(db.CACHE_STATEMENTS) + [
        s for s in db.BOOTSTRAP_STATEMENTS if db.CACHE_RELATION in s
    ]

    for surface in surfaces:
        lowered = surface.lower()
        assert "interval" not in lowered, surface
        assert "now()" not in lowered, surface
        assert "current_timestamp" not in lowered, surface
        assert "expires" not in lowered, surface
        assert "ttl" not in lowered, surface
        assert not re.search(r"fetched_at\s*[<>]\s*(?!=?\s*EXCLUDED)", surface), surface


def test_the_shipping_store_has_no_way_to_remove_a_record() -> None:
    """Asserted against the IMPLEMENTATION, not the Protocol.

    A `Protocol` that omits `delete` proves nothing about the class behind it —
    a delete added to `PostgresProfileCacheStore` would satisfy the Protocol and
    leave the old version of this test green.
    """
    for holder in (db.PostgresProfileCacheStore, db.ProfileCacheStore, ProfileCache):
        for name in ("delete", "evict", "purge", "expire", "prune", "clear"):
            assert not hasattr(holder, name), f"{holder.__name__}.{name}"


# --- The SQL the shipping store actually executes -----------------------------


def test_the_executed_load_filters_on_the_public_id() -> None:
    """Asserting on the constant proves what it says; this proves what runs."""
    connection = RecordingConnection(rows=[(PUBLIC_ID, "{}", ENVELOPE_VERSION, FETCHED_AT)])
    store = db.PostgresProfileCacheStore(connect_fn=lambda: connection)

    store.load(PUBLIC_ID)

    (sql, params) = connection.calls[0]
    assert re.findall(r"(\w+)\s*=\s*%s", sql) == ["public_id"], sql
    assert params == (PUBLIC_ID,)


def test_a_loaded_row_maps_back_to_the_columns_it_asked_for() -> None:
    """`_CACHE_COLUMNS` and `_to_cache_row` must agree, positionally."""
    row = row_for('{"a": 1}')
    by_name = {
        "public_id": row.public_id,
        "body": row.body,
        "envelope_version": row.envelope_version,
        "fetched_at": row.fetched_at,
    }
    ordered = tuple(by_name[name.strip()] for name in db._CACHE_COLUMNS.split(","))
    connection = RecordingConnection(rows=[ordered])
    store = db.PostgresProfileCacheStore(connect_fn=lambda: connection)

    assert store.load(PUBLIC_ID) == row


def test_the_executed_save_binds_every_column_in_order() -> None:
    """A parameter swap would file the document under the timestamp."""
    connection = RecordingConnection(rows=[(PUBLIC_ID, "{}", ENVELOPE_VERSION, FETCHED_AT)])
    store = db.PostgresProfileCacheStore(connect_fn=lambda: connection)

    store.save(PUBLIC_ID, '{"a": 1}', ENVELOPE_VERSION, FETCHED_AT)

    (sql, params) = connection.calls[0]
    assert params == (PUBLIC_ID, '{"a": 1}', ENVELOPE_VERSION, FETCHED_AT)
    assert "ON CONFLICT (public_id) DO UPDATE" in sql


def test_the_executed_save_refuses_to_move_the_record_backwards() -> None:
    """The guard is in the statement, not only in the in-memory double."""
    assert f"WHERE {db.CACHE_TABLE}.fetched_at <= EXCLUDED.fetched_at" in db._CACHE_SAVE_SQL


def test_a_save_that_a_newer_record_won_returns_no_row_rather_than_raising() -> None:
    """`RETURNING` yields nothing when the `ON CONFLICT` guard refuses the update."""
    connection = RecordingConnection(rows=[])
    store = db.PostgresProfileCacheStore(connect_fn=lambda: connection)

    assert store.save(PUBLIC_ID, "{}", ENVELOPE_VERSION, FETCHED_AT) is None


def test_the_cache_connection_bounds_its_own_statements() -> None:
    """The half of the hang fix that `asyncio.timeout` cannot provide.

    A timeout around `to_thread` frees the request but cannot cancel the thread;
    only Postgres aborting the statement hands the worker back. Without it a
    wedged backend starves the executor that `vault.unlock` shares.
    """
    import inspect

    default = inspect.signature(db.PostgresProfileCacheStore.__init__).parameters[
        "connect_fn"
    ].default
    assert default is db.connect_for_cache, "the shipping store must use the bounded connection"

    source = inspect.getsource(db.connect_for_cache)
    assert f"statement_timeout={db.CACHE_STATEMENT_TIMEOUT_MS}" in source.replace(
        "{CACHE_STATEMENT_TIMEOUT_MS}", str(db.CACHE_STATEMENT_TIMEOUT_MS)
    )
    assert db.CACHE_STATEMENT_TIMEOUT_MS > 0
    assert db.CACHE_CONNECT_TIMEOUT_SECONDS <= db.CONNECT_TIMEOUT_SECONDS
    # And the session/bootstrap path must NOT have inherited it: its DDL waits
    # on an advisory lock a concurrently starting container may hold.
    assert "statement_timeout" not in inspect.getsource(db.connect)


def test_the_shipping_cache_is_backed_by_postgres() -> None:
    """The process-wide instance the route actually resolves.

    Constructing it opens no connection, which is what keeps `import app.main`
    working on a laptop with the stack down.
    """
    assert isinstance(cache_module.cache._store, db.PostgresProfileCacheStore)
