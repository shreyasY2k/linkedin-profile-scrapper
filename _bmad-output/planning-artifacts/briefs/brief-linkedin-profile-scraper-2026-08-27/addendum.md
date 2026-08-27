---
title: "Addendum: LinkedIn Profile API"
status: draft
created: 2026-08-27
updated: 2026-08-27
---

# Addendum — LinkedIn Profile API

Depth captured during the brief conversation that belongs to downstream documents (PRD, architecture) rather than the brief itself.

## Options Considered and Rejected

### Credential model

| Option | Why not |
|---|---|
| Single owner-held LinkedIn account in the backend | The assignment's suggested shape and the simplest to build. Rejected: concentrates all rate-limit and challenge exposure on one account, so one lockout takes the whole service down during the evaluation window. |
| Pool of rotated accounts | Best throughput. Rejected: acquiring and warming multiple accounts is not achievable in two days, and cold accounts trip security checks quickly. |
| **Per-user BYO session (chosen)** | Distributes rate-limit exposure across callers, and each request is made under the requester's own session. Cost: encrypted per-user secret storage, a cookie-upload flow, and a revocation story. |

### Fetch strategy

| Option | Why not |
|---|---|
| Public HTML, logged out | No credential risk at all. Rejected: LinkedIn serves authwalls to most datacenter IP ranges and truncates the visible data, which would fail the field-coverage requirement outright. |
| Headless browser as the primary path | More resilient to API shape changes. Rejected as primary: heavy RAM, slow per request, and `arm64` Chromium support needs verification. |
| **Voyager-first, browser fallback (chosen, fallback demoted)** | Voyager returns structured JSON directly and is the literal task. The browser fallback was demoted from Must to Could once the RAM arithmetic and `arm64` uncertainty were laid out. |

### Evaluator access

| Option | Why not |
|---|---|
| Fully open, no auth | Most literal reading of "deploy publicly". Rejected: any traffic that finds the URL burns real LinkedIn quota. |
| Google SSO browser flow only | Not scriptable via `curl`. Rejected as the sole lane — an evaluator who hits an un-scriptable redirect may record the endpoint as unreachable. |
| **Keycloak service account, `client_credentials` (chosen)** | Two `curl` commands, no browser, standard OAuth2. |

### Hosting

Non-Oracle hosting (Fly.io, Koyeb, Render) was raised on the grounds that the assignment requires only public HTTPS and never names a provider, which would move the scarce ARM capacity off the critical path. **The user reviewed this and reaffirmed Oracle-only.** Recorded as a deliberate accepted risk, not an oversight.

## Resource Arithmetic

Basis for the conclusion that the Always Free AMD shape (`VM.Standard.E2.1.Micro`, 1 GB) cannot host this stack:

| Component | Realistic RSS |
|---|---|
| Keycloak (JVM, containerized) | ~600 MB – 1 GB |
| Postgres (Keycloak store) | ~200 MB |
| Playwright Chromium under load | ~400 – 700 MB |
| FastAPI + uvicorn | ~150 MB |
| **Total** | **~2 GB+** |

ARM Ampere A1 (Always Free, up to 4 OCPU / 24 GB) is therefore required.

**Status as of 2026-08-27: resolved.** The ARM instance is provisioned and running. The tactics below are retained for the record and would apply only if the instance had to be rebuilt.

### Capacity tactics

- Request **1 OCPU / 6 GB** rather than the full 4 / 24. Smaller allocations clear capacity considerably more often and 6 GB is sufficient here.
- Drive `oci compute instance launch` in a scripted retry loop rather than the console, rotating through every availability domain in the home region.
- Upgrading to Pay-As-You-Go materially improves capacity priority while Always Free resources remain free. Attaches a payment method; staying inside free limits becomes the operator's responsibility.

## Technical Constraints for Architecture

- **Runtime:** Python 3.11+, FastAPI, uvicorn. Pydantic models define the response schema and generate the OpenAPI document, which serves as the README's API documentation.
- **Config:** twelve-factor throughout. `.env.example` committed with dummy values; real `.env` gitignored. Identical image local and deployed; only the env file differs.
- **Local parity:** `docker-compose.yml` brings up FastAPI, Keycloak, and Postgres in one command, and doubles as the README setup instructions.
- **Secret handling:** `li_at` cookies encrypted at rest, key supplied via env and never committed. Verify git history is clean before submission, not just the working tree.
- **Cache:** keyed by profile identifier. Stores full response plus fetch timestamp. On live-fetch failure, return cached payload with `"stale": true` and the timestamp. Storage choice (Postgres table vs. Redis) deferred to architecture — Postgres is already present for Keycloak.
- **Identity:** Keycloak realm with two clients — a public client for Google SSO users, and a confidential service-account client for the evaluator. API validates JWTs against the realm's JWKS.
- **Topology:** Cloudflare DNS → OCI Load Balancer → host nginx → Docker Compose stack. Application containers never bind to public ports. nginx and Docker are host-installed; the whole application stack is one `docker compose`, which makes nginx the sole deployment-time wiring step.
- **TLS:** termination point still open between Cloudflare, the load balancer, and nginx. Recommended is Cloudflare proxied with a Cloudflare Origin Certificate at nginx — free, 15-year, no renewal to manage on a server that stops being watched after submission.
- **Oracle host firewall:** stock Ubuntu and Oracle Linux images drop 80/443 in host `iptables`/`firewalld` independently of Security List rules. Both must be opened, or the load-balancer health check fails while every cloud-side setting reads as correct.
- **Session lifecycle:** `PUT` overwrites the stored `li_at`; no delete or revoke path in the Must tier.
- **Cache lifecycle:** unbounded. No TTL, no eviction; `fetched_at` is the caller's only staleness signal.

## Field Coverage

Required by the assignment: name, headline, location, about, experience, education, skills, certifications, languages, profile images.

Fields genuinely absent from a source profile should be omitted or explicitly null rather than silently defaulted — the distinction between "not present" and "we failed to retrieve it" matters to any consumer and is worth encoding in the schema.

## Terms of Service Posture

Automated collection of profile data is contrary to LinkedIn's User Agreement irrespective of which account's session is used. The per-user credential model narrows the question — each request is made under the requester's own authenticated session rather than a shared harvesting account — but does not resolve it.

The system is built for assignment evaluation, not production operation. The README should state this plainly. The assignment's explicit request for "known limitations" is read as an invitation to demonstrate that the adversarial and legal environment is understood, and a candid treatment is expected to score better than a minimised one.
