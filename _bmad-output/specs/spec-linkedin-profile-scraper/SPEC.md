---
id: SPEC-linkedin-profile-scraper
companions:
  - response-schema.md
  - scope-tiers.md
  - ../../planning-artifacts/briefs/brief-linkedin-profile-scraper-2026-08-27/addendum.md
sources:
  - ../../planning-artifacts/briefs/brief-linkedin-profile-scraper-2026-08-27/brief.md
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# LinkedIn Profile API

## Why

A mandate with a deadline: a graded take-home requiring a publicly hosted HTTPS API that accepts a LinkedIn profile URL and returns the profile as structured JSON, submitted by 2026-08-29 with a public repository and a README covering setup, API documentation, approach, and known limitations. LinkedIn publishes no public profile API, so the data must be retrieved from its internal endpoints under an authenticated session — an adversarial environment where sessions expire, datacenter IPs draw challenge pages, and unversioned endpoints shift without notice. The evaluator will exercise the deployed URL at an unknown time after submission against a profile never tested during development. That single fact drives every trade-off here: the system is graded on whether it still answers, and on whether its documentation already predicted how it would fail.

## Capabilities

- **CAP-1**
  - **intent:** A caller submits a LinkedIn profile URL and receives that profile as structured JSON.
  - **success:** For a profile not used during development, the response populates every field in `response-schema.md` that is present on the source profile, with correct absent-versus-null semantics for those that are not.

- **CAP-2**
  - **intent:** A caller must prove identity before reaching any profile data.
  - **success:** A request carrying no token, an expired token, or a token from another realm is rejected with 401; only a valid realm-issued token reaches the retrieval path.

- **CAP-3**
  - **intent:** An evaluator obtains a working token and calls the API without a browser.
  - **success:** Two `curl` commands copied verbatim from the README — one to mint a token, one to call the endpoint — return a populated profile on a machine that has never authenticated to the system.

- **CAP-4**
  - **intent:** An authenticated user supplies their own LinkedIn session, and their requests are made under it.
  - **success:** A stored session is recoverable only by the Keycloak subject that supplied it, is unreadable in the datastore without the encryption key, and a second user's request demonstrably uses the second user's session. Re-supplying a session replaces the stored value outright.

- **CAP-5**
  - **intent:** The API keeps answering for previously seen profiles after LinkedIn stops answering.
  - **success:** With retrieval forced to fail, a previously fetched profile still returns 200 with its full payload, `"stale": true`, and the original fetch timestamp — regardless of how old that record is.

- **CAP-6**
  - **intent:** A caller can tell what went wrong and whether retrying will help.
  - **success:** Every failure path — invalid URL, missing session, expired session, rate limit, profile not found, upstream challenge — returns the typed error body in `response-schema.md` with its mapped status code. No unhandled exception reaches the client.

- **CAP-7**
  - **intent:** Someone with only the repository can run the service locally.
  - **success:** On a clean machine, a fresh clone plus the README's documented steps brings up the service, and the same request shape works locally and against the deployed URL with only the env file differing.

- **CAP-8**
  - **intent:** The submission explains itself without the author present.
  - **success:** The README contains all four required sections — setup, API documentation, approach, known limitations — and the limitations section names the specific failure modes in `../../planning-artifacts/briefs/brief-linkedin-profile-scraper-2026-08-27/addendum.md` rather than generic caveats.

## Constraints

- Profile data is retrieved from LinkedIn's internal Voyager JSON endpoints under an authenticated session. Parsing rendered HTML and logged-out public-page scraping are both ruled out — the former as the fallback tier only, the latter entirely.
- All configuration arrives through environment variables. `.env.example` is committed with dummy values, the real `.env` is gitignored, and the same image runs locally and deployed with only the env file differing. Hardcoded or environment-branched configuration is ruled out.
- No credential, cookie, key, or secret appears in the repository or anywhere in its git history.
- `li_at` cookies are encrypted at rest under a key supplied by environment variable.
- Hosting is a running Oracle Cloud ARM Ampere A1 instance on `shreyaskaushik.dpdns.org`. Alternate hosts were offered and declined.
- Deployment topology: Cloudflare DNS resolves to an OCI Load Balancer, which forwards to the instance, where host-installed nginx reverse-proxies into the Docker Compose stack. Application containers are never bound directly to public ports.
- The entire application stack — API, Keycloak, Postgres — comes up under a single `docker compose`. nginx and Docker are host-installed and are the only components configured outside compose, making the nginx configuration the sole deployment-time wiring step.
- The runtime footprint is roughly 2 GB and above, so the Always Free AMD 1 GB micro shape could not have hosted this stack. The ARM Ampere shape is required rather than preferred.
- Python 3.11+ with FastAPI and Pydantic. The generated OpenAPI document serves as the README's API documentation.
- Submission is due 2026-08-29. Under time pressure, scope is cut in the order fixed in `scope-tiers.md`; the Must tier is never cut.
- The repository is public and contains the complete source.

## Non-goals

- Bulk or batch endpoints. One profile per request.
- Company pages, job posts, people search, posts, connections, and activity feeds.
- Any frontend beyond the screens Keycloak provides.
- Automated session refresh or programmatic re-login when a `li_at` cookie dies. Expiry is surfaced, not repaired.
- A delete or revoke path for a stored session. Re-supplying via `PUT` overwrites, and that is the whole of the lifecycle.
- Cache expiry. Stale-serve is unbounded by decision; no TTL, no eviction policy.
- Production operation. This is built for evaluation, and the README says so.
- Defeating challenge pages, CAPTCHAs, or IP-reputation blocks. They are reported as typed errors and absorbed by stale-serve.

## Success signal

At an arbitrary time after submission, an evaluator copies two `curl` commands from the README, runs them against `https://shreyaskaushik.dpdns.org`, and gets back a complete, well-formed JSON profile for a LinkedIn URL that was never tested during development — or, if LinkedIn refuses the live call, the last good record explicitly flagged stale, never an error page or a naked 500. Independently, a cold clone of the public repository plus the documented setup steps produces a working local instance, and no secret is found anywhere in the git history.

## Assumptions

- The unit of LinkedIn authentication is the `li_at` session cookie, pasted by the user, not a username-and-password login performed by the service.
- The evaluator will use `curl` or an equivalent scriptable client rather than a browser.
- Cached records live in the Postgres instance already required by Keycloak; final storage choice is left to implementation.
- Profile URLs are the public `/in/{public-id}` form.

## Open Questions

- Where does TLS terminate — Cloudflare, the OCI load balancer, or nginx? This determines the nginx configuration, whether the load balancer needs a certificate, and whether Let's Encrypt renewal is in scope. Recommended: Cloudflare proxied, with a Cloudflare Origin Certificate installed at nginx — free, 15-year, and no renewal to manage.
- Is the evaluator also given a Google SSO account, or is the service-account lane the only one documented?
- What concrete rate-limit values apply if the Should tier is built?
