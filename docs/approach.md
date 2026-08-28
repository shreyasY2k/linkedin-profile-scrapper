# Approach, in full

The design rationale behind every decision the README's
[Approach](../README.md#approach) section summarises, plus the repository
layout. Diagrams are in [`architecture.md`](architecture.md); the deployment
runbook is in [`../deploy/README.md`](../deploy/README.md).

## The shape of the problem

LinkedIn publishes no public profile API. The data an assignment like this asks
for — name, headline, location, about, experience, education, skills,
certifications, languages, images — exists only behind an authenticated session,
and the environment is actively adversarial: authwalls, challenge pages,
datacenter-IP reputation, and an internal API that is unversioned and can change
without notice. So the interesting decisions are not about parsing. They are
about **whose credential is spent, what happens when the answer does not come,
and how the response tells the truth about what it could not read.**

## Credential model — bring your own session

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
[Known limitations](limitations.md#there-is-no-way-to-delete-a-stored-session)).

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
[Known limitations](limitations.md#the-evaluator-lane-runs-under-the-authors-own-linkedin-session).

## Retrieval — Voyager JSON, and no browser

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

One fetch is **six** calls in the normal case: one for the core profile, then
five concurrent section calls (experience, education, skills, certifications,
languages). There is no retry, because the failures worth retrying are the ones
an immediate retry makes worse. Two documented paths can add a seventh, and
neither is a retry of a failure: a core request whose decoration LinkedIn
refuses is repeated once without it (the profile then carries no `region`), and
a `403` from a profile resource that is not a wall spends one memoized call on
`me` to decide whether the refusal is about the member or about the cookie.

Sections are requested with `count=100` rather than the default 20 — found the
hard way, when a profile with 33 skills returned 20 of them with a
`200` and no error. Beyond 100 the shortfall is **reported** rather than
silently truncated: the section is omitted and named in `partial`.

The client is the only place in the codebase that puts a LinkedIn session on the
wire, calls `linkedin.com`, or knows the endpoint map. It refuses to follow a
redirect off `linkedin.com` while carrying the cookie — verified, because the
naive version forwards a manually-set `Cookie` header across hosts.

## Staleness — answer, or explain why not

LinkedIn will refuse sometimes, and an evaluation service that returns `502` at
that moment has failed at the only thing it does. So every successful answer is
stored, and when a live retrieval fails **for a reason retrying could fix**, the
last good record is returned instead, with `stale: true` and the original
`fetched_at`.

Three properties make that honest rather than a lie of convenience:

1. **Only retryable failures fall back.** A permanent failure reaches you as
   itself, however good the cached copy is. A dead session that LinkedIn
   *states* as a refusal is `428`, not a comfortable `200`.

   **The qualifier is load-bearing and the gap is real.** LinkedIn does not
   always state a refusal as a refusal: a dead `li_at` is often answered with an
   authwall carrying a `200`, which is the same page a datacenter IP draws with
   a perfectly healthy session. On a profile fetch the two are
   indistinguishable, so both classify as `502 UPSTREAM_CHALLENGE` — which is
   retryable — and that kind of dead session **is** stale-served, indefinitely.
   So the honest form of the property is "only retryable failures fall back",
   not "a dead session always reaches you as a 428". `PUT /api/v1/session`
   narrows it by verifying against `me`, where a wall *is* evidence about the
   cookie, but it does not close it. Full treatment under
   [Known limitations](limitations.md#a-dead-cookie-can-be-reported-as-staleness-rather-than-expiry).

2. **The record is served exactly as stored.** Same `profile`, same `partial`,
   same omitted keys — nothing is re-derived on the way out, so "this was true
   once, at this timestamp" is a checkable claim.

3. **It is unbounded, and that is the trade.** No TTL, no eviction, no delete
   endpoint. An answer you can date and judge beats an error page. `fetched_at`
   is what makes it actionable, and refusing any response with `stale: true` is
   a one-line client-side check.

The rejected alternative was a TTL. It would have converted "old but dated
answer" into "error", which is the failure this design exists to avoid.

## Evaluator access — Keycloak service accounts

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

## Deployment

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
exists), is in [`deploy/README.md`](../deploy/README.md). Diagrams are in
[`docs/architecture.md`](architecture.md).

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
  test_postgres_live.py  the opt-in database round-trip; 16 tests, skipped by
                     default — the larger half of the 17 skips
  test_profile_api.py  GET /api/v1/profile end to end, stubbed client
  support.py         shared test helpers — the single seam between test modules
  test_linkedin_client.py  the retrieval edge-case matrix, entirely offline
  test_linkedin_live.py    the single opt-in LinkedIn check; the 17th skip
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
_bmad-output/        the planning trail this was built from — brief, SPEC, the
                     nine story files, and `specs/spec-linkedin-profile-scraper/
                     response-schema.md`, the wire contract the mapper and the
                     error taxonomy are both written against
Dockerfile           slim python base, non-root, deps layer before source
docker-compose.yml   api + keycloak + postgres, healthchecked, loopback-only
.env.example         the env contract
```
