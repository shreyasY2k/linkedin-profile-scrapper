---
title: 'Project skeleton, environment config, and local parity'
type: 'feature'
created: '2026-08-27'
status: 'done'
baseline_commit: 'NO_VCS'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/specs/spec-linkedin-profile-scraper/SPEC.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The repository is empty, yet stories 2–9 all assume a FastAPI service, an identity provider, and a database behaving identically on the laptop and on the Oracle ARM instance. Unless that foundation and its secret-hygiene rules are fixed at the first commit, every later story retrofits its own conventions under deadline.

**Approach:** Scaffold the `app/` package with one twelve-factor settings object, a Dockerfile, and one `docker compose` bringing up API + Keycloak + Postgres with healthchecks and loopback-only ports; initialise git with `.env` ignored before anything is committed.

## Boundaries & Constraints

**Always:**
- Every setting arrives as an env var, read once through a single `pydantic-settings` object. No code may branch on an environment name.
- `.env.example` committed with dummy values; `.env` gitignored in the commit that creates the repo, not later.
- Published ports bind `127.0.0.1` only — story 2 puts nginx in front.
- Images pinned to explicit tags publishing `linux/arm64` manifests (ARM Ampere target, Apple Silicon dev machine).
- One `app/` package with the `/api/v1` router seam already carved out, so stories 3–8 do not renegotiate layout.

**Ask First:**
- Any runtime dependency beyond what booting the skeleton needs.
- Any compose topology change — a fourth service, a different datastore.
- Committing any value that could plausibly be a real credential.

**Never:**
- No business logic: no Voyager client, profile mapping, session vault, JWT validation, or cache. Stories 3–8 own those.
- No nginx config, load-balancer wiring, or deployment (story 2).
- No secret in the tree or in history, including the first commit.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Behavior | Error Handling |
|---|---|---|---|
| Cold bring-up | Clean clone, `.env` from `.env.example` | `docker compose up -d --wait` exits 0, all three healthy | N/A |
| Liveness | `GET /health` | `200 {"status": "ok"}` | N/A |
| Missing config | A required env var unset | API exits non-zero at startup, stderr names the field | Validation error at import; never reports healthy |
| Start ordering | Postgres not yet accepting connections | Keycloak waits for Postgres to pass its healthcheck | `depends_on: condition: service_healthy` |
| Exposure | Scan of the host's public interface | No stack port answers | Loopback-only publishing |
| Secret hygiene | `.env` present with real values | Untracked, invisible to `git status` | `.gitignore` from first commit |

</frozen-after-approval>

## Code Map

Greenfield. The tree holds only `.claude/`, `_bmad/`, `_bmad-output/` — no code, no git history, no reuse surface. Everything below is created here.

- `_bmad-output/specs/spec-linkedin-profile-scraper/SPEC.md` — read-only; source of the config, hygiene, topology and runtime constraints restated above.
- Host toolchain verified: Docker 29.2.1, Compose v5.0.2, git 2.50.1. **Host Python is 3.9.6**, below the 3.11+ floor — development runs in the container and the host interpreter is never a dependency.

## Tasks & Acceptance

**Execution:**
- [x] `.gitignore` — write, then `git init` and `git remote add origin git@github.com:shreyasY2k/linkedin-profile-scrapper.git` — ignore file must precede the first commit so `.env`, `__pycache__/`, `.venv/`, `*.pem` are never tracked. Do not push or fetch; the human owns the first push
- [x] `.env.example` — every variable `Settings` reads, dummy values — the env contract stories 3–8 extend
- [x] `requirements.txt` — pin fastapi, uvicorn[standard], pydantic, pydantic-settings — boring on purpose; readable without a lockfile tool
- [x] `app/config.py` — `Settings(BaseSettings)` + module-level instance — one config read; required fields fail at boot, not at first use
- [x] `app/main.py` — `create_app()` with OpenAPI title/version, routers mounted — that OpenAPI doc is story 9's API documentation
- [x] `app/api/health.py` — unauthenticated `GET /health` — container healthcheck and story 2's walking skeleton both need it
- [x] `app/api/v1/__init__.py` — `APIRouter(prefix="/api/v1")`, mounted, no routes — the prefix `response-schema.md` fixes; the seam for stories 5–8
- [x] `Dockerfile` — slim Python base, non-root user, deps layer before source
- [x] `docker-compose.yml` — api, keycloak, postgres; healthchecks, `depends_on` conditions, loopback ports, named Postgres volume
- [x] `.pre-commit-config.yaml` — gitleaks hook — cheap insurance against story 9's history audit
- [x] `README.md` — setup section only, flagged as story 9's to finish
- [x] `tests/test_health.py` — cover the matrix: `/health` returns 200, and `Settings` raises when a required var is absent

**Acceptance Criteria:**
- Given a clean clone and `cp .env.example .env`, when `docker compose up -d --wait` runs, then it exits 0 with all services healthy and no further manual step.
- Given the repository at its first commit, when history is searched for credential patterns, then nothing real is found and `.env` is untracked.
- Given `docker compose down -v` then `up -d --wait`, then the stack returns healthy from empty volumes.

## Spec Change Log

**2026-08-27 — implementation additions beyond the task list.** None changes the frozen intent; all are recorded here rather than left implicit.

- `requirements-dev.txt` + a `test` Dockerfile stage. The runtime image must stay exactly `requirements.txt`, but the test needs a client and a runner, and host Python is 3.9. Test deps install into a separate stage (`docker build --target test`) that never ships. `httpx2==2.12.0` rather than `httpx`, which starlette's `TestClient` now deprecates.
- `.dockerignore`. Keeps `.env` and `*.pem` out of the build context entirely, not merely out of the image.
- `.claude/` and `_bmad/` added to `.gitignore` — local agent/tooling installs, not project source. `_bmad/_config/files-manifest.csv` also carries file checksums that trip gitleaks' `generic-api-key` rule, which would fail the pre-commit hook on every commit. `_bmad-output/` is deliberately **not** ignored and **not** committed: whether the public repo carries the planning artifacts is the author's call.
- Keycloak shares the application's Postgres **database**, not just the instance — consistent with the SPEC's "cached records live in the Postgres instance already required by Keycloak", and avoids a bootstrap script for a second database. Keycloak owns the default `public` schema; stories 5–8 should namespace their tables.
- First commit contains the skeleton only. `git remote add origin` done; **not pushed** — the human owns the first push.

**2026-08-27 — review pass 1 (no spec loopback). 12 patches applied to story-1 code.** Nothing in the frozen block changed; the intent is unchanged and every patch tightens an existing guarantee rather than adding surface.

*Leak of test material into the shipping artifact*
1. `Dockerfile` — `COPY tests ./tests` moved out of `base` into the `test` stage; the runtime image now holds only `app/` and `requirements.txt`. `docker-compose.yml` and `.env.example` are copied into the `test` stage for the contract tests below.

*Fail-fast contract, which was weaker than it read*
2. `app/main.py` — deleted `_ = settings.keycloak_realm` and its comment. `from app.config import settings` had already run `Settings()` at module scope, so the line validated nothing; the comment claimed work it did not do. The import is kept (aliased, `noqa: F401`) because it *is* the enforcement.
3. `app/config.py` — `RequiredSetting = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]`. A whitespace-only value passed a bare `min_length=1` and booted a service with an all-spaces encryption key.
9. `tests/test_health.py` — the boot guarantee is now asserted against the *boot path*: a subprocess runs `python -c "import app.main"` with a scrubbed env minus one required variable, asserting non-zero exit and the field name in stderr. It runs from a scratch cwd with the repo on `PYTHONPATH`, so a developer's real `.env` (which `env_file=".env"` resolves relative to cwd) cannot rescue it. A positive control asserts the same call succeeds with a complete env.

*Configuration that could drift silently*
4. `.env.example` + `docker-compose.yml` + `README.md` — Postgres credentials were encoded twice, in `POSTGRES_*` and again inside `DATABASE_URL`. The api service now composes `DATABASE_URL` from the `POSTGRES_*` parts in compose `environment:` (which overrides `env_file`), making those three the single source of truth. Because `/health` checks no dependencies, the old mismatch would have stayed green until story 3 opened the first connection. `.env.example` keeps a `DATABASE_URL=` line, annotated as compose-derived and relevant only outside compose. The README no longer implies `POSTGRES_PASSWORD` alone is enough.
5. `docker-compose.yml` — `:?` guards added to `KC_DB_URL` / `KC_DB_USERNAME` / `KC_DB_PASSWORD`, matching the postgres service. Keycloak could otherwise start against a blank database name instead of failing with a message naming the variable.
6. `docker-compose.yml` — postgres healthcheck gains `-h 127.0.0.1`. The comment claimed `-U`/`-d` prevented a false green during `initdb`; they do not. Only forcing a TCP check does, since the entrypoint's temporary server listens on the unix socket alone.

*Deployment wiring, moved earlier rather than rediscovered*
7. `Dockerfile` — uvicorn CMD gains `--proxy-headers --forwarded-allow-ips "*"`. Keycloak already had `KC_PROXY_HEADERS`; without the API's equivalent, story 2's nginx would leave the app reading the proxy's IP as the client and building `http://` URLs for `https://` requests.

*Tests that could not fail*
8. `tests/conftest.py` — `os.environ.setdefault` replaced with unconditional assignment: an ambient `DATABASE_URL` or `KEYCLOAK_*` in the developer's shell silently won, running the suite against their live configuration. A collection-time assertion now pins `{k.lower() for k in REQUIRED_ENV} == set(Settings.model_fields)`, so a field added to `Settings` without a `REQUIRED_ENV` entry fails loudly instead of quietly losing its coverage.
10. `tests/test_health.py` — `assert v1_router.routes == []` replaced. It succeeded only while the project made no progress and story 5 would have had to delete it. Now: every v1 route's path starts `/api/v1`, plus a probe route proving the seam is genuinely mounted on the built app (deleting `include_router(v1.router)` previously failed no test).
11. `tests/test_health.py` — the `.env.example` contract test no longer `pytest.skip`s when the file is absent; a `.dockerignore` or `Dockerfile` edit that stopped shipping it would have turned the only contract test into a green skip. It is now a hard assertion, and a second test parses `${VAR}` interpolations out of `docker-compose.yml` (ignoring comment lines) and requires a `VAR=` line in `.env.example` for each — covering the five compose-only variables `Settings` never sees.
12. `app/api/health.py` — `HealthResponse.status` is `Literal["ok"]`, so the OpenAPI document story 9 ships names the only legal value rather than promising "some string".

*Findings deliberately not acted on* — each disproved by running the code, per the review coordinator: `httpx2` is real and starlette 1.6.0 prefers it, so the pin stands; the Keycloak `/dev/tcp` healthcheck works (Keycloak answers `HTTP/1.0 200 OK`); and `extra="forbid"` on `Settings` would break the api container, because compose's `env_file` injects `POSTGRES_*` — `extra="ignore"` stays, now with a comment saying why.

*Verification of the patches themselves* — four mutations were injected and each was caught by the intended test: unmounting the v1 seam, dropping the whitespace strip, commenting out a compose-only variable in `.env.example`, and making config validation lazy. A complete `.env` planted at the repo root did not rescue the boot-death test.

## Design Notes

**Required-now, used-later.** `Settings` declares the full env surface the SPEC implies — database URL, Keycloak realm and client, session encryption key — as required, though stories 5–8 consume them. A deploy missing `SESSION_ENCRYPTION_KEY` should die at boot on evaluation eve, not at the first `PUT /api/v1/session`. There is deliberately no `APP_ENV`: with no environment name in the config surface, nothing can branch on one.

**Pinned runtime** (verified 2026-08-27 against arm64 manifests and PyPI wheel coverage; do not substitute without re-verifying):

```
quay.io/keycloak/keycloak:26.7.2
postgres:18.6-trixie
python:3.13-slim-trixie
```

Traps confirmed in that verification, each of which silently breaks the stack:

- **Postgres 18 moved its volume path.** Mount `pgdata:/var/lib/postgresql`, *not* `/var/lib/postgresql/data` — the old path now fails at container start. Healthcheck must pass both flags: `pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"`; bare `pg_isready` reports green during init while only a local socket is up.
- **Keycloak admin vars.** `KC_BOOTSTRAP_ADMIN_USERNAME` / `KC_BOOTSTRAP_ADMIN_PASSWORD`. `KEYCLOAK_ADMIN` has been deprecated since 26.0 and is ignored — the container starts with no admin and story 3 has nothing to log into.
- **Keycloak health is on port 9000, not 8080**, and needs `KC_HEALTH_ENABLED=true`. The image ships no curl or wget, so the healthcheck is the bash TCP form: `{ printf 'HEAD /health/ready HTTP/1.0\r\n\r\n' >&0; grep 'HTTP/1.0 200'; } 0<>/dev/tcp/localhost/9000`. If bash turns out to be absent, fall back to `service_started` for Keycloak and say so — do not silently drop the dependency condition.
- **Keycloak sizes its heap as a percentage of the container limit**, so with no limit it takes ~70% of the host. Set an explicit memory limit on that service.
- **Use `start`, not `start-dev`.** `start-dev` has insecure defaults and would make local differ from deployed by command rather than by env — the one thing this story forbids. `start --optimized` needs a prior `kc.sh build` in a custom image; plain `start` does an implicit build at boot and works from the stock image. Pair with `KC_HOSTNAME_STRICT=false`, `KC_HTTP_ENABLED=true`, `KC_PROXY_HEADERS=xforwarded` so story 2's nginx terminates TLS without a Keycloak change.
- **No `platform:` key anywhere.** All three images are native arm64; adding one drags in QEMU, under which the Keycloak JVM is slow and crash-prone.
- **Compose has no `version:` key** — obsolete and warned about.

For dependencies stories 3–8 will add: prefer `pyjwt` over the unmaintained `python-jose`, and `psycopg[binary]` or `asyncpg` over `psycopg2-binary`, which lags on aarch64 wheels and would force a source build in a slim image with no compiler.

## Verification

**Commands:**
- `docker compose config -q` — expected: parses clean, no `version:` key
- `docker compose up -d --wait` — expected: exit 0, all services healthy
- `curl -fsS http://127.0.0.1:8000/health` — expected: `{"status":"ok"}`
- `docker compose run --rm --no-deps -e SESSION_ENCRYPTION_KEY= api python -c "import app.config"` — expected: non-zero, names the missing field
- `docker compose ps --format '{{.Ports}}'` — expected: every published port on `127.0.0.1`
- `git status --porcelain` — expected: `.env` absent

## Suggested Review Order

**The configuration contract — start here**

- The single config read; importing this module *is* the validation, so a broken env kills the process.
  [`config.py:82`](../../../../app/config.py#L82)

- `RequiredSetting` strips before length-checking, so a whitespace-only value fails like an absent one.
  [`config.py:28`](../../../../app/config.py#L28)

- Six fields required now, consumed by stories 5–8 — a bad deploy dies at boot, not at first use.
  [`config.py:52`](../../../../app/config.py#L52)

**Credential single-source-of-truth**

- DSN composed from the `POSTGRES_*` parts, so rotating a password cannot leave the API stale.
  [`docker-compose.yml:98`](../../../../docker-compose.yml#L98)

- `:?` guards turn a missing variable into a named error instead of a blank credential.
  [`docker-compose.yml:13`](../../../../docker-compose.yml#L13)

**Stack topology and liveness**

- Keycloak on `start` not `start-dev`, so local and deployed differ by env alone, never by command.
  [`docker-compose.yml:39`](../../../../docker-compose.yml#L39)

- Health probe on port 9000 via bash `/dev/tcp` — the image ships neither curl nor wget.
  [`docker-compose.yml:73`](../../../../docker-compose.yml#L73)

- Postgres 18 moved its volume path; the old `/data` mount fails at container start.
  [`docker-compose.yml:13`](../../../../docker-compose.yml#L13)

- Every published port bound to loopback; host nginx is the only public face (story 2).
  [`docker-compose.yml:84`](../../../../docker-compose.yml#L84)

**Application surface**

- Liveness is deliberately dependency-free: an outage downstream must not restart this container.
  [`health.py:29`](../../../../app/api/health.py#L29)

- The `/api/v1` seam, carved and mounted but empty, so stories 5–8 never renegotiate layout.
  [`v1/__init__.py:13`](../../../../app/api/v1/__init__.py#L13)

- OpenAPI metadata is a deliverable, not decoration — story 9 ships this document as the API docs.
  [`main.py:31`](../../../../app/main.py#L31)

**Image boundary**

- Tests live only in the `test` stage; the shipping image holds `app/` and `requirements.txt` alone.
  [`Dockerfile:45`](../../../../Dockerfile#L45)

- Proxy headers trusted because the port is loopback-only — revisit if story 2 ever publishes it.
  [`Dockerfile:38`](../../../../Dockerfile#L38)

**Tests worth reading (they were the weakest part before review)**

- Boot-death proven through a real subprocess, run from a scratch cwd so a stray `.env` cannot rescue it.
  [`test_health.py:161`](../../../../tests/test_health.py#L161)

- Asserts `.env.example` covers the five compose-only variables no `Settings` field would catch.
  [`test_health.py:286`](../../../../tests/test_health.py#L286)

- Seam mounting proven by a probe route; deleting `include_router` now fails a test.
  [`test_health.py:76`](../../../../tests/test_health.py#L76)

- `REQUIRED_ENV` pinned to `Settings.model_fields`, so a new field cannot silently lose coverage.
  [`conftest.py:19`](../../../../tests/conftest.py#L19)
