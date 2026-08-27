# LinkedIn Profile API

Accepts a LinkedIn profile URL and returns the profile as structured JSON,
retrieved under the caller's own LinkedIn session.

**Live at <https://shreyaskaushik.dpdns.org>.** Interactive API documentation:
<https://shreyaskaushik.dpdns.org/docs>.

Four sections, in the order the assignment asks for them: **[Setup](#setup)**,
**[API documentation](#api-documentation)**, **[Approach](#approach)**,
**[Known limitations](#known-limitations)**. Everything else is a subsection of
one of them.

---

## Setup

### Run it against the deployed service — the two graded commands

Nothing to install, nothing to clone, no file to source. Copy these two blocks
into any shell with `curl` and `python3` and they work as written.

**1 — mint a token.**

```bash
curl -fsS -X POST \
  -d grant_type=client_credentials \
  -d client_id=linkedin-profile-api \
  -d client_secret=REPLACE_WITH_EVALUATOR_CLIENT_SECRET \
  https://shreyaskaushik.dpdns.org/realms/linkedin/protocol/openid-connect/token
```

That returns `{"access_token": "eyJ...", "expires_in": 900, ...}`.

**2 — fetch a profile with it.**

```bash
TOKEN=$(curl -fsS -X POST \
  -d grant_type=client_credentials \
  -d client_id=linkedin-profile-api \
  -d client_secret=REPLACE_WITH_EVALUATOR_CLIENT_SECRET \
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
of them. That convenience has a real cost, and it is the first entry under
[Known limitations](#the-evaluator-lane-runs-under-the-authors-own-linkedin-session).

> ### About that client secret
>
> `REPLACE_WITH_EVALUATOR_CLIENT_SECRET` is a **real, working credential,
> published here on purpose.** The assignment is graded on two `curl` commands
> running verbatim from a machine that has never authenticated, and a
> placeholder cannot satisfy that. So the trade was made deliberately and is
> stated rather than hidden:
>
> - It reaches **only this evaluation service**. It is a Keycloak
>   `client_credentials` secret for one confidential client in one realm on one
>   host. It is not reused anywhere, it unlocks no other system, and it grants
>   nothing beyond calling the three routes below.
> - It is **permanent in the git history** from the moment this repository is
>   published. Rewriting history would not recall a published secret.
> - **It should be rotated after grading.** Rotate it in the Keycloak admin
>   console (reachable only over an SSH tunnel to `127.0.0.1:8080`) and mirror
>   the new value into the instance's `.env`. The realm export is imported
>   `IGNORE_EXISTING`, so editing `.env` alone will not change an existing
>   realm — see [Reset the stack](#reset-the-stack).
>
> It is the **only** real credential anywhere in this repository. `.gitleaks.toml`
> allowlists it **by its literal value**, not by path, so the secret scan stays
> a real scan — see [Secret scanning](#secret-scanning).

### Use your own LinkedIn session instead — the third command

The two commands above ride on a pre-seeded session. The model this service was
actually built on is **bring your own**: you upload your own `li_at` once, it is
encrypted at rest and bound to your token's subject, and every profile request
you make runs under it. One `PUT` switches the evaluator lane over to your
cookie:

```bash
TOKEN=$(curl -fsS -X POST \
  -d grant_type=client_credentials \
  -d client_id=linkedin-profile-api \
  -d client_secret=REPLACE_WITH_EVALUATOR_CLIENT_SECRET \
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
including the pre-seeded one, and there is no undo and no delete endpoint. If
you would rather not disturb the graded lane, mint with
`linkedin-profile-api-second` instead (see
[Prove the isolation](#prove-the-isolation-two-callers-two-sessions)); it is a
different Keycloak service account and therefore a different vault row.

`last_use_ok: true` in the reply is not decoration — the service spent one call
on LinkedIn's `me` endpoint with the cookie you just sent, so `true` means it
works right now. Everything the endpoint does, and deliberately does not do, is
under [Store your LinkedIn session](#store-your-linkedin-session).

The rest of this section is about running the stack **locally**. If you only
want to exercise the deployed service, skip to
[API documentation](#api-documentation).

### Requirements

Docker and Docker Compose. Nothing else — not even a Python interpreter.
Development, tests and the running service all happen inside containers, so the
host's Python version is irrelevant.

Verified against Docker 29.2.1 / Compose v5.0.2 on `linux/arm64`
(Apple Silicon locally, Oracle Ampere A1 deployed). All three images are native
arm64; there is deliberately no `platform:` key anywhere.

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

### Get a token (locally)

Everything under `/api/v1` requires a Keycloak bearer token. `/health` does not
and never will — the container healthcheck has no token to present.

The realm, its confidential client and the audience mapper are all created by
`docker compose up` from the committed export at
`deploy/keycloak/realm-linkedin.json`. There is no console step. The client runs
`client_credentials` only — every browser flow is switched off — so minting is
one `curl` with no redirect anywhere in it.

Every block below is runnable **verbatim** from the repository root, against a
**local** stack that is already up — the deployed equivalents, which depend on
no local file at all, are at the top of this section. The first line loads
`.env`, which is where your client secret, realm and issuer URL actually live;
without it the commands post an empty secret and get a 401.

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

**The graded lane is the public host, not this one.** The two commands at the
top of this section are the ones the submission is scored on; these are their
local twins.

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

One message for every *rejection*, deliberately: whether your token was expired,
foreign-realm or simply absent, the 401 says the same thing, and the specific
reason is in the API container's logs where an operator can read it and a prober
cannot. That uniformity is about the 401 body and nothing else — this service
does not otherwise try to be uninformative, and the paragraph below is an
example of it being deliberately informative instead.

**A 401 is always about your token.** If *this service* cannot reach Keycloak to
read the realm's signing keys, you get a `502 UPSTREAM_ERROR` with
`"retryable": true` instead, because your token was never checked and telling
you to stop trying would be a claim about a credential nobody looked at. Tokens
already validated against a cached key set keep working through the outage. The
message names the identity provider rather than LinkedIn, which the status code
would have given away regardless.

Every path under `/api/v1` answers that same 401 without a token, whether or not
the path exists. That is for the caller's benefit, not for concealment: someone
who forgot their token gets told they forgot their token, instead of a `404`
about a route that is sitting right there. It is **not** an attempt to hide
which routes exist — `/openapi.json` is unauthenticated and lists every one of
them, by design, because it is this API's documentation.

```bash
docker compose logs api | grep "Rejected request"
# 2026-08-27 08:42:11,204 WARNING app.auth: Rejected request: InvalidAudienceError: Audience doesn't match
```

Every error this API returns — 400, 401, 404, 405, 422, 428, 429, 500, 502, 503
— wears that same envelope. No response ever leaves in FastAPI's default
`{"detail": "..."}` shape. The full table is under
[Fetch a profile](#fetch-a-profile).

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

The local twin of the second graded command. The `url` query parameter is the
whole of the request; everything else about the endpoint is response shape.

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

**Branch on `retryable`, not on the code.** It is a property of the *response*,
not of the code: there is one condition — LinkedIn answering about a different
member — that returns `502 UPSTREAM_ERROR` with `"retryable": false`, because
that condition is permanent and repeating the request cannot change it. The
field exists precisely so you never have to parse prose to find that out.

The three whose default is `"retryable": true` — `RATE_LIMITED`,
`UPSTREAM_CHALLENGE`, `UPSTREAM_ERROR` — are the ones you may never actually
see, because a cached record outranks them. The reverse also holds and matters more: a `428
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
the cached record. The body says so: that response carries `"retryable": false`
even though `UPSTREAM_ERROR` is retryable everywhere else, and the flag on the
wire is authoritative over the table above. The condition is permanent, and
under a cache with no expiry a stale `200` would republish the old identity
mapping for ever without ever telling you the URL has stopped meaning what you
think.

Hiding a dead cookie behind cached data would report success forever about a
credential that stopped working, which is the one failure this design exists to
avoid.

> **The one gap in that promise, and it is real.** LinkedIn does not always
> *state* a refusal as a refusal. A dead `li_at` is sometimes answered with a
> redirect to an authwall carrying a `200`, which is the same page a datacenter
> IP draws with a perfectly good session — this service genuinely cannot tell
> them apart, so **on a profile fetch** it classifies both as
> `UPSTREAM_CHALLENGE`, which is retryable, which means that particular kind of
> dead session **is** stale-served. This was verified against the running stack,
> not theorised.
>
> The gap is narrower than it was. `PUT /api/v1/session` verifies your cookie
> against the `me` endpoint, which describes the session's *own owner* — the
> same wall in reply to *that* question is about the cookie and nothing else, so
> it is read as expiry and the `PUT` answers `last_use_ok: false` immediately.
> So: if `stale` has been `true` for longer than you can explain, re-`PUT` your
> session. Neither endpoint will ever report a false `true`.

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

The working tree is the easy half. The **history** is the half that matters,
because a secret removed in a later commit is still in the repository for ever:

```bash
docker run --rm -v "$PWD":/repo -w /repo zricethezav/gitleaks:latest \
  git --config .gitleaks.toml --redact
```

`.gitleaks.toml` keeps gitleaks' default rule set in full and adds exactly three
allowlist entries, each **anchored to one literal value** and none of them
path-based:

| Allowlisted value | Why it is not a leak |
|---|---|
| The `SESSION_ENCRYPTION_KEY` placeholder in `.env.example` | A valid Fernet key is 44 base64 characters, so the generic-api-key rule reads it as a credential. Base64-decode it and it says `change-me-generate-a-real-key!!!`. It has to be a *valid* key or `cp .env.example .env && docker compose up` would die on a clean clone |
| The identifier `OptionalSecret` | A Python type-annotation name. `linkedin` followed by a 14–16 character token trips the `linkedin-client-id` rule, and the string is quoted in committed design notes where a rename cannot reach it |
| The evaluator client secret | A **real** credential, published on purpose — see [About that client secret](#about-that-client-secret) |

**Deliberately not by path.** A `paths` entry makes gitleaks skip a whole file
before it reads a line of it, so a genuine secret pasted into `.env.example`
would sail straight through — verified, and the reason there is not one. A
one-string allowlist can only ever silence that one string.

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

```bash
curl -sS https://shreyaskaushik.dpdns.org/openapi.json \
  | python3 -c "import sys,json;print(sorted(json.load(sys.stdin)['paths']))"
# ['/api/v1/profile', '/api/v1/session', '/health']
```

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
OAuth2 `clientCredentials` flow with its `tokenUrl` pointing at the realm:

```
https://shreyaskaushik.dpdns.org/realms/linkedin/protocol/openid-connect/token
```

Tokens are validated against the realm's JWKS, with `iss` and `aud` checked by
exact equality. Access tokens live 900 s. Swagger UI's **Authorize** button
drives the same flow — see the caution under
[Get a token (locally)](#get-a-token-locally) before typing a deployed secret
into a browser.

### The success envelope

`GET /api/v1/profile` returns `ProfileEnvelope`. The profile itself is nested
inside a wrapper that carries the provenance, and reading the wrapper first is
the point:

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

- **`stale`** — `false` if `profile` was read from LinkedIn during this request,
  `true` if the live call failed and a stored record was served instead. There
  is no third value.
- **`fetched_at`** — when the returned profile was read from LinkedIn, never
  when the response was served. Nothing re-stamps it.
- **`partial`** — always present, `[]` on a complete answer. It carries the
  absent-versus-unreadable distinction that the whole schema is built around: a
  key that is **present** is a positive claim, a key that is **omitted** and
  named here means "we could not read it and are not going to guess". Full
  treatment, including dotted sub-field names, under
  [Fetch a profile](#fetch-a-profile).

`PUT`/`GET /api/v1/session` return `SessionResponse`:
`{"stored": bool, "stored_at": ..., "last_used_at": ..., "last_use_ok": bool|null}`.

Every response, success included, carries `Cache-Control: no-store`. These
bodies are specific to the caller, and a shared cache in front of the service
keys on nothing that distinguishes one from another.

### The error envelope

**Every non-2xx response wears the same shape.** Nothing ever leaves in
FastAPI's default `{"detail": "..."}`, and nothing ever returns a naked 500 or a
stack trace:

```json
{"error": {"code": "SESSION_EXPIRED",
           "message": "LinkedIn refused the stored session.",
           "retryable": false}}
```

`code` is a stable machine-readable string; `message` is for a human;
`retryable` says whether repeating the request could plausibly succeed.

**Branch on `retryable`, not on the code.** It is a property of the *response*,
not of the code — there is one condition where the same code carries the
opposite flag, and it is documented in
[Known limitations](#retryable-on-the-wire-outranks-the-documented-table).

The full status/code table is under [Fetch a profile](#fetch-a-profile). In
summary: `400 INVALID_URL`, `401 UNAUTHENTICATED`, `404 PROFILE_NOT_FOUND`,
`405 METHOD_NOT_ALLOWED`, `422 INVALID_REQUEST`, `428 NO_SESSION` /
`428 SESSION_EXPIRED`, `429 RATE_LIMITED`, `502 UPSTREAM_CHALLENGE` /
`502 UPSTREAM_ERROR`, `503 SERVICE_UNAVAILABLE`, `500 INTERNAL_ERROR`.

---

## Approach

### The shape of the problem

LinkedIn publishes no public profile API. The data an assignment like this asks
for — name, headline, location, about, experience, education, skills,
certifications, languages, images — exists only behind an authenticated session,
and the environment is actively adversarial: authwalls, challenge pages,
datacenter-IP reputation, and an internal API that is unversioned and can change
without notice. So the interesting decisions are not about parsing. They are
about **whose credential is spent, what happens when the answer does not come,
and how the response tells the truth about what it could not read.**

### Credential model — bring your own session

The obvious design, and the one the assignment's own wording suggests, is a
single LinkedIn account held in the backend. It was rejected: it concentrates
every rate limit and every security challenge onto one account, so **one lockout
takes the whole service down** during exactly the window it is being graded in.
A pool of rotated accounts is the throughput answer and was rejected as
unbuildable in the time available — cold accounts trip security checks quickly.

What shipped is **per-caller BYO session**: you `PUT` your own `li_at` once, it
is encrypted with Fernet, bound to your token's `sub`, and every fetch you make
runs under it. Exposure is distributed across callers rather than pooled on one
account, and each request is made under the requester's own authenticated
session rather than a shared harvesting account. The costs are real and were
accepted: encrypted per-user secret storage, an upload flow, and a revocation
story that is thinner than it should be (see
[Known limitations](#there-is-no-way-to-delete-a-stored-session)).

The subject is encrypted **inside** the ciphertext, not merely stored in a
column beside it. Fernet has no associated-data parameter, so its tag proves
"written with the key", not "written for this row" — without the binding,
anyone who could write the table but not read the key could move caller A's
session into caller B's row and B would silently run under A's LinkedIn
identity.

**The evaluator lane bends this deliberately.** The `linkedin-profile-api`
client's vault row is pre-seeded so the two graded commands work from a machine
that has never authenticated. That means evaluator traffic runs under the
author's own session — concentrating precisely the exposure this model was
chosen to avoid. It is a knowing trade for a literal reading of the grading
criterion, and it is the first entry under
[Known limitations](#the-evaluator-lane-runs-under-the-authors-own-linkedin-session).

### Retrieval — Voyager JSON, and no browser

Three options were on the table:

| Option | Verdict |
|---|---|
| Public HTML, logged out | **Rejected.** No credential risk at all, but LinkedIn serves authwalls to most datacenter IP ranges and truncates what remains. It would fail the field-coverage requirement outright |
| Headless browser as the primary path | **Rejected as primary.** More resilient to API shape changes, but 400–700 MB of Chromium per request, slow, and `arm64` support needed verifying on the one instance that exists |
| **Voyager JSON (chosen)** | LinkedIn's own internal API returns structured JSON directly. It is the literal task, and it is one HTTP client with no rendering engine |

The browser path was written into the plan as a **fallback** and then demoted
from Must to Could once the RAM arithmetic was laid out. **It was never built,
and there is no Playwright, Selenium or Chromium anywhere in this repository** —
`requirements.txt` is seven packages and none of them renders a page. Saying so
plainly is better than leaving a reader to infer a fallback that does not exist.

One fetch is **six** calls: one for the core profile, then five concurrent
section calls (experience, education, skills, certifications, languages). There
is no retry, because the failures worth retrying are the ones an immediate retry
makes worse. Sections are requested with `count=100` rather than the default 20
— found the hard way, when a profile with 33 skills returned 20 of them with a
`200` and no error. Beyond 100 the shortfall is **reported** rather than
silently truncated: the section is omitted and named in `partial`.

The client is the only place in the codebase that puts a LinkedIn session on the
wire, calls `linkedin.com`, or knows the endpoint map. It refuses to follow a
redirect off `linkedin.com` while carrying the cookie — verified, because the
naive version forwards a manually-set `Cookie` header across hosts.

### Staleness — answer, or explain why not

LinkedIn will refuse sometimes, and an evaluation service that returns `502` at
that moment has failed at the only thing it does. So every successful answer is
stored, and when a live retrieval fails **for a reason retrying could fix**, the
last good record is returned instead, with `stale: true` and the original
`fetched_at`.

Three properties make that honest rather than a lie of convenience:

1. **Only retryable failures fall back.** A permanent failure reaches you as
   itself, however good the cached copy is. A dead session is `428`, not a
   comfortable `200`.
2. **The record is served exactly as stored.** Same `profile`, same `partial`,
   same omitted keys — nothing is re-derived on the way out, so "this was true
   once, at this timestamp" is a checkable claim.
3. **It is unbounded, and that is the trade.** No TTL, no eviction, no delete
   endpoint. An answer you can date and judge beats an error page. `fetched_at`
   is what makes it actionable, and refusing any response with `stale: true` is
   a one-line client-side check.

The rejected alternative was a TTL. It would have converted "old but dated
answer" into "error", which is the failure this design exists to avoid.

### Evaluator access — Keycloak service accounts

Leaving the service fully open is the most literal reading of "deploy publicly"
and was rejected: any traffic that finds the URL burns real LinkedIn quota
against somebody's real account. Google SSO alone was rejected as the sole lane
because it is not scriptable — an evaluator who hits a browser redirect may
simply record the endpoint as unreachable.

**Keycloak `client_credentials`** is what shipped: two `curl` commands, no
browser, standard OAuth2, and the realm is created from a committed export
(`deploy/keycloak/realm-linkedin.json`) on container start, so there is no
console step to remember. The export carries `${KEYCLOAK_CLIENT_SECRET}`
placeholders substituted at import, and is secret-free by construction.

The realm ships **two** confidential clients on purpose. One
`client_credentials` client is one service-account user and therefore one `sub`
— per-caller isolation was real in the code and undemonstrable in the
deployment. The second client is a second subject and nothing else.

### Deployment

Cloudflare (proxied) → OCI load balancer, which terminates TLS and holds the
certificate → host nginx on port 80 → the compose stack on loopback. The
instance has no public IP; its only inbound path is the load balancer, and
nginx is the only process on it that answers off-loopback. Application
containers bind `127.0.0.1` exclusively.

The whole application is one `docker compose up`, identical image locally and
deployed — only `.env` differs, and there is no `APP_ENV` or any code path that
branches on an environment name. Full runbook, including the traps that cost
real time (Oracle's host `iptables` dropping port 80 independently of the
Security List; the load-balancer health check needing a route that actually
exists), is in [`deploy/README.md`](deploy/README.md). Diagrams are in
[`docs/architecture.md`](docs/architecture.md).

### Repository layout

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
.gitleaks.toml       secret-scan config: the default rules, plus three
                     value-anchored allowlists — two known non-secrets and the
                     deliberately published evaluator client secret
deploy/
  README.md          the deployment runbook: topology, the Oracle firewall trap,
                     the redeploy recipe, and how to re-export the realm safely
  keycloak/          the committed realm export, imported on container start,
                     with ${...} placeholders where the client secrets go
  nginx/             the deployed site config — port 80 only, no certificate
  open-ports.sh      the host-iptables step, insert-only and non-persisting
docs/
  architecture.md    diagrams: topology, request flow, retrieval fan-out,
                     the auth boundary, and the decision log
Dockerfile           slim python base, non-root, deps layer before source
docker-compose.yml   api + keycloak + postgres, healthchecked, loopback-only
.env.example         the env contract
```

---

## Known limitations

Written candidly and at length, because the honest failure modes of a service
like this are more informative than a list of features. Every item below is
either **observed** against the running system or a **deliberate decision** —
where it is a decision, it says so and says why.

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

### The evaluator lane runs under the author's own LinkedIn session

The two graded commands work from a cold machine because the
`linkedin-profile-api` client's vault row is **pre-seeded with the author's own
`li_at`**. Every consequence of that is real:

- Evaluator traffic spends the author's LinkedIn quota and accumulates against
  the author's account — concentrating exactly the rate-limit and
  terms-of-service exposure the BYO model was chosen to distribute.
- If that account is challenged or locked during grading, the graded lane
  returns `502 UPSTREAM_CHALLENGE`, or a stale `200` if the profile was fetched
  before.
- Anyone holding the published client secret can spend that quota.

It is a deliberate, author-approved trade for the criterion being graded — two
commands, verbatim, no prior authentication — and not an oversight. `PUT`ting
your own cookie (the third command in [Setup](#setup)) moves you off it.

### The published evaluator client secret

Restating it here because it belongs in any honest limitations list: a **real
working credential is committed to this repository**, deliberately, so the
graded commands run verbatim. It reaches only this evaluation service and grants
nothing beyond the three routes — but it is permanent in the git history once
published, and anyone who finds it can call the API and spend the LinkedIn quota
described above. **It should be rotated after grading.** Full reasoning under
[About that client secret](#about-that-client-secret).

### A dead cookie can be reported as staleness rather than expiry

**Verified against the running stack, not theorised.** LinkedIn does not always
state a refusal as a refusal: a dead `li_at` is sometimes answered with a
redirect to an authwall carrying a `200`, and that is the *same page* a
datacenter IP draws with a perfectly healthy session. On a profile fetch this
service genuinely cannot tell them apart, so it classifies both as
`502 UPSTREAM_CHALLENGE` — which is retryable — which means that particular kind
of dead session **is** stale-served, indefinitely, and the caller is never told
to store a new cookie.

The gap is narrower than it was, but it is not closed. `PUT /api/v1/session`
verifies the cookie against LinkedIn's `me` endpoint, which describes the
session's own owner; a wall in reply to *that* question is evidence about the
cookie and nothing else, so it is read as expiry and the `PUT` answers
`last_use_ok: false` immediately. On a profile fetch the two pages remain
indistinguishable. **Practical consequence: if `stale` has been `true` for
longer than you can explain, re-`PUT` your session.**

The converse is evidenced in one direction only. Nothing establishes that
LinkedIn never walls `me` for a *healthy* session from a datacenter IP; if it
does, `PUT` reports `last_use_ok: false` about a credential that works. The
blast radius is bounded — `me` is reached only by the `PUT` verification, never
by the profile route — so a wrong verdict costs a misleading bookkeeping field
rather than a failed fetch.

### The cache is keyed by profile; LinkedIn's retrieval is viewer-relative

A record fetched under one caller's session answers another caller's request for
the same profile. Access is controlled — the session check happens *before* the
cache is consulted, so nobody without a working session of their own can reach
it — but **content is not**. LinkedIn's profile responses depend on the viewer:
connection degree, and whatever privacy settings the member applies to people
outside it, change what comes back. So a stale answer can be a **richer** view
than the requesting caller's own session would ever have retrieved live.

Accepted knowingly for a single-evaluator service. It would not be acceptable
multi-tenant, where the cache key would have to include the viewer — which also
multiplies the LinkedIn call cost by the number of callers, so it is a real
design change and not a one-line fix.

### Stale records carry image URLs that have expired

The `images` URLs LinkedIn returns are **signed and time-limited** — a live
fetch returns things like `…?e=1789603200&v=beta&t=…`. Because stale-serve is
unbounded, a record served long after it was fetched carries URLs that `403`.

They are deliberately **not** stripped. Removing them would mean re-shaping the
record on the way out, which is exactly what "served exactly as it was stored"
forbids; and a missing `images` key would say "this member has no photo" — a
claim about the member, when the truth is a fact about a URL. `fetched_at` tells
you how likely it still resolves. The honest fix, if this ever mattered, is a
media-proxy endpoint that re-signs on demand, not a mutation of the stored
record.

### The cache grows without bound and nothing can remove one record

No TTL, no eviction, no delete endpoint — `ProfileCacheStore` deliberately
exposes no delete so it cannot be reintroduced by accident. The table grows by
roughly 7 KB per distinct profile ever fetched. **The only way to drop a record
is `docker compose down -v`, which also destroys the Keycloak realm and every
stored session.**

Two consequences worth naming: there is no way to honour a deletion request for
a cached profile short of dropping everything, and a profile whose owner has
since made it private keeps being republished from cache. Do not add a TTL
without renegotiating the design — the entire stale-serve argument rests on its
absence.

### There is no way to delete a stored session

`PUT` overwrite is the whole lifecycle. The remedy for "my cookie leaked" is
"supply a second valid cookie", which is not the same thing as revocation.

Bounded rather than fixed: the stored value is unreadable without
`SESSION_ENCRYPTION_KEY`, is returned by no endpoint under any flag, and
revoking at LinkedIn's end ("log out of all sessions") invalidates the cookie
whatever this vault holds. The shape that would close it is
`DELETE /api/v1/session` keyed on the verified `sub`.

### A revoked token stays accepted for up to 900 seconds

Token validation is stateless JWT verification against the realm's JWKS, with no
introspection call. So a rotated client secret or a disabled service account
does not take effect until the outstanding access token expires — **up to 900 s**.
Inherent to the design, acceptable for an evaluation service, and worth knowing
before rotating the published secret and assuming it took effect immediately.

Related: `require_claims` **authenticates but does not authorise**. Beyond issuer
and audience there is no `azp`, scope or role check, so any other realm client
carrying an audience mapper aimed at `linkedin-profile-api` would get full
access. Deliberate — a Should-tier Google SSO lane would mint user tokens with a
different `azp` — but it is one realm-configuration mistake away from mattering.

### The committed encryption-key placeholder is a valid key

`SESSION_ENCRYPTION_KEY` in `.env.example` is a **real, valid Fernet key** that
base64-decodes to `change-me-generate-a-real-key!!!`. It has to be valid, or
`cp .env.example .env && docker compose up` would die on a clean clone — which
is an acceptance criterion. **A deployment that never replaces it therefore
encrypts every stored cookie under a key printed in a public repository.**

The stricter alternative — refuse to boot on the placeholder — was considered
and rejected for that reason. It is mitigated by loudness rather than fatality:
the API logs `CRITICAL` on every start when the configured key is the shipped
placeholder. Tests pin both that it still boots and that it shouts. The change
is one line if this service ever outlives the evaluation.

### Rotating the encryption key silently orphans every stored session

There is no re-encryption path. Rows written under an old
`SESSION_ENCRYPTION_KEY` cannot be decrypted, and they surface as
`428 SESSION_EXPIRED` — the same code a genuinely dead cookie produces. The real
reason is in the API log; the caller sees only "store a new one". Key rotation
is an ordinary operational act with a non-obvious consequence here, and the
remedy is one `PUT` per caller.

### `retryable` on the wire outranks the documented table

A deliberate, approved deviation from a published contract, called out because a
client that trusts the table will get this wrong.

`retryable` is a property of the **response**, not of the code.
`response-schema.md` marks `UPSTREAM_ERROR` retryable — and it is, everywhere
except one case: a fetch that comes back naming a **different member** than the
URL asked for returns `502 UPSTREAM_ERROR` with `"retryable": false`, because
that condition is permanent and repeating the request cannot change it. The wire
value is authoritative over the table for that case. Adding a new taxonomy row
was the declined alternative. **Branch on the flag, not on the code.**

### Route existence is partially observable without a token

`/api/v1` paths answer `401` whether or not they exist, which closes the obvious
enumeration channel — but **enumeration resistance is not a property this
service claims**, and two things leak:

- `/openapi.json` is unauthenticated and publishes every route, by design,
  because it is this API's documentation.
- FastAPI reads and parses a request body *before* route dependencies run, so
  `PUT /api/v1/session` with malformed bytes returns `400` while the same
  malformed request to a non-existent path returns `401`.

Closing the second means moving authentication into middleware ahead of body
parsing — a larger change than the leak justifies given the first. Accepted, and
stated rather than implied.

Related: because the unmatched-path guard matches every method, an `OPTIONS`
preflight to any `/api/v1` path is answered `401` before anything CORS-aware
runs. Nothing is broken today — there is no CORS middleware and no browser
client — but whoever adds a browser lane must install `CORSMiddleware` ahead of
the router rather than carving an exception into the guard.

### The Voyager endpoint map is undocumented, unversioned, and verified against one profile

LinkedIn's internal API is not a published interface. It can change without
notice, and the endpoint most third-party documentation still names is already
`410 Gone`. One opt-in test (`tests/test_linkedin_live.py`, skipped by default,
two gates to run) is the only assertion that can catch a shape change, and it
fetches **only the profile the session itself owns** — so per-profile shape
variation is untested. A profile with no certifications, a hidden headline, or a
non-`en_US` primary locale may carry shapes no fixture mirrors. The mapper
treats every field as optional for exactly this reason, but "degrades into
`partial[]`" is the best guarantee available, not "works".

Measured, and worth knowing: `profileLanguages` returned **0 elements** on one
call and **3** on an identical call minutes later, HTTP 200 both times. A
zero-length section is therefore not evidence that the member lacks that data,
which is why empty sections can land in `partial[]` rather than being published
as `[]`.

### Sections beyond 100 entries are reported, not retrieved

Each section is requested with `count=100` in a single call. Beyond that the
shortfall is **visible rather than fixed**: the section is omitted from
`profile` and named in `partial`. Following further pages would multiply the
per-request call count against LinkedIn and was not done.

The truncation signal is also slightly conservative: a section returning exactly
100 elements with no `paging.total` is reported as possibly truncated, which
will occasionally be a false positive. The two errors are not symmetric — a
false positive costs a caller an unnecessary caveat, a false negative publishes
a partial career as a complete one.

### A whole section is discarded for one unreadable entry

A single element that cannot be mapped onto the contract shape sends its
**entire** section to `partial[]`, discarding the entries that mapped fine.
Deliberate, and the same argument as truncation: dropping the bad entry silently
would shorten somebody's career without saying so, and the envelope has exactly
two states for a field — present-and-complete, or omitted-and-named. There is no
way to say "here are four of five roles" in the current contract. Reached only
by a non-object element, or by a skills entry with no readable name.

Similarly, `images.profile` and `images.background` conflate **absent** and
**unreadable**: a picture that exists but whose `vectorImage` cannot be joined
into a URL is `null`, the same as a member with no picture, and `images` never
reaches `partial[]`. Fixing it properly means `partial` accepting dotted paths
for nested scalars, which is a contract change.

### `employment_type` is never resolved

You will see `experience.employment_type` in `partial` on most real profiles.
LinkedIn references an employment type on each position
(`urn:li:fsd_employmentType:12`) and delivers nothing that names it. Publishing
the URN in a field a consumer would read as "Full-time" is an unreadable value
dressed as a readable one, and decoding it from a remembered lookup table would
be this service guessing at a label for somebody's job. So it is omitted and
reported. This is correct behaviour rather than a defect, but it is a field the
assignment's consumer might expect to be populated.

### A changed vanity URL is refused rather than resolved

If the profile returned does not carry the `public-id` that was asked for, the
request fails with `502` — and **not** from cache, even when a record exists.
Fail-closed on purpose: serving one person's profile under another's URL, then
caching it unboundedly, is the worst failure available here.

The cost is untested and real. It is unknown whether LinkedIn's `memberIdentity`
lookup resolves an old vanity name to the current profile; if it does, a
legitimate old URL becomes a hard failure. Refusing is recoverable by the caller;
answering with the wrong person is not.

### Operational gaps

None of these bite an evaluation. All of them would bite anything longer-lived.

- **No migration tool.** The schema is created by an idempotent
  `CREATE ... IF NOT EXISTS` bootstrap on every boot, so there is no migration
  history and no down-path. Adding a **column** to an existing table is a trap:
  the DDL is a no-op on a warm volume and every statement naming the column then
  fails — and on the cache path that failure is swallowed by design, so it would
  ship as a silently dead cache. Until a tool exists, a new column needs an
  accompanying `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`.
- **One instance, no redundancy.** A single Oracle Ampere A1 VM. No autoscaling,
  no failover, no backups of the Postgres volume. Hosting was reaffirmed as
  Oracle-only after non-Oracle alternatives were raised, as a deliberate
  accepted risk.
- **No rate limiting.** Nothing throttles a caller, so a holder of the published
  secret can spend LinkedIn quota as fast as the upstream allows.
- **The API shares Postgres with Keycloak as the same superuser.** Application
  tables live in an `app` schema and Keycloak owns `public`, so they cannot
  collide over a name — but a bug in the API can reach Keycloak's identity
  tables. A least-privilege role is the fix.
- **No CI.** `pre-commit` and the test suite are opt-in local installs a
  contributor can skip; nothing enforces gitleaks or the tests on push. Every
  verification in this README was run by hand.
- **Every rejected request logs at `WARNING`**, so an unauthenticated flood is
  also a log flood, and there is no log rotation configured for the `api` or
  `postgres` containers.
- **Timeouts are a backstop, not a budget.** Each of the six calls has a 15 s
  timeout and the whole fetch a 45 s deadline — set deliberately *above* the
  worst legitimate case so it never kills a merely-slow fetch. A healthy-but-slow
  request can therefore hold a connection open for ~30 s. `asyncio.timeout` also
  cannot cancel work already inside `asyncio.to_thread`, so the thread runs to
  completion even after the request is abandoned.

---

Built for a graded evaluation, not for production operation.
