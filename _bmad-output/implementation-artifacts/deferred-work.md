- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/1-project-skeleton-env-config-local-parity.md`
  summary: SESSION_ENCRYPTION_KEY is validated only as non-empty, so the .env.example placeholder boots healthy and fails at the first cookie write.
  evidence: A real Fernet-key validator needs `cryptography` in the runtime requirements, which story 5 adds when it builds the vault. Adding it in story 1 would pull a dependency for a code path that does not exist yet.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/1-project-skeleton-env-config-local-parity.md`
  summary: KEYCLOAK_SERVER_URL expresses only the in-network Keycloak address, but token validation also needs the external issuer that appears in JWTs minted through nginx.
  evidence: One variable cannot carry both the internal JWKS endpoint and the expected external `iss`. Story 3 owns JWT validation and should add KEYCLOAK_ISSUER_URL rather than renegotiating .env under deadline.
  status: RESOLVED by story 3 — KEYCLOAK_ISSUER_URL added to Settings, .env.example and .env; app/auth.py compares `iss` against it by exact equality.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/1-project-skeleton-env-config-local-parity.md`
  summary: Nothing creates the `linkedin` realm or its clients, so a fresh `docker compose up` yields a Keycloak with no realm despite KEYCLOAK_REALM being required config.
  evidence: Story 3 explicitly owns the realm, the confidential service-account client, and exporting the realm config into the repo. Needs `--import-realm` plus a mounted export at that point.
  status: RESOLVED by story 3 — deploy/keycloak/realm-linkedin.json is imported by `start --import-realm` from a read-only mount, verified on a cold volume.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/1-project-skeleton-env-config-local-parity.md`
  summary: DATABASE_URL is required config but no database driver, ORM, or migration tooling is in requirements.txt.
  evidence: Deliberate under the required-now-used-later design, but stories 5-8 must add the driver and a migrations story before the first table exists.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/1-project-skeleton-env-config-local-parity.md`
  summary: The API connects to Postgres as the same superuser that owns Keycloak's identity tables, in the same database and default schema.
  evidence: Confirmed in docker-compose.yml — the api service receives POSTGRES_USER/POSTGRES_PASSWORD. A bug in the API reaches Keycloak's tables. Stories 5-8 should namespace application tables and ideally add a least-privilege role.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/1-project-skeleton-env-config-local-parity.md`
  summary: No log rotation and no memory limits on the postgres or api services, on a host where only Keycloak is capped.
  evidence: json-file logs grow unbounded on the Ampere instance, which is a deployment concern story 2 owns along with the nginx and load-balancer wiring.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/1-project-skeleton-env-config-local-parity.md`
  summary: Transitive dependencies float despite requirements.txt claiming exact pinning; fastapi declares starlette>=0.46.0 with no upper bound.
  evidence: The test image's ability to import TestClient depends on which starlette the build resolves, since starlette <1.0 imports httpx and 1.6.0 prefers httpx2. A lockfile or hash pinning would close it; out of scope for a boring-on-purpose requirements.txt.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/1-project-skeleton-env-config-local-parity.md`
  summary: Nothing enforces gitleaks or the test suite on push; pre-commit is an opt-in local install a contributor can skip.
  evidence: Story 9 audits the full history for secrets. CI running `pre-commit run --all-files` and `docker build --target test` would catch a leak before it reaches history rather than after.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/1-project-skeleton-env-config-local-parity.md`
  summary: A green `docker compose up --wait` proves only that processes booted, not that the API can reach Postgres or Keycloak.
  evidence: /health checks no dependencies by design, which is correct for liveness. A separate readiness endpoint would make the deployed stack's true state visible to story 2's nginx and load-balancer health checks.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/3-keycloak-realm-and-jwt-validation.md`
  summary: A JWKS fetch failure is reported to the caller as 401 UNAUTHENTICATED, so a Keycloak outage looks to the evaluator like a bad token.
  evidence: Failing closed is correct and 500 is forbidden, but the honest code is UPSTREAM_ERROR/502, which belongs to story 8's taxonomy. app/auth.py already logs the real reason at ERROR; story 8 should map SigningKeyUnavailable-due-to-fetch-failure separately from unknown-kid.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/3-keycloak-realm-and-jwt-validation.md`
  summary: Keycloak's realm import runs with strategy IGNORE_EXISTING, so KEYCLOAK_CLIENT_SECRET in .env only takes effect on a realm that does not exist yet.
  evidence: Rotating the secret on an existing stack needs `docker compose down -v` or an admin-console change. Documented in README and .env.example. OVERWRITE_EXISTING was not chosen because it would destroy realm users on every restart, and CAP-4 binds stored sessions to Keycloak subjects.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/3-keycloak-realm-and-jwt-validation.md`
  summary: The `location /realms/` block in deploy/nginx/linkedin-profile-api.conf is still commented out, so the evaluator's token-minting curl has no public path on the deployed host.
  evidence: Story 2 deferred it to "the realm story", but enabling it requires an nginx reload on the instance and a redeploy of this realm, neither of which this story can verify from the dev machine. Story 9 owns the final deploy; enabling and testing that block is a prerequisite for CAP-3 against the public URL, not merely a nicety.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/3-keycloak-realm-and-jwt-validation.md`
  summary: With KC_HOSTNAME_STRICT=false, Keycloak derives `iss` from the Host header of the mint request, so minting via `localhost:8080` instead of `127.0.0.1:8080` yields a token the API correctly rejects.
  evidence: Verified empirically. Pinning KC_HOSTNAME to KEYCLOAK_ISSUER_URL would make `iss` deterministic, but it also redirects the admin console away from the SSH-tunnel address story 2 documents; KC_HOSTNAME_ADMIN would be needed alongside it. A deployment decision for story 9, mitigated for now by a bolded README instruction.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/3-keycloak-realm-and-jwt-validation.md`
  summary: JwksCache holds its lock across the network fetch, so concurrent requests during a refresh serialise behind a 5s timeout.
  evidence: Deliberate — it collapses a thundering herd into one fetch, and the cached-hit path never touches the network. Only worth revisiting if the service ever sees real concurrency, which an evaluation workload will not.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/3-keycloak-realm-and-jwt-validation.md`
  summary: Nothing exercises the realm export in CI; the offline suite asserts its contents but only a human running `docker compose down -v && up` proves Keycloak accepts it.
  evidence: The 255-character `description` limit was found exactly this way, by an import that refused to start. A CI job doing a cold compose boot and minting a token would catch the next such trap before a deploy does.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/3-keycloak-realm-and-jwt-validation.md`
  summary: A Keycloak outage answers a perfectly valid token with 401 UNAUTHENTICATED, retryable false, telling the caller not to retry when retrying is exactly right.
  evidence: JWKS fetch failure is indistinguishable from a bad token in the current error table. Story 8 owns UPSTREAM_ERROR/502 and should route IdP unavailability there, and the README's enumerated 401 causes should gain this case.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/3-keycloak-realm-and-jwt-validation.md`
  summary: require_claims authenticates but never authorizes - no azp, scope, or role check beyond audience.
  evidence: Any other realm client carrying an audience mapper aimed at linkedin-profile-api gets full access. Deliberately not enforced now because Should-tier Google SSO would mint user tokens with a different azp; revisit if that lane is built, and add a PERMISSION_DENIED row to the error table if so.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/3-keycloak-realm-and-jwt-validation.md`
  summary: A revoked service account or rotated secret stays accepted for up to the 900s access-token lifespan.
  evidence: Inherent to stateless JWT validation with no introspection. Acceptable for evaluation, but story 9's known-limitations section should state the revocation window rather than leave it implied.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/3-keycloak-realm-and-jwt-validation.md`
  summary: KC_HOSTNAME is unset with KC_HOSTNAME_STRICT false, so the token issuer follows the request Host header and localhost vs 127.0.0.1 mint mutually unacceptable tokens.
  evidence: Mitigated only by a README paragraph. Pinning KC_HOSTNAME to KEYCLOAK_ISSUER_URL (plus KC_HOSTNAME_ADMIN for the admin console) would delete the footgun instead of documenting it - a deployment-shaped change that belongs with story 2.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/3-keycloak-realm-and-jwt-validation.md`
  summary: keycloak_client_secret is a required setting the API process never consumes; it only validates tokens, never mints them.
  evidence: Compose delivers it to the api container via env_file, so a secret with no consumer widens the blast radius. Drop the field or record which story consumes it.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/3-keycloak-realm-and-jwt-validation.md`
  summary: No documented procedure for regenerating the realm export, and the natural method emits the literal client secret and realm key material.
  evidence: Exporting from a running Keycloak produces a file that would leak on commit; only the test suite or the opt-in pre-commit hook would catch it. Story 9 should add the re-export recipe to deploy/README.md.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/3-keycloak-realm-and-jwt-validation.md`
  summary: app/errors.py carries a fallback code set (NOT_FOUND, METHOD_NOT_ALLOWED, INVALID_REQUEST, BAD_REQUEST, INTERNAL_ERROR) that is deliberately disjoint from response-schema.md's taxonomy.
  evidence: Added in review pass 1 so that 404/405/422/500 wear the typed envelope, which nothing else guaranteed. They are shape insurance, not taxonomy. Story 8 should replace each with a real code (INVALID_URL for the validation case, UPSTREAM_ERROR for the failure case) and must NOT delete the fallback itself - a test keeps the two sets disjoint.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/3-keycloak-realm-and-jwt-validation.md`
  summary: Logging is configured at a hardcoded INFO with no LOG_LEVEL variable.
  evidence: Story 1 fixed that every Settings field is required and non-blank, so an optional LOG_LEVEL would be a special case in the env contract and a required one would stop every existing .env from booting. If a later story needs adjustable verbosity it should decide how optional settings fit the contract first, not add one ad hoc.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/3-keycloak-realm-and-jwt-validation.md`
  summary: Every rejected request logs at WARNING, so an unauthenticated flood is also a log flood.
  evidence: Acceptable for an evaluation service and it is the only debuggability channel the single-message 401 leaves. If rate limiting is ever built (Should tier), the rejection log should be sampled or moved behind it.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/2-walking-skeleton-deploy.md`
  summary: An unauthenticated caller can enumerate which /api/v1 paths exist, because a non-existent path returns 404 while a real one returns 401.
  evidence: Verified on the deployed host - GET /api/v1/x without a token returns 404 NOT_FOUND, since FastAPI resolves routing before router-level dependencies run. No data is exposed, but it distinguishes real routes from absent ones. Story 8 could normalise unmatched /api/v1 paths to 401.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/4-voyager-client-raw-profile-json.md`
  summary: The Voyager endpoint map is verified against exactly one profile — the developer's own — so per-profile shape variation is untested.
  evidence: The live check fetches only the session owner, by design (every fetch spends real quota against a real account, and a third party's profile is not the author's data to spend). Profiles with no certifications, a hidden headline, or a non-en_US primary locale may carry shapes no fixture mirrors. Story 6's mapper must therefore treat every field as optional rather than trusting the measured shape table.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/4-voyager-client-raw-profile-json.md`
  summary: A section failing with SESSION_EXPIRED or RATE_LIMITED degrades to a partial profile rather than failing the whole fetch.
  evidence: The Design Notes say a section that errors should degrade and only a core failure should abort, and that is what is implemented. But those two codes are systemic, not per-section: if languages is throttled the others probably were too, and answering 200-with-partial is arguably less honest than answering 429. The client records the code per section so story 6/8 can decide; they should decide deliberately rather than inherit this.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/4-voyager-client-raw-profile-json.md`
  summary: Section pagination is not followed — a profile with more than 100 entries in one section is still silently truncated.
  evidence: Found live, not hypothesised: the default page size is 20 and the developer's own profile has 33 skills, so the first implementation returned 20 of 33 with a 200 and no error. Fixed by requesting `count=100`, which returns all 33 in the SAME single call and so does not touch the per-profile call budget. Beyond 100 the shortfall is visible rather than fixed — `SectionFetch.reported_total` carries `data.paging.total` and `RawProfile.truncated_sections` names any section where it exceeds what came back. Story 6 must act on that rather than presenting a truncated list as a complete history; following pages would multiply the call count and is Ask First.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/4-voyager-client-raw-profile-json.md`
  summary: There is no retry and no timeout budget across the whole fetch, only a per-call timeout.
  evidence: Deliberate — a retry multiplies quota spend and the failures worth retrying are the ones an immediate retry makes worse. But five concurrent sections each with a 15s timeout means a wedged upstream holds a request for 15s, and nothing caps the total. Story 7's stale-serve is the intended recovery; if it lands, a whole-fetch deadline belongs with it.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/4-voyager-client-raw-profile-json.md`
  summary: `LINKEDIN_DEV_COOKIE` is the first optional field in the env contract, weakening the "every variable is required, blank fails at boot" invariant story 1 established.
  evidence: Argued at the field and asserted in `tests/conftest.py` (OPTIONAL_SETTINGS is pinned, so a second optional field fails a test). The alternative — a required variable no deployment uses — is worse. Story 3's deferred LOG_LEVEL note asked that optional settings be decided deliberately before one is added ad hoc; this is that decision, and it is narrow: developer-only, read by nothing on the request path.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/4-voyager-client-raw-profile-json.md`
  summary: The type alias in `app/config.py` is named `OptionalSecretSetting` specifically so gitleaks' `linkedin-client-id` rule does not fire on the field declaration.
  evidence: That rule matches `linkedin` followed by a 14-16 character token, so `linkedin_dev_cookie: OptionalSecret = Field(` reads as a leaked credential and the pre-commit hook refuses the commit. Documented at the alias. A `.gitleaksignore` or an allowlist rule would be the honest fix; naming around a scanner is a trap for whoever renames it next.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/4-voyager-client-raw-profile-json.md`
  summary: A challenge is detected by final URL and content type, so a challenge served as valid JSON at the requested URL would be read as data.
  evidence: LinkedIn has no reason to do this today and both observed challenge forms (redirect to /authwall, HTML in place) are caught. Adding a body-shape check — "a 200 that parses but carries neither `data` nor `included`" — would close it, at the cost of guessing about payloads that were never observed.

