# Asterisk container build (Phase K)

Two Dockerfiles, same image contract. Pick `dev` for fast local
iteration; `prod` for reproducible images shipped from this fork.

## Build

```bash
# Fast (~30 s, ~250 MB) - overlay andrius/asterisk:22-current with our
# configs/samples. Use for docker-compose / k3d.
docker build -t velents/asterisk:dev -f deploy/Dockerfile.dev .

# Reproducible (~10-15 min on a 4-core builder, ~600 MB) - multi-stage
# build from this fork's source. CI pipeline target.
docker build -t velents/asterisk:22.0.0 -f deploy/Dockerfile.prod .
```

Both images expose the same surface: same ports, same volumes, same
entrypoint, same env contract. The dev/prod swap is invisible to
operators.

## Run (single host)

```bash
docker run --rm -it --network host \
  -e ARI_PASSWORD=replace-me \
  -e EXTERNAL_MEDIA_ADDRESS=$(curl -s ifconfig.me) \
  -v $PWD/local-keys:/etc/asterisk/keys:ro \
  -v asterisk-recordings:/var/spool/asterisk/recording \
  velents/asterisk:dev
```

`--network host` is the recommended dev path because SIP/RTP NAT is
brittle. In K8s the equivalent is `hostNetwork: true` on the pod spec
(see _Kubernetes notes_ below).

## Ports

| Port        | Proto      | Purpose |
|-------------|------------|---------|
| 5060        | UDP + TCP  | SIP signalling (PJSIP `transport-udp`) |
| 5061        | TCP        | SIPS (TLS) — optional |
| 8088        | TCP        | Asterisk HTTP (ARI + WS). **Internal cluster only.** |
| 8089        | TCP        | Asterisk HTTPS (ARI HTTPS + WSS for browser softphones) |
| 8092        | TCP        | Call-engine control API (sidecar Python process, see `deploy/control_api.py`). **Internal cluster only**; reach it from outside via the `asterisk.velents.ai` ingress. |
| 10000-20000 | UDP        | RTP / RTCP media |
| 5038        | TCP        | AMI — internal only; **disabled by default** |

`hostNetwork: true` bypasses Docker `EXPOSE`; the table above is the
full network contract regardless.

## Volumes

| Path                                | Origin            | Purpose |
|-------------------------------------|-------------------|---------|
| `/etc/asterisk`                     | ConfigMap         | All `.conf` files. `*.conf.template` are rendered by entrypoint. |
| `/etc/asterisk/keys`                | Secret (read-only)| TLS cert + key for WSS / SIPS |
| `/var/spool/asterisk/recording`     | PVC or emptyDir   | MixMonitor output. DevOps cron syncs to S3. |
| `/var/spool/asterisk/voicemail`     | PVC               | Voicemail (deferred; module not enabled by default) |
| `/var/lib/asterisk/sounds/custom`   | ConfigMap or PVC  | Per-tenant prompts (`sound:custom/<id>`) |

## Env vars (entrypoint `envsubst`)

Files ending in `.conf.template` in `/etc/asterisk` are rendered to
`.conf` on container start with `envsubst`. The image ships **no**
template files by default; DevOps mounts them via ConfigMap.

Common vars to template:

| Var                          | Where it goes |
|------------------------------|---------------|
| `ARI_PASSWORD`               | `ari.conf` `[asterisk] password` |
| `ODBC_DSN` / `ODBC_USER` / `ODBC_PASSWORD` | `res_odbc.conf` |
| `EXTERNAL_MEDIA_ADDRESS`     | `pjsip.conf` `transport-udp` / `transport-wss` |
| `EXTERNAL_SIGNALING_ADDRESS` | same |
| `TLS_CERT_FILE` / `TLS_PRIVATE_KEY` | `http.conf` |

## Health checks

```yaml
livenessProbe:
  exec:
    command: ["asterisk", "-rx", "core show uptime"]
  periodSeconds: 30
readinessProbe:
  exec:
    command:
      - sh
      - -c
      - 'curl -fsS -u "$ARI_USERNAME:$ARI_PASSWORD" http://127.0.0.1:8088/ari/asterisk/info >/dev/null'
  periodSeconds: 10
```

## Module set

Both images ship the upstream default menuselect set with `BUILD_NATIVE`
disabled (so the binary is portable across CPUs). Verify the modules
the call-engine depends on:

```bash
docker run --rm velents/asterisk:dev asterisk -rx "module show like pjsip_transport_websocket"
docker run --rm velents/asterisk:dev asterisk -rx "module show like audiosocket"
docker run --rm velents/asterisk:dev asterisk -rx "module show like sorcery_realtime"
```

All three should be `Running`.

## Kubernetes notes (DevOps wires)

- **`hostNetwork: true`**. SIP/RTP-in-K8s without it is doable but ugly
  (UDP NodePort / external IP advertisement / `external_media_address`
  rewrites). For `≤ 1 k` registrations, hostNetwork is the standard.
- **StatefulSet, replicas=1**. PJSIP registrations live in memory;
  scaling out requires Kamailio / OpenSIPS in front.
- **ConfigMap subPath mounts** so DevOps can override individual files
  without disturbing the rest of `/etc/asterisk` (sounds + module dirs
  also live there).
- **External IP advertisement**: when the pod is behind NAT (cloud
  load balancer), set `EXTERNAL_MEDIA_ADDRESS` to the public VIP and
  use a `*.conf.template` to substitute it into the PJSIP transport.
- **TURN server**: browser softphones behind symmetric NAT need TURN.
  Run `coturn` as a sibling pod; not part of this image.
- **Recording PV size**: 16 kHz mono PCM at ~30 kB/sec → ~108 MB/h
  per concurrent call. Size accordingly + nightly S3 cron.

## What this image does NOT include

- K8s manifests / Helm chart — DevOps writes those against the contract
  documented above.
- Per-tenant sound packs — mount into
  `/var/lib/asterisk/sounds/custom/<tenant>/`.
- Voicemail / MeetMe / DAHDI / chan_sip — disabled by default; enable
  via menuselect overrides in a fork of `Dockerfile.prod`.
- TLS certificates — provided via Secret mount at `/etc/asterisk/keys`.

## Call-engine control API ingress (cross-cluster)

The call-engine runs as a Python sidecar process inside the Asterisk
pod (`deploy/control_api.py`, launched by `entrypoint.sh`). It binds
**TCP 8092** for `/control/*` and `/healthz`. It is a sensitive admin
surface: it will provision PJSIP trunks, disposition calls, drive the
dialplan over ARI, and expose operational telemetry. The current
checked-in version is a stub: `/healthz` works and bearer auth is
enforced on `/control/*`, but the real ARI/AMI plumbing for each
endpoint lands incrementally.

Required env in the pod spec:

| Var                   | Purpose |
|-----------------------|---------|
| `CONTROL_API_SECRET`  | Bearer secret for `/control/*`. Without it the sidecar replies 503 to `/control/*` (still serves `/healthz`). Must match `agent-hub`'s `CONTROL_API_SECRET`. |
| `CONTROL_API_PORT`    | Optional, defaults to 8092. |

To verify from inside the Asterisk pod:

```bash
curl -fsS http://127.0.0.1:8092/healthz
curl -fsS -H "Authorization: Bearer $CONTROL_API_SECRET" \
  http://127.0.0.1:8092/control/sip/trunks
```

agent-hub runs in our **GCP** cluster and has to reach this surface
cross-cloud over the public internet, because there is no
VPC-peering / Interconnect between the two clusters. The Next.js
server-side helper (`agent-hub:lib/cx/control-client.ts`) reads
`CALL_ENGINE_CONTROL_URL` (full base URL, no trailing slash) and sends
`Authorization: Bearer ${CONTROL_API_SECRET}` with every request.

### What DevOps needs to wire up

| Hop | Concern | Required setting |
|-----|---------|------------------|
| Public DNS | hostname | `asterisk.velents.ai` already CNAME's the AWS NLB / k8s ingress (Cloudflare grey-cloud, DNS-only — keep it grey-cloud so the bearer token isn't terminated at Cloudflare). |
| Ingress (AWS) | `Host: asterisk.velents.ai` matcher | Add a new path block for `/control/` and `/healthz` (in addition to whatever exists today on 80/443). |
| Ingress backend | service + port | Forward `/control/` and `/healthz` to the Asterisk pod on container port `8092` (the control-api sidecar inside the same pod). The Asterisk `Service` needs a port entry for 8092 → 8092. |
| Auth | bearer token | The app enforces this. The ingress only needs to pass `Authorization` through. Do **not** strip it. |
| TLS | scheme | HTTPS only on the public side. Internal hop ingress→call-engine can stay HTTP/8092 within the cluster. |
| Source-IP allowlist | scope | **Deferred (follow-up).** We are launching with bearer-only. Track adding `nginx.ingress.kubernetes.io/whitelist-source-range: <GCP-NAT-CIDR>` (or the equivalent ALB SG rule) as a follow-up before this surface widens beyond trunks. |

### agent-hub side (GCP)

Set in the agent-hub deployment env:

```
CALL_ENGINE_CONTROL_URL=https://asterisk.velents.ai
CONTROL_API_SECRET=<rotated-shared-secret>
```

Test from a GCP-side shell with curl:

```bash
curl -fsS -H "Authorization: Bearer $CONTROL_API_SECRET" \
  https://asterisk.velents.ai/healthz
curl -fsS -H "Authorization: Bearer $CONTROL_API_SECRET" \
  https://asterisk.velents.ai/control/sip/trunks
```

Both must return 200 from the GCP cluster's egress before the
`/dashboard/build/voip/trunks` page will render. If you see 502 / 504
in agent-hub logs (`call-engine unreachable at <url>` or
`call-engine timed out at <url> after 5s`), the network path is the
problem, not the app.

### Common gotcha

If `curl http://127.0.0.1:8092/healthz` from inside the Asterisk pod
returns `Connection refused`, the control-api sidecar isn't running.
The entrypoint now fails the container with a clear message in
`kubectl logs <pod>` whenever any of these is true:

| Log line | Meaning | Fix |
|----------|---------|-----|
| `entrypoint: FATAL: python3 not found on PATH` | Image was built before python3 was added (or a slimming step removed it). | Rebuild from `Dockerfile.dev` or `Dockerfile.prod`. |
| `entrypoint: FATAL: /usr/local/bin/control-api missing or unreadable` | Image is older than the sidecar, or a ConfigMap mount on `/usr/local/bin/` masked it. | Rebuild, or fix the mount path. |
| `entrypoint: FATAL: control-api did not bind 127.0.0.1:8092 within 10s` | Sidecar started but the python process exited before binding — usually a syntax error in `control_api.py` or a port collision (`hostNetwork: true` + something else on 8092). | Look for the `[control-api] ...` lines just above for the underlying error. |

If you instead see `[control-api] ... listening on 0.0.0.0:8092 ... ready`
followed by `entrypoint: control-api ready on :8092`, the sidecar is
healthy and any 502 you're seeing in agent-hub is downstream — usually
the cross-cluster ingress (see the table above). The sidecar is also
auto-restarted on crash so a transient panic doesn't permanently break
the listener.

### Endpoint contract: SIP trunks CRUD (stub)

The agent-hub `/api/cx/trunks` route helpers (`lib/cx/trunks.ts`,
`lib/cx/control-client.ts`) call into the call-engine for trunk
provisioning. The Python sidecar in this repo provides an
**in-memory** implementation so the agent-hub trunks page works
end-to-end against a freshly-deployed pod. Storage is process-local;
a pod restart wipes it. The real implementation will replace this
with PJSIP realtime writes (`ps_endpoints` / `ps_aors` / `ps_auths`)
and **must keep this exact wire contract** so agent-hub doesn't have
to change.

| Method | Path | Behavior |
|--------|------|----------|
| GET    | `/control/sip/trunks`       | `200` `{"items": [TrunkRow, ...]}` (NB: key is `items`, not `trunks`). |
| POST   | `/control/sip/trunks`       | `200` TrunkRow. **Upsert by `id`** — replaces if exists, `created_at` preserved. |
| GET    | `/control/sip/trunks/{id}`  | `200` TrunkRow, or `404` `{"error":"trunk not found"}`. |
| POST/PUT | `/control/sip/trunks/{id}` | Upsert; the URL `id` and (optional) body `id` must match. |
| DELETE | `/control/sip/trunks/{id}`  | `204` empty body, or `404`. |
| POST   | `/control/asterisk/reload`  | `200` `{"reloaded": false, "stub": true, "module": "..."}`. The real call-engine will exec `module reload` over AMI/ARI and flip `reloaded: true`. agent-hub already swallows failures here, so a 501 is harmless — but 200 keeps the network tab clean. |

Bearer auth (`CONTROL_API_SECRET`) on every `/control/*` route, same
as the existing surfaces. `/healthz` stays unauthenticated.

#### Request body (POST — camelCase)

Matches the body the agent-hub `lib/cx/trunks.ts::upsertTrunk`
helper sends today. Required: `id`, `displayName`, `serverUri`,
`username`. The rest are optional.

```jsonc
{
  "id":           "primary",                  // required, ^[a-zA-Z0-9_-]{1,60}$
  "displayName":  "PSTN Primary",
  "serverUri":    "sip:sip.example.com",
  "username":     "alice",
  "password":     "…",                        // stored, NEVER returned
  "provider":     "twilio",                   // optional
  "region":       "eu-central",               // optional
  "channelLimit": 50,                         // 1..1000, default 50
  "description":  "…",                        // optional
  "transport":    "udp",                      // udp | tcp | tls
  "context":      "from-trunk",
  "clientUri":    "sip:agent-hub.velents.ai",
  "fromUser":     "+15550000",
  "fromDomain":   "velents.ai",
  "expiration":   3600,                       // seconds, 60..86400
  "enabled":      true                        // default true
}
```

#### Response body (TrunkRow — snake_case)

Mirrors `lib/cx/trunks.ts::TrunkRow` minus `state` and `active_channels`
(those are decorated from Redis on the agent-hub side, not the
call-engine's concern). Reserved fields (`outbound_auth`, `identify_by`,
`allow`) are emitted as `null` from the stub; the real call-engine
populates them from PJSIP realtime row state.

```jsonc
{
  "id":             "primary",
  "display_name":   "PSTN Primary",
  "provider":       null,
  "region":         null,
  "channel_limit":  50,
  "description":    null,
  "transport":      null,
  "context":        null,
  "outbound_auth":  null,
  "from_user":      null,
  "from_domain":    null,
  "identify_by":    null,
  "allow":          null,
  "server_uri":     "sip:sip.example.com",
  "client_uri":     null,
  "expiration":     null,
  "username":       "alice",
  "enabled":        true,
  "created_at":     "2026-05-09T17:55:00Z",
  "updated_at":     "2026-05-09T17:55:00Z"
}
```

`password` is never present in any response. Don't add it back when
implementing the real version — agent-hub's `TrunkRow` interface
doesn't carry it either.

#### Error envelope

| Status | When |
|--------|------|
| 400    | Body isn't valid JSON / not an object. |
| 401    | Missing or wrong bearer. |
| 404    | Unknown trunk id on GET / DELETE / upsert-by-id. |
| 415    | `Content-Type` other than `application/json`. |
| 422    | Validation: missing required, bad `id` regex, bad `transport`, out-of-range `channelLimit` / `expiration`, URL/body `id` mismatch. |
| 503    | `CONTROL_API_SECRET` not set in the pod env. |

### Endpoint contract: flow analytics (spec 2.1.5)

The velentsAgents tenant API exposes per-flow IVR analytics under
`/FlowAnalytics/*` (see `app/FlowAnalytics/` in that repo). It does
**not** own the lifecycle event source-of-truth — it proxies and
transforms responses from the call-engine. The call-engine team
owns the four endpoints below, served by the same control-api
process documented above (bearer auth, same secret, same host).

All endpoints are **GET**, return `application/json`, and accept
the standard `Authorization: Bearer ${CONTROL_API_SECRET}` header
that velentsAgents already forwards. Multi-tenancy: velentsAgents
passes `?tenant=<tenant-id>` on every call so the call-engine
scopes its query (do not infer tenant from any other source).

| Method | Path | Query | Returns |
|--------|------|-------|---------|
| GET | `/control/flow-analytics/overview` | `from`, `to`, `tenant`, `flow_id?` | `{ kpis }` |
| GET | `/control/flow-analytics/flows` | `tenant` | `{ flows: [{ flow_id, name, is_active }] }` |
| GET | `/control/flow-analytics/{flow}/funnel` | `from`, `to`, `tenant` | `{ funnel, steps }` |
| GET | `/control/flow-analytics/{flow}/trend` | `from`, `to`, `tenant`, `granularity=day\|hour\|week` | `{ trend }` |

#### Response shapes

Stable envelope keys — additional keys may appear; consumers must
ignore unknown ones. `null` is permitted for percentile fields
when the sample size is too small. All durations are in
**milliseconds** for per-step dwell, **seconds** for whole-call
duration. Don't mix units.

```jsonc
// /overview
{
  "kpis": {
    "total_calls":            1234,
    "completed_calls":         987,
    "completion_rate_pct":      80.0,
    "abandoned_calls":         247,
    "avg_duration_seconds":    142.3,
    "p50_duration_seconds":    118,
    "p95_duration_seconds":    412
  }
}
```

```jsonc
// /{flow}/funnel
{
  "funnel": [ /* same shape as steps[]; ordered by flow position */ ],
  "steps":  [
    {
      "step_id":              "node_42",
      "label":                "Verify account number",
      "entered":              900,
      "completed":            812,
      "abandoned":             88,
      "abandonment_rate_pct":   9.8,
      "avg_dwell_ms":         3450,
      "p50_dwell_ms":         2900,
      "p95_dwell_ms":         9100
    }
  ]
}
```

```jsonc
// /{flow}/trend
{
  "trend": [
    {
      "bucket":                "2026-05-01",        // ISO date for granularity=day; ISO hour for hour; ISO Mon-of-week for week
      "total_calls":            128,
      "completed_calls":         99,
      "completion_rate_pct":     77.3,
      "avg_duration_seconds":   140.0
    }
  ]
}
```

```jsonc
// /flows
{
  "flows": [
    { "flow_id": "flow_abc123", "name": "Main IVR", "is_active": true }
  ]
}
```

#### Error envelope

velentsAgents converts every non-2xx into a clean 502/503 with a
short message. The call-engine should respond with:

| Status | When |
|--------|------|
| 401    | Missing or wrong bearer (velentsAgents will surface as 502) |
| 422    | `from > to`, unknown `granularity`, malformed `tenant` |
| 503    | Underlying event store unavailable (velentsAgents will retry twice with 200 ms backoff before surfacing) |

#### Out-of-scope (deferred for v2)

- CSV / XLSX export of any of the above.
- Period-over-period delta (`?compare=true`) — the agent-hub UI
  reserves the slot but velentsAgents will not pass it yet.
- Materialized rollup table on the velentsAgents side. Add only
  if dashboard latency exceeds ~500 ms p95.

## See also

- `configs/samples/README.call-engine.md` — install steps for a
  non-containerised host. The Asterisk-side docs there explain each
  config fragment we ship.
- `agent-hub/services/agenthub-call-engine/README.md` — companion
  service that talks ARI to this Asterisk.
