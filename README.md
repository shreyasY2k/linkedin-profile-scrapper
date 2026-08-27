# LinkedIn Profile API

Accepts a LinkedIn profile URL and returns the profile as structured JSON,
retrieved under the caller's own LinkedIn session.

> **This README is incomplete.** Story 1 wrote the **Setup** section; story 3
> added **Get a token** to it, including the first of the two copy-paste `curl`
> commands CAP-3 is graded on; story 5 added **Store your LinkedIn session**,
> which the second `curl` depends on. Story 9 owns the remaining three required
> sections — **API documentation**, **Approach**, and **Known limitations** —
> and that second `curl`, which needs the profile route stories 6-8 ship.
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

**The graded lane is the `curl` above.** That is what CAP-3 is scored on and
what the two commands in this README are for.

For local development there is also an **Authorize** button on
<http://127.0.0.1:8000/docs>: give it `KEYCLOAK_CLIENT_ID` and
`KEYCLOAK_CLIENT_SECRET` and every "Try it out" below carries a freshly minted
token. It posts to the same endpoint as the `curl` — the OpenAPI document
advertises `KEYCLOAK_ISSUER_URL`, not the in-container `KEYCLOAK_SERVER_URL`,
because your browser cannot resolve a compose service name.

> **Use it with a local secret only.** `client_credentials` is a
> machine-to-machine grant; a confidential client's secret is not meant to be
> typed into a browser, where it lives in page memory and in whatever the
> browser's dev tools and extensions can see. Convenient against your own stack
> with a throwaway secret; not something to do with a deployed one.

> **Needs a cold volume on an existing stack.** The realm's CORS origins arrive
> through the realm import, whose strategy is `IGNORE_EXISTING` — so a stack
> whose `pgdata` volume predates them keeps the old realm, the button renders,
> and the mint fails on CORS with nothing in either log to say why. Run
> `docker compose down -v && docker compose up -d --build --wait` first. Same
> prerequisite, same reason, as changing `KEYCLOAK_CLIENT_SECRET`.

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

Every error this API returns — 401, 404, 405, 422, 428, 500 — wears that same
envelope. No response ever leaves in FastAPI's default `{"detail": "..."}`
shape.

### Store your LinkedIn session

LinkedIn publishes no public profile API, so the data is read from its internal
endpoints **under your own LinkedIn session**. The service holds no session of
its own: you supply yours once, it is encrypted at rest under
`SESSION_ENCRYPTION_KEY` and bound to your token's subject, and every profile
request you make is performed under it.

Get the cookie: log in to LinkedIn, then DevTools → Application → Cookies →
`https://www.linkedin.com` → `li_at`. Copy the value.

```bash
set -a; . ./.env; set +a

TOKEN=$(curl -fsS -X POST \
  -d grant_type=client_credentials \
  -d "client_id=$KEYCLOAK_CLIENT_ID" \
  -d "client_secret=$KEYCLOAK_CLIENT_SECRET" \
  "$KEYCLOAK_ISSUER_URL/realms/$KEYCLOAK_REALM/protocol/openid-connect/token" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

read -rs -p 'Paste your li_at: ' LI_AT; echo   # -s: not echoed, not in history

LI_AT="$LI_AT" python3 -c 'import json,os;print(json.dumps({"li_at":os.environ["LI_AT"]}))' \
  | curl -fsS -X PUT \
      -H "Authorization: Bearer $TOKEN" \
      -H 'content-type: application/json' \
      --data-binary @- \
      http://127.0.0.1:8000/api/v1/session
# {"stored":true,"stored_at":"...","last_used_at":"...","last_use_ok":true}

unset LI_AT
```

Three deliberate awkwardnesses, each closing a way the cookie escapes:

- `read -rs` keeps it out of your shell history and off the screen.
- The body is **piped on stdin**, not passed as `-d "{...}"`. A `-d` argument is
  expanded into `curl`'s argv, which `ps` shows to every user on the machine —
  so the obvious version leaks the cookie to anyone with a shell on the box,
  history or no history.
- `python3 -c` builds the JSON, so a cookie containing a quote or a backslash
  is escaped rather than producing a malformed body. The value reaches it
  through the environment, which is readable only by you and root.

`last_use_ok` in the response is not decoration: the service makes one cheap
call to LinkedIn's `me` endpoint with the cookie you just supplied, so `true`
means it works right now. `false` means LinkedIn refused it — store a different
one. `null` means the check could not reach a verdict (a throttle, a challenge,
no network); the session is stored either way, and a failed check never costs
you the credential.

Check it later without re-sending it:

```bash
curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/session
# {"stored":true,"stored_at":"...","last_used_at":null,"last_use_ok":null}
```

What that endpoint deliberately does **not** do:

- **It never returns the cookie.** Not under a query parameter, a flag or a
  debug mode; there is no such thing. `GET` answers presence and whether the
  last use of the session succeeded, which is what you can act on. Returning
  the value would make this a credential-disclosure endpoint that happens to
  require a token.
- **Nobody else can read yours.** The vault key is the `sub` claim of the
  verified token, never a field in the request, so a second caller sees only
  their own state — including whether you have stored anything at all.
- **It is unreadable in the database.** What lands in Postgres is a Fernet
  token. `docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
  -c 'select * from app.linkedin_session'` shows a subject, two timestamps, and
  a hex blob.
- **There is no delete or revoke endpoint.** `PUT` again and the previous value
  is overwritten outright — no history is kept. Overwrite is the whole
  lifecycle, by decision.
- **Expiry is surfaced, never repaired.** Nothing here refreshes or re-logs-in.
  When LinkedIn stops accepting your cookie you get `428 SESSION_EXPIRED` and
  store a new one.

`li_at` cookies are long-lived but not permanent, and LinkedIn invalidates them
on password change and on some security events. When that happens, repeat the
`PUT`.

### Prove the isolation (two callers, two sessions)

CAP-4 says a stored session is recoverable only by the Keycloak subject that
supplied it. The realm ships **two** confidential service-account clients so
that claim is demonstrable rather than merely true: each one is a distinct
service-account user, so each mints a token with a different `sub`. The client
in `KEYCLOAK_CLIENT_ID` is the evaluator lane; `KEYCLOAK_SECOND_CLIENT_ID`
exists only to be a second caller.

> **Needs a cold volume.** The realm import strategy is `IGNORE_EXISTING`, so a
> stack whose `pgdata` volume predates this section already has the old
> single-client realm and will never see the second one. Run
> `docker compose down -v && docker compose up -d --build --wait` first.

```bash
set -a; . ./.env; set +a

mint () {  # mint <client-id> <client-secret>
  curl -fsS -X POST \
    -d grant_type=client_credentials -d "client_id=$1" -d "client_secret=$2" \
    "$KEYCLOAK_ISSUER_URL/realms/$KEYCLOAK_REALM/protocol/openid-connect/token" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])'
}

A=$(mint "$KEYCLOAK_CLIENT_ID" "$KEYCLOAK_CLIENT_SECRET")
B=$(mint "$KEYCLOAK_SECOND_CLIENT_ID" "$KEYCLOAK_SECOND_CLIENT_SECRET")

# Two different subjects — this is what makes the rest meaningful.
# (A JWT segment is base64url with the padding stripped, so `base64 -d` alone
#  fails on it; this re-pads before decoding.)
subject_of () {
  printf '%s' "$1" | cut -d. -f2 | python3 -c '
import sys, base64, json
raw = sys.stdin.read().strip()
print(json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))["sub"])'
}

subject_of "$A"
subject_of "$B"

# A stores a session; B has none and cannot even see that A does.
LI_AT="$LI_AT" python3 -c 'import json,os;print(json.dumps({"li_at":os.environ["LI_AT"]}))' \
  | curl -fsS -X PUT -H "Authorization: Bearer $A" \
      -H 'content-type: application/json' --data-binary @- \
      http://127.0.0.1:8000/api/v1/session

curl -fsS -H "Authorization: Bearer $B" http://127.0.0.1:8000/api/v1/session
# {"stored":false,...}   <- B sees its own state, and only its own
```

Two rows, two subjects, and neither token can read the other's value — because
no endpoint returns a value at all, and the vault key comes from the token
rather than from anything a caller can type.

### Configuration

All configuration arrives through environment variables, read once through a
single `pydantic-settings` object in `app/config.py`. The same image runs
locally and deployed; only `.env` differs. There is no `APP_ENV` and no code
path that branches on an environment name.

`.env.example` lists every variable with a placeholder value. The stack boots on
those placeholders, which is the point — but every one of them is in this
repository, so replace at minimum:

- `POSTGRES_PASSWORD`
- `KEYCLOAK_ADMIN_PASSWORD`
- `KEYCLOAK_CLIENT_SECRET`
- `SESSION_ENCRYPTION_KEY`

`SESSION_ENCRYPTION_KEY` is the one with a shape requirement: it must be a real
Fernet key — 32 bytes, url-safe base64 — not an arbitrary passphrase. The API
validates it at import and refuses to start on anything else, because a service
that cannot encrypt must not accept a cookie. Generate one with:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Rotating it does not migrate the rows already stored under the old key. Those
are surfaced as `428 SESSION_EXPIRED` on the next read, with the real reason in
the API container's log, and the fix is for each caller to `PUT` their session
again.

Booting on the placeholder is allowed but not quiet — the API logs a `CRITICAL`
line naming the variable on every start, because a deployment whose encryption
key is printed in a public repository should not be something you can forget.

#### Upgrading an existing `.env`

Two variables changed shape. A `.env` copied from an earlier `.env.example`
needs both, or the stack will not come up:

| Variable | What to do |
|---|---|
| `SESSION_ENCRYPTION_KEY` | The old placeholder (`change-me-generate-a-real-key-before-deploying`) is **not a valid Fernet key**, and the API now refuses to start on it — you will see `InvalidEncryptionKey` in `docker compose logs api` and the container will never report healthy. Generate a real key with the command above. |
| `KEYCLOAK_SECOND_CLIENT_ID`, `KEYCLOAK_SECOND_CLIENT_SECRET` | New, and required by compose. Copy the two lines from `.env.example` and pick your own secret. They are read only by the Keycloak container. |

Changing `SESSION_ENCRYPTION_KEY` makes every previously stored session
unreadable — see the rotation note above. The second client needs
`docker compose down -v` to appear at all, because the realm import is
`IGNORE_EXISTING`.

You do **not** need to re-edit `DATABASE_URL` after changing `POSTGRES_PASSWORD`.
`POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` are the single source of
truth: the `api` service composes `DATABASE_URL` from those three and injects it
as a compose-level `environment` entry, which overrides the `DATABASE_URL` line
in `.env`. That line matters only if you run the app outside compose. (If your
password contains `@ : / ? #`, percent-encode it in the compose expression.)

Every variable `Settings` declares is **required and non-empty**, including the
ones no code reads yet, with exactly one deliberate exception noted below. A
deployment missing `SESSION_ENCRYPTION_KEY` dies at boot with that field named
on stderr, rather than at the first request that needs it. The API container
will never report healthy with a broken environment.

The exception is `LINKEDIN_DEV_COOKIE`, which is **optional and must be left
blank in any deployment**. It is a developer's own LinkedIn session, read only
by the opt-in live check (see below). It is optional because real sessions
arrive per-caller at runtime through `PUT /api/v1/session` — requiring it would
force operators to invent a value for something the service never reads — and
`docker-compose.yml` blanks it for the `api` container so that a value filled in
locally never reaches the running process's environment.

`.env` is gitignored and was gitignored in the commit that created the
repository. No credential, cookie, key or secret belongs anywhere in this tree
or in its history.

### The live check

Everything in the test suite runs offline against synthetic fixtures. One test
does not: `tests/test_linkedin_live.py` calls the real Voyager API, to prove the
endpoint map is still real. LinkedIn's internal API is unversioned and
undocumented — the endpoint most documentation still names is already `410
Gone` — so this is the only assertion that can fail for a reason no offline test
can predict.

It is **skipped by default** and needs two gates to run, so that CI and an
evaluator running the suite never spend anyone's LinkedIn quota:

```bash
# 1. put your own li_at in .env as LINKEDIN_DEV_COOKIE (see .env.example)
# 2. then:
LINKEDIN_LIVE_CHECK=1 pytest -q -m live
```

It fetches only the profile the session itself owns — the public id comes from
LinkedIn's `me` endpoint, not from a constant, so it cannot be pointed at a
third party by editing a string. Run it at most once per change to
`app/linkedin/client.py`.

### Reset the stack

```bash
docker compose down -v && docker compose up -d --wait
```

`-v` drops the `pgdata` volume — which is where Keycloak keeps its realms *and*
where the session vault's table lives, so this destroys the realm and every
stored session. The stack comes back healthy from empty volumes: the realm is
re-imported from the committed export, and the API recreates its schema on
start. There is no migration step and no manual SQL, by decision — the schema
is created by an idempotent bootstrap that runs on every boot (see `app/db.py`).

Add `--build` if you have changed application source: `docker compose up` reuses
an already-built image, so without it you will bring the stack back on the old
code.

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
  config.py          the one configuration read; every field required but one
  main.py            create_app(); OpenAPI title/version; routers mounted;
                     the lifespan that bootstraps the database schema
  auth.py            JWKS-backed token validation, as a FastAPI dependency
  errors.py          the typed error envelope from response-schema.md
  db.py              Postgres connections + the idempotent schema bootstrap.
                     Application tables live in the `app` schema; Keycloak
                     owns `public` in the same database
  vault.py           the encrypted per-subject session vault — the ONLY place a
                     stored li_at exists in plaintext
  api/
    health.py        unauthenticated GET /health
    v1/__init__.py   APIRouter(prefix="/api/v1") — the seam AND the auth boundary
    v1/session.py    PUT|GET /api/v1/session; presence in, never the value out
  linkedin/
    client.py        the Voyager client: the ONLY place that puts a LinkedIn
                     session on the wire, calls linkedin.com, or knows the
                     endpoint map
tests/
  test_health.py     liveness + missing-configuration coverage
  test_auth.py       the full token-rejection matrix, signed offline
  test_vault.py      the vault matrix: encryption at rest, subject isolation,
                     overwrite, rotated keys — no Postgres, no network
  test_session_api.py  both session endpoints end to end against a real token
  test_linkedin_client.py  the retrieval edge-case matrix, entirely offline
  test_linkedin_live.py    the one opt-in live check; skipped by default
  fixtures/          synthetic Voyager payloads — invented people, .invalid
                     hosts, no captured data (a test enforces this)
.gitleaks.toml       secret-scan config: the default rules, plus two
                     value-anchored allowlists for known non-secrets
deploy/
  keycloak/          the committed realm export, imported on container start
  nginx/             the deployed site config (story 2)
Dockerfile           slim python base, non-root, deps layer before source
docker-compose.yml   api + keycloak + postgres, healthchecked, loopback-only
.env.example         the env contract
```

Built for a graded evaluation, not for production operation.
