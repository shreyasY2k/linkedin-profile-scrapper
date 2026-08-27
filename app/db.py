"""Postgres access, and the schema this application owns.

Two things live here and nothing else: how a connection is opened, and the
SQL the session vault runs. The vault above it (:mod:`app.vault`) knows about
ciphertext and never about Postgres; this module knows about Postgres and never
about plaintext. That split is what makes "the cookie is in plaintext in
exactly one module" a checkable claim rather than a habit.

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

#: Identifiers are interpolated into DDL below, which parameter binding cannot
#: do. They are module constants rather than anything caller-supplied, and this
#: pattern is what keeps them that way if someone later makes one configurable.
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_]*$")
for _identifier in (SCHEMA, SESSION_TABLE):
    if not _IDENTIFIER_RE.match(_identifier):
        raise ValueError(f"{_identifier!r} is not a safe SQL identifier")

#: ``schema.table``, written once so no statement can name a different one.
SESSION_RELATION = f"{SCHEMA}.{SESSION_TABLE}"


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

    @contextmanager
    def _guarded(self) -> Iterator[Any]:
        """One connection, one cursor, and psycopg errors renamed on the way out."""
        try:
            with self._connect() as connection:
                with connection.cursor() as cursor:
                    yield cursor
        except psycopg.Error as exc:
            # The message is logged, never returned: a psycopg error can quote
            # the statement, and the statement for an upsert carries the
            # ciphertext parameter.
            logger.error(
                "Datastore operation failed: %s: %s", type(exc).__name__, exc
            )
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
