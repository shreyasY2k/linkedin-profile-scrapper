- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/1-project-skeleton-env-config-local-parity.md`
  summary: SESSION_ENCRYPTION_KEY is validated only as non-empty, so the .env.example placeholder boots healthy and fails at the first cookie write.
  evidence: A real Fernet-key validator needs `cryptography` in the runtime requirements, which story 5 adds when it builds the vault. Adding it in story 1 would pull a dependency for a code path that does not exist yet.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/1-project-skeleton-env-config-local-parity.md`
  summary: KEYCLOAK_SERVER_URL expresses only the in-network Keycloak address, but token validation also needs the external issuer that appears in JWTs minted through nginx.
  evidence: One variable cannot carry both the internal JWKS endpoint and the expected external `iss`. Story 3 owns JWT validation and should add KEYCLOAK_ISSUER_URL rather than renegotiating .env under deadline.

- source_spec: `_bmad-output/specs/spec-linkedin-profile-scraper/stories/1-project-skeleton-env-config-local-parity.md`
  summary: Nothing creates the `linkedin` realm or its clients, so a fresh `docker compose up` yields a Keycloak with no realm despite KEYCLOAK_REALM being required config.
  evidence: Story 3 explicitly owns the realm, the confidential service-account client, and exporting the realm config into the repo. Needs `--import-realm` plus a mounted export at that point.

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
