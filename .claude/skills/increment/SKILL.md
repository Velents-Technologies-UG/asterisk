---
name: increment
description: Run one disciplined CODE-CHANGE increment end-to-end — ground, plan, human gate, build, verify, review, human gate, report. Use when the user asks to add, implement, build, fix, wire, refactor, or migrate something in this repository, or hands over a Linear issue ID to implement, or says "next increment". Do NOT use for writing a PRD or spec (build-feature), competitive research (comp-search), verifying an existing spec against production (qa-verify-spec), GTM/marketing copy (gtm), or roadmap planning (build-roadmap) — those own their own workflows and no code is being changed.
argument-hint: [what to build]
---

# Run one increment: $ARGUMENTS

Follow this sequence. Do not skip steps and do not run ahead of the gates.

## 1. Ground (read-before-build — mandatory)

Delegate to the **grounder** subagent, or read directly: the actual code, specs, config, and data for this area. Establish:
- What already exists (it is often more than the task assumes).
- The canonical conventions in use here (access control, data fetching, tenancy, i18n).
- What is wired vs. present-but-unwired.
- Any divergence between what code expects and what schema/config provides.

**If the premise is wrong — it already exists, or exists differently — stop and say so. Do not build the wrong thing.**

## 2. Plan — produce the scoped brief

Write it in this shape and show it to me:

- **Goal** — one sentence; the outcome, and what "done" means as real verified behaviour.
- **Grounding** — what you read, and what it changed about the plan.
- **Known context** — the established facts from `docs/PROJECT_CONTEXT.md` and `docs/COVERAGE.md` so nothing gets re-discovered: environment, canonical conventions, what is already verified, what is gated, relevant prior findings.
- **In scope** — exactly what this pass builds.
- **Out of scope** — explicitly what it does not; where to flag a hook rather than build.
- **Hard rules in play** — name the non-negotiables that bite on *this* increment: own-tenant-only, secrets never echoed, reuse-don't-fork, surface-before-touching-shared-infra, RBAC per surface.
- **Definition of Done** — the real verification steps, each producing observable evidence (a 200 with real data, a 403 for the unauthorised case, a cross-tenant denial, the downstream contract returning what was authored).
- **Consequential?** — does this touch a fleet-running migration, an access-control/security model, shared or production-adjacent infrastructure, or anything irreversible?
- **[NEEDS-ME]** — what requires me (access, a decision, infrastructure).

## 3. GATE — stop for my approval

**Halt here.** Do not write code until I approve or refine the plan.

## 4. Build

Delegate to the **builder** subagent (or build directly under CLAUDE.md rules). One increment, to depth. Reuse and compose; never fork. Security ships with the surface. Diffs surgical and reviewable.

## 5. Verify

Run the DoD. Real request → real data. Unauthorised → 403. Cross-tenant → denied. Downstream contract satisfied. If it cannot be verified here, report **UNVERIFIED** with exactly what is needed — never round up.

**Use the workspace's existing QA engine — do not build a third convention (§9):**

| Situation | Run |
|---|---|
| Increment traces to a PRD, Epic, or Linear issue with ACs | **`qa-verify-spec`** — per-AC pass/fail/blocked matrix |
| Just a diff, no spec | **`qa-code-hunt`** — blast-radius + heuristic bug hunt |
| Neither applies | **`/verify`** — real request, negative case, downstream contract |

If verification fails, fix and re-verify. After 3 failed loops, **escalate to me** instead of grinding.

## 6. Review

Delegate to the **reviewer** subagent with the diff. Route its findings back to build. If it returns ESCALATE, bring it to me.

## 7. GATE — consequential changes

If this increment touches a fleet migration, a security model, shared/production-adjacent infrastructure, or anything irreversible: **halt and get my explicit approval before it lands.** The reviewer's approval is not a substitute for mine.

## 8. File the findings — don't leave them in the transcript (§8)

A finding that only exists in a chat log is buried, not surfaced.

- Every finding above `minor` becomes a **Linear issue**, labelled `build-graph`, with its severity in the title and the evidence in the body. Team: **Production Bugs** for a defect in shipped behaviour, **Agent Hub** (`AGH`) for a gap in work in flight. The `build-graph` label does not exist yet — create it on first use.
- Set the assignee if there is an obvious owner. If there is not, write **`Owner: UNOWNED`** in the body explicitly — flagging that is the whole point, not an omission to tidy up.
- Add a row to `docs/BLOCKERS.md` (blockers) or `docs/COVERAGE.md` (scope gaps) that references the Linear ID, and put the `docs/` row reference in the Linear issue. They point at each other.
- Keep the **finding** distinct from the **fix** — separate items, often separate owners.
- Any decision made during this increment goes in `docs/DECISIONS.md` so it isn't re-litigated next session.

## 9. Report and stop

- **Verified** — each check with its evidence.
- **UNVERIFIED / [NEEDS-ME]** — precisely what is blocked and on what.
- **Findings** — the Linear IDs filed, with severity and owner (or `UNOWNED`).
- Then **stop.** Do not start the next increment.

## 10. Write the resume note (§11)

Before you stop, overwrite `docs/RESUME.md`: current state, what is verified, what is open, decisions made, rules that must hold, what is next. A fresh session inherits nothing otherwise — and this file is the only thing that survives a `/clear`.
