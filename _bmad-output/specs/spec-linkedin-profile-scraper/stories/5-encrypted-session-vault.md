---
title: 'Encrypted per-user LinkedIn session vault'
type: 'feature'
created: '2026-08-27'
status: 'ready-for-dev'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/specs/spec-linkedin-profile-scraper/SPEC.md'
  - '{project-root}/_bmad-output/specs/spec-linkedin-profile-scraper/response-schema.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The Voyager client takes an `li_at` as an argument, but nothing stores one, so no caller can actually reach a profile. CAP-4 requires each caller's session bound to their own Keycloak subject, encrypted at rest, and recoverable by nobody else. Separately, no route is mounted under `/api/v1` yet, so the auth boundary is never exercised by a real request and `/docs` has no Authorize button.

**Approach:** `PUT` and `GET /api/v1/session`, storing a Fernet-encrypted cookie keyed by the JWT subject in a dedicated Postgres schema, and swap the bearer scheme for OAuth2 client-credentials so Swagger can obtain a token itself.

## Boundaries & Constraints

**Always:**
- The stored cookie value is never returned by any endpoint, under any flag, query parameter, debug mode, or error path. `GET` returns presence and last-use validity only.
- The encryption key arrives by environment variable and is never committed. What lands in Postgres is ciphertext: someone reading the table without the key learns nothing.
- A stored session is recoverable only by the Keycloak subject that supplied it. Subject identity comes from the verified token, never from a request field a caller controls.
- `PUT` replaces any stored session outright. Overwrite is the entire lifecycle.
- The cookie appears in no log, trace, exception message, error body, OpenAPI example, or test output.
- Application tables live in their own schema. Keycloak owns `public` in this database.

**Ask First:**
- Any change to the request or response shapes fixed by `response-schema.md`.
- Any runtime dependency beyond a Postgres driver — `cryptography` is already present from story 3.
- Storing any user attribute beyond what the vault needs.
- Any migration mechanism heavier than this story requires.

**Never:**
- No delete or revoke endpoint. `PUT` overwrite is the whole lifecycle in the Must tier.
- No automated refresh, re-login, or cookie repair. Expiry is surfaced, never healed.
- No profile fetching, schema mapping, or caching (stories 6–7).
- No storing the cookie anywhere but the encrypted column — not in a log line, not in a cache key, not in a metric label.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Behavior | Error Handling |
|---|---|---|---|
| Store a session | `PUT` with a valid token and a well-formed cookie | Stored encrypted; response confirms presence without echoing the value | N/A |
| Replace | `PUT` again with a different cookie | Previous value overwritten, no history retained | N/A |
| Read presence | `GET` with a valid token, session stored | Presence and last-use validity only | N/A |
| Nothing stored | `GET` with a valid token, no session | A successful response saying so — not an error | Presence reported false |
| Subject isolation | Subject B reads after subject A stored | B sees only B's own state; whether A has a session at all is invisible to B | Never leak another subject's state |
| No token | `PUT` or `GET` with no bearer | 401 `UNAUTHENTICATED` | Inherited from the router dependency |
| Malformed cookie | Empty, control characters, absurd length | Typed 4xx naming the problem, nothing stored | Reuse the session validation from story 4 |
| At rest | Read the table directly with `psql` | Ciphertext only; no substring of the cookie is recoverable | N/A |
| Wrong key | Key rotated, stored row from the old key | A clear typed failure, never a crash or a silent empty value | Surfaced, not swallowed |

</frozen-after-approval>

## Code Map

- `../../../../app/api/v1/__init__.py:27` — the guarded router. The first real routes mount here and inherit `require_claims`; do not re-declare auth per route.
- `../../../../app/auth.py:364` — `require_claims` returns the verified claims; `sub` is the vault key. Swapping the OpenAPI security scheme happens here.
- `../../../../app/auth.py:75` — `JWKS_URL` / `EXPECTED_ISSUER` show the internal-vs-external URL split the OAuth2 scheme must respect: Swagger's token URL is the **external** issuer, not the in-network one.
- `../../../../app/errors.py:67` — `ERROR_SPECS` already carries `NO_SESSION` and `SESSION_EXPIRED` from story 4. Extend, do not fork.
- `../../../../app/linkedin/client.py` — `LinkedInSession` validates cookie shape; reuse it rather than writing a second validator. Story 4's review added control-character rejection there.
- `../../../../app/config.py` — `session_encryption_key` and `database_url` are already required settings that nothing yet consumes. This story is their first consumer.
- `../../../../docker-compose.yml:13` — Postgres, shared with Keycloak. Keycloak owns the `public` schema.
- `../../../../tests/test_auth.py` — token-minting and probe-route patterns to follow.
- Deployed stack runs from a clean clone on the instance; a schema change must apply itself on a cold volume with no manual step.

## Tasks & Acceptance

**Execution:**
- [ ] `requirements.txt` — add a Postgres driver with an arm64 wheel; nothing else
- [ ] `app/db.py` — connection handling plus schema bootstrap that is safe to run on every start — the deployed stack must come up on a cold volume unattended
- [ ] `app/vault.py` — encrypt/decrypt with the key from settings, and the subject-keyed store/load — the only place the cookie is in plaintext
- [ ] `app/api/v1/session.py` — `PUT` and `GET` exactly as `response-schema.md` fixes them
- [ ] `app/api/v1/__init__.py` — mount the session router
- [ ] `app/auth.py` — replace the bearer scheme with OAuth2 client-credentials pointing at the external token URL, so `/docs` shows an Authorize button that mints its own token
- [ ] `app/errors.py` — any new typed code the matrix needs
- [ ] `tests/test_vault.py` — every matrix row, including at-rest ciphertext and cross-subject isolation
- [ ] `tests/test_session_api.py` — the two endpoints end to end against a real token
- [ ] `README.md` — the store-your-session step, since CAP-3's second curl depends on it

**Acceptance Criteria:**
- Given two different Keycloak subjects, when each stores a session and reads it back, then neither can observe the other's value or presence.
- Given a stored session, when the database is read directly, then no substring of the cookie is recoverable without the key.
- Given any response, log line or error body produced by this story, when searched for the stored value, then it does not appear.
- Given `docker compose down -v` then `up -d --wait`, then the schema exists and both endpoints work with no manual step.
- Given the generated OpenAPI document, when it is loaded in `/docs`, then an Authorize button appears and obtains a token against the external issuer.

## Spec Change Log

## Design Notes

**Swagger's token URL is the external issuer.** `KEYCLOAK_SERVER_URL` is the in-network address used to fetch JWKS; a browser cannot reach it. The OAuth2 scheme must advertise `{KEYCLOAK_ISSUER_URL}/realms/{realm}/protocol/openid-connect/token`, which is exactly why story 3 split the two settings. Story 3's tests assert the scheme name and that `/health` carries no security — expect to update them deliberately, not to weaken them.

**Subject comes from the token, never the request.** The vault key is the verified `sub` claim. A subject supplied in a body or query parameter would let any authenticated caller read any other caller's session, which is CAP-4 inverted.

**Presence is not the value.** `GET` answering "a session is stored, last use succeeded" is useful; answering with the cookie is a credential disclosure endpoint. There is no debug flag that should change this, and a test should assert the value is absent from the response under every code path.

**Migrations are deliberately out of scope, by decision (2026-08-27).** The human's call: no migration tool until the APIs are completely built, then introduce one. So this story creates its schema with an idempotent bootstrap that is safe on every start, and stories 6-8 do the same for anything they add. The trade-off accepted knowingly: no migration history and no down-path until a tool lands later. Do not add Alembic here.

**Schema namespacing.** Keycloak owns `public` in this database. Application tables belong in their own schema so a Keycloak migration and an application migration can never collide — a deferred finding from story 1 that lands here.

## Verification

**Commands:**
- `docker compose down -v && docker compose up -d --wait` — expected: exit 0, schema created unattended
- `docker build --target test -t lps-test . && docker run --rm --network none lps-test` — expected: all tests pass with no network
- `PUT` then `GET /api/v1/session` with a minted token — expected: presence reported, value never echoed
- `docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c 'select * from <schema>.<table>'` — expected: ciphertext only
- `docker compose logs api | grep -F "$COOKIE"` — expected: no match
- `curl -fsS http://127.0.0.1:8000/openapi.json` — expected: an OAuth2 scheme whose token URL is the external issuer
- `docker run --rm -v "$PWD":/repo -w /repo zricethezav/gitleaks:latest git --no-banner` — expected: no leaks
