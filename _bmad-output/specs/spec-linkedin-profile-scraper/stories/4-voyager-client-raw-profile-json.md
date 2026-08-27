---
title: 'Voyager client returning raw profile JSON'
type: 'feature'
created: '2026-08-27'
status: 'draft'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/specs/spec-linkedin-profile-scraper/SPEC.md'
  - '{project-root}/_bmad-output/specs/spec-linkedin-profile-scraper/response-schema.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The API authenticates callers but retrieves nothing. Nothing in the codebase talks to LinkedIn, and the endpoint most documentation still names — `identity/profiles/{id}/profileView` — is **410 Gone**, so any implementation reasoned from memory rather than from the live API will fail.

**Approach:** A single Voyager client that authenticates with an `li_at` cookie, resolves a profile URL to the dash profile entity, and fans out to the five sub-resources, returning the raw normalized JSON. Mapping to the response contract is story 6's; this story stops at retrieval.

## Boundaries & Constraints

**Always:**
- Retrieval only. Return raw Voyager JSON exactly as received. Any reshaping toward `response-schema.md` belongs to story 6.
- The exact request shape — header set, the `csrf-token`/`JSESSIONID` pairing, cookie form, endpoint list — is documented in code comments, because story 9's approach section is written from them.
- Every outbound call goes through one function, so rate-limit and challenge detection has a single choke point and the live-call count per fetch is knowable.
- The cookie value never appears in logs, traces, exception messages, test output, or an error body.
- Upstream failure is classified into the typed codes in `response-schema.md`, never leaked as a raw exception.

**Ask First:**
- Any new runtime dependency.
- Fetching any profile other than the developer's own during development, since each fetch spends real quota against a real account.
- Any change that increases the number of live calls per profile.

**Never:**
- No HTML parsing and no logged-out public-page scraping. Both are ruled out by SPEC.
- **No real captured profile payload committed.** The repository is public and a live payload is a third party's personal data. Fixtures are redacted or synthetic.
- No schema mapping, no caching, no session storage (stories 5–7).
- No automated re-login, cookie refresh, or challenge solving. Expiry is surfaced, never repaired.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Behavior | Error Handling |
|---|---|---|---|
| Known profile | Valid cookie, real public id | Raw JSON for the core entity plus all five sections | N/A |
| Empty section | A section with zero elements | Preserved as an empty result, distinguishable from a failed fetch | Never conflated with an error |
| Malformed URL | Not a `/in/{public-id}` URL | `INVALID_URL` / 400 | Rejected before any network call |
| Expired cookie | Cookie LinkedIn no longer accepts | `SESSION_EXPIRED` / 428 | Distinguished from a missing cookie |
| Unknown profile | Well-formed id that does not exist | `PROFILE_NOT_FOUND` / 404 | Not conflated with an auth failure |
| Throttled | Upstream 429 | `RATE_LIMITED` / 429, `retryable: true` | `Retry-After` propagated when upstream sends one |
| Challenge | HTML authwall or challenge page instead of JSON | `UPSTREAM_CHALLENGE` / 502 | Detected by content type, not by status alone |
| Endpoint withdrawn | A resource starts returning 410 | `UPSTREAM_ERROR` / 502, surfaced loudly | Never silently treated as an empty profile |

</frozen-after-approval>

## Code Map

**The endpoint map below was verified live against LinkedIn on 2026-08-27.** It is fact, not inference — implement against it and do not substitute remembered endpoints.

Authentication that works: cookie header `li_at={cookie}; JSESSIONID="{J}"` with header `csrf-token: {J}`, where `J` may be any value (`ajax:0000000000000000000` verified). Plus `accept: application/vnd.linkedin.normalized+json+2.1`, `x-restli-protocol-version: 2.0.0`, and a browser user-agent.

| Purpose | Path under `https://www.linkedin.com/voyager/api/` | Verified |
|---|---|---|
| Session check | `me` | 200, yields `publicIdentifier` |
| Core profile | `identity/dash/profiles?q=memberIdentity&memberIdentity={public_id}` | 200, one `Profile` in `included[]` |
| Experience | `identity/dash/profilePositions?q=viewee&profileUrn={urlencoded entityUrn}` | 200, `Position[]` |
| Education | `identity/dash/profileEducations?q=viewee&profileUrn={…}` | 200, `Education[]` |
| Skills | `identity/dash/profileSkills?q=viewee&profileUrn={…}` | 200, `Skill[]` |
| Certifications | `identity/dash/profileCertifications?q=viewee&profileUrn={…}` | 200, `Certification[]` |
| Languages | `identity/dash/profileLanguages?q=viewee&profileUrn={…}` | 200, `Language[]` |

Measured entity shapes (populated fields on the dev profile, 2026-08-27):

| Entity | Fields that matter for story 6 |
|---|---|
| `Profile` | `firstName`, `lastName`, `headline`, `summary`, `publicIdentifier`, `profilePicture`, `backgroundPicture`, `geoLocation.geoUrn`, `location.countryCode`, `experienceCardUrn`, `educationCardUrn` |
| `Position` | `title`, `companyName`, `companyUrn`, `dateRange{start,end:{month,year}}`, `description`, `employmentTypeUrn`, `locationName`, `geoLocationName` |
| `Education` | `schoolName`, `schoolUrn`, `degreeName`, `fieldOfStudy`, `grade`, `dateRange{start,end:{year}}` |
| `Certification` | `name`, `authority`, `url`, `licenseNumber`, `dateRange{start:{month,year}}` — **start only, no end** |
| `Skill` | `name` |
| `Language` | `name`, `proficiency` |

Date precision falls out of the source exactly as `response-schema.md` requires: `Position` carries month+year, `Education` year only. Never widen either.

**Dead — do not use:** `identity/profiles/{id}/profileView` → 410; `identity/profiles/{id}` → 410; `graphql` without a `queryId` → 403.

**Real payloads already captured**, deliberately outside the repository, at `/Users/shreyasmathur/.claude/jobs/c5ebd238/tmp/voyager-shapes/` — `core.json` plus one file per section. Derive redacted fixtures from these rather than spending further live calls. They contain a real person's data: read them, never copy one into `tests/` unredacted.

- `../../../../app/config.py` — `Settings`; add the developer cookie as an **optional** field. Making it required would break deploys, since the real cookie arrives per-user in story 5.
- `../../../../app/errors.py` — story 3's typed envelope. Extend it here; do not fork a second error shape.
- `../../../../app/auth.py` — the JWKS client shows the house pattern for an outbound HTTP call with bounded retry.
- `../../../../tests/test_auth.py` — the fixture and signing patterns to follow.

## Tasks & Acceptance

**Execution:**
- [ ] `app/config.py` — optional developer cookie setting, excluded from any repr or log output
- [ ] `.env.example` — document it as optional and developer-only
- [ ] `app/linkedin/client.py` — the client: URL parsing, the single request choke point, URN resolution, the six-call fan-out
- [ ] `app/errors.py` — add the upstream codes from the matrix to story 3's existing table
- [ ] `tests/test_linkedin_client.py` — every matrix row against synthetic fixtures, no network
- [ ] `tests/fixtures/` — redacted or synthetic payloads mirroring the real entity shapes; assert no fixture contains a real cookie or a third party's data
- [ ] one live check — the developer's own profile only, asserting all six resources return, kept out of the default test run so CI and graders never hit LinkedIn

**Acceptance Criteria:**
- Given a valid cookie and a real profile URL, when the client runs, then all six resources return and the raw payloads are available unmodified.
- Given any failure mode in the matrix, when it occurs, then a typed error is raised carrying the mapped status, and no raw exception escapes.
- Given any code path, when logs and error bodies are inspected, then the cookie value appears in none of them.
- Given the repository, when fixtures are inspected, then none contains real personal data or a real cookie.

## Spec Change Log

## Design Notes

**The normalized envelope.** Responses are `{data, included}`. `data` holds references (`*elements`, `*miniProfile`); the entities live in `included[]` and are joined by `entityUrn`. Consumers must resolve across that array rather than reading nested objects — this is the single biggest departure from how most Voyager examples are written.

**Six calls per profile, not one.** The core `Profile` carries only `experienceCardUrn` and `educationCardUrn` pointers, so sections require their own requests. That multiplies rate-limit exposure per API call sixfold and is the strongest argument for story 7's stale-serve. Fan the five section calls out concurrently and fail the whole fetch only when the core profile fails — a section that errors should degrade, not abort, since story 6 must report it in `partial[]`.

**Dates arrive at the right precision.** `dateRange` is `{start:{month,year}, end:{month,year}}`, so `YYYY-MM` for experience and `YYYY` for education fall out of the source. Never widen these into timestamps.

**`location` is thin.** The core entity gives `location.countryCode` and a `geoLocation.geoUrn`, not a human-readable region. `response-schema.md` wants `{country, region}`; resolving the geo URN is story 6's problem, but note it now rather than discovering it during mapping.

**An empty section is not proof of absence.** `profileLanguages` returned **0 elements** on one call and **3 elements** on an identical call minutes later, with a 200 and no error both times. So a zero-length section cannot be mapped to `[]` and called "this profile has none" — that is the `partial[]` case in `response-schema.md`, and getting it wrong publishes a confident falsehood about a real person. Story 6 owns the mapping, but the client must preserve enough signal to tell the two apart: record per-section whether the call succeeded, distinctly from how many elements it returned.

## Verification

**Commands:**
- `docker build --target test -t lps-test . && docker run --rm lps-test` — expected: all tests pass, no network access
- `docker run --rm lps-test python -m pytest -q -k linkedin` — expected: every matrix row covered and passing
- `grep -rIn "li_at" app/ | grep -v "^app/linkedin/client.py"` — expected: no incidental handling of the cookie outside the client
- live check, developer profile only — expected: six resources return 200; run at most once per change
- `docker run --rm -v "$PWD":/repo -w /repo zricethezav/gitleaks:latest git --no-banner` — expected: no leaks
