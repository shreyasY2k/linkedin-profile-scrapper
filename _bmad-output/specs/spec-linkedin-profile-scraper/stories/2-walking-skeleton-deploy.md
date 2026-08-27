---
title: 'Walking-skeleton deploy through the load balancer and nginx'
type: 'feature'
created: '2026-08-27'
status: 'in-progress'
baseline_commit: '6f67e1b'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/specs/spec-linkedin-profile-scraper/SPEC.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The instance has never served anything. Docker and nginx are absent, host iptables rejects every port but 22, the instance holds no public IP, and `shreyaskaushik.dpdns.org` has no A record. Stories 3–9 are each a redeploy, so an unproven pipeline moves every deployment failure to evaluation eve.

**Approach:** Take the existing `/health` endpoint all the way to `https://shreyaskaushik.dpdns.org` — install Docker and nginx, open 80/443 at both firewall layers, run the story-1 compose stack on the instance, reverse-proxy to it, and terminate TLS at nginx with a Cloudflare Origin Certificate under Full (strict).

## Boundaries & Constraints

**Always:**
- nginx is the only public listener on the host and proxies to `127.0.0.1`. Application containers keep their loopback-only bindings from story 1.
- Both firewall layers must open 80/443: host iptables **and** the OCI Security List. Opening one while the other blocks is the failure that looks like a working configuration.
- Firewall rules, Docker, nginx, and the stack all survive a reboot with no manual step.
- The deployed stack runs the same compose file and images as local. Only `.env` differs.
- The origin private key exists only on the host at mode 0600, never in the repository.

**Ask First:**
- Anything requiring the OCI console or API — load balancer, Security List, VNIC. There are no OCI credentials on this machine; those changes are the human's to make.
- Replacing or rotating the Origin Certificate.
- Any change that makes the instance reachable other than through the load balancer.

**Never:**
- No Let's Encrypt or certbot. The Origin Certificate is chosen precisely to keep renewal out of scope.
- No application code changes. This story is deployment wiring; if the app must change, that is a finding, not a fix.
- No realm configuration, no business logic (stories 3–8).
- No secret committed, and no credential typed into a shell that records history.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Behavior | Error Handling |
|---|---|---|---|
| Public happy path | `curl https://shreyaskaushik.dpdns.org/health` from outside the network | `200 {"status":"ok"}` over a valid TLS chain | N/A |
| Plain HTTP | `http://.../health` | Redirected to HTTPS | 301 |
| Host firewall shut | 80/443 rejected by host iptables | LB backend marked unhealthy while every OCI console setting reads correct | Open both layers; verify from off-host |
| Origin cert missing | Cloudflare set to Full (strict) | Cloudflare returns 526 rather than serving plaintext | Install cert + key before switching the mode |
| Container exposure | Probe instance `10.0.1.173:8000` from another host | Refused — only nginx listens off-loopback | Loopback bindings unchanged |
| Reboot | Instance restarts | nginx, Docker, the stack and the firewall rules all return unaided | `restart: unless-stopped`, enabled units, persisted iptables |

</frozen-after-approval>

## Code Map

Verified on the instance this session — several SPEC statements were wrong and are corrected here.

- Host `instance-20260827-1151`, `VM.Standard.A1.Flex`, ap-mumbai-1, **Ubuntu 24.04.4 aarch64, 4 KB pages** (the 64 KB page-size risk does not apply), 11.9 GB RAM, passwordless sudo.
- Reached as `oci-docker` in `~/.ssh/config` — `ProxyJump oci-jump` (80.225.240.74), user `ubuntu`, `~/.ssh/id_rsa`.
- **Docker and nginx are NOT installed**, contradicting SPEC's "host-installed". Both are this story's work.
- Instance has **no public IP**: `10.0.1.173/24`, egress via NAT (`129.154.237.13`). The load balancer is the only public path.
- Host iptables ends in `-A INPUT -j REJECT --reject-with icmp-host-prohibited` with only 22 accepted above it — new rules must be **inserted before** that REJECT, not appended.
- `shreyaskaushik.dpdns.org` uses Cloudflare nameservers (`jerry`/`maria.ns.cloudflare.com`) and currently resolves to **no A record**.
- `../../../../docker-compose.yml`, `../../../../Dockerfile` — deployed unchanged from story 1.

**Pending human inputs** (blocking only the final end-to-end check): the load balancer's public IP, and the Origin Certificate + key.

## Tasks & Acceptance

**Execution:**
- [ ] host — install Docker Engine + compose plugin from Docker's official arm64 repo; enable the unit — the stack cannot run otherwise
- [ ] host — install nginx and enable the unit — the sole public listener
- [ ] host — insert ACCEPT rules for 80/443 **above** the trailing REJECT, then persist via `iptables-persistent` — survives reboot
- [ ] host — clone the repo and write `.env` with real values, never echoed into shell history — same compose file as local
- [ ] `deploy/nginx/linkedin-profile-api.conf` — commit the site config: `server_name`, `proxy_pass` to `127.0.0.1:8000`, `X-Forwarded-*`, HTTP→HTTPS redirect — the one deployment-time wiring step, kept reproducible in the repo
- [ ] host — install the Origin Certificate and key under `/etc/ssl/`, key at 0600 root-owned
- [ ] host — `docker compose up -d --wait` on the instance
- [ ] `deploy/README.md` — record the exact steps performed, so story 9 can describe the deployment truthfully
- [ ] verify — from this machine, off the instance network, over the public name

**Acceptance Criteria:**
- Given a machine outside the instance's network, when it requests `https://shreyaskaushik.dpdns.org/health`, then it receives `200 {"status":"ok"}` over a valid chain.
- Given the deployed host, when its listening sockets are inspected, then only nginx answers off-loopback and the application ports remain bound to `127.0.0.1`.
- Given the instance is rebooted, when it returns, then the stack answers publicly again with no manual intervention.
- Given the repository, when it is inspected, then it contains the nginx config and deployment notes but no certificate key and no `.env`.

## Spec Change Log

**2026-08-27 — PAUSED partway through host preparation, by human decision.** Deployment work is deferred until the local application code is complete and tested; the human asked that no further SSH be made to the instance. Resume from here, do not re-probe.

State verified on the host at the moment of pause:
- **Done:** Docker 29.7.2 + Compose v5.5.0 installed; nginx 1.24.0 installed, enabled and active, still serving the default site on `0.0.0.0:80`.
- **Not done:** host iptables is **unchanged** — port 22 accepted then a blanket REJECT, so nginx is listening but unreachable from outside. No repository on the host, no containers, no certificate, no site config.
- **Blocked on the human:** the iptables insert (positions 5 and 6, above the REJECT) plus `netfilter-persistent save`; the OCI Security List ingress rules for 80/443; the load balancer's public IP; the Cloudflare A record; and the Origin Certificate and key.
- No firewall rule was altered, so the host is exactly as found apart from the two packages installed.
- Repo artifacts produced and committed (`4327573`): `deploy/nginx/linkedin-profile-api.conf`, `deploy/open-ports.sh`, `deploy/README.md`. The nginx config was exercised against the live local API inside its network namespace, not merely syntax-checked.

Findings recorded for the resumed run:
- **The load-balancer health check must not probe `:80 /health` expecting 200.** nginx answers 301 there, so the LB marks a healthy backend down — the same false-negative class as the firewall trap. Probe `:443 /health` or accept 301.
- **The GitHub repository is private** (unauthenticated API returns 404), so `git clone` on the instance fails and SPEC's public-repository requirement is unmet. Make it public rather than placing a credential on the host.
- **`netfilter-persistent save` captures Docker's chains too.** Harmless, but it makes the post-reboot verification mandatory rather than optional.
- Docker was installed as `docker-ce`, replacing an in-flight `docker.io` install found on the host; `docker.io` ships no compose plugin. `docker.socket` had to be enabled before `dockerd` would start.

## Design Notes

**The Oracle trap, concretely.** Opening 80/443 in the OCI Security List is not sufficient: the stock Ubuntu image drops those ports in host iptables independently, and the load-balancer health check then fails while every cloud-side setting reads correct. This host is confirmed to be in exactly that state — only port 22 is accepted, and the chain ends in a blanket REJECT. Rules must be **inserted** above that REJECT (`iptables -I INPUT ...`), because appending puts them after it where they never match.

**Firewall safety — SSH is the only way in.** This host has no public IP and no console access from here. Never flush, replace, or set a policy on the INPUT chain: only `iptables -I INPUT` to insert, and confirm the port-22 ACCEPT is still present and a second SSH session still connects after every change, before persisting. A mistake here is unrecoverable without the OCI console.

**TLS.** Cloudflare proxied, Full (strict), Origin Certificate installed at nginx — chosen after the plaintext-cookie exposure of Flexible was raised, since this service carries `li_at` cookies and bearer tokens. Free, 15-year, no renewal on a server nobody watches after submission.

## Verification

**Commands:**
- `curl -fsS https://shreyaskaushik.dpdns.org/health` — expected: `{"status":"ok"}`
- `curl -sI http://shreyaskaushik.dpdns.org/health` — expected: a 301 to the HTTPS URL
- `curl -sv https://shreyaskaushik.dpdns.org/health 2>&1 | grep -i "issuer\|subject"` — expected: a valid chain, no verification error
- `ssh oci-docker 'sudo iptables -S INPUT'` — expected: ACCEPT for 80 and 443 listed above the REJECT
- `ssh oci-docker 'docker compose ps'` — expected: all three services healthy
- `ssh oci-docker 'ss -tlnp'` — expected: nginx on `0.0.0.0:80/443`; 8000, 8080, 5432 on `127.0.0.1` only
- `ssh oci-docker 'sudo systemctl is-enabled docker nginx'` — expected: `enabled` for both
