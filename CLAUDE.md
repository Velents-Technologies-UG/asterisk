# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is a full upstream Asterisk source tree (build system, `main/`, `res/`, `channels/`, etc.) with Velents-specific deployment and dialplan config layered on top. **Don't treat this as "our" codebase to refactor** — almost everything under the repo root is stock upstream Asterisk; the Velents-owned surface is narrow:

- `configs/samples/` — a handful of Velents-authored `.sample` files mixed in among ~120 stock upstream samples (see below for which is which).
- `configs/basic-pbx/` — a hand-built example single-tenant PBX config.
- `deploy/` — Dockerfiles, `entrypoint.sh`, and a Python control-API sidecar (`control_api.py`, `sip_store.py`) with its own detailed `deploy/README.md`.

The root `README.md` is the **stock upstream Asterisk README** — build/install instructions for Asterisk itself, nothing Velents-specific. For the actual deployment contract (ports, volumes, env-var templating, K8s notes, the `control_api.py` sidecar's full CRUD contract for PJSIP trunks), read `deploy/README.md`. For the call-engine-side dialplan wiring specifically, read `configs/samples/README.call-engine.md` (note: it references an `ari_call_engine.conf.sample` that doesn't currently exist in this repo — only `ari.conf.sample`/`.template` — a stale reference in that doc, not a real file to look for).

**Open question, not yet reconciled**: `deploy/control_api.py` documents its own SIP-trunks CRUD contract (`/control/sip/trunks*`, Postgres-backed `ps_endpoints`/`ps_aors`/`ps_auths`/`ps_registrations` upserts) that reads nearly identically to the `call-engine` repo's own `src/control-api.js` + `src/sip-store.js` (Node, not Python). Before touching either, confirm which one is actually live in a given environment — they may be the same functionality implemented twice at different points in the project's history, one superseding the other.

## Velents-authored dialplan (`configs/samples/extensions_ai_runtime.conf.sample`)

The call-engine service's own dialplan. Contexts: `[ai-runtime]` (AudioSocket bridge target), `[call-engine-test]`, `[from-flows]` (visual flow runner entry), `[from-trunk]` (inbound from PSTN/SIP trunks), `[from-agents]` (ring a staff member's PJSIP endpoint — inbound ring target only), `[from-wss-agents-out]` (WSS softphone outbound dial), `[from-trunk-out]` (the actual PSTN/trunk leg for a human-agent outbound call). See `call-engine`'s own CLAUDE.md for how each context gets driven from `src/ari.js`/`src/transferer.js`.

### Two confirmed, non-obvious Asterisk pattern-matching gotchas (2026-08-17)

Both cost significant debugging time this session — check for these before trusting *any* new dialplan pattern here:

1. **`X`/`Z`/`N` — and their lowercase equivalents — are digit-class wildcards ANYWHERE in a pattern, not just when clearly intended as one.** A pattern like `_agent_.` looks like "literal `agent_` + wildcard", but the `n` in "agent" is silently reinterpreted as `[2-9]`, meaning the pattern **never matches the literal word "agent" at all**. Confirmed via isolated `dialplan add extension`/`dialplan show` testing in a scratch context (bypassing the real config, ruling out file corruption/caching/full-restart-staleness first): `flow`/`staf`/`hello` all matched fine as literal prefixes in the same position; `agent`/`xyz` never did, for exactly this reason. **Fix: wrap the offending letter in a single-character class** — `[n]` — which forces literal matching: `_age[n]t_.` (see `[from-agents]`). Check any literal-looking prefix for `x`/`z`/`n` (either case) before assuming it'll match as written.
2. **`X` alone (uppercase, the normal "any digit" wildcard) does not match a leading `+`.** A bare `_X.` pattern silently rejects any E.164-formatted string like `+201121750740` with "extension not found" even though it's a perfectly valid number — `X` only covers `0-9`. Fix: add a parallel `_+X.` pattern with the identical body (see `[from-trunk]`, `[from-wss-agents-out]`, `[from-trunk-out]` — each has both). Both the `_X.`-only and `_agent_.`-style bugs above independently broke **outbound dialing to E.164 numbers** and **every feature that rings an agent by Local-channel origination** (transfer, conference, consult, snoop, queue-routed inbound) respectively — check both when a Local-channel-originated call reports `core_local.c: No such extension/context ... while calling Local channel`.

## TODO / open items (as of 2026-08-17)

- [ ] Reconcile `deploy/control_api.py` vs. `call-engine`'s `src/control-api.js`/`src/sip-store.js` — confirm which is actually the live control API for SIP trunk CRUD in each environment, and retire or clearly scope the other.
- [ ] `configs/samples/README.call-engine.md` references a non-existent `ari_call_engine.conf.sample` — fix the doc or add the file, whichever reflects intent.
- [ ] Voylo's own outbound-trunk-creation wizard can generate a SIP URI whose hostname doesn't resolve in DNS at all (confirmed via two independent resolvers, once) — if this recurs for a new trunk, `ps_endpoints.outbound_proxy` (see `call-engine`'s migration `0008`) is the workaround: route to the provider's known-good IP while keeping their expected hostname in the actual SIP request.
