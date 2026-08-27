# LinkedIn Profile API

Accepts a LinkedIn profile URL and returns the profile as structured JSON,
retrieved under the caller's own LinkedIn session.

> **This README is incomplete.** Story 1 wrote the **Setup** section only.
> Story 9 owns the remaining three required sections — **API documentation**,
> **Approach**, and **Known limitations** — plus the two copy-paste `curl`
> commands (mint a token, call the endpoint) that CAP-3 is graded on.
> Placeholders below mark where they go. Do not delete the placeholders; fill
> them.

---

## Setup

### Requirements

Docker and Docker Compose. Nothing else — not even a Python interpreter.
Development, tests and the running service all happen inside containers, so the
host's Python version is irrelevant.

Verified against Docker 29.2.1 / Compose v5.0.2 on `linux/arm64`
(Apple Silicon locally, Oracle Ampere A1 deployed). All three images are native
arm64; there is deliberately no `platform:` key anywhere.

### Run it

```bash
git clone git@github.com:shreyasY2k/linkedin-profile-scrapper.git
cd linkedin-profile-scrapper

cp .env.example .env       # then edit .env — see "Configuration" below
docker compose up -d --wait
```

`--wait` returns only once every service reports healthy. When it exits 0:

```bash
curl -fsS http://127.0.0.1:8000/health
# {"status":"ok"}
```

| Service  | Local URL                   | Notes                              |
|----------|-----------------------------|------------------------------------|
| API      | http://127.0.0.1:8000       | OpenAPI UI at `/docs`              |
| Keycloak | http://127.0.0.1:8080       | Admin console; bootstrap admin from `.env` |
| Postgres | `127.0.0.1:5432`            | Shared by Keycloak and the API     |

Every published port binds `127.0.0.1` only. Nothing on this stack answers on
the host's public interface — in the deployed topology, host-installed nginx is
the only thing that does.

### Configuration

All configuration arrives through environment variables, read once through a
single `pydantic-settings` object in `app/config.py`. The same image runs
locally and deployed; only `.env` differs. There is no `APP_ENV` and no code
path that branches on an environment name.

`.env.example` lists every variable with a placeholder value. Copy it, then
replace at minimum:

- `POSTGRES_PASSWORD`
- `KEYCLOAK_ADMIN_PASSWORD`
- `KEYCLOAK_CLIENT_SECRET`
- `SESSION_ENCRYPTION_KEY`

Every variable `Settings` declares is **required and non-empty**, including the
ones no code reads yet. A deployment missing `SESSION_ENCRYPTION_KEY` dies at
boot with that field named on stderr, rather than at the first request that
needs it. The API container will never report healthy with a broken
environment.

`.env` is gitignored and was gitignored in the commit that created the
repository. No credential, cookie, key or secret belongs anywhere in this tree
or in its history.

### Reset the stack

```bash
docker compose down -v && docker compose up -d --wait
```

`-v` drops the `pgdata` volume. The stack comes back healthy from empty volumes;
the Keycloak realm and any stored data are gone with it.

### Tests

Tests run in a dedicated image stage, so `pytest` and `httpx` never enter the
image that ships:

```bash
docker build --target test -t linkedin-profile-api:test .
docker run --rm linkedin-profile-api:test
```

### Secret scanning

```bash
pip install pre-commit
pre-commit install            # gitleaks now runs on every commit
pre-commit run --all-files
```

---

## API documentation

<!-- STORY 9: the generated OpenAPI document (http://127.0.0.1:8000/docs,
     /openapi.json) is the API documentation. Include the two copy-paste curl
     commands CAP-3 requires: one to mint a Keycloak token, one to call
     GET /api/v1/profile with it. -->

_To be written by story 9._

## Approach

<!-- STORY 9 -->

_To be written by story 9._

## Known limitations

<!-- STORY 9: name the specific failure modes from the brief addendum —
     session expiry, challenge pages, datacenter-IP reputation, unversioned
     Voyager endpoints, unbounded stale-serve — not generic caveats. -->

_To be written by story 9._

---

## Repository layout

```
app/
  config.py          the one configuration read; every field required
  main.py            create_app(); OpenAPI title/version; routers mounted
  api/
    health.py        unauthenticated GET /health
    v1/__init__.py   APIRouter(prefix="/api/v1") — the seam, no routes yet
tests/
  test_health.py     liveness + missing-configuration coverage
Dockerfile           slim python base, non-root, deps layer before source
docker-compose.yml   api + keycloak + postgres, healthchecked, loopback-only
.env.example         the env contract
```

Built for a graded evaluation, not for production operation.
