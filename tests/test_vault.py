"""The story-5 edge-case matrix, as tests.

| Scenario          | Input / State                          | Expected                          |
|-------------------|----------------------------------------|-----------------------------------|
| Store a session   | valid token, well-formed cookie        | stored encrypted, value not echoed|
| Replace           | store again with a different cookie    | overwritten, no history           |
| Read presence     | valid token, session stored            | presence + last-use validity only |
| Nothing stored    | valid token, no session                | success saying so, not an error   |
| Subject isolation | B reads after A stored                 | B sees only B's own state         |
| Malformed cookie  | empty, control characters, huge        | typed 4xx, nothing stored         |
| At rest           | read the stored bytes directly         | ciphertext only                   |
| Wrong key         | row written under a rotated key        | typed failure, never a crash      |

Every test here runs against an in-memory store and needs no Postgres, no
network and no running stack — `docker run --network none` is one of the
story's verification commands. The half the fake cannot prove is that Postgres
holds bytea: that is the `psql` command in the story's Verification block, and
the boundary this file *can* prove is that the vault hands its store nothing but
ciphertext.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import psycopg
import pytest
from cryptography.fernet import Fernet

from app import db, vault as vault_module
from app.db import SessionRow
from app.errors import ApiError
from app.linkedin.client import MAX_COOKIE_LENGTH, LinkedInSession
from app.vault import InvalidEncryptionKey, SessionVault, build_cipher

#: Invented, and shaped like the real thing: a real `li_at` is ~150 opaque
#: base64-ish characters beginning `AQEDA`. Nothing here is a live credential.
#:
#: Deliberately free of long runs of one character. The substring scan below
#: looks for any eight-character window of this value inside the ciphertext, and
#: a run like `00000000` would have a (tiny, but non-zero) chance of appearing
#: in random base64 and failing the suite once a month for no reason.
COOKIE = "AQEDATestVaultCookie7f3b91d4c8e206a5bd4917e3c8f0a2b6d5417c9e0f3a8b2d6c4"
OTHER_COOKIE = "AQEDASecondVaultCookie2c8f61a0d3b47e95f1a8c204b7d63e18f5a0c9b3d72e4f16a"

SUBJECT_A = "615225e6-fb6a-4d02-a323-7b1fe4b6e88b"
SUBJECT_B = "9f2c1d84-0a77-4a15-bd0e-1c7a3f5b2e40"


class InMemoryStore:
    """A :class:`app.db.SessionStore` that keeps rows in a dict.

    Deliberately dumb, and deliberately structural rather than a Mock: the point
    of these tests is what the vault *hands* a store, so the store has to record
    exactly that and nothing else. `written` keeps every ciphertext ever passed
    in, including ones an overwrite replaced, so the at-rest assertions can scan
    the whole history rather than only the surviving row.
    """

    def __init__(self) -> None:
        self.rows: dict[str, SessionRow] = {}
        self.written: list[bytes] = []

    def upsert(self, subject: str, ciphertext: bytes) -> SessionRow:
        assert isinstance(ciphertext, bytes), "the vault must hand the store bytes"
        self.written.append(ciphertext)
        self.rows[subject] = SessionRow(
            subject=subject,
            ciphertext=ciphertext,
            stored_at=datetime.now(timezone.utc),
            last_used_at=None,
            last_use_ok=None,
        )
        return self.rows[subject]

    def fetch(self, subject: str) -> SessionRow | None:
        return self.rows.get(subject)

    def mark_use(
        self, subject: str, *, ok: bool, at: datetime, stored_at: datetime
    ) -> SessionRow | None:
        row = self.rows.get(subject)
        # Mirrors `WHERE subject = %s AND stored_at = %s`: a verdict for a row
        # that has since been replaced matches nothing and is dropped.
        if row is None or row.stored_at != stored_at:
            return None
        self.rows[subject] = SessionRow(
            subject=row.subject,
            ciphertext=row.ciphertext,
            stored_at=row.stored_at,
            last_used_at=at,
            last_use_ok=ok,
        )
        return self.rows[subject]


@pytest.fixture(name="store")
def _store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture(name="vault")
def _vault(store: InMemoryStore) -> SessionVault:
    return SessionVault(store, Fernet(Fernet.generate_key()))


# --- Matrix: store, replace, read --------------------------------------------


def test_storing_a_session_reports_presence_without_the_value(
    vault: SessionVault,
) -> None:
    state = vault.store(SUBJECT_A, COOKIE)

    assert state.stored is True
    assert state.stored_at is not None
    # Stored but never used. Three-state, and this is the third one.
    assert state.last_used_at is None
    assert state.last_use_ok is None
    assert COOKIE not in repr(state)


def test_a_stored_session_round_trips(vault: SessionVault) -> None:
    """The negative assertions everywhere else must be failing for the right reason."""
    vault.store(SUBJECT_A, COOKIE)

    session, state = vault.unlock(SUBJECT_A)

    assert session.reveal() == COOKIE
    assert state.stored is True


def test_putting_again_replaces_outright_and_keeps_no_history(
    vault: SessionVault, store: InMemoryStore
) -> None:
    """Overwrite is the entire lifecycle: no second row, no old value left."""
    vault.store(SUBJECT_A, COOKIE)
    vault.store(SUBJECT_A, OTHER_COOKIE)

    assert list(store.rows) == [SUBJECT_A]
    assert vault.unlock(SUBJECT_A)[0].reveal() == OTHER_COOKIE
    # The superseded ciphertext is gone from the row, and the surviving one does
    # not decrypt to the old value.
    assert store.rows[SUBJECT_A].ciphertext == store.written[-1]


def test_replacing_resets_the_use_tracking(vault: SessionVault) -> None:
    """A fresh cookie must not inherit the previous one's verdict.

    Reporting `last_use_ok: false` about a session the caller has only just
    supplied is the one thing a caller who has just fixed their session must not
    be told.
    """
    first = vault.store(SUBJECT_A, COOKIE)
    vault.record_use(SUBJECT_A, ok=False, stored_at=first.stored_at)
    assert vault.state(SUBJECT_A).last_use_ok is False

    vault.store(SUBJECT_A, OTHER_COOKIE)

    state = vault.state(SUBJECT_A)
    assert state.last_use_ok is None
    assert state.last_used_at is None


def test_reading_presence_reports_the_last_use_verdict(vault: SessionVault) -> None:
    stored = vault.store(SUBJECT_A, COOKIE)
    vault.record_use(SUBJECT_A, ok=True, stored_at=stored.stored_at)

    state = vault.state(SUBJECT_A)

    assert state.stored is True
    assert state.last_use_ok is True
    assert state.last_used_at is not None


def test_nothing_stored_is_a_successful_answer_not_an_error(
    vault: SessionVault,
) -> None:
    """The matrix is explicit: "no session" is a state to report, not a failure."""
    state = vault.state(SUBJECT_A)

    assert state.stored is False
    assert state.stored_at is None
    assert state.last_use_ok is None


def test_unlocking_with_nothing_stored_is_no_session(vault: SessionVault) -> None:
    """The retrieval path in stories 6-7 needs the actionable code, not None."""
    with pytest.raises(ApiError) as caught:
        vault.unlock(SUBJECT_A)

    assert caught.value.code == "NO_SESSION"
    assert caught.value.spec.status_code == 428
    assert caught.value.spec.retryable is False


def test_recording_use_for_an_unknown_subject_is_a_no_op(vault: SessionVault) -> None:
    """No row must be conjured by a write that was meant to update one."""
    assert vault.record_use(
        SUBJECT_A, ok=True, stored_at=datetime.now(timezone.utc)
    ) is None

    assert vault.state(SUBJECT_A).stored is False


# --- Matrix: subject isolation ------------------------------------------------


def test_one_subject_cannot_observe_another_subjects_session(
    vault: SessionVault,
) -> None:
    """CAP-4: whether A has a session at all is invisible to B."""
    vault.store(SUBJECT_A, COOKIE)

    assert vault.state(SUBJECT_B).stored is False
    with pytest.raises(ApiError) as caught:
        vault.unlock(SUBJECT_B)
    assert caught.value.code == "NO_SESSION"


def test_two_subjects_hold_two_independent_sessions(
    vault: SessionVault, store: InMemoryStore
) -> None:
    vault.store(SUBJECT_A, COOKIE)
    vault.store(SUBJECT_B, OTHER_COOKIE)

    assert vault.unlock(SUBJECT_A)[0].reveal() == COOKIE
    assert vault.unlock(SUBJECT_B)[0].reveal() == OTHER_COOKIE
    assert set(store.rows) == {SUBJECT_A, SUBJECT_B}


def test_storing_for_b_does_not_disturb_a(vault: SessionVault) -> None:
    """A second caller writing must not overwrite the first — the key is `sub`."""
    vault.store(SUBJECT_A, COOKIE)
    a_before = vault.state(SUBJECT_A)

    vault.store(SUBJECT_B, OTHER_COOKIE)

    assert vault.unlock(SUBJECT_A)[0].reveal() == COOKIE
    assert vault.state(SUBJECT_A).stored_at == a_before.stored_at


@pytest.mark.parametrize("subject", [None, 1, True, [], {}, "", "   "])
def test_the_vault_refuses_a_subject_that_is_not_a_usable_key(
    vault: SessionVault, store: InMemoryStore, subject: Any
) -> None:
    """Defence in depth behind `require_claims`.

    A vault that accepted `None` as a key would pool every such caller into one
    shared row, which is precisely the isolation failure CAP-4 forbids.
    """
    with pytest.raises(ApiError) as caught:
        vault.store(subject, COOKIE)

    assert caught.value.code == "UNAUTHENTICATED"
    assert not store.rows


def test_an_over_long_subject_is_refused(
    vault: SessionVault, store: InMemoryStore
) -> None:
    with pytest.raises(ApiError):
        vault.store("x" * (vault_module.MAX_SUBJECT_LENGTH + 1), COOKIE)

    assert not store.rows


# --- Matrix: malformed cookie -------------------------------------------------


@pytest.mark.parametrize(
    ("cookie", "code"),
    [
        # Empty and whitespace-only: nothing was supplied at all.
        ("", "NO_SESSION"),
        ("   ", "NO_SESSION"),
        # Present but unusable. SESSION_EXPIRED, because a session WAS supplied
        # and the remedy is to supply a different one.
        ("AQEDA\nInjected: header", "SESSION_EXPIRED"),
        ("AQEDA\r\nInjected: header", "SESSION_EXPIRED"),
        ("AQEDA\x00null", "SESSION_EXPIRED"),
        ('AQEDA"quote', "SESSION_EXPIRED"),
        ("AQEDA;second=cookie", "SESSION_EXPIRED"),
        ("A" * (MAX_COOKIE_LENGTH + 1), "SESSION_EXPIRED"),
    ],
)
def test_a_malformed_cookie_is_a_typed_4xx_and_stores_nothing(
    vault: SessionVault, store: InMemoryStore, cookie: str, code: str
) -> None:
    """Story 4's `LinkedInSession` is the validator, reused rather than reinvented."""
    with pytest.raises(ApiError) as caught:
        vault.store(SUBJECT_A, cookie)

    assert caught.value.code == code
    assert 400 <= caught.value.spec.status_code < 500
    assert not store.rows, "a rejected cookie must leave nothing behind"
    assert not store.written


def test_a_rejected_cookie_does_not_replace_an_existing_one(
    vault: SessionVault,
) -> None:
    """A bad `PUT` must not destroy the session the caller already had."""
    vault.store(SUBJECT_A, COOKIE)

    with pytest.raises(ApiError):
        vault.store(SUBJECT_A, "AQEDA\nbroken")

    assert vault.unlock(SUBJECT_A)[0].reveal() == COOKIE


def test_a_cookie_exactly_at_the_cap_is_accepted(vault: SessionVault) -> None:
    """The length rejection must be a cap, not a rejection of long cookies."""
    at_cap = "A" * MAX_COOKIE_LENGTH

    assert vault.store(SUBJECT_A, at_cap).stored is True
    assert vault.unlock(SUBJECT_A)[0].reveal() == at_cap


def test_a_rejected_cookie_is_not_named_in_the_exception(
    vault: SessionVault,
) -> None:
    """The refusal reason must never be built out of the value."""
    secret = "AQEDA-secret-but-illegal\nvalue"

    with pytest.raises(ApiError) as caught:
        vault.store(SUBJECT_A, secret)

    assert "secret-but-illegal" not in str(caught.value)
    assert "secret-but-illegal" not in (caught.value.log_detail or "")
    assert "secret-but-illegal" not in caught.value.message


# --- Matrix: at rest ----------------------------------------------------------


def test_what_reaches_the_store_is_ciphertext(
    vault: SessionVault, store: InMemoryStore
) -> None:
    """Someone reading the table without the key learns nothing."""
    vault.store(SUBJECT_A, COOKIE)

    (ciphertext,) = store.written

    assert isinstance(ciphertext, bytes)
    assert COOKIE.encode() not in ciphertext
    assert COOKIE not in ciphertext.decode("ascii", "replace")


def test_no_substring_of_the_cookie_survives_into_the_ciphertext(
    vault: SessionVault, store: InMemoryStore
) -> None:
    """Stronger than "the whole value is absent", and the criterion the story sets.

    A hypothetical encoding bug that stored, say, the first half in the clear
    would pass a whole-value check and fail this one. Eight characters is short
    enough to catch a partial leak and long enough not to fire on the base64
    alphabet by coincidence.
    """
    vault.store(SUBJECT_A, COOKIE)
    blob = store.written[0]

    window = 8
    for start in range(0, len(COOKIE) - window + 1):
        fragment = COOKIE[start : start + window].encode()
        assert fragment not in blob, COOKIE[start : start + window]


def test_the_stored_row_carries_no_plaintext_field(
    vault: SessionVault, store: InMemoryStore
) -> None:
    """The row's own `repr` reaches logs and tracebacks; it must be safe."""
    vault.store(SUBJECT_A, COOKIE)
    row = store.rows[SUBJECT_A]

    assert COOKIE not in repr(row)
    assert set(vars(row)) == {
        "subject",
        "ciphertext",
        "stored_at",
        "last_used_at",
        "last_use_ok",
    }


def test_two_stores_of_the_same_cookie_produce_different_ciphertext(
    vault: SessionVault, store: InMemoryStore
) -> None:
    """Fernet is randomised (IV + timestamp), so equal rows are not equal bytes.

    Worth pinning: a deterministic ciphertext would let anyone with table access
    tell that two subjects share a session, and would make a stored value
    testable by comparison against a guess.
    """
    vault.store(SUBJECT_A, COOKIE)
    vault.store(SUBJECT_B, COOKIE)

    assert store.written[0] != store.written[1]


def test_the_state_object_cannot_carry_the_value(vault: SessionVault) -> None:
    """The type is the guarantee, not the handler's memory."""
    vault.store(SUBJECT_A, COOKIE)

    state = vault.state(SUBJECT_A)

    assert set(vars(state)) == {"stored", "stored_at", "last_used_at", "last_use_ok"}


# --- Matrix: wrong key --------------------------------------------------------


def test_a_row_written_under_another_key_is_a_typed_failure(
    store: InMemoryStore,
) -> None:
    """Key rotated, old row still in the table. Surfaced, never swallowed."""
    SessionVault(store, Fernet(Fernet.generate_key())).store(SUBJECT_A, COOKIE)

    rotated = SessionVault(store, Fernet(Fernet.generate_key()))

    with pytest.raises(ApiError) as caught:
        rotated.unlock(SUBJECT_A)
    assert caught.value.code == "SESSION_EXPIRED"
    assert caught.value.spec.status_code == 428
    assert caught.value.spec.retryable is False, (
        "a retryable code here would let story 7 stale-serve forever over a row "
        "that can never be read"
    )


def test_reading_presence_of_an_undecryptable_row_fails_rather_than_lying(
    store: InMemoryStore,
) -> None:
    """`stored: true` about a row nothing can decrypt is worse than an error.

    It sends the caller away satisfied with a vault entry that can never be
    used, and no later request would tell them otherwise until the profile
    route failed for a reason that looks like ordinary expiry.
    """
    SessionVault(store, Fernet(Fernet.generate_key())).store(SUBJECT_A, COOKIE)

    with pytest.raises(ApiError) as caught:
        SessionVault(store, Fernet(Fernet.generate_key())).state(SUBJECT_A)

    assert caught.value.code == "SESSION_EXPIRED"


def test_garbage_in_the_ciphertext_column_is_a_typed_failure_not_a_crash(
    vault: SessionVault, store: InMemoryStore
) -> None:
    """Not every unreadable row got there by key rotation."""
    store.rows[SUBJECT_A] = SessionRow(
        subject=SUBJECT_A,
        ciphertext=b"\x00\x01not-a-fernet-token",
        stored_at=datetime.now(timezone.utc),
        last_used_at=None,
        last_use_ok=None,
    )

    with pytest.raises(ApiError) as caught:
        vault.unlock(SUBJECT_A)

    assert caught.value.code == "SESSION_EXPIRED"


def test_a_decryption_failure_says_why_in_the_log_and_not_to_the_caller(
    store: InMemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    """The caller cannot tell this from an expiry; the log is where it is stated."""
    SessionVault(store, Fernet(Fernet.generate_key())).store(SUBJECT_A, COOKIE)
    rotated = SessionVault(store, Fernet(Fernet.generate_key()))

    with caplog.at_level(logging.ERROR, logger="app.vault"):
        with pytest.raises(ApiError) as caught:
            rotated.unlock(SUBJECT_A)

    assert "SESSION_ENCRYPTION_KEY" in caplog.text
    assert COOKIE not in caplog.text
    assert "SESSION_ENCRYPTION_KEY" not in caught.value.message


# --- The encryption key -------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "test-encryption-key",
        "",
        "   ",
        "not-base64!!!",
        # 32 raw bytes base64'd would be right; 31 is not.
        Fernet.generate_key().decode()[:-4],
    ],
)
def test_a_key_that_is_not_a_fernet_key_fails_at_boot(key: str) -> None:
    """A service that cannot encrypt must not accept a cookie.

    Requiring a real Fernet key rather than stretching an arbitrary passphrase
    into one is deliberate: derivation would let a deployment that never
    replaced the `.env.example` placeholder boot happily under a key an attacker
    can guess.
    """
    with pytest.raises(InvalidEncryptionKey) as caught:
        build_cipher(key)

    assert "SESSION_ENCRYPTION_KEY" in str(caught.value)
    assert "Fernet.generate_key" in str(caught.value)


def test_the_boot_failure_never_prints_the_key() -> None:
    """It reaches stderr and `docker compose logs`, which are not private."""
    almost = "AAAAstill-not-a-valid-fernet-key-but-secret"

    with pytest.raises(InvalidEncryptionKey) as caught:
        build_cipher(almost)

    assert almost not in str(caught.value)
    assert almost not in repr(caught.value.__cause__)


def test_a_generated_key_is_accepted() -> None:
    """The rejections above must be failing for the right reason."""
    assert build_cipher(Fernet.generate_key().decode()) is not None


def test_the_shipped_placeholder_key_actually_boots() -> None:
    """`cp .env.example .env && docker compose up -d --wait` must work (CAP-7).

    Requiring a real Fernet key means the placeholder has to BE one — an
    arbitrary "change-me" string would make a clean clone die at boot, which is
    the opposite of what `.env.example` promises at the top of the file. So the
    shipped value is a valid key that decodes to a sentence telling you to
    replace it, and this test is what keeps it valid.
    """
    import base64
    import re
    from pathlib import Path

    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
        encoding="utf-8"
    )
    match = re.search(r"^SESSION_ENCRYPTION_KEY=(.+)$", example, flags=re.MULTILINE)
    assert match, ".env.example does not assign SESSION_ENCRYPTION_KEY"
    key = match.group(1).strip()

    assert build_cipher(key) is not None
    # And it is unmistakably a placeholder, not something that looks generated.
    assert b"change-me" in base64.urlsafe_b64decode(key)


def test_the_process_vault_is_built_from_the_configured_key() -> None:
    """Wiring, so nothing can quietly construct a second cipher of its own."""
    from app.config import settings

    token = build_cipher(
        settings.session_encryption_key.get_secret_value()
    ).encrypt(b"x")

    assert vault_module.cipher.decrypt(token) == b"x"


def test_importing_the_vault_is_safe_with_postgres_down() -> None:
    """Module import must open no connection: the whole suite runs offline.

    `PostgresSessionStore()` holding a connection would make every test in this
    file need a database, and would make `import app.main` fail on a laptop with
    the stack down — which is not the failure mode story 1 designed for.
    """
    assert isinstance(vault_module.vault._store, db.PostgresSessionStore)


# --- The schema bootstrap -----------------------------------------------------
#
# Asserted structurally rather than against a live Postgres: the suite must pass
# under `docker run --network none`. The live half is the `docker compose down
# -v && up -d --wait` command in the story's Verification block.


def test_every_bootstrap_statement_is_idempotent() -> None:
    """It runs on EVERY start, warm volume included."""
    assert db.BOOTSTRAP_STATEMENTS
    for statement in db.BOOTSTRAP_STATEMENTS:
        assert "IF NOT EXISTS" in statement, statement


def test_the_application_schema_is_not_public() -> None:
    """Keycloak owns `public` in this database and migrates it on every upgrade."""
    assert db.SCHEMA != "public"
    assert db.SESSION_RELATION.startswith(f"{db.SCHEMA}.")


def test_the_bootstrap_creates_the_relation_every_statement_uses() -> None:
    """A table created in one schema and queried in another fails at runtime only."""
    created = " ".join(db.BOOTSTRAP_STATEMENTS)

    assert f"CREATE SCHEMA IF NOT EXISTS {db.SCHEMA}" in created
    assert db.SESSION_RELATION in created


def test_the_ciphertext_column_is_bytea() -> None:
    """`text` would make a `select *` in psql print something base64-shaped.

    Hex-rendered bytea is what makes the at-rest verification honest at a
    glance, which matters because that verification is a human running psql.
    """
    ddl = " ".join(db.BOOTSTRAP_STATEMENTS)

    assert "ciphertext   bytea" in ddl or "ciphertext bytea" in ddl


def test_the_subject_is_the_primary_key() -> None:
    """One session per caller, replaced outright — a property of the schema."""
    ddl = " ".join(db.BOOTSTRAP_STATEMENTS)

    assert "subject" in ddl and "PRIMARY KEY" in ddl


def test_no_sql_in_this_module_can_return_another_subjects_row() -> None:
    """Isolation enforced at the bottom of the stack, not only at the top."""
    assert "WHERE subject = %s" in db._FETCH_SQL
    assert "WHERE subject = %s" in db._MARK_USE_SQL


def test_the_upsert_clears_the_previous_use_verdict() -> None:
    """The SQL, not just the in-memory fake, must reset the tracking."""
    assert "last_used_at = NULL" in db._UPSERT_SQL
    assert "last_use_ok  = NULL" in db._UPSERT_SQL


# --- The plaintext boundary ---------------------------------------------------


def test_the_unlocked_session_refuses_to_render_itself(vault: SessionVault) -> None:
    """The only object that ever holds plaintext is the one that cannot print it."""
    vault.store(SUBJECT_A, COOKIE)

    session, _ = vault.unlock(SUBJECT_A)

    assert isinstance(session, LinkedInSession)
    assert COOKIE not in repr(session)
    assert COOKIE not in str(session)
    assert COOKIE not in f"{session}"


def test_storing_a_session_logs_the_subject_and_not_the_cookie(
    vault: SessionVault, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="app.vault"):
        vault.store(SUBJECT_A, COOKIE)

    assert SUBJECT_A in caplog.text
    assert COOKIE not in caplog.text


# --- The store the shipping app actually uses ---------------------------------
#
# Everything above runs against `InMemoryStore`, which is the right way to test
# the VAULT and is worth nothing as a test of `PostgresSessionStore`. Until this
# section existed, three separate mutations of the executed SQL left the whole
# suite green:
#
#   (a) dropping `WHERE subject = %s` from the executed fetch — one caller
#       reading another caller's stored session, CAP-4 exactly inverted;
#   (b) swapping two names in `_COLUMNS` without touching `_to_row`, so a row
#       comes back with the ciphertext in the subject field;
#   (c) reordering the parameters handed to `mark_use`.
#
# `test_no_sql_in_this_module_can_return_another_subjects_row` could not catch
# (a), because it inspects the SQL *string constant* rather than what is
# executed. These tests observe the executed `(sql, params)` pairs instead, and
# `tests/test_postgres_live.py` runs the real thing against real Postgres.


class RecordingCursor:
    """A DB-API cursor that records every `(sql, params)` it is handed."""

    def __init__(self, calls: list[tuple[str, Any]], rows: list[tuple | None]) -> None:
        self.calls = calls
        self._rows = rows

    def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))

    def fetchone(self) -> tuple | None:
        return self._rows.pop(0) if self._rows else None

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class RecordingConnection:
    def __init__(self, rows: list[tuple | None] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._rows = list(rows or [])

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self.calls, self._rows)

    def __enter__(self) -> "RecordingConnection":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _row_tuple(row: SessionRow) -> tuple:
    """Build a result tuple in the order `_COLUMNS` actually asks for.

    This is what catches mutation (b). The fake answers in whatever order the
    store's own column list names, so reordering `_COLUMNS` without reordering
    `_to_row`'s unpacking makes the round-trip below return a `SessionRow` whose
    fields are swapped — and the equality assertion fails.
    """
    by_name = {
        "subject": row.subject,
        "ciphertext": row.ciphertext,
        "stored_at": row.stored_at,
        "last_used_at": row.last_used_at,
        "last_use_ok": row.last_use_ok,
    }
    return tuple(by_name[name.strip()] for name in db._COLUMNS.split(","))


def _placeholder_order(sql: str) -> list[str]:
    """Column names bound to `%s`, in the order the driver will fill them.

    Only meaningful for statements of the `name = %s` form (the UPDATE and the
    SELECT). It is what turns "the parameters are in some order" into "the
    parameters are in the order the SQL asks for", so reordering either side
    alone fails.
    """
    return re.findall(r"(\w+)\s*=\s*%s", sql)


SAMPLE_ROW = SessionRow(
    subject=SUBJECT_A,
    ciphertext=b"not-really-ciphertext",
    stored_at=datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc),
    last_used_at=None,
    last_use_ok=None,
)


def test_the_executed_fetch_filters_on_the_subject() -> None:
    """Mutation (a): the CAP-4 inversion, caught where it actually happens.

    Asserting on `db._FETCH_SQL` proves what the constant says. This proves what
    the store *runs* — which is the only thing that decides whose row comes
    back.
    """
    connection = RecordingConnection(rows=[_row_tuple(SAMPLE_ROW)])
    store = db.PostgresSessionStore(connect_fn=lambda: connection)

    store.fetch(SUBJECT_A)

    (sql, params) = connection.calls[0]
    assert _placeholder_order(sql) == ["subject"], sql
    assert params == (SUBJECT_A,)


def test_a_fetched_row_maps_back_to_the_columns_it_asked_for() -> None:
    """Mutation (b): `_COLUMNS` and `_to_row` must agree, positionally."""
    connection = RecordingConnection(rows=[_row_tuple(SAMPLE_ROW)])
    store = db.PostgresSessionStore(connect_fn=lambda: connection)

    assert store.fetch(SUBJECT_A) == SAMPLE_ROW


def test_the_executed_upsert_binds_subject_ciphertext_and_timestamp_in_order() -> None:
    connection = RecordingConnection(rows=[_row_tuple(SAMPLE_ROW)])
    store = db.PostgresSessionStore(connect_fn=lambda: connection)

    store.upsert(SUBJECT_A, b"cipher")

    (sql, params) = connection.calls[0]
    columns = re.search(r"INSERT INTO \S+ \(([^)]*)\)", sql).group(1)
    inserted = [name.strip() for name in columns.split(",")]
    # The first three columns are the ones bound to `%s`; the rest are literal
    # NULLs. Their order is the order of `params`.
    assert inserted[:3] == ["subject", "ciphertext", "stored_at"], sql
    assert params[0] == SUBJECT_A
    assert params[1] == b"cipher"
    assert isinstance(params[2], datetime)


def test_the_executed_mark_use_binds_every_parameter_where_the_sql_asks() -> None:
    """Mutation (c): reordering either the SQL or the parameters must fail."""
    at = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)
    connection = RecordingConnection(rows=[_row_tuple(SAMPLE_ROW)])
    store = db.PostgresSessionStore(connect_fn=lambda: connection)

    store.mark_use(SUBJECT_A, ok=True, at=at, stored_at=SAMPLE_ROW.stored_at)

    (sql, params) = connection.calls[0]
    expected = {
        "last_used_at": at,
        "last_use_ok": True,
        "subject": SUBJECT_A,
        "stored_at": SAMPLE_ROW.stored_at,
    }
    order = _placeholder_order(sql)
    assert set(order) == set(expected), sql
    assert params == tuple(expected[name] for name in order)


def test_every_statement_the_store_executes_names_the_subject() -> None:
    """No operation may address a row by anything but the caller's own subject."""
    connection = RecordingConnection(
        rows=[_row_tuple(SAMPLE_ROW), _row_tuple(SAMPLE_ROW), _row_tuple(SAMPLE_ROW)]
    )
    store = db.PostgresSessionStore(connect_fn=lambda: connection)

    store.upsert(SUBJECT_A, b"cipher")
    store.fetch(SUBJECT_A)
    store.mark_use(
        SUBJECT_A, ok=True, at=SAMPLE_ROW.stored_at, stored_at=SAMPLE_ROW.stored_at
    )

    assert len(connection.calls) == 3
    for sql, params in connection.calls:
        assert "subject" in sql, sql
        assert SUBJECT_A in params, sql


def test_mark_use_returns_none_when_the_row_was_replaced() -> None:
    """`UPDATE ... RETURNING` matching nothing is how the race is detected."""
    connection = RecordingConnection(rows=[None])
    store = db.PostgresSessionStore(connect_fn=lambda: connection)

    assert (
        store.mark_use(
            SUBJECT_A, ok=True, at=SAMPLE_ROW.stored_at, stored_at=SAMPLE_ROW.stored_at
        )
        is None
    )


class _Boom(psycopg.Error):
    pass


@pytest.mark.parametrize(
    "operation",
    [
        lambda store: store.fetch(SUBJECT_A),
        lambda store: store.upsert(SUBJECT_A, b"cipher"),
        lambda store: store.mark_use(
            SUBJECT_A, ok=True, at=SAMPLE_ROW.stored_at, stored_at=SAMPLE_ROW.stored_at
        ),
    ],
)
def test_a_driver_error_becomes_datastore_unavailable(operation: Any) -> None:
    """psycopg must not leak upward: every layer above catches one named thing."""

    def exploding() -> Any:
        raise _Boom("connection refused")

    store = db.PostgresSessionStore(connect_fn=exploding)

    with pytest.raises(db.DatastoreUnavailable):
        operation(store)


def test_a_driver_error_does_not_reach_the_caller_verbatim(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A psycopg message can quote the statement, and the statement has the ciphertext."""

    def exploding() -> Any:
        raise _Boom("FATAL: password authentication failed for user 'linkedin'")

    store = db.PostgresSessionStore(connect_fn=exploding)

    with caplog.at_level(logging.ERROR, logger="app.db"):
        with pytest.raises(db.DatastoreUnavailable) as caught:
            store.fetch(SUBJECT_A)

    assert "password authentication failed" not in str(caught.value)
    assert "password authentication failed" in caplog.text


# --- The bootstrap is wired into startup --------------------------------------
#
# Deleting `lifespan=lifespan` from `create_app()`, and separately deleting the
# `await asyncio.to_thread(db.bootstrap)` line inside it, each left the whole
# suite green. On a cold volume that produces a container that starts, answers
# `/health` with 200, satisfies the compose healthcheck and `up -d --wait` —
# and 500s on every session request with `UndefinedTable`.


def test_starting_the_app_creates_the_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """`TestClient` as a CONTEXT MANAGER is what runs the lifespan.

    Every other test in this suite constructs `TestClient(...)` bare, precisely
    so the lifespan does NOT fire and no test needs a database. This one opts
    in, with `db.bootstrap` replaced by a recorder.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    fired: list[bool] = []
    monkeypatch.setattr(db, "bootstrap", lambda: fired.append(True))

    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200

    assert fired == [True], (
        "the schema bootstrap did not run at startup — on a cold volume the "
        "container would come up healthy and 500 on every session request"
    )


def test_a_failing_bootstrap_aborts_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fatal on purpose: a container without its schema must not report healthy."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    def explode() -> None:
        raise RuntimeError("could not create the app schema")

    monkeypatch.setattr(db, "bootstrap", explode)

    with pytest.raises(RuntimeError, match="could not create the app schema"):
        with TestClient(create_app()):
            pass


def test_the_bootstrap_takes_the_advisory_lock_first() -> None:
    """`IF NOT EXISTS` is not concurrency-safe, whatever it reads like.

    Two sessions can both pass the existence check and the loser raises
    `UniqueViolation` on `pg_namespace`. The retry loop would absorb that, but
    the log would then read as a Postgres outage — an operator sent to
    investigate the wrong thing.
    """
    assert "pg_advisory_xact_lock" in db.BOOTSTRAP_LOCK_SQL
    # In the DDL transaction, not merely somewhere: a lock taken in another
    # transaction protects nothing.
    assert not any("pg_advisory" in s for s in db.BOOTSTRAP_STATEMENTS)


def test_the_bootstrap_locks_before_it_creates() -> None:
    """Order asserted against what is executed, not against the source."""
    connection = RecordingConnection()
    executed: list[str] = []

    class _Cursor(RecordingCursor):
        def execute(self, sql: str, params: Any = None) -> None:
            executed.append(sql)

    connection.cursor = lambda: _Cursor([], [])  # type: ignore[method-assign]

    db.bootstrap(attempts=1, retry_seconds=0, connect_fn=lambda: connection)

    assert "pg_advisory_xact_lock" in executed[0]
    assert executed[1:] == list(db.BOOTSTRAP_STATEMENTS)


# --- The ciphertext is bound to the subject it was stored under ---------------
#
# Fernet has no associated-data parameter, so its tag proves "written by someone
# holding the key" and NOT "written for this row". Without a binding, anyone who
# can write the table but cannot read the key can move A's ciphertext into B's
# row: it decrypts cleanly, the vault reports a healthy `stored: true`, and B's
# requests run under A's LinkedIn identity.


def test_a_row_transplanted_from_another_subject_is_refused(
    vault: SessionVault, store: InMemoryStore
) -> None:
    """The whole point of the binding, stated as the attack it prevents."""
    vault.store(SUBJECT_A, COOKIE)
    stolen = store.rows[SUBJECT_A]

    # Exactly what an attacker with table-write access would do: same bytes,
    # different subject. No key required.
    store.rows[SUBJECT_B] = SessionRow(
        subject=SUBJECT_B,
        ciphertext=stolen.ciphertext,
        stored_at=stolen.stored_at,
        last_used_at=None,
        last_use_ok=None,
    )

    with pytest.raises(ApiError) as caught:
        vault.unlock(SUBJECT_B)

    assert caught.value.code == "SESSION_EXPIRED"
    # And the presence read must not report it as healthy either.
    with pytest.raises(ApiError):
        vault.state(SUBJECT_B)
    # A's own row is untouched and still works.
    assert vault.unlock(SUBJECT_A)[0].reveal() == COOKIE


def test_the_bound_subject_is_inside_the_ciphertext_not_beside_it(
    vault: SessionVault, store: InMemoryStore
) -> None:
    """Encrypted with the cookie, so it cannot be edited without the key."""
    vault.store(SUBJECT_A, COOKIE)
    blob = store.written[0]

    assert SUBJECT_A.encode() not in blob
    plaintext = vault._cipher.decrypt(blob)
    assert plaintext == SUBJECT_A.encode() + vault_module.BINDING_SEPARATOR + COOKIE.encode()


def test_a_row_with_no_binding_at_all_is_refused(
    vault: SessionVault, store: InMemoryStore
) -> None:
    """A row written by an older build cannot be proven to belong where it sits."""
    store.rows[SUBJECT_A] = SessionRow(
        subject=SUBJECT_A,
        # The pre-binding format: the bare cookie, encrypted under the right key.
        ciphertext=vault._cipher.encrypt(COOKIE.encode()),
        stored_at=datetime.now(timezone.utc),
        last_used_at=None,
        last_use_ok=None,
    )

    with pytest.raises(ApiError) as caught:
        vault.unlock(SUBJECT_A)

    assert caught.value.code == "SESSION_EXPIRED"


def test_the_binding_separator_cannot_occur_in_either_half() -> None:
    """What makes `partition` unambiguous rather than merely usually right."""
    from app.auth import _SUBJECT_UNSAFE_RE
    from app.linkedin.client import _HEADER_UNSAFE_RE

    separator = vault_module.BINDING_SEPARATOR.decode("latin-1")

    assert _SUBJECT_UNSAFE_RE.search(separator), "a subject could contain the separator"
    assert _HEADER_UNSAFE_RE.search(separator), "a cookie could contain the separator"


def test_a_cookie_containing_the_separator_never_reaches_the_vault(
    vault: SessionVault, store: InMemoryStore
) -> None:
    """Belt and braces on the sentence above, through the real entry point."""
    with pytest.raises(ApiError):
        vault.store(SUBJECT_A, "AQEDA\x00smuggled")

    assert not store.rows


# --- Decryption failures are narrowly classified ------------------------------


def test_an_unexpected_error_is_not_reported_as_key_rotation(
    vault: SessionVault, store: InMemoryStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A bug must not send an operator to rotate keys.

    An earlier draft caught `ValueError` and `TypeError` here too, so a
    `memoryview` reaching `decrypt` — or a `None` in the column — was logged as
    "written under a different SESSION_ENCRYPTION_KEY". That is a code defect
    wearing an operations incident's clothes.
    """
    store.rows[SUBJECT_A] = SessionRow(
        subject=SUBJECT_A,
        ciphertext=None,  # type: ignore[arg-type]
        stored_at=datetime.now(timezone.utc),
        last_used_at=None,
        last_use_ok=None,
    )

    with caplog.at_level(logging.ERROR, logger="app.vault"):
        with pytest.raises(Exception) as caught:
            vault.unlock(SUBJECT_A)

    assert not isinstance(caught.value, ApiError), (
        "a bug in this codebase must surface as an unexpected error (a typed "
        "500), not as a session problem for the caller to act on"
    )
    assert "SESSION_ENCRYPTION_KEY" not in caplog.text


# --- The subject key is normalised once ---------------------------------------


def test_a_padded_subject_is_the_same_caller(vault: SessionVault, store: InMemoryStore) -> None:
    """`"uuid "` and `"uuid"` must not be two primary keys for one person.

    Validating the stripped value and then storing the unstripped one — which an
    earlier draft did — means a caller stores under one and reads back "no
    session stored" under the other, with nothing in any log to explain it.
    """
    vault.store(f"  {SUBJECT_A}  ", COOKIE)

    assert list(store.rows) == [SUBJECT_A]
    assert vault.state(SUBJECT_A).stored is True
    assert vault.unlock(f"{SUBJECT_A}\t")[0].reveal() == COOKIE


def test_a_subject_containing_a_control_character_is_refused(
    vault: SessionVault, store: InMemoryStore
) -> None:
    """Postgres `text` cannot hold a NUL: unchecked, this is a 500, not a 401."""
    with pytest.raises(ApiError) as caught:
        vault.store(f"{SUBJECT_A}\x00", COOKIE)

    assert caught.value.code == "UNAUTHENTICATED"
    assert not store.rows


# --- A late verdict cannot libel a fresh cookie -------------------------------


def test_a_use_verdict_for_a_replaced_session_is_discarded(
    vault: SessionVault,
) -> None:
    """The race: verification in flight while a concurrent PUT replaces the row.

    Recording the old cookie's verdict against the new one tells a caller who
    has *just fixed* their session that the session they just supplied does not
    work — the single most confusing thing this endpoint could say.
    """
    first = vault.store(SUBJECT_A, COOKIE)
    vault.store(SUBJECT_A, OTHER_COOKIE)  # the concurrent PUT wins

    assert vault.record_use(SUBJECT_A, ok=False, stored_at=first.stored_at) is None

    state = vault.state(SUBJECT_A)
    assert state.last_use_ok is None, "a stale verdict reached the new session"


def test_a_verdict_for_the_current_session_is_recorded(vault: SessionVault) -> None:
    """The guard above must be a race check, not a rejection of every write."""
    stored = vault.store(SUBJECT_A, COOKIE)

    returned = vault.record_use(SUBJECT_A, ok=True, stored_at=stored.stored_at)

    assert returned is not None
    assert returned.last_use_ok is True
    assert returned.stored_at == stored.stored_at


# --- The shipped placeholder key is loud --------------------------------------


def test_booting_on_the_shipped_placeholder_key_is_shouted_about(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """It boots, per CAP-7 — but it must never do so quietly.

    The stricter alternative (refuse to start) was considered and rejected: it
    would break `cp .env.example .env && docker compose up -d --wait` on a clean
    clone, which is an acceptance criterion. See the story's Design Notes.
    """
    with caplog.at_level(logging.CRITICAL, logger="app.vault"):
        cipher = build_cipher(vault_module.SHIPPED_PLACEHOLDER_KEY)

    assert cipher is not None, "it must still boot"
    record = next(r for r in caplog.records if r.levelno == logging.CRITICAL)
    assert "placeholder" in record.getMessage()
    assert "SESSION_ENCRYPTION_KEY" in record.getMessage()


def test_a_real_key_is_not_shouted_about(caplog: pytest.LogCaptureFixture) -> None:
    """The warning above must be failing for the right reason."""
    with caplog.at_level(logging.CRITICAL, logger="app.vault"):
        build_cipher(Fernet.generate_key().decode())

    assert not [r for r in caplog.records if r.levelno == logging.CRITICAL]


def test_the_placeholder_constant_is_the_one_env_example_ships() -> None:
    """Two copies of a literal, so editing one and not the other fails here."""
    import re as _re
    from pathlib import Path

    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text("utf-8")
    match = _re.search(r"^SESSION_ENCRYPTION_KEY=(.+)$", example, flags=_re.MULTILINE)

    assert match and match.group(1).strip() == vault_module.SHIPPED_PLACEHOLDER_KEY


# --- Configuration secrets do not render --------------------------------------


@pytest.mark.parametrize(
    "field", ["session_encryption_key", "keycloak_client_secret", "database_url"]
)
def test_a_credential_setting_is_not_rendered_by_repr_or_dump(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    """`Settings` is a module-level singleton imported at boot.

    That makes it exactly the kind of object that turns up in a traceback frame
    or a debug dump — and one of these three is the master key protecting every
    stored LinkedIn session, while another embeds the Postgres password.
    """
    from app.config import Settings

    from tests.conftest import REQUIRED_ENV

    sentinel = "MUST-NEVER-BE-PRINTED-9c1f"
    values = {name.lower(): value for name, value in REQUIRED_ENV.items()}
    if field == "session_encryption_key":
        # Has to stay a valid Fernet key, so the sentinel goes in decoded form.
        import base64

        raw = (sentinel + "x" * 32).encode()[:32]
        values[field] = base64.urlsafe_b64encode(raw).decode()
        secret = values[field]
    else:
        values[field] = f"{values.get(field, '')}{sentinel}"
        secret = values[field]

    settings = Settings(_env_file=None, **values)

    assert getattr(settings, field).get_secret_value() == secret
    assert secret not in repr(settings)
    assert secret not in str(settings)
    assert secret not in str(settings.model_dump())
