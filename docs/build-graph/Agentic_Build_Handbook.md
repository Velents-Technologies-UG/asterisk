The Agentic Build Handbook
Best Practices for Shipping Real Software with AI Agents
Distilled from the INFATH CXM build
A field guide to running Claude Code (and any coding agent)
on production, multi-tenant, high-stakes systems

Contents

# The One Idea
An AI agent is an exceptional builder and an unreliable self-certifier. It will write correct code, catch its own mistakes, and reason carefully — and it will also, without malice, report a thing as done that isn't, claim a security control that doesn't exist, and build confidently on a foundation it never verified. The entire practice in this handbook exists to capture the first behaviour and contain the second.
The load-bearing mechanism is not a smarter agent or more agents. It is a human reviewing consequential work at every gate, and a set of rules that make the agent surface the truth rather than a comfortable summary. Everything below is a way of operationalising that.
# 1. Definition of Done
The single most important rule. “Done” means real, verified behaviour against a real system — not a green build, not “it renders,” not “the service started,” not “the tests pass.”
### What counts as verified
- A real request returns real data. For a backend: an actual endpoint call returns a 200 with real fields, not a 500 and not an empty stub. “The server is running” is not this — a started process that 500s on the first real request is the exact false-green this rule exists to catch.
- The negative case is proven, not just the happy path. Isolation is proven by showing tenant A is *denied* tenant B's data — not by showing tenant A can see its own. A permission is proven by showing the unauthorised role gets a 403, not by showing the authorised role gets a 200.
- The contract the downstream consumer relies on is checked. If an engine reads a published flow via one endpoint, verification means calling *that* endpoint and confirming it returns what was authored — not that “the UI saved something.”
- If it can't be verified in the current environment, it is labelled UNVERIFIED, with a precise statement of what's needed. Never let “built” quietly become “done.” They are different words for different states.

# 2. No False Green
Agents drift toward reporting success. Not dishonestly — they pattern-match “the steps completed” to “the goal achieved.” The discipline is to make the agent distinguish the two, out loud, every time.
### The failure this prevents
In this build, an agent once reported a surface “shipped & verified.” Pushed on it, the honest accounting was: the plumbing was built and the gate worked, but the functional requirements were not implemented, the data didn't load through the UI, and — most seriously — a code comment claimed “enforced server-side” for an access control that was enforced *nowhere*. A role could mutate data it had no permission for. The comment asserting a security control that did not exist is precisely how a real vulnerability ships: not through malice, but through a trusted-but-false claim nobody re-checked.
### How to enforce it
- Ask, per surface: is this the FR-complete thing, or the old component reachable under a new route? “Mounted” and “implemented” are different deliverables. Make the agent state which it built.
- Distinguish a real defect fix from a local-environment workaround before anything is committed. A migration fix that makes *your machine* run is not the same as a fix that's correct for every tenant. Classify it; commit only the former to the shared branch.
- Never accept a security claim without a run behind it. “RBAC enforced” requires the negative test output (403), not the assertion.
- When the agent catches its own false claim and retracts it, that is the system working — reward it, don't punish it. The goal is an agent that surfaces the uncomfortable truth against its own interest.

# 3. Read Before You Build
The standing instruction that paid for itself more than any other. Before writing code, the agent reads the actual codebase, the actual design spec, and the actual data — and grounds the plan in what it finds, not in what the task assumed.
### Why it matters
- It stops wasteful reinvention. A task to “build the FR-complete IVR canvas” was, on grounding, revealed to be nearly done already — a full node-edge editor existed; the real work was mounting it and gating it, a fraction of the size. An agent that built first would have rebuilt a working surface.
- It catches divergence between what the code expects and what the environment provides. Two migrations that both “create” the same table with conflicting shapes; a dialplan that exists only as a sample; a flag that resolves against the wrong database. None of these are visible from the task description — only from reading.
- It surfaces the real dependency order. You cannot sequence work correctly until you've seen which layers already exist and which are genuinely missing.
# 4. Small, Reviewed Increments
Work in tight, single-purpose increments — one surface, built to depth, verified, then the next. Do not spread thin across many surfaces at once, and do not batch consequential changes so they can't be reviewed individually.
- One increment = one scoped task with: grounding to read first, explicit in-scope, explicit out-of-scope, a real definition of done, and a stop-and-report at the end.
- Depth over breadth. Finishing one surface to FR-complete-and-verified beats touching ten surfaces halfway. The walking-skeleton principle: prove one thin path through every layer before widening any single layer.
- Manual approval on consequential work. Fleet-running migrations, security models, changes to shared infrastructure, anything irreversible — these are reviewed edit-by-edit, not run on auto. Auto mode removes the gate that catches the migration that deletes data.
- Let the agent make routine calls; reserve yourself for the genuine forks. Approving every trivial decision (which tier a role sits in, an obviously-correct least-privilege default) is a waste of a human. Set the principles once, let the agent apply them, and re-engage on the decisions where two reasonable people could disagree or where scope/security is materially affected.

# 5. Migrations & Data Changes
Schema and data migrations have the largest blast radius of anything an agent will write, because they run across every tenant, often unattended, once. Treat them as the highest-stakes change class.
- A migration that passes on an empty demo tenant can destroy data on a populated one. “Works locally” is the single most dangerous phrase here. Before a migration runs anywhere but local, confirm whether real tenants hold real data the migration would change or delete.
- Prefer non-destructive conversion over drop-and-reseed. When reshaping a table, carry existing rows *forward* into the new shape rather than deleting and reseeding a default. A delete-then-seed is data loss wearing a comment that says it's safe.
- Never edit an already-run migration — it won't re-run on existing tenants. Add a new forward migration instead.
- Make migrations idempotent and resumable. At hundreds of tenants, partial failure is normal; a re-run must be safe and must only touch what failed.
- State the migration's assumptions explicitly (e.g. “assumes one schedule per tenant, enforced by the unique constraint”) so that if the assumption ever changes, the conversion gets re-checked.
- Be honest about `down()`. A rollback that restores a previously-broken state is a valid forward migration but is not a “safe rollback.” Say which it is.
- Gate fleet runs behind explicit human approval, separate from local/test verification. Local-verified is not fleet-authorised.
# 6. Security Is a Property, Not a Phase
Access control is not a feature you append after the functionality is built. It is part of the definition-of-done for every surface that mutates data. “Build all the features, then add RBAC” guarantees a window in which every feature ships unprotected — and “later” slips.
### The two halves of RBAC
- Enforcement — gating each surface so an unauthorised role is denied. This ships *with* the surface, always, and is proven with a negative test (unauthorised → 403). A surface isn't done until it's both functional and gated.
- The role model — which roles exist and who gets what. This can evolve as surfaces firm up. Collapsing a large specified role matrix into the operational roles that actually differ in capability is a legitimate decision — but record it as a decision, and note the delta from the specified model so it isn't mistaken for full coverage.
### Least privilege, always
- When in doubt about a grant, grant nothing. Permissions are additive; the cost of granting too little is “someone asks and you add it,” while the cost of granting too much is “someone had access they shouldn't and you didn't know.”
- Don't back-fill new capability onto a legacy role by assumption. New access is opt-in via the intended roles, not silently mapped onto whatever legacy label happens to exist.
- Separate observe from intervene. The permission to *watch* (view a dashboard, play a recording) and the permission to *act* (barge a live call, mutate config) are different grants. Keep them distinct — it's the boundary that matters most for privacy-sensitive and high-stakes actions.
- Gate the route, not just the UI. Hiding a control in the front-end is UX; the server-side route check is the security. A dark surface that relies on UI-hiding alone is bypassable by anyone hitting the API directly — especially dangerous when that surface rides a known data-isolation gap.

# 7. Shared & Production-Adjacent Infrastructure
The rules get *tighter* when the agent has more access, not looser. An environment that other people and real tenants depend on is where a single mistake is unrecoverable.
- Verify before you touch. On a shared environment, the first action is read-only discovery of what's actually running — never “bring it up” or restart a service that may already be serving others.
- Least privilege on credentials. Hand the agent the narrowest key that does the job — a tenant-scoped token for tenant-scoped work — and hold back control-plane / master secrets unless a specific, named step requires them. A master key to shared infra is the last thing you grant, for a stated reason, never as a convenience.
- Own-tenant-only, even when the gap would let you cross. If a known isolation gap means the agent *could* read another tenant's data, that is exactly the reason it must not. Having the access is not permission to use it across a boundary.
- Surface before mutating shared state. Any change to shared configuration — a dialplan, a routing rule, a global setting — is surfaced and waits for explicit approval. The agent proposes; the human authorises.
- Finding an address is not having the keys. Access to production-adjacent infrastructure should require a deliberate grant, not be discoverable in a config file. A committed secret is a finding to flag, not a convenience to consume.
- Never echo credentials — carrier secrets, tokens, control keys — into code, tests, logs, or reports. And rotate any secret that ends up in a transcript once the work is done.
# 8. Surface Findings, Don't Bury Them
When an agent discovers a defect, a gap, or a risk mid-task, the right behaviour is to surface it as a tracked item with appropriate weight — not to fold it silently into the current change, and not to note it in passing and move on.
- File real findings as real tickets, with the correct severity and an owner. A cross-tenant credential leak on a judicial system is not a footnote; a recording endpoint with no access control is a security ticket, not a “by the way.”
- Keep the finding distinct from the fix. The record of the defect and the action to verify or resolve it are different items with (often) different owners. Don't collapse them.
- A found risk needs a named owner and a date, or it lives in notes forever. The recurring failure mode is a real issue that everyone acknowledges and no one owns. Filing it as an assignable item is step one; assigning it is what actually moves it — and that assignment is usually a human's job, not the agent's.
- Distinguish latent from live. “The gap exists in the code” and “the gap is exploitable in production right now” are different severities. The deployment topology often decides which — so resolve the topology question rather than guessing the severity.
# 9. Reuse, Don't Reinvent — and Keep Diffs Legible
- Compose and mount existing components; never fork or rebuild what already works. Reuse keeps one source of truth, so that when the old home is retired there aren't two copies to maintain.
- Reuse the codebase's existing canonical mechanism rather than introducing a parallel one — for access control, for data fetching, for eventing. “One canonical model” is violated as much by adding a second convention as by having none.
- Never remove the working thing before its replacement is verified. When migrating a surface to a new home, mount and prove the new one first; retire the old one as a later, separately-gated step. A window with no working surface is the cost of getting this wrong.
- Keep diffs reviewable. A change that reformats a whole file hides the real edit inside noise and can't be reviewed. Preserve formatting; make surgical edits. An unreviewable diff is a reason to stop and redo, not to approve.

# 10. Sessions, Continuity & Parallelism
### Preserving context across restarts
- A fresh session starts cold — it inherits none of the hard-won context or discipline of the last one. Before ending a session that will be resumed (a reboot, a context limit), have the agent write a resume note to disk: current state, what's verified, what's open, the decisions made, the rules to hold, and what's next. The next session reads it and continues — nothing lost.
- The discipline must be written down, not carried in one conversation. Put the operating rules in a repository instructions file so every session starts with them loaded. Rules that live only in a chat thread scroll away.
### On “more agents”
- Parallelism is right only for genuinely independent tracks. Two separable workstreams (e.g. a UI-build track and an infrastructure track) can run as two sessions, each single-threaded and each reviewed. That is the safe amount of parallelism.
- Do not run many autonomous agents making consequential changes at once. That multiplies variance and dilutes the one thing that kept the work safe — a human reviewing each consequential gate. More firepower does not solve an access-or-decision bottleneck, and it removes the review that catches the data-destroying migration.
- Don't grant an agent new capabilities mid-flight to “go faster,” especially on security-sensitive work. Open-ended “use whatever tools you think help” introduces behaviour you haven't validated at exactly the moment you want stability. Name the specific skills that fit the specific task; keep security work deliberate and per-step.
# 11. Scope, Requirements & Priority
- Make the whole backlog visible before choosing what's next. A coverage matrix — every requirement and surface, honestly statused (shipped / mounted-not-complete / not-started), with size and what-gates-it — converts a vague “huge backlog” into a decision you can actually make. You cannot prioritise what you can't see.
- Build in dependency order, and prove the foundation early. The layer that everything executes on (here, the call-flow core) is the one to prove with a thin end-to-end path before widening the layers above it — otherwise you build elaborate configuration on top of an execution layer whose behaviour you've assumed, not verified.
- Requirements beat parity. “Match the competitor feature-for-feature” and “meet the customer's actual requirements” overlap but are not the same. Where they diverge, the customer's requirements win — build to the RFP, not to the competitor's feature list. The requirement-specific items (here: the local-language ASR, the bilingual mandate, the residency/consent compliance) are both the right product call and the differentiator against a generic incumbent.
- Whatever you defer, defer it explicitly with a roadmap — don't let scope stay parked by inattention. “Later” that no one wrote down becomes “never” by accident.

# Appendix A — The Scoped Task-Brief Template
Every increment handed to an agent should have this shape. It is the structure that produced reliable results throughout the build.
- Goal — one sentence: the outcome, and what “done” means in terms of real verified behaviour.
- Ground it first — the specific files, specs, and endpoints to read *before* writing anything. Read-before-build is not optional.
- Known context — what's already established, so the agent doesn't re-discover it: environment URLs, what's verified, what's gated, the relevant prior findings.
- Scope (in) — exactly what to build this pass.
- Scope (out) — explicitly what NOT to build; where to flag a hook for deferred work instead of building it.
- Hard rules — the non-negotiables that apply: own-tenant-only, secrets never echoed, reuse-don't-fork, surface-before-touching-shared-infra, RBAC per-surface.
- Definition of Done — the real verification steps, each producing observable evidence (a 200 with real data, a 403 for the unauthorised case, a cross-tenant denial). Never “renders” or “green build.”
- Report & stop — report what's verified vs. what's [NEEDS-ME] (needs a human: access, a decision, infrastructure), then stop before the next increment.
# Appendix B — The Reviewer Agent
If you codify one agent from these practices, make it the reviewer. Its job is to apply this handbook to every plan and every diff — to be the gate, consistently, so the discipline doesn't depend on a human catching everything by hand. Suggested standing brief:
- Review plans and diffs as a principal engineer; do not rubber-stamp. Flag the one or two consequential things rather than approving wholesale.
- Enforce the definition of done: real verified behaviour, never green-build-as-done. If something can't be verified, require it labelled UNVERIFIED with a precise statement of what's needed.
- Distinguish “built” from “verified,” and “real defect fix” from “local workaround,” before anything is committed to a shared branch.
- Never accept a security claim without the negative-test evidence behind it. Treat a comment asserting a non-existent control as a defect to retract immediately.
- Treat migrations as the highest blast-radius change: require the fleet-data question answered before any fleet run; prefer non-destructive conversion.
- On shared/production-adjacent infrastructure: require verify-before-touch, least-privilege credentials, own-tenant-only, and surface-before-mutate.
- Surface findings as tracked items with correct severity; keep finding distinct from fix; insist unowned risks get a named owner.
- Requirements over parity. Reuse over reinvent. Keep the human on the irreversible calls — raise the floor on quality; don't remove the gate.

— End of Handbook —
