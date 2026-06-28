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
2. **TURN server (WebRTC media behind NAT).** Only STUN is configured. Agents on
   corporate/symmetric NAT get SIP-OK + dead air.
   - Deploy coturn (devops helm) with a public IP on 3478/UDP+TCP (and 5349/TLS).
   - Wire it into `asterisk configs/samples/rtp.conf.sample`
     (`turnaddr/turnusername/turnpassword`) AND `agent-hub` env
     `ASTERISK_ICE_SERVERS` (a JSON array incl. the `turn:` URL + credentials —
     `lib/sip/provision.ts` already parses it).
   - Verify: an agent on a mobile hotspot / locked-down LAN gets two-way audio.
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
