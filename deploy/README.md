# Deployment runbook

Everything outside `docker-compose.yml` that has to exist on the instance for
`https://shreyaskaushik.dpdns.org/health` to answer.

The application stack itself — API, Keycloak, Postgres — is entirely inside
`docker-compose.yml` and needs nothing here. Only Docker, nginx, the host
firewall and the TLS material are configured outside compose, which is what
makes the nginx site config the sole deployment-time wiring step.

---

## Topology

```
client
  → Cloudflare        DNS for shreyaskaushik.dpdns.org, proxied, Full (strict)
    → OCI Load Balancer                     the only public path to the instance
      → instance 10.0.1.173:80 / :443       host nginx, the only public listener
        → 127.0.0.1:8000                    the api container
```

The instance has **no public IP**. Its only inbound path is the load balancer;
its egress is a NAT gateway (`129.154.237.13`). Application containers keep the
loopback-only bindings they were given in `docker-compose.yml` — nothing but
nginx answers off-loopback.

**TLS terminates at nginx**, on a Cloudflare Origin Certificate, with Cloudflare
set to Full (strict). This resolves the SPEC's open question. It was chosen over
Cloudflare Flexible because Flexible leaves the Cloudflare→origin hop in
plaintext and this service carries `li_at` cookies and bearer tokens; and over
Let's Encrypt because an Origin Certificate is free, lasts 15 years, and needs
no ACME client or renewal timer on a host nobody watches after submission.
**There is deliberately no certbot on this instance.**

---

## The Oracle trap — read this before anything else

Opening 80/443 in the **OCI Security List is not sufficient.** The stock Ubuntu
image drops those ports in host `iptables` independently. Open one layer and
leave the other shut and you get the worst possible symptom: the load balancer
marks the backend unhealthy while every setting in the OCI console reads
correct.

**Both layers must be open:**

| Layer | Where | How |
|---|---|---|
| OCI Security List | OCI console — VCN → Security List | Ingress rules for TCP 80 and 443 |
| Host iptables | On the instance | `deploy/open-ports.sh` |

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

Recorded 2026-08-27. Host `shreyasmkmathur-compute-vnic`, `VM.Standard.A1.Flex`,
ap-mumbai-1, Ubuntu 24.04.4 aarch64, 11.9 GB RAM, 45 GB disk, passwordless sudo.
Reached as `oci-docker` in `~/.ssh/config` (`ProxyJump oci-jump` → 80.225.240.74).

Two SPEC statements were wrong and are corrected here: Docker and nginx were
described as already "host-installed" and were not, and the 64 KB page-size risk
does not apply — the kernel reports 4 KB pages.

### Done

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

- [x] **nginx 1.24.0** (Ubuntu noble), `enabled` and `active`. Still serving
      only the stock `default` site.

### Not done — these are yours to run

The remaining steps were not executed: the cloud side of this deployment is
owned by the operator, and the two inputs the last step needs (the load
balancer's public IP, and the Origin Certificate + key) are not on the dev
machine.

- [ ] Host firewall — `deploy/open-ports.sh`, then persist (step 1)
- [ ] OCI Security List ingress for 80/443 — console (step 2)
- [ ] Repo + `.env` on the instance (step 3)
- [ ] nginx site config (step 4)
- [ ] Origin Certificate + key (step 5)
- [ ] `docker compose up -d --wait` (step 6)
- [ ] Load balancer backend + Cloudflare DNS (step 7)

---

## Step 1 — Host firewall

```bash
ssh oci-docker
sudo bash ~/linkedin-profile-scrapper/deploy/open-ports.sh
```

It prints the chain before and after and stops short of persisting. Now, from
the dev machine, in a **separate terminal**:

```bash
ssh oci-docker 'echo still-in'
```

Only once that succeeds:

```bash
ssh oci-docker 'sudo netfilter-persistent save'
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
| `0.0.0.0/0` | TCP | 443 |

Leave the existing port-22 rule alone.

## Step 3 — Repo and `.env` on the instance

```bash
ssh oci-docker
git clone https://github.com/shreyasY2k/linkedin-profile-scrapper.git
cd linkedin-profile-scrapper
```

> While the repository is still private, an unauthenticated clone fails with
> `could not read Username for 'https://github.com'`. Either publish the
> repository — the SPEC requires a public repo at submission anyway — or clone
> once over forwarded SSH agent (`ssh -A oci-docker`, remote set to the `git@`
> URL), which leaves no key on the instance. Do not put a GitHub credential on
> the host.

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

`nginx -t` fails until step 5 puts the certificate in place — that is expected,
and is why reload comes after. The config was validated against nginx 1.24.0
and exercised against the live API before it was committed (see "Verification").

## Step 5 — Origin Certificate

Cloudflare dashboard → the zone → **SSL/TLS → Origin Server → Create
Certificate**. Take the default (RSA, 15 years) for
`shreyaskaushik.dpdns.org`. Cloudflare shows the key **once**.

Install with the paths the site config expects:

```bash
sudo install -m 0644 -o root -g root /dev/null /etc/ssl/certs/shreyaskaushik.dpdns.org.pem
sudo install -m 0600 -o root -g root /dev/null /etc/ssl/private/shreyaskaushik.dpdns.org.key

sudo $EDITOR /etc/ssl/certs/shreyaskaushik.dpdns.org.pem   # paste the certificate
sudo $EDITOR /etc/ssl/private/shreyaskaushik.dpdns.org.key # paste the private key
```

Paste into an editor. Do not `echo` the key into a file — that is a secret in
shell history.

Confirm the modes, then reload:

```bash
sudo ls -l /etc/ssl/private/shreyaskaushik.dpdns.org.key   # -rw------- root root
sudo nginx -t && sudo systemctl reload nginx
```

Finally set Cloudflare **SSL/TLS → Overview → Full (strict)**. Order matters:
switch to Full (strict) *before* the certificate is installed and Cloudflare
returns **526** rather than serving anything in plaintext.

The key exists only here, at mode 0600, root-owned, and never in the repository.

## Step 6 — Bring the stack up

```bash
cd ~/linkedin-profile-scrapper
docker compose up -d --wait
```

Same compose file, same images, same Dockerfile as local. Only `.env` differs —
there is no `APP_ENV` and nothing in the code branches on an environment name.

`--wait` returns only once all three services report healthy; on a cold start
with empty volumes this takes roughly 30 s, most of it Keycloak's first-boot
schema build. A non-zero exit means a service never went healthy — start with
`docker compose logs api`. A missing or blank required variable aborts the API
at import time, naming the offending field.

Every service is `restart: unless-stopped`, so the stack returns after a reboot
on its own.

## Step 7 — Load balancer and DNS

Both are console changes.

1. **OCI Load Balancer** — backend set containing `10.0.1.173`, listeners on 80
   and 443 forwarding to the same ports on the backend.

   > **Health check:** point it at **`:443` `/health`**, or at `:80 /health`
   > with **301 accepted**. Port 80 redirects everything to HTTPS, `/health`
   > included, so an `:80` check expecting `200` marks a perfectly healthy
   > backend as down. This is the same class of false negative as the firewall
   > trap, and it is easy to spend an hour on.

2. **Cloudflare DNS** — an **A** record for `shreyaskaushik.dpdns.org` pointing
   at the load balancer's public IP, **proxied** (orange cloud). The zone is
   already on Cloudflare nameservers (`jerry` / `maria.ns.cloudflare.com`) and
   currently has no A record at all.

---

## Verification

### Already verified, locally

The parts that do not need the cloud were proven on the dev machine before
being committed:

| Check | Result |
|---|---|
| `docker compose up -d --wait` from empty volumes | all three healthy in ~27 s |
| `curl -fsS http://127.0.0.1:8000/health` | `{"status":"ok"}` |
| Test suite (`docker build --target test`) | 44 passed |
| `nginx -t` on the site config, nginx 1.24.0 | syntax ok |
| The site config in front of the real API | `http` → `301`, `https` → `HTTP/2 200` `{"status":"ok"}`, HSTS and nosniff present |
| Published ports reachable off-loopback | refused on 8000, 8080 and 5432 |

The nginx config was exercised by running nginx 1.24 inside the API container's
network namespace, so `proxy_pass http://127.0.0.1:8000` resolved to the real
API and the file was tested verbatim.

### To run once steps 1–7 are done

From a machine **outside** the instance's network:

```bash
curl -fsS https://shreyaskaushik.dpdns.org/health          # {"status":"ok"}
curl -sI  http://shreyaskaushik.dpdns.org/health           # 301 to the https URL
curl -sv  https://shreyaskaushik.dpdns.org/health 2>&1 | grep -i "issuer\|subject"
```

On the instance:

```bash
ssh oci-docker 'sudo iptables -S INPUT'          # ACCEPT 80 and 443 ABOVE the REJECT
ssh oci-docker 'cd linkedin-profile-scrapper && docker compose ps'   # three healthy
ssh oci-docker 'sudo ss -tlnp'                   # nginx on 0.0.0.0:80/443;
                                                 # 8000, 8080, 5432 on 127.0.0.1 only
ssh oci-docker 'sudo systemctl is-enabled docker nginx'             # enabled, enabled
```

### The reboot check — do not skip it

The acceptance criterion is that the instance comes back unaided. It is also the
only real test that the firewall rules were persisted and not merely applied.

```bash
ssh oci-docker 'sudo systemctl reboot'
# wait ~60s
curl -fsS https://shreyaskaushik.dpdns.org/health
ssh oci-docker 'sudo iptables -S INPUT'
```

The `/health` call must succeed with no manual step in between.
