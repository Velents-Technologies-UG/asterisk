---
name: signal
description: Classify an incoming product ask and route it into the Product Graph — a client request, an exec question, a new idea, a status claim from engineering, a bug report, or "what should we do next?". Declares the tier, opens or resumes the feature ledger, names the entry stage, and stops. Use at the start of any product work when it is not already obvious which stage you are in. Do NOT use to do the work itself — it routes and hands off.
argument-hint: [the ask, in the words it arrived in]
---

# Signal: $ARGUMENTS

The entry node of `docs/build-graph/Product_Graph.md`. Classify, declare, open, route, **stop**.

Five steps. Do not start the downstream work — routing is the whole job.

## 1. Is this product work at all?

The most useful thing this router does is send things away. Check first:

| The ask is | Send it to | Not this graph because |
| -- | -- | -- |
| Build / fix / wire something already scoped | `/increment` | the decision is already made; this is execution |
| Does the product actually do what this spec says? | `qa-verify-spec` | verification, not scoping |
| Find bugs in a diff, no spec | `qa-code-hunt` | same |
| Who else does X? (research only, no feature behind it) | `comp-search` | discovery without a decision downstream |
| Quarter / half / annual planning | `build-roadmap` | one altitude up; it feeds signals *into* here |
| Launch copy, positioning, market messaging | `gtm` | outbound marketing, not the product decision |
| Scoping a prospect, proposal, POC | `velents-presales` | commercial artefact |
| A status report going to a stakeholder | `/status` | that is `communicate`; enter there directly |

If it routes away, say so in one line and stop. Do not open a ledger.

## 2. Already in flight?

Check `features/*/ledger.md` for this feature before opening anything.

If one exists: read it, report its **Now** block and current stage, and route to the next stage. **Never open a second ledger for a feature that has one** — two ledgers is worse than none.

## 3. Classify — what kind of signal, and where does it enter?

| The signal | Enters at |
| -- | -- |
| A vague ask, a new idea, a client wants "something like X" | `discover` |
| A well-understood problem, no solution decided | `frame` |
| "What should we build next?" / a backlog to prioritise | `decide-scope` |
| A decided change to existing behaviour, scope already clear | `tickets` |
| A claim from engineering or an agent — "it's done", "it's shipped" | `oversee` |
| A bug report from a customer or QA | `oversee` |
| Something needs saying to an exec, a client, or the team | `communicate` |
| A date, a promise, a contractual commitment | `commit` |

When two entries are plausible, take the **earlier** one. Entering late skips a gate; entering early costs one conversation.

## 4. Declare the tier

| Tier | When | Ledger |
| -- | -- | -- |
| **bug** | broken behaviour that was already promised and already shipped | none — the bug report is the record |
| **change** | scope is decided and unambiguous; nothing new is promised | opened at `tickets` |
| **feature** | anything that changes what a customer was promised | opened now |

**The rule:** tier is a judgement about the promise, never about the size of the diff. A one-line change that alters what a customer was told is a **feature**.

**The guard:** the failure mode is tier deflation — calling a feature a "change" to skip G1 and G4. If you are choosing between `feature` and `change`, that hesitation *is* the signal to escalate. **Ask; do not pick.** Tier selection is scope judgement, and scope judgement is G1 — the user's, not yours.

## 5. Open the ledger and report

For `feature` (and `change`, at `tickets`):

- Create `features/<kebab-name>/` if it does not exist.
- Copy `docs/build-graph/feature-ledger.template.md` to `features/<name>/ledger.md`.
- Fill frontmatter: `feature`, `tier`, `stage`, `opened`, `owner`. Write the **Now** block — three true lines.
- Leave every gate `pending`. The router records no verdicts.

Then report, in four lines, and **stop**:

- **Signal** — what arrived, in one sentence.
- **Tier** — which, and the promise-based reason for it.
- **Entering at** — the stage, and what it needs to produce to leave.
- **Ledger** — the path, or why there isn't one.

## What this router never does

- Start the work at the entry stage. Routing and running are separate turns.
- Record a gate verdict. G1 and G4 are the user's; G2 and G3 need evidence the router has not seen.
- Pick a tier it is unsure about (see the guard).
- Open a second ledger for a feature already in flight.
