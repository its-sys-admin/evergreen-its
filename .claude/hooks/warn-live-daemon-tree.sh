#!/bin/bash
# SessionStart — advisory topology notice (forensic class #5: worktree-on-live-tree).
#
# The live launchd daemons execute the ~/its working tree from disk every ~60s, so any
# uncommitted Python-source edit there goes live within a cycle, and committing in ~/its
# mid-cycle can strand the publish daemon on a feature branch. If this session is rooted
# at the live ~/its tree, surface a reminder to use a per-task worktree (+ its own venv)
# for Python-source edits. Advisory ONLY — SessionStart cannot block; this prints context
# and always exits 0 (the doc worktree_discipline.md reserved exactly this lightweight,
# low-false-positive surface).

# --- §56 strict mode, ADVISORY variant ----------------------------------------
# Strict mode is applied here for consistency with the block-*.sh guards, but
# this hook's contract is the OPPOSITE of theirs: it is a SessionStart advisory
# that MUST always exit 0 (SessionStart cannot block, and a non-zero exit here
# surfaces as a hook error on every session start). It invokes no `jq`, so it
# carries no dependency assertion.
#
# Every command substitution below is therefore explicitly tolerant (`|| true`).
# Without that, `set -e` makes a MISSING $HOME/its fatal — which is the normal
# state on every customer fork inheriting .claude/hooks/ and on any non-ITS
# checkout. Verified 2026-08-17: the un-tolerated form exits 1 there.
set -euo pipefail

cwd="$(pwd -P 2>/dev/null || true)"
its="$(cd "$HOME/its" 2>/dev/null && pwd -P || true)"
[ -n "$its" ] && [ "$cwd" = "$its" ] || exit 0

branch=$(git -C "$its" branch --show-current 2>/dev/null || true)
cat <<EOF
NOTE (ITS topology): this session is rooted at the LIVE daemon tree $its (branch: ${branch:-?}).
The launchd daemons run this tree from disk every ~60s — uncommitted Python edits go live, and
committing here mid-cycle can strand the publish daemon. For any Python-SOURCE edit, use a per-task
worktree off origin/main with its OWN FRESH venv:
  git worktree add -b feat/<task> ../its-<task> origin/main
  cd ../its-<task> && python3 -m venv .venv-wt && .venv-wt/bin/pip install -e '.[dev]'
Do NOT 'cp -R .venv' — a copied venv's bin/pip keeps a shebang pointing at ~/its/.venv, so
'.venv-wt/bin/pip install' silently repoints the LIVE editable install (corrupts the daemons).
Verify isolation: '.venv-wt/bin/pip show its' must say its-<task>; '~/its/.venv' must be unchanged.
Docs-only edits are fine here. See docs/operations/worktree_discipline.md.
EOF
exit 0
