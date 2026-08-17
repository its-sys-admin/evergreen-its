"""Safety Reports weekly send polling daemon — the thin SAFETY entry.

Phase-5: discovers `WSR_human_review` rows with `Send Now` (immediate) OR
`Approve for Scheduled Send` (the Monday-≥07:00-Pacific batch) checked, runs the
F22 approval-attestation gate on the driving checkbox, stamps the verified approver
(Approved By/At), and dispatches each to `safety_reports.weekly_send.send_one_row`.
The poller has zero send capability of its own; it is an iterator + dispatcher. The
handler is the only place `graph_client.send_mail` is called.

P1c — parameterize-not-clone (Op Stds §14). The dispatch BODY now lives in
`safety_reports/send_poll_core.py`, parameterized by a required no-default
`DaemonConfig`; this module is the thin SAFETY entry that binds the one config
(`CONFIG`), constructs this daemon's `HeartbeatReporter`, and re-exports the
test mock seams. A future `progress_send_poll` binds its own `DaemonConfig` over
the same core — the F22 fail-closed gate, the no-double-send SENDING exclusion,
and the per-row fence are written ONCE in the core (§42 there).

launchd schedule
----------------

Single-cycle execution: `poll_once()` is the public API; the `__main__` guard
calls it exactly once and exits. launchd handles cadence via `StartInterval` in
`scripts/launchd/org.solutionsmith.its.weekly-send.plist` (default 900 s = 15 min;
sourced from ITS_Config `safety_reports.weekly_send.poll_interval_seconds` at install).

Capability gating
-----------------

Zero AI capability. Dispatches a send via `CONFIG.send_fn` (bound
`weekly_send.send_one_row`). The AST gate in `tests/test_capability_gating.py::
SEND_SCRIPTS` forbids `anthropic_client` / `anthropic` in this file, in
`send_poll_core.py`, AND in `weekly_send.py`.

Heartbeat
---------

The ITS_Daemon_Health heartbeat helpers live in `shared/heartbeat.py`
(`HeartbeatReporter`); this module keeps only the two test-mock seams
(`_write_heartbeat` / `_write_heartbeat_row`) as thin delegators to the
module-level `_heartbeat_reporter`. The per-daemon registration metadata is its
constructor config. (P0 #344 closed the earlier verbatim-duplication; the core
calls these seams via injection so the suite's entry-module patches stay valid.)
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from safety_reports import send_poll_core, weekly_send, wsr_review
from shared import approval_verification, sheet_ids
from shared.error_log import its_error_log
from shared.heartbeat import HeartbeatReporter, HeartbeatStatus
from shared.kill_switch import require_active
from shared.required_config import ConfigKey, resolve_and_log

# Re-export PollStats (the public return shape) at the entry for callers/tests.
PollStats = send_poll_core.PollStats

SCRIPT_NAME = "safety_reports.weekly_send_poll"
WORKSTREAM = "safety_reports"

# ITS_Config keys.
CFG_POLLING_ENABLED = "safety_reports.weekly_send.polling_enabled"
CFG_POLL_INTERVAL = "safety_reports.weekly_send.poll_interval_seconds"
CFG_SCHEDULED_SEND_LOCAL = "safety_reports.weekly_send.scheduled_send_local"
DEFAULT_SCHEDULED_SEND_LOCAL = "MON 07:00"
SEND_TZ = "America/Los_Angeles"

# HOUSE_REFLEXES §5 (dark-ship default-False), extending the CO-1 flip already applied to
# po_send_poll / rfq_send_poll / subcontract_send_poll to the last two fail-open lanes: a
# MISSING/malformed `safety_reports.weekly_send.polling_enabled` row must fail SAFE (send
# daemon disabled), never fail-open to SENDING. This lane defaulted True from before the
# reflex existed, which meant `send_poll_core._read_str_setting`'s three fallback branches
# (SmartsheetNotFoundError — which logs NOTHING — plus circuit-open and transient read
# error) silently RE-ARMED an operator-paused send daemon: deleting or renaming the config
# row resumed customer email with no signal on any surface. The seeded row is load-bearing
# for normal operation; this default only governs the row-absent case. A send gate never
# fails open.
DEFAULT_POLLING_ENABLED = False
DEFAULT_POLL_INTERVAL = 900  # 15 minutes

# State paths.
STATE_DIR = Path.home() / "its" / "state"
HEARTBEAT_PATH = STATE_DIR / "weekly_send_heartbeat.txt"
LOCK_PATH = STATE_DIR / "weekly_send.lock"
HEARTBEAT_ROW_STATE_PATH = STATE_DIR / "heartbeat_row_ids.json"

DAEMON_NAME = "safety_reports.weekly_send_poll"

WATCHDOG_MARKER_DIR = Path.home() / "its" / ".watchdog"
WATCHDOG_JOB_SLUG = "safety_weekly_send_poll"

_REGISTRATION_INTERVAL_SECONDS = DEFAULT_POLL_INTERVAL
_REGISTRATION_SOURCE_ID = f"WSR_human_review ({sheet_ids.SHEET_WSR_HUMAN_REVIEW})"

# Shared ITS_Daemon_Health reporter for this daemon.
_heartbeat_reporter = HeartbeatReporter(
    script_name=SCRIPT_NAME,
    daemon_name=DAEMON_NAME,
    workstream=WORKSTREAM,
    liveness_path=HEARTBEAT_PATH,
    interval_seconds=_REGISTRATION_INTERVAL_SECONDS,
    source_id=_REGISTRATION_SOURCE_ID,
    row_state_path=HEARTBEAT_ROW_STATE_PATH,
)

# Send Status values the poller dispatches on. SENDING is EXCLUDED — the
# load-bearing no-double-send exclusion (see send_poll_core §42 + DaemonConfig
# __post_init__). PENDING + FAILED are candidates.
DISPATCH_STATUSES = frozenset({weekly_send.STATUS_PENDING, weekly_send.STATUS_FAILED})

# F22 wake reasons (operator page) vs forensic-only.
_WAKE_REASONS = frozenset({
    approval_verification.VerdictReason.UNAUTHORIZED_ACTOR,
    approval_verification.VerdictReason.EMPTY_ALLOWLIST,
})


# ---- The one SAFETY DaemonConfig -----------------------------------------
#
# NO field defaults to a safety value (contamination gate, send_poll_core §42).
# `send_fn` is a LATE-BINDING lambda so `weekly_send.send_one_row` stays patchable;
# it passes the safety `weekly_send.CONFIG` (P1b's SendConfig) as the second arg.

CONFIG = send_poll_core.DaemonConfig(
    script_name=SCRIPT_NAME,
    config_workstream=WORKSTREAM,
    daemon_name=DAEMON_NAME,
    lock_path=LOCK_PATH,
    watchdog_marker_dir=WATCHDOG_MARKER_DIR,
    watchdog_job_slug=WATCHDOG_JOB_SLUG,
    cfg_polling_enabled=CFG_POLLING_ENABLED,
    default_polling_enabled=DEFAULT_POLLING_ENABLED,
    cfg_scheduled_send_local=CFG_SCHEDULED_SEND_LOCAL,
    default_scheduled_send_local=DEFAULT_SCHEDULED_SEND_LOCAL,
    send_tz=SEND_TZ,
    poll_sheet_id=sheet_ids.SHEET_WSR_HUMAN_REVIEW,
    f22_workspace_id=sheet_ids.WORKSPACE_SAFETY_PORTAL,
    col_send_now=wsr_review.COL_SEND_NOW,
    col_approve_scheduled=wsr_review.COL_APPROVE_SCHEDULED,
    col_send_status=wsr_review.COL_SEND_STATUS,
    col_notes=wsr_review.COL_NOTES,
    col_approved_by=wsr_review.COL_APPROVED_BY,
    col_approved_at=wsr_review.COL_APPROVED_AT,
    dispatch_statuses=DISPATCH_STATUSES,
    status_pending=weekly_send.STATUS_PENDING,
    status_failed=weekly_send.STATUS_FAILED,
    max_send_retries=weekly_send.MAX_SEND_RETRIES,
    parse_retry_count=weekly_send._parse_retry_count,
    to_datetime=wsr_review.to_wsr_datetime,
    wake_reasons=_WAKE_REASONS,
    send_fn=lambda row_id: weekly_send.send_one_row(row_id, weekly_send.CONFIG),
)

# #336 — the ITS_Config keys this daemon resolves at RUNTIME (both carried on the DaemonConfig,
# read by send_poll_core under CONFIG.config_workstream='safety_reports'). The
# *.poll_interval_seconds key is EXCLUDED (declared but never runtime-read). Declared for the
# startup observability pass. #336-fix (review): the send-time from_mailbox key IS re-declared here —
# THIS poll daemon is the PRODUCTION driver (send_fn → weekly_send.send_one_row reads from_mailbox on
# every dispatch); weekly_send.main is the manual-rerun path, OFF the daemon, so declaring from_mailbox
# only there left it invisible on real automated sends.
REQUIRED_CONFIG: list[ConfigKey] = [
    ConfigKey(CONFIG.cfg_polling_enabled, CONFIG.config_workstream, CONFIG.default_polling_enabled, "bool"),
    ConfigKey(CONFIG.cfg_scheduled_send_local, CONFIG.config_workstream, CONFIG.default_scheduled_send_local, "str"),
    ConfigKey(weekly_send.CONFIG.from_mailbox_cfg_key, weekly_send.CONFIG.config_workstream, weekly_send.CONFIG.from_mailbox_default, "str"),
]


# ---- Test-mock seams (the suite patches these exact symbols) --------------
# The core calls these via INJECTION (resolved from this module at poll-call
# time), so patching them here still bites inside a poll cycle.


def _write_heartbeat() -> None:
    """Liveness file touch — thin delegator to the shared HeartbeatReporter."""
    _heartbeat_reporter.write_liveness()


def _write_heartbeat_row(
    *,
    status: HeartbeatStatus,
    items_processed: int,
    error_summary: str | None = None,
    correlation_id: str | None = None,
    notes: str | None = None,
    daemon_name: str = DAEMON_NAME,
) -> None:
    """ITS_Daemon_Health per-cycle row update — thin delegator to the reporter."""
    _heartbeat_reporter.write_row(
        status=status,
        items_processed=items_processed,
        error_summary=error_summary,
        correlation_id=correlation_id,
        notes=notes,
        daemon_name=daemon_name,
    )


def _write_watchdog_marker() -> None:
    """Touch the Check C freshness marker — delegates to the core with CONFIG."""
    send_poll_core._write_watchdog_marker(CONFIG)


def _stamp_approval(row_id: int, verdict: approval_verification.ApprovalVerdict) -> None:
    """Stamp the verified approver — delegates to the core with CONFIG."""
    send_poll_core._stamp_approval(CONFIG, row_id, verdict)


# Pure helpers re-exported so direct callers/tests (and the injection) resolve
# them on THIS module.
def _is_scheduled_window(now_local: datetime, spec: str) -> bool:
    return send_poll_core._is_scheduled_window(now_local, spec)


def _parse_scheduled_spec(spec: str):  # -> tuple[int, time]
    return send_poll_core._parse_scheduled_spec(spec)


def _filter_dispatch_candidates(rows):
    return send_poll_core._filter_dispatch_candidates(CONFIG, rows)


def _load_authorized_approvers():
    return send_poll_core._load_authorized_approvers(CONFIG)


def _read_str_setting(key: str, fallback: str) -> str:
    """ITS_Config string read (safety workstream) — kept as the smoke-test seam."""
    return send_poll_core._read_str_setting(CONFIG, key, fallback)


# The injected I/O seams are resolved from THIS module at call time (passed
# explicitly below, not via **kwargs, so mypy verifies the callable types) so the
# suite's entry-module patches apply inside a poll cycle.


def _poll_inside_lock() -> PollStats:
    """Body of poll_once under the file lock — delegates to the core with CONFIG."""
    return send_poll_core.poll_inside_lock(
        CONFIG,
        write_liveness=_write_heartbeat,
        write_row=_write_heartbeat_row,
        write_watchdog_marker=_write_watchdog_marker,
        stamp_approval=_stamp_approval,
        is_scheduled_window=_is_scheduled_window,
    )


# ---- Public API ----------------------------------------------------------


@its_error_log(SCRIPT_NAME)
@require_active
def poll_once() -> PollStats:
    """Run one poll cycle. Public API; idempotent across crashes."""
    # #336 startup observability (after @require_active, fail-open). Additive (§14).
    resolve_and_log(SCRIPT_NAME, REQUIRED_CONFIG)

    return send_poll_core.poll_once(
        CONFIG,
        write_liveness=_write_heartbeat,
        write_row=_write_heartbeat_row,
        write_watchdog_marker=_write_watchdog_marker,
        stamp_approval=_stamp_approval,
        is_scheduled_window=_is_scheduled_window,
    )


if __name__ == "__main__":
    poll_once()
