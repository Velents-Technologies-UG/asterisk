---
name: status
description: Produce an evidence-checked status report for stakeholders. Use when writing a status update, weekly report, leadership summary, or client-facing progress note — anything where a claim leaves the team.
argument-hint: [scope of the update]
---

# Evidence-checked status: $ARGUMENTS

You are producing something a stakeholder will treat as **fact**. Every claim must be traceable to evidence before it goes out.

## 1. Classify every item into one honest state

| State | Means | Safe externally? |
|---|---|---|
| **Built** | code exists, unverified | No |
| **Verified** | real request → real data; negative case proven | Internally only |
| **Shippable** | verified + gated + compliant + owned | Yes, with conditions |
| **Demoable** | proven end-to-end on a real environment | Yes |

Anything that does not fit a state is **UNVERIFIED with a stated reason**. Never round up. "Mounted under a new route" is not "shipped". "Middleware added" is not "enforced".

## 2. Trace every claim

For each factual assertion, name the evidence (the verification run, the ticket, the matrix row). **Remove or downgrade any claim you cannot trace.** If a claim came from another agent or an engineer without evidence, ask for the evidence rather than passing it along.

## 3. Blockers — every one needs a name and a date

List each open blocker with its **owner** and **target date**. Explicitly flag any blocker that has no owner — an unowned risk is unmanaged, and surfacing that is part of the report.

## 4. Distinguish

- **Latent vs live** risk (deployment topology usually decides severity).
- **Deferred with a plan** vs **not started** — do not let scope stay parked silently.

## 5. Write for the audience

Match register to reader — exec brief, client-facing note, team update are different artifacts, not the same text at different lengths. Adapt language and formality to the audience; for Gulf client correspondence use formal MSA, for internal use the team's normal register.

## 6. Before it leaves

Show me the draft with its claim-to-evidence trace. **Do not send anything on my behalf without my explicit approval.**
