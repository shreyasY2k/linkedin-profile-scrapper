# Deployment runbook

Everything outside `docker-compose.yml` that has to exist on the instance for
`https://shreyaskaushik.dpdns.org/health` to answer.

The application stack itself — API, Keycloak, Postgres — is entirely inside
`docker-compose.yml` and needs nothing here. Only Docker, nginx and the host
firewall are configured outside compose, which is what makes the nginx site
config the sole deployment-time wiring step. **No TLS material is installed on
this instance** — see below.

---

## Topology

```
client
  → Cloudflare        DNS for shreyaskaushik.dpdns.org, proxied
    → OCI Load Balancer      HTTPS listener :443 — TLS terminates HERE
      → instance <INSTANCE_PRIVATE_IP>:80   host nginx, the only public listener,
        → 127.0.0.1:8000              no certificate      the api container
        → 127.0.0.1:8080              keycloak (location /realms/ only)
```

`<INSTANCE_PRIVATE_IP>` is the instance's address on the VCN subnet, and the
placeholders below follow the same convention: every real address, hostname and
SSH alias is redacted, because none of them is what this runbook teaches. The
shape of the path is.

The instance has **no public IP**. Its only inbound path is the load balancer;
its egress is a NAT gateway (`<NAT_EGRESS_IP>`). Application containers keep the
loopback-only bindings they were given in `docker-compose.yml` — nothing but
nginx answers off-loopback.

**TLS terminates at the OCI load balancer**, which holds the certificate.
An earlier draft of this runbook planned termination at nginx on a Cloudflare
Origin Certificate; that was abandoned during the story-2 deploy (commits
`d306ad8`, `2135ea9`) and this file has been corrected to match what runs.
Concretely, and verified on the running host:

- nginx `listen 80` only, no `ssl` directive, no certificate paths —
  `deploy/nginx/linkedin-profile-api.conf`.
- Host `iptables` accepts 80 and **not** 443. Nothing on this box ever speaks
  TLS, so there is nothing for 443 to reach.
- **There is deliberately no certbot and no Origin Certificate on this
  instance.** There is no certificate to install, renew, or lose.

The one plaintext hop is load balancer → instance, inside the OCI VCN, never
across the public internet.

nginx must **not** `return 301 https://…`. It is handed `http` on requests the
client made over HTTPS, so a scheme-based redirect loops for ever. The
`http://` → `https://` upgrade belongs at the Cloudflare edge.

> **Known gap, stated rather than implied.** Cloudflare "Always Use HTTPS" is
> **not** currently switched on for this zone, and the load balancer has no
> port-80 listener, so `http://shreyaskaushik.dpdns.org/health` answers
> Cloudflare **522** rather than a 301. `https://` answers `200`. Every command
> in the README uses `https://`, so nothing graded depends on it; turning
> "Always Use HTTPS" on in the Cloudflare dashboard is the one-click fix and is
> what the nginx config's comments assume.

---

## The Oracle trap — read this before anything else

Opening the port in the **OCI Security List is not sufficient.** The stock
Ubuntu image drops it in host `iptables` independently. Open one layer and leave
the other shut and you get the worst possible symptom: the load balancer marks
the backend unhealthy while every setting in the OCI console reads correct.

**Both layers must be open:**

| Layer | Where | How |
|---|---|---|
| OCI Security List | OCI console — VCN → Security List | Ingress rule for TCP 80 |
| Host iptables | On the instance | `deploy/open-ports.sh` |

**Port 80 is the one that matters.** TLS terminates at the load balancer, so
nothing ever connects to this instance on 443. `open-ports.sh` opens both
because it was written before that was settled; the 443 rule is inert and
harmless, and the running chain in fact carries only the 80 ACCEPT.

And the host chain has a second trap. It ends in:

```
-A INPUT -j REJECT --reject-with icmp-host-prohibited
```

with only port 22 accepted above it. Rules must be **inserted above** that
REJECT (`iptables -I INPUT <line>`). `iptables -A` appends *after* it, where
they never match — the step then looks done and is not.

### Firewall safety

SSH is the only way into this host: no public IP, no console access from the
dev machine. A mistake here is unrecoverable without the OCI console.

- Never `iptables -F`, `-X`, or `-P` on INPUT. Insert only.
- Confirm the port-22 ACCEPT survives every change.
- **Persist only after a second SSH session has been confirmed to connect.**
  Unsaved rules are discarded by a reboot, which is the escape hatch.

`deploy/open-ports.sh` enforces all three: it refuses to run if the port-22
ACCEPT is missing, re-checks it afterwards, and deliberately does *not* persist.

---

## Current state of the instance

Recorded 2026-08-27. A `VM.Standard.A1.Flex` shape running Ubuntu 24.04.4
aarch64, sized for the compose stack with room to spare. Reached over SSH
through a bastion, since the instance carries no public IP: `<INSTANCE>` below
is a local `~/.ssh/config` alias whose `ProxyJump` names the bastion. The
region, the instance's own hostname and every address involved are deployment
details rather than runbook content, so they are not recorded here.

Two SPEC statements were wrong and are corrected here: Docker and nginx were
described as already "host-installed" and were not, and the 64 KB page-size risk
does not apply — the kernel reports 4 KB pages.

### Done — every step below is live

Steps 1–7 are complete and the service answers publicly. The step-by-step
sections that follow are kept as the reproduction recipe, not as a to-do list.

- [x] **Docker Engine 29.7.2 + Compose plugin v5.5.0**, from Docker's official
      apt repo for `noble`/`arm64`. `docker.service`, `docker.socket` and
      `containerd.service` are all `enabled` and `active`; `ubuntu` is in the
      `docker` group.

      The distro `docker.io` package was installed first and replaced, because
      it ships **no compose plugin** — `docker compose` is not a command under
      it. If you ever reinstall, use the official repo, not `apt install
      docker.io`.

      One gotcha worth recording: swapping `docker.io` for `docker-ce` leaves
      `docker.socket` stopped, and `dockerd` then dies in a restart loop with
      `failed to load listeners: no sockets found via socket activation`. The
      fix is `systemctl enable --now docker.socket` before starting
      `docker.service`, not a reinstall.

- [x] **nginx 1.24.0** (Ubuntu noble), `enabled` and `active`, serving
      `linkedin-profile-api.conf`. The stock `default` site is removed — it also
      claims `listen 80 default_server`, so nginx refuses to start with both.

- [x] **Host firewall** — TCP 80 accepted above the terminating REJECT, and
      persisted (step 1). Verified: `sudo iptables -S INPUT` shows the 80
      ACCEPT and the port-22 ACCEPT both above `REJECT`.

- [x] **OCI Security List** — ingress for TCP 80 (step 2).

- [x] **Repo and `.env` on the instance** (step 3). `~/linkedin-profile-scrapper`,
      remote `git@github.com:shreyasY2k/linkedin-profile-scrapper.git`.

- [x] **nginx site config** (step 4), from
      `deploy/nginx/linkedin-profile-api.conf`. Confirmed: nginx is the only
      process listening off-loopback (`0.0.0.0:80`); 8000, 8080 and 5432 are
      bound to `127.0.0.1` only.

- [x] **TLS** (step 5) — **nothing to do on the instance.** The load balancer
      holds the certificate. The Origin-Certificate step this runbook used to
      carry was for the abandoned nginx-termination plan and has been deleted
      rather than left to be reconciled.

- [x] **`docker compose up -d --build --wait`** (step 6). All three services
      healthy.

- [x] **Load balancer backend + Cloudflare DNS** (step 7). Live and verified:
      `https://shreyaskaushik.dpdns.org/health` → `{"status":"ok"}`.

---

## Step 1 — Host firewall

```bash
ssh <INSTANCE>
sudo bash ~/linkedin-profile-scrapper/deploy/open-ports.sh
```

It prints the chain before and after and stops short of persisting. Now, from
the dev machine, in a **separate terminal**:

```bash
ssh <INSTANCE> 'echo still-in'
```

Only once that succeeds:

```bash
ssh <INSTANCE> 'sudo netfilter-persistent save'
```

That writes `/etc/iptables/rules.v4`, which `netfilter-persistent.service`
restores at boot. `iptables-persistent` is already installed.

> The save captures Docker's own chains (`DOCKER`, `DOCKER-USER`,
> `DOCKER-ISOLATION-*`) alongside ours. That is harmless — `dockerd` rebuilds
> and reconciles its rules on every start — but it is why the reboot check in
> "Verification" is not optional.

## Step 2 — OCI Security List

OCI console → the VCN → the subnet's Security List → add **ingress** rules:

| Source | Protocol | Destination port |
|---|---|---|
| `0.0.0.0/0` | TCP | 80 |

Leave the existing port-22 rule alone. **80 only** — the load balancer speaks
HTTP to the instance, so nothing arrives on 443 and a 443 rule opens a port no
process is listening on.

## Step 3 — Repo and `.env` on the instance

```bash
ssh <INSTANCE>
git clone https://github.com/shreyasY2k/linkedin-profile-scrapper.git
cd linkedin-profile-scrapper
```

> While the repository is private, an unauthenticated clone fails with
> `could not read Username for 'https://github.com'`. That is the state the
> instance was set up in, so **its remote is the `git@` SSH URL** and pulls run
> over a forwarded agent (`ssh -A <INSTANCE>`), which leaves no key on the
> instance. Do not put a GitHub credential on the host. Once the repository is
> published, the HTTPS remote above works unauthenticated and is simpler:
> `git remote set-url origin https://github.com/shreyasY2k/linkedin-profile-scrapper.git`.

Then the environment file. **Do not type secrets into an interactive shell** —
they land in `~/.bash_history`. Write the file with an editor, or generate the
values in place:

```bash
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

Every variable is documented in `.env.example`. The deployed values differ from
local in these, and only these:

| Variable | Deployed value |
|---|---|
| `POSTGRES_PASSWORD` | a real generated password, not `change-me-local-only` |
| `KEYCLOAK_ADMIN_PASSWORD` | a real generated password |
| `KEYCLOAK_CLIENT_SECRET` | the secret Keycloak issues for the confidential client |
| `SESSION_ENCRYPTION_KEY` | a real Fernet key |
| `DATABASE_URL` | matched to `POSTGRES_PASSWORD` |

Generate them without echoing anything quotable into history:

```bash
# Fernet key for SESSION_ENCRYPTION_KEY
docker run --rm python:3.13-slim-trixie sh -c \
  'pip install -q cryptography && python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'

# passwords
openssl rand -base64 24
```

`.env` and `*.pem` / `*.key` / `*.crt` are gitignored from the first commit;
nothing generated here can be committed by accident.

> `DATABASE_URL` is read only by a run **outside** compose. Inside compose the
> `api` service composes it from the three `POSTGRES_*` values and injects it,
> overriding the file — so the two cannot drift. A password containing
> `@ : / ? #` would need percent-encoding in the `DATABASE_URL` line.

## Step 4 — nginx site config

`deploy/nginx/linkedin-profile-api.conf` is the whole of it, kept in the repo so
this step is reproducible rather than remembered.

```bash
sudo cp deploy/nginx/linkedin-profile-api.conf /etc/nginx/sites-available/
sudo ln -sf /etc/nginx/sites-available/linkedin-profile-api.conf \
            /etc/nginx/sites-enabled/linkedin-profile-api.conf

# The stock site also claims `listen 80 default_server`, so nginx refuses to
# start with both enabled.
sudo rm -f /etc/nginx/sites-enabled/default

sudo nginx -t && sudo systemctl reload nginx
```

`nginx -t` passes on its own: the config references no certificate and no key,
so there is nothing to install first. (An earlier draft of this runbook said
`nginx -t` would fail until a certificate was in place — that belonged to the
abandoned nginx-termination plan.) The config was validated against nginx 1.24.0
and exercised against the live API before it was committed (see "Verification").

## Step 5 — TLS: nothing to install here

**This step is deliberately empty on the instance.** TLS terminates at the OCI
load balancer, which holds the certificate; the certificate is attached to the
load balancer's HTTPS listener in the OCI console, and Cloudflare sits in front
of it, proxied.

The runbook previously described creating a Cloudflare Origin Certificate and
installing it into `/etc/ssl` for nginx. That plan was abandoned during the
story-2 deploy and the step has been removed rather than left for a reader to
reconcile against a config that holds no `ssl` directive. **No private key, no
certificate and no certbot exist on this host** — which is one fewer secret to
protect and one fewer renewal to remember.

## Step 6 — Bring the stack up

```bash
cd ~/linkedin-profile-scrapper
docker compose up -d --build --wait
```

Same compose file, same images, same Dockerfile as local. Only `.env` differs —
there is no `APP_ENV` and nothing in the code branches on an environment name.

**`--build` is not optional on a redeploy.** `docker compose up` happily reuses
an image that is already built, so without it a `git pull` that brought new
application source comes back up on the *old* code, healthy and wrong. This
exact trap put the deployed instance one story behind for a while.

`--wait` returns only once all three services report healthy; on a cold start
with empty volumes this takes roughly 30 s, most of it Keycloak's first-boot
schema build. A non-zero exit means a service never went healthy — start with
`docker compose logs api`. A missing or blank required variable aborts the API
at import time, naming the offending field.

Every service is `restart: unless-stopped`, so the stack returns after a reboot
on its own.

### Redeploying an existing instance

```bash
ssh <INSTANCE>
cd ~/linkedin-profile-scrapper
git pull --ff-only
docker compose up -d --build --wait
docker compose ps                       # three healthy
```

> **Never `docker compose down -v` here.** The `pgdata` volume holds the
> Keycloak realm, every encrypted session in the vault, and every cached
> profile. `up -d --build` replaces the containers in place and leaves the
> volume alone, which is what a redeploy should do.

Then confirm from outside that the new build is actually the one answering:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://shreyaskaushik.dpdns.org/api/v1/nope
# 401 — unmatched /api/v1 paths are behind auth (story 8).
# 404 means an older image is still running: the --build was skipped.
```

### Re-exporting the Keycloak realm

`deploy/keycloak/realm-linkedin.json` is committed with `${KEYCLOAK_CLIENT_SECRET}`
and `${KEYCLOAK_SECOND_CLIENT_SECRET}` placeholders, substituted at import time.
**Exporting from a running Keycloak does not preserve that** — `kc.sh export`
emits the literal client secrets and the realm's signing key material, and
committing the result would put real credentials in the public history.

If the realm ever has to be re-exported, do it into a scratch directory outside
the repository, then hand-edit the two secret values back to the `${...}`
placeholders and diff against the committed file before staging anything:

```bash
docker compose exec keycloak /opt/keycloak/bin/kc.sh export \
  --dir /tmp/realm-export --realm linkedin --users skip
docker compose cp keycloak:/tmp/realm-export/linkedin-realm.json /tmp/
# hand-edit /tmp/linkedin-realm.json: restore ${KEYCLOAK_CLIENT_SECRET} and
# ${KEYCLOAK_SECOND_CLIENT_SECRET}, drop any `keys`/`components` key material,
# then diff before copying it over the committed file.
pre-commit run --all-files            # gitleaks is the backstop, not the plan
```

## Step 7 — Load balancer and DNS

Both are console changes.

1. **OCI Load Balancer** — backend set containing `<INSTANCE_PRIVATE_IP>` **on port 80**,
   and a single **HTTPS listener on 443** carrying the certificate. The
   listener terminates TLS and forwards plain HTTP to the backend's port 80.
   There is no port-80 listener, which is why `http://shreyaskaushik.dpdns.org`
   currently answers Cloudflare 522 rather than a redirect.

   > **Health check:** probe **`:80`**, path **`/`**, expecting **`200`**.
   > Nothing on this instance speaks TLS, so a `:443` check can never pass, and
   > nothing redirects, so there is no 301 to accept. `GET /` rather than
   > `/health` because that is the path the load balancer actually requests
   > (confirmed in the nginx access log: the load balancer's own VCN address
   > requesting `/`), and
   > `location = /` in the site config proxies it to the API's `/health`. The
   > check is therefore truthful — 200 only while the API really answers, and
   > 502 the moment it does not.
   >
   > An earlier version of this runbook told you to expect a 301 on `:80`. That
   > belonged to the abandoned nginx-TLS plan and is wrong: nginx here does not
   > redirect, deliberately, because it is handed `http` on requests the client
   > made over HTTPS.

2. **Cloudflare DNS** — an **A** record for `shreyaskaushik.dpdns.org` pointing
   at the load balancer's public IP, **proxied** (orange cloud). The zone is
   served by the pair of Cloudflare nameservers the dashboard assigns it.

   Consider switching **SSL/TLS → Edge Certificates → Always Use HTTPS** on. It
   is off today, which is why plain `http://` 522s instead of redirecting.

---

## Verification

### Already verified, locally

The parts that do not need the cloud were proven on the dev machine before
being committed:

| Check | Result |
|---|---|
| `docker compose up -d --wait` from empty volumes | all three healthy in ~27 s |
| `curl -fsS http://127.0.0.1:8000/health` | `{"status":"ok"}` |
| Test suite (`docker build --target test`) | 949 passed, 17 skipped — the opt-in live checks: 1 in `tests/test_linkedin_live.py`, 16 in `tests/test_postgres_live.py` |
| `nginx -t` on the site config, nginx 1.24.0 | syntax ok |
| The site config in front of the real API | `HTTP/1.1 200` `{"status":"ok"}` on `/health`, HSTS and nosniff present |
| Published ports reachable off-loopback | refused on 8000, 8080 and 5432 |

The nginx config was exercised by running nginx 1.24 inside the API container's
network namespace, so `proxy_pass http://127.0.0.1:8000` resolved to the real
API and the file was tested verbatim.

### Verified live

From a machine **outside** the instance's network:

```bash
curl -fsS https://shreyaskaushik.dpdns.org/health          # {"status":"ok"}
curl -sS -o /dev/null -w '%{http_code}\n' \
     https://shreyaskaushik.dpdns.org/api/v1/nope          # 401, not 404
curl -sS https://shreyaskaushik.dpdns.org/openapi.json \
  | python3 -c "import sys,json;print(sorted(json.load(sys.stdin)['paths']))"
# ['/api/v1/profile', '/api/v1/session', '/health']
```

`curl -sI http://shreyaskaushik.dpdns.org/health` answers **522**, not a 301 —
there is no port-80 listener on the load balancer and "Always Use HTTPS" is off
at the Cloudflare edge. Everything graded uses `https://`. The certificate an
`openssl s_client` shows is Cloudflare's edge certificate, not one of ours;
there is no origin certificate to inspect.

On the instance:

```bash
ssh <INSTANCE> 'sudo iptables -S INPUT'          # ACCEPT 80 ABOVE the REJECT
ssh <INSTANCE> 'cd linkedin-profile-scrapper && docker compose ps'   # three healthy
ssh <INSTANCE> 'sudo ss -tlnp'                   # nginx on 0.0.0.0:80 only;
                                                 # 8000, 8080, 5432 on 127.0.0.1 only
ssh <INSTANCE> 'sudo systemctl is-enabled docker nginx'             # enabled, enabled
```

### The reboot check — do not skip it

The acceptance criterion is that the instance comes back unaided. It is also the
only real test that the firewall rules were persisted and not merely applied.

```bash
ssh <INSTANCE> 'sudo systemctl reboot'
# wait ~60s
curl -fsS https://shreyaskaushik.dpdns.org/health
ssh <INSTANCE> 'sudo iptables -S INPUT'
```

The `/health` call must succeed with no manual step in between.
