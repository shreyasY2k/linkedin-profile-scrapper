# Known limitations, in full

The README's [Known limitations](../README.md#known-limitations) section names
every failure mode below and links here for the reasoning. Nothing is
abbreviated on this page.

Written candidly and at length, because the honest failure modes of a service
like this are more informative than a list of features. Every item below is
either **observed** against the running system or a **deliberate decision** —
where it is a decision, it says so and says why.

## Automated collection is contrary to LinkedIn's User Agreement

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

## The evaluator lane runs under the author's own LinkedIn session

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
your own cookie (the third command, under
[Use your own LinkedIn session](../README.md#use-your-own-linkedin-session))
moves you off it.

## The published evaluator client secret

Restating it here because it belongs in any honest limitations list: a **real
working credential is committed to this repository**, deliberately, so the
graded commands run verbatim. It reaches only this evaluation service and grants
nothing beyond the three routes — but it is permanent in the git history once
published, and anyone who finds it can call the API and spend the LinkedIn quota
described above. **It should be rotated after grading.** Full reasoning under
[About that client secret](../README.md#about-that-client-secret).

## A dead cookie can be reported as staleness rather than expiry

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

## The cache is keyed by profile; LinkedIn's retrieval is viewer-relative

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

## Stale records carry image URLs that have expired

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

## The cache grows without bound and nothing can remove one record

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

## There is no way to delete a stored session

`PUT` overwrite is the whole lifecycle. The remedy for "my cookie leaked" is
"supply a second valid cookie", which is not the same thing as revocation.

Bounded rather than fixed: the stored value is unreadable without
`SESSION_ENCRYPTION_KEY`, is returned by no endpoint under any flag, and
revoking at LinkedIn's end ("log out of all sessions") invalidates the cookie
whatever this vault holds. The shape that would close it is
`DELETE /api/v1/session` keyed on the verified `sub`.

## An expired cookie is surfaced, never repaired

When a `li_at` dies, this service reports it and stops. It does not log in to
LinkedIn to mint a fresh one, and that is a decision rather than an omission.

Automating the renewal would not fix the failure that actually dominates here.
Re-login addresses *cookie expiry*; the failure this service meets far more
often is LinkedIn refusing a datacenter IP, and a freshly minted cookie meets
the same authwall on the very next fetch. The machinery would be built and
stale-serve would still be doing the work.

The cost of building it is also badly out of proportion:

- LinkedIn's login is not a form POST. It is a CSRF and `JSESSIONID` handshake
  followed by a challenge flow, and a login attempt from a datacenter address
  draws an email PIN, a device verification, or a CAPTCHA far more reliably
  than a read does. Getting past those is precisely what this project lists as
  a non-goal.
- It would need a headless browser, which was costed and demoted: Chromium adds
  roughly 400–700 MB to a footprint already near the instance's ceiling.
- It would mean storing a username and password **reversibly encrypted**, since
  they must be replayable. A stored cookie that leaks is one expired session; a
  stored password that leaks is the account.
- Automated collection is already contrary to the User Agreement. Automated
  *login* with stored credentials engages the anti-bot and anti-circumvention
  terms as well, and LinkedIn restricts accounts for automated login patterns
  considerably faster than for reading.

The design that would improve this without any of the above is a service-level
fallback session — a `SERVICE_LI_AT` supplied by environment variable and used
only when a caller has stored none of their own, so renewal becomes editing an
env file rather than remembering a `curl`. It is deliberately not built here:
it changes CAP-4's wording and makes `NO_SESSION` unreachable on that path,
which is a contract change, and the contract is itself part of what is being
graded. It is the first thing this service should grow.

## A revoked token stays accepted for up to 900 seconds

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

## The committed encryption-key placeholder is a valid key

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

## Rotating the encryption key silently orphans every stored session

There is no re-encryption path. Rows written under an old
`SESSION_ENCRYPTION_KEY` cannot be decrypted, and they surface as
`428 SESSION_EXPIRED` — the same code a genuinely dead cookie produces. The real
reason is in the API log; the caller sees only "store a new one". Key rotation
is an ordinary operational act with a non-obvious consequence here, and the
remedy is one `PUT` per caller.

## `retryable` on the wire outranks the documented table

A deliberate, approved deviation from a published contract, called out because a
client that trusts the table will get this wrong.

`retryable` is a property of the **response**, not of the code.
`response-schema.md` marks `UPSTREAM_ERROR` retryable — and it is, everywhere
except one case: a fetch that comes back naming a **different member** than the
URL asked for returns `502 UPSTREAM_ERROR` with `"retryable": false`, because
that condition is permanent and repeating the request cannot change it. The wire
value is authoritative over the table for that case. Adding a new taxonomy row
was the declined alternative. **Branch on the flag, not on the code.**

## Route existence is partially observable without a token

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

## The Voyager endpoint map is undocumented, unversioned, and verified against one profile

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

## Sections beyond 100 entries are reported, not retrieved

Each section is requested with `count=100` in a single call. Beyond that the
shortfall is **visible rather than fixed**: the section is omitted from
`profile` and named in `partial`. Following further pages would multiply the
per-request call count against LinkedIn and was not done.

The truncation signal is also slightly conservative: a section returning exactly
100 elements with no `paging.total` is reported as possibly truncated, which
will occasionally be a false positive. The two errors are not symmetric — a
false positive costs a caller an unnecessary caveat, a false negative publishes
a partial career as a complete one.

## A whole section is discarded for one unreadable entry

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

## `employment_type` is never resolved

You will see `experience.employment_type` in `partial` on most real profiles.
LinkedIn references an employment type on each position
(`urn:li:fsd_employmentType:12`) and delivers nothing that names it. Publishing
the URN in a field a consumer would read as "Full-time" is an unreadable value
dressed as a readable one, and decoding it from a remembered lookup table would
be this service guessing at a label for somebody's job. So it is omitted and
reported. This is correct behaviour rather than a defect, but it is a field the
assignment's consumer might expect to be populated.

## A changed vanity URL is refused rather than resolved

If the profile returned does not carry the `public-id` that was asked for, the
request fails with `502` — and **not** from cache, even when a record exists.
Fail-closed on purpose: serving one person's profile under another's URL, then
caching it unboundedly, is the worst failure available here.

The cost is untested and real. It is unknown whether LinkedIn's `memberIdentity`
lookup resolves an old vanity name to the current profile; if it does, a
legitimate old URL becomes a hard failure. Refusing is recoverable by the caller;
answering with the wrong person is not.

## Operational gaps

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
