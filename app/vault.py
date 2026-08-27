"""The encrypted, per-subject LinkedIn session vault.

**This module is the only place a stored ``li_at`` exists in plaintext.** It
enters at :meth:`SessionVault.store` and leaves at :meth:`SessionVault.unlock`,
wrapped both times in :class:`~app.linkedin.client.LinkedInSession` so that even
here it cannot render itself into a log line, an f-string, a traceback frame or
a pytest assertion dump. Everything below this module sees ciphertext;
everything above it sees presence and validity.

===============================================================================
WHAT CAP-4 ACTUALLY REQUIRES
===============================================================================

*A stored session is recoverable only by the Keycloak subject that supplied
it.* The vault key is the verified ``sub`` claim and nothing else. There is no
code path here that takes a subject from a request body, a query parameter or a
header — a caller-supplied subject would let any authenticated caller read any
other caller's session, which is CAP-4 exactly inverted.

*It is unreadable in the datastore without the encryption key.* Fernet
(AES-128-CBC with an HMAC-SHA256 authentication tag) under the key in
``SESSION_ENCRYPTION_KEY``. What reaches Postgres is the Fernet token; someone
reading the table without the key learns the subject, the timestamps, and
nothing else.

*And it is bound to the subject it was stored under.* Fernet has no associated-
data parameter, so its authentication tag proves only "this ciphertext was
written by someone holding the key" — not "…for this row". Without a binding,
anyone who can WRITE the table but cannot read the key could copy A's
ciphertext into B's row: it would decrypt cleanly, the vault would report a
healthy ``stored: true``, and B's requests would run under A's LinkedIn
identity. So the subject is encrypted *inside* the plaintext
(``subject \\x00 cookie``) and checked against the row on the way out, which
turns that transplant from a silent success into a typed failure. Neither half
can contain a NUL — ``require_claims`` refuses one in ``sub`` and
:class:`LinkedInSession` refuses one in the cookie — so the separator is
unambiguous.

*Re-supplying replaces the stored value outright.* One row per subject, the
subject is the primary key, and the overwrite also clears the use tracking —
the previous cookie's outcome says nothing about the new one.

===============================================================================
WHY DECRYPTION FAILURE IS ``SESSION_EXPIRED``
===============================================================================

The edge-case matrix requires a rotated key over an old row to produce "a clear
typed failure, never a crash or a silent empty value". It is *not* a new
taxonomy code: ``response-schema.md`` fixes the table, and
``tests/test_linkedin_client.py`` pins ``ERROR_SPECS`` against a hand copy of
it, so inventing a code here would put the wire contract and the code out of
agreement.

``SESSION_EXPIRED`` is the honest row. It is 428 — "a precondition is missing
and you can fix it" — it is ``retryable: false`` so story 7 will not stale-serve
over it forever, and the remedy it states is precisely right: supply a new
session, which re-encrypts under the current key and repairs the row. The real
cause goes to the log at ERROR, where an operator can see that the key changed
rather than that a cookie died.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

from app.auth import _SUBJECT_UNSAFE_RE, MAX_SUBJECT_LENGTH
from app.config import settings
from app.db import PostgresSessionStore, SessionRow, SessionStore
from app.errors import ApiError, unauthenticated
from app.linkedin.client import LinkedInSession

logger = logging.getLogger(__name__)


#: Separates the bound subject from the cookie inside the encrypted plaintext.
#: A byte neither side can contain: ``require_claims`` refuses a control
#: character in ``sub`` and :class:`LinkedInSession` refuses one in the cookie.
BINDING_SEPARATOR = b"\x00"

#: The key shipped in ``.env.example`` so a clean clone boots (CAP-7). Every
#: clone has it, so a deployment still running on it is a deployment whose
#: stored cookies anyone with the repository and table access can read.
#: Compared against at import and shouted about — see :func:`build_cipher`.
SHIPPED_PLACEHOLDER_KEY = "Y2hhbmdlLW1lLWdlbmVyYXRlLWEtcmVhbC1rZXkhISE="


class InvalidEncryptionKey(RuntimeError):
    """``SESSION_ENCRYPTION_KEY`` is not a usable Fernet key.

    A boot-time failure, deliberately: story 1 fixed that a broken environment
    kills the process rather than the first request that needs it, and a service
    that cannot encrypt is a service that must not accept a cookie.
    """


def build_cipher(key: str) -> Fernet:
    """Build the Fernet from the configured key, or fail with a usable message.

    The key is required to be a real Fernet key rather than an arbitrary
    passphrase stretched into one. Deriving a key from ``hunter2`` would let a
    deployment that never replaced the ``.env.example`` placeholder boot happily
    with an encryption key an attacker can guess — and ``.env.example`` already
    ships the exact command that generates a real one.

    The exception message never contains the key. ``Fernet`` raises
    ``ValueError("Fernet key must be 32 url-safe base64-encoded bytes.")``,
    which is safe to surface; the value itself is not, and stderr at boot is
    captured by ``docker compose logs``.
    """
    try:
        cipher = Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise InvalidEncryptionKey(
            "SESSION_ENCRYPTION_KEY is not a valid Fernet key. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        ) from exc

    if key == SHIPPED_PLACEHOLDER_KEY:
        # CRITICAL and not fatal, and the split is deliberate. Fatal would break
        # `cp .env.example .env && docker compose up -d --wait` on a clean
        # clone, which is CAP-7 and an acceptance criterion. Silent would let a
        # deployment encrypt happily, report success, and store every caller's
        # LinkedIn session under a key printed in a public repository. So it
        # boots, and it says so at the loudest level there is, on every start.
        logger.critical(
            "SESSION_ENCRYPTION_KEY is still the placeholder shipped in "
            ".env.example. Every stored LinkedIn session is readable by anyone "
            "with this repository. Generate a real key before this is exposed "
            "to anyone: python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"'
        )
    return cipher


@dataclass(frozen=True)
class SessionState:
    """Everything ``GET /api/v1/session`` is allowed to know.

    There is deliberately no field on this object that could hold the cookie,
    in any encoding, under any flag. The route's response model is built from
    exactly these four names, so "the value is never returned" is a property of
    the type rather than of a handler remembering not to add it.

    ``last_use_ok`` has three states and all three mean something different:
    ``None`` — stored but never used; ``True`` — the last use worked;
    ``False`` — the last use was refused by LinkedIn.
    """

    stored: bool
    stored_at: datetime | None = None
    last_used_at: datetime | None = None
    last_use_ok: bool | None = None


#: What a caller with nothing stored gets. A successful answer, not an error:
#: the matrix is explicit that "no session" is a state to report, not a failure.
NOTHING_STORED = SessionState(stored=False)


class SessionVault:
    """Encrypt, store, and retrieve one LinkedIn session per Keycloak subject.

    ``store`` and ``cipher`` are constructor arguments so the whole matrix —
    including at-rest ciphertext and cross-subject isolation — is testable with
    no Postgres and no network, which is what ``docker run --network none``
    requires of this suite.
    """

    def __init__(self, store: SessionStore, cipher: Fernet) -> None:
        self._store = store
        self._cipher = cipher

    # -- Writing --------------------------------------------------------------

    def store(self, subject: str, cookie: str) -> SessionState:
        """Encrypt ``cookie`` and store it against ``subject``, replacing any row.

        Validation is :class:`LinkedInSession` — story 4's, reused rather than
        re-implemented, so the rules that decide what may be written into a
        request header are the same rules that decide what may be stored. A
        cookie that could never work is refused here, at zero LinkedIn quota,
        with the reason logged and never echoed.

        The typed 4xx comes out of ``LinkedInSession`` unchanged: ``NO_SESSION``
        for an empty value, ``SESSION_EXPIRED`` for one that is over the length
        cap or carries a character illegal in a header. Both are 428 and both
        tell the caller the same actionable thing — send a usable cookie.
        """
        key = _subject_key(subject)
        # Raises before anything is written. Nothing is stored on a bad cookie.
        session = LinkedInSession(cookie)

        # The subject is encrypted WITH the cookie, not merely stored beside it.
        # See the module docstring: without this a row copied from one subject
        # to another decrypts cleanly and is honoured.
        bound = key.encode("utf-8") + BINDING_SEPARATOR + session.reveal().encode("utf-8")
        ciphertext = self._cipher.encrypt(bound)
        row = self._store.upsert(key, ciphertext)
        logger.info("Stored a LinkedIn session for subject %s", _loggable(key))
        return _state(row)

    def record_use(
        self, subject: str, *, ok: bool, stored_at: datetime
    ) -> SessionState | None:
        """Record how the stored session fared the last time it was used.

        The writer for the validity half of ``GET /api/v1/session``: ``PUT``
        calls it with the outcome of the one cheap ``me`` check it performs, and
        the profile route in stories 6-7 calls it after LinkedIn has answered or
        refused. Nothing about the cookie itself is written — only whether it
        worked, and when.

        ``stored_at`` scopes the write to the exact row the use was performed
        against. A verification that started before a concurrent ``PUT``
        replaced the session must not stamp the old cookie's verdict onto the
        new one; with the timestamp in the predicate that late write simply
        matches nothing.

        Returns the row's new state, or ``None`` when it matched nothing —
        which means exactly that: someone replaced the session while this use
        was in flight, and the verdict belongs to a cookie that is no longer
        stored.
        """
        row = self._store.mark_use(
            _subject_key(subject),
            ok=ok,
            at=datetime.now(timezone.utc),
            stored_at=stored_at,
        )
        if row is None:
            logger.info(
                "Discarded a use verdict for subject %s: the session was "
                "replaced while it was being verified",
                _loggable(subject),
            )
            return None
        return _state(row)

    # -- Reading --------------------------------------------------------------

    def state(self, subject: str) -> SessionState:
        """Presence and last-use validity. Never the value.

        The stored row is decrypted and the plaintext immediately discarded.
        That is not wasted work: a row this process cannot decrypt is not a
        usable session, and answering ``stored: true`` about it would send the
        caller away satisfied with a vault entry that can never be used. The
        matrix's rotated-key row is surfaced here rather than swallowed.
        """
        row = self._store.fetch(_subject_key(subject))
        if row is None:
            return NOTHING_STORED
        self._decrypt(row)
        return _state(row)

    def unlock(self, subject: str) -> tuple[LinkedInSession, SessionState]:
        """The stored session itself, for the code that makes the LinkedIn call.

        The only way plaintext leaves this module, and it leaves wrapped in
        :class:`LinkedInSession` — which refuses to render itself — so the
        retrieval path in stories 6-7 physically cannot log it.

        ``NO_SESSION`` when nothing is stored: 428, and the caller's remedy is
        ``PUT /api/v1/session``.
        """
        row = self._store.fetch(_subject_key(subject))
        if row is None:
            raise ApiError(
                "NO_SESSION",
                log_detail=f"no stored session for subject {_loggable(subject)}",
            )
        return self._decrypt(row), _state(row)

    def _decrypt(self, row: SessionRow) -> LinkedInSession:
        """Ciphertext to :class:`LinkedInSession`, or a typed 428.

        Two failures are handled here and they are different things:

        *The token does not authenticate.* ``InvalidToken`` — the key was
        rotated, or the column holds something that is not a Fernet token at
        all. ``UnicodeDecodeError`` cannot happen once the tag has verified and
        is caught with it because the alternative is a naked 500.

        *The token authenticates but names a different subject.* Someone with
        write access to the table moved a row. See the module docstring.

        The except clause is deliberately NARROW. Catching ``ValueError`` and
        ``TypeError`` as well — which an earlier draft did — reports a genuine
        bug (a ``memoryview`` reaching ``decrypt``, a ``None`` in the column) as
        key rotation, and sends an operator to rotate keys for a code defect.
        Anything outside these two becomes a typed 500, which is what an
        unexpected bug should be.

        Logged at ERROR, not WARNING. A caller seeing this cannot distinguish it
        from an ordinary expiry, so the log line is the only place the real
        cause is ever stated.
        """
        try:
            plaintext = self._cipher.decrypt(row.ciphertext)
        except (InvalidToken, UnicodeDecodeError) as exc:
            logger.error(
                "Stored session for subject %s could not be decrypted (%s). The "
                "row was written under a different SESSION_ENCRYPTION_KEY; the "
                "caller must store a new session.",
                _loggable(row.subject),
                type(exc).__name__,
            )
            raise self._unreadable(
                "stored ciphertext did not authenticate under the configured key"
            ) from exc

        bound_subject, separator, cookie = plaintext.partition(BINDING_SEPARATOR)
        if not separator or bound_subject.decode("utf-8", "replace") != row.subject:
            # The ciphertext is genuine — it was produced by something holding
            # this key — but it was not produced FOR this row. Either the row
            # was transplanted, or it predates the subject binding and cannot be
            # proven to belong where it sits. Refusing costs the caller one
            # `PUT`; honouring it would run their requests under someone else's
            # LinkedIn identity.
            logger.error(
                "Stored session for subject %s is bound to a different subject "
                "(or to none at all). The row does not belong to the caller it "
                "is filed under; refusing to use it.",
                _loggable(row.subject),
            )
            raise self._unreadable("stored ciphertext is bound to a different subject")

        try:
            revealed = cookie.decode("utf-8")
        except UnicodeDecodeError as exc:  # pragma: no cover - tag already verified
            raise self._unreadable("stored plaintext is not UTF-8") from exc

        # Revalidated on the way out, not merely on the way in. A row written by
        # an older build, or by a future story, must still satisfy the header
        # rules before anything can put it in a request.
        return LinkedInSession(revealed)

    @staticmethod
    def _unreadable(detail: str) -> ApiError:
        """The one code an unusable stored row can honestly wear.

        ``SESSION_EXPIRED``, not a new code: ``response-schema.md`` fixes the
        taxonomy and ``tests/test_linkedin_client.py`` pins ``ERROR_SPECS``
        against a hand copy of it. It is also the honest row — 428, a
        precondition the caller can fix, ``retryable: false`` so story 7 cannot
        stale-serve over it forever, and the remedy it states (store a new
        session) is exactly the one that works.
        """
        return ApiError("SESSION_EXPIRED", log_detail=detail)


def _state(row: SessionRow) -> SessionState:
    return SessionState(
        stored=True,
        stored_at=row.stored_at,
        last_used_at=row.last_used_at,
        last_use_ok=row.last_use_ok,
    )


def _subject_key(subject: object) -> str:
    """The vault key, NORMALISED, or a 401.

    ``require_claims`` already guarantees a non-empty string ``sub`` of bounded
    length, so this is defence in depth rather than the primary check — but it
    is the check that is local to the thing it protects. A vault that accepted
    ``None`` as a key would silently pool every such caller into one row.

    The **stripped** value is returned, not the value that was validated. An
    earlier draft validated ``subject.strip()`` and then returned ``subject``,
    which made ``"uuid "`` and ``"uuid"`` two distinct primary keys for one
    caller: they would store a session under one and read back "no session
    stored" under the other, with nothing in any log to explain it. A key is
    normalised once, here, or it is not normalised at all.
    """
    if not isinstance(subject, str):
        raise unauthenticated(log_detail="token subject is not a string")
    normalised = subject.strip()
    if not normalised:
        raise unauthenticated(log_detail="token subject is blank")
    if len(normalised) > MAX_SUBJECT_LENGTH:
        raise unauthenticated(
            log_detail=f"token subject is {len(normalised)} characters, over the cap"
        )
    if _SUBJECT_UNSAFE_RE.search(normalised):
        # Postgres `text` cannot hold a NUL, so this would be a 500 on the write
        # rather than a 401 on the request.
        raise unauthenticated(log_detail="token subject contains a control character")
    return normalised


def _loggable(subject: str) -> str:
    """A subject in a log line. Not a secret, but not a free-form string either."""
    rendered = repr(subject)
    return rendered if len(rendered) <= 80 else rendered[:80] + "...'"


#: Process-wide instances. Constructing the store opens no connection, so import
#: is safe with Postgres down; constructing the cipher validates the key, so an
#: unusable ``SESSION_ENCRYPTION_KEY`` kills the process at boot with the
#: variable named — the same contract every other setting already has.
cipher = build_cipher(settings.session_encryption_key.get_secret_value())
vault = SessionVault(PostgresSessionStore(), cipher)
