# Scope Tiers

Delivery is bounded by a 2026-08-29 submission deadline. `SPEC.md` capabilities carry the Must tier only. This file holds the tiers below it and the order in which work is abandoned under pressure — so that a cut is a decision already made, not one taken at 2am.

## Must — the graded surface

CAP-1 through CAP-8 in `SPEC.md`. Not cuttable. If any of these is incomplete, the submission is incomplete regardless of what else was built.

## Should — build if day two has room

| Item | Value | Why it is not Must |
|---|---|---|
| Google SSO federation through Keycloak | Makes the auth story real rather than demonstrative | The evaluator lane is the service account; Google SSO is not on the path they will actually use |
| Rate limiting | Protects the shared deployment from incidental abuse | No traffic is expected during evaluation beyond the evaluator's own |

## Could — stretch only

| Item | Value | Why it is not Should |
|---|---|---|
| Playwright headless-browser fallback when Voyager fails | A genuine second retrieval path; strongest possible resilience story | Highest cost in the plan. Chromium adds roughly 400–700 MB to a footprint already near the instance ceiling, and `arm64` Chromium support must be verified before it can be relied on at all. Stale-serve (CAP-5) already covers the failure mode it addresses, at a fraction of the cost. |

Originally planned as the primary fallback and deliberately demoted once the resource arithmetic in the adopted addendum was worked through.

## Won't — out of scope

Enumerated as Non-goals in `SPEC.md`.

## Cut order

Under time pressure, abandon in this order and stop as soon as the Must tier is safe:

1. Playwright fallback
2. Google SSO federation
3. Rate limiting

The Must tier is never cut. If the Must tier is at risk, the correct response is to reduce polish inside it — fewer cached profiles, thinner tests — not to drop a capability.

## Verify before committing to the Could tier

`arm64` Chromium must be confirmed to launch on the target instance before any Playwright work begins. Discovering it does not on day two costs the fallback and the hours spent on it.

## Delivery sequence

Two days. Infrastructure is the critical path and does not get faster with effort, so it starts first and runs in parallel with everything else.

**Day 1** — Repository skeleton, env configuration, `docker-compose.yml`. Walking-skeleton deploy through the load balancer and nginx to prove the pipeline. Voyager client authenticating and returning raw JSON for one known profile. Keycloak realm and service-account client.

The instance is already provisioned and running, so the capacity risk that originally shaped this plan no longer applies. What remains on the deployment path is nginx configuration and the load-balancer wiring.

**Day 2** — Full field extraction against `response-schema.md`. Encrypted session vault. Cache and stale-serve. Deploy behind Cloudflare, wire DNS. README written last, while the failure modes are still fresh. Could tier only if the day has room.

The ordering inside day 2 is deliberate: extraction, vault, and stale-serve are all Must, and the README depends on knowing how the earlier three actually behaved.
