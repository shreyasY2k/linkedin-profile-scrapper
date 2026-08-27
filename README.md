# LinkedIn Profile API

Accepts a LinkedIn profile URL and returns the profile as structured JSON,
retrieved under the caller's own LinkedIn session.

> **This README is incomplete.** Story 1 wrote the **Setup** section; story 3
> added **Get a token** to it, including the first of the two copy-paste `curl`
> commands CAP-3 is graded on; story 5 added **Store your LinkedIn session**,
> which the second `curl` depends on; story 6 added **Fetch a profile**, which
> is that second `curl`; story 7 added **When LinkedIn will not answer**, which
> is what `stale` means. Story 9 owns the remaining three required sections —
> **API documentation**, **Approach**, and **Known limitations**. Placeholders
> below mark where they go. Do not delete the placeholders; fill them.

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

### Fetch a profile

This is the second of the two copy-paste `curl` commands the submission is
graded on. The first minted a token; this one uses it.

```bash
set -a; . ./.env; set +a

TOKEN=$(curl -fsS -X POST \
  -d grant_type=client_credentials \
  -d "client_id=$KEYCLOAK_CLIENT_ID" \
  -d "client_secret=$KEYCLOAK_CLIENT_SECRET" \
  "$KEYCLOAK_ISSUER_URL/realms/$KEYCLOAK_REALM/protocol/openid-connect/token" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -fsS -G http://127.0.0.1:8000/api/v1/profile \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode 'url=https://www.linkedin.com/in/example'
```

`-G --data-urlencode` rather than pasting the URL into the path: a LinkedIn
profile URL carries `?trk=...` tracking parameters and sometimes non-ASCII
public ids, and both need encoding to survive as a query value.

The answer is the envelope from `response-schema.md`:

```json
{
  "url": "https://www.linkedin.com/in/example",
  "public_id": "example",
  "stale": false,
  "fetched_at": "2026-08-27T09:00:00Z",
  "partial": ["experience.employment_type"],
  "profile": {
    "name": {"first": "…", "last": "…", "full": "…"},
    "headline": "…",
    "location": {"country": "IN", "region": "Bengaluru, Karnataka"},
    "about": "…",
    "experience": [
      {
        "title": "…", "company": "…",
        "company_url": "https://www.linkedin.com/company/1234",
        "location": "…",
        "start": "2023-03", "end": null,
        "description": "…"
      }
    ],
    "education": [{"school": "…", "school_url": "…", "degree": "…",
                   "field_of_study": "…", "start": "2016", "end": "2020"}],
    "skills": ["…"],
    "certifications": [{"name": "…", "issuer": "…", "issued": "2022-11",
                        "credential_url": "…"}],
    "languages": [{"name": "…", "proficiency": "NATIVE_OR_BILINGUAL"}],
    "images": {"profile": "https://…", "background": "https://…"}
  }
}
```

**Read `partial` before you read `profile`.** It is the whole of the
absent-versus-unreadable contract, and it is what makes a degraded answer
honest rather than misleading:

- A section the member **genuinely does not have** comes back as `[]` (or
  `null` for a scalar), the key is present, and its name is **not** in
  `partial`. That is a positive statement: this person has no certifications.
- A section this run **could not retrieve** — the sub-request failed, or
  LinkedIn said there were more entries than it returned — has its key
  **omitted from `profile` entirely** and its name listed in `partial`. That
  says: we do not know, and we are not going to guess.

So `"certifications": []` and `"partial": ["certifications"]` are two different
answers, and a client can branch on them without parsing prose. A non-empty
`partial` on an otherwise-200 response is the signal that extraction degraded
without failing. `partial` is **always present** and is `[]` on a complete
answer — it is never omitted, so you can read it unconditionally.

**Dotted names are sub-fields.** `experience.employment_type` means that
sub-field was unreadable for at least one entry in that array and is omitted
from the entries where it could not be read; entries where it was readable
still carry it. Any field in the profile object may appear in `partial`, not
only the array-valued ones.

You will see `experience.employment_type` on most real profiles, and that is
correct rather than a defect. LinkedIn references an employment type on each
position (`urn:li:fsd_employmentType:12`) and delivers nothing that names it.
Publishing the URN in a field you would read as "Full-time" is an unreadable
value dressed as a readable one, and decoding it from a remembered table would
be this service guessing at a label for someone's job. So it is omitted and
reported, which is what the rest of this contract does with anything it could
not read.

A few things the shape deliberately does **not** do:

- **Dates keep the source's own precision.** Experience dates are `YYYY-MM`, or
  `YYYY` when LinkedIn stated only a year; education is `YYYY`; certifications
  are `YYYY-MM`. Never widened into a timestamp — LinkedIn does not expose day
  precision, and inventing one would misrepresent the source.
- **`"end": null` on a position means the person still holds it**, and nothing
  else. A finished role whose end date carries only a year renders as `"2019"`,
  never `null`. (It did render as `null` in an earlier draft, which republished
  finished jobs as current ones — the two precisions exist to prevent exactly
  that.)
- **`location.region` costs no extra call.** It is resolved from the geo entity
  the core request already returns, and a redundant trailing country name is
  trimmed because `country` has its own field. It is `null` when LinkedIn
  delivers no readable place name — an absence, not a failure, so it never
  appears in `partial`.
- **`employment_type` is never a raw URN.** See the `partial` note above.

Errors wear the same typed envelope as everything else:

| You get | When |
|---|---|
| `400 INVALID_URL` | not a `/in/{public-id}` URL — rejected before any LinkedIn call |
| `401 UNAUTHENTICATED` | missing, expired or foreign-realm token |
| `428 NO_SESSION` | you have not stored a LinkedIn session yet |
| `428 SESSION_EXPIRED` | LinkedIn refused your stored cookie; store a new one |
| `404 PROFILE_NOT_FOUND` | no such member, or not visible to your session |
| `429 RATE_LIMITED` | LinkedIn throttled us; `Retry-After` when it says |
| `502 UPSTREAM_CHALLENGE` | LinkedIn served a challenge or authwall |
| `502 UPSTREAM_ERROR` | any other upstream failure — a withdrawn endpoint, an unreadable body, or the whole fetch exceeding its deadline |
| `422 INVALID_REQUEST` | the `url` query parameter is missing entirely |
| `503 SERVICE_UNAVAILABLE` | this service could not reach its own datastore |
| `500 INTERNAL_ERROR` | a bug here. It still wears the envelope above; nothing ever returns a naked 500 or a stack trace |

The three with `"retryable": true` — `RATE_LIMITED`, `UPSTREAM_CHALLENGE`,
`UPSTREAM_ERROR` — are the ones you may never actually see, because a cached
record outranks them. The reverse also holds and matters more: a `428
SESSION_EXPIRED` is never softened into a stale `200`, with one honest
exception. See **When LinkedIn will not answer** below for both.

Every one of these is `Cache-Control: no-store`, including the successful
response: these bodies are specific to *you*, and a shared cache in front of the
service keys on nothing that distinguishes one caller from another.

Each request costs **six** live calls to LinkedIn: one for the core profile,
then five concurrent section calls. There is no retry — the failures worth
retrying are the ones an immediate retry makes worse. (One exception, and it is
not a retry of a failure: if LinkedIn refuses the decoration the core request
asks for, that one call is repeated without it, and the profile comes back
without a `region`. A brittle nicety must not take down the fetch.)

Each call has a 15s timeout and the whole fetch has a 45s backstop, so a wedged
upstream cannot hold a request open indefinitely.

### When LinkedIn will not answer

Every request depends on LinkedIn answering *right now*, across six calls, under
a cookie that may have died since you stored it. So every successful answer is
kept, and when a live retrieval fails for a reason **retrying could fix**, you
get the last good record for that profile instead of the error:

```json
{
  "url": "https://www.linkedin.com/in/example",
  "public_id": "example",
  "stale": true,
  "fetched_at": "2026-08-27T09:00:00Z",
  "partial": ["experience.employment_type"],
  "profile": {
    "name": {"first": "…", "last": "…", "full": "…"},
    "headline": "…",
    "location": {"country": "IN", "region": "Bengaluru, Karnataka"},
    "about": "…",
    "experience": [ ... ], "education": [ ... ], "skills": [ ... ],
    "certifications": [ ... ], "languages": [ ... ],
    "images": {"profile": "https://…", "background": "https://…"}
  }
}
```

That is the **full** payload, not a summary of one: a stale answer is
byte-for-byte the response that was returned when the profile was last fetched,
with `stale` flipped. (`profile` is elided above only for length — the shape is
the one documented under **Fetch a profile**.)

Two fields carry the whole of it, and they are meant to be read together:

- **`stale`** — `false` when `profile` was retrieved from LinkedIn during *this*
  request; `true` when the live call failed and a stored record was served in
  its place. There is no third value and no other signal: a 200 is either fresh
  or explicitly flagged.
- **`fetched_at`** — when the returned profile was actually read from LinkedIn,
  **never** when the request was served. On a stale answer it is the older
  timestamp. That is what makes staleness actionable rather than cosmetic, and
  it is the reason nothing in this service ever re-stamps it.

The record served is the record that was stored, unchanged — the same `profile`,
the same `partial[]`, the same omitted keys. Nothing is re-derived on the way
out, so a stale answer is an answer that was once true, republished with an
honest label on it.

**Staleness is unbounded, by decision.** There is no TTL, no eviction and no age
limit, so a record from any point in the past is served in preference to a
`502`. That is a trade made deliberately: an answer you can date and judge is
more useful than an error page, and `fetched_at` gives you everything you need
to decide whether to trust it. If you want only fresh data, refuse any response
whose `stale` is `true` — the flag exists so that is a one-line check.

**What is never masked this way.** Only the three `retryable: true` codes fall
back to a record. A failure this API classifies as permanent reaches you as
itself, however good the cached copy is. Every answer the endpoint can give you
with a record in the cache:

| Situation | Cached record exists | You get |
|---|---|---|
| LinkedIn throttled us (`RATE_LIMITED`) | yes | `200`, `"stale": true` |
| Challenge or authwall (`UPSTREAM_CHALLENGE`) | yes | `200`, `"stale": true` |
| Any other upstream failure (`UPSTREAM_ERROR`) | yes | `200`, `"stale": true` |
| Any of the three | no | the typed error, unchanged |
| Any of the three, but the cache itself could not be read | — | the typed error, unchanged |
| Any of the three, but the record is unreadable or was written under an older response shape | — | the typed error, unchanged |
| Your session died (`SESSION_EXPIRED`) | yes | `428` — never a stale 200. **See the gap below.** |
| You stored no session (`NO_SESSION`) | yes | `428`, and the record is not even looked up |
| No or invalid bearer token (`UNAUTHENTICATED`) | yes | `401`, before this endpoint runs at all |
| Profile gone or hidden (`PROFILE_NOT_FOUND`) | yes | `404` — a deleted profile is not stale data |
| Malformed URL (`INVALID_URL`) | yes | `400` |
| LinkedIn answered about a **different member** | yes | `502` — see below |

The last row is a deliberate choice rather than a consequence. If a fetch comes
back naming somebody other than the member you asked for — a vanity URL that has
changed hands, a redirect, an upstream substitution — you get a `502` and *not*
the cached record, even though the code it carries is technically retryable.
That condition is permanent, and under a cache with no expiry a stale `200`
would republish the old identity mapping for ever without ever telling you the
URL has stopped meaning what you think.

Hiding a dead cookie behind cached data would report success forever about a
credential that stopped working, which is the one failure this design exists to
avoid.

> **The one gap in that promise, and it is real.** LinkedIn does not always
> *state* a refusal as a refusal. A dead `li_at` is sometimes answered with a
> redirect to an authwall carrying a `200`, which is the same page a datacenter
> IP draws with a perfectly good session — this service genuinely cannot tell
> them apart, so it classifies both as `UPSTREAM_CHALLENGE`, which is retryable,
> which means that particular kind of dead session **is** stale-served. This was
> verified against the running stack, not theorised. If `stale` has been `true`
> for longer than you can explain, re-`PUT` your session before assuming
> LinkedIn is the problem; `GET /api/v1/session` will show `last_use_ok: null`
> ("could not tell"), never a false `true`.

**The cache is shared between callers, and that is a trade.** It is keyed by
public id, not by caller, so a record fetched under one caller's session answers
another's. The session check happens *before* the cache is consulted, so nobody
without a working session of their own can reach it — but that is a control on
*access*, not on *content*. LinkedIn's profile responses are viewer-relative:
connection degree, and whatever privacy settings the member applies to people
outside it, change what comes back. So a stale answer can be a slightly richer
view than your own session would ever have retrieved live. This is accepted
knowingly for a single-evaluator service; it would not be acceptable in a
multi-tenant one, where the cache key would have to include the viewer.

**Media URLs in a stale record expire.** The `images` URLs LinkedIn returns are
signed and time-limited, so on a record served long after it was fetched they
will `403`. They are deliberately **not** stripped: removing them would mean
re-shaping the record on the way out, which is exactly what "served exactly as
it was stored" forbids, and a missing `images` key would say "this member has no
photo" — a claim about the member, when the truth is a fact about a URL. The
field stays as it was fetched and `fetched_at` tells you how likely it still
resolves.

The records live in the same Postgres the session vault uses, in the `app`
schema, and nothing about a session or a subject is stored beside them:

```bash
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c 'select public_id, fetched_at from app.profile_cache'
```

A cache write that fails never costs you the answer: if the datastore rejects
the write on an otherwise successful fetch, the live `200` is returned anyway
and the failure goes to the log — at `ERROR`, naming CAP-5, so a cache that has
never once worked cannot be mistaken for one that simply has nothing in it yet.

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
where the session vault and the response cache live, so this destroys the realm,
every stored session, and every cached profile. (Records never expire on their
own, and **the application never removes one** — no TTL, no eviction, no delete
endpoint. Dropping the volume, or a `DELETE` you run yourself in `psql`, is the
only thing that does.) The stack comes back healthy from
empty volumes: the realm is
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
     Voyager endpoints, unbounded stale-serve — not generic caveats.

     Story 7 verified three that belong here verbatim; all three are written up
     under "When LinkedIn will not answer" and recorded in
     _bmad-output/implementation-artifacts/deferred-work.md:

     1. A dead li_at whose authwall arrives as a 200 classifies as
        UPSTREAM_CHALLENGE (retryable) and IS stale-served indefinitely, so the
        "SESSION_EXPIRED is never a stale 200" guarantee has a real gap.
        Verified live, not theorised.
     2. The response cache is keyed by profile and shared across callers, while
        LinkedIn's retrieval is viewer-relative — so a stale answer can be a
        richer view than the requesting caller's own session would produce.
        Accepted for a single-evaluator service; not acceptable multi-tenant.
     3. Media URLs in a stale record are signed and expire, so an old record's
        images 403. Deliberately not stripped — see the README section. -->

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
  db.py              Postgres connections + the idempotent schema bootstrap, and
                     the two stores (session, response cache). Application tables
                     live in the `app` schema; Keycloak owns `public` in the same
                     database
  vault.py           the encrypted per-subject session vault — the ONLY place a
                     stored li_at exists in plaintext
  cache.py           the response cache and the stale-serve rule: fall back only
                     when the failure is retryable and a usable record exists.
                     No TTL, no eviction, no delete — unbounded by decision, so
                     a record it cannot trust is ignored rather than removed
  api/
    health.py        unauthenticated GET /health
    v1/__init__.py   APIRouter(prefix="/api/v1") — the seam AND the auth boundary
    v1/session.py    PUT|GET /api/v1/session; presence in, never the value out
    v1/profile.py    GET /api/v1/profile — the graded endpoint: session lookup,
                     fetch, map, envelope
  linkedin/
    client.py        the Voyager client: the ONLY place that puts a LinkedIn
                     session on the wire, calls linkedin.com, or knows the
                     endpoint map
  mapping/
    profile.py       raw entities to response-schema.md, and the absent-versus-
                     unreadable decision that fills partial[]
    dates.py         dateRange to YYYY-MM / YYYY at the source's own precision
    images.py        a vectorImage joined into one absolute URL
    text.py          the ONE place text and URLs are judged publishable
tests/
  test_health.py     liveness + missing-configuration coverage
  test_auth.py       the full token-rejection matrix, signed offline
  test_vault.py      the vault matrix: encryption at rest, subject isolation,
                     overwrite, rotated keys — no Postgres, no network
  test_session_api.py  both session endpoints end to end against a real token
  test_mapping.py    the mapping matrix — absent versus unreadable, asserted
                     both ways for every section
  test_cache.py      the stale-serve matrix — every non-retryable code that must
                     NOT be answered from the cache, plus a resolver that checks
                     every cache statement against the schema bootstrap creates
                     (the cache is the one thing here that can break silently)
  test_postgres_live.py  the opt-in database round-trip; skipped by default
  test_profile_api.py  GET /api/v1/profile end to end, stubbed client
  support.py         shared test helpers — the single seam between test modules
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
