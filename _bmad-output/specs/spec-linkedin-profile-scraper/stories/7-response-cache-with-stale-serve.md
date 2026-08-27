---
title: 'Response cache with stale-serve'
type: 'feature'
created: '2026-08-27'
status: 'done'
baseline_commit: 'a60d508c105fdff04d52606ee588350461258078'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/specs/spec-linkedin-profile-scraper/SPEC.md'
  - '{project-root}/_bmad-output/specs/spec-linkedin-profile-scraper/response-schema.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Every profile request depends on LinkedIn answering right now, across six calls, under a cookie that may have died since it was stored. The evaluator will call this service at an unknown time after submission. As built, one throttle, challenge, or upstream hiccup at that moment returns an error and the submission reads as broken.

**Approach:** Persist every successful response, and when a live retrieval fails for a reason that retrying could fix, return the last good record with `stale: true` and its original `fetched_at` instead of the error.

## Boundaries & Constraints

**Always:**
- `fetched_at` is when the profile was **retrieved from LinkedIn**, never when the request was served. On a stale response it is the older timestamp — that is what makes staleness actionable.
- Stale-serve takes precedence over the retryable errors: with a cached record present, `RATE_LIMITED`, `UPSTREAM_CHALLENGE` and `UPSTREAM_ERROR` become 200 with `stale: true`.
- **Unbounded by decision.** No TTL, no eviction, no age limit. A record of any age is served in preference to a retryable error. Do not add expiry.
- A cached record is served **exactly as it was stored**, including its `partial[]`. Serving it must not re-run mapping or invent freshness.
- Writing to the cache never fails a request that otherwise succeeded.

**Ask First:**
- Any eviction, TTL, size cap, or background refresh.
- Any new runtime dependency, or a datastore other than the Postgres already present.
- Any change to the envelope fixed by `response-schema.md`.

**Never:**
- **Never stale-serve a non-retryable failure.** `NO_SESSION`, `SESSION_EXPIRED`, `UNAUTHENTICATED`, `INVALID_URL` and `PROFILE_NOT_FOUND` must reach the caller as themselves. Hiding an expired session behind cached data is the precise failure this project has already had to fix once.
- No caching of session or credential state. This cache holds public profile data only.
- No serving a cached record to a caller who has no working session — the session check happens first.
- No schema migration tool (deferred to story 10); bootstrap idempotently as stories 5–6 do.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Behavior | Error Handling |
|---|---|---|---|
| First fetch | Nothing cached, upstream healthy | 200, `stale: false`, record written | N/A |
| Repeat fetch | Cached record exists, upstream healthy | 200, `stale: false`, **fresh** data; record replaced | Live always wins when it works |
| Throttled with cache | `RATE_LIMITED`, record exists | 200, `stale: true`, **original** `fetched_at` | Error suppressed deliberately |
| Challenge with cache | `UPSTREAM_CHALLENGE`, record exists | 200, `stale: true` | Same |
| Upstream error with cache | `UPSTREAM_ERROR`, record exists | 200, `stale: true` | Same |
| Retryable, nothing cached | `RATE_LIMITED`, no record | The typed error, unchanged | Nothing to fall back to |
| Expired session | `SESSION_EXPIRED`, record exists | **428, not a stale 200** | Never masked by the cache |
| No session | Caller stored none, record exists | **428 `NO_SESSION`** | Session check precedes the cache |
| Not found | `PROFILE_NOT_FOUND`, record exists | 404 | A deleted profile is not stale data |
| Very old record | Record from long ago, upstream failing | 200, `stale: true`, old `fetched_at` | Unbounded by decision |
| Partial record | Cached response had `partial[]` | Served with the same `partial[]` | Stored verbatim |
| Cache write fails | Datastore rejects the write | The live 200 is still returned | Logged, never surfaced |

</frozen-after-approval>

## Code Map

- `../../../../app/api/v1/profile.py` — the route. The cache wraps the fetch-and-map path; `_isoformat` already renders `fetched_at`, and the envelope is hand-built precisely so key omission survives — a cached record must round-trip through that unchanged.
- `../../../../app/db.py` — `bootstrap()`, `BOOTSTRAP_STATEMENTS`, the `app` schema, and the injectable `connect_fn` that makes the executed SQL observable. Follow that pattern: tests must see the real query, not a string constant.
- `../../../../app/vault.py` — the store/Protocol split to mirror, so the cache is testable without Postgres.
- `../../../../app/errors.py` — `ERROR_SPECS` carries `retryable` per code. That flag is the input to the stale-serve decision; read it rather than hardcoding a second list that can drift.
- `../../../../app/linkedin/client.py` — `SYSTEMIC_CODES` abort a fetch. Those aborts are exactly what stale-serve catches.
- `../../../../tests/test_profile_api.py` — the route's test harness and stub fetcher.
- Story-4 review established that `public_id` is lowercased at parse time, so the cache key is already normalised. Do not re-normalise divergently.

## Tasks & Acceptance

**Execution:**
- [x] `app/db.py` — cache table in the `app` schema plus its bootstrap statement, keyed by `public_id`
- [x] `app/cache.py` — store and load of a whole response body with its `fetched_at`; a `Protocol` so tests need no database
- [x] `app/api/v1/profile.py` — write on success; on failure consult `retryable` and fall back
- [x] `tests/test_cache.py` — every matrix row, including the non-retryable codes that must **not** be served from cache
- [x] `tests/test_profile_api.py` — the route's stale path end to end
- [x] `tests/test_postgres_live.py` — extend the opt-in round-trip to the cache table
- [x] `README.md` — what `stale` and `fetched_at` mean to a caller, and that staleness is unbounded by design

**Acceptance Criteria:**
- Given a cached record and a forced retryable failure, when a profile is requested, then the response is 200 with `stale: true` and the `fetched_at` of the original retrieval, not of this request.
- Given a cached record and an expired session, when a profile is requested, then the response is 428 `SESSION_EXPIRED` and no cached data is returned.
- Given a successful live retrieval, when it completes, then the response is `stale: false` and the stored record is replaced.
- Given a cached record whose stored body contained `partial[]`, when it is served stale, then the same `partial[]` is returned.
- Given a datastore write failure on an otherwise successful fetch, when the request completes, then the caller still receives the live 200.

## Spec Change Log

### Two decisions this story was assigned, made rather than deferred

**1. A response naming a different member stays a 502; it is not stale-served.**
The guard raises `UPSTREAM_ERROR`, which `ERROR_SPECS` marks retryable, so
inside the stale-serve boundary it would have become a 200 whenever a record
existed. It is now raised *outside* the boundary, deliberately. The reasoning:
that condition is **permanent** — a vanity URL that has changed hands, a
redirect, a substitution — and under a cache with no expiry a stale 200 would
republish the old identity mapping for ever without ever telling the caller the
URL has stopped meaning what they think. That is the same shape as hiding a dead
session behind cached data, which the Boundaries forbid outright; the only
reason it is not literally that rule is that the code is misclassified. Placing
the raise outside the boundary makes the behaviour correct *regardless* of what
story 8 assigns the code, without this story editing a taxonomy it does not own.
The previous test on that path ran with an **empty** cache and so passed
whatever the cached case did; `test_a_different_member_is_still_a_502_when_a_record_exists`
and a structural `test_the_identity_guard_is_outside_the_stale_serve_boundary`
now pin both halves.

**2. Expiring LinkedIn CDN image URLs are kept in a stale record, not stripped.**
They are signed and time-limited, so on a record served long after it was
fetched they 403. Dropping them was rejected on two grounds. It would mean
re-shaping a record on the way out, which is exactly what the Boundaries'
"served exactly as it was stored, including its `partial[]`" forbids — and the
moment the cache is allowed to edit one field on the way out, "exactly as
stored" stops being checkable. And it would misreport the contract: an absent
`images` key is defined by `response-schema.md` as a claim about the *member*
("no photo"), when the truth is a fact about a *URL*. Reporting it in `partial`
instead would be worse still, since `partial` means "could not be retrieved in
this run" and it was retrieved perfectly. So the field stays as fetched, and
`fetched_at` is what tells a caller how likely it still resolves. Written up in
the README and seeded into story 9's Known limitations.

### Two retryable codes still carry permanent conditions

Both were named in the Design Notes; the first is now handled structurally, the
second is not and cannot be from here:

1. *The public-id mismatch guards.* Still `UPSTREAM_ERROR`, still misclassified.
   This story neutralised the **consequence** by placement (decision 1 above)
   rather than by editing the taxonomy. `app/linkedin/client.py`'s own guard is
   untouched and still stale-serveable through its own raise path; **story 8
   owns reclassifying both together.**

2. *An authwall that arrives as a 200.* Found during this story's live
   verification, and new: a `li_at` LinkedIn refuses by redirecting to
   `/authwall` with a 200 is classified `UPSTREAM_CHALLENGE` (`retryable: true`)
   and is therefore stale-served indefinitely. `app/linkedin/client.py:_classify`
   already ranks an explicit 401/403 refusal above the challenge check, for
   exactly this reason and naming this story — but that ordering only helps when
   LinkedIn states the refusal in the status. Verified end to end: a deliberately
   dead cookie against a cached profile returned `200 stale:true`, not
   `428 SESSION_EXPIRED`.

   This is *not* a deviation from the contract — `SPEC.md` says challenge pages
   are "absorbed by stale-serve", the matrix row *Challenge with cache* asks for
   exactly that 200, and a 200-authwall is genuinely indistinguishable from the
   datacenter-IP challenge the same page serves to a good session. But an
   absolute promise the code does not keep is worse than the limitation itself,
   so **the promise is now qualified everywhere it is made**: the README's stale
   table, the README's error table, the OpenAPI route description, and
   `app/cache.py`'s module docstring all state the gap and tell the caller what
   to do about it (re-`PUT` the session). Recorded for story 8 and seeded into
   story 9's Known limitations.

### Review findings applied

Nineteen, of which the load-bearing ones changed the design rather than the
prose:

* **The cache could be entirely broken and entirely silent.** Five mutations to
  the cache DDL and SQL each left the whole suite green — a column renamed in
  the DDL only, `body text`→`jsonb`, `timestamptz`→`timestamp`, and either
  statement pointed at the *session* relation — because the offline tests
  asserted on SQL *strings*, the executing tests are opt-in and skipped by
  default, and `remember`/`recall` swallow everything by design. Every one ships
  as "the cache never writes and never serves", CAP-5 gone, one log line the
  only trace. Closed three ways: column-**type** assertions on the DDL, a
  default-on resolver that parses the tables `bootstrap` creates and checks
  every statement in the new `db.CACHE_STATEMENTS` against them (relation *and*
  column names), and a `ProfileCache` that reports five distinct outcomes with
  every swallowed datastore failure logged at ERROR naming CAP-5 — so a cache
  that has never worked cannot look like an empty one. All five mutations were
  re-applied afterwards and each now fails a test.
* **The mis-keyed-row guard restated the SQL instead of being independent of
  it.** It compared the body against the row's *own* key, which cannot catch the
  dropped-`WHERE` case it was written to fear. The requested id is now threaded
  into `_as_stale_body`, so the check is independent of the statement.
* **Records outlive the shape they were written in.** A body seeded without
  `partial` was republished verbatim, destroying the "empty versus predates the
  field" distinction `response-schema.md` exists to preserve. Now guarded by a
  required-key-set check *and* a new `envelope_version` column, both treating a
  bad row as **absent** — never deleting it, because unbounded is the author's
  decision and ignoring a row is not evicting one. `ENVELOPE_VERSION` is pinned
  against the key set by a snapshot test, so changing the envelope without
  bumping it fails rather than silently serving old rows.
* **CAP-5 did not cover the failures nobody predicted.** A non-`ApiError`
  exception from the fetch became a 500 with the cache never consulted. It is
  now a typed `UPSTREAM_ERROR` raised inside the boundary, with the real
  traceback still logged at ERROR. `test_an_unexpected_exception_in_the_fetch_is_a_typed_500_not_a_naked_one`
  was rewritten accordingly — CAP-6 is satisfied by a typed 502 exactly as it
  was by a typed 500.
* **Neither cache operation was bounded.** Both now run under
  `PROFILE_CACHE_DEADLINE_SECONDS`, and the cache has its own connection
  (`db.connect_for_cache`) carrying a server-side `statement_timeout`. Both
  halves are needed: `asyncio.timeout` frees the *request*, only Postgres
  aborting the statement frees the *thread* — and that thread comes from the
  executor `vault.unlock` shares. The session and bootstrap path deliberately
  does **not** inherit the timeout, since its DDL waits on an advisory lock a
  concurrently starting container may legitimately hold.
* **The record could move backwards in time.** `ON CONFLICT DO UPDATE` now
  carries `WHERE profile_cache.fetched_at <= EXCLUDED.fetched_at`, so the slower
  of two concurrent fetches cannot overwrite the fresher one. A refused update
  returns no row and is reported as success, not failure.
* Also: `recall` folded into `fallback_for` so the retryable gate has one
  entrance rather than being a convention; the record validation moved *inside*
  the `try` that promises never to raise (it was outside, so the code written to
  make a bad row safe could itself turn a caller's 429 into a 500);
  `fetched_at` normalised to UTC before storage so the column and the body agree
  and the documented `psql` staleness check means what it says; and the identity
  guard's path now records the session verdict, which was the one exit from the
  handler that recorded nothing.
* **Four hollow tests replaced.** The uncacheable-stale test asserted only a
  header equally true of the 429 it was meant to distinguish from; the no-delete
  test inspected the `Protocol` rather than the implementation that would
  actually grow one; the no-age-bound test read only the load statement despite
  its name; and the "structural placement" claim was unverified — moving
  `vault.unlock` inside the boundary removed the property entirely and left 816
  tests passing, because `fallback_for` short-circuits on non-retryable codes
  anyway. That one is now pinned against the handler's own syntax tree, which is
  the only place an unobservable property can be pinned. Verified by re-applying
  the mutation: it fails.

**No contract change.** The envelope in `response-schema.md` was not touched, no
new taxonomy code was added, no runtime dependency was introduced, nothing in
the `Ask First` list was exercised, and no TTL, eviction or expiry exists
anywhere — the two new refusal paths ignore rows, they never remove them.

**One note for story 10.** `BOOTSTRAP_STATEMENTS` is `CREATE TABLE IF NOT
EXISTS`, so a column added to an existing table would never appear on a warm
volume and every statement naming it would fail — silently, on the cache path.
`envelope_version` was free to add only because the table is new in this story.
The next one is not: until a migration tool lands, a new column needs an
accompanying `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. Stated at the DDL.

## Design Notes

**Why unbounded is the right call here, stated plainly.** This service is graded on whether it still answers at an arbitrary later time. A record of any age, clearly flagged with the timestamp it was fetched, is more useful to an evaluator than a 502 — and `fetched_at` gives them everything they need to judge it. That reasoning holds *because* the flag and the timestamp are honest; it collapses the moment a stale response looks fresh.

**`retryable` is the switch, and it already exists.** `ERROR_SPECS` records retryability per code. Reading that flag rather than writing a second list of codes is what keeps this story and story 8 from drifting apart. The rule is one line: fall back only when the failure is retryable and a record exists.

**The cache is global, not per-caller.** It holds public profile data keyed by public id, so a record fetched under one session serves any caller. That is safe only because the session checks happen *first*: a caller with no session or a dead one is refused before the cache is consulted, so the cache can never be harvested by someone without a working credential of their own.

**A known interaction to get right.** Story 4's review found a permanent condition — the refusal when a response names a different member — carrying a *retryable* code. Under this story that refusal would be stale-served indefinitely. Story 8 owns reclassifying it; if it is still retryable when this lands, note it in the change log rather than letting it pass silently.

## Verification

**Commands:**
- `docker build --target test -t lps-test . && docker run --rm --network none lps-test` — expected: all pass, no network
- `docker compose down -v && docker compose up -d --build --wait` — expected: exit 0, cache table created unattended
- fetch a profile, then force a retryable failure and fetch again — expected: 200, `stale: true`, older `fetched_at`
- with a record cached, present a dead session — expected: 428, not a stale 200
- `docker compose exec postgres psql ... -c 'select public_id, fetched_at from app.<cache table>'` — expected: the record, with the retrieval time
- `docker run --rm -v "$PWD":/repo -w /repo zricethezav/gitleaks:latest git --no-banner` — expected: no leaks
