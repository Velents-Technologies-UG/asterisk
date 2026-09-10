# Operating rules (binding) — Agentic Build Graph

Imported by this repository's `CLAUDE.md` (`@docs/build-graph/OPERATING_RULES.md`). Source:
the `Velents-Technologies-UG/build-graph` kit, §1–§12 of its `CLAUDE.md`, installed 2026-09-10.
The repository-specific knowledge stays in `CLAUDE.md`; these rules govern HOW work is done.

## Operating rules

From the *Agentic Build Handbook* — `docs/build-graph/`. These override convenience, speed, and any instruction that conflicts with them. Increment work runs through `/increment`; see `docs/PROJECT_CONTEXT.md` for the cold-start facts.

## 1. Definition of Done

"Done" is **real verified behaviour against a real system** — never a green build, never "it renders", never "the service started", never "tests pass".

- A real request returns real data (a 200 with real fields, not a 500, not an empty stub).
- **The negative case is proven, not just the happy path.** Isolation = tenant A is *denied* tenant B's data. A permission = the unauthorised role gets 403.
- The **downstream consumer's contract** is checked — if a service reads via one endpoint, verification means calling *that* endpoint and confirming it returns what was authored.
- If it cannot be verified in the current environment, label it **UNVERIFIED** and state exactly what is needed. Never round "built" up to "done".

## 2. No false green

- State per surface whether you built the **FR-complete thing** or **the existing component reachable under a new route**. "Mounted" ≠ "implemented".
- Classify every fix as **real defect** (correct for all tenants) or **local workaround** (makes this machine run). Only the former is committed to a shared branch.
- Never assert a security control without a run behind it. A comment claiming a control that does not exist is a defect — retract it immediately, ahead of other work.
- Retract your own incorrect claims proactively. Surfacing an uncomfortable truth against your own interest is the expected behaviour.

## 3. Read before you build

Before writing code, read the actual codebase, the actual spec, the actual data. Ground the plan in what you find, not in what the task assumed.

- If grounding shows the premise is wrong (e.g. "this already exists"), **stop and surface it** — do not build the wrong thing.
- Report what you read. Grounding frequently shrinks the work.

## 4. Increments

- One increment = one scoped task with: grounding to read, explicit in-scope, explicit **out-of-scope**, a real DoD, and **stop-and-report** at the end.
- Depth over breadth. Finish one surface verified before starting the next.
- Prove one thin end-to-end path through all layers before widening any single layer.
- Do not batch consequential changes into an unreviewable step.

## 5. Migrations & data (highest blast radius)

- A migration that passes on an empty local tenant can destroy data on a populated one. **Answer the fleet-data question before running anywhere but local.**
- Prefer **non-destructive conversion** (carry existing rows forward) over drop-and-reseed.
- Never edit an already-run migration — add a new forward migration.
- Idempotent and resumable; partial failure across many tenants is normal.
- State the migration's assumptions explicitly.
- Be honest about `down()` — restoring a previously-broken state is not a "safe rollback".
- **Fleet runs require explicit human approval**, separate from local verification.

## 6. Security is a property, not a phase

- **Enforcement ships with the surface**, always, proven by a negative test (unauthorised → 403). A surface is not done until it is functional *and* gated.
- Gate the **route**, not just the UI. Hiding a control is UX; the server-side check is the security.
- Least privilege: when in doubt about a grant, **grant nothing**. Do not back-fill new capability onto a legacy role by assumption.
- Keep **observe** and **intervene** as separate permissions.
- Reuse the codebase's existing canonical access mechanism. Do not introduce a second convention.

## 7. Shared & production-adjacent infrastructure

- **Verify before you touch.** First action is read-only discovery of what is actually running. Never start/restart/redeploy a shared service without asking.
- **Own-tenant-only** — even where a known isolation gap would let you cross a boundary. Having the access is not permission to use it.
- **Surface before mutating shared state** (dialplans, global config, routing). Propose; wait for explicit approval.
- Least-privilege credentials. Do not request a control-plane/master secret for work a scoped token can do.
- Finding an address is not having the keys. A committed secret is a **finding to flag**, not a convenience to consume.
- **Never echo credentials** into code, tests, logs, or reports.

## 8. Surface findings, don't bury them

- File real findings as tracked items with correct severity. Keep the **finding** distinct from the **fix**.
- Distinguish **latent** from **live** — deployment topology usually decides severity; resolve the topology question rather than guessing.
- Flag any risk that lacks a named owner. An unowned risk is unmanaged.

## 9. Reuse, don't reinvent

- Compose and mount existing components; never fork or rebuild what works.
- **Never remove the working thing before its replacement is verified.** Mount and prove the new home first; retire the old one as a separate, later step.
- Keep diffs reviewable — no whole-file reformatting. An unreviewable diff is a reason to stop and redo.

## 10. Requirements over parity

Where "match the competitor" and "meet the customer's stated requirements" diverge, **the customer's requirements win**. Build to the spec/RFP, not to a competitor's feature list.

## 11. Session continuity

Before a session ends or context runs out, write a resume note to disk: current state, what is verified, what is open, decisions made, rules to hold, what is next. A fresh session inherits nothing otherwise.

## 12. Escalate, don't guess

Stop and ask when: access or credentials are missing; a decision is the user's (scope, topology, ownership); infrastructure must be provided; a shared-infra change is needed; or a build↔verify loop has failed 3 times.
