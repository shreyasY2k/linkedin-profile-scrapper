# LinkedIn Profile API

Accepts a LinkedIn profile URL and returns the profile as structured JSON,
retrieved under the caller's own LinkedIn session.

> **This README is incomplete.** Story 1 wrote the **Setup** section; story 3
> added **Get a token** to it, including the first of the two copy-paste `curl`
> commands CAP-3 is graded on. Story 9 owns the remaining three required
> sections — **API documentation**, **Approach**, and **Known limitations** —
> and the second `curl`, which cannot be written until stories 5-8 ship a route
> to call. Placeholders below mark where they go. Do not delete the
> placeholders; fill them.

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

### Get a token

Everything under `/api/v1` requires a Keycloak bearer token. `/health` does not
and never will — the container healthcheck has no token to present.

The realm, its confidential client and the audience mapper are all created by
`docker compose up` from the committed export at
`deploy/keycloak/realm-linkedin.json`. There is no console step. The client runs
`client_credentials` only — every browser flow is switched off — so minting is
one `curl` with no redirect anywhere in it.

Every block below is runnable **verbatim** from the repository root, against a
stack that is already up. The first line loads `.env`, which is where your
client secret, realm and issuer URL actually live; without it the commands post
an empty secret and get a 401.

```bash
set -a; . ./.env; set +a          # load KEYCLOAK_* from your .env

curl -fsS -X POST \
  -d grant_type=client_credentials \
  -d "client_id=$KEYCLOAK_CLIENT_ID" \
  -d "client_secret=$KEYCLOAK_CLIENT_SECRET" \
  "$KEYCLOAK_ISSUER_URL/realms/$KEYCLOAK_REALM/protocol/openid-connect/token"
```

That returns `{"access_token": "eyJ...", "expires_in": 900, ...}`. Capture it
and call the API with it:

```bash
set -a; . ./.env; set +a

TOKEN=$(curl -fsS -X POST \
  -d grant_type=client_credentials \
  -d "client_id=$KEYCLOAK_CLIENT_ID" \
  -d "client_secret=$KEYCLOAK_CLIENT_SECRET" \
  "$KEYCLOAK_ISSUER_URL/realms/$KEYCLOAK_REALM/protocol/openid-connect/token" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/...
```

With the defaults in `.env.example` those expand to realm `linkedin`, client
`linkedin-profile-api`, and a token endpoint at
`http://127.0.0.1:8080/realms/linkedin/protocol/openid-connect/token`.

**The mint URL and `KEYCLOAK_ISSUER_URL` must be the same URL.** That is why
the commands above build the token endpoint out of `$KEYCLOAK_ISSUER_URL`
rather than hardcoding a host: the API requires the token's `iss` claim to
equal `$KEYCLOAK_ISSUER_URL/realms/$KEYCLOAK_REALM` exactly, and Keycloak
derives `iss` from the host you minted through — so `http://localhost:8080` and
`http://127.0.0.1:8080` produce different, mutually unacceptable tokens.
Deployed, both are the public HTTPS name.

Any missing, malformed, expired, wrong-issuer or wrong-audience token is
answered with 401 and the typed error body:

```json
{"error": {"code": "UNAUTHENTICATED", "message": "Missing or invalid bearer token.", "retryable": false}}
```

One message for every rejection reason, deliberately: the specific reason is in
the API container's logs, where an operator can read it and a prober cannot.

```bash
docker compose logs api | grep "Rejected request"
# 2026-08-27 08:42:11,204 WARNING app.auth: Rejected request: InvalidAudienceError: Audience doesn't match
```

Every error this API returns — 401, 404, 405, 422, 500 — wears that same
envelope. No response ever leaves in FastAPI's default `{"detail": "..."}`
shape.

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

You do **not** need to re-edit `DATABASE_URL` after changing `POSTGRES_PASSWORD`.
`POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` are the single source of
truth: the `api` service composes `DATABASE_URL` from those three and injects it
as a compose-level `environment` entry, which overrides the `DATABASE_URL` line
in `.env`. That line matters only if you run the app outside compose. (If your
password contains `@ : / ? #`, percent-encode it in the compose expression.)

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

`-v` drops the `pgdata` volume — which is where Keycloak keeps its realms, so
this destroys the realm along with any stored data. The stack comes back healthy
from empty volumes and re-imports the realm from the committed export, so the
client and its audience mapper return exactly as they were.

This is also the only way to change `KEYCLOAK_CLIENT_SECRET` from `.env`: the
import strategy is `IGNORE_EXISTING`, so it applies to a realm that does not
exist yet and never overwrites one that does. Either reset the volume, or rotate
the secret in the admin console and mirror it into `.env`.

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
  auth.py            JWKS-backed bearer validation, as a FastAPI dependency
  errors.py          the typed error envelope from response-schema.md
  api/
    health.py        unauthenticated GET /health
    v1/__init__.py   APIRouter(prefix="/api/v1") — the seam AND the auth boundary
tests/
  test_health.py     liveness + missing-configuration coverage
  test_auth.py       the full token-rejection matrix, signed offline
deploy/
  keycloak/          the committed realm export, imported on container start
  nginx/             the deployed site config (story 2)
Dockerfile           slim python base, non-root, deps layer before source
docker-compose.yml   api + keycloak + postgres, healthchecked, loopback-only
.env.example         the env contract
```

Built for a graded evaluation, not for production operation.
