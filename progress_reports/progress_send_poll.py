"""Progress Reports weekly send polling daemon — the thin PROGRESS entry.

Purpose
-------
The progress twin of ``safety_reports.weekly_send_poll``. Discovers ``WPR_human_review``
rows with ``Send Now`` (immediate) OR ``Approve for Scheduled Send`` (the
Monday-≥07:00-Pacific batch) checked, runs the F22 approval-attestation gate on the driving
checkbox against the **Progress Reporting** workspace, stamps the verified approver, and
dispatches each to ``progress_reports.progress_send.send_one_row``. The poller has zero send
capability of its own; it is an iterator + dispatcher. ``poll_once()`` is the public API; the
``__main__`` guard calls it exactly once and exits, launchd handling cadence via
``StartInterval`` (default 900 s = 15 min, sourced from ITS_Config
``progress_reports.progress_send.poll_interval_seconds`` at install).

Invariants (§42 — why a thin binding, not a clone)
--------------------------------------------------
- **P5 parameterize-not-clone (Op Stds §14):** the dispatch BODY lives in
  ``safety_reports/send_poll_core.py`` (a required no-default ``DaemonConfig``); this module
  binds the one ``CONFIG``, constructs this daemon's ``HeartbeatReporter``, and re-exports the
  test mock seams — exactly as ``weekly_send_poll`` is the thin SAFETY entry. The F22
  fail-closed gate, the no-double-send ``SENDING`` exclusion, and the per-row fence are written
  ONCE in the core.
- **No cross-workstream mix-up:** ``poll_sheet_id`` is the **WPR** sheet; ``f22_workspace_id``
  is the **Progress Reporting** workspace (approver set = membership of THAT workspace, never
  the Safety Portal's); ``send_fn`` dispatches the progress ``SendConfig`` (which resolves
  recipients only from ``ITS_Active_Jobs_Progress``). Independent lock + heartbeat + review
  sheet from safety's send poll, so the two coexist without a shared mutex.
- **Invariant 1 (External Send Gate):** zero AI capability. Dispatches via ``CONFIG.send_fn``
  (bound ``progress_send.send_one_row`` → ``weekly_send.send_one_row``); the AST gate in
  ``tests/test_capability_gating.py::SEND_SCRIPTS`` forbids ``anthropic_client`` / ``anthropic``
  in this file and ``progress_send.py``.

Failure modes
-------------
Each candidate row is dispatched under a per-row fence; one failing row never tears down the
cycle. The F22 gate is fail-CLOSED — a circuit-open / auth error reading the approver set
propagates (cycle aborts, zero sends) and an empty approver set blocks all sends
(``EMPTY_ALLOWLIST``). A disabled ``progress_reports.progress_send.polling_enabled`` ITS_Config
value short-circuits the cycle (operator pause). Successor-remediation tree:
``docs/runbooks/progress_send.md`` (Op Stds §43).

Consumers
---------
- launchd daemon ``org.solutionsmith.its.progress-send`` (the 15-min interval poll).
- Writes a ``progress_send_poll.last_run`` watchdog marker each cycle. Registering that slug in
  ``watchdog.TRACKED_JOBS`` (Check-C staleness) lands in the P5 watchdog slice alongside the
  ``progress_weekly_generate`` Check-I/C wiring (deferred there exactly as P4 deferred the
  compile slug) — until then the marker is written but not yet monitored.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from progress_reports import progress_send, wpr_review
from safety_reports import send_poll_core, weekly_send
from shared import approval_verification, sheet_ids
from shared.error_log import its_error_log
from shared.heartbeat import HeartbeatReporter, HeartbeatStatus
from shared.kill_switch import require_active
from shared.required_config import ConfigKey, resolve_and_log

# Re-export PollStats (the public return shape) at the entry for callers/tests.
PollStats = send_poll_core.PollStats

SCRIPT_NAME = "progress_reports.progress_send_poll"
WORKSTREAM = "progress_reports"

# ITS_Config keys (progress-scoped; never the safety keys).
CFG_POLLING_ENABLED = "progress_reports.progress_send.polling_enabled"
CFG_POLL_INTERVAL = "progress_reports.progress_send.poll_interval_seconds"
CFG_SCHEDULED_SEND_LOCAL = "progress_reports.progress_send.scheduled_send_local"
DEFAULT_SCHEDULED_SEND_LOCAL = "MON 07:00"
SEND_TZ = "America/Los_Angeles"

# HOUSE_REFLEXES §5 (dark-ship default-False) — the PROGRESS twin of the same flip applied
# to weekly_send_poll above, and to po/rfq/subcontract at CO-1: a MISSING/malformed
# `progress_reports.progress_send.polling_enabled` row must fail SAFE (send daemon
# disabled), never fail-open to SENDING. See weekly_send_poll for the full rationale — the
# short version is that `send_poll_core._read_str_setting` falls back on three branches, the
# NotFound one silently, so a True default let a deleted config row RE-ARM a paused send
# daemon with no signal. A send gate never fails open.
DEFAULT_POLLING_ENABLED = False
DEFAULT_POLL_INTERVAL = 900  # 15 minutes

# State paths. The heartbeat-row-id cache is the SHARED cross-daemon file (ARCH-2);
# only the liveness + lock files are per-daemon.
STATE_DIR = Path.home() / "its" / "state"
HEARTBEAT_PATH = STATE_DIR / "progress_send_heartbeat.txt"
LOCK_PATH = STATE_DIR / "progress_send.lock"
HEARTBEAT_ROW_STATE_PATH = STATE_DIR / "heartbeat_row_ids.json"

DAEMON_NAME = "progress_reports.progress_send_poll"

WATCHDOG_MARKER_DIR = Path.home() / "its" / ".watchdog"
WATCHDOG_JOB_SLUG = "progress_send_poll"

_REGISTRATION_INTERVAL_SECONDS = DEFAULT_POLL_INTERVAL
_REGISTRATION_SOURCE_ID = f"WPR_human_review ({sheet_ids.SHEET_WPR_HUMAN_REVIEW})"

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

# Send Status values the poller dispatches on. SENDING is EXCLUDED — the load-bearing
# no-double-send exclusion (see send_poll_core §42 + DaemonConfig __post_init__).
# PENDING + FAILED are candidates. The statuses are the shared WSR/WPR picklist
# (wpr_review re-exports them from wsr_review).
DISPATCH_STATUSES = frozenset({wpr_review.STATUS_PENDING, wpr_review.STATUS_FAILED})

# F22 wake reasons (operator page) vs forensic-only.
_WAKE_REASONS = frozenset({
    approval_verification.VerdictReason.UNAUTHORIZED_ACTOR,
    approval_verification.VerdictReason.EMPTY_ALLOWLIST,
})


# ---- The one PROGRESS DaemonConfig ---------------------------------------
#
# NO field defaults to a safety value (contamination gate, send_poll_core §42).
# `send_fn` is a LATE-BINDING lambda so `progress_send.send_one_row` stays patchable;
# it routes through the shared `weekly_send.send_one_row` engine with the PROGRESS
# SendConfig (progress_send.CONFIG), which resolves recipients only from
# ITS_Active_Jobs_Progress. `f22_workspace_id` is the Progress Reporting workspace —
# the approver authority for progress sends is membership of THAT workspace (§46).

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
    poll_sheet_id=sheet_ids.SHEET_WPR_HUMAN_REVIEW,
    f22_workspace_id=sheet_ids.WORKSPACE_PROGRESS_REPORTING,
    col_send_now=wpr_review.COL_SEND_NOW,
    col_approve_scheduled=wpr_review.COL_APPROVE_SCHEDULED,
    col_send_status=wpr_review.COL_SEND_STATUS,
    col_notes=wpr_review.COL_NOTES,
    col_approved_by=wpr_review.COL_APPROVED_BY,
    col_approved_at=wpr_review.COL_APPROVED_AT,
    dispatch_statuses=DISPATCH_STATUSES,
    status_pending=wpr_review.STATUS_PENDING,
    status_failed=wpr_review.STATUS_FAILED,
    max_send_retries=weekly_send.MAX_SEND_RETRIES,
    parse_retry_count=weekly_send._parse_retry_count,
    to_datetime=wpr_review.to_wsr_datetime,
    wake_reasons=_WAKE_REASONS,
    send_fn=lambda row_id: progress_send.send_one_row(row_id),
)

# #336 — the ITS_Config keys this daemon resolves at RUNTIME (polling_enabled + scheduled_send_local on
# the DaemonConfig, read by send_poll_core under CONFIG.config_workstream='progress_reports').
# *.poll_interval_seconds is EXCLUDED. #336-fix (review): from_mailbox IS re-declared here — this poll
# daemon is the PRODUCTION driver (send_fn → progress_send.send_one_row reads from_mailbox every
# dispatch); progress_send.main is the manual-rerun path, OFF the daemon.
REQUIRED_CONFIG: list[ConfigKey] = [
    ConfigKey(CONFIG.cfg_polling_enabled, CONFIG.config_workstream, CONFIG.default_polling_enabled, "bool"),
    ConfigKey(CONFIG.cfg_scheduled_send_local, CONFIG.config_workstream, CONFIG.default_scheduled_send_local, "str"),
    ConfigKey(progress_send.CONFIG.from_mailbox_cfg_key, progress_send.CONFIG.config_workstream, progress_send.CONFIG.from_mailbox_default, "str"),
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
    """ITS_Config string read (progress workstream) — kept as the smoke-test seam."""
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
