---
feature: <kebab-case-name>
tier: feature          # feature | change | bug
stage: signal          # signal · discover · frame · decide-scope · brief · prototype · slice · tickets · handover · oversee · communicate · commit
opened: 2026-08-01
owner: MK
linear: —              # parent issue id(s) once they exist
---

# Ledger — <Feature Name>

*Copy to `features/<name>/ledger.md` at `signal`. Read → advance one stage → write back.*
*Graph: `docs/build-graph/Product_Graph.md`. This file is an **index** — it points at the registers, it never copies them.*

> **A stage is not advanced until its exit artefact exists on disk.** A ledger that says `slice` with no increment plan is a false green.

## Now

**Stage:** signal · **Next:** open discovery — what job is this doing?
**Blocked on:** nothing
**Last touched:** 2026-08-01

*Three lines, rewritten every time the ledger is touched. A fresh session reads this first and nothing else. Keep it true or the rest is decoration.*

## Gates

| Gate | Fired leaving | Who | Date | Verdict |
| -- | -- | -- | -- | -- |
| G0 · worth building | frame | — | — | pending |
| G1 · scope | decide-scope | MK | — | pending |
| G2 · evidence | oversee | — | — | pending |
| G3 · owner + date | oversee | — | — | pending |
| G4 · sign-off | communicate | MK | — | pending |
| G4 · sign-off | commit | MK | — | pending |

G1 and G4 are yours alone — an agent may recommend, never record. G2 and G3 cannot be crossed on an agent's word.

## Deliverables

| Artefact | Path | Status |
| -- | -- | -- |
| Competitive analysis | `features/<name>/competitive-analysis.md` | — |
| Question set (who asks what) | `features/<name>/questions.md` | — · **G0** |
| Viability — already answered? | `features/<name>/viability.md` | — · **G0** |
| Brief (~700w) | `features/<name>/brief.md` | — |
| Prototype | `features/<name>/prototype.html` | — |
| Design decisions | `features/<name>/design-decisions.md` | — |
| Increment plan | `features/<name>/increment-plan.md` | — |
| Tickets | Linear `AGH-…` | — |

## Handover

| Check | Story | Done |
| -- | -- | -- |
| Brief linked; why / locked decisions / guardrails / out-of-scope live there only | — | ☐ |
| Prototype committed; sole home of user-visible copy (EN + AR) | — | ☐ |
| Every AC numbered, binary, one requirement per line | — | ☐ |
| Every control has its own negative AC | — | ☐ |
| Fallback ACs paired with a threshold AC | — | ☐ |
| Coverage prompts answered | — | ☐ |
| Coexistence stated where the brief calls a surface unchanged | — | ☐ |
| Estimate, priority, dependencies set | — | ☐ |
| "Not listed = not decided" line present | — | ☐ |
| **Restatement posted by the assignee** | — | ☐ |

Until the restatement exists the ticket was received, not accepted. One row set per story; repeat the table if the increment has several.

## Claims

Everything delivery reports lands here before it goes anywhere. One row, one state — never rounded up.

| Claim | State | Evidence | Date |
| -- | -- | -- | -- |
| *e.g. Require-SSO switch is gated on a verified domain* | UNVERIFIED | *what is missing to verify it* | — |

`built` (code exists, unverified) · `verified` (real request, negative proven) · `shippable` (verified + gated + owned) · `demoable` (end-to-end, on a real environment, in front of a person) · `UNVERIFIED` **with a stated reason**.

Only `verified` and above may appear in anything a customer reads.

## Open constraints from grounding

Binding until a later stage answers or retires them **in writing** (L2). A constraint that quietly stops being mentioned has not been resolved.

| Constraint | Raised at | Answered / retired by | How |
| -- | -- | -- | -- |

## Back-edges taken

| From | To | Reason | Date |
| -- | -- | -- | -- |

The same edge twice for the same cause is a defect in the stage upstream. Leave the rows in — the pattern is the finding.

## Pointers

*IDs only. These files stay the system of record.*

- **Decisions** — `docs/DECISIONS.md`: —
- **Blockers** — `docs/BLOCKERS.md` / Linear: — *(every row carries a name and a date, or G3 does not pass)*
- **Commitments** — `docs/COMMITMENTS.md`: —
- **Coverage** — `docs/COVERAGE.md`: —
