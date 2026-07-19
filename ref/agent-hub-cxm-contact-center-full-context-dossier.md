# Agent Hub CXM — Contact Center — Full Context Dossier

Generated on 2026-07-13

## What this document is

This dossier is intended to be the most complete single-file context pack for the **Agent Hub CXM — Contact Center** project. It combines:

- project-level strategy and sequencing context
- milestone / phase structure
- execution and readiness assessment
- parent issue hierarchy
- sub-issue hierarchy
- issue metadata
- dependency notes
- explicit refinements captured in comments
- risk and external dependency context

Where issue descriptions or acceptance details were authored in the project, they are preserved as closely as possible in this dossier. The goal is to make this a working source of truth for planning, review, handoff, and execution sequencing.

---

## Project overview

**Project:** Agent Hub CXM — Contact Center  
**Team:** Agent Hub  
**Status:** Backlog  
**Priority:** High  
**Created:** 2026-06-28  
**Progress ratio:** ~0.25%

### Core framing

This project is structured as a **multi-tenant, Arabic-first, in-region contact-center product**. The launch client is **INFATH**, but the project is explicitly framed so INFATH-specific behavior should be implemented as **tenant configuration**, not hardcoded product logic.

The project is phased deliberately:

- **P0** establishes the voice engine spine and critical production path.
- **P1** builds the foundations that should be implemented early, even when they are not all immediate blockers.
- **P2** activates live operations that depend on the P0 runtime spine.
- **P3** expands into depth capabilities, omnichannel, outbound, productivity, and richer operational workflows.
- **P4** covers hardening and roadmap-timed work.
- **P5** focuses on productization and second-client readiness.

### Strategic intent

The project is not just a feature bundle. It is a platformization effort. The structure implies three simultaneous goals:

- stand up a working live contact-center runtime,
- keep implementation compliant and in-region,
- avoid turning launch-client requirements into one-off hardcoded behavior that prevents future productization.

---

## Full project description context

The project is designed around a phased contact-center build-out with explicit sequencing from telephony foundations to live operations, reporting, governance, omnichannel, outbound, productivity, and eventual second-client productization.

The description establishes these key ideas:

- The product must support **Arabic-first** operations.
- The product must run **in-region**, specifically with Dammam deployment constraints.
- The system must be **multi-tenant**, with launch-client specifics isolated into configuration.
- The work should be executed in **P0 → P5** order, where later phases deepen product value but do not replace the need to finish the critical path first.
- A specific external dependency exists for **recording retention / backup / restore policy**, tracked in **AGH-6685**, and that input is explicitly treated as an early dependency rather than a late compliance afterthought.

---

## Milestones / phases

- **E2E Golden Path** — superseded planning unit / historical planning artifact
- **P0 Voice Engine Spine** — target 2026-08-31
- **P1 Build-Now Foundations** — target 2026-09-15
- **P2 Live Operations** — target 2026-10-31
- **P3 Depth, Omnichannel & Proactive** — target 2026-12-15
- **P4 Hardening & Roadmap-Timed** — target 2027-03-31
- **P5 Productize — Second-Client Readiness** — target 2027-03-31

### Phase sizing read

- **P0** is small in count but highest in leverage.
- **P1** is a large foundation bucket and should reduce rework risk if staffed early.
- **P2** depends materially on the live event/session spine from P0.
- **P3** is the largest expansion layer and covers many depth capabilities.
- **P4** is currently light.
- **P5** is strategic and explicitly future-facing.

---

## Execution snapshot

### Issue inventory

- Total issues in project: **100**
- Parent issues: **13**
- Sub-issues: **87**
- Status mix:
  - In Progress: 1
  - Backlog: 98
  - Canceled: 1
- Assigned to Abdullah Nashaat: 12
- Unassigned: 88

### What that means

The project is strongly decomposed and well structured, but active execution has barely spread beyond the initial critical path. In practical terms, this is still much closer to a well-architected delivery program than a broadly active implementation board.

### Immediate execution reality

Only **AGH-6655 — Inc 1: Voice Engine Spine** is in progress. Nearly everything else is still in backlog, which means:

- the dependency model is defined,
- the work breakdown is largely done,
- but staffing / activation at the sub-issue level is still limited.

---

## Critical-path and dependency summary

### Most important external dependency

**AGH-6685** is the clearest named outside dependency. It covers retention, backup, and restore policy input with NCA / PDPL implications. The note attached to it makes clear this is the single explicit early external input that should be requested immediately.

### High-level dependency chain

- **AGH-6655** blocks:
  - AGH-6656
  - AGH-6658
  - AGH-6747
  - AGH-6662
  - AGH-6663
- **AGH-6656** is blocked by:
  - AGH-6655
  - AGH-6657
- **AGH-6657** blocks:
  - AGH-6656
  - AGH-6658
- **AGH-6661** blocks:
  - AGH-6662
- **AGH-6662** is blocked by:
  - AGH-6661
  - AGH-6655
- **AGH-6663** is blocked by:
  - AGH-6655
- **AGH-6747** is blocked by:
  - AGH-6655
  and blocks:
  - AGH-6705
- **AGH-7315** relates to:
  - AGH-6685
  - AGH-6760

### Operational interpretation

The project is intentionally broad, but the actual executable path is narrow:

1. complete P0,
2. advance P1 foundations in parallel,
3. unlock P2 live operations,
4. then deepen into P3/P4/P5 without prematurely spreading effort.

---

## Parent issues and full context

---

## AGH-6655 — Inc 1: Voice Engine Spine

### Metadata

- Status: In Progress
- Priority: High
- Milestone: P0 Voice Engine Spine
- Assignee: Abdullah Nashaat
- Role in program: foundational runtime spine / primary critical path

### Description

This parent issue covers the core voice runtime and session/event foundation required for the contact-center platform to handle telephony end to end. It is the central program unlock because downstream live desktop, supervisor intervention, outbound behavior, reporting, and IVR execution depend on the existence of canonical sessions and reliable call-event flow.

The scope is fundamentally about making live voice interactions real, observable, and routable in a multi-tenant and in-region-compliant product context.

### Dependency notes

Blocks:
- AGH-6656
- AGH-6658
- AGH-6747
- AGH-6662
- AGH-6663

### Child issues

#### AGH-6664 — P0.1 SIP trunk / telephony edge connectivity
- Status: Backlog
- Priority: High
- Milestone: P0 Voice Engine Spine
- Assignee: Unassigned
- Description:
  Establish inbound and outbound telephony edge connectivity so calls can enter and leave the platform through the required telephony path. This is the earliest runtime path needed to turn the project from planning into executable call handling.
- Acceptance / refinement details:
  - A comment adds an explicit measurable UAT expectation: **an inbound call should be offered to routing within 2 seconds of trunk answer at P95**.
- Notes:
  This is one of the most foundational sub-issues in the entire project because it precedes all routing and desktop behavior.

#### AGH-6670 — P0.2 Canonical call session and event model
- Status: Backlog
- Priority: High
- Milestone: P0 Voice Engine Spine
- Assignee: Unassigned
- Description:
  Define and implement the canonical contact-center session and event model so the system has a stable runtime abstraction for call lifecycle, state transitions, and event propagation.
- Notes:
  This is a structural prerequisite for reporting, routing, supervision, and auditability.

#### AGH-6681 — P0.3 In-region media / capture path
- Status: Backlog
- Priority: High
- Milestone: P0 Voice Engine Spine
- Assignee: Unassigned
- Description:
  Ensure media handling / capture path is compatible with the in-region deployment model and foundational compliance expectations.
- Notes:
  This supports later recording and audit constraints.

#### AGH-6688 — P0.4 Click-to-call / call initiation spine
- Status: Backlog
- Priority: High
- Milestone: P0 Voice Engine Spine
- Assignee: Unassigned
- Description:
  Implement the base initiation path for contact-center originated voice interactions so the desktop and workflow layers can trigger supported calls through the shared runtime spine.

#### AGH-6695 — P0.5 Screen-pop / customer context handoff spine
- Status: Backlog
- Priority: High
- Milestone: P0 Voice Engine Spine
- Assignee: Unassigned
- Description:
  Deliver the core handoff that lets routing/runtime context reach the live agent experience, enabling screen-pop and consistent context transfer.

#### AGH-7262 — P0.6 DR posture / resilience for voice spine
- Status: Backlog
- Priority: High
- Milestone: P0 Voice Engine Spine
- Assignee: Unassigned
- Description:
  Establish the disaster-recovery / resilience posture for the voice spine so the runtime foundation is not purely happy-path.

---

## AGH-6656 — Inc 2: Unified Agent Desktop (live)

### Metadata

- Status: Backlog
- Priority: High
- Milestone: P2 Live Operations
- Assignee: Abdullah Nashaat

### Description

This parent issue covers the live agent operating surface: the desktop experience used while handling real-time voice interactions. It depends on runtime and routing foundations, and it is one of the first areas where the platform becomes tangible to agents.

### Dependency notes

Blocked by:
- AGH-6655
- AGH-6657

Blocks:
- AGH-7309

### Child issues

#### AGH-6665 — Agent live call workspace shell
- Status: Backlog
- Priority: High
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Build the live workspace shell used during active call handling.

#### AGH-6669 — Live customer context / identity display
- Status: Backlog
- Priority: High
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Surface caller / customer context to the agent during live sessions.

#### AGH-6673 — Agent call controls
- Status: Backlog
- Priority: High
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Provide the core live-call controls needed for day-to-day voice handling.

#### AGH-6679 — After-call / wrap-up flow
- Status: Backlog
- Priority: High
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Define the post-call agent flow for wrap-up and call completion handling.

#### AGH-6687 — Agent notes / interaction capture in live context
- Status: Backlog
- Priority: High
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Support live note-taking and interaction capture in the agent desktop.

#### AGH-7263 — Live desktop readiness / UX finishing slice
- Status: Backlog
- Priority: Medium
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Covers readiness and finishing work for the live desktop beyond the basic shell.

---

## AGH-6657 — Inc 3: Routing & ACD parity

### Metadata

- Status: Backlog
- Priority: High
- Milestone: P1 Build-Now Foundations
- Assignee: Abdullah Nashaat

### Description

This parent issue covers the routing, distribution, queueing, and parity logic required for contact-center operation. It is a foundation phase area because even though some live operations wait on P0, the logic and rules here should be built early to prevent later rework.

### Dependency notes

Blocks:
- AGH-6656
- AGH-6658

### Child issues

#### AGH-6667 — Queue model / queue primitives
- Status: Backlog
- Priority: High
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Define queue primitives and queue model behavior used by routing.

#### AGH-6677 — Skill / rule-based routing configuration
- Status: Backlog
- Priority: High
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Implement configurable routing rules and skill-aware assignment behavior.

#### AGH-6683 — Agent availability / eligibility logic
- Status: Backlog
- Priority: High
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Establish how agent readiness and eligibility participate in routing decisions.

#### AGH-6691 — Queue prioritization and distribution policies
- Status: Backlog
- Priority: High
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Support routing policy variations and queue prioritization behavior.

#### AGH-6696 — Overflow / fallback routing
- Status: Backlog
- Priority: Medium
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Define fallback behavior when ideal routing paths are not available.

#### AGH-6699 — SLA-sensitive routing / timers
- Status: Backlog
- Priority: Medium
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Add timer- and SLA-sensitive routing logic.

#### AGH-6703 — Transfer routing parity
- Status: Backlog
- Priority: Medium
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Ensure transfer behaviors align with target contact-center operating model.

#### AGH-6706 — Requeue / retry handling
- Status: Backlog
- Priority: Medium
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Handle requeue and retry scenarios correctly.

#### AGH-6708 — Queue visibility / state instrumentation
- Status: Backlog
- Priority: Medium
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Expose queue states in a way that downstream reporting and supervision can consume.

#### AGH-6710 — Multi-tenant routing isolation
- Status: Backlog
- Priority: Medium
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Ensure routing logic remains safely tenant-scoped.

#### AGH-7264 — Advanced routing parity follow-up
- Status: Backlog
- Priority: Medium
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Follow-up routing parity slice.

#### AGH-7265 — Routing edge-case hardening slice
- Status: Backlog
- Priority: Medium
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Hardening work for routing edge cases.

---

## AGH-6658 — Inc 4: Supervisor live ops

### Metadata

- Status: Backlog
- Priority: High
- Milestone: P2 Live Operations
- Assignee: Abdullah Nashaat

### Description

This parent issue covers live supervisor capabilities: operational awareness, intervention, coaching, monitoring, and runtime management needed to run a real contact center. It is one of the heaviest issue groups in the entire project.

### Dependency notes

Blocked by:
- AGH-6655
- AGH-6657

### Child issues

#### AGH-6666 — Supervisor live board / runtime visibility
- Status: Backlog
- Priority: High
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Create a live supervisor view into active operations.

#### AGH-6674 — Live queue / agent state supervision
- Status: Backlog
- Priority: High
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Show live queue and agent state information for operational supervision.

#### AGH-6682 — Supervisor intervention controls
- Status: Backlog
- Priority: High
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Provide intervention controls for live supervision.

#### AGH-6692 — Whisper / barge / monitor baseline
- Status: Backlog
- Priority: High
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Support the core supervisor intervention modes expected in live operations.

#### AGH-6697 — Intrusive action consent / audit safeguards
- Status: Backlog
- Priority: High
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Define and implement guardrails around intrusive supervisor actions such as monitor / whisper / barge.
- Acceptance / refinement details from comments:
  - on decline, nothing is logged as performed
  - consent banner must name the specific PDPL / NCA legal basis
  - audit entry must be written before audio bridges are created

#### AGH-6700 — Live takeover / escalation handling
- Status: Backlog
- Priority: Medium
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Support escalation / takeover behavior in live operational scenarios.

#### AGH-6702 — Supervisor-triggered transfer / reassignment tools
- Status: Backlog
- Priority: Medium
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Provide tools for active supervisor-directed call movement.

#### AGH-6704 — Live alerts for SLA / operational events
- Status: Backlog
- Priority: Medium
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Trigger supervisor-facing alerts for important runtime conditions.

#### AGH-6707 — Queue congestion handling / watchpoints
- Status: Backlog
- Priority: Medium
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Add operational watchpoints and congestion visibility.

#### AGH-6709 — Agent state override / administrative action
- Status: Backlog
- Priority: Medium
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Permit controlled administrative supervisor actions on agent state.

#### AGH-6711 — Live call inspection support
- Status: Backlog
- Priority: Medium
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Let supervisors inspect active interaction state in more detail.

#### AGH-6712 — Team-level live operations views
- Status: Backlog
- Priority: Medium
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Add higher-level views for live team operations.

#### AGH-6713 — Exception / incident runtime surfacing
- Status: Backlog
- Priority: Medium
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Surface live runtime exceptions or operational incidents.

#### AGH-6714 — Supervisor operational notes / flags
- Status: Backlog
- Priority: Low
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Capture supervisor notes or flags within live workflows.

#### AGH-6715 — Live coaching support
- Status: Backlog
- Priority: Low
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Enable coaching-oriented live supervisor workflows.

#### AGH-6716 — Final supervisor ops finishing slice
- Status: Backlog
- Priority: Low
- Milestone: P2 Live Operations
- Assignee: Unassigned
- Description:
  Remaining finishing work for supervisor live operations.

---

## AGH-6659 — Inc 5: Recording Management

### Metadata

- Status: Backlog
- Priority: High
- Milestone: P1 Build-Now Foundations
- Assignee: Abdullah Nashaat

### Description

This issue covers recording controls, retention policy, and operational handling of call recording within compliance constraints.

### Child issues

#### AGH-6668 — Recording control baseline
- Status: Backlog
- Priority: High
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Establish baseline recording management controls.

#### AGH-6676 — Recording access / console / search baseline
- Status: Backlog
- Priority: High
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Add management and retrieval capabilities for recordings.

#### AGH-6685 — Retention / backup / restore policy input
- Status: Backlog
- Priority: High
- Milestone: P1 Build-Now Foundations
- Due date: 2026-07-19
- Assignee: Unassigned
- Description:
  Request and lock the compliance / policy inputs for recording retention, backup, and restore handling.
- Important comment / PM refinement:
  - request **NCA / PDPL** recording-retention policy reference and per-category durations from INFATH immediately
  - this is the **single explicit external P1 critical-path input**
  - the due date refers to the request / policy input timing, not to the complete implementation build

---

## AGH-6660 — Inc 6: Omnichannel completion

### Metadata

- Status: Backlog
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Abdullah Nashaat

### Description

This issue expands the platform from voice-first into broader interaction-channel completion, aligning the contact-center runtime with richer service operations.

### Child issues

#### AGH-6672 — Omnichannel interaction model extension
- Status: Backlog
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Extend the interaction model beyond voice.

#### AGH-6680 — Agent omnichannel handling baseline
- Status: Backlog
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Bring omnichannel handling into the agent experience.

#### AGH-6690 — Omnichannel operational handling / parity
- Status: Backlog
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Add channel-complete operational behavior.

---

## AGH-6661 — Inc 7: Agent / Workforce management

### Metadata

- Status: Backlog
- Priority: Medium
- Milestone: P1 Build-Now Foundations
- Assignee: Abdullah Nashaat

### Description

This issue handles the agent / workforce management layer that influences readiness, staffing, and operational visibility.

### Dependency notes

Blocks:
- AGH-6662

### Child issues

#### AGH-6671 — Agent profile / staffing model baseline
- Status: Backlog
- Priority: Medium
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Create the baseline agent staffing / capability model.

#### AGH-6684 — Agent state and schedule / readiness rules
- Status: Backlog
- Priority: Medium
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Define the agent-side rules that affect participation in operations.

#### AGH-6694 — Workforce operational administration
- Status: Backlog
- Priority: Medium
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Add operational administration capabilities for workforce handling.

#### AGH-7266 — Workforce follow-up / hardening slice
- Status: Backlog
- Priority: Low
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Follow-up slice for workforce management.

---

## AGH-6662 — Inc 8: CC dashboards & reports

### Metadata

- Status: Backlog
- Priority: High
- Milestone: P1 Build-Now Foundations
- Assignee: Abdullah Nashaat

### Description

This issue covers the reporting and dashboarding layer required for live contact-center management, contractual reporting, and operational performance visibility.

### Dependency notes

Blocked by:
- AGH-6661
- AGH-6655

### Child issues

#### AGH-6675 — KPI defaults / contractual reporting baseline
- Status: Backlog
- Priority: High
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Establish the baseline KPI and contractual reporting expectations used in dashboards and reports.
- Comment refinement:
  - service level default: **95% within 30s**
  - average handle time default: **4.5 minutes**
  - abandonment default: **≤ 5%**
  - quality default: **90%**
  - reporting should reconcile with CDR within **±1%**

#### AGH-6686 — Supervisor / operations dashboard views
- Status: Backlog
- Priority: Medium
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Add supervisory / operational dashboard views.

#### AGH-6693 — Historical / export / reporting completeness slice
- Status: Backlog
- Priority: Medium
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Expand beyond baseline dashboards into fuller reporting outputs.

---

## AGH-6663 — Inc 9: IVR & self-service (+ P1-late)

### Metadata

- Status: Backlog
- Priority: High
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Abdullah Nashaat

### Description

This issue addresses IVR and self-service capability. The naming indicates part of the groundwork belongs earlier, but a fuller experience is planned later.

### Dependency notes

Blocked by:
- AGH-6655

### Child issues

#### AGH-6678 — IVR foundation / flow baseline
- Status: Backlog
- Priority: High
- Milestone: P1 Build-Now Foundations or P3-linked groundwork
- Assignee: Unassigned
- Description:
  Establish IVR flow foundations.

#### AGH-6689 — Self-service path handling
- Status: Backlog
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Add self-service handling paths.

#### AGH-6698 — IVR routing / escalation integration
- Status: Backlog
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Connect IVR paths with routing and escalation handling.

#### AGH-6701 — Voice drawer copilot / earlier concept
- Status: Canceled
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  This item was originally part of the voice-drawer / assist direction.
- Cancellation note from comments:
  - canceled as superseded by **AGH-6749**
  - the later Inc 10 framing replaces the earlier voice-drawer-only copilot idea with a broader agent assist direction

#### AGH-6705 — Callback / IVR interaction dependency slice
- Status: Backlog
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Related to IVR / callback continuation flows.
- Relation note:
  - blocked by parent outbound track via AGH-6747

#### AGH-7267 — IVR follow-up / finishing slice
- Status: Backlog
- Priority: Low
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Follow-up / finishing work for IVR and self-service.

---

## AGH-6746 — Inc 10: Agent Assist & Productivity

### Metadata

- Status: Backlog
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Abdullah Nashaat

### Description

This issue expands the product into agent assistance and productivity improvements. It is one of the largest work buckets and appears to absorb earlier narrower assist concepts into a broader framework.

### Child issues

#### AGH-6749 — Agent assist baseline / superseding assist direction
- Status: Backlog
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Establish the broad baseline for agent assist capabilities.
- Notes:
  Supersedes the narrower canceled AGH-6701 concept.

#### AGH-6752 — Knowledge / suggestion assist slice
- Status: Backlog
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Bring assistive suggestions into agent workflow.

#### AGH-6755 — Productivity action acceleration slice
- Status: Backlog
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Reduce repetitive work through assisted actions.

#### AGH-6758 — Assistive contextual guidance
- Status: Backlog
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Provide context-sensitive guidance to the agent.

#### AGH-6761 — Summary / wrap-up assistance
- Status: Backlog
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Help agents with post-interaction summaries or completion assistance.

#### AGH-6762 — Agent assist interaction intelligence slice
- Status: Backlog
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Add intelligence to in-flow agent support.

#### AGH-6764 — Suggested next-best action / productivity support
- Status: Backlog
- Priority: Low
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Surface possible next actions or workflow accelerators.

#### AGH-6765 — Assist telemetry / quality observation slice
- Status: Backlog
- Priority: Low
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Add observation / telemetry around assist behavior.

#### AGH-6766 — Assist UX / interaction pattern support
- Status: Backlog
- Priority: Low
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Refine assist interaction UX patterns.

#### AGH-6767 — Assist controls / configuration slice
- Status: Backlog
- Priority: Low
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Add controls and configuration boundaries for assist behavior.

#### AGH-6768 — Assist hardening / completeness slice
- Status: Backlog
- Priority: Low
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Hardening and completeness work for assist capabilities.

#### AGH-7268 — Additional assist follow-up slice
- Status: Backlog
- Priority: Low
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Follow-up assist work.

---

## AGH-6747 — Inc 11: Outbound & Callback

### Metadata

- Status: Backlog
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Abdullah Nashaat

### Description

This issue handles outbound contact-center flows and callback capabilities, which rely on the shared voice spine and must connect cleanly with customer journey continuation paths.

### Dependency notes

Blocked by:
- AGH-6655

Blocks:
- AGH-6705

### Child issues

#### AGH-6750 — Outbound foundation / call initiation rules
- Status: Backlog
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Establish outbound calling foundations.

#### AGH-6753 — Callback scheduling / request handling
- Status: Backlog
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Support callback request handling.

#### AGH-6756 — Outbound / callback operational controls
- Status: Backlog
- Priority: Medium
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Add operational controls for outbound and callback flows.

#### AGH-6759 — Outbound / callback reporting or completion slice
- Status: Backlog
- Priority: Low
- Milestone: P3 Depth, Omnichannel & Proactive
- Assignee: Unassigned
- Description:
  Complete the outbound / callback set with reporting or follow-up capabilities.

---

## AGH-6748 — Inc 12: Governance, RBAC & Audit

### Metadata

- Status: Backlog
- Priority: High
- Milestone: P1 Build-Now Foundations
- Assignee: Abdullah Nashaat

### Description

This issue addresses access control, governance, and auditability. It is a foundation area because compliance and operational integrity depend on it, especially in a sensitive regulated environment.

### Child issues

#### AGH-6751 — RBAC baseline
- Status: Backlog
- Priority: High
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Define the baseline role and permission model.

#### AGH-6754 — Admin / operational governance controls
- Status: Backlog
- Priority: High
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Add governance controls for operational administration.

#### AGH-6757 — Audit trail baseline
- Status: Backlog
- Priority: High
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Ensure key actions are auditable.

#### AGH-6760 — Policy-sensitive governance handling
- Status: Backlog
- Priority: High
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Cover governance behaviors with compliance-sensitive implications.
- Relation note:
  Later productization work AGH-7315 relates to this issue.

#### AGH-6763 — Audit reporting / governance completeness slice
- Status: Backlog
- Priority: Medium
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Add reporting / completeness on governance behaviors.

#### AGH-7269 — Governance follow-up / hardening slice
- Status: Backlog
- Priority: Low
- Milestone: P1 Build-Now Foundations
- Assignee: Unassigned
- Description:
  Follow-up / hardening work for governance and audit.

---

## AGH-7309 — Inc 13: Productization — Second-Client Readiness

### Metadata

- Status: Backlog
- Priority: Medium
- Milestone: P5 Productize — Second-Client Readiness
- Assignee: Unassigned

### Description

This issue converts the launch-client implementation into a cleaner reusable product surface. It is the clearest proof that the project is intended to become a reusable product, not a one-off deployment.

### Dependency notes

Blocked by:
- AGH-6656

### Child issues

#### AGH-7310 — Tenantization gap closure
- Status: Backlog
- Priority: Medium
- Milestone: P5 Productize — Second-Client Readiness
- Assignee: Unassigned
- Description:
  Close tenantization gaps so launch-client specifics remain configurable.

#### AGH-7311 — Configuration / defaults productization
- Status: Backlog
- Priority: Medium
- Milestone: P5 Productize — Second-Client Readiness
- Assignee: Unassigned
- Description:
  Turn client-specific assumptions into reusable defaults and configuration.

#### AGH-7312 — Product boundary cleanup
- Status: Backlog
- Priority: Medium
- Milestone: P5 Productize — Second-Client Readiness
- Assignee: Unassigned
- Description:
  Clean product boundaries and remove one-off behaviors.

#### AGH-7313 — Deployment / onboarding repeatability slice
- Status: Backlog
- Priority: Medium
- Milestone: P5 Productize — Second-Client Readiness
- Assignee: Unassigned
- Description:
  Improve repeatability for future client rollout.

#### AGH-7314 — Documentation / packaging / readiness slice
- Status: Backlog
- Priority: Medium
- Milestone: P5 Productize — Second-Client Readiness
- Assignee: Unassigned
- Description:
  Package the product more cleanly for future adoption.

#### AGH-7315 — Productization of policy-sensitive behavior
- Status: Backlog
- Priority: Medium
- Milestone: P5 Productize — Second-Client Readiness
- Assignee: Unassigned
- Description:
  Ensure policy-sensitive areas are abstracted in a reusable way.
- Relation notes:
  - relates to AGH-6685
  - relates to AGH-6760

---

## Cross-cutting observations

### 1. The board is better decomposed than staffed

The hierarchy is strong. The logic is coherent. But nearly all child issues remain unassigned. This is a planning-complete / activation-incomplete project.

### 2. P0 is tiny in count but dominant in leverage

Until the voice runtime spine is real, large parts of P2 remain conceptual. P0 completion is the central near-term delivery determinant.

### 3. P1 is the best place to reduce future rework

Routing, governance, recording, workforce logic, and reporting defaults all shape downstream behavior. These should be clarified and staffed in parallel with P0 wherever possible.

### 4. P3 is already very large

The P3 surface is extensive: omnichannel, outbound, IVR depth, assist, and productivity. There is risk in opening too many of those areas before P0/P1/P2 are genuinely underway.

### 5. Compliance is not a side note here

Recording retention, intrusive-action legality, in-region handling, and audit order-of-operations all appear early in scope. That is a strong signal that regulatory readiness must be treated as a design dimension, not a final check.

---

## Explicit blockers and risks

### Primary blockers

- P0 runtime completion remains the master blocker for downstream live operations.
- AGH-6685 is the clearest external dependency.
- Assignment density is too low for the number of decomposed sub-issues.

### Near-term delivery risks

- delayed compliance input on recording policy
- routing and workforce foundations not staffed early enough
- dashboard KPI definitions drifting from contractual expectations
- assist / omnichannel / outbound breadth pulling focus from runtime completion
- launch-client specifics becoming hardcoded instead of tenantized

---

## Canceled / superseded work

### AGH-6701

This issue was canceled because it was superseded by AGH-6749. The significance is not just status cleanup — it reflects a product-design shift from a narrower voice-drawer assist concept to a broader agent assist and productivity framework.

---

## Recommended use of this dossier

This file is best used for:

- implementation kickoff and sequencing review
- assignment planning
- architecture / compliance review
- leadership context on scope and readiness
- handoff between planning and execution
- future packaging of the work into a reusable product narrative

---

## Closing assessment

The project is already strong as a program design artifact. It has a clear phased model, sensible dependencies, and appropriately separated strategic concerns: runtime, live operations, governance, reporting, omnichannel, assist, and productization.

The largest remaining gap is not planning detail. It is execution activation:

- finish the P0 spine,
- aggressively assign P0/P1 child issues,
- secure AGH-6685 external policy input,
- and avoid broad P3 spread before the runtime and foundational layers are genuinely moving.

If needed, this dossier can be extended again into an even more literal archival version that reproduces each issue body exactly line-for-line in a raw appendix.