#!/bin/bash
# Block writes to version-gated doctrine at the Edit|Write PreToolUse layer.
#
# The session-close-maintainer subagent edits living docs (info-gap,
# memory-archive, tech-debt) directly, but `doctrine/` is version-gated
# and requires explicit operator approval. The agent's prompt already
# says "ask once before touching doctrine" — this hook is the structural
# backstop so a misfire can't silently rewrite an invariant.
#
# Wired via the session-close-maintainer agent frontmatter
# (hooks.PreToolUse, matcher: Edit|Write). Mirrors the §38 git-guardrails
# precedent. Matches any path under a doctrine/ directory in either repo.


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
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path')

if echo "$FILE_PATH" | grep -qE "/doctrine/"; then
  echo "BLOCKED: write to '$FILE_PATH' targets version-gated doctrine. The session-close-maintainer must not edit doctrine/ without explicit operator approval — surface the proposed change as a diff for the operator instead. See .claude/agents/session-close-maintainer.md." >&2
  exit 2
fi

exit 0
