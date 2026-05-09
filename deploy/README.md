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
Check `kubectl logs <pod>` for `control-api ... listening on
0.0.0.0:8092`. Most common causes: the image was built before the
sidecar was added (rebuild with `Dockerfile.prod` or `Dockerfile.dev`),
or `entrypoint.sh` was overridden by a ConfigMap mount that masked
`/usr/local/bin/control-api`.

## See also

- `configs/samples/README.call-engine.md` — install steps for a
  non-containerised host. The Asterisk-side docs there explain each
  config fragment we ship.
- `agent-hub/services/agenthub-call-engine/README.md` — companion
  service that talks ARI to this Asterisk.
