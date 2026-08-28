# LinkedIn Profile API

Accepts a LinkedIn profile URL and returns the profile as structured JSON,
retrieved under the caller's own LinkedIn session. LinkedIn publishes no public
profile API, so the data is read from its internal endpoints under a session you
supply once, and every answer carries the provenance needed to judge it — when
it was fetched, whether it is stale, and which fields could not be read.

**Live at <https://shreyaskaushik.dpdns.org>.** Interactive API documentation:
<https://shreyaskaushik.dpdns.org/docs>.

The four sections the assignment asks for, in the order it asks for them:
**[Setup](#setup)**, **[API documentation](#api-documentation)**,
**[Approach](#approach)**, **[Known limitations](#known-limitations)**. The
depth behind each lives in `docs/` — see [Project structure](#project-structure).

### Quick start

The two graded commands, against the deployed service. Nothing to install,
nothing to clone, no file to source: copy them into any shell with `curl` and
`python3`.

**1 — mint a token.**
```bash
curl -fsS -X POST \
  -d grant_type=client_credentials \
  -d client_id=linkedin-profile-api \
  -d client_secret=Vu2NKrgzXPSioKp_xTDAUA-OW_Xj32a1rjynOH0kbWk \
  https://shreyaskaushik.dpdns.org/realms/linkedin/protocol/openid-connect/token
```

That returns `{"access_token": "eyJ...", "expires_in": 900, ...}`.

**2 — fetch a profile with it.**

```bash
TOKEN=$(curl -fsS -X POST \
  -d grant_type=client_credentials \
  -d client_id=linkedin-profile-api \
  -d client_secret=Vu2NKrgzXPSioKp_xTDAUA-OW_Xj32a1rjynOH0kbWk \
  https://shreyaskaushik.dpdns.org/realms/linkedin/protocol/openid-connect/token \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -sS -G https://shreyaskaushik.dpdns.org/api/v1/profile \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode 'url=https://www.linkedin.com/in/williamhgates' \
  | python3 -m json.tool
```

A populated profile comes back. **You do not have to supply a LinkedIn cookie
first** — the `linkedin-profile-api` client's vault row is pre-seeded with a
working session so that these two commands stand alone, which is the whole point
of them. That convenience has a real cost, and it is the first of the named
failure modes under [Known limitations](#known-limitations).

<a id="about-that-client-secret"></a>

> ### About that client secret
>
> `Vu2NKrgzXPSioKp_xTDAUA-OW_Xj32a1rjynOH0kbWk` is a **real, working credential,
> published here on purpose.** The assignment is graded on two `curl` commands
> running verbatim from a machine that has never authenticated, and a
> placeholder cannot satisfy that. So the trade is stated rather than hidden: it
> reaches **only this evaluation service** — one confidential client, one realm,
> one host, reused nowhere and unlocking nothing else — and it is **permanent in
> the git history** once published. **It should be rotated after grading**, in
> the Keycloak admin console (reachable only over an SSH tunnel to
> `127.0.0.1:8080`), mirroring the new value into the instance's `.env`; the
> realm import is `IGNORE_EXISTING`, so editing `.env` alone changes nothing —
> see [Reset the stack](docs/usage.md#reset-the-stack). It is the **only** real
> credential in this repository, and `.gitleaks.toml` allowlists it by its
> literal value rather than by path, so the scan stays a real scan — see
> [Secret scanning](docs/usage.md#secret-scanning).

### Use your own LinkedIn session

The two commands above ride on a pre-seeded session. The model this service was
actually built on is **bring your own**: you upload your own `li_at` once, it is
encrypted at rest and bound to your token's subject, and every profile request
you make runs under it. One `PUT` switches the evaluator lane over to your
cookie:
```bash
TOKEN=$(curl -fsS -X POST \
  -d grant_type=client_credentials \
  -d client_id=linkedin-profile-api \
  -d client_secret=Vu2NKrgzXPSioKp_xTDAUA-OW_Xj32a1rjynOH0kbWk \
  https://shreyaskaushik.dpdns.org/realms/linkedin/protocol/openid-connect/token \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# LinkedIn → DevTools → Application → Cookies → https://www.linkedin.com → li_at
read -rs -p 'Paste your li_at: ' LI_AT; echo   # -s: not echoed, not in history

LI_AT="$LI_AT" python3 -c 'import json,os;print(json.dumps({"li_at":os.environ["LI_AT"]}))' \
  | curl -fsS -X PUT \
      -H "Authorization: Bearer $TOKEN" \
      -H 'content-type: application/json' \
      --data-binary @- \
      https://shreyaskaushik.dpdns.org/api/v1/session
# {"stored":true,"stored_at":"...","last_used_at":"...","last_use_ok":true}

unset LI_AT
```

`PUT` **overwrites** — it replaces whatever session is stored for your subject,
including the pre-seeded one, and there is no undo and no delete endpoint. To
leave the graded lane undisturbed, mint with `linkedin-profile-api-second`
instead (see
[Prove the isolation](docs/usage.md#prove-the-isolation-two-callers-two-sessions));
it is a different service account and therefore a different vault row.
`last_use_ok: true` is not decoration — the service spent one call on LinkedIn's
`me` endpoint with the cookie you just sent, so `true` means it works right now.
Everything the endpoint does, and deliberately does not do, is under
[Store your LinkedIn session](docs/usage.md#store-your-linkedin-session).

### Run it locally

```bash
git clone https://github.com/shreyasY2k/linkedin-profile-scrapper.git
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

### Project structure

```
app/config.py           the one configuration read; every field required but one
app/main.py             create_app(), routers, the schema-bootstrap lifespan
app/auth.py             JWKS-backed token validation, as a FastAPI dependency
app/errors.py           the typed envelope every non-2xx response wears
app/db.py               Postgres, the idempotent schema bootstrap, both stores
app/vault.py            the encrypted per-subject session vault — the ONLY place
                        a stored li_at exists in plaintext
app/cache.py            the response cache and the stale-serve rule
app/api/                GET /health, and the /api/v1 router that is both the
                        versioning seam and the auth boundary
app/linkedin/client.py  the Voyager client — the ONLY place that puts a LinkedIn
                        session on the wire or knows the endpoint map
app/mapping/            raw entities to the response schema, and the
                        absent-versus-unreadable decision that fills partial[]
tests/                  the offline matrices, plus two opt-in live checks
deploy/                 realm export, nginx site, host-firewall script, runbook
docs/                   the companion documents below
docker-compose.yml      api + keycloak + postgres, healthchecked, loopback-only
Dockerfile, .env.example, .gitleaks.toml
```

| Document | What it covers |
|---|---|
| [`docs/usage.md`](docs/usage.md) | The full walkthrough: minting a token, storing a session, fetching a profile, what happens when LinkedIn will not answer, the two-caller isolation proof, the live check, resetting the stack, tests, secret scanning, and the complete configuration reference |
| [`docs/approach.md`](docs/approach.md) | The design rationale in full, with the rejected alternatives and the annotated repository layout |
| [`docs/limitations.md`](docs/limitations.md) | Every known limitation, unabridged |
| [`docs/architecture.md`](docs/architecture.md) | Diagrams: deployment topology, request flow, retrieval fan-out, the auth boundary, and the decision log |
| [`deploy/README.md`](deploy/README.md) | The deployment runbook: topology, the Oracle firewall trap, the redeploy recipe, and how to re-export the realm safely |

---

## Setup

### Requirements

Docker and Docker Compose. Nothing else — not even a Python interpreter.
Development, tests and the running service all happen inside containers, so the
host's Python version is irrelevant.

Verified against Docker 29.2.1 / Compose v5.0.2 on `linux/arm64`
(Apple Silicon locally, Oracle Ampere A1 deployed). All three images are native
arm64; there is deliberately no `platform:` key anywhere.

### The two paths

**Deployed** — nothing to install and no cookie to supply: the two graded
commands are in [Quick start](#quick-start), and the third, which switches you
onto your own `li_at`, is in
[Use your own LinkedIn session](#use-your-own-linkedin-session).

**Local** — [Run it locally](#run-it-locally) brings the stack up in one
command; everything from there is in [`docs/usage.md`](docs/usage.md).

### Configuration

All configuration arrives through environment variables, read once through a
single `pydantic-settings` object in `app/config.py`. The same image runs
locally and deployed; only `.env` differs. There is no `APP_ENV` and no code
path that branches on an environment name. `.env.example` lists every variable
with a placeholder and the stack boots on those placeholders — which is the
point, and also the risk, because every one of them is in this repository.

| Variable | What it is |
|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | The single source of truth for the database; the `api` service composes `DATABASE_URL` from these three, so you do not re-edit it after changing the password. `DATABASE_URL` itself matters only outside compose |
| `KEYCLOAK_ADMIN_USERNAME`, `KEYCLOAK_ADMIN_PASSWORD` | Bootstrap admin for the Keycloak console |
| `KEYCLOAK_REALM`, `KEYCLOAK_SERVER_URL`, `KEYCLOAK_ISSUER_URL` | The realm and its two URLs. A token's `iss` must equal `$KEYCLOAK_ISSUER_URL/realms/$KEYCLOAK_REALM` **exactly**, so mint through the same URL the API advertises |
| `KEYCLOAK_CLIENT_ID`, `KEYCLOAK_CLIENT_SECRET` | The confidential client the evaluator lane uses |
| `KEYCLOAK_SECOND_CLIENT_ID`, `KEYCLOAK_SECOND_CLIENT_SECRET` | A second service account, which exists only so per-caller isolation is demonstrable |
| `SESSION_ENCRYPTION_KEY` | Must be a real Fernet key — 32 bytes, url-safe base64, not a passphrase. Validated at import; the API refuses to start on anything else, because a service that cannot encrypt must not accept a cookie |
| `LINKEDIN_DEV_COOKIE` | The one optional variable, and it **must be left blank in any deployment**. A developer's own `li_at`, read only by the opt-in live check; compose blanks it for the `api` container |

Replace at minimum `POSTGRES_PASSWORD`, `KEYCLOAK_ADMIN_PASSWORD`,
`KEYCLOAK_CLIENT_SECRET` and `SESSION_ENCRYPTION_KEY`; generate a key with
`python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
Booting on the placeholder is allowed but not quiet — the API logs a `CRITICAL`
line naming the variable on every start. `.env` is gitignored, and was
gitignored in the commit that created the repository. Key rotation, upgrading an
existing `.env`, and what each variable does when it goes wrong are under
[Configuration](docs/usage.md#configuration).

---

## API documentation

**The generated OpenAPI document is the API documentation**, and it is
unauthenticated on the public host so it can be read before a token exists:

| | |
|---|---|
| **Swagger UI** | <https://shreyaskaushik.dpdns.org/docs> |
| **ReDoc** | <https://shreyaskaushik.dpdns.org/redoc> |
| **OpenAPI 3.1 JSON** | <https://shreyaskaushik.dpdns.org/openapi.json> |

It is generated from the same Pydantic models the endpoints return, so it cannot
drift from the implementation the way a hand-written table can. Locally the same
three URLs live under `http://127.0.0.1:8000`.

### The four routes

| Route | What it does | Auth |
|---|---|---|
| `GET /api/v1/profile?url=…` | **The graded endpoint.** Takes a LinkedIn profile URL as the `url` query parameter, returns the profile as structured JSON. Six live calls to LinkedIn per request, or the last good record if a retryable failure gets in the way | Bearer |
| `PUT /api/v1/session` | Store or replace your `li_at`. Body `{"li_at": "..."}`. Encrypted at rest, bound to your token's `sub`, verified once against LinkedIn's `me` endpoint on the way in. Overwrite is the entire lifecycle — there is no delete | Bearer |
| `GET /api/v1/session` | Whether you have a session stored, when, and whether its last use worked. **Never returns the cookie**, under any flag | Bearer |
| `GET /health` | Liveness. `{"status":"ok"}`. Checks no dependencies, by design — it is what the container healthcheck and the load-balancer probe call, and neither has a token | None |

Anything else under `/api/v1` answers `401`, whether or not the path exists.

### Authentication

One scheme, `KeycloakClientCredentials`, declared in the OpenAPI document as an
OAuth2 `clientCredentials` flow whose `tokenUrl` is
`https://shreyaskaushik.dpdns.org/realms/linkedin/protocol/openid-connect/token`.
Tokens are validated against the realm's JWKS with `iss` and `aud` checked by
exact equality, and access tokens live 900 s. Every rejection — missing,
malformed, expired, wrong-issuer, wrong-audience — gets the same `401
UNAUTHENTICATED` body, with the specific reason in the API container's log where
an operator can read it and a prober cannot. If *this service* cannot reach
Keycloak at all you get a retryable `502 UPSTREAM_ERROR` instead, because your
token was never checked. Swagger UI's **Authorize** button drives the same flow
— see the caution under [Get a token](docs/usage.md#get-a-token-locally) before
typing a deployed secret into a browser.

### The success envelope

`GET /api/v1/profile` returns `ProfileEnvelope`: the profile nested inside a
wrapper that carries the provenance, and reading the wrapper first is the point.

```json
{
  "url":        "the URL you asked for",
  "public_id":  "the /in/{public-id} it resolved to",
  "stale":      false,
  "fetched_at": "2026-08-27T09:00:00Z",
  "partial":    [],
  "profile":    { "name": {...}, "headline": "...", "location": {...},
                  "about": "...", "experience": [...], "education": [...],
                  "skills": [...], "certifications": [...],
                  "languages": [...], "images": {...} }
}
```

**`stale`** is `false` if `profile` was read from LinkedIn during this request
and `true` if the live call failed and a stored record was served instead; there
is no third value. **`fetched_at`** is when the returned profile was read from
LinkedIn, never when the response was served — nothing re-stamps it.
**`partial`** is always present and `[]` on a complete answer; it carries the
absent-versus-unreadable distinction the whole schema is built around, where a
key that is **present** is a positive claim and a key that is **omitted** and
named here means "we could not read it and are not going to guess". Full
treatment, including dotted sub-field names like `experience.employment_type`,
under [Fetch a profile](docs/usage.md#fetch-a-profile).

`PUT`/`GET /api/v1/session` return `SessionResponse`:
`{"stored": bool, "stored_at": ..., "last_used_at": ..., "last_use_ok": bool|null}`.
Every response, success included, carries `Cache-Control: no-store` — these
bodies are specific to the caller.

### The error envelope

**Every non-2xx response wears the same shape.** Nothing ever leaves in
FastAPI's default `{"detail": "..."}`, and nothing returns a naked 500 or a
stack trace:

```json
{"error": {"code": "SESSION_EXPIRED",
           "message": "LinkedIn refused the stored session.",
           "retryable": false}}
```

`code` is a stable machine-readable string, `message` is for a human, and
`retryable` says whether repeating the request could plausibly succeed. The
codes: `400 INVALID_URL`, `401 UNAUTHENTICATED`, `404 PROFILE_NOT_FOUND`,
`405 METHOD_NOT_ALLOWED`, `422 INVALID_REQUEST`, `428 NO_SESSION` /
`428 SESSION_EXPIRED`, `429 RATE_LIMITED`, `502 UPSTREAM_CHALLENGE` /
`502 UPSTREAM_ERROR`, `503 SERVICE_UNAVAILABLE`, `500 INTERNAL_ERROR`. The full
table, with the condition behind each, is under
[Fetch a profile](docs/usage.md#fetch-a-profile).

**Branch on `retryable`, not on the code** — it is a property of the *response*,
and there is one condition where the same code carries the opposite flag
([why](docs/limitations.md#retryable-on-the-wire-outranks-the-documented-table)).
When a live retrieval fails for a reason retrying could fix you do not get the
error at all: the last good record is returned instead, with `stale: true` and
its original `fetched_at`. Which failures fall back and which never do is a
table under
[When LinkedIn will not answer](docs/usage.md#when-linkedin-will-not-answer).

---

## Approach

LinkedIn publishes no public profile API. The data an assignment like this asks
for exists only behind an authenticated session, and the environment is actively
adversarial: authwalls, challenge pages, datacenter-IP reputation, and an
internal API that is unversioned and can change without notice. So the
interesting decisions are not about parsing. They are about **whose credential
is spent, what happens when the answer does not come, and how the response tells
the truth about what it could not read.** Every decision below is written out in
full, with the alternatives it beat and the annotated repository layout, in
[`docs/approach.md`](docs/approach.md).

**Credential model — bring your own session.** A single LinkedIn account held in
the backend was rejected: it concentrates every rate limit and every security
challenge onto one account, so one lockout takes the whole service down during
exactly the window it is being graded in. A pool of rotated accounts is the
throughput answer and was unbuildable in the time available. What shipped is
**per-caller BYO**: you `PUT` your own `li_at` once, it is encrypted with
Fernet, bound to your token's `sub`, and every fetch you make runs under it. The
subject is encrypted *inside* the ciphertext rather than stored in a column
beside it, because Fernet's tag proves "written with the key", not "written for
this row". The evaluator lane bends this deliberately — its vault row is
pre-seeded so the two graded commands work cold, concentrating precisely the
exposure this model avoids, and that is the first of the named failure modes
under [Known limitations](#known-limitations).

**Retrieval — Voyager JSON, and the browser demoted.** Logged-out public HTML
was rejected: LinkedIn serves authwalls to most datacenter IP ranges and
truncates what remains. A headless browser as the primary path was rejected on
cost — 400–700 MB of Chromium per request on the one instance that exists — then
kept as a fallback and demoted from Must to Could. **It was never built, and
there is no Playwright, Selenium or Chromium anywhere in this repository**;
saying so plainly beats leaving a reader to infer a fallback that does not
exist. One fetch is **six** calls — the core profile, then five concurrent
section calls, each asking `count=100` rather than the default 20, found the
hard way when a profile with 33 skills returned 20 of them with a `200`. There
is no retry: the failures worth retrying are the ones an immediate retry makes
worse.

**Staleness — answer, or explain why not.** Every successful answer is stored,
and when a live retrieval fails for a reason retrying could fix, the last good
record is returned with `stale: true` and the original `fetched_at`. Three
properties keep that honest: only retryable failures fall back, so a dead
session is a `428` and not a comfortable `200`; the record is served exactly as
stored, so "this was true once, at this timestamp" is a checkable claim; and it
is **unbounded** — no TTL, no eviction, no delete. The rejected alternative was
a TTL, which converts "old but dated answer" into "error", the exact failure
this design exists to avoid.

**Evaluator access — Keycloak service accounts.** Leaving the service open was
rejected: any traffic that finds the URL burns real LinkedIn quota against
somebody's real account. Google SSO alone was rejected as the sole lane because
it is not scriptable, and an evaluator who hits a browser redirect may simply
record the endpoint as unreachable. `client_credentials` is two `curl` commands
and no browser, and the realm is created from a committed export on container
start, so there is no console step. It ships **two** confidential clients on
purpose: one client is one service-account subject, so per-caller isolation was
real in the code and undemonstrable in the deployment until the second existed.

**Deployment.** Cloudflare (proxied) → OCI load balancer, which terminates TLS →
host nginx on port 80 → the compose stack on loopback. The instance has no
public IP and application containers bind `127.0.0.1` exclusively. The whole
application is one `docker compose up`, identical image locally and deployed.
The runbook, including the traps that cost real time, is in
[`deploy/README.md`](deploy/README.md); the diagrams are in
[`docs/architecture.md`](docs/architecture.md).

---

## Known limitations

Written candidly, because the honest failure modes of a service like this are
more informative than a list of features. Every item is either **observed**
against the running system or a **deliberate decision** — where it is a
decision, it says so and says why. Each entry links to its full treatment in
[`docs/limitations.md`](docs/limitations.md), where all twenty are unabridged.

### Automated collection is contrary to LinkedIn's User Agreement

Stated plainly and not softened, because it is the first thing an operator of
this service needs to know.

**Automated collection of profile data is contrary to LinkedIn's User Agreement,
irrespective of whose session is used.** The per-user credential model narrows
the question — each request is made under the requester's own authenticated
session rather than a shared harvesting account, so nobody's data is being
gathered under a credential they did not supply — but it **does not resolve it**.
Using this service against your own account carries the ordinary consequences
LinkedIn applies to automated access: challenges, throttling, and account
restriction up to and including permanent suspension.

Nor is the User Agreement the only frame. Profile data is personal data, and
collecting or storing it engages data-protection law wherever the subject is —
this service caches full profiles indefinitely with no subject-access, deletion,
or lawful-basis story of any kind.

**This system is built for the evaluation of a coding assignment, not for
production operation, and it should not be pointed at anything that matters.**
The assignment's request for known limitations is read here as an invitation to
show that the environment is understood, so the position is stated rather than
minimised.

### The failure modes, named

**The evaluator lane runs under the author's own LinkedIn session.** The two
graded commands work from a cold machine because the `linkedin-profile-api`
vault row is pre-seeded with the author's own `li_at`, so evaluator traffic
spends the author's quota and concentrates exactly the exposure the BYO model
was chosen to distribute — and anyone holding the published secret can spend it
too. A deliberate, author-approved trade; `PUT`ting your own cookie moves you
off it.
[Full entry](docs/limitations.md#the-evaluator-lane-runs-under-the-authors-own-linkedin-session).

**Session expiry is surfaced, never repaired.** When LinkedIn stops accepting a
stored `li_at` you get `428 SESSION_EXPIRED` and store a new one. Nothing here
logs in to mint a fresh cookie — a decision, not an omission: re-login draws
challenges harder than a read does and would mean storing a replayable password.
Rotating `SESSION_ENCRYPTION_KEY` produces the same `428` for a different
reason, and there is no delete or revoke endpoint at all.
[Expiry](docs/limitations.md#an-expired-cookie-is-surfaced-never-repaired) ·
[key rotation](docs/limitations.md#rotating-the-encryption-key-silently-orphans-every-stored-session) ·
[no delete](docs/limitations.md#there-is-no-way-to-delete-a-stored-session).

**Rate limiting, in both directions.** LinkedIn throttles a session that fetches
too often — `429 RATE_LIMITED`, with `Retry-After` when it says so — and each
profile costs six live calls. In the other direction **nothing here throttles a
caller**, so a holder of the published secret can spend LinkedIn quota as fast
as the upstream allows.
[Full entry](docs/limitations.md#operational-gaps).

**Challenge pages and datacenter-IP reputation.** LinkedIn does not always state
a refusal as a refusal: a dead `li_at` is sometimes answered with a redirect to
an authwall carrying a `200`, and that is the *same page* a datacenter IP draws
with a perfectly healthy session. On a profile fetch this service genuinely
cannot tell them apart, so it classifies both as retryable
`502 UPSTREAM_CHALLENGE` — which means that kind of dead session **is**
stale-served indefinitely and the caller is never told to store a new cookie.
Verified against the running stack, not theorised. If `stale` has been `true`
for longer than you can explain, re-`PUT` your session.
[Full entry](docs/limitations.md#a-dead-cookie-can-be-reported-as-staleness-rather-than-expiry).

**The Voyager endpoints are unversioned, and schema drift is barely guarded.**
LinkedIn's internal API is not a published interface, can change without notice,
and the endpoint most third-party documentation still names is already
`410 Gone`. The only assertion that can catch a shape change is one opt-in live
test fetching **only the profile the session itself owns**, so per-profile
variation is untested. Measured: `profileLanguages` returned 0 elements on one
call and 3 on an identical call minutes later, HTTP 200 both times.
[Full entry](docs/limitations.md#the-voyager-endpoint-map-is-undocumented-unversioned-and-verified-against-one-profile).

**Stale-serve is unbounded.** No TTL, no eviction, no delete endpoint: a record
from any point in the past is served in preference to a retryable error, the
table grows by roughly 7 KB per distinct profile ever fetched, and the only way
to drop one is `docker compose down -v` — which also destroys the realm and
every stored session. So a deletion request cannot be honoured short of dropping
everything, and a profile whose owner has since made it private keeps being
republished.
[Full entry](docs/limitations.md#the-cache-grows-without-bound-and-nothing-can-remove-one-record).

The rest, one line each:

- **The published evaluator client secret** is a real credential, permanent in
  the history once published, and should be rotated after grading —
  [full entry](docs/limitations.md#the-published-evaluator-client-secret).
- **The cache is keyed by profile while LinkedIn's retrieval is viewer-relative**,
  so a stale answer can be richer than your own session would have retrieved —
  [full entry](docs/limitations.md#the-cache-is-keyed-by-profile-linkedins-retrieval-is-viewer-relative).
- **Stale records carry signed `images` URLs that have expired**, deliberately
  not stripped —
  [full entry](docs/limitations.md#stale-records-carry-image-urls-that-have-expired).
- **`retryable` on the wire outranks the documented table** for the one
  different-member case —
  [full entry](docs/limitations.md#retryable-on-the-wire-outranks-the-documented-table).
- **A revoked token stays accepted for up to 900 seconds**, and `require_claims`
  authenticates without authorising —
  [full entry](docs/limitations.md#a-revoked-token-stays-accepted-for-up-to-900-seconds).
- **The committed encryption-key placeholder is a valid Fernet key**, so a
  deployment that never replaces it encrypts cookies under a published key —
  [full entry](docs/limitations.md#the-committed-encryption-key-placeholder-is-a-valid-key).
- **Route existence is partially observable without a token** —
  [full entry](docs/limitations.md#route-existence-is-partially-observable-without-a-token).
- **Sections beyond 100 entries are reported, not retrieved** —
  [full entry](docs/limitations.md#sections-beyond-100-entries-are-reported-not-retrieved).
- **A whole section is discarded for one unreadable entry** —
  [full entry](docs/limitations.md#a-whole-section-is-discarded-for-one-unreadable-entry).
- **`employment_type` is never resolved**, so it appears in `partial` on most
  real profiles —
  [full entry](docs/limitations.md#employment_type-is-never-resolved).
- **A changed vanity URL is refused with `502` rather than resolved** —
  [full entry](docs/limitations.md#a-changed-vanity-url-is-refused-rather-than-resolved).
- **Operational gaps**: no migration tool, one instance without redundancy or
  backups, Postgres shared with Keycloak as the same superuser, no CI, unrotated
  logs, and timeouts set as a backstop rather than a budget —
  [full entry](docs/limitations.md#operational-gaps).

---

Built for a graded evaluation, not for production operation.
