"""ITS daily watchdog — runs every morning at 7:00 AM via launchd.

Verifies operational state via per-check probes. Silent if green; surfaces
WARN / CRITICAL through `shared.error_log.log()` (which fans out to the
local log file, ITS_Errors, and the Resend/Sentry legs on CRITICAL).

Kill-switch semantics (Op Stds v9 §2):
    ACTIVE      — run all checks normally.
    MAINTENANCE — run all checks; alerts suppressed (WARN/CRITICAL results
                  downgraded to INFO before routing, so the Smartsheet row
                  still lands but Resend/Sentry legs never fire).
    PAUSED      — skip all checks; single INFO line "PAUSED — skipping".

Failure isolation (Op Stds v9 §27):
    Each check runs inside `_run_check`, which catches `Exception` and emits
    a distinguishable marker line through `error_log.log` at ERROR severity.
    A failure in one check does NOT prevent later checks from running. The
    harness itself writes to no side channel — `error_log.log()` has its
    own three recursion guards (Smartsheet, Resend, Sentry), so the harness
    does NOT need a separate recursion guard.

Checks shipped:
    A. Stale ITS_Review_Queue items (PENDING past 2× SLA) — WARN. Session 1.
    B. Open CRITICAL ITS_Errors rows (Resolved At blank) — WARN. Session 1.
    C. Scheduled-jobs last-run via marker files — Session 2+. Each entry in
       TRACKED_JOBS must have written a {slug}.last_run marker within its
       freshness window (default 24h; per-job overrides in
       TRACKED_JOB_WINDOWS). Tracked today: safety_weekly_generate,
       safety_weekly_send_poll, and safety_picklist_audit. A missing or
       stale marker is a WARN. The watchdog's own run-marker is intentionally
       NOT in TRACKED_JOBS — a daemon can't reliably detect its own death;
       that's the external heartbeat observer's job (see main()).
    D. 14-day reviewer-chain forward scan — Session 2. Logs an INFO ANOMALY
       row to ITS_Review_Queue per workstream with reviewer-chain gaps in
       the next 14 days (Op Stds v9 §18).
    F. (RETIRED 2026-06-05) Mail-intake silent-disable check — removed with
       the safety email-intake retirement (the Safety Portal PULL model
       supersedes the safety@ mailbox; see decision_phase5-portal-transport).
       portal_poll's basic liveness is now covered by Check C (its
       safety_portal_poll marker, registered 2026-06-06); a dedicated
       silent-disable health check for it and an Email-Triage mailbox-silence
       check remain future additions when those surfaces mature.
    G. Alert-routing dedupe summary sweep — Session 3 (PR β). For each
       expired entry in `~/its/state/alert_dedupe.json`, fire a single
       operator summary email naming what was suppressed during the
       window, mark the entry, and delete it on the next sweep
       (two-phase deletion for crash safety). Summary emails are a
       Resend-only push notification — they do NOT write to ITS_Errors
       or Sentry (no new forensic data; the rows already exist).

       MAINTENANCE behavior: Check G receives `alerts_suppressed` via
       signature inspection in `_run_check` and DEFERS phase-1 summary
       firing (no Resend, no mark) while the kill switch is in
       MAINTENANCE. Entries stay in expired+unsummarized state; the
       first post-MAINTENANCE sweep fires the deferred digest normally.
       Phase-2 deletion (already-summarized or clean-expired entries)
       still proceeds during MAINTENANCE because that path has no push
       side-effect. Op Stds v10 §2 codifies this carve-out.
    I. weekly_generate catch-up recovery — 2026-06-01. weekly_generate is
       the one tracked daemon on a calendar schedule (StartCalendarInterval,
       Friday 14:00), so a *crashed* Friday cycle is not re-invoked by
       launchd until the next Friday — launchd treats a started-then-failed
       job as "ran" (unlike the interval pollers, whose next StartInterval
       tick IS their recovery). Check C detects the resulting marker
       staleness (8-day window) and the external UptimeRobot ping (audit
       F16) covers total-host death, but neither *recovers* the missed run.
       Check I closes that gap: on a subsequent daily run, if the current
       target week's generation did not run and we are still inside a short
       catch-up window, it re-fires the generation once. CRITICAL triple-fire
       on catch-up failure (page deferred — record kept — during MAINTENANCE
       per the push-vs-record carve-out). See the in-code rationale above
       `_check_weekly_generate_catchup`.

       (There is no Check H. Doctrine once named a heartbeat-staleness check
       "Check H", but that mechanism was never built — the marker-file Check
       C is the staleness floor. Corrected in the 2026-06-01 blueprint
       doctrine pass; the next free check letter is therefore I.)
    O. Row-cap rotation for ITS_Errors + ITS_Review_Queue (eval A5, growth
       Slice 1). The Smartsheet per-sheet row cap is a verified 20,000 (not
       the eval's 5,000); past it add_rows fails, losing the forensic record
       and blinding Check B. WARN at 15,000; at 16,000 delete TERMINAL rows
       older than 90d, oldest-first, batches of 200 (thresholds in
       shared/defaults.py). If the 90d pass finds nothing, STORM-MODE
       re-selects at the 48h storm floor (2026-07-13 incident — retention
       can exceed the system's age). NEVER deletes open CRITICALs (blank
       Resolved At) or PENDING / IN_REVIEW / ESCALATED queue rows. Every
       rotation writes an ITS_Errors summary record (never silent);
       CRITICAL only when even the storm floor finds nothing deletable.
       `--dry` CLI flag previews.

Planned (NOT in this file, scheduled for a follow-on PR — the Check E
shipping PR; see `docs/tech_debt.md`):
    E. Anthropic spend trend. Deferred from Session 2 — the Admin API key
       provisioning is the operator's prerequisite, not a code path.

Trigger this script from a launchd plist. See `scripts/launchd/`.
"""
from __future__ import annotations

import inspect
import json
import shutil
import subprocess
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from po_materials import po_review, rfq_review
from progress_reports import progress_weekly_generate, wpr_review
from safety_reports import generate_core, portal_poll, weekly_generate, wsr_review
from shared import (
    alert_dedupe,
    box_client,
    circuit_breaker,
    defaults,
    heartbeat_client,
    keychain,
    log_rotation,
    portal_client,
    resend_client,
    review_queue,
    safety_week,
    sheet_ids,
    smartsheet_client,
    state_io,
    sustained_failure,
)
from shared.error_log import (
    Severity,
    _alert_critical,
    _maybe_fire_window_summary,
    its_error_log,
    log,
)
from shared.errors_rotation import errors_row_is_terminal, row_age_date
from shared.kill_switch import SystemState, check_system_state
from shared.required_config import ConfigKey, resolve_and_log
from shared.review_queue import ReviewReason, SlaTier
from shared.scheduling import TimeOffClient, is_federal_holiday, resolve_chain
from subcontracts import subcontract_review

_SCRIPT = "scripts.watchdog"

# #336 — every ITS_Config key the watchdog resolves at RUNTIME across its checks + heartbeat
# beacon, declared for the startup observability pass (resolve_and_log). Two are 'global'-scoped;
# the Worker base URL is read under safety_reports (reusing portal_poll's canonical names).
REQUIRED_CONFIG: list[ConfigKey] = [
    ConfigKey(
        "circuit_breaker.prolonged_open_alert_seconds", "global",
        defaults.CIRCUIT_BREAKER_PROLONGED_OPEN_ALERT_SECONDS, "int",
    ),
    ConfigKey("system.heartbeat_url", "global", "", "str"),
    ConfigKey(
        portal_poll.CFG_WORKER_BASE_URL, portal_poll.WORKSTREAM, "", "str",
        description="Shared Worker base URL, read for Check V's prune-status creds.",
    ),
]

# Caps prevent the WARN detail string from ballooning when something goes
# truly sideways (e.g., dozens of rows past SLA after a long PAUSED window).
# 5 is enough for an operator to triage; the full set is one Smartsheet
# query away anyway.
REVIEW_QUEUE_ITEM_CAP = 5
CRITICAL_ITEMS_CAP = 5

# Check C scaffold. Marker dir lives under ~/its/ so it's co-located with
# the rest of the codebase but stays out of the repo (.gitignored). Each
# scheduled job calls `write_last_run_marker(<job_name>)` on success;
# Check C verifies the markers stay fresh for everything in TRACKED_JOBS.
WATCHDOG_MARKER_DIR = Path.home() / "its" / ".watchdog"
# Watchdog state dir (first-seen / baseline snapshots for the P5 operability checks T/U).
# All writes go through shared.state_io (atomic + path-locked) per the state-write discipline.
STATE_DIR = Path.home() / "its" / "state"

# ---- The review sheets every send lane stages rows on -----------------------
#
# (label, sheet id, send-status column, HELD value, SENDING value) for all FIVE
# send lanes. Checks N (stuck SENDING) and T (stale HELD) are both backstops for
# the SHARED send engine — the engine writes its write-ahead SENDING marker and
# every HELD disposition to `cfg.review.SHEET_ID`, i.e. whichever review sheet the
# lane's SendConfig names — so both checks must scan every lane, not just safety.
#
# They did not. Until 2026-07-21 Check N scanned ONLY WSR_human_review and Check T
# only WSR + WPR, so on the three PROCUREMENT lanes (po_send, rfq_send,
# subcontract_send — the ones that transmit to outside vendors) a row stuck in
# SENDING was never reported at all, and a row stuck HELD had no 24h backstop.
# A stuck SENDING row is the worst case: by design it is never re-dispatched (to
# avoid a double-send), so it sits silently unsent forever.
#
# Both checks now derive from this ONE table, so they can never again drift apart
# or fall behind a lane. `tests/test_watchdog.py::test_review_scan_covers_every_send_lane`
# derives the required set from the send daemons themselves and fails if a lane is
# missing here.
_REVIEW_SHEETS: list[tuple[str, int, str, str, str]] = [
    ("WSR", wsr_review.SHEET_ID, wsr_review.COL_SEND_STATUS,
     wsr_review.STATUS_HELD, wsr_review.STATUS_SENDING),
    ("WPR", wpr_review.SHEET_ID, wpr_review.COL_SEND_STATUS,
     wpr_review.STATUS_HELD, wpr_review.STATUS_SENDING),
    ("PO", po_review.SHEET_ID, po_review.COL_SEND_STATUS,
     po_review.STATUS_HELD, po_review.STATUS_SENDING),
    ("RFQ", rfq_review.SHEET_ID, rfq_review.COL_SEND_STATUS,
     rfq_review.STATUS_HELD, rfq_review.STATUS_SENDING),
    ("SUB", subcontract_review.SHEET_ID, subcontract_review.COL_SEND_STATUS,
     subcontract_review.STATUS_HELD, subcontract_review.STATUS_SENDING),
]
TRACKED_JOBS: list[str] = [
    "safety_weekly_generate",
    "safety_weekly_send_poll",
    "safety_picklist_audit",
    # safety_intake removed 2026-06-05: the safety email-intake poller is RETIRED
    # (Safety Portal PULL model supersedes it). The tombstone writes no marker, so
    # tracking it would perpetually WARN.
    # C4: the hourly picklist SYNC job (run_picklist_sync) — distinct from the
    # weekly picklist AUDIT above. Previously untracked, so its silent death was
    # invisible; it now writes a safety_picklist_sync marker each run.
    "safety_picklist_sync",
    # Safety Portal pull-model poller (safety_reports.portal_poll). Writes a
    # safety_portal_poll.last_run marker each cycle; registered here at the
    # 2026-06-06 deploy session (previously a deferred "future addition") so a dead
    # puller surfaces via Check C.
    "safety_portal_poll",
    # On-demand Compile-Now poller (safety_reports.compile_now_poll, Part B). Writes a
    # safety_compile_now_poll.last_run marker each cycle. It is INTENDED to be always-loaded
    # (on-demand compile only works while it runs); ACTIVATION is `install.sh load
    # org.solutionsmith.its.compile-now-poll`. Until the operator loads it, this entry
    # correctly WARNs (the daemon is not running) — register + load together.
    "safety_compile_now_poll",
    # Progress Reporting daemons (P5). The compile is Friday 14:30 calendar-driven
    # (progress_weekly_generate, mirrors the safety weekly compile → 8-day window + Check-I
    # catch-up below); the send poll is a 15-min interval (progress_send_poll → 30-min window,
    # mirrors safety_weekly_send_poll). Both write their markers now; like
    # safety_compile_now_poll, these entries WARN until the operator LOADS the progress plists
    # at the progress cutover (register + load together — the daemons are dark until intake is
    # flipped on). Closes the P4/P5 "marker written but nothing reads it" gap.
    "progress_weekly_generate",
    "progress_send_poll",
    # P2.5 job up-sync dual-sheet mirror daemon (field_ops.fieldops_sync). Writes a
    # fieldops_sync.last_run marker each cycle; registered here at the 2026-07-01 cutover
    # deploy. Unlike the compile-now / progress entries above, it is ALREADY loaded + live
    # (sync_enabled=true, running each ~90s), so it will NOT WARN spuriously — its marker is
    # fresh. Check C surfaces a silent death of this live daemon.
    "fieldops_sync",
    # Purchase-Order pull daemon (po_materials.po_poll, PO S4). Writes a
    # po_poll.last_run marker each cycle once ACTIVE. Like the compile-now / progress
    # entries this WARNs until the operator both LOADS the plist (`install.sh load
    # org.solutionsmith.its.po-poll`) AND flips at least one po_materials.po_poll.*
    # gate — a loaded daemon whose gates are all false is a pre-lock no-op and writes
    # NO marker (an intentional dark state, not a fault). Register + activate together.
    "po_poll",
    # Purchase-Order SEND poll daemon (po_materials.po_send_poll, PO S5b). Writes a
    # po_send_poll.last_run marker each cycle once ACTIVE. Like po_poll it WARNs until the
    # operator both LOADS the plist (`install.sh load org.solutionsmith.its.po-send`) AND
    # flips po_materials.po_send.polling_enabled true — a loaded daemon with polling off is
    # a pre-lock no-op and writes NO marker (intentional dark state). Register + activate
    # together.
    "po_send_poll",
    # Subcontract pull daemon (subcontracts.subcontract_poll, SC-S3c). Writes a
    # subcontract_poll.last_run marker each cycle once ACTIVE. Like po_poll it WARNs until the
    # operator both LOADS the plist (`install.sh load org.solutionsmith.its.subcontract-poll`)
    # AND flips at least one subcontracts.subcontract_poll.* gate — a loaded daemon whose gates
    # are all false is a pre-lock no-op and writes NO marker (intentional dark state). Register +
    # activate together.
    "subcontract_poll",
    # Subcontract SEND poll daemon (subcontracts.subcontract_send_poll, SC-S4). Writes a
    # subcontract_send_poll.last_run marker each cycle once ACTIVE. Like po_send_poll it WARNs
    # until the operator both LOADS the plist (`install.sh load org.solutionsmith.its.subcontract-send`)
    # AND flips subcontracts.subcontract_send.polling_enabled true — a loaded daemon with the
    # send gate off is a pre-lock no-op and writes NO marker (intentional dark state, the
    # External Send Gate). Register + activate together at SC-S4 go-live.
    "subcontract_send_poll",
    # Vendor-estimate pull daemon (po_materials.estimate_poll, ADR-0004 Lane 1 PR-A).
    # Writes an estimate_poll.last_run marker each cycle once ACTIVE. Like po_poll /
    # subcontract_poll it WARNs until the operator both LOADS the plist (`install.sh
    # load org.solutionsmith.its.estimate-poll`) AND flips
    # po_materials.estimate_poll.polling_enabled true — a loaded daemon with the gate
    # false is a pre-lock no-op and writes NO marker (intentional dark state, the
    # lane ships dark). Register + activate together.
    "estimate_poll",
    # Materials-manifest import daemon (field_ops.manifest_poll, PR3b / ADR-0005).
    # Writes a manifest_poll.last_run marker each cycle once ACTIVE. Like estimate_poll
    # it WARNs until the operator both LOADS the plist (`install.sh load
    # org.solutionsmith.its.manifest-poll`) AND flips
    # field_ops.manifest_poll.polling_enabled true — a loaded daemon with the gate
    # false is a pre-lock no-op and writes NO marker (intentional dark state, the
    # lane ships dark). Register + activate together.
    "manifest_poll",
    # Outbound-RFQ generation daemon (po_materials.rfq_poll, ADR-0004 Lane 2 R2).
    # Writes an rfq_poll.last_run marker each cycle once ACTIVE. Like estimate_poll
    # it WARNs until the operator both LOADS the plist (`install.sh load
    # org.solutionsmith.its.rfq-poll`) AND flips
    # po_materials.rfq_poll.polling_enabled true — a loaded daemon with the gate
    # false is a pre-lock no-op and writes NO marker (intentional dark state, the
    # lane ships dark). Register + activate together.
    "rfq_poll",
    # RFQ SEND poll daemon (po_materials.rfq_send_poll, ADR-0004 R3). Writes an
    # rfq_send_poll.last_run marker each cycle once ACTIVE. Like po_send_poll it WARNs
    # until the operator both LOADS the plist (`install.sh load
    # org.solutionsmith.its.rfq-send`) AND flips po_materials.rfq_send.polling_enabled
    # true — a loaded daemon with the send gate off is a pre-lock no-op and writes NO
    # marker (intentional dark state, the External Send Gate). Register + activate
    # together at R3 go-live (a FIXED high-class operator action).
    "rfq_send_poll",
    # §50 privileged config actuator (po_materials.config_actuator). Writes a
    # config_actuator.last_run marker at the end of each cycle that actually reached the
    # pull+claim work. This is the HIGHEST-consequence entry in this list: it is the SOLE
    # privileged code-actuator (commit → CI → merge → deploy), and a silent death looks
    # exactly like "nothing is queued" — config edits simply stop reaching the live Worker
    # with no other signal. Like po_poll it WARNs until the operator both LOADS the plist
    # (`install.sh load org.solutionsmith.its.config-actuator`) AND flips
    # po_materials.config_actuator.polling_enabled true — a loaded daemon with the gate
    # false returns before the marker write (intentional dark state, not a fault).
    "config_actuator",
    # Form-publish actuator (safety_reports.publish_daemon), the same privileged
    # commit → CI → deploy rail against the publish_requests queue. Writes a
    # publish_daemon.last_run marker each completed cycle; same load-AND-flip caveat
    # (`install.sh load org.solutionsmith.its.publish-daemon` +
    # safety_reports.publish_daemon.polling_enabled true).
    "publish_daemon",
]

# Per-job freshness windows. Jobs not in this map use the default 24h
# window — appropriate for daily cadences. Weekly (Friday) jobs use 8 days
# so a missed Friday + the following Wednesday still surface as stale, but
# a 1-day-late run does not false-positive. High-frequency pollers use
# a tight window (a couple of poll intervals) so a missed cycle surfaces
# promptly without the operator having to wait for the daily watchdog.
TRACKED_JOB_WINDOWS: dict[str, timedelta] = {
    "safety_weekly_generate": timedelta(days=8),
    # weekly_send_poll runs every 15 min (default); 30 min == 2 cycles.
    # A single missed cycle is tolerated; two consecutive misses fire.
    "safety_weekly_send_poll": timedelta(minutes=30),
    # picklist drift audit runs once weekly (Sunday afternoon per
    # operator launchd schedule). 8-day window matches the weekly_generate
    # pattern — a missed Sunday + the following Friday still surfaces.
    "safety_picklist_audit": timedelta(days=8),
    # (safety_intake window removed 2026-06-05 — poller retired; see TRACKED_JOBS.)
    # run_picklist_sync runs hourly (launchd StartInterval=3600). 3 h == ~3
    # cycles: tolerates a coalesced/delayed run without false-positiving, but a
    # genuine stall surfaces at the next daily watchdog run. (C4.)
    "safety_picklist_sync": timedelta(hours=3),
    # portal_poll runs every 60s (default). 5 min == ~5 cycles: tolerates
    # coalesced/delayed runs, but a genuine stall surfaces at the next daily
    # watchdog. (Mirrors the high-frequency-poller window pattern.)
    "safety_portal_poll": timedelta(minutes=5),
    # compile_now_poll runs every 90s (default). 8 min == ~5 cycles — same
    # high-frequency-poller tolerance as portal_poll, scaled to the 90s cadence.
    "safety_compile_now_poll": timedelta(minutes=8),
    # progress_weekly_generate runs Friday 14:30 (StartCalendarInterval) — mirror the
    # safety weekly compile's 8-day window (a missed Friday + the following Wednesday
    # surfaces; a 1-day-late run does not false-positive). Check-I below auto-recovers it.
    "progress_weekly_generate": timedelta(days=8),
    # progress_send_poll runs every 15 min (default); 30 min == 2 cycles — mirror
    # safety_weekly_send_poll (a single missed cycle tolerated; two consecutive fire).
    "progress_send_poll": timedelta(minutes=30),
    # fieldops_sync runs every 90s (default). 8 min == ~5 cycles — same high-frequency-poller
    # tolerance as safety_compile_now_poll, scaled to the 90s cadence. The interval is TUNABLE
    # (default 90s, via field_ops.fieldops_sync.poll_interval_seconds / the install.sh arg): an
    # operator who raises it well above 90s should widen this window to match; 8 min fits the
    # default 90s cadence.
    "fieldops_sync": timedelta(minutes=8),
    # po_poll runs every 90s (default). 8 min == ~5 cycles — same high-frequency-poller
    # tolerance as fieldops_sync/safety_compile_now_poll, scaled to the 90s cadence.
    # Same tunable-interval caveat as fieldops_sync (po_materials.po_poll.
    # poll_interval_seconds / the install.sh arg — widen this window if raised).
    "po_poll": timedelta(minutes=8),
    # po_send_poll runs every 15 min (default); 30 min == 2 cycles — mirror
    # safety_weekly_send_poll / progress_send_poll (a single missed cycle tolerated; two
    # consecutive fire). It is an approval poller, not a fast puller.
    "po_send_poll": timedelta(minutes=30),
    # subcontract_poll runs every 120s (default). 10 min == ~5 cycles — same high-frequency-poller
    # tolerance as po_poll (8 min at 90s), scaled to the 120s cadence.
    "subcontract_poll": timedelta(minutes=10),
    # subcontract_send_poll runs every 15 min (default); 30 min == 2 cycles — mirror
    # po_send_poll / weekly_send_poll (an approval poller, not a fast puller).
    "subcontract_send_poll": timedelta(minutes=30),
    # estimate_poll runs every 120s (default). 10 min == ~5 cycles — same
    # high-frequency-poller tolerance as subcontract_poll (identical 120s cadence).
    # Same tunable-interval caveat as po_poll (po_materials.estimate_poll.
    # poll_interval_seconds / the install.sh arg — widen this window if raised).
    "estimate_poll": timedelta(minutes=10),
    # manifest_poll runs every 120s (default). 10 min == ~5 cycles — same
    # high-frequency-poller tolerance as estimate_poll (identical 120s cadence).
    # Same tunable-interval caveat as po_poll (field_ops.manifest_poll.
    # poll_interval_seconds / the install.sh arg — widen this window if raised).
    "manifest_poll": timedelta(minutes=10),
    # rfq_poll runs every 120s (default). 10 min == ~5 cycles — same
    # high-frequency-poller tolerance as estimate_poll/subcontract_poll (identical
    # 120s cadence). Same tunable-interval caveat as po_poll
    # (po_materials.rfq_poll.poll_interval_seconds / the install.sh arg — widen
    # this window if raised).
    "rfq_poll": timedelta(minutes=10),
    # rfq_send_poll runs every 15 min (default); 30 min == 2 cycles — mirror
    # po_send_poll / subcontract_send_poll / weekly_send_poll (an approval poller, not a
    # fast puller; a single missed cycle tolerated, two consecutive fire).
    "rfq_send_poll": timedelta(minutes=30),
    # config_actuator / publish_daemon both run every 120s (plist StartInterval), but they
    # do NOT get the 10-min high-frequency-poller window their cadence would suggest. They
    # are the two privileged ACTUATORS, and one cycle can legitimately block for a very long
    # time: per claimed request it waits out CI (CI_TIMEOUT_S = 900s) then deploys, serially
    # for every request it claimed — and the Check-C marker is written only once, at the END
    # of the cycle. A 10-min window would fire CRITICAL on a perfectly healthy multi-request
    # publish. 90 min sits above each daemon's own STALE_RECLAIM_S ceiling (config 3300s /
    # publish 2700s — the point past which even THEY call an in-flight request dead), so it
    # cannot false-positive on legitimate work. Nothing is lost by the wider window: Check C
    # is evaluated by the once-daily 07:00 watchdog, so a genuinely dead actuator surfaces at
    # the same sweep either way.
    "config_actuator": timedelta(minutes=90),
    "publish_daemon": timedelta(minutes=90),
}
DEFAULT_TRACKED_JOB_WINDOW = timedelta(hours=24)

# Check D scan window. 14 days = ~2 weeks of forward visibility; long
# enough that PTO planned at the start of the next sprint surfaces; short
# enough that the next watchdog run inevitably re-catches anything still
# unresolved.
REVIEWER_CHAIN_SCAN_DAYS = 14

# Workstreams whose chains Check D walks every morning. Add a slug here
# when its three-tier chain goes live in ITS_Config / DEFAULT_REVIEWER_CHAINS.
WORKSTREAMS_TO_SCAN: list[str] = ["safety_reports"]

# (Check F mailbox routing WORKSTREAM_TO_MAILBOX removed 2026-06-05 with the
# safety email-intake retirement. Email Triage owns its own mailbox-silence check
# when it lands; the shared Graph plumbing in shared/graph_client.py is preserved.)


@dataclass(frozen=True)
class CheckResult:
    severity: Severity
    summary: str
    details: str = ""


# ---- Check A: stale review queue ----------------------------------------


def _check_stale_review_queue() -> CheckResult:
    """PENDING rows past 2× SLA → WARN with capped Item ID list."""
    rows = review_queue.get_pending()
    stale = [r for r in rows if review_queue.is_past_sla(r)]

    if not stale:
        return CheckResult(
            severity=Severity.INFO,
            summary="No stale items in ITS_Review_Queue (past 2× SLA).",
        )

    item_ids = [str(r["Item ID"]) for r in stale]
    capped = item_ids[:REVIEW_QUEUE_ITEM_CAP]
    details = f"Item IDs: {', '.join(capped)}"
    if len(item_ids) > REVIEW_QUEUE_ITEM_CAP:
        details += f" (showing first {REVIEW_QUEUE_ITEM_CAP} of {len(item_ids)})"

    return CheckResult(
        severity=Severity.WARN,
        summary=f"{len(stale)} item(s) past 2× SLA in ITS_Review_Queue.",
        details=details,
    )


# ---- Check B: open CRITICAL events --------------------------------------


def _open_critical_rows() -> list[dict[str, Any]]:
    """The OPEN-CRITICAL working set in ITS_Errors: Severity CRITICAL with a blank
    `Resolved At`. THE single reader for the "am I on fire?" query — shared by Check B
    (`_check_open_criticals`, below) and Check W's incident-safety gate
    (`_check_log_dir_rotation`), so the query lives in ONE place (HOUSE_REFLEXES §1,
    multi-surface fan-out) and the two can never disagree about what "open" means.

    The `Severity` filter is passed to `get_rows` so the filtering happens inside the SDK
    layer (currently client-side post-fetch per smartsheet_client.get_rows — at sandbox
    volume that's fine). The `Resolved At` blank-check is local because a missing-key row
    dict and a row with `Resolved At=None` both mean "open" per the schema's "presence
    implies resolved" design.
    """
    rows = smartsheet_client.get_rows(
        sheet_ids.SHEET_ERRORS,
        filters={"Severity": "CRITICAL"},
    )
    return [r for r in rows if not r.get("Resolved At")]


def _check_open_criticals() -> CheckResult:
    """CRITICAL rows in ITS_Errors with Resolved At blank → WARN."""
    open_rows = _open_critical_rows()

    if not open_rows:
        return CheckResult(
            severity=Severity.INFO,
            summary="No open CRITICAL events in ITS_Errors.",
        )

    codes: list[str] = []
    for r in open_rows:
        code = r.get("Error")
        if code in (None, ""):
            codes.append("<no-code>")
        else:
            codes.append(str(code))
    capped = codes[:CRITICAL_ITEMS_CAP]
    details = f"Error codes: {', '.join(capped)}"
    if len(codes) > CRITICAL_ITEMS_CAP:
        details += f" (showing first {CRITICAL_ITEMS_CAP} of {len(codes)})"

    return CheckResult(
        severity=Severity.WARN,
        summary=f"{len(open_rows)} open CRITICAL event(s) in ITS_Errors.",
        details=details,
    )


# ---- Check C: scheduled-jobs last-run via marker file -------------------


def write_last_run_marker(job_name: str) -> None:
    """Write a `.last_run` marker for `job_name` with current UTC timestamp.

    Every scheduled script calls this on successful completion. Pattern:
    `<job_name>.last_run` file in `~/its/.watchdog/`, contents = ISO 8601
    UTC. Directory created on demand. Fail-soft: marker write failures log
    WARN but do not raise — a failed marker is operationally less severe
    than a failed job, and the job itself has already succeeded by the
    time this helper is called.
    """
    try:
        WATCHDOG_MARKER_DIR.mkdir(parents=True, exist_ok=True)
        marker = WATCHDOG_MARKER_DIR / f"{job_name}.last_run"
        marker.write_text(datetime.now(UTC).isoformat())
    except OSError as e:
        log(
            Severity.WARN,
            f"{_SCRIPT}.write_last_run_marker",
            f"failed to write marker for {job_name!r}: {e!r}",
        )


def _check_scheduled_jobs() -> CheckResult:
    """Check C: verify each TRACKED_JOBS entry has fired within its expected window.

    Each tracked job has either a per-job window in TRACKED_JOB_WINDOWS or
    falls back to DEFAULT_TRACKED_JOB_WINDOW (24h). Adding a new daily job
    is one line: append the slug to TRACKED_JOBS and ensure the job calls
    `write_last_run_marker` on success. Adding a weekly/monthly job is two
    lines: also add a per-job timedelta to TRACKED_JOB_WINDOWS.

    Returns INFO with a noop summary when TRACKED_JOBS is empty. When jobs
    are tracked, returns WARN if any marker is missing or older than that
    job's window, otherwise INFO.
    """
    if not TRACKED_JOBS:
        return CheckResult(
            severity=Severity.INFO,
            summary="No scheduled jobs tracked (TRACKED_JOBS is empty).",
        )

    now = datetime.now(UTC)
    stale: list[str] = []
    for job in TRACKED_JOBS:
        window = TRACKED_JOB_WINDOWS.get(job, DEFAULT_TRACKED_JOB_WINDOW)
        marker = WATCHDOG_MARKER_DIR / f"{job}.last_run"
        if not marker.exists():
            stale.append(f"{job} (no marker)")
            continue
        try:
            last_run = datetime.fromisoformat(marker.read_text().strip())
        except (OSError, ValueError) as e:
            stale.append(f"{job} (unreadable marker: {e!r})")
            continue
        if last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=UTC)
        if (now - last_run) > window:
            stale.append(f"{job} (last_run={last_run.isoformat()})")

    if not stale:
        return CheckResult(
            severity=Severity.INFO,
            summary=f"All {len(TRACKED_JOBS)} tracked scheduled job(s) fresh.",
        )
    return CheckResult(
        severity=Severity.WARN,
        summary=f"{len(stale)} of {len(TRACKED_JOBS)} tracked scheduled job(s) stale.",
        details="; ".join(stale),
    )


# ---- Check D: 14-day reviewer-chain forward scan ------------------------


def _check_reviewer_chain_forward() -> CheckResult:
    """Check D: 14-day forward scan for reviewer-chain gaps (Op Stds v9 §18).

    For each workstream in `WORKSTREAMS_TO_SCAN`, walk the next
    `REVIEWER_CHAIN_SCAN_DAYS` days. Federal holidays are skipped (the
    business doesn't need reviewer coverage on a closed day). For each
    business day, resolve the chain (PTO-aware via the live
    `TimeOffClient`); if the chain is empty, that day is a gap.

    Gaps are logged to `ITS_Review_Queue` as an INFO row per workstream
    (one row collecting all that workstream's gaps; not one row per gap).
    `reason=OTHER` and `sla_tier=SUBCONTRACT_DRAFT` chosen so Check A's
    "past 2× SLA" stale detector gives the operator a 4-day triage window
    on anomaly rows before re-WARNing.

    Known behavior: Check D does NOT deduplicate across runs. A persistent
    gap creates one new row per watchdog run. Acceptable for Session 2;
    future enhancement is to scan for an existing matching anomaly row
    before adding if the proliferation becomes painful.
    """
    time_off = TimeOffClient()  # per-instance cache: one fetch for all workstreams
    today = date.today()
    rows_written = 0

    for workstream in WORKSTREAMS_TO_SCAN:
        gaps: list[date] = []
        for offset in range(REVIEWER_CHAIN_SCAN_DAYS):
            scan_date = today + timedelta(days=offset)
            if is_federal_holiday(scan_date):
                continue
            chain = resolve_chain(workstream, scan_date, time_off=time_off)
            if chain.is_empty:
                gaps.append(scan_date)

        if gaps:
            _log_anomaly_to_review_queue(workstream, gaps)
            rows_written += 1

    if rows_written == 0:
        return CheckResult(
            severity=Severity.INFO,
            summary=(
                f"No reviewer-chain gaps in next {REVIEWER_CHAIN_SCAN_DAYS} day(s) "
                f"across {len(WORKSTREAMS_TO_SCAN)} workstream(s)."
            ),
        )
    return CheckResult(
        severity=Severity.INFO,
        summary=(
            f"Logged {rows_written} reviewer-chain anomaly row(s) to "
            f"ITS_Review_Queue."
        ),
    )


def _log_anomaly_to_review_queue(workstream: str, gaps: list[date]) -> None:
    """Write one INFO ANOMALY row to ITS_Review_Queue for a workstream's gaps.

    Schema mapping (resolved 2026-05-20 per operator pre-flight decision):
      - `workstream='global'` (the ITS_Review_Queue VALID_WORKSTREAMS set
        does not include `'watchdog'`; `'global'` is the closest fit and
        already used by the kill-switch.)
      - `reason=ReviewReason.OTHER` (no `'anomaly'` value exists in the
        live picklist).
      - `sla_tier=SlaTier.SUBCONTRACT_DRAFT` (4-day stale window so
        Check A's WARN doesn't auto-fire on these anomaly rows within
        the operator's normal triage window).
      - `severity=Severity.INFO`.

    Payload carries the actual workstream and gap dates so the operator
    can act on them; Item ID is auto-generated by `review_queue.add`.
    """
    summary = (
        f"reviewer-chain gap detected ({len(gaps)} day(s)) for "
        f"workstream={workstream!r}"
    )
    payload = {
        "type": "reviewer_chain_gap",
        "workstream": workstream,
        "gap_dates": [d.isoformat() for d in gaps],
    }
    review_queue.add(
        workstream="global",
        summary=summary,
        payload=payload,
        sla_tier=SlaTier.SUBCONTRACT_DRAFT,
        reason=ReviewReason.OTHER,
        severity=Severity.INFO,
        source_file=__file__,
        security_flag=False,
    )


# ---- Check F: RETIRED 2026-06-05 (safety mail-intake silent-disable) ----
# Removed with the safety email-intake retirement — the Safety Portal PULL model
# supersedes the safety@ mailbox (see decision_phase5-portal-transport), so there is
# no safety mailbox to monitor for silence. The shared Graph plumbing this check used
# (shared/graph_client.fetch_latest_inbound_timestamp) is PRESERVED untouched for the
# future Email Triage workstream, which will own its own mailbox-silence check.


# ---- Check G: alert-dedupe summary sweep --------------------------------

_SUMMARY_SUBJECT_PREFIX = "[ITS CRITICAL SUMMARY]"
_SENTRY_KEY_PREFIX = "sentry::"
_SENTRY_LEG_TAG = "[sentry-leg]"


def _compose_summary(entry: alert_dedupe.ExpiredEntry, run_ts: str) -> tuple[str, str]:
    """Build (subject, body) for one expired-window summary email.

    Subject:  [ITS CRITICAL SUMMARY] {script}: N suppressed occurrences
              (suffixed ` [sentry-leg]` for a Sentry-leg dedupe entry)
    Body:     Fields naming the window + filter criteria for ITS_Errors.

    The body references filter criteria rather than enumerating
    correlation IDs inline because the state file aggregates only
    (suppressed_count, timestamps) — individual correlation IDs live in
    ITS_Errors. Operator pulls detail from the sheet with the filter.

    `entry.key` is `f"{script}::{error_code}"` for the Resend leg, or the
    namespaced `f"sentry::{script}::{error_code}"` for the Sentry leg
    (Sentry reclassified record→deduped-push, operator-ratified
    2026-07-03). A `sentry::` prefix is stripped before display and the
    subject is tagged `[sentry-leg]` so the operator can tell which push
    leg was suppressed. We then split once on `::` to recover the two
    parts for display. A key without `::` falls back to using the whole
    string as `script` and an empty error_code (the `record_fire` callers
    always build keys with `::`, so this fallback is defensive against
    hand-edited state files).
    """
    key = entry.key
    is_sentry_leg = key.startswith(_SENTRY_KEY_PREFIX)
    if is_sentry_leg:
        key = key[len(_SENTRY_KEY_PREFIX):]
    script, sep, error_code = key.partition("::")
    if not sep:
        error_code = ""
    leg_tag = f" {_SENTRY_LEG_TAG}" if is_sentry_leg else ""
    subject = (
        f"{_SUMMARY_SUBJECT_PREFIX} {script}: "
        f"{entry.suppressed_count} suppressed occurrences{leg_tag}"
    )
    body = "\n".join(
        [
            f"Script:           {script}",
            f"Error code:       {error_code}",
            f"Window opened:    {entry.first_fired_at}",
            f"Window closed:    {entry.window_ends_at}",
            f"First fire:       {entry.first_fired_at}",
            f"Last fire:        {entry.last_fired_at}",
            f"Suppressed count: {entry.suppressed_count}",
            "",
            f"See ITS_Errors (sheet {sheet_ids.SHEET_ERRORS}) for full row detail.",
            "",
            "Filter ITS_Errors by:",
            f"  Script = {script}",
            f"  Surfaced At BETWEEN {entry.first_fired_at} AND {entry.last_fired_at}",
            "",
            f"Sent by watchdog summary sweep, {run_ts}.",
        ]
    )
    return subject, body


def _check_alert_dedupe_summaries(*, alerts_suppressed: bool = False) -> CheckResult:
    """Check G: sweep alert-dedupe state for expired windows.

    For each expired entry:
      - **Phase 1** — If `suppressed_count >= 1` AND `summarized == False`:
        fire a single Resend summary email, then `mark_summarized(key)`.
        The entry stays one more sweep before deletion (phase 2 below).
        Crash safety: a crash between send and mark causes the next
        sweep to re-fire (duplicate email is acceptable).
      - **Phase 2** — Otherwise (`summarized == True` OR
        `suppressed_count == 0`): `delete_entry(key)`. Either the entry
        was summarized in a prior sweep, or the window closed with no
        suppressions (a clean expiry needing no signal).

    **MAINTENANCE behavior (`alerts_suppressed=True`):** Phase 1 is
    DEFERRED — summary emails are not fired and the entry's `summarized`
    flag stays False, so the entry persists in expired+unsummarized
    state across the MAINTENANCE window. The first post-MAINTENANCE
    sweep fires the deferred digest normally. Phase 2 (delete of
    already-summarized or clean-expired entries) PROCEEDS during
    MAINTENANCE — that path doesn't fire push, so suppressing it would
    create unbounded state growth without any operator-visibility
    benefit. Bounded delay = MAINTENANCE window + one watchdog cadence;
    no information loss (the underlying CRITICAL events already wrote
    to ITS_Errors at occurrence time per Op Stds v9 §27). Op Stds v10
    §2 codifies this carve-out.

    Per Op Stds v9 §27 push-vs-record separation, the summary email is
    a Resend-only operational signal — it does NOT write to ITS_Errors
    (the rows already exist from PR α) and does NOT fire Sentry (this
    is not an exception event).

    Returns INFO `CheckResult` with sweep stats. The check itself never
    fails the watchdog run — `_run_check` wraps it for harness-level
    isolation, and the per-entry Resend / mark / delete calls each have
    their own try/except so one bad entry doesn't poison the sweep.
    """
    run_ts = datetime.now(UTC).isoformat()
    entries = alert_dedupe.list_expired_summaries()

    if not entries:
        return CheckResult(
            severity=Severity.INFO,
            summary="No expired alert-dedupe windows to sweep.",
        )

    summaries_fired = 0
    entries_deleted = 0
    summaries_deferred = 0
    fired_keys: list[str] = []
    deferred_keys: list[str] = []

    for entry in entries:
        is_phase_1 = entry.suppressed_count >= 1 and not entry.summarized
        if is_phase_1 and alerts_suppressed:
            # MAINTENANCE defer: skip send AND mark. Entry stays
            # summarized=False with expired window; first sweep after
            # MAINTENANCE clears fires the deferred digest normally.
            deferred_keys.append(entry.key)
            summaries_deferred += 1
            continue
        if is_phase_1:
            subject, body = _compose_summary(entry, run_ts)
            try:
                resend_client.send_alert(subject, body)
            except Exception as e:
                # Resend failure leaves entry unmarked → next sweep retries.
                # No marker rewrite here — resend_client / error_log paths
                # have their own logging; the watchdog status line below
                # surfaces the aggregate count.
                log(
                    Severity.WARN,
                    f"{_SCRIPT}._check_alert_dedupe_summaries",
                    f"[summary-send-failed] key={entry.key!r} {e!r}",
                )
                continue
            alert_dedupe.mark_summarized(entry.key)
            summaries_fired += 1
            fired_keys.append(entry.key)
        else:
            # Phase 2: proceeds during MAINTENANCE — no push side-effect.
            alert_dedupe.delete_entry(entry.key)
            entries_deleted += 1

    parts = [
        f"Examined {len(entries)} expired entr{'y' if len(entries) == 1 else 'ies'}",
        f"fired {summaries_fired} summary email(s)",
        f"deleted {entries_deleted} entr{'y' if entries_deleted == 1 else 'ies'}",
    ]
    if summaries_deferred:
        parts.append(
            f"deferred {summaries_deferred} summar"
            f"{'y' if summaries_deferred == 1 else 'ies'} during MAINTENANCE"
        )
    summary = "; ".join(parts) + "."
    detail_parts = []
    if fired_keys:
        detail_parts.append(f"Summaries fired for: {', '.join(fired_keys)}")
    if deferred_keys:
        detail_parts.append(
            f"Summaries deferred (MAINTENANCE) for: {', '.join(deferred_keys)}"
        )
    details = " | ".join(detail_parts)
    return CheckResult(severity=Severity.INFO, summary=summary, details=details)


# ---- Check I: weekly_generate catch-up recovery -------------------------
#
# Motivating finding (Tier-1 self-heal completion, 2026-06-01). Every other
# tracked daemon is interval-driven (launchd StartInterval), so a crashed
# cycle is simply re-run at the next interval — launchd re-invocation IS the
# recovery, and no KeepAlive is needed. weekly_generate is the lone
# exception: it runs Friday 14:00 via StartCalendarInterval, so a *crashed*
# Friday cycle is not re-invoked until the next Friday (launchd treats a
# started-then-failed job as "ran"; it only re-runs a calendar job that the
# host MISSED while asleep/off, not one that ran and errored). Check C
# detects the resulting marker staleness (8-day window) and the external
# UptimeRobot ping (audit F16) covers total-host death — but neither
# *recovers* the missed run; it stays missing until next Friday or a human
# acts. Check I is that recovery: a daily, self-correcting re-fire that
# brings weekly_generate up to the interval pollers' self-heal bar.
#
# This is the one open leg of the V&R Pre-Cutover Condition 4 (Tier-1
# self-heal) gate; the all-daemon Check C coverage and the F16 ping legs are
# already met. See the 2026-06-01 blueprint doctrine correction.

# The job whose Friday calendar-run Check I recovers — the same marker slug
# Check C tracks (TRACKED_JOBS[0]), but read here against the most-recent
# Friday trigger instant rather than Check C's broad 8-day staleness window.
WEEKLY_GENERATE_JOB_SLUG = "safety_weekly_generate"

# weekly_generate's launchd trigger: Friday (Python date.weekday() == 4) at
# 14:00 local time. Mirrors scripts/launchd/org.solutionsmith.its.weekly-
# generate.plist (StartCalendarInterval Weekday=5 / Hour=14); keep in sync
# if that plist's schedule ever changes.
WEEKLY_GENERATE_TRIGGER_WEEKDAY = 4
WEEKLY_GENERATE_TRIGGER_HOUR = 14

# Catch-up window measured from the Friday 14:00 trigger: through the end of
# the following Monday (the "following business day" bound). That spans the
# Saturday / Sunday / Monday daily-or-on-wake watchdog runs after a missed
# Friday, without re-firing an ancient week — a miss not recovered by Monday
# falls through to Check C's 8-day WARN → human (Tier 2/3). Re-firing is
# idempotent (weekly_generate replace-if-unapproved / refuse-if-approved),
# so the bound is about not wasting Anthropic spend on stale weeks, not
# about safety.
CATCHUP_WINDOW = timedelta(days=3)


def _local_now() -> datetime:
    """Local timezone-aware 'now'. Seam so tests can pin the clock.

    weekly_generate's launchd trigger fires in LOCAL time, so the Friday-
    trigger math runs in local time. The marker (written by weekly_generate
    as UTC) is compared as an aware datetime, which Python resolves correctly
    across zones.
    """
    return datetime.now().astimezone()


def _most_recent_friday_trigger(now: datetime) -> datetime:
    """Most recent Friday 14:00 local at or before `now` (aware, local tz)."""
    days_since_friday = (now.weekday() - WEEKLY_GENERATE_TRIGGER_WEEKDAY) % 7
    candidate = (now - timedelta(days=days_since_friday)).replace(
        hour=WEEKLY_GENERATE_TRIGGER_HOUR, minute=0, second=0, microsecond=0
    )
    if candidate > now:
        # `now` is earlier in the day than the trigger hour on a Friday →
        # this week's trigger hasn't happened; the most recent is last week.
        candidate -= timedelta(days=7)
    return candidate


def _read_marker_datetime(job_slug: str) -> datetime | None:
    """Read a `{slug}.last_run` marker as an aware datetime, or None.

    None on missing / unreadable / unparseable marker — each such case is
    treated as "did not run" by the caller (catch-up errs toward firing, not
    toward a silent miss). Mirrors Check C's contents-based read; a naive
    timestamp is assumed UTC.
    """
    marker = WATCHDOG_MARKER_DIR / f"{job_slug}.last_run"
    if not marker.exists():
        return None
    try:
        parsed = datetime.fromisoformat(marker.read_text().strip())
    except (OSError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _review_rows_exist_for_week(review_sheet_id: int, week_start: date) -> bool:
    """True iff the review sheet (WSR for safety / WPR for progress) has >=1 row for the week.

    The second "did it complete" signal alongside the marker's "did it run": the compile's
    marker write is fail-soft, so a successful run can leave a stale/missing marker. Row
    presence catches that and prevents a wasteful re-fire. `week_start` is the catch-up's
    Monday; the `Week Of` column keys on the Saturday that opens the Sat→Fri week, so we
    convert via safety_week. Fail-soft: a read error logs WARN and returns False, so the
    decision falls back to the marker signal — and a Smartsheet outage that hides the rows here
    resurfaces when the (also-Smartsheet) catch-up compile runs and fails loudly.
    """
    saturday = safety_week.week_bounds(week_start).start
    try:
        rows = smartsheet_client.get_rows(
            review_sheet_id,
            filters={"Week Of": saturday.isoformat()},
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft to marker-only decision
        log(
            Severity.WARN,
            f"{_SCRIPT}._review_rows_exist_for_week",
            f"review sheet {review_sheet_id} read failed for week {saturday}: {exc!r}",
        )
        return False
    return bool(rows)


@dataclass(frozen=True)
class _CatchupTarget:
    """One Friday-calendar weekly compile the Check-I catch-up can recover (P5 generalized).

    `slug` = the Check-C marker slug (in TRACKED_JOBS); `review_sheet_id` = the WSR/WPR sheet
    whose row presence is the "produced output" signal; `refire` = the direct, decorator-free
    re-fire entry (runs during MAINTENANCE — generation is internal, no external send);
    `label` = the operator-facing job name + the `{label}_catchup_failed` error_code stem.
    Both compiles share the Friday-trigger math (`_most_recent_friday_trigger`); progress's
    actual 14:30 run produces a marker ≥ the 14:00 trigger instant, so the shared lower-bound
    trigger is correct for both.
    """

    slug: str
    review_sheet_id: int
    refire: Callable[[date], dict[str, Any]]
    label: str


_SAFETY_CATCHUP_TARGET = _CatchupTarget(
    slug=WEEKLY_GENERATE_JOB_SLUG,  # "safety_weekly_generate"
    review_sheet_id=sheet_ids.SHEET_WSR_HUMAN_REVIEW,
    refire=lambda wk: weekly_generate._run_pipeline(week_start_override=wk),
    label="weekly_generate",
)
_PROGRESS_CATCHUP_TARGET = _CatchupTarget(
    slug="progress_weekly_generate",
    review_sheet_id=sheet_ids.SHEET_WPR_HUMAN_REVIEW,
    # The progress compile has no `_run_pipeline` shim; its main() delegates to
    # generate_core.run_generate(PROGRESS_GENERATE_CONFIG). Call that directly (decorator-free,
    # MAINTENANCE-runnable) with the same RunSummary dict shape the safety path returns.
    refire=lambda wk: generate_core.run_generate(
        progress_weekly_generate.PROGRESS_GENERATE_CONFIG, week_start_override=wk
    ),
    label="progress_weekly_generate",
)


def _check_generate_catchup(
    target: _CatchupTarget, *, alerts_suppressed: bool
) -> CheckResult:
    """Check I (generalized): re-fire a missed Friday weekly-compile run (Tier-1 self-heal).

    Catch-up fires iff ALL THREE hold for the current target week:
      (a) we are within CATCHUP_WINDOW of the most recent Friday trigger (don't recover an
          ancient week — Check C owns those);
      (b) the Check C marker is missing or older than that trigger ("did not run"); AND
      (c) the target's review sheet has no row for that week ("produced nothing").
    A fresh marker OR existing review rows means the run happened, so we do not re-fire.
    Combining the two "ran" signals with OR (fire only when BOTH are negative) is deliberately
    conservative: re-firing is safe but burns compute, and a fail-soft marker write must not
    look like a miss. The "ran but every project errored" case is out of scope (those runs DID
    complete — rows + placeholders exist); Check I closes only the "calendar run never executed"
    Tier-1 gap.

    On fire, calls `target.refire` directly — NOT the `@require_active`-decorated `main()`. The
    watchdog's own `main()` has already honored the kill switch, and the direct entry is what
    lets a catch-up run during MAINTENANCE (generation is internal — no external send — so the
    Tier-1 brief requires it; the decorated `main()` would be blocked by `@require_active`).
    Capability gate is unaffected: the watchdog is neither a generation nor a send script in
    tests/test_capability_gating.py (scripts/ is not walked), and Check I drives generation only.

    MAINTENANCE (`alerts_suppressed=True`): generation still RUNS, but a catch-up FAILURE's
    operator page is DEFERRED — only the ITS_Errors record row is written (push-vs-record, Op
    Stds §3.1; same carve-out as Check G). The deferred CRITICAL resurfaces via Check B.
    `alerts_suppressed` is wired in by `_run_check`'s signature inspection.

    At most one catch-up per target per watchdog run (no loop): a successful run refreshes the
    marker so the next run takes the "marker fresh" early-return; a persistent failure re-attempts
    on the next daily run while still inside the window, then falls to Check C / a human.
    """
    now = _local_now()
    last_trigger = _most_recent_friday_trigger(now)
    target_week = (last_trigger - timedelta(days=4)).date()  # Friday → Monday

    deadline = datetime.combine(
        last_trigger.date() + CATCHUP_WINDOW, time.max, tzinfo=last_trigger.tzinfo
    )
    if now > deadline:
        return CheckResult(
            severity=Severity.INFO,
            summary=(
                f"{target.label} catch-up: week {target_week} is past the "
                f"catch-up window (Check C covers older misses)."
            ),
        )

    marker_dt = _read_marker_datetime(target.slug)
    if marker_dt is not None and marker_dt >= last_trigger:
        return CheckResult(
            severity=Severity.INFO,
            summary=f"{target.label} ran for week {target_week} (marker fresh).",
        )

    if _review_rows_exist_for_week(target.review_sheet_id, target_week):
        return CheckResult(
            severity=Severity.INFO,
            summary=(
                f"{target.label} produced review rows for week {target_week} "
                f"(marker stale but rows present); no catch-up."
            ),
        )

    return _fire_generate_catchup(target, target_week, alerts_suppressed=alerts_suppressed)


def _check_weekly_generate_catchup(*, alerts_suppressed: bool = False) -> CheckResult:
    """Check I (safety): re-fire a missed safety weekly_generate Friday run. Thin wrapper over
    the generalized catch-up bound to the SAFETY target (byte-identical to the pre-P5 behavior)."""
    return _check_generate_catchup(_SAFETY_CATCHUP_TARGET, alerts_suppressed=alerts_suppressed)


def _check_progress_generate_catchup(*, alerts_suppressed: bool = False) -> CheckResult:
    """Check I (progress, P5): re-fire a missed progress_weekly_generate Friday 14:30 run. Thin
    wrapper over the generalized catch-up bound to the PROGRESS target — the Tier-1 self-heal
    that recovers the one progress daemon launchd can't (calendar-scheduled, like the safety
    compile). Re-fires generate_core.run_generate(PROGRESS_GENERATE_CONFIG); send-free + AI-free."""
    return _check_generate_catchup(_PROGRESS_CATCHUP_TARGET, alerts_suppressed=alerts_suppressed)


def _fire_generate_catchup(
    target: _CatchupTarget, target_week: date, *, alerts_suppressed: bool
) -> CheckResult:
    """Re-run weekly_generate for a missed week; escalate on failure.

    Returns INFO on success, WARN on an empty-reviewer-chain abort
    (weekly_generate has already recorded its own CRITICAL row — no
    double-escalation), and INFO on a generation failure (the CRITICAL row +
    operator page are fired explicitly inside `_escalate_catchup_failure`
    with a threaded correlation_id, so the returned result is informational
    only and avoids a duplicate, correlation-id-less row).
    """
    log(
        Severity.INFO,
        _SCRIPT,
        f"[catch-up] {target.label} did not run for week {target_week}; "
        f"re-firing generation",
    )
    try:
        result = target.refire(target_week)
    except Exception as exc:  # noqa: BLE001 — convert to a MAINTENANCE-aware CRITICAL
        tb = traceback.format_exc()
        return _escalate_catchup_failure(
            target_week, exc, tb, label=target.label, alerts_suppressed=alerts_suppressed
        )

    # `result` is generate_core.run_generate's dict: RunSummary.__dict__ + week bounds +
    # correlation_id. Read the REAL counters (packets_compiled / wsr_written / errors_per_job) —
    # the pre-P5 code read drafts_written/drafts_failed/aborted_empty_chain, which run_generate
    # NEVER produces, so every success summary mis-reported "0 written" and the empty-chain WARN
    # branch was dead code (it only fired in tests via a hand-rolled key). Fixed here while
    # generalizing the catch-up to progress (it doubled that surface).
    packets = result.get("packets_compiled", 0)
    rows_written = result.get("wsr_written", 0)
    job_errors = len(result.get("errors_per_job", {}) or {})
    return CheckResult(
        severity=Severity.INFO,
        summary=(
            f"{target.label} catch-up fired for week {target_week}: "
            f"{packets} packet(s) compiled, {rows_written} review row(s) written, "
            f"{job_errors} job error(s)."
        ),
        details=f"correlation_id={result.get('correlation_id', '?')}",
    )


def _escalate_catchup_failure(
    target_week: date,
    exc: Exception,
    tb: str,
    *,
    label: str,
    alerts_suppressed: bool,
) -> CheckResult:
    """Programmatic CRITICAL triple-fire for a failed catch-up generation.

    Mirrors the canonical pattern in `shared.picklist_sync.sync_all`
    (sync_all does not raise on partial failure, so its triple-fire is
    explicit): write the ITS_Errors record row via `log(CRITICAL, ...)` and
    fire the operator-page legs via `error_log._alert_critical`, threading a
    single `correlation_id` across both so one grep recovers the full
    Smartsheet / Resend / Sentry picture.

    push-vs-record (Op Stds §3.1): the record row is ALWAYS written — even in
    MAINTENANCE, a real failure must leave a forensic trail. Only the
    Resend/Sentry PAGE is deferred under `alerts_suppressed`; the deferred
    CRITICAL resurfaces post-MAINTENANCE via Check B (open CRITICALs).
    """
    correlation_id = str(uuid.uuid4())
    error_code = f"{label}_catchup_failed"
    message = f"{label} catch-up FAILED for week {target_week}: {exc!r}"
    # A3: alert=False — the watchdog manages its own operator page below
    # (deferred under MAINTENANCE via alerts_suppressed), so the record log
    # must NOT auto-fire the alert legs or it would page during MAINTENANCE
    # and double-fire the Sentry leg.
    log(
        Severity.CRITICAL,
        _SCRIPT,
        message,
        error_code=error_code,
        exc_info=tb,
        correlation_id=correlation_id,
        alert=False,
    )
    if alerts_suppressed:
        log(
            Severity.INFO,
            _SCRIPT,
            f"[catch-up] CRITICAL page deferred during MAINTENANCE for week "
            f"{target_week} (corr={correlation_id[:8]}); record row written.",
        )
    else:
        _alert_critical(
            _SCRIPT,
            message,
            tb,
            correlation_id=correlation_id,
            error_code=error_code,
        )
    return CheckResult(
        severity=Severity.INFO,
        summary=(
            f"{label} catch-up FAILED for week {target_week} — CRITICAL "
            + (
                "recorded (page deferred, MAINTENANCE)"
                if alerts_suppressed
                else "triple-fired"
            )
            + f" (corr={correlation_id[:8]})."
        ),
    )


# ---- Check J: circuit-breaker prolonged-open alert ----------------------


def _check_circuit_breaker_prolonged_open(
    *, alerts_suppressed: bool = False
) -> CheckResult:
    """Page the operator when the Smartsheet circuit breaker has been OPEN (one
    outage episode) longer than ``circuit_breaker.prolonged_open_alert_seconds``.

    Reads ``circuit_breaker.seconds_open()`` — a lock-free LOCAL-file read, so it
    works during the very Smartsheet outage this fires for. The threshold read
    DOES short-circuit during that outage, so it MUST fail open to the default
    (mirrors the heartbeat_url read in ``main()``).

    The page fires INLINE via ``_alert_critical`` — NOT a returned CRITICAL:
    ``_run_check`` routes results through ``log()``, which writes records but
    does NOT fire the Resend/Sentry legs, so a returned-CRITICAL would be a
    silent missed wake-up (see the ``log(CRITICAL)``-doesn't-page tech-debt).
    A STABLE ``error_code`` lets the per-key dedupe throttle the page to ~1/hour
    even though this runs every cycle while OPEN.

    The ``_alert_critical`` call is wrapped in ``circuit_breaker.bypass()``: its
    Resend leg reads ``system.operator_email`` from ITS_Config via the GUARDED
    ``get_setting``, which would itself short-circuit while the breaker is OPEN
    — i.e. exactly when this check fires — so without the bypass the page could
    never send (confirmed by the PR-1 smoke's ``[resend-alert-failed]
    SmartsheetCircuitOpenError`` lines). The ITS_Errors record write is
    independently bypassed (§3.1 fold-in); Resend/Sentry are HTTP, so the page
    goes out whenever Smartsheet is reachable (incl. cooldown-after-recovery).
    """
    dur = circuit_breaker.seconds_open()
    if dur is None:
        return CheckResult(Severity.INFO, "circuit breaker not open")

    try:
        raw = smartsheet_client.get_setting(
            "circuit_breaker.prolonged_open_alert_seconds", workstream="global"
        )
        threshold = (
            int(raw)
            if raw is not None
            else defaults.CIRCUIT_BREAKER_PROLONGED_OPEN_ALERT_SECONDS
        )
    except (smartsheet_client.SmartsheetError, ValueError, TypeError):
        # Fail open to the default — the ITS_Config read short-circuits during
        # the very outage this check exists for.
        threshold = defaults.CIRCUIT_BREAKER_PROLONGED_OPEN_ALERT_SECONDS

    if dur <= threshold:
        return CheckResult(
            Severity.WARN,
            f"circuit breaker OPEN for {dur:.0f}s (< {threshold}s threshold)",
        )

    correlation_id = str(uuid.uuid4())
    message = (
        f"Smartsheet circuit breaker OPEN for {dur:.0f}s (> {threshold}s) — "
        "backend degraded and not self-recovering."
    )
    # A3: alert=False — same rationale as the catch-up escalation. The page
    # below is wrapped in circuit_breaker.bypass() (the breaker is OPEN, so the
    # Resend leg's operator_email read must bypass it); log()'s auto-fire could
    # not provide that wrapper, so paging stays explicit here.
    log(
        Severity.CRITICAL,
        _SCRIPT,
        message,
        error_code="circuit_breaker_prolonged_open",
        correlation_id=correlation_id,
        alert=False,
    )
    if alerts_suppressed:
        log(
            Severity.INFO,
            _SCRIPT,
            f"[prolonged-open] CRITICAL page deferred during MAINTENANCE "
            f"(corr={correlation_id[:8]}); record row written.",
        )
    else:
        # bypass() so the Resend leg's operator_email read isn't short-circuited
        # by the very-OPEN breaker (see docstring).
        with circuit_breaker.bypass():
            _alert_critical(
                _SCRIPT,
                message,
                "",
                correlation_id=correlation_id,
                error_code="circuit_breaker_prolonged_open",
            )
    return CheckResult(
        Severity.INFO,
        f"circuit breaker OPEN for {dur:.0f}s (> {threshold}s) — CRITICAL "
        + (
            "recorded (page deferred, MAINTENANCE)"
            if alerts_suppressed
            else "triple-fired"
        )
        + f" (corr={correlation_id[:8]}).",
    )


# ---- Check K: guaranteed F09 cap-window-summary sweep -------------------


def _check_alert_rate_cap_window(
    *, alerts_suppressed: bool = False
) -> CheckResult:
    """Guarantee the F09 alerts-per-hour cap WINDOW SUMMARY fires even when no
    new alert arrives to trigger the opportunistic path in
    ``error_log._maybe_fire_window_summary``.

    Calls ``_maybe_fire_window_summary`` once per cycle → it pops the due window
    via ``alert_dedupe.pop_due_window_summary()`` (atomically marks
    ``summarized=True``) and sends the one exempt summary. The ``summarized``
    flag is the double-fire guard SHARED with the opportunistic path — calling
    from both is safe (first wins; the other gets None).

    DISTINCT from Check G (``_check_alert_dedupe_summaries``), which sweeps the
    PER-KEY dedupe summaries and explicitly skips the reserved
    ``_alerts_per_hour_window`` key. Per-key and per-hour are separate subsystems.

    MAINTENANCE-defer: when ``alerts_suppressed`` do NOT fire (matching Check G);
    the window record persists for the next sweep. The OPPORTUNISTIC path does
    not itself respect MAINTENANCE (error_log-level, pre-existing, out of scope);
    this sweep just keeps its own behavior consistent.
    """
    if alerts_suppressed:
        return CheckResult(
            Severity.INFO, "alert-rate cap-window summary sweep deferred (MAINTENANCE)"
        )
    correlation_id = uuid.uuid4().hex
    _maybe_fire_window_summary(correlation_id)
    return CheckResult(
        Severity.INFO,
        f"alert-rate cap-window summary sweep ran (corr={correlation_id[:8]}).",
    )


# ---- Failure-isolation harness ------------------------------------------


def _run_check(
    check_fn: Callable[..., CheckResult],
    *,
    alerts_suppressed: bool,
) -> dict[str, str]:
    """Run one check with own try/except + marker line on failure.

    Per Op Stds v9 §27. `alerts_suppressed=True` (MAINTENANCE) downgrades
    WARN/CRITICAL results to INFO before routing so the Smartsheet row
    still lands but Resend/Sentry legs (which trigger only on CRITICAL via
    `error_log._alert_critical`) never fire. ERROR (from harness catches)
    is NOT downgraded — a broken check must remain operator-visible
    regardless of operational state.

    Most checks take zero args; severity-downgrade after-the-fact is
    enough. Check G fires Resend directly inline (push side-effect during
    result computation, not via `log()` later), so it needs to know
    `alerts_suppressed` BEFORE running. Detected via signature inspection
    so heterogeneous check signatures coexist without a typed protocol.

    Returns the sweep RECORD for this check — the RAW result severity (pre
    MAINTENANCE-downgrade; the sweep file carries `alerts_suppressed` at the
    top level), consumed by main()'s results-file write for the operator
    dashboard's sweep panel.
    """
    name = check_fn.__name__
    letter = CHECK_LETTERS.get(name, "?")
    try:
        sig = inspect.signature(check_fn)
        if "alerts_suppressed" in sig.parameters:
            result = check_fn(alerts_suppressed=alerts_suppressed)
        else:
            result = check_fn()
    except Exception as e:
        log(
            Severity.ERROR,
            _SCRIPT,
            f"[watchdog-check-failed:{name}] {e!r}",
        )
        return {
            "check": name,
            "letter": letter,
            "severity": "ERROR",
            "summary": f"check raised {type(e).__name__} (harness-isolated)",
        }

    severity = result.severity
    if alerts_suppressed and severity in (Severity.WARN, Severity.CRITICAL):
        severity = Severity.INFO

    message = result.summary
    if result.details:
        message = f"{result.summary} | {result.details}"

    log(severity, _SCRIPT, message)
    return {
        "check": name,
        "letter": letter,
        "severity": result.severity.name,
        "summary": result.summary,
    }


def _check_token_write_capability() -> CheckResult:
    """Check L (B2): verify ITS_SMARTSHEET_TOKEN can WRITE, not just read.

    A read-only or mis-scoped token (e.g. after a botched rotation) passes every
    READ and only fails at the first real daemon WRITE — a silent mid-cycle 401
    that is hard to trace. This probe (create + delete a throwaway sheet) turns
    that into a LOUD daily signal: a SmartsheetWriteCapabilityError → CRITICAL,
    which (post-A3) pages the operator via `_run_check`'s `log(CRITICAL)` and is
    deferred during MAINTENANCE by the standard `alerts_suppressed` downgrade
    above. A Smartsheet OUTAGE (SmartsheetCircuitOpenError) is INFO-skipped — it
    is not a token verdict; any other transient error is WARN-inconclusive. Cost
    is one create + one delete per daily watchdog run (negligible footprint;
    the throwaway sheet is named `_its_write_probe_*` and deleted immediately).
    """
    try:
        probe_sheet_id = smartsheet_client.verify_write_capability()
    except smartsheet_client.SmartsheetWriteCapabilityError as exc:
        return CheckResult(
            Severity.CRITICAL,
            f"ITS_SMARTSHEET_TOKEN cannot write (read-only or mis-scoped?): {exc}",
        )
    except smartsheet_client.SmartsheetCircuitOpenError:
        return CheckResult(
            Severity.INFO,
            "token write-probe skipped — Smartsheet circuit breaker OPEN.",
        )
    except smartsheet_client.SmartsheetError as exc:
        return CheckResult(
            Severity.WARN,
            f"token write-probe inconclusive (transient Smartsheet error): {exc!r}",
        )
    # Created → the token can write. Clean up the throwaway probe sheet, with a
    # create→delete eventual-consistency settle retry (the immediate delete can
    # 404 / errorCode 5036 before the new sheet propagates — surfaced in the B2
    # smoke).
    try:
        smartsheet_client.delete_sheet_settling(probe_sheet_id)
    except smartsheet_client.SmartsheetError as exc:
        return CheckResult(
            Severity.WARN,
            f"token write OK, but probe sheet {probe_sheet_id} delete failed "
            f"(manual cleanup of `_its_write_probe_*` may be needed): {exc!r}",
        )
    return CheckResult(Severity.INFO, "ITS_SMARTSHEET_TOKEN write capability OK.")


# Check M (C3): the blueprint repo's .claude guard symlinks.
_BLUEPRINT_ROOT = Path.home() / "its-blueprint"
_BLUEPRINT_GUARD_PATHS = (".claude/agents", ".claude/hooks")


def _check_blueprint_guard_symlinks() -> CheckResult:
    """Check M (C3): the blueprint repo's `.claude` guard symlinks must resolve.

    `~/its-blueprint/.claude/{agents,hooks}` are committed RELATIVE symlinks into
    `../../its/.claude/...`; they resolve ONLY when `~/its-blueprint` is a
    `~/`-level sibling of `~/its`. If they DANGLE (a repo move / non-sibling
    layout), Claude Code loads zero agents AND the propose-only guard hooks
    (block-doctrine-write, block-codeql-dismiss, block-doc-reconciliation-write)
    silently vanish — a fail-OPEN with no warning. This converts that into a
    loud WARN on the production host, where the watchdog runs and the symlinks
    SHOULD resolve.

    Scope (C3 fix-direction (a)): this catches a PRODUCTION-layout regression —
    the one place a structural check can actually run against a resolvable
    layout. A bare blueprint clone / CI runner has no `~/its` sibling and no
    watchdog running, so that case stays a documented operating constraint
    (`docs/operations/worktree_discipline.md`). WARN (not CRITICAL): the guards
    are propose-only safety nets, not the security boundary — operator-actionable
    "fix your layout", not a page-worthy emergency.
    """
    if not _BLUEPRINT_ROOT.exists():
        return CheckResult(
            Severity.INFO,
            "blueprint repo not on this host — guard-symlink check skipped.",
        )
    # Path.exists() follows the symlink, so a dangling link (target missing) and
    # an outright-missing path both read as False — either way the guards are gone.
    unresolved = [rel for rel in _BLUEPRINT_GUARD_PATHS if not (_BLUEPRINT_ROOT / rel).exists()]
    if unresolved:
        return CheckResult(
            Severity.WARN,
            f"blueprint guard symlinks unresolved: {unresolved} — the propose-only "
            f"guard hooks may be SILENTLY ABSENT (fail-open). Confirm ~/its-blueprint "
            f"is a ~/-level sibling of ~/its (docs/operations/worktree_discipline.md).",
        )
    return CheckResult(Severity.INFO, "blueprint guard symlinks resolve OK.")


# ---- Check N: WSR rows stuck in SENDING (write-ahead-marker safety net) --

# Cap on the row-ID list in the WARN detail (mirrors REVIEW_QUEUE_ITEM_CAP); the full
# set is one Smartsheet query away.
WSR_SENDING_ITEM_CAP = 10


def _check_stuck_wsr_send() -> CheckResult:
    """Check N: review rows stuck in Send Status=SENDING, across ALL send lanes.

    The shared send engine writes a SENDING write-ahead marker immediately BEFORE the
    irreversible Graph send, then flips it to SENT (Stage 7). A row that stays in
    SENDING means that SENT-stamp failed (the report WAS sent but not recorded) or
    the daemon died mid-send. By design such a row is NOT re-dispatched — no
    double-send (weekly_send_poll.DISPATCH_STATUSES excludes SENDING) — so without
    this check it would sit SILENTLY unsent/unrecorded. A legitimate SENDING is
    sub-second, so any row this (hourly) check catches is effectively stuck → WARN
    (operator: confirm delivery, then mark SENT; or set back to PENDING to re-send).

    Scans every lane in `_REVIEW_SHEETS` (it scanned only WSR until 2026-07-21, so a
    stuck row on the three vendor-facing procurement lanes was never reported).
    Per-sheet fail-soft: one lane's read error cannot hide the other four.

    READ-ONLY: never writes a review row and never sends — its only effect is the
    returned CheckResult. A Smartsheet read failure is caught by the `_run_check`
    fence (logged ERROR, other checks unaffected), so this check cannot break the
    run, cause a send, or cause a missed send. WARN is paged-deferred during
    MAINTENANCE and never triggers the CRITICAL-only Resend/Sentry legs.
    """
    stuck: list[str] = []  # "LABEL:row_id" per stuck row, across every lane
    read_errors: list[str] = []
    for label, sheet_id, col, _held, sending in _REVIEW_SHEETS:
        try:
            rows = smartsheet_client.get_rows(sheet_id, filters={col: sending})
        except Exception as exc:  # noqa: BLE001 — per-sheet fail-soft
            # One lane's read failure must not hide the other four (the Check-T
            # pattern): record it and keep scanning.
            read_errors.append(f"{label}: {exc!r}")
            continue
        stuck.extend(f"{label}:{r.get('_row_id')}" for r in rows)

    if not stuck:
        if read_errors:
            return CheckResult(
                severity=Severity.INFO,
                summary=f"No review rows stuck in SENDING ({len(read_errors)} sheet read(s) failed).",
                details="; ".join(read_errors),
            )
        return CheckResult(severity=Severity.INFO, summary="No review rows stuck in SENDING.")

    capped = stuck[:WSR_SENDING_ITEM_CAP]
    details = f"Rows: {', '.join(capped)}"
    if len(stuck) > WSR_SENDING_ITEM_CAP:
        details += f" (showing first {WSR_SENDING_ITEM_CAP} of {len(stuck)})"
    if read_errors:
        details += f" | read errors: {'; '.join(read_errors)}"
    return CheckResult(
        severity=Severity.WARN,
        summary=(
            f"{len(stuck)} review row(s) stuck in SENDING (sent-but-not-stamped, or daemon "
            f"died mid-send) — confirm delivery + mark SENT, or set PENDING to re-send."
        ),
        details=details,
    )


# ---- Check Q: portal_poll sustained pending-fetch outage escalation ------


def _check_portal_poll_fetch_outage() -> CheckResult:
    """Check Q (A4): re-raise a SUSTAINED portal_poll pending-fetch outage.

    portal_poll counts consecutive failed `GET /api/internal/pending` cycles in
    `portal_poll.FETCH_FAIL_STATE_PATH` and ALREADY fires an inline CRITICAL once the count
    reaches `FETCH_FAIL_CRITICAL_THRESHOLD` (filing is stopped). This check is the daily
    second-opinion backstop: if that inline page was lost / deferred (MAINTENANCE) / not-yet-
    fired, the watchdog re-raises it. The counter RESETS to 0 on any successful fetch, so a
    value at/over threshold means the MOST RECENT cycle also failed — the outage is ACTIVE,
    not historical → CRITICAL (paged by `_run_check`; MAINTENANCE-deferred like every other).

    FAIL-SOFT: a missing / unreadable counter is INFO (no data → no page). A watchdog read
    error must never MASK an outage by erroring, nor INVENT one — INFO is the honest "unknown".
    """
    path = portal_poll.FETCH_FAIL_STATE_PATH
    if not path.exists():
        return CheckResult(
            severity=Severity.INFO,
            summary="portal_poll fetch-failure counter absent (no outage recorded).",
        )
    try:
        count = int(json.loads(path.read_text()).get("count", 0))
    except (OSError, ValueError, TypeError):
        return CheckResult(
            severity=Severity.INFO,
            summary="portal_poll fetch-failure counter unreadable (transient).",
        )
    threshold = portal_poll.FETCH_FAIL_CRITICAL_THRESHOLD
    if count >= threshold:
        return CheckResult(
            severity=Severity.CRITICAL,
            summary=(
                f"portal_poll pending-fetch OUTAGE: {count} consecutive failed cycles "
                f"(>= {threshold}) — filing is STOPPED and submissions are accumulating."
            ),
        )
    return CheckResult(
        severity=Severity.INFO,
        summary=f"portal_poll pending-fetch healthy ({count}/{threshold} consecutive failures).",
    )


# ---- Check R: portal_poll unfiled pending-backlog (stuck-drain) ----------

# How long a stuck-backlog latch must hold before the daily watchdog WARNs. A one-cycle burst
# self-clears the latch; only a SUSTAINED stuck drain (intake erroring on every row) crosses this.
_PENDING_BACKLOG_SUSTAINED = timedelta(hours=2)


def _check_portal_poll_pending_backlog() -> CheckResult:
    """Check R (A4): WARN on a SUSTAINED unfiled backlog portal_poll can't drain.

    portal_poll writes `portal_poll.PENDING_BACKLOG_STATE_PATH` each cycle, latching
    `high_since_utc` whenever a saturated pending page (>= PENDING_LIMIT) drains NOTHING
    (intake erroring on every row → nothing marked-filed; true depth masked by the page cap).
    This WARNs only once that latch has held past `_PENDING_BACKLOG_SUSTAINED`, so a transient
    burst never pages. Distinct from Check Q (can't FETCH) and Check C (daemon stale): Check R
    is "fetching fine, draining nothing". WARN, not CRITICAL — operator-actionable ("why is
    intake rejecting every row") and recoverable, not a host-down emergency.

    FAIL-SOFT: missing / unreadable / unlatched / unparseable timestamp all read as INFO.
    """
    path = portal_poll.PENDING_BACKLOG_STATE_PATH
    if not path.exists():
        return CheckResult(
            severity=Severity.INFO,
            summary="portal_poll pending-backlog marker absent (no stuck backlog).",
        )
    try:
        data = json.loads(path.read_text())
        high_since_raw = data.get("high_since_utc")
    except (OSError, ValueError, TypeError):
        return CheckResult(
            severity=Severity.INFO,
            summary="portal_poll pending-backlog marker unreadable (transient).",
        )
    if not high_since_raw:
        return CheckResult(
            severity=Severity.INFO,
            summary="portal_poll backlog draining normally (no stuck latch).",
        )
    try:
        high_since = datetime.fromisoformat(high_since_raw)
    except (ValueError, TypeError):
        return CheckResult(
            severity=Severity.INFO,
            summary="portal_poll pending-backlog marker has an unparseable high_since_utc.",
        )
    if high_since.tzinfo is None:
        high_since = high_since.replace(tzinfo=UTC)
    stuck_for = datetime.now(UTC) - high_since
    if stuck_for >= _PENDING_BACKLOG_SUSTAINED:
        hrs = stuck_for.total_seconds() / 3600
        count = data.get("count", "?")
        return CheckResult(
            severity=Severity.WARN,
            summary=(
                f"portal_poll unfiled backlog STUCK ~{hrs:.1f}h (page saturated at {count}, "
                f"draining nothing) — intake is likely erroring on every row; submissions are "
                f"piling up behind the {portal_poll.PENDING_LIMIT}-row page cap."
            ),
        )
    return CheckResult(
        severity=Severity.INFO,
        summary=(
            f"portal_poll backlog latched but under the {_PENDING_BACKLOG_SUSTAINED} window."
        ),
    )


# ---- Check P: Box OAuth refresh-token freshness -------------------------
#
# §42: Box rotates the refresh token on every exchange and it EXPIRES 60 days
# from last use (see shared/box_client.py module docstring). Steady-state daily
# workstreams refresh well inside that window, but a multi-day host outage — or a
# daemon that quietly stopped touching Box — erodes the margin INVISIBLY, and the
# failure mode is catastrophic: once expired, EVERY Box operation fails until the
# operator re-runs setup_box_oauth.py. box_client._store_tokens stamps a freshness
# marker on every successful persist (A3); this check reads it and escalates ahead
# of expiry. WARN at 50d / CRITICAL at 58d give a 10-day then 2-day buffer.
# Read-only — returns a CheckResult (no inline alert), so _run_check pages the
# WARN/CRITICAL and MAINTENANCE-defers it like Checks L/M/N. (Check O is the A5
# row-cap rotation, built in the 2026-07 growth slice; this is P.)
BOX_TOKEN_FRESHNESS_WARN_DAYS = 50
BOX_TOKEN_FRESHNESS_CRITICAL_DAYS = 58


def _check_box_token_freshness() -> CheckResult:
    """Check P: Box OAuth refresh token must be exercised inside the 60-day window.

    Reads the freshness marker box_client._store_tokens writes on every persist.
    Marker absent → WARN ("unknown"): expected briefly right after A3 ships (until
    the first refresh writes it); a persistent absence means Box has never authed.
    """
    marker = box_client.BOX_TOKEN_REFRESH_MARKER
    if not marker.exists():
        return CheckResult(
            Severity.WARN,
            "Box OAuth refresh marker absent — token freshness unknown. Expected "
            "briefly right after enabling A3 (until the first refresh writes it); a "
            "persistent absence means Box has never authed — run "
            "scripts/setup_box_oauth.py.",
        )
    try:
        data = json.loads(marker.read_text())
        last_refresh = datetime.fromisoformat(data["last_refresh_utc"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return CheckResult(
            Severity.WARN,
            f"Box OAuth refresh marker unreadable ({exc!r}) — token freshness unknown.",
        )
    if last_refresh.tzinfo is None:
        last_refresh = last_refresh.replace(tzinfo=UTC)
    idle_days = (datetime.now(UTC) - last_refresh).days
    if idle_days >= BOX_TOKEN_FRESHNESS_CRITICAL_DAYS:
        return CheckResult(
            Severity.CRITICAL,
            f"Box OAuth refresh token idle {idle_days}d "
            f"(>= {BOX_TOKEN_FRESHNESS_CRITICAL_DAYS}) — expires at 60d. Re-run "
            f"scripts/setup_box_oauth.py NOW or all Box operations will fail "
            f"(escalate to Seth — secrets/auth, a fixed high-capability-class category).",
        )
    if idle_days >= BOX_TOKEN_FRESHNESS_WARN_DAYS:
        return CheckResult(
            Severity.WARN,
            f"Box OAuth refresh token idle {idle_days}d "
            f"(>= {BOX_TOKEN_FRESHNESS_WARN_DAYS}) — approaching the 60d expiry; "
            f"confirm the Box-writing daemons are running.",
        )
    return CheckResult(
        Severity.INFO, f"Box OAuth refresh token fresh (idle {idle_days}d)."
    )


# ---- Check S: origin/main CI is green -----------------------------------
#
# This repo is Evergreen-specific (one customer per private repo), so the slug is
# a constant. The watchdog launchd plist sets PATH=/opt/homebrew/bin:... so `gh`
# resolves under launchd; on any gh/network failure the check is INFO-skipped.
# Scoped to the ci.yml workflow = the REQUIRED suites (test/portal/secrets); the
# separate CodeQL default-setup workflow (non-required, periodically infra-flaky)
# is deliberately excluded so it can never false-page.
#
# CORRECTED 2026-08-07: this read `SolutionSmith-debug/its` — a DIFFERENT, still-active
# repository (both were pushed to on the day this was found; neither is a fork of the
# other). Check S is the mechanical step 4 of the four-part landing verify: "did the code
# this host is running actually land green on ITS OWN main?". Pointed at another repo it
# answered a question about somebody else's code and could NEVER WARN about ours — a
# silent false-green on a landing gate, which is worse than having no check at all. The
# slug must name the repo `origin` points at; `test_check_s_repo_matches_origin_remote`
# pins that and RED-lights on any future rename or re-point.
GH_MAIN_CI_REPO = "its-sys-admin/evergreen-its"
GH_MAIN_CI_WORKFLOW = "ci.yml"
GH_MAIN_CI_TIMEOUT_SECONDS = 30


def _resolve_main_head_sha(gh: str) -> str | None:
    """Resolve origin/main's actual HEAD sha via `gh api` (Check S staleness probe).

    Fail-SAFE like every other Check S probe: any gh/network/parse failure — or an
    output that is not a full 40-hex sha — returns None, which the caller treats as
    "probe skipped" (INFO, never WARN). Our OWN inability to resolve HEAD must not
    page.
    """
    try:
        proc = subprocess.run(
            [gh, "api", f"repos/{GH_MAIN_CI_REPO}/commits/main", "--jq", ".sha"],
            capture_output=True,
            text=True,
            timeout=GH_MAIN_CI_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    head_sha = (proc.stdout or "").strip()
    if len(head_sha) != 40 or any(c not in "0123456789abcdef" for c in head_sha.lower()):
        return None
    return head_sha


def _check_main_branch_ci_green() -> CheckResult:
    """Check S: origin/main's required CI (ci.yml) is green on the latest commit.

    Promotes the manual four-part-verify step 4 (docs/operations/pr_merge_discipline.md)
    to a mechanical <=24h detector. A PR that lands but turns main RED — or a
    concurrency-cancelled merge-commit run — otherwise sits undetected (forensic
    class #13: six PRs once landed on a red main for days). Queries the LATEST ci.yml
    run on origin/main via `gh`.

    Fail-SAFE: `gh` missing / network error / no runs / a run that is not yet
    complete all return INFO — never CRITICAL on our OWN inability to check. Only a
    CONCLUSIVE non-success conclusion pages. The CRITICAL is paged + MAINTENANCE-
    deferred by `_run_check`'s `log(CRITICAL)` (no inline alert).

    HEAD-staleness (2026-07-19): a green LATEST RUN proves nothing about the latest
    COMMIT — on 2026-07-19 GitHub stopped delivering push events for main merges, so
    three consecutive merge commits got ZERO ci.yml runs and this check kept reading
    the older green run as "main-CI green" indefinitely. So a conclusive green run is
    ALSO compared against origin/main's actual HEAD sha; a mismatch means HEAD is
    UNVERIFIED → WARN (not CRITICAL — the tree may be verified by other means), with
    the remediation `gh workflow run ci --ref main` (the workflow_dispatch trigger,
    PR #619). HEAD-resolution failure stays fail-SAFE INFO, never WARN.
    """
    gh = shutil.which("gh")
    if gh is None:
        return CheckResult(Severity.INFO, "main-CI check skipped — `gh` not on PATH.")
    try:
        proc = subprocess.run(
            [
                gh, "run", "list", "--repo", GH_MAIN_CI_REPO, "--branch", "main",
                "--workflow", GH_MAIN_CI_WORKFLOW, "--limit", "1",
                "--json", "status,conclusion,headSha",
            ],
            capture_output=True,
            text=True,
            timeout=GH_MAIN_CI_TIMEOUT_SECONDS,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return CheckResult(Severity.INFO, f"main-CI check skipped — gh error: {exc!r}")

    if proc.returncode != 0:
        return CheckResult(
            Severity.INFO,
            f"main-CI check skipped — gh exited {proc.returncode}: {proc.stderr.strip()[:160]}",
        )
    try:
        runs = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return CheckResult(Severity.INFO, "main-CI check skipped — unparseable gh output.")
    if not runs:
        return CheckResult(Severity.INFO, "main-CI check skipped — no ci.yml runs on main.")

    run = runs[0]
    status = str(run.get("status", ""))
    conclusion = str(run.get("conclusion") or "")
    sha = str(run.get("headSha", ""))[:7]

    if status != "completed":
        return CheckResult(Severity.INFO, f"main-CI run on {sha} is {status} (not yet conclusive).")
    if conclusion == "success":
        # Green — but only for the RUN's sha. Compare against origin/main's actual
        # HEAD (the 2026-07-19 push-event delivery gap: merges with ZERO runs made
        # the older green run read as current indefinitely).
        head_sha = _resolve_main_head_sha(gh)
        if head_sha is None:
            return CheckResult(
                Severity.INFO,
                f"main-CI green on {sha} (ci.yml: test/portal/secrets); HEAD-staleness "
                "probe skipped — could not resolve origin/main HEAD.",
            )
        run_sha = str(run.get("headSha", ""))
        if run_sha != head_sha:
            return CheckResult(
                Severity.WARN,
                f"[main-ci-stale] latest green ci.yml run is for {run_sha[:7]} but "
                f"origin/main HEAD is {head_sha[:7]} — HEAD has NO conclusive CI run "
                "(push-event delivery gap, 2026-07-19 class). Trigger one: "
                "gh workflow run ci --ref main",
            )
        return CheckResult(Severity.INFO, f"main-CI green on {sha} (ci.yml: test/portal/secrets).")
    return CheckResult(
        Severity.CRITICAL,
        f"[main-ci-red] origin/main CI is '{conclusion}' on {sha} — a PR landed on a red main.",
        details=(
            "The required ci.yml suites (test/portal/secrets) did not pass on the latest main "
            "commit (four-part-verify step 4; forensic class #13 partial-PR-landed). Investigate: "
            f"gh run list --repo {GH_MAIN_CI_REPO} --branch main --workflow {GH_MAIN_CI_WORKFLOW} --limit 3"
        ),
    )


# ---- Check T: review rows stuck HELD (WSR + WPR; P5 operability) ---------
#
# A HELD is an operator-actionable refusal (no recipient / missing PDF / oversized packet /
# workstream contamination). The companion `shared/recipient_health` module (P5, PR #380) files
# a per-incident ITS_Review_Queue RECORD at SEND time for the no-recipient subset; this DAILY
# scan is the catch-all backstop for ALL HELD reasons across BOTH review sheets (today also
# covered at HELD time by weekly_send's generic `weekly_send.held` WARN), so a HELD left
# un-actioned never sits silently. Age-thresholded via a first-seen
# state file so a just-created HELD (e.g. during a live smoke, or a row the operator is actively
# resolving) does NOT false-fire — only a HELD that has SAT past HELD_ROW_STALE_AFTER WARNs.
HELD_ROW_STALE_AFTER = timedelta(hours=24)
HELD_ROW_FIRST_SEEN_PATH = STATE_DIR / "held_row_first_seen.json"
HELD_ROW_ITEM_CAP = 10

# (label, sheet id, send-status column, HELD value) for each review sheet.
# Derived from _REVIEW_SHEETS so Checks N and T can never scan different lane sets.
_HELD_SCAN_SHEETS: list[tuple[str, int, str, str]] = [
    (label, sheet_id, col, held) for label, sheet_id, col, held, _sending in _REVIEW_SHEETS
]


def _load_held_first_seen_unlocked() -> dict[str, str]:
    """Read the first-seen map ({"WSR:123": iso_utc}); {} on any read/parse error."""
    try:
        if HELD_ROW_FIRST_SEEN_PATH.exists():
            data = json.loads(HELD_ROW_FIRST_SEEN_PATH.read_text())
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _merge_held_first_seen(
    prior: dict[str, str], held_now: set[str], scanned_labels: set[str], now: datetime
) -> dict[str, str]:
    """Merge the prior first-seen map with this round's HELD keys.

    CRITICAL (Check-T state-loss guard): only prune keys whose sheet LABEL was scanned
    SUCCESSFULLY this round — a key whose sheet had a transient read error is PRESERVED with its
    prior timestamp, so a one-sheet read failure can never silently reset the OTHER (or the same,
    next cycle) sheet's staleness clock. Cleared rows on a scanned sheet drop out; newly-seen
    HELD keys are stamped `now`. (Mirrors Check U's baseline merge, which preserves an unread
    workspace's prior snapshot.)"""
    merged = dict(prior)
    for key in list(merged):
        label = key.split(":", 1)[0]
        if label in scanned_labels and key not in held_now:
            del merged[key]  # this sheet WAS read and the row is no longer HELD → cleared
    for key in held_now:
        merged.setdefault(key, now.isoformat())
    return merged


def _update_held_first_seen(
    held_now: set[str], scanned_labels: set[str], now: datetime
) -> dict[str, str]:
    """Read-modify-write the first-seen map under a path lock; returns the merged map.

    Fail-soft (never raises): on any state I/O error, WARN and return a best-effort in-memory
    map (current keys stamped `now`), so age-thresholding degrades to "nothing stale yet" this
    run rather than crashing the check or inventing staleness."""
    try:
        with state_io.with_path_lock(HELD_ROW_FIRST_SEEN_PATH):
            prior = _load_held_first_seen_unlocked()
            merged = _merge_held_first_seen(prior, held_now, scanned_labels, now)
            state_io.atomic_write_json(HELD_ROW_FIRST_SEEN_PATH, merged)
            return merged
    except Exception as exc:  # noqa: BLE001 — fail-soft per state-write discipline
        log(
            Severity.WARN,
            _SCRIPT,
            f"held-row first-seen state I/O failed ({exc!r}); age-thresholding degraded this run.",
        )
        return {k: now.isoformat() for k in held_now}


def _check_stale_held_rows() -> CheckResult:
    """Check T: WSR + WPR rows stuck Send Status=HELD beyond HELD_ROW_STALE_AFTER → WARN.

    READ-ONLY against Smartsheet (no row write, no send). A per-sheet read failure is recorded
    and the other sheet still scans; a total read failure → INFO (no data, no false WARN). The
    only persistent side effect is the first-seen state file (atomic + path-locked)."""
    now = datetime.now(UTC)
    held_now: set[str] = set()
    scanned_labels: set[str] = set()
    read_errors: list[str] = []
    for label, sheet_id, status_col, held_val in _HELD_SCAN_SHEETS:
        try:
            rows = smartsheet_client.get_rows(sheet_id, filters={status_col: held_val})
        except Exception as exc:  # noqa: BLE001 — per-sheet fail-soft; other sheet still scans
            read_errors.append(f"{label}: {exc!r}")
            continue
        scanned_labels.add(label)  # this sheet read OK → its keys may be pruned-if-cleared
        for r in rows:
            held_now.add(f"{label}:{r.get('_row_id')}")

    if not scanned_labels:
        # BOTH sheets failed → no data; never invent a WARN, and don't touch the state file
        # (so no successfully-scanned label exists to prune — the prior clocks are untouched).
        return CheckResult(
            severity=Severity.INFO,
            summary=f"HELD-row scan: review sheet read failed ({'; '.join(read_errors)}); no data.",
        )

    first_seen = _update_held_first_seen(held_now, scanned_labels, now)
    stale: list[str] = []
    for key, iso in first_seen.items():
        try:
            seen = datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            continue  # unparseable stamp → treat as just-seen (errs toward not-stale)
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=UTC)
        if now - seen >= HELD_ROW_STALE_AFTER:
            stale.append(key)

    if not stale:
        if held_now:
            return CheckResult(
                severity=Severity.INFO,
                summary=f"{len(held_now)} review row(s) HELD but none past {HELD_ROW_STALE_AFTER} yet.",
            )
        return CheckResult(severity=Severity.INFO, summary="No review rows stuck HELD.")

    capped = sorted(stale)[:HELD_ROW_ITEM_CAP]
    details = f"Keys (sheet:row): {', '.join(capped)}"
    if len(stale) > HELD_ROW_ITEM_CAP:
        details += f" (showing {HELD_ROW_ITEM_CAP} of {len(stale)})"
    if read_errors:
        details += f" | partial scan (read errors: {'; '.join(read_errors)})"
    return CheckResult(
        severity=Severity.WARN,
        summary=(
            f"{len(stale)} review row(s) stuck HELD > {HELD_ROW_STALE_AFTER} (WSR/WPR) — "
            f"operator action overdue (fix the contact / recompile / re-tag, then clear the HELD)."
        ),
        details=details,
    )


# ---- Check U: send-workspace approver-set drift (F22 authority; P5) ------
#
# The F22 approver set for a workstream's external sends IS the membership of its send
# workspace (Op Stds §46). Two failure modes this surfaces: (a) an EMPTY set → every send for
# that workstream fail-closed-blocks (EMPTY_ALLOWLIST) — silent until someone notices nothing
# sent; (b) a CHANGE to who-may-approve since the last run — a security-relevant event (someone
# added/removed from the send-approval authority) the operator should confirm was intentional.
# There is no stored "intended" approver list (membership IS the source of truth), so "drift" =
# change-vs-baseline; the first run seeds the baseline (no drift reported).
#
# SCOPE = every workspace some send daemon verifies approvals against, i.e. the distinct set of
# `send_poll_core.DaemonConfig.f22_workspace_id` across all five send lanes. This list carried
# only the two SAFETY/PROGRESS workspaces until 2026-07-21, so approver drift on the PROCUREMENT
# workspaces went unwatched even after po_send, subcontract_send and rfq_send went live — the
# three lanes that transmit to outside VENDORS. Someone added to the Purchase Orders workspace
# silently gained authority to release a PO or an RFQ, and nothing reported it.
#
# Deliberately a literal rather than derived-by-import: importing the send daemons here would
# pull `graph_client.send_mail` into the watchdog process, giving a monitoring script send
# capability (Invariant 1). `tests/test_watchdog.py::test_approver_workspaces_cover_every_send_lane`
# does the derivation instead — in the test process, where importing is free — and fails if a new
# send lane's workspace is missing here.
APPROVER_BASELINE_PATH = STATE_DIR / "approver_set_baseline.json"
_APPROVER_WORKSPACES: list[tuple[str, int]] = [
    ("Safety Portal", sheet_ids.WORKSPACE_SAFETY_PORTAL),
    ("Progress Reporting", sheet_ids.WORKSPACE_PROGRESS_REPORTING),
    ("Purchase Orders", sheet_ids.WORKSPACE_PURCHASE_ORDERS),  # po_send + rfq_send
    ("Subcontracts", sheet_ids.WORKSPACE_SUBCONTRACTS),
]


def _load_approver_baseline_unlocked() -> dict[str, list[str]]:
    try:
        if APPROVER_BASELINE_PATH.exists():
            data = json.loads(APPROVER_BASELINE_PATH.read_text())
            if isinstance(data, dict):
                return {str(k): [str(e) for e in v] for k, v in data.items() if isinstance(v, list)}
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _check_approver_drift() -> CheckResult:
    """Check U: F22 send-approver set per send workspace — empty (sends blocked) or changed.

    READ-ONLY against Smartsheet. Fail-soft: a per-workspace read error is recorded and the
    other workspace still checks; a total failure / state-I/O failure → INFO (never invents
    drift). WARN (not CRITICAL): an empty set is operator-actionable (re-share approvers, §46)
    and a membership change wants confirmation, neither is a page-worthy host emergency."""
    now_sets: dict[str, list[str]] = {}
    empty: list[str] = []
    read_errors: list[str] = []
    for label, workspace_id in _APPROVER_WORKSPACES:
        try:
            emails = smartsheet_client.list_workspace_share_emails(workspace_id)
        except Exception as exc:  # noqa: BLE001 — per-workspace fail-soft
            read_errors.append(f"{label}: {exc!r}")
            continue
        now_sets[label] = sorted(emails)
        if not emails:
            empty.append(label)

    if not now_sets:
        return CheckResult(
            severity=Severity.INFO,
            summary=f"approver-drift scan: workspace read failed ({'; '.join(read_errors)}); no data.",
        )

    # Compare to the baseline + persist the new snapshot (fail-soft: on state error, skip drift
    # detection this run but still report empties).
    drift_notes: list[str] = []
    try:
        with state_io.with_path_lock(APPROVER_BASELINE_PATH):
            baseline = _load_approver_baseline_unlocked()
            for label, current in now_sets.items():
                prior = baseline.get(label)
                if prior is not None and set(prior) != set(current):
                    added = sorted(set(current) - set(prior))
                    removed = sorted(set(prior) - set(current))
                    drift_notes.append(
                        f"{label}: +{added or '[]'} -{removed or '[]'}"
                    )
            # Persist the union of prior (workspaces not read this run keep their baseline) + current.
            merged = {**baseline, **now_sets}
            state_io.atomic_write_json(APPROVER_BASELINE_PATH, merged)
    except Exception as exc:  # noqa: BLE001 — fail-soft per state-write discipline
        log(
            Severity.WARN,
            _SCRIPT,
            f"approver-set baseline state I/O failed ({exc!r}); drift detection skipped this run.",
        )

    if empty:
        details = f"changes: {'; '.join(drift_notes)}" if drift_notes else ""
        return CheckResult(
            severity=Severity.WARN,
            summary=(
                f"EMPTY send-approver set for {', '.join(empty)} workspace(s) — every "
                f"send there is fail-closed-blocked (F22 EMPTY_ALLOWLIST). Re-share approvers (§46)."
            ),
            details=details,
        )
    if drift_notes:
        return CheckResult(
            severity=Severity.WARN,
            summary=(
                "Send-approver set CHANGED since last run — confirm intentional (who may "
                "approve external sends changed)."
            ),
            details="; ".join(drift_notes),
        )
    suffix = f" (partial: {'; '.join(read_errors)})" if read_errors else ""
    return CheckResult(
        severity=Severity.INFO,
        summary=f"Send-approver sets present + unchanged for {', '.join(now_sets)}.{suffix}",
    )


# ---- Check V: D1 prune heartbeat (GS2 — unbounded-growth audit Slice 2) ---
#
# The Worker's daily scheduled prune (safety_portal/worker/prune.ts) is ALL of D1
# retention — 90d payload strip, 365d audit, PDF cache, terminal publish rows, empty
# jobs. Before GS2 it was a single point of SILENT failure: no last-run record, no
# failure flag, success only a console.log nobody tails (audit time-bomb #4). A dead
# prune at 20×20 scale is a 10 GB D1 wall (every INSERT fails → /api/submit 500s →
# total field-capture outage) in 7–17 weeks. GS2 gives the prune a durable heartbeat:
# each stage runs fenced (a throw no longer skips later stages), and the scheduled
# handler UPSERTs a one-row prune_meta record (migration 0033) after EVERY run. This
# check reads it back over the bearer-gated GET /api/internal/prune-status (the
# poller's internal-token tier — shared.portal_client.get_prune_status) and escalates:
#
#   CRITICAL — failed_stages non-empty (a retention stage is throwing; at persistence
#              that IS the dead-prune failure mode) OR db_size_bytes over the 6 GB
#              threshold (the previously console-only WARN in prune.ts, now paged).
#   WARN     — last_run_at >48h stale (the daily cron missed ~2 runs — dead cron,
#              broken deploy, or an unwritable meta row; all operator-actionable) or
#              the meta row absent/malformed (prune has never recorded a run since
#              0033 — briefly expected right after the migration lands, a real signal
#              if it persists; mirrors Check P's absent-marker posture).
#   INFO     — healthy, or fail-soft "no data" (creds unresolved — portal_poll owns
#              that page — or a transient transport error; never mask, never invent).
#
# Letter bookkeeping: A–D live, E deferred (Anthropic spend), F retired, G/I–N live,
# H deliberately never built (doctrine artifact — "there is no Check H"), O reserved
# for the A5 row-cap rotation (being built by the parallel growth-Slice-1), P–U live.
# V is the first genuinely free letter — this is Check V.

PRUNE_META_STALE_AFTER = timedelta(hours=48)
PRUNE_DB_SIZE_CRITICAL_BYTES = 6_000_000_000


def _resolve_prune_status_creds() -> tuple[str, str] | None:
    """Best-effort (base_url, bearer) for the internal-token tier — fail-soft None.

    Reuses portal_poll's canonical names (CFG_WORKER_BASE_URL ITS_Config key +
    KC_BEARER Keychain entry) so there is exactly ONE definition of where the Worker
    lives and which token drains it. Unlike portal_poll._resolve_creds this NEVER
    raises/pages — missing or unreadable creds resolve to None and Check V reports
    INFO (portal_poll itself owns the missing-creds CRITICAL; duplicating that page
    from the watchdog would be alert noise)."""
    try:
        raw = smartsheet_client.get_setting(
            portal_poll.CFG_WORKER_BASE_URL, workstream=portal_poll.WORKSTREAM
        )
    except smartsheet_client.SmartsheetError:
        return None
    base_url = raw if isinstance(raw, str) and raw else ""
    try:
        bearer = keychain.get_secret(portal_poll.KC_BEARER)
    except keychain.KeychainError:
        return None
    if not (base_url and bearer):
        return None
    return base_url, bearer


def _check_portal_prune_health() -> CheckResult:
    """Check V: the D1 prune heartbeat — stale WARN, failed-stage / size CRITICAL.

    Read-only over GET /api/internal/prune-status. Fail-soft on transport (INFO —
    never masks an outage by erroring, never invents one); WARN on a rejected bearer
    (deterministic misconfig, will not self-heal) and on an absent/malformed meta row.
    """
    creds = _resolve_prune_status_creds()
    if creds is None:
        return CheckResult(
            severity=Severity.INFO,
            summary=(
                "prune-status creds unresolved (worker_base_url / internal token) — "
                "skipping; portal_poll owns the missing-creds page."
            ),
        )
    base_url, bearer = creds
    try:
        meta = portal_client.get_prune_status(base_url, bearer)
    except portal_client.PortalAuthError as exc:
        return CheckResult(
            severity=Severity.WARN,
            summary=(
                "prune-status bearer REJECTED (401) — deterministic misconfig "
                "(rotated/missing ITS_PORTAL_INTERNAL_TOKEN?); prune health is unobserved."
            ),
            details=repr(exc),
        )
    except portal_client.PortalTransportError as exc:
        return CheckResult(
            severity=Severity.INFO,
            summary="prune-status unreachable (transient) — no prune-health data this run.",
            details=repr(exc),
        )
    if meta is None:
        return CheckResult(
            severity=Severity.WARN,
            summary=(
                "prune_meta row ABSENT — the daily D1 prune has never recorded a run "
                "since migration 0033. Expected briefly right after the migration lands "
                "(first cron within 24h); a persistent absence means the cron is not "
                "firing or the meta write is failing."
            ),
        )

    failed_raw = meta.get("failed_stages")
    failed = failed_raw if isinstance(failed_raw, list) else ["<malformed failed_stages>"]
    if failed:
        return CheckResult(
            severity=Severity.CRITICAL,
            summary=(
                f"D1 prune ran with FAILED stage(s): {', '.join(str(s) for s in failed)} — "
                "retention for those tables is silently skipped; if persistent this is the "
                "dead-prune → 10 GB-wall trajectory (field-capture outage)."
            ),
            details=f"prune_meta: {meta!r}",
        )

    db_size_raw = meta.get("db_size_bytes")
    db_size: float | None = (
        float(db_size_raw)
        if isinstance(db_size_raw, (int, float)) and not isinstance(db_size_raw, bool)
        else None
    )
    if db_size is not None and db_size > PRUNE_DB_SIZE_CRITICAL_BYTES:
        return CheckResult(
            severity=Severity.CRITICAL,
            summary=(
                f"D1 size {int(db_size):,} bytes exceeds the "
                f"{PRUNE_DB_SIZE_CRITICAL_BYTES:,}-byte threshold (10 GB hard cap ahead — "
                "every INSERT fails at the cap). Review retention windows / large tables."
            ),
        )

    last_run = meta.get("last_run_at")
    if not isinstance(last_run, (int, float)) or isinstance(last_run, bool):
        return CheckResult(
            severity=Severity.WARN,
            summary="prune_meta.last_run_at malformed — prune health is unobservable.",
            details=f"prune_meta: {meta!r}",
        )
    age = datetime.now(UTC) - datetime.fromtimestamp(float(last_run), tz=UTC)
    if age >= PRUNE_META_STALE_AFTER:
        hrs = age.total_seconds() / 3600
        return CheckResult(
            severity=Severity.WARN,
            summary=(
                f"D1 prune STALE: last recorded run ~{hrs:.0f}h ago "
                f"(> {PRUNE_META_STALE_AFTER}) — the daily cron missed at least two runs "
                "(dead cron / broken deploy / meta write failing). Retention is not running."
            ),
        )
    size_note = f", size {int(db_size):,} bytes" if db_size is not None else ""
    return CheckResult(
        severity=Severity.INFO,
        summary=(
            f"D1 prune healthy: last run {age.total_seconds() / 3600:.1f}h ago"
            f"{size_note}, no failed stages."
        ),
    )

# ---- Check O: ITS_Errors + ITS_Review_Queue row-cap rotation (A5) --------
#
# Motivating finding (2026-07 unbounded-growth audit, ranked rows #2/#10;
# eval A5). Both sheets grow monotonically with NO deleter anywhere in the
# codebase: ITS_Errors is record-per-occurrence by design (the §3.1 forensic
# surface even bypasses the circuit breaker), and a drained Review-Queue row
# is a status flip, not a delete. The Smartsheet per-sheet row cap is a
# VERIFIED 20,000 at these column widths (not the eval text's 5,000 — that
# spec lives on unmerged branch c0cbf3b and is corrected here). Past the cap
# `add_rows` fails: the forensic record is LOST and Check B (open-CRITICALs)
# goes blind — the "am I on fire" surface dies quietly. Measured build-rate
# puts ITS_Errors 4.5–11 months out; a persistent failure loop on a 60s
# daemon ("storm") burns it in ~13 days.
#
# Mechanism (boring, rides the daily watchdog): count each sheet; WARN at
# SHEET_ROW_WARN_THRESHOLD (15,000); at SHEET_ROW_ROTATE_THRESHOLD (16,000)
# delete TERMINAL rows older than SHEET_ROW_ROTATION_RETENTION_DAYS (90d),
# oldest first, in delete_rows batches of 200 (NOT a documented Smartsheet
# cap — the SDK passes row IDs in the URL query string, and 450 sixteen-digit
# IDs blew the URL length limit with an HTTP 400 when first exercised live,
# 2026-07-13; 200 is the live-verified working size — see defaults.py),
# bounded per run — the next daily run re-counts and continues (Smartsheet's
# eventual-consistency window makes an in-run recount unreliable anyway).
# Every rotation writes a WARN summary record to ITS_Errors (never silent).
#
# STORM-MODE fallback (added 2026-07-13 — the day ITS_Errors actually hit the
# 20,000 cap): the 90d retention structurally exceeds the system's age for
# its whole first 90 days of life, so during that window NOTHING can ever be
# age-eligible — the 2026-07-13 config-WARN storm (~1,400–4,500 rows/day from
# daemons WARNing per-cycle on 5 missing ITS_Config rows) filled the sheet to
# the hard cap while this check fired CRITICAL "nothing deletable" two days
# running and deleted nothing (the ~13-day storm arithmetic above played out
# for real). When a sheet is over the rotate mark and the 90d pass yields
# ZERO eligible rows, eligibility is RE-SELECTED with the
# SHEET_ROW_STORM_FLOOR_DAYS (2d = 48h at date granularity) floor — same
# terminal predicate, same missing/unparseable-date exclusion, same
# oldest-first order, batching, per-run cap, and partial-failure semantics.
# A storm-mode rotation is WARN, with a LOUD "STORM-MODE" prefix in the note
# and the same never-silent row_cap_rotation record naming the overridden
# retention. Only when even the storm floor finds nothing does the CRITICAL
# fire (paged via _run_check; MAINTENANCE-deferred like every CheckResult
# page).
#
# "Terminal" is defined from each sheet's REAL columns:
#   ITS_Errors (written by error_log._smartsheet_log; resolution = operator
#   stamping `Resolved At`, blank-means-open per Check B):
#     terminal ⇔ Severity != "CRITICAL"  (records; nothing is ever "open")
#              OR Severity == "CRITICAL" AND `Resolved At` non-blank.
#     An OPEN CRITICAL (blank `Resolved At`) is NEVER deleted — it is Check
#     B's working set.
#   ITS_Review_Queue (shared/review_queue.py; Status picklist):
#     terminal ⇔ Status ∈ {APPROVED, REJECTED} (drained/reviewed).
#     PENDING and IN_REVIEW are live work; ESCALATED is deliberately treated
#     as OPEN too (it awaits Tier-3 action — the A5 spec's suggestion to
#     rotate ESCALATED rows is rejected as unsafe; an old escalation is an
#     operator signal, not debris).
# Age comes from the sheet's own date column (`Timestamp` / `Created At`,
# ISO YYYY-MM-DD). A row with a missing/unparseable date is NOT eligible —
# we cannot prove it is past retention (conservative, never-guess).
#
# Failure isolation (§27): every Smartsheet call is fenced. A breaker-OPEN
# read → INFO skip (an outage is not a rotation verdict); any other
# SmartsheetError → WARN, never a raise. Deletion failures stop the batch
# loop and report a partial rotation — the next run continues.
#
# §43 (successor-operator): symptom = daily watchdog WARN "row-cap" naming a
# sheet (a note prefixed "STORM-MODE" means the 90d retention was overridden
# — the rotation deleted terminal rows older than the 48h storm floor; no
# action needed, but it signals an active error storm worth investigating),
# or CRITICAL "nothing deletable". Low-class repair = none needed for WARN
# (rotation is automatic; verify next-day count fell). For the CRITICAL —
# which now means even the 48h storm floor found nothing: the sheet is full
# of rows the rotation refuses to touch (open CRITICALs / un-drained queue
# rows / rows with unprovable dates, all inside the storm floor or
# protected) — resolve/drain them in Smartsheet (stamp `Resolved At` on
# stale CRITICALs after review; drain queue rows), then re-run
# `python3 scripts/watchdog.py --dry` to preview. Escalate to Seth if the
# CRITICAL persists after draining (retention/threshold/floor change = a
# code change, high-class).


# ITS_Errors terminality + age parsing now live in shared/errors_rotation.py — the
# SINGLE SOURCE OF TRUTH shared with the dashboard clear-error-log verb
# (operator_dashboard/act/errors_ops.py), so the two can never drift. Thin private
# aliases keep the call sites below + the Check O rationale block above unchanged.
_errors_row_is_terminal = errors_row_is_terminal
_row_age_date = row_age_date


def _review_queue_row_is_terminal(row: dict[str, Any]) -> bool:
    """ITS_Review_Queue terminality — drained/reviewed only, NEVER PENDING /
    IN_REVIEW / ESCALATED (see the Check O rationale block above)."""
    status = str(row.get("Status") or "").strip()
    return status in {"APPROVED", "REJECTED"}


# The per-sheet rotation policy table: (label, sheet id, terminal-predicate,
# date-column title). Predicates take the get_rows row dict.
_ROTATION_POLICIES: list[tuple[str, int, Callable[[dict[str, Any]], bool], str]] = [
    ("ITS_Errors", sheet_ids.SHEET_ERRORS, _errors_row_is_terminal, "Timestamp"),
    ("ITS_Review_Queue", sheet_ids.SHEET_REVIEW_QUEUE, _review_queue_row_is_terminal, "Created At"),
]

_SEVERITY_ORDER = {Severity.INFO: 0, Severity.WARN: 1, Severity.CRITICAL: 2}


def _select_rotation_eligible(
    rows: list[dict[str, Any]],
    is_terminal: Callable[[dict[str, Any]], bool],
    date_column: str,
    retention_days: int,
) -> list[tuple[date, int]]:
    """TERMINAL rows strictly older than the retention cutoff, oldest-first.

    The single eligibility filter for BOTH the normal 90d pass and the
    storm-mode re-select — the invariants live here so the storm floor can
    never widen them: non-terminal rows (open CRITICALs, un-drained queue
    rows) and rows with a missing/unparseable date are NEVER eligible, at
    any retention. Sorted (date, row id) for a stable oldest-first order.
    """
    cutoff = date.today() - timedelta(days=retention_days)
    eligible: list[tuple[date, int]] = []
    for row in rows:
        row_id = row.get("_row_id")
        if not isinstance(row_id, int):
            continue
        if not is_terminal(row):
            continue
        aged = _row_age_date(row, date_column)
        if aged is None or aged >= cutoff:
            continue
        eligible.append((aged, row_id))
    eligible.sort()
    return eligible


def _rotate_one_sheet(
    label: str,
    sheet_id: int,
    is_terminal: Callable[[dict[str, Any]], bool],
    date_column: str,
    *,
    dry_run: bool,
) -> tuple[Severity, str]:
    """Count one sheet and rotate if over the mark. Returns (severity, note).

    Never raises: breaker-OPEN → INFO skip; other SmartsheetError → WARN.
    """
    try:
        rows = smartsheet_client.get_rows(sheet_id)
    except smartsheet_client.SmartsheetCircuitOpenError:
        return (Severity.INFO, f"{label}: breaker OPEN — count skipped, no rotation verdict")
    except smartsheet_client.SmartsheetError as e:
        return (Severity.WARN, f"{label}: row-count read failed ({e!r}) — no rotation this run")

    count = len(rows)
    if count < defaults.SHEET_ROW_WARN_THRESHOLD:
        return (
            Severity.INFO,
            f"{label}: {count} rows (< {defaults.SHEET_ROW_WARN_THRESHOLD} warn mark)",
        )
    if count < defaults.SHEET_ROW_ROTATE_THRESHOLD:
        return (
            Severity.WARN,
            f"{label}: {count} rows past the {defaults.SHEET_ROW_WARN_THRESHOLD} warn mark "
            f"(rotate at {defaults.SHEET_ROW_ROTATE_THRESHOLD}; hard cap "
            f"{defaults.SHEET_ROW_HARD_CAP})",
        )

    # Over the rotate mark — select eligible rows: TERMINAL and older than
    # the retention window, oldest first (date, then row id for a stable order).
    eligible = _select_rotation_eligible(
        rows, is_terminal, date_column, defaults.SHEET_ROW_ROTATION_RETENTION_DAYS
    )
    storm_mode = False
    if not eligible:
        # STORM-MODE fallback (2026-07-13 incident — see the rationale block
        # above): the 90d retention found nothing, which during the system's
        # first 90 days is STRUCTURAL (retention > system age), so re-select
        # with the 48h storm floor. Same invariants — only TERMINAL rows with
        # a provable age are ever deleted, at any floor.
        eligible = _select_rotation_eligible(
            rows, is_terminal, date_column, defaults.SHEET_ROW_STORM_FLOOR_DAYS
        )
        storm_mode = bool(eligible)

    if not eligible:
        return (
            Severity.CRITICAL,
            f"{label}: {count} rows over rotate mark {defaults.SHEET_ROW_ROTATE_THRESHOLD} but "
            f"NOTHING is deletable (no terminal rows older than the "
            f"{defaults.SHEET_ROW_ROTATION_RETENTION_DAYS}d retention, and none older than the "
            f"{defaults.SHEET_ROW_STORM_FLOOR_DAYS}d storm floor either — the sheet is open "
            f"CRITICALs / un-drained queue rows / unprovable dates) — resolve/drain rows or "
            f"escalate; hard cap {defaults.SHEET_ROW_HARD_CAP} approaching",
        )

    effective_retention_days = (
        defaults.SHEET_ROW_STORM_FLOOR_DAYS
        if storm_mode
        else defaults.SHEET_ROW_ROTATION_RETENTION_DAYS
    )
    storm_prefix = (
        f"STORM-MODE — {defaults.SHEET_ROW_ROTATION_RETENTION_DAYS}d retention OVERRIDDEN "
        f"(nothing was age-eligible; retention exceeds the rows' age — error storm), "
        f"falling back to the {defaults.SHEET_ROW_STORM_FLOOR_DAYS}d storm floor: "
        if storm_mode
        else ""
    )

    batch_size = defaults.SHEET_ROW_ROTATION_DELETE_BATCH
    per_run_cap = batch_size * defaults.SHEET_ROW_ROTATION_MAX_BATCHES_PER_RUN
    to_delete = [row_id for _, row_id in eligible[:per_run_cap]]

    if dry_run:
        return (
            Severity.WARN,
            f"{label}: DRY RUN — {storm_prefix}{count} rows over rotate mark; would delete "
            f"{len(to_delete)} of {len(eligible)} eligible terminal rows "
            f"(oldest-first, batches of {batch_size})",
        )

    deleted = 0
    delete_error = ""
    for start in range(0, len(to_delete), batch_size):
        chunk = to_delete[start : start + batch_size]
        try:
            smartsheet_client.delete_rows(sheet_id, chunk)
        except smartsheet_client.SmartsheetError as e:
            # Partial rotation: report what landed; next daily run continues.
            delete_error = f"; delete failed after {deleted} rows: {e!r}"
            break
        deleted += len(chunk)

    note = (
        f"{label}: {storm_prefix}rotated {deleted} of {len(eligible)} eligible terminal rows "
        f"(was {count} rows, rotate mark {defaults.SHEET_ROW_ROTATE_THRESHOLD}, "
        f"retention {effective_retention_days}d, oldest-first"
        f"{', per-run cap ' + str(per_run_cap) if len(eligible) > per_run_cap else ''})"
        f"{delete_error}"
    )
    # The never-silent rotation record: an explicit ITS_Errors row (WARN always
    # writes to Smartsheet) with a stable error_code, IN ADDITION to the
    # CheckResult line _run_check routes through log(). A rotation that deleted
    # nothing (first batch failed) is still recorded — it names the failure.
    log(Severity.WARN, _SCRIPT, f"[row-cap-rotation] {note}", error_code="row_cap_rotation")
    return (Severity.WARN, note)


def _check_row_cap_rotation(dry_run: bool = False) -> CheckResult:
    """Check O (A5): row-cap rotation for ITS_Errors + ITS_Review_Queue.

    See the rationale block above for the terminality definitions, the
    threshold mechanism, and the STORM-MODE fallback (2026-07-13: when the
    90d retention yields nothing, re-select at the 48h storm floor before
    declaring CRITICAL). `dry_run=True` (the `--dry` CLI flag) previews the
    rotation — counts + would-delete numbers, including whether storm-mode
    would engage, zero deletes, zero rotation records. Rotation itself
    proceeds during MAINTENANCE (it is a safety measure; only the *page* is
    deferred, by _run_check's downgrade).
    """
    severities: list[Severity] = []
    notes: list[str] = []
    for label, sheet_id, is_terminal, date_column in _ROTATION_POLICIES:
        severity, note = _rotate_one_sheet(
            label, sheet_id, is_terminal, date_column, dry_run=dry_run
        )
        severities.append(severity)
        notes.append(note)

    worst = max(severities, key=lambda s: _SEVERITY_ORDER[s])
    if worst is Severity.INFO:
        summary = "Row caps healthy for ITS_Errors + ITS_Review_Queue."
    elif worst is Severity.CRITICAL:
        summary = (
            "Row-cap rotation BLOCKED — sheet(s) over rotate mark with nothing "
            "deletable, even at the storm floor."
        )
    else:
        summary = "Row-cap threshold crossed — see per-sheet rotation notes."
    return CheckResult(severity=worst, summary=summary, details=" | ".join(notes))


# ---- Check W: ~/its/logs directory-growth bound (archive, never delete) -----
#
# Growth Slice 2. Slice 1 cut the log SOURCES; Check W BOUNDS the on-disk retention
# using the pure `shared/log_rotation.py` engine. It is an ARCHIVE bound, NOT a cleanup:
# DAILY logs older than DAILY_GZIP_AGE_DAYS are gzip'd IN PLACE (the .log removed only
# after a verified .gz sibling exists), and launchd `.out.log` files are copy-gz-TRUNCATED
# in place (never renamed / unlinked — launchd's O_APPEND child fd follows the inode, and
# there is no SIGHUP handler, so a rename strands the live file at 0 bytes forever). The
# one irreversible op — deletion of a forensic file — ships in a SEPARATE later slice.

# Sustained-failure escalation for Check W. Routes through the CAPPED ladder
# `sustained_failure.is_escalation_cycle` (F2, 2026-07-21), NOT `has_crossed_threshold`.
# The prior code used `has_crossed_threshold`, which is True for EVERY cycle at/past the
# threshold — so a persistently-wedged pruner would mint one NEW open-CRITICAL ITS_Errors
# row EVERY daily run, forever. An open CRITICAL is never terminal (`shared/errors_rotation`
# returns False without a "Resolved At" stamp), so those rows are UNROTATABLE at every floor
# including Check O's storm floor — exactly the 20k-cap lockout the 2026-07-21 error-triage
# work exists to prevent (ITS_Errors hit 19,975/20,000 on 2026-07-13). `is_escalation_cycle`
# fires on the CROSSING cycle then at 2×/4×/8× the threshold, capping the step at
# threshold × LADDER_MAX_MULTIPLIER (8) — i.e. days 3, 6, 12, 24, then every 24 days — so a
# long unattended wedge re-notifies on a bounded ladder while the between-rung cycles still
# write their per-occurrence row at Severity.WARN (terminal, reclaimable). A real sustained
# outage still escalates to CRITICAL; nothing is silent.
#
# LADDER_EXEMPT / test_transient_fence.py: watchdog stays in LADDER_EXEMPT (it is NOT a
# 90-120 s daemon and its call is not a raw `n >= …CRITICAL_THRESHOLD` compare). The exempt
# set only gates `test_no_daemon_hand_rolls_the_threshold_compare_again`. The SECOND fence
# test (`test_every_escalation_site_routes_through_the_shared_helper`) enumerates every module
# that CALLS `is_escalation_cycle` and asserts it equals LADDER_CONSUMERS — so this file must
# be enrolled in LADDER_CONSUMERS in the same landing (test-owned change).
_LOG_ROTATION_FAILS = sustained_failure.SustainedFailureCounter(
    STATE_DIR / "log_dir_rotation_failures.json",
    _SCRIPT,
    "log_dir_rotation_counter_failed",
)


def _rotate_log_dir(*, skip_launchd: bool, dry_run: bool) -> log_rotation.RotationOutcome:
    """DELEGATE to the single `log_rotation.run_log_rotation` orchestrator (F6).

    Formerly this re-implemented the scan/plan/archive loop so it could express the
    watchdog-only "hold the whole launchd lane during an open-CRITICAL incident" policy,
    which the engine had no way to say. That duplication was a footgun: the engine's
    exported `run_log_rotation` lacked the incident gate, so a future caller wiring the
    "public" entry point would truncate launchd files mid-incident. `run_log_rotation`
    now takes `skip_launchd`, so incident-safety lives in the ONE loop and this fn is a
    thin delegator — the check still OWNS the decision (`skip_launchd = "open CRITICALs
    exist"`, computed in `_check_log_dir_rotation`) and merely passes it down.

    Kept as a named seam (not inlined) because `tests/test_watchdog.py` patches
    `watchdog._rotate_log_dir` to inject synthetic outcomes into the check.
    """
    return log_rotation.run_log_rotation(skip_launchd=skip_launchd, dry_run=dry_run)


def _check_log_dir_rotation(
    dry_run: bool = False, *, alerts_suppressed: bool = False
) -> CheckResult:
    """Check W (growth Slice 2): BOUND ~/its/logs growth by ARCHIVING — never deleting.

    gzip aged DAILY logs in place and copy-gz-truncate launchd `.out.log`, via the single
    `log_rotation.run_log_rotation` orchestrator (`_rotate_log_dir` delegates to it, F6).
    Returns a CheckResult, so `_run_check` pages / MAINTENANCE-defers it like every other
    check. `dry_run=True` (the `--dry` CLI flag) previews the would-gzip / would-truncate
    counts with ZERO writes.

    `alerts_suppressed` (F3): threaded by `_run_check` via signature inspection (like Check
    G / I / J / K). The sustained-failure CRITICAL is logged INLINE here (bypassing
    `_run_check`'s WARN/CRITICAL→INFO MAINTENANCE downgrade), so during a maintenance window
    it would page unless we opt out — under `alerts_suppressed` it RECORDS the CRITICAL to
    ITS_Errors with `alert=False` (no push legs); the still-open row resurfaces via Check B
    post-MAINTENANCE. Outside MAINTENANCE it pages as before.

    SEVERITY (deliberate refinement of Check O's always-WARN): a ROUTINE, fully-completed run
    returns INFO — INFO does not reach ITS_Errors (unless ITS_ERROR_LOG_INFO=1), so a check
    that truncates `.out.log` EVERY daily run does not add rows forever to the very sheet whose
    20k cap has locked out twice. WARN only on an ABNORMAL outcome (a per-run cap / deadline
    partial, an oversized-file skip, the launchd lane held for incident-safety, or a
    gzip-verify / truncate FAILURE).
    """
    # Incident gate: hold back the ENTIRE launchd truncate pass while the operator is likely
    # mid-incident (any open CRITICAL) — they may be tailing a `.out.log`. Reuse Check B's
    # open-CRITICAL reader (`_open_critical_rows`) so the "am I on fire?" query lives in ONE
    # place. FAIL-SAFE: if the signal itself is unreadable, ASSUME an incident (hold launchd;
    # the .gz daily lane is always safe) rather than truncating blind.
    try:
        skip_launchd = bool(_open_critical_rows())
    except Exception:  # noqa: BLE001 — fail-safe: unreadable "am I on fire?" ⇒ hold launchd
        skip_launchd = True

    try:
        outcome: log_rotation.RotationOutcome | None = _rotate_log_dir(
            skip_launchd=skip_launchd, dry_run=dry_run
        )
        run_error: str | None = None
    except Exception as exc:  # noqa: BLE001 — self-limit: a check must never raise out
        outcome = None
        run_error = repr(exc)

    # A FAILURE cycle = the pruner itself erroring (a per-file gzip-verify / truncate failure
    # or an internal exception), NOT a merely-partial run (cap / deadline / incident-hold) —
    # a partial run means the pruner worked and simply left work for the next run.
    had_failure = run_error is not None or (outcome is not None and outcome.had_errors)
    abnormal = had_failure or (
        outcome is not None
        and bool(
            outcome.deadline_hit
            or outcome.daily_pending
            or outcome.launchd_pending
            or outcome.oversized_skipped  # F4: a runaway log skipped over the size cap
        )
    )

    # F5: when the ONLY abnormality is the open-CRITICAL launchd HOLD — no real per-file
    # failure, no deadline, no cap-driven daily overflow, no oversized skip, and the launchd
    # deferral is entirely the incident hold (which sets launchd_pending == every eligible
    # launchd file) — keep the CheckResult WARN for dashboard visibility but do NOT write the
    # SECOND, explicit `log_dir_rotation` row and do NOT touch the failure streak. This removes
    # the DUPLICATE per-run row; the single CheckResult WARN still lands one row/run via
    # _run_check, but it is a WARN (non-CRITICAL) — TERMINAL and rotatable by Check O — so it
    # can never drive the 20k lockout the way a never-terminal open CRITICAL does. The point is
    # to not DOUBLE-log while Check B is already surfacing the open CRITICAL that caused the hold.
    incident_hold_only = bool(
        skip_launchd
        and not had_failure
        and outcome is not None
        and not outcome.deadline_hit
        and not outcome.daily_pending
        and not outcome.oversized_skipped
        and outcome.launchd_pending
    )

    # Human note — the engine's note() already carries the "[log-dir-rotation] " prefix; a
    # None outcome means the compose itself raised.
    if outcome is not None:
        note = outcome.note()
    else:
        note = f"[log-dir-rotation] internal error, no work done: {run_error}"
    if skip_launchd and outcome is not None and outcome.launchd_pending:
        note += (
            " | launchd lane HELD (incident-safety: an open CRITICAL is present, or the "
            "open-CRITICAL signal was unreadable) — deferred to the next clean run"
        )

    if dry_run:
        # Operator preview (--dry): ZERO writes — no explicit record, no counter I/O, no
        # truncate, no .gz. Just the would-do numbers. Mirrors Check O's --dry preview.
        return CheckResult(
            severity=Severity.WARN if abnormal else Severity.INFO,
            summary="Log-dir rotation preview (--dry): no writes performed.",
            details=note,
        )

    # Sustained-failure escalation (see the `_LOG_ROTATION_FAILS` comment above). Increment on
    # a real failure cycle; RESET on any non-failure cycle (a cap / deadline / oversized / hold
    # partial IS a success — the pruner did not error). A state glitch degrades record() to
    # 1 + WARN, which is below threshold, so it never false-pages. F2: the escalate decision
    # routes through the CAPPED ladder `is_escalation_cycle` (fires day 3, 6, 12, 24, then every
    # 24), NOT `has_crossed_threshold` (which fired every day past 3 and minted a NEW unrotatable
    # open-CRITICAL row daily). The failure streak is NOT touched on an incident-hold-only cycle
    # (F5) — that path takes the else branch here anyway (had_failure is False), a harmless reset.
    escalate = False
    if had_failure:
        n = _LOG_ROTATION_FAILS.record()
        escalate = sustained_failure.is_escalation_cycle(
            n, defaults.LOG_DIR_ROTATION_CRITICAL_THRESHOLD
        )
    else:
        _LOG_ROTATION_FAILS.reset()

    # NEVER-SILENT record: on an ABNORMAL cycle write an explicit ITS_Errors row OUTSIDE the
    # CheckResult, carrying the stable `log_dir_rotation` error_code — exactly as Check O does
    # at `_rotate_one_sheet` — because `_run_check` downgrades a WARN/CRITICAL CheckResult to
    # INFO during MAINTENANCE and INFO never reaches Smartsheet, so a CheckResult-only record
    # would vanish precisely when an operator is watching. A ROUTINE (fully-completed, no-defer)
    # cycle — AND an incident-hold-only cycle (F5) — writes NOTHING here, which keeps Check W
    # from adding a row every day to the 20k-capped ITS_Errors sheet. On a sustained FAILURE
    # (`escalate`) the row is CRITICAL, so error_log's push legs wake someone — EXCEPT during
    # MAINTENANCE (F3: `alert=False` records the CRITICAL without paging; Check B resurfaces the
    # still-open row post-window).
    if abnormal and not incident_hold_only:
        if had_failure:
            log(
                Severity.CRITICAL if escalate else Severity.WARN,
                _SCRIPT,
                f"{note} | pruner FAILURE"
                + (
                    f" — {defaults.LOG_DIR_ROTATION_CRITICAL_THRESHOLD}+ consecutive daily "
                    "runs, ESCALATED to CRITICAL on the capped re-notify ladder "
                    "(pruner appears wedged)"
                    if escalate
                    else " this run (retries next run)"
                ),
                error_code="log_dir_rotation",
                alert=not alerts_suppressed,  # F3: record-only during MAINTENANCE, no page
            )
        else:
            log(Severity.WARN, _SCRIPT, note, error_code="log_dir_rotation")

    # CheckResult routed by `_run_check` (MAINTENANCE-downgradeable). Routine = INFO; abnormal =
    # WARN. A sustained FAILURE already pages via the explicit CRITICAL above, so the CheckResult
    # itself stays WARN and never double-pages.
    if not abnormal:
        return CheckResult(
            severity=Severity.INFO,
            summary="~/its/logs growth bound healthy — archived, nothing to escalate.",
            details=note,
        )
    return CheckResult(
        severity=Severity.WARN,
        summary="~/its/logs rotation abnormal — partial / deferred / failed (see note).",
        details=note,
    )


# ---- Entrypoint ---------------------------------------------------------


CHECKS: list[Callable[..., CheckResult]] = [
    _check_stale_review_queue,
    _check_open_criticals,
    _check_scheduled_jobs,
    _check_reviewer_chain_forward,
    # _check_mail_intake_silent_disable RETIRED 2026-06-05 (safety email intake retired).
    _check_alert_dedupe_summaries,
    # Check I runs after Check C (above): Check C reports staleness; Check I
    # recovers the one daemon launchd can't self-recover (weekly_generate,
    # calendar-scheduled). It fires generation inline, so — like Check G —
    # it takes alerts_suppressed (threaded by _run_check) to defer the
    # operator page during MAINTENANCE.
    _check_weekly_generate_catchup,
    # Check I (progress, P5): the progress twin — recovers a missed progress_weekly_generate
    # Friday 14:30 calendar run (the one progress daemon launchd can't self-recover). Re-fires
    # generate_core.run_generate(PROGRESS_GENERATE_CONFIG) directly (send-free, AI-free,
    # MAINTENANCE-runnable); takes alerts_suppressed like the safety catch-up.
    _check_progress_generate_catchup,
    # Check J / K (F08/F09 PR 2): prolonged-open page + guaranteed cap-window
    # summary sweep. Both fire alerts inline (J via _alert_critical, K via
    # _maybe_fire_window_summary's _send_exempt_alert), so — like Check G / I —
    # they take alerts_suppressed (threaded by _run_check) to defer the operator
    # page during MAINTENANCE.
    _check_circuit_breaker_prolonged_open,
    _check_alert_rate_cap_window,
    # Check L (B2): token write-capability probe. Returns a CheckResult, so its
    # CRITICAL is paged + MAINTENANCE-deferred by _run_check (no inline alert).
    _check_token_write_capability,
    # Check M (C3): blueprint .claude guard-symlink resolution. Returns a
    # CheckResult (no inline alert); WARN-only if the symlinks dangle.
    _check_blueprint_guard_symlinks,
    # Check N: WSR rows stuck in SENDING (weekly_send write-ahead-marker safety net).
    # Read-only; returns a CheckResult (no inline alert); WARN-only.
    _check_stuck_wsr_send,
    # Check O (A5, growth Slice 1): ITS_Errors + ITS_Review_Queue row-cap
    # rotation. Registered before the sheet-consuming letter-neighbors purely
    # by letter order; rotation deletes TERMINAL rows only (never open
    # CRITICALs / PENDING queue rows) and writes a rotation record to
    # ITS_Errors. Returns a CheckResult (WARN on rotation/approach — incl.
    # the STORM-MODE 48h-floor fallback when the 90d retention yields
    # nothing, 2026-07-13 incident; CRITICAL only when even the storm floor
    # finds nothing deletable) — paged + MAINTENANCE-deferred by _run_check.
    _check_row_cap_rotation,
    # Check P (A3): Box OAuth refresh-token freshness. Read-only marker read;
    # returns a CheckResult, so its WARN/CRITICAL is paged + MAINTENANCE-deferred
    # by _run_check. (Check O is the A5 row-cap rotation above.)
    _check_box_token_freshness,
    # Check Q / R (A4): portal_poll resilience. Q re-raises a sustained pending-fetch outage
    # (CRITICAL second-opinion to portal_poll's inline page); R WARNs on a stuck unfiled
    # backlog (saturated page draining nothing). Both return a CheckResult (no inline alert),
    # so _run_check pages/MAINTENANCE-defers them normally. (H was never built.)
    _check_portal_poll_fetch_outage,
    _check_portal_poll_pending_backlog,
    # Check S: origin/main required CI (ci.yml: test/portal/secrets) green on the latest
    # commit — the mechanical four-part-verify step 4 (forensic class #13, partial-PR-landed).
    # Returns a CheckResult; CRITICAL paged + MAINTENANCE-deferred by _run_check. Fail-safe
    # to INFO on any gh/network/parse failure.
    _check_main_branch_ci_green,
    # Check T (P5): WSR + WPR rows stuck HELD past HELD_ROW_STALE_AFTER — the daily catch-all
    # backstop for every HELD reason (recipient_health is the hourly send-time signal for the
    # no-recipient subset). Read-only + first-seen state file; returns a CheckResult (WARN-only).
    _check_stale_held_rows,
    # Check U (P5): F22 send-approver-set health per send workspace — EMPTY (sends blocked,
    # §46 re-share) or CHANGED-since-baseline (who-may-approve drift). Read-only + baseline
    # state file; returns a CheckResult (WARN-only).
    _check_approver_drift,
    # Check V (GS2): D1 prune heartbeat via GET /api/internal/prune-status. Read-only;
    # returns a CheckResult (no inline alert), so its WARN/CRITICAL is paged +
    # MAINTENANCE-deferred by _run_check. CRITICAL on failed prune stages or D1 size
    # over 6 GB; WARN on >48h-stale last run / absent meta row; fail-soft INFO on
    # unresolved creds or transient transport. (O is reserved for the A5 row-cap
    # rotation; H was never built; V is the first free letter after U.)
    _check_portal_prune_health,
    # Check W (growth Slice 2): ~/its/logs directory-growth BOUND — gzip aged daily logs in
    # place + copy-gz-TRUNCATE launchd .out.log via the pure shared/log_rotation.py engine.
    # It ARCHIVES, it does NOT delete (the one irreversible op ships in a later slice). Returns
    # a CheckResult, so _run_check pages / MAINTENANCE-defers it. Registered LAST on purpose:
    # it is the only check that walks the filesystem + gzips, so a self-limited overrun (its own
    # cap + monotonic deadline) can never delay an ALERTING check ahead of it. Routine runs are
    # INFO (no ITS_Errors row); only an abnormal partial / deferred / failed / oversized run is
    # WARN, with a sustained failure escalating to CRITICAL via the CAPPED is_escalation_cycle
    # ladder (F2 — days 3/6/12/24 then every 24, so a wedge never mints a NEW unrotatable
    # open-CRITICAL every day — see the _LOG_ROTATION_FAILS note).
    _check_log_dir_rotation,
    # Check E (Anthropic spend trend) deferred to a follow-on PR (the
    # Check E shipping PR) — requires an Admin API key (sk-ant-admin01-...
    # prefix) provisioned in Keychain under ITS_ANTHROPIC_ADMIN_API_KEY.
    # The current key is a workspace key (sk-ant-api03-...) which
    # /v1/organizations/cost_report rejects with 401. See
    # docs/session_logs/2026-05-20_watchdog_session_2.md for the
    # pre-flight finding.
]

# Check letter per registered check fn — the sweep-results vocabulary the
# operator dashboard's sweep panel displays (and the system map badges by).
# Registry-reconciliation contract (HOUSE_REFLEXES §1): registering a new
# check in CHECKS adds its letter HERE in the same PR;
# tests/test_watchdog.py::test_check_letters_cover_every_registered_check
# enforces the parity. Check I appears twice deliberately (safety + progress
# catch-up wrappers share the letter).
CHECK_LETTERS: dict[str, str] = {
    "_check_stale_review_queue": "A",
    "_check_open_criticals": "B",
    "_check_scheduled_jobs": "C",
    "_check_reviewer_chain_forward": "D",
    "_check_alert_dedupe_summaries": "G",
    "_check_weekly_generate_catchup": "I",
    "_check_progress_generate_catchup": "I",
    "_check_circuit_breaker_prolonged_open": "J",
    "_check_alert_rate_cap_window": "K",
    "_check_token_write_capability": "L",
    "_check_blueprint_guard_symlinks": "M",
    "_check_stuck_wsr_send": "N",
    "_check_row_cap_rotation": "O",
    "_check_box_token_freshness": "P",
    "_check_portal_poll_fetch_outage": "Q",
    "_check_portal_poll_pending_backlog": "R",
    "_check_main_branch_ci_green": "S",
    "_check_stale_held_rows": "T",
    "_check_approver_drift": "U",
    "_check_portal_prune_health": "V",
    "_check_log_dir_rotation": "W",
}


# ---- Cadence tiers (2026-08-07) ----------------------------------------------
#
# The watchdog moved from a once-daily 07:00 calendar fire to an HOURLY StartInterval after a
# 12.7-hour Smartsheet outage on 2026-08-06 (breaker OPEN 729 min, 361 short-circuits across the
# fleet — "approved sends are FROZEN and nothing is being filed") went completely UNPAGED: Check J
# WARNed at 450s on the 11:00Z sweep and nothing looked again for twelve hours. The watchdog is the
# fleet's only cross-daemon observer, so its cadence IS the outage-detection floor.
#
# Running EVERY check hourly is not the fix — these are actively harmful at that cadence and stay
# on the daily tier:
#
#   D  _check_reviewer_chain_forward  no cross-run dedupe (see its docstring) -> a standing gap
#                                     writes 24 ITS_Review_Queue rows/day. A 14-day FORWARD PTO
#                                     scan has no hourly-resolution value anyway.
#   G  _check_alert_dedupe_summaries  semantically fine hourly — its two-phase state machine emits
#                                     exactly one summary per entry regardless of cadence, and
#                                     hourly would cut summary latency 24h -> 1h. But its phase-1
#                                     send currently fails on the Resend 403 WITHOUT marking the
#                                     entry summarized, so it retries forever: each stuck key would
#                                     pump 24 WARN rows/day. PROMOTE TO HOURLY once the Resend leg
#                                     is fixed — that is the only reason G is here.
#   I  _check_*_generate_catchup      a persistently failing catch-up re-fires a FULL weekly compile
#                                     per run: 3 attempts across the 3-day window becomes ~72, each
#                                     minting an open CRITICAL. Open CRITICALs are never terminal,
#                                     so they are unrotatable at every floor INCLUDING the storm
#                                     floor — the 20k-row lockout mechanism.
#   L  _check_token_write_capability  creates AND deletes a real Smartsheet sheet per run; its own
#                                     docstring prices this as "one create + one delete per daily
#                                     watchdog run (negligible)", which does not survive 24x/day.
#   O  _check_row_cap_rotation        in the 15k-16k WARN band it writes one ITS_Errors row per
#                                     sweep — into the very sheet whose row count it is warning
#                                     about. A positive feedback loop.
#   U  _check_approver_drift          absorbs the change into its baseline every run, so the drift
#                                     WARN's visibility window shrinks 24h -> 1h on a
#                                     security-relevant signal (who may approve a send).
#   W  _check_log_dir_rotation        its launchd lane truncates UNCONDITIONALLY by design (no
#                                     per-file mtime skip — an always-on daemon always looks
#                                     recent, F1) and that lane NEVER deletes, so hourly would mint
#                                     ~24 permanent .gz archives per launchd log per day —
#                                     inverting the very growth bound Check W exists to enforce.
#
# Keeping W on the daily tier ALSO preserves two cadence-coupled constants for free, so neither
# needs touching: LOG_DIR_ROTATION_CRITICAL_THRESHOLD (3) keeps meaning three DAYS rather than
# three hours, and LOG_ROTATION_TEMP_ORPHAN_AGE_SECONDS (3600.0) cannot collide with a 3600s sweep
# interval and reap a temp file the CURRENT run is mid-write on.
#
# This is a FILTER, deliberately NOT a wrapper: `CHECKS` keeps its exact identity and order,
# because tests/test_watchdog.py asserts the registry by function identity (plus four more tests
# asserting membership / index position). Wrapping any check would break all five and would
# decouple the registry from CHECK_LETTERS parity.
DAILY_ONLY_CHECKS: frozenset[Callable[..., CheckResult]] = frozenset(
    {
        _check_reviewer_chain_forward,
        _check_alert_dedupe_summaries,
        _check_weekly_generate_catchup,
        _check_progress_generate_catchup,
        _check_token_write_capability,
        _check_row_cap_rotation,
        _check_approver_drift,
        _check_log_dir_rotation,
    }
)

# The daily tier is due when its own marker is older than this. Deliberately BELOW 24h: a 24h
# window plus run-to-run jitter would eventually land just under the threshold and defer the tier
# by a whole day.
DAILY_TIER_WINDOW = timedelta(hours=20)
DAILY_TIER_JOB = "watchdog_daily"


def _daily_tier_due(now: datetime | None = None) -> bool:
    """True when the DAILY_ONLY_CHECKS tier should run in THIS sweep.

    Gated on the age of the `watchdog_daily.last_run` marker rather than on a wall-clock hour
    (e.g. `hour == 7`): a laptop asleep at the designated hour must not skip the daily tier for a
    whole day — precisely the "nobody looked" failure mode this whole change exists to remove.

    FAIL-OPEN by design: an absent, unreadable, or unparseable marker RUNS the tier. A redundant
    daily pass is cheap; a silently-skipped one is the bug.
    """
    marker = WATCHDOG_MARKER_DIR / f"{DAILY_TIER_JOB}.last_run"
    try:
        raw = marker.read_text().strip()
    except OSError:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if last.tzinfo is None:  # tolerate a naive stamp from an older writer
        last = last.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) - last >= DAILY_TIER_WINDOW


# Per-sweep results file (operator-dashboard sweep panel's read-only source).
# Written once per run via shared.state_io (atomic; single writer, whole-file
# readers — no lock needed).
WATCHDOG_RESULTS_PATH = Path.home() / "its" / "state" / "watchdog_results.json"


@its_error_log(_SCRIPT)
def main() -> None:
    state = check_system_state()
    if state == SystemState.PAUSED:
        log(Severity.INFO, _SCRIPT, "PAUSED — skipping all checks")
        return
    alerts_suppressed = state == SystemState.MAINTENANCE
    if alerts_suppressed:
        log(
            Severity.INFO,
            _SCRIPT,
            "MAINTENANCE — checks will run but alerts suppressed",
        )
    # #336 startup observability: resolve+log every runtime ITS_Config key the checks + the
    # heartbeat beacon read, with its source (a MISSING declared row WARNs distinctly). Placed
    # after the PAUSED early-return above (a paused watchdog never logs) and fail-open — never
    # blocks the checks. The watchdog has no @require_active; the manual PAUSED guard above is
    # the equivalent gate.
    resolve_and_log(_SCRIPT, REQUIRED_CONFIG)

    # Cadence tiering (see DAILY_ONLY_CHECKS): every sweep runs the hourly tier; the daily-only
    # tier runs when its own marker has aged past DAILY_TIER_WINDOW. A deferred check still
    # records a sweep row so the dashboard's sweep panel shows it as "deferred", never as a
    # check that silently vanished from the registry.
    daily_due = _daily_tier_due()
    if not daily_due:
        log(
            Severity.INFO,
            _SCRIPT,
            f"hourly sweep — {len(DAILY_ONLY_CHECKS)} daily-tier check(s) deferred to the "
            f"next pass due ≥{int(DAILY_TIER_WINDOW.total_seconds() // 3600)}h after the last",
        )

    sweep_records: list[dict[str, str]] = []
    for check in CHECKS:
        if not daily_due and check in DAILY_ONLY_CHECKS:
            sweep_records.append(
                {
                    "check": check.__name__,
                    "letter": CHECK_LETTERS.get(check.__name__, "?"),
                    "severity": "INFO",
                    "summary": "deferred this sweep — daily tier (runs ~once per 24h)",
                }
            )
            continue
        sweep_records.append(_run_check(check, alerts_suppressed=alerts_suppressed))

    # Stamp the daily tier AFTER its checks ran. Deliberately unconditional on their outcomes:
    # every check is harness-isolated by _run_check, so a failing daily check must not pin the
    # tier "due" forever and re-run it every hour — the exact amplification this tiering prevents.
    if daily_due:
        write_last_run_marker(DAILY_TIER_JOB)

    # Persist the sweep's per-check outcomes for the operator dashboard's
    # "watchdog sweep" panel — the "did last night's sweep run, which letters
    # passed" surface (previously only inferable from ITS_Errors rows).
    # Best-effort: a write failure WARNs and must never block the retry
    # summary, the liveness marker, or the heartbeat beacon below.
    try:
        state_io.atomic_write_json(
            WATCHDOG_RESULTS_PATH,
            {
                "run_at": datetime.now(UTC).isoformat(),
                "alerts_suppressed": alerts_suppressed,
                "results": sweep_records,
            },
        )
    except Exception as exc:  # noqa: BLE001 — deliberate best-effort isolation
        log(Severity.WARN, _SCRIPT, f"watchdog_results write failed: {exc!r}")

    # D3: summarize any Smartsheet retries that SUCCEEDED across this run into ONE WARN
    # row. The watchdog issues more enrolled reads in a single pass than any daemon (20
    # checks, most of them sheet reads), so a chronically-flaky sheet shows up here first
    # — and without this the recoveries are invisible by construction (nothing raises,
    # nothing is logged). Placed after the checks and before the liveness marker so a
    # partial run still reports what it absorbed; best-effort inside, so it can never
    # disturb the marker or the heartbeat beacon below.
    sustained_failure.flush_retry_recovery(_SCRIPT)

    # Local Check-C freshness marker for the watchdog's own run. The
    # watchdog is deliberately NOT in TRACKED_JOBS (self-tracking has a
    # chicken-and-egg hole — a dead watchdog can't flag its own staleness),
    # so this marker is unconsumed today; it's written so adding the
    # watchdog to TRACKED_JOBS later is a one-line change. The external
    # "is the watchdog (and the host) alive" signal is the heartbeat ping
    # below (audit F16) — that's the real dead-man's switch.
    write_last_run_marker("watchdog")

    # External heartbeat beacon (audit F16). Read the configured ping URL
    # and notify the external monitor (UptimeRobot) that the watchdog —
    # and by proxy the whole host — is alive. This is the ONLY external
    # detector for total-host failure (crash, disk-full, launchd unload,
    # user logout); every in-tenant signal goes silent in that scenario
    # with nothing to raise the alarm. Fail-soft end-to-end: a read failure
    # WARNs and no-ops; a missing/placeholder URL no-ops (INFO); a ping
    # failure is swallowed+logged inside heartbeat_client.ping. The watchdog
    # must complete its real work regardless of monitor health.
    #
    # MAINTENANCE behavior: the ping fires on every non-PAUSED run,
    # INCLUDING MAINTENANCE — the host IS alive during a maintenance window,
    # so suppressing the ping would trip a false "host dead" alert on the
    # external monitor. (Alert *suppression* during MAINTENANCE applies to
    # the checks' own alerts, not to this liveness beacon.) PAUSED returns
    # above before the marker and ping — a deliberately-paused system does
    # not claim liveness.
    try:
        heartbeat_url = smartsheet_client.get_setting(
            "system.heartbeat_url", workstream="global"
        )
    except smartsheet_client.SmartsheetError as exc:
        log(Severity.WARN, _SCRIPT, f"heartbeat_url read failed: {exc!r}")
        heartbeat_url = None

    # Guard: skip the ping when unconfigured. A fork that hasn't provisioned
    # a monitor leaves the seeded placeholder in place — pinging it is a
    # guaranteed failure, so no-op (INFO) rather than WARN every run. This
    # token MUST stay equal to the seed Value in scripts/seed_its_config.py.
    if heartbeat_url and heartbeat_url != "PLACEHOLDER_uptimerobot_heartbeat_url":
        heartbeat_client.ping(heartbeat_url)
    else:
        log(
            Severity.INFO,
            _SCRIPT,
            "system.heartbeat_url not configured (missing or placeholder) "
            "— skipping heartbeat ping",
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ITS daily watchdog")
    parser.add_argument(
        "--dry",
        action="store_true",
        help=(
            "Preview the two destructive-retention checks ONLY — Check O (row-cap "
            "rotation) and Check W (log-dir rotation): print counts and would-delete / "
            "would-gzip / would-truncate numbers WITHOUT deleting rows, gzipping, "
            "truncating, writing rotation records, or running the other checks."
        ),
    )
    args = parser.parse_args()
    if args.dry:
        # Operator preview surface — runs the destructive-retention checks with all writes
        # disabled and prints each CheckResult instead of routing it through log() (no
        # records, no pages; the daily launchd run never passes --dry).
        for _result in (
            _check_row_cap_rotation(dry_run=True),
            _check_log_dir_rotation(dry_run=True),
        ):
            _line = f"{_result.severity.value}: {_result.summary}"
            if _result.details:
                _line += f" | {_result.details}"
            print(_line)
    else:
        main()
