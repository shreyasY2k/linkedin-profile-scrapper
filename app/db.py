"""Postgres access, and the schema this application owns.

Two things live here and nothing else: how a connection is opened, and the
SQL the layers above it run. The vault above it (:mod:`app.vault`) knows about
ciphertext and never about Postgres; this module knows about Postgres and never
about plaintext. That split is what makes "the cookie is in plaintext in
exactly one module" a checkable claim rather than a habit.

Story 7's response cache (:mod:`app.cache`) is filed the same way and for the
same reason: this module stores an opaque text document against a public id and
has no idea it is a JSON response envelope. Nothing here can reshape a cached
body, because nothing here knows what one looks like.

===============================================================================
NO MIGRATION TOOL, BY DECISION (2026-08-27)
===============================================================================

The human's call, recorded in the story's Design Notes: no migration tool until
the APIs are completely built, then introduce one. So the schema is created by
:func:`bootstrap`, which is idempotent and safe to run on every start — the
deployed stack must come up on a cold volume with no manual step, and
``docker compose down -v && docker compose up -d --wait`` is an acceptance
criterion, not a convenience.

The trade-off is accepted knowingly: no migration history, and no down-path,
until a tool lands later. Stories 6-8 extend :data:`BOOTSTRAP_STATEMENTS` the
same way. **Do not add Alembic here.**

===============================================================================
SCHEMA NAMESPACING
===============================================================================

Keycloak owns ``public`` in this database — it is the same Postgres instance,
and Keycloak runs its own migrations against it on every version bump. Every
table this application creates therefore lives in :data:`SCHEMA`, so a Keycloak
migration and an application change can never collide over a name. This is the
deferred finding from story 1, landing here.
"""

from __future__ import annotations

import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Protocol

import psycopg

from app.config import settings

logger = logging.getLogger(__name__)


# --- Identifiers -------------------------------------------------------------

#: The application's own schema. NOT ``public``: see the module docstring.
SCHEMA = "app"

#: One row per Keycloak subject. Overwrite is the whole lifecycle, so there is
#: no history table and no soft-delete column — story 5 has no delete path.
SESSION_TABLE = "linkedin_session"

#: One row per LinkedIn public id: the last good response for that profile.
#:
#: Keyed by profile, **not** by caller, and that is a decision rather than an
#: oversight. This table holds public profile data, so a record fetched under
#: one session answers any caller — which is safe only because the session
#: checks in :mod:`app.api.v1.profile` happen *before* the cache is consulted.
#: A caller with no session, or a dead one, is refused before this table is
#: read, so it cannot be harvested by somebody with no working credential of
#: their own. Nothing about a session or a subject is ever written here.
CACHE_TABLE = "profile_cache"

#: Identifiers are interpolated into DDL below, which parameter binding cannot
#: do. They are module constants rather than anything caller-supplied, and this
#: pattern is what keeps them that way if someone later makes one configurable.
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
for _identifier in (SCHEMA, SESSION_TABLE, CACHE_TABLE):
    if not _IDENTIFIER_RE.match(_identifier):
        raise ValueError(f"{_identifier!r} is not a safe SQL identifier")

#: ``schema.table``, written once so no statement can name a different one.
SESSION_RELATION = f"{SCHEMA}.{SESSION_TABLE}"
CACHE_RELATION = f"{SCHEMA}.{CACHE_TABLE}"


# --- The schema --------------------------------------------------------------

#: Every statement is ``IF NOT EXISTS``. That is the entire bootstrap contract:
#: running this on a cold volume creates the schema, running it on a warm one
#: changes nothing, and running it concurrently from two containers is safe.
#:
#: Column notes:
#:
#: * ``subject`` is the verified ``sub`` claim, and it is the PRIMARY KEY. That
#:   is what makes "one session per caller, replaced outright" a property of the
#:   schema rather than of the code that writes it.
#: * ``ciphertext`` is ``bytea``, never ``text``. A Fernet token is printable
#:   base64, so ``text`` would work — and would make a ``select *`` in psql
#:   print something that *looks* like it might be readable. Hex-rendered bytea
#:   makes the at-rest verification honest at a glance.
#: * ``last_used_at`` / ``last_use_ok`` are nullable and start NULL, meaning
#:   "stored, never used yet". Distinct from "used and it failed", which is what
#:   ``GET /api/v1/session`` has to be able to say.
#: * ``public_id`` is the PRIMARY KEY of the cache table, which is what makes
#:   "one record per profile, replaced by every successful fetch" a property of
#:   the schema. It is already lower-cased by ``parse_profile_url``, so there is
#:   no second normalisation here to drift away from that one.
#: * ``body`` is ``text`` holding the serialised response envelope, NOT
#:   ``jsonb``. Two reasons, and both are about the story's central promise that
#:   a record is served exactly as it was stored. ``jsonb`` reorders keys and
#:   collapses duplicates, so a stale response would come back reshaped; and
#:   ``jsonb`` cannot hold a ``\\u0000`` escape at all, so one NUL anywhere in a
#:   member's "about" text would turn every cache write for that profile into a
#:   logged failure. ``text`` round-trips byte for byte. An operator who wants
#:   to query into it can still ``select body::jsonb -> 'profile'``.
#: * ``fetched_at`` is when LinkedIn was actually read — never when the row was
#:   written. It is the caller's only staleness signal, so it is stored as its
#:   own ``timestamptz`` column rather than only inside ``body``: that is what
#:   makes the psql check in the story's Verification block possible. There is
#:   deliberately no second "written at" timestamp; a column nothing reads is an
#:   invitation to serve the wrong one.
#:
#: * ``envelope_version`` records which response shape the stored document was
#:   written in. Because records never expire, a body written before an envelope
#:   change would otherwise be republished verbatim for ever — a reviewer seeded
#:   one with no ``partial`` key and got a 200 without it, while
#:   ``response-schema.md`` says that key is always present precisely so that
#:   "empty" and "predates the field" stay distinguishable. A row whose version
#:   is not the current one is treated as **absent**, never deleted: unbounded is
#:   the decision, and ignoring a row is not evicting it. See
#:   :data:`app.cache.ENVELOPE_VERSION`.
#:
#: There is no TTL column, no expiry index and no eviction, by decision — see
#: :mod:`app.cache`. Do not add one.
#:
#: **Adding a column later is not free.** These are ``CREATE TABLE IF NOT
#: EXISTS``, so a table that already exists is left exactly as it is: a new
#: column in this DDL would simply never appear on a warm volume, and every
#: statement naming it would fail — silently, on the cache path. Until story 10
#: brings a migration tool, a column added here must also be added by an
#: ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` statement in this tuple. Nothing
#: needed one yet because both tables are still new.
BOOTSTRAP_STATEMENTS: tuple[str, ...] = (
    f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
    f"""
    CREATE TABLE IF NOT EXISTS {SESSION_RELATION} (
        subject      text        PRIMARY KEY,
        ciphertext   bytea       NOT NULL,
        stored_at    timestamptz NOT NULL,
        last_used_at timestamptz,
        last_use_ok  boolean
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {CACHE_RELATION} (
        public_id        text        PRIMARY KEY,
        body             text        NOT NULL,
        envelope_version integer     NOT NULL,
        fetched_at       timestamptz NOT NULL
    )
    """,
)

#: Arbitrary but fixed. Two API containers starting at the same moment against
#: one database is the ordinary case behind a load balancer, and ``CREATE
#: SCHEMA IF NOT EXISTS`` is NOT safe under concurrency despite how it reads:
#: two sessions can both pass the existence check and the loser raises
#: ``UniqueViolation`` on ``pg_namespace``. The retry loop below would absorb
#: that, but the log would then read as a Postgres outage — an operator sent to
#: investigate the wrong thing.
#:
#: ``pg_advisory_xact_lock`` takes the lock for the duration of the surrounding
#: transaction and releases it on commit or rollback, so there is no unlock to
#: forget and a crashed container cannot leave it held. Held only across the
#: DDL, which is milliseconds.
BOOTSTRAP_LOCK_ID = 0x1EA5_5E55_1043_0001

BOOTSTRAP_LOCK_SQL = "SELECT pg_advisory_xact_lock(%s)"


# --- Connections -------------------------------------------------------------

#: Seconds to wait for a TCP connection. Postgres is a service on the compose
#: network; if it does not answer promptly it is down, not slow.
CONNECT_TIMEOUT_SECONDS = 10

#: Shows up in ``pg_stat_activity``, so an operator can tell this application's
#: connections from Keycloak's in the same database.
APPLICATION_NAME = "linkedin-profile-api"

#: Bootstrap retries. ``depends_on: service_healthy`` already means Postgres is
#: accepting connections before this container starts, so these exist for the
#: narrow window where it is restarting rather than for a long outage.
BOOTSTRAP_ATTEMPTS = 10
BOOTSTRAP_RETRY_SECONDS = 2.0


def connect() -> psycopg.Connection:
    """Open one connection.

    Connection-per-operation rather than a pool. This service answers a handful
    of requests during an evaluation, ``psycopg_pool`` is a separate package the
    story's boundaries put behind "Ask First", and a pool that is wrong is worse
    than no pool at all. If load ever justifies one, it goes here and nothing
    above this module changes.

    The DSN is ``DATABASE_URL``, which inside compose is composed from the
    ``POSTGRES_*`` parts by the ``api`` service — so it cannot drift from the
    credentials Postgres was actually initialised with.
    """
    return psycopg.connect(
        # `.get_secret_value()` because the DSN embeds the Postgres password —
        # see `RequiredSecret` in app/config.py. This is the only call site, and
        # the explicit accessor is what makes that greppable.
        settings.database_url.get_secret_value(),
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        application_name=APPLICATION_NAME,
    )


#: Server-side ceiling on any single cache statement, in milliseconds.
#:
#: The cache is the one caller in this codebase whose failures are **swallowed**
#: on purpose, which is exactly why it is the one that must not be able to hang.
#: Everywhere else a wedged statement surfaces to the caller as a typed 503; on
#: the cache path it would instead hold the request open — after the upstream
#: failure has already been decided, or after a correct 200 body has already
#: been built — and occupy a thread from the default executor that
#: ``vault.unlock`` shares, so one stuck Postgres backend starves requests that
#: have nothing to do with the cache.
#:
#: ``asyncio.timeout`` around the ``to_thread`` call frees the *request*, but it
#: cannot cancel work already running in a thread. This is the half that frees
#: the *thread*: Postgres aborts the statement itself, the driver returns, and
#: the worker goes back to the pool. Both halves are needed, and neither is
#: sufficient alone.
#:
#: Generous relative to the work — every cache statement is a single-row lookup
#: or upsert on a primary key — so it can only fire on a database that has
#: stopped answering, never on one that is merely busy.
CACHE_STATEMENT_TIMEOUT_MS = 5_000

#: The cache's connect timeout, deliberately shorter than the session store's.
#: A caller waiting on the session vault has no answer without it; a caller
#: waiting on the cache either already has their profile or already has their
#: error, so a slow connection there is pure added latency on a request whose
#: outcome is settled.
CACHE_CONNECT_TIMEOUT_SECONDS = 5


def connect_for_cache() -> psycopg.Connection:
    """Open one connection for the response cache, with both timeouts set.

    Separate from :func:`connect` rather than changing it, because the two have
    genuinely different failure contracts. A statement timeout on the session
    store would also apply to :func:`bootstrap`, whose DDL waits on an advisory
    lock that a second starting container may legitimately hold — turning a
    normal startup race into a boot failure. The cache runs no DDL and no lock,
    so it can carry a ceiling the rest of the module must not.
    """
    return psycopg.connect(
        settings.database_url.get_secret_value(),
        connect_timeout=CACHE_CONNECT_TIMEOUT_SECONDS,
        application_name=APPLICATION_NAME,
        # libpq passes this through to the backend as a session GUC, so the
        # ceiling is enforced by Postgres rather than by anything here choosing
        # to give up. `-c` options are server settings, not shell arguments.
        options=f"-c statement_timeout={CACHE_STATEMENT_TIMEOUT_MS}",
    )


def bootstrap(
    *,
    attempts: int = BOOTSTRAP_ATTEMPTS,
    retry_seconds: float = BOOTSTRAP_RETRY_SECONDS,
    connect_fn: Callable[[], Any] | None = None,
) -> None:
    """Create the schema if it is not there. Safe on every start.

    Called from the application lifespan in :mod:`app.main`, so a cold volume
    comes up unattended — which is exactly what
    ``docker compose down -v && docker compose up -d --wait`` has to prove.

    Failure is fatal on purpose. An API container that boots without its schema
    reports healthy (``/health`` checks no dependencies, by story-1 decision)
    and then fails every session request, with the cause nowhere near the
    symptom. Dying here puts the reason in ``docker compose logs api`` and keeps
    the container out of the healthy set.

    Concurrency-safe by the advisory lock, NOT by ``IF NOT EXISTS`` — see
    :data:`BOOTSTRAP_LOCK_ID` for why the two are not the same thing.

    ``connect_fn`` is the same seam :class:`PostgresSessionStore` has, and for
    the same reason: without it nothing can observe what this function executes,
    and the live check in ``tests/test_postgres_live.py`` cannot point it at a
    real database — the offline suite deliberately configures a ``DATABASE_URL``
    that resolves nowhere. Defaulted at call time, not at definition time, so
    the shipping path is always the module's own :func:`connect`.
    """
    open_connection = connect_fn or connect
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with open_connection() as connection:
                with connection.cursor() as cursor:
                    # First, and in the same transaction as the DDL: a lock
                    # taken in a different transaction protects nothing.
                    cursor.execute(BOOTSTRAP_LOCK_SQL, (BOOTSTRAP_LOCK_ID,))
                    for statement in BOOTSTRAP_STATEMENTS:
                        cursor.execute(statement)  # type: ignore[arg-type]
            logger.info(
                "Schema %s is present (bootstrap attempt %d)", SCHEMA, attempt
            )
            return
        except psycopg.Error as exc:
            last_error = exc
            logger.warning(
                "Schema bootstrap attempt %d/%d failed: %s",
                attempt, attempts, type(exc).__name__,
            )
            if attempt < attempts:
                time.sleep(retry_seconds)

    raise RuntimeError(
        f"could not create the {SCHEMA} schema after {attempts} attempts"
    ) from last_error


# --- The session store -------------------------------------------------------


@dataclass(frozen=True)
class SessionRow:
    """One stored row, exactly as the table holds it.

    ``ciphertext`` is the only thing here that came from the caller, and it is
    encrypted. Nothing on this object is plaintext, which is why it is safe for
    this dataclass to have an ordinary ``repr``.
    """

    subject: str
    ciphertext: bytes
    stored_at: datetime
    last_used_at: datetime | None
    last_use_ok: bool | None


class DatastoreUnavailable(Exception):
    """The database could not be reached or refused the statement.

    Raised in place of ``psycopg.Error`` so that psycopg stays confined to this
    module and every layer above it can catch one named thing. Rendered as a
    typed 503 by :func:`app.errors.datastore_unavailable_handler` — never as a
    naked 500, which is what CAP-6 forbids, and never as a taxonomy code, since
    ``response-schema.md`` has no row for "our own datastore is down".
    """


class SessionStore(Protocol):
    """What :class:`app.vault.SessionVault` needs from a datastore.

    Declared as a Protocol so the vault's own tests can run against an
    in-memory implementation with no Postgres and no network — the suite must
    be able to fail on a laptop with the stack down, and
    ``docker run --network none`` is a verification command for this story.

    Note what is *not* in this interface: nothing takes or returns a plaintext
    cookie. A store implementation cannot see one even by accident.
    """

    def upsert(self, subject: str, ciphertext: bytes) -> SessionRow: ...

    def fetch(self, subject: str) -> SessionRow | None: ...

    def mark_use(
        self, subject: str, *, ok: bool, at: datetime, stored_at: datetime
    ) -> SessionRow | None: ...


_COLUMNS = "subject, ciphertext, stored_at, last_used_at, last_use_ok"

#: ``PUT`` replaces outright, and the replacement resets the use tracking.
#: Keeping ``last_use_ok`` across an overwrite would report the *previous*
#: cookie's outcome about the new one — which is the one thing a caller who has
#: just supplied a fresh session must not be told.
_UPSERT_SQL = f"""
INSERT INTO {SESSION_RELATION} (subject, ciphertext, stored_at, last_used_at, last_use_ok)
VALUES (%s, %s, %s, NULL, NULL)
ON CONFLICT (subject) DO UPDATE
   SET ciphertext   = EXCLUDED.ciphertext,
       stored_at    = EXCLUDED.stored_at,
       last_used_at = NULL,
       last_use_ok  = NULL
RETURNING {_COLUMNS}
"""

#: Keyed on the subject alone. There is no query in this module that can return
#: another subject's row, which is how CAP-4's isolation is enforced at the
#: bottom of the stack rather than only at the top.
_FETCH_SQL = f"SELECT {_COLUMNS} FROM {SESSION_RELATION} WHERE subject = %s"

#: Scoped to the exact ``stored_at`` the use was performed under, not to the
#: subject alone. Without the second predicate a slow verification can land
#: after a concurrent ``PUT`` replaced the row and stamp the OLD cookie's
#: verdict onto the NEW one — telling a caller who has just fixed their session
#: that the session they just supplied does not work. Matching on ``stored_at``
#: makes a late write a no-op instead.
#: ``RETURNING`` so the caller learns two things at once: the row as it now
#: stands (which is what ``PUT`` answers with), and — by getting ``None`` —
#: that the update matched nothing because a concurrent ``PUT`` had already
#: replaced the row.
_MARK_USE_SQL = f"""
UPDATE {SESSION_RELATION}
   SET last_used_at = %s, last_use_ok = %s
 WHERE subject = %s AND stored_at = %s
RETURNING {_COLUMNS}
"""


class PostgresSessionStore:
    """:class:`SessionStore` backed by the table :func:`bootstrap` creates.

    ``connect`` is injectable for one reason, and it is not decoration: without
    it nothing in an offline suite can observe what this class actually
    *executes*. A test that asserts a substring of ``_FETCH_SQL`` passes just as
    happily when the executed call drops its ``WHERE`` clause — which is one
    caller reading another caller's stored session, CAP-4 exactly inverted. The
    seam lets ``tests/test_vault.py`` record every ``(sql, params)`` pair this
    class hands the driver and assert on those.

    Every ``psycopg.Error`` is re-raised as :class:`DatastoreUnavailable`, so
    psycopg does not leak upward and a database failure during a request becomes
    a typed 503 rather than an unhandled 500.
    """

    def __init__(self, connect_fn: Callable[[], Any] = connect) -> None:
        self._connect = connect_fn

    def upsert(self, subject: str, ciphertext: bytes) -> SessionRow:
        stored_at = datetime.now(timezone.utc)
        with self._guarded() as cursor:
            cursor.execute(_UPSERT_SQL, (subject, ciphertext, stored_at))
            row = cursor.fetchone()
        if row is None:  # pragma: no cover - RETURNING on an upsert always yields
            raise DatastoreUnavailable("upsert returned no row")
        return _to_row(row)

    def fetch(self, subject: str) -> SessionRow | None:
        with self._guarded() as cursor:
            cursor.execute(_FETCH_SQL, (subject,))
            row = cursor.fetchone()
        return _to_row(row) if row is not None else None

    def mark_use(
        self, subject: str, *, ok: bool, at: datetime, stored_at: datetime
    ) -> SessionRow | None:
        with self._guarded() as cursor:
            cursor.execute(_MARK_USE_SQL, (at, ok, subject, stored_at))
            row = cursor.fetchone()
        return _to_row(row) if row is not None else None

    def _guarded(self) -> Any:
        """One connection, one cursor, and psycopg errors renamed on the way out."""
        return _guarded_cursor(self._connect)


@contextmanager
def _guarded_cursor(connect_fn: Callable[[], Any]) -> Iterator[Any]:
    """One connection, one cursor, and psycopg errors renamed on the way out.

    Shared by both stores in this module rather than written twice. The two
    tables have nothing in common, but the failure contract does: every
    ``psycopg.Error`` becomes :class:`DatastoreUnavailable`, so the driver never
    leaks upward and a database fault is a typed 503 rather than a naked 500.
    """
    try:
        with connect_fn() as connection:
            with connection.cursor() as cursor:
                yield cursor
    except psycopg.Error as exc:
        # The message is logged, never returned: a psycopg error can quote the
        # statement, and the statement for a session upsert carries the
        # ciphertext parameter.
        logger.error("Datastore operation failed: %s: %s", type(exc).__name__, exc)
        raise DatastoreUnavailable(type(exc).__name__) from exc


def _to_row(row: tuple) -> SessionRow:
    subject, ciphertext, stored_at, last_used_at, last_use_ok = row
    # `bytes(...)`: psycopg returns `bytes` for bytea today, but a memoryview
    # would silently break the `not in` substring checks the leak tests rely on.
    return SessionRow(
        subject=subject,
        ciphertext=bytes(ciphertext),
        stored_at=stored_at,
        last_used_at=last_used_at,
        last_use_ok=last_use_ok,
    )


# --- The response cache store (story 7) --------------------------------------


@dataclass(frozen=True)
class CacheRow:
    """One cached profile, exactly as the table holds it.

    ``body`` is an opaque string here on purpose. This module stores and returns
    the characters it was given and never parses them; :mod:`app.cache` is the
    only thing that knows they are a JSON response envelope. That is what makes
    "a record is served exactly as it was stored" checkable at this layer: there
    is no code path in this module that could reshape one.

    Nothing on this object is a secret — it is public profile data plus the time
    it was read — so an ordinary ``repr`` is safe.
    """

    public_id: str
    body: str
    envelope_version: int
    fetched_at: datetime


class ProfileCacheStore(Protocol):
    """What :class:`app.cache.ProfileCache` needs from a datastore.

    A Protocol for the same reason :class:`SessionStore` is one: the whole
    stale-serve matrix has to be provable with no Postgres and no network, since
    ``docker run --network none`` is a verification command for this story.

    Note what is *not* here: no delete, no expiry, no sweep. Stale-serve is
    unbounded by decision, and an interface with no way to remove a record is
    how that decision survives someone later "tidying up" — see
    :mod:`app.cache`. The implementation must not grow one either; a Protocol
    that omits a method proves nothing about the class behind it, which is why
    ``tests/test_cache.py`` asserts against :class:`PostgresProfileCacheStore`.
    """

    def save(
        self, public_id: str, body: str, envelope_version: int, fetched_at: datetime
    ) -> CacheRow | None: ...

    def load(self, public_id: str) -> CacheRow | None: ...


_CACHE_COLUMNS = "public_id, body, envelope_version, fetched_at"

#: Every successful live retrieval replaces the record — unless a **newer** one
#: already stands.
#:
#: The ``WHERE`` on the ``DO UPDATE`` is the whole of that qualifier and it is
#: not hypothetical: two concurrent requests for the same profile finish in
#: whatever order LinkedIn answers them, not in the order they started, so a
#: plain ``DO UPDATE`` lets the slower fetch's older body overwrite the faster
#: fetch's newer one. The record is defined as *the last good one*, and moving
#: it backwards in time would publish a ``fetched_at`` that goes down.
#:
#: ``<=`` rather than ``<`` so that re-storing an identical fetch is still a
#: write; only a strictly older one is refused, and a refusal returns no row.
#: There is no history and no second version: the contract offers exactly one
#: alternative to a live answer, so keeping older bodies would be storing
#: something nothing can ever return.
_CACHE_SAVE_SQL = f"""
INSERT INTO {CACHE_RELATION} (public_id, body, envelope_version, fetched_at)
VALUES (%s, %s, %s, %s)
ON CONFLICT (public_id) DO UPDATE
   SET body             = EXCLUDED.body,
       envelope_version = EXCLUDED.envelope_version,
       fetched_at       = EXCLUDED.fetched_at
 WHERE {CACHE_TABLE}.fetched_at <= EXCLUDED.fetched_at
RETURNING {_CACHE_COLUMNS}
"""

#: Keyed on the public id alone, with no age predicate anywhere in it. A record
#: of any age is served in preference to a retryable error, so a ``WHERE
#: fetched_at > ...`` here would silently reintroduce the TTL the spec rules
#: out.
_CACHE_LOAD_SQL = f"SELECT {_CACHE_COLUMNS} FROM {CACHE_RELATION} WHERE public_id = %s"

#: Every statement this module runs against the cache table. Enumerated so that
#: ``tests/test_cache.py`` can check *all* of them — for an age bound, for the
#: relation they name, and for columns that exist — rather than whichever one a
#: test author happened to think of. A new statement that is not added here is
#: a statement nothing checks.
CACHE_STATEMENTS: tuple[str, ...] = (_CACHE_SAVE_SQL, _CACHE_LOAD_SQL)


class PostgresProfileCacheStore:
    """:class:`ProfileCacheStore` backed by the table :func:`bootstrap` creates.

    ``connect_fn`` is injectable for exactly the reason it is on
    :class:`PostgresSessionStore`: a test that asserts a substring of
    ``_CACHE_LOAD_SQL`` passes just as happily when the executed statement drops
    its ``WHERE`` clause — which here means answering one member's request with
    whichever profile Postgres hands back first. The seam lets the offline suite
    record every ``(sql, params)`` pair this class hands the driver.

    It defaults to :func:`connect_for_cache`, not :func:`connect`: see that
    function for why the cache is the one path in this module that carries a
    statement timeout.
    """

    def __init__(self, connect_fn: Callable[[], Any] = connect_for_cache) -> None:
        self._connect = connect_fn

    def save(
        self, public_id: str, body: str, envelope_version: int, fetched_at: datetime
    ) -> CacheRow | None:
        """Store the record, or return ``None`` if a newer one already stands.

        ``None`` is a success, not a failure: it means a concurrent request for
        the same profile got a fresher answer in first, and the row was left
        holding that one. See :data:`_CACHE_SAVE_SQL`.
        """
        with _guarded_cursor(self._connect) as cursor:
            cursor.execute(
                _CACHE_SAVE_SQL, (public_id, body, envelope_version, fetched_at)
            )
            row = cursor.fetchone()
        return _to_cache_row(row) if row is not None else None

    def load(self, public_id: str) -> CacheRow | None:
        with _guarded_cursor(self._connect) as cursor:
            cursor.execute(_CACHE_LOAD_SQL, (public_id,))
            row = cursor.fetchone()
        return _to_cache_row(row) if row is not None else None


def _to_cache_row(row: tuple) -> CacheRow:
    public_id, body, envelope_version, fetched_at = row
    return CacheRow(
        public_id=public_id,
        body=body,
        envelope_version=envelope_version,
        fetched_at=fetched_at,
    )
