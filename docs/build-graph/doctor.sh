#!/usr/bin/env bash
# Product Graph preflight. Read-only: fetches, never checks out, never pulls.
#   bash docs/build-graph/doctor.sh [feature-name]
# Runs from a multi-repo workspace root OR from inside a single repository.
# Exit 0 = clear · 1 = WARN (proceed, but say so) · 2 = FAIL (do not ground on this)
#
# Exists because on 2026-08-01 a `discover` stage grounded against a tree 1320
# commits behind `test` and produced a conclusion that had to be retracted.
# Absence observed in a stale tree is not evidence of absence.

cd "$(dirname "$0")/../.." || exit 2
warn=0; fail=0
say() { printf '%-22s %s\n' "$1" "$2"; }

# Canonical branch per repo — mirrors the repo map in docs/codebase-dna.md.
canon() { case "$1" in
  velentsAgents|agent-hub|mcp-tool-generator|voice-agent|call-engine|asterisk) echo test ;;
  *) echo UNKNOWN ;;
esac; }

# Two layouts. The workspace this was written for holds the repos as sibling
# subdirectories. Installed INTO one repository (2026-09-10), the repository is
# `.` — named after its origin remote, not its checkout directory, because a
# clone can land under any path (`velents-technologies-ug/build-graph`, a
# worktree) while the canonical-branch map keys on the GitHub name.
if [ -d .git ]; then
  single=1
  repos="."
else
  single=0
  repos="velentsAgents agent-hub text-agent mcp-tool-generator voice-agent activepieces-source"
fi
repo_name() {
  local n; n=$(git -C "$1" remote get-url origin 2>/dev/null | sed -E 's#/*$##; s#\.git$##; s#.*/##')
  [ -n "$n" ] && echo "$n" || basename "$(cd "$1" && pwd)"
}

echo "== repos =="
for r in $repos; do
  [ -d "$r/.git" ] || { say "$r" "ABSENT"; warn=1; continue; }
  git -C "$r" fetch --all --quiet 2>/dev/null
  name=$(repo_name "$r"); want=$(canon "$name"); dir="$r"; via=""

  # The canonical branch is often checked out in a sibling worktree rather than
  # in the primary directory. Ground against wherever it actually lives — the
  # first version of this script failed a repo whose `test` tree sat next door.
  if [ "$want" != UNKNOWN ]; then
    wt=$(git -C "$r" worktree list --porcelain 2>/dev/null \
         | awk -v b="refs/heads/$want" '/^worktree /{p=$2} /^branch /{if($2==b) print p}' | head -1)
    # compare basenames — `git worktree list` prints native paths, $(pwd) prints MSYS ones
    if [ -n "$wt" ] && [ "$(basename "$wt")" != "$r" ]; then dir="$wt"; via=" (worktree $(basename "$wt"))"; fi
  fi

  br=$(git -C "$dir" branch --show-current)
  dirty=$(git -C "$dir" status --porcelain | wc -l | tr -d ' ')
  note=""

  if [ "$want" = UNKNOWN ]; then
    note="canonical branch not in the DNA repo map"; warn=1
  elif [ "$br" != "$want" ]; then
    # In a single repository, working on a feature branch is the normal state,
    # so being off-canonical is a caveat to carry, not a reason to stop. The
    # distance to canonical (below) is still what decides FAIL.
    if [ "$single" = 1 ]; then note="on '$br', not canonical '$want'"; warn=1
    else note="no '$want' checked out anywhere; primary is on '$br'"; fail=1; fi
  fi

  # Measure against the CANONICAL branch, not the one that happens to be checked
  # out — the gap to canonical is the number that decides whether grounding holds.
  ref="$want"; [ "$ref" = UNKNOWN ] && ref="$br"
  if git -C "$dir" rev-parse --verify -q "origin/$ref" >/dev/null 2>&1; then
    behind=$(git -C "$dir" rev-list --count "HEAD..origin/$ref")
    # A repo with no canonical branch in the DNA map can only WARN — we do not
    # know what it is supposed to track, so "behind" is not a finding about it.
    if [ "$behind" -gt 200 ] && [ "$want" != UNKNOWN ]; then note="$note; ${behind} BEHIND origin/$ref"; fail=1
    elif [ "$behind" -gt 0 ]; then note="$note; ${behind} behind origin/$ref"; warn=1; fi
  else
    note="$note; no origin/$ref"; warn=1
  fi

  [ "$dirty" -gt 0 ] && { note="$note; ${dirty} uncommitted"; warn=1; }
  say "$name" "${br}${via} @ $(git -C "$dir" log -1 --format=%h)${note:+ — ${note# ; }}"
done

echo
echo "== dna =="
if [ -f docs/codebase-dna.md ]; then
  age=$(( ( $(date +%s) - $(date -r docs/codebase-dna.md +%s) ) / 86400 ))
  say "codebase-dna.md" "${age}d old"
  [ "$age" -gt 30 ] && warn=1
else
  say "codebase-dna.md" "MISSING"; fail=1
fi

# Ledger integrity — a stage is not closed until its exit artefact exists on disk.
if [ -n "$1" ] && [ -f "features/$1/ledger.md" ]; then
  echo
  echo "== ledger: $1 =="
  st=$(sed -n 's/^stage: *\([a-z-]*\).*/\1/p' "features/$1/ledger.md" | head -1)
  say "stage" "$st"
  need=""
  case "$st" in
    frame)      need="competitive-analysis.md" ;;
    brief)      need="brief.md" ;;
    prototype)  need="brief.md" ;;
    slice)      need="prototype.html" ;;
    tickets)    need="increment-plan.md" ;;
  esac
  if [ -n "$need" ] && [ ! -f "features/$1/$need" ]; then
    say "FALSE GREEN" "stage '$st' but features/$1/$need is missing"
    fail=1
  fi
fi

echo
[ "$fail" -eq 1 ] && { echo "FAIL — do not ground on this. Fix, or record it as DEGRADED in the ledger before proceeding."; exit 2; }
[ "$warn" -eq 1 ] && { echo "WARN — proceed, but state the caveat in whatever you write."; exit 1; }
echo "clear"; exit 0
