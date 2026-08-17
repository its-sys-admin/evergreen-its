#!/bin/bash
# Block any write/mutation attempted by the doc-reconciliation-auditor subagent.
#
# The doc-reconciliation-auditor is PROPOSE-ONLY: it emits a findings report for
# operator action and must NEVER edit files, close tech-debt, bump versions, or
# otherwise mutate the repo. An auto-editing doctrine agent would reintroduce the
# very drift it exists to surface. This hook is the structural backstop so a
# misfire cannot silently reconcile drift. Mirrors block-codeql-dismiss.sh /
# block-doctrine-write.sh (the §38 git-guardrails precedent).
#
# Wired via the agent frontmatter (hooks.PreToolUse, matcher
# Edit|Write|MultiEdit|NotebookEdit|Bash). Any write tool is refused outright;
# Bash is allowed for READ-ONLY inspection but refused for mutation
# (git-write / gh-write / sed -i / tee / rm|mv|cp / redirect-to-file).


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
TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty')

case "$TOOL" in
  Edit|Write|MultiEdit|NotebookEdit)
    TARGET=$(echo "$INPUT" | jq -r '.tool_input.file_path // .tool_input.notebook_path // "(unknown)"')
    echo "BLOCKED: doc-reconciliation-auditor is propose-only and cannot write ('$TOOL' -> '$TARGET'). Emit findings for the operator to apply instead. See .claude/agents/doc-reconciliation-auditor.md." >&2
    exit 2
    ;;
  Bash)
    COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
    BLOCK=""
    # (1) Repo/state-mutating verbs. Leading (^|sep|space) anchor avoids matching
    #     these as substrings (e.g. the "rm" in "confirm").
    if echo "$COMMAND" | grep -qE '(^|[;&|]|[[:space:]])(git[[:space:]]+(commit|push|add|merge|rebase|reset|checkout|restore|tag|branch|cherry-pick|apply|am|mv|rm|clean|stash)|sed[[:space:]]+-i|tee([[:space:]]|$)|dd[[:space:]]|truncate[[:space:]]|(rm|mv|cp)[[:space:]])'; then
      BLOCK=1
    fi
    # (2) gh write verbs / mutating REST methods.
    if echo "$COMMAND" | grep -qiE 'gh[[:space:]]+(pr|issue|release|repo|api|run|secret|label)[[:space:]].*(create|edit|merge|close|delete|comment|--method[[:space:]]*(POST|PATCH|PUT|DELETE)|-X[[:space:]]*(POST|PATCH|PUT|DELETE))'; then
      BLOCK=1
    fi
    # (3) Output redirection to a real file (allow /dev/null and fd-dup like >&2).
    if echo "$COMMAND" | grep -oE '>>?[[:space:]]*[^[:space:]&]+' | grep -qvE '/dev/null'; then
      BLOCK=1
    fi
    if [ -n "$BLOCK" ]; then
      echo "BLOCKED: doc-reconciliation-auditor is propose-only; refusing mutating Bash command: '$COMMAND'. Reads are allowed (cat/grep/git log|diff|show|status, gh ... view|list, python -m scripts.check_doctrine_drift). See .claude/agents/doc-reconciliation-auditor.md." >&2
      exit 2
    fi
    ;;
esac

exit 0
