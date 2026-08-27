---
title: 'Error taxonomy and handlers'
type: 'feature'
created: '2026-08-27'
status: 'in-review'
baseline_commit: '02b90939ebabfe82f51e5b0c5f5d62f5d1cf999d'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/specs/spec-linkedin-profile-scraper/response-schema.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The envelope, the eight codes, the handlers and the OpenAPI declarations already exist and are correct. What is wrong is *what gets classified as what* on four paths earlier stories deferred here: two guards report a permanent failure as retryable (one is stale-served forever as a result), a dead `li_at` arriving as a 200 authwall is reported as staleness so the caller is never told to replace it, and an unreachable Keycloak is reported as a 401 telling a valid token not to retry.

**Approach:** Make `retryable` an honest per-response property rather than a per-code constant, reclassify the four paths, and close the assertion gaps so every code's status, envelope shape and `retryable` flag are pinned on the wire rather than in the table alone.

## Boundaries & Constraints

**Always:**
- `ERROR_SPECS` stays byte-identical in codes, statuses and default `retryable`. It is pinned to `response-schema.md` by a hand-transcribed test that must remain green and must not be rewritten to follow the code.
- `retryable` may be overridden only per raise site, never per code. The default for every code remains what the contract table says.
- Anything gating on retryability reads the *effective* value, not the code's default.
- No unhandled exception reaches the client, and no error body carries upstream text, cookie material, or a stack trace.
- Every error response keeps `Cache-Control: no-store`.

**Ask First:**
- Any edit to `response-schema.md` itself, or to the eight rows of `ERROR_SPECS`.
- Deleting `FALLBACK_CODES` outright, as opposed to shrinking the rows a real taxonomy code now supersedes.
- Any change that makes a currently non-retryable code retryable.

**Never:**
- No new capability or retrieval behavior. This story reclassifies and asserts; it does not fetch, map, or cache.
- No widening of the story-6 decoration retry beyond the one refused-decoration case it already covers.
- No `cause` value in a client-facing body — it is operator-only, like `log_detail`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Behavior | Error Handling |
|---|---|---|---|
| Member mismatch, record cached | Core response names a different member; cache holds the requested profile | `502 UPSTREAM_ERROR`, `retryable: false`, **not** stale-served | Effective retryable is false, so the cache fallback declines |
| Member mismatch, endpoint guard | Response disagrees with the requested public id | `502 UPSTREAM_ERROR`, `retryable: false` | Same override, applied at both guards |
| IdP unreachable, valid token | JWKS fetch fails (network/5xx) | `502 UPSTREAM_ERROR`, `retryable: true` | Was 401; a good token must not be told to give up |
| Unknown signing key | Token `kid` absent from a reachable JWKS | `401 UNAUTHENTICATED`, `retryable: false` | Unchanged — must not regress into a 502 |
| Dead cookie via 200 authwall | `me` resource answers 200 with an authwall body | `428 SESSION_EXPIRED`, `retryable: false` | Challenge on `me` is evidence about the cookie itself |
| Datacenter challenge on a fetch | Profile fetch answers a challenge; record cached | `200` with `"stale": true` | Unchanged — still absorbed by stale-serve |
| Unknown `/api/v1` path, no token | `GET /api/v1/nope` unauthenticated | `401 UNAUTHENTICATED` | Was 404, which let a caller enumerate real routes |
| Any taxonomy code | Each of the eight codes raised | Body keys are exactly `{error:{code,message,retryable}}` with the contract's status | Asserted on the wire for all eight, not just four |

</frozen-after-approval>

## Code Map

Read-only contract: `_bmad-output/specs/spec-linkedin-profile-scraper/response-schema.md` — the eight-row table and the stale-serve precedence rule.

- `app/errors.py:71` `ERROR_SPECS` — all eight rows, correct today. **Do not edit.** `:167` `ApiError` (carries code, message, headers, operator-only `log_detail`) and `:193` `to_response()` are where the per-instance override and `cause` belong. `:246` `FALLBACK_CODES` + `:259` `FALLBACK_CODE` — shrink where a taxonomy code now applies; the set itself must survive. `:277` `envelope()`, `:307`–`:373` the five handlers, `:410` `error_responses()` feeding OpenAPI.
- `app/linkedin/client.py:1002` — client-side member-mismatch guard, raises retryable `UPSTREAM_ERROR` **inside** the stale-serve boundary. The one that is actually stale-served today.
- `app/linkedin/client.py:1236`–`:1292` `_classify` — status→code mapping. `:1257`/`:1261` are the challenge branches; `:1279`–`:1292` collapse 400 / 410 / unexpected-status / malformed-envelope into one `UPSTREAM_ERROR`, which is what `cause` disambiguates.
- `app/api/v1/profile.py:666` — endpoint mismatch guard, already placed outside the stale-serve boundary by story 7 with a comment handing the classification here. `:540`–`:623` is that boundary; `:594` is the `except ApiError` that calls the fallback.
- `app/cache.py:253` `fallback_for` — gates solely on `error.spec.retryable`. **This is the line that must read the effective value**, or the override changes the body while the record is still served.
- `app/auth.py:489` — swallows `SigningKeyUnavailable` (raised at `:245`/`:259`) into `_reject()` at `:571`, conflating a JWKS fetch failure with an unknown `kid`.
- `app/api/v1/__init__.py:30` — router-level `responses=UNAUTHENTICATED_RESPONSE`; the unmatched-path 401 belongs near here or in `app/main.py:117` where handlers install.
- `tests/test_linkedin_client.py:1796` `test_the_error_table_matches_the_response_schema_exactly` — the pinning test. Must stay green **unmodified**.
- `tests/test_auth.py:1019` `_assert_envelope` — the strict key-set check, currently applied only to 404/405/422/500.

## Tasks & Acceptance

**Execution:**
- [x] `app/errors.py` — `ApiError` gains an optional per-instance `retryable` override and an operator-only `cause`; expose an effective-retryable property; render it in `to_response()`. `cause` never reaches the body
- [x] `app/cache.py` — gate `fallback_for` on effective retryability so an overridden error is not stale-served
- [x] `app/linkedin/client.py` — mark both the `_core_profile` mismatch guard and its endpoint twin non-retryable; classify a challenge on the `me` resource as `SESSION_EXPIRED`; attach `cause` in `_classify` so the decoration retry fires only for a refused decoration
- [x] `app/api/v1/profile.py` — apply the same override at the endpoint guard; keep it outside the stale-serve boundary
- [x] `app/auth.py` — split JWKS-fetch failure from unknown-`kid` and route the former to `UPSTREAM_ERROR` 502; unknown `kid` stays 401
- [x] `app/api/v1/__init__.py` — unmatched `/api/v1/*` answers 401 rather than 404, without breaking real 404s from real routes
- [x] `app/errors.py` — shrink the `FALLBACK_CODES` rows a taxonomy code now supersedes; keep the set and its disjointness test intact
- [x] `app/api/v1/profile.py`, `app/api/v1/session.py` — update the hand-written 400/422/502 OpenAPI descriptions where the reclassification changes what a status means
- [x] `tests/` — parametrize the strict envelope check across all eight codes; assert `retryable` on the wire for all eight; add a test per matrix row above, including that the mismatch is *not* stale-served when a record exists

**Acceptance Criteria:**
- Given a cached record and a response naming a different member, when the profile is requested, then the caller gets a non-retryable 502 rather than a stale 200.
- Given Keycloak is unreachable and the token is otherwise valid, when any `/api/v1` route is called, then the caller gets a retryable 502 and no request is refused as unauthenticated.
- Given the eight taxonomy codes, when each is provoked over HTTP, then body keys are exactly `{error:{code,message,retryable}}` with the contract's status and the expected `retryable`.
- Given the full suite, when it runs, then the hand-transcribed table test passes unmodified.

## Spec Change Log

- **`ApiError` gained `cause` alongside the `retryable` override.** The task list
  named `cause` under the client's bullet; it is declared on `ApiError` in
  `app/errors.py` because that is where `log_detail` lives and the two are the
  same kind of thing — operator-only, never in a body. The vocabulary
  (`CAUSE_*`, `DECORATION_RETRY_CAUSES`) stays in `app/linkedin/client.py`,
  since the values are that module's domain.

- **The `me` split is by challenge *reason*, not by every challenge branch.** A
  wall — a redirect to `/authwall`, `/checkpoint`, `/login`, or a non-JSON body
  — is `SESSION_EXPIRED` on `me`. LinkedIn's `999` bot status deliberately is
  not: it is decided at the edge before any session is considered and arrives
  identically for a brand-new cookie, so reporting it as expiry would send a
  caller to replace a credential that works — the same lie this story is fixing,
  pointed the other way. Argued in a comment at the branch, pinned by
  `test_linkedins_bot_status_on_me_is_still_a_challenge`.

- **The unmatched-path route is registered on the app, not on the `/api/v1`
  router.** Two reasons, both mechanical: `include_router` leaves a lazy marker
  carrying no `path`, and the guard has to identify its own route by path to
  exclude it when computing `Allow`; and the test suite mounts probe routers
  onto `router` *after* import, which a catch-all sitting inside it would
  shadow. It is installed last, from `app/main.py`, and a test asserts that
  ordering.

- **A wrong method on a real route still answers 405 with `Allow`.** Claiming
  every method means Starlette never produces its own 405, so
  `_methods_answering` asks the app's routing table which methods actually
  answer the path. Not in the story, but silently degrading 405 to 404 would
  have been a regression paid for a fix elsewhere.

- **`VoyagerClient`'s transport default is resolved at construction, not bound
  in the signature.** Not planned. `tests/test_session_api.py::
  test_the_real_verifier_never_raises` replaced `client.urllib_transport` on the
  module and the client never called the substitute, because the default
  argument had captured the original function object at import — so that test
  had been reaching the real linkedin.com from inside the offline suite. The
  `me` reclassification surfaced it by changing what that live response
  classified as. The suite now passes under `docker run --network none`.

- **The README was corrected where this story made it false**, and no further.
  Three places: a 401 is now always about the token, the member-mismatch row
  states `retryable: false` on the wire, and the dead-cookie gap is narrower
  than it was. The story-9 obligation from the Design Notes — that the wire
  value is authoritative over the published table for this one case — is
  recorded in the `Known limitations` placeholder and in the deferred-work log.

## Design Notes

**Per-instance `retryable` is a deliberate, human-approved contract deviation.** `response-schema.md` marks `UPSTREAM_ERROR` retryable-yes; after this story a member-mismatch returns that code with `retryable: false`. A new contract row was the declined alternative. Two obligations follow: the override is reachable only from named raise sites, never a general escape hatch; and story 9's Known limitations must state that the wire value is authoritative over the published table for this one case.

**The override is inert unless every gate reads it.** `fallback_for` is the only gate today, but the rule is general: a grep for `.spec.retryable` should return nothing that decides behavior once this lands.

**`me` is not like a profile fetch.** A challenge on a profile URL says nothing about the cookie — LinkedIn serves that page to healthy sessions from datacenter IPs. A challenge on `me`, which describes the session's own owner, is evidence about the session. That asymmetry is the entire justification for splitting them; keep it in a comment at the branch.

## Verification

**Commands:**
- `docker build -q --target test -t lps-test . && docker run --rm lps-test` — expected: full suite passes, including the unmodified table test
- `docker run --rm lps-test python -m pytest -q -k "envelope or retryable or classify or mismatch"` — expected: the new parametrized assertions pass across all eight codes
- `docker compose up -d --wait && curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/v1/nope` — expected: `401`
- `docker run --rm lps-test python -c "import app.main"` — expected: clean import, handlers install
