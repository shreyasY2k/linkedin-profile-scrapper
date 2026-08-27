# Architecture

Diagrams render natively on GitHub. Everything shown as built is verified running; everything shown as pending is named as such rather than drawn optimistically.

## Deployment topology

The instance holds **no public IP**. The load balancer is the only public path, and nginx is the only process on the host that listens off-loopback — every application container binds `127.0.0.1`.

```mermaid
flowchart LR
    EV["Evaluator<br/><code>curl</code>"]

    subgraph Edge["Public edge — TLS lives here"]
        CF["Cloudflare<br/>proxied"]
        LB["OCI Load Balancer<br/>terminates TLS · holds the certificate"]
    end

    subgraph Host["Oracle A1 Flex · Ubuntu 24.04 · arm64 · 10.0.1.173/24 (private, no public IP)"]
        NGX["host nginx 1.24<br/>port 80 only · no certificate"]

        subgraph DC["docker compose — one command"]
            API["FastAPI<br/>127.0.0.1:8000"]
            KC["Keycloak 26.7.2<br/>127.0.0.1:8080"]
            PG[("Postgres 18.6<br/>127.0.0.1:5432")]
        end
    end

    LI["LinkedIn Voyager API"]

    EV -->|HTTPS| CF -->|HTTPS| LB -->|"HTTP :80 (inside the VCN)"| NGX
    NGX -->|"location = / → /health"| API
    NGX -->|"location /"| API
    NGX -->|"location /realms/"| KC
    API --> PG
    KC --> PG
    API -->|"li_at session"| LI
```

**TLS terminates at the load balancer**, which holds the certificate. nginx therefore holds none and listens on port 80 only — and host `iptables` opens 80 and *not* 443, which matches exactly. The one plaintext hop is load balancer → instance, inside the OCI VCN rather than across the public internet.

This is why nginx must **not** redirect to HTTPS: it sees `http` on a request the client made over HTTPS, so a scheme-based redirect loops forever. Cloudflare handles `http://` → `https://` at the edge.

`/admin/` is intentionally **not** proxied. The Keycloak admin console is reachable only over an SSH tunnel.

## Request flow

Two `curl` commands are what CAP-3 is graded on: mint a token, then call the endpoint.

```mermaid
sequenceDiagram
    autonumber
    participant C as Caller
    participant N as nginx
    participant A as FastAPI
    participant K as Keycloak
    participant D as Postgres
    participant V as Voyager

    C->>N: POST /realms/{realm}/protocol/openid-connect/token
    N->>K: client_credentials (no browser redirect)
    K-->>C: access_token · 900s

    C->>N: GET /api/v1/profile?url=… + Bearer
    N->>A: proxy, X-Forwarded-* preserved
    A->>K: JWKS (process-cached, refetched on unknown kid)
    A->>A: verify signature → iss → aud → exp/iat/nbf
    A->>D: load encrypted li_at for this subject
    A->>V: 6 requests — core + 5 sections

    alt live retrieval succeeds
        V-->>A: normalized {data, included}
        A->>D: cache payload + fetched_at
        A-->>C: 200 · stale=false
    else upstream refuses, cached record exists
        D-->>A: last good record, any age
        A-->>C: 200 · stale=true · original fetched_at
    else upstream refuses, nothing cached
        A-->>C: typed error · retryable flag set honestly
    end
```

Step 8 is the load-bearing one: **a profile costs six upstream calls, not one.** The core entity carries only `experienceCardUrn` and `educationCardUrn` pointers, so each section is its own request. That multiplies rate-limit exposure sixfold per API call and is the strongest argument for stale-serve.

## Retrieval fan-out

```mermaid
flowchart TD
    URL["/in/{public-id}"] --> CORE["identity/dash/profiles<br/>?q=memberIdentity"]
    CORE -->|"entityUrn"| FAN{"fan out<br/>concurrently"}

    FAN --> P["profilePositions"]
    FAN --> E["profileEducations"]
    FAN --> S["profileSkills"]
    FAN --> CE["profileCertifications"]
    FAN --> L["profileLanguages"]

    P & E & S & CE & L --> M["map to response-schema"]
    CORE --> M
    M --> OUT["profile + partial[]"]

    DEAD["profileView · 410 Gone<br/>graphql without queryId · 403"]
    classDef dead fill:#3b1f1f,stroke:#a33,color:#fca
    class DEAD dead
```

`profileView` — the endpoint most published guidance still recommends — returns **410 Gone**. The map above was measured against the live API, not recalled.

A section failing degrades rather than aborts: it is omitted from the payload and named in `partial[]`. Only a core-profile failure fails the whole fetch.

## Absent versus unreadable

The distinction `response-schema.md` calls load-bearing, and the easiest thing in this system to get quietly wrong.

```mermaid
flowchart TD
    Q{"section request"}
    Q -->|"HTTP error"| U["unreadable<br/>omit key · add to partial[]"]
    Q -->|"200, elements present"| V["map normally"]
    Q -->|"200, zero elements"| AMB{"genuinely empty?"}
    AMB -->|"cannot be distinguished"| U

    classDef warn fill:#3a2f14,stroke:#b8860b,color:#f5deb3
    class AMB warn
```

Measured: `profileLanguages` returned **0 elements** on one call and **3** on an identical call minutes later — HTTP 200, no error, both times. So a zero-length section is *not* evidence the profile lacks that data. Mapping empty → `[]` would publish "this person speaks no languages" as fact. It belongs in `partial[]`.

## Authentication boundary

```mermaid
flowchart LR
    subgraph Public["unauthenticated"]
        H["GET /health"]
    end

    subgraph Guarded["/api/v1 router — dependency attached here"]
        PR["GET /profile"]
        SG["GET /session"]
        SP["PUT /session"]
    end

    REQ["request"] --> H
    REQ --> AUTH{"require_claims"}
    AUTH -->|"valid"| PR & SG & SP
    AUTH -->|"otherwise"| E401["401 UNAUTHENTICATED"]
```

Auth attaches to the **router**, not to individual routes, so a later route inherits protection whether or not its author remembered it. A test asserts this by mounting a route that declares no dependency of its own and requiring it to 401 — removing the router dependency fails three tests.

`/health` stays outside `/api/v1` because the container healthcheck has no token to present.

## Build status

| # | Story | State |
|---|---|---|
| 1 | Skeleton, env config, local parity | Done |
| 2 | Deploy through LB and nginx | **Done** — live at https://shreyaskaushik.dpdns.org |
| 3 | Keycloak realm, clients, JWT validation | Done |
| 4 | Voyager client, raw JSON | In progress |
| 5 | Encrypted per-user session vault | Pending |
| 6 | Profile extraction and schema mapping | Pending — the graded core |
| 7 | Response cache with stale-serve | Pending |
| 8 | Error taxonomy and handlers | Pending |
| 9 | README, secret audit, final deploy | Pending |

## Decisions worth knowing

| Decision | Why |
|---|---|
| Per-user BYO `li_at` rather than one owner account | One lockout would otherwise take the whole service down during evaluation |
| Voyager JSON, never rendered HTML | HTML is authwalled and truncated for datacenter IPs; the API is the literal task |
| Stale-serve unbounded — no TTL, no eviction | The service is graded on still answering. A record of any age beats an error |
| Keycloak `start`, never `start-dev` | Local and deployed must differ by env alone, never by command |
| TLS terminates at the load balancer | Cloudflare→LB is encrypted; the only plaintext hop is LB→instance inside the private VCN, never the public internet |
| Load-balancer health check probes `GET /`, proxied to `/health` | The check must be *truthful*: 200 only while the API really answers. A static 200 would report a dead backend as healthy |
| 428 for missing/expired session | The caller has a fixable missing precondition, which is exactly what 428 means |
