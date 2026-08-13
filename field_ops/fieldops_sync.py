"""Field-Ops D1→Smartsheet job up-sync daemon (P2.5 Slice 5 — portal-as-writer).

Purpose
-------
The Mac-side mirror half of the job-tracker pivot ("D1 primary + dual Active-Jobs
mirror"). A job is created/edited/lifecycle-changed in the ITS Portal Job Tracker; the
Cloudflare Worker records it SEND-FREE in D1 (`origin='portal'`, `sync_state='pending'`)
and bumps a `mirror_version`. This launchd daemon pulls the dirty jobs and mirrors each UP
into BOTH ITS-owned Active-Jobs Smartsheets — the safety workspace's `ITS_Active_Jobs` and
the progress workspace's `ITS_Active_Jobs_Progress` — so those sheets become the downstream
source of truth every existing consumer reads. One writer ⇒ the two workstreams never drift
(§50 privileged code-actuation, §51 ITS-owned structured-SoR write-back).

Version-vector consistency (no cross-sheet 2-phase commit)
----------------------------------------------------------
D1 carries `mirror_version` + two watermarks (`safety_mirrored_version` /
`progress_mirrored_version`); a job is dirty when a watermark trails `mirror_version`. The
daemon writes each sheet independently (find-or-create by "Portal Job Key") and the commit
point is PER SHEET: after the safety upsert confirms it marks ONLY safety mirrored, THEN
attempts progress. A progress failure therefore leaves the job dirty with safety already
advanced; next cycle re-attempts both — safety's find-or-create no-ops on the existing row,
progress retries. The vector encodes exactly which sheet is behind (at-least-once,
idempotent effect; crash-safe).

Invariants
----------
- AI-FREE and customer-SEND-FREE (External Send Gate, FM Invariant 1): imports no
  `anthropic*` and no `graph_client.send_mail` / `resend` / `smtplib` / `email.mime`.
  Enrolled in tests/test_capability_gating.py GATED_SCRIPTS. The Smartsheet WRITE is the
  intended capability (SoR mirroring, NOT a customer send); the HTTP egress to OUR Worker
  goes through the F02-allowlisted `shared.portal_client`, so this module imports no raw
  network library.
- Kill-switch first (`@require_active`) + `@its_error_log` on the public entry. The runtime
  gate is `field_ops.fieldops_sync.sync_enabled` in ITS_Config (ARCH-1: the canonical gate,
  NOT a Daemon_Health checkbox). Ships **OFF** — the operator flips it at cutover, AFTER
  Slice 4's "Portal Job Key" column exists on BOTH sheets (else `add_rows` KeyErrors).
- Bearer privilege separation: authenticates with `ITS_PORTAL_FIELDOPS_TOKEN`
  (mirrors the Worker's `PORTAL_FIELDOPS_API_TOKEN`) — DISTINCT from portal_poll's internal
  token; neither can do the other's mutations.

Failure modes
-------------
- PAUSED / MAINTENANCE → `@require_active` exits cleanly.
- `sync_enabled=false` → short-circuit no-op (the shipped default; no log spam).
- Missing base URL or bearer → FAIL-CLOSED: do NOT sync; CRITICAL (won't self-heal) +
  ERROR heartbeat. 401 on pending-jobs → CRITICAL; other transport error → ERROR; both
  leave every job dirty for the next cycle.
- Per-job fence: `PicklistViolationError` / `SmartsheetValidationError` /
  `SmartsheetPermissionError` / `SmartsheetNotFoundError` (all permanent) → a
  `progress_reports` Review-Queue row (carrying the partial-commit state — which sheet failed and
  whether safety already mirrored), job left dirty. The last two were added 2026-08-13: a §46
  share change or a deleted tracker sheet used to fall to the transient arm and log "re-projects
  next cycle" forever — no ticket, no CRITICAL, no operator ever told, on a retry that could
  never succeed. Every Review-Queue write on this path now goes through `review_queue.safe_add`
  (never `add`): the ticket is best-effort, so a Review-Queue outage can no longer abort the
  cycle and skip the later mirror passes, the heartbeat and the watchdog marker.
  `PortalAuthError` on the mark-mirrored
  write-back (401, non-transient) → CRITICAL (`fieldops_mark_mirrored_unauthorized`, see runbook
  Symptom E), job left dirty; any other `SmartsheetError` / `PortalTransportError` (transient) →
  ERROR-logged, job left dirty. One bad job never kills the cycle.

Consumers
---------
- launchd `org.solutionsmith.its.fieldops-sync` (StartInterval; RunAtLoad).
- Watchdog Check C marker (`fieldops_sync`) + ITS_Daemon_Health row (via shared.heartbeat).
  The watchdog TRACKED_JOBS registration is deferred to the deploy session (the marker write
  here is forward-compatible and harmless if unregistered) — see docs/tech_debt.md.
"""
from __future__ import annotations

import fcntl
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from field_ops import job_archive
from progress_reports import (
    equipment_status,
    hours_log,
    material_incidents,
    material_list,
    material_receipts,
)
from shared import (
    active_jobs_writer,
    circuit_breaker,
    creds_resolution,
    error_log,
    keychain,
    picklist_validation,
    portal_client,
    review_queue,
    sheet_ids,
    smartsheet_client,
    state_io,
    sustained_failure,
)
from shared.creds_resolution import TransientUnavailable
from shared.error_log import Severity, its_error_log
from shared.heartbeat import HeartbeatReporter, HeartbeatStatus
from shared.kill_switch import require_active
from shared.required_config import ConfigKey, resolve_and_log

SCRIPT_NAME = "field_ops.fieldops_sync"
WORKSTREAM = "field_ops"

# ITS_Config keys.
CFG_SYNC_ENABLED = "field_ops.fieldops_sync.sync_enabled"
CFG_WORKER_BASE_URL = "safety_reports.portal.worker_base_url"  # shared with portal_poll
# The shared Worker base-URL key is owned by the safety_reports workstream (matches
# portal_poll) — read it there so no duplicate field_ops ITS_Config row is needed.
CFG_WORKER_BASE_URL_WORKSTREAM = "safety_reports"

# Keychain entry name (mirrors the Worker's PORTAL_FIELDOPS_API_TOKEN; the Mac-side name is
# distinct on purpose, and SEPARATE from portal_poll's ITS_PORTAL_INTERNAL_TOKEN).
KC_FIELDOPS_TOKEN = "ITS_PORTAL_FIELDOPS_TOKEN"  # noqa: S105 — Keychain entry NAME, not a secret

DEFAULT_SYNC_ENABLED = False  # ships OFF; operator flips it on at cutover (after Slice 4).
SYNC_INTERVAL_SECONDS = 90  # registration metadata; mirrors the launchd StartInterval (install.sh
#                             default 90 — the installed plist runs at 90s, not 300; wiring-audit M-3).

# P7 Slice 1 — the per-job Hours Log up-sync pass runs INSIDE this same daemon (one host, one
# lock, one heartbeat — no 4th daemon). Its OWN gate ships OFF so the pass is dark until the
# operator applies migration 0038 + deploys the Worker hours routes, then flips this on.
CFG_HOURS_ENABLED = "field_ops.fieldops_sync.hours_enabled"
DEFAULT_HOURS_ENABLED = False

# P7 Slice 2 — the per-job Equipment Status & Location snapshot pass runs INSIDE this same daemon
# (one host, one lock, one heartbeat). Its OWN gate ships OFF so the pass is dark until the operator
# deploys the Worker equipment-snapshot route, then flips this on. Unlike the hours pass this is a
# SNAPSHOT (re-projected each cycle) — no watermark, no mark-mirrored.
CFG_EQUIPMENT_ENABLED = "field_ops.fieldops_sync.equipment_enabled"
DEFAULT_EQUIPMENT_ENABLED = False

# P7 Material List (M2) — the per-job Material List snapshot pass runs INSIDE this same daemon (one
# host, one lock, one heartbeat). Its OWN gate ships OFF so the pass is dark until the operator
# applies migration 0039 + deploys the Worker material-list-snapshot route, then flips this on. Like
# the equipment pass this is a SNAPSHOT (re-projected each cycle) — no watermark, no mark-mirrored.
CFG_MATERIALS_ENABLED = "field_ops.fieldops_sync.materials_enabled"
DEFAULT_MATERIALS_ENABLED = False

# P7 Material Incidents (M3 Slice 2) — the per-job Material Incidents APPEND-ONLY ledger pass runs
# INSIDE this same daemon (one host, one lock, one heartbeat). Its OWN gate ships OFF so the pass is
# dark until the operator deploys the Worker material-incidents route, then flips this on. Unlike the
# other passes there is no migration prerequisite (a read-only endpoint over the existing submissions
# table). APPEND-ONLY: no watermark, no mark-mirrored, and NO retire (a filed incident is immutable).
CFG_INCIDENTS_ENABLED = "field_ops.fieldops_sync.incidents_enabled"
DEFAULT_INCIDENTS_ENABLED = False

# PR4 material RECEIPTS — the delivery-mark ledger (`material_receipt_events`, migration 0059) mirrored
# UP into the per-job Material Receipts sheet. Its own gate for the same reason each mirror has one:
# the passes are independent endpoints and an operator must be able to pause exactly one without
# darkening the others. APPEND-ONLY like incidents — no watermark, no mark-mirrored, no retire (a
# signed-for delivery is an immutable historical fact), so re-projection is the whole retry story.
#
# The gate ROW is seeded (at false) by `scripts/migrations/seed_daemon_gate_config.py` in the same
# change that adds this code, because a boolean gate read with `default=False` treats a MISSING row
# identically to `false` — so a capability that "ships dark" with no row at all leaves the operator
# hunting for a switch that does not exist (HOUSE_REFLEXES §5).
CFG_RECEIPTS_ENABLED = "field_ops.fieldops_sync.receipts_enabled"
DEFAULT_RECEIPTS_ENABLED = False

# Track 6 job archive — the pass that drains `/archive-pending` and relocates a closed job's eight
# containers across Smartsheet and Box. Its own gate, shipped OFF, and the reason it needs one is
# not caution about the code: turning this on is what makes an operator-visible Archive button in
# the portal actually DO something. Until then a press records intent and nothing acts on it, which
# is the deliberate state (`docs/ROADMAP.md` Track 6).
#
# The gate ROW is seeded (at false) by `scripts/migrations/seed_daemon_gate_config.py` in the same
# change that adds this code, because a boolean gate read with `default=False` treats a MISSING row
# identically to `false` — so a capability that "ships dark" with no row at all leaves the operator
# hunting for a switch that does not exist (HOUSE_REFLEXES §5).
CFG_ARCHIVE_ENABLED = "field_ops.fieldops_sync.archive_enabled"
DEFAULT_ARCHIVE_ENABLED = False
_PACIFIC = ZoneInfo("America/Los_Angeles")  # tracker cells are the operator's wall-clock

# #336 — every ITS_Config key this daemon resolves at RUNTIME, declared once for the
# startup observability pass (resolve_and_log). THREE workstreams: the seven field_ops
# gates, the SHARED safety_reports Worker base-URL (read under safety_reports), and the
# five progress_reports row-cap thresholds the hours/equipment/material/incidents/receipts
# passes read mid-cycle via
# progress_reports.{hours_log,equipment_status,material_list,material_incidents,material_receipts}
# .check_row_cap.
# The declared-but-not-runtime-read *.poll_interval_seconds key is deliberately EXCLUDED.
REQUIRED_CONFIG: list[ConfigKey] = [
    ConfigKey(CFG_SYNC_ENABLED, WORKSTREAM, DEFAULT_SYNC_ENABLED, "bool"),
    ConfigKey(CFG_HOURS_ENABLED, WORKSTREAM, DEFAULT_HOURS_ENABLED, "bool"),
    ConfigKey(CFG_EQUIPMENT_ENABLED, WORKSTREAM, DEFAULT_EQUIPMENT_ENABLED, "bool"),
    ConfigKey(CFG_MATERIALS_ENABLED, WORKSTREAM, DEFAULT_MATERIALS_ENABLED, "bool"),
    ConfigKey(CFG_INCIDENTS_ENABLED, WORKSTREAM, DEFAULT_INCIDENTS_ENABLED, "bool"),
    ConfigKey(CFG_RECEIPTS_ENABLED, WORKSTREAM, DEFAULT_RECEIPTS_ENABLED, "bool"),
    ConfigKey(CFG_ARCHIVE_ENABLED, WORKSTREAM, DEFAULT_ARCHIVE_ENABLED, "bool"),
    ConfigKey(
        CFG_WORKER_BASE_URL, CFG_WORKER_BASE_URL_WORKSTREAM, "", "str",
        description="Shared Worker base URL; owned by safety_reports, read here too.",
    ),
    ConfigKey(
        hours_log.CFG_ROW_CAP_WARN, "progress_reports",
        hours_log.DEFAULT_ROW_CAP_WARN, "int",
    ),
    ConfigKey(
        equipment_status.CFG_ROW_CAP_WARN, "progress_reports",
        equipment_status.DEFAULT_ROW_CAP_WARN, "int",
    ),
    ConfigKey(
        material_list.CFG_ROW_CAP_WARN, "progress_reports",
        material_list.DEFAULT_ROW_CAP_WARN, "int",
    ),
    ConfigKey(
        material_incidents.CFG_ROW_CAP_WARN, "progress_reports",
        material_incidents.DEFAULT_ROW_CAP_WARN, "int",
    ),
    ConfigKey(
        material_receipts.CFG_ROW_CAP_WARN, "progress_reports",
        material_receipts.DEFAULT_ROW_CAP_WARN, "int",
    ),
]

# State paths. HEARTBEAT_ROW_STATE_PATH is SHARED with the other daemons — same JSON file,
# different daemon_name key (ARCH-2).
STATE_DIR = Path.home() / "its" / "state"
HEARTBEAT_PATH = STATE_DIR / "fieldops_sync_heartbeat.txt"
LOCK_PATH = STATE_DIR / "fieldops_sync.lock"
HEARTBEAT_ROW_STATE_PATH = STATE_DIR / "heartbeat_row_ids.json"

# Sustained pending-jobs fetch-outage escalation (mirrors portal_poll's Check-Q counter). Because
# the decoupled hours/equipment passes now let a cycle COMPLETE (marker written) even when the
# job-QUEUE fetch fails, the old "no-marker → Check-C-stale" backstop for a SUSTAINED job-fetch
# outage is gone. A persisted consecutive-failure counter escalates the per-cycle ERROR to CRITICAL
# (the triple-fire push path) once the outage crosses the threshold; a successful fetch resets it.
PENDING_FETCH_FAIL_STATE_PATH = STATE_DIR / "fieldops_pending_fetch_failures.json"
PENDING_FETCH_FAIL_CRITICAL_THRESHOLD = 5  # consecutive cycles before escalating ERROR → CRITICAL

DAEMON_NAME = "field_ops.fieldops_sync"

# A1 self-provision metadata (the ONLY per-daemon difference in the heartbeat helpers).
_REGISTRATION_SOURCE_ID = "Safety Portal Worker /api/internal/fieldops/pending-jobs"

# Shared ITS_Daemon_Health reporter for this daemon.
_heartbeat_reporter = HeartbeatReporter(
    script_name=SCRIPT_NAME,
    daemon_name=DAEMON_NAME,
    workstream=WORKSTREAM,
    liveness_path=HEARTBEAT_PATH,
    interval_seconds=SYNC_INTERVAL_SECONDS,
    source_id=_REGISTRATION_SOURCE_ID,
    row_state_path=HEARTBEAT_ROW_STATE_PATH,  # shared file — make the contract explicit
)

# Watchdog Check C marker (TRACKED_JOBS registration in scripts/watchdog.py is deferred to
# the deploy session — the marker write here is forward-compatible and harmless if
# unregistered; see docs/tech_debt.md). Mirrors portal_poll's pattern.
WATCHDOG_MARKER_DIR = Path.home() / "its" / ".watchdog"
WATCHDOG_JOB_SLUG = "fieldops_sync"


@dataclass(frozen=True)
class SyncStats:
    """Summary of one sync_once() invocation."""

    skipped_disabled: bool = False
    skipped_locked: bool = False
    halted_no_creds: bool = False
    # base URL temporarily unreadable (Smartsheet blip / circuit OPEN) — transient,
    # distinct from halted_no_creds, which is a genuine misconfig that pages.
    halted_transient: bool = False
    scanned: int = 0
    mirrored: int = 0   # jobs whose BOTH sheets committed this cycle
    reviewed: int = 0   # jobs routed to the Review Queue (permanent failure)
    errors: int = 0     # transient per-job failures (left dirty) + skipped malformed rows
    # P7 hours pass (0 when hours_enabled is off — the shipped default).
    hours_mirrored: int = 0   # time entries whose Hours Log row committed this cycle
    hours_reviewed: int = 0   # entries/jobs routed to the Review Queue (permanent failure)
    hours_errors: int = 0     # transient hours failures (left unmirrored) + skipped malformed
    # P7 equipment pass (0 when equipment_enabled is off — the shipped default).
    equipment_upserted: int = 0   # equipment rows inserted/updated in place this cycle
    equipment_retired: int = 0    # equipment rows flipped On Job → Off Job this cycle
    equipment_reviewed: int = 0   # equipment/jobs routed to the Review Queue (permanent failure)
    equipment_errors: int = 0     # transient equipment failures (left for next cycle) + skipped
    # P7 material-list pass (0 when materials_enabled is off — the shipped default).
    materials_upserted: int = 0   # material-list rows inserted/updated in place this cycle
    materials_retired: int = 0    # material-list rows marked On List → Removed this cycle
    materials_reviewed: int = 0   # material/jobs routed to the Review Queue (permanent failure)
    materials_errors: int = 0     # transient material failures (left for next cycle) + skipped
    # P7 material-incidents pass (0 when incidents_enabled is off — the shipped default). APPEND-ONLY
    # ledger: no `retired` counter (a filed incident is immutable and never removed).
    incidents_upserted: int = 0   # incident rows inserted/updated in place this cycle
    incidents_reviewed: int = 0   # incidents/jobs routed to the Review Queue (permanent failure)
    incidents_errors: int = 0     # transient incident failures (left for next cycle) + skipped
    # PR4 material-receipts pass (0 when receipts_enabled is off — the shipped default). APPEND-ONLY
    # ledger: no `retired` counter (a signed-for delivery is immutable and never removed).
    receipts_upserted: int = 0    # receipt-event rows inserted/updated in place this cycle
    receipts_reviewed: int = 0    # events/jobs routed to the Review Queue (permanent failure)
    receipts_errors: int = 0      # transient receipt failures (left for next cycle) + skipped


# ---- Config readers (replicated per preservation, mirror portal_poll) ----------


def _read_str_setting(key: str, fallback: str, workstream: str | None = None) -> str:
    # workstream defaults to this daemon's WORKSTREAM (field_ops); pass an explicit owner for
    # a key owned by a different workstream (e.g. the shared safety_reports base-URL key).
    try:
        raw = smartsheet_client.get_setting(
            key, workstream=workstream if workstream is not None else WORKSTREAM
        )
    except smartsheet_client.SmartsheetNotFoundError:
        return fallback
    except smartsheet_client.SmartsheetCircuitOpenError:
        return fallback
    except smartsheet_client.SmartsheetError as exc:
        # Transient read failure (timeout / 5xx) — a single-cycle blip must not
        # escape to @its_error_log as a spurious CRITICAL. WARN + fall open to
        # the fallback, same disposition as the circuit-open branch above.
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"config read failed for {key}: {exc!r} — using fallback {fallback!r}",
            error_code="config_read_error",
        )
        return fallback
    return raw if isinstance(raw, str) and raw else fallback


def _read_bool_setting(key: str, fallback: bool) -> bool:
    raw = _read_str_setting(key, str(fallback).lower())
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _sync_enabled() -> bool:
    return _read_bool_setting(CFG_SYNC_ENABLED, DEFAULT_SYNC_ENABLED)


def _hours_enabled() -> bool:
    return _read_bool_setting(CFG_HOURS_ENABLED, DEFAULT_HOURS_ENABLED)


def _equipment_enabled() -> bool:
    return _read_bool_setting(CFG_EQUIPMENT_ENABLED, DEFAULT_EQUIPMENT_ENABLED)


def _materials_enabled() -> bool:
    return _read_bool_setting(CFG_MATERIALS_ENABLED, DEFAULT_MATERIALS_ENABLED)


def _incidents_enabled() -> bool:
    return _read_bool_setting(CFG_INCIDENTS_ENABLED, DEFAULT_INCIDENTS_ENABLED)


def _receipts_enabled() -> bool:
    return _read_bool_setting(CFG_RECEIPTS_ENABLED, DEFAULT_RECEIPTS_ENABLED)


def _archive_enabled() -> bool:
    return _read_bool_setting(CFG_ARCHIVE_ENABLED, DEFAULT_ARCHIVE_ENABLED)


# ---- Lock + heartbeat seams (mirror portal_poll) -------------------------------


@contextmanager
def _file_lock(path: Path) -> Iterator[bool]:
    """Acquire exclusive non-blocking lock; yield True on success, False if held."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        except Exception:  # noqa: BLE001 — cleanup best-effort
            pass
        handle.close()


def _write_heartbeat() -> None:
    """Liveness file touch — thin delegator to the shared HeartbeatReporter.

    Kept as a module-level function because it is the canonical test mock seam
    (the suite patches this exact symbol). See shared/heartbeat.py (§42).
    """
    _heartbeat_reporter.write_liveness()


def _write_heartbeat_row(
    *,
    status: HeartbeatStatus,
    items_processed: int,
    error_summary: str | None = None,
    correlation_id: str | None = None,
    notes: str | None = None,
) -> None:
    """ITS_Daemon_Health per-cycle row update — thin delegator to the shared
    HeartbeatReporter (the canonical test mock seam). See shared/heartbeat.py (§42)."""
    _heartbeat_reporter.write_row(
        status=status,
        items_processed=items_processed,
        error_summary=error_summary,
        correlation_id=correlation_id,
        notes=notes,
    )


def _write_watchdog_marker() -> None:
    """Touch the Check C freshness marker for this run (mirror portal_poll)."""
    try:
        WATCHDOG_MARKER_DIR.mkdir(parents=True, exist_ok=True)
        marker = WATCHDOG_MARKER_DIR / f"{WATCHDOG_JOB_SLUG}.last_run"
        marker.write_text(datetime.now(UTC).isoformat())
    except OSError as exc:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"watchdog marker write failed: {exc!r}",
            error_code="watchdog_marker_failed",
        )


# ---- Credential resolution (fail-CLOSED) ---------------------------------------


def _resolve_credentials() -> tuple[str, str] | TransientUnavailable | None:
    """Resolve (base_url, bearer) fail-CLOSED, three ways.

    `TransientUnavailable` means the base-URL row could not be READ this cycle
    (Smartsheet blip / circuit open) — self-heals, so the caller WARNs and skips.
    `None` means a credential is genuinely absent — a misconfig that pages.

    The base-URL read deliberately does NOT go through `_read_str_setting`: that
    helper swallows both circuit-open and transient errors into its fallback, so
    a one-cycle Smartsheet blip arrived here as `""` and was indistinguishable
    from an unset row — which is exactly how a network hiccup produced a CRITICAL
    blaming *credentials* that were never missing (this bit po_poll live on
    2026-07-20 04:42Z; every puller shared the flaw).
    """
    resolved = creds_resolution.read_base_url(
        CFG_WORKER_BASE_URL, CFG_WORKER_BASE_URL_WORKSTREAM
    )
    if isinstance(resolved, TransientUnavailable):
        return resolved
    base_url = resolved or ""
    try:
        bearer = keychain.get_secret(KC_FIELDOPS_TOKEN)
    except keychain.KeychainError:
        bearer = ""
    if not (base_url and bearer):
        return None
    return base_url, bearer


# ---- Public API ----------------------------------------------------------------


@its_error_log(SCRIPT_NAME)
@require_active
def sync_once() -> int:
    """Run one mirror cycle. Returns the number of jobs fully mirrored (BOTH sheets).

    SKELETON-superseded (P2.5 Slice 5): gate → fail-closed creds → GET pending-jobs →
    per-job dual-sheet find-or-create + per-sheet mark-mirrored → heartbeat + marker.
    launchd invokes this once per StartInterval; idempotent across crashes.
    """
    # #336 startup observability: resolve+log every runtime ITS_Config key with its
    # source; a MISSING declared row WARNs distinctly (config_row_missing). Runs after
    # @require_active (a PAUSED daemon never logs) and is fail-open — it never blocks the
    # cycle. The runtime _read_*_setting reads below are UNCHANGED (§14, additive).
    # The archive pass's five Box-root keys ride this daemon's startup sweep — the
    # contract job_archive.py states beside its REQUIRED_CONFIG (that module has no
    # entry point of its own; wiring audit 2026-08-12 found the promise unkept).
    resolve_and_log(SCRIPT_NAME, REQUIRED_CONFIG + job_archive.REQUIRED_CONFIG)

    if not _sync_enabled():
        # Shipped default (OFF until cutover) — an intentional state, not an anomaly, so no
        # heartbeat/marker/log every cycle (would be 5-minute spam). The ITS_Config gate is
        # the operator's switch; flipping it on is the only thing that starts work.
        return 0

    with _file_lock(LOCK_PATH) as acquired:
        if not acquired:
            error_log.log(
                Severity.INFO, SCRIPT_NAME,
                "another sync cycle holds the lock; skipping this cycle",
                error_code="sync_lock_held",
            )
            return 0
        try:
            return _sync_inside_lock().mirrored
        finally:
            # D3 — see portal_poll: one summarized WARN row per pass that recovered on retry.
            sustained_failure.flush_retry_recovery(SCRIPT_NAME)


def _record_pending_fetch_failure() -> int:
    """Increment + persist the consecutive pending-jobs fetch-failure counter; return the new count.

    Used ONLY for a transient `PortalTransportError` (a 401 pages immediately). On any state error,
    returns 1 (treat as a single failure — never page off a state glitch). Mirrors
    `safety_reports.portal_poll._record_fetch_failure`."""
    try:
        with state_io.with_path_lock(PENDING_FETCH_FAIL_STATE_PATH):
            count = 0
            if PENDING_FETCH_FAIL_STATE_PATH.exists():
                try:
                    count = int(
                        json.loads(PENDING_FETCH_FAIL_STATE_PATH.read_text()).get("count", 0)
                    )
                except (OSError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
                    count = 0
            count += 1
            state_io.atomic_write_json(PENDING_FETCH_FAIL_STATE_PATH, {"count": count})
            return count
    except Exception as exc:  # noqa: BLE001 — counter is best-effort; never page off a state glitch
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"pending-fetch-failure counter write failed (treating as #1): {exc!r}",
            error_code="fieldops_pending_fetch_counter_failed",
        )
        return 1


def _reset_pending_fetch_failures() -> None:
    """Zero the consecutive pending-jobs fetch-failure counter after a successful fetch. Best-effort:
    a reset failure only risks one spurious CRITICAL next cycle, never a missed outage."""
    try:
        with state_io.with_path_lock(PENDING_FETCH_FAIL_STATE_PATH):
            if PENDING_FETCH_FAIL_STATE_PATH.exists():
                state_io.atomic_write_json(PENDING_FETCH_FAIL_STATE_PATH, {"count": 0})
    except Exception:  # noqa: BLE001 — best-effort reset
        pass


def _cycle_error_summary(
    total_errors: int, total_reviewed: int, stopped_archives: int
) -> str | None:
    """The `ITS_Daemon_Health` Error Summary cell for one cycle, or None when there is nothing to say.

    A stopped archive is called out BY NAME rather than folded into the errors count, because the
    two need different things from the operator. An `errors=` count means "the daemon hit failures;
    it will try again next cycle". A stopped archive means "a job is half-relocated across two
    external systems and NOTHING will resume it until you press Archive again" — the queue serves
    only `requested`/`in_progress`, so there is no next cycle for it. A number that reads like the
    self-healing kind would send the operator right past the kind that isn't.
    """
    if total_errors == 0 and total_reviewed == 0:
        return None
    summary = f"errors={total_errors} reviewed={total_reviewed}"
    if stopped_archives:
        summary += f" — {stopped_archives} archive(s) STOPPED, will not resume without a re-press"
    return summary


def _sync_inside_lock() -> SyncStats:
    """Body of sync_once running under the file lock."""
    creds = _resolve_credentials()
    if isinstance(creds, TransientUnavailable):
        # Smartsheet was TEMPORARILY unreachable when reading the Worker base URL —
        # NOT a misconfig, and it self-heals next cycle. WARN + skip; do NOT page
        # (paging here was a false CRITICAL blaming credentials on every blip).
        # Skip the watchdog freshness marker exactly like the no-creds path, so a
        # SUSTAINED outage still surfaces via the Check-C staleness floor — just
        # without per-cycle CRITICAL spam.
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"field-ops Worker base URL temporarily unreadable ({creds.reason}) — skipping "
            f"this cycle; will retry next interval (transient, self-heals)",
            error_code="fieldops_creds_transient",
        )
        _write_heartbeat()
        _write_heartbeat_row(status="WARN", items_processed=0,
                             error_summary=f"base URL unreadable ({creds.reason}) — transient")
        return SyncStats(halted_transient=True)
    if creds is None:
        # FAIL-CLOSED: missing base URL / bearer → do NOT sync. A MISCONFIG that will NOT
        # self-heal (unset ITS_Config base URL or a removed/rotated Keychain entry) → page
        # immediately (CRITICAL). No watchdog marker (let Check C go stale too).
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            (
                # Deliberately does NOT interpolate the ITS_Config key or the Keychain entry
                # NAME (naming secret-store entries in a log is a CodeQL clear-text trip).
                "fail-closed: missing field-ops portal credentials — the Worker base URL "
                "(ITS_Config) and/or the field-ops bearer Keychain entry are unset; NOT "
                "syncing until fixed (see docs/runbooks/fieldops_sync.md Symptom B)"
            ),
            error_code="fieldops_creds_missing",
        )
        _write_heartbeat()
        _write_heartbeat_row(status="ERROR", items_processed=0,
                             error_summary="fail-closed: field-ops credentials missing")
        return SyncStats(halted_no_creds=True)

    base_url, bearer = creds
    counters = {"mirrored": 0, "reviewed": 0, "errors": 0}
    # The job pass and the hours / equipment passes hit INDEPENDENT Worker endpoints
    # (/pending-jobs vs /hours-pending, /equipment-snapshot). A TRANSIENT job-fetch failure must
    # NOT starve the downstream passes — that was the live "logged time never reaches the Hours
    # Log" bug: a recurring pending-jobs PortalTransportError returned the whole cycle early, so
    # hours mirroring only ran on the cycles the job-fetch happened to succeed. (A 401 still stops
    # the whole cycle — the SAME bearer fails every endpoint.)
    jobs: list[dict[str, Any]] | None = None
    try:
        jobs = portal_client.get_fieldops_pending_jobs(base_url, bearer)
    except portal_client.PortalAuthError as exc:
        # 401 — bad/rotated/missing SHARED bearer. NOTHING can work this cycle (the hours /
        # equipment endpoints use the SAME bearer and would 401 too), so STOP: page CRITICAL + no
        # watchdog marker (let Check C go stale on a persistent 401). Caught BEFORE
        # PortalTransportError (its subclass).
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            f"pending-jobs fetch UNAUTHORIZED (401) — field-ops bearer rejected; up-sync "
            f"STOPPED until the token is fixed: {exc!r}",
            error_code="fieldops_pending_auth_failed",
        )
        _write_heartbeat()
        _write_heartbeat_row(status="ERROR", items_processed=0,
                             error_summary="pending-jobs UNAUTHORIZED (401) — bearer rejected")
        return SyncStats(errors=1)
    except portal_client.PortalTransportError as exc:
        # TRANSIENT job-fetch failure (Worker blip / network / an intermittent bot-challenge).
        # Jobs stay dirty + re-pull next cycle (no silent loss). CRUCIAL: do NOT return — the
        # hours / equipment passes hit DIFFERENT, independent endpoints (/hours-pending,
        # /equipment-snapshot) that may well be reachable this cycle, so they MUST still run.
        counters["errors"] += 1
        n = _record_pending_fetch_failure()
        if sustained_failure.is_escalation_cycle(n, PENDING_FETCH_FAIL_CRITICAL_THRESHOLD):
            # SUSTAINED outage — the decoupled cycle no longer goes Check-C-stale, so escalate to
            # CRITICAL (the triple-fire push path). Mirrors portal_poll's Check-Q.
            # On the shared LADDER, not every cycle past the threshold: an open CRITICAL is
            # never terminal, so the old `n >= threshold` minted UNROTATABLE rows for the
            # whole outage. See `sustained_failure.is_escalation_cycle`.
            error_log.log(
                Severity.CRITICAL, SCRIPT_NAME,
                f"pending-jobs fetch failing for {n} consecutive cycles — SUSTAINED job-queue "
                f"outage (portal jobs not mirroring; hours/equipment UNAFFECTED — independent "
                f"endpoints). See docs/runbooks/fieldops_sync.md Symptom D: {exc!r}",
                error_code="fieldops_pending_fetch_sustained",
            )
        else:
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"failed to GET pending-jobs (jobs left dirty for next cycle; hours/equipment "
                f"passes still run — independent endpoints; {n} consecutive): {exc!r}",
                error_code="fieldops_pending_fetch_failed",
            )

    if jobs is not None:
        _reset_pending_fetch_failures()  # a successful fetch clears the sustained-outage counter
        for job in jobs:
            _mirror_job(job, base_url, bearer, counters)
    scanned = len(jobs) if jobs is not None else 0

    # P7 hours pass — INDEPENDENT endpoint; runs even after a TRANSIENT job-fetch failure (a 401
    # returned above). Its own per-entry/per-job fences mean a hours failure NEVER aborts the job
    # mirror above nor the heartbeat below.
    hours = {"mirrored": 0, "reviewed": 0, "errors": 0}
    if _hours_enabled():
        hours = _mirror_hours_pass(base_url, bearer)

    # P7 equipment pass — same, INDEPENDENT endpoint. Gated OFF by default; SNAPSHOT: no
    # mark-mirrored.
    equip = {"upserted": 0, "retired": 0, "reviewed": 0, "errors": 0}
    if _equipment_enabled():
        equip = _mirror_equipment_pass(base_url, bearer)

    # P7 material-list pass — same, INDEPENDENT endpoint (/material-list-snapshot). Gated OFF by
    # default; SNAPSHOT: no mark-mirrored. Runs LAST (downstream); its own per-job/per-line fences
    # mean a material failure NEVER aborts the job/hours/equipment passes above nor the heartbeat
    # below — consistent with the decouple fix (no re-introduced job-fetch dependency).
    materials = {"upserted": 0, "retired": 0, "reviewed": 0, "errors": 0}
    if _materials_enabled():
        materials = _mirror_material_list_pass(base_url, bearer)

    # P7 material-incidents pass — same, INDEPENDENT endpoint (/material-incidents). Gated OFF by
    # default; APPEND-ONLY ledger (immutable filed events): no reconcile roster, no retire, no
    # mark-mirrored. Runs LAST (downstream); its own per-job/per-incident fences mean an incident
    # failure NEVER aborts the passes above nor the heartbeat below — consistent with the decouple
    # fix (no re-introduced job-fetch dependency).
    incidents = {"upserted": 0, "reviewed": 0, "errors": 0}
    if _incidents_enabled():
        incidents = _mirror_material_incidents_pass(base_url, bearer)

    # PR4 material-receipts pass — same, INDEPENDENT endpoint (/material-receipts). Gated OFF by
    # default; APPEND-ONLY ledger (immutable delivery marks): no reconcile roster, no retire, no
    # mark-mirrored. Runs LAST of the mirrors; its own per-job/per-event fences mean a receipt
    # failure NEVER aborts the passes above nor the heartbeat below — consistent with the decouple
    # fix (no re-introduced job-fetch dependency).
    receipts = {"upserted": 0, "reviewed": 0, "errors": 0}
    if _receipts_enabled():
        receipts = _mirror_material_receipts_pass(base_url, bearer)

    # Track 6 archive pass — its OWN queue (/archive-pending), not the job-dirty list, so a failed
    # relocation genuinely retries instead of being silenced by an unrelated mirror success. Gated
    # OFF by default. Runs LAST, after every mirror: relocating a closed job's folders is the least
    # time-critical thing this daemon does, and putting it last means its per-job fences cannot
    # delay the mirrors that feed live reports.
    archive = {"complete": 0, "partial": 0, "failed": 0, "capped": 0, "errors": 0}
    if _archive_enabled():
        archive = _archive_pass(base_url, bearer)

    _write_heartbeat()
    # A `partial` or `failed` archive counts as an ERROR for the cycle, not merely an outcome.
    #
    # This is not bookkeeping pedantry. Those two states are TERMINAL for the daemon — the queue
    # route serves only `archive_state IN ('requested','in_progress')`, so a stopped archive leaves
    # the queue and NOTHING picks it up again until a human presses Archive. Until 2026-08-10 they
    # were tallied only into `archive["partial"]`/`["failed"]`, which fed `items_processed` and
    # never `total_errors` — so a job left half-relocated across Smartsheet AND Box reported
    # `cycle_status="OK"` and a green heartbeat, with a single WARN row as the only trace. The one
    # archive outcome that always needs a human was the one outcome nothing surfaced.
    #
    # `archive["errors"]` alone is not sufficient: it counts the pass's own failures (queue fetch,
    # pre-flight, a raising job), not a relocation that ran to completion and came up short.
    stopped_archives = archive["partial"] + archive["failed"]
    total_errors = (
        counters["errors"] + hours["errors"] + equip["errors"] + materials["errors"]
        + incidents["errors"] + receipts["errors"] + archive["errors"]
        + stopped_archives
    )
    total_reviewed = (
        counters["reviewed"] + hours["reviewed"] + equip["reviewed"] + materials["reviewed"]
        + incidents["reviewed"] + receipts["reviewed"]
    )
    if total_errors > 0:
        cycle_status: HeartbeatStatus = "DEGRADED"
    elif total_reviewed > 0:
        cycle_status = "WARN"
    else:
        cycle_status = "OK"
    if circuit_breaker.is_open():
        cycle_status = "CIRCUIT_OPEN"

    try:
        _write_heartbeat_row(
            status=cycle_status,
            items_processed=(
                counters["mirrored"] + hours["mirrored"] + equip["upserted"]
                + materials["upserted"] + incidents["upserted"] + receipts["upserted"]
                + archive["complete"] + archive["partial"]
            ),
            error_summary=_cycle_error_summary(total_errors, total_reviewed, stopped_archives),
        )
    except Exception as exc:  # noqa: BLE001 — heartbeat must never block
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"heartbeat write outer-catch tripped: {exc!r}",
            error_code="daemon_health_write_failed",
        )
    _write_watchdog_marker()

    error_log.log(
        Severity.INFO, SCRIPT_NAME,
        (
            f"sync cycle: scanned={scanned} mirrored={counters['mirrored']} "
            f"reviewed={counters['reviewed']} errors={counters['errors']}; "
            f"hours mirrored={hours['mirrored']} reviewed={hours['reviewed']} "
            f"errors={hours['errors']}; equipment upserted={equip['upserted']} "
            f"retired={equip['retired']} reviewed={equip['reviewed']} errors={equip['errors']}; "
            f"materials upserted={materials['upserted']} retired={materials['retired']} "
            f"reviewed={materials['reviewed']} errors={materials['errors']}; "
            f"incidents upserted={incidents['upserted']} reviewed={incidents['reviewed']} "
            f"errors={incidents['errors']}; "
            f"receipts upserted={receipts['upserted']} reviewed={receipts['reviewed']} "
            f"errors={receipts['errors']}; "
            f"archive complete={archive['complete']} partial={archive['partial']} "
            f"failed={archive['failed']} capped={archive['capped']} errors={archive['errors']}"
        ),
        error_code="sync_cycle_summary",
    )
    return SyncStats(
        scanned=scanned,
        mirrored=counters["mirrored"],
        reviewed=counters["reviewed"],
        errors=counters["errors"],
        hours_mirrored=hours["mirrored"],
        hours_reviewed=hours["reviewed"],
        hours_errors=hours["errors"],
        equipment_upserted=equip["upserted"],
        equipment_retired=equip["retired"],
        equipment_reviewed=equip["reviewed"],
        equipment_errors=equip["errors"],
        materials_upserted=materials["upserted"],
        materials_retired=materials["retired"],
        materials_reviewed=materials["reviewed"],
        materials_errors=materials["errors"],
        incidents_upserted=incidents["upserted"],
        incidents_reviewed=incidents["reviewed"],
        incidents_errors=incidents["errors"],
        receipts_upserted=receipts["upserted"],
        receipts_reviewed=receipts["reviewed"],
        receipts_errors=receipts["errors"],
    )


def _mirror_job(
    job: dict[str, Any], base_url: str, bearer: str, counters: dict[str, int]
) -> None:
    """Mirror one dirty job into BOTH sheets under a per-job fence; mutates `counters`.

    Per-sheet commit point: safety upsert → mark-mirrored(safety) → progress upsert →
    mark-mirrored(progress). A failure after the safety commit leaves the job dirty with
    safety already advanced (the version-vector self-heal).
    """
    job_id = str(job.get("job_id") or "").strip()
    if not job_id:
        # A job with no key can't be mirrored / marked-mirrored — surface, don't dispatch.
        counters["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            "pending job missing job_id; skipping",
            error_code="fieldops_job_no_id",
        )
        return

    raw_mirror_version = job.get("mirror_version")
    if isinstance(raw_mirror_version, int):
        mirror_version = raw_mirror_version
    else:
        # Never-silent: a missing/malformed mirror_version (vs the Worker's monotonic-MAX
        # watermark) coerces to 0 → the job would stay permanently dirty (re-attempted every
        # cycle, never escalated). WARN so the cause is observable instead of an invisible loop.
        mirror_version = 0
        error_log.log(
            Severity.WARN,
            SCRIPT_NAME,
            f"job {job.get('job_id')!r} has a missing/malformed mirror_version "
            f"({raw_mirror_version!r}); coercing to 0 — it will stay dirty until a well-formed "
            f"version arrives (likely a Worker pending-jobs payload defect).",
            error_code="fieldops_mirror_version_malformed",
        )
    correlation_id = uuid.uuid4().hex[:12]
    # Track the dual-sheet commit point so a permanent failure's Review-Queue ticket records the
    # PARTIAL state (safety already mirrored vs nothing mirrored) — the operator's fix differs.
    mirrored_safety = False

    try:
        # ── SAFETY sheet ──────────────────────────────────────────────────────
        safety_row_id, canonical = active_jobs_writer.upsert_job(
            active_jobs_writer.SAFETY_WRITE_CONFIG, job
        )
        portal_client.mark_fieldops_jobs_mirrored(
            base_url, bearer,
            [{
                "job_id": job_id,
                "sheet": "safety",
                "mirrored_version": mirror_version,
                "row_id": safety_row_id,
                "canonical_job_id": canonical or None,
            }],
        )
        mirrored_safety = True
        # ── PROGRESS sheet ────────────────────────────────────────────────────
        # Commit point already advanced safety; if this raises, the job stays dirty with
        # only safety mirrored and next cycle re-attempts both (safety find-or-create no-ops).
        progress_row_id, _progress_canonical = active_jobs_writer.upsert_job(
            active_jobs_writer.PROGRESS_WRITE_CONFIG, job
        )
        portal_client.mark_fieldops_jobs_mirrored(
            base_url, bearer,
            [{
                "job_id": job_id,
                "sheet": "progress",
                "mirrored_version": mirror_version,
                "row_id": progress_row_id,
            }],
        )
        counters["mirrored"] += 1
        # DISARMED (ROADMAP Track 6, PR-0). The §51 archive-on-closure move used to fire from
        # HERE — `if lifecycle == 'archived': _archive_closed_job_trackers(...)` — on any mirror
        # of an archived job.
        #
        # Its LOCATION was the defect. This is the job-dirty mirror path and the job is
        # mark-synced immediately after, so a failed move was PERMANENT — `_warn_archive_move_failed`
        # says exactly that in its own WARN text. Coupled to a portal dropdown that offered
        # "Archived" with no confirmation, it was a one-click, un-retryable, four-sheet relocation
        # whose only feedback was a WARN row on a sheet nobody watches in real time.
        #
        # The relocation moves to its own drained queue with its own gate, a durable per-container
        # ledger, and real cross-cycle retry — the same shape as the hours / equipment / material
        # passes. `_archive_closed_job_trackers` is PRESERVED below (§14 preservation-over-refactor)
        # until that path is live-proven; it simply has no caller now, so nothing fires
        # automatically. `tests/test_fieldops_sync.py::test_job_mirror_never_archives` is the
        # standing fence that keeps the two decoupled.
    except (
        picklist_validation.PicklistViolationError,
        smartsheet_client.SmartsheetValidationError,
        # A §46 share change or a deleted tracker sheet is PERMANENT: without these two
        # the fault fell to the transient arm below and logged "re-projects next cycle"
        # forever — no ticket, no CRITICAL, no operator ever told.
        smartsheet_client.SmartsheetPermissionError,
        smartsheet_client.SmartsheetNotFoundError,
    ) as exc:
        # PERMANENT — the row will never succeed as-is (a bad lifecycle value, an HTTP-400
        # reject). Route to the Review Queue; leave the job dirty (the operator has a ticket).
        counters["reviewed"] += 1
        _route_to_review(job, job_id, exc, correlation_id, mirrored_safety=mirrored_safety)
    except portal_client.PortalAuthError:
        # 401 on mark-mirrored — the field-ops bearer was rejected while writing back the
        # watermark. NOT transient: a bad/rotated bearer will NOT self-heal → page (CRITICAL),
        # same posture as the pending-jobs 401. The sheet write itself already SUCCEEDED (upsert
        # is a Smartsheet write, not a Worker call), so only the Worker watermark is missing — the
        # job stays dirty and is safely re-attempted (find-or-create no-ops) once the bearer is
        # fixed. PortalAuthError is a PortalTransportError SUBCLASS, so this MUST precede the
        # transient clause below or a 401 would be mis-classified as a self-healing blip.
        counters["errors"] += 1
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            f"mark-mirrored UNAUTHORIZED (401) — field-ops bearer rejected during write-back for "
            f"job_id={job_id!r}; the sheet write landed but the Worker watermark did not, so the "
            f"job is left dirty (safe re-attempt once the bearer is fixed). "
            f"See docs/runbooks/fieldops_sync.md Symptom E.",
            error_code="fieldops_mark_mirrored_unauthorized",
            correlation_id=correlation_id,
        )
    except (smartsheet_client.SmartsheetError, portal_client.PortalTransportError) as exc:
        # TRANSIENT — leave the job dirty (do NOT mark-mirrored); next cycle retries.
        counters["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"transient failure mirroring job_id={job_id!r} (left dirty for next cycle): "
            f"{type(exc).__name__}: {exc!r}",
            error_code="fieldops_job_transient",
            correlation_id=correlation_id,
        )
    except Exception as exc:  # noqa: BLE001 — per-job fence; one bad job never kills the cycle
        counters["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"unexpected failure mirroring job_id={job_id!r}: {type(exc).__name__}: {exc!r}",
            error_code="fieldops_job_unexpected",
            correlation_id=correlation_id,
        )


def _archive_closed_job_trackers(
    job_id: str, project_name: str, correlation_id: str
) -> None:
    """§51 archive-on-closure — MOVE a closed job's standing tracker sheets into the
    Archive workspace's "Closed Projects" folder.

    Called ONLY for a job whose lifecycle is `archived`, right after it mirrors. Best-
    effort + fully fenced: ANY failure WARNs (`fieldops_archive_on_closure_failed`) and
    returns — a move failure must NEVER fail the mirror (the job is already mirrored +
    mark-synced back to the Worker). NEVER deletes rows or sheets; a pure relocation.

    Idempotent by construction: it resolves the tracker sheet find-or-create-FREE in the
    SOURCE (per-job PROGRESS) folder. Once a sheet has been moved out of that folder it is
    no longer found there → this returns without a second move (the natural idempotency —
    the same daemon may re-see an already-archived job on a later dirty cycle).

    Standing trackers moved: the per-job `<Job> — Hours Log` (P7 Slice 1), the per-job
    `<Job> — Equipment` (P7 Slice 2), the per-job `<Job> — Material List` (P7 M2), AND the per-job
    `<Job> — Material Incidents` (P7 M3 Slice 2). Each is resolved + moved INDEPENDENTLY under its own
    fence, so a failure moving one never blocks the others.

    Edge case (by design, not handled here): if an archived job later receives NEW hours /
    equipment reads, the hours / equipment pass would find-or-CREATE a fresh tracker back in
    the active PROGRESS folder (this helper only moves what exists at closure time).
    Archived/closed jobs are not expected to receive new field data; note that new field data
    flows through the SEPARATE pending queues (`_mirror_hours_pass` / `_mirror_equipment_pass`),
    NOT the job-dirty list that drives this helper, so a fresh sheet would re-archive only when
    the JOB itself is next re-dirtied (edited) — not automatically.
    """
    # Resolve the per-job folder in the PROGRESS workspace WITHOUT creating it — the SAME
    # folder the Hours Log / Equipment / week sheets live in (identical name via safety_naming).
    # If this raises, no tracker can be resolved → fenced, WARN once, return.
    try:
        folder = smartsheet_client.find_folder_by_name_in_workspace(
            sheet_ids.WORKSPACE_PROGRESS_REPORTING, hours_log._folder_name(project_name)
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; never fails the mirror
        _warn_archive_move_failed(job_id, project_name, correlation_id, exc)
        return
    if folder is None:
        # No per-job folder → nothing was ever created for this job → nothing to archive.
        return

    # Each tracker: resolve find-no-create + move, independently fenced. `None` ⇒ already moved
    # (prior cycle) OR never existed — either way skip. Add new trackers to this list.
    trackers = (
        hours_log.hours_log_sheet_name(project_name),
        equipment_status.equipment_sheet_name(project_name),
        material_list.material_list_sheet_name(project_name),
        material_incidents.material_incidents_sheet_name(project_name),
        material_receipts.material_receipts_sheet_name(project_name),
    )
    for sheet_name in trackers:
        try:
            sid = smartsheet_client.find_sheet_by_name_in_folder(folder, sheet_name)
            if sid is None:
                continue
            smartsheet_client.move_sheet_to_folder(sid, sheet_ids.FOLDER_ARCHIVE_CLOSED_PROJECTS)
        except Exception as exc:  # noqa: BLE001 — best-effort; a move failure never fails the mirror
            _warn_archive_move_failed(job_id, project_name, correlation_id, exc, sheet_name)


def _warn_archive_move_failed(
    job_id: str, project_name: str, correlation_id: str, exc: Exception,
    sheet_name: str | None = None,
) -> None:
    """WARN for a best-effort archive-on-closure move failure (never fails the mirror)."""
    which = f" ({sheet_name!r})" if sheet_name else ""
    error_log.log(
        Severity.WARN, SCRIPT_NAME,
        f"archive-on-closure move failed for job_id={job_id!r}{which} "
        f"(project_name={project_name!r}); the job is already mirrored + mark-synced so this "
        f"never fails the mirror — but the job is now CLEAN, so the move does NOT auto-retry: "
        f"the tracker stays (never lost/deleted) in the active PROGRESS folder until the job "
        f"is next re-dirtied (edited) or an operator moves it manually "
        f"(docs/runbooks/hours_log_sync.md Fault F). {type(exc).__name__}: {exc!r}",
        error_code="fieldops_archive_on_closure_failed",
        correlation_id=correlation_id,
    )


def _route_to_review(
    job: dict[str, Any], job_id: str, exc: Exception, correlation_id: str,
    *, mirrored_safety: bool,
) -> None:
    """Route a permanently-failed job to ITS_Review_Queue (workstream progress_reports).

    `mirrored_safety` records the dual-sheet PARTIAL-commit state at the point of failure so the
    operator's ticket says WHERE the job stands: if the safety sheet already mirrored, only the
    PROGRESS sheet failed (safety is live + correct); otherwise nothing mirrored yet (the failure
    was on the safety sheet). The remediation differs between the two, so name it explicitly.
    """
    failed_sheet = "progress" if mirrored_safety else "safety"
    partial = (
        "safety sheet already mirrored — only the progress sheet failed"
        if mirrored_safety
        else "nothing mirrored yet — failed on the safety sheet"
    )
    review_queue.safe_add(
        script_name=SCRIPT_NAME,
        workstream="progress_reports",
        summary=(
            f"field-ops up-sync: PERMANENT failure mirroring job {job_id!r} on the {failed_sheet} "
            f"sheet ({type(exc).__name__}) — {partial}; left dirty, needs operator fix"
        ),
        payload={
            "job_id": job_id,
            "project_name": job.get("project_name"),
            "lifecycle": job.get("lifecycle"),
            "failed_sheet": failed_sheet,
            "safety_mirrored": mirrored_safety,
            "error": f"{type(exc).__name__}: {exc!r}",
        },
        sla_tier=review_queue.SlaTier.SAFETY_INTAKE,
        reason=review_queue.ReviewReason.POLICY_EDGE,
        severity=Severity.WARN,
        source_file=job_id,
    )
    error_log.log(
        Severity.WARN, SCRIPT_NAME,
        f"job_id={job_id!r} routed to Review Queue (permanent, failed on the {failed_sheet} "
        f"sheet): {type(exc).__name__}: {exc!r}",
        error_code="fieldops_job_permanent",
        correlation_id=correlation_id,
    )


# ---- P7 Hours Log up-sync pass (Track 2, Slice 1) --------------------------------


def _fmt_epoch_date(epoch: Any) -> str:
    """Epoch seconds → Pacific 'YYYY-MM-DD' (the operator's work day). '' on missing/malformed."""
    if isinstance(epoch, bool) or not isinstance(epoch, int):
        return ""
    try:
        return datetime.fromtimestamp(epoch, _PACIFIC).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def _fmt_epoch_dt(epoch: Any) -> str:
    """Epoch seconds → Pacific ISO datetime (the Recorded At server-time column). '' when unset."""
    if isinstance(epoch, bool) or not isinstance(epoch, int):
        return ""
    try:
        return datetime.fromtimestamp(epoch, _PACIFIC).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def _fmt_hours(hours: Any) -> str:
    """Field-reported hours → a trimmed string ('' when unset)."""
    if isinstance(hours, bool) or not isinstance(hours, (int, float)):
        return ""
    return f"{hours:g}"


def _group_hours_by_job(
    entries: list[dict[str, Any]],
) -> dict[str, tuple[str, list[dict[str, Any]]]]:
    """Group pending hours rows by job_id → (project_name, [rows]). Skips a row missing its
    uuid / job_id / project_name (a data anomaly that can't be foldered) — never silent."""
    by_job: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for e in entries:
        entry_uuid = str(e.get("uuid") or "").strip()
        job_id = str(e.get("job_id") or "").strip()
        project = str(e.get("project_name") or "").strip()
        if not entry_uuid or not job_id or not project:
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"hours entry skipped — missing uuid/job_id/project_name "
                f"(uuid={e.get('uuid')!r} job_id={e.get('job_id')!r})",
                error_code="fieldops_hours_row_malformed",
            )
            continue
        by_job.setdefault(job_id, (project, []))[1].append(e)
    return by_job


def _mirror_hours_pass(base_url: str, bearer: str) -> dict[str, int]:
    """Mirror unmirrored crew time entries UP into per-job Hours Log sheets.

    Returns {mirrored, reviewed, errors}. Never raises — the caller runs it after the job mirror,
    so a hours failure must never abort the cycle. Per-job (sheet) + per-entry fences; a permanent
    failure routes to the Review Queue; a transient one leaves the entry unmirrored (mirrored_at
    stays NULL) for the next cycle. mark-mirrored is the LAST step (crash-safe: a crash before it
    re-mirrors idempotently — the sheet find-or-create by Entry UUID no-ops).
    """
    out = {"mirrored": 0, "reviewed": 0, "errors": 0}
    try:
        entries = portal_client.get_fieldops_pending_hours(base_url, bearer)
    except portal_client.PortalAuthError as exc:
        # 401 on the SAME bearer that just drained pending-jobs — surface (bad/rotated token) but
        # do not crash: the job pass may have succeeded before a mid-cycle rotation.
        out["errors"] += 1
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            f"hours-pending fetch UNAUTHORIZED (401) — field-ops bearer rejected; hours up-sync "
            f"skipped this cycle: {exc!r}",
            error_code="fieldops_hours_pending_auth_failed",
        )
        return out
    except portal_client.PortalTransportError as exc:
        out["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"hours-pending fetch failed (entries left unmirrored for next cycle): {exc!r}",
            error_code="fieldops_hours_pending_fetch_failed",
        )
        return out

    succeeded: list[str] = []
    for job_id, (project_name, rows) in _group_hours_by_job(entries).items():
        correlation_id = uuid.uuid4().hex[:12]
        try:
            sheet_id = hours_log.ensure_hours_log_sheet(project_name)
        except (
            picklist_validation.PicklistViolationError,
            smartsheet_client.SmartsheetValidationError,
            # A §46 share change or a deleted tracker sheet is PERMANENT: without these two
            # the fault fell to the transient arm below and logged "re-projects next cycle"
            # forever — no ticket, no CRITICAL, no operator ever told.
            smartsheet_client.SmartsheetPermissionError,
            smartsheet_client.SmartsheetNotFoundError,
        ) as exc:
            out["reviewed"] += 1
            _route_hours_to_review(job_id, project_name, exc, correlation_id, phase="ensure-sheet")
            continue
        except Exception as exc:  # noqa: BLE001 — per-job fence; one bad job never kills the pass
            out["errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"transient failure ensuring Hours Log sheet for {project_name!r} "
                f"(job {job_id}); entries left unmirrored: {type(exc).__name__}: {exc!r}",
                error_code="fieldops_hours_sheet_transient",
                correlation_id=correlation_id,
            )
            continue

        for e in rows:
            entry_uuid = str(e["uuid"]).strip()
            try:
                hours_log.upsert_entry_row(
                    sheet_id,
                    entry_uuid=entry_uuid,
                    # The portal daily-report time form never populates the wall-clock times, so
                    # /hours-pending no longer projects work_started_at/_ended_at (2026-07-05); the
                    # Hours Log work day is the server record date. See docs/tech_debt.md
                    # "Hours Log — replace Started/Ended columns with a Task column".
                    work_date=_fmt_epoch_date(e.get("created_at")),
                    personnel=str(e.get("personnel_name") or "").strip(),
                    hours=_fmt_hours(e.get("hours")),
                    task=str(e.get("task") or "").strip(),
                    notes=str(e.get("notes") or "").strip(),
                    recorded_at=_fmt_epoch_dt(e.get("created_at")),
                )
                amends = str(e.get("amends_uuid") or "").strip()
                if amends and not hours_log.supersede_entry_row(sheet_id, amends, entry_uuid):
                    # The amend names an entry we never mirrored — surface, do NOT block the amend
                    # (its own Active row is written; the prior may arrive later, out of order).
                    error_log.log(
                        Severity.WARN, SCRIPT_NAME,
                        f"hours amend {entry_uuid!r} names prior {amends!r} not yet on the Hours "
                        f"Log for {project_name!r} — amend row written, prior left unmarked",
                        error_code="fieldops_hours_amend_prior_missing",
                        correlation_id=correlation_id,
                    )
                succeeded.append(entry_uuid)
            except (
                picklist_validation.PicklistViolationError,
                smartsheet_client.SmartsheetValidationError,
                # A §46 share change or a deleted tracker sheet is PERMANENT: without these
                # two the fault fell to the transient arm and logged "re-projects next
                # cycle" forever — no ticket, no CRITICAL, no operator ever told.
                smartsheet_client.SmartsheetPermissionError,
                smartsheet_client.SmartsheetNotFoundError,
            ) as exc:
                out["reviewed"] += 1
                _route_hours_to_review(
                    job_id, project_name, exc, correlation_id, phase="upsert", entry_uuid=entry_uuid
                )
            except Exception as exc:  # noqa: BLE001 — per-entry fence
                out["errors"] += 1
                error_log.log(
                    Severity.ERROR, SCRIPT_NAME,
                    f"transient failure mirroring hours entry {entry_uuid!r} for {project_name!r} "
                    f"(left unmirrored): {type(exc).__name__}: {exc!r}",
                    error_code="fieldops_hours_entry_transient",
                    correlation_id=correlation_id,
                )

        # §51 A5 row-cap watchdog (SoR-safe, refined per the 2026-07-04 v19.x rider): once per job
        # after its upserts, WARN + Review-Queue an operator period-split as the standing sheet
        # nears the row cap. Advisory — check_row_cap owns its try/except, so it never raises here.
        hours_log.check_row_cap(sheet_id, hours_log.hours_log_sheet_name(project_name))

    if succeeded:
        try:
            portal_client.mark_fieldops_hours_mirrored(base_url, bearer, succeeded)
            out["mirrored"] += len(succeeded)
        except portal_client.PortalAuthError as exc:
            out["errors"] += 1
            error_log.log(
                Severity.CRITICAL, SCRIPT_NAME,
                f"hours mark-mirrored UNAUTHORIZED (401) — {len(succeeded)} entries filed to the "
                f"Hours Log but the D1 watermark did not advance; safe re-mirror (idempotent "
                f"find-or-create) once the bearer is fixed: {exc!r}",
                error_code="fieldops_hours_mark_mirrored_unauthorized",
            )
        except portal_client.PortalTransportError as exc:
            out["errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"hours mark-mirrored failed for {len(succeeded)} entries (filed to the Hours Log; "
                f"re-mirrored idempotently next cycle): {exc!r}",
                error_code="fieldops_hours_mark_mirrored_failed",
            )
    return out


def _route_hours_to_review(
    job_id: str, project_name: str, exc: Exception, correlation_id: str,
    *, phase: str, entry_uuid: str | None = None,
) -> None:
    """Route a PERMANENTLY-failed hours mirror to ITS_Review_Queue (workstream progress_reports)."""
    review_queue.safe_add(
        script_name=SCRIPT_NAME,
        workstream="progress_reports",
        summary=(
            f"field-ops Hours Log up-sync: PERMANENT failure ({phase}) for job {job_id!r} "
            f"({project_name!r}, {type(exc).__name__})"
            + (f" entry {entry_uuid!r}" if entry_uuid else "")
            + " — left unmirrored, needs operator fix"
        ),
        payload={
            "job_id": job_id,
            "project_name": project_name,
            "phase": phase,
            "entry_uuid": entry_uuid,
            "error": f"{type(exc).__name__}: {exc!r}",
        },
        sla_tier=review_queue.SlaTier.SAFETY_INTAKE,
        reason=review_queue.ReviewReason.POLICY_EDGE,
        severity=Severity.WARN,
        source_file=entry_uuid or job_id,
    )
    error_log.log(
        Severity.WARN, SCRIPT_NAME,
        f"hours mirror routed to Review Queue (permanent, {phase}) job={job_id!r} "
        f"entry={entry_uuid!r}: {type(exc).__name__}: {exc!r}",
        error_code="fieldops_hours_permanent",
        correlation_id=correlation_id,
    )


# ---- P7 Equipment Status & Location snapshot pass (Track 2, Slice 2) -------------


def _fmt_coord(value: Any) -> str:
    """A latitude/longitude REAL → a trimmed string ('' when NULL/unavailable)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    return f"{value:g}"


def _group_equipment_by_job(
    rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group CURRENT snapshot equipment rows by job_id → [rows]. Skips a row missing its
    equipment_id / job_id (a data anomaly that can't be keyed) — never silent. The project_name is
    NOT required here (it comes from the reconcile roster, the authoritative jobs-table source)."""
    by_job: dict[str, list[dict[str, Any]]] = {}
    for e in rows:
        equipment_id = str(e.get("equipment_id") or "").strip()
        job_id = str(e.get("job_id") or "").strip()
        if not equipment_id or not job_id:
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"equipment snapshot row skipped — missing equipment_id/job_id "
                f"(equipment_id={e.get('equipment_id')!r} job_id={e.get('job_id')!r})",
                error_code="fieldops_equipment_row_malformed",
            )
            continue
        by_job.setdefault(job_id, []).append(e)
    return by_job


def _mirror_equipment_pass(base_url: str, bearer: str) -> dict[str, int]:
    """Re-project the CURRENT on-active-job equipment snapshot UP into per-job Equipment sheets.

    Returns {upserted, retired, reviewed, errors}. Never raises — the caller runs it after the job
    + hours mirrors, so an equipment failure must never abort the cycle. Per-job (sheet) + per-item
    fences; a permanent failure routes to the Review Queue; a transient one is simply left for the
    next cycle (the snapshot re-projects the whole live state every cycle, so nothing is "lost" —
    there is NO watermark, NO mark-mirrored).

    RECONCILE ROSTER (the count-drops-to-zero fix): the pass iterates `jobs_with_equipment` — every
    active job with ANY equipment_location history — NOT just the jobs that have current equipment
    this cycle. For each job:
      • HAS current equipment → ensure the sheet (find-or-create) → upsert each (change-only) →
        retire any sheet row NOT in THIS cycle's snapshot (Off Job, never delete) → row-cap watchdog.
      • ZERO current equipment → find (NEVER create) the sheet; if it exists, retire ALL its Active
        rows (Off Job); if no sheet ever existed, skip (never create an empty sheet). Without this
        branch a job whose whole complement moved away would keep stale `On Job=Active` rows forever.

    No throttle: per-cycle change-only re-projection is simple-correct at current scale (an upsert
    with no change is a no-op read, and retire skips already-Off-Job rows). A throttle is a FUTURE
    optimization if the 20×20 read-load bites — do NOT build it now.
    """
    out = {"upserted": 0, "retired": 0, "reviewed": 0, "errors": 0}
    try:
        snapshot = portal_client.get_fieldops_equipment_snapshot(base_url, bearer)
    except portal_client.PortalAuthError as exc:
        # 401 on the SAME bearer that just drained pending-jobs/hours — surface (bad/rotated token)
        # but do not crash: the earlier passes may have succeeded before a mid-cycle rotation.
        out["errors"] += 1
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            f"equipment-snapshot fetch UNAUTHORIZED (401) — field-ops bearer rejected; equipment "
            f"snapshot skipped this cycle: {exc!r}",
            error_code="fieldops_equipment_snapshot_auth_failed",
        )
        return out
    except portal_client.PortalTransportError as exc:
        out["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"equipment-snapshot fetch failed (snapshot re-projects next cycle): {exc!r}",
            error_code="fieldops_equipment_snapshot_fetch_failed",
        )
        return out

    by_job = _group_equipment_by_job(snapshot.equipment)
    now_iso = datetime.now(_PACIFIC).isoformat()
    # Iterate the RECONCILE ROSTER (active jobs with equipment_location history), so a job whose
    # current complement dropped to ZERO is still visited and its stale Active rows retired.
    for roster in snapshot.jobs_with_equipment:
        job_id = str(roster.get("job_id") or "").strip()
        project_name = str(roster.get("project_name") or "").strip()
        if not job_id or not project_name:
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"equipment roster row skipped — missing job_id/project_name "
                f"(job_id={roster.get('job_id')!r} project_name={roster.get('project_name')!r})",
                error_code="fieldops_equipment_roster_malformed",
            )
            continue
        correlation_id = uuid.uuid4().hex[:12]
        current = by_job.get(job_id)
        if current:
            _reconcile_job_with_equipment(
                job_id, project_name, current, now_iso, correlation_id, out
            )
        else:
            _reconcile_job_zeroed(job_id, project_name, correlation_id, out)
    return out


def _reconcile_job_with_equipment(
    job_id: str, project_name: str, rows: list[dict[str, Any]], now_iso: str,
    correlation_id: str, out: dict[str, int],
) -> None:
    """Reconcile a job that HAS current on-job equipment: find-or-create its sheet, change-only
    upsert each item, retire any sheet row NOT in this cycle's snapshot, run the row-cap watchdog."""
    try:
        sheet_id = equipment_status.ensure_equipment_sheet(project_name)
    except (
        picklist_validation.PicklistViolationError,
        smartsheet_client.SmartsheetValidationError,
        # A §46 share change or a deleted tracker sheet is PERMANENT: without these two
        # the fault fell to the transient arm below and logged "re-projects next cycle"
        # forever — no ticket, no CRITICAL, no operator ever told.
        smartsheet_client.SmartsheetPermissionError,
        smartsheet_client.SmartsheetNotFoundError,
    ) as exc:
        out["reviewed"] += 1
        _route_equipment_to_review(job_id, project_name, exc, correlation_id, phase="ensure-sheet")
        return
    except Exception as exc:  # noqa: BLE001 — per-job fence; one bad job never kills the pass
        out["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"transient failure ensuring Equipment sheet for {project_name!r} "
            f"(job {job_id}); snapshot re-projects next cycle: {type(exc).__name__}: {exc!r}",
            error_code="fieldops_equipment_sheet_transient",
            correlation_id=correlation_id,
        )
        return

    # The authoritative on-job set for retire = EVERY equipment in this cycle's snapshot for the
    # job, regardless of per-item upsert success (a transient upsert failure does NOT mean the item
    # left the job — retiring it would be wrong; it re-upserts next cycle).
    snapshot_ids = {str(e.get("equipment_id") or "").strip() for e in rows}
    for e in rows:
        equipment_id = str(e.get("equipment_id") or "").strip()
        try:
            equipment_status.upsert_equipment_row(
                sheet_id,
                equipment_id=equipment_id,
                name=str(e.get("name") or "").strip(),
                kind=str(e.get("kind") or "").strip(),
                unit_no=str(e.get("identifier") or "").strip(),
                status=str(e.get("status") or "").strip(),
                status_note=str(e.get("status_note") or "").strip(),
                status_changed=_fmt_epoch_date(e.get("status_changed_at")),
                location=str(e.get("location_label") or "").strip(),
                lat=_fmt_coord(e.get("lat")),
                lon=_fmt_coord(e.get("lon")),
                location_read_at=_fmt_epoch_dt(e.get("read_at")),
                updated_at=now_iso,
            )
            out["upserted"] += 1
        except (
            picklist_validation.PicklistViolationError,
            smartsheet_client.SmartsheetValidationError,
            # A §46 share change or a deleted tracker sheet is PERMANENT: without these two
            # the fault fell to the transient arm below and logged "re-projects next cycle"
            # forever — no ticket, no CRITICAL, no operator ever told.
            smartsheet_client.SmartsheetPermissionError,
            smartsheet_client.SmartsheetNotFoundError,
        ) as exc:
            out["reviewed"] += 1
            _route_equipment_to_review(
                job_id, project_name, exc, correlation_id,
                phase="upsert", equipment_id=equipment_id,
            )
        except Exception as exc:  # noqa: BLE001 — per-item fence
            out["errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"transient failure upserting equipment {equipment_id!r} for {project_name!r} "
                f"(snapshot re-projects next cycle): {type(exc).__name__}: {exc!r}",
                error_code="fieldops_equipment_upsert_transient",
                correlation_id=correlation_id,
            )

    _retire_equipment(sheet_id, snapshot_ids, job_id, project_name, correlation_id, out)

    # §51 A5 row-cap watchdog — advisory, owns its own try/except (never raises here).
    equipment_status.check_row_cap(
        sheet_id, equipment_status.equipment_sheet_name(project_name)
    )


def _reconcile_job_zeroed(
    job_id: str, project_name: str, correlation_id: str, out: dict[str, int],
) -> None:
    """Reconcile a job with ZERO current on-job equipment (its whole complement moved away / went
    inactive): FIND (never create) its Equipment sheet and retire ALL remaining Active rows. If no
    sheet ever existed, skip — never create an empty sheet. This is the count-drops-to-zero fix; the
    normal zero case (no sheet) is a silent no-op, NOT an error."""
    try:
        sheet_id = equipment_status.find_equipment_sheet(project_name)
    except (
        picklist_validation.PicklistViolationError,
        smartsheet_client.SmartsheetValidationError,
        # A §46 share change or a deleted tracker sheet is PERMANENT: without these two
        # the fault fell to the transient arm below and logged "re-projects next cycle"
        # forever — no ticket, no CRITICAL, no operator ever told.
        smartsheet_client.SmartsheetPermissionError,
        smartsheet_client.SmartsheetNotFoundError,
    ) as exc:
        out["reviewed"] += 1
        _route_equipment_to_review(job_id, project_name, exc, correlation_id, phase="find-sheet")
        return
    except Exception as exc:  # noqa: BLE001 — per-job fence
        out["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"transient failure finding Equipment sheet for {project_name!r} "
            f"(job {job_id}); re-projects next cycle: {type(exc).__name__}: {exc!r}",
            error_code="fieldops_equipment_find_sheet_transient",
            correlation_id=correlation_id,
        )
        return
    if sheet_id is None:
        # No sheet was ever created for this job → nothing to retire, and we NEVER create an empty
        # sheet. The common zero case — a silent no-op, not a fault.
        return
    # Retire EVERY row (empty current set → all rows Off Job). retire_off_job is idempotent, so a
    # steady all-Off-Job sheet issues no write.
    _retire_equipment(sheet_id, set(), job_id, project_name, correlation_id, out)


def _retire_equipment(
    sheet_id: int, current_ids: set[str], job_id: str, project_name: str,
    correlation_id: str, out: dict[str, int],
) -> None:
    """Retire any sheet row whose Equipment ID is NOT in `current_ids` (Off Job, never delete),
    under a fence — a retire failure never blocks the caller's row-cap watchdog and re-projects next
    cycle. An empty `current_ids` retires the whole sheet (the reconcile-zeroed case)."""
    try:
        out["retired"] += equipment_status.retire_off_job(sheet_id, current_ids)
    except (
        picklist_validation.PicklistViolationError,
        smartsheet_client.SmartsheetValidationError,
        # A §46 share change or a deleted tracker sheet is PERMANENT: without these two
        # the fault fell to the transient arm below and logged "re-projects next cycle"
        # forever — no ticket, no CRITICAL, no operator ever told.
        smartsheet_client.SmartsheetPermissionError,
        smartsheet_client.SmartsheetNotFoundError,
    ) as exc:
        out["reviewed"] += 1
        _route_equipment_to_review(job_id, project_name, exc, correlation_id, phase="retire")
    except Exception as exc:  # noqa: BLE001 — per-job fence
        out["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"transient failure retiring off-job equipment for {project_name!r} "
            f"(job {job_id}); re-projects next cycle: {type(exc).__name__}: {exc!r}",
            error_code="fieldops_equipment_retire_transient",
            correlation_id=correlation_id,
        )


def _route_equipment_to_review(
    job_id: str, project_name: str, exc: Exception, correlation_id: str,
    *, phase: str, equipment_id: str | None = None,
) -> None:
    """Route a PERMANENTLY-failed equipment mirror to ITS_Review_Queue (workstream
    progress_reports)."""
    review_queue.safe_add(
        script_name=SCRIPT_NAME,
        workstream="progress_reports",
        summary=(
            f"field-ops Equipment snapshot up-sync: PERMANENT failure ({phase}) for job {job_id!r} "
            f"({project_name!r}, {type(exc).__name__})"
            + (f" equipment {equipment_id!r}" if equipment_id else "")
            + " — re-projects next cycle, needs operator fix"
        ),
        payload={
            "job_id": job_id,
            "project_name": project_name,
            "phase": phase,
            "equipment_id": equipment_id,
            "error": f"{type(exc).__name__}: {exc!r}",
        },
        sla_tier=review_queue.SlaTier.SAFETY_INTAKE,
        reason=review_queue.ReviewReason.POLICY_EDGE,
        severity=Severity.WARN,
        source_file=equipment_id or job_id,
    )
    error_log.log(
        Severity.WARN, SCRIPT_NAME,
        f"equipment mirror routed to Review Queue (permanent, {phase}) job={job_id!r} "
        f"equipment={equipment_id!r}: {type(exc).__name__}: {exc!r}",
        error_code="fieldops_equipment_permanent",
        correlation_id=correlation_id,
    )


# ---- P7 Material List snapshot pass (Track 2, M2) --------------------------------


def _fmt_qty(value: Any) -> str:
    """A material quantity REAL → a trimmed string ('' when NULL/unset)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    return f"{value:g}"


def _group_materials_by_job(
    rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group CURRENT snapshot material lines by job_id → [rows]. Skips a row missing its
    line_uuid / job_id (a data anomaly that can't be keyed) — never silent. The project_name is NOT
    required here (it comes from the reconcile roster, the authoritative jobs-table source)."""
    by_job: dict[str, list[dict[str, Any]]] = {}
    for e in rows:
        line_uuid = str(e.get("line_uuid") or "").strip()
        job_id = str(e.get("job_id") or "").strip()
        if not line_uuid or not job_id:
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"material line skipped — missing line_uuid/job_id "
                f"(line_uuid={e.get('line_uuid')!r} job_id={e.get('job_id')!r})",
                error_code="fieldops_material_row_malformed",
            )
            continue
        by_job.setdefault(job_id, []).append(e)
    return by_job


def _mirror_material_list_pass(base_url: str, bearer: str) -> dict[str, int]:
    """Re-project the CURRENT per-job Material List UP into per-job Material List sheets.

    Returns {upserted, retired, reviewed, errors}. Never raises — the caller runs it after the job +
    hours + equipment mirrors, so a material failure must never abort the cycle. Per-job (sheet) +
    per-line fences; a permanent failure routes to the Review Queue; a transient one is simply left
    for the next cycle (the snapshot re-projects the whole live list every cycle, so nothing is
    "lost" — there is NO watermark, NO mark-mirrored).

    RECONCILE ROSTER (the count-drops-to-zero fix): the pass iterates `jobs_with_materials` — every
    active job with ANY `job_expected_materials` row (active OR deactivated) — NOT just the jobs that
    have active lines this cycle. For each job:
      • HAS active lines → ensure the sheet (find-or-create) → upsert each (change-only) → mark
        Removed any sheet row NOT in THIS cycle's snapshot (On List=Removed, never delete) →
        row-cap watchdog.
      • ZERO active lines → find (NEVER create) the sheet; if it exists, mark ALL its Active rows
        Removed; if no sheet ever existed, skip (never create an empty sheet). Without this branch a
        job whose whole list was deactivated would keep stale `On List=Active` rows forever.

    No throttle: per-cycle change-only re-projection is simple-correct at current scale (an upsert
    with no change is a no-op read, and retire skips already-Removed rows). A throttle is a FUTURE
    optimization if read-load bites — do NOT build it now.
    """
    out = {"upserted": 0, "retired": 0, "reviewed": 0, "errors": 0}
    try:
        snapshot = portal_client.get_fieldops_material_list_snapshot(base_url, bearer)
    except portal_client.PortalAuthError as exc:
        # 401 on the SAME bearer that just drained pending-jobs/hours/equipment — surface (bad/rotated
        # token) but do not crash: the earlier passes may have succeeded before a mid-cycle rotation.
        out["errors"] += 1
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            f"material-list-snapshot fetch UNAUTHORIZED (401) — field-ops bearer rejected; material "
            f"snapshot skipped this cycle: {exc!r}",
            error_code="fieldops_material_snapshot_auth_failed",
        )
        return out
    except portal_client.PortalTransportError as exc:
        out["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"material-list-snapshot fetch failed (snapshot re-projects next cycle): {exc!r}",
            error_code="fieldops_material_snapshot_fetch_failed",
        )
        return out

    by_job = _group_materials_by_job(snapshot.lines)
    # Iterate the RECONCILE ROSTER (active jobs with any material line history), so a job whose
    # active lines dropped to ZERO is still visited and its stale Active rows marked Removed.
    for roster in snapshot.jobs_with_materials:
        job_id = str(roster.get("job_id") or "").strip()
        project_name = str(roster.get("project_name") or "").strip()
        if not job_id or not project_name:
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"material roster row skipped — missing job_id/project_name "
                f"(job_id={roster.get('job_id')!r} project_name={roster.get('project_name')!r})",
                error_code="fieldops_material_roster_malformed",
            )
            continue
        correlation_id = uuid.uuid4().hex[:12]
        current = by_job.get(job_id)
        if current:
            _reconcile_job_with_materials(
                job_id, project_name, current, correlation_id, out
            )
        else:
            _reconcile_job_zeroed_materials(job_id, project_name, correlation_id, out)
    return out


def _reconcile_job_with_materials(
    job_id: str, project_name: str, rows: list[dict[str, Any]],
    correlation_id: str, out: dict[str, int],
) -> None:
    """Reconcile a job that HAS active material lines: find-or-create its sheet, change-only upsert
    each line, mark Removed any sheet row NOT in this cycle's snapshot, run the row-cap watchdog."""
    try:
        sheet_id = material_list.ensure_material_list_sheet(project_name)
    except (
        picklist_validation.PicklistViolationError,
        smartsheet_client.SmartsheetValidationError,
        # A §46 share change or a deleted tracker sheet is PERMANENT: without these two
        # the fault fell to the transient arm below and logged "re-projects next cycle"
        # forever — no ticket, no CRITICAL, no operator ever told.
        smartsheet_client.SmartsheetPermissionError,
        smartsheet_client.SmartsheetNotFoundError,
    ) as exc:
        out["reviewed"] += 1
        _route_material_to_review(job_id, project_name, exc, correlation_id, phase="ensure-sheet")
        return
    except Exception as exc:  # noqa: BLE001 — per-job fence; one bad job never kills the pass
        out["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"transient failure ensuring Material List sheet for {project_name!r} "
            f"(job {job_id}); snapshot re-projects next cycle: {type(exc).__name__}: {exc!r}",
            error_code="fieldops_material_sheet_transient",
            correlation_id=correlation_id,
        )
        return

    # The authoritative on-list set for retire = EVERY line in this cycle's snapshot for the job,
    # regardless of per-line upsert success (a transient upsert failure does NOT mean the line was
    # removed — marking it Removed would be wrong; it re-upserts next cycle).
    snapshot_uuids = {str(e.get("line_uuid") or "").strip() for e in rows}
    for e in rows:
        line_uuid = str(e.get("line_uuid") or "").strip()
        catalog_name = str(e.get("catalog_name") or "").strip()
        description = str(e.get("description") or "").strip()
        try:
            material_list.upsert_line_row(
                sheet_id,
                line_uuid=line_uuid,
                line=(catalog_name or description),
                material=(catalog_name or material_list.MATERIAL_NONE),
                description=description,
                qty=_fmt_qty(e.get("qty")),
                unit=str(e.get("unit") or "").strip(),
                expected_date=str(e.get("expected_date") or "").strip(),
                status=str(e.get("status") or "").strip(),
                delivered_qty=_fmt_qty(e.get("qty_received")),
                received_at=_fmt_epoch_date(e.get("received_at")),
                received_by=str(e.get("received_by_display") or "").strip(),
                note=str(e.get("note") or "").strip(),
                unplanned=material_list.UNPLANNED_YES if e.get("unplanned") else "",
                # 0059 line additions. `expected_ship_date` is the SHIP date — deliberately NOT the
                # same field as `expected_date` above, which is the expected DELIVERY date; both are
                # mirrored because the office schedules against one and receives against the other.
                part_number=str(e.get("part_number") or "").strip(),
                category=str(e.get("category") or "").strip(),
                expected_ship_date=str(e.get("expected_ship_date") or "").strip(),
            )
            out["upserted"] += 1
        except (
            picklist_validation.PicklistViolationError,
            smartsheet_client.SmartsheetValidationError,
            # A §46 share change or a deleted tracker sheet is PERMANENT: without these two
            # the fault fell to the transient arm below and logged "re-projects next cycle"
            # forever — no ticket, no CRITICAL, no operator ever told.
            smartsheet_client.SmartsheetPermissionError,
            smartsheet_client.SmartsheetNotFoundError,
        ) as exc:
            out["reviewed"] += 1
            _route_material_to_review(
                job_id, project_name, exc, correlation_id,
                phase="upsert", line_uuid=line_uuid,
            )
        except Exception as exc:  # noqa: BLE001 — per-line fence
            out["errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"transient failure upserting material line {line_uuid!r} for {project_name!r} "
                f"(snapshot re-projects next cycle): {type(exc).__name__}: {exc!r}",
                error_code="fieldops_material_upsert_transient",
                correlation_id=correlation_id,
            )

    _retire_materials(sheet_id, snapshot_uuids, job_id, project_name, correlation_id, out)

    # §51 A5 row-cap watchdog — advisory, owns its own try/except (never raises here).
    material_list.check_row_cap(
        sheet_id, material_list.material_list_sheet_name(project_name)
    )


def _reconcile_job_zeroed_materials(
    job_id: str, project_name: str, correlation_id: str, out: dict[str, int],
) -> None:
    """Reconcile a job with ZERO active material lines (its whole list was deactivated): FIND (never
    create) its Material List sheet and mark ALL remaining Active rows Removed. If no sheet ever
    existed, skip — never create an empty sheet. This is the count-drops-to-zero fix; the normal
    zero case (no sheet) is a silent no-op, NOT an error."""
    try:
        sheet_id = material_list.find_material_list_sheet(project_name)
    except (
        picklist_validation.PicklistViolationError,
        smartsheet_client.SmartsheetValidationError,
        # A §46 share change or a deleted tracker sheet is PERMANENT: without these two
        # the fault fell to the transient arm below and logged "re-projects next cycle"
        # forever — no ticket, no CRITICAL, no operator ever told.
        smartsheet_client.SmartsheetPermissionError,
        smartsheet_client.SmartsheetNotFoundError,
    ) as exc:
        out["reviewed"] += 1
        _route_material_to_review(job_id, project_name, exc, correlation_id, phase="find-sheet")
        return
    except Exception as exc:  # noqa: BLE001 — per-job fence
        out["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"transient failure finding Material List sheet for {project_name!r} "
            f"(job {job_id}); re-projects next cycle: {type(exc).__name__}: {exc!r}",
            error_code="fieldops_material_find_sheet_transient",
            correlation_id=correlation_id,
        )
        return
    if sheet_id is None:
        # No sheet was ever created for this job → nothing to retire, and we NEVER create an empty
        # sheet. The common zero case — a silent no-op, not a fault.
        return
    # Mark EVERY row Removed (empty current set → all rows Removed). retire_removed is idempotent, so
    # a steady all-Removed sheet issues no write.
    _retire_materials(sheet_id, set(), job_id, project_name, correlation_id, out)


def _retire_materials(
    sheet_id: int, current_uuids: set[str], job_id: str, project_name: str,
    correlation_id: str, out: dict[str, int],
) -> None:
    """Mark Removed any sheet row whose Line UUID is NOT in `current_uuids` (On List=Removed, never
    delete), under a fence — a retire failure never blocks the caller's row-cap watchdog and
    re-projects next cycle. An empty `current_uuids` marks the whole sheet Removed (the
    reconcile-zeroed case)."""
    try:
        out["retired"] += material_list.retire_removed(sheet_id, current_uuids)
    except (
        picklist_validation.PicklistViolationError,
        smartsheet_client.SmartsheetValidationError,
        # A §46 share change or a deleted tracker sheet is PERMANENT: without these two
        # the fault fell to the transient arm below and logged "re-projects next cycle"
        # forever — no ticket, no CRITICAL, no operator ever told.
        smartsheet_client.SmartsheetPermissionError,
        smartsheet_client.SmartsheetNotFoundError,
    ) as exc:
        out["reviewed"] += 1
        _route_material_to_review(job_id, project_name, exc, correlation_id, phase="retire")
    except Exception as exc:  # noqa: BLE001 — per-job fence
        out["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"transient failure marking removed material lines for {project_name!r} "
            f"(job {job_id}); re-projects next cycle: {type(exc).__name__}: {exc!r}",
            error_code="fieldops_material_retire_transient",
            correlation_id=correlation_id,
        )


def _route_material_to_review(
    job_id: str, project_name: str, exc: Exception, correlation_id: str,
    *, phase: str, line_uuid: str | None = None,
) -> None:
    """Route a PERMANENTLY-failed material mirror to ITS_Review_Queue (workstream
    progress_reports)."""
    review_queue.safe_add(
        script_name=SCRIPT_NAME,
        workstream="progress_reports",
        summary=(
            f"field-ops Material List up-sync: PERMANENT failure ({phase}) for job {job_id!r} "
            f"({project_name!r}, {type(exc).__name__})"
            + (f" line {line_uuid!r}" if line_uuid else "")
            + " — re-projects next cycle, needs operator fix"
        ),
        payload={
            "job_id": job_id,
            "project_name": project_name,
            "phase": phase,
            "line_uuid": line_uuid,
            "error": f"{type(exc).__name__}: {exc!r}",
        },
        sla_tier=review_queue.SlaTier.SAFETY_INTAKE,
        reason=review_queue.ReviewReason.POLICY_EDGE,
        severity=Severity.WARN,
        source_file=line_uuid or job_id,
    )
    error_log.log(
        Severity.WARN, SCRIPT_NAME,
        f"material mirror routed to Review Queue (permanent, {phase}) job={job_id!r} "
        f"line={line_uuid!r}: {type(exc).__name__}: {exc!r}",
        error_code="fieldops_material_permanent",
        correlation_id=correlation_id,
    )


# ---- P7 Material Incidents ledger pass (Track 2, M3 Slice 2) ----------------------


def _group_incidents_by_job(
    rows: list[dict[str, Any]],
) -> dict[str, tuple[str, list[dict[str, Any]]]]:
    """Group filed incident rows by job_id → (project_name, [rows]). Skips a row missing its
    submission_uuid / job_id / project_name (a data anomaly that can't be keyed / foldered) — never
    silent. Unlike the material-list grouper there is no reconcile roster, so project_name MUST ride
    each incident row (the Worker's active-job JOIN supplies it)."""
    by_job: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for e in rows:
        incident_uuid = str(e.get("submission_uuid") or "").strip()
        job_id = str(e.get("job_id") or "").strip()
        project = str(e.get("project_name") or "").strip()
        if not incident_uuid or not job_id or not project:
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"material incident skipped — missing submission_uuid/job_id/project_name "
                f"(submission_uuid={e.get('submission_uuid')!r} job_id={e.get('job_id')!r})",
                error_code="fieldops_incident_row_malformed",
            )
            continue
        by_job.setdefault(job_id, (project, []))[1].append(e)
    return by_job


def _mirror_material_incidents_pass(base_url: str, bearer: str) -> dict[str, int]:
    """Re-project the CURRENT filed material-incident ledger UP into per-job Material Incidents sheets.

    Returns {upserted, reviewed, errors}. Never raises — the caller runs it after the job + hours +
    equipment + material mirrors, so an incident failure must never abort the cycle. Per-job (sheet) +
    per-incident fences; a permanent failure routes to the Review Queue; a transient one is simply left
    for the next cycle (the filed set re-projects every cycle, so nothing is "lost" — there is NO
    watermark, NO mark-mirrored).

    APPEND-ONLY LEDGER (the deliberate contrast with the material-list snapshot): each incident is an
    immutable filed event, so there is NO reconcile roster and NO retire — a job with zero incidents
    simply produces no rows and is never visited (no sheet is created for it). The count-drops-to-zero
    / #468 zero-drop class is structurally impossible: there is no retire path to wrongly zero.

    No throttle: per-cycle change-only re-projection is simple-correct at current scale (an upsert with
    no change is a no-op read). A throttle is a FUTURE optimization if read-load bites — do NOT build
    it now.
    """
    out = {"upserted": 0, "reviewed": 0, "errors": 0}
    try:
        incidents = portal_client.get_fieldops_material_incidents(base_url, bearer)
    except portal_client.PortalAuthError as exc:
        # 401 on the SAME bearer that just drained the earlier passes — surface (bad/rotated token)
        # but do not crash: the earlier passes may have succeeded before a mid-cycle rotation.
        out["errors"] += 1
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            f"material-incidents fetch UNAUTHORIZED (401) — field-ops bearer rejected; incident "
            f"ledger skipped this cycle: {exc!r}",
            error_code="fieldops_incidents_fetch_auth_failed",
        )
        return out
    except portal_client.PortalTransportError as exc:
        out["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"material-incidents fetch failed (ledger re-projects next cycle): {exc!r}",
            error_code="fieldops_incidents_fetch_failed",
        )
        return out

    for job_id, (project_name, rows) in _group_incidents_by_job(incidents).items():
        correlation_id = uuid.uuid4().hex[:12]
        _reconcile_job_incidents(job_id, project_name, rows, correlation_id, out)
    return out


def _archive_pass(base_url: str, bearer: str) -> dict[str, int]:
    """Drain the archive queue: relocate each queued job's containers and report the outcome back.

    Returns {complete, partial, failed, capped, errors}. Never raises — it runs after every mirror
    pass, so an archive failure must never abort the cycle or the heartbeat below.

    THE QUEUE IS THE RETRY. This pass deliberately does NOT ride the job-dirty list: that list is
    cleared the moment an unrelated mirror succeeds, which is exactly why the pre-Track-6 archive
    move "did not auto-retry" and why a failed relocation was permanent. `/archive-pending` keeps
    serving a job until its archive reaches a terminal state, so a failure here is genuinely
    resumable and needs no bookkeeping of its own.

    The ADMIN pre-flight runs ONCE per cycle rather than per job — it is five workspace reads and
    the answer cannot differ between jobs in the same cycle. It runs only when the queue is
    non-empty, so an idle daemon costs nothing.
    """
    out = {"complete": 0, "partial": 0, "failed": 0, "capped": 0, "errors": 0}
    try:
        jobs = portal_client.get_fieldops_pending_archives(base_url, bearer)
    except portal_client.PortalAuthError as exc:
        out["errors"] += 1
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            f"archive-queue fetch UNAUTHORIZED (401) — field-ops bearer rejected; no job was "
            f"relocated this cycle: {exc!r}",
            error_code="fieldops_archive_fetch_auth_failed",
        )
        return out
    except portal_client.PortalTransportError as exc:
        out["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"archive-queue fetch failed (the queue re-serves next cycle): {exc!r}",
            error_code="fieldops_archive_fetch_failed",
        )
        return out

    if not jobs:
        return out

    if not job_archive.verify_archive_capability():
        # The probe already WARNed naming the deficient workspace. Skipping the whole pass is right
        # HERE (unlike the per-container fences inside it): every job would 403 on the same
        # shortfall, so attempting them would produce N identical failures and burn N attempts
        # against the retry cap for a condition no retry can fix.
        out["errors"] += 1
        return out

    updates: list[dict[str, Any]] = []
    for job in jobs:
        job_id = str(job.get("job_id") or "")
        direction = str(job.get("archive_direction") or "")
        try:
            attempts = int(job.get("archive_attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0

        if attempts >= job_archive.MAX_ARCHIVE_ATTEMPTS:
            # A PERMANENT condition (a deleted destination, a share never fixed) would otherwise
            # re-fire the whole eight-container sequence every cycle forever. The operator's "Try
            # again" resets the counter in D1. Deliberately NOT logged per cycle: the failures that
            # got it here already have their own ITS_Errors rows, and a WARN every few minutes for
            # a row nobody is watching is how a log stops being read.
            out["capped"] += 1
            continue

        correlation_id = uuid.uuid4().hex[:12]
        try:
            results = job_archive.run_archive_pass(job, correlation_id)
        except Exception as exc:  # noqa: BLE001 — per-JOB fence; one job never blocks the rest
            out["errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"archive pass raised for job_id={job_id!r} ({direction or 'no direction'}); the "
                f"job stays queued and retries next cycle: {type(exc).__name__}: {exc!r}",
                error_code="fieldops_archive_job_failed",
                correlation_id=correlation_id,
            )
            continue

        state = job_archive.state_from_results(results)
        out[state] = out.get(state, 0) + 1
        updates.append({
            "job_id": job_id,
            "direction": direction,
            "state": state,
            "containers": [r.as_dict() for r in results],
        })

    if not updates:
        # The Worker REFUSES an empty updates array (400). Every job was capped, or every one
        # raised — either way there is nothing to report.
        return out

    try:
        ack = portal_client.post_fieldops_archive_progress(base_url, bearer, updates)
    except (portal_client.PortalAuthError, portal_client.PortalTransportError) as exc:
        # The relocations ALREADY HAPPENED; only the report failed. Nothing is lost: the queue
        # re-serves each job next cycle and every container operation is idempotent (an already
        # relocated container resolves to "nothing to move"), so the re-run reports the same
        # terminal state. WARN, not CRITICAL, for exactly that reason.
        out["errors"] += 1
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"archive progress post FAILED for {len(updates)} job(s) — the folders moved but the "
            f"portal was not told. The queue re-serves them next cycle and the relocation is "
            f"idempotent, so this self-heals: {type(exc).__name__}: {exc!r}",
            error_code="fieldops_archive_progress_failed",
        )
        return out

    skipped = ack.get("skipped") or []
    if skipped:
        # NOT an error: the Worker's UPDATE is forward-only, so a row that no longer matches means
        # the operator changed direction (or it already completed) while the pass was working.
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"archive progress: {len(skipped)} job(s) no longer matched their queued direction and "
            f"were skipped by the Worker (the request changed mid-pass): {skipped!r}",
            error_code="fieldops_archive_progress_skipped",
        )
    return out


def _reconcile_job_incidents(
    job_id: str, project_name: str, rows: list[dict[str, Any]],
    correlation_id: str, out: dict[str, int],
) -> None:
    """Reconcile a job that HAS filed incidents: find-or-create its Material Incidents sheet,
    change-only upsert each incident (append-only — never retire), run the row-cap watchdog."""
    try:
        sheet_id = material_incidents.ensure_material_incidents_sheet(project_name)
    except (
        picklist_validation.PicklistViolationError,
        smartsheet_client.SmartsheetValidationError,
        # A §46 share change or a deleted tracker sheet is PERMANENT: without these two
        # the fault fell to the transient arm below and logged "re-projects next cycle"
        # forever — no ticket, no CRITICAL, no operator ever told.
        smartsheet_client.SmartsheetPermissionError,
        smartsheet_client.SmartsheetNotFoundError,
    ) as exc:
        out["reviewed"] += 1
        _route_incident_to_review(job_id, project_name, exc, correlation_id, phase="ensure-sheet")
        return
    except Exception as exc:  # noqa: BLE001 — per-job fence; one bad job never kills the pass
        out["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"transient failure ensuring Material Incidents sheet for {project_name!r} "
            f"(job {job_id}); ledger re-projects next cycle: {type(exc).__name__}: {exc!r}",
            error_code="fieldops_incidents_sheet_transient",
            correlation_id=correlation_id,
        )
        return

    for e in rows:
        incident_uuid = str(e.get("submission_uuid") or "").strip()
        try:
            material_incidents.upsert_incident_row(
                sheet_id,
                incident_uuid=incident_uuid,
                material=str(e.get("material_description") or "").strip(),
                issue=str(e.get("issue") or "").strip(),
                line_uuid=str(e.get("line_uuid") or "").strip(),
                line_status=str(e.get("line_status") or "").strip(),
                qty_expected=_fmt_qty(e.get("qty_expected")),
                qty_received=_fmt_qty(e.get("qty_received")),
                delivery_ref=str(e.get("delivery_ref") or "").strip(),
                details=str(e.get("details") or "").strip(),
                action_taken=str(e.get("action_taken") or "").strip(),
                reported_by=str(e.get("reported_by_display") or "").strip(),
                reported_at=(
                    str(e.get("work_date") or "").strip()
                    or _fmt_epoch_date(e.get("created_at"))
                ),
                report=str(e.get("box_link") or "").strip(),
            )
            out["upserted"] += 1
        except (
            picklist_validation.PicklistViolationError,
            smartsheet_client.SmartsheetValidationError,
            # A §46 share change or a deleted tracker sheet is PERMANENT: without these two
            # the fault fell to the transient arm below and logged "re-projects next cycle"
            # forever — no ticket, no CRITICAL, no operator ever told.
            smartsheet_client.SmartsheetPermissionError,
            smartsheet_client.SmartsheetNotFoundError,
        ) as exc:
            out["reviewed"] += 1
            _route_incident_to_review(
                job_id, project_name, exc, correlation_id,
                phase="upsert", incident_uuid=incident_uuid,
            )
        except Exception as exc:  # noqa: BLE001 — per-incident fence
            out["errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"transient failure upserting incident {incident_uuid!r} for {project_name!r} "
                f"(ledger re-projects next cycle): {type(exc).__name__}: {exc!r}",
                error_code="fieldops_incident_upsert_transient",
                correlation_id=correlation_id,
            )

    # §51 A5 row-cap watchdog — advisory, owns its own try/except (never raises here). MORE relevant
    # here than for the bounded Material List: the incident ledger grows monotonically (append-only).
    material_incidents.check_row_cap(
        sheet_id, material_incidents.material_incidents_sheet_name(project_name)
    )


def _route_incident_to_review(
    job_id: str, project_name: str, exc: Exception, correlation_id: str,
    *, phase: str, incident_uuid: str | None = None,
) -> None:
    """Route a PERMANENTLY-failed incident mirror to ITS_Review_Queue (workstream
    progress_reports)."""
    review_queue.safe_add(
        script_name=SCRIPT_NAME,
        workstream="progress_reports",
        summary=(
            f"field-ops Material Incidents up-sync: PERMANENT failure ({phase}) for job {job_id!r} "
            f"({project_name!r}, {type(exc).__name__})"
            + (f" incident {incident_uuid!r}" if incident_uuid else "")
            + " — re-projects next cycle, needs operator fix"
        ),
        payload={
            "job_id": job_id,
            "project_name": project_name,
            "phase": phase,
            "incident_uuid": incident_uuid,
            "error": f"{type(exc).__name__}: {exc!r}",
        },
        sla_tier=review_queue.SlaTier.SAFETY_INTAKE,
        reason=review_queue.ReviewReason.POLICY_EDGE,
        severity=Severity.WARN,
        source_file=incident_uuid or job_id,
    )
    error_log.log(
        Severity.WARN, SCRIPT_NAME,
        f"incident mirror routed to Review Queue (permanent, {phase}) job={job_id!r} "
        f"incident={incident_uuid!r}: {type(exc).__name__}: {exc!r}",
        error_code="fieldops_incident_permanent",
        correlation_id=correlation_id,
    )


# ---- PR4 material RECEIPTS mirror (the delivery ledger's §51 up-sync) ----------


# D1 stores the mark lower-cased and CHECK-constrained to exactly these three
# (`0059_material_tracking.sql`); the Smartsheet column is prose the office reads.
_RECEIPT_KIND_DISPLAY: dict[str, str] = {
    "delivered": "Delivered",
    "partial": "Partial",
    "not_delivered": "Not delivered",
}


def _fmt_receipt_kind(raw: Any) -> str:
    """Map the D1 receipt `kind` to its display casing; pass an UNKNOWN value through verbatim.

    Pass-through rather than blank-on-unknown is deliberate. The D1 CHECK constraint makes an
    unrecognized kind impossible today, so seeing one means the constraint changed and this map
    drifted — and the honest response to that is to put the unmapped value in the cell where an
    operator sees it, not to silently empty the column and make the drift invisible. The column is
    TEXT_NUMBER, so an unmapped value cannot fail a picklist validation on the way in.
    """
    key = str(raw or "").strip()
    return _RECEIPT_KIND_DISPLAY.get(key, key)


def _group_receipts_by_job(
    rows: list[dict[str, Any]],
) -> dict[str, tuple[str, list[dict[str, Any]]]]:
    """Group receipt events by job_id → (project_name, [rows]). Skips a row missing its
    event_uuid / job_id / project_name (a data anomaly that can't be keyed / foldered) — never
    silent.

    Deliberately NOT `_group_incidents_by_job`: that one keys on `submission_uuid`, and a receipt
    event is keyed by `event_uuid`. Reusing it would silently drop every receipt (no row carries a
    submission_uuid), which the malformed-row WARN would then report as a data problem rather than
    the wiring bug it actually is. Like incidents and unlike the material-list grouper there is no
    reconcile roster, so project_name MUST ride each row (the Worker's active-job JOIN supplies it).
    """
    by_job: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for e in rows:
        event_uuid = str(e.get("event_uuid") or "").strip()
        job_id = str(e.get("job_id") or "").strip()
        project = str(e.get("project_name") or "").strip()
        if not event_uuid or not job_id or not project:
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"material receipt skipped — missing event_uuid/job_id/project_name "
                f"(event_uuid={e.get('event_uuid')!r} job_id={e.get('job_id')!r})",
                error_code="fieldops_receipt_row_malformed",
            )
            continue
        by_job.setdefault(job_id, (project, []))[1].append(e)
    return by_job


def _reconcile_job_receipts(
    job_id: str, project_name: str, rows: list[dict[str, Any]],
    correlation_id: str, out: dict[str, int],
) -> None:
    """Reconcile a job that HAS receipt events: find-or-create its Material Receipts sheet,
    change-only upsert each event (append-only — never retire), run the row-cap watchdog."""
    try:
        sheet_id = material_receipts.ensure_material_receipts_sheet(project_name)
    except (
        picklist_validation.PicklistViolationError,
        smartsheet_client.SmartsheetValidationError,
        # A §46 share change or a deleted tracker sheet is PERMANENT: without these two
        # the fault fell to the transient arm below and logged "re-projects next cycle"
        # forever — no ticket, no CRITICAL, no operator ever told.
        smartsheet_client.SmartsheetPermissionError,
        smartsheet_client.SmartsheetNotFoundError,
    ) as exc:
        out["reviewed"] += 1
        _route_receipt_to_review(job_id, project_name, exc, correlation_id, phase="ensure-sheet")
        return
    except Exception as exc:  # noqa: BLE001 — per-job fence; one bad job never kills the pass
        out["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"transient failure ensuring Material Receipts sheet for {project_name!r} "
            f"(job {job_id}); ledger re-projects next cycle: {type(exc).__name__}: {exc!r}",
            error_code="fieldops_receipts_sheet_transient",
            correlation_id=correlation_id,
        )
        return

    for e in rows:
        event_uuid = str(e.get("event_uuid") or "").strip()
        try:
            material_receipts.upsert_receipt_row(
                sheet_id,
                event_uuid=event_uuid,
                material=str(e.get("material_description") or "").strip(),
                kind=_fmt_receipt_kind(e.get("kind")),
                # NULL for not_delivered (enforced at the Worker write route), so this lands blank
                # on its own. Deliberately NOT force-blanked on kind here: that would mask a qty
                # that should not exist rather than showing the operator the anomaly.
                qty=_fmt_qty(e.get("qty")),
                unit=str(e.get("unit") or "").strip(),
                part_number=str(e.get("part_number") or "").strip(),
                line_uuid=str(e.get("line_uuid") or "").strip(),
                # The DERIVED pair — the only two fields that legitimately change after the event
                # is written, and therefore the whole reason this re-projects every cycle.
                line_status=str(e.get("line_status") or "").strip(),
                line_qty_expected=_fmt_qty(e.get("line_qty_expected")),
                line_qty_received=_fmt_qty(e.get("line_qty_received")),
                bol=str(e.get("bol_number") or "").strip(),
                note=str(e.get("note") or "").strip(),
                received_by=str(e.get("received_by_display") or "").strip(),
                # Already a naive 'YYYY-MM-DD' Pacific work day in D1 — no epoch conversion.
                event_date=str(e.get("event_date") or "").strip(),
            )
            out["upserted"] += 1
        except (
            picklist_validation.PicklistViolationError,
            smartsheet_client.SmartsheetValidationError,
            # A §46 share change or a deleted tracker sheet is PERMANENT: without these two
            # the fault fell to the transient arm below and logged "re-projects next cycle"
            # forever — no ticket, no CRITICAL, no operator ever told.
            smartsheet_client.SmartsheetPermissionError,
            smartsheet_client.SmartsheetNotFoundError,
        ) as exc:
            out["reviewed"] += 1
            _route_receipt_to_review(
                job_id, project_name, exc, correlation_id,
                phase="upsert", event_uuid=event_uuid,
            )
        except Exception as exc:  # noqa: BLE001 — per-event fence
            out["errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"transient failure upserting receipt {event_uuid!r} for {project_name!r} "
                f"(ledger re-projects next cycle): {type(exc).__name__}: {exc!r}",
                error_code="fieldops_receipt_upsert_transient",
                correlation_id=correlation_id,
            )

    # §51 A5 row-cap watchdog — advisory, owns its own try/except (never raises here). It matters
    # MORE here than for incidents: a job with many lines delivered across many truckloads writes an
    # event per load, so this ledger is the fastest-growing of the per-job trackers.
    material_receipts.check_row_cap(
        sheet_id, material_receipts.material_receipts_sheet_name(project_name)
    )


def _route_receipt_to_review(
    job_id: str, project_name: str, exc: Exception, correlation_id: str,
    *, phase: str, event_uuid: str | None = None,
) -> None:
    """Route a PERMANENTLY-failed receipt mirror to ITS_Review_Queue (workstream
    progress_reports)."""
    review_queue.safe_add(
        script_name=SCRIPT_NAME,
        workstream="progress_reports",
        summary=(
            f"field-ops Material Receipts up-sync: PERMANENT failure ({phase}) for job {job_id!r} "
            f"({project_name!r}, {type(exc).__name__})"
            + (f" receipt {event_uuid!r}" if event_uuid else "")
            + " — re-projects next cycle, needs operator fix"
        ),
        payload={
            "job_id": job_id,
            "project_name": project_name,
            "phase": phase,
            "event_uuid": event_uuid,
            "error": f"{type(exc).__name__}: {exc!r}",
        },
        sla_tier=review_queue.SlaTier.SAFETY_INTAKE,
        reason=review_queue.ReviewReason.POLICY_EDGE,
        severity=Severity.WARN,
        source_file=event_uuid or job_id,
    )
    error_log.log(
        Severity.WARN, SCRIPT_NAME,
        f"receipt mirror routed to Review Queue (permanent, {phase}) job={job_id!r} "
        f"receipt={event_uuid!r}: {type(exc).__name__}: {exc!r}",
        error_code="fieldops_receipt_permanent",
        correlation_id=correlation_id,
    )


def _mirror_material_receipts_pass(base_url: str, bearer: str) -> dict[str, int]:
    """Re-project the CURRENT delivery-mark ledger UP into per-job Material Receipts sheets.

    Returns {upserted, reviewed, errors}. Never raises — the caller runs it after the job + hours +
    equipment + material + incident mirrors, so a receipt failure must never abort the cycle. Per-job
    (sheet) + per-event fences; a permanent failure routes to the Review Queue; a transient one is
    simply left for the next cycle (the ledger re-projects every cycle, so nothing is "lost" — there
    is NO watermark, NO mark-mirrored).

    APPEND-ONLY LEDGER (the deliberate contrast with the material-list snapshot): each mark is an
    immutable historical assertion that something did or did not arrive on a date, so there is NO
    reconcile roster and NO retire — a job with zero receipts simply produces no rows and is never
    visited (no sheet is created for it). The count-drops-to-zero / #468 zero-drop class is
    structurally impossible: there is no retire path to wrongly zero.

    Re-projection is not redundant work: two of the sixteen fields (`line_status`,
    `line_qty_received`) are DERIVED and change as later events land against the same line, so a
    write-once mirror would go stale. `upsert_receipt_row` is change-only, so a cycle where nothing
    moved costs reads and no writes.

    No throttle: per-cycle change-only re-projection is simple-correct at current scale. A throttle
    is a FUTURE optimization if read-load bites — do NOT build it now.
    """
    out = {"upserted": 0, "reviewed": 0, "errors": 0}
    try:
        receipts = portal_client.get_fieldops_material_receipts(base_url, bearer)
    except portal_client.PortalAuthError as exc:
        # 401 on the SAME bearer that just drained the earlier passes — surface (bad/rotated token)
        # but do not crash: the earlier passes may have succeeded before a mid-cycle rotation.
        out["errors"] += 1
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            f"material-receipts fetch UNAUTHORIZED (401) — field-ops bearer rejected; delivery "
            f"ledger skipped this cycle: {exc!r}",
            error_code="fieldops_receipts_fetch_auth_failed",
        )
        return out
    except portal_client.PortalTransportError as exc:
        out["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"material-receipts fetch failed (ledger re-projects next cycle): {exc!r}",
            error_code="fieldops_receipts_fetch_failed",
        )
        return out

    for job_id, (project_name, rows) in _group_receipts_by_job(receipts).items():
        correlation_id = uuid.uuid4().hex[:12]
        _reconcile_job_receipts(job_id, project_name, rows, correlation_id, out)
    return out


if __name__ == "__main__":
    sync_once()
