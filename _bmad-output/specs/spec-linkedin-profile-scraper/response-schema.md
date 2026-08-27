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
  "profile": { }
}
```

- `stale` — `false` when `profile` came from a live retrieval during this request, `true` when the live call failed and a cached record was served instead.
- `fetched_at` — when the returned `profile` was actually retrieved from LinkedIn, not when the request was served. On a stale response this is the older timestamp, and it is the value that makes staleness actionable.

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

### Entry shapes

```
experience[]     title, company, company_url, employment_type, location,
                 start (YYYY-MM), end (YYYY-MM | null for current), description
education[]      school, school_url, degree, field_of_study,
                 start (YYYY), end (YYYY | null)
certifications[] name, issuer, issued (YYYY-MM | null), credential_url
languages[]      name, proficiency
```

Dates are strings in the stated granularity, not full timestamps — LinkedIn does not expose day precision, and inventing it would misrepresent the source.

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

Stale-serve takes precedence over the retryable errors: when a cached record exists, `RATE_LIMITED`, `UPSTREAM_CHALLENGE`, and `UPSTREAM_ERROR` become a 200 with `"stale": true`. The error is only returned when there is nothing cached to fall back to.

Stale-serve is **unbounded**. Cached records have no TTL and are never evicted, so a record of any age is served in preference to a retryable error. `fetched_at` is the caller's only staleness signal, which makes it load-bearing rather than informational.
