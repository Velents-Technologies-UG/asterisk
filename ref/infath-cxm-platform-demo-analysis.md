# INFATH CXM Platform — Working Demo: Full Analysis

Source file: `INFATH CXM Platform.html` (this directory), a self-contained "bundled artifact" export (~3.4MB) downloaded from a claude.ai artifact. Analyzed on 2026-07-14 by decompressing its embedded assets directly (no Playwright/browser automation was available in-session — see "How this was analyzed" at the end). This is **not a static mockup** — it's an interactive, fully click-through prototype with a rich mock dataset simulating a live tenant.

## What it actually is, technically

The file is a "bundler" wrapper (see the inline comment `GENERATED from dc-runtime/src/*.ts — do not edit. Rebuild with 'cd dc-runtime && bun run build'`) that self-unpacks in the browser:

- **`<script type="__bundler/manifest">`** — JSON map of UUID → `{mime, compressed, data}`, each `data` a base64 (gzip-compressed) blob. Contains:
  - `0eec94ee-...` (58KB decompressed) — **dc-runtime**: a custom component runtime (the `<x-dc>` custom element system the template is written against — not React directly, though it looks React-shaped: `class Component` with `componentDidMount`/`componentDidUpdate`/`componentWillUnmount`).
  - `3f2dd30d-...` (163KB decompressed) — **the shared mock dataset** (`window.INFATH`), see below. Header comment: *"INFATH × Velents AgentHub — Shared prototype dataset. Arabic-first CXM platform. All data is invented but realistic."*
  - `5421fbd4-...` (611KB decompressed) — **lucide icon library v1.21.0** (icon set used throughout the UI).
  - ~26 embedded font files (`otf`/`ttf`/`woff2`) — "The Year of Handicrafts" (English display/headline), "Montserrat" (English body), "Cairo" (Arabic) — per the "Velents.ai — Colors & Type" brand sheet inlined in the template's `<style>`.
- **`<script type="__bundler/template">`** — the actual app: one JSON-escaped HTML string (868KB decompressed / 7,060 lines), a single `<x-dc>` root containing one giant `class Component` with **149 distinct methods** — effectively a monolithic single-page app.

There is no backend — everything is driven by the static `window.INFATH` dataset plus in-memory component state (`this.state`, `this.setStore()`). It's a click-through simulation, not a wired system.

## Product identity (from `data.workspace` / `data.tenant`)

| Field | Value |
|---|---|
| Name | إنفاذ / **INFATH** |
| Subtitle | منصة تجربة المستفيد / **Beneficiary Experience Platform** |
| Platform | **AgentHub** (matches the Linear project name) |
| Region | me-central2 · الدمام (**Dammam**) |
| Compliance | **PDPL**, **NCA ECC**, **NDMO** |
| Deployment | نشر سيادي داخل المحيط / **Sovereign on-prem / in-perimeter** |
| Tenant plan | Government Enterprise (حكومي — مؤسسي), 240 seats |

This matches the dossier's framing exactly: Arabic-first, in-region (Dammam), multi-tenant, PDPL/NCA-governed.

### The app suite (Odoo-style app switcher — `data.apps`)

INFATH CXM is modeled as **one app within a larger Velents "AgentHub" suite**, switchable via a top app-picker:

| App | Arabic | Purpose | Status here |
|---|---|---|---|
| `assistant` | المساعد / Ask Velents | Cross-tenant AI assistant | installed, inactive |
| **`cxm`** | تجربة العملاء / **Customer Experience** | **Omnichannel contact center — this demo** | **installed, active** |
| `build` | البناء / Build | Agents, feed, customers, campaigns | installed, inactive |
| `observe` | المراقبة / Observe | Workspaces, scorecards, org overview | installed, inactive |
| `support` | الدعم / Support | Tickets & helpdesk | installed, inactive |
| `voip` | الاتصالات / Telephony | Trunks, dial plan, numbers, queues | installed, inactive |

Only `cxm` is fleshed out in this build; the other five are stubs in the switcher (context for how AgentHub positions itself as a platform, not just a CXM point solution).

## Module catalog — what's "built" vs. roadmap (`data.nav`)

This is the single most useful artifact for cross-referencing against the Linear dossier: every module carries an explicit `built: true/false` flag, a licensing `tier`, and compliance flags. Grouped by the four left-rail sections (`data.moduleGroups`: **Engage · Route · Observe · Optimize**):

| Group | Module | Tier | Built (interactive)? | Sensitive/residency note |
|---|---|---|---|---|
| Engage | **Agent Desktop** | core | ✅ built | — |
| Engage | Beneficiary 360 | addon | ❌ roadmap | — |
| Engage | Outbound Campaigns | pro | ❌ roadmap | — |
| Engage | Knowledge Base | addon | ❌ roadmap, **not licensed** | — |
| Route | **Routing & Queues** | core | ✅ built | — |
| Route | **IVR Flow Designer** | pro | ✅ built | — |
| Observe | **Supervisor** | core | ✅ built | monitoring/barge requires consent, written to audit log |
| Observe | **Recording & Quality** | pro | ✅ built | stored in me-central2 (Dammam), PDPL-compliant, auto-redaction |
| Observe | **Analytics** | core | ✅ built | — |
| Optimize | **Agents & WFM** | pro | ✅ built | — |
| Optimize | Quality Management | pro | ❌ roadmap | — |
| Optimize | Voice of Customer | addon | ❌ roadmap | — |

**7 of 11 modules are fully interactive** in this build: Agent Desktop, Routing & Queues, IVR Designer, Supervisor, Recording & Quality, Analytics, WFM. The other 4 (Beneficiary 360, Outbound Campaigns, Knowledge Base, Quality Management, Voice of Customer) exist only as catalog entries in the Module Manager (`selectView('modules')`), not as working screens — worth checking against the dossier's P0–P5 numbering (the built set reads like a P0+P1+P2 core slice; the roadmap set lines up with P3's omnichannel/outbound/depth layer and P4/P5 items like VoC).

## Personas / role switcher (`data.personas`)

A global role switcher drives which view loads by default and (implicitly) which capabilities are visible:

| Persona | Arabic | Default view |
|---|---|---|
| Service Agent | موظف الخدمة | agent-desktop |
| Supervisor | مشرف | supervisor |
| Team Leader | تيم ليدر | supervisor |
| QA Inspector | مفتش الجودة | recordings |
| Builder / Admin | باني الوكلاء | ivr |
| Leadership | القيادة | analytics |
| AI Agent | الوكيل الذكي | agent-desktop |
| Beneficiary | المستفيد | agent-desktop |

## The flagship simulated scenario

All the demo data is grounded in one running example — a **travel-ban lifting case** (منع سفر), tightly coupled to real Saudi judicial-execution terminology:

- **Beneficiary**: محمد سالم الزهراني (Mohammed S. Alzahrani), respondent/debtor (منفّذ ضده), ID 1078945521, Nafath-verified, from Jeddah, sentiment negative/declining.
- **Case**: #70234551, financial claim (مطالبة مالية) against شركة الراجحي للتمويل, amount 48,500 SAR, fully paid 2026-06-21, but travel ban still active — urgent because of travel within 24 hours; the lift requires judicial review (needs human handoff — the AI copilot handles first contact, then hands off).
- This exact scenario is what appeared in your screenshot: the AI-suggested action "إنشاء طلب رفع منع سفر مستعجل" and KB match "إجراءات رفع منع السفر بعد سداد كامل المبلغ" (96% match, KB-204).
- **Queues** modeled around this domain: التنفيذ — منع السفر (Enforcement — travel ban), النفقة (Alimony), الحجز والاعتراضات (Seizure & objections), الدعم العام (General support), الديون التجارية (Commercial debts).

## Real system integrations modeled (`data.settings.integrations`)

These are the actual external systems INFATH would integrate with — not invented placeholders, but named real Saudi government/commercial systems:

| Integration | Purpose | Status in demo |
|---|---|---|
| **Nafath** (نفاذ) | National SSO / identity verification | connected |
| **Najiz** (ناجز) — Ministry of Justice | Judicial case & procedure data | connected |
| **Hollat** (حصّالة) | Collections & e-payments | connected |
| Generic CRM | Beneficiary record sync | disconnected |

Channel connectors also modeled: SIP voice trunk (STC Business, DID 920-001-700), WhatsApp Business (via Unifonic KSA), web chat (infath.gov.sa widget), email (care@infath.gov.sa), X/social and SMS (both scaffolded but disconnected).

## Feature inventory (derived from the 149 component methods)

- **Call handling**: answer/decline offer, mute/hold/end, DTMF, screen-pop accept/dismiss, consult, warm/cold transfer (`closeXfer`, `completeXfer`, `cancelConsult`), voicemail.
- **IVR Flow Designer**: visual node editor (add/edit/delete nodes and branches), pan/zoom/fit/minimap, import/export/diff, publish with versioning and rollback, live flow simulator (`simStart`/`simNoInput`/`simReset`). Three flows modeled: main unified line (live, v7), after-hours (live, v4), collections outbound campaign (**draft**, v2).
- **Supervisor**: live KPI tiles with sparklines and interval comparisons (live/30m/shift/today) for active calls, waiting, avg wait, service level, AHT, occupancy, abandon rate; per-queue drill-down with live interactions; per-agent roster with status/AHT/adherence/sentiment/score; SLA-breach and disconnect alerts; broadcast messaging; bulk queue rebalancing; listen/whisper/barge (consent-gated, audit-logged).
- **Outbound**: preview/predictive-style dialer scaffolding (`obCancelCall`, `obToggleHold/Mute`, `obSave`, `obClosePreview`).
- **Omnichannel inbox**: voice, WhatsApp, web chat, email, X — unified inbox with per-conversation SLA countdowns; concurrent chat handling (capacity 3, response-due timers).
- **AI Copilot**: live sentiment score, detected intent, call summary, next-best-actions, suggested replies (editable/regeneratable), KB match suggestions with confidence %, a compliance checklist (identity verified, statutory timelines mentioned, no creditor-sensitive disclosure) — i.e. real-time compliance guardrailing baked into the assist UI, not just an afterthought.
- **WFM**: agent skill/adherence/AHT/score roster, aux/break codes, gamification note (weekly performance + points).
- **Recording & Quality**: filterable recording list with QA scores, PII/PCI auto-masking, weighted QA scoring criteria (identity check, statutory-timeline adherence, empathy/tone, non-disclosure of creditor data, correct disposition code), 180-day retention policy.
- **Governance / RBAC**: 14-capability matrix (`cxm.*` keys) across 7 system+custom roles (CC Admin, Supervisor, Agent, QA Inspector, Flow Builder, WFM Planner, Leadership), user/team management with invite flow, **maker-checker approval workflow** for sensitive settings changes.
- **Audit log**: append-only, human-readable trail (who/what/when) spanning recording access, IVR publishes, barge-ins, settings changes, role edits, cross-border-transfer blocks, integration connections.
- **Command palette** (Cmd+K style: `toggleCmdk`), bilingual toggle (`toggleLang` — the EN/AR switch visible top-left in your screenshot), simulation/chaos toggles for demoing resilience (`netDrop`/`netRecoverNow`, `toggleSimApiFail`, `toggleSimAfterHours`).

## Settings taxonomy (`data.settings`, 13 numbered sections)

1. **Channel connectors** — per-channel provider/DID/status.
2. **RBAC capability matrix** — 14 `cxm.*` capabilities × role.
2b. **Users & teams** — the administration unit, each user joins a role.
2c. **Maker-checker** — settings requiring a second approver before going live.
3. **Recording & retention** — per-channel recording toggle, PII/PCI redaction, consent mode (announce/explicit/off), retention days (default 180), AES-256 encryption (locked), region locked to Dammam.
2d. **Compliance guardrails (legal floors)** — hard-coded citations that block unsafe changes: retention floor 90 days (PDPL), mandatory voice recording (NCA ECC), no disabling recording without lawful basis (PDPL), no disabling NDMO governance.
4. **Compliance & governance** — residency lock (in-Kingdom, cross-border transfer hard-locked off), anonymization toggle, framework status (PDPL enforced, NCA ECC Level 3, NDMO compliant), data-subject rights (export/right-to-erasure).
5. **Governance: approvals + audit** — pending approvals queue, audit trail.
6. **General/organization** — language mode, date format, numerals, week start.
7. **Agent desktop** — ACW required + timeout, auto-answer, max concurrent chats, screen-pop, sentiment alerts.
8. **Routing defaults** — strategy, SLA %/sec targets, abandon/overflow timers, callback offer, priority aging.
9. **IVR & flow defaults** — TTS voice (Microsoft Hamed ar-SA default), locale, max retries, holiday/after-hours routing, DTMF timeout.
10. **Workforce management** — shrinkage %, adherence tolerance, schedule horizon, overtime approval, auto-schedule.
11. **Analytics & reporting** — SLA/AHT/CSAT targets, scheduled reports, anomaly alerts, refresh interval.
12. **Integrations, API keys, webhooks** — see integrations table above; masked API keys; webhook endpoints (interaction.*, sla.breach).
13. **Notification routing** — per-event email/in-app toggles (SLA breach, missed callback, new QA eval, queue surge, escalation).

**This settings model is effectively a worked answer to the dossier's AGH-6685 dependency** (retention/backup/restore policy with NCA/PDPL implications) — it proposes a concrete 90-day PDPL retention floor, AES-256 encryption, Dammam-locked storage, and a compliance-guardrail mechanism that blocks unsafe changes at the UI level. Worth treating as a design proposal to validate against whatever AGH-6685 ultimately resolves to, not as an already-approved policy.

## Routing detail (`data.routing`)

- **Methods**: skills-based, priority, least-busy, longest-wait, **AI predictive** (5 strategies).
- **Queues** (5, matching the flagship scenario's domain): التنفيذ — منع السفر (9 agents, predictive routing, SLA 20s target), النفقة (6 agents, skill-based, SLA 30s), الحجز والاعتراضات (5 agents, priority, SLA 30s — shown breaching at 71% in the supervisor alerts), الدعم العام (8 agents, least-busy, SLA 45s), الديون التجارية (4 agents, longest-wait, SLA 60s).
- Callback offering, overflow targets, tier expansion, VIP lists (government entities, VIP beneficiaries, media-sensitive cases), Saudi holiday calendar (Eid al-Fitr, Eid al-Adha, National Day, Founding Day), AST timezone.

## Notes on how comprehensive this is

At 149 methods and 33 top-level dataset sections, this prototype goes well beyond a UI mockup — it's a genuinely thorough simulation of the target product's information architecture, including things easy to omit in a demo (a maker-checker approval flow, hard-coded compliance guardrails with legal citations, a chaos/resilience-simulation panel, an audit trail with realistic entries). Treat it as a strong reference for scope and UX intent, but remember: **no backend, no real integrations, all data invented** — it should inform product/UX decisions, not be mistaken for validated policy (especially the compliance guardrail numbers, e.g. the 90-day retention floor, which read as a plausible proposal rather than a confirmed legal requirement).

## How this was analyzed

No Playwright/browser-automation tool was available in this session, so the app wasn't driven interactively. Instead, the manifest's base64+gzip blobs and the JSON-escaped template string were extracted and decompressed directly (via a small Node script using `zlib.gunzipSync`), which is exhaustive rather than sampling a few clicked-through screens — every method name, every dataset section, and the full settings/RBAC/compliance model were readable in full. What this approach *can't* verify: actual rendered visual layout, CSS correctness, or runtime behavior/bugs in the `dc-runtime` — for that, the file still needs to be opened in a real browser.
