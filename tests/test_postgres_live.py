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

**It writes only to its own rows.** Every subject used here is generated per run
and prefixed, and the test deletes what it created. It never touches a row it
did not write, so pointing it at the development stack costs nothing.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import db

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

#: Every row this file writes carries it, so a stray row is identifiable and a
#: cleanup that misses one is obvious in `psql`.
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


def _subject(subjects: list[str]) -> str:
    subject = SUBJECT_PREFIX + uuid.uuid4().hex
    subjects.append(subject)
    return subject


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
