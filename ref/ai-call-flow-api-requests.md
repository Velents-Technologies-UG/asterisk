# AI call flow — API request sequence (notes behind the diagram)

Companion notes for `ai-call-flow-api-requests.html` (published artifact:
https://claude.ai/code/artifact/ee29f731-c96f-49ba-9b69-f46f47d64317). Captured 2026-07-19.
The user proposed an inbound-call flow (tenant IVR → AI/human queue → per-tenant AI-copilot
webhook fan-out → MongoDB → velentsAgents backend → frontend, plus an AI-agent-handled call a
human can take over); this doc is the critique, the open questions raised, the user's answers,
and what ended up in the diagram.

---

## The flow as originally proposed

1. Call comes into Asterisk.
2. Using the tenant's IVR, the call lands on an AI agent, a human agent, or plain TTS/recorded
   audio, depending on client choice (e.g. "working hours" vs. "talk to IT support").
3. AI agents and human agents sit in two separate queues. A human can take control of an
   AI-handled call (for urgent cases) — the caller hears a recorded message ("You are now
   directed to a human agent") first.
4. Per-tenant AI-assist config (`sentimental_analyses`, `client_intention`,
   `tool_recommendation`, `reply_recommendation`, `question_to_ai_recommendation`, each a
   bool) drives a webhook fan-out: for each enabled feature, a webhook is created and sent
   (as an array, twice) — once to a media/STT ingestion service (possibly LiveKit's voice
   inbound service), which does STT and forwards to separate task services. Reply
   recommendation calls `text_agent` for N variations. Tasks may consult tenant KB. Results
   (with KB-match %) are written to MongoDB, and a webhook notifies the velentsAgents backend,
   which fetches from MongoDB and streams the results to the frontend alongside the live call
   audio. The human agent can also control the call and see metadata from the frontend.

## Critique — where this didn't line up with what's actually built or documented

- **Steps 1–2 depend on two pieces that don't exist in any repo on this machine.** The
  "call-engine" (ARI/Stasis app) every dialplan hook routes into has no confirmed location —
  Asterisk's own docs point to an uncloned `agent-hub` repo, contradicting an earlier note in
  `velents-platform-services-overview.md` claiming "agent-hub = velentsAgents." The IVR
  FlowRunner that would execute "route to AI vs. human vs. TTS" also doesn't exist — only the
  dialplan entry hook (`Stasis(call-engine, flow, <publicId>)`) and an analytics contract are
  in place (see `asterisk-deployment-test-suite-and-feature-gap-plan.md`).
- **"AI agents as an ACD queue member" is a real design commitment, not a given.** For a human
  to take over an AI call the way normal Asterisk queue tooling works, the AI would need a
  persistent SIP UA sitting in a queue like a human agent. Buildable, but nothing in the docs
  previously assumed this.
- **Tenant KB has two unreconciled candidate sources**: velentsAgents' own
  Agents/KnowledgeBase model, vs. the standalone `voice-agent` service — which, despite the
  name, is a Weaviate KB API, not an audio pipeline (a correction already noted in the
  feature-gap plan). The proposed flow's "uses tenant KB" doesn't say which.
- **MongoDB is a new datastore.** Nothing else in the platform uses Mongo — `control_api.py` is
  Postgres, `velentsAgents` is per-tenant MySQL/Postgres via `stancl/tenancy`. Worth being
  deliberate about introducing a third datastore for this one feature.
- **"Sent in an array twice" was ambiguous** as originally phrased — needed clarification on
  the second destination.
- **The STT ingestion path contradicts an earlier documented recommendation.** The
  services-overview doc explicitly argued human-assist mode is one-way (listen-only) and
  "doesn't need LiveKit at all" — a direct `AudioSocket`/`externalMedia` tap to a transcription
  service was the recommended path. Routing through "livekit voice inbound service" instead is
  a real fork, not a detail.
- **No mechanism was specified for getting live updates to the open browser tab** — webhook →
  backend → fetch from Mongo gets data into the backend, but not to the frontend.

## Questions asked, and the answers that shaped the diagram

1. **STT ingestion path** — direct `AudioSocket`/`externalMedia` tap vs. bridging into a
   LiveKit room as a silent subscriber?
   → **Not decided.** Drawn as an explicit open fork in the diagram rather than picked.

2. **What's the second destination in "sent as an array twice"?**
   → Once to the ingestion/STT service, which fires the corresponding webhook to each
   individual AI task service (sentiment, intent, tool-rec, reply-rec); and once to the
   velentsAgents backend, so it's listening for the callbacks.

3. **AI → human takeover mechanics at the telephony layer?**
   → AI-agent-handled calls run their own full pipeline (STT + `text_agent` + TTS, plus the
   same sentiment/intent analysis as the assist flow), shown to a human in the frontend as a
   live chat-style transcript with tools-used and analysis, with the full call viewable/audible.
   The human can listen, then decide to take the call after an announcement plays.

4. **How do live updates reach the open browser tab?**
   → WebSocket push (e.g. Laravel Reverb/Pusher) from the velentsAgents backend to the
   frontend, once it reads the new result from MongoDB.

## What the diagram ended up showing (three flows)

1. **Routing** — trunk → dialplan → call-engine (location TBD) → tenant IVR (FlowRunner not
   built) → TTS-only / human queue / AI queue.
2. **Human-agent assist** — tenant AI-feature config sent to both the ingestion/STT service and
   the velentsAgents backend at call start; per transcript segment, the ingestion service fires
   webhooks to sentiment/intent/tool-rec/reply-rec services (reply-rec calls `text_agent` for N
   variations); each writes to MongoDB with KB-match %, fires a completion webhook to the
   backend, which pushes to the agent's browser over WebSocket. Raw call audio reaches the
   agent over the existing WebRTC/WSS softphone path, never through this pipeline. The
   STT-ingestion fork (direct tap vs. LiveKit room) is drawn as unresolved.
3. **AI-agent-handled + takeover** — the AI's own STT→LLM→TTS loop runs in parallel with the
   same sentiment/intent analysis, streamed to a human as a live transcript with tools-used;
   the human can optionally listen in (reusing the same ARI `externalMedia` snoop primitive
   already needed for supervisor whisper/barge), then requests takeover — an announcement
   plays, the AI leg drops, and the caller is bridged to the human agent.

## Still open (carried in the diagram's callout box, not resolved by the above answers)

- The STT-ingestion fork itself (direct tap vs. LiveKit room) — explicitly deferred.
- The call-engine's actual location/repo.
- The IVR FlowRunner — not built anywhere.
- Which service would do `tool_recommendation`'s KB lookup — not built anywhere, so its KB
  source is still genuinely open (see below for what's now resolved on the reply-rec side).
- Whether MongoDB is the right call as a new, platform-wide-unused datastore for this feature
  alone — see below, this framing turned out to be slightly wrong.

---

## Update 2026-07-19 — grounded against the real `text-agent` service

Read `E:\Projects\Velents\text-agent` directly (FastAPI app, `CLAUDE.md`, `src/services/knowledge_service.py`,
`src/services/weaviate_kb_service.py`, `src/database/connection.py`/`models.py`) to resolve two
of the open items against real code instead of inference. Diagram 2 and the callout box were
updated to match.

**Resolved — Tenant KB for the reply_recommendation branch.** `text-agent`'s live chat KB
retrieval is an HTTP call, not an embedded vector store: it rewrites the query using recent
conversation context, then `POST`s to `{KB_URL}/kb/retrieve` (default
`https://voice-agent-test.velents.ai`) with `{agent_id, query, top_k}`. So **`voice-agent` is
confirmed as the real KB backend** for this branch — `velentsAgents`' own KnowledgeBase model is
not used anywhere in it. `text-agent` also has a direct Weaviate client
(`weaviate_kb_service.py`), but that path is admin/dev-only (KB overview/health, DSPy
optimizer, simulator training data) — not the live-chat retrieval path. KB isolation is
per-`agent_id` (one Weaviate collection per agent), not per full tenant. Diagram 2 now names
`voice-agent` directly on the reply-rec → `text_agent` branch; the `tool_recommendation` branch
still points at an unresolved `Tenant KB (source TBD)`, since no service exists to check.

**New gap — no "N variations" capability, and no KB-match score.** `text-agent` has no
multi-variant generation endpoint anywhere — every chat/completion route
(`/api/v1/sessions/{id}/process`, DSPy `/agent/chat`) returns exactly one response. It also
discards whatever similarity/confidence score `voice-agent` might return, keeping only
`page_content`. So the originally proposed "`reply_recommendation` → `text_agent` for N
variations, with KB match %" needs two capabilities added upstream before it works as
described — this isn't a wiring gap, it's a missing feature in `text-agent`/`voice-agent`
themselves.

**Correction — MongoDB isn't a new technology.** The earlier critique flagged MongoDB as a
third datastore nothing else on the platform uses. That's not quite right: `text-agent` already
runs an async Motor client against MongoDB for session/conversation persistence (its own
`mlapi` database — `conversations`, `session_metrics`, DSPy prompt/log collections). Using
Mongo for AI-copilot task results would be a new *use case* on already-live infrastructure, not
a new stack to stand up — still worth deciding whether it shares `text-agent`'s Mongo instance
or gets a dedicated one, since `text-agent`'s Mongo connection is explicitly non-fatal/optional
and scoped to its own session model, not designed as a shared results store.
