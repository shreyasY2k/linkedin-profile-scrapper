# Response Schema

The wire contract for `SPEC-linkedin-profile-scraper`. Field names are normative; types are normative; nesting may be adjusted during implementation only if this file is updated to match.

## Endpoint

`GET /api/v1/profile?url={linkedin_profile_url}`

Requires `Authorization: Bearer {keycloak_jwt}`. Requires that the calling subject has a stored LinkedIn session (see Session endpoints).

## Success envelope

```json
{
  "url": "https://www.linkedin.com/in/example",
  "public_id": "example",
  "stale": false,
  "fetched_at": "2026-08-27T09:00:00Z",
  "partial": [],
  "profile": { }
}
```

- `stale` — `false` when `profile` came from a live retrieval during this request, `true` when the live call failed and a cached record was served instead.
- `fetched_at` — when the returned `profile` was actually retrieved from LinkedIn, not when the request was served. On a stale response this is the older timestamp, and it is the value that makes staleness actionable.
- `partial` — the names of fields that could not be retrieved in this run. **Always present**, and `[]` on a complete answer. It is never omitted and never `null`: a consumer must be able to read it unconditionally, because "there is nothing in it" and "this response predates the field" have to be distinguishable. See *Absent versus unreadable* below.

`url` is the canonical `https://www.linkedin.com/in/{public_id}` form of the profile that was fetched, not the string the caller supplied — a caller's URL may carry a locale prefix, a `/details/...` sub-path, tracking parameters or arbitrary case, and `url` and `public_id` are guaranteed to agree with each other.

## Profile object

Every field below is required by the assignment. Absent-versus-null semantics are load-bearing: a consumer must be able to distinguish "this profile has no certifications" from "we could not read the certifications".

| Field | Type | Notes |
|---|---|---|
| `name` | object | `first`, `last`, `full` |
| `headline` | string \| null | |
| `location` | object \| null | `country`, `region` where separable |
| `about` | string \| null | Full summary text, newlines preserved |
| `experience` | array | Ordered most-recent first |
| `education` | array | Ordered most-recent first |
| `skills` | array of string | |
| `certifications` | array | |
| `languages` | array | `name`, `proficiency` where present |
| `images` | object | `profile`, `background` — absolute URLs |

### Absent versus unreadable

- Field genuinely not present on the source profile → `null` for scalars, `[]` for arrays.
- Field present but not retrievable in this run → omit the key entirely and add its name to a top-level `partial[]` array on the envelope.

A response is never silently defaulted. `partial` being non-empty on an otherwise-200 response is the signal that extraction degraded without failing.

There is no third state. In particular an unreadable value is never published in a readable value's place: an internal URN standing in for a human-readable label is an unreadable value, and it is omitted like any other.

**Any field in the profile object may appear in `partial`**, not only the array-valued ones. Extraction is contained per field, so a scalar that could not be read is omitted and named in exactly the same way.

**Dotted sub-field paths.** An entry in `partial` may name a sub-field of an array, e.g. `experience.employment_type`. It means: that sub-field was unreadable for at least one entry in that array and is **omitted from the entries where it could not be read**. Entries where it was readable still carry it, and an entry whose source genuinely states no value keeps `null` — absence, which never appears in `partial`.

### Entry shapes

```
experience[]     title, company, company_url, employment_type, location,
                 start (YYYY-MM | YYYY), end (YYYY-MM | YYYY | null for current),
                 description
education[]      school, school_url, degree, field_of_study,
                 start (YYYY), end (YYYY | null)
certifications[] name, issuer, issued (YYYY-MM | null), credential_url
languages[]      name, proficiency
```

Dates are strings in the stated granularity, not full timestamps — LinkedIn does not expose day precision, and inventing it would misrepresent the source.

**Experience dates accept two precisions, and the reason is load-bearing.** LinkedIn lets a member date a position with a year and no month. Rendering that as `null` was tried and is wrong: `end: null` is defined here as *the person still holds this role*, so a position ending in 2019 with no month would be republished as a current job — an invented fact about someone's employment, produced by a rule written to avoid inventing facts. Rendering it as `2019-01` is equally an invention. So `start` and `end` carry whichever precision the source actually stated, and a consumer distinguishes them by length: `^\d{4}(-\d{2})?$`.

`certifications[].issued` stays strictly `YYYY-MM`. `issued` has no null-means-current meaning, so a year-only value there is a plain absence rather than a false claim.

`employment_type` is LinkedIn's readable label, `null` when the position states none, or **absent** when LinkedIn referenced a type without delivering a readable name for it — in which case `partial` carries `experience.employment_type`.

`location.region` is the member's place as LinkedIn names it, with a redundant trailing country name trimmed since `country` has its own field. It is `null` when no readable place name is available, which is an absence rather than a failure.

## Session endpoints

```
PUT  /api/v1/session     body { "li_at": "..." }   store or replace the caller's LinkedIn session
GET  /api/v1/session                                presence and validity only, never the cookie value
```

`GET` returns whether a session is stored and whether its last use succeeded. It never returns the stored value, in any form, under any flag.

`PUT` replaces any existing session outright. There is no delete or revoke endpoint — overwrite is the entire lifecycle, by decision.

## Error envelope

```json
{
  "error": {
    "code": "SESSION_EXPIRED",
    "message": "Stored LinkedIn session is no longer valid.",
    "retryable": false
  }
}
```

| Code | Status | Retryable | Meaning |
|---|---|---|---|
| `INVALID_URL` | 400 | no | Not a parseable LinkedIn profile URL |
| `UNAUTHENTICATED` | 401 | no | Missing, expired, or foreign-realm token |
| `NO_SESSION` | 428 | no | Caller has not stored a LinkedIn session |
| `SESSION_EXPIRED` | 428 | no | Stored `li_at` rejected by LinkedIn; caller must supply a new one |
| `PROFILE_NOT_FOUND` | 404 | no | Profile does not exist or is not visible to this session |
| `RATE_LIMITED` | 429 | yes | LinkedIn throttled the request; `Retry-After` set when known |
| `UPSTREAM_CHALLENGE` | 502 | yes | LinkedIn served a challenge or authwall |
| `UPSTREAM_ERROR` | 502 | yes | Any other upstream failure |

`retryable` exists so a client can decide without parsing prose. `428` is chosen over `403` for the session cases because the condition is precisely "a precondition is missing and you can fix it", which is what the caller needs to know.

**The `Retryable` column above is the default for each code, not a guarantee about every response carrying it: `retryable` is a property of the response and the value on the wire is authoritative.** A client branches on the field, never on the code — one condition (an upstream answer naming a different member than the request asked for) returns `UPSTREAM_ERROR` with `retryable: false`, because it is permanent and repeating the request cannot change it.

Stale-serve takes precedence over the retryable errors: when a cached record exists, `RATE_LIMITED`, `UPSTREAM_CHALLENGE`, and `UPSTREAM_ERROR` become a 200 with `"stale": true`. The error is only returned when there is nothing cached to fall back to.

Stale-serve is **unbounded**. Cached records have no TTL and are never evicted, so a record of any age is served in preference to a retryable error. `fetched_at` is the caller's only staleness signal, which makes it load-bearing rather than informational.
