# Project context — cold-start facts

Register of the Agentic Build Graph (`docs/build-graph/`). `/increment` reads this before planning so
established facts are not re-discovered. Keep entries short and dated; a fact that has expired is
worse than none.

- **Codebase knowledge** lives in `CLAUDE.md` at the repository root (module map, conventions, traps).
  Do not duplicate it here — point at it.
- **Canonical branch:** `test` (the branch CI deploys to the `velents-test` environment).
- **Environment facts** (URLs, which pod runs what, credentials' *locations* — never their values): add below as they are verified.

## Verified facts

_(none recorded yet — add `YYYY-MM-DD · fact · how it was verified`)_
