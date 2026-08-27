"""Safety Portal pull-model polling daemon (Phase 5) — the Mac-side queue drain.

The puller half of decision_phase5-portal-transport. The Cloudflare Worker signs +
queues each portal submission send-free in D1; this launchd daemon drains the queue:

    GET /api/internal/pending  (shared.portal_client)
      → per row: recompute the canonical HMAC (shared.portal_hmac) and constant-time
        compare to the row's `hmac`. HMAC FAIL = reject (anomaly-log + Review-Queue
        flag, security_flag=True) and NEVER hand to intake — the downgrade defense.
      → on verify pass: safety_reports.intake.process_portal_submission(row)
      → on a DRAIN outcome: POST /api/internal/mark-filed (the receipt).

Fail-CLOSED: if the bearer token, the HMAC secret, or the Worker base URL is
missing, the cycle does NOT poll — it logs + halts (a silent no-op that drops
submissions is forbidden, CLAUDE.md "never silent").

Capability gating (Invariant 1): this daemon is ENROLLED in
tests/test_capability_gating.py::GATED_SCRIPTS — it must NOT import any external-send
capability (send_mail / resend / smtplib / email.mime). It pulls + files only; the
HTTP egress lives in the audited shared.portal_client (F02 allowlist), so this module
itself imports no network library. This is the whole point of the pull model: the
Python puller is INSIDE the AST capability gate the TS Worker was outside of.

launchd schedule
----------------
Single-cycle: `poll_once()` is the public API; `__main__` calls it once and exits.
launchd handles the ~60 s cadence via StartInterval (sourced from ITS_Config
`safety_reports.portal_poll.poll_interval_seconds` at install time).

Per-cycle behavior (mirrors the canonical weekly_send_poll pattern)
------------------------------------------------------------------
  1. `polling_enabled` ITS_Config gate — false short-circuits.
  2. fcntl file lock — skip-if-held (launchd-overlap guard).
  3. Fail-closed credential resolution (bearer + HMAC secret + base URL).
  4. GET pending; per-row HMAC verify → dispatch → receipt; per-row fence.
  5. seen-set (state file) — fast-path re-receipt for an already-filed UUID whose
     mark-filed was lost, and one-shot flagging for a rejected (bad-HMAC) UUID, so
     neither re-files nor re-spams the Review Queue every cycle.
  5b. Best-effort fenced passes (never block the drain): the PR-4 PDF-cache
      servicing pass (_service_pdf_requests) and the G1 checklist item-photo
      screening pass (_service_item_photos — HMAC verify → the byte-identical §34
      photo_screen pipeline → clean files to Box + refused pages/reviews → the
      delete-on-screen disposition post-back).
  6. Heartbeat file + ITS_Daemon_Health row + watchdog Check C marker.
  No @its_error_log CRITICAL-spam on a clean empty poll (only real failures log).

The ITS_Daemon_Health heartbeat helpers — once replicated VERBATIM from
weekly_send_poll (itself from the retired intake_poll), the polling-daemon
doctrine's 2nd-consumer extraction trigger (Op Stds §14) — now live in
`shared/heartbeat.py` as `HeartbeatReporter`; this daemon keeps only the
`_write_heartbeat` / `_write_heartbeat_row` mock seams as thin delegators to
the module-level `_heartbeat_reporter`.
"""
from __future__ import annotations

import base64
import fcntl
import json
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from safety_reports import intake, photo_screen, safety_naming
from shared import (
    active_jobs,
    anomaly_logger,
    box_client,
    circuit_breaker,
    creds_resolution,
    error_log,
    keychain,
    portal_client,
    portal_hmac,
    review_queue,
    smartsheet_client,
    state_io,
    sustained_failure,
)
from shared.creds_resolution import CREDS_TRANSIENT as _CREDS_TRANSIENT
from shared.creds_resolution import TransientUnavailable
from shared.error_log import Severity, its_error_log
from shared.heartbeat import HeartbeatReporter, HeartbeatStatus
from shared.kill_switch import require_active
from shared.required_config import ConfigKey, resolve_and_log

SCRIPT_NAME = "safety_reports.portal_poll"
WORKSTREAM = "safety_reports"

# ITS_Config keys.
CFG_POLLING_ENABLED = "safety_reports.portal_poll.polling_enabled"
CFG_POLL_INTERVAL = "safety_reports.portal_poll.poll_interval_seconds"
CFG_WORKER_BASE_URL = "safety_reports.portal.worker_base_url"

# Keychain entry names (mirror the Worker's PORTAL_INTERNAL_API_TOKEN +
# HMAC_PAYLOAD_SECRET; the Mac-side names are distinct on purpose).
KC_BEARER = "ITS_PORTAL_INTERNAL_TOKEN"  # noqa: S105 — Keychain entry NAME, not a secret
KC_HMAC_SECRET = "ITS_PORTAL_HMAC_SECRET"  # noqa: S105 — Keychain entry NAME, not a secret

DEFAULT_POLLING_ENABLED = True
DEFAULT_POLL_INTERVAL = 60  # 60 s

# #336 — every ITS_Config key this daemon resolves at RUNTIME, declared for the startup
# observability pass (resolve_and_log). The photo-clamav + Box-root keys are read mid-cycle
# via intake / safety_naming (shared constants). The *.poll_interval_seconds key is EXCLUDED
# (declared but never runtime-read here).
REQUIRED_CONFIG: list[ConfigKey] = [
    ConfigKey(CFG_POLLING_ENABLED, WORKSTREAM, DEFAULT_POLLING_ENABLED, "bool"),
    ConfigKey(CFG_WORKER_BASE_URL, WORKSTREAM, "", "str"),
    ConfigKey(intake.CFG_PHOTO_CLAMAV, WORKSTREAM, False, "bool"),
    ConfigKey(safety_naming.CFG_BOX_PORTAL_ROOT, WORKSTREAM, "", "str"),
]
PENDING_LIMIT = 50  # Worker caps at 200; 50 drains a normal backlog per cycle.
MAX_SEEN = 2000  # cap the seen-set file (oldest entries are harmless dead weight).

# PR-4 Part A — request-driven PDF cache servicing pass.
PDF_REQUEST_LIMIT = 25  # Worker caps at 100; 25 services a normal request backlog.
# Raw bytes per chunk BEFORE base64. 700 KB raw → ~933 KB base64, comfortably under
# the Worker's 1 MB decoded-chunk ceiling and D1's ~2 MB per-row practical limit.
PDF_CHUNK_BYTES = 700_000
# Recover the Box file id from a `https://app.box.com/file/<id>` link (the shape
# intake._box_link produces). Same pattern as weekly_send._box_file_id.
_BOX_FILE_LINK_RE = re.compile(r"/file/(\d+)")

# G1 Slice 2 — checklist item-photo screening pass (_service_item_photos), a fenced
# sibling of the PDF pass above. Rows per cycle (Worker caps at 100; 25 drains a
# normal backlog — a saturated page self-heals across cycles, and the >7d
# stuck-pending prune stage is the Worker-side growth cap).
ITEM_PHOTO_LIMIT = 25
# Box filing target: <portal root>/ITS Photos/checklist/<item_state_id>/photo_<id>.jpg.
# The path is derived ONLY from HMAC-covered data (item_state_id + the photo id served
# with the signed row) — never from an unsigned D1 field. "ITS Photos" mirrors
# intake._file_portal_photos' operator naming rule (ITS-prefixed system folders).
ITEM_PHOTO_BOX_FOLDER = "ITS Photos"
ITEM_PHOTO_BOX_SUBFOLDER = "checklist"

# State paths. HEARTBEAT_ROW_STATE_PATH is SHARED with the other daemons —
# same JSON file, different daemon_name key.
STATE_DIR = Path.home() / "its" / "state"
HEARTBEAT_PATH = STATE_DIR / "portal_poll_heartbeat.txt"
LOCK_PATH = STATE_DIR / "portal_poll.lock"
SEEN_PATH = STATE_DIR / "portal_poll_seen.json"
HEARTBEAT_ROW_STATE_PATH = STATE_DIR / "heartbeat_row_ids.json"
# Consecutive pending-fetch failures before a TRANSIENT transport error escalates ERROR →
# CRITICAL. The puller runs every ~60s, so 5 ≈ a 5-minute sustained filing outage — pages
# promptly instead of waiting for the next daily watchdog. A one-off blip stays ERROR and the
# counter resets on the next successful fetch. (Auth / missing-creds page IMMEDIATELY — they
# never self-heal — so they don't go through this counter.)
FETCH_FAIL_STATE_PATH = STATE_DIR / "portal_poll_fetch_failures.json"
FETCH_FAIL_CRITICAL_THRESHOLD = 5

# G1: one-shot flag state for BAD-HMAC item-photo rows — the item-photo analog of the
# submission seen-set's 'rejected' fast-path. The first failure fires the full
# anomaly-log + Review-Queue + CRITICAL and posts the refused disposition (which drains
# the row); the flag file only matters when that post-back is LOST — it suppresses the
# per-cycle re-flag spam while the post keeps retrying every cycle until the drain lands.
ITEM_PHOTO_FLAGGED_PATH = STATE_DIR / "portal_poll_item_photo_flagged.json"
MAX_ITEM_PHOTO_FLAGS = 500  # cap the flag file (drained rows leave dead weight only)

# DR-photo-pool Slice 2 — the additional-photo POOL screening pass
# (_service_daily_photos), the daily_photo_pool (migration 0037) twin of the
# item-photo pass above. The pool is per-(job, work_date) and mountable on ANY form
# with an `additional_photos` section — the "daily" in these identifiers is a
# load-bearing historical name (error codes / Box paths / state files keep it), not
# a daily-report-only scope. Same page size rationale; the Worker caps at 100.
DAILY_PHOTO_LIMIT = 25
# Box filing target: <portal root>/ITS Photos/daily/<job_id>/<work_date>/photo_<id>.jpg.
# job_id + work_date are HMAC-COVERED (the daily-photo canonical binds them — a signed
# photo cannot be replayed onto another job/date), and the photo id is served with the
# signed row (the item-photo precedent) — the path never derives from an unsigned field.
DAILY_PHOTO_BOX_SUBFOLDER = "daily"
# One-shot bad-HMAC flag state for daily-pool rows (the exact ITEM_PHOTO_FLAGGED_PATH
# semantics, its own file — flag ids are per-table AUTOINCREMENTs and must not collide).
DAILY_PHOTO_FLAGGED_PATH = STATE_DIR / "portal_poll_daily_photo_flagged.json"
MAX_DAILY_PHOTO_FLAGS = 500

# A4: "stuck backlog" marker for the daily watchdog (Check Q). A backlog is *stuck* when a
# SATURATED pending page (len(rows) >= PENDING_LIMIT) drains NOTHING in a cycle — the daemon is
# fetching fine but intake is erroring on every row, so nothing is marked-filed and submissions
# pile up behind the page cap (which masks true depth). `high_since_utc` latches the first such
# cycle and clears the instant the queue makes any progress; the watchdog WARNs only once that
# latch holds past a sustained window, so a one-cycle burst never pages. Distinct from the
# fetch-failure counter above (can't FETCH) — this is "fetches fine, drains nothing".
PENDING_BACKLOG_STATE_PATH = STATE_DIR / "portal_poll_pending_backlog.json"

DAEMON_NAME = "safety_reports.portal_poll"

# A1 self-provision metadata (the ONLY per-daemon difference in the otherwise
# byte-identical heartbeat helpers — kept OUT of the helper bodies so the
# verbatim-duplication invariant + the future shared/heartbeat.py extraction stay clean).
_REGISTRATION_INTERVAL_SECONDS = DEFAULT_POLL_INTERVAL
_REGISTRATION_SOURCE_ID = "Safety Portal Worker /api/internal/pending"

# Shared ITS_Daemon_Health reporter for this daemon. The per-daemon registration
# values are the ONLY heartbeat difference between daemons (see shared/heartbeat.py).
_heartbeat_reporter = HeartbeatReporter(
    script_name=SCRIPT_NAME,
    daemon_name=DAEMON_NAME,
    workstream=WORKSTREAM,
    liveness_path=HEARTBEAT_PATH,
    interval_seconds=_REGISTRATION_INTERVAL_SECONDS,
    source_id=_REGISTRATION_SOURCE_ID,
    row_state_path=HEARTBEAT_ROW_STATE_PATH,  # shared file — make the contract explicit
)

# Watchdog Check C marker (TRACKED_JOBS registration in scripts/watchdog.py is
# deferred to the deploy session — see docs/tech_debt.md; the marker write here is
# forward-compatible and harmless if unregistered).
WATCHDOG_MARKER_DIR = Path.home() / "its" / ".watchdog"
WATCHDOG_JOB_SLUG = "safety_portal_poll"


# intake return statuses that mean "stop serving this row" — post the receipt.
# 'error' is the ONLY non-drain status (transient → re-pull retries).
DRAIN_STATUSES = frozenset({"processed", "already_filed", "review_queue"})


@dataclass(frozen=True)
class PollStats:
    """Summary of one poll_once() invocation."""
    skipped_disabled: bool = False
    skipped_locked: bool = False
    halted_no_creds: bool = False
    halted_transient: bool = False  # base URL temporarily unreadable (Smartsheet circuit OPEN)
    scanned: int = 0
    filed: int = 0      # processed + already_filed
    reviewed: int = 0   # review_queue (flagged + drained)
    rejected: int = 0   # HMAC verify failures (never filed)
    remarked: int = 0   # seen-as-filed rows whose mark-filed was re-posted
    errors: int = 0     # transient intake errors + per-row exceptions (NOT drained)
    deferred: int = 0   # DR-photo-pool: submissions deferred a cycle awaiting pool screening
    pdf_serviced: int = 0  # PR-4: request-driven PDF caches uploaded this cycle
    item_photos_screened: int = 0  # G1: item photos dispositioned (clean+refused) this cycle
    daily_photos_screened: int = 0  # DR-photo-pool: pool photos dispositioned this cycle


# ---- Box helper (PR-4 Part A) -------------------------------------------


def _box_file_id(link: str) -> str | None:
    """Recover the Box file id from a `_box_link` URL. None if not present.

    Copy of weekly_send._box_file_id (Op Stds §14 preservation). Used only to
    backstop a missing box_file_id on a re-served already-filed row — the canonical
    id rides on ProcessResult.box_file_id from intake.
    """
    m = _BOX_FILE_LINK_RE.search(link or "")
    return m.group(1) if m else None


# ---- Config readers (replicated per preservation) -----------------------


def _read_str_setting(key: str, fallback: str) -> str:
    try:
        raw = smartsheet_client.get_setting(key, workstream=WORKSTREAM)
    except smartsheet_client.SmartsheetNotFoundError:
        # Row genuinely absent — the documented fallback case (config never set).
        return fallback
    except (smartsheet_client.SmartsheetAuthError, smartsheet_client.SmartsheetPermissionError):
        # Deterministic misconfig (revoked API token / lost share) — will NOT
        # self-heal. Propagate so @its_error_log pages CRITICAL, exactly like a
        # rotated-out credential. Must precede the generic SmartsheetError catch
        # below (both are subclasses of it).
        raise
    except smartsheet_client.SmartsheetError as exc:
        # TRANSIENT Smartsheet failure — circuit OPEN, or a rate-limit/5xx blip
        # BEFORE the breaker trips (the breaker needs `failure_threshold`
        # consecutive failures, so early-outage cycles raise the raw error, not
        # SmartsheetCircuitOpenError). The row exists and self-heals; previously
        # the raw class propagated → a misleading CRITICAL `uncaught_exception`
        # on every one-cycle blip. WARN-loud (observable config resolution — the
        # collapse to fallback must never be silent) and use the fallback; the
        # subsequent base-URL read routes the cycle to the transient-skip path.
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"ITS_Config read failed transiently for a portal setting — using fallback "
            f"this cycle (self-heals): {type(exc).__name__}: {exc!r}",
            error_code="portal_config_transient",
        )
        return fallback
    return raw if isinstance(raw, str) and raw else fallback


def _read_bool_setting(key: str, fallback: bool) -> bool:
    raw = _read_str_setting(key, str(fallback).lower())
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _polling_enabled() -> bool:
    return _read_bool_setting(CFG_POLLING_ENABLED, DEFAULT_POLLING_ENABLED)


# ---- State / lock helpers -----------------------------------------------


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
    daemon_name: str = DAEMON_NAME,
) -> None:
    """ITS_Daemon_Health per-cycle row update — thin delegator to the shared
    HeartbeatReporter (the canonical test mock seam; the suite patches this exact
    symbol). The ``daemon_name`` param is retained for signature back-compat and
    always resolves to this daemon. See shared/heartbeat.py (§42).
    """
    _heartbeat_reporter.write_row(
        status=status,
        items_processed=items_processed,
        error_summary=error_summary,
        correlation_id=correlation_id,
        notes=notes,
        daemon_name=daemon_name,
    )


# ---- Consecutive pending-fetch failure counter (sustained-outage escalation) ----


def _record_fetch_failure() -> int:
    """Increment + persist the consecutive pending-fetch failure counter; return the new count.

    Used ONLY for transient transport failures (auth / missing-creds page immediately). On any
    state error, returns 1 (treat as a single failure — do NOT page off a state glitch; the
    un-faked watchdog Check-C marker is the reliable backstop for a sustained outage)."""
    try:
        with state_io.with_path_lock(FETCH_FAIL_STATE_PATH):
            count = 0
            if FETCH_FAIL_STATE_PATH.exists():
                try:
                    count = int(json.loads(FETCH_FAIL_STATE_PATH.read_text()).get("count", 0))
                except (OSError, json.JSONDecodeError, ValueError, TypeError, AttributeError):
                    count = 0
            count += 1
            state_io.atomic_write_json(FETCH_FAIL_STATE_PATH, {"count": count})
            return count
    except Exception as exc:  # noqa: BLE001 — counter is best-effort; Check C is the backstop
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"fetch-failure counter write failed (treating as #1): {exc!r}",
            error_code="portal_fetch_counter_failed",
        )
        return 1


def _reset_fetch_failures() -> None:
    """Zero the consecutive-failure counter after a successful pending fetch. Best-effort: a
    reset failure only risks one spurious CRITICAL next cycle, never a missed outage."""
    try:
        with state_io.with_path_lock(FETCH_FAIL_STATE_PATH):
            if FETCH_FAIL_STATE_PATH.exists():
                state_io.atomic_write_json(FETCH_FAIL_STATE_PATH, {"count": 0})
    except Exception:  # noqa: BLE001 — best-effort reset
        pass


# ---- Seen-set (idempotency defense-in-depth) ----------------------------


def _load_seen() -> dict[str, dict[str, Any]]:
    """Load the seen-set `{uuid: {status, box_link}}`. {} on any read error."""
    if not SEEN_PATH.exists():
        return {}
    try:
        parsed = json.loads(SEEN_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _persist_seen(seen: dict[str, dict[str, Any]]) -> None:
    """Atomically persist the seen-set, capped to the most-recent MAX_SEEN entries.

    Lock-timeout fails OPEN (log WARN + skip): a lost seen-set only costs a
    redundant intake call next cycle (the week-sheet UUID check is the real dedupe
    authority), never a double-file. Caps to bound the file (oldest = dead weight
    once the Worker has drained the row)."""
    if len(seen) > MAX_SEEN:
        seen = dict(list(seen.items())[-MAX_SEEN:])
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with state_io.with_path_lock(SEEN_PATH):
            state_io.atomic_write_json(SEEN_PATH, seen)
    except state_io.StateLockTimeoutError:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"could not acquire lock on {SEEN_PATH} after retries; seen-set not persisted",
            error_code="portal_seen_persist_failed",
        )


# Watchdog Check C marker — replicated from weekly_send_poll per preservation
# (Op Stds §14); out of P0 heartbeat-extraction scope.
def _write_watchdog_marker() -> None:
    """Touch the Check C freshness marker for this run."""
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


# ---- HMAC verification --------------------------------------------------


def _verify_row_hmac(row: dict[str, Any], provided_hmac: str, secret: str) -> bool:
    """Recompute the canonical HMAC for a pulled row and constant-time compare to
    `provided_hmac`. The downgrade defense: a row that fails is rejected, never
    filed. payload_json is used VERBATIM (re-serializing would change the bytes).

    `provided_hmac` is passed SEPARATELY (not read from `row`) so the HMAC value
    stays isolated to this verification step and never travels inside the row dict
    into intake or any log line — both better hygiene (an integrity tag has no
    business downstream) and it keeps CodeQL's clear-text-logging taint off the
    submission fields the daemon logs."""
    return portal_hmac.verify(
        secret,
        provided_hmac,
        submission_uuid=str(row.get("submission_uuid") or ""),
        job_id=str(row.get("job_id") or ""),
        form_code=str(row.get("form_code") or ""),
        work_date=str(row.get("work_date") or ""),
        payload_json=str(row.get("payload_json") or ""),
    )


def _handle_hmac_failure(
    row: dict[str, Any], correlation_id: str, *, base_url: str, bearer: str
) -> None:
    """Reject a bad-HMAC row: anomaly-log + Review-Queue (security_flag) + CRITICAL.

    NEVER handed to intake (downgrade defense) and NEVER mark-filed (the row stays
    in D1 for forensics). The caller records the UUID in the seen-set as 'rejected'
    so subsequent cycles skip re-flagging (no 60 s Review-Queue spam)."""
    submission_uuid = str(row.get("submission_uuid") or "")
    # Tripwire (Invariant 2, Layer 5) — record the suspicious output pattern.
    anomaly_logger.check({"portal_hmac_failure": submission_uuid, "job_id": row.get("job_id")})
    review_queue.add(
        workstream=WORKSTREAM,
        summary=(
            f"portal: HMAC verification FAILED for submission {submission_uuid} "
            f"(job_id={row.get('job_id')!r}) — rejected, NOT filed"
        ),
        payload={
            "submission_uuid": submission_uuid,
            "job_id": row.get("job_id"),
            "form_code": row.get("form_code"),
            "work_date": row.get("work_date"),
            # The HMAC value is deliberately NOT recorded — it is signature
            # material, isolated to verification; the submission_uuid + the
            # CRITICAL alert are the forensic handle, and the raw row stays in D1.
        },
        sla_tier=review_queue.SlaTier.SAFETY_INTAKE,
        reason=review_queue.ReviewReason.SECURITY_TRIGGER,
        severity=Severity.CRITICAL,
        source_file=submission_uuid,
        security_flag=True,
    )
    error_log.log(
        Severity.CRITICAL, SCRIPT_NAME,
        (
            f"portal HMAC FAIL submission_uuid={submission_uuid} job_id={row.get('job_id')!r} "
            f"— rejected, not filed (downgrade defense)"
        ),
        error_code="portal_hmac_failure",
        correlation_id=correlation_id,
    )
    # M4 (PR-4): flip the row to box_verified=-1 (terminal) so /pending stops re-serving it every
    # cycle forever. The seen-set 'rejected' fast-path remains as belt-and-suspenders. Best-effort:
    # a transport failure just re-pulls (+ re-flags, seen-set-suppressed) next cycle, not a loss.
    try:
        portal_client.mark_rejected(
            base_url, bearer, submission_uuid=submission_uuid, reason="HMAC verification failed"
        )
    except portal_client.PortalTransportError as exc:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"portal could not mark submission {submission_uuid} rejected: {exc!r}",
            error_code="portal_mark_rejected_failed", correlation_id=correlation_id,
        )


# ---- Public API ----------------------------------------------------------


@its_error_log(SCRIPT_NAME)
@require_active
def poll_once() -> PollStats:
    """Run one poll cycle. Public API; idempotent across crashes."""
    # #336 startup observability (after @require_active, fail-open — never blocks the
    # cycle). Additive to the runtime _read_*_setting reads below (§14).
    resolve_and_log(SCRIPT_NAME, REQUIRED_CONFIG)

    if not _polling_enabled():
        error_log.log(
            Severity.INFO, SCRIPT_NAME,
            "polling disabled via ITS_Config; exiting cycle",
            error_code="polling_disabled",
        )
        return PollStats(skipped_disabled=True)

    with _file_lock(LOCK_PATH) as acquired:
        if not acquired:
            error_log.log(
                Severity.INFO, SCRIPT_NAME,
                "another poll cycle holds the lock; skipping this cycle",
                error_code="poll_lock_held",
            )
            return PollStats(skipped_locked=True)
        try:
            return _poll_inside_lock()
        finally:
            # D3 — one summarized WARN row if any Smartsheet call in this pass RECOVERED
            # on retry. In `finally` so a raising pass still reports its recoveries
            # instead of silently rolling them into the next cycle's row.
            sustained_failure.flush_retry_recovery(SCRIPT_NAME)


@dataclass(frozen=True)
class _PortalCreds:
    """Resolved portal credentials with NAMED fields.

    Deliberately a dataclass, NOT a `(base_url, bearer, secret)` tuple: tuple
    unpacking is taint-imprecise (CodeQL can't tell which element is the secret,
    so unpacking taints `base_url` from `bearer` and the false taint then rides
    the Worker request into the response → every logged submission field). With
    named fields CodeQL is field-sensitive, so `base_url` never inherits the
    bearer/secret taint. Also just clearer than positional unpacking.
    """

    base_url: str
    bearer: str
    secret: str


# The transient-vs-absent sentinel + its exception taxonomy were FIRST written here,
# then hoisted verbatim into `shared/creds_resolution.py` so the other five pullers
# (po_poll, rfq_poll, estimate_poll, subcontract_poll, fieldops_sync) stop repeating
# the bug this module already fixed — each of them was still collapsing a failed
# config READ into "credential absent" and paging falsely on every Smartsheet blip.
# Re-exported under the original private names so every call site and test here is
# unchanged; `shared.creds_resolution` is now the single implementation.
_TransientUnavailable = TransientUnavailable
CREDS_TRANSIENT = _CREDS_TRANSIENT


def _resolve_credentials() -> _PortalCreds | _TransientUnavailable | None:
    """Resolve portal credentials fail-CLOSED.

    Returns one of three states so the caller can page ONLY on a genuine misconfig:
      * `_PortalCreds`      — all three present.
      * `_TransientUnavailable` (e.g. `CREDS_TRANSIENT`) — the Worker base URL couldn't be READ
        because Smartsheet is TEMPORARILY unreachable: the circuit is OPEN, or a raw
        rate-limit/5xx blip hit before the breaker tripped (transient — the config row exists,
        Smartsheet is momentarily unreachable). This SELF-HEALS; the caller WARNs + skips, it
        does NOT page. Previously the circuit-open case swallowed to `""` and looked identical
        to a genuine misconfig → a false CRITICAL on every transient outage — and the pre-trip
        raw-error case propagated → a CRITICAL `uncaught_exception`.
      * `None`              — a credential is GENUINELY absent: a missing/blank ITS_Config base-URL
        row, or a rotated-out Keychain bearer/secret. A misconfig that will NOT self-heal → page.

    Bearer + secret come from the macOS Keychain (local — unaffected by a Smartsheet outage), so
    only the base-URL read distinguishes transient-vs-absent. `SmartsheetNotFoundError` (the row is
    genuinely absent) falls through to `None`, NOT transient.
    """
    # The exception taxonomy that used to be inlined here now lives in
    # shared/creds_resolution.py — hoisted unchanged, so all six pullers classify a
    # failed read identically instead of five of them repeating the bug this module
    # had already fixed. Behaviour here is byte-for-byte what it was.
    resolved = creds_resolution.read_base_url(CFG_WORKER_BASE_URL, WORKSTREAM)
    if isinstance(resolved, TransientUnavailable):
        return resolved
    base_url = resolved or ""
    try:
        bearer = keychain.get_secret(KC_BEARER)
        secret = keychain.get_secret(KC_HMAC_SECRET)
    except keychain.KeychainError:
        bearer = secret = ""
    if not (base_url and bearer and secret):
        return None
    return _PortalCreds(base_url=base_url, bearer=bearer, secret=secret)


def _push_active_jobs(base_url: str, bearer: str) -> None:
    """Full-replace push of the ITS_Active_Jobs set → the Worker's D1 dropdown cache.

    The pull model's symmetric write-leg: each cycle the Mac tells the Worker the
    current job set so the portal's job dropdown stays current (a job created via
    the ITS_Active_Jobs form appears within one cycle). Send-free — control-plane
    to OUR OWN Worker via the F02-allowlisted portal_client, NOT a customer send
    (outside the External Send Gate, Invariant 1).

    REFUSES to push an empty set: `active_jobs.list_all_jobs()` returns [] on a
    Smartsheet read miss, and pushing [] would deactivate the entire dropdown. So a
    transient Smartsheet outage is a no-op here, not a wipe (belt-and-suspenders
    with the Worker's own empty_jobs rejection).
    """
    all_jobs = active_jobs.list_all_jobs()
    if not all_jobs:
        return  # read miss / genuinely-empty sheet → skip, never wipe the dropdown
    payload = [
        # `address` (C1): the ITS_Active_Jobs "Address" — auto-fills the subcontract builder's Site
        # address from the Smartsheet SoR. A Worker that predates the `address` sync field ignores the
        # extra key (it validates only job_id/project_name/active), so daemon-first and worker-first
        # deploy orders are both safe.
        {"job_id": j.job_id, "project_name": j.project_name, "active": 1 if j.is_active else 0,
         "address": j.address}
        for j in all_jobs
    ]
    result = portal_client.push_jobs(base_url, bearer, payload)
    error_log.log(
        Severity.INFO, SCRIPT_NAME,
        f"job sync: upserted={result.get('upserted')} deactivated={result.get('deactivated')}",
        error_code="portal_job_sync_ok",
    )


def _record_pending_backlog(scanned: int, drained: int) -> None:
    """A4: persist a 'stuck backlog' marker for the daily watchdog (Check Q).

    A backlog is *stuck* when a SATURATED pending page (``scanned >= PENDING_LIMIT``) drained
    NOTHING (``drained == 0``) — the daemon is fetching fine but intake is erroring on every
    row, so nothing is marked-filed and submissions pile up behind the page cap (which masks
    true depth). ``high_since_utc`` latches the first cycle the condition holds and clears the
    instant the queue makes ANY progress; the watchdog WARNs only when it stays latched past a
    sustained window, so a one-cycle burst never pages. Read-modify-write under the same
    path-lock + atomic-write discipline as ``_record_fetch_failure``.

    FAIL-SOFT: a marker-write error is logged WARN and swallowed — it must NEVER block or delay
    the intake drain (the marker is observability, not part of filing).
    """
    try:
        now = datetime.now(UTC).isoformat()
        stuck = scanned >= PENDING_LIMIT and drained == 0
        with state_io.with_path_lock(PENDING_BACKLOG_STATE_PATH):
            prior_high: str | None = None
            if stuck and PENDING_BACKLOG_STATE_PATH.exists():
                try:
                    prior_high = json.loads(PENDING_BACKLOG_STATE_PATH.read_text()).get("high_since_utc")
                except (OSError, ValueError):
                    prior_high = None
            state_io.atomic_write_json(
                PENDING_BACKLOG_STATE_PATH,
                {
                    "count": scanned,
                    "drained": drained,
                    "last_scan_utc": now,
                    "high_since_utc": (prior_high or now) if stuck else None,
                },
            )
    except Exception as exc:  # noqa: BLE001 — best-effort observability; must never block filing
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"pending-backlog marker write failed (filing unaffected): {exc!r}",
            error_code="portal_backlog_marker_failed",
        )


def _poll_inside_lock() -> PollStats:
    """Body of poll_once running under the file lock."""
    creds = _resolve_credentials()
    if isinstance(creds, _TransientUnavailable):
        # Smartsheet was TEMPORARILY unreachable (circuit OPEN, or a raw pre-trip blip —
        # see creds.reason) when reading the Worker base URL —
        # NOT a misconfig, and it self-heals when the circuit closes. WARN + skip this cycle; do
        # NOT page (paging here was a false CRITICAL on every transient Smartsheet/network blip).
        # Skip the watchdog freshness marker exactly like the no-creds path, so a SUSTAINED outage
        # STILL surfaces via the Check-C staleness floor — just without per-cycle CRITICAL spam.
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"portal base URL temporarily unreadable ({creds.reason}) — skipping this "
            f"cycle; will retry next interval (transient, self-heals)",
            error_code="portal_creds_transient",
        )
        _write_heartbeat()
        _write_heartbeat_row(status="WARN", items_processed=0,
                             error_summary=f"base URL unreadable ({creds.reason}) — transient")
        return PollStats(halted_transient=True)
    if creds is None:
        # FAIL-CLOSED: missing bearer / HMAC secret / base URL → do NOT poll. This is a
        # MISCONFIG that will NOT self-heal (a removed/rotated Keychain entry or an unset
        # Worker base URL) and STOPS all filing → page immediately (CRITICAL). And do NOT
        # write the watchdog freshness marker: a cycle that never polled must let Check C go
        # stale, so a sustained no-creds state ALSO surfaces via the staleness floor (the
        # marker was previously written here, which masked the outage from Check C).
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            (
                # Deliberately does NOT interpolate the ITS_Config key or the
                # Keychain entry NAMES — naming secret-store entries in a log is
                # both a CodeQL clear-text-logging trip and poor hygiene. The
                # operator looks them up in the §43 runbook.
                "fail-closed: missing portal credentials — the Worker base URL "
                "(ITS_Config) and/or the bearer + HMAC-secret Keychain entries are unset; "
                "NOT polling and filing is STOPPED until fixed (see safety_reports/README.md §43)"
            ),
            error_code="portal_creds_missing",
        )
        _write_heartbeat()
        _write_heartbeat_row(status="ERROR", items_processed=0,
                             error_summary="fail-closed: portal credentials missing")
        return PollStats(halted_no_creds=True)

    # ── DR-photo-pool Slice 2: the daily-pool screening pass runs BEFORE the submission
    # drain — before the /pending FETCH itself. THE ORDERING CRUX: additional photos
    # upload individually BEFORE their referencing daily report submits, so by screening
    # the pool first (claimed rows included), a photo uploaded before submit is usually
    # CLEAN by the time its referencing submission processes — and because the fetch
    # below happens AFTER this pass, the claim manifest the Worker attaches to each
    # /pending row ({id, status, box_file_id}) reflects POST-screen state, so the
    # common same-cycle case files immediately instead of taking a spurious one-cycle
    # defer. A still-pending reference at intake time defers the submission one cycle
    # (bounded — see intake._resolve_additional_photos); this pass + the re-pull is
    # what resolves it. FENCED like every best-effort pass (own error code, WARN,
    # never blocks the drain): a dead pass degrades referencing submissions to the
    # bounded defer-then-file-without path and leaves non-referencing submissions
    # untouched — while an unscreened backlog is NEVER silent (watchdog Check C /
    # ITS_Daemon_Health page a dead daemon; the Worker's >7d unclaimed prune stage is
    # the loud growth cap for abandoned uploads).
    daily_screened = 0
    try:
        daily_screened = _service_daily_photos(creds.base_url, creds.bearer, creds.secret)
    except Exception as exc:  # noqa: BLE001 — best-effort; must not block intake filing
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"daily-photo screening pass failed (intake unaffected): "
            f"{type(exc).__name__}: {exc!r}",
            error_code="portal_daily_photo_service_failed",
        )

    try:
        rows = portal_client.get_pending(creds.base_url, creds.bearer, limit=PENDING_LIMIT)
    except portal_client.PortalAuthError as exc:
        # 401 — bad/rotated/missing bearer. A MISCONFIG that will NOT self-heal and STOPS all
        # filing → page immediately (CRITICAL). No watchdog marker (let Check C go stale too).
        # Caught BEFORE PortalTransportError (its subclass) so auth never reads as transient.
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            f"portal pending fetch UNAUTHORIZED (401) — bearer token rejected; filing is "
            f"STOPPED until the token is fixed: {exc!r}",
            error_code="portal_pending_auth_failed", exc_info=repr(exc),
        )
        _write_heartbeat()
        _write_heartbeat_row(status="ERROR", items_processed=0,
                             error_summary="pending fetch UNAUTHORIZED (401) — bearer rejected")
        return PollStats(errors=1, daily_photos_screened=daily_screened)
    except portal_client.PortalTransportError as exc:
        # Transport failure (Worker down / wrong base URL / network). A one-off blip is ERROR
        # and self-heals; a SUSTAINED outage (>= threshold consecutive cycles) escalates to
        # CRITICAL — filing is stopped and that must PAGE, not just WARN at the next daily
        # watchdog. No watchdog marker either way (the un-faked Check-C marker is the backstop).
        #
        # The CRITICAL fires on the shared LADDER (crossing cycle, then 2×/4×/8× …), not on
        # every cycle past the threshold: an open CRITICAL is never terminal per
        # `errors_rotation`, so `n >= threshold` minted UNROTATABLE rows for the entire
        # outage. Every cycle still writes its per-occurrence row, at ERROR in between.
        n = _record_fetch_failure()
        sustained = sustained_failure.is_escalation_cycle(n, FETCH_FAIL_CRITICAL_THRESHOLD)
        error_log.log(
            Severity.CRITICAL if sustained else Severity.ERROR, SCRIPT_NAME,
            f"failed to GET pending (consecutive failure #{n}, CRITICAL from "
            f"{FETCH_FAIL_CRITICAL_THRESHOLD} on the escalation ladder"
            + (" — SUSTAINED: filing is STOPPED and submissions are accumulating"
               if sustained else "")
            + f"): {exc!r}",
            error_code="portal_pending_fetch_failed",
        )
        _write_heartbeat()
        _write_heartbeat_row(status="ERROR", items_processed=0,
                             error_summary=f"pending fetch failed (#{n}): {type(exc).__name__}")
        return PollStats(errors=1, daily_photos_screened=daily_screened)

    # Fetch succeeded → clear the consecutive-failure counter (a recovered blip never
    # accumulates toward the CRITICAL threshold).
    _reset_fetch_failures()

    seen = _load_seen()
    counters = {
        "filed": 0, "reviewed": 0, "rejected": 0, "remarked": 0, "errors": 0,
        "deferred": 0, "pdf_serviced": 0, "item_photos": 0,
        "daily_photos": daily_screened,
    }

    for row in rows:
        # Split the HMAC off the row IMMEDIATELY: it is verified separately and
        # never travels into intake or any log line (the `clean` dict is what the
        # rest of the cycle sees). Keeps signature material isolated to verify.
        provided_hmac = str(row.get("hmac") or "")
        clean = {k: v for k, v in row.items() if k != "hmac"}
        try:
            _process_row(
                clean, provided_hmac, creds.base_url, creds.bearer, creds.secret,
                seen, counters,
            )
        except Exception as exc:  # noqa: BLE001 — per-row fence; one bad row never kills the cycle
            counters["errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                (
                    f"per-row unexpected exception submission_uuid="
                    f"{clean.get('submission_uuid')!r}: {type(exc).__name__}: {exc!r}"
                ),
                error_code="portal_row_unexpected",
            )

    _persist_seen(seen)

    # Best-effort request-driven PDF cache servicing (PR-4 Part A). Placed AFTER the
    # intake drain + _persist_seen so a PDF-service failure can NEVER affect filing,
    # and FENCED identically to the job-sync below (a failure WARNs, never blocks the
    # pull). The pass is idempotent end-to-end (per-chunk INSERT OR REPLACE Worker-side),
    # so a skipped/failed cycle self-heals on the next.
    try:
        counters["pdf_serviced"] += _service_pdf_requests(creds.base_url, creds.bearer)
    except Exception as exc:  # noqa: BLE001 — best-effort; must not block intake filing
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"PDF-request servicing failed (intake unaffected): {type(exc).__name__}: {exc!r}",
            error_code="portal_pdf_service_failed",
        )

    # Best-effort checklist item-photo screening pass (G1 Slice 2). CLONED from the PDF
    # pass above: placed AFTER the intake drain + _persist_seen so a pass failure can
    # NEVER affect submission filing, and FENCED identically (its own error_code, WARN,
    # never blocks the drain). Idempotent end-to-end: the Worker serves only
    # status='pending' rows, the disposition post-back is a found=false no-op on a
    # re-screened row, and Box filing is version-on-conflict — so a skipped/failed
    # cycle self-heals on the next. An unscreened backlog is NEVER silent: a dead pass
    # leaves rows pending → watchdog Check C / ITS_Daemon_Health page the dead daemon
    # within hours, and the Worker's >7d stuck-pending prune stage is the loud growth
    # cap (prune.ts ITEM_PHOTO_STUCK_PENDING_DAYS — the deleting backstop, not a page;
    # Check V does not page on prune counters, so the two signals never double-page).
    try:
        counters["item_photos"] += _service_item_photos(
            creds.base_url, creds.bearer, creds.secret
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; must not block intake filing
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"item-photo screening pass failed (intake unaffected): "
            f"{type(exc).__name__}: {exc!r}",
            error_code="portal_item_photo_service_failed",
        )

    # Best-effort job-set sync (ITS_Active_Jobs → the Worker's D1 dropdown cache).
    # FENCED so a sync failure never affects the intake drain above; the sync is
    # idempotent (full-replace), so a skipped/failed cycle self-heals next time.
    try:
        _push_active_jobs(creds.base_url, creds.bearer)
    except Exception as exc:  # noqa: BLE001 — best-effort; must not block intake filing
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"job sync push failed (intake unaffected): {type(exc).__name__}: {exc!r}",
            error_code="portal_job_sync_failed",
        )

    _write_heartbeat()

    if counters["errors"] > 0:
        cycle_status: HeartbeatStatus = "DEGRADED"
    elif counters["rejected"] > 0 or counters["reviewed"] > 0:
        cycle_status = "WARN"
    else:
        cycle_status = "OK"
    if circuit_breaker.is_open():
        cycle_status = "CIRCUIT_OPEN"

    try:
        _write_heartbeat_row(
            status=cycle_status,
            items_processed=counters["filed"],
            error_summary=(
                None
                if counters["errors"] == 0 and counters["rejected"] == 0
                else f"errors={counters['errors']} rejected={counters['rejected']}"
            ),
            notes=_cycle_notes(counters),
        )
    except Exception as exc:  # noqa: BLE001 — heartbeat must never block
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"heartbeat write outer-catch tripped: {exc!r}",
            error_code="daemon_health_write_failed",
        )
    _write_watchdog_marker()

    # A4: record the unfiled-backlog marker (drained = every disposition that gets the row
    # marked-filed, i.e. any progress). A saturated page that drained nothing latches the
    # stuck-backlog signal for watchdog Check Q. Fail-soft inside the helper — never blocks.
    _record_pending_backlog(
        len(rows),
        counters["filed"] + counters["reviewed"]
        + counters["rejected"] + counters["remarked"],
    )

    error_log.log(
        Severity.INFO, SCRIPT_NAME,
        (
            f"poll cycle: scanned={len(rows)} filed={counters['filed']} "
            f"reviewed={counters['reviewed']} rejected={counters['rejected']} "
            f"remarked={counters['remarked']} errors={counters['errors']} "
            f"deferred={counters['deferred']} "
            f"pdf_serviced={counters['pdf_serviced']} "
            f"item_photos_screened={counters['item_photos']} "
            f"daily_photos_screened={counters['daily_photos']}"
        ),
        error_code="poll_cycle_summary",
    )
    return PollStats(
        scanned=len(rows),
        filed=counters["filed"],
        reviewed=counters["reviewed"],
        rejected=counters["rejected"],
        remarked=counters["remarked"],
        errors=counters["errors"],
        deferred=counters["deferred"],
        pdf_serviced=counters["pdf_serviced"],
        item_photos_screened=counters["item_photos"],
        daily_photos_screened=counters["daily_photos"],
    )


def _process_row(
    row: dict[str, Any],
    provided_hmac: str,
    base_url: str,
    bearer: str,
    secret: str,
    seen: dict[str, dict[str, Any]],
    counters: dict[str, int],
) -> None:
    """Verify + dispatch + receipt one pulled row. `row` is HMAC-free (the caller
    split the signature off into `provided_hmac`); mutates `seen` + `counters`."""
    submission_uuid = str(row.get("submission_uuid") or "")
    if not submission_uuid:
        # A row with no UUID can't be receipted/deduped — flag, don't dispatch.
        counters["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            "pulled row missing submission_uuid; skipping",
            error_code="portal_row_no_uuid",
        )
        return

    rec = seen.get(submission_uuid)
    if rec is not None:
        if rec.get("status") == "rejected":
            # Already flagged a bad-HMAC row in a PRIOR cycle (the seen-set
            # prevents Review-Queue spam on re-pulls). The first rejection did the
            # anomaly-log + Review-Queue + CRITICAL via _handle_hmac_failure; this
            # repeat is intentionally silent + never dispatched/drained.
            return
        if rec.get("status") == "filed":
            # Already filed but the row is being served again → the mark-filed
            # receipt was lost. Re-post it (no re-file) to drain the queue. Carry
            # box_file_id (PR-4) so the cache handle survives the re-post; fall back
            # to parsing the link for a seen-set record written before PR-4.
            box_link = str(rec.get("box_link") or "")
            box_file_id = str(rec.get("box_file_id") or "") or _box_file_id(box_link)
            if portal_client.mark_filed(
                base_url, bearer, submission_uuid=submission_uuid,
                box_link=box_link, box_file_id=box_file_id or None,
            ):
                counters["remarked"] += 1
            return

    correlation_id = uuid.uuid4().hex[:12]

    # Downgrade defense: verify the HMAC BEFORE intake ever sees the row.
    if not _verify_row_hmac(row, provided_hmac, secret):
        _handle_hmac_failure(row, correlation_id, base_url=base_url, bearer=bearer)
        seen[submission_uuid] = {"status": "rejected"}
        counters["rejected"] += 1
        return

    result = intake.process_portal_submission(row)

    if result.status == "error":
        # TRANSIENT — do NOT mark-filed, do NOT record seen; re-pull retries.
        counters["errors"] += 1
        return

    if result.status == "deferred":
        # DR-photo-pool Slice 2 — the submission references pool photos still PENDING
        # §34 screening. The SAME soft-fail mechanics as 'error' (NOT mark-filed, NOT
        # seen-recorded → the row re-pulls next cycle, by which time the pass above
        # has usually screened them), but counted SEPARATELY: a defer is an expected
        # ordering race, not an infra failure — the cycle status stays OK and no
        # error row spams ITS_Errors. Bounded intake-side: after
        # intake.DAILY_PHOTO_DEFER_MAX_SECONDS the submission files WITHOUT the
        # missing photos + a PDF note + a WARN (never blocks filing forever).
        counters["deferred"] += 1
        return

    if result.status in DRAIN_STATUSES:
        box_link = result.box_link or ""
        box_file_id = result.box_file_id or ""
        marked = portal_client.mark_filed(
            base_url, bearer, submission_uuid=submission_uuid, box_link=box_link,
            box_file_id=box_file_id or None,
        )
        # Record as filed so a future re-serve (lost receipt) re-posts without re-filing.
        # box_file_id rides too so the re-post (843-851) re-carries the cache handle.
        seen[submission_uuid] = {
            "status": "filed", "box_link": box_link, "box_file_id": box_file_id,
        }
        if result.status == "review_queue":
            counters["reviewed"] += 1
        else:
            counters["filed"] += 1
        if not marked:
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                (
                    f"mark-filed returned found=False for submission_uuid={submission_uuid} "
                    f"(status={result.status}); Worker had no matching row"
                ),
                error_code="portal_mark_filed_not_found",
                correlation_id=result.correlation_id,
            )
        # 0074 site-photos bridge — register the filed inline photos in the WPR pool.
        # AFTER mark-filed and §43 BEST-EFFORT FENCED (the *_perjob_sheet_failed posture):
        # a register failure WARNs and the filing/receipt stand untouched — the miss is
        # permanent for this cycle (already_filed re-serves carry no registrations) and
        # the repair is the operator backfill script. Only a genuinely-processed result
        # carries registrations, so the fence can never fire on a review/quarantine drain.
        if result.status == "processed" and result.site_photo_registrations:
            try:
                portal_client.post_daily_photos_register(
                    base_url, bearer,
                    submission_uuid=submission_uuid,
                    photos=result.site_photo_registrations,
                )
            except Exception as exc:  # noqa: BLE001 — best-effort; never disturb the filing
                error_log.log(
                    Severity.WARN, SCRIPT_NAME,
                    (
                        f"site-photo pool registration failed for submission_uuid="
                        f"{submission_uuid} ({len(result.site_photo_registrations)} photo(s)): "
                        f"{type(exc).__name__}: {exc!r}"
                    ),
                    error_code="portal_site_photo_register_failed",
                    correlation_id=result.correlation_id,
                )


# ---- PR-4 Part A: request-driven PDF cache servicing ---------------------


def _service_pdf_requests(base_url: str, bearer: str) -> int:
    """Service request-driven PDF caches → returns the count of items serviced.

    A user who clicked "make available for download" flips pdf_requested=1 on a
    FILED submission; the Worker exposes those rows (box_file_id known, not yet
    cached) at GET /api/internal/pdf-requests. For each, this fetches the canonical
    filed PDF from Box (by box_file_id), base64-chunks it, and POSTs each chunk to
    POST /api/internal/filed-pdf — the Worker reassembles + serves the PM the
    byte-identical Box-filed copy.

    BEST-EFFORT + PER-ITEM FENCED: the whole pass is wrapped by the caller's
    try/except (a total failure WARNs, never blocks the intake pull); inside, one bad
    item (missing id / Box fetch error / upload error) is logged + skipped so it never
    aborts servicing the rest. Idempotent end-to-end — the Worker INSERT-OR-REPLACEs
    each chunk and a re-pulled request after a lost ack is a no-op.

    Box fetch is generation-side (already used by intake); the HTTPS post-back rides
    the F02-allowlisted portal_client. No send capability, no LLM (Invariant 1).
    """
    rows = portal_client.get_pdf_requests(base_url, bearer, limit=PDF_REQUEST_LIMIT)
    serviced = 0
    for row in rows:
        submission_uuid = str(row.get("submission_uuid") or "")
        box_file_id = str(row.get("box_file_id") or "")
        if not submission_uuid or not box_file_id:
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"pdf-request row missing submission_uuid/box_file_id; skipping "
                f"(submission_uuid={submission_uuid!r})",
                error_code="portal_pdf_request_malformed",
            )
            continue
        try:
            pdf = box_client.download_file(box_file_id)
            if not pdf:
                # A zero-byte filed PDF is a DATA error (a render/upload that produced
                # nothing), not a chunk to ship — an empty chunk_b64 would only be 400'd by
                # the Worker. Surface it as a WARN skip; the request stays unready and the
                # operator can re-file. (Never the silent empty-chunk the Worker rejects.)
                error_log.log(
                    Severity.WARN, SCRIPT_NAME,
                    f"pdf-request: Box file {box_file_id} returned 0 bytes; skipping "
                    f"(submission_uuid={submission_uuid!r})",
                    error_code="portal_pdf_empty_file",
                )
                continue
            chunks = [
                pdf[i:i + PDF_CHUNK_BYTES] for i in range(0, len(pdf), PDF_CHUNK_BYTES)
            ]
            total = len(chunks)
            for index, chunk in enumerate(chunks):
                portal_client.upload_filed_pdf(
                    base_url, bearer,
                    submission_uuid=submission_uuid,
                    chunk_index=index,
                    chunk_total=total,
                    chunk_b64=base64.b64encode(chunk).decode(),
                )
            serviced += 1
        except Exception as exc:  # noqa: BLE001 — per-item fence; one bad item never aborts the pass
            # NEVER interpolate PDF bytes / chunk_b64 into the log line.
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"pdf-request servicing failed for submission_uuid={submission_uuid}: "
                f"{type(exc).__name__}: {exc!r}",
                error_code="portal_pdf_request_item_failed",
            )
    return serviced


def _cycle_notes(counters: dict[str, int]) -> str | None:
    """Compose the optional ITS_Daemon_Health Notes fragment from the best-effort
    pass counters (PDF servicing + item/daily photo screening) and the pool-defer
    count. None when all are zero (a quiet cycle writes no note — the pre-G1
    behavior)."""
    parts = []
    if counters["pdf_serviced"]:
        parts.append(f"pdf_serviced={counters['pdf_serviced']}")
    if counters["item_photos"]:
        parts.append(f"item_photos_screened={counters['item_photos']}")
    if counters.get("daily_photos"):
        parts.append(f"daily_photos_screened={counters['daily_photos']}")
    if counters.get("deferred"):
        parts.append(f"deferred={counters['deferred']}")
    return "; ".join(parts) if parts else None


# ---- G1 Slice 2: checklist item-photo screening pass ----------------------
#
# The Mac half of the G1 item-photo capture queue (Option D, RATIFIED 2026-07-03:
# record-only — the photo's permanent home is Box; NO route ever serves the bytes to a
# browser; DELETE-ON-SCREEN — D1 holds bytes only while pending). Worker-side capture
# is fieldops_checklist.ts POST /api/fieldops/checklist/item-state/:id/photo
# (migration 0036). This pass is a fenced clone of _service_pdf_requests:
#
#   GET /api/internal/item-photos/pending
#     → per row: verify the item-photo HMAC (shared.portal_hmac.verify_item_photo,
#       constant-time — the downgrade defense; a bad row is one-shot-flagged and NEVER
#       screened or filed)
#     → run the BYTE-IDENTICAL §34 pipeline the submission photos get:
#       photo_screen.decode_b64 → photo_screen.screen_photo — same module, same
#       layers (L1 magic/size → L2 verify+bomb-cap+metadata-destroying re-encode →
#       optional L3 ClamAV), same ITS_Config gate
#       (safety_reports.photo_screen.clamav_enabled via intake.CFG_PHOTO_CLAMAV — one
#       operator flag governs BOTH photo classes), same disposition ladder
#       (clean | suspicious | malicious).
#     → CLEAN: file the SANITIZED re-encode to Box
#       <portal root>/ITS Photos/checklist/<item_state_id>/photo_<id>.jpg
#       (version-on-conflict — idempotent re-screen), THEN post
#       {status:'clean', box_file_id} — Box-before-post-back is load-bearing: the
#       post-back deletes the D1 bytes (delete-on-screen), so the permanent record
#       must exist first or a crash window destroys the only copy.
#     → SUSPICIOUS/MALICIOUS: the intake._portal_photo_refusal pattern — page/record
#       FIRST (malicious → CRITICAL naming the account for operator disable;
#       suspicious → WARN), security-flagged Review-Queue row, then post
#       {status:'refused'}. The refused bytes are NEVER filed anywhere; the item's
#       COMPLETION STANDS (evidence refused ≠ work not done — the Worker touches only
#       photo_ref, never the item status).


def _resolve_item_photo_clamav() -> tuple[bool, str]:
    """Resolve the §34 L3 ClamAV gate for the item-photo pass — the SAME ITS_Config
    key intake's submission-photo screening reads (intake.CFG_PHOTO_CLAMAV =
    `safety_reports.photo_screen.clamav_enabled`, default OFF), referenced through
    intake's constant so the two surfaces can never drift apart (multi-surface
    fan-out class). Returns `(enabled, source)` — source is `"ITS_Config"` or
    `"default"` — so the caller logs the resolution observably (config resolution is
    never silent, forensic class #7; the §43 runbook's clamd Symptom 2 applies to
    item photos unchanged)."""
    raw = _read_str_setting(intake.CFG_PHOTO_CLAMAV, "")
    if not raw.strip():
        return False, "default"
    return raw.strip().lower() in ("true", "1", "yes", "on"), "ITS_Config"


def _load_item_photo_flags() -> dict[str, str]:
    """Load the bad-HMAC one-shot flag set `{photo_id: 'flagged'}`. {} on any read
    error (fail-open: the only cost is one redundant re-flag, never a missed alert)."""
    if not ITEM_PHOTO_FLAGGED_PATH.exists():
        return {}
    try:
        parsed = json.loads(ITEM_PHOTO_FLAGGED_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _persist_item_photo_flags(flags: dict[str, str]) -> None:
    """Atomically persist the flag set, capped to MAX_ITEM_PHOTO_FLAGS (drained rows
    leave dead weight only). Lock-timeout fails OPEN with a WARN — a lost flag set
    costs a duplicate Review-Queue flag next cycle, never a missed one."""
    if len(flags) > MAX_ITEM_PHOTO_FLAGS:
        flags = dict(list(flags.items())[-MAX_ITEM_PHOTO_FLAGS:])
    ITEM_PHOTO_FLAGGED_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with state_io.with_path_lock(ITEM_PHOTO_FLAGGED_PATH):
            state_io.atomic_write_json(ITEM_PHOTO_FLAGGED_PATH, flags)
    except state_io.StateLockTimeoutError:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"could not acquire lock on {ITEM_PHOTO_FLAGGED_PATH} after retries; "
            f"item-photo flag set not persisted",
            error_code="portal_item_photo_flags_persist_failed",
        )


def _handle_item_photo_hmac_failure(
    photo_id: int,
    item_state_id: int,
    correlation_id: str,
    *,
    base_url: str,
    bearer: str,
    flags: dict[str, str],
) -> None:
    """Reject a bad-HMAC item-photo row — the item-photo twin of _handle_hmac_failure.

    NEVER screened, NEVER filed (downgrade defense). One-shot: the anomaly-log +
    Review-Queue + CRITICAL fire only on the FIRST sighting (the `flags` set
    suppresses per-cycle re-flag spam if the drain below is lost); the refused
    post-back retries EVERY cycle until the Worker drains the row (photo_json NULLed,
    photo_ref 'refused:<id>' — the crew sees a refused marker and can retry)."""
    key = str(photo_id)
    if key not in flags:
        # Tripwire (Invariant 2, Layer 5) — record the suspicious pattern.
        anomaly_logger.check({
            "portal_item_photo_hmac_failure": photo_id, "item_state_id": item_state_id,
        })
        review_queue.add(
            workstream=WORKSTREAM,
            summary=(
                f"portal: HMAC verification FAILED for checklist item photo {photo_id} "
                f"(item_state_id={item_state_id}) — rejected, NOT screened or filed"
            ),
            payload={
                "item_photo_id": photo_id,
                "item_state_id": item_state_id,
                # The HMAC value is deliberately NOT recorded (signature material —
                # same posture as the submission twin); photo bytes NEVER ride a
                # Review-Queue payload.
            },
            sla_tier=review_queue.SlaTier.SAFETY_INTAKE,
            reason=review_queue.ReviewReason.SECURITY_TRIGGER,
            severity=Severity.CRITICAL,
            source_file=f"item_photo:{photo_id}",
            security_flag=True,
        )
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            (
                f"portal item-photo HMAC FAIL photo_id={photo_id} "
                f"item_state_id={item_state_id} — rejected, not screened or filed "
                f"(downgrade defense)"
            ),
            error_code="portal_item_photo_hmac_failure",
            correlation_id=correlation_id,
        )
        flags[key] = "flagged"
    # Drain the row (terminal refused; delete-on-screen NULLs the tampered bytes; the
    # Review-Queue row above is the forensic record). Best-effort: a transport failure
    # re-pulls next cycle — the flag set keeps the re-flag suppressed while this retries.
    try:
        portal_client.post_item_photo_result(
            base_url, bearer, photo_id=photo_id, status="refused",
            detail="hmac_verification_failed",
        )
    except portal_client.PortalTransportError as exc:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"portal could not post refused result for bad-HMAC item photo {photo_id}: {exc!r}",
            error_code="portal_item_photo_mark_refused_failed",
            correlation_id=correlation_id,
        )


def _item_photo_refusal(
    photo_id: int,
    item_state_id: int,
    uploaded_by: str,
    *,
    disposition: str,   # "malicious" | "suspicious"
    detail: str,
    correlation_id: str,
) -> None:
    """Refuse ONE checklist item photo on a screening verdict — the item-photo
    application of intake._portal_photo_refusal (same severities, same page-first
    ordering, same account-naming CRITICAL).

    The refused bytes are NEVER re-encoded, filed to Box, or served — the caller
    posts {status:'refused'} and the Worker NULLs photo_json (delete-on-screen).
    MALICIOUS pages the operator (CRITICAL) naming the uploading account (from the
    HMAC-covered photo_json, not a spoofable sidecar) with the disable instruction;
    SUSPICIOUS files a WARN-severity, security-flagged row without paging. The page
    fires BEFORE the Smartsheet write so an outage cannot suppress it. The ITEM
    COMPLETION STANDS — evidence refused ≠ work not done; a re-pulled row re-screens
    to the same verdict (deterministic pipeline) and the post-back is idempotent."""
    malicious = disposition == "malicious"
    severity = Severity.CRITICAL if malicious else Severity.WARN
    if malicious:
        summary = (
            f"MALICIOUS checklist item photo rejected ({detail}); DISABLE portal "
            f"account {uploaded_by!r} pending review (§34) — item photo {photo_id} "
            f"(item_state_id={item_state_id}); item completion stands"
        )
        page = (
            f"portal: MALICIOUS checklist item photo ({detail}) photo_id={photo_id} "
            f"item_state_id={item_state_id} actor={uploaded_by!r} — disable this "
            f"portal account pending review (§34)"
        )
    else:
        summary = (
            f"suspicious checklist item photo routed to review ({detail}) — item "
            f"photo {photo_id} (item_state_id={item_state_id}) actor "
            f"{uploaded_by!r}; item completion stands"
        )
        page = (
            f"portal: suspicious checklist item photo ({detail}) photo_id={photo_id} "
            f"item_state_id={item_state_id} actor={uploaded_by!r}"
        )
    # Page/record FIRST (never blocked by a Smartsheet write failure).
    error_log.log(
        severity, SCRIPT_NAME, page,
        error_code=f"portal_item_photo_{disposition}", correlation_id=correlation_id,
    )
    review_queue.add(
        workstream=WORKSTREAM,
        summary=summary,
        payload={
            "item_photo_id": photo_id,
            "item_state_id": item_state_id,
            "actor": uploaded_by,
            "disposition": disposition,
            "detail": detail,
            # Photo BYTES deliberately absent: delete-on-screen means D1 drops them on
            # the refused post-back and nothing durable retains attacker-controlled
            # binary. The verdict detail + actor are the forensic handle.
        },
        sla_tier=review_queue.SlaTier.SAFETY_INTAKE,
        reason=review_queue.ReviewReason.SECURITY_TRIGGER,
        severity=severity,
        source_file=f"item_photo:{photo_id}",
        security_flag=True,
    )


def _file_item_photo(item_state_id: int, photo_id: int, clean_jpeg: bytes) -> str:
    """File the §34-SANITIZED re-encode (never the raw upload) to Box under
    `<portal root>/ITS Photos/checklist/<item_state_id>/photo_<photo_id>.jpg`;
    return the Box file id.

    intake._file_portal_photos' shape: find-or-create at every level +
    version-on-conflict upload (a re-screen after a lost ack re-uploads a new VERSION
    of the same file — idempotent, stable file id). Raises on any failure —
    including an unset portal Box root (ITS_Config
    `safety_reports.box.portal_root_folder_id`, the same key intake's mirror tree
    reads) — so the caller's per-item fence WARNs and the row STAYS PENDING (Box is
    the permanent record; a clean result may never be posted without it)."""
    root = _read_str_setting(safety_naming.CFG_BOX_PORTAL_ROOT, "").strip()
    if not root:
        raise RuntimeError(
            "portal Box root unset (ITS_Config safety_reports.box.portal_root_folder_id) "
            "— cannot file item photo; row stays pending until configured"
        )
    photos_root = box_client.get_or_create_folder(root, ITEM_PHOTO_BOX_FOLDER)
    checklist_root = box_client.get_or_create_folder(photos_root, ITEM_PHOTO_BOX_SUBFOLDER)
    leaf = box_client.get_or_create_folder(checklist_root, str(item_state_id))
    uploaded = box_client.upload_bytes_or_new_version(
        leaf, f"photo_{photo_id}.jpg", clean_jpeg
    )
    return str(uploaded["id"])


def _screen_one_item_photo(
    row: dict[str, Any],
    *,
    base_url: str,
    bearer: str,
    secret: str,
    clamav_enabled: bool,
    flags: dict[str, str],
) -> bool:
    """Verify + screen + disposition ONE pulled item-photo row. Returns True when the
    photo reached a terminal disposition post-back (clean or refused) this cycle.
    Raises only on transport/Box failures the per-item fence should WARN about (the
    row stays pending and re-pulls next cycle)."""
    photo_id = row.get("id")
    item_state_id = row.get("item_state_id")
    photo_json = row.get("photo_json")
    if (
        not isinstance(photo_id, int)
        or not isinstance(item_state_id, int)
        or not isinstance(photo_json, str)
        or not photo_json
    ):
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"item-photo row malformed (id={row.get('id')!r}); skipping",
            error_code="portal_item_photo_malformed",
        )
        return False

    correlation_id = uuid.uuid4().hex[:12]

    # Downgrade defense: verify the HMAC BEFORE any byte is decoded. photo_json is
    # the VERBATIM stored string (re-serializing would change the bytes); the compare
    # is constant-time (shared.portal_hmac).
    provided_hmac = str(row.get("hmac") or "")
    if not portal_hmac.verify_item_photo(
        secret, provided_hmac, item_state_id=item_state_id, photo_json=photo_json
    ):
        _handle_item_photo_hmac_failure(
            photo_id, item_state_id, correlation_id,
            base_url=base_url, bearer=bearer, flags=flags,
        )
        return False

    # HMAC verified — parse the covered JSON. uploaded_by rides INSIDE photo_json so
    # the malicious CRITICAL names the account from HMAC-covered data.
    try:
        parsed: Any = json.loads(photo_json)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        # Authenticated but structurally impossible (the Worker only signs the exact
        # 5-key JSON it built) — refuse as suspicious rather than crash the pass.
        _item_photo_refusal(
            photo_id, item_state_id, "unknown",
            disposition="suspicious", detail="unparseable_photo_json",
            correlation_id=correlation_id,
        )
        portal_client.post_item_photo_result(
            base_url, bearer, photo_id=photo_id, status="refused",
            detail="unparseable_photo_json",
        )
        return True
    uploaded_by = str(parsed.get("uploaded_by") or "unknown")

    decoded = photo_screen.decode_b64(str(parsed.get("data") or ""))
    if decoded is None:
        _item_photo_refusal(
            photo_id, item_state_id, uploaded_by,
            disposition="suspicious", detail="undecodable_base64",
            correlation_id=correlation_id,
        )
        portal_client.post_item_photo_result(
            base_url, bearer, photo_id=photo_id, status="refused",
            detail="undecodable_base64",
        )
        return True

    # The §34 trust boundary — the BYTE-IDENTICAL pipeline submission photos get
    # (same module, same layers, same config gate, same disposition ladder).
    result = photo_screen.screen_photo(decoded, clamav_enabled=clamav_enabled)
    if result.disposition in ("malicious", "suspicious"):
        detail = f"{result.layer}:{result.detail}"
        _item_photo_refusal(
            photo_id, item_state_id, uploaded_by,
            disposition=result.disposition, detail=detail,
            correlation_id=correlation_id,
        )
        portal_client.post_item_photo_result(
            base_url, bearer, photo_id=photo_id, status="refused", detail=detail,
        )
        return True

    # CLEAN — Box FIRST, post-back SECOND (delete-on-screen: the post-back deletes the
    # D1 bytes, so the permanent Box record must exist before it). A Box failure
    # raises to the per-item fence: WARN, row stays pending, next cycle retries.
    box_file_id = _file_item_photo(item_state_id, photo_id, result.clean_jpeg or b"")
    found = portal_client.post_item_photo_result(
        base_url, bearer, photo_id=photo_id, status="clean", box_file_id=box_file_id,
    )
    if not found:
        # Benign: the disposition was already applied by a prior cycle whose ack was
        # lost (idempotent re-screen), or the row was pruned. The Box upload above was
        # version-on-conflict, so no duplicate artifact either way.
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            (
                f"item-photo result post found=False for photo_id={photo_id} "
                f"(already screened or pruned) — benign no-op"
            ),
            error_code="portal_item_photo_result_not_found",
            correlation_id=correlation_id,
        )
    return True


def _service_item_photos(base_url: str, bearer: str, secret: str) -> int:
    """Screen queued checklist item photos → returns the count dispositioned.

    The fenced pass CLONED from _service_pdf_requests (see the block comment above
    for the full contract). BEST-EFFORT + PER-ITEM FENCED: the whole pass is wrapped
    by the caller's try/except (a total failure WARNs with
    error_code=portal_item_photo_service_failed and NEVER blocks the submission
    drain); inside, one bad item is logged + skipped so it never aborts the rest.

    Config resolution is OBSERVABLE (forensic class #7): the ClamAV gate's resolved
    value + source are logged whenever the pass has work — the same
    `safety_reports.photo_screen.clamav_enabled` flag that governs submission photos.
    """
    rows = portal_client.get_item_photos_pending(base_url, bearer, limit=ITEM_PHOTO_LIMIT)
    if not rows:
        return 0
    clamav_enabled, clamav_source = _resolve_item_photo_clamav()
    error_log.log(
        Severity.INFO, SCRIPT_NAME,
        (
            f"item-photo screen: {len(rows)} pending; clamav_enabled={clamav_enabled} "
            f"(source={clamav_source}, key={intake.CFG_PHOTO_CLAMAV})"
        ),
        error_code="portal_item_photo_config_resolved",
    )
    flags = _load_item_photo_flags()
    serviced = 0
    for row in rows:
        try:
            if _screen_one_item_photo(
                row, base_url=base_url, bearer=bearer, secret=secret,
                clamav_enabled=clamav_enabled, flags=flags,
            ):
                serviced += 1
        except Exception as exc:  # noqa: BLE001 — per-item fence; one bad item never aborts the pass
            # NEVER interpolate photo bytes / photo_json into the log line.
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"item-photo screening failed for photo_id={row.get('id')!r}: "
                f"{type(exc).__name__}: {exc!r}",
                error_code="portal_item_photo_item_failed",
            )
    _persist_item_photo_flags(flags)
    return serviced


# ---- DR-photo-pool Slice 2: additional-photo pool screening pass -----------
#
# The Mac half of the additional-photo POOL (migration 0037; Option D inherited
# from G1: record-only — the photo's permanent home is Box; NO route ever serves
# the bytes to a browser; DELETE-ON-SCREEN — D1 holds bytes only while pending).
# The pool is keyed per-(job, work_date) and mountable on ANY form carrying an
# `additional_photos` section (it shipped with the daily report, hence the
# load-bearing "daily" names on the error codes, Box path, and state files —
# deliberately KEPT; the scope is not daily-report-only). Worker-side capture is
# fieldops_daily_photos.ts
# POST /api/fieldops/daily-photo; the submission carries only CLAIMED references
# (values.additional_photos). This pass is the item-photo pass CLONED onto the pool:
#
#   GET /api/internal/daily-photos/pending  (claimed + unclaimed pending alike)
#     → per row: verify the daily-photo HMAC (shared.portal_hmac.verify_daily_photo,
#       constant-time; job_id + work_date are inside the canonical, so a signed photo
#       cannot be replayed onto another job or date) — a bad row is one-shot-flagged
#       and NEVER screened or filed
#     → the BYTE-IDENTICAL §34 pipeline (photo_screen.decode_b64 → screen_photo, same
#       module / layers / ITS_Config ClamAV gate / disposition ladder)
#     → CLEAN: file the SANITIZED re-encode to Box
#       <portal root>/ITS Photos/daily/<job_id>/<work_date>/photo_<id>.jpg
#       (version-on-conflict — idempotent re-screen), THEN post
#       {status:'clean', box_file_id} — Box-before-post-back is load-bearing: the
#       post-back deletes the D1 bytes (delete-on-screen), so the permanent record
#       must exist first or a crash window destroys the only copy. box_file_id is
#       what intake later downloads for the referencing report's PDF grid.
#     → SUSPICIOUS/MALICIOUS: the intake refusal pattern — page/record FIRST
#       (malicious → CRITICAL naming the account from the HMAC-covered photo_json;
#       suspicious → WARN), security-flagged Review-Queue row, then post
#       {status:'refused'}. The refused bytes are NEVER filed anywhere; a submission
#       already referencing the photo files WITH a "refused by screening" PDF note
#       (the claim stands as the forensic marker — evidence refused ≠ report invalid).
#
# ORDERING: called by _poll_inside_lock BEFORE the /pending submission fetch — see
# the block comment there (the design's crux: photos screen before their referencing
# submission is fetched, so the claim manifest reflects post-screen state).


def _load_daily_photo_flags() -> dict[str, str]:
    """Load the daily-pool bad-HMAC one-shot flag set `{photo_id: 'flagged'}`. {} on
    any read error (fail-open: the only cost is one redundant re-flag, never a missed
    alert)."""
    if not DAILY_PHOTO_FLAGGED_PATH.exists():
        return {}
    try:
        parsed = json.loads(DAILY_PHOTO_FLAGGED_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _persist_daily_photo_flags(flags: dict[str, str]) -> None:
    """Atomically persist the daily-pool flag set, capped to MAX_DAILY_PHOTO_FLAGS.
    Lock-timeout fails OPEN with a WARN — a lost flag set costs a duplicate
    Review-Queue flag next cycle, never a missed one."""
    if len(flags) > MAX_DAILY_PHOTO_FLAGS:
        flags = dict(list(flags.items())[-MAX_DAILY_PHOTO_FLAGS:])
    DAILY_PHOTO_FLAGGED_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with state_io.with_path_lock(DAILY_PHOTO_FLAGGED_PATH):
            state_io.atomic_write_json(DAILY_PHOTO_FLAGGED_PATH, flags)
    except state_io.StateLockTimeoutError:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"could not acquire lock on {DAILY_PHOTO_FLAGGED_PATH} after retries; "
            f"daily-photo flag set not persisted",
            error_code="portal_daily_photo_flags_persist_failed",
        )


def _handle_daily_photo_hmac_failure(
    photo_id: int,
    job_id: str,
    work_date: str,
    correlation_id: str,
    *,
    base_url: str,
    bearer: str,
    flags: dict[str, str],
) -> None:
    """Reject a bad-HMAC daily-pool row — the pool twin of
    _handle_item_photo_hmac_failure.

    NEVER screened, NEVER filed (downgrade defense). One-shot: the anomaly-log +
    Review-Queue + CRITICAL fire only on the FIRST sighting (the `flags` set
    suppresses per-cycle re-flag spam if the drain below is lost); the refused
    post-back retries EVERY cycle until the Worker drains the row (photo_json NULLed
    — the tampered bytes leave D1; a referencing submission renders the refused
    note)."""
    key = str(photo_id)
    if key not in flags:
        # Tripwire (Invariant 2, Layer 5) — record the suspicious pattern.
        anomaly_logger.check({
            "portal_daily_photo_hmac_failure": photo_id,
            "job_id": job_id, "work_date": work_date,
        })
        review_queue.add(
            workstream=WORKSTREAM,
            summary=(
                f"portal: HMAC verification FAILED for daily pool photo {photo_id} "
                f"(job_id={job_id!r} work_date={work_date}) — rejected, NOT screened "
                f"or filed"
            ),
            payload={
                "daily_photo_id": photo_id,
                "job_id": job_id,
                "work_date": work_date,
                # The HMAC value is deliberately NOT recorded (signature material —
                # same posture as the submission/item twins); photo bytes NEVER ride
                # a Review-Queue payload.
            },
            sla_tier=review_queue.SlaTier.SAFETY_INTAKE,
            reason=review_queue.ReviewReason.SECURITY_TRIGGER,
            severity=Severity.CRITICAL,
            source_file=f"daily_photo:{photo_id}",
            security_flag=True,
        )
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            (
                f"portal daily-photo HMAC FAIL photo_id={photo_id} job_id={job_id!r} "
                f"work_date={work_date} — rejected, not screened or filed "
                f"(downgrade defense)"
            ),
            error_code="portal_daily_photo_hmac_failure",
            correlation_id=correlation_id,
        )
        flags[key] = "flagged"
    # Drain the row (terminal refused; delete-on-screen NULLs the tampered bytes; the
    # Review-Queue row above is the forensic record). Best-effort: a transport failure
    # re-pulls next cycle — the flag set keeps the re-flag suppressed while this retries.
    try:
        portal_client.post_daily_photo_result(
            base_url, bearer, photo_id=photo_id, status="refused",
            detail="hmac_verification_failed",
        )
    except portal_client.PortalTransportError as exc:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"portal could not post refused result for bad-HMAC daily photo {photo_id}: {exc!r}",
            error_code="portal_daily_photo_mark_refused_failed",
            correlation_id=correlation_id,
        )


def _daily_photo_refusal(
    photo_id: int,
    job_id: str,
    work_date: str,
    uploaded_by: str,
    *,
    disposition: str,   # "malicious" | "suspicious"
    detail: str,
    correlation_id: str,
) -> None:
    """Refuse ONE daily-pool photo on a screening verdict — the pool application of
    intake._portal_photo_refusal (same severities, same page-first ordering, same
    account-naming CRITICAL).

    The refused bytes are NEVER re-encoded, filed to Box, or served — the caller
    posts {status:'refused'} and the Worker NULLs photo_json (delete-on-screen).
    MALICIOUS pages the operator (CRITICAL) naming the uploading account (from the
    HMAC-covered photo_json, not a spoofable sidecar) with the disable instruction;
    SUSPICIOUS files a WARN-severity, security-flagged row without paging. The page
    fires BEFORE the Smartsheet write so an outage cannot suppress it. A submission
    that already claimed the photo still FILES — its PDF renders a "refused by
    screening" note (evidence refused ≠ report invalid); a re-pulled row re-screens
    to the same verdict (deterministic pipeline) and the post-back is idempotent."""
    malicious = disposition == "malicious"
    severity = Severity.CRITICAL if malicious else Severity.WARN
    if malicious:
        summary = (
            f"MALICIOUS daily pool photo rejected ({detail}); DISABLE portal "
            f"account {uploaded_by!r} pending review (§34) — daily photo {photo_id} "
            f"(job_id={job_id!r} work_date={work_date})"
        )
        page = (
            f"portal: MALICIOUS daily pool photo ({detail}) photo_id={photo_id} "
            f"job_id={job_id!r} work_date={work_date} actor={uploaded_by!r} — "
            f"disable this portal account pending review (§34)"
        )
    else:
        summary = (
            f"suspicious daily pool photo routed to review ({detail}) — daily "
            f"photo {photo_id} (job_id={job_id!r} work_date={work_date}) actor "
            f"{uploaded_by!r}"
        )
        page = (
            f"portal: suspicious daily pool photo ({detail}) photo_id={photo_id} "
            f"job_id={job_id!r} work_date={work_date} actor={uploaded_by!r}"
        )
    # Page/record FIRST (never blocked by a Smartsheet write failure).
    error_log.log(
        severity, SCRIPT_NAME, page,
        error_code=f"portal_daily_photo_{disposition}", correlation_id=correlation_id,
    )
    review_queue.add(
        workstream=WORKSTREAM,
        summary=summary,
        payload={
            "daily_photo_id": photo_id,
            "job_id": job_id,
            "work_date": work_date,
            "actor": uploaded_by,
            "disposition": disposition,
            "detail": detail,
            # Photo BYTES deliberately absent: delete-on-screen means D1 drops them on
            # the refused post-back and nothing durable retains attacker-controlled
            # binary. The verdict detail + actor are the forensic handle.
        },
        sla_tier=review_queue.SlaTier.SAFETY_INTAKE,
        reason=review_queue.ReviewReason.SECURITY_TRIGGER,
        severity=severity,
        source_file=f"daily_photo:{photo_id}",
        security_flag=True,
    )


def _file_daily_photo(
    job_id: str, work_date: str, photo_id: int, clean_jpeg: bytes
) -> str:
    """File the §34-SANITIZED re-encode (never the raw upload) to Box under
    `<portal root>/ITS Photos/daily/<job_id>/<work_date>/photo_<photo_id>.jpg`;
    return the Box file id.

    _file_item_photo's shape: find-or-create at every level + version-on-conflict
    upload (a re-screen after a lost ack re-uploads a new VERSION of the same file —
    idempotent, stable file id). Every path component is HMAC-covered (job_id +
    work_date ride the daily-photo canonical) or served with the signed row (the
    photo id — the item-photo precedent). Raises on any failure — including an unset
    portal Box root — so the caller's per-item fence WARNs and the row STAYS PENDING
    (Box is the permanent record; a clean result may never be posted without it)."""
    root = _read_str_setting(safety_naming.CFG_BOX_PORTAL_ROOT, "").strip()
    if not root:
        raise RuntimeError(
            "portal Box root unset (ITS_Config safety_reports.box.portal_root_folder_id) "
            "— cannot file daily pool photo; row stays pending until configured"
        )
    photos_root = box_client.get_or_create_folder(root, ITEM_PHOTO_BOX_FOLDER)
    daily_root = box_client.get_or_create_folder(photos_root, DAILY_PHOTO_BOX_SUBFOLDER)
    job_folder = box_client.get_or_create_folder(daily_root, job_id)
    leaf = box_client.get_or_create_folder(job_folder, work_date)
    uploaded = box_client.upload_bytes_or_new_version(
        leaf, f"photo_{photo_id}.jpg", clean_jpeg
    )
    return str(uploaded["id"])


def _screen_one_daily_photo(
    row: dict[str, Any],
    *,
    base_url: str,
    bearer: str,
    secret: str,
    clamav_enabled: bool,
    flags: dict[str, str],
) -> bool:
    """Verify + screen + disposition ONE pulled daily-pool row. Returns True when
    the photo reached a terminal disposition post-back (clean or refused) this
    cycle. Raises only on transport/Box failures the per-item fence should WARN
    about (the row stays pending and re-pulls next cycle)."""
    photo_id = row.get("id")
    job_id = row.get("job_id")
    work_date = row.get("work_date")
    photo_json = row.get("photo_json")
    if (
        not isinstance(photo_id, int)
        or not isinstance(job_id, str) or not job_id
        or not isinstance(work_date, str) or not work_date
        or not isinstance(photo_json, str) or not photo_json
    ):
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"daily-photo row malformed (id={row.get('id')!r}); skipping",
            error_code="portal_daily_photo_malformed",
        )
        return False

    correlation_id = uuid.uuid4().hex[:12]

    # Downgrade defense: verify the HMAC BEFORE any byte is decoded. photo_json is
    # the VERBATIM stored string (re-serializing would change the bytes); the compare
    # is constant-time (shared.portal_hmac). job_id + work_date are INSIDE the
    # canonical — a signed photo replayed onto another job/date fails here.
    provided_hmac = str(row.get("hmac") or "")
    if not portal_hmac.verify_daily_photo(
        secret, provided_hmac, job_id=job_id, work_date=work_date, photo_json=photo_json
    ):
        _handle_daily_photo_hmac_failure(
            photo_id, job_id, work_date, correlation_id,
            base_url=base_url, bearer=bearer, flags=flags,
        )
        return False

    # HMAC verified — parse the covered JSON. uploaded_by rides INSIDE photo_json so
    # the malicious CRITICAL names the account from HMAC-covered data.
    try:
        parsed: Any = json.loads(photo_json)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        # Authenticated but structurally impossible (the Worker only signs the exact
        # 5-key JSON it built) — refuse as suspicious rather than crash the pass.
        _daily_photo_refusal(
            photo_id, job_id, work_date, "unknown",
            disposition="suspicious", detail="unparseable_photo_json",
            correlation_id=correlation_id,
        )
        portal_client.post_daily_photo_result(
            base_url, bearer, photo_id=photo_id, status="refused",
            detail="unparseable_photo_json",
        )
        return True
    uploaded_by = str(parsed.get("uploaded_by") or "unknown")

    decoded = photo_screen.decode_b64(str(parsed.get("data") or ""))
    if decoded is None:
        _daily_photo_refusal(
            photo_id, job_id, work_date, uploaded_by,
            disposition="suspicious", detail="undecodable_base64",
            correlation_id=correlation_id,
        )
        portal_client.post_daily_photo_result(
            base_url, bearer, photo_id=photo_id, status="refused",
            detail="undecodable_base64",
        )
        return True

    # The §34 trust boundary — the BYTE-IDENTICAL pipeline submission photos get
    # (same module, same layers, same config gate, same disposition ladder).
    result = photo_screen.screen_photo(decoded, clamav_enabled=clamav_enabled)
    if result.disposition in ("malicious", "suspicious"):
        detail = f"{result.layer}:{result.detail}"
        _daily_photo_refusal(
            photo_id, job_id, work_date, uploaded_by,
            disposition=result.disposition, detail=detail,
            correlation_id=correlation_id,
        )
        portal_client.post_daily_photo_result(
            base_url, bearer, photo_id=photo_id, status="refused", detail=detail,
        )
        return True

    # CLEAN — Box FIRST, post-back SECOND (delete-on-screen: the post-back deletes the
    # D1 bytes, so the permanent Box record must exist before it). A Box failure
    # raises to the per-item fence: WARN, row stays pending, next cycle retries.
    box_file_id = _file_daily_photo(job_id, work_date, photo_id, result.clean_jpeg or b"")
    # 0074: derive the picker thumbnail from the §34 clean re-encode (never raw bytes);
    # None (thumbnailing failed) degrades to a thumbless row — the disposition still lands.
    thumb = photo_screen.make_thumbnail(result.clean_jpeg or b"")
    found = portal_client.post_daily_photo_result(
        base_url, bearer, photo_id=photo_id, status="clean", box_file_id=box_file_id,
        thumb_b64=base64.b64encode(thumb).decode("ascii") if thumb is not None else None,
    )
    if not found:
        # Benign: the disposition was already applied by a prior cycle whose ack was
        # lost (idempotent re-screen), or the row was pruned. The Box upload above was
        # version-on-conflict, so no duplicate artifact either way.
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            (
                f"daily-photo result post found=False for photo_id={photo_id} "
                f"(already screened or pruned) — benign no-op"
            ),
            error_code="portal_daily_photo_result_not_found",
            correlation_id=correlation_id,
        )
    return True


def _service_daily_photos(base_url: str, bearer: str, secret: str) -> int:
    """Screen queued daily-pool photos → returns the count dispositioned.

    The fenced pass CLONED from _service_item_photos (see the block comment above
    for the full contract; the ORDERING note lives at the _poll_inside_lock call
    site). BEST-EFFORT + PER-ITEM FENCED: the whole pass is wrapped by the caller's
    try/except (a total failure WARNs with
    error_code=portal_daily_photo_service_failed and NEVER blocks the submission
    drain); inside, one bad item is logged + skipped so it never aborts the rest.

    Config resolution is OBSERVABLE (forensic class #7): the ClamAV gate's resolved
    value + source are logged whenever the pass has work — the SAME
    `safety_reports.photo_screen.clamav_enabled` flag that governs submission and
    item photos (one operator flag, three surfaces, referenced through
    intake.CFG_PHOTO_CLAMAV so they can never drift apart).
    """
    rows = portal_client.get_daily_photos_pending(base_url, bearer, limit=DAILY_PHOTO_LIMIT)
    if not rows:
        return 0
    clamav_enabled, clamav_source = _resolve_item_photo_clamav()
    error_log.log(
        Severity.INFO, SCRIPT_NAME,
        (
            f"daily-photo screen: {len(rows)} pending; clamav_enabled={clamav_enabled} "
            f"(source={clamav_source}, key={intake.CFG_PHOTO_CLAMAV})"
        ),
        error_code="portal_daily_photo_config_resolved",
    )
    flags = _load_daily_photo_flags()
    serviced = 0
    for row in rows:
        try:
            if _screen_one_daily_photo(
                row, base_url=base_url, bearer=bearer, secret=secret,
                clamav_enabled=clamav_enabled, flags=flags,
            ):
                serviced += 1
        except Exception as exc:  # noqa: BLE001 — per-item fence; one bad item never aborts the pass
            # NEVER interpolate photo bytes / photo_json into the log line.
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"daily-photo screening failed for photo_id={row.get('id')!r}: "
                f"{type(exc).__name__}: {exc!r}",
                error_code="portal_daily_photo_item_failed",
            )
    _persist_daily_photo_flags(flags)
    return serviced


if __name__ == "__main__":
    poll_once()
