#!/usr/bin/env bash
#
# ITS launchd helper — load, unload, and inspect ITS LaunchAgents.
#
# Substitutes __ITS_HOME__ → "$HOME/its" (and __POLL_INTERVAL_SECONDS__ for the
# interval daemons — see below) when copying the plist into
# ~/Library/LaunchAgents/. The plist files in this directory stay generic; the
# installed copies have concrete values.
#
# __POLL_INTERVAL_SECONDS__ (weekly-send, portal-poll, compile-now-poll, progress-send,
# fieldops-sync, po-poll, po-send, subcontract-poll, subcontract-send, estimate-poll, rfq-poll, rfq-send, manifest-poll): these plists carry the placeholder in <integer>StartInterval</integer>.
# `load`/`dry-run` resolve it from the optional [interval] arg, else a per-daemon default
# (900 / 60 / 90 / 900 / 90 / 90 / 900 / 120 / 900 / 120 / 120 / 900 respectively, matching the daemon's ITS_Config poll-interval row
# default — safety_reports.weekly_send / safety_reports.portal_poll /
# safety_reports.compile_now_poll / progress_reports.progress_send /
# field_ops.fieldops_sync / po_materials.po_poll / po_materials.po_send /
# subcontracts.subcontract_poll / subcontracts.subcontract_send /
# po_materials.estimate_poll / po_materials.rfq_poll / po_materials.rfq_send .poll_interval_seconds). The interval is BAKED into
# the installed plist, so a later ITS_Config change needs a re-install (pass the
# new value as [interval]). WITHOUT this substitution the installed plist keeps
# the literal placeholder and fails `plutil -lint` → the daemon won't load.
#
# Usage:
#   ./install.sh load    <plist> [interval]   # substitute, copy, bootstrap
#   ./install.sh unload  <plist>     # bootout, remove from LaunchAgents
#   ./install.sh status  [<plist>]   # show ITS jobs (all if no arg)
#   ./install.sh dry-run <plist> [interval]   # print resolved plist to stdout
#
# <plist> can be the filename (with or without .plist) or the label.
#
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ITS_HOME="${HOME}/its"
TARGET_DIR="${HOME}/Library/LaunchAgents"
LOG_DIR="${ITS_HOME}/logs/launchd"
UID_NUM="$(id -u)"

usage() {
    cat <<EOF
usage: $0 {load|unload|status|dry-run} [plist] [interval]

  load     <plist> [interval]   substitute placeholders + bootstrap into launchd
  unload   <plist>              bootout and remove from ~/Library/LaunchAgents/
  status   [plist]              list loaded ITS jobs (or one if specified)
  dry-run  <plist> [interval]   print the resolved plist content to stdout

  [interval] (positive integer seconds) overrides the StartInterval for the
  poll-interval daemons (weekly-send → default 900, portal-poll → default 60,
  compile-now-poll → default 90, progress-send → default 900,
  fieldops-sync → default 90, po-poll → default 90, po-send → default 900,
  subcontract-poll → default 120, subcontract-send → default 900,
  estimate-poll → default 120, rfq-poll → default 120, rfq-send → default 900,
  manifest-poll → default 120).
EOF
    exit 1
}

resolve_plist_name() {
    local name="$1"
    # Strip a trailing .plist if present, then add it back — normalizes input.
    name="${name%.plist}.plist"
    echo "$name"
}

# Per-daemon ITS_Config poll-interval row + default. Empty/empty for the
# non-interval daemons (calendar-driven or no placeholder) → substitution skipped.
poll_interval_config_key() {
    case "$1" in
        org.solutionsmith.its.weekly-send)   echo "safety_reports.weekly_send.poll_interval_seconds" ;;
        org.solutionsmith.its.portal-poll)   echo "safety_reports.portal_poll.poll_interval_seconds" ;;
        org.solutionsmith.its.compile-now-poll) echo "safety_reports.compile_now_poll.poll_interval_seconds" ;;
        org.solutionsmith.its.progress-send) echo "progress_reports.progress_send.poll_interval_seconds" ;;
        org.solutionsmith.its.fieldops-sync) echo "field_ops.fieldops_sync.poll_interval_seconds" ;;
        org.solutionsmith.its.po-poll)       echo "po_materials.po_poll.poll_interval_seconds" ;;
        org.solutionsmith.its.po-send)       echo "po_materials.po_send.poll_interval_seconds" ;;
        org.solutionsmith.its.subcontract-poll) echo "subcontracts.subcontract_poll.poll_interval_seconds" ;;
        org.solutionsmith.its.subcontract-send) echo "subcontracts.subcontract_send.poll_interval_seconds" ;;
        org.solutionsmith.its.estimate-poll)    echo "po_materials.estimate_poll.poll_interval_seconds" ;;
        org.solutionsmith.its.manifest-poll)    echo "field_ops.manifest_poll.poll_interval_seconds" ;;
        org.solutionsmith.its.rfq-poll)         echo "po_materials.rfq_poll.poll_interval_seconds" ;;
        org.solutionsmith.its.rfq-send)         echo "po_materials.rfq_send.poll_interval_seconds" ;;
        *) echo "" ;;
    esac
}
poll_interval_default() {
    case "$1" in
        org.solutionsmith.its.weekly-send)   echo "900" ;;
        org.solutionsmith.its.portal-poll)   echo "60" ;;
        org.solutionsmith.its.compile-now-poll) echo "90" ;;
        org.solutionsmith.its.progress-send) echo "900" ;;
        org.solutionsmith.its.fieldops-sync) echo "90" ;;
        org.solutionsmith.its.po-poll)       echo "90" ;;
        org.solutionsmith.its.po-send)       echo "900" ;;
        org.solutionsmith.its.subcontract-poll) echo "120" ;;
        org.solutionsmith.its.subcontract-send) echo "900" ;;
        org.solutionsmith.its.estimate-poll)    echo "120" ;;
        org.solutionsmith.its.manifest-poll)    echo "120" ;;
        org.solutionsmith.its.rfq-poll)         echo "120" ;;
        org.solutionsmith.its.rfq-send)         echo "900" ;;
        *) echo "" ;;
    esac
}

# Read a numeric ITS_Config setting via the venv python (reusing
# shared.smartsheet_client + keychain). Echoes the value, or nothing on any
# failure — Smartsheet may be unreachable / the token unseeded at cutover time,
# in which case the caller falls back to the per-daemon default.
read_its_config_interval() {
    local key="$1"
    [[ -n "$key" ]] || return 0
    "${ITS_HOME}/.venv/bin/python" - "$key" 2>/dev/null <<'PYEOF' || true
import sys
sys.path.insert(0, ".")
try:
    from shared import smartsheet_client
    # The ITS_Config row is scoped to the workstream that owns the key; the key's
    # leading dotted segment IS that workstream package ("safety_reports.…",
    # "progress_reports.…"), so derive it rather than hardcoding safety. A read miss
    # (workstream not yet seeded) just falls through to the caller's per-daemon default.
    key = sys.argv[1]
    workstream = key.split(".", 1)[0]
    raw = smartsheet_client.get_setting(key, workstream=workstream)
    interval = int(str(raw).strip())
    if interval >= 1:
        print(interval)
except Exception:
    pass
PYEOF
}

# Resolve __POLL_INTERVAL_SECONDS__ for a label: [interval] arg > ITS_Config row
# > per-daemon default. Echoes the value (empty for non-interval daemons, so the
# substitution is skipped). Returns 1 on a non-positive-integer value.
resolve_poll_interval() {
    local label="$1" cli_arg="${2:-}" key default value
    key="$(poll_interval_config_key "$label")"
    default="$(poll_interval_default "$label")"
    [[ -z "$key" && -z "$default" ]] && { printf ''; return 0; }  # non-interval daemon
    if [[ -n "$cli_arg" ]]; then
        value="$cli_arg"
    else
        value="$(cd "${ITS_HOME}" 2>/dev/null && read_its_config_interval "$key")"
        if [[ -z "$value" ]]; then
            echo "note: could not read ${key} from ITS_Config; using default ${default}s" >&2
            value="$default"
        fi
    fi
    if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
        echo "error: interval must be a positive integer of seconds, got: ${value}" >&2
        return 1
    fi
    printf '%s' "$value"
}

# Render a plist to stdout: always substitute __ITS_HOME__; substitute
# __POLL_INTERVAL_SECONDS__ only when $interval is non-empty (the interval
# daemons). Non-interval plists that mention the placeholder only in a comment
# (e.g. weekly-generate) are left untouched — comments don't fail plutil.
render_plist() {
    local src="$1" interval="${2:-}"
    if [[ -n "$interval" ]]; then
        sed -e "s|__ITS_HOME__|${ITS_HOME}|g" \
            -e "s|__POLL_INTERVAL_SECONDS__|${interval}|g" "$src"
    else
        sed "s|__ITS_HOME__|${ITS_HOME}|g" "$src"
    fi
}

cmd_load() {
    local plist; plist="$(resolve_plist_name "$1")"
    local cli_interval="${2:-}"
    local src="${SRC_DIR}/${plist}"
    local dst="${TARGET_DIR}/${plist}"
    local label="${plist%.plist}"

    [[ -f "$src" ]] || { echo "error: $src not found" >&2; exit 1; }

    # Resolve the interval BEFORE writing $dst so a bad value aborts cleanly.
    local interval
    interval="$(resolve_poll_interval "$label" "$cli_interval")" || exit 1

    mkdir -p "$TARGET_DIR" "$LOG_DIR"

    # Substitute __ITS_HOME__ (+ __POLL_INTERVAL_SECONDS__ for interval daemons).
    render_plist "$src" "$interval" > "$dst"

    # Validate before loading — plutil catches XML/typing errors early.
    if ! plutil -lint "$dst" >/dev/null; then
        echo "error: $dst failed plutil -lint; not loading" >&2
        rm -f "$dst"
        exit 1
    fi

    # Unload first if already loaded; ignore errors.
    launchctl bootout "gui/${UID_NUM}/${label}" 2>/dev/null || true

    launchctl bootstrap "gui/${UID_NUM}" "$dst"
    echo "loaded: ${label}"
    echo "  plist:  $dst"
    # Read the real log paths straight from the installed plist. The label's last
    # segment is hyphenated (portal-poll, fieldops-sync) but StandardOutPath/
    # StandardErrorPath use underscores (portal_poll.out.log), so reconstructing
    # from "${label##*.}" printed a path that doesn't exist. $dst is already
    # rendered (__ITS_HOME__ substituted) and passed plutil -lint above.
    echo "  stdout: $(plutil -extract StandardOutPath raw "$dst")"
    echo "  stderr: $(plutil -extract StandardErrorPath raw "$dst")"
}

cmd_unload() {
    local plist; plist="$(resolve_plist_name "$1")"
    local dst="${TARGET_DIR}/${plist}"
    local label="${plist%.plist}"

    if [[ -f "$dst" ]]; then
        launchctl bootout "gui/${UID_NUM}/${label}" 2>/dev/null || true
        rm "$dst"
        echo "unloaded: ${label}"
    else
        # Try bootout in case label exists but file is missing.
        launchctl bootout "gui/${UID_NUM}/${label}" 2>/dev/null && \
            echo "unloaded: ${label} (file was missing)" || \
            echo "not loaded: ${label}"
    fi
}

cmd_status() {
    if [[ $# -gt 0 ]]; then
        local plist; plist="$(resolve_plist_name "$1")"
        local label="${plist%.plist}"
        if launchctl print "gui/${UID_NUM}/${label}" >/dev/null 2>&1; then
            launchctl print "gui/${UID_NUM}/${label}" | \
                grep -E "^\s+(state|pid|last exit code|path|program)" | \
                sed 's/^[[:space:]]*//'
        else
            echo "not loaded: ${label}"
        fi
    else
        local out
        out="$(launchctl list 2>/dev/null | grep -E "\sorg\.solutionsmith\.its\." || true)"
        if [[ -z "$out" ]]; then
            echo "no ITS jobs loaded"
        else
            printf '%-8s %-8s %s\n' "PID" "EXIT" "LABEL"
            echo "$out"
        fi
    fi
}

cmd_dry_run() {
    local plist; plist="$(resolve_plist_name "$1")"
    local cli_interval="${2:-}"
    local src="${SRC_DIR}/${plist}"
    local label="${plist%.plist}"
    [[ -f "$src" ]] || { echo "error: $src not found" >&2; exit 1; }
    local interval
    interval="$(resolve_poll_interval "$label" "$cli_interval")" || exit 1
    render_plist "$src" "$interval"
}

main() {
    local cmd="${1:-}"
    case "$cmd" in
        load)    [[ $# -ge 2 ]] || usage; cmd_load    "$2" "${3:-}" ;;
        unload)  [[ $# -ge 2 ]] || usage; cmd_unload  "$2" ;;
        status)  shift; cmd_status "$@" ;;
        dry-run) [[ $# -ge 2 ]] || usage; cmd_dry_run "$2" "${3:-}" ;;
        *) usage ;;
    esac
}

main "$@"
