---
title: 'Profile extraction and response schema mapping'
type: 'feature'
created: '2026-08-27'
status: 'done'
baseline_commit: '84b7dc9b8a7dd58d8a9650994910559b2d822c9b'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/specs/spec-linkedin-profile-scraper/SPEC.md'
  - '{project-root}/_bmad-output/specs/spec-linkedin-profile-scraper/response-schema.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Every piece is built and none of them meet. The client returns raw Voyager JSON, the vault holds a session, and nothing maps one onto the other or exposes it. `GET /api/v1/profile` — the endpoint the whole submission is graded on — does not exist, so CAP-1 is entirely unbuilt.

**Approach:** Mount `GET /api/v1/profile`, load the caller's session from the vault, fetch through the Voyager client, and map the raw entities onto every field of `response-schema.md`, keeping absent and unreadable distinguishable throughout.

## Boundaries & Constraints

**Always:**
- **Absent and unreadable are different claims.** A field genuinely not on the profile is `null` for scalars and `[]` for arrays. A field that could not be retrieved is **omitted entirely** and its name added to the top-level `partial[]`. A response is never silently defaulted.
- Dates keep exactly the granularity LinkedIn exposes: `YYYY-MM` for experience and certifications, `YYYY` for education. Never widened into a timestamp, never invented.
- Every field named in `response-schema.md` is populated when the source profile has it. The envelope shape is fixed by that file, not by what is convenient to produce.
- The caller's own stored session is used. A caller with no session gets `NO_SESSION`, never someone else's session and never an anonymous fetch.
- Mapping is pure and total: it never raises on unexpected input. Anything it cannot read becomes `partial[]`, not an exception.

**Ask First:**
- Any change to the field names, nesting, or types in `response-schema.md`.
- Any new runtime dependency.
- Fetching any profile other than the developer's own during development.
- Anything that would increase the six-call budget per profile.

**Never:**
- No caching or stale-serve (story 7 owns them; `stale` is `false` here and `fetched_at` is the live fetch time).
- No HTML parsing, no logged-out fallback.
- No inventing data to fill a field — an absent middle name is not an empty string, and an unreadable section is not an empty array.
- No cookie value in any response, log, or error body.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Behavior | Error Handling |
|---|---|---|---|
| Complete profile | Valid token, stored session, real URL | 200 with every populated field mapped, `stale: false`, `partial` empty | N/A |
| Sparse profile | Sections the member genuinely lacks | `null` scalars and `[]` arrays — **not** `partial[]` | N/A |
| Section unreadable | A section fetch failed | Its key omitted and named in `partial[]`; the rest still returned | 200, degraded honestly |
| Truncated section | More elements exist than were returned | Treated as unreadable, named in `partial[]` | Never a silently short list |
| No session | Caller has stored none | `NO_SESSION` / 428 | Before any upstream call |
| Dead session | Stored cookie refused by LinkedIn | `SESSION_EXPIRED` / 428 | Whole fetch aborts; not a partial 200 |
| Unknown profile | Well-formed URL, no such member | `PROFILE_NOT_FOUND` / 404 | Not confused with an auth failure |
| Bad URL | Not a `/in/{public-id}` URL | `INVALID_URL` / 400 | Rejected before any upstream call |
| Throttled | LinkedIn returns 429 | `RATE_LIMITED` / 429, `Retry-After` when known | Whole fetch aborts |
| No token | No bearer | 401 `UNAUTHENTICATED` | Inherited from the router |
| Unexpected shape | An entity missing fields the mapper expects | That field absent or `partial[]`; never a 500 | Mapping never raises |

</frozen-after-approval>

## Code Map

- `../../../../app/linkedin/client.py` — `fetch_profile` returns `RawProfile`: `core`, five `SectionFetch`es, `fetched_at`, `call_count`, `truncated_sections`. `SectionFetch` records `ok`, `element_count` and `reported_total` as **three separate facts** — that separation is what makes absent-versus-unreadable decidable. `resolve_elements` performs the `*elements` → `included` join.
- `../../../../app/vault.py` — `unlock(subject)` returns the caller's `LinkedInSession`; `record_use` writes the outcome. This story is `record_use`'s main production caller.
- `../../../../app/api/v1/session.py` — the pattern for a guarded route: mounted on the v1 router, no security dependency of its own, `error_responses(...)` for the OpenAPI failure list.
- `../../../../app/errors.py` — `ERROR_SPECS` already carries every code this story needs. Extend nothing.
- `../../../../tests/fixtures/` — synthetic Voyager payloads matching the measured entity shapes. A **sparse** fixture is required by the story and may need adding.
- `../../../../_bmad-output/specs/spec-linkedin-profile-scraper/response-schema.md` — the normative contract. Field names and types come from there verbatim.

**Measured source shapes** (verified live 2026-08-27, in the story-4 spec):

| Target | Source |
|---|---|
| `name` | `Profile.firstName` / `lastName` |
| `headline`, `about` | `Profile.headline`, `Profile.summary` |
| `location` | `Profile.location.countryCode`; `geoLocation.geoUrn` carries no readable region |
| `images` | `Profile.profilePicture` / `backgroundPicture` — a `vectorImage` with a `rootUrl` plus artifact path segments that must be **joined into absolute URLs** |
| `experience[]` | `Position.title`, `companyName`, `companyUrn`, `dateRange{start,end:{month,year}}`, `description`, `employmentTypeUrn`, `locationName` |
| `education[]` | `Education.schoolName`, `schoolUrn`, `degreeName`, `fieldOfStudy`, `dateRange{start,end:{year}}` |
| `certifications[]` | `Certification.name`, `authority`, `url`, `dateRange{start:{month,year}}` — **start only, no end** |
| `skills[]`, `languages[]` | `Skill.name`; `Language.name`, `proficiency` |

## Tasks & Acceptance

**Execution:**
- [x] `app/mapping/profile.py` — the pure mapper: raw entities in, contract-shaped dict plus the `partial[]` list out; never raises
- [x] `app/mapping/dates.py` — `dateRange` to `YYYY-MM` / `YYYY` at the source's own granularity
- [x] `app/mapping/images.py` — vector image to absolute URL
- [x] `app/api/v1/profile.py` — `GET /api/v1/profile`, mounted on the guarded router: session lookup, fetch, map, envelope
- [x] `app/api/v1/__init__.py` — mount it
- [x] `tests/fixtures/` — add a **sparse** profile fixture: missing sections, a current role with no end date, a certification with no expiry
- [x] `tests/test_mapping.py` — every matrix row, and every absent-versus-unreadable pairing explicitly
- [x] `tests/test_profile_api.py` — the endpoint end to end against a real token and a stubbed client
- [x] `README.md` — the second graded `curl`, verified verbatim

**Acceptance Criteria:**
- Given a profile with a genuinely empty section, when it is mapped, then that field is `[]` or `null` and its name does **not** appear in `partial[]`.
- Given a section whose fetch failed, when it is mapped, then its key is absent from `profile` and its name **does** appear in `partial[]`.
- Given any experience entry, when it is serialised, then its dates match `^\d{4}(-\d{2})?$` or are `null`, and no date carries day or time precision. (Amended during review, author-approved: `YYYY` is accepted because LinkedIn permits a year-only position date, and rendering that as `null` republished a finished job as a current role — `end: null` means "still held". `response-schema.md` was amended to match.)
- Given any malformed or unexpected raw entity, when it is mapped, then a response is still produced and no exception escapes.
- Given a caller with no stored session, when they request a profile, then `NO_SESSION` is returned before any upstream call is made.

## Spec Change Log

## Design Notes

**The empty-section rule, and why it is not obvious.** An empty section is ambiguous: `profileLanguages` was measured returning **0 elements on one call and 3 on an identical call minutes later**, HTTP 200 both times. But routing every empty section to `partial[]` would put most real profiles' certifications and languages there permanently, which reads as broken. Use the third fact the client records:

- `ok` false → **unreadable** → `partial[]`
- `ok` true, `reported_total` absent or `0`, zero elements → **genuinely empty** → `[]`
- `ok` true, `reported_total` greater than the elements returned → **truncated, therefore unreadable** → `partial[]`

`paging.total` is what disambiguates, which is why the client keeps it separate from the count.

**`partial[]` names the contract field, not the Voyager resource.** A caller reads `response-schema.md`, not our endpoint map, so the array says `certifications`, never `profileCertifications`.

**Region is not available.** The core entity gives `location.countryCode` and a `geoLocation.geoUrn`; no readable region name comes back. `response-schema.md` says `country`, `region` "where separable", so `region` is `null` — genuinely absent, not unreadable. Do not spend a seventh call resolving the geo URN.

**Current roles.** A present role has `dateRange.end` absent; the contract wants `end: null`. That is absent-not-unreadable, and it must not reach `partial[]`.

**Author decisions, 2026-08-27 — three Ask First items resolved.**

1. **`employment_type` is omitted and named in `partial[]`** when it resolves only to a raw URN (`urn:li:fsd_employmentType:12`, which is every position on the live profile). A raw URN is an unreadable value dressed as a readable one, so reporting it as the value violates this story's own central rule. Do not guess enum labels for someone's real job.

2. **`location.region` is now resolvable — in the SAME call, not a seventh one.** Verified live: adding `&decorationId=com.linkedin.voyager.dash.deco.identity.profile.FullProfile-77` to the existing core request returns `Geo` entities in `included[]`, joined from `Profile.geoLocation["*geo"]`, carrying `defaultLocalizedName` (measured: `"Bengaluru, Karnataka, India"`, plus a country-level `"India"`). The call budget is unchanged. `country` still comes from `location.countryCode`; `region` comes from the joined Geo name and may have a redundant trailing country trimmed — never invented. **The decoration id is version-pinned and brittle**: if the decorated request fails, fall back to the undecorated one and treat `region` as absent rather than failing the fetch.

3. **`response-schema.md` is amended so experience dates accept `YYYY-MM` **or** `YYYY`.** LinkedIn permits a position dated to the year alone, and the previous contract forced that to `null` — discarding data the source had. This matches the SPEC's principle that dates keep the granularity LinkedIn actually exposes. Update the contract file, the mapper, and the acceptance criterion together.

## Verification

**Commands:**
- `docker build --target test -t lps-test . && docker run --rm --network none lps-test` — expected: all pass, no network
- `docker compose down -v && docker compose up -d --build --wait` — expected: exit 0
- store a session, then `GET /api/v1/profile?url=...` with a minted token — expected: 200, `stale: false`, populated fields
- the same against the **sparse** fixture — expected: `[]`/`null` for missing sections, `partial` empty
- `curl -fsS http://127.0.0.1:8000/openapi.json` — expected: `/api/v1/profile` present with its typed failure list
- `docker compose logs api | grep -F "$COOKIE"` — expected: no match
