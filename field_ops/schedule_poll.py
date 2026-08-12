"""Job-schedule import daemon — the Mac half of the ADR-0006 schedule pool (PR-3).

Purpose
-------
The office uploads a project-schedule PDF in the portal; the Cloudflare Worker
(`safety_portal/worker/fieldops_schedules.ts`) bounds-gates it, signs `schedule:v1`
and queues the bytes SEND-FREE in D1 (`job_schedules` + `job_schedule_chunks`,
migration 0066). This launchd daemon (`org.solutionsmith.its.schedule-poll`,
StartInterval 120 s default) drains that pool over its OWN bearer tier and turns each
document into a REVIEWABLE GRID for the validate screen:

    GET /api/fieldops/schedules/internal/pending → per row:
      claim FIRST (crash recovery) → pull chunks → STRICT reassembly →
      schedule:v1 HMAC verify (`shared.portal_hmac.verify_schedule`) + len/sha256
      recompute vs the SIGNED values → §34 screen
      (`po_materials.po_attach_screen.screen_attachment`, reused verbatim) →
      SANDBOXED OCR (`field_ops.schedule_ocr` — Quartz render + rotation ladder +
      Apple Vision, ALL in the killable child) → row reconstruction
      (`field_ops.schedule_geometry`) → semantic parse
      (`field_ops.schedule_parse`) → Box file to <job>/Schedules/ →
      POST rows in ≤200-row pages → POST previews (best-effort) →
      result post LAST (the commit point)

WHY OCR RUNS IN A CHILD PROCESS. 31 of the 32 corpus schedule exports have NO text
layer — the table text is vector glyph outlines, so extraction is a Quartz render +
Apple Vision pass over attacker-influenceable bytes. Running that here would put a
wedged or crashing render one bug away from the daemon's own state, so render AND OCR
both run in the killable, rlimited `estimate_sandbox` child (spike-proven 2026-08-11 —
ADR-0006 decision 4; this lane carries no ADR-0004 §Vision in-process deviation) and a
failure DEGRADES the schedule to `refused` rather than killing the cycle. Reusing the
estimate lane's sandbox rather than cloning one is Op Stds §14 preservation-over-
refactor; the cross-package import is deliberate (ADR-0006 decision 1).

WHY THIS PRODUCES A GRID, NOT TASKS. See ADR-0006 decision 2: the parser PROPOSES and
the human DISPOSES. Apple Vision misreads digits at confidence 1.00 ('12/01/25' →
'72/01/25' observed on the real corpus), so confidence is NOT a filter and the validate
screen's side-by-side page preview is the ONLY fidelity control. Nothing here reaches
the living task list — that is the PR-4 commit, a human action.

Invariants
----------
- GENERATION-side of the External Send Gate (FM Invariant 1): AI-FREE and customer-SEND-
  FREE — no `graph_client` / `send_mail` / `resend` / `smtplib` / `email.mime` / any
  `anthropic*` / `ollama_client` (NO cloud OR local LLM anywhere in this lane, ADR-0006
  decision 3 — Apple Vision OCR is a local recognizer, not a language model). Enrolled in
  tests/test_capability_gating.py GATED_SCRIPTS. All egress rides the F02-allowlisted
  `shared.portal_client` + `shared.box_client`; this module imports no raw network library.
- Invariant 2: a /pending row is UNTRUSTED until its `schedule:v1` HMAC verifies AND the
  reassembled bytes match the SIGNED length and sha256 — two separate checks, because the
  HMAC covers only the CLAIMS about the bytes. §34 screening precedes every render, and
  every hostile-byte decode (render + OCR) runs in the sandbox child.
- Bearer privilege separation: the Keychain `ITS_PORTAL_SCHEDULE_TOKEN` scopes ONLY
  /api/fieldops/schedules/internal/* (ADR-0006 decision 5 — this process decodes hostile
  PDF bytes, so a compromise of it must reach nothing else).
- Kill-switch first (`@require_active`) + `@its_error_log`; observable config resolution
  (`REQUIRED_CONFIG` + `resolve_and_log`, #336).

Failure modes
-------------
- PAUSED / MAINTENANCE → `@require_active` exits cleanly. Gate false → pure no-op (no
  heartbeat, no marker, no log spam — a dark ship is an intentional state, not an anomaly).
- Missing base URL / bearer / HMAC secret → FAIL-CLOSED: CRITICAL + ERROR heartbeat, and
  deliberately NO watchdog marker so a sustained outage still trips the Check-C floor.
- 401 anywhere → the SAME bearer fails every route, so the cycle STOPS after one CRITICAL.
- Per-row fences. PERMANENT (bad HMAC / digest / chunk set; a §34 MALICIOUS verdict; an
  unreadable document) → Review-Queue row + one-shot flag in
  `state/schedule_poll_flagged.json`, no retry. A §34 SUSPICIOUS verdict PROCEEDS with a
  visible warning (the manifest-lane 2026-08-11 posture — see `_service_one_schedule`).
  TRANSIENT (`BoxError` / `PortalTransportError`) → ERROR-logged, the row stays CLAIMED
  and serviceable, next cycle retries.
- An unresolved Box root is a CONFIG gap, not a per-row defect: ERROR, row left claimed
  and UNFLAGGED so it self-heals the moment the root is configured.

Consumers
---------
- launchd `org.solutionsmith.its.schedule-poll` (StartInterval; RunAtLoad).
- Watchdog Check C marker (`schedule_poll`) + ITS_Daemon_Health row (shared.heartbeat).
- §43 runbook: docs/runbooks/schedule_import_path.md.
"""
from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from field_ops import schedule_geometry, schedule_ocr, schedule_parse
from po_materials import estimate_sandbox, po_attach_screen
from safety_reports import safety_naming
from shared import (
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
from shared.creds_resolution import TransientUnavailable
from shared.error_log import Severity, its_error_log
from shared.heartbeat import HeartbeatReporter, HeartbeatStatus
from shared.kill_switch import require_active
from shared.required_config import ConfigKey, resolve_and_log

SCRIPT_NAME = "field_ops.schedule_poll"
WORKSTREAM = "field_ops"

# ITS_Config keys. The Worker base URL and the Box mirror-tree root are OWNED by
# safety_reports and read cross-workstream — `get_setting` matches on BOTH the Setting
# name and the Workstream cell, so the owning workstream must be passed explicitly.
CFG_POLLING_ENABLED = "field_ops.schedule_poll.polling_enabled"
CFG_WORKER_BASE_URL = "safety_reports.portal.worker_base_url"  # shared with portal_poll
CFG_WORKER_BASE_URL_WORKSTREAM = "safety_reports"
# The §34 screener's ClamAV posture is SHARED with the PO/estimate/manifest document
# pools — reused, never re-declared, so one scanner posture spans every document lane.
CFG_ATTACH_CLAMAV = "po_materials.po_attach_screen.clamav_enabled"
CFG_ATTACH_CLAMAV_WORKSTREAM = "po_materials"

KC_SCHEDULE_TOKEN = "ITS_PORTAL_SCHEDULE_TOKEN"  # noqa: S105 — Keychain entry NAME, not a secret
KC_HMAC_SECRET = "ITS_PORTAL_HMAC_SECRET"  # noqa: S105 — Keychain entry NAME, not a secret

DEFAULT_POLLING_ENABLED = False  # ships dark; the operator flips the seeded row
POLL_INTERVAL_SECONDS = 120  # registration metadata; mirrors the launchd StartInterval

MIME_PDF = "application/pdf"  # the Worker's allowlist is PDF-ONLY (ADR-0006 decision 1)

# Box filing path leaves under ROOT → <job> → 'Schedules' — the schedule PDF files
# BESIDE the job's other artifacts, not under Materials (ADR-0006 decision 11).
SCHEDULES_BOX_SUBFOLDER = "Schedules"

# Wire bounds, mirroring the Worker's own caps so a page can never be refused for size.
ROWS_PER_POST = 200
# Preview pages rendered per document (schedule_ocr.DEFAULT_MAX_PAGES — corpus schedules
# are 2–4 pages; 6 leaves headroom and stays under the Worker's MAX_PREVIEW_PAGES=12).
PREVIEW_MAX_PAGES = 6

REQUIRED_CONFIG: list[ConfigKey] = [
    ConfigKey(CFG_POLLING_ENABLED, WORKSTREAM, DEFAULT_POLLING_ENABLED, "bool"),
    ConfigKey(
        CFG_WORKER_BASE_URL,
        CFG_WORKER_BASE_URL_WORKSTREAM,
        "",
        "str",
        description="Shared Worker base URL; owned by safety_reports, read here too.",
    ),
    ConfigKey(
        safety_naming.CFG_BOX_PORTAL_ROOT,
        CFG_WORKER_BASE_URL_WORKSTREAM,
        "",
        "str",
        description=(
            "Shared Box mirror-tree root; owned by safety_reports. Unset means the "
            "filing folder cannot resolve and rows stay claimed until it is configured."
        ),
    ),
    ConfigKey(
        CFG_ATTACH_CLAMAV,
        CFG_ATTACH_CLAMAV_WORKSTREAM,
        False,
        "bool",
        description=(
            "Shared §34 ClamAV posture; owned by po_materials. One scanner setting "
            "spans every document pool (PO attachments, estimates, manifests, "
            "schedules)."
        ),
    ),
]

STATE_DIR = Path.home() / "its" / "state"
HEARTBEAT_PATH = STATE_DIR / "schedule_poll_heartbeat.txt"
LOCK_PATH = STATE_DIR / "schedule_poll.lock"

# Consecutive pending-fetch failures. A 21-hour every-cycle ERROR storm was once
# invisible on every CRITICAL-keyed surface; this escalates on the shared capped ladder.
_FETCH_FAILS = sustained_failure.SustainedFailureCounter(
    STATE_DIR / "schedule_pending_fetch_failures.json",
    SCRIPT_NAME,
    "schedule_pending_fetch_counter_failed",
)

# ARCH-2: SHARED across every HeartbeatReporter consumer (keyed by daemon name) — passed
# explicitly so the shared-file contract is visible at the call site.
HEARTBEAT_ROW_STATE_PATH = STATE_DIR / "heartbeat_row_ids.json"

# One-shot flags `{schedule_id: reason}` for PERMANENTLY-refused rows — suppresses a
# per-cycle Review-Queue / CRITICAL storm. The operator remediates by deleting an entry.
FLAGGED_PATH = STATE_DIR / "schedule_poll_flagged.json"
MAX_FLAGS = 500

# Mirrors of the Worker's grid bounds (fieldops_schedules.ts MAX_ROW_CELLS /
# MAX_CELL_CHARS / MAX_ROWS_TOTAL — a schedule grid is ~10 concepts wide and a 300-task
# utility schedule fits the 2 000-row ceiling 6×, so these are far tighter than the
# manifest lane's BOM bounds). The parse clamps to these BEFORE posting — visibly, with a
# parse note per clamp — because a grid over the Worker's bounds draws a permanent 400
# that the transient classifier would retry every cycle forever (the manifest lane's
# audit-A5 wedge, inherited as a control here). A clamped-and-noted grid reaches the
# validate screen where a human can SEE the truncation; a wedged one reaches nobody.
WORKER_MAX_ROW_CELLS = 40
WORKER_MAX_CELL_CHARS = 2_000
WORKER_MAX_ROWS_TOTAL = 2_000

DAEMON_NAME = "field_ops.schedule_poll"
_REGISTRATION_SOURCE_ID = "Safety Portal Worker /api/fieldops/schedules/internal/pending"

_heartbeat_reporter = HeartbeatReporter(
    script_name=SCRIPT_NAME,
    daemon_name=DAEMON_NAME,
    workstream=WORKSTREAM,
    liveness_path=HEARTBEAT_PATH,
    interval_seconds=POLL_INTERVAL_SECONDS,
    source_id=_REGISTRATION_SOURCE_ID,
    row_state_path=HEARTBEAT_ROW_STATE_PATH,  # shared file — make the contract explicit
)

WATCHDOG_MARKER_DIR = Path.home() / "its" / ".watchdog"
WATCHDOG_JOB_SLUG = "schedule_poll"


@dataclass(frozen=True)
class SchedulePollStats:
    """One cycle's outcome. Every field is what an operator reading the log needs."""

    skipped_disabled: bool = False
    skipped_locked: bool = False
    halted_no_creds: bool = False
    # Base URL temporarily unreadable (Smartsheet blip / circuit OPEN) — transient, and
    # deliberately distinct from halted_no_creds, which is a real misconfig that pages.
    halted_transient: bool = False
    bearer_rejected: bool = False
    scanned: int = 0
    filed: int = 0  # schedules parsed, filed to Box, rows posted, result posted
    refused: int = 0  # §34 / unreadable-document refusals posted
    integrity_failures: int = 0  # bad HMAC / digest / chunk set (NO result post)
    skipped_flagged: int = 0  # rows already one-shot-flagged in a prior cycle
    rows_posted: int = 0
    previews_posted: int = 0
    errors: int = 0  # transient failures (row stays serviceable)


class _BearerRejectedError(Exception):
    """Internal: a 401 anywhere — the SAME bearer fails every schedule route, so the
    cycle stops rather than burning the whole pool against a dead token."""


@dataclass(frozen=True)
class _ScheduleCreds:
    """Resolved credentials with NAMED fields (the portal_poll CodeQL taint rationale:
    named fields keep the bearer/secret taint off base_url and everything logged)."""

    base_url: str
    bearer: str
    secret: str


# ---- Config readers (deliberate per-daemon replication, Op Stds §14) ---------------


def _read_str_setting(key: str, fallback: str, workstream: str | None = None) -> str:
    try:
        raw = smartsheet_client.get_setting(
            key, workstream=workstream if workstream is not None else WORKSTREAM
        )
    except smartsheet_client.SmartsheetNotFoundError:
        return fallback
    except smartsheet_client.SmartsheetCircuitOpenError:
        return fallback
    return raw if isinstance(raw, str) and raw else fallback


def _read_bool_setting(key: str, fallback: bool, workstream: str | None = None) -> bool:
    raw = _read_str_setting(key, str(fallback).lower(), workstream=workstream)
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _polling_enabled() -> bool:
    return _read_bool_setting(CFG_POLLING_ENABLED, DEFAULT_POLLING_ENABLED)


def _attach_clamav_enabled() -> bool:
    return _read_bool_setting(
        CFG_ATTACH_CLAMAV, False, workstream=CFG_ATTACH_CLAMAV_WORKSTREAM
    )


# ---- Lock / heartbeat / marker seams ----------------------------------------------


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
    """Liveness file touch — thin delegator to the shared HeartbeatReporter (the
    canonical test mock seam; see shared/heartbeat.py §42)."""
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
    HeartbeatReporter (the canonical test mock seam)."""
    _heartbeat_reporter.write_row(
        status=status,
        items_processed=items_processed,
        error_summary=error_summary,
        correlation_id=correlation_id,
        notes=notes,
    )


def _write_watchdog_marker() -> None:
    """Touch the Check C freshness marker for this run. NOT under ~/its/state/, which is
    why a direct write_text is legitimate here."""
    try:
        WATCHDOG_MARKER_DIR.mkdir(parents=True, exist_ok=True)
        marker = WATCHDOG_MARKER_DIR / f"{WATCHDOG_JOB_SLUG}.last_run"
        marker.write_text(datetime.now(UTC).isoformat())
    except OSError as exc:
        error_log.log(
            Severity.WARN,
            SCRIPT_NAME,
            f"watchdog marker write failed: {exc!r}",
            error_code="watchdog_marker_failed",
        )


# ---- One-shot flag state ----------------------------------------------------------


def _load_flags() -> dict[str, str]:
    """Load the one-shot flag set `{schedule_id: reason}`. {} on any read error
    (fail-open: the only cost is one redundant re-flag, never a missed alert)."""
    if not FLAGGED_PATH.exists():
        return {}
    try:
        parsed = json.loads(FLAGGED_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _persist_flags(flags: dict[str, str]) -> None:
    """Atomically persist the flag set (capped). Lock-timeout fails OPEN with a WARN — a
    lost flag costs a duplicate Review-Queue row next cycle, never a missed one."""
    if len(flags) > MAX_FLAGS:
        flags = dict(list(flags.items())[-MAX_FLAGS:])
    FLAGGED_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with state_io.with_path_lock(FLAGGED_PATH):
            state_io.atomic_write_json(FLAGGED_PATH, flags)
    except state_io.StateLockTimeoutError:
        error_log.log(
            Severity.WARN,
            SCRIPT_NAME,
            f"could not acquire lock on {FLAGGED_PATH} after retries; "
            f"schedule flag set not persisted",
            error_code="schedule_flags_persist_failed",
        )


# ---- Credential resolution (fail-CLOSED) ------------------------------------------


def _resolve_credentials() -> _ScheduleCreds | TransientUnavailable | None:
    """Three-way: creds / transient-unreadable / genuinely missing.

    The base URL MUST go through `creds_resolution.read_base_url`, never the plain
    string reader — that helper collapses a circuit-open blip into its "" fallback,
    which is indistinguishable from an unset row, and that exact confusion fired a
    false credentials CRITICAL on po_poll live at 2026-07-20 04:42Z.
    """
    resolved = creds_resolution.read_base_url(
        CFG_WORKER_BASE_URL, CFG_WORKER_BASE_URL_WORKSTREAM
    )
    if isinstance(resolved, TransientUnavailable):
        return resolved
    base_url = resolved or ""
    try:
        bearer = keychain.get_secret(KC_SCHEDULE_TOKEN)
    except keychain.KeychainError:
        bearer = ""
    try:
        secret = keychain.get_secret(KC_HMAC_SECRET)
    except keychain.KeychainError:
        secret = ""
    if not (base_url and bearer and secret):
        return None
    return _ScheduleCreds(base_url=base_url, bearer=bearer, secret=secret)


# ---- STRICT chunk reassembly ------------------------------------------------------


def _reassemble_chunks(chunks: list[dict[str, Any]]) -> bytes:
    """Rebuild the document from its chunk rows, refusing ANY malformation.

    The chunk set was written atomically with its parent row in one `db.batch`, so a
    broken set is tamper or a serving defect — never a benign partial. Every rejection
    raises ValueError and the caller converts that to an INTEGRITY failure: no result
    post, bytes left in D1 for forensics. Out-of-order is NOT an error; it is fixed by
    the sort before concatenation.
    """
    if not chunks:
        raise ValueError("empty chunk set")
    totals = {c.get("chunk_total") for c in chunks}
    if len(totals) != 1:
        raise ValueError("inconsistent chunk_total")
    (total,) = totals
    if not isinstance(total, int) or isinstance(total, bool) or total < 1:
        raise ValueError("malformed chunk_total")
    indices: list[int] = []
    for c in chunks:
        idx = c.get("chunk_index")
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise ValueError("malformed chunk_index")
        indices.append(idx)
    # Catches gaps AND duplicates (a dup shifts the sorted list), plus a length check.
    if sorted(indices) != list(range(total)) or len(chunks) != total:
        raise ValueError("chunk index set not gap-free")
    by_index = sorted(chunks, key=lambda c: int(c["chunk_index"]))
    parts: list[bytes] = []
    for chunk in by_index:
        b64 = chunk.get("chunk_b64")
        if not isinstance(b64, str) or not b64:
            raise ValueError("malformed chunk_b64")
        try:
            parts.append(base64.b64decode(b64, validate=True))
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"chunk_b64 decode failed: {exc}") from exc
    return b"".join(parts)


# ---- Public API -------------------------------------------------------------------


@its_error_log(SCRIPT_NAME)
@require_active
def poll_once() -> SchedulePollStats:
    """One drain cycle. Decorator order is load-bearing: @its_error_log OUTERMOST so a
    PAUSED system still logs its started/completed pair."""
    resolve_and_log(SCRIPT_NAME, REQUIRED_CONFIG)

    if not _polling_enabled():
        # Deliberately silent: no heartbeat, no marker, no log line. A dark ship is an
        # intentional state, not an anomaly — and it is exactly why watchdog Check C
        # WARNs until the operator BOTH loads the plist AND flips the gate.
        return SchedulePollStats(skipped_disabled=True)

    with _file_lock(LOCK_PATH) as acquired:
        if not acquired:
            error_log.log(
                Severity.INFO,
                SCRIPT_NAME,
                "another schedule cycle holds the lock; skipping this cycle",
                error_code="schedule_poll_lock_held",
            )
            return SchedulePollStats(skipped_locked=True)
        try:
            return _poll_inside_lock()
        finally:
            sustained_failure.flush_retry_recovery(SCRIPT_NAME)


def _poll_inside_lock() -> SchedulePollStats:
    creds = _resolve_credentials()
    if isinstance(creds, TransientUnavailable):
        error_log.log(
            Severity.WARN,
            SCRIPT_NAME,
            f"worker base URL temporarily unreadable ({creds.reason}) — skipping this "
            f"cycle; rows stay queued",
            error_code="schedule_creds_transient",
        )
        _write_heartbeat()
        _write_heartbeat_row(
            status="WARN",
            items_processed=0,
            error_summary=f"base URL unreadable ({creds.reason}) — transient",
        )
        # NO watchdog marker: a sustained outage must still trip the Check-C floor.
        return SchedulePollStats(halted_transient=True)
    if creds is None:
        error_log.log(
            Severity.CRITICAL,
            SCRIPT_NAME,
            "fail-closed: missing schedule-pool credentials (worker base URL, bearer "
            "or HMAC secret) — NOT polling until fixed",
            error_code="schedule_creds_missing",
        )
        _write_heartbeat()
        _write_heartbeat_row(
            status="ERROR",
            items_processed=0,
            error_summary="fail-closed: schedule credentials missing",
        )
        return SchedulePollStats(halted_no_creds=True)

    counters: dict[str, int] = {
        "scanned": 0,
        "filed": 0,
        "refused": 0,
        "integrity_failures": 0,
        "skipped_flagged": 0,
        "rows_posted": 0,
        "previews_posted": 0,
        "errors": 0,
    }
    bearer_rejected = False
    try:
        _schedule_pass(creds, counters)
    except _BearerRejectedError:
        bearer_rejected = True
        error_log.log(
            Severity.CRITICAL,
            SCRIPT_NAME,
            "schedule bearer UNAUTHORIZED (401) — cycle STOPPED; the same token fails "
            "every schedule route, so rows stay queued until it is rotated",
            error_code="schedule_bearer_rejected",
        )

    _write_heartbeat()
    # `skipped_flagged` counts items ALREADY one-shot-flagged — permanently wedged out
    # of the queue until an operator fixes the cause and clears the entry. Omitting it
    # made Last Cycle Status flip back to OK on the very next cycle, so the daemon read
    # healthy while an item sat fenced indefinitely (2026-08-10, rfq_poll). A standing
    # fence is a standing WARN by design: ITS_Daemon_Health is a visibility surface, not
    # a push surface, so this pages nobody — and it self-clears when the flag is cleared.
    total_flagged = (
        counters["refused"] + counters["integrity_failures"]
        + counters["skipped_flagged"]
    )
    if bearer_rejected:
        cycle_status: HeartbeatStatus = "ERROR"
    elif counters["errors"] > 0:
        cycle_status = "DEGRADED"
    elif total_flagged > 0:
        cycle_status = "WARN"
    else:
        cycle_status = "OK"
    if circuit_breaker.is_open():
        cycle_status = "CIRCUIT_OPEN"
    error_summary = (
        None
        if (counters["errors"] == 0 and total_flagged == 0 and not bearer_rejected)
        else (
            f"errors={counters['errors']} flagged={total_flagged}"
            + (f" standing={counters['skipped_flagged']}"
               if counters["skipped_flagged"] else "")
            + (" bearer_rejected" if bearer_rejected else "")
        )
    )
    try:
        _write_heartbeat_row(
            status=cycle_status,
            items_processed=counters["filed"],  # FILED, not scanned — work COMPLETED
            error_summary=error_summary,
        )
    except Exception as exc:  # noqa: BLE001 — a heartbeat must never block primary work
        error_log.log(
            Severity.WARN,
            SCRIPT_NAME,
            f"heartbeat write outer-catch tripped: {exc!r}",
            error_code="daemon_health_write_failed",
        )
    _write_watchdog_marker()
    error_log.log(
        Severity.INFO,
        SCRIPT_NAME,
        f"schedule cycle: scanned={counters['scanned']} filed={counters['filed']} "
        f"refused={counters['refused']} integrity={counters['integrity_failures']} "
        f"rows={counters['rows_posted']} previews={counters['previews_posted']} "
        f"skipped_flagged={counters['skipped_flagged']} errors={counters['errors']}",
        error_code="schedule_cycle_summary",
    )
    # Built field-by-field rather than splatting `counters`: the dataclass leads with
    # bool flags, so a **splat is both a type error and a silent reordering hazard.
    return SchedulePollStats(
        bearer_rejected=bearer_rejected,
        scanned=counters["scanned"],
        filed=counters["filed"],
        refused=counters["refused"],
        integrity_failures=counters["integrity_failures"],
        skipped_flagged=counters["skipped_flagged"],
        rows_posted=counters["rows_posted"],
        previews_posted=counters["previews_posted"],
        errors=counters["errors"],
    )


def _schedule_pass(creds: _ScheduleCreds, counters: dict[str, int]) -> None:
    try:
        pending = portal_client.get_schedules_pending(creds.base_url, creds.bearer)
    # PortalAuthError SUBCLASSES PortalTransportError — it MUST be caught first or a 401
    # is absorbed as a transient blip and the bearer CRITICAL never fires.
    except portal_client.PortalAuthError as exc:
        raise _BearerRejectedError from exc
    except portal_client.PortalTransportError as exc:
        counters["errors"] += 1
        n = _FETCH_FAILS.record()
        if sustained_failure.is_escalation_cycle(
            n, sustained_failure.DEFAULT_CRITICAL_THRESHOLD
        ):
            error_log.log(
                Severity.CRITICAL,
                SCRIPT_NAME,
                f"pending fetch failing for {n} consecutive cycles — SUSTAINED schedule "
                f"intake outage; uploads are queueing unserviced: {exc!r}",
                error_code="schedule_pending_fetch_sustained",
            )
        else:
            error_log.log(
                Severity.ERROR,
                SCRIPT_NAME,
                f"failed to GET schedules pending (rows left for next cycle); "
                f"{n} consecutive: {exc!r}",
                error_code="schedule_pending_fetch_failed",
            )
        return
    # Cleared BEFORE the empty early-out, so an empty-but-successful poll still resets.
    _FETCH_FAILS.reset()
    if not pending:
        return

    clamav_enabled = _attach_clamav_enabled()  # ONE read per cycle, threaded down
    flags = _load_flags()
    flags_before = dict(flags)
    try:
        for row in pending:
            counters["scanned"] += 1
            _service_one_schedule(row, creds, counters, flags, clamav_enabled)
    finally:
        # Snapshot-compare in a `finally`, NOT a return-value protocol: a
        # _BearerRejectedError raised out of a helper leaves the loop AFTER the
        # in-flight row earned its flag, and that flag not reaching disk means a
        # fixed-later bearer replays the whole CRITICAL/Review-Queue storm.
        if flags != flags_before:
            _persist_flags(flags)


def _service_one_schedule(
    row: dict[str, Any],
    creds: _ScheduleCreds,
    counters: dict[str, int],
    flags: dict[str, str],
    clamav_enabled: bool,
) -> bool:
    """Service ONE pooled schedule. Returns True when the row reached a terminal
    disposition this cycle, False when it stays serviceable."""
    raw_id = row.get("id")
    if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0:
        counters["errors"] += 1
        error_log.log(
            Severity.ERROR,
            SCRIPT_NAME,
            "pending schedule row has no usable id — skipping",
            error_code="schedule_row_no_id",
        )
        return False
    schedule_id = raw_id
    flag_key = str(schedule_id)
    if flag_key in flags:
        counters["skipped_flagged"] += 1
        return False

    schedule_uuid = str(row.get("schedule_uuid") or "")
    job_id = str(row.get("job_id") or "")
    # The Worker JOINs jobs.project_name into the pending payload (display/foldering
    # metadata, deliberately OUTSIDE the schedule:v1 signature). The Box folder keys
    # off the PROJECT NAME — the same `safety_naming.job_folder_name` folder every
    # other artifact (and the Track 6 archive) resolves — falling back to job_id
    # only when the join found nothing. Keying off the raw job_id grew an id-named
    # folder (JOB-000031) beside the job's real folder in the manifest lane.
    project_name = str(row.get("project_name") or "")
    box_folder_key_source = project_name or job_id
    filename = str(row.get("filename") or "")
    declared_mime = str(row.get("declared_mime") or "")
    uploaded_by = str(row.get("uploaded_by") or "")
    provided_hmac = str(row.get("hmac") or "")
    signed_sha256 = str(row.get("sha256") or "")
    raw_size = row.get("size_bytes")
    # A non-int size fails the signature CLOSED: str(48213.0) is "48213.0" in Python and
    # "48213" in JS, so a float would silently break every canonical.
    size_bytes = (
        raw_size if isinstance(raw_size, int) and not isinstance(raw_size, bool) else -1
    )
    correlation_id = uuid.uuid4().hex[:12]

    try:
        # 1. CLAIM FIRST — a crash after this leaves an observably `claimed` row that
        #    re-serves next cycle, rather than a `pending` one that silently re-services
        #    a hostile document forever.
        portal_client.claim_schedule(
            creds.base_url, creds.bearer, schedule_id=schedule_id
        )

        # 2. Pull + STRICT reassembly.
        chunks = portal_client.get_schedule_chunks(
            creds.base_url, creds.bearer, schedule_id=schedule_id
        )
        try:
            data = _reassemble_chunks(chunks)
        except ValueError as exc:
            counters["integrity_failures"] += 1
            _handle_integrity_failure(
                schedule_id, schedule_uuid, job_id, filename, uploaded_by,
                f"chunk set malformed: {exc}", correlation_id, flags,
            )
            return True

        # 3. VERIFY — signature first, digest second. Never screen unverified bytes.
        if not portal_hmac.verify_schedule(
            creds.secret,
            provided_hmac,
            schedule_uuid=schedule_uuid,
            job_id=job_id,
            filename=filename,
            declared_mime=declared_mime,
            size_bytes=size_bytes,
            sha256=signed_sha256,
        ):
            counters["integrity_failures"] += 1
            _handle_integrity_failure(
                schedule_id, schedule_uuid, job_id, filename, uploaded_by,
                "schedule:v1 HMAC verification failed", correlation_id, flags,
            )
            return True
        # The HMAC covers the CLAIMS about the bytes; this covers the bytes.
        if len(data) != size_bytes or hashlib.sha256(data).hexdigest() != signed_sha256:
            counters["integrity_failures"] += 1
            _handle_integrity_failure(
                schedule_id, schedule_uuid, job_id, filename, uploaded_by,
                "reassembled bytes do not match the signed length/digest",
                correlation_id, flags,
            )
            return True

        # 4. §34 screen — the SAME document screener the PO/estimate/manifest pools use.
        #    MALICIOUS refuses, always. SUSPICIOUS (in practice a PDF OpenAction — an
        #    artifact virtually every vendor/Smartsheet-exported PDF carries) PROCEEDS
        #    WITH A VISIBLE WARNING — the manifest lane's 2026-08-11 operator posture,
        #    inherited: an office-uploaded schedule's bytes are only ever OPENED by ITS
        #    inside the killable sandbox, so a refusal is high-friction protection
        #    against a reader that is already contained. Never silent: WARN here + a
        #    parse note the validate screen displays beside the grid.
        screened = po_attach_screen.screen_attachment(
            filename, declared_mime, data, clamav_enabled=clamav_enabled
        )
        if screened.disposition == "malicious":
            counters["refused"] += 1
            _refuse_malicious(
                schedule_id, schedule_uuid, job_id, filename, uploaded_by,
                screened, correlation_id, flags,
            )
            _post_refused_result(
                creds, schedule_id,
                f"screen:{screened.disposition}:{screened.layer}:{screened.detail}"[:200],
                correlation_id, counters,
            )
            return True
        screen_warning: str | None = None
        if screened.disposition == "suspicious":
            screen_warning = (
                f"ACTIVE CONTENT detected ({screened.layer}:{screened.detail}) — imported "
                f"anyway (office upload; ITS parses it only inside a sandbox). Take care "
                f"opening the ORIGINAL from Box outside a viewer you trust."
            )
            error_log.log(
                Severity.WARN,
                SCRIPT_NAME,
                f"schedule {schedule_id} ({filename!r}, job {job_id}) carries active "
                f"content ({screened.layer}:{screened.detail}) — proceeding per the "
                f"2026-08-11 disposition (suspicious=warn+import, malicious=refuse)",
                error_code="schedule_active_content",
                correlation_id=correlation_id,
            )

        # 5. SANDBOXED OCR → geometry → parse. The OCR stage degrades (None) rather
        #    than raise; geometry + parse are pure and never raise on content.
        ocr = schedule_ocr.ocr_schedule_pages(data)
        if ocr is None:
            counters["refused"] += 1
            _refuse_unreadable(
                schedule_id, schedule_uuid, job_id, filename, uploaded_by,
                "the document could not be read (render/OCR failed or timed out)",
                correlation_id, flags,
            )
            _post_refused_result(
                creds, schedule_id, "ocr_failed", correlation_id, counters
            )
            return True
        pages = schedule_geometry.reconstruct(ocr.pages)
        parsed = schedule_parse.parse_schedule(pages)
        parsed = _clamp_to_worker_bounds(parsed)
        if screen_warning is not None:
            # The screen verdict rides the parse notes so the validate screen shows it
            # beside the grid — the operator sees WHY the document was flagged without
            # the upload being blocked.
            parsed = replace(parsed, notes=[*parsed.notes, screen_warning])
        if not parsed.rows:
            # OCR succeeded but reconstruction found no rows at all (a blank export, a
            # cover page). The Worker 400s an EMPTY rows post, and a 0-row "parsed"
            # result would put a blank grid on the validate screen — refuse VISIBLY
            # instead (the manifest lane's no-importable-rows posture).
            counters["refused"] += 1
            _refuse_unreadable(
                schedule_id, schedule_uuid, job_id, filename, uploaded_by,
                "no schedule rows were recognised in the document",
                correlation_id, flags,
            )
            _post_refused_result(
                creds, schedule_id, "no_rows_recognised", correlation_id, counters
            )
            return True

        # 6. File the ORIGINAL bytes to Box. An unresolved root is a CONFIG gap — the row
        #    stays claimed and UNFLAGGED so it self-heals once the root is set.
        folder_id = _resolve_schedules_box_folder(box_folder_key_source)
        if folder_id is None:
            counters["errors"] += 1
            error_log.log(
                Severity.ERROR,
                SCRIPT_NAME,
                f"Box mirror-tree root unset — schedule {schedule_uuid} left claimed "
                f"and will file once the root is configured",
                error_code="schedule_box_root_unresolved",
                correlation_id=correlation_id,
            )
            return False
        # The uuid prefix disambiguates same-named uploads AND turns a crash-retry into
        # a Box VERSION rather than a duplicate file. Byte-identical re-uploads recur BY
        # DESIGN here: re-uploading a superseded revision's exact bytes is the rollback
        # path (ADR-0006 decision 6).
        filed_name = f"{schedule_uuid} - {filename}"
        file_info = box_client.upload_bytes_or_new_version(folder_id, filed_name, data)
        box_file_id = str(file_info["id"])

        # 7. Post the grid in pages, then the previews (best-effort).
        counters["rows_posted"] += _post_rows(creds, schedule_id, parsed)
        counters["previews_posted"] += _post_previews_best_effort(
            creds, schedule_id, data, correlation_id, counters
        )

        # 8. RESULT POST LAST — the commit point. Everything above is idempotent, so a
        #    crash before this re-serves the claimed row harmlessly.
        portal_client.post_schedule_result(
            creds.base_url,
            creds.bearer,
            schedule_id=schedule_id,
            status="parsed",
            box_file_id=box_file_id,
            profile=parsed.profile,
            row_count=len(parsed.rows),
            column_map=dict(parsed.column_map),
            header_meta=dict(parsed.meta),
            parse_notes="\n".join(parsed.notes)[:4000] or None,
        )
        counters["filed"] += 1
        error_log.log(
            Severity.INFO,
            SCRIPT_NAME,
            f"schedule filed: {filename!r} (uuid {schedule_uuid}, job {job_id}) "
            f"profile={parsed.profile} rows={len(parsed.rows)} box={box_file_id}",
            error_code="schedule_filed",
            correlation_id=correlation_id,
        )
        return True

    except _BearerRejectedError:
        raise  # MUST propagate past the generic fence below
    except portal_client.PortalAuthError as exc:
        raise _BearerRejectedError from exc
    except portal_client.PortalTransportError as exc:
        # A Worker 4xx is PERMANENT: the same request re-serves the same rejection every
        # cycle forever, so "stays serviceable" is a wedge, not resilience (the manifest
        # lane's audit-A5 class: ~720 ERROR rows/day, no CRITICAL, no ticket, item never
        # draining). One-shot flag + a Review-Queue ticket; the §43 remediation is the
        # flag-file entry, same as a refusal. 401 never reaches here (PortalAuthError
        # above); 429/503 raise PortalRateLimitError with no status_code and stay
        # transient below.
        status = getattr(exc, "status_code", None)
        if status is not None and 400 <= status < 500:
            counters["errors"] += 1
            review_queue.safe_add(
                script_name=SCRIPT_NAME,
                workstream=WORKSTREAM,
                summary=(
                    f"schedule: Worker PERMANENTLY rejected servicing of schedule "
                    f"{schedule_id} (HTTP {status}) — flagged, will not retry; "
                    f"see the runbook's flag-file remediation"
                ),
                payload={"schedule_id": schedule_id, "http_status": status,
                         "detail": str(exc)[:300]},
                sla_tier=review_queue.SlaTier.RFQ_DRAFT,
                reason=review_queue.ReviewReason.POLICY_EDGE,
                severity=Severity.ERROR,
                source_file=f"schedule:{schedule_id}",
            )
            error_log.log(
                Severity.ERROR,
                SCRIPT_NAME,
                f"Worker rejected schedule {schedule_id} with HTTP {status} — "
                f"PERMANENT, one-shot-flagged (a 4xx re-serves identically forever): "
                f"{exc!r}",
                error_code="schedule_worker_rejected",
                correlation_id=correlation_id,
            )
            flags[str(schedule_id)] = "worker_rejected"
            return True
        counters["errors"] += 1
        error_log.log(
            Severity.ERROR,
            SCRIPT_NAME,
            f"transient failure servicing schedule {schedule_id} (stays serviceable "
            f"for next cycle): {type(exc).__name__}: {exc!r}",
            error_code="schedule_transient",
            correlation_id=correlation_id,
        )
        return False
    except (
        smartsheet_client.SmartsheetError,
        box_client.BoxError,
    ) as exc:
        counters["errors"] += 1
        error_log.log(
            Severity.ERROR,
            SCRIPT_NAME,
            f"transient failure servicing schedule {schedule_id} (stays serviceable "
            f"for next cycle): {type(exc).__name__}: {exc!r}",
            error_code="schedule_transient",
            correlation_id=correlation_id,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — per-row fence; one bad row never kills the cycle
        counters["errors"] += 1
        error_log.log(
            Severity.ERROR,
            SCRIPT_NAME,
            f"unexpected failure servicing schedule {schedule_id}: "
            f"{type(exc).__name__}: {exc!r}",
            error_code="schedule_service_failed",
            correlation_id=correlation_id,
        )
        return False


# ---- Grid clamp (Worker bounds) ----------------------------------------------------


def _clamp_to_worker_bounds(
    parsed: schedule_parse.ParsedSchedule,
) -> schedule_parse.ParsedSchedule:
    """Clamp the parsed grid to the Worker's own row/cell bounds — VISIBLY.

    The Worker 400s a grid over its bounds (row_index > 2 000, > 40 cells/row,
    > 2 000 chars/cell), and a 400 re-serves identically every cycle: without this
    clamp the transient classifier would retry it forever (the manifest lane's audit-A5
    wedge, inherited as a control). Truncating SILENTLY would be worse — inventing a
    smaller document than the office sent — so every clamp appends a parse note the
    validate screen displays beside the grid: the human sees exactly what was cut and
    can ask for a smaller export if the tail mattered."""
    notes: list[str] = []
    rows = parsed.rows
    if len(rows) > WORKER_MAX_ROWS_TOTAL:
        notes.append(
            f"TRUNCATED: the document has {len(rows)} rows; only the first "
            f"{WORKER_MAX_ROWS_TOTAL} are shown/importable (grid ceiling)"
        )
        rows = [r for r in rows[:WORKER_MAX_ROWS_TOTAL] if r.index <= WORKER_MAX_ROWS_TOTAL]
    out: list[schedule_parse.ParsedScheduleRow] = []
    narrowed = 0
    shortened = 0
    for r in rows:
        cells = r.cells
        if len(cells) > WORKER_MAX_ROW_CELLS:
            narrowed += 1
            cells = cells[:WORKER_MAX_ROW_CELLS]
        if any(len(c) > WORKER_MAX_CELL_CHARS for c in cells):
            shortened += sum(1 for c in cells if len(c) > WORKER_MAX_CELL_CHARS)
            cells = [c[:WORKER_MAX_CELL_CHARS] for c in cells]
        out.append(replace(r, cells=cells) if cells is not r.cells else r)
    if narrowed:
        notes.append(
            f"TRUNCATED: {narrowed} row(s) had more than {WORKER_MAX_ROW_CELLS} columns; "
            f"extra columns were dropped"
        )
    if shortened:
        notes.append(
            f"TRUNCATED: {shortened} cell(s) were longer than {WORKER_MAX_CELL_CHARS} "
            f"characters and were shortened"
        )
    if not notes:
        return parsed
    return replace(parsed, rows=out, notes=[*parsed.notes, *notes])


def _post_rows(
    creds: _ScheduleCreds, schedule_id: int, parsed: schedule_parse.ParsedSchedule
) -> int:
    """Post the parsed grid in ≤ROWS_PER_POST pages. The Worker upserts on
    (schedule_id, row_index), so a re-post is a no-op and a crash mid-way simply
    re-posts from the start next cycle."""
    written = 0
    for start in range(0, len(parsed.rows), ROWS_PER_POST):
        page = parsed.rows[start : start + ROWS_PER_POST]
        written += portal_client.post_schedule_rows(
            creds.base_url,
            creds.bearer,
            schedule_id=schedule_id,
            rows=[
                {
                    "row_index": r.index,
                    "source_page": r.source_page or None,
                    "kind": r.kind,
                    "cells": r.cells,
                    "flags": ",".join(r.flags) if r.flags else None,
                }
                for r in page
            ],
        )
    return written


def _post_previews_best_effort(
    creds: _ScheduleCreds,
    schedule_id: int,
    data: bytes,
    correlation_id: str,
    counters: dict[str, int],
) -> int:
    """Render + post source-page previews. BEST-EFFORT by design: the previews are the
    validate screen's fidelity aid — the ONLY fidelity control this lane has (ADR-0006
    decision 2) — but a render failure must never cost the import; a preview-less grid
    still reaches a human who can demand a re-export."""
    raw = estimate_sandbox.run_sandboxed(
        "render_page_pngs",
        data,
        timeout_s=estimate_sandbox.PREVIEW_TIMEOUT_S,
        args=[str(PREVIEW_MAX_PAGES)],
    )
    if raw is None:
        return 0
    try:
        pngs = json.loads(raw)["pngs"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return 0
    if not isinstance(pngs, list):
        return 0
    posted = 0
    for page, png_b64 in enumerate(pngs, start=1):
        if not isinstance(png_b64, str) or not png_b64:
            continue
        try:
            portal_client.post_schedule_preview(
                creds.base_url,
                creds.bearer,
                schedule_id=schedule_id,
                page=page,
                png_b64=png_b64,
            )
            posted += 1
        except portal_client.PortalAuthError as exc:
            raise _BearerRejectedError from exc
        except portal_client.PortalTransportError as exc:
            counters["errors"] += 1
            error_log.log(
                Severity.WARN,
                SCRIPT_NAME,
                f"preview page {page} post failed for schedule {schedule_id} "
                f"(import unaffected): {exc!r}",
                error_code="schedule_preview_post_failed",
                correlation_id=correlation_id,
            )
            break
    return posted


# ---- Refusal / integrity handling -------------------------------------------------


def _handle_integrity_failure(
    schedule_id: int, schedule_uuid: str, job_id: str, filename: str,
    uploaded_by: str, detail: str, correlation_id: str, flags: dict[str, str],
) -> None:
    """A tampered or malformed row. Deliberately NO result post: the bytes stay in D1
    for forensics and the row stays visible in the pool. One-shot flagged so the alert
    fires once, not every interval."""
    review_queue.safe_add(
        script_name=SCRIPT_NAME,
        workstream=WORKSTREAM,
        summary=(
            f"schedule: INTEGRITY FAILURE on {filename!r} (uuid {schedule_uuid}, job "
            f"{job_id}, uploaded by {uploaded_by!r}) — {detail}; NOT parsed, NOT filed"
        ),
        payload={
            "schedule_id": schedule_id,
            "schedule_uuid": schedule_uuid,
            "job_id": job_id,
            "filename": filename,
            "uploaded_by": uploaded_by,
            "detail": detail,
        },
        sla_tier=review_queue.SlaTier.RFQ_DRAFT,
        reason=review_queue.ReviewReason.SECURITY_TRIGGER,
        severity=Severity.CRITICAL,
        source_file=f"schedule:{schedule_id}",
        security_flag=True,
    )
    error_log.log(
        Severity.CRITICAL,
        SCRIPT_NAME,
        f"schedule INTEGRITY failure (schedule {schedule_id}, uuid {schedule_uuid}, "
        f"account {uploaded_by!r}): {detail} — bytes retained in D1 for forensics",
        error_code="schedule_integrity_failed",
        correlation_id=correlation_id,
    )
    flags[str(schedule_id)] = "integrity"


def _refuse_malicious(
    schedule_id: int, schedule_uuid: str, job_id: str, filename: str,
    uploaded_by: str, result: po_attach_screen.ScreenResult,
    correlation_id: str, flags: dict[str, str],
) -> None:
    """Route a §34-MALICIOUS schedule to the Review Queue, one-shot flagged, with a
    CRITICAL NAMING THE ACCOUNT (the photo_screen / intake posture). Malicious-ONLY on
    purpose — this lane was born after the 2026-08-11 suspicious=warn+import decision,
    so unlike `manifest_poll._refuse_screened` it carries no vestigial suspicious
    branch; the suspicious path lives inline in `_service_one_schedule`.

    The flag is set BEFORE the result post-back: the alert has already fired, so if the
    post fails transiently the flag IS the dedupe against a per-interval re-fire storm.
    """
    detail = f"{result.layer}:{result.detail}"
    review_queue.safe_add(
        script_name=SCRIPT_NAME,
        workstream=WORKSTREAM,
        summary=(
            f"schedule: MALICIOUS upload {filename!r} (uuid {schedule_uuid}, job "
            f"{job_id}, uploaded by {uploaded_by!r}) — refused before filing ({detail})"
        ),
        payload={
            "schedule_id": schedule_id, "schedule_uuid": schedule_uuid,
            "job_id": job_id, "filename": filename,
            "uploaded_by": uploaded_by, "detail": detail,
        },
        sla_tier=review_queue.SlaTier.RFQ_DRAFT,
        reason=review_queue.ReviewReason.SECURITY_TRIGGER,
        severity=Severity.CRITICAL,
        source_file=f"schedule:{schedule_id}",
        security_flag=True,
    )
    error_log.log(
        Severity.CRITICAL,
        SCRIPT_NAME,
        f"MALICIOUS schedule refused (schedule {schedule_id}, uuid {schedule_uuid}, "
        f"account {uploaded_by!r}): {detail} — review the account before re-enabling "
        f"uploads",
        error_code="schedule_malicious",
        correlation_id=correlation_id,
    )
    flags[str(schedule_id)] = "refused"


def _refuse_unreadable(
    schedule_id: int, schedule_uuid: str, job_id: str, filename: str,
    uploaded_by: str, detail: str, correlation_id: str, flags: dict[str, str],
) -> None:
    """A CLEAN document the pipeline could not turn into a grid — a wedged render, an
    OCR pass that found nothing, a cover-page-only export. Not a security event: an
    ordinary review item telling the office to send a different file."""
    review_queue.safe_add(
        script_name=SCRIPT_NAME,
        workstream=WORKSTREAM,
        summary=(
            f"schedule: {filename!r} (uuid {schedule_uuid}, job {job_id}) could not be "
            f"imported — {detail}. Ask the office for a fresh Smartsheet PDF export of "
            f"the schedule."
        ),
        payload={
            "schedule_id": schedule_id, "schedule_uuid": schedule_uuid,
            "job_id": job_id, "filename": filename,
            "uploaded_by": uploaded_by, "detail": detail,
        },
        sla_tier=review_queue.SlaTier.RFQ_DRAFT,
        reason=review_queue.ReviewReason.POLICY_EDGE,
        severity=Severity.WARN,
        source_file=f"schedule:{schedule_id}",
    )
    error_log.log(
        Severity.WARN,
        SCRIPT_NAME,
        f"schedule unreadable (schedule {schedule_id}, uuid {schedule_uuid}): {detail}",
        error_code="schedule_unreadable",
        correlation_id=correlation_id,
    )
    flags[str(schedule_id)] = "refused"


def _post_refused_result(
    creds: _ScheduleCreds,
    schedule_id: int,
    detail: str,
    correlation_id: str,
    counters: dict[str, int],
) -> None:
    """Post the refusal, locally fenced. The ERROR names the exact remediation, which is
    the §43 contract: an operator reading it knows precisely what to clear to retry."""
    try:
        portal_client.post_schedule_result(
            creds.base_url,
            creds.bearer,
            schedule_id=schedule_id,
            status="refused",
            detail=detail,
        )
    except portal_client.PortalAuthError as exc:
        raise _BearerRejectedError from exc
    except portal_client.PortalTransportError as exc:
        counters["errors"] += 1
        error_log.log(
            Severity.ERROR,
            SCRIPT_NAME,
            f"refused-disposition post failed for schedule {schedule_id}; clear "
            f"'{schedule_id}' from {FLAGGED_PATH.name} to retry after the transport "
            f"recovers: {exc!r}",
            error_code="schedule_result_post_failed",
            correlation_id=correlation_id,
        )


# ---- Box folder resolution --------------------------------------------------------


def _resolve_schedules_box_folder(job_folder_key_source: str) -> str | None:
    """§45 find-or-create the schedule filing folder: mirror-tree ROOT → per-job folder
    (the SAME `safety_naming.job_folder_name` as every other portal artifact) →
    'Schedules'. One level shallower than the manifest lane's Materials/Manifests
    nesting — the schedule files BESIDE the job's other artifacts (ADR-0006 decision
    11). None when the shared root is unconfigured (the caller leaves the row claimed +
    ERRORs — a config gap, not a per-row defect).

    `job_folder_key_source` MUST be the job's PROJECT NAME whenever one is known
    (the Worker joins it into the pending payload); the caller falls back to job_id
    only when the name is unavailable."""
    root = _read_str_setting(
        safety_naming.CFG_BOX_PORTAL_ROOT, "",
        workstream=CFG_WORKER_BASE_URL_WORKSTREAM,
    ).strip()
    if not root:
        return None
    job_folder = box_client.get_or_create_folder(
        root, safety_naming.job_folder_name(job_folder_key_source)
    )
    return box_client.get_or_create_folder(job_folder, SCHEDULES_BOX_SUBFOLDER)


if __name__ == "__main__":
    poll_once()
