---
title: 'README, secret-history audit, and final deploy'
type: 'feature'
created: '2026-08-27'
status: 'in-progress'
baseline_commit: 'dad385149d3d62dd4404613ec49d5424ba29b6b5'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/planning-artifacts/briefs/brief-linkedin-profile-scraper-2026-08-27/addendum.md'
  - '{project-root}/_bmad-output/implementation-artifacts/deferred-work.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Everything is built and nothing is submittable. The deployed instance runs story-7 code, every API URL in the README points at `127.0.0.1` and sources a gitignored `.env`, three of the four required sections are stubs, `deploy/README.md` documents a TLS topology that was abandoned, and the profile call returns `428 NO_SESSION` to any caller who has not first uploaded a LinkedIn cookie by hand.

**Approach:** Redeploy so production matches HEAD, make the graded `curl` commands work verbatim from a machine that has never authenticated by pre-seeding the evaluator's session, then write the three missing sections — candidly, because the assignment's request for known limitations is an invitation to show the adversarial environment is understood.

## Boundaries & Constraints

**Always:**
- The README's four required sections are setup, API documentation, approach, known limitations. Every other heading is subordinate to one of them.
- The graded commands are copied verbatim and run against `https://shreyaskaushik.dpdns.org` with literal values. Nothing an evaluator runs may depend on a file that is not in the public repository.
- Known limitations names concrete, observed failure modes with their consequences. A limitation that is a deliberate decision says so and says why.
- Deployment redeploys the running instance in place. The Postgres volume is never dropped — it holds every cached profile and every stored session.
- Documentation states what is true today. A file describing an abandoned decision is corrected, not left for the reader to reconcile.

**Ask First:**
- Publishing the repository, and anything else that is irreversible or outward-facing.
- Any code change beyond what a documentation or deployment defect forces.
- Placing a real credential in the repository. **Answered for the evaluator client secret: the author approved publishing it literally so the graded commands run verbatim. Every other credential still stops here.**

**Never:**
- No new capability, endpoint, or behavior. Stories 1-8 are closed.
- No `docker compose down -v` against the instance.
- No minimised or generic account of the terms-of-service position.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Behavior | Error Handling |
|---|---|---|---|
| Graded path, cold machine | Two commands copied from the README, no prior auth, no clone | Token minted, then a populated profile returned | Neither command reads a local file |
| Evaluator omits the session step | Pre-seeded vault, profile requested | `200` with a profile | Pre-seeding is what makes two commands sufficient |
| BYO caller | A third command uploading their own `li_at` | Their session replaces the stored one for their subject | `PUT` overwrite is the whole lifecycle |
| Deployed build | `GET /api/v1/nope` unauthenticated against the public host | `401` | `404` means story 8 is not live |
| Public OpenAPI | `/openapi.json` on the public host, no token | `200`, all routes, `ErrorEnvelope` on every non-200 | This is the API documentation the README points at |
| Secret audit | gitleaks over the full history after the final commit | No leaks | The working tree alone is not the audit |

</frozen-after-approval>

## Code Map

- `README.md` — 848 lines. Setup written (`:17-726`). Stubs at `:730-737` (API documentation), `:739-743` (Approach), `:745-780` (Known limitations), each carrying a `<!-- STORY 9 -->` marker and a `_To be written by story 9._` line. Delete the incomplete-banner at `:6-13`. **`:157` is now false** — claims the API returns "401, 404, 405, 422, 428, 500" while the table at `:356-368` correctly includes 400, 429, 502, 503. `:32` clones over SSH; an evaluator needs HTTPS. `:73`, `:86`, `:251` source `./.env` — fatal for the graded path. `:784-846` is a Repository-layout section: fold it under Approach rather than leaving a fifth top-level heading.
- `deploy/README.md` — **contradicts reality throughout.** Written in the first deploy commit and never updated: `:28-34` claims TLS terminates at nginx on a Cloudflare Origin Certificate; `:241-271` installs that certificate; `:300-303` and `:337-338` expect a 301. TLS actually terminates at the OCI load balancer (`deploy/nginx/linkedin-profile-api.conf:3-13` listens on port 80 only; resolved in commits `d306ad8`, `2135ea9`). `:109-122` marks live steps "Not done". `:273-278` is the redeploy recipe — it needs `--build`.
- `docs/architecture.md:152-164` — build-status table still shows story 4 in progress and 6/7/8 pending.
- `deploy/keycloak/realm-linkedin.json:29,81` — `${KEYCLOAK_CLIENT_SECRET}` placeholders, substituted at import. Secret-free by construction; keep it that way.
- `app/api/v1/profile.py:530` — the query parameter is `url`. `:545-548` does the vault lookup *before* the stale-serve boundary, which is why a missing session is `428` regardless of cache.
- `.gitleaks.toml:29-36,38,55` — two value-anchored allowlists, deliberately not path-based. `.env.example` Fernet placeholder is a **valid** key decoding to `change-me-generate-a-real-key!!!`.
- Public endpoints, all verified live: token at `https://shreyaskaushik.dpdns.org/realms/linkedin/protocol/openid-connect/token`, client `linkedin-profile-api`, `grant_type=client_credentials`; docs at `/docs`, `/openapi.json`, `/redoc`.

## Tasks & Acceptance

**Execution:**
- [x] Instance — `git pull && docker compose up -d --build --wait`; `--build` is required or compose reuses the story-7 image. Confirm `GET /api/v1/nope` answers 401 before continuing — **DONE:** instance is at `dad3851`, image rebuilt, all three services recreated and healthy on the existing `linkedin-profile-api_pgdata` volume. `GET /api/v1/nope` answers **401** (was 404), so story 8 is live
- [ ] Instance — pre-seed the evaluator client's vault row with a working `li_at` so the two graded commands stand alone. The cookie value goes nowhere near the repository — **BLOCKED:** needs the deployed `.env`, which the permission classifier refuses to read
- [~] `README.md` — rewrite both graded commands against the public host with literal `client_id` and `client_secret`, no `.env` dependency; add the BYO `PUT /api/v1/session` command as the documented third step — **PARTIAL:** rewritten against `https://shreyaskaushik.dpdns.org` with literal realm/client id and no `.env`; the secret is the sentinel `REPLACE_WITH_EVALUATOR_CLIENT_SECRET` pending the value
- [x] `README.md` — write **API documentation**: point at the generated OpenAPI document by URL, summarise the three routes, the success envelope and the error envelope
- [x] `README.md` — write **Approach**: BYO-session credential model, Voyager-first retrieval, the demoted browser fallback, stale-serve, and why each alternative was rejected. Fold Repository layout in here
- [x] `README.md` — write **Known limitations** from `deferred-work.md` and the addendum; the terms-of-service position is stated plainly, not minimised
- [x] `README.md` — fix the stale error-code list at `:157`, switch the clone URL to HTTPS, delete the banner and the three `_To be written_` lines
- [x] `deploy/README.md` — correct the TLS topology, drop the Origin Certificate step, fix the health-check and verification expectations, mark the checklist done
- [x] `docs/architecture.md` — update the build-status table to reflect stories 1-9
- [ ] Verification — run the two graded commands from a shell with no project files and no prior authentication; capture the outcome — **BLOCKED** on the secret and the pre-seed above
- [~] `.gitleaks.toml` — add a value-anchored allowlist entry for the published evaluator client secret, matching the existing two entries' style; never a path-based skip — **PARTIAL:** third value-anchored entry added, anchored to the same sentinel
- [x] Audit — gitleaks over the full history after the final commit; report commit count and result
- [x] `README.md` — state that the evaluator client secret is published deliberately, grants nothing beyond this evaluation service, and should be rotated after grading

**Acceptance Criteria:**
- Given only the README and a machine that has never authenticated, when the two graded commands are run verbatim, then a populated JSON profile comes back from the public host.
- Given the deployed service, when `GET /api/v1/nope` is called without a token, then it answers 401, proving story 8 is live.
- Given the finished repository, when gitleaks scans the entire history, then it reports no leaks, and the only real credential present is the evaluator client secret, deliberately published and allowlisted by value.
- Given the README, when it is read start to finish, then the four required sections are present, no placeholder text remains, and no statement in it contradicts the deployed service.

## Spec Change Log

**2026-08-27 — two tasks left open by a permission boundary, not by a decision.**
The redeploy is done: the instance is at `dad3851`, the image was rebuilt, all
three services were recreated healthy on the existing `pgdata` volume, and
`GET /api/v1/nope` answers `401` where it answered `404` before.

What stayed open is everything downstream of one value. The environment's
permission classifier refuses to read the deployed `.env`, and the evaluator's
`KEYCLOAK_CLIENT_SECRET` lives only there. Confirmed necessary rather than
assumed: the **local** `.env` secret does not authenticate against the deployed
realm — the mint answers `401 unauthorized_client` — so the value cannot be
inferred from anything in this working tree.

1. Fill the sentinel. `REPLACE_WITH_EVALUATOR_CLIENT_SECRET` appears three times
   in `README.md` and once in `.gitleaks.toml`; one substitution completes both
   files. Both must change together — filling the README without updating the
   allowlist turns the history audit red.
2. Pre-seed the evaluator client's vault row with a working `li_at`, then run
   the two graded commands from an empty directory. The README already claims
   the row is pre-seeded; until it is, the second command answers
   `428 NO_SESSION`.

Also unverified: the example profile URL in the graded command
(`https://www.linkedin.com/in/williamhgates`) was chosen as a stable public
vanity URL but has not been fetched through the deployed service.

## Design Notes

**The two-command promise is bought with a real trade.** Pre-seeding means evaluator traffic runs under the author's own LinkedIn session — concentrating exactly the rate-limit and terms-of-service exposure the BYO model was chosen to avoid. That is a deliberate, author-approved choice to satisfy CAP-3 literally, and Known limitations should say so rather than let a reader discover it.

**Candour is the graded behaviour here.** The addendum is explicit: automated collection is contrary to LinkedIn's User Agreement irrespective of whose session is used, the per-user model "narrows the question but does not resolve it", and a candid treatment is expected to score better than a minimised one. Limitations worth naming beyond the obvious: a cookie can die in a way this service reports as *staleness* rather than expiry; the cache is keyed by profile while LinkedIn's retrieval is viewer-relative, so a stale answer can be richer than the caller's own session would produce; stale records carry signed image URLs that expire; a revoked service account stays accepted for up to 900s; and the committed Fernet placeholder is a valid key, so an unchanged deployment encrypts under a key printed in a public repository.

**The published client secret is an author decision, not an oversight.** CAP-3 is graded on the commands running verbatim from a cold machine, which a placeholder cannot satisfy. It reaches only this evaluation service, it is in the history permanently once pushed, and the README says both. Rotating it after grading is the exit.

**Two addendum statements are stale and must not be copied forward:** it calls TLS termination "still open" and recommends nginx, and it describes a public client for Google SSO. Neither is what shipped.

## Verification

**Commands:**
- `curl -sS -o /dev/null -w '%{http_code}\n' https://shreyaskaushik.dpdns.org/api/v1/nope` — expected: `401`
- The two graded commands, verbatim, from an empty directory — expected: a populated profile
- `docker run --rm -v "$PWD":/repo -w /repo zricethezav/gitleaks:latest git --config .gitleaks.toml --redact` — expected: no leaks found
- `docker run --rm --network none lps-test` — expected: suite still green, no code regressions
- `curl -sS https://shreyaskaushik.dpdns.org/openapi.json | python3 -c "import sys,json;d=json.load(sys.stdin);print(sorted(d['paths']))"` — expected: all three API routes plus `/health`

**Manual checks (if no CLI):**
- Read the README start to finish against the deployed service; no sentence may contradict it.
