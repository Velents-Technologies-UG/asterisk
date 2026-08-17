# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is a full upstream Asterisk source tree (build system, `main/`, `res/`, `channels/`, etc.) with Velents-specific deployment and dialplan config layered on top. **Don't treat this as "our" codebase to refactor** — almost everything under the repo root is stock upstream Asterisk; the Velents-owned surface is narrow:

- `configs/samples/` — a handful of Velents-authored `.sample` files mixed in among ~120 stock upstream samples (see below for which is which).
- `configs/basic-pbx/` — a hand-built example single-tenant PBX config.
- `deploy/` — Dockerfiles, `entrypoint.sh`, and a Python control-API sidecar (`control_api.py`, `sip_store.py`) with its own detailed `deploy/README.md`.

The root `README.md` is the **stock upstream Asterisk README** — build/install instructions for Asterisk itself, nothing Velents-specific. For the actual deployment contract (ports, volumes, env-var templating, K8s notes, the `control_api.py` sidecar's full CRUD contract for PJSIP trunks), read `deploy/README.md`. For the call-engine-side dialplan wiring specifically, read `configs/samples/README.call-engine.md`.

### Which control API is live: `control_api.py` (Python) vs. call-engine's `control-api.js` (Node) — RECONCILED 2026-08-17

Both exist and both are live, but they are **not** duplicates competing for the same job. They overlap on exactly one surface — `/control/sip/*` — and there the Python sidecar wins.

| | `deploy/control_api.py` (this repo, Python) | `call-engine/src/control-api.js` (Node) |
|---|---|---|
| Runs in | the **Asterisk pod**, as a supervised sidecar | the **call-engine pod** |
| Started by | `deploy/entrypoint.sh` (`supervise_control_api`, lines ~238-285) — unconditional, pod fails readiness if it doesn't bind | `src/index.js`, only when `CONTROL_API_SECRET` is set |
| Port | 8092 (`CONTROL_API_PORT`) | 8092 (`CONTROL_API_PORT`) — same default, different pod |
| SIP surface | trunks + **providers** + **trunk-accounts**, all tenant-scoped; `originate`; agent credentials; `asterisk/reload` | trunks + agent credentials only, **no tenant scoping** |
| Call ops | none | transfer / conference / hold / consult / record / snoop / MOH — **only implementation, not duplicated anywhere** |

**Verdict: `control_api.py` is the live SIP-trunk CRUD path.** Evidence, in descending strength:

1. `CALL_ENGINE_CONTROL_URL=http://asterisk:8092` — the **`asterisk`** service, not `call-engine` — in both `devops:aws/velents/prod/helm-charts/agenthub-call-engine/values.yaml:71` and `devops:aws/velents/RUNBOOK-agenthub-call-engine.md:55`.
2. agent-hub's caller (`agent-hub:lib/cx/control-client.ts`) names the Python module in its own comments ("`control_api` refuses these routes with 401 if no `X-Tenant-Id` arrives") and stamps `X-Tenant-Id` — which **only** the Python one reads (`control_api.py:1210-1230`). Send an agent-hub trunk request to the Node one and tenant scoping silently vanishes.
3. Prod has **no `call-engine` chart at all** (only `agenthub-call-engine`, and that deployment was deleted — see the topology note in memory). Prod's `asterisk` chart is what exposes 8092.
4. `deploy/README.md` already documents this sidecar as the trunks CRUD contract end-to-end (`## Call-engine control API ingress`, `#### Endpoint contract: SIP trunks CRUD`).

**So**: treat `control-api.js`'s `/control/sip/*` block + `sip-store.js` as **legacy/superseded** — retiring them is a `call-engine`-repo task, not this repo's. The rest of `control-api.js` (call ops) is load-bearing and must not be touched. In **test**, `devops:aws/velents/test/helm-charts/call-engine/values.yaml` does expose 8092 as `control-api` on the call-engine pod, so a test-env `CALL_ENGINE_CONTROL_URL` pointed there would reach the tenant-blind Node implementation. That value comes from a secret and isn't in the devops repo — **verify it per environment before debugging a "trunk saved to the wrong tenant" report.**

**Bonus, closes an open question in the devops repo**: `devops:aws/velents/test/helm-charts/asterisk/values.yaml:169-173` records "Something HTTP answers on 8092 in the prod image — it returns 404 rather than refusing the connection — but it is not ARI." That something **is this sidecar**; it 404s every non-`/control/` path by design (`control_api.py:1129-1130`). Prod's chart mislabels the port `http-ari` and routes `asterisk-ari.velents.ai` → 8092, which therefore cannot serve ARI at all — real ARI is 8088 only, via `asterisk.velents.ai`. DevOps-owned fix; flagged in the TODO below.

## Velents-authored dialplan (`configs/samples/extensions_ai_runtime.conf.sample`)

The call-engine service's own dialplan. Contexts: `[ai-runtime]` (AudioSocket bridge target), `[call-engine-test]`, `[from-flows]` (visual flow runner entry), `[from-trunk]` (inbound from PSTN/SIP trunks), `[from-agents]` (ring a staff member's PJSIP endpoint — inbound ring target only), `[from-wss-agents-out]` (WSS softphone outbound dial), `[from-trunk-out]` (the actual PSTN/trunk leg for a human-agent outbound call). See `call-engine`'s own CLAUDE.md for how each context gets driven from `src/ari.js`/`src/transferer.js`.

### Two confirmed, non-obvious Asterisk pattern-matching gotchas (2026-08-17)

Both cost significant debugging time this session — check for these before trusting *any* new dialplan pattern here:

1. **`X`/`Z`/`N` — and their lowercase equivalents — are digit-class wildcards ANYWHERE in a pattern, not just when clearly intended as one.** A pattern like `_agent_.` looks like "literal `agent_` + wildcard", but the `n` in "agent" is silently reinterpreted as `[2-9]`, meaning the pattern **never matches the literal word "agent" at all**. Confirmed via isolated `dialplan add extension`/`dialplan show` testing in a scratch context (bypassing the real config, ruling out file corruption/caching/full-restart-staleness first): `flow`/`staf`/`hello` all matched fine as literal prefixes in the same position; `agent`/`xyz` never did, for exactly this reason. **Fix: wrap the offending letter in a single-character class** — `[n]` — which forces literal matching: `_age[n]t_.` (see `[from-agents]`). Check any literal-looking prefix for `x`/`z`/`n` (either case) before assuming it'll match as written.
2. **`X` alone (uppercase, the normal "any digit" wildcard) does not match a leading `+`.** A bare `_X.` pattern silently rejects any E.164-formatted string like `+201121750740` with "extension not found" even though it's a perfectly valid number — `X` only covers `0-9`. Fix: add a parallel `_+X.` pattern with the identical body (see `[from-trunk]`, `[from-wss-agents-out]`, `[from-trunk-out]` — each has both). Both the `_X.`-only and `_agent_.`-style bugs above independently broke **outbound dialing to E.164 numbers** and **every feature that rings an agent by Local-channel origination** (transfer, conference, consult, snoop, queue-routed inbound) respectively — check both when a Local-channel-originated call reports `core_local.c: No such extension/context ... while calling Local channel`.

## TODO / open items (as of 2026-08-17)

- [x] ~~Reconcile `deploy/control_api.py` vs. `call-engine`'s `src/control-api.js`/`src/sip-store.js`~~ — **done 2026-08-17**, see the reconciliation table above. Python sidecar is live for SIP CRUD; Node's `/control/sip/*` is superseded; Node's call ops are the sole implementation and stay.
- [x] ~~`configs/samples/README.call-engine.md` references a non-existent `ari_call_engine.conf.sample`~~ — **fixed 2026-08-17**. The content it described is `ari.conf.template` (the file `entrypoint.sh` actually renders); `ari.conf.sample` beside it is stock upstream. Same pass fixed the dead `agent-hub/services/agenthub-call-engine` paths in that doc — the service is its own `call-engine` repo now.
- [ ] **Retire the superseded SIP half of `call-engine`** — `src/control-api.js`'s `/control/sip/*` routes + `src/sip-store.js`. Not this repo's change; needs doing in `call-engine` so the tenant-blind path can't be reached by a mis-set `CALL_ENGINE_CONTROL_URL`. Until then, confirm that env var per environment.
- [ ] **DevOps: prod's `asterisk` chart mislabels port 8092 as `http-ari` and routes `asterisk-ari.velents.ai` at it.** 8092 is the control-api sidecar and 404s every `/ari/` path; real ARI is 8088 (`asterisk.velents.ai`). Either drop that host or repoint it at 8088, and rename the port entry (`devops:aws/velents/prod/helm-charts/asterisk/values.yaml:96-98,212-220`). The test chart already documents and deliberately avoids this (`.../test/helm-charts/asterisk/values.yaml:150-177`).
- [ ] **`deploy/README.md`'s control-api section understates what's shipped** — partially corrected 2026-08-17 (the "current checked-in version is a stub" claim was false: 1661 lines, full trunk/provider/account CRUD). Still worth an end-to-end pass: it documents the ingress as `asterisk.velents.ai` + `/control/` path blocks, but prod actually fronts it with a whole separate host (`asterisk-ari.velents.ai` → 8092, path `/`).
- [ ] Voylo's own outbound-trunk-creation wizard can generate a SIP URI whose hostname doesn't resolve in DNS at all — **`sip.uae.voylo.ai` still NXDOMAIN as of 2026-08-17** (third independent check; `voylo.ai` itself resolves fine to 167.172.191.79). This is persistent provider misconfiguration, not a transient. `ps_endpoints.outbound_proxy` (`call-engine` migration `0008`) is the standing workaround: route to the provider's known-good IP while keeping their expected hostname in the actual SIP request. Apply it directly for any new Voylo trunk rather than debugging DNS again.
