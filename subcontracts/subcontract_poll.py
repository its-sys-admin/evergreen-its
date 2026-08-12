"""Subcontract pull daemon — the ONE multi-pass Mac half of the subcontract pipeline (SC-S3c).

Purpose
-------
The Worker (safety_portal/worker/subcontract.ts) validates/computes/signs/queues each
generated subcontract SEND-FREE in D1; this launchd daemon (120s,
`org.solutionsmith.its.subcontract-poll`) is the Mac-side consumer — the `po_poll`
multi-pass model (one host, one lock, one heartbeat; per-pass ITS_Config gates, ALL
shipped false):

  ① **Drafts pass** (`subcontracts.subcontract_poll.polling_enabled`): GET
     /api/subcontracts/internal/pending → per row: recompute the sub:v1 canonical
     string + constant-time HMAC verify (`shared.portal_hmac.verify_sub`) → SOV
     recompute + assert vs the SIGNED §2.1 Contract Price (`money.sov_mismatches`) →
     Subcontract_Log collision double-check (`numbering.check_collision` — hand-issued
     subcontracts in transition) → ITS_Subcontractors SoR subcontractor snapshot (#494) →
     DETERMINISTIC render (`subcontract_docx.render_package` → **three** files: the
     Subcontract body `.docx` + the Exhibit A `.docx` + the Annex C Schedule-of-Values
     `.xlsx`) → Box (**three** uploads; §45 find-or-create the subcontract root's per-job
     folder — subcontract_naming.CFG_BOX_PORTAL_ROOT → <job>, the lane's OWN tree since
     2026-08-12; §47 version-on-conflict) → Subcontract_Log append +
     Subcontract_Pending_Review row (+ inline attach of all three files on review, ledger
     AND per-job rows, best-effort) → mark-filed receipt WITH box_file_id (the
     contract `.docx` id). The
     receipt is LAST: a crash anywhere before it re-pulls the row and every prior step
     is idempotent (version-on-conflict upload; Subcontract_Log/review-row dedupe by
     sc_number / sc_id). A bad-HMAC or SOV-mismatch row is ONE-SHOT-FLAGGED (CRITICAL +
     security Review-Queue row on first sighting, then skipped) — NEVER rendered, NEVER
     filed, NEVER marked; the row stays queued in D1 for forensics (the subcontract
     internal tier has no mark-rejected route by design).

     The `agreement_ymd` printed on the subcontract preamble is derived from the D1 row's
     immutable `created_at` (US/Pacific calendar date), NOT `datetime.now()`
     (BUILD_DECISIONS #1) — a re-render across a midnight boundary stays byte-identical,
     preserving the §47 version-on-conflict idempotency the render's OOXML clock pins to
     that date. The operator adjusts the printed preamble date in Word before signature if
     a different agreement date is wanted (the deliverable is an editable `.docx`).
  ② **Subcontractor down-sync pass** (`.subcontractors_sync_enabled`): full
     ITS_Subcontractors projection → POST subcontractors/sync (full-replace; the Worker's
     dirty-row fence protects un-mirrored portal edits; an EMPTY projection is REFUSED
     here too — a read-miss must never wipe the cache).
  ③ **Subcontractor up-sync pass** (same gate): GET subcontractors/pending → per
     subcontractor: bridge-key find-or-create into ITS_Subcontractors
     (`subcontractors.upsert_subcontractor`, column-scoped non-clobber) → mark-mirrored
     with the READ watermark (the Worker's in-WHERE version guard makes a racing portal
     edit win).
  ④ **Status pass** (`.status_sync_enabled`): read Subcontract_Pending_Review approve/SENT
     state → POST status-sync (approved BEFORE sent per the Worker's guarded batch) →
     mirror the stamps into Subcontract_Log (sent + Sent At; the superseded flip onto the
     predecessor row, resolved via the Notes sc_id join). D1 status is a display cache;
     F22 approval verification stays with the S4 send poller — this pass reports, it does
     not authorize. The `executed` (wet countersignature) terminal has no natural portal
     signal: an operator-set `executed` on Subcontract_Log is mirrored into the D1 display
     cache here (BUILD_DECISIONS #4), guarded server-side from `sent`.

Invariants
----------
- GENERATION-side of the External Send Gate (FM Invariant 1): AI-FREE and
  customer-SEND-FREE — no `anthropic*`, no `graph_client`/`send_mail`/`resend`/
  `smtplib`/`email.mime` (enrolled in tests/test_capability_gating.py GATED_SCRIPTS).
  All egress rides the F02-allowlisted `shared.portal_client` (our Worker) +
  `shared.box_client` (filing) + `shared.smartsheet_client` (SoR/ledger writes).
- Invariant 2: a /pending row is UNTRUSTED until its HMAC verifies; the money on the
  legal document is re-derived and asserted, never taken on faith; the subcontractor
  identity embedded in the .docx comes from the Smartsheet SoR, never the D1 cache.
- Kill-switch first (`@require_active`) + `@its_error_log`; observable config
  resolution (`REQUIRED_CONFIG` + `resolve_and_log`, #336); bearer privilege
  separation (`ITS_PORTAL_SUB_TOKEN` — the Worker's `requireSubToken` tier accepts no
  sibling token).

Failure modes
-------------
- PAUSED/MAINTENANCE → `@require_active` exits cleanly. ALL gates false (the shipped
  default) → pure no-op (no per-cycle log spam; the seeded ITS_Config rows are the
  operator's switches — scripts/migrations/seed_subcontracts_config.py).
- Missing base URL / bearer / HMAC secret → FAIL-CLOSED: no pass runs; CRITICAL
  (won't self-heal) + ERROR heartbeat.
- 401 anywhere → the SAME bearer fails every subcontract route, so the cycle STOPS:
  CRITICAL (`subcontract_bearer_rejected`) + ERROR heartbeat; everything stays
  queued/dirty.
- Per-item fences: PERMANENT (bad HMAC / SOV mismatch / collision / unknown
  subcontractor / TermsError / render / picklist / HTTP-400 validation) → Review-Queue
  row + one-shot flag (state `subcontract_poll_flagged.json` — delete an entry to retry
  after fixing the cause); TRANSIENT (SmartsheetError / BoxError / PortalTransportError)
  → ERROR-logged, row left queued/dirty, next cycle retries. One bad row never kills
  the cycle.

Consumers
---------
- launchd `org.solutionsmith.its.subcontract-poll` (StartInterval 120s default;
  RunAtLoad).
- Watchdog Check C marker (`subcontract_poll`) + ITS_Daemon_Health row
  (shared.heartbeat).
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
from typing import Any, cast
from zoneinfo import ZoneInfo

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
from subcontracts import (
    governing_law,
    money,
    numbering,
    subcontract_docx,
    subcontract_generate,
    subcontract_log,
    subcontract_naming,
    subcontract_review,
    subcontractors,
)
from subcontracts import terms as terms_lib

SCRIPT_NAME = "subcontracts.subcontract_poll"
WORKSTREAM = "subcontracts"

# ITS_Config keys (all read under Workstream='subcontracts' except the two SHARED
# safety_reports-owned keys — same ownership pattern as po_poll / fieldops_sync).
CFG_POLLING_ENABLED = "subcontracts.subcontract_poll.polling_enabled"
CFG_SUBS_SYNC_ENABLED = "subcontracts.subcontract_poll.subcontractors_sync_enabled"
CFG_STATUS_SYNC_ENABLED = "subcontracts.subcontract_poll.status_sync_enabled"
CFG_WORKER_BASE_URL = "safety_reports.portal.worker_base_url"  # shared with portal_poll / po_poll
CFG_WORKER_BASE_URL_WORKSTREAM = "safety_reports"

# Keychain entry names (NOT secrets). The subcontract bearer mirrors the Worker's
# PORTAL_SUB_API_TOKEN (privilege-separated from every sibling tier); the HMAC secret is
# the SAME payload secret the Worker signs with (domain separation via `sub:v1`, not key
# separation, isolates the subcontract protocol).
KC_SUB_TOKEN = "ITS_PORTAL_SUB_TOKEN"  # noqa: S105 — Keychain entry NAME, not a secret
KC_HMAC_SECRET = "ITS_PORTAL_HMAC_SECRET"  # noqa: S105 — Keychain entry NAME, REUSED from PO

DEFAULT_POLLING_ENABLED = False       # ships dark; the operator flips the seeded row
DEFAULT_SUBS_SYNC_ENABLED = False     # ships dark
DEFAULT_STATUS_SYNC_ENABLED = False   # ships dark
POLL_INTERVAL_SECONDS = 120  # registration metadata; mirrors the launchd StartInterval.
# 120s (vs po_poll 90, portal_poll 60) both staggers off the sibling daemons and suits
# the low subcontract volume.

# The per-job Smartsheet tracking sheet name (Feature A). Lives inside the job's
# folder under sheet_ids.FOLDER_SC_JOBS; structure-cloned from the flat
# Subcontract_Log by shared/job_sheet.ensure_job_sheet. (The Box side needs no
# matching subfolder any more: subcontract documents file DIRECTLY in the
# subcontract root's per-job folder — subcontract_naming.CFG_BOX_PORTAL_ROOT → <job>.)
PERJOB_SHEET_NAME = "Subcontracts"

_PACIFIC = ZoneInfo("America/Los_Angeles")  # the agreement date is Pacific wall-clock

# #336 — every ITS_Config key this daemon resolves at RUNTIME. The declared-but-not-
# runtime-read *.poll_interval_seconds key is deliberately EXCLUDED (install.sh bakes it
# into the plist; the daemon never reads it) — same posture as po_poll / fieldops_sync.
REQUIRED_CONFIG: list[ConfigKey] = [
    ConfigKey(CFG_POLLING_ENABLED, WORKSTREAM, DEFAULT_POLLING_ENABLED, "bool"),
    ConfigKey(CFG_SUBS_SYNC_ENABLED, WORKSTREAM, DEFAULT_SUBS_SYNC_ENABLED, "bool"),
    ConfigKey(CFG_STATUS_SYNC_ENABLED, WORKSTREAM, DEFAULT_STATUS_SYNC_ENABLED, "bool"),
    ConfigKey(
        CFG_WORKER_BASE_URL, CFG_WORKER_BASE_URL_WORKSTREAM, "", "str",
        description="Shared Worker base URL; owned by safety_reports, read here too.",
    ),
    ConfigKey(
        subcontract_naming.CFG_BOX_PORTAL_ROOT,
        subcontract_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM, "", "str",
        description=(
            "The subcontract lane's OWN Box mirror-tree root ('ITS Subcontracts' — built "
            "by build_box_roots.py). The drafts pass files the .docx/.xlsx package + the "
            "send ZIP under ROOT→<job>; also the archive's box:subcontracts source."
        ),
    ),
]

# State paths. HEARTBEAT_ROW_STATE_PATH is the SHARED row-id cache (ARCH-2).
STATE_DIR = Path.home() / "its" / "state"
HEARTBEAT_PATH = STATE_DIR / "subcontract_poll_heartbeat.txt"
LOCK_PATH = STATE_DIR / "subcontract_poll.lock"
# Sustained pending-fetch escalation (2026-07-20 forensic: a 21h every-cycle ERROR storm
# was invisible on every CRITICAL-keyed fire surface — shared/sustained_failure.py).
_FETCH_FAILS = sustained_failure.SustainedFailureCounter(
    STATE_DIR / "subcontract_pending_fetch_failures.json", SCRIPT_NAME, "subcontract_pending_fetch_counter_failed",
)
HEARTBEAT_ROW_STATE_PATH = STATE_DIR / "heartbeat_row_ids.json"
# One-shot flag state for PERMANENTLY-refused pending rows (`{sc_id: reason}`).
# A flagged row is skipped every subsequent cycle (no 120s Review-Queue spam); the
# operator remediates by fixing the cause and deleting the entry (or the file).
SUBCONTRACT_FLAGGED_PATH = STATE_DIR / "subcontract_poll_flagged.json"
MAX_SUBCONTRACT_FLAGS = 500  # drained/settled entries are dead weight only — cap the file

DAEMON_NAME = "subcontracts.subcontract_poll"
_REGISTRATION_SOURCE_ID = "Safety Portal Worker /api/subcontracts/internal/pending"

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
WATCHDOG_JOB_SLUG = "subcontract_poll"


@dataclass(frozen=True)
class SubcontractPollStats:
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
    filed: int = 0        # subcontracts fully filed + receipted this cycle
    rejected: int = 0     # bad-HMAC refusals (first sighting)
    fenced: int = 0       # permanent Review-Queue fences (SOV/collision/subcontractor/terms/…)
    skipped_flagged: int = 0  # rows already one-shot-flagged in a prior cycle
    draft_errors: int = 0     # transient failures (left queued)
    # ②③ subcontractor passes
    subs_downsynced: int = 0
    subs_upsynced: int = 0
    subs_reviewed: int = 0
    sub_errors: int = 0
    # ④ status pass
    status_synced: int = 0
    status_errors: int = 0


class _BearerRejectedError(Exception):
    """Internal: a 401 anywhere — the SAME bearer fails every subcontract route, stop the cycle."""


@dataclass(frozen=True)
class _SubcontractCreds:
    """Resolved credentials with NAMED fields (the portal_poll CodeQL taint rationale:
    named fields keep the bearer/secret taint off base_url and everything logged)."""

    base_url: str
    bearer: str
    secret: str


# ---- Config readers (replicated per preservation, mirror po_poll) ---------------


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


def _subs_sync_enabled() -> bool:
    return _read_bool_setting(CFG_SUBS_SYNC_ENABLED, DEFAULT_SUBS_SYNC_ENABLED)


def _status_sync_enabled() -> bool:
    return _read_bool_setting(CFG_STATUS_SYNC_ENABLED, DEFAULT_STATUS_SYNC_ENABLED)


# ---- Lock + heartbeat + marker seams (mirror po_poll) ---------------------------


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
    """Touch the Check C freshness marker for this run (mirror po_poll)."""
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
    """Load the one-shot flag set `{sc_id: reason}`. {} on any read error (fail-open:
    the only cost is one redundant re-flag, never a missed alert)."""
    if not SUBCONTRACT_FLAGGED_PATH.exists():
        return {}
    try:
        parsed = json.loads(SUBCONTRACT_FLAGGED_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _persist_flags(flags: dict[str, str]) -> None:
    """Atomically persist the flag set (capped). Lock-timeout fails OPEN with a WARN —
    a lost flag set costs a duplicate Review-Queue flag next cycle, never a missed one."""
    if len(flags) > MAX_SUBCONTRACT_FLAGS:
        flags = dict(list(flags.items())[-MAX_SUBCONTRACT_FLAGS:])
    SUBCONTRACT_FLAGGED_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with state_io.with_path_lock(SUBCONTRACT_FLAGGED_PATH):
            state_io.atomic_write_json(SUBCONTRACT_FLAGGED_PATH, flags)
    except state_io.StateLockTimeoutError:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"could not acquire lock on {SUBCONTRACT_FLAGGED_PATH} after retries; "
            f"subcontract flag set not persisted",
            error_code="subcontract_flags_persist_failed",
        )


# ---- Credential resolution (fail-CLOSED) -----------------------------------------


def _resolve_credentials() -> _SubcontractCreds | TransientUnavailable | None:
    """Resolve (base_url, bearer, secret) fail-CLOSED, three ways.

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
        bearer = keychain.get_secret(KC_SUB_TOKEN)
    except keychain.KeychainError:
        bearer = ""
    try:
        secret = keychain.get_secret(KC_HMAC_SECRET)
    except keychain.KeychainError:
        secret = ""
    if not (base_url and bearer and secret):
        return None
    return _SubcontractCreds(base_url=base_url, bearer=bearer, secret=secret)


# ---- Public API -------------------------------------------------------------------


@its_error_log(SCRIPT_NAME)
@require_active
def poll_once() -> SubcontractPollStats:
    """Run one multi-pass subcontract cycle. launchd invokes this once per StartInterval;
    idempotent across crashes (see the module docstring's receipt-is-last design)."""
    # #336 startup observability (after @require_active, fail-open — never blocks).
    resolve_and_log(SCRIPT_NAME, REQUIRED_CONFIG)

    drafts_on = _polling_enabled()
    subs_on = _subs_sync_enabled()
    status_on = _status_sync_enabled()
    if not (drafts_on or subs_on or status_on):
        # Shipped default (ALL gates false) — an intentional dark state, not an
        # anomaly: no heartbeat/marker/log spam every 120s. The seeded ITS_Config
        # rows are the operator's switches.
        return SubcontractPollStats(skipped_disabled=True)

    with _file_lock(LOCK_PATH) as acquired:
        if not acquired:
            error_log.log(
                Severity.INFO, SCRIPT_NAME,
                "another poll cycle holds the lock; skipping this cycle",
                error_code="subcontract_poll_lock_held",
            )
            return SubcontractPollStats(skipped_locked=True)
        try:
            return _poll_inside_lock(drafts_on, subs_on, status_on)
        finally:
            # D3 — see portal_poll: one summarized WARN row per pass that recovered on retry.
            sustained_failure.flush_retry_recovery(SCRIPT_NAME)


def _poll_inside_lock(drafts_on: bool, subs_on: bool, status_on: bool) -> SubcontractPollStats:
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
            f"subcontract Worker base URL temporarily unreadable ({creds.reason}) — "
            f"skipping this cycle; will retry next interval (transient, self-heals)",
            error_code="subcontract_creds_transient",
        )
        _write_heartbeat()
        _write_heartbeat_row(status="WARN", items_processed=0,
                             error_summary=f"base URL unreadable ({creds.reason}) — transient")
        return SubcontractPollStats(halted_transient=True)
    if creds is None:
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            (
                # Deliberately does NOT interpolate the ITS_Config key or the Keychain
                # entry NAMES (secret-store names in a log are a CodeQL clear-text trip).
                "fail-closed: missing subcontract portal credentials — the Worker base "
                "URL (ITS_Config) and/or the subcontract bearer + HMAC-secret Keychain "
                "entries are unset; NOT polling until fixed"
            ),
            error_code="subcontract_creds_missing",
        )
        _write_heartbeat()
        _write_heartbeat_row(status="ERROR", items_processed=0,
                             error_summary="fail-closed: subcontract portal credentials missing")
        return SubcontractPollStats(halted_no_creds=True)

    counters: dict[str, int] = {
        "drafts_scanned": 0, "filed": 0, "rejected": 0, "fenced": 0,
        "skipped_flagged": 0, "draft_errors": 0,
        "subs_downsynced": 0, "subs_upsynced": 0, "subs_reviewed": 0,
        "sub_errors": 0, "status_synced": 0, "status_errors": 0,
    }
    bearer_rejected = False
    try:
        if drafts_on:
            _drafts_pass(creds, counters)
        if subs_on:
            _subcontractor_down_sync_pass(creds, counters)
            _subcontractor_up_sync_pass(creds, counters)
        if status_on:
            _status_pass(creds, counters)
    except _BearerRejectedError:
        # A 401 anywhere: the SAME bearer fails every subcontract route, so nothing else
        # can work this cycle. A bad/rotated bearer will NOT self-heal → page.
        bearer_rejected = True
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            "subcontract bearer UNAUTHORIZED (401) — rejected by the Worker's "
            "requireSubToken tier; cycle STOPPED until the token is fixed (everything "
            "stays queued/dirty — safe re-attempt)",
            error_code="subcontract_bearer_rejected",
        )

    _write_heartbeat()
    total_errors = counters["draft_errors"] + counters["sub_errors"] + counters["status_errors"]
    # `skipped_flagged` counts items ALREADY one-shot-flagged — permanently wedged out
    # of the queue until an operator fixes the cause and clears the entry. Omitting it
    # made Last Cycle Status flip back to OK on the very next cycle, so the daemon read
    # healthy while an item sat fenced indefinitely (2026-08-10, rfq_poll). A standing
    # fence is a standing WARN by design: ITS_Daemon_Health is a visibility surface, not
    # a push surface, so this pages nobody — and it self-clears when the flag is cleared.
    total_flagged = (
        counters["rejected"] + counters["fenced"] + counters["subs_reviewed"]
        + counters["skipped_flagged"]
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
                counters["filed"] + counters["subs_upsynced"] + counters["status_synced"]
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
            f"subcontract cycle: drafts scanned={counters['drafts_scanned']} "
            f"filed={counters['filed']} rejected={counters['rejected']} "
            f"fenced={counters['fenced']} flag-skipped={counters['skipped_flagged']} "
            f"errors={counters['draft_errors']}; subcontractors "
            f"down={counters['subs_downsynced']} up={counters['subs_upsynced']} "
            f"reviewed={counters['subs_reviewed']} errors={counters['sub_errors']}; "
            f"status synced={counters['status_synced']} errors={counters['status_errors']}"
        ),
        error_code="subcontract_cycle_summary",
    )
    return SubcontractPollStats(
        bearer_rejected=bearer_rejected,
        drafts_scanned=counters["drafts_scanned"],
        filed=counters["filed"],
        rejected=counters["rejected"],
        fenced=counters["fenced"],
        skipped_flagged=counters["skipped_flagged"],
        draft_errors=counters["draft_errors"],
        subs_downsynced=counters["subs_downsynced"],
        subs_upsynced=counters["subs_upsynced"],
        subs_reviewed=counters["subs_reviewed"],
        sub_errors=counters["sub_errors"],
        status_synced=counters["status_synced"],
        status_errors=counters["status_errors"],
    )


# ---- ① Drafts pass ----------------------------------------------------------------


def _drafts_pass(creds: _SubcontractCreds, counters: dict[str, int]) -> None:
    """Drain the queued-subcontract queue: verify → assert → render → file → receipt."""
    try:
        pending = portal_client.get_pending_subcontracts(creds.base_url, creds.bearer)
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
                f"(rows left queued; see docs/runbooks/subcontract_generation_path.md): {exc!r}",
                error_code="subcontract_pending_fetch_sustained",
            )
        else:
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"failed to GET subcontract pending (rows left queued for next cycle); {n} consecutive: {exc!r}",
            error_code="subcontract_pending_fetch_failed",
            )
        return
    _FETCH_FAILS.reset()  # a successful fetch clears the sustained-outage counter
    if not pending:
        return

    # Per-CYCLE config resolution: the Contractor identity (the review email body seed +
    # the render's contractor_entity fallback). Unlike PO there is NO tax/rates table —
    # the subcontract money model has no tax; `money.sov_mismatches` needs no rates. A
    # broken config file is a deploy defect — abort the pass loudly, leave every row
    # queued. (The render loads contractor internally too, so it is self-contained.)
    try:
        contractor = terms_lib.load_contractor_config()
    except terms_lib.TermsError as exc:
        counters["draft_errors"] += 1
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            f"contractor config unreadable — drafts pass ABORTED (rows left queued): {exc}",
            error_code="subcontract_config_unreadable",
        )
        return

    flags = _load_flags()
    flags_dirty = False
    for row in pending:
        counters["drafts_scanned"] += 1
        if _process_pending_subcontract(row, creds, counters, flags, contractor):
            flags_dirty = True
    if flags_dirty:
        _persist_flags(flags)


def _process_pending_subcontract(
    row: dict[str, Any],
    creds: _SubcontractCreds,
    counters: dict[str, int],
    flags: dict[str, str],
    contractor: dict[str, Any],
) -> bool:
    """Verify + assert + render + file + receipt ONE pending subcontract. Returns True
    iff the one-shot flag set was mutated (the caller persists once per cycle)."""
    raw_id = row.get("id")
    if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0:
        counters["draft_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"pending subcontract row has a missing/malformed id ({raw_id!r}); skipping",
            error_code="subcontract_row_no_id",
        )
        return False
    sc_id = raw_id
    key = str(sc_id)
    if key in flags:
        counters["skipped_flagged"] += 1
        return False

    sc_number = str(row.get("sc_number") or "")
    # Split the HMAC off the row IMMEDIATELY (the portal_poll hygiene: an integrity tag
    # has no business traveling into render/filing/logs).
    provided_hmac = str(row.get("hmac") or "")
    subcontract = {k: v for k, v in row.items() if k != "hmac"}
    raw_lines = subcontract.get("sov_lines")
    sov_lines = [ln for ln in raw_lines if isinstance(ln, dict)] if isinstance(raw_lines, list) else []
    correlation_id = uuid.uuid4().hex[:12]

    # 1 — HMAC verify (Invariant 2 downgrade defense; constant-time). The canonical is
    # rebuilt from the D1 row VERBATIM (sub_key signed, subcontractor_entity NOT present);
    # the SoR-injected render-only fields below are added AFTER this and are NOT canonical
    # keys, so they never affect the signature (the two-dict discipline, data_contract A.4).
    try:
        canonical = portal_hmac.sub_canonical_json(subcontract, sov_lines)
    except ValueError as exc:
        # NaN/Infinity in a money field — malformed beyond what the Worker could ever
        # have signed. Permanent.
        counters["fenced"] += 1
        _fence_subcontract(sc_id, sc_number, f"canonical serialization failed: {exc}",
                           "subcontract_canonical_invalid", correlation_id, flags, "canonical")
        return True
    if not portal_hmac.verify_sub(
        creds.secret, provided_hmac,
        sc_id=sc_id, sc_number=sc_number, canonical_json=canonical,
    ):
        counters["rejected"] += 1
        _handle_subcontract_hmac_failure(sc_id, sc_number, subcontract, correlation_id, flags)
        return True

    # 2 — SOV recompute + assert vs the SIGNED §2.1 Contract Price (never render a number
    # we did not re-derive). `money.sov_mismatches` RETURNS (never raises): a malformed
    # SOV row is a mismatch string, not a daemon-killing exception.
    # sov_mismatches guards a non-int/None contract price at runtime (RETURNS a mismatch,
    # never raises) — cast (not `or 0`) so a None fences rather than masquerading as 0.
    mismatches = money.sov_mismatches(cast(int, subcontract.get("contract_price_cents")), sov_lines)
    if mismatches:
        counters["fenced"] += 1
        _fence_subcontract(
            sc_id, sc_number,
            f"SOV recompute disagrees with the signed contract price: {mismatches}",
            "subcontract_sov_mismatch", correlation_id, flags, "sov",
            reason=review_queue.ReviewReason.MISMATCHED_REFERENCE,
        )
        return True

    try:
        # 3 — Subcontract_Log collision double-check (hand-issued subcontracts in transition).
        collision = numbering.check_collision(sc_number, sc_id)
        if collision is not None:
            counters["fenced"] += 1
            _fence_subcontract(
                sc_id, sc_number,
                f"SC number already in Subcontract_Log and not ours ({collision}) — a "
                f"hand-issued subcontract or ledger defect; NOT filing a duplicate number",
                "subcontract_number_collision", correlation_id, flags, "collision",
                reason=review_queue.ReviewReason.MISMATCHED_REFERENCE,
            )
            return True

        # 4 — subcontractor snapshot from the ITS_Subcontractors SoR at render time (#494).
        # REQUIRED (not just preference): migration 0050 has NO subcontractor_entity column
        # (only sub_key), yet subcontract_generate._REQUIRED_FIELDS needs it — without this
        # injection render_package raises. The identity comes from the Smartsheet SoR, never
        # the D1 cache; the HMAC covers only sub_key.
        sub_key = str(subcontract.get("sub_key") or "")
        subcontractor = subcontractors.get_subcontractor_by_key(sub_key)
        if subcontractor is None:
            counters["fenced"] += 1
            _fence_subcontract(
                sc_id, sc_number,
                f"subcontractor {sub_key!r} not found in ITS_Subcontractors (the SoR) — "
                f"cannot embed a Subcontractor identity; fix the row, then clear this "
                f"entry from {SUBCONTRACT_FLAGGED_PATH.name} to retry",
                "subcontract_sub_unknown", correlation_id, flags, "subcontractor",
            )
            return True
        subcontractor_name = str(subcontractor.get(subcontractors.COL_SUB_NAME) or "")
        subcontract["subcontractor_entity"] = subcontractor_name

        # 4b — agreement date: derived from the D1 row's IMMUTABLE created_at (Pacific
        # calendar date), NOT datetime.now() (BUILD_DECISIONS #1). A stable date keeps the
        # render byte-identical across a crash→retry midnight boundary (§47 idempotency —
        # subcontract_docx pins its OOXML clock to agreement_ymd). A row missing created_at
        # is a Worker /pending SELECT defect — fence permanently rather than reintroduce a
        # non-deterministic now() date.
        created_at = subcontract.get("created_at")
        if not isinstance(created_at, int) or isinstance(created_at, bool) or created_at <= 0:
            counters["fenced"] += 1
            _fence_subcontract(
                sc_id, sc_number,
                "D1 row carries no usable created_at — cannot derive a stable agreement "
                "date (the Worker /pending SELECT must return created_at); NOT rendering "
                "with a non-deterministic date",
                "subcontract_created_at_missing", correlation_id, flags, "created_at",
            )
            return True
        sc_date = datetime.fromtimestamp(created_at, _PACIFIC).date()
        subcontract["agreement_ymd"] = [sc_date.year, sc_date.month, sc_date.day]
        # Contractor identity for the docx core-property author (also render-only,
        # unsigned; the render falls back to its own default if absent).
        subcontract["contractor_entity"] = str(contractor.get("entity") or "")

        # 5 — terms pins threaded into render_package (it loads + sha-verifies + Layer-A-
        # gates + token-fills INTERNALLY — no separate resolve_terms call, unlike PO).
        # An empty terms_profile_id maps to the standard profile defensively (the Worker
        # already defaults it at draft-save, BUILD_DECISIONS #5).
        terms_profile_id = str(subcontract.get("terms_profile_id") or "standard_subcontract")
        terms_version = str(subcontract.get("terms_version") or "") or None

        # 6 — predecessor number for the supersession clause + ledger display.
        supersedes_sc_id = subcontract.get("supersedes_sc_id")
        supersedes_display: str | None = None
        if isinstance(supersedes_sc_id, int) and not isinstance(supersedes_sc_id, bool):
            supersedes_display = subcontract_log.find_sc_number_by_d1_id(supersedes_sc_id)

        # 7 — deterministic render → the editable package (TWO files, keyed by filename).
        package = subcontract_docx.render_package(
            subcontract, sov_lines,
            terms_profile_id=terms_profile_id, terms_version=terms_version,
        )
        docx_bytes = package["Subcontract.docx"]
        exhibit_bytes = package["Exhibit A.docx"]
        xlsx_bytes = package["Annex C - Schedule of Values.xlsx"]

        # 8 — Box filing: §45 find-or-create the subcontract root's per-job folder, §47
        # version-on-conflict under the deterministic name. THREE uploads (the .docx
        # contract + the Exhibit A .docx + the .xlsx SOV); the contract .docx id is the
        # primary receipt.
        folder_id = _resolve_subcontract_box_folder(str(subcontract.get("job_name") or ""))
        if folder_id is None:
            counters["draft_errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"Subcontract Box root unresolved (ITS_Config "
                f"{subcontract_naming.CFG_BOX_PORTAL_ROOT} unset) — subcontract {sc_number} "
                f"left queued until the root is configured",
                error_code="subcontract_box_root_unresolved",
                correlation_id=correlation_id,
            )
            return False
        # The Box filenames MUST embed sc_number: two subcontracts on the same job filed
        # under the render dict's generic keys ("Subcontract.docx") would collide in the
        # job-scoped folder and upload_bytes_or_new_version would overwrite one as a
        # version of the other → silent contract loss (BUILD_DECISIONS #2). The job name is
        # treated as STABLE (a subcontract's job is fixed once allocated); a job rename in
        # the crash→retry window would (narrowly) yield a second Box file rather than a new
        # version — a recoverable duplicate, never data loss (§47).
        job_name = subcontract.get("job_name")
        docx_name = subcontract_naming.sc_docx_filename(sc_number, job_name)
        exhibit_name = subcontract_naming.sc_exhibit_filename(sc_number, job_name)
        xlsx_name = subcontract_naming.sc_xlsx_filename(sc_number, job_name)
        docx_info = box_client.upload_bytes_or_new_version(folder_id, docx_name, docx_bytes)
        box_client.upload_bytes_or_new_version(folder_id, exhibit_name, exhibit_bytes)
        box_client.upload_bytes_or_new_version(folder_id, xlsx_name, xlsx_bytes)
        box_file_id = str(docx_info["id"])
        box_link = f"https://app.box.com/file/{box_file_id}"
        # SC-S4 send artifact (2026-07-15 operator decision): the whole signable package as ONE
        # DETERMINISTIC ZIP, filed alongside the editable files (§47 version-on-conflict — a
        # fixed record re-zips byte-identical, so no redundant upload). The review row's
        # "Compiled PDF" slot links THIS so the shared single-attachment send engine transmits
        # the complete package (body + Exhibit A + Annex C SoV) unchanged. The three editable
        # .docx/.xlsx stay in Box (and inline-attached to the review row below) for the operator
        # to review / hand-edit before approving; the ledger receipt stays the contract .docx.
        zip_name = subcontract_naming.sc_package_zip_filename(sc_number, job_name)
        zip_bytes = subcontract_docx.zip_package(package, sc_date)
        zip_info = box_client.upload_bytes_or_new_version(folder_id, zip_name, zip_bytes)
        zip_link = f"https://app.box.com/file/{zip_info['id']}"

        # 9 — Subcontract_Log append (idempotent: the collision check above proved any
        # existing row is OURS — a crash-retry — so only append when absent).
        ledger_row_kwargs: dict[str, Any] = {
            "sc_number": sc_number,
            "job_project": f"{subcontract.get('job_no')} — {subcontract.get('job_name')}",
            "job_id": str(subcontract.get("job_id") or ""),
            "subcontractor_name": subcontractor_name,
            "sub_key": sub_key,
            "total_cents": int(subcontract.get("contract_price_cents") or 0),
            "pdf_link": box_link,
            "supersedes_display": supersedes_display or "",
            "terms_profile": terms_profile_id,
            "created_by": str(subcontract.get("created_by") or ""),
            "created_at_iso": sc_date.isoformat(),
            "notes": subcontract_log.notes_for_filed_row(sc_id),
        }
        package_files = [
            (docx_name, docx_bytes), (exhibit_name, exhibit_bytes), (xlsx_name, xlsx_bytes),
        ]
        existing_ledger = subcontract_log.find_row_by_sc_number(sc_number)
        if existing_ledger is None:
            ledger_row_id = subcontract_log.append_filed_row(**ledger_row_kwargs)
        else:
            ledger_row_id = int(existing_ledger["_row_id"])
        # Inline attach of all three files on the ledger row (PO/RFQ-ledger parity,
        # operator ask 2026-08-12 — the review row already carried them; the ledger row
        # now does too). On EVERY service, not fresh-append-only: attach_pdf_to_row
        # REPLACES a same-filename attachment and the names are deterministic per
        # subcontract, so this is duplicate-free — and an attach that failed in a cycle
        # whose receipt also failed SELF-HEALS on the re-serve.
        _attach_files_best_effort(
            ledger_row_id, package_files, correlation_id, sheet_id=subcontract_log.SHEET_ID,
        )

        # 9b — per-job tracking sheet mirror (Feature A), BEST-EFFORT: the same
        # ledger row into "<Jobs>/<job>/Subcontracts" (find-or-create; independently
        # idempotent per target sheet), now WITH the inline package files (operator
        # ask 2026-08-12). Fenced inside the helper — a per-job failure must NEVER
        # fail the filing (Box + the flat Subcontract_Log are the SoR).
        _append_perjob_row_best_effort(
            str(subcontract.get("job_name") or ""), ledger_row_kwargs, correlation_id,
            files=package_files,
        )

        # 10 — Subcontract_Pending_Review row (idempotent via the Notes sc_id join) + the
        # inline attach of ALL THREE files (best-effort — Box is the SoR). The attach runs
        # on EVERY service, not fresh-creation-only (adversarial review 2026-08-12): a crash
        # between add_sc_review_row and the attach used to leave the review row — the surface
        # the approver actually reads — permanently missing its inline copies, because the
        # re-serve found the row and skipped the whole guarded block. Same self-heal posture
        # as the ledger/per-job rows (deterministic names → replace, never duplicate).
        existing_review = subcontract_review.find_row_by_sc_id(sc_id)
        if existing_review is not None:
            review_row_id = int(existing_review["_row_id"])
        else:
            email_body = subcontract_review.sc_email_body_template(
                contact_name=str(subcontractor.get(subcontractors.COL_CONTACT_NAME) or ""),
                sc_number=sc_number,
                job_name=str(subcontract.get("job_name") or ""),
                contractor_entity=str(contractor.get("entity") or ""),
            )
            review_row_id = subcontract_review.add_sc_review_row(
                job_project=f"{subcontract.get('job_no')} — {subcontract.get('job_name')}",
                sub_key=sub_key,
                agreement_date=sc_date,
                package_link=zip_link,  # "Compiled PDF" slot ← the Subcontract Package.zip (SC-S4 send artifact)
                recipient_to=str(subcontractor.get(subcontractors.COL_CONTACT_EMAIL) or ""),
                cc_display="",  # the SoR has no subcontractor CC list — recipient is one sub
                email_body=email_body,
                notes=subcontract_review.notes_for_review_row(
                    sc_id, sc_number,
                    supersedes_sc_id=supersedes_sc_id
                    if isinstance(supersedes_sc_id, int) and not isinstance(supersedes_sc_id, bool)
                    else None,
                ),
            )
        _attach_files_best_effort(review_row_id, package_files, correlation_id)

        # 11 — the receipt, LAST (queued→pending_review; a crash before this line re-pulls
        # the row and every step above is idempotent).
        portal_client.mark_subcontract_filed(
            creds.base_url, creds.bearer, sc_id=sc_id, box_file_id=box_file_id
        )
        counters["filed"] += 1
        error_log.log(
            Severity.INFO, SCRIPT_NAME,
            f"filed subcontract {sc_number} (sc_id={sc_id}) → Box + Subcontract_Log + "
            f"Subcontract_Pending_Review",
            error_code="subcontract_filed",
            correlation_id=correlation_id,
        )
        return False
    except portal_client.PortalAuthError as exc:
        raise _BearerRejectedError from exc
    except terms_lib.TermsError as exc:
        counters["fenced"] += 1
        _fence_subcontract(sc_id, sc_number, f"terms resolution failed: {exc}",
                           "subcontract_terms_error", correlation_id, flags, "terms")
        return True
    except subcontract_docx.SubcontractDocxError as exc:
        # The single fence for SOV mismatch / bad shape / unfilled token / uncleared
        # Layer-A terms / unknown state — render_package wraps them all.
        counters["fenced"] += 1
        _fence_subcontract(sc_id, sc_number, f"render failed: {exc}",
                           "subcontract_render_failed", correlation_id, flags, "render")
        return True
    except (
        subcontract_generate.SubcontractGenerateError,
        money.MoneyError,
        governing_law.GoverningLawError,
    ) as exc:
        # Belt-and-suspenders: render_package already wraps these into
        # SubcontractDocxError, but a direct call path could raise them.
        counters["fenced"] += 1
        _fence_subcontract(sc_id, sc_number,
                           f"render failed ({type(exc).__name__}): {exc}",
                           "subcontract_render_failed", correlation_id, flags, "render")
        return True
    except (
        picklist_validation.PicklistViolationError,
        smartsheet_client.SmartsheetValidationError,
    ) as exc:
        counters["fenced"] += 1
        _fence_subcontract(sc_id, sc_number,
                           f"permanent write reject ({type(exc).__name__}): {exc}",
                           "subcontract_permanent_reject", correlation_id, flags, "permanent")
        return True
    except (
        smartsheet_client.SmartsheetError,
        box_client.BoxError,
        portal_client.PortalTransportError,
    ) as exc:
        counters["draft_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"transient failure filing subcontract {sc_number} (sc_id={sc_id}; left "
            f"queued for next cycle): {type(exc).__name__}: {exc!r}",
            error_code="subcontract_filing_transient",
            correlation_id=correlation_id,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — per-row fence; one bad subcontract never kills the cycle
        counters["draft_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"unexpected failure filing subcontract {sc_number} (sc_id={sc_id}; left "
            f"queued): {type(exc).__name__}: {exc!r}",
            error_code="subcontract_filing_unexpected",
            correlation_id=correlation_id,
        )
        return False


def _handle_subcontract_hmac_failure(
    sc_id: int, sc_number: str, subcontract: dict[str, Any],
    correlation_id: str, flags: dict[str, str],
) -> None:
    """Reject a bad-HMAC subcontract row — the subcontract twin of po_poll's handler.

    NEVER rendered, NEVER filed, NEVER mark-filed (the downgrade defense; the row stays
    queued in D1 for forensics — the subcontract tier has no mark-rejected route by
    design). One-shot: anomaly-log + security Review-Queue row + CRITICAL fire only on
    the FIRST sighting; the flag set suppresses per-cycle re-flag spam."""
    # Tripwire (Invariant 2, Layer 5) — record the suspicious pattern.
    anomaly_logger.check({"subcontract_hmac_failure": sc_id, "sc_number": sc_number})
    review_queue.safe_add(
        script_name=SCRIPT_NAME,
        workstream=WORKSTREAM,
        summary=(
            f"subcontract: HMAC verification FAILED for SC {sc_number or sc_id} — "
            f"rejected, NOT rendered or filed"
        ),
        payload={
            "sc_id": sc_id,
            "sc_number": sc_number,
            "job_no": subcontract.get("job_no"),
            "sub_key": subcontract.get("sub_key"),
            # The HMAC value is deliberately NOT recorded (signature material — same
            # posture as the submission twin); the raw row stays in D1.
        },
        sla_tier=review_queue.SlaTier.SUBCONTRACT_DRAFT,
        reason=review_queue.ReviewReason.SECURITY_TRIGGER,
        severity=Severity.CRITICAL,
        source_file=f"subcontract:{sc_id}",
        security_flag=True,
    )
    error_log.log(
        Severity.CRITICAL, SCRIPT_NAME,
        (
            f"subcontract HMAC FAIL sc_id={sc_id} sc_number={sc_number!r} — rejected, "
            f"not rendered or filed (downgrade defense)"
        ),
        error_code="subcontract_hmac_failure",
        correlation_id=correlation_id,
    )
    flags[str(sc_id)] = "hmac"


def _fence_subcontract(
    sc_id: int,
    sc_number: str,
    detail: str,
    error_code: str,
    correlation_id: str,
    flags: dict[str, str],
    flag_reason: str,
    *,
    reason: review_queue.ReviewReason = review_queue.ReviewReason.POLICY_EDGE,
) -> None:
    """Route a PERMANENTLY-refused pending subcontract to the Review Queue + one-shot flag.

    The row is never filed and never mark-filed; it stays queued in D1. Remediation:
    fix the cause, then delete the sc_id entry from `subcontract_poll_flagged.json` (the
    daemon retries it the next cycle)."""
    review_queue.safe_add(
        script_name=SCRIPT_NAME,
        workstream=WORKSTREAM,
        summary=f"subcontract: SC {sc_number or sc_id} refused before filing — {detail}",
        payload={"sc_id": sc_id, "sc_number": sc_number, "detail": detail},
        sla_tier=review_queue.SlaTier.SUBCONTRACT_DRAFT,
        reason=reason,
        severity=Severity.WARN,
        source_file=f"subcontract:{sc_id}",
    )
    error_log.log(
        Severity.WARN, SCRIPT_NAME,
        f"subcontract fenced (permanent, {error_code}) sc_id={sc_id} sc_number={sc_number!r}: {detail}",
        error_code=error_code,
        correlation_id=correlation_id,
    )
    flags[str(sc_id)] = flag_reason


def _resolve_subcontract_box_folder(job_name: str) -> str | None:
    """§45 find-or-create the subcontract filing folder: the lane's OWN Box ROOT
    (`subcontract_naming.CFG_BOX_PORTAL_ROOT`) → per-job folder (the SAME
    `safety_naming.job_folder_name` as every other portal artifact). The package
    files DIRECTLY in the job folder. None when the root is unconfigured (the
    caller leaves the subcontract queued + ERRORs — a config gap, not a
    per-subcontract defect)."""
    root = _read_str_setting(
        subcontract_naming.CFG_BOX_PORTAL_ROOT, "",
        workstream=subcontract_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM,
    ).strip()
    if not root:
        return None
    return box_client.get_or_create_folder(root, safety_naming.job_folder_name(job_name))


_ATTACH_CONTENT_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _attach_files_best_effort(
    row_id: int, files: list[tuple[str, bytes]], correlation_id: str,
    *, sheet_id: int | None = None,
) -> None:
    """Attach the rendered package files inline on a Smartsheet row, BEST-EFFORT (Box is
    the SoR; a failure is a WARN that never fails the filing — mirror po_poll). All three —
    the Subcontract .docx, the Exhibit A .docx, and the Annex C .xlsx — attach with the
    correct OpenXML MIME via `attach_pdf_to_row`'s `content_type` param (the former
    application/pdf-hardcode caveat, closed by the Feature-B attach-helper fix).
    `sheet_id` picks the target sheet: None = the review sheet (the original surface);
    the filing pass ALSO passes the flat Subcontract_Log's id and the per-job tracking
    sheet's id (PO/RFQ-lane parity, operator ask 2026-08-12)."""
    for name, data in files:
        suffix = name[name.rfind("."):].lower() if "." in name else ""
        content_type = _ATTACH_CONTENT_TYPES.get(suffix, "application/pdf")
        try:
            smartsheet_client.attach_pdf_to_row(
                sheet_id if sheet_id is not None else subcontract_review.SHEET_ID,
                row_id, name, data, content_type=content_type,
            )
        except Exception as exc:  # noqa: BLE001 — supplementary inline copy; Box is the SoR
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"row file attach failed (row {row_id}, {name!r}, "
                f"sheet {'review' if sheet_id is None else sheet_id}): "
                f"{type(exc).__name__}: {exc!r}",
                error_code="subcontract_row_file_attach_failed",
                correlation_id=correlation_id,
            )


def _append_perjob_row_best_effort(
    job_name: str, row_kwargs: dict[str, Any], correlation_id: str,
    *, files: list[tuple[str, bytes]],
) -> None:
    """Mirror the freshly-filed ledger row into the job's per-job tracking sheet
    (Feature A), BEST-EFFORT — a failure is a WARN that never fails the filing
    (mirror `_attach_files_best_effort`; Box + the flat Subcontract_Log are the SoR).

    Resolves the SAME job folder name the Box per-job folder uses
    (`safety_naming.job_folder_name`), find-or-creates the folder + "Subcontracts"
    sheet under sheet_ids.FOLDER_SC_JOBS (structure-cloned from the flat Log, so
    `append_filed_row` writes it unchanged), then appends unless the SC number is
    already present in the TARGET sheet (independent idempotency — a crash between
    the flat append and this mirror re-runs cleanly). The inline attaches (the three
    package files, operator ask 2026-08-12) ride the SAME every-service self-heal
    posture as the ledger row's — deterministic filenames → replace, never duplicate."""
    try:
        sid = job_sheet.ensure_job_sheet(
            sheet_ids.FOLDER_SC_JOBS,
            sheet_ids.SHEET_SUBCONTRACT_LOG,
            safety_naming.job_folder_name(job_name),
            PERJOB_SHEET_NAME,
            workspace_id=sheet_ids.WORKSPACE_SUBCONTRACTS,  # §51 A1 margin-check target
            workstream=WORKSTREAM,
            correlation_id=correlation_id,
        )
        existing = subcontract_log.find_row_by_sc_number(
            str(row_kwargs["sc_number"]), sheet_id=sid
        )
        if existing is None:
            row_id = subcontract_log.append_filed_row(sheet_id=sid, **row_kwargs)
        else:
            row_id = int(existing["_row_id"])
        _attach_files_best_effort(row_id, files, correlation_id, sheet_id=sid)
    except Exception as exc:  # noqa: BLE001 — supplementary per-job mirror; never fail the filing
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"per-job tracking sheet append failed (job {job_name!r}, "
            f"SC {row_kwargs.get('sc_number')!r}): {type(exc).__name__}: {exc!r}",
            error_code="subcontract_perjob_sheet_failed",
            correlation_id=correlation_id,
        )


# ---- ② Subcontractor down-sync pass -------------------------------------------------


def _subcontractor_down_sync_pass(creds: _SubcontractCreds, counters: dict[str, int]) -> None:
    """Project the FULL ITS_Subcontractors SoR into the Worker's D1 cache (full-replace;
    the Worker's dirty-row fence protects un-mirrored portal edits)."""
    try:
        payload = subcontractors.build_down_sync_payload()
    except smartsheet_client.SmartsheetError as exc:
        counters["sub_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"ITS_Subcontractors read failed — down-sync skipped this cycle: {exc!r}",
            error_code="subcontract_subs_read_failed",
        )
        return
    for row_id, skip_reason in payload.skipped:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"ITS_Subcontractors row {row_id} excluded from down-sync: {skip_reason}",
            error_code="subcontract_sub_row_skipped",
        )
    if not payload.subcontractors:
        # NEVER POST an empty set — an empty projection (fresh sheet, mass read miss) must
        # not wipe the portal cache (the Worker refuses it too).
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            "ITS_Subcontractors projected EMPTY — refusing to down-sync an empty set",
            error_code="subcontract_subs_empty_projection",
        )
        return
    try:
        result = portal_client.subcontractors_sync(
            creds.base_url, creds.bearer, payload.subcontractors
        )
    except portal_client.PortalAuthError as exc:
        raise _BearerRejectedError from exc
    except portal_client.PortalTransportError as exc:
        counters["sub_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"subcontractors down-sync POST failed (retries next cycle): {exc!r}",
            error_code="subcontract_subs_sync_failed",
        )
        return
    counters["subs_downsynced"] = len(payload.subcontractors)
    skipped_dirty = result.get("skipped_dirty")
    if isinstance(skipped_dirty, int) and skipped_dirty > 0:
        error_log.log(
            Severity.INFO, SCRIPT_NAME,
            f"subcontractors down-sync: {skipped_dirty} dirty portal row(s) fenced "
            f"(un-mirrored portal edits preserved — the up-sync converges them)",
            error_code="subcontract_subs_dirty_fenced",
        )


# ---- ③ Subcontractor up-sync pass ---------------------------------------------------


def _subcontractor_up_sync_pass(creds: _SubcontractCreds, counters: dict[str, int]) -> None:
    """Mirror portal-edited (dirty) subcontractors UP into ITS_Subcontractors, per-row commit."""
    try:
        pending = portal_client.get_pending_subcontractors(creds.base_url, creds.bearer)
    except portal_client.PortalAuthError as exc:
        raise _BearerRejectedError from exc
    except portal_client.PortalTransportError as exc:
        counters["sub_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"failed to GET pending subcontractors (left dirty for next cycle): {exc!r}",
            error_code="subcontract_subs_pending_fetch_failed",
        )
        return
    for sub in pending:
        sub_key = str(sub.get("sub_key") or "")
        mirror_version = sub.get("mirror_version")
        if not isinstance(mirror_version, int) or isinstance(mirror_version, bool):
            counters["sub_errors"] += 1
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"pending subcontractor {sub_key!r} has a malformed mirror_version "
                f"({mirror_version!r}); skipping (stays dirty)",
                error_code="subcontract_sub_version_malformed",
            )
            continue
        try:
            subcontractors.upsert_subcontractor(sub)
            portal_client.mark_subcontractors_mirrored(
                creds.base_url, creds.bearer,
                [{"sub_key": sub_key, "mirrored_version": mirror_version}],
            )
            counters["subs_upsynced"] += 1
        except portal_client.PortalAuthError as exc:
            raise _BearerRejectedError from exc
        except (
            ValueError,
            picklist_validation.PicklistViolationError,
            smartsheet_client.SmartsheetValidationError,
        ) as exc:
            # PERMANENT — the row will never succeed as-is; ticket the operator, leave the
            # subcontractor dirty (the Worker keeps serving it; the ticket is the de-dup).
            counters["subs_reviewed"] += 1
            review_queue.safe_add(
                script_name=SCRIPT_NAME,
                workstream=WORKSTREAM,
                summary=(
                    f"subcontract: subcontractor up-sync PERMANENT failure for {sub_key!r} "
                    f"({type(exc).__name__}) — portal edit not mirrored to ITS_Subcontractors"
                ),
                payload={
                    "sub_key": sub_key,
                    "mirror_version": mirror_version,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                sla_tier=review_queue.SlaTier.SUBCONTRACT_DRAFT,
                reason=review_queue.ReviewReason.POLICY_EDGE,
                severity=Severity.WARN,
                source_file=sub_key or "subcontractor",
            )
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"subcontractor up-sync fenced (permanent) {sub_key!r}: "
                f"{type(exc).__name__}: {exc!r}",
                error_code="subcontract_sub_upsert_permanent",
            )
        except (smartsheet_client.SmartsheetError, portal_client.PortalTransportError) as exc:
            counters["sub_errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"transient subcontractor up-sync failure for {sub_key!r} (left dirty): "
                f"{type(exc).__name__}: {exc!r}",
                error_code="subcontract_sub_upsert_transient",
            )


# ---- ④ Status pass ------------------------------------------------------------------


def _status_pass(creds: _SubcontractCreds, counters: dict[str, int]) -> None:
    """Mirror review-sheet approve/SENT stamps → Worker status-sync + Subcontract_Log.

    Candidates are bounded by the Subcontract_Log ledger state (a settled row generates
    no update), so the steady-state cycle POSTs nothing. Updates for one subcontract are
    ordered approved-then-sent — the Worker's guarded batch walks the machine forward and
    a replay no-ops. Subcontract_Log stamps apply ONLY after a successful POST (D1 first,
    then the mirror; a lost POST retries whole next cycle).

    The `executed` (wet countersignature) terminal has no natural portal signal: an
    operator-set `executed` Status on Subcontract_Log is mirrored into the D1 display
    cache here (BUILD_DECISIONS #4; the Worker guards it from `sent`), with no ledger
    stamp (the ledger IS already executed — the operator set it)."""
    try:
        review_rows = smartsheet_client.get_rows(subcontract_review.SHEET_ID)
        ledger_rows = smartsheet_client.get_rows(subcontract_log.SHEET_ID)
    except smartsheet_client.SmartsheetError as exc:
        counters["status_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"status pass sheet read failed (retries next cycle): {exc!r}",
            error_code="subcontract_status_read_failed",
        )
        return
    ledger_status: dict[str, str] = {}
    for row in ledger_rows:
        number = str(row.get(subcontract_log.COL_SC_NUMBER) or "").strip()
        if number:
            ledger_status[number] = str(row.get(subcontract_log.COL_STATUS) or "").strip()

    updates: list[dict[str, Any]] = []
    # Deferred Subcontract_Log stamps: (kind, sc_number, sent_at_iso, supersedes_sc_id).
    stamps: list[tuple[str, str, str | None, int | None]] = []
    for row in review_rows:
        tag = str(row.get(subcontract_review.COL_WORKSTREAM) or "").strip()
        if tag and tag != subcontract_review.WORKSTREAM_TAG:
            # Contamination signal (P1b) — a foreign-workstream row on the subcontract
            # review sheet is never status-synced; the send guard owns the HARD-HELD.
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"subcontract review row {row.get('_row_id')} carries foreign workstream "
                f"tag {tag!r}; ignored by the status pass",
                error_code="subcontract_status_foreign_tag",
            )
            continue
        sc_id = subcontract_review.row_sc_id(row)
        if sc_id is None:
            continue  # a row without the Notes join (hand row) — nothing to sync
        sc_number = subcontract_review.row_sc_number(row)
        if not sc_number:
            continue
        current = ledger_status.get(sc_number, "")
        if current == subcontract_log.STATUS_EXECUTED:
            # Operator countersigned (wet signature) — mirror the terminal transition into
            # the D1 display cache. Guarded server-side from `sent`; a replay when D1 is
            # already executed no-ops. No ledger stamp (the ledger is the source here).
            updates.append({"sc_id": sc_id, "status": "executed"})
            continue
        if current in (
            subcontract_log.STATUS_SENT,
            subcontract_log.STATUS_SUPERSEDED,
            subcontract_log.STATUS_CANCELED,
        ):
            continue  # settled — nothing to move forward

        sent = str(row.get(subcontract_review.COL_SEND_STATUS) or "") == subcontract_review.STATUS_SENT
        approved = bool(
            row.get(subcontract_review.COL_APPROVE_SCHEDULED)
            or row.get(subcontract_review.COL_SEND_NOW)
            or row.get(subcontract_review.COL_APPROVED_BY)
        )
        supersedes_sc_id = subcontract_review.row_supersedes_sc_id(row)
        if sent:
            # Walk the D1 machine forward in ONE ordered pair — approved (guarded from
            # pending_review) THEN sent (guarded from approved); replays no-op.
            updates.append({"sc_id": sc_id, "status": "approved"})
            updates.append({"sc_id": sc_id, "status": "sent"})
            sent_at = str(row.get(subcontract_review.COL_SENT_AT) or "")[:10] or None
            stamps.append(("sent", sc_number, sent_at, supersedes_sc_id))
        elif approved and current == subcontract_log.STATUS_PENDING_REVIEW:
            updates.append({"sc_id": sc_id, "status": "approved"})
            stamps.append(("approved", sc_number, None, None))

    if not updates:
        return
    try:
        portal_client.subcontract_status_sync(creds.base_url, creds.bearer, updates)
    except portal_client.PortalAuthError as exc:
        raise _BearerRejectedError from exc
    except portal_client.PortalTransportError as exc:
        counters["status_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"status-sync POST failed (stamps deferred to next cycle): {exc!r}",
            error_code="subcontract_status_sync_failed",
        )
        return

    for kind, sc_number, sent_at_iso, supersedes_sc_id in stamps:
        try:
            if kind == "sent":
                subcontract_log.stamp_status(
                    sc_number, subcontract_log.STATUS_SENT, sent_at_iso=sent_at_iso
                )
                if supersedes_sc_id is not None:
                    predecessor = subcontract_log.find_sc_number_by_d1_id(supersedes_sc_id)
                    if predecessor:
                        # The ledger mirror of the Worker's superseded flip.
                        subcontract_log.stamp_status(
                            predecessor, subcontract_log.STATUS_SUPERSEDED,
                            superseded_by=sc_number,
                        )
            else:
                subcontract_log.stamp_status(sc_number, subcontract_log.STATUS_APPROVED)
            counters["status_synced"] += 1
        except (
            picklist_validation.PicklistViolationError,
            smartsheet_client.SmartsheetError,
            ValueError,
        ) as exc:
            counters["status_errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"Subcontract_Log stamp failed for {sc_number} ({kind}; D1 already "
                f"synced — the ledger self-heals next cycle): {type(exc).__name__}: {exc!r}",
                error_code="subcontract_log_stamp_failed",
            )


if __name__ == "__main__":
    poll_once()
