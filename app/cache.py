"""The response cache, and the one rule that decides when a stale record wins.

CAP-5: *the API keeps answering for previously seen profiles after LinkedIn
stops answering.* Every successful retrieval is persisted here, and when a live
retrieval fails for a reason that retrying could fix, the last good record is
returned with ``stale: true`` and its **original** ``fetched_at`` instead of the
error.

This module is to :mod:`app.db` what :mod:`app.vault` is: the layer that knows
what the bytes mean. :mod:`app.db` stores an opaque text document against a
public id; this module knows that document is a response envelope, serialises
it on the way in, and parses and validates it on the way out.

===============================================================================
THE RULE IS ONE LINE, AND ITS INPUT ALREADY EXISTS
===============================================================================

*Fall back only when the failure is retryable and a usable record exists.*

``retryable`` is read off :data:`~app.errors.ERROR_SPECS` — the table
transcribed from ``response-schema.md`` — rather than being a second list of
codes kept here. A second list is a thing that drifts: story 8 owns the
taxonomy, and if it reclassifies a code, this module has to follow it without
being edited. :meth:`ProfileCache.fallback_for` is the **only** public way to
read this cache, so that gate cannot be walked around by calling something else.

The consequence is what makes this safe. ``NO_SESSION``, ``SESSION_EXPIRED``,
``UNAUTHENTICATED``, ``INVALID_URL`` and ``PROFILE_NOT_FOUND`` are all
``retryable: false``, so **none of them can ever be answered from this cache**.
Hiding an expired session behind cached data is the precise failure this project
has already had to fix once: the caller would be told their profile request
succeeded, forever, while their credential was dead and nothing in the response
said so.

(One honest qualification, verified live and recorded in the story's change log:
LinkedIn does not always *state* that refusal. A dead cookie whose authwall
arrives as a 200 redirect is classified ``UPSTREAM_CHALLENGE`` — retryable — and
is stale-served like any other challenge, because that page is indistinguishable
from the one a datacenter IP draws with a perfectly good session. The gate below
is exact; what feeds it is not.)

===============================================================================
UNBOUNDED, BY DECISION — AND WHAT THAT OBLIGES
===============================================================================

There is no TTL, no eviction, no age limit and no background refresh, and their
absence is a decision recorded in ``SPEC.md`` rather than something nobody got
to. This service is graded on whether it still answers at an arbitrary later
time. A record of any age, flagged ``stale: true`` and carrying the timestamp it
was actually fetched, is more useful to a caller than a 502 — and ``fetched_at``
gives them everything they need to judge it for themselves.

That reasoning holds **because the flag and the timestamp are honest**. It
collapses the moment a stale response looks fresh, which is why
:func:`_as_stale_body` sets ``stale`` and touches nothing else.

But "never removed" also means "outlives every later deploy", and that obliges
this module to check what it is about to republish. A body written under an
older envelope shape is not a slightly-old answer, it is a *wrongly shaped* one:
a reviewer seeded a record with no ``partial`` key and got a 200 without it,
while ``response-schema.md`` says that key is always present precisely so that
"nothing in it" and "this response predates the field" stay distinguishable.
:data:`ENVELOPE_VERSION` and :data:`REQUIRED_ENVELOPE_KEYS` are the two guards
against that, and both treat a bad row as **absent** — never as something to
delete. Ignoring a row is not evicting it, and the distinction matters: the
author's decision was that nothing here removes data.

===============================================================================
FAILURE IS SWALLOWED, WHICH IS EXACTLY WHY IT MUST BE LOUD
===============================================================================

Neither direction can raise. :meth:`ProfileCache.remember` cannot, because the
profile was retrieved and mapped successfully and failing that request over a
bookkeeping write would turn a working service into a broken one every time
Postgres hiccups. :meth:`ProfileCache.fallback_for` cannot, because a cache
lookup that throws must not replace the caller's real ``RATE_LIMITED`` with a
503 about *this* service's datastore.

That swallowing is what makes a broken cache invisible — five separate mutations
to the SQL and DDL each left the whole suite green and shipped as "the cache
silently never writes and never serves". Everywhere else in this codebase a bad
statement surfaces to the caller as a typed 503; here nothing surfaces at all.
So two things are non-negotiable:

* **Every swallowed datastore failure logs at ERROR**, naming CAP-5, so a
  cache that has never once worked cannot be mistaken in a log for a cache that
  simply has nothing in it yet.
* **Every outcome is reported distinctly.** :class:`Fallback` carries a
  :attr:`~Fallback.reason`, and "there is no record for this profile" and "the
  datastore could not be consulted" are different values of it. They are the
  same 502 to the caller and must never be the same line to an operator.

The compile-time half of the same problem lives in ``tests/test_cache.py``,
which resolves every statement in :data:`app.db.CACHE_STATEMENTS` against the
schema ``bootstrap`` actually creates — with no Postgres, in the default suite.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from app.db import CacheRow, PostgresProfileCacheStore, ProfileCacheStore
from app.errors import ApiError

logger = logging.getLogger(__name__)


#: The envelope key that says where the profile came from. ``False`` on a live
#: retrieval, ``True`` on a record served from this cache. It is the ONLY key
#: :func:`_as_stale_body` changes.
STALE_KEY = "stale"

#: The envelope key naming the profile the record is about. Cross-checked on the
#: way out against the id that was **requested** — see :func:`_as_stale_body`.
PUBLIC_ID_KEY = "public_id"

#: The response shape stored records are written in.
#:
#: **Bump this whenever the envelope's shape changes.** Records are never
#: evicted, so without a version a body written under an older shape is
#: republished verbatim for ever; with one, it is simply invisible to
#: :meth:`ProfileCache.fallback_for` and the next live fetch replaces it.
#:
#: ``tests/test_cache.py`` pins this against :data:`REQUIRED_ENVELOPE_KEYS`, so
#: changing the envelope without bumping the version fails a test rather than
#: quietly serving old rows.
ENVELOPE_VERSION = 1

#: Every top-level key ``response-schema.md`` fixes for the success envelope. A
#: stored body missing any of them — or carrying one it does not name — is not a
#: response this service may republish, whatever else is right about it.
REQUIRED_ENVELOPE_KEYS = frozenset(
    {"url", "public_id", "stale", "fetched_at", "partial", "profile"}
)


# --- What the cache could offer -----------------------------------------------

#: A record was found and is being served.
SERVED = "served"
#: The failure was not retryable, so the cache was never consulted.
NOT_RETRYABLE = "not-retryable"
#: The cache was consulted and holds nothing for this profile.
NO_RECORD = "no-record"
#: The datastore could not be read. **Not** the same thing as NO_RECORD.
DATASTORE_UNAVAILABLE = "datastore-unavailable"
#: A row exists but cannot be published — unparseable, wrongly shaped, written
#: under an older envelope version, or filed against a different member.
UNUSABLE_RECORD = "unusable-record"


@dataclass(frozen=True)
class Fallback:
    """What the cache could offer for one failed retrieval, and why.

    ``body is None`` is the only thing the route branches on; ``reason`` exists
    so that four operationally different situations are four different log
    lines. A cache that is broken and a cache that is empty produce the same
    502 for the caller and must never produce the same line for an operator —
    see this module's docstring.
    """

    reason: str
    body: dict[str, Any] | None = None


class ProfileCache:
    """Persist a response envelope per profile, and serve it when live fails.

    ``store`` is a constructor argument so the whole matrix — including the
    non-retryable codes that must *not* be served from here — is testable with
    no Postgres and no network.
    """

    def __init__(self, store: ProfileCacheStore) -> None:
        self._store = store

    # -- Writing --------------------------------------------------------------

    def remember(
        self, public_id: str, body: Mapping[str, Any], fetched_at: datetime
    ) -> bool:
        """Store ``body`` as the last good record for ``public_id``.

        **This never raises.** It returns whether the datastore now holds a
        record this write is happy with; the profile route ignores the answer,
        because there is nothing it could usefully do with it, but a caller that
        wants to assert on it can.

        ``fetched_at`` is passed separately rather than parsed back out of
        ``body`` — the column exists so an operator can read the retrieval time
        in ``psql`` without decoding a JSON document — and is **normalised to
        UTC** here. Storing a non-UTC or naive value would leave the column and
        the body's own ``fetched_at`` string reporting different times, so the
        psql staleness check documented in the README would disagree with the
        response it is checking.

        The body is serialised here and stored as text, so what comes back out
        is the same document, key order included.
        """
        moment = _utc(fetched_at)
        try:
            document = json.dumps(body, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            # A body that cannot be serialised is a bug in the mapper, not a
            # datastore fault, and it is worth a traceback in the log. It still
            # does not fail the request: the caller already has their profile.
            logger.exception(
                "CAP-5 degraded: could not serialise the response for %r, so it "
                "will not be cached and no stale answer will exist for it",
                public_id,
            )
            return False

        try:
            row = self._store.save(public_id, document, ENVELOPE_VERSION, moment)
        except Exception:
            # Deliberately wide. `DatastoreUnavailable` is the expected one, but
            # the contract here is "writing to the cache never fails a request
            # that otherwise succeeded", and narrowing it to the exception we
            # thought of would make that true only for the failures we predicted.
            #
            # ERROR, and it names CAP-5: this is the line that has to exist for a
            # cache that never works to be distinguishable from one that is
            # merely empty, because nothing about it ever reaches a caller.
            logger.exception(
                "CAP-5 degraded: could not cache the response for %r. Serving it "
                "anyway, but no stale answer will exist for this profile",
                public_id,
            )
            return False

        if row is None:
            # Not a failure: a concurrent request for the same profile got a
            # fresher answer in first, and the row holds that one. See
            # `_CACHE_SAVE_SQL` — the record must never move backwards in time.
            logger.info(
                "Kept the newer cached record for %r; this fetch (%s) was older",
                public_id,
                moment,
            )
            return True

        logger.info("Cached the response for %r (fetched_at=%s)", public_id, moment)
        return True

    # -- Reading --------------------------------------------------------------

    def fallback_for(self, public_id: str, error: ApiError) -> Fallback:
        """The stale body to answer ``error`` with, or why there is none.

        **The whole stale-serve rule, in one place, and the only way in.** Fall
        back only when the failure is retryable and a usable record exists.
        There is deliberately no public "just read the cache" method: a second
        entry point is a second place for the gate to be forgotten, which is how
        a cached profile eventually reaches a caller whose session has died.

        ``error.spec.retryable`` is the table in ``response-schema.md``, read
        rather than re-stated. A code this API considers permanent —
        ``SESSION_EXPIRED`` and ``NO_SESSION`` above all — reaches the caller as
        itself no matter how good the cached record is, because a caller whose
        credential has died needs to be told so, and a 200 with someone's
        profile in it does not tell them.

        **This never raises**, for the reason in the module docstring: a cache
        read that throws must not turn the caller's real upstream error into a
        503 about this service's own datastore.
        """
        if not error.spec.retryable:
            # Not logged: this is the ordinary path for every permanent failure,
            # and a line here would put one per 404 in the log for no reader.
            return Fallback(NOT_RETRYABLE)

        # One `try` around BOTH the load and the parse. An earlier version
        # guarded only the load, so a `TypeError` while validating the document
        # escaped uncaught and turned a caller's real 429 into a 500 — the exact
        # thing the "never raises" contract exists to prevent, reached through
        # the code written to enforce it.
        try:
            row = self._store.load(public_id)
            if row is None:
                logger.info(
                    "Nothing cached for %r; returning the live %s",
                    public_id,
                    error.code,
                )
                return Fallback(NO_RECORD)
            body = _as_stale_body(public_id, row)
        except Exception:
            logger.exception(
                "CAP-5 degraded: could not read a cached response for %r, so the "
                "live %s will be returned as it is",
                public_id,
                error.code,
            )
            return Fallback(DATASTORE_UNAVAILABLE)

        if body is None:
            # `_as_stale_body` has already logged which of the four checks
            # refused it, at ERROR, with the detail.
            return Fallback(UNUSABLE_RECORD)
        return Fallback(SERVED, body)


def _utc(moment: datetime) -> datetime:
    """Timezone-aware UTC, treating a naive value as UTC rather than local.

    The same rule, and the same reasoning, as ``_isoformat`` in the profile
    route: everything in this codebase stamps aware, and silently reinterpreting
    a naive value as local time would make the stored column disagree with the
    string in the body it was taken from.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _as_stale_body(public_id: str, row: CacheRow) -> dict[str, Any] | None:
    """Parse a stored row into the body to return, or ``None`` if unusable.

    ``public_id`` is the id the caller **asked for**, threaded down from
    :meth:`ProfileCache.fallback_for` rather than taken from the row. That is
    the whole point of the first check: comparing the body against the row's own
    key catches "this document disagrees with the row it sits in", but it cannot
    catch "the store handed back a row for a different key" — which is precisely
    the dropped-``WHERE`` mutation the SQL tests exist to fear, and in that case
    the row's key and the body's id agree with each other perfectly. A defence
    that restates the SQL is not a defence against the SQL being wrong.

    The four refusals below are not ceremony. A row that will not parse, is not
    an object, was written under a different envelope shape, or names a
    different member is a row this service cannot honestly publish — and
    publishing one anyway is how a cache answers a request for one member with
    another member's profile, which is the single worst failure this API has.
    Story 4's review already caught the live path doing that; a mis-keyed cache
    row is the same bug with a longer fuse, because the cache has no expiry.

    Every refusal returns ``None`` and **nothing here deletes anything**.
    Unbounded is the author's decision; ignoring a row honours it, removing one
    would not.
    """
    if row.public_id != public_id:
        logger.error(
            "The cache was asked for %r and returned a row keyed %r. Refusing to "
            "serve it: the lookup did not filter on the id it was given.",
            public_id,
            row.public_id,
        )
        return None

    if row.envelope_version != ENVELOPE_VERSION:
        logger.error(
            "The cached record for %r was written under envelope version %r, not "
            "%r. Ignoring it — a body in an older shape is a wrongly shaped "
            "answer, not an old one. The next live fetch will replace it.",
            public_id,
            row.envelope_version,
            ENVELOPE_VERSION,
        )
        return None

    try:
        body = json.loads(row.body)
    except ValueError:
        logger.error(
            "The cached record for %r is not parseable JSON; ignoring it",
            public_id,
        )
        return None

    if not isinstance(body, dict):
        logger.error(
            "The cached record for %r is not a JSON object; ignoring it", public_id
        )
        return None

    if set(body) != REQUIRED_ENVELOPE_KEYS:
        # Belt to `envelope_version`'s braces, and the one that catches a row
        # written by a build that changed the envelope and forgot to bump the
        # version. `partial` is the key this exists for: `response-schema.md`
        # makes it always-present so that "nothing in it" and "this response
        # predates the field" stay distinguishable, and republishing a body
        # without it destroys exactly that distinction.
        logger.error(
            "The cached record for %r carries the keys %s, not the envelope's %s; "
            "ignoring it",
            public_id,
            sorted(body),
            sorted(REQUIRED_ENVELOPE_KEYS),
        )
        return None

    stored_id = body.get(PUBLIC_ID_KEY)
    if stored_id != public_id:
        logger.error(
            "The cached record filed under %r names %r; refusing to serve one "
            "member's profile under another member's URL.",
            public_id,
            stored_id,
        )
        return None

    # The one key that changes, and it is a copy rather than a mutation: the
    # stored document stays exactly as it was, which is what makes a second
    # stale serve of the same row produce the same answer.
    return {**body, STALE_KEY: True}


#: Process-wide instance, mirroring :data:`app.vault.vault`. Constructing the
#: store opens no connection, so importing this module is safe with Postgres
#: down — which is what keeps ``import app.main`` working on a laptop with the
#: stack stopped.
cache = ProfileCache(PostgresProfileCacheStore())
