# Worker 2 (call-engine) — architecture and data-ownership design

**Version:** v0.1 — new doc, written from the design discussion at the end of the same session
documented in `asterisk-deployment-test-suite-and-feature-gap-plan.md`, immediately after a real
end-to-end inbound-call test finally located and exercised the service in question.

**Published:** https://claude.ai/code/artifact/8e707b9d-e432-4c27-b659-443634e46ed8

## Why this doc exists

The feature-gap plan had an open question for weeks: where does the ARI/Stasis "call-engine" that
every Asterisk dialplan hook routes into actually live? A real end-to-end call test this session
finally answered it — it's `call-engine`, deployed from the `Velents-Technologies-UG/
agent-hub` GitHub repo. That answer immediately raised a bigger one: `agent-hub`'s own `main`
branch is a Next.js **frontend**, with no trace of this backend service at its root. That mismatch
— and the fact that more clients (not just `velentsAgents`) will eventually want to use the same
call-center feature — is what this doc works through: what the service actually does, where its
logic should live, and how its data should be owned as the platform grows past a single backend.

---

## Part 1 — what `call-engine` does on an inbound call

In plain terms: it's the always-on process that Asterisk hands every live call to, and which
decides what happens to that call.

Asterisk's own dialplan (`from-trunk` context) does almost nothing itself — it just recognizes an
inbound call and hands the channel off:

```
NoOp(inbound ${CALLERID(num)} -> ${EXTEN})
Set(__CALLED_DID=${EXTEN})
Stasis(call-engine, inbound, ${EXTEN})
Hangup()
```

`Stasis(call-engine, ...)` routes the channel to whichever external client has registered itself as
the ARI Stasis application named `call-engine`. That's `call-engine` — confirmed directly
from its own boot log this session:

```
ARI connected url=http://asterisk:8088 app=call-engine
ARI Stasis app 'call-engine' subscribed and ready
```

### The sequence, step by step (grounded in this session's actual logs, not inferred)

1. **`StasisStart` arrives**, carrying the call's args (`"inbound"`, the dialed DID). Confirmed:
   `Flow StasisStart customer=... flow=forward-to-personal-test caller=voylo_locat
   did=+966115030505 source=inbound`.
2. **DID resolution** — an HTTP call to the central DID registry (today, `velentsAgents`'
   `/ML/Did/Resolve/<DID>`) to find which tenant owns the number and which Flow or queue handles
   it. Confirmed: `inbound DID '+966115030505' -> tenant=testCallCenter
   flow=forward-to-personal-test`.
3. **Branch: Flow vs. Queue.** Two resolvers are wired at startup — one for DIDs mapped to a
   Flow (a scripted node-graph), one for DIDs mapped straight to a live human-agent ACD queue.
   The DID registry row decides which path a call takes.
4. **Flow execution** (for a flow-mapped DID) — `flow-runner.js` loads the tenant's Flow (a JSON
   node graph, stored per-tenant) and walks it from `entryNodeId`. Node types it supports: `start`,
   `play`, `dtmf_collect`, `queue_enter`, `hangup`, `set_var`, `if`, `webhook`, `transfer`,
   `voicemail`, `business_hours`, `bridge_ai`.
5. **Transfer execution** — `transferer.js` handles hand-off to `agent_id`, `extension`, or `e164`
   targets, issuing an ARI dial/originate and bridging once answered.
6. **Call-state tracking** — a lifecycle state machine (`callstate (none) -> INCOMING`, etc.),
   intended to feed call-event persistence. In the current deployment this is disabled
   (`DATABASE_URL not set; call_events writes will no-op`, and the Laravel HTTP fallback is also
   unconfigured) — so nothing is actually persisted right now, only logged.

### Where it broke, precisely (this session's live finding)

~600ms after `StasisStart`, before the transfer completed: `flow run failed (inbound
forward-to-personal-test): "Channel not found"` → immediate teardown. That's an ARI command
(almost certainly the flow's first real action on the channel) being rejected because the channel
had already stopped existing on Asterisk's side — i.e. one leg dropped before the flow's first
action landed. A timing/race bug internal to the flow-runner, not the dialplan or PJSIP layer.

### Other subsystems wired at boot (not exercised by this session's test, but part of the same
process)

- `Queue dispatcher wired (ACD enabled)` — human-agent queue routing, the parallel path to Flows.
- `Outbound router wired (human dial-plan rules enabled)` — agent-initiated outbound calls.
- `AudioSocket stub listening on 0.0.0.0:8090` + a loaded sample WAV — backs the Flow's `bridge_ai`
  node type; per the feature-gap plan, still just a 3-second-tone placeholder, not a real AI-audio
  bridge.
- Redis (`Redis ready`) — pub/sub, likely for pushing live call state to a frontend; a `gateway`
  component is explicitly disabled in this deployment (`"gateway":"disabled"`).
- Its own internal Control API on `127.0.0.1:8092` (Node) — a *different* API from `deploy/
  control_api.py`'s trunk-CRUD API (which runs inside the `asterisk` pod, on the same port number
  coincidentally). This one exposes agent-facing mid-call actions like `/control/calls/:uuid/
  transfer`.

### Functionality and API summary

**Core functionality:** ARI client and Stasis-event handling; DID→owner resolution; Flow-script
execution; queue/ACD dispatch (alternate path); transfer/bridge execution; call-state/event
logging (currently not persisting); agent-facing mid-call control; a stubbed AI-audio bridge hook.

**Outbound API calls it makes:**
- `GET .../Did/Resolve/<number>` — who owns this number, what handles it (central directory).
- `GET .../Flows/<flow_id>` — the tenant's actual call script (per-tenant data).
- `POST` call-event/CDR writes — record what happened (currently unconfigured, no-ops).
- Whatever a Flow's `webhook` node points to (varies per flow, external).
- ARI commands to Asterisk itself (answer, dial, bridge, play, hang up).

**Inbound API it exposes:**
- `/control/calls/:id/transfer` (and likely hold/hangup) — so an agent's screen can control an
  in-progress call.

---

## Part 2 — the architecture mismatch

`agent-hub`'s own `package.json` (root, `main` branch) is a plain Next.js frontend: `next dev` /
`next build` / `next start`, dependencies like `@dnd-kit` and `@fullcalendar` — no ARI client, no
WebSocket server, nothing resembling `call-engine`. Yet the deployed
`call-engine` Kubernetes pod is a pure backend telephony service with no UI code at all.

One real clue from the repo's own history: a commit titled **"refactor(voice): remove Asterisk/
Twilio telephony runtime from the UI."** That strongly suggests this backend used to live bundled
inside the frontend app and was deliberately split out at some point — but the currently-deployed
Jenkins build checks out a different branch (`claude/innov2-sip-auth-error-pjgbng`, not `main`), so
it's unclear whether that split was ever fully finished or pointed at a real, permanent destination.
Also relevant: other branch names in that repo's history (`feat(engine): real transfer/consult/
conference call-control primitives`, `fix(call-engine): connect gateway Redis subscriber before
psubscribe`, `feat(cxm): trunk health card, outbound-down guard, DID flow|queue + inbound test`)
show substantial, ongoing work on exactly this call-engine domain — just not on `main`, and not in
a location that maps cleanly onto "this is a frontend repo."

---

## Part 3 — tech-stack comparison for where the live call-event loop should run

"Worker 2" is really two different kinds of work bundled together:

1. **Provisioning/lookup APIs** — "who owns this number," "what's the flow," CRUD for DIDs/Flows/
   trunks. Normal request/response REST work.
2. **The live call event loop** — holding an open ARI connection, reacting to a call in real time,
   running the flow with sub-second timing sensitivity (the exact race that's currently broken).
   This needs a long-running, event-driven process — which is *why* the current implementation is
   Node.js, not PHP.

The comparison below is specifically for #2 — the live event loop:

| Stack | Performance | Effort | Est. time | Key risks |
|---|---|---|---|---|
| **Node.js — relocate current code** to its own dedicated repo/service (extract the existing `call-engine` logic out of `agent-hub`, keep as-is) | Proven live this session — ARI connect, Stasis subscribe, DID resolve, and Flow load all worked correctly against real production traffic. Node's event loop is a natural fit for many concurrent call state machines. | **Low** — no rewrite, mainly repo extraction, CI/CD setup, and fixing the one known bug (the "Channel not found" race) | **1–2 weeks** (mostly the race-condition fix + repo/deploy hygiene) | Inherits whatever else is unfinished/undocumented in the current code (e.g. disabled DB writes, disabled gateway) — those need auditing, not just the one bug |
| **Node.js — clean rewrite** as its own service (same language, fresh architecture) | Same ceiling as above, but cleaner internals (proper DB writes, tests, no legacy cruft) | **Medium-High** — full rewrite of ARI client, flow-runner, transferer, DID resolver | **4–8 weeks** | Re-introduces bugs already solved once; hard to justify unless the current code is worse than it looks from outside |
| **PHP + Laravel Octane (Swoole)** inside `velents_integrations` | Swoole gives PHP a real async event loop — technically capable, but Asterisk ARI's PHP ecosystem is small and far less battle-tested than Node's | **High** — adds a new runtime mode (Octane) to a repo that's never run it; ARI client, flow engine, transfer logic all built from scratch in PHP | **6–10 weeks**, plus Octane operational learning curve (memory leaks, worker restarts are common early pain points) | Team likely has little/no Octane experience; small ARI-for-PHP community means fewer examples/support when something breaks in production |
| **PHP + custom ReactPHP daemon** (`artisan call-engine:listen`, no Octane) inside `velents_integrations` | Workable async I/O without Swoole, but hand-rolled reconnect/event handling — more fragile than a mature framework | **High** — same from-scratch build as above, plus maintaining your own daemon supervision (restart-on-crash, health checks) instead of relying on Octane's | **6–9 weeks** | Bespoke infrastructure = more edge cases to discover the hard way (this session alone found 4 subtle Asterisk-side bugs; a hand-rolled PHP daemon adds its own class of subtle bugs) |
| **Python (`ari-py` + `asyncio`)** as a new dedicated service | Solid async support, but ARI-for-Python is a smaller, less actively maintained ecosystem than Node's | **High** — full rewrite, new language for this domain, no existing code to build from | **6–10 weeks** | Smallest community of these options for ARI specifically; least reason to pick it unless the team has strong Python-only preferences |

### Recommendation

**Don't rewrite — relocate.** The current Node.js code got 90% of the way there *live, this
session, against production Asterisk*: it correctly connected to ARI, resolved the DID, loaded the
right Flow, and only failed on one specific timing bug in the transfer step. That's a small,
fixable bug in working code, not evidence the stack is wrong. Rewriting it in PHP/Octane or Python
to satisfy "it should live somewhere else" would mean throwing away something that already works to
rebuild it in an ecosystem with less Asterisk/ARI tooling support than what exists today.

---

## Part 4 — data-ownership design (decided)

### The trigger for this discussion

`velents_integrations` will eventually be used not just by `velentsAgents` (for `velents`-branded
tenants), but by other backend services for other clients (e.g. `safha`), all sharing the same
call-center feature. That means deciding, per piece of data: does it belong in each client's own
tenant DB (owned by whichever backend serves that client), or in a shared, backend-agnostic
telephony config store?

### Precedent already in place

Two Postgres databases already exist, both incidentally named `velentsagents`, on two different RDS
instances — a real, working example of roughly this split, whether or not it was planned that way:

- **PROD instance** — `sip_trunks`, `ps_endpoints`, `ps_auths`, `ps_aors`, `ps_identify` — realtime
  PJSIP config Asterisk reads directly. Written by `deploy/control_api.py`.
- **NONPROD instance** — `did_registry`, `flows`, `queues`, `tenants` — tenant/business data, owned
  by `velentsAgents`' Laravel app.

### The decision rule

*Does Asterisk/call-engine need to read this at call-time, fast, without depending on a specific
tenant's backend being up?*
- **Yes** → shared, backend-agnostic telephony config, exposed through `velents_integrations`.
- **No, it's about the tenant's business, not the phone-call routing** → stays in that tenant's own
  backend/DB (`velentsAgents`, `safha`'s BE, whoever).

### The split

**Shared telephony-config data (owned by `velents_integrations`, read by Asterisk/call-engine):**
- DID registry — number → tenant + which Flow/queue handles it. Has to be a single global
  namespace regardless of client, since two different products can't both claim the same number.
- SIP trunk/endpoint/identify config — already the case today.
- The **current published** version of each Flow — just the runtime-executable definition, not
  authoring history.
- Queue definitions needed for live routing (agent availability/skill data), or at minimum a
  synced, low-latency copy.
- Call-detail/event write path, so recording/monitoring works the same regardless of which backend
  owns the tenant.

**Tenant's own DB (owned by `velentsAgents`, `safha`'s BE, etc.):**
- Everything about *authoring* a Flow — drafts, versions, who edited what, unpublished changes.
- Agent profiles, business rules beyond simple routing, CRM/customer context, conversation
  history, analytics, billing.
- Anything gated by that tenant backend's own auth/permissions model.

### The synced-copy model (decided, with refinements)

For Flows specifically (and by extension anything else in the shared store that a tenant backend
authors): **push a synced copy on publish**, rather than call-engine fetching live from whichever
backend owns the tenant, or a webhook/polling scheme.

- **Trigger**: explicit — when a tenant backend marks something as published, it calls a
  `velents_integrations` write endpoint right then (e.g. `POST /telephony/flows/{tenant}/
  {flow_id}`). Not a webhook subscription, not polling — publishing is already a deliberate,
  low-frequency action.
- **Read path**: call-engine reads through `velents_integrations`' API, **not** the shared DB
  directly. This is the whole point of centralizing this — validation and constraints get enforced
  in one place, not re-implemented by every consumer.
- **Constraints `velents_integrations` enforces on every write**: DID uniqueness *globally* across
  every tenant/client, not just per-backend; Flow schema validation (valid node graph, no dangling
  references); a DID's referenced Flow must exist and be published before the mapping is accepted.
- **Failure handling**: call-engine is never blocked by a tenant backend being unreachable — worst
  case, it keeps serving the last successfully synced copy until the next publish succeeds. This is
  the specific property a synced-copy model buys over a live-fetch model: one client's backend
  outage can't take down another client's live calls.

### Why synced-copy over live-fetch

- **Live-fetch** (today's model — call-engine calls `velentsAgents`' API at call-time) is simpler,
  always current, but couples call-time reliability to every backend's uptime — exactly the
  cross-service dependency this design is trying to avoid by centralizing telephony config.
- **Synced copy** costs an extra publish-time push and a brief window of possible staleness right
  after a publish, in exchange for call-time reads never depending on a specific tenant backend
  being reachable. Given the whole reason for this design is supporting multiple independent
  backends sharing one call-center feature, this tradeoff was the deciding factor.
