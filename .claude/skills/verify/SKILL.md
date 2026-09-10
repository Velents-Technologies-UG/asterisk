---
name: verify
description: Prove a surface or change actually works against a real system — real data, negative cases, downstream contract. Use when asked to verify, test, or confirm something works, or before reporting anything as done.
argument-hint: [what to verify]
---

# Verify: $ARGUMENTS

Verification means **real behaviour against a real system**. A green build, a started process, and a rendered page are not verification.

**First, check whether a better-fitted engine already exists (§9 — don't fork it):** if there is a PRD, Epic, or Linear issue with acceptance criteria behind this, hand off to **`qa-verify-spec`**. If it is a diff with no spec, hand off to **`qa-code-hunt`**. Use the checks below only when neither applies.

## Run these checks and show the actual output for each

1. **Real request → real data.** Call the actual endpoint/surface. Show the response: a 200 with real fields. A 500, an empty stub, or a started-but-unqueried service is a FAIL, not a pass.
2. **The negative case.** This is the one most often skipped.
   - Isolation: a second tenant/user is **denied** the first one's data.
   - Permission: a role **without** the permission gets **403** on the mutating route; a role with it succeeds.
3. **The downstream contract.** If another service or consumer reads this data, call **that** endpoint and confirm it returns what was authored/saved — not just that the write appeared to succeed.
4. **Persistence and scope.** Write, then read back, and confirm it is correctly scoped (tenant/user/workspace).

## Report honestly

For each check: **PASS** with the evidence, **FAIL** with the actual error, or **UNVERIFIED** with the precise reason and exactly what is needed to verify it (a credential, a running service, an environment, a real call).

Never round UNVERIFIED up to PASS. Never report a surface as working on the strength of config alone or a green build. If something can only be verified in an environment you do not have, say so plainly and name what is missing.

## Then state the honest status

- **Built** — code exists, unverified.
- **Verified** — real request → real data, negative case proven.
- **Shippable** — verified, gated, compliant, owned.
- **Demoable** — proven end-to-end on a real environment.
