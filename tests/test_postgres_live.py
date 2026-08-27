"""The one live database check. Skipped by default, and it must stay that way.

`tests/test_vault.py` proves what `PostgresSessionStore` *executes* — the SQL
and the parameters it hands the driver — against a recording fake. That is the
half an offline suite can prove. It cannot prove the other half: that Postgres
accepts those statements, that the schema `bootstrap` creates actually has the
columns they name, that `bytea` round-trips as `bytes`, and that
`timestamptz` comes back with a timezone attached.

That gap is not academic. Every one of the following mutations left the whole
offline suite green before this file existed, and each is a real defect:

* dropping `WHERE subject = %s` from the executed fetch — one caller reading
  another caller's stored session, CAP-4 exactly inverted;
* swapping two names in `_COLUMNS` while `_to_row` unpacks positionally;
* reordering the parameters bound in `mark_use`.

Two gates, matching the convention `tests/test_linkedin_live.py` set, so that
`docker build --target test && docker run --rm --network none` — the command CI
and a grader use — collects this file and skips it::

    POSTGRES_LIVE_CHECK=1 \
    POSTGRES_LIVE_URL="postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@127.0.0.1:5432/$POSTGRES_DB" \
      pytest -q -m postgres

`POSTGRES_LIVE_URL` is a variable of its own rather than `DATABASE_URL`, because
`tests/conftest.py` overwrites `DATABASE_URL` unconditionally before
`app.config` is imported. Reading a separate one keeps this honest about what it
is connecting to.

Story 7's response cache is covered here for the same reason and by the same
argument: the offline suite proves what `PostgresProfileCacheStore` executes,
and only a real database can prove that `bootstrap` created the columns those
statements name — and that the stored body comes back as the same characters,
which is the story's central promise.

**It writes only to its own rows.** Every subject and every public id used here
is generated per run and prefixed, and the test deletes what it created. It
never touches a row it did not write, so pointing it at the development stack
costs nothing.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import cache, db
from app.errors import ApiError

#: Both gates must open. The flag alone is not enough — a developer with a DSN
#: exported for other reasons must not have this fire because they typed
#: `pytest` — and neither is the DSN alone.
LIVE_ENABLED = os.environ.get("POSTGRES_LIVE_CHECK") == "1"
LIVE_URL = os.environ.get("POSTGRES_LIVE_URL")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not LIVE_ENABLED,
        reason="live database check is opt-in: set POSTGRES_LIVE_CHECK=1",
    ),
]

#: Every row this file writes carries it — session subjects and cache public ids
#: alike — so a stray row is identifiable and a cleanup that misses one is
#: obvious in `psql`.
SUBJECT_PREFIX = "pytest-live-"


def _connect():
    import psycopg

    if not LIVE_URL:
        pytest.skip("POSTGRES_LIVE_URL is not set; see this module's docstring")
    return psycopg.connect(LIVE_URL, connect_timeout=5)


@pytest.fixture(name="store")
def _store():
    """The real store, pointed at the real database, cleaning up after itself."""
    store = db.PostgresSessionStore(connect_fn=_connect)
    subjects: list[str] = []

    yield store, subjects

    with _connect() as connection:
        with connection.cursor() as cursor:
            for subject in subjects:
                cursor.execute(
                    f"DELETE FROM {db.SESSION_RELATION} WHERE subject = %s", (subject,)
                )


@pytest.fixture(name="cache_store")
def _cache_store():
    """The real cache store, cleaning up after itself.

    The cache table has no delete path in the application — stale-serve is
    unbounded by decision — so this fixture issues the ``DELETE`` itself rather
    than through the store. That is the correct shape: a test tidying up after
    itself must not require production code that the spec says should not exist.
    """
    store = db.PostgresProfileCacheStore(connect_fn=_connect)
    public_ids: list[str] = []

    yield store, public_ids

    with _connect() as connection:
        with connection.cursor() as cursor:
            for public_id in public_ids:
                cursor.execute(
                    f"DELETE FROM {db.CACHE_RELATION} WHERE public_id = %s",
                    (public_id,),
                )


def _subject(subjects: list[str]) -> str:
    subject = SUBJECT_PREFIX + uuid.uuid4().hex
    subjects.append(subject)
    return subject


def _public_id(public_ids: list[str]) -> str:
    public_id = SUBJECT_PREFIX + uuid.uuid4().hex
    public_ids.append(public_id)
    return public_id


def test_the_bootstrap_is_idempotent_against_a_real_database() -> None:
    """It runs on every start, warm volume included, and must be a no-op then."""
    db.bootstrap(attempts=1, retry_seconds=0, connect_fn=_connect)
    db.bootstrap(attempts=1, retry_seconds=0, connect_fn=_connect)


def test_a_session_round_trips_through_postgres(store) -> None:
    """Store, read back, and get the same bytes and the same timestamps.

    `bytea` must come back as `bytes` — a `memoryview` would silently break the
    substring assertions the leak tests depend on — and `timestamptz` must come
    back timezone-aware, or `fetched_at`-style values serialise as "some local
    time" to a consumer.
    """
    store, subjects = store
    db.bootstrap(attempts=1, retry_seconds=0, connect_fn=_connect)
    subject = _subject(subjects)

    written = store.upsert(subject, b"\x00\x01ciphertext-bytes\xff")
    read = store.fetch(subject)

    assert read == written
    assert isinstance(read.ciphertext, bytes)
    assert read.ciphertext == b"\x00\x01ciphertext-bytes\xff"
    assert read.stored_at.tzinfo is not None
    assert read.last_used_at is None and read.last_use_ok is None


def test_a_fetch_never_returns_another_subjects_row(store) -> None:
    """CAP-4, against the real query. The mutation this file exists for.

    Dropping the `WHERE` clause makes this return whichever row Postgres happens
    to hand back first — which is exactly the bug that stayed invisible while
    the offline suite asserted a substring of a string constant.
    """
    store, subjects = store
    db.bootstrap(attempts=1, retry_seconds=0, connect_fn=_connect)
    a, b = _subject(subjects), _subject(subjects)

    store.upsert(a, b"subject-a-ciphertext")

    assert store.fetch(b) is None

    store.upsert(b, b"subject-b-ciphertext")
    assert store.fetch(a).ciphertext == b"subject-a-ciphertext"
    assert store.fetch(b).ciphertext == b"subject-b-ciphertext"


def test_upsert_replaces_rather_than_duplicating(store) -> None:
    """`subject` is the PRIMARY KEY, so overwrite is enforced by the schema."""
    store, subjects = store
    db.bootstrap(attempts=1, retry_seconds=0, connect_fn=_connect)
    subject = _subject(subjects)

    store.upsert(subject, b"first")
    store.upsert(subject, b"second")

    assert store.fetch(subject).ciphertext == b"second"
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT count(*) FROM {db.SESSION_RELATION} WHERE subject = %s",
                (subject,),
            )
            assert cursor.fetchone()[0] == 1


def test_mark_use_writes_the_verdict_against_the_right_row(store) -> None:
    """Catches a parameter reorder: `ok` in a timestamptz column simply fails."""
    store, subjects = store
    db.bootstrap(attempts=1, retry_seconds=0, connect_fn=_connect)
    subject, other = _subject(subjects), _subject(subjects)
    store.upsert(other, b"untouched")
    row = store.upsert(subject, b"ciphertext")
    at = datetime.now(timezone.utc)

    updated = store.mark_use(subject, ok=True, at=at, stored_at=row.stored_at)

    assert updated is not None
    assert updated.last_use_ok is True
    assert updated.last_used_at is not None
    assert store.fetch(subject).last_use_ok is True
    # The other subject's row must not have been touched by a subject-less
    # UPDATE, which is the same class of bug as the fetch above.
    assert store.fetch(other).last_use_ok is None


def test_a_verdict_for_a_replaced_row_matches_nothing(store) -> None:
    """The `AND stored_at = %s` predicate, against the real database."""
    store, subjects = store
    db.bootstrap(attempts=1, retry_seconds=0, connect_fn=_connect)
    subject = _subject(subjects)
    first = store.upsert(subject, b"first")
    store.upsert(subject, b"second")  # the concurrent PUT

    late = store.mark_use(
        subject,
        ok=False,
        at=datetime.now(timezone.utc),
        stored_at=first.stored_at,
    )

    assert late is None
    assert store.fetch(subject).last_use_ok is None, "a stale verdict was recorded"


def test_a_stale_timestamp_that_matches_nothing_is_not_an_error(store) -> None:
    """The no-op path must stay a no-op, not a raise."""
    store, subjects = store
    db.bootstrap(attempts=1, retry_seconds=0, connect_fn=_connect)
    subject = _subject(subjects)
    store.upsert(subject, b"ciphertext")

    assert (
        store.mark_use(
            subject,
            ok=True,
            at=datetime.now(timezone.utc),
            stored_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        is None
    )


# --- The response cache table (story 7) ---------------------------------------


def test_a_cached_response_round_trips_through_postgres(cache_store) -> None:
    """The whole point of an offline-provable cache still needs the schema to exist.

    Two things only a real database can settle: that ``bootstrap`` creates the
    columns ``_CACHE_SAVE_SQL`` names, and that the body comes back as the same
    characters it went in as. The second is the story's central promise — a
    record is served exactly as it was stored — and a ``jsonb`` column would
    quietly fail it by reordering keys, which is why the column is ``text``.
    """
    cache_store, public_ids = cache_store
    db.bootstrap(attempts=1, retry_seconds=0, connect_fn=_connect)
    public_id = _public_id(public_ids)
    fetched_at = datetime.now(timezone.utc) - timedelta(days=3)
    # Deliberately awkward: key order that JSON canonicalisation would change,
    # non-ASCII, and an escaped NUL — which `jsonb` cannot store at all.
    body = '{"url": "https://example.invalid", "z": 1, "a": "Plaçéholder\\u0000"}'

    written = cache_store.save(public_id, body, cache.ENVELOPE_VERSION, fetched_at)
    read = cache_store.load(public_id)

    assert read == written
    assert read.body == body
    assert read.envelope_version == cache.ENVELOPE_VERSION
    assert read.fetched_at.tzinfo is not None
    assert read.fetched_at == fetched_at


def test_a_load_never_returns_another_profiles_record(cache_store) -> None:
    """Dropping the `WHERE` clause here answers one member with another's profile."""
    cache_store, public_ids = cache_store
    db.bootstrap(attempts=1, retry_seconds=0, connect_fn=_connect)
    a, b = _public_id(public_ids), _public_id(public_ids)
    now = datetime.now(timezone.utc)

    cache_store.save(a, '{"who": "a"}', cache.ENVELOPE_VERSION, now)

    assert cache_store.load(b) is None

    cache_store.save(b, '{"who": "b"}', cache.ENVELOPE_VERSION, now)
    assert cache_store.load(a).body == '{"who": "a"}'
    assert cache_store.load(b).body == '{"who": "b"}'


def test_a_second_save_replaces_rather_than_duplicating(cache_store) -> None:
    """`public_id` is the PRIMARY KEY: one record per profile, enforced by the schema."""
    cache_store, public_ids = cache_store
    db.bootstrap(attempts=1, retry_seconds=0, connect_fn=_connect)
    public_id = _public_id(public_ids)
    first = datetime.now(timezone.utc) - timedelta(days=1)
    second = datetime.now(timezone.utc)

    cache_store.save(public_id, '{"n": 1}', cache.ENVELOPE_VERSION, first)
    cache_store.save(public_id, '{"n": 2}', cache.ENVELOPE_VERSION, second)

    read = cache_store.load(public_id)
    assert read.body == '{"n": 2}'
    assert read.fetched_at == second, "the replacement carries its own fetch time"
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT count(*) FROM {db.CACHE_RELATION} WHERE public_id = %s",
                (public_id,),
            )
            assert cursor.fetchone()[0] == 1


def test_an_older_save_cannot_move_the_record_backwards(cache_store) -> None:
    """The `ON CONFLICT ... WHERE` guard, against the real planner.

    Two concurrent fetches for one profile finish in LinkedIn's order, not the
    caller's, so a plain `DO UPDATE` lets the slower fetch's older body
    overwrite the newer one and the record's `fetched_at` goes *down*. The
    syntax of a `WHERE` on a `DO UPDATE` — and that a refused update returns no
    row rather than raising — is exactly what an offline fake cannot settle.
    """
    cache_store, public_ids = cache_store
    db.bootstrap(attempts=1, retry_seconds=0, connect_fn=_connect)
    public_id = _public_id(public_ids)
    newer = datetime.now(timezone.utc)
    older = newer - timedelta(hours=6)

    cache_store.save(public_id, '{"n": "new"}', cache.ENVELOPE_VERSION, newer)
    refused = cache_store.save(public_id, '{"n": "old"}', cache.ENVELOPE_VERSION, older)

    assert refused is None, "a refused update must return no row, not raise"
    read = cache_store.load(public_id)
    assert read.body == '{"n": "new"}'
    assert read.fetched_at == newer


def test_an_equally_timed_save_still_rewrites(cache_store) -> None:
    """`<=`, not `<`: re-storing an identical fetch is a write, not a refusal."""
    cache_store, public_ids = cache_store
    db.bootstrap(attempts=1, retry_seconds=0, connect_fn=_connect)
    public_id = _public_id(public_ids)
    at = datetime.now(timezone.utc)

    cache_store.save(public_id, '{"n": "first"}', cache.ENVELOPE_VERSION, at)
    again = cache_store.save(public_id, '{"n": "second"}', cache.ENVELOPE_VERSION, at)

    assert again is not None
    assert cache_store.load(public_id).body == '{"n": "second"}'


def test_a_very_old_record_is_still_returned(cache_store) -> None:
    """Unbounded by decision. A TTL added to the SQL would fail here and nowhere else."""
    cache_store, public_ids = cache_store
    db.bootstrap(attempts=1, retry_seconds=0, connect_fn=_connect)
    public_id = _public_id(public_ids)
    ancient = datetime(2015, 1, 1, tzinfo=timezone.utc)

    cache_store.save(public_id, '{"old": true}', cache.ENVELOPE_VERSION, ancient)

    read = cache_store.load(public_id)
    assert read is not None
    assert read.fetched_at == ancient


def test_the_whole_cache_round_trip_works_through_the_real_connection(
    cache_store,
) -> None:
    """`ProfileCache` over `PostgresProfileCacheStore`, end to end.

    Every offline test of `ProfileCache` runs over an in-memory double, and
    every offline test of the store runs over a recording fake. Neither proves
    the two fit together against a real database — which is the shape of the
    failure this story's review found: five ways for the cache to be entirely
    broken and entirely silent.
    """
    cache_store, public_ids = cache_store
    db.bootstrap(attempts=1, retry_seconds=0, connect_fn=_connect)
    public_id = _public_id(public_ids)
    profile_cache = cache.ProfileCache(cache_store)
    fetched_at = datetime.now(timezone.utc) - timedelta(days=2)
    body = {
        "url": f"https://www.linkedin.com/in/{public_id}",
        "public_id": public_id,
        "stale": False,
        "fetched_at": fetched_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "partial": ["certifications"],
        "profile": {"name": {"full": "Ada Plaçéholder"}},
    }

    assert profile_cache.remember(public_id, body, fetched_at) is True

    served = profile_cache.fallback_for(public_id, ApiError("RATE_LIMITED"))

    assert served.reason == cache.SERVED, served.reason
    assert served.body == {**body, "stale": True}


def test_the_cache_connections_statement_timeout_is_one_postgres_accepts() -> None:
    """The half of the hang fix Postgres itself has to enforce.

    `asyncio.timeout` frees the request but cannot cancel a thread already
    inside the driver; only the server aborting the statement hands the worker
    back. A malformed `options` string is accepted by nobody until libpq sends
    it, so this connects with the real one and asks the backend what it got.

    It builds the connection from `POSTGRES_LIVE_URL` rather than calling
    `connect_for_cache` directly, because `tests/conftest.py` points
    `DATABASE_URL` at nothing on purpose. That `connect_for_cache` passes THIS
    constant is asserted offline, in `tests/test_cache.py`.
    """
    import psycopg

    if not LIVE_URL:
        pytest.skip("POSTGRES_LIVE_URL is not set; see this module's docstring")

    with psycopg.connect(
        LIVE_URL,
        connect_timeout=db.CACHE_CONNECT_TIMEOUT_SECONDS,
        options=f"-c statement_timeout={db.CACHE_STATEMENT_TIMEOUT_MS}",
    ) as connection:
        with connection.cursor() as cursor:
            # `pg_settings`, not `SHOW`: the latter renders the value back in
            # whatever unit reads most nicely (5000ms becomes "5s"), so
            # comparing its string would fail on a setting that is correct.
            cursor.execute(
                "SELECT setting, unit FROM pg_settings WHERE name = 'statement_timeout'"
            )
            setting, unit = cursor.fetchone()
            assert unit == "ms"
            assert int(setting) == db.CACHE_STATEMENT_TIMEOUT_MS


def test_an_unreachable_database_is_datastore_unavailable() -> None:
    """The typed 503 path, against a real driver rather than a fake exception."""
    import psycopg

    def unreachable():
        # Port 1 is reserved and nothing listens on it.
        return psycopg.connect(
            "postgresql://nobody:nobody@127.0.0.1:1/nothing", connect_timeout=2
        )

    store = db.PostgresSessionStore(connect_fn=unreachable)

    with pytest.raises(db.DatastoreUnavailable):
        store.fetch("whoever")
