"""Purchase-Order pull daemon — the ONE multi-pass Mac half of the PO pipeline (S4).

Purpose
-------
The Worker (safety_portal/worker/po.ts) validates/computes/signs/queues each generated
PO SEND-FREE in D1; this launchd daemon (90s, `org.solutionsmith.its.po-poll`) is the
Mac-side consumer — the `fieldops_sync` multi-pass model (one host, one lock, one
heartbeat; per-pass ITS_Config gates, ALL shipped false):

  ① **Drafts pass** (`po_materials.po_poll.polling_enabled`): GET
     /api/po/internal/pending → per row: recompute the po:v1 canonical string +
     constant-time HMAC verify (`shared.portal_hmac.verify_po`) → totals recompute +
     assert vs the SIGNED values (`po_generate.totals_mismatches`) → PO_Log collision
     double-check (`numbering.check_collision` — hand-issued POs in transition) →
     ITS_Vendors SoR vendor snapshot (#494) → terms/purchaser resolution →
     DETERMINISTIC render → Box file (§45 find-or-create the PO root's per-job
     folder — po_naming.CFG_BOX_PORTAL_ROOT → <job>, the lane's OWN tree since
     2026-08-11; §47 version-on-conflict) → PO_Log append + PO_Pending_Review row
     (+ inline PDF attach on review, ledger AND per-job rows, best-effort) →
     mark-filed receipt WITH box_file_id. The receipt is
     LAST: a crash anywhere before it re-pulls the row and every prior step is
     idempotent (version-on-conflict upload; PO_Log/review-row dedupe by d1_id /
     po_id). A bad-HMAC or totals-mismatch row is ONE-SHOT-FLAGGED (CRITICAL +
     security Review-Queue row on first sighting, then skipped) — NEVER rendered,
     NEVER filed, NEVER marked; the row stays queued in D1 for forensics (the PO
     internal tier has no mark-rejected route by design).
  ①b **Attachment pass** (same `.polling_enabled` gate; Feature B): after the drafts
     drain, GET /api/po/internal/attachments/pending → per attachment: claim-first →
     pull chunks → REASSEMBLE + verify (po-att:v1 HMAC + sha256 over the bytes —
     `shared/portal_hmac.verify_po_attachment`) → §34 doc screen
     (`po_attach_screen.screen_attachment`: magic/consistency → PDF/OpenXML/image
     structural → config-gated ClamAV `po_materials.po_attach_screen.clamav_enabled`)
     → CLEAN files the ORIGINAL bytes to the PO's Box per-job folder (beside its
     PO PDF) + the PO_Log row (content-typed attach) + result post-back (the
     Worker deletes the D1 chunks); SUSPICIOUS → Review-Queue row (+security flag on structural
     active content) + refused; MALICIOUS → CRITICAL NAMING THE ACCOUNT +
     security-flagged Review-Queue row + refused. Integrity failures (bad HMAC /
     digest mismatch) are one-shot-flagged like a bad-HMAC PO — never screened,
     never filed, bytes left in D1 for forensics. The WHOLE pass is fenced
     (`po_attachment_service_failed`) — it can never block PO filing.
  ② **Vendor down-sync pass** (`.vendors_sync_enabled`): full ITS_Vendors projection
     → POST vendors/sync (full-replace; the Worker's dirty-row fence protects
     un-mirrored portal edits; an EMPTY projection is REFUSED here too — a read-miss
     must never wipe the cache).
  ③ **Vendor up-sync pass** (same gate): GET vendors/pending → per vendor: bridge-key
     find-or-create into ITS_Vendors (`vendors.upsert_vendor`, column-scoped
     non-clobber) → mark-mirrored with the READ watermark (the Worker's in-WHERE
     version guard makes a racing portal edit win).
  ④ **Status pass** (`.status_sync_enabled`): read PO_Pending_Review approve/SENT
     state → POST status-sync (approved BEFORE sent per PO — the Worker's guarded
     batch walks the machine in order) → mirror the stamps into PO_Log (sent + Sent
     At; the superseded flip onto the predecessor row, resolved via the Notes d1_id
     join). D1 status is a display cache; F22 approval verification stays with the
     S5 send poller — this pass reports, it does not authorize.

Invariants
----------
- GENERATION-side of the External Send Gate (FM Invariant 1): AI-FREE and
  customer-SEND-FREE — no `anthropic*`, no `graph_client`/`send_mail`/`resend`/
  `smtplib`/`email.mime` (enrolled in tests/test_capability_gating.py GATED_SCRIPTS).
  All egress rides the F02-allowlisted `shared.portal_client` (our Worker) +
  `shared.box_client` (filing) + `shared.smartsheet_client` (SoR/ledger writes).
- Invariant 2: a /pending row is UNTRUSTED until its HMAC verifies; the money on the
  legal document is re-derived and asserted, never taken on faith; the vendor
  identity embedded in the PDF comes from the Smartsheet SoR, never the D1 cache.
- Kill-switch first (`@require_active`) + `@its_error_log`; observable config
  resolution (`REQUIRED_CONFIG` + `resolve_and_log`, #336); bearer privilege
  separation (`ITS_PORTAL_PO_TOKEN` — the Worker's `requirePoToken` tier accepts no
  sibling token).

Failure modes
-------------
- PAUSED/MAINTENANCE → `@require_active` exits cleanly. ALL gates false (the shipped
  default) → pure no-op (no per-cycle log spam; the seeded ITS_Config rows are the
  operator's switches — scripts/migrations/seed_po_materials_config.py).
- Missing base URL / bearer / HMAC secret → FAIL-CLOSED: no pass runs; CRITICAL
  (won't self-heal) + ERROR heartbeat.
- 401 anywhere → the SAME bearer fails every PO route, so the cycle STOPS: CRITICAL
  (`po_bearer_rejected`) + ERROR heartbeat; everything stays queued/dirty.
- Per-item fences: PERMANENT (bad HMAC / totals mismatch / collision / unknown
  vendor / TermsError / picklist / HTTP-400 validation) → Review-Queue row +
  one-shot flag (state `po_poll_flagged.json` — delete an entry to retry after
  fixing the cause); TRANSIENT (SmartsheetError / BoxError / PortalTransportError)
  → ERROR-logged, row left queued/dirty, next cycle retries. One bad row never
  kills the cycle.

Consumers
---------
- launchd `org.solutionsmith.its.po-poll` (StartInterval 90s default; RunAtLoad).
- Watchdog Check C marker (`po_poll`) + ITS_Daemon_Health row (shared.heartbeat).
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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from po_materials import (
    numbering,
    po_attach_screen,
    po_generate,
    po_log,
    po_naming,
    po_review,
    vendors,
)
from po_materials import terms as terms_lib
from safety_reports import safety_naming
from shared import (
    anomaly_logger,
    box_client,
    circuit_breaker,
    creds_resolution,
    error_log,
    job_sheet,
    keychain,
    picklist_validation,
    portal_client,
    portal_hmac,
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

SCRIPT_NAME = "po_materials.po_poll"
WORKSTREAM = "po_materials"

# ITS_Config keys (all read under Workstream='po_materials' except the SHARED
# safety_reports-owned worker_base_url — same ownership pattern as fieldops_sync).
CFG_POLLING_ENABLED = "po_materials.po_poll.polling_enabled"
CFG_VENDORS_SYNC_ENABLED = "po_materials.po_poll.vendors_sync_enabled"
CFG_STATUS_SYNC_ENABLED = "po_materials.po_poll.status_sync_enabled"
# Feature B — the §34 doc-attachment screener's optional ClamAV layer (default OFF;
# seeded false by scripts/migrations/seed_po_materials_config.py — the dark-gate reflex).
CFG_ATTACH_CLAMAV = "po_materials.po_attach_screen.clamav_enabled"
CFG_WORKER_BASE_URL = "safety_reports.portal.worker_base_url"  # shared with portal_poll
CFG_WORKER_BASE_URL_WORKSTREAM = "safety_reports"

# Keychain entry names (NOT secrets). The PO bearer mirrors the Worker's
# PORTAL_PO_API_TOKEN (privilege-separated from every sibling tier); the HMAC secret
# is the SAME payload secret the Worker signs with (domain separation, not key
# separation, isolates the po:v1 protocol).
KC_PO_TOKEN = "ITS_PORTAL_PO_TOKEN"  # noqa: S105 — Keychain entry NAME, not a secret
KC_HMAC_SECRET = "ITS_PORTAL_HMAC_SECRET"  # noqa: S105 — Keychain entry NAME, not a secret

DEFAULT_POLLING_ENABLED = False       # ships dark; the operator flips the seeded row
DEFAULT_VENDORS_SYNC_ENABLED = False  # ships dark
DEFAULT_STATUS_SYNC_ENABLED = False   # ships dark
POLL_INTERVAL_SECONDS = 90  # registration metadata; mirrors the launchd StartInterval

# The per-job Smartsheet tracking sheet name (Feature A). Lives inside the job's
# folder under sheet_ids.FOLDER_PO_JOBS; structure-cloned from the flat PO_Log by
# shared/job_sheet.ensure_job_sheet. (The Box side needs no matching subfolder any
# more: PO PDFs file DIRECTLY in the PO root's per-job folder —
# po_naming.CFG_BOX_PORTAL_ROOT → <job>.)
PERJOB_SHEET_NAME = "Purchase Orders"

_PACIFIC = ZoneInfo("America/Los_Angeles")  # the PO date is operator wall-clock

# #336 — every ITS_Config key this daemon resolves at RUNTIME. The declared-but-not-
# runtime-read *.poll_interval_seconds key is deliberately EXCLUDED (install.sh bakes
# it into the plist; the daemon never reads it) — same posture as fieldops_sync.
REQUIRED_CONFIG: list[ConfigKey] = [
    ConfigKey(CFG_POLLING_ENABLED, WORKSTREAM, DEFAULT_POLLING_ENABLED, "bool"),
    ConfigKey(CFG_VENDORS_SYNC_ENABLED, WORKSTREAM, DEFAULT_VENDORS_SYNC_ENABLED, "bool"),
    ConfigKey(CFG_STATUS_SYNC_ENABLED, WORKSTREAM, DEFAULT_STATUS_SYNC_ENABLED, "bool"),
    ConfigKey(
        CFG_ATTACH_CLAMAV, WORKSTREAM, False, "bool",
        description=(
            "Optional ClamAV layer of the §34 PO document-attachment screener "
            "(po_attach_screen L3). Default OFF; requires clamd + pyclamd on the Mac."
        ),
    ),
    ConfigKey(
        CFG_WORKER_BASE_URL, CFG_WORKER_BASE_URL_WORKSTREAM, "", "str",
        description="Shared Worker base URL; owned by safety_reports, read here too.",
    ),
    ConfigKey(
        po_naming.CFG_BOX_PORTAL_ROOT, po_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM, "", "str",
        description=(
            "The PO lane's OWN Box mirror-tree root ('ITS Purchase Orders' — built by "
            "build_box_roots.py). The drafts pass files PO PDFs under ROOT→<job>; "
            "rfq_poll/estimate_poll file into its 'RFQs' / 'Vendor Quotes' children."
        ),
    ),
]

# State paths. HEARTBEAT_ROW_STATE_PATH is the SHARED row-id cache (ARCH-2).
STATE_DIR = Path.home() / "its" / "state"
HEARTBEAT_PATH = STATE_DIR / "po_poll_heartbeat.txt"
LOCK_PATH = STATE_DIR / "po_poll.lock"
# Sustained pending-fetch escalation (2026-07-20 forensic: a 21h every-cycle ERROR storm
# was invisible on every CRITICAL-keyed fire surface — shared/sustained_failure.py).
_FETCH_FAILS = sustained_failure.SustainedFailureCounter(
    STATE_DIR / "po_pending_fetch_failures.json", SCRIPT_NAME, "po_pending_fetch_counter_failed",
)
HEARTBEAT_ROW_STATE_PATH = STATE_DIR / "heartbeat_row_ids.json"
# One-shot flag state for PERMANENTLY-refused pending rows (`{po_id: reason}`).
# A flagged row is skipped every subsequent cycle (no 90s Review-Queue spam); the
# operator remediates by fixing the cause and deleting the entry (or the file).
PO_FLAGGED_PATH = STATE_DIR / "po_poll_flagged.json"
MAX_PO_FLAGS = 500  # drained/settled entries are dead weight only — cap the file

DAEMON_NAME = "po_materials.po_poll"
_REGISTRATION_SOURCE_ID = "Safety Portal Worker /api/po/internal/pending"

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
WATCHDOG_JOB_SLUG = "po_poll"


@dataclass(frozen=True)
class PoPollStats:
    """Summary of one poll_once() invocation."""

    skipped_disabled: bool = False
    skipped_locked: bool = False
    halted_no_creds: bool = False
    # base URL temporarily unreadable (Smartsheet blip / circuit OPEN) — transient,
    # distinct from halted_no_creds, which is a genuine misconfig that pages.
    halted_transient: bool = False
    bearer_rejected: bool = False
    # ① drafts pass
    drafts_scanned: int = 0
    filed: int = 0        # POs fully filed + receipted this cycle
    rejected: int = 0     # bad-HMAC refusals (first sighting)
    fenced: int = 0       # permanent Review-Queue fences (totals/collision/vendor/terms/…)
    skipped_flagged: int = 0  # rows already one-shot-flagged in a prior cycle
    draft_errors: int = 0     # transient failures (left queued)
    # ①b attachment pass (Feature B — runs after the drafts drain, same gate)
    attachments_filed: int = 0    # clean attachments filed to Box + PO_Log this cycle
    attachments_refused: int = 0  # suspicious/malicious dispositions posted
    attachment_errors: int = 0    # transient failures (row stays serviceable)
    # ②③ vendor passes
    vendors_downsynced: int = 0
    vendors_upsynced: int = 0
    vendors_reviewed: int = 0
    vendor_errors: int = 0
    # ④ status pass
    status_synced: int = 0
    status_errors: int = 0


class _BearerRejectedError(Exception):
    """Internal: a 401 anywhere — the SAME bearer fails every PO route, stop the cycle."""


@dataclass(frozen=True)
class _PoCreds:
    """Resolved credentials with NAMED fields (the portal_poll CodeQL taint rationale:
    named fields keep the bearer/secret taint off base_url and everything logged)."""

    base_url: str
    bearer: str
    secret: str


# ---- Config readers (replicated per preservation, mirror fieldops_sync) --------


def _read_str_setting(key: str, fallback: str, workstream: str | None = None) -> str:
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


def _polling_enabled() -> bool:
    return _read_bool_setting(CFG_POLLING_ENABLED, DEFAULT_POLLING_ENABLED)


def _vendors_sync_enabled() -> bool:
    return _read_bool_setting(CFG_VENDORS_SYNC_ENABLED, DEFAULT_VENDORS_SYNC_ENABLED)


def _status_sync_enabled() -> bool:
    return _read_bool_setting(CFG_STATUS_SYNC_ENABLED, DEFAULT_STATUS_SYNC_ENABLED)


def _attach_clamav_enabled() -> bool:
    """ITS_Config gate `po_materials.po_attach_screen.clamav_enabled` (default OFF)."""
    return _read_bool_setting(CFG_ATTACH_CLAMAV, False)


# ---- Lock + heartbeat + marker seams (mirror fieldops_sync) ---------------------


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
    """Touch the Check C freshness marker for this run (mirror fieldops_sync)."""
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


# ---- One-shot flag state (the item-photo flag pattern, portal_poll) -------------


def _load_flags() -> dict[str, str]:
    """Load the one-shot flag set `{po_id: reason}`. {} on any read error (fail-open:
    the only cost is one redundant re-flag, never a missed alert)."""
    if not PO_FLAGGED_PATH.exists():
        return {}
    try:
        parsed = json.loads(PO_FLAGGED_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _persist_flags(flags: dict[str, str]) -> None:
    """Atomically persist the flag set (capped). Lock-timeout fails OPEN with a WARN —
    a lost flag set costs a duplicate Review-Queue flag next cycle, never a missed one."""
    if len(flags) > MAX_PO_FLAGS:
        flags = dict(list(flags.items())[-MAX_PO_FLAGS:])
    PO_FLAGGED_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with state_io.with_path_lock(PO_FLAGGED_PATH):
            state_io.atomic_write_json(PO_FLAGGED_PATH, flags)
    except state_io.StateLockTimeoutError:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"could not acquire lock on {PO_FLAGGED_PATH} after retries; "
            f"PO flag set not persisted",
            error_code="po_flags_persist_failed",
        )


# ---- Credential resolution (fail-CLOSED) -----------------------------------------


def _resolve_credentials() -> _PoCreds | TransientUnavailable | None:
    """Resolve (base_url, bearer, secret) fail-CLOSED, three ways.

    `TransientUnavailable` means the base-URL row could not be READ this cycle
    (Smartsheet blip / circuit open) — self-heals, so the caller WARNs and skips.
    `None` means a credential is genuinely absent — a misconfig that pages.

    The base-URL read deliberately does NOT go through `_read_str_setting`: that
    helper swallows both circuit-open and transient errors into its fallback, so
    a one-cycle Smartsheet blip arrived here as `""` and was indistinguishable
    from an unset row — which is exactly how a network hiccup produced a CRITICAL
    blaming PO *credentials* that were never missing (live, 2026-07-20 04:42Z).
    """
    resolved = creds_resolution.read_base_url(
        CFG_WORKER_BASE_URL, CFG_WORKER_BASE_URL_WORKSTREAM
    )
    if isinstance(resolved, TransientUnavailable):
        return resolved
    base_url = resolved or ""
    try:
        bearer = keychain.get_secret(KC_PO_TOKEN)
    except keychain.KeychainError:
        bearer = ""
    try:
        secret = keychain.get_secret(KC_HMAC_SECRET)
    except keychain.KeychainError:
        secret = ""
    if not (base_url and bearer and secret):
        return None
    return _PoCreds(base_url=base_url, bearer=bearer, secret=secret)


# ---- Public API -------------------------------------------------------------------


@its_error_log(SCRIPT_NAME)
@require_active
def poll_once() -> PoPollStats:
    """Run one multi-pass PO cycle. launchd invokes this once per StartInterval;
    idempotent across crashes (see the module docstring's receipt-is-last design)."""
    # #336 startup observability (after @require_active, fail-open — never blocks).
    resolve_and_log(SCRIPT_NAME, REQUIRED_CONFIG)

    drafts_on = _polling_enabled()
    vendors_on = _vendors_sync_enabled()
    status_on = _status_sync_enabled()
    if not (drafts_on or vendors_on or status_on):
        # Shipped default (ALL gates false) — an intentional dark state, not an
        # anomaly: no heartbeat/marker/log spam every 90s. The seeded ITS_Config
        # rows are the operator's switches.
        return PoPollStats(skipped_disabled=True)

    with _file_lock(LOCK_PATH) as acquired:
        if not acquired:
            error_log.log(
                Severity.INFO, SCRIPT_NAME,
                "another poll cycle holds the lock; skipping this cycle",
                error_code="po_poll_lock_held",
            )
            return PoPollStats(skipped_locked=True)
        try:
            return _poll_inside_lock(drafts_on, vendors_on, status_on)
        finally:
            # D3 — see portal_poll: one summarized WARN row per pass that recovered on retry.
            sustained_failure.flush_retry_recovery(SCRIPT_NAME)


def _poll_inside_lock(drafts_on: bool, vendors_on: bool, status_on: bool) -> PoPollStats:
    """Body of poll_once running under the file lock."""
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
            f"PO Worker base URL temporarily unreadable ({creds.reason}) — skipping "
            f"this cycle; will retry next interval (transient, self-heals)",
            error_code="po_creds_transient",
        )
        _write_heartbeat()
        _write_heartbeat_row(status="WARN", items_processed=0,
                             error_summary=f"base URL unreadable ({creds.reason}) — transient")
        return PoPollStats(halted_transient=True)
    if creds is None:
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            (
                # Deliberately does NOT interpolate the ITS_Config key or the Keychain
                # entry NAMES (secret-store names in a log are a CodeQL clear-text trip).
                "fail-closed: missing PO portal credentials — the Worker base URL "
                "(ITS_Config) and/or the PO bearer + HMAC-secret Keychain entries are "
                "unset; NOT polling until fixed"
            ),
            error_code="po_creds_missing",
        )
        _write_heartbeat()
        _write_heartbeat_row(status="ERROR", items_processed=0,
                             error_summary="fail-closed: PO portal credentials missing")
        return PoPollStats(halted_no_creds=True)

    counters: dict[str, int] = {
        "drafts_scanned": 0, "filed": 0, "rejected": 0, "fenced": 0,
        "skipped_flagged": 0, "draft_errors": 0,
        "attachments_filed": 0, "attachments_refused": 0, "attachment_errors": 0,
        "vendors_downsynced": 0, "vendors_upsynced": 0, "vendors_reviewed": 0,
        "vendor_errors": 0, "status_synced": 0, "status_errors": 0,
    }
    bearer_rejected = False
    try:
        if drafts_on:
            _drafts_pass(creds, counters)
            # Feature B — the attachment pass runs AFTER the drafts drain (a PO filed
            # this cycle is already pending_review, so its attachments are serviceable
            # immediately) and is FENCED: an attachment failure must NEVER block or
            # taint PO filing (mirror portal_poll._service_pdf_requests' fence).
            try:
                _attachments_pass(creds, counters)
            except _BearerRejectedError:
                raise
            except Exception as exc:  # noqa: BLE001 — the fence; retries next cycle
                counters["attachment_errors"] += 1
                error_log.log(
                    Severity.ERROR, SCRIPT_NAME,
                    f"attachment pass failed (never blocks PO filing; retries next "
                    f"cycle): {type(exc).__name__}: {exc!r}",
                    error_code="po_attachment_service_failed",
                )
        if vendors_on:
            _vendor_down_sync_pass(creds, counters)
            _vendor_up_sync_pass(creds, counters)
        if status_on:
            _status_pass(creds, counters)
    except _BearerRejectedError:
        # A 401 anywhere: the SAME bearer fails every PO route, so nothing else can
        # work this cycle. A bad/rotated bearer will NOT self-heal → page.
        bearer_rejected = True
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            "PO bearer UNAUTHORIZED (401) — rejected by the Worker's requirePoToken "
            "tier; cycle STOPPED until the token is fixed (everything stays "
            "queued/dirty — safe re-attempt)",
            error_code="po_bearer_rejected",
        )

    _write_heartbeat()
    total_errors = (
        counters["draft_errors"] + counters["attachment_errors"]
        + counters["vendor_errors"] + counters["status_errors"]
    )
    # `skipped_flagged` counts items ALREADY one-shot-flagged — permanently wedged out
    # of the queue until an operator fixes the cause and clears the entry. Omitting it
    # made Last Cycle Status flip back to OK on the very next cycle, so the daemon read
    # healthy while an item sat fenced indefinitely (2026-08-10, rfq_poll). A standing
    # fence is a standing WARN by design: ITS_Daemon_Health is a visibility surface, not
    # a push surface, so this pages nobody — and it self-clears when the flag is cleared.
    total_flagged = (
        counters["rejected"] + counters["fenced"] + counters["attachments_refused"]
        + counters["vendors_reviewed"] + counters["skipped_flagged"]
    )
    if bearer_rejected:
        cycle_status: HeartbeatStatus = "ERROR"
    elif total_errors > 0:
        cycle_status = "DEGRADED"
    elif total_flagged > 0:
        cycle_status = "WARN"
    else:
        cycle_status = "OK"
    if circuit_breaker.is_open():
        cycle_status = "CIRCUIT_OPEN"
    if total_errors == 0 and total_flagged == 0 and not bearer_rejected:
        error_summary = None
    else:
        error_summary = (
            f"errors={total_errors} flagged={total_flagged}"
            + (f" standing={counters['skipped_flagged']}"
               if counters["skipped_flagged"] else "")
            + (" bearer_rejected" if bearer_rejected else "")
        )
    try:
        _write_heartbeat_row(
            status=cycle_status,
            items_processed=(
                counters["filed"] + counters["attachments_filed"]
                + counters["vendors_upsynced"] + counters["status_synced"]
            ),
            error_summary=error_summary,
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
            f"po cycle: drafts scanned={counters['drafts_scanned']} filed={counters['filed']} "
            f"rejected={counters['rejected']} fenced={counters['fenced']} "
            f"flag-skipped={counters['skipped_flagged']} errors={counters['draft_errors']}; "
            f"attachments filed={counters['attachments_filed']} "
            f"refused={counters['attachments_refused']} errors={counters['attachment_errors']}; "
            f"vendors down={counters['vendors_downsynced']} up={counters['vendors_upsynced']} "
            f"reviewed={counters['vendors_reviewed']} errors={counters['vendor_errors']}; "
            f"status synced={counters['status_synced']} errors={counters['status_errors']}"
        ),
        error_code="po_cycle_summary",
    )
    return PoPollStats(
        bearer_rejected=bearer_rejected,
        drafts_scanned=counters["drafts_scanned"],
        filed=counters["filed"],
        rejected=counters["rejected"],
        fenced=counters["fenced"],
        skipped_flagged=counters["skipped_flagged"],
        draft_errors=counters["draft_errors"],
        attachments_filed=counters["attachments_filed"],
        attachments_refused=counters["attachments_refused"],
        attachment_errors=counters["attachment_errors"],
        vendors_downsynced=counters["vendors_downsynced"],
        vendors_upsynced=counters["vendors_upsynced"],
        vendors_reviewed=counters["vendors_reviewed"],
        vendor_errors=counters["vendor_errors"],
        status_synced=counters["status_synced"],
        status_errors=counters["status_errors"],
    )


# ---- ① Drafts pass ----------------------------------------------------------------


def _drafts_pass(creds: _PoCreds, counters: dict[str, int]) -> None:
    """Drain the queued-PO queue: verify → assert → render → file → receipt."""
    try:
        pending = portal_client.get_pending_pos(creds.base_url, creds.bearer)
    except portal_client.PortalAuthError as exc:
        raise _BearerRejectedError from exc
    except portal_client.PortalTransportError as exc:
        counters["draft_errors"] += 1
        n = _FETCH_FAILS.record()
        if sustained_failure.is_escalation_cycle(
            n, sustained_failure.DEFAULT_CRITICAL_THRESHOLD
        ):
            # SUSTAINED outage: escalate to CRITICAL (the triple-fire push path + the
            # dashboard fire surfaces key on CRITICAL — per-cycle ERROR alone is invisible).
            # On the shared LADDER, not every cycle past the threshold: an open CRITICAL is
            # never terminal, so the old `n >= threshold` minted ~626 UNROTATABLE rows per
            # 21 h outage. See `sustained_failure.is_escalation_cycle`.
            error_log.log(
                Severity.CRITICAL, SCRIPT_NAME,
                f"pending fetch failing for {n} consecutive cycles — SUSTAINED intake outage "
                f"(rows left queued; see docs/runbooks/po_poll.md): {exc!r}",
                error_code="po_pending_fetch_sustained",
            )
        else:
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"failed to GET po pending (rows left queued for next cycle); {n} consecutive: {exc!r}",
            error_code="po_pending_fetch_failed",
            )
        return
    _FETCH_FAILS.reset()  # a successful fetch clears the sustained-outage counter
    if not pending:
        return

    # Per-CYCLE config resolution: purchaser identity + tax table (versioned files,
    # D5/D8). A broken config file is a deploy defect — abort the pass loudly, leave
    # every row queued.
    try:
        purchaser = terms_lib.load_purchaser_config()
        tax_config = terms_lib.load_tax_config()
    except terms_lib.TermsError as exc:
        counters["draft_errors"] += 1
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            f"purchaser/tax config unreadable — drafts pass ABORTED (rows left "
            f"queued): {exc}",
            error_code="po_config_unreadable",
        )
        return

    flags = _load_flags()
    flags_dirty = False
    for row in pending:
        counters["drafts_scanned"] += 1
        if _process_pending_po(row, creds, counters, flags, purchaser, tax_config):
            flags_dirty = True
    if flags_dirty:
        _persist_flags(flags)


def _process_pending_po(
    row: dict[str, Any],
    creds: _PoCreds,
    counters: dict[str, int],
    flags: dict[str, str],
    purchaser: dict[str, Any],
    tax_config: dict[str, Any],
) -> bool:
    """Verify + assert + render + file + receipt ONE pending PO. Returns True iff
    the one-shot flag set was mutated (the caller persists once per cycle)."""
    raw_id = row.get("id")
    if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0:
        counters["draft_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"pending PO row has a missing/malformed id ({raw_id!r}); skipping",
            error_code="po_row_no_id",
        )
        return False
    po_id = raw_id
    key = str(po_id)
    if key in flags:
        counters["skipped_flagged"] += 1
        return False

    po_number = str(row.get("po_number") or "")
    # Split the HMAC off the row IMMEDIATELY (the portal_poll hygiene: an integrity
    # tag has no business traveling into render/filing/logs).
    provided_hmac = str(row.get("hmac") or "")
    po = {k: v for k, v in row.items() if k != "hmac"}
    raw_lines = po.get("line_items")
    line_items = [ln for ln in raw_lines if isinstance(ln, dict)] if isinstance(raw_lines, list) else []
    correlation_id = uuid.uuid4().hex[:12]

    # 1 — HMAC verify (Invariant 2 downgrade defense; constant-time).
    try:
        canonical = portal_hmac.po_canonical_json(po, line_items)
    except ValueError as exc:
        # NaN/Infinity in a money field — malformed beyond what the Worker could
        # ever have signed. Permanent.
        counters["fenced"] += 1
        _fence_po(po_id, po_number, f"canonical serialization failed: {exc}",
                  "po_canonical_invalid", correlation_id, flags, "canonical")
        return True
    if not portal_hmac.verify_po(
        creds.secret, provided_hmac,
        po_id=po_id, po_number=po_number, canonical_json=canonical,
    ):
        counters["rejected"] += 1
        _handle_po_hmac_failure(po_id, po_number, po, correlation_id, flags)
        return True

    # 2 — totals recompute + assert vs the SIGNED values (never render a number we
    # did not re-derive).
    mismatches = po_generate.totals_mismatches(
        po, line_items, rates_bp=tax_config["rates_bp"]
    )
    if mismatches:
        counters["fenced"] += 1
        _fence_po(
            po_id, po_number,
            f"totals recompute disagrees with the signed values: {mismatches}",
            "po_totals_mismatch", correlation_id, flags, "totals",
            reason=review_queue.ReviewReason.MISMATCHED_REFERENCE,
        )
        return True

    try:
        # 3 — PO_Log collision double-check (hand-issued POs in transition).
        collision = numbering.check_collision(po_number, po_id)
        if collision is not None:
            counters["fenced"] += 1
            _fence_po(
                po_id, po_number,
                f"PO number already in PO_Log and not ours ({collision}) — a "
                f"hand-issued PO or ledger defect; NOT filing a duplicate number",
                "po_number_collision", correlation_id, flags, "collision",
                reason=review_queue.ReviewReason.MISMATCHED_REFERENCE,
            )
            return True

        # 4 — vendor snapshot from the ITS_Vendors SoR at render time (#494).
        vendor_key = str(po.get("vendor_key") or "")
        vendor = vendors.get_vendor_by_key(vendor_key)
        if vendor is None:
            counters["fenced"] += 1
            _fence_po(
                po_id, po_number,
                f"vendor {vendor_key!r} not found in ITS_Vendors (the SoR) — cannot "
                f"embed a Seller identity; fix the vendor row, then clear this PO's "
                f"entry from {PO_FLAGGED_PATH.name} to retry",
                "po_vendor_unknown", correlation_id, flags, "vendor",
            )
            return True
        vendor_name = str(vendor.get(vendors.COL_VENDOR_NAME) or "")

        # 5 — terms resolution (strict token fill; TermsError → permanent fence).
        terms = po_generate.resolve_terms(
            str(po.get("terms_profile_id") or ""),
            str(po.get("terms_version") or ""),
            purchaser_entity=str(purchaser.get("entity") or ""),
            seller_name=vendor_name,
        )

        # 6 — predecessor number for the supersession clause + ledger display.
        supersedes_po_id = po.get("supersedes_po_id")
        supersedes_display: str | None = None
        if isinstance(supersedes_po_id, int) and not isinstance(supersedes_po_id, bool):
            supersedes_display = po_log.find_po_number_by_d1_id(supersedes_po_id)

        # 7 — deterministic render (the PO date = the filing date, Pacific).
        po_date = datetime.now(_PACIFIC).date()
        pdf = po_generate.render_po_pdf(
            po, line_items, vendor, purchaser, terms,
            po_date=po_date,
            supersedes_po_number=supersedes_display,
            state_names=tax_config["state_names"],
        )

        # 8 — Box filing: §45 find-or-create the PO root's per-job folder, §47
        # version-on-conflict under the deterministic name.
        folder_id = _resolve_po_box_folder(str(po.get("job_name") or ""))
        if folder_id is None:
            counters["draft_errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"PO Box root unresolved (ITS_Config "
                f"{po_naming.CFG_BOX_PORTAL_ROOT} unset) — PO {po_number} left "
                f"queued until the root is configured",
                error_code="po_box_root_unresolved",
                correlation_id=correlation_id,
            )
            return False
        # The Box filename is the version-on-conflict idempotency key (upload_bytes_or_new_version
        # resolves the existing file BY NAME in the folder). It embeds the job name, which is treated
        # as STABLE per PO — a PO's job is fixed once allocated. A job rename in the crash→retry window
        # would (very narrowly) yield a second Box file rather than a new version; the folder is already
        # the job folder, so it's a recoverable duplicate, never data loss (§47).
        file_info = box_client.upload_bytes_or_new_version(
            folder_id, po_naming.po_pdf_filename(po_number, po.get("job_name")), pdf
        )
        box_file_id = str(file_info["id"])
        box_link = f"https://app.box.com/file/{box_file_id}"

        # 9 — PO_Log append (idempotent: the collision check above proved any
        # existing row is OURS — a crash-retry — so only append when absent).
        ledger_row_kwargs: dict[str, Any] = {
            "po_number": po_number,
            "job_project": f"{po.get('job_no')} — {po.get('job_name')}",
            "job_id": str(po.get("job_id") or ""),
            "vendor_name": vendor_name,
            "vendor_key": vendor_key,
            "total_cents": int(po.get("total_cents") or 0),
            "pdf_link": box_link,
            "supersedes_display": supersedes_display or "",
            "terms_profile": str(po.get("terms_profile_id") or ""),
            "created_by": str(po.get("created_by") or ""),
            "created_at_iso": po_date.isoformat(),
            "notes": po_log.notes_for_filed_row(po_id),
        }
        pdf_filename = po_naming.po_pdf_filename(po_number, po.get("job_name"))
        existing_ledger = po_log.find_row_by_po_number(po_number)
        if existing_ledger is None:
            ledger_row_id = po_log.append_filed_row(**ledger_row_kwargs)
        else:
            ledger_row_id = int(existing_ledger["_row_id"])
        # Inline attach of the PO PDF on the ledger row (RFQ-ledger parity, operator
        # ask 2026-08-11 — the review row already carried it; the ledger row now does
        # too). On EVERY service, not fresh-append-only (the rfq_poll posture):
        # attach_pdf_to_row REPLACES a same-filename attachment and the filename is
        # deterministic per PO, so this is duplicate-free — and an attach that failed
        # in a cycle whose receipt also failed SELF-HEALS on the re-serve.
        _attach_pdf_best_effort(
            ledger_row_id, pdf_filename, pdf, correlation_id, sheet_id=po_log.SHEET_ID,
        )

        # 9b — per-job tracking sheet mirror (Feature A), BEST-EFFORT: the same
        # ledger row into "<Jobs>/<job>/Purchase Orders" (find-or-create;
        # independently idempotent per target sheet), now WITH the inline PDF
        # (operator ask 2026-08-11). Fenced inside the helper — a per-job failure
        # must NEVER fail the filing (Box + the flat PO_Log are the SoR).
        _append_perjob_row_best_effort(
            str(po.get("job_name") or ""), ledger_row_kwargs, correlation_id,
            pdf_filename=pdf_filename, pdf_bytes=pdf,
        )

        # 10 — PO_Pending_Review row (idempotent via the Notes po_id join) + the
        # inline PDF attach (best-effort — Box is the SoR). The attach runs on EVERY
        # service, not fresh-create-only (wiring audit 2026-08-12, the #98 subcontract
        # posture): a crash between add_po_review_row and the attach used to leave the
        # approver's row permanently bare, because the re-serve found the row and
        # skipped the guarded block. Deterministic filename → replace, never duplicate.
        existing_review = po_review.find_row_by_po_id(po_id)
        if existing_review is not None:
            review_row_id = int(existing_review["_row_id"])
        else:
            email_body = po_review.po_email_body_template(
                contact_name=str(vendor.get(vendors.COL_CONTACT_NAME) or ""),
                po_number=po_number,
                job_name=str(po.get("job_name") or ""),
                purchaser_entity=str(purchaser.get("entity") or ""),
            )
            routing = purchaser.get("invoice_routing") or {}
            cc_display = ", ".join(str(c) for c in routing.get("cc", []))
            review_row_id = po_review.add_po_review_row(
                job_project=f"{po.get('job_no')} — {po.get('job_name')}",
                vendor_key=vendor_key,
                po_date=po_date,
                pdf_link=box_link,
                recipient_to=str(vendor.get(vendors.COL_CONTACT_EMAIL) or ""),
                cc_display=cc_display,
                email_body=email_body,
                notes=po_review.notes_for_review_row(
                    po_id, po_number,
                    supersedes_po_id=supersedes_po_id
                    if isinstance(supersedes_po_id, int) and not isinstance(supersedes_po_id, bool)
                    else None,
                ),
            )
        _attach_pdf_best_effort(review_row_id, pdf_filename, pdf, correlation_id)

        # 11 — the receipt, LAST (queued→pending_review; a crash before this line
        # re-pulls the row and every step above is idempotent).
        portal_client.mark_po_filed(
            creds.base_url, creds.bearer, po_id=po_id, box_file_id=box_file_id
        )
        counters["filed"] += 1
        error_log.log(
            Severity.INFO, SCRIPT_NAME,
            f"filed PO {po_number} (po_id={po_id}) → Box + PO_Log + PO_Pending_Review",
            error_code="po_filed",
            correlation_id=correlation_id,
        )
        return False
    except portal_client.PortalAuthError as exc:
        raise _BearerRejectedError from exc
    except terms_lib.TermsError as exc:
        counters["fenced"] += 1
        _fence_po(po_id, po_number, f"terms resolution failed: {exc}",
                  "po_terms_error", correlation_id, flags, "terms")
        return True
    except (
        picklist_validation.PicklistViolationError,
        smartsheet_client.SmartsheetValidationError,
    ) as exc:
        counters["fenced"] += 1
        _fence_po(po_id, po_number,
                  f"permanent write reject ({type(exc).__name__}): {exc}",
                  "po_permanent_reject", correlation_id, flags, "permanent")
        return True
    except (
        smartsheet_client.SmartsheetError,
        box_client.BoxError,
        portal_client.PortalTransportError,
    ) as exc:
        counters["draft_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"transient failure filing PO {po_number} (po_id={po_id}; left queued "
            f"for next cycle): {type(exc).__name__}: {exc!r}",
            error_code="po_filing_transient",
            correlation_id=correlation_id,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — per-row fence; one bad PO never kills the cycle
        counters["draft_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"unexpected failure filing PO {po_number} (po_id={po_id}; left queued): "
            f"{type(exc).__name__}: {exc!r}",
            error_code="po_filing_unexpected",
            correlation_id=correlation_id,
        )
        return False


def _handle_po_hmac_failure(
    po_id: int, po_number: str, po: dict[str, Any],
    correlation_id: str, flags: dict[str, str],
) -> None:
    """Reject a bad-HMAC PO row — the PO twin of portal_poll._handle_hmac_failure.

    NEVER rendered, NEVER filed, NEVER mark-filed (the downgrade defense; the row
    stays queued in D1 for forensics — the PO tier has no mark-rejected route by
    design). One-shot: anomaly-log + security Review-Queue row + CRITICAL fire only
    on the FIRST sighting; the flag set suppresses per-cycle re-flag spam."""
    # Tripwire (Invariant 2, Layer 5) — record the suspicious pattern.
    anomaly_logger.check({"po_hmac_failure": po_id, "po_number": po_number})
    review_queue.safe_add(
        script_name=SCRIPT_NAME,
        workstream=WORKSTREAM,
        summary=(
            f"po: HMAC verification FAILED for PO {po_number or po_id} — rejected, "
            f"NOT rendered or filed"
        ),
        payload={
            "po_id": po_id,
            "po_number": po_number,
            "job_no": po.get("job_no"),
            "vendor_key": po.get("vendor_key"),
            # The HMAC value is deliberately NOT recorded (signature material —
            # same posture as the submission twin); the raw row stays in D1.
        },
        sla_tier=review_queue.SlaTier.RFQ_DRAFT,
        reason=review_queue.ReviewReason.SECURITY_TRIGGER,
        severity=Severity.CRITICAL,
        source_file=f"po:{po_id}",
        security_flag=True,
    )
    error_log.log(
        Severity.CRITICAL, SCRIPT_NAME,
        (
            f"po HMAC FAIL po_id={po_id} po_number={po_number!r} — rejected, not "
            f"rendered or filed (downgrade defense)"
        ),
        error_code="po_hmac_failure",
        correlation_id=correlation_id,
    )
    flags[str(po_id)] = "hmac"


def _fence_po(
    po_id: int,
    po_number: str,
    detail: str,
    error_code: str,
    correlation_id: str,
    flags: dict[str, str],
    flag_reason: str,
    *,
    reason: review_queue.ReviewReason = review_queue.ReviewReason.POLICY_EDGE,
) -> None:
    """Route a PERMANENTLY-refused pending PO to the Review Queue + one-shot flag it.

    The row is never filed and never mark-filed; it stays queued in D1. Remediation:
    fix the cause, then delete the po_id entry from `po_poll_flagged.json` (the
    daemon retries it the next cycle)."""
    review_queue.safe_add(
        script_name=SCRIPT_NAME,
        workstream=WORKSTREAM,
        summary=f"po: PO {po_number or po_id} refused before filing — {detail}",
        payload={"po_id": po_id, "po_number": po_number, "detail": detail},
        sla_tier=review_queue.SlaTier.RFQ_DRAFT,
        reason=reason,
        severity=Severity.WARN,
        source_file=f"po:{po_id}",
    )
    error_log.log(
        Severity.WARN, SCRIPT_NAME,
        f"po fenced (permanent, {error_code}) po_id={po_id} po_number={po_number!r}: {detail}",
        error_code=error_code,
        correlation_id=correlation_id,
    )
    flags[str(po_id)] = flag_reason


def _resolve_po_box_folder(job_name: str) -> str | None:
    """§45 find-or-create the PO filing folder: the PO lane's OWN Box ROOT
    (`po_naming.CFG_BOX_PORTAL_ROOT`) → per-job folder (the SAME
    `safety_naming.job_folder_name` as every other portal artifact). PO PDFs file
    DIRECTLY in the job folder; 'RFQs' / 'Vendor Quotes' are its children
    (rfq_poll / estimate_poll). None when the root is unconfigured (the caller
    leaves the PO queued + ERRORs — a config gap, not a per-PO defect)."""
    root = _read_str_setting(
        po_naming.CFG_BOX_PORTAL_ROOT, "",
        workstream=po_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM,
    ).strip()
    if not root:
        return None
    return box_client.get_or_create_folder(root, safety_naming.job_folder_name(job_name))


def _attach_pdf_best_effort(
    row_id: int, filename: str, pdf_bytes: bytes, correlation_id: str,
    *, sheet_id: int | None = None,
) -> None:
    """Attach the rendered PDF inline on a Smartsheet row, BEST-EFFORT (Box is the
    SoR; a failure is a WARN that never fails the filing — mirror intake).
    `sheet_id` picks the target sheet: None = the review sheet (the original
    surface); the filing pass ALSO passes the flat PO_Log's id and the per-job
    tracking sheet's id (RFQ-lane parity, operator ask 2026-08-11 — the ledger and
    per-job mirror rows carry the same inline copy)."""
    try:
        smartsheet_client.attach_pdf_to_row(
            sheet_id if sheet_id is not None else po_review.SHEET_ID,
            row_id, filename, pdf_bytes,
        )
    except Exception as exc:  # noqa: BLE001 — supplementary inline copy; Box is the SoR
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"row PDF attach failed (row {row_id}, {filename!r}, "
            f"sheet {'review' if sheet_id is None else sheet_id}): "
            f"{type(exc).__name__}: {exc!r}",
            error_code="po_row_pdf_attach_failed",
            correlation_id=correlation_id,
        )


# ---- ①b Attachment pass (Feature B — §34 doc-attachment screen + file) --------------


def _reassemble_chunks(chunks: list[dict[str, Any]]) -> bytes:
    """Concatenate the decoded chunk bytes into the original file.

    STRICT: every chunk must agree on chunk_total, the index set must be exactly
    {0..n-1} (gap-free — the filed_pdfs completeness rule), and every chunk_b64 must
    strictly decode. Raises ValueError on ANY malformation — the caller treats that
    as an INTEGRITY failure (the chunk set was written atomically with the row, so a
    broken set is tamper or a serving defect, never a benign partial)."""
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


def _attachments_pass(creds: _PoCreds, counters: dict[str, int]) -> None:
    """Service the PO document-attachment queue (Feature B): claim → pull bytes →
    verify (po-att:v1 HMAC + sha256) → §34 screen (po_attach_screen) → CLEAN files
    to the PO's Box folder + the PO_Log row; SUSPICIOUS/MALICIOUS are refused with a
    Review-Queue record. Runs AFTER the drafts drain under the same polling gate —
    an attachment can never block or taint the PO filing itself (the caller fences
    this whole pass with error_code=po_attachment_service_failed)."""
    try:
        pending = portal_client.get_po_attachments_pending(creds.base_url, creds.bearer)
    except portal_client.PortalAuthError as exc:
        raise _BearerRejectedError from exc
    except portal_client.PortalTransportError as exc:
        counters["attachment_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"failed to GET po attachments pending (rows left for next cycle): {exc!r}",
            error_code="po_attachments_fetch_failed",
        )
        return
    if not pending:
        return
    clamav_enabled = _attach_clamav_enabled()
    flags = _load_flags()
    flags_dirty = False
    for row in pending:
        if _service_one_attachment(row, creds, counters, flags, clamav_enabled):
            flags_dirty = True
    if flags_dirty:
        _persist_flags(flags)


def _service_one_attachment(
    row: dict[str, Any],
    creds: _PoCreds,
    counters: dict[str, int],
    flags: dict[str, str],
    clamav_enabled: bool,
) -> bool:
    """Claim, verify, screen, and disposition ONE pending attachment. Returns True
    iff the one-shot flag set was mutated (the caller persists once per pass).

    Verify-before-anything (Invariant 2): the po-att:v1 HMAC binds the row's fields
    AND the content digest; the sha256 recompute over the reassembled chunks extends
    the signature to the bytes. Either failing → CRITICAL + security Review-Queue
    row + one-shot flag; the bytes stay in D1 for forensics (no disposition post).
    """
    raw_id = row.get("id")
    if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0:
        counters["attachment_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"pending attachment row has a missing/malformed id ({raw_id!r}); skipping",
            error_code="po_attachment_row_no_id",
        )
        return False
    att_id = raw_id
    flag_key = f"att-{att_id}"
    if flag_key in flags:
        return False
    att_uuid = str(row.get("att_uuid") or "")
    po_number = str(row.get("po_number") or "")
    job_name = str(row.get("job_name") or "")
    filename = str(row.get("filename") or "")
    declared_mime = str(row.get("declared_mime") or "")
    uploaded_by = str(row.get("uploaded_by") or "")
    provided_hmac = str(row.get("hmac") or "")
    raw_po_id = row.get("po_id")
    po_id = raw_po_id if isinstance(raw_po_id, int) and not isinstance(raw_po_id, bool) else 0
    raw_size = row.get("size_bytes")
    size_bytes = raw_size if isinstance(raw_size, int) and not isinstance(raw_size, bool) else -1
    signed_sha256 = str(row.get("sha256") or "")
    correlation_id = uuid.uuid4().hex[:12]

    try:
        # 1 — claim FIRST (the photo-pool claim-first semantics): a crash after this
        # leaves an observable 'claimed' row that re-serves next cycle.
        portal_client.claim_po_attachment(creds.base_url, creds.bearer, attachment_id=att_id)

        # 2 — pull + reassemble the bytes (the ONLY Mac-ward byte flow).
        chunks = portal_client.get_po_attachment_chunks(
            creds.base_url, creds.bearer, attachment_id=att_id
        )
        try:
            data = _reassemble_chunks(chunks)
        except ValueError as exc:
            counters["attachments_refused"] += 1
            _handle_attachment_integrity_failure(
                att_id, po_number, filename, uploaded_by,
                f"chunk reassembly failed: {exc}", correlation_id, flags,
            )
            return True

        # 3 — verify: the po-att:v1 HMAC over the served fields, then the content
        # digest + size against the SIGNED values (never screen unverified bytes).
        if not portal_hmac.verify_po_attachment(
            creds.secret, provided_hmac,
            att_uuid=att_uuid, po_id=po_id, filename=filename,
            declared_mime=declared_mime, size_bytes=size_bytes, sha256=signed_sha256,
        ):
            counters["attachments_refused"] += 1
            _handle_attachment_integrity_failure(
                att_id, po_number, filename, uploaded_by,
                "HMAC verification FAILED", correlation_id, flags,
            )
            return True
        if len(data) != size_bytes or hashlib.sha256(data).hexdigest() != signed_sha256:
            counters["attachments_refused"] += 1
            _handle_attachment_integrity_failure(
                att_id, po_number, filename, uploaded_by,
                "content digest/size disagrees with the signed values", correlation_id, flags,
            )
            return True

        # 4 — §34 screen (po_attach_screen: L1 magic/consistency → L2 structural →
        # L3 config-gated ClamAV on the raw bytes).
        result = po_attach_screen.screen_attachment(
            filename, declared_mime, data, clamav_enabled=clamav_enabled
        )

        if result.disposition == "clean":
            # 5 — file the ORIGINAL bytes: Box (the PO root's per-job folder, §47
            # version-on-conflict) + the PO_Log row attachment (best-effort), then
            # the disposition post-back (the Worker deletes the D1 chunks).
            folder_id = _resolve_po_box_folder(job_name)
            if folder_id is None:
                counters["attachment_errors"] += 1
                error_log.log(
                    Severity.ERROR, SCRIPT_NAME,
                    f"Box portal root unresolved — attachment {att_id} for PO "
                    f"{po_number} left claimed until the root is configured",
                    error_code="po_box_root_unresolved",
                    correlation_id=correlation_id,
                )
                return False
            # The attachment ID disambiguates the filed name (review BLOCKER fix):
            # two same-named uploads on one PO are DISTINCT documents — without the
            # id the 2nd would version-over the 1st in Box AND replace its PO_Log
            # inline copy (attach replace=True). A crash-retry of THIS attachment
            # keeps its id → same name → idempotent §47 version, as intended.
            filed_name = po_naming.po_attachment_filename(po_number, att_id, filename)
            file_info = box_client.upload_bytes_or_new_version(folder_id, filed_name, data)
            box_file_id = str(file_info["id"])
            _attach_bytes_to_po_log_best_effort(
                po_number, filed_name, data, declared_mime, correlation_id
            )
            portal_client.post_po_attachment_result(
                creds.base_url, creds.bearer,
                attachment_id=att_id, status="filed", box_file_id=box_file_id,
            )
            counters["attachments_filed"] += 1
            error_log.log(
                Severity.INFO, SCRIPT_NAME,
                f"filed PO attachment {filed_name!r} (att {att_id}, PO {po_number}) "
                f"→ Box + PO_Log row",
                error_code="po_attachment_filed",
                correlation_id=correlation_id,
            )
            return False

        detail = f"{result.layer}:{result.detail}"
        if result.disposition == "malicious":
            # MALICIOUS → CRITICAL NAMING THE ACCOUNT + security-flagged Review-Queue
            # row, refused before filing (the photo_screen/intake posture). The PO
            # itself stays filed — only the attachment is refused.
            review_queue.safe_add(
                script_name=SCRIPT_NAME,
                workstream=WORKSTREAM,
                summary=(
                    f"po: MALICIOUS attachment {filename!r} on PO {po_number} "
                    f"(uploaded by {uploaded_by!r}) — refused before filing ({detail})"
                ),
                payload={
                    "attachment_id": att_id, "po_number": po_number,
                    "filename": filename, "uploaded_by": uploaded_by, "detail": detail,
                },
                sla_tier=review_queue.SlaTier.RFQ_DRAFT,
                reason=review_queue.ReviewReason.SECURITY_TRIGGER,
                severity=Severity.CRITICAL,
                source_file=f"po-att:{att_id}",
                security_flag=True,
            )
            error_log.log(
                Severity.CRITICAL, SCRIPT_NAME,
                f"MALICIOUS PO attachment refused (att {att_id}, PO {po_number}, "
                f"account {uploaded_by!r}): {detail} — review the account before "
                f"re-enabling uploads",
                error_code="po_attachment_malicious",
                correlation_id=correlation_id,
            )
        else:  # suspicious
            security = po_attach_screen.is_structural_active_content(result)
            review_queue.safe_add(
                script_name=SCRIPT_NAME,
                workstream=WORKSTREAM,
                summary=(
                    f"po: attachment {filename!r} on PO {po_number} refused as "
                    f"SUSPICIOUS ({detail}) — not filed; operator review"
                ),
                payload={
                    "attachment_id": att_id, "po_number": po_number,
                    "filename": filename, "uploaded_by": uploaded_by, "detail": detail,
                },
                sla_tier=review_queue.SlaTier.RFQ_DRAFT,
                reason=(
                    review_queue.ReviewReason.SECURITY_TRIGGER
                    if security else review_queue.ReviewReason.POLICY_EDGE
                ),
                severity=Severity.WARN,
                source_file=f"po-att:{att_id}",
                security_flag=security,
            )
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"suspicious PO attachment refused (att {att_id}, PO {po_number}): {detail}",
                error_code="po_attachment_suspicious",
                correlation_id=correlation_id,
            )
        # ONE-SHOT FLAG the content rejection BEFORE the disposition post-back
        # (review SHOULD-FIX #3): the CRITICAL/WARN + Review-Queue row above already
        # fired; if the post below fails transiently, the row would otherwise
        # re-serve + re-screen + RE-FIRE every ~90s — duplicate CRITICALs + duplicate
        # Review-Queue rows on the cap-sensitive sheets. Mirrors _fence_po /
        # _handle_po_hmac_failure / _handle_attachment_integrity_failure: every
        # permanent-rejection path flags. On a successful post the flag is benign
        # residue (the refused row never re-serves); on a failed post the flag IS the
        # dedupe — remediation is the documented flag-file entry delete.
        flags[flag_key] = "refused"
        # Disposition post-back LAST: the Worker flips the row + deletes the chunks
        # (delete-on-disposition). Handled LOCALLY (not the outer transient fence) so
        # a failed post still persists the flag mutation via return True.
        try:
            portal_client.post_po_attachment_result(
                creds.base_url, creds.bearer,
                attachment_id=att_id, status="refused", detail=detail[:200],
            )
        except portal_client.PortalAuthError as exc:
            raise _BearerRejectedError from exc
        except portal_client.PortalTransportError as exc:
            counters["attachment_errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"refused-disposition post-back failed for attachment {att_id} "
                f"(PO {po_number}; row stays claimed in D1, one-shot flag prevents "
                f"re-alert; clear 'att-{att_id}' from {PO_FLAGGED_PATH.name} to "
                f"retry after the transport recovers): {exc!r}",
                error_code="po_attachment_result_post_failed",
                correlation_id=correlation_id,
            )
        counters["attachments_refused"] += 1
        return True
    except portal_client.PortalAuthError as exc:
        raise _BearerRejectedError from exc
    except (
        smartsheet_client.SmartsheetError,
        box_client.BoxError,
        portal_client.PortalTransportError,
    ) as exc:
        counters["attachment_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"transient failure servicing attachment {att_id} (PO {po_number}; "
            f"stays serviceable for next cycle): {type(exc).__name__}: {exc!r}",
            error_code="po_attachment_transient",
            correlation_id=correlation_id,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — per-row fence; one attachment never kills the pass
        counters["attachment_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"unexpected failure servicing attachment {att_id} (PO {po_number}): "
            f"{type(exc).__name__}: {exc!r}",
            error_code="po_attachment_unexpected",
            correlation_id=correlation_id,
        )
        return False


def _handle_attachment_integrity_failure(
    att_id: int, po_number: str, filename: str, uploaded_by: str,
    detail: str, correlation_id: str, flags: dict[str, str],
) -> None:
    """Reject an attachment whose transport integrity failed (bad HMAC / digest
    mismatch / malformed chunk set) — the attachment twin of _handle_po_hmac_failure.

    NEVER screened, NEVER filed, NO disposition post (the bytes stay in D1 for
    forensics — mirroring the bad-HMAC PO posture). One-shot: anomaly-log + security
    Review-Queue row + CRITICAL only on the FIRST sighting; the flag set suppresses
    per-cycle re-flag spam."""
    anomaly_logger.check({"po_attachment_integrity": att_id, "po_number": po_number})
    review_queue.safe_add(
        script_name=SCRIPT_NAME,
        workstream=WORKSTREAM,
        summary=(
            f"po: attachment INTEGRITY FAILURE (att {att_id}, PO {po_number or att_id}, "
            f"file {filename!r}) — {detail}; rejected, NOT screened or filed"
        ),
        payload={
            "attachment_id": att_id,
            "po_number": po_number,
            "filename": filename,
            "uploaded_by": uploaded_by,
            "detail": detail,
            # The HMAC value is deliberately NOT recorded (signature material);
            # the raw row + chunks stay in D1.
        },
        sla_tier=review_queue.SlaTier.RFQ_DRAFT,
        reason=review_queue.ReviewReason.SECURITY_TRIGGER,
        severity=Severity.CRITICAL,
        source_file=f"po-att:{att_id}",
        security_flag=True,
    )
    error_log.log(
        Severity.CRITICAL, SCRIPT_NAME,
        f"po attachment integrity FAIL att_id={att_id} po_number={po_number!r}: "
        f"{detail} (downgrade defense — never screened or filed)",
        error_code="po_attachment_integrity_failure",
        correlation_id=correlation_id,
    )
    flags[f"att-{att_id}"] = "integrity"


def _attach_bytes_to_po_log_best_effort(
    po_number: str, filed_name: str, data: bytes, content_type: str, correlation_id: str
) -> None:
    """Attach the screened file inline on the PO's PO_Log ledger row, BEST-EFFORT
    (Box is the SoR; a failure is a WARN that never fails the disposition — the
    _attach_pdf_best_effort posture). Passes the REAL content type through the
    content_type-aware shared helper (the Feature-B MIME fix)."""
    try:
        log_row = po_log.find_row_by_po_number(po_number)
        if log_row is None:
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"PO_Log row for {po_number} not found — attachment {filed_name!r} "
                f"filed to Box only",
                error_code="po_attachment_log_row_missing",
                correlation_id=correlation_id,
            )
            return
        smartsheet_client.attach_pdf_to_row(
            po_log.SHEET_ID, int(log_row["_row_id"]), filed_name, data,
            content_type=content_type,
        )
    except Exception as exc:  # noqa: BLE001 — supplementary inline copy; Box is the SoR
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"PO_Log attachment failed ({filed_name!r}, PO {po_number}): "
            f"{type(exc).__name__}: {exc!r}",
            error_code="po_attachment_log_attach_failed",
            correlation_id=correlation_id,
        )


def _append_perjob_row_best_effort(
    job_name: str, row_kwargs: dict[str, Any], correlation_id: str,
    *, pdf_filename: str, pdf_bytes: bytes,
) -> None:
    """Mirror the freshly-filed ledger row into the job's per-job tracking sheet
    (Feature A), BEST-EFFORT — a failure is a WARN that never fails the filing
    (mirror `_attach_pdf_best_effort`; Box + the flat PO_Log are the SoR).

    Resolves the SAME job folder name the Box per-job folder uses
    (`safety_naming.job_folder_name`), find-or-creates the folder + "Purchase
    Orders" sheet under sheet_ids.FOLDER_PO_JOBS (structure-cloned from the flat
    Log, so `append_filed_row` writes it unchanged), then appends unless the PO
    number is already present in the TARGET sheet (independent idempotency — a
    crash between the flat append and this mirror re-runs cleanly). The inline PDF
    attach rides the SAME every-service self-heal posture as the ledger row's
    (deterministic filename → replace, never duplicate)."""
    try:
        sid = job_sheet.ensure_job_sheet(
            sheet_ids.FOLDER_PO_JOBS,
            sheet_ids.SHEET_PO_LOG,
            safety_naming.job_folder_name(job_name),
            PERJOB_SHEET_NAME,
            workspace_id=sheet_ids.WORKSPACE_PURCHASE_ORDERS,  # §51 A1 margin-check target
            workstream=WORKSTREAM,
            correlation_id=correlation_id,
        )
        existing = po_log.find_row_by_po_number(str(row_kwargs["po_number"]), sheet_id=sid)
        if existing is None:
            row_id = po_log.append_filed_row(sheet_id=sid, **row_kwargs)
        else:
            row_id = int(existing["_row_id"])
        _attach_pdf_best_effort(row_id, pdf_filename, pdf_bytes, correlation_id, sheet_id=sid)
    except Exception as exc:  # noqa: BLE001 — supplementary per-job mirror; never fail the filing
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"per-job tracking sheet append failed (job {job_name!r}, "
            f"PO {row_kwargs.get('po_number')!r}): {type(exc).__name__}: {exc!r}",
            error_code="po_perjob_sheet_failed",
            correlation_id=correlation_id,
        )


# ---- ② Vendor down-sync pass --------------------------------------------------------


def _vendor_down_sync_pass(creds: _PoCreds, counters: dict[str, int]) -> None:
    """Project the FULL ITS_Vendors SoR into the Worker's D1 cache (full-replace;
    the Worker's dirty-row fence protects un-mirrored portal edits)."""
    try:
        payload = vendors.build_down_sync_payload()
    except smartsheet_client.SmartsheetError as exc:
        counters["vendor_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"ITS_Vendors read failed — down-sync skipped this cycle: {exc!r}",
            error_code="po_vendors_read_failed",
        )
        return
    for row_id, skip_reason in payload.skipped:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"ITS_Vendors row {row_id} excluded from down-sync: {skip_reason}",
            error_code="po_vendor_row_skipped",
        )
    if not payload.vendors:
        # NEVER POST an empty set — an empty projection (fresh sheet, mass read
        # miss) must not wipe the portal cache (the Worker refuses it too).
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            "ITS_Vendors projected EMPTY — refusing to down-sync an empty vendor set",
            error_code="po_vendors_empty_projection",
        )
        return
    try:
        result = portal_client.vendors_sync(creds.base_url, creds.bearer, payload.vendors)
    except portal_client.PortalAuthError as exc:
        raise _BearerRejectedError from exc
    except portal_client.PortalTransportError as exc:
        counters["vendor_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"vendors down-sync POST failed (retries next cycle): {exc!r}",
            error_code="po_vendors_sync_failed",
        )
        return
    counters["vendors_downsynced"] = len(payload.vendors)
    skipped_dirty = result.get("skipped_dirty")
    if isinstance(skipped_dirty, int) and skipped_dirty > 0:
        error_log.log(
            Severity.INFO, SCRIPT_NAME,
            f"vendors down-sync: {skipped_dirty} dirty portal row(s) fenced (un-mirrored "
            f"portal edits preserved — the up-sync converges them)",
            error_code="po_vendors_dirty_fenced",
        )


# ---- ③ Vendor up-sync pass ----------------------------------------------------------


def _vendor_up_sync_pass(creds: _PoCreds, counters: dict[str, int]) -> None:
    """Mirror portal-edited (dirty) vendors UP into ITS_Vendors, per-vendor commit."""
    try:
        pending = portal_client.get_pending_vendors(creds.base_url, creds.bearer)
    except portal_client.PortalAuthError as exc:
        raise _BearerRejectedError from exc
    except portal_client.PortalTransportError as exc:
        counters["vendor_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"failed to GET pending vendors (left dirty for next cycle): {exc!r}",
            error_code="po_vendors_pending_fetch_failed",
        )
        return
    for vendor in pending:
        vendor_key = str(vendor.get("vendor_key") or "")
        mirror_version = vendor.get("mirror_version")
        if not isinstance(mirror_version, int) or isinstance(mirror_version, bool):
            counters["vendor_errors"] += 1
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"pending vendor {vendor_key!r} has a malformed mirror_version "
                f"({mirror_version!r}); skipping (stays dirty)",
                error_code="po_vendor_version_malformed",
            )
            continue
        try:
            vendors.upsert_vendor(vendor)
            portal_client.mark_vendors_mirrored(
                creds.base_url, creds.bearer,
                [{"vendor_key": vendor_key, "mirrored_version": mirror_version}],
            )
            counters["vendors_upsynced"] += 1
        except portal_client.PortalAuthError as exc:
            raise _BearerRejectedError from exc
        except (
            ValueError,
            picklist_validation.PicklistViolationError,
            smartsheet_client.SmartsheetValidationError,
        ) as exc:
            # PERMANENT — the row will never succeed as-is; ticket the operator,
            # leave the vendor dirty (the Worker keeps serving it; the ticket is
            # the de-dup — same posture as fieldops' permanent job fence).
            counters["vendors_reviewed"] += 1
            review_queue.safe_add(
                script_name=SCRIPT_NAME,
                workstream=WORKSTREAM,
                summary=(
                    f"po: vendor up-sync PERMANENT failure for {vendor_key!r} "
                    f"({type(exc).__name__}) — portal edit not mirrored to ITS_Vendors"
                ),
                payload={
                    "vendor_key": vendor_key,
                    "mirror_version": mirror_version,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                sla_tier=review_queue.SlaTier.RFQ_DRAFT,
                reason=review_queue.ReviewReason.POLICY_EDGE,
                severity=Severity.WARN,
                source_file=vendor_key or "vendor",
            )
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"vendor up-sync fenced (permanent) {vendor_key!r}: "
                f"{type(exc).__name__}: {exc!r}",
                error_code="po_vendor_upsert_permanent",
            )
        except (smartsheet_client.SmartsheetError, portal_client.PortalTransportError) as exc:
            counters["vendor_errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"transient vendor up-sync failure for {vendor_key!r} (left dirty): "
                f"{type(exc).__name__}: {exc!r}",
                error_code="po_vendor_upsert_transient",
            )


# ---- ④ Status pass ------------------------------------------------------------------


def _status_pass(creds: _PoCreds, counters: dict[str, int]) -> None:
    """Mirror review-sheet approve/SENT stamps → Worker status-sync + PO_Log.

    Candidates are bounded by the PO_Log ledger state (a settled row generates no
    update), so the steady-state cycle POSTs nothing. Updates for one PO are ordered
    approved-then-sent — the Worker's guarded batch walks the machine forward and a
    replay no-ops. PO_Log stamps apply ONLY after a successful POST (D1 first, then
    the mirror; a lost POST retries whole next cycle)."""
    try:
        review_rows = smartsheet_client.get_rows(po_review.SHEET_ID)
        ledger_rows = smartsheet_client.get_rows(po_log.SHEET_ID)
    except smartsheet_client.SmartsheetError as exc:
        counters["status_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"status pass sheet read failed (retries next cycle): {exc!r}",
            error_code="po_status_read_failed",
        )
        return
    ledger_status: dict[str, str] = {}
    for row in ledger_rows:
        number = str(row.get(po_log.COL_PO_NUMBER) or "").strip()
        if number:
            ledger_status[number] = str(row.get(po_log.COL_STATUS) or "").strip()

    updates: list[dict[str, Any]] = []
    # Deferred PO_Log stamps: (kind, po_number, sent_at_iso, supersedes_po_id).
    stamps: list[tuple[str, str, str | None, int | None]] = []
    for row in review_rows:
        tag = str(row.get(po_review.COL_WORKSTREAM) or "").strip()
        if tag and tag != po_review.WORKSTREAM_TAG:
            # Contamination signal (P1b) — a foreign-workstream row on the PO review
            # sheet is never status-synced; the send guard owns the HARD-HELD.
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"PO review row {row.get('_row_id')} carries foreign workstream tag "
                f"{tag!r}; ignored by the status pass",
                error_code="po_status_foreign_tag",
            )
            continue
        po_id = po_review.row_po_id(row)
        if po_id is None:
            continue  # a row without the Notes join (hand row) — nothing to sync
        note_match = str(row.get(po_review.COL_NOTES) or "")
        po_number = ""
        for part in note_match.split(";"):
            part = part.strip()
            if part.startswith("po_number="):
                po_number = part[len("po_number="):].strip()
        if not po_number:
            continue
        current = ledger_status.get(po_number, "")
        if current in (po_log.STATUS_SENT, po_log.STATUS_SUPERSEDED, po_log.STATUS_CANCELED):
            continue  # settled — nothing to move forward

        sent = str(row.get(po_review.COL_SEND_STATUS) or "") == po_review.STATUS_SENT
        approved = bool(
            row.get(po_review.COL_APPROVE_SCHEDULED)
            or row.get(po_review.COL_SEND_NOW)
            or row.get(po_review.COL_APPROVED_BY)
        )
        supersedes_po_id = po_review.row_supersedes_po_id(row)
        if sent:
            # Walk the D1 machine forward in ONE ordered pair — approved (guarded
            # from pending_review) THEN sent (guarded from approved); replays no-op.
            updates.append({"po_id": po_id, "status": "approved"})
            updates.append({"po_id": po_id, "status": "sent"})
            sent_at = str(row.get(po_review.COL_SENT_AT) or "")[:10] or None
            stamps.append(("sent", po_number, sent_at, supersedes_po_id))
        elif approved and current == po_log.STATUS_PENDING_REVIEW:
            updates.append({"po_id": po_id, "status": "approved"})
            stamps.append(("approved", po_number, None, None))

    if not updates:
        return
    try:
        portal_client.po_status_sync(creds.base_url, creds.bearer, updates)
    except portal_client.PortalAuthError as exc:
        raise _BearerRejectedError from exc
    except portal_client.PortalTransportError as exc:
        counters["status_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"status-sync POST failed (stamps deferred to next cycle): {exc!r}",
            error_code="po_status_sync_failed",
        )
        return

    for kind, po_number, sent_at_iso, supersedes_po_id in stamps:
        try:
            if kind == "sent":
                po_log.stamp_status(po_number, po_log.STATUS_SENT, sent_at_iso=sent_at_iso)
                if supersedes_po_id is not None:
                    predecessor = po_log.find_po_number_by_d1_id(supersedes_po_id)
                    if predecessor:
                        # The ledger mirror of the Worker's superseded flip (D7).
                        po_log.stamp_status(
                            predecessor, po_log.STATUS_SUPERSEDED,
                            superseded_by=po_number,
                        )
            else:
                po_log.stamp_status(po_number, po_log.STATUS_APPROVED)
            counters["status_synced"] += 1
        except (
            picklist_validation.PicklistViolationError,
            smartsheet_client.SmartsheetError,
        ) as exc:
            counters["status_errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"PO_Log stamp failed for {po_number} ({kind}; D1 already synced — "
                f"the ledger self-heals next cycle): {type(exc).__name__}: {exc!r}",
                error_code="po_log_stamp_failed",
            )


if __name__ == "__main__":
    poll_once()
