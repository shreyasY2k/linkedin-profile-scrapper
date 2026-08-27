---
title: "Product Brief: LinkedIn Profile API"
status: draft
created: 2026-08-27
updated: 2026-08-27
---

# Product Brief: LinkedIn Profile API

## Executive Summary

A hosted HTTPS API that accepts a LinkedIn profile URL and returns the profile as structured JSON — name, headline, location, about, experience, education, skills, certifications, languages, and images. Data is retrieved by calling LinkedIn's own internal Voyager endpoints with an authenticated session, rather than by parsing rendered HTML.

The design departs from the assignment's suggested shape in one deliberate way. Rather than running every request through a single owner-held LinkedIn account, each caller authenticates through Keycloak (Google SSO) and supplies their **own** LinkedIn session, held in an encrypted per-user vault. This removes the single-account rate limit that is the usual failure mode of scrapers like this, and it means every request is made under the session of the person who asked for it.

This is a graded take-home with a two-day clock. It is scoped accordingly: one endpoint, done properly, with an honest account of where it breaks.

## The Problem

LinkedIn publishes no public profile API. The official Marketing and Sign-In APIs return only the authenticated member's own data, and partner access is gated behind an approval process measured in weeks. Anyone needing profile data programmatically — recruiters enriching a pipeline, sales teams qualifying leads, researchers assembling a dataset — is left with three bad options: pay a per-profile fee to an intermediary such as PhantomBuster or Proxycurl, copy fields by hand, or build the integration themselves and absorb the maintenance.

The self-build route is where the real difficulty sits, and it is not the parsing. Profile HTML is rendered client-side and heavily obfuscated. Sessions expire. Datacenter IP ranges draw authwalls and challenge pages. A scraper that works on a laptop on Monday routinely returns empty on a server on Wednesday, and the naive version of this project fails not at the extraction step but days later, in production, silently.

## The Solution

A single well-documented endpoint — `GET /api/v1/profile?url=...` — returning a stable, versioned JSON schema.

Behind it:

- **Voyager-first retrieval.** The backend calls LinkedIn's internal JSON API (`/voyager/api/identity/*`) with a valid session cookie. This returns structured data directly, avoiding HTML parsing entirely, and is the "reverse engineer LinkedIn APIs" the assignment is asking for.
- **Per-user credential vault.** After signing in through Keycloak, each user supplies their own `li_at` session cookie. It is encrypted at rest, keyed to their Keycloak subject, and never leaves the server.
- **Cache with stale-serve.** Every successful fetch is cached. If a later live call fails — expired cookie, rate limit, challenge page — the API returns the last good record flagged `"stale": true` with a timestamp, rather than a 500. Degradation is visible and honest instead of catastrophic.
- **A frictionless evaluator lane.** A Keycloak service account using the OAuth2 `client_credentials` grant means the API can be exercised with two `curl` commands and no browser redirect.
- **Environment parity.** All configuration is supplied through environment variables, with a committed `.env.example` and a `docker-compose.yml` that brings up FastAPI, Keycloak, and Postgres locally in one command. The same image runs unchanged on the server.
- **Deployment topology.** Cloudflare DNS resolves to an OCI Load Balancer, which forwards to the running ARM instance, where host-installed nginx reverse-proxies into the Compose stack. nginx is the only piece configured outside compose, so deployment reduces to one wiring step.

## What Makes This Different

Measured against a naive submission of the same assignment, rather than against commercial products:

- **The credential model is the real design decision.** Per-user sessions distribute rate-limit exposure across callers instead of concentrating it on one account, and they give the system a defensible answer to the question of whose authority each request is made under.
- **It plans for its own failure.** Stale-serve means the endpoint stays useful after LinkedIn starts refusing the live call — which it eventually will.
- **The limitations section is written honestly.** The assignment explicitly asks for known limitations. That is an invitation to demonstrate understanding of the adversarial environment, not a disclaimer to minimise.

There is no technical moat here, and claiming one would be dishonest. Voyager endpoint knowledge is public and decays; anyone can replicate this. The value on offer is judgment: which failure modes were anticipated, and what the system does when they arrive.

## Who This Serves

**The evaluator (primary).** Reads the README, clones the repo, expects a local run to work from the documented steps, then calls the deployed URL — possibly days after submission, with a profile never tested against. Success means: it responded, the JSON was complete and well-shaped, and the README already said whatever went wrong might go wrong.

**The API consumer (secondary, notional).** A developer enriching profile data who wants one predictable endpoint and a schema that does not shift underneath them.

## Success Criteria

Mapped to what is actually being graded:

1. Public HTTPS endpoint on `shreyaskaushik.dpdns.org`, reachable and correct on an unseen profile at an arbitrary later time.
2. Every field in the assignment's list populated when present on the source profile, absent-not-null when genuinely missing.
3. A cold `git clone` plus the documented setup steps yields a working local instance.
4. README covers setup, API documentation, approach, and known limitations — all four, with the limitations section specific rather than generic.
5. No credential, cookie, key, or secret anywhere in git history.
6. Failures return structured, meaningful errors with correct status codes. Nothing 500s naked.

## Scope

**Must**
FastAPI service; Voyager client; extraction of all specified fields; public HTTPS via Cloudflare; Keycloak with `client_credentials` evaluator lane; encrypted per-user cookie vault; cache with stale-serve; twelve-factor env config with local/prod parity; README; public repository; secrets excluded.

**Should**
Google SSO federation through Keycloak; rate limiting.

**Could**
Playwright headless-browser fallback when Voyager fails. *Deliberately demoted from the original plan — see Risks.*

**Won't**
Bulk or batch endpoints; company pages; people search; posts, connections, or activity; any real frontend beyond what Keycloak provides.

## Risks and Known Limitations

| Risk | Standing |
|---|---|
| **Oracle ARM capacity** — the stack needs ~2 GB+ (Keycloak ~1 GB, Postgres ~200 MB, FastAPI ~150 MB, Chromium ~500 MB if built), which the Always Free AMD 1 GB shape cannot host. | **Resolved 2026-08-27.** The ARM Ampere A1 instance is provisioned and running. What remains on the deployment path is load-balancer wiring and nginx configuration, not capacity. |
| **TLS termination undecided** — Cloudflare, the OCI load balancer, and nginx are all candidates, and the choice determines the nginx config and whether certificate renewal is in scope. | Open. Recommended: Cloudflare proxied with a Cloudflare Origin Certificate at nginx — free, 15-year, no renewal. |
| **Host firewall vs. security list** — Oracle's stock images drop 80/443 in host `iptables`/`firewalld` regardless of Security List rules, so health checks fail while the console looks correct. | Known trap; addressed during the walking-skeleton deploy. |
| **Session cookie expiry** — `li_at` cookies expire and are invalidated by password changes and security challenges. | Mitigated by stale-serve; documented as a limitation. Cookie refresh is out of scope. |
| **Rate limiting and challenge pages** — LinkedIn throttles aggressively and serves challenges to datacenter IPs. | Per-user credentials distribute load; stale-serve absorbs the rest. |
| **Voyager schema drift** — internal endpoints are unversioned and change without notice. | Accepted. Noted in README as inherent to the approach. |
| **Playwright on `arm64`** — Chromium under ARM64 Linux needs verifying before the fallback can be relied on. | Reason the fallback sits in Could rather than Must. Verify before building. |
| **Terms of service** — automated profile collection is contrary to LinkedIn's User Agreement regardless of credential model. | Stated plainly in the README. Per-user sessions narrow the question but do not settle it. Built for assignment evaluation, not production operation. |

## Delivery Plan

**Day 1** — Repository skeleton, env config, docker-compose. A walking-skeleton deploy through the load balancer and nginx, to prove the pipeline while there is still time to fix it. Voyager client, authenticated, returning raw JSON for one known profile. Keycloak realm and service-account client.

**Day 2** — Full field extraction and response schema. Cookie vault with encryption. Cache and stale-serve. Deploy behind Cloudflare, wire DNS. README last, while the failure modes are still fresh. Playwright only if the day has room.

**Cut order under pressure:** Playwright first, then Google SSO federation, then rate limiting. The Must list does not get cut — it is what is being graded.

## Vision

Nothing beyond the assignment is planned, and pretending otherwise would pad the document. If the credential-vault pattern proves out, the natural continuation is additional entity types — companies, job posts — behind the same auth and caching layer, at which point the interesting product question stops being extraction and becomes freshness: how to keep a cached corpus current against a source that does not want to be read.
