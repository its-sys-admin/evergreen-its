#!/bin/bash
# Block CodeQL alert dismissal at the Bash PreToolUse layer.
#
# The codeql-fp-triager subagent is PROPOSE-ONLY: it surfaces candidate
# false positives with quoted evidence; a human applies the dismissal.
# This hook is the structural backstop so a misclassification can never
# silently dismiss a real alert. Listing and reading alerts (GET) is
# allowed; any code-scanning dismissal command is refused.
#
# Wired via the codeql-fp-triager agent frontmatter (hooks.PreToolUse,
# matcher: Bash). Mirrors the §38 git-guardrails precedent
# (.claude/hooks/block-dangerous-git.sh). Scoped to that one subagent —
# the operator's own session can still dismiss manually.


# --- §56 Guard Dependency Integrity (fail-closed) -----------------------------
# A guard whose failure mode is "permit" is not a guard. `jq` is a hard
# dependency of the payload parse below; if it is missing, or if any command in
# this script fails, we must BLOCK (exit 2) rather than fall through to exit 0.
# Audit 2026-08-16 C-1: every hook in this directory previously exited 0 when
# `jq` was absent from PATH, silently permitting force-push, doctrine writes,
# CodeQL dismissals and stale Cloudflare deploys.
set -euo pipefail

_guard_block() {
  echo "BLOCKED: $1" >&2
  exit 2
}

command -v jq >/dev/null 2>&1 \
  || _guard_block "guard $(basename "${BASH_SOURCE[0]}") requires \`jq\`, which is not on PATH. Failing CLOSED per Op Stds §56. Install jq (brew install jq) and retry."

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command')

# Block only a code-scanning DISMISSAL: must touch code-scanning AND
# carry a dismiss intent. GET list/read calls have neither and pass.
if echo "$COMMAND" | grep -qE "code-scanning" && echo "$COMMAND" | grep -qiE "dismiss"; then
  echo "BLOCKED: '$COMMAND' attempts a CodeQL alert dismissal. The codeql-fp-triager is propose-only — it surfaces candidate FPs with evidence; the operator applies dismissals manually. See .claude/agents/codeql-fp-triager.md." >&2
  exit 2
fi

exit 0
