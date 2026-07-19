# Infath CXM — Service Scope, Softphone Build, and Tech Alternatives

Captured from chat: https://claude.ai/share/9014df82-a515-4845-8d1e-175d142f94be
Transcribed on 2026-07-14 (client-rendered page, no automated fetch available — pasted by user).

## How many services do we need to create?

The board is organized by 13 feature increments, not by deployable services — those are not the same unit. The count is an architecture decision, not something the board answers directly.

### What you operate/configure vs. what you write

- **~5 infrastructure components (deployed-and-configured, not written):** SBC, Kamailio, FreeSWITCH, LiveKit, STT/LLM/TTS engines.
- **~4 shared platform pieces (stood up once):** event bus (Kafka/NATS), primary datastore, object store for recordings, API/realtime gateway.
- Engineering time goes into the glue services on top of these, not into the infra/platform layer itself.

### P0 — the spine (small, on purpose)

Realistically **3 services**:

1. **Call/session orchestrator** — owns the canonical session and event model (AGH-6670), drives FreeSWITCH via ESL/ARI, handles call initiation (AGH-6688). Nearly everything downstream reads its events.
2. **Realtime gateway** — WebSocket layer pushing session state and screen-pop context (AGH-6695) to the agent surface. Needed early since both desktop and supervisor tiers depend on it.
3. **Media/fork bridge** — receives the `mod_audio_fork` stream, resamples μ-law→PCM, routes into the recording tap and (later) the LiveKit room. Optional in P0 if AI voice is deferred — then P0 is just 2 services plus the recording tap as FreeSWITCH config.

Trunk connectivity and DR posture are configuration/topology, not new services.

### Full product — P0 through P5

Mapping the 13 increments to natural service boundaries lands around **10–14 services**:

- **Core:** voice, routing, desktop-BFF, supervisor, recording
- **Depth layer:** reporting/analytics, IVR/flow engine, agent-assist/RAG, outbound/dialer, omnichannel/channel-adapters, WFM
- **Platform layer:** tenant-config + entitlements + metering, auth/RBAC + audit (one or two services)

It's a range, not a fixed number, because several increments don't deserve their own service (e.g. governance/RBAC/audit as one service, not three; dashboards as a read-model over the event bus, not a heavy service; WFM can start as a module inside another service).

### Recommendation

Don't build one microservice per increment. Start as a modular monolith or 3–4 coarse services (voice-runtime, contact-center-core, platform/tenant, AI-voice), and split out a service only where scaling, isolation, or the residency boundary forces it — recording and the media/AI leg are the obvious early split-outs due to compliance and latency constraints.

**Flag:** because tickets are service-silent, this decomposition isn't captured anywhere on the board. Before P1 staffing ramps, write a one-page service/deployment topology so the session-orchestrator boundary (and who owns the event contract) is decided before multiple people build against it differently.

---

## How to build the "visual phone" (agent softphone)

A "visual phone" here means the on-screen softphone the agent uses in the browser: dialpad, call display, call-control buttons (answer, hang up, mute, hold, transfer). Not a hardware desk phone.

A mockup was shown in-chat: an Arabic-first (RTL) agent softphone in the connected-call state, with dialpad and core controls, digits clickable. (See `infath_agent_softphone_mockup.svg` in this directory for a reconstruction — the original artifact's exact pixels were not preserved in the plain-text chat export.)

### The core decision: WebRTC in the browser, not a SIP hardware stack

The browser speaks WebRTC, not SIP-over-UDP to a carrier. You need a client library doing SIP signaling over WebSocket, plus a server that terminates that WebSocket and bridges into the voice spine.

**Two realistic client paths:**

1. **SIP.js / JsSIP route** — a JS SIP-over-WebSocket library registers the agent as a SIP endpoint, handles INVITE/answer/BYE, and manages the `RTCPeerConnection` carrying audio. Connects to Kamailio (running its WebSocket module as a WSS-to-SIP gateway) in front of FreeSWITCH.
   - answer = accept incoming session
   - hangup = `session.bye()`
   - mute = toggle local audio track's `enabled`
   - hold = renegotiate with audio direction set to `inactive`
   - transfer = SIP `REFER`
   - dialpad = `session.dtmf()`
   - This is the classic CCaaS softphone architecture and lines up with the Kamailio/FreeSWITCH tiers already in the stack. **Preferred for full supervisor barge/whisper + recording**, since FreeSWITCH stays the media anchor where those features live.
2. **LiveKit-client route** — if the agent leg goes through a LiveKit room (the AI-voice tier), the softphone is a `Room` connection: publish the mic track, subscribe to the caller's audio; the "SIP participant" is the bridged call. Simpler client code (no SIP handling in-browser), but only makes sense if agent audio is committed to routing through LiveKit rather than direct WebRTC-to-FreeSWITCH.

### Three things the UI mockup hides

1. **State machine.** idle → ringing → connected → held → wrap-up → idle, driven by SIP/session events, not clicks. Buttons enable/disable by state; visible state must come from server events, not optimistic local guesses, or agent/switch state drifts.
2. **Screen-pop wiring.** Caller name/number/queue isn't typed — it's pushed from the realtime gateway (the P0.5 handoff, AGH-6695) the instant the call is offered. The softphone subscribes to that channel and renders context before the agent answers.
3. **Device and permission handling.** `getUserMedia` prompts, mic/speaker device selection, headset detection, graceful failure when the browser blocks audio — unglamorous, but where real softphones break.

### Board mapping

This component is essentially **AGH-6673** (agent call controls) sitting on top of **AGH-6665** (workspace shell). It can't be fully exercised until the P0 session spine emits real events — build the UI against a mock event stream first, then wire to the orchestrator.

---

## Tech alternatives across the stack

Four tiers were diagrammed, carrier edge through to the agent's screen. In each, the **teal box is the recommended/primary choice already in the stack**, and **gray boxes are drop-in alternatives**. Reconstructed as SVGs in this directory:

- `infath_voice_signaling_media_tier_alternatives.svg`
- `infath_ai_voice_pipeline_tier_alternatives.svg`
- `infath_platform_data_tier_alternatives.svg`
- `infath_application_delivery_tier_alternatives.svg`

### Voice path — carrier edge → call control → WebRTC media

- **SBC:** Kamailio (primary) vs OpenSIPS (alternative) — close cousins, same lineage, both battle-tested SIP proxies; the choice is largely operator familiarity.
- **Media/call-control server:** FreeSWITCH (primary) vs Asterisk (alternative) — **not a free swap**: media-forking changes from `mod_audio_fork` to Asterisk's ARI `externalMedia`, so swapping this tier ripples into the AI-audio pipeline.

### AI voice tier — speech in, bot/agent-assist response out

Pipeline: audio fork (PCM) → Arabic STT → LLM (+ optional NLU/RAG) → TTS → back into the call.

- This is where the Arabic-first constraint bites hardest: for STT and TTS, the real selection criterion isn't brand, it's **Saudi-dialect (Gulf Arabic) accuracy** — bench candidates on your own call audio before committing.
- All components must run **in-region** (or via an in-region endpoint) to stay inside the residency boundary — this quietly rules some managed/global options in or out.
- *Note: the specific vendor shortlist shown in the original diagram's image was not preserved in this text-only chat export — treat vendor selection here as still open.*

### Platform and data tier — the shared backbone

- **Event bus:** Kafka (primary) vs NATS (alternative) — a real weight-vs-simplicity tradeoff, not a rename.
- **Cache:** Redis (primary) vs Valkey (alternative) — nearly a rename (Valkey is the open-source Redis fork).
- **Object store (recordings):** must stay in-region — MinIO (self-hosted) or a KSA cloud's S3-compatible service — **never a default US bucket**. Recordings carry PDPL/NCA retention rules (AGH-6685), so this pick has a direct compliance angle.
- **Primary datastore:** mentioned generically in the source discussion; no specific engine was named — still an open decision.

### Application and delivery tier — services you write + agent's screen

- **Backend service framework:** NestJS, Go, Java, .NET all called out as "fine CCaaS backends" — described as near-equivalent; pick for **team fluency**, not diagrammed with a stated primary.
- **Softphone client library — the one genuinely coupled choice:** must match the media tier.
  - SIP.js/JsSIP (primary) if agents connect via Kamailio WebSocket → FreeSWITCH.
  - LiveKit client (alternative) if agent audio routes through a LiveKit room.
  - This decision follows from the voice-tier diagram, not the other way around.

### Caveats stated in the original discussion

- "Primary" markings reflect what's already in the stack, not universal technical superiority — for several tiers (backend language, cache, event bus) the alternatives are near-equivalent, and the decision should be team fluency / ops burden.
- These lists are not exhaustive — each tier has a long tail of niche options; only what a team building a KSA-region CCaaS would realistically shortlist is shown.

---

## Abbreviations glossary (as given in chat)

**Telephony and signaling:** SIP (Session Initiation Protocol), SBC (Session Border Controller), PSTN (Public Switched Telephone Network), DID (Direct Inward Dialing), SIP trunk (carrier SIP link carrying many concurrent calls), RTP (Real-time Transport Protocol, carries audio), SDP (Session Description Protocol, negotiates codecs/media inside SIP), DTMF (Dual-Tone Multi-Frequency, touch-tone keypad signals), REFER (SIP method used to transfer a call), WebRTC (Web Real-Time Communication), WSS (WebSocket Secure), SFU (Selective Forwarding Unit).

**Platform terms:** CPaaS (Communications Platform as a Service, e.g. Voylo), CCaaS (Contact Center as a Service — the product being built), CDR (Call Detail Record), API (Application Programming Interface), ARI (Asterisk REST Interface), ESL (Event Socket Library, FreeSWITCH's equivalent), S3 (Simple Storage Service, the de-facto object-storage API standard), BFF (Backend for Frontend), HA (High Availability), DR (Disaster Recovery).

**Audio/AI pipeline:** STT (Speech-to-Text), TTS (Text-to-Speech), LLM (Large Language Model), NLU (Natural Language Understanding), RAG (Retrieval-Augmented Generation), PCM (Pulse-Code Modulation, raw uncompressed audio), μ-law (compressed 8kHz phone-network audio encoding, resampled to PCM for AI), MSA (Modern Standard Arabic, distinct from spoken Gulf/Saudi dialects).

**Contact-center domain:** ACD (Automatic Call Distributor), IVR (Interactive Voice Response), SLA (Service Level Agreement), AHT (Average Handle Time), KPI (Key Performance Indicator), WFM (Workforce Management), QM (Quality Management), VoC (Voice of the Customer).

**Governance and compliance (Saudi):** PDPL (Personal Data Protection Law), NCA (National Cybersecurity Authority), NDMO (National Data Management Office), RBAC (Role-Based Access Control).

---

## Diagram compatibility check (against the board)

Verdict: `infath_cxm_voice_tier_reference_architecture` is compatible with the tasks. The two planning diagrams (`infath_call_center_build_dependency_map`, `infath_call_center_release_sequence_plan`) are compatible in spirit but not in bookkeeping.

### `infath_cxm_voice_tier_reference_architecture` — compatible

Lines up cleanly with P0/P1 tech scope: Channels → KSA carrier SIP trunks → Kamailio SBC (active + standby HA) → FreeSWITCH cluster with recording tap → `mod_audio_fork` → Arabic STT/LLM/TTS → CXM app tier → data/recording/PDPL·NDMO layer. Carries OpenSIPS and Asterisk-ARI as annotated alternatives.

It's more specific than the tickets — names carriers (STC, Mobily, Salam), asserts a three-node FreeSWITCH cluster, puts HA on a standby SBC. None of that contradicts the tasks, but since AGH-6664/6670/6681 are stack-silent, this diagram is effectively the missing implementation detail and **should be linked into those P0 tickets**.

### The two planning diagrams — three incompatible numbering systems for the same program

1. Board/dossier: phases P0–P5 (six), AGH-#### issue IDs, ~13 parent increments.
2. `infath_call_center_build_dependency_map`: phases 0–4 (five), no IDs.
3. `infath_call_center_release_sequence_plan`: releases R1–R3 plus increments Inc 1–12 (sub-tags like I2, I3, Inc 3.1), no AGH IDs.

None cite AGH-#### numbers, so no box in any diagram traces back to a Linear issue, and the two planning diagrams don't agree with each other (5-phase model vs. 3-release/12-increment model).

Rough correspondence once lined up: Inc 1 Voice Engine Spine = AGH-6655 (P0); Inc 4 Supervisor = AGH-6658; Inc 12 Governance/RBAC/Audit = the P1 governance work; etc. "Roughly" is the problem for a board meant to be executed against.

### Specific mismatches to fix or verify

- **Missing P5 (the real one).** Both planning diagrams stop at parity (release plan's furthest point is R3 "complete parity"; build map ends at Phase 4 WFM). Neither shows the productization / second-client / multi-tenant-readiness phase that's an entire parent increment on the board (P5, AGH-7309). Worth adding since Agent Hub CXM is explicitly multi-tenant, not just the INFATH deployment.
- **WFM placement — verify.** Both diagrams treat WFM as last (Phase 4 / R3). The board reading put WFM (AGH-6661) in P1 build-now foundations, feeding dashboards. If that's right, diagrams and board disagree on sequencing — worth a deliberate decision, not an accidental drift.
- **Desktop as thin slice vs. P2 parent.** Release plan threads a minimal desktop through R1's golden path (Desktop, pop+ctrl · I2); board has the desktop parent (AGH-6656) in P2. Consistent with walking-skeleton philosophy (thin vertical slice early, full increment later) — not a contradiction, but nothing on the diagram says the R1 "I2" tag and the AGH-6656 P2 parent describe the same increment at two maturities.
- **Minor tag ambiguity:** in the release plan, tag I8 appears both on the golden-path "Action (ticket+CSAT)" seam and on "Dashboards Inc 8" in R2 — worth confirming whether that's the same increment loosely labeled, or a mis-tag.

### Net recommendation

Attach the architecture diagram to the P0 tickets as-is. Reconcile the two planning diagrams: pick one canonical phase vocabulary (the board's P0–P5 + AGH IDs, since that's what Linear holds), add the P5 productization lane, and resolve the WFM timing against the board. A crosswalk table (each diagram's box → board phase → AGH-#### increment, conflicts flagged) was proposed as the next artifact but not yet produced.
