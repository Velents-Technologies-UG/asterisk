---
name: stage
description: Advance a feature one stage through the Product Graph. Reads features/<name>/ledger.md, runs the current stage's owner, checks for a back-edge, writes the ledger back, and stops. Use when a feature already has a ledger and the next thing to do is "run the current stage" — or when asked to continue, resume, or move a feature forward. Do NOT use to enter the graph (that is /signal) or to change code (that is /increment).
argument-hint: [feature name — only needed when more than one is in flight]
---

# Stage: $ARGUMENTS

One stage. Then stop. Two stages in a turn means a gate was crossed without anyone looking at it.

## 0. Run the doctor — first, always

```
bash docs/build-graph/doctor.sh <feature>
```

| Exit | Means | Do |
| -- | -- | -- |
| 2 · FAIL | repos are off-canonical or far behind; or the ledger claims a stage whose artefact is missing | **Stop.** Report it and ask. Ground only if the user accepts the risk *and* `DEGRADED` is written into the ledger first |
| 1 · WARN | stale but usable | Proceed — and the caveat goes into whatever you write, not just the transcript |
| 0 | clear | Proceed |

**Never skip it because the tree looks fine.** It exists because on 2026-08-01 a `discover` stage grounded against a checkout 1,320 commits behind `test` and produced a conclusion that had to be retracted. Staleness is invisible by inspection — that is the entire point.

An absence observed in a stale tree is not evidence of absence. Do not write "X does not exist" on a FAIL.

## 1. Read the ledger

`features/$ARGUMENTS/ledger.md`, or the only in-flight ledger if the argument is empty. More than one and no argument → list them and ask which.

No ledger → **stop and say so**: this feature has not entered the graph. Run `/signal` first. Do not open one here; classification and tier are that router's job.

## 2. Verify the last stage actually closed

Before running anything, confirm the **previous** stage's exit artefact exists on disk. A ledger reading `slice` with no increment plan is a false green, and it will be inherited by every later stage as fact.

If the artefact is missing, the ledger is wrong: correct it back to the real stage, say what you found, and stop. Do not run forward over a gap.

## 3. Run the current stage

| Stage | Run | Leaves behind |
| -- | -- | -- |
| `discover` | `comp-search` | `features/<name>/competitive-analysis.md` |
| `frame` | problem · goals · **non-goals** · success criteria into `brief.md`, validated with the actual asker. Then `questions.md` — what an admin and a manager need answered, per module — and `viability.md`, which of those the product already answers, read against the canonical branch | `questions.md` + `viability.md` — then **halt at G0** |
| `decide-scope` | coverage matrix; classify **ready-now / gated-on-X / net-new**; sequence by dependency, foundation first; **recommend** | a recommendation — then **G1**, below |
| `brief` | `ticket-format.md` §1 — complete `brief.md` to ~700 words, one journey diagram, **Surfaces touched** | `features/<name>/brief.md` |
| `prototype` | `build-feature` Phase 4 + the prototyping framework | `prototype.html`, `design-decisions.md` |
| `slice` | `build-feature` Phase 5 | `increment-plan.md` |
| `tickets` | `ticket-format.md` §2–§5 | Linear parent + sub-issues; IDs into the ledger |
| `handover` | walk the packet checklist in the ledger | every box ticked, restatement posted |
| `oversee` | place each incoming claim in one state; give every blocker a name and a date | claim table — then **G2**, **G3** |
| `communicate` | audience and register first; trace every assertion | draft — then **G4** |
| `commit` | ground in `verified` only | a row in `docs/COMMITMENTS.md` — then **G4** |

Run the owner as written. Do not reimplement what a skill already does (operating rule 9).

## 4. Back-edge check — before writing anything

Each stage has one question that sends it backwards. Ask it honestly; the answer is often yes and that is the graph working, not failing.

| Leaving | Ask | If yes |
| -- | -- | -- |
| `prototype` | did a screen prove a stated requirement wrong? | → `brief` |
| `slice` | does every surface in *Surfaces touched* have an owning story? | no → `brief` |
| `tickets` | can every requirement be written as a numbered binary AC? | no → `brief` — the decision was never made |
| `handover` | did the restatement raise an ambiguity or a wrong assumption? | → `tickets` |
| `oversee` | did an AC fail? | → `tickets` |
| `communicate` | is any claim untraced? | → `oversee` |

Taking a back-edge: log it in the ledger's **Back-edges taken** table with its reason, set `stage` to the target, and stop. Never delete an old row — the same edge twice for the same cause is a defect in the stage upstream, and the pattern is the finding.

## 4b. Rules that bind every stage

Each earned by a specific failure — full account in `docs/build-graph/Product_Graph.md` §Lessons wired in.

- **L1 · No closed option sets.** Putting a choice to the user? State what the options exclude and always offer "none of these". A framing you invented is not a finding, and a wrong one arrives wearing the authority of a question.
- **L2 · Grounding constraints are binding.** Anything grounding raised stays in the ledger's *Open constraints* table until a stage answers or retires it **in writing**. If the direction changes and a constraint still bites, **re-raise it** — silence is not resolution.
- **L5 · Data surfaces owe more than design fidelity.** R1–R13 police the design system and will pass a faithful, worthless screen. Any surface showing numbers also owes: **distributions, not means**; **a reference for every figure** — target, normal range, previous period, or peer rank; **time patterns** where load varies by hour or day; **ranked outliers**, computed, not left in a table for the reader to find.
- **L6 · Name the craft benchmark before building** — dense ops console, clean product analytics, or exec narrative. They are different products.
- **L7 · Predict before you check.** Write the expected answer down before a verification pass, and keep it in the artefact when it turns out wrong.

## 4c. Two back-edges means stop

If the ledger already shows **two** traversals of the edge you are about to take a third time — for any cause — **do not build again.** Halt, name the upstream stage that keeps producing the error, and take it to the user. Three discarded artefacts is the evidence that produced this rule.

## 5. Gates — halt, do not cross

- **G0** (leaving `frame`) is **structural** — it needs no permission to stop the work. If `viability.md` shows the product already answers most of the question set, say so and **stop**. Killing a feature at G0 is the graph working, not a failure to deliver. **Never draw a prototype before this gate.**
- **G1** (leaving `decide-scope`) and **G4** (leaving `communicate`, and `commit`) are the user's. Produce the matrix, the draft, the recommendation — then **halt and ask**. Recording either verdict yourself is the failure this graph exists to prevent.
- **G2** (evidence) and **G3** (owner + date) may be recorded, but only with the evidence attached and every blocker carrying a name and a date. No evidence → the claim stays `UNVERIFIED` with a stated reason. Never round `built` up to `verified`.

## 6. Write the ledger back

- `stage` → the next stage, or the back-edge target.
- Deliverables table → the real path of what was produced.
- Gate row → verdict, who, date — only where §5 permits.
- **Now** → three true lines: stage, next, blocked on.
- `last touched` → today.

## 7. Report and stop

Four lines: **what ran** · **what it produced, with the path** · **stage now** · **what the next turn needs from you**. Then stop. Do not begin the next stage.

## What this never does

- Run two stages in one turn.
- Advance past a missing exit artefact.
- Record G1 or G4.
- Open a ledger — that is `/signal`.
- Touch code — that is `/increment`.
