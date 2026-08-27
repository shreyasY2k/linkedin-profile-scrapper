---
title: 'Voyager client returning raw profile JSON'
type: 'feature'
created: '2026-08-27'
status: 'done'
baseline_commit: '7b36c08d9523ce9266434cb105d25450a7d99a7b'
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
- [x] `app/config.py` — optional developer cookie setting, excluded from any repr or log output
- [x] `.env.example` — document it as optional and developer-only
- [x] `app/linkedin/client.py` — the client: URL parsing, the single request choke point, URN resolution, the six-call fan-out
- [x] `app/errors.py` — add the upstream codes from the matrix to story 3's existing table
- [x] `tests/test_linkedin_client.py` — every matrix row against synthetic fixtures, no network
- [x] `tests/fixtures/` — redacted or synthetic payloads mirroring the real entity shapes; assert no fixture contains a real cookie or a third party's data
- [x] one live check — the developer's own profile only, asserting all six resources return, kept out of the default test run so CI and graders never hit LinkedIn

**Acceptance Criteria:**
- Given a valid cookie and a real profile URL, when the client runs, then all six resources return and the raw payloads are available unmodified.
- Given any failure mode in the matrix, when it occurs, then a typed error is raised carrying the mapped status, and no raw exception escapes.
- Given any code path, when logs and error bodies are inspected, then the cookie value appears in none of them.
- Given the repository, when fixtures are inspected, then none contains real personal data or a real cookie.

## Spec Change Log

- **Review pass 1 (20 findings applied). Four changed observable behaviour and are recorded here because downstream stories are written against them.**
  - *An explicit 401/403 now outranks the challenge signals it arrives with.* A dead cookie is usually delivered as a redirect to the login page, so it carries every marker a challenge does. The old ordering made the commonest expiry an `UPSTREAM_CHALLENGE` — `retryable: true` — which story 7 stale-serves unboundedly, so a caller would have been fed ever-older cached data forever and never told to store a new cookie. `SESSION_EXPIRED` existing as a separate code was doing nothing.
  - *`SESSION_EXPIRED`, `RATE_LIMITED` and `UPSTREAM_CHALLENGE` from a section now abort the fetch* instead of degrading into `partial[]` (`SYSTEMIC_CODES`). All three are account-wide by construction, so a 200 carrying whichever sections landed first is not a partial answer but a wrong one — and story 7 would cache it. Per-section failures (404, malformed envelope, unexpected status) still degrade, which is what the Design Notes intend.
  - *The core `Profile` is resolved through `*elements` and cross-checked against the requested public id,* rather than taken as the first `Profile`-shaped thing in `included`. Answering with — and caching, under the requested URL — a different human being is the worst failure available to this system.
  - *A section 200 whose body is not a collection envelope is now `ok=False`.* It previously produced `element_count=0`, which story 6 maps to "the profile has none of these" — stating as fact something that was never read. That is precisely the absent-versus-unreadable error this story exists to prevent.
- **The transport refuses redirects off LinkedIn.** `urllib` follows redirects and forwards a manually-set `Cookie` header to the new host; it strips only content headers. Verified empirically. Without `LinkedInRedirectHandler` the module's central claim — the session cookie is written in one place and goes to one host — was false, and the failure was silent.
- **`LINKEDIN_DEV_COOKIE` is blanked for the `api` container in `docker-compose.yml`.** `env_file: .env` loads the whole file, so a developer who filled it in to run the live check was shipping their live session into the running service's environment. The same audit found the superseded `LINKEDIN_LI_AT` still being injected; it has been removed from `.env`.

- **`NO_SESSION` added to the error table alongside the six upstream codes.** The task said "add the upstream codes from the matrix"; the matrix row for the expired cookie says it must be "distinguished from a missing cookie", and there is no way to distinguish the two without a second code. `app/errors.py` therefore now carries the complete `response-schema.md` table rather than seven of its eight rows. Story 5 wires `NO_SESSION` to the caller-has-stored-nothing path; the client raises it when constructed with a blank cookie.
- **Section requests carry `&count=100`, which the Code Map's endpoint table does not show.** Verified live on 2026-08-27, and a correctness fix rather than an addition: the default page size is 20, the developer's own profile has 33 skills, and the table's request as written returned 20 of them with a 200 and no error — a truncated list indistinguishable from a complete one. `count=100` returns all 33 in the same single call, so the per-profile call count is unchanged. Where a section still exceeds the page, `SectionFetch.reported_total` and `RawProfile.truncated_sections` make the shortfall visible instead of silent.
- **`RawProfile.call_count` is per-fetch, not per-client.** Discovered by the live check, which validates the session and then fetches on one client and so read 7 for a six-call fetch. Recorded here because story 5's flow is exactly that shape.

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
