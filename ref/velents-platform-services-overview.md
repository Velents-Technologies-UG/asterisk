# Velents Platform — Existing Services Overview

Captured 2026-07-14 by reading the actual repos under `E:\Projects\Velents\` (not chat-derived — this reflects real code, so re-check against the repos themselves before relying on specifics like route lists or module names, since they'll drift as the code changes).

## `asterisk` — Velents' Asterisk fork (voice trunk + human-agent softphone + AI/IVR hooks)

**Correction to an earlier version of this doc**: this repo did not exist under `E:\Projects` when first checked, but does now (`git remote`: `https://github.com/Velents-Technologies-UG/asterisk.git`, branch `master`, HEAD `3a7324fd2`). It's a real fork of upstream Asterisk (the full C source tree — `main/`, `channels/`, `res/`, `pbx/`, etc. — vanilla apart from one added top-level dir, `deploy/`, plus a handful of custom files under `configs/samples/`). This is the most direct answer to "what tech stack for the voice trunk + IVR gap," so it gets its own detailed section below rather than a one-line summary.

### What already exists here

- **Container build** (`deploy/Dockerfile.dev`, `Dockerfile.prod`, `entrypoint.sh`): two images with an identical port/volume/env contract — dev overlays configs onto a base image (~30s build), prod is a full multi-stage build from this fork's source (~10-15min, CI target). Ships Asterisk 22.
- **Control-API sidecar** (`deploy/control_api.py`, 1609 lines): a dependency-light Python **stdlib-only HTTP server** (`http.server.ThreadingHTTPServer`, no Flask/FastAPI) running alongside Asterisk in the same pod, bearer-auth protected, binds `127.0.0.1:8092`. Implements SIP trunk CRUD (`/control/sip/trunks`) by upserting the four PJSIP realtime rows (`ps_endpoints`/`ps_aors`/`ps_auths`/`ps_registrations`) directly into Postgres via `psycopg2` (falls back to an in-memory dict — no real SIP — if `DATABASE_URL`/psycopg2 aren't available, and logs this loudly). Also exposes `/control/asterisk/reload` (execs `asterisk -rx module reload` over a CLI, whitelisted to `res_pjsip.so` and `res_pjsip_endpoint_identifier_ip.so`) and a `/control/flow-analytics/*` proxy contract (overview/funnel/trend per IVR flow, tenant-scoped).
- **Inbound/outbound trunk dialplan** (`configs/samples/extensions_ai_runtime.conf.sample`): `[from-trunk]` sends every inbound DID straight to `Stasis(call-engine, inbound, ${EXTEN})`; `[from-wss-agents-out]` sends every dial from a logged-in agent's browser softphone to `Stasis(call-engine, outbound_human, ${EXTEN}, ${CHANNEL(endpoint)})`; `[from-trunk-out]` is the actual PSTN-facing leg the call-engine originates once it's picked a trunk (`TRUNK_ENDPOINT`) and caller ID. So: **the inbound/outbound trunk plumbing for human agents already exists end-to-end at the Asterisk config + control-API layer.**
- **Browser softphone for human agents** (`pjsip_wss_agents.conf.sample`, `http_wss.conf.sample`): PJSIP over WSS (WebRTC, opus/alaw, DTLS-SRTP), per-agent SIP credentials auto-provisioned via realtime templates when AgentHub calls `POST /api/agents/<id>/sip-credentials`. TURN (`coturn`) support was added for symmetric-NAT ICE candidates (see git log: `feat(rtp): inject TURN (coturn) config into rtp.conf for WebRTC media`).
- **AI audio tap** (`[ai-runtime]` context): originates a Local channel that runs Asterisk's `AudioSocket()` app — a raw TCP stream of 16kHz signed-linear PCM (`slin16`) to a given host:port/uuid. **Currently a stub**: the smoke test just plays a 3-second tone back. This is the hook point where a real AI pipeline attaches (STT/LLM/TTS, or a bridge into the existing LiveKit voice-agent stack) — it is not wired to one yet in what's checked in here.
- **IVR hook, not the IVR itself** (`[from-flows]` context): `flow_<publicId>` extensions route into `Stasis(call-engine, flow, <publicId>)` for something the comment calls "the visual call-flow runner (Phase J1+)" — plus a `/control/flow-analytics/*` REST contract (per-flow overview/funnel/trend, node-level dwell/abandonment) already specified in `deploy/README.md`. **Neither the visual builder UI nor the FlowRunner execution logic are in this repo** — see below.

### Correction: "agent-hub" is velentsAgents, not a separate Next.js/Node.js repo

An earlier version of this doc assumed `agent-hub` was a distinct GitHub repo (`Velents-Technologies-UG/agent-hub`) holding a Node.js ARI Stasis app plus a Next.js dashboard. **That's wrong.** Per the user (2026-07-15): `agent-hub` *is* `velentsAgents` — the Laravel 12 app described below, which is a full PHP/Laravel front-end + back-end, not a Next.js frontend over a separate Node.js service. References below to a Next.js dashboard, `<Softphone>` component, `/dashboard/build/voip/trunks`, `lib/cx/trunks.ts`, `lib/cx/control-client.ts` should be read as **stale/unverified** — treat them as "the shape of a dashboard+trunk-mgmt surface exists somewhere," not as literal Next.js file paths, until re-verified against the actual `velentsAgents` repo.

**Still open (flagged TBD, not yet confirmed either way):** the ARI (Asterisk REST Interface) Stasis application that holds the live WebSocket connection to Asterisk — referred to elsewhere in this doc and in the `asterisk` repo's own docs as `agenthub-call-engine` — is unconfirmed as to whether it's:
(a) PHP/Laravel code living inside `velentsAgents` itself, or
(b) a separate small Node.js (or other) service alongside `velentsAgents` that just talks to it.
This matters concretely for the IVR FlowRunner recommendation below (Node.js `ari-client` vs. a PHP ARI approach) — re-verify against the actual `velentsAgents` repo (and check for any adjacent call-control service) before building on that recommendation.

### So, precisely, what's missing (refining the user's original framing)

1. **Not "a voice trunk"** — inbound DID routing, outbound trunk selection or per-tenant rules, and the human-agent WebRTC softphone are already designed and largely wired (Postgres-backed, ARI-driven). What's actually missing is **the AI media integration on top of that trunk**: nothing currently listens on the `AudioSocket` tap or speaks back through it — it's a stub. Both target modes (AI assisting a live human-agent call, and a fully-AI-handled call) need this wired to a real pipeline.
2. **IVR**: the dialplan entry point and the analytics contract exist; the visual flow builder and the FlowRunner execution engine that would consume `flow_<publicId>` do not exist in any repo currently on this machine (may exist further along in `agent-hub`'s history — unconfirmed without cloning it).

## `velentsAgents` — the core platform ("VelentsAI")

Laravel 12, multi-tenant (database-per-tenant via `stancl/tenancy`, `tenant_{id}_boddy`). This is the central backend the rest of the Velents suite integrates around — it already has its own detailed `CLAUDE.md` in that repo; this is a condensed pointer, not a replacement for it.

**Module map** (`app/` tree):

| Module | Purpose |
|---|---|
| Agents | AI agent config/lifecycle (Agent, KnowledgeBase, AgentBatch, AgentCalendarSlot) |
| Conversations | Text-channel sessions |
| Calls | Voice-channel sessions (LiveKit) |
| Analytics | Polymorphic call/conversation analysis |
| Payment | PaymentPlan/PaymentPlanLog/PaymentPlanTemplate |
| Management | Staff (Spatie roles/permissions), Invitation |
| Integration | ElevenLabs, LiveKit, Genesys, WhatsApp, CallGateway, Activepieces, Kafka/FFmpeg |
| Observer | Mature QA/analytics overlay over external call-center/helpdesk systems (Genesys, Cisco, Avaya, 3CX, Issabel, Zoho, Freshchat, Freshdesk) — ingest → Gemini-graded scorecard judging → leaderboards/disputes |
| Support | Full helpdesk/ticketing ("Support Center"): SLA policies, automation rules, CSAT, AI→human handoff |
| Customers | Cross-channel end-customer 360 (EndCustomer, AI snapshot via OpenAI) |
| Assistant | NL analytics chat over a server-side semantic layer (tenant-safe: LLM never sees raw SQL) |
| Insights | Scheduled anomaly/forecast watchdog, reuses Assistant's forecast core |
| Automation / Workflow | Two generations of an embedded Activepieces (no-code automation) bridge, per-tenant |
| Inbox | Read-only view merging Conversations + Calls |
| CRM | Cross-tenant ops-dashboard aggregator (unrelated to the `Customers` module despite the name) |
| Phones | Phone number management |
| Core | Tenant, User, base classes, middleware, helpers |
| AuditLog | Tracks all staff actions |
| Tools | Dynamic tool generation from files/text/URLs via Gemini |
| SupportCenter | Dead/orphaned — not wired to any route; don't confuse with the real Support Center (`app/Support/`) |

**External services it calls** (`config/services.php`): TextAgent, VoiceAgent, ElevenLabs, LivekitDispatcher, CallGateway, **VelentsIntegrationsVelentsAi** (this is `velents_integrations`, below), Gemini, OpenAI, Activepieces, GenesysCloud/Freshchat/Freshdesk.

**Agent lifecycle** (the core domain flow): create → configure (prompt/KB/tools/channels) → dispatch by channel (text → Conversation via TextAgent; voice → Call via LivekitDispatcher) → per-turn/per-call analytics → optional deep-copy clone.

Run/test commands, auth-guard details, and the full module-by-module deep dive live in `velentsAgents/CLAUDE.md` — read that directly when working in this repo rather than duplicating it here.

## `velents_integrations` — third-party channel & payment gateway service

Laravel 12, standalone service (not multi-tenant-aware itself — it's called *by* velentsAgents as an external integration, referenced there as `VelentsIntegrationsVelentsAi` / `VELENTSINTEGRATIONSVELENTSAI_URL`). Its own `README.md`/`composer.json` are still the unmodified Laravel skeleton (`"name": "laravel/laravel"`) — no descriptive docs exist in-repo, so this overview is derived from the actual `app/` tree and `routes/api.php`.

**What it does**, by route group:

- **WhatsApp** (`/whatsapp/*`, `App\Services\v1\Integrations\MetaWhatsapp`): customer onboarding, template creation (incl. document templates), send first message / send message, incoming webhook, webhook rotation.
- **Meta-namespaced WhatsApp** (`/Meta/*`, `App\Http\Controllers\Api\Meta\*`): a second, more structured Whatsapp Business Cloud API surface — tenant creation/show/secrets, phone number management, verification codes, subscribed apps, message send/mark-as-read, webhook registration, template CRUD (`apiResource`). Guarded by `auth:tenant` and `auth:whatsapp` guards plus a `ForceJsonResponse` middleware group.
- **Facebook Messenger & Instagram** (`/messanger/*`, `/instagram/*`, share the same `MessangerChatController`): OAuth redirect/callback, webhook (GET+POST), send message.
- **Payment** (`/payment/*`, `App\Services\v1\Integrations\MoneyHash`): create payment plan, create customer, create card token, create subscription, create account, generate payment link, list transactions, callback — all keyed by a `{reference_id}` tying back to the calling tenant/org in velentsAgents.
- **Velents accounts** (`/velents/accounts`): the account-provisioning endpoint implementing the "org → account → providers → methods" model noted inline in the routes file — this is the join point between a velentsAgents tenant and this service's provider accounts.

**Key models**: `Account`, `Messanger`, `WhatsappAccount`, `meta/MetaTenant`, `meta/MetaWhatsappAccount`, `meta/MetaWhatsappPhones`, `PaymentCustomer`, `PaymentPlan`, `PaymentTransaction(Details)`, `PaymetAccount`/`PaymetProvider` (note the repo's actual spelling — "Paymet", not "Payment" — on these two).

**Dependencies of note**: `twilio/sdk` (present in composer.json but no Twilio controller/service currently wired — either planned or superseded by the Meta direct-API path), `maatwebsite/excel` (import/export), `predis/predis`.

**Caveat**: local dev DB is SQLite (a file literally named `velents_integration`, no extension — don't mistake it for a text file).

## How these three fit together (as currently understood)

- `velentsAgents` is the tenant-facing core (agents, conversations, calls, support, analytics) and treats `velents_integrations` as an external dependency for anything touching WhatsApp/Messenger/Instagram messaging and payment processing/subscriptions.
- `asterisk` is the SIP/media edge + human-agent softphone layer, driven by an ARI Stasis app (`agenthub-call-engine`) that also owns the not-yet-built IVR FlowRunner. `agenthub-call-engine`'s dashboard/frontend counterpart is `velentsAgents` (Laravel, PHP full-stack) — **not** a separate Next.js repo, per the correction above. Whether the ARI/Stasis process itself is PHP-in-`velentsAgents` or a still-separate small service is unconfirmed (TBD).
- **Important reconciliation needed with the planning docs**: `infath_voice_signaling_media_tier_alternatives.svg` and `infath-service-scope-softphone-and-tech-alternatives.md` (both in this directory) frame **FreeSWITCH as primary, Asterisk as the alternative** for the media/call-control tier, on the reasoning that FreeSWITCH is already "in the stack." That framing is now stale: the actual codebase has committed to **Asterisk**, with real infrastructure built on it (PJSIP realtime trunks, WSS agent softphones, ARI/Stasis call control, AudioSocket AI tap). Don't silently follow the old planning docs' FreeSWITCH-primary framing — surface this conflict if it comes up, and treat Asterisk as the de facto choice going forward unless there's a reason to revisit it.

## Recommended tech stack for the two missing pieces

Per request, focusing specifically on: (1) a voice trunk usable for inbound/outbound calls where either a human agent handles the call with AI listening in to assist/suggest, or the AI fully handles the call; and (2) IVR. The guiding principle below is **build on what's already there rather than introduce a second stack** — Asterisk, ARI/Stasis, Postgres, and the existing LiveKit-based voice-agent pipeline are all already committed to; the gap is wiring them together, not picking new technology from scratch.

### 1. AI-on-trunk (human-assist and fully-AI modes)

- **Keep Asterisk + ARI/Stasis as the call-control layer.** It already does inbound DID routing, outbound trunk selection, and WebRTC human-agent softphones. No reason to introduce FreeSWITCH now (see reconciliation note above) or a second SIP stack.
- **Audio tap**: the `AudioSocket()` hook (`[ai-runtime]` context) already exists end-to-end as a stub — cheapest path is finishing that wiring rather than switching mechanisms. It's a plain TCP-framed 16kHz PCM stream, trivial to consume from any language. Consider Asterisk's native ARI `channels/externalMedia` (RTP-based channel snoop, Asterisk 18+) only if/when raw AudioSocket latency or the lack of a true "snoop-only" (non-intrusive listen) mode becomes a real constraint — it's the more idiomatic mechanism for a **listen-only** tap (which is exactly the "AI assists, doesn't speak" mode), and doubles as the same primitive the Supervisor listen/whisper/barge feature already needs (see `infath-cxm-platform-demo-analysis.md`) — one mechanism, two use cases.
- **Don't build a second AI-voice pipeline.** `voice-agent`, `text-agent`, `livekit-dispatcher`, and `livekit-outbound-caller` already exist and are already the AI-voice implementation used elsewhere in the platform (per `velentsAgents/CLAUDE.md`: calls dispatch via LivekitDispatcher → VoiceAgent + ElevenLabs). Reuse that service behind the Asterisk tap instead of standing up a parallel STT/LLM/TTS integration:
  - **Fully-AI mode**: bridge the Asterisk trunk leg into the existing LiveKit pipeline (LiveKit has a native SIP bridge, `livekit-sip`) so AI-handled calls flow Asterisk → LiveKit → the existing VoiceAgent/ElevenLabs service, reusing that pipeline rather than forking a second one on raw AudioSocket.
  - **Human-assist mode**: feed the AudioSocket (or externalMedia) tap into the same STT/LLM layer used by VoiceAgent, but in a listen-only capacity — transcribe + suggest, pushed to the agent's UI (the Next.js dashboard's copilot panel) over a WebSocket, never injected back into the call audio. This is a one-way analysis path, not a duplex AI-speaks role, so it doesn't need LiveKit at all.
- **Net effect**: one AI-voice implementation, two entry points (LiveKit-SIP for full-AI, AudioSocket/externalMedia tap for assist-only), rather than duplicating STT/LLM/TTS logic for each mode.

### 2. IVR

- **Data model**: a JSON node-graph (nodes + edges), tenant-scoped, versioned with publish/draft states and rollback — this is already implied by the `/control/flow-analytics/*` contract's node-level shape (`step_id: "node_42"`) and matches the versioning pattern already modeled in the INFATH CXM demo dataset (`data.ivr.versions`, live/draft flows — see `infath-cxm-platform-demo-analysis.md`). Store in Postgres, next to the trunk/routing tables the control-API already writes to.
- **Visual builder**: React Flow (MIT-licensed, the standard choice for exactly this kind of drag-and-drop node/edge editor) inside the `velentsAgents` (Laravel/PHP full-stack) frontend — corrected from an earlier assumption of a separate Next.js dashboard; React Flow can be mounted inside a Laravel-served frontend same as any SPA-in-a-page setup. The INFATH CXM demo prototype already has a fully fleshed-out set of IVR-builder interactions (`ivrFlow`, `ivrNodes`, minimap, zoom, import/export/diff, publish — see the demo analysis doc's method inventory) worth treating as the UX reference/spec rather than designing the builder from scratch.
- **Runtime ("FlowRunner")**: lives inside `agenthub-call-engine` since that's what already owns the ARI/Stasis connection for a channel — no separate service needed. **Language TBD**: if `agenthub-call-engine` turns out to be PHP-in-`velentsAgents` (unconfirmed, see correction above), FlowRunner would be PHP driving Asterisk's ARI over HTTP/WebSocket (less common than Node's `ari-client` but workable via a long-running worker/queue process); if it's a still-separate Node.js service, the original Node.js `ari-client` recommendation stands. The `[from-flows]` dialplan hook (`Stasis(call-engine, flow, <publicId>)`) is already the entry point; FlowRunner just needs to exist on the other end. Node types to support at minimum:
  - DTMF menu (native Asterisk `Read`/`Background`)
  - TTS/prompt playback (pre-rendered audio via `Playback`, or on-the-fly via the existing VoiceAgent/ElevenLabs integration)
  - AI/voicebot step — hands the channel to the same AudioSocket/LiveKit AI tap used for fully-AI calls, so an IVR can seamlessly hand off into a conversational AI segment
  - Transfer to queue/agent (reuses the existing `[from-agents]` dialplan)
  - Branch/condition nodes (e.g. business-hours check — the demo dataset already models a Saudi holiday calendar and after-hours routing under `data.routing`)
- **Analytics from day one**: since the `/control/flow-analytics/*` contract (overview/funnel/trend, per-node enter/complete/abandon, dwell percentiles) is already specified in `deploy/README.md`, build FlowRunner to emit those events natively rather than retrofitting instrumentation later.

This file is a snapshot for planning purposes (which parts of the existing Velents stack could be extended/reworked for Agent Hub CXM / INFATH) — treat specifics (route names, model names) as current-as-of-read, not contractually stable; re-grep the actual repos before making a change based on this doc. `agent-hub` = `velentsAgents` (Laravel, PHP full-stack) per the 2026-07-15 correction above — this doc's claims about `agenthub-call-engine`'s behavior are still inferred from references in the `asterisk` repo's docs, not from reading `velentsAgents`' code directly, and the ARI/Stasis process's exact location (PHP-in-`velentsAgents` vs. a still-separate service) remains TBD. Re-verify directly against `velentsAgents` (see its own `CLAUDE.md`) before relying on this.
