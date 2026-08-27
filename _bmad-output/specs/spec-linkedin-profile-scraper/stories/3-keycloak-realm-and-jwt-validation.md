---
title: 'Keycloak realm, clients, and JWT validation'
type: 'feature'
created: '2026-08-27'
status: 'done'
baseline_commit: '6f67e1bcba16539440464455915f27a2ff4c03bc'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/specs/spec-linkedin-profile-scraper/SPEC.md'
  - '{project-root}/_bmad-output/specs/spec-linkedin-profile-scraper/response-schema.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Keycloak runs but holds no realm, so `KEYCLOAK_REALM` and the client credentials are required configuration pointing at nothing. The API accepts every request unauthenticated, and the evaluator has no way to mint a token — CAP-2 is entirely unbuilt and CAP-3's scriptable lane does not exist.

**Approach:** Commit a realm export that Keycloak imports on startup, carrying a confidential service-account client, and put JWT validation against the realm JWKS in front of the `/api/v1` surface so every future route inherits it.

## Boundaries & Constraints

**Always:**
- The realm is reproducible from the repository: a committed export, imported on container start, so a clean clone plus `docker compose up` yields the same realm.
- No client secret, signing key, or password appears in the committed realm export or anywhere in git history.
- Validation is cryptographic against the realm's published JWKS. Never trust an unverified claim, and never decode without verifying the signature.
- Rejection is the typed `UNAUTHENTICATED` body from `response-schema.md` with status 401 — missing, malformed, expired, wrong-issuer, and wrong-audience tokens all land there.
- Auth attaches to the `/api/v1` router, not to individual routes, so stories 5–8 cannot accidentally ship an unprotected endpoint.

**Ask First:**
- Any runtime dependency beyond a JWT library and its crypto backend.
- Any new public route shape not already named in `response-schema.md`.
- Weakening validation — skipping audience, issuer, or expiry checks — to make something pass.

**Never:**
- No Google SSO federation. It is Should tier and not the lane the evaluator uses.
- No `/health` change: it stays unauthenticated, outside `/api/v1`, because the container healthcheck has no token.
- No session vault, Voyager client, profile mapping, or cache (stories 4–8).
- No full error taxonomy — story 8 generalises it. This story implements only `UNAUTHENTICATED`, in the shape story 8 will adopt.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Behavior | Error Handling |
|---|---|---|---|
| Evaluator mints a token | `client_credentials` POST to the realm token endpoint | 200 with an access token, no browser redirect anywhere in the flow | N/A |
| Valid token | `Authorization: Bearer <valid>` on an `/api/v1` route | Request reaches the route; subject claim available to it | N/A |
| No header | `/api/v1` route, no `Authorization` | 401 `UNAUTHENTICATED` | Typed envelope, `retryable: false` |
| Malformed header | `Authorization: potato` / `Bearer not.a.jwt` | 401 `UNAUTHENTICATED` | Never a 500 |
| Expired token | `exp` in the past | 401 `UNAUTHENTICATED` | Signature verified first, then expiry |
| Foreign realm | Token correctly signed by a different realm or issuer | 401 `UNAUTHENTICATED` | Issuer must match; unknown `kid` must not fetch-and-trust |
| Wrong audience | Valid realm token minted for another client | 401 `UNAUTHENTICATED` | Audience check enforced |
| Liveness unaffected | `GET /health`, no token | 200 `{"status":"ok"}` | N/A |

</frozen-after-approval>

## Code Map

Story 1 shipped the skeleton; this story is the first to add behaviour. Continuity from `1-project-skeleton-env-config-local-parity.md` — its Design Notes and Spec Change Log carry the conventions below.

- `../../../../app/api/v1/__init__.py:13` — the empty seam. Auth attaches here as a router-level dependency.
- `../../../../app/config.py:33` — `Settings`; `keycloak_server_url`, `keycloak_realm`, `keycloak_client_id`, `keycloak_client_secret` already exist and are required. `RequiredSetting` strips whitespace.
- `../../../../app/main.py:31` — `create_app()`; routers mounted here.
- `../../../../docker-compose.yml:39` — Keycloak service, `command: ["start"]`. Realm import changes this line and adds a read-only mount.
- `../../../../.env.example` — the env contract; every new variable needs an entry or `test_env_example_documents_every_setting` fails.
- `../../../../tests/test_health.py:76` — the probe-route pattern for asserting router wiring without inventing public surface. Reuse it.
- `../../../../requirements.txt` — four runtime pins today; no HTTP client, no crypto.
- Local Keycloak 26.7.2 is running and healthy, so every claim here is empirically checkable against it rather than reasoned about.

## Tasks & Acceptance

**Execution:**
- [x] `deploy/keycloak/realm-linkedin.json` — realm export: the confidential service-account client (service accounts on, standard flow and direct grants off) plus an **audience mapper** so tokens carry the API as `aud` — committed, secret-free
- [x] `docker-compose.yml` — `--import-realm` and a read-only mount of the export; secret supplied from env, never baked into the file
- [x] `requirements.txt` — add a JWT library with its crypto backend; nothing else
- [x] `app/config.py` — add the external issuer setting; the in-network URL cannot also express the issuer a proxied token carries
- [x] `.env.example` — document every new variable
- [x] `app/errors.py` — the typed error envelope and an `UNAUTHENTICATED` raiser, shaped so story 8 extends rather than replaces it
- [x] `app/auth.py` — JWKS-backed bearer validation as a FastAPI dependency returning the verified claims
- [x] `app/api/v1/__init__.py` — attach the dependency at router level
- [x] `tests/test_auth.py` — cover every matrix row, signing test tokens locally so no network is needed
- [x] README — the evaluator's token-minting `curl`, verified against the running stack (added to the Setup section as "Get a token"; `deploy/README.md` was not created, since story 2 left the deploy notes inline in `deploy/nginx/linkedin-profile-api.conf` and a second README would fragment them)

**Acceptance Criteria:**
- Given `docker compose down -v` then `up -d --wait`, when the stack returns, then the realm and its client exist without any manual console step.
- Given the repository and its full history, when searched for credential patterns, then the realm export contains no usable secret.
- Given a token minted by the service-account client, when it is presented to an `/api/v1` route, then the request is authorised; and when the same route is called without it, then 401 with the typed body.
- Given the OpenAPI document, when it is generated, then `/api/v1` routes advertise bearer security and `/health` does not.

## Spec Change Log

**2026-08-27 — implementation additions beyond the task list.** Nothing in the frozen block changed. Every item below was forced by something observed against the running Keycloak 26.7.2 rather than reasoned about.

- **`${VAR}` is the substitution syntax Keycloak's realm importer supports.** Proved by importing a throwaway realm carrying three candidate spellings into a scratch 26.7.2 container and reading the secrets back through the admin API: `${PROBE_SECRET}` was substituted, `$(env:PROBE_SECRET)` and `${env.PROBE_SECRET}` were stored *verbatim*. The last two are the dangerous failure — the container starts, the realm exists, and the client's secret is a literal placeholder string. This is why `tests/test_auth.py::test_the_realm_export_carries_no_secret` asserts the exact spelling rather than merely "no literal secret".
- **The realm export is hand-written, not a `kc.sh export` dump.** A real export is thousands of lines carrying every default, plus `credentials`, key material and per-install ids — an artifact nobody can review for secrets. The import fills in every default, so a minimal file produces the same realm and stays greppable. `test_the_realm_export_carries_no_secret` walks the parsed JSON for credential-bearing keys, so this holds if a later story adds a second client or an SMTP block.
- **Client `description` must stay under 255 characters.** A longer one aborts the import with `ERROR: value too long for type character varying(255)` and Keycloak refuses to start — loud, but the message names neither the field nor the file.
- **`fullScopeAllowed: false` on the client.** Together with the audience mapper this is what makes `aud` exactly `linkedin-profile-api`; with full scope on, the token also carries `account` and the audience assertion becomes much weaker than it reads.
- **`cryptography` is pinned on its own line** rather than left implicit inside a `pyjwt[crypto]` extra, so every runtime version stays visible in `requirements.txt` as story 1 promised. Still two packages, still no HTTP client: the JWKS fetch uses stdlib `urllib`.
- **`RequiredBaseUrl` in `app/config.py`.** Both Keycloak URLs are trailing-slash-normalised, because `iss` is compared by exact string equality and a trailing slash in `.env` would reject every legitimate token while `/health`, the realm and the token endpoint all stayed green.
- **A `typ` claim check.** Keycloak stamps `typ: "Bearer"` on an access token and `typ: "ID"` on an ID token. An ID token from this realm has the same issuer, the same signature and `aud` equal to the client id — audience validation alone does not separate them. Confirmed against a real minted token before the check was written.
- **`WWW-Authenticate` on the 401**, per RFC 6750, with `error="invalid_token"` only when a credential was actually presented. Both values are constants, so the header leaks nothing the body does not.
- **One error message for every rejection reason.** `response-schema.md` fixes the code, not the prose; distinguishing "expired" from "wrong audience" from "unknown kid" in the body would hand a prober a free oracle on the realm's configuration. The specific reason is logged at WARNING instead — verified visible in `docker compose logs api`.

**2026-08-27 — two story-1 tests adapted, no behaviour change intended.** Recorded because both touch a file story 1 owns.

- `tests/test_health.py::test_v1_seam_is_mounted_on_the_built_app` now overrides `require_claims` on the built app. The seam answers 401 before a probe route can run, and this test is about *mounting*; `tests/test_auth.py` is where validation is exercised.
- `tests/test_health.py::test_every_v1_route_lives_under_the_prefix` now asserts over the generated OpenAPI paths instead of `v1_router.routes`. **FastAPI 0.141 changed `include_router`**: it stores a lazy `_IncludedRouter` marker rather than copying routes, so the old loop hit `AttributeError` on a marker with no `path` — and, worse, would have gone silently blind to every route stories 5-8 attach through a sub-router. The same change broke both files' probe-route cleanup, which filtered by `path` and therefore leaked probes into the rest of the session; both now snapshot and restore `v1_router.routes`.

**2026-08-27 — review pass 1 (no spec loopback). 20 findings applied.** Nothing in the frozen block changed; every patch tightens an existing guarantee or closes a gap between what a comment claimed and what the code did. Two reported findings were **not** applied, both disproved by the coordinator: the runtime pins do build and run (a reviewer's local 3.12 venv resolved differently from the image), and the slash-only guard in `app/config.py` was present all along (two reviewers mutation-testing one working tree saw each other's edits).

*The boundary was unverified — the critical one*
1. `tests/test_auth.py` — **deleting `dependencies=[Depends(require_claims)]` from `app/api/v1/__init__.py` left the whole suite green.** Every probe route named `require_claims` in its own signature, and `tests/test_health.py` overrode the dependency outright, so nothing tested inheritance — the exact regression `app/api/v1/__init__.py`'s docstring calls impossible, and exactly what stories 5-8 would ship by omission. A second probe now declares no dependency, no parameters and returns a constant; three behavioural tests plus one structural test cover it. Under mutation the deletion now fails 3 tests.

*Correctness*
2. `app/auth.py` — `_loaded_at`/`_attempted_at` initialised to `0.0` and compared against `time.monotonic()`, whose reference point the language leaves undefined. On a platform counting from process or boot start, the first JWKS fetch is suppressed for the whole refresh interval and every legitimate token 401s at start-up. Now `None`, meaning "never"; a parametrised test stubs `monotonic` to several process-start epochs.
3. `app/auth.py` — three paths escaped as 500 on a request the matrix says must be 401: `_signature_keys` caught only `PyJWKError` while `PyJWK.from_dict` raises `InvalidKeyError`/`binascii.Error`/`TypeError` on malformed `n`/`e`; `_refresh` called `.get` on a document that might have parsed as a list; and `jwt.decode` raises `TypeError`, not a `PyJWTError`, when `exp`/`iat`/`nbf` is a list or object. All three clauses broadened, all three tested.
4. `app/auth.py` — a JWKS entry omitting `alg` was silently dropped, though `alg` is OPTIONAL in RFC 7517 and `use` was already handled permissively. Only an entry that *states* an algorithm outside the policy is dropped now; `jwt.decode`'s `algorithms=` list is what constrains it, unchanged.
5. `app/auth.py` — the JWKS `read()` was unbounded and the cache lock was held across the network call. Capped at 1 MiB, and the fetch moved outside the lock with the refresh slot still claimed under it, so concurrent misses still collapse into one call. The test has the fetcher attempt a non-blocking acquire of the lock it must not be holding.
6. `app/errors.py` — only `ApiError` was handled, so `RequestValidationError` (422), `StarletteHTTPException` (404/405) and unhandled exceptions still returned `{"detail": ...}` or a bare 500 — the shape this module's own docstring says is forbidden. Handlers added for all three. The 422 handler deliberately drops the pydantic error list rather than summarising it: story 5's `PUT /api/v1/session` takes an `li_at` cookie in its body, and FastAPI's default 422 echoes the offending input back to the caller. `FALLBACK_CODES` is kept explicitly disjoint from `ERROR_SPECS`, with a test, so the fallback cannot quietly become the taxonomy story 8 owns.
7. `app/errors.py` — `assert isinstance(...)` replaced with an explicit `raise TypeError`. `python -O` strips the assert, and under it the narrowing fell through to an `AttributeError` — a 500 — instead of failing where the mistake was.

*Config drift — the story-1 `DATABASE_URL` lesson, reapplied*
8. `deploy/keycloak/realm-linkedin.json` — `realm` and `clientId` were hardcoded while the API read `KEYCLOAK_REALM`/`KEYCLOAK_CLIENT_ID` from `.env`, so a `.env` edit alone produced a stack that boots entirely healthy and 401s every token. Both are now `${VAR}` placeholders, as is the audience mapper's target, with the variables passed into the Keycloak container under `:?` guards. Verified empirically on a cold volume: the realm is named `linkedin` from `.env`, and `/realms/${KEYCLOAK_REALM}` as a literal returns 404. A test also requires every placeholder in the export to be both supplied by compose and documented in `.env.example`.
9. `docker-compose.yml` — the export was bind-mounted as a single file. Docker creates an empty **directory** at a bind source that does not exist, so a rename left Keycloak booting healthy with no realm and nothing in the logs. The directory is mounted instead.
10. `deploy/keycloak/realm-linkedin.json` — nothing cross-checked `defaultSignatureAlgorithm` or `access.token.signed.response.alg` against `ALLOWED_ALGORITHMS`; setting either to HS256 left the suite green while producing a realm minting tokens the validator categorically refuses. Asserted now.
11. `tests/test_auth.py` — the credential scan required `publicKey` and `certificate` to hold `${ENV_VAR}` placeholders, which they can never do, and treated `credentials` as a string when a real export writes it as a list of `{type, value}`. As written it would have failed on the first valid re-export with a message pointing at the wrong problem. Narrowed to genuinely secret-bearing keys, with the list form handled, a positive control proving the scan can fail, and a test pinning the narrowing itself.

*Untested branches, each mutation-proven green beforehand*
12. Empty-key-set guard in `_refresh` — and the two cache tests meant to cover it were **vacuous**: with the default 600s TTL a second `signing_key()` for a known `kid` never refreshes, so the document swapped in between calls was never fetched. Both now use a zero TTL and assert the fetch count.
13. `app/config.py` — reverting `keycloak_server_url` to a plain `RequiredSetting`, and deleting the slash-only `raise`, both left the suite green. Normalisation is now parametrised over both base-URL fields. Fixing this surfaced a real gap: `http://host  /` kept its trailing spaces, because the outer strip runs before the slash is removed. Slashes and whitespace are now stripped in one pass.
14. `app/auth.py` — removing `iat` from `REQUIRED_CLAIMS` left the suite green. Every entry now has its own omission test.
15. `tests/test_health.py` — the prefix test read only OpenAPI paths, so a route with `include_in_schema=False` escaping `/api/v1` passed, and an empty paths dict passed vacuously. A real walk over `app.routes` is back, recursing through FastAPI 0.141's `_IncludedRouter` markers to reconstruct full prefixed paths, with a companion test asserting the walk sees strictly more than the document.
16. `tests/test_auth.py` — clock skew was pinned from one side only, so any leeway between one second and an hour passed. A just-outside-leeway case is added. On the future-`iat` half of this finding: PyJWT 2.13 *does* reject a future `iat`, contrary to the report — but it coerces with `int(payload["iat"])`, so `"iat": "1787818576"` and `"iat": true` both validate. The explicit check now enforces the *type*, which is the part PyJWT does not, and the comment says so rather than claiming credit for what the library already does.

*Docs and observability*
17. `README.md` — the graded CAP-3 `curl` referenced `"$KEYCLOAK_CLIENT_SECRET"` while telling the reader to substitute it by hand, so pasted verbatim it posted an empty secret and 401ed. Every block now starts `set -a; . ./.env; set +a` and builds the token endpoint from `$KEYCLOAK_ISSUER_URL`, which also removes the mint-URL mismatch trap by construction. Both blocks were pasted verbatim into a shell and run against the live stack.
18. `app/main.py` — nothing configured logging, so the root logger had no handlers: every `logger.info` was discarded and warnings reached stderr only through `logging.lastResort`, with no timestamp, level or logger name. The README's promise that an operator can read the rejection reason in `docker compose logs api` was simply false. `configure_logging()` is now called from `create_app()`, idempotently. Verified live: `2026-08-27 08:57:42,942 WARNING  app.auth: Rejected request: ...`. No `LOG_LEVEL` variable — story 1 fixed that every `Settings` field is required and non-blank, so an optional one would be a special case in the env contract and a required one would stop every existing `.env` from booting.
19. `app/auth.py` — `kid` and JWS-header parse errors reached log lines unescaped, so a `kid` containing newlines could forge log records. `_loggable()` applies `repr` and a length cap. Verified live: a `kid` of `evil\nFORGED LOG LINE` renders on one line as `'evil\nFORGED LOG LINE'`.
20. `app/main.py`, `app/auth.py` — two comments asserted couplings that do not exist: that handler registration must precede router inclusion (Starlette resolves the handler table per request), and that `TOKEN_URL` is quoted verbatim in the README and read by tests (nothing referenced it). Both corrected to what is actually true — `TOKEN_URL` feeds the OpenAPI description of the bearer scheme.

*Verification of the patches themselves* — 19 mutations were injected one at a time and each was caught by the intended test: the router-level dependency deleted; the monotonic sentinels reverted; `iat` dropped from `REQUIRED_CLAIMS`; both base-URL normalisations reverted; the empty-key-set guard disabled; the `jwt.decode` except clause narrowed; the missing-`alg` filter re-tightened; the fetch moved back inside the lock; each of the three new exception handlers removed; the 422 handler made to echo its input; the realm hardcoded; realm and client signature algorithms set to HS256; the single-file mount restored; a route escaping the prefix behind `include_in_schema=False`; the leeway widened to an hour; the `iat` type check removed; the `assert` restored in place of the `raise`; and `repr` dropped from the log escaper. Two mutations were correctly *not* caught, and are recorded rather than papered over: making a failed fetch return `{"keys": []}` is semantically identical to returning `None` **because** the empty-key-set guard exists, and re-adding `publicKey` to the secret scan is unobservable against an export that has no such key — which is why a separate test now pins that narrowing directly.

## Design Notes

**Audience is the trap.** A bare `client_credentials` token from Keycloak carries `aud: "account"`, not the API's client id, so strict audience validation rejects every legitimate token and the obvious "fix" is to disable the check. Add a dedicated audience mapper to the client in the realm export instead, so real tokens genuinely carry the right `aud` and validation stays strict.

*Resolved:* the mapper plus `fullScopeAllowed: false` produces `aud: "linkedin-profile-api"` — a single string, with `account` gone entirely. Verified on a token minted from the running realm. `tests/test_auth.py::test_keycloaks_default_account_audience_is_401` pins the trap shut from the other side: a token carrying `aud: account` must be rejected, so nobody can make a failure go away by loosening the check.

**Issuer, internal versus external.** `KEYCLOAK_SERVER_URL` is the in-network address (`http://keycloak:8080`) used to fetch JWKS. Once story 2's nginx fronts Keycloak, minted tokens carry an external `iss`. One variable cannot be both — this is the deferred finding from story 1, and this story is where it lands.

*Resolved:* `KEYCLOAK_ISSUER_URL` added. With `KC_HOSTNAME_STRICT=false` Keycloak derives `iss` from the `Host` header of the mint request, which was confirmed empirically: minting the same client through `http://localhost:8080` instead of `http://127.0.0.1:8080` produced `iss: http://localhost:8080/realms/linkedin` and was rejected with `InvalidIssuerError`. That is correct behaviour, but it is a usability edge — the README therefore says, in bold, to mint at exactly `KEYCLOAK_ISSUER_URL`. Pinning `KC_HOSTNAME` would remove the edge entirely; it was left alone because it also redirects the admin console away from the SSH-tunnel address story 2 documents, and that is a deployment decision, not this story's.

**Secrets in the export.** Keycloak's realm import is the one place a client secret could silently reach git. Resolve it empirically against the running container: prefer env substitution in the import, otherwise set the secret post-import via the admin API. Do not commit a real value under any circumstance.

*Resolved:* env substitution works and no admin-API post-step is needed. The export carries `"secret": "${KEYCLOAK_CLIENT_SECRET}"`, compose passes that variable into the Keycloak container under a `:?` guard, and the importer substitutes it. `gitleaks git` over the full history reports no leaks; a `gitleaks dir` scan finds the real secret only in `.env`, which is gitignored — which is exactly the shape the design intends.

**Why the boundary is the router, not the routes.** The dependency hangs on `APIRouter(prefix="/api/v1", dependencies=[...])`, so a route added in stories 5-8 is protected whether or not its author remembered to protect it. The inverse also matters: `/health` is open because it lives *outside* that router, not because an exception was carved into it. There is no list of exempt paths anywhere in the codebase, and therefore no list to get wrong.

**Failing closed when Keycloak is unreachable.** A JWKS fetch failure rejects with 401 rather than 500 or 200. Two consequences, both deliberate: an outage never authorises anyone, and it never surfaces a naked stack trace. It does mean a Keycloak outage is reported to the caller as a bad token — misleading, but the honest alternative (`UPSTREAM_ERROR`, 502) belongs to story 8's taxonomy, and *nothing* justifies the third option. A failed refresh also keeps the keys already held, so a blip cannot invalidate tokens signed by a key already trusted.

**Bounded refetch on an unknown `kid`.** Refetching is how a realm key rotation is picked up without a restart, so it cannot simply be refused; doing it unconditionally turns anyone sending random `kid`s into an amplifier aimed at Keycloak. One refetch per 30s, and the fetch URL is only ever the one built from configuration at import — the token's `jku`/`x5u` headers are never read. An unknown `kid` is a rejection, never an invitation to go and trust a new key.

**The verification suite is offline.** Every token in `tests/test_auth.py` is signed by an RSA key generated in-process, against a `JwksCache` seeded from a local key set shaped exactly like Keycloak's — signing key plus the `use: "enc"` RSA-OAEP key that must never reach signature verification. The suite therefore fails on a laptop with the stack down, which is the only way it can be trusted to fail at all. The one matrix row it cannot hold is minting; that is verified with the `curl` below and pinned in the README.

## Verification

**Commands** — all run 2026-08-27 against the local stack, all as expected:

- `docker compose down -v && docker compose up -d --wait` — exit 0, three services healthy, `GET /realms/linkedin` returns 200 on a cold volume with no console step
- `curl -fsS -d grant_type=client_credentials -d client_id=... -d client_secret=... http://127.0.0.1:8080/realms/linkedin/protocol/openid-connect/token` — 200, `access_token` present, `expires_in: 900`, `num_redirects: 0` under `-L`
- `docker build --target test -t lps-test . && docker run --rm lps-test` — **176 passed**, no warnings (97 before review pass 1)
- `curl -fsS http://127.0.0.1:8000/health` — `{"status":"ok"}` with no token
- `docker run --rm -v "$PWD":/repo -w /repo zricethezav/gitleaks:latest git --no-banner` — 4 commits scanned, **no leaks found**. A `gitleaks dir` scan finds the real secret only in `.env` and in gitignored `_bmad/` tooling, never in anything tracked.

**Live end-to-end**, against the running realm rather than the offline suite. The `/api/v1` seam still has no routes (stories 5-8 own them), so a throwaway container was started on the compose network with one probe route mounted from a bind mount — never committed, never in an image, torn down afterwards. Every matrix row, verified with a token minted from the real realm and a real JWKS fetch over the compose network:

| Row | Result | Logged reason |
|---|---|---|
| Valid token | 200, `sub` reached the route, `aud: linkedin-profile-api` | — |
| No header | 401 typed envelope, `WWW-Authenticate: Bearer` | `no bearer credentials` |
| `Authorization: potato` | 401 typed envelope | `no bearer credentials` |
| `Bearer not.a.jwt` | 401 typed envelope | `unparseable JWS header` |
| Foreign realm (`master`, real token) | 401 typed envelope | `no published signature key for kid=...` |
| Wrong issuer (same client, minted via `localhost` not `127.0.0.1`) | 401 typed envelope | `InvalidIssuerError` |
| Wrong audience (second client created in the same realm, same `iss`, same `kid`) | 401 typed envelope | `InvalidAudienceError` |
| `GET /health`, no token | 200 `{"status":"ok"}` | — |

Re-run after review pass 1 against a cold volume, this time through a probe route declaring **no dependency of its own** — the router-level boundary, end to end: 200 with a real token, 401 typed without one, 401 typed for a hostile `kid`. `docker compose logs` showed `2026-08-27 08:57:42,942 WARNING  app.auth: Rejected request: no published signature key for kid='evil\nFORGED LOG LINE'` — timestamped, named, and on one line, so neither the operator promise nor the log-forgery guard is taken on trust. The unknown-path and wrong-method responses on the live stack carry the same envelope (`{"error": {"code": "NOT_FOUND", ...}}`, `METHOD_NOT_ALLOWED`), and both README `curl` blocks were pasted verbatim into a shell and returned a token.

Also re-verified after templating the realm export: on a cold volume `/realms/linkedin` returns 200 (the name coming from `.env`), while `/realms/${KEYCLOAK_REALM}` as a literal returns 404 — the substitution really happened rather than the placeholder being stored.

The scratch `other-client` created for the audience row was deleted from the realm afterwards; it exists in neither the export nor the running realm.

**OpenAPI**, on the same probe: `components.securitySchemes.KeycloakBearer` is `{type: http, scheme: bearer}`, the `/api/v1` route carries `security: [{"KeycloakBearer": []}]` and a 401 response referencing `#/components/schemas/ErrorEnvelope`, and `/health` carries no `security` key at all.

## Suggested Review Order

**The boundary — start here**

- Auth attaches to the router, so a story-5-8 route is protected by construction rather than by remembering to be.
  [`v1/__init__.py:21`](../../../../app/api/v1/__init__.py#L21)

- Two Keycloak URLs, and why one variable cannot be both. The story-1 deferred finding, landed.
  [`config.py:101`](../../../../app/config.py#L101)

**Validation policy — the parts worth disagreeing with**

- Asymmetric algorithms only. `none` and `HS256` are closed by omission, not by a special case.
  [`auth.py:84`](../../../../app/auth.py#L84)

- `iss` compared by exact equality against the *external* base, never the in-network one.
  [`auth.py:57`](../../../../app/auth.py#L57)

- Required-claim list: absent `exp` must fail closed, absent `aud` must fail the check rather than skip it.
  [`auth.py:99`](../../../../app/auth.py#L99)

- The `typ` check — the only thing separating an ID token from an access token here.
  [`auth.py:106`](../../../../app/auth.py#L106)

- The single `jwt.decode` call: every option explicit, nothing left to a library default.
  [`auth.py:311`](../../../../app/auth.py#L311)

**The JWKS cache**

- Bounded refetch on an unknown `kid`; rotation still picked up, amplification not.
  [`auth.py:174`](../../../../app/auth.py#L174)

- A failed refresh keeps the keys already held — a Keycloak blip must not invalidate good tokens.
  [`auth.py:198`](../../../../app/auth.py#L198)

- Keycloak publishes an `enc` key beside the `sig` key; only one may reach verification.
  [`auth.py:219`](../../../../app/auth.py#L219)

**The error envelope, shaped for story 8**

- Table-driven: story 8 adds seven rows and changes nothing else.
  [`errors.py:62`](../../../../app/errors.py#L62)

- One message for every rejection reason; the specific reason goes to the log, not the body.
  [`errors.py:117`](../../../../app/errors.py#L117), [`auth.py:340`](../../../../app/auth.py#L340)

**The realm, reproducible and secret-free**

- `${KEYCLOAK_CLIENT_SECRET}` — the one spelling Keycloak 26.7.2 actually substitutes.
  [`realm-linkedin.json:29`](../../../../deploy/keycloak/realm-linkedin.json#L29)

- The audience mapper, with `fullScopeAllowed: false` beside it. Together they are why `aud` is right.
  [`realm-linkedin.json:54`](../../../../deploy/keycloak/realm-linkedin.json#L54)

- `--import-realm` plus a read-only single-file mount; `IGNORE_EXISTING` and what that costs.
  [`docker-compose.yml:46`](../../../../docker-compose.yml#L46)

**Tests that would catch a regression**

- `aud: account` must be rejected — the trap the mapper exists to avoid, pinned shut from both sides.
  [`test_auth.py`](../../../../tests/test_auth.py)

- The HMAC forgery is assembled by hand, because relying on PyJWT refusing to *encode* it tests the wrong side.
  [`test_auth.py`](../../../../tests/test_auth.py)

- Credential-bearing keys are walked structurally over parsed JSON; a raw substring scan trips on the word `client_credentials`.
  [`test_auth.py`](../../../../tests/test_auth.py)
