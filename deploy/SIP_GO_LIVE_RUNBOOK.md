# SIP go-live runbook — making inbound/outbound smooth

Audit-derived checklist for reliable SIP. Items are grouped by who must run them
and whether they need the live cluster to verify. **Shipped** items are already
merged on `claude/voip-cs-agent-friendly-i93uhq`; the rest are specced here
because they need a test environment, an ops action, or the external call-engine.

## Shipped (in-repo, verified by tsc)
- **Softphone auto-reconnect / re-register watchdog** — `agent-hub
  components/softphone/Softphone.tsx`. Backoff WS recovery + re-REGISTER when the
  socket is up but registration lapsed/failed. Verify: kill the agent's network
  ~60s while idle → it auto-re-registers and inbound rings without a refresh.
- **Outbound dial auto-retry** — `agent-hub components/cx-call/DialpadModal.tsx`.
  One retry on transport failure / 502-503-504; 4xx stay immediate. Verify: bounce
  the call-engine during a dial → the call still places on the retry.

## Ops / infra (cannot be done from the repos — need AWS + a deploy)
1. **RTP capacity (the ~2-call ceiling).** Today `rtp.conf rtpstart/rtpend` and the
   helm `serviceUDP.portRange` are `10000–10003`, because the AWS LB Controller
   creates **one SG rule per port** and the default quota is 60 rules/SG.
   - Raise the **EC2-VPC "Inbound or outbound rules per security group"** service
     quota (e.g. to 350+), OR re-architect to `hostNetwork: true` + a single
     UDP **port-range** SG rule (one rule covers the whole range).
   - Then widen **in lockstep**: `devops .../asterisk/values.yaml`
     `serviceUDP.portRange.end` and `asterisk configs/samples/rtp.conf.sample`
     `rtpend` to e.g. `10300` (~150 calls). They MUST match or Asterisk allocates
     ports the NLB won't forward (symptom: "connects, no audio").
   - Verify: place 5+ concurrent calls; all have two-way audio.
2. **TURN server (WebRTC media behind NAT).** STUN alone cannot work behind the
   NLB — Asterisk's only ICE candidates are the pod `host` (10.x) and a STUN srflx
   carrying the symmetric NAT-gateway egress IP, neither reachable by the browser —
   so every WebRTC agent gets SIP-OK + dead air, not just those on corporate NAT.
   The in-repo wiring is now **shipped**; the remaining work is the coturn deploy +
   AWS config.
   - Shipped: `deploy/entrypoint.sh` injects `turnaddr/turnusername/turnpassword`
     into `rtp.conf` from `ASTERISK_TURN_ADDR` / `ASTERISK_TURN_USERNAME` /
     `ASTERISK_TURN_PASSWORD`; `agent-hub lib/sip/provision.ts` already parses
     `ASTERISK_ICE_SERVERS` and (PR #687) passes it to JsSIP; `devops
     .../helm-charts/coturn/` chart added.
   - Ops: allocate an Elastic IP and pin it to the coturn NLB
     (`eip-allocations`); set it as coturn `external-ip` + the `turn.velents.ai`
     DNS A record. Mirror the coturn image to ECR. Create Secrets Manager
     `prod/coturn/env` (`TURN_PASSWORD=…`) and append the matching
     `ASTERISK_TURN_*` vars to `prod/asterisk/env` and `ASTERISK_ICE_SERVERS`
     (JSON array incl. the `turn:` URL + credentials) to `prod/agent-hub/env`.
   - TLS (5349) is deferred — needs a server cert; `turn:…?transport=tcp` covers
     TCP-only networks meanwhile.
   - Verify: an agent on a mobile hotspot / locked-down LAN gets two-way audio;
     `chrome://webrtc-internals` shows a `relay` candidate and ICE `connected`.
3. **K8s health probes + ARI health.** `devops values.yaml` has empty probes;
   `control_api.py /healthz` only checks bind, not ARI reachability.
   - Add liveness/readiness/startup probes; make `/healthz` confirm ARI is
     reachable so a half-up pod leaves the LB pool.
4. **External-media-IP drift monitor.** Add a CronJob comparing the NLB IP to
   `ASTERISK_EXTERNAL_MEDIA_ADDRESS`; patch the ConfigMap + roll on mismatch.

## Asterisk provisioning (needs a live Asterisk to verify before merge)
In `asterisk deploy/control_api.py` endpoint provisioning + `Dockerfile.prod`:
- **opus**: add `opus` to the agent endpoint `allow` AND `menuselect --enable
  codec_opus` in the build. ⚠️ Do these together — advertising opus without the
  codec compiled can break currently-working alaw calls. Verify
  `core show codecs | grep opus`, then a WebRTC↔PSTN call.
- **`rtp_timeout=30` / `rtp_timeout_hold=300`** on endpoints — kills one-way /
  zombie media. Confirm the realtime `ps_endpoints` schema has the columns first.
- **Session timers** (`timers=yes; timers_sess_expires=1800`) — keeps long-call
  NAT pinholes open / satisfies carrier re-INVITE.
- **Per-trunk `media_encryption` (SRTP/SDES) + TLS `verify_server`** and outbound
  **trunk failover** Dial — carrier-dependent; test per carrier.

## App-side reliability backlog (in-repo, but needs a test env — state-machine/webhook risk)
- **Registration-gated routing**: expose a real `sipStatus` (from PJSIP
  `ps_contacts` / the contact-state Redis keys `control_api.py` already tracks) on
  `/api/cx/agents/{id}`; `[call-engine]` then skips unregistered agents. Stops
  calls ringing dead endpoints.
- **Event-bus resync on reconnect**: add `GET /api/cx/calls/active` (velentsAgents)
  and have `agent-hub AgentSoftphoneShell` re-fetch the current assignment after a
  WS reconnect (otherwise a mid-call reconnect orphans the ACW uuid).
- **Zombie-call sweep**: extend `CallsExpiresInJob` to move calls stuck in
  `dialing`/`connecting` past a threshold → `timed_out` (both are ALLOWED
  transitions in `CallStatusManager`; do NOT force `processing`/`in_conversation`,
  which are legit long-running). Confirm the `calls` timestamp column first.
- **CallGateway Release retry**: retry the dispatcher `Release()` on failure
  (`Integration/InBound/CallGateway/Controllers/CallGateway.php` + the `http` base
  client's retry) so a failed release doesn't leak a reservation.
- **Webhook idempotency**: dedupe duplicate success callbacks in
  `CallStatusManager` — requires an idempotency key from the `[call-engine]`.

## Call-engine items (external service — not in these four repos)
ARI Stasis reconnection loop; the routing decision that consumes `sipStatus`;
outbound trunk **selection**. Build the in-repo half (data, endpoints, config) and
hand these off to the call-engine.

## Trunk failover & DR posture (AGH-7262)

**State of play: failover-READY, not automatic.** No secondary trunk or carrier is
contracted, so nothing consults trunk health at originate time and no code path
switches carriers on its own. Everything below is either shipped detection or a
procedure a human runs. Do not read `priority` on a trunk as "the system will fail
over" — it records the intended order for the manual procedure and nothing more.
When a second trunk exists, build the deferred design at the end of this section
and delete this paragraph.

### What detects a problem, and where it surfaces

| Layer | Mechanism | Where it shows |
|---|---|---|
| Reachability sweep | `control_api.py` status feeder, every ~5s: `pjsip show endpoints/identifies/aors` → `cx:trunks:status:{tenantId}` + `cx:trunks:checked_at` | Trunk health card, trunks list |
| Staleness | `trunkPosture()` demotes a `connected` posture to `degraded` when the last sweep is older than the freshness window | Health card badge |
| Drop / restore edge | Feeder POSTs `Webhook/CallEngine/TrunkStatusChanged` (bearer `CALL_ENGINE_WEBHOOK_SECRET`) | Audit log (`TRUNK_CONNECTION_LOST` / `_RESTORED`), in-app notification to Owners+Admins, Owner email when `TRUNK_ALERT_MAIL_ENABLED` |
| Outbound attempt while down | Server-side guard on the API path; posture pre-check on the header dialpad | Toast to the agent, not a failed call |
| Inbound while the engine is down | `[from-trunk]` plays an all-circuits-busy treatment instead of falling to `Hangup()` | The caller hears something |

**The honest boundary.** When the trunk's own REGISTER lapses, inbound INVITEs never
reach us — what the caller hears is the carrier's, and no configuration here changes
it. The treatments above cover the cases we do control: engine down, flow crash,
unmapped DID, outbound leg failure. Say this plainly to anyone reading AC-1.1.4 as a
promise about the dead-air case.

### Manual switch-over procedure

Preconditions: a second trunk exists in Settings → CS Agent → Trunks, is `enabled`,
and has been proved with a test call (see step 5 — prove it BEFORE you need it).

1. **Confirm the failure is the trunk.** Health card posture `disconnected`, and the
   audit log shows `TRUNK_CONNECTION_LOST` for the trunk you expect. A `degraded`
   posture with a stale `checked_at` means the *feeder* is down, not the trunk —
   check the Asterisk pod before touching carrier config.
2. **Raise the standby's precedence.** Set the standby trunk's priority below the
   failed one's (lower = preferred). This is recorded as `TRUNK_CONFIG_CHANGED` in
   the audit log; it does not itself move any traffic.
3. **Repoint outbound.** Outbound trunk choice comes from the tenant's
   `outbound_rules` rows, each naming a `trunk_endpoint` — priority is not consulted.
   Update the matching rules to the standby's endpoint id. Until this step, outbound
   still tries the dead trunk.
4. **Inbound is the carrier's to move.** Either the carrier re-points the DID at the
   standby, or both trunks register and they load-share. There is no switch on our
   side; if the numbers are contracted to a single carrier, inbound stays down until
   they act. Know this before the incident, not during it.
5. **Prove it.** One inbound call to a DID that reaches routing, one outbound call
   through the standby. Confirm the health card reads `connected` and the audit trail
   shows the config change and the restore.
6. **Failback** reverses steps 2–3 after a `TRUNK_CONNECTION_RESTORED`. Do not
   failback on a single restore event — wait out one full staleness window so a
   flapping registration does not move traffic twice.

### RPO / RTO

- **Detection:** ≤ ~5s (feeder sweep) + one webhook round trip.
- **RTO, outbound:** human-bound — the `outbound_rules` edit and its verification
  call. There is no automatic path; budget minutes, not seconds.
- **RTO, inbound:** carrier-bound, and outside our control entirely.
- **RPO, call records:** call rows and events are written per transition, so at most
  the in-flight transition is lost.
- **RPO, recordings:** see the recording pipeline's upload queue — a storage outage
  queues uploads and alerts rather than writing out-of-region, so the exposure is
  the un-uploaded tail on the Asterisk pod's spool. That pipeline, not this section,
  owns the guarantee.

### Deferred design — build this when a second trunk is contracted

- `outbound-router` consults trunk health before returning a rule, and retries the
  next-priority enabled trunk when placement fails. Today it returns the first
  matching enabled rule regardless of reachability.
- A `trunk.failover.switched` event on the bus so the switch is visible in the
  supervisor surfaces and the audit trail without a config diff.
- Inbound: dual-REGISTER both trunks, or a documented carrier DID re-point SLA.
- Provider-IP-allowlist carriers (Twilio/Telnyx style, no REGISTER) are out of the
  current trunk model — `ps_identify` has no writer here. Adding a non-registering
  carrier is a prerequisite change, not a config change.

### Go-live gates that invalidate a failover test until fixed

- **RTP range must move in lockstep** across `configs/samples/rtp.conf.sample` and
  both helm values files (§Ops-1). A failover test that "passes" while the range
  caps concurrency at a couple of legs has proved nothing about capacity.
- **opus** must be enabled in the build *and* the endpoint allow-list together, or
  neither (§Asterisk provisioning) — a half-applied change breaks working alaw calls.
- **K8s probes + an ARI-aware `/healthz`** (§Ops-3), or a half-up pod stays in the
  load-balancer pool and looks like a trunk problem.
- A DNS resolution check on the trunk's `serverUri` belongs in the health sweep: a
  provider-supplied hostname that NXDOMAINs presents exactly like a dead trunk, and
  cost real debugging time before. Not built; worth adding with the first
  non-registering carrier.
