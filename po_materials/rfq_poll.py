"""RFQ pull daemon — the Mac generation half of the outbound-RFQ lane (ADR-0004 R2).

Purpose
-------
The Worker (safety_portal/worker/rfq.ts) validates/signs/queues each composed RFQ
SEND-FREE in D1 (`rfqs`, migration 0056); this launchd daemon (120s,
`org.solutionsmith.its.rfq-poll`) is the Mac-side consumer — the po_poll multi-pass
model (one host, one lock, one heartbeat) behind ONE gate
(`po_materials.rfq_poll.polling_enabled`, shipped false):

  ① **RFQ pass**: GET /api/po/rfqs/internal/pending → per row: rebuild the rfq:v1
     canonical (`shared.portal_hmac.rfq_canonical_json` — recompute-from-fields,
     the po:v1 pattern) + constant-time HMAC verify (`verify_rfq`; bad → ONE-SHOT
     flag `rfq_poll_flagged.json` + security Review-Queue row + CRITICAL — NEVER
     rendered, NEVER filed, NEVER marked; the row stays queued in D1 for forensics)
     → per PENDING vendor row in the (signed, sorted) fan-out: resolve from the
     ITS_Vendors SoR (`vendors.get_vendor_by_key`, READ-ONLY — ADR-0004 decision 9;
     unknown vendor → per-vendor Review-Queue fence, the OTHER vendors proceed) →
     DETERMINISTIC price-free render (`rfq_generate.render_rfq_pdf`) → Box file
     (§45 find-or-create the PO ROOT→job→"RFQs" — the lane's own root; §47
     version-on-conflict under `rfq_naming.rfq_pdf_filename`) → **R4: ALSO render
     the fillable `.xlsx` quote form (`quote_form.render_quote_form`) → file it to
     the SAME Box folder (best-effort; PDF-only degrade)** → RFQ_Log (rfq, vendor)
     row + RFQ_Pending_Review row (PO-twin columns; inline attach of BOTH files
     best-effort; the form's Box id seeded in the row Notes so PR-D's send attaches
     it; Send Status PENDING; Workstream 'po_materials_rfq' — the DISTINCT lane tag,
     see rfq_review) → collect (vendor_key, box_pdf_file_id, box_form_file_id,
     review_row_id) → **mark-filed ONCE per rfq, LAST**. A crash anywhere before
     the receipt re-serves the row and every prior step is find-or-skip idempotent
     (RFQ_Log by RFQ Number+Vendor Key; review rows by the Notes rfq_id+vendor
     join; Box by §47 version-on-conflict; Worker-side each vendor row flips
     pending→filed in-WHERE). A TRANSIENT per-vendor failure aborts
     the rfq WITHOUT the receipt (whole-rfq retry next cycle); if EVERY vendor is
     PERMANENTLY fenced the rfq is one-shot-flagged (`vendors_fenced`) instead of
     receipted — visible, never silently drained.
  ② **Status pass** (same gate): read the RFQ_Pending_Review SENT markers →
     POST /api/po/rfqs/internal/status-sync per (rfq, vendor) (FORWARD-ONLY,
     mirroring po_poll pass ④) → stamp the RFQ_Log mirror AFTER a successful POST
     (D1 first; a lost POST retries whole next cycle). F22 approval verification
     stays with the PR-D send poller — this pass reports, it does not authorize.

Invariants
----------
- GENERATION-side of the External Send Gate (FM Invariant 1): AI-FREE (cloud AND
  local) and customer-SEND-FREE — no `anthropic*`, no `graph_client`/`send_mail`/
  `resend`/`smtplib`/`email.mime` (enrolled in tests/test_capability_gating.py
  GATED_SCRIPTS). All egress rides the F02-allowlisted `shared.portal_client` (our
  Worker) + `shared.box_client` (filing) + `shared.smartsheet_client` (ledger).
  The actual vendor send is PR-D's rfq_send/rfq_send_poll pair.
- Invariant 2: a /pending row is UNTRUSTED until its rfq:v1 HMAC verifies; the
  vendor fan-out list is signature-covered; the vendor identity embedded in the
  PDF comes from the Smartsheet SoR, never the D1 cache; the document is
  PRICE-FREE by construction (no money field exists in the protocol).
- Bearer privilege separation (ADR-0004 red-team #1 / decision 4): the Keychain
  `ITS_PORTAL_RFQ_TOKEN` mirrors the Worker's PORTAL_RFQ_API_TOKEN and scopes
  ONLY /api/po/rfqs/internal/* — deliberately separate from the estimate lane's
  bearer (the hostile-PDF decoder must not reach this lane) and the PO tier's.
- Kill-switch first (`@require_active`) + `@its_error_log`; observable config
  resolution (`REQUIRED_CONFIG` + `resolve_and_log`, #336).

Failure modes
-------------
- PAUSED/MAINTENANCE → `@require_active` exits cleanly. Gate false (the shipped
  default) → pure no-op (no per-cycle spam; the seeded ITS_Config row is the
  operator's switch — scripts/migrations/seed_rfq_config.py).
- Missing base URL / bearer / HMAC secret → FAIL-CLOSED: CRITICAL + ERROR
  heartbeat, nothing polled.
- 401 anywhere → the SAME bearer fails every RFQ route, so the cycle STOPS:
  CRITICAL (`rfq_bearer_rejected`) + ERROR heartbeat.
- Per-item fences: PERMANENT (bad HMAC / malformed canonical / bad due date /
  empty vendor list / all-vendors-fenced) → Review-Queue row + one-shot flag
  (state `rfq_poll_flagged.json` — delete an entry to retry after fixing the
  cause); PERMANENT per-vendor (unknown vendor) → Review-Queue row, the other
  vendors proceed; TRANSIENT (SmartsheetError / BoxError / PortalTransportError)
  → ERROR-logged, row left queued, next cycle retries. One bad row never kills
  the cycle (`rfq_service_failed`).

Consumers
---------
- launchd `org.solutionsmith.its.rfq-poll` (StartInterval 120s default; RunAtLoad).
- Watchdog Check C marker (`rfq_poll`) + ITS_Daemon_Health row (shared.heartbeat).
- §43 runbook: docs/runbooks/rfq_generation_path.md.
"""
from __future__ import annotations

import fcntl
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from po_materials import (
    po_naming,
    rfq_generate,
    rfq_log,
    rfq_naming,
    rfq_review,
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

SCRIPT_NAME = "po_materials.rfq_poll"
WORKSTREAM = "po_materials"

# ITS_Config keys (read under Workstream='po_materials' except the SHARED
# safety_reports-owned worker_base_url — the po_poll/estimate_poll ownership pattern).
CFG_POLLING_ENABLED = "po_materials.rfq_poll.polling_enabled"
CFG_WORKER_BASE_URL = "safety_reports.portal.worker_base_url"  # shared with portal_poll
CFG_WORKER_BASE_URL_WORKSTREAM = "safety_reports"

# Keychain entry names (NOT secrets). The RFQ bearer mirrors the Worker's
# PORTAL_RFQ_API_TOKEN (privilege-separated per ADR-0004 decision 4 — separate from
# the estimate AND PO tiers); the HMAC secret is the SAME payload secret the Worker
# signs with (domain separation, not key separation, isolates the rfq:v1 protocol).
KC_RFQ_TOKEN = "ITS_PORTAL_RFQ_TOKEN"  # noqa: S105 — Keychain entry NAME, not a secret
KC_HMAC_SECRET = "ITS_PORTAL_HMAC_SECRET"  # noqa: S105 — Keychain entry NAME, not a secret

DEFAULT_POLLING_ENABLED = False  # ships dark; the operator flips the seeded row
POLL_INTERVAL_SECONDS = 120  # registration metadata; mirrors the launchd StartInterval

# Box filing leaf under the PO root's per-job folder (§45 find-or-create at every
# level; the PO lane's OWN tree — po_naming.CFG_BOX_PORTAL_ROOT → <job> → "RFQs").
RFQS_SUBFOLDER = "RFQs"

# The fillable-quote-form OpenXML MIME (R4) — used for the Smartsheet inline attach so the
# xlsx is not mislabeled application/pdf (the shared engine derives the same from the
# filename at send time via weekly_send._attachment_content_type).
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# #336 — every ITS_Config key this daemon resolves at RUNTIME. The declared-but-not-
# runtime-read *.poll_interval_seconds key is deliberately EXCLUDED (install.sh bakes
# it into the plist; the daemon never reads it) — same posture as po_poll.
REQUIRED_CONFIG: list[ConfigKey] = [
    ConfigKey(CFG_POLLING_ENABLED, WORKSTREAM, DEFAULT_POLLING_ENABLED, "bool"),
    ConfigKey(
        CFG_WORKER_BASE_URL, CFG_WORKER_BASE_URL_WORKSTREAM, "", "str",
        description="Shared Worker base URL; owned by safety_reports, read here too.",
    ),
    # The PO lane's OWN Box root; owned by po_naming (the config-dictionary
    # description lives on po_poll's declaration — one owner's prose per key). RFQ
    # PDFs + quote forms file under ROOT→<job>→'RFQs'.
    ConfigKey(
        po_naming.CFG_BOX_PORTAL_ROOT, po_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM, "", "str",
    ),
]

# State paths. HEARTBEAT_ROW_STATE_PATH is the SHARED row-id cache (ARCH-2).
STATE_DIR = Path.home() / "its" / "state"
HEARTBEAT_PATH = STATE_DIR / "rfq_poll_heartbeat.txt"
LOCK_PATH = STATE_DIR / "rfq_poll.lock"
# Sustained pending-fetch escalation (2026-07-20 forensic: a 21h every-cycle ERROR storm
# was invisible on every CRITICAL-keyed fire surface — shared/sustained_failure.py).
_FETCH_FAILS = sustained_failure.SustainedFailureCounter(
    STATE_DIR / "rfq_pending_fetch_failures.json", SCRIPT_NAME, "rfq_pending_fetch_counter_failed",
)
HEARTBEAT_ROW_STATE_PATH = STATE_DIR / "heartbeat_row_ids.json"
# One-shot flag state for PERMANENTLY-refused pending rows (`{rfq_id: reason}`).
# A flagged row is skipped every subsequent cycle (no 120s Review-Queue spam); the
# operator remediates by fixing the cause and deleting the entry (or the file).
RFQ_FLAGGED_PATH = STATE_DIR / "rfq_poll_flagged.json"
MAX_RFQ_FLAGS = 500  # drained/settled entries are dead weight only — cap the file

DAEMON_NAME = "po_materials.rfq_poll"
_REGISTRATION_SOURCE_ID = "Safety Portal Worker /api/po/rfqs/internal/pending"

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
WATCHDOG_JOB_SLUG = "rfq_poll"

_PACIFIC = ZoneInfo("America/Los_Angeles")  # the RFQ date is operator wall-clock


@dataclass(frozen=True)
class RfqPollStats:
    """Summary of one poll_once() invocation."""

    skipped_disabled: bool = False
    skipped_locked: bool = False
    halted_no_creds: bool = False
    # base URL temporarily unreadable (Smartsheet blip / circuit OPEN) — transient,
    # distinct from halted_no_creds, which is a genuine misconfig that pages.
    halted_transient: bool = False
    bearer_rejected: bool = False
    # ① RFQ pass
    scanned: int = 0
    filed: int = 0            # RFQs fully receipted this cycle (mark-filed posted)
    vendors_filed: int = 0    # per-vendor copies filed (Box + ledger + review row)
    rejected: int = 0         # bad-HMAC refusals (first sighting)
    fenced: int = 0           # permanent whole-rfq fences (canonical/due-date/vendors)
    vendors_fenced: int = 0   # per-vendor permanent fences (unknown vendor)
    skipped_flagged: int = 0  # rows already one-shot-flagged in a prior cycle
    errors: int = 0           # transient failures (row left queued)
    # ② status pass
    status_synced: int = 0
    status_errors: int = 0


class _BearerRejectedError(Exception):
    """Internal: a 401 anywhere — the SAME bearer fails every RFQ route, stop the cycle."""


@dataclass(frozen=True)
class _RfqCreds:
    """Resolved credentials with NAMED fields (the portal_poll CodeQL taint rationale:
    named fields keep the bearer/secret taint off base_url and everything logged)."""

    base_url: str
    bearer: str
    secret: str


# ---- Config readers (replicated per preservation, mirror estimate_poll) -----------


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


def _read_bool_setting(key: str, fallback: bool) -> bool:
    raw = _read_str_setting(key, str(fallback).lower())
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _polling_enabled() -> bool:
    return _read_bool_setting(CFG_POLLING_ENABLED, DEFAULT_POLLING_ENABLED)


# ---- Lock + heartbeat + marker seams (mirror estimate_poll) ------------------------


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
    """Touch the Check C freshness marker for this run (mirror estimate_poll)."""
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


# ---- One-shot flag state (the po_poll flag pattern) --------------------------------


def _load_flags() -> dict[str, str]:
    """Load the one-shot flag set `{rfq_id: reason}`. {} on any read error (fail-open:
    the only cost is one redundant re-flag, never a missed alert)."""
    if not RFQ_FLAGGED_PATH.exists():
        return {}
    try:
        parsed = json.loads(RFQ_FLAGGED_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _persist_flags(flags: dict[str, str]) -> None:
    """Atomically persist the flag set (capped). Lock-timeout fails OPEN with a WARN —
    a lost flag set costs a duplicate Review-Queue flag next cycle, never a missed one."""
    if len(flags) > MAX_RFQ_FLAGS:
        flags = dict(list(flags.items())[-MAX_RFQ_FLAGS:])
    RFQ_FLAGGED_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with state_io.with_path_lock(RFQ_FLAGGED_PATH):
            state_io.atomic_write_json(RFQ_FLAGGED_PATH, flags)
    except state_io.StateLockTimeoutError:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"could not acquire lock on {RFQ_FLAGGED_PATH} after retries; "
            f"RFQ flag set not persisted",
            error_code="rfq_flags_persist_failed",
        )


# ---- Credential resolution (fail-CLOSED) -------------------------------------------


def _resolve_credentials() -> _RfqCreds | TransientUnavailable | None:
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
        bearer = keychain.get_secret(KC_RFQ_TOKEN)
    except keychain.KeychainError:
        bearer = ""
    try:
        secret = keychain.get_secret(KC_HMAC_SECRET)
    except keychain.KeychainError:
        secret = ""
    if not (base_url and bearer and secret):
        return None
    return _RfqCreds(base_url=base_url, bearer=bearer, secret=secret)


# ---- Public API --------------------------------------------------------------------


@its_error_log(SCRIPT_NAME)
@require_active
def poll_once() -> RfqPollStats:
    """Run one RFQ cycle (pass ① + pass ②). launchd invokes this once per
    StartInterval; idempotent across crashes (the mark-filed receipt is LAST and
    ONCE per rfq — see the module docstring)."""
    # #336 startup observability (after @require_active, fail-open — never blocks).
    resolve_and_log(SCRIPT_NAME, REQUIRED_CONFIG)

    if not _polling_enabled():
        # Shipped default — an intentional dark state, not an anomaly: no
        # heartbeat/marker/log spam every 120s. The seeded ITS_Config row is the
        # operator's switch (scripts/migrations/seed_rfq_config.py).
        return RfqPollStats(skipped_disabled=True)

    with _file_lock(LOCK_PATH) as acquired:
        if not acquired:
            error_log.log(
                Severity.INFO, SCRIPT_NAME,
                "another rfq cycle holds the lock; skipping this cycle",
                error_code="rfq_poll_lock_held",
            )
            return RfqPollStats(skipped_locked=True)
        try:
            return _poll_inside_lock()
        finally:
            # D3 — see portal_poll: one summarized WARN row per pass that recovered on retry.
            sustained_failure.flush_retry_recovery(SCRIPT_NAME)


def _poll_inside_lock() -> RfqPollStats:
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
            f"RFQ Worker base URL temporarily unreadable ({creds.reason}) — skipping "
            f"this cycle; will retry next interval (transient, self-heals)",
            error_code="rfq_creds_transient",
        )
        _write_heartbeat()
        _write_heartbeat_row(
            status="WARN", items_processed=0,
            error_summary=f"base URL unreadable ({creds.reason}) — transient",
        )
        return RfqPollStats(halted_transient=True)
    if creds is None:
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            (
                # Deliberately does NOT interpolate the ITS_Config key or the Keychain
                # entry NAMES (secret-store names in a log are a CodeQL clear-text trip).
                "fail-closed: missing RFQ portal credentials — the Worker base URL "
                "(ITS_Config) and/or the RFQ bearer + HMAC-secret Keychain entries "
                "are unset; NOT polling until fixed"
            ),
            error_code="rfq_creds_missing",
        )
        _write_heartbeat()
        _write_heartbeat_row(
            status="ERROR", items_processed=0,
            error_summary="fail-closed: RFQ portal credentials missing",
        )
        return RfqPollStats(halted_no_creds=True)

    counters: dict[str, int] = {
        "scanned": 0, "filed": 0, "vendors_filed": 0, "rejected": 0, "fenced": 0,
        "vendors_fenced": 0, "skipped_flagged": 0, "errors": 0,
        "status_synced": 0, "status_errors": 0,
    }
    bearer_rejected = False
    try:
        _rfq_pass(creds, counters)
        _status_pass(creds, counters)
    except _BearerRejectedError:
        # A 401 anywhere: the SAME bearer fails every RFQ route, so nothing else
        # can work this cycle. A bad/rotated bearer will NOT self-heal → page.
        bearer_rejected = True
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            "RFQ bearer UNAUTHORIZED (401) — rejected by the Worker's "
            "requireRfqToken tier; cycle STOPPED until the token is fixed "
            "(everything stays queued — safe re-attempt)",
            error_code="rfq_bearer_rejected",
        )

    _write_heartbeat()
    # `skipped_flagged` counts items ALREADY one-shot-flagged — permanently wedged out of
    # the queue until an operator fixes the cause and clears the entry. Omitting it made
    # Last Cycle Status flip back to OK on the very next cycle, so the daemon read healthy
    # while an RFQ sat fenced indefinitely (2026-08-10). A standing fence is a standing
    # WARN by design: ITS_Daemon_Health is a visibility surface, not a push surface, so
    # this never pages anyone — and it self-clears the moment the flag entry is cleared.
    total_flagged = (
        counters["rejected"] + counters["fenced"] + counters["vendors_fenced"]
        + counters["skipped_flagged"]
    )
    total_errors = counters["errors"] + counters["status_errors"]
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
            # Distinguish NEW fences this cycle from items merely STANDING flagged, so
            # the operator can tell "something just broke" from "something is still
            # waiting on me" without opening the log.
            + (f" standing={counters['skipped_flagged']}"
               if counters["skipped_flagged"] else "")
            + (" bearer_rejected" if bearer_rejected else "")
        )
    try:
        _write_heartbeat_row(
            status=cycle_status,
            items_processed=counters["filed"] + counters["status_synced"],
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
            f"rfq cycle: scanned={counters['scanned']} filed={counters['filed']} "
            f"vendors filed={counters['vendors_filed']} rejected={counters['rejected']} "
            f"fenced={counters['fenced']} vendors fenced={counters['vendors_fenced']} "
            f"flag-skipped={counters['skipped_flagged']} errors={counters['errors']}; "
            f"status synced={counters['status_synced']} errors={counters['status_errors']}"
        ),
        error_code="rfq_cycle_summary",
    )
    return RfqPollStats(
        bearer_rejected=bearer_rejected,
        scanned=counters["scanned"],
        filed=counters["filed"],
        vendors_filed=counters["vendors_filed"],
        rejected=counters["rejected"],
        fenced=counters["fenced"],
        vendors_fenced=counters["vendors_fenced"],
        skipped_flagged=counters["skipped_flagged"],
        errors=counters["errors"],
        status_synced=counters["status_synced"],
        status_errors=counters["status_errors"],
    )


# ---- ① RFQ pass --------------------------------------------------------------------


def _rfq_pass(creds: _RfqCreds, counters: dict[str, int]) -> None:
    """Drain the queued-RFQ queue: verify → per-vendor render/file/stage → receipt."""
    try:
        pending = portal_client.get_rfqs_pending(creds.base_url, creds.bearer)
    except portal_client.PortalAuthError as exc:
        raise _BearerRejectedError from exc
    except portal_client.PortalTransportError as exc:
        counters["errors"] += 1
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
                f"(rows left queued; see docs/runbooks/rfq_generation_path.md): {exc!r}",
                error_code="rfq_pending_fetch_sustained",
            )
        else:
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"failed to GET rfqs pending (rows left queued for next cycle); {n} consecutive: {exc!r}",
            error_code="rfq_pending_fetch_failed",
            )
        return
    _FETCH_FAILS.reset()  # a successful fetch clears the sustained-outage counter
    if not pending:
        return

    # Per-CYCLE purchaser identity (D5 versioned config — the SAME artifact
    # po_generate consumes). A broken config file is a deploy defect — abort the
    # pass loudly, leave every row queued (the po_poll posture).
    try:
        purchaser = terms_lib.load_purchaser_config()
    except terms_lib.TermsError as exc:
        counters["errors"] += 1
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            f"purchaser config unreadable — rfq pass ABORTED (rows left queued): {exc}",
            error_code="rfq_config_unreadable",
        )
        return

    flags = _load_flags()
    flags_before = dict(flags)
    try:
        for row in pending:
            counters["scanned"] += 1
            _process_pending_rfq(row, creds, counters, flags, purchaser)
    finally:
        # Persist-on-mutation via snapshot compare, in a finally — NOT a
        # return-value dirty bool. A bearer abort (_BearerRejectedError raised out
        # of the per-vendor fan-out / mark-filed post) leaves the loop AFTER
        # _fence_rfq / _handle_rfq_hmac_failure already wrote the in-flight row's
        # one-shot flag, and a return-value protocol cannot see that mutation (the
        # raise skips the return). The finally guarantees every flag written this
        # cycle — including the one the aborting row just earned — reaches disk, so
        # a fixed-later bearer never replays a 120s CRITICAL/Review-Queue re-alert
        # storm for already-flagged rows. (Mirrors estimate_poll, PR-A.)
        if flags != flags_before:
            _persist_flags(flags)


def _process_pending_rfq(
    row: dict[str, Any],
    creds: _RfqCreds,
    counters: dict[str, int],
    flags: dict[str, str],
    purchaser: dict[str, Any],
) -> bool:
    """Verify + fan out + file + receipt ONE pending RFQ. Returns True iff the
    one-shot flag set was mutated (the caller persists once per cycle)."""
    raw_id = row.get("id")
    if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0:
        counters["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"pending rfq row has a missing/malformed id ({raw_id!r}); skipping",
            error_code="rfq_row_no_id",
        )
        return False
    rfq_id = raw_id
    if str(rfq_id) in flags:
        counters["skipped_flagged"] += 1
        return False

    rfq_number = str(row.get("rfq_number") or "")
    # Split the HMAC off the row IMMEDIATELY (the portal_poll hygiene: an integrity
    # tag has no business traveling into render/filing/logs).
    provided_hmac = str(row.get("hmac") or "")
    rfq = {k: v for k, v in row.items() if k != "hmac"}
    raw_lines = rfq.get("line_items")
    line_items = [ln for ln in raw_lines if isinstance(ln, dict)] if isinstance(raw_lines, list) else []
    correlation_id = uuid.uuid4().hex[:12]

    # The Worker joins the per-vendor rfq_vendors rows onto the pending row; the
    # fan-out list (and the signed vendor_keys array) derives from them.
    raw_vendor_rows = rfq.get("vendors")
    vendor_rows = [
        v for v in raw_vendor_rows if isinstance(v, dict)
    ] if isinstance(raw_vendor_rows, list) else []
    vendor_keys = [
        str(v.get("vendor_key")) for v in vendor_rows
        if isinstance(v.get("vendor_key"), str) and str(v.get("vendor_key")).strip()
    ]

    # 1 — HMAC verify (Invariant 2 downgrade defense; constant-time,
    # recompute-from-fields — the po:v1 pattern; vendor_keys sorted inside).
    try:
        canonical = portal_hmac.rfq_canonical_json(rfq, line_items, vendor_keys)
    except ValueError as exc:
        # NaN/Infinity in a numeric field — malformed beyond what the Worker could
        # ever have signed. Permanent.
        counters["fenced"] += 1
        _fence_rfq(rfq_id, rfq_number, f"canonical serialization failed: {exc}",
                   "rfq_canonical_invalid", correlation_id, flags, "canonical")
        return True
    if not portal_hmac.verify_rfq(
        creds.secret, provided_hmac,
        rfq_id=rfq_id, rfq_number=rfq_number, canonical_json=canonical,
    ):
        counters["rejected"] += 1
        _handle_rfq_hmac_failure(rfq_id, rfq_number, rfq, correlation_id, flags)
        return True

    # 2 — signed-field validation (permanent fences; the values are now trusted
    # as SIGNED, but a signed defect must still fence, never render garbage).
    if not vendor_keys:
        counters["fenced"] += 1
        _fence_rfq(rfq_id, rfq_number,
                   "served vendor row set is empty/malformed — an RFQ with no "
                   "vendors cannot fan out",
                   "rfq_no_vendors", correlation_id, flags, "no_vendors")
        return True
    # due_date is nullable by contract (rfq.ts: 'YYYY-MM-DD' or null — "quote per
    # vendor's soonest"); a PRESENT-but-malformed value is a signed defect → fence.
    raw_due = rfq.get("due_date")
    due_date: date | None = None
    if raw_due not in (None, ""):
        try:
            due_date = date.fromisoformat(str(raw_due))
        except ValueError:
            counters["fenced"] += 1
            _fence_rfq(rfq_id, rfq_number,
                       f"signed due_date {raw_due!r} is not YYYY-MM-DD",
                       "rfq_bad_due_date", correlation_id, flags, "due_date")
            return True

    try:
        # 3 — Box folder (§45): the PO lane's OWN ROOT → job → "RFQs".
        folder_id = _resolve_rfq_box_folder(
            str(rfq.get("job_name") or rfq.get("job_no") or "")
        )
        if folder_id is None:
            counters["errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"PO Box root unresolved (ITS_Config "
                f"{po_naming.CFG_BOX_PORTAL_ROOT} unset) — RFQ {rfq_number} "
                f"left queued until the root is configured",
                error_code="rfq_box_root_unresolved",
                correlation_id=correlation_id,
            )
            return False

        # 4 — per-vendor fan-out: render → Box → RFQ_Log → review row. The RFQ
        # date = the filing date, Pacific (the po_poll posture). Only rfq_vendors
        # rows still 'pending' need servicing (a re-serve after a PARTIAL receipt
        # skips already-filed vendors; a re-serve after a LOST receipt still sees
        # them pending and find-or-skips the Mac-side artifacts).
        rfq_date = datetime.now(_PACIFIC).date()
        pending_keys = [
            str(v.get("vendor_key")) for v in vendor_rows
            if str(v.get("status") or "pending") == "pending"
            and isinstance(v.get("vendor_key"), str)
        ]
        filed_vendors: list[dict[str, Any]] = []
        for vendor_key in pending_keys:
            outcome = _file_one_vendor(
                rfq, line_items, vendor_key, purchaser, folder_id,
                rfq_id=rfq_id, rfq_number=rfq_number,
                rfq_date=rfq_date, due_date=due_date, secret=creds.secret,
                correlation_id=correlation_id, counters=counters,
            )
            if outcome is not None:
                filed_vendors.append(outcome)

        if not filed_vendors:
            # EVERY vendor permanently fenced — do NOT receipt (the row would
            # silently drain with zero artifacts); one-shot flag instead so the
            # operator repairs the vendor rows, deletes the flag entry, and the
            # next cycle re-files.
            counters["fenced"] += 1
            _fence_rfq(
                rfq_id, rfq_number,
                f"ALL {len(pending_keys)} pending vendor(s) fenced (unknown in "
                f"ITS_Vendors) — nothing filed, receipt withheld",
                "rfq_all_vendors_fenced", correlation_id, flags, "vendors_fenced",
            )
            return True

        # 5 — the receipt, LAST and ONCE per rfq (each vendor row flips
        # pending→filed in-WHERE; the rfq flips queued→generated only when no
        # pending vendor remains — a crash before this line re-serves the row and
        # every step above is find-or-skip idempotent; replays no-op Worker-side).
        portal_client.post_rfq_mark_filed(
            creds.base_url, creds.bearer, rfq_id=rfq_id, vendor_results=filed_vendors,
        )
        counters["filed"] += 1
        error_log.log(
            Severity.INFO, SCRIPT_NAME,
            f"filed RFQ {rfq_number} (rfq_id={rfq_id}) → {len(filed_vendors)} "
            f"vendor cop(ies): Box + RFQ_Log + RFQ_Pending_Review",
            error_code="rfq_filed",
            correlation_id=correlation_id,
        )
        return False
    except portal_client.PortalAuthError as exc:
        raise _BearerRejectedError from exc
    except (
        smartsheet_client.SmartsheetError,
        box_client.BoxError,
        portal_client.PortalTransportError,
    ) as exc:
        # TRANSIENT: no receipt — the whole rfq retries next cycle; already-filed
        # vendors find-or-skip.
        counters["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"transient failure filing RFQ {rfq_number} (rfq_id={rfq_id}; left "
            f"queued for next cycle): {type(exc).__name__}: {exc!r}",
            error_code="rfq_filing_transient",
            correlation_id=correlation_id,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — per-row fence; one bad RFQ never kills the cycle
        counters["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"unexpected failure filing RFQ {rfq_number} (rfq_id={rfq_id}; left "
            f"queued): {type(exc).__name__}: {exc!r}",
            error_code="rfq_service_failed",
            correlation_id=correlation_id,
        )
        return False


def _file_one_vendor(
    rfq: dict[str, Any],
    line_items: list[dict[str, Any]],
    vendor_key: str,
    purchaser: dict[str, Any],
    folder_id: str,
    *,
    rfq_id: int,
    rfq_number: str,
    rfq_date: date,
    due_date: date | None,
    secret: str,
    correlation_id: str,
    counters: dict[str, int],
) -> dict[str, Any] | None:
    """Render + file + stage ONE vendor's copy. Returns the collected
    `{vendor_key, box_pdf_file_id, box_form_file_id, review_row_id}` outcome, or None
    when the vendor is PERMANENTLY fenced (unknown in the SoR — the caller's receipt then
    excludes it; the other vendors proceed). TRANSIENT failures raise to the caller
    (whole-rfq retry, no receipt).

    R4 round-trip: alongside the PRICE-FREE RFQ PDF this ALSO renders the vendor's fillable
    ``.xlsx`` quote form (Tier-0 round-trip form, `quote_form.render_quote_form`), files it
    to the SAME Box folder, seeds its Box file id into the review row's Notes (so PR-D's
    ``rfq_send`` attaches it as the second attachment) + the mark-filed vendor_results (→ the
    Worker's `rfq_vendors.box_form_file_id`), and attaches BOTH to the review row. The form
    is BEST-EFFORT: a render/upload failure degrades to PDF-only (WARN once/cycle) — the RFQ
    PDF is the essential document, never blocked on the convenience form."""
    vendor = vendors.get_vendor_by_key(vendor_key)
    if vendor is None:
        counters["vendors_fenced"] += 1
        _safe_review_queue_add(
            summary=(
                f"rfq: vendor {vendor_key!r} on RFQ {rfq_number or rfq_id} not "
                f"found in ITS_Vendors (the SoR) — this vendor's copy NOT rendered; "
                f"the other vendors proceed. Add the ITS_Vendors row, THEN clear this "
                f"RFQ's entry from state/rfq_poll_flagged.json — the original RFQ "
                f"re-serves and files this vendor itself. Do NOT compose a replacement "
                f"RFQ (it double-files). See docs/runbooks/rfq_generation_path.md."
            ),
            payload={
                "rfq_id": rfq_id,
                "rfq_number": rfq_number,
                "vendor_key": vendor_key,
                "job_no": rfq.get("job_no"),
            },
            source_file=f"rfq:{rfq_id}",
            correlation_id=correlation_id,
        )
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"rfq vendor fenced (permanent) rfq_id={rfq_id} vendor={vendor_key!r}: "
            f"unknown in ITS_Vendors",
            error_code="rfq_vendor_unknown",
            correlation_id=correlation_id,
        )
        return None
    vendor_name = str(vendor.get(vendors.COL_VENDOR_NAME) or "")

    # Deterministic price-free render (escaped throughout — red-team #11).
    pdf = rfq_generate.render_rfq_pdf(
        rfq, line_items, vendor, purchaser, rfq_date=rfq_date, due_date=due_date,
    )

    # Box: §47 version-on-conflict under the vendor-suffixed deterministic name.
    filename = rfq_naming.rfq_pdf_filename(rfq_number, vendor_name)
    file_info = box_client.upload_bytes_or_new_version(folder_id, filename, pdf)
    box_file_id = str(file_info["id"])
    box_link = f"https://app.box.com/file/{box_file_id}"

    # R4 — the fillable xlsx quote form alongside the PDF (best-effort; PDF-only degrade).
    form_bytes, box_form_file_id = _render_and_file_quote_form(
        rfq, line_items, vendor_key, vendor_name, folder_id,
        rfq_number=rfq_number, due_date=due_date, secret=secret,
        correlation_id=correlation_id, counters=counters,
    )

    # RFQ_Pending_Review row (idempotent via the Notes rfq_id+vendor join) + the
    # inline attach of BOTH files (best-effort — Box is the SoR).
    existing = rfq_review.find_row_by_rfq_vendor(rfq_id, vendor_key)
    if existing is not None:
        review_row_id = int(existing["_row_id"])
    else:
        email_body = rfq_review.rfq_email_body_template(
            contact_name=str(vendor.get(vendors.COL_CONTACT_NAME) or ""),
            rfq_number=rfq_number,
            job_name=str(rfq.get("job_name") or ""),
            purchaser_entity=str(purchaser.get("entity") or ""),
            due_date_display=(
                due_date.strftime("%-m/%-d/%Y") if due_date is not None
                else "your earliest convenience"
            ),
        )
        review_row_id = rfq_review.add_rfq_review_row(
            job_project=f"{rfq.get('job_no')} — {rfq.get('job_name')}",
            vendor_key=vendor_key,
            rfq_date=rfq_date,
            pdf_link=box_link,
            recipient_to=str(vendor.get(vendors.COL_CONTACT_EMAIL) or ""),
            cc_display="",
            email_body=email_body,
            notes=rfq_review.notes_for_review_row(
                rfq_id, rfq_number, vendor_key, box_form_file_id
            ),
        )
        _attach_file_best_effort(review_row_id, filename, pdf, correlation_id)
        if box_form_file_id and form_bytes is not None:
            _attach_file_best_effort(
                review_row_id,
                rfq_naming.rfq_form_filename(rfq_number, vendor_name),
                form_bytes, correlation_id, content_type=_XLSX_MIME,
            )

    # RFQ_Log (rfq, vendor) row (idempotent by RFQ Number + Vendor Key — the
    # crash-retry find-or-skip the mark-filed replay contract depends on). The kwargs
    # dict is shared with the per-job mirror below (the po_poll ledger_row_kwargs shape).
    ledger_row_kwargs: dict[str, Any] = {
        "rfq_number": rfq_number,
        "job_no": str(rfq.get("job_no") or ""),
        "vendor_key": vendor_key,
        "vendor_name": vendor_name,
        "status": rfq_log.STATUS_FILED,
        "box_pdf_file_id": box_file_id,
        "review_row_id": str(review_row_id),
        "detail": f"due {due_date.isoformat()}" if due_date is not None else "",
    }
    existing_log = rfq_log.find_row(rfq_number, vendor_key)
    if existing_log is None:
        log_row_id = rfq_log.append_row(**ledger_row_kwargs)
    else:
        log_row_id = int(existing_log["_row_id"])
    # Inline attach of BOTH files on the ledger row (PO-lane parity, operator ask
    # 2026-07-20 — the review row already carried them; the ledger row now does too).
    # On EVERY service, not fresh-append-only (adversarial review 2026-07-20):
    # attach_pdf_to_row REPLACES a same-filename attachment and both filenames are
    # deterministic per (rfq, vendor), so this is duplicate-free — and an attach that
    # failed in a cycle whose receipt also failed SELF-HEALS on the re-serve (the
    # po_poll reference posture; a fresh-append-only guard made that miss permanent).
    _attach_file_best_effort(
        log_row_id, filename, pdf, correlation_id, sheet_id=rfq_log.sheet_id(),
    )
    if box_form_file_id and form_bytes is not None:
        _attach_file_best_effort(
            log_row_id,
            rfq_naming.rfq_form_filename(rfq_number, vendor_name),
            form_bytes, correlation_id,
            content_type=_XLSX_MIME, sheet_id=rfq_log.sheet_id(),
        )

    # Per-job tracking sheet mirror (Feature A parity, operator ask 2026-07-20):
    # the SAME ledger row into "<Jobs>/<job>/RFQs" beside the job's "Purchase
    # Orders" sheet, now WITH the same inline copies (operator ask 2026-08-11).
    # BEST-EFFORT — fenced inside the helper; a per-job failure must NEVER fail
    # the filing (Box + the flat RFQ_Log are the SoR).
    _append_perjob_rfq_row_best_effort(
        str(rfq.get("job_name") or "") or str(rfq.get("job_no") or ""),
        ledger_row_kwargs, correlation_id,
        pdf_filename=filename, pdf_bytes=pdf,
        form_filename=rfq_naming.rfq_form_filename(rfq_number, vendor_name),
        form_bytes=form_bytes if box_form_file_id else None,
    )

    counters["vendors_filed"] += 1
    # The Worker's vendor_results shape (string ids). R4: box_form_file_id is the fillable
    # xlsx quote form's Box file id — "" when the form degraded to PDF-only (the Worker's
    # optStr maps "" → NULL, so an absent form stores NULL, unchanged from R2).
    return {
        "vendor_key": vendor_key,
        "box_pdf_file_id": box_file_id,
        "box_form_file_id": box_form_file_id,
        "review_row_id": str(review_row_id),
    }


def _render_and_file_quote_form(
    rfq: dict[str, Any],
    line_items: list[dict[str, Any]],
    vendor_key: str,
    vendor_name: str,
    folder_id: str,
    *,
    rfq_number: str,
    due_date: date | None,
    secret: str,
    correlation_id: str,
    counters: dict[str, int],
) -> tuple[bytes | None, str]:
    """Render the vendor's fillable ``.xlsx`` quote form (Tier-0 round-trip form) and file
    it to the SAME Box folder as the RFQ PDF. Returns ``(form_bytes, box_form_file_id)`` —
    the bytes (for the review-row inline attach) + the Box file id (for the Notes seed +
    mark-filed vendor_results).

    BEST-EFFORT (R4): any failure — a missing ``quote_form`` module (lazy import), a render
    crash, or a Box upload error — degrades to PDF-only, returning ``(None, "")``. WARNs
    ONCE PER CYCLE (a `counters` sentinel suppresses per-vendor spam) — the RFQ PDF is the
    essential document; the fillable form is a convenience the vendor can also fill on their
    own letterhead (the review-row body says so). Never raises."""
    try:
        from po_materials import quote_form  # noqa: PLC0415 — lazy: not needed while dark
        form_bytes = quote_form.render_quote_form(
            rfq_number, vendor_key, str(rfq.get("job_name") or ""), line_items,
            secret=secret.encode("utf-8"),
            due_date=due_date.isoformat() if due_date is not None else None,
        )
        form_filename = rfq_naming.rfq_form_filename(rfq_number, vendor_name)
        info = box_client.upload_bytes_or_new_version(folder_id, form_filename, form_bytes)
        return form_bytes, str(info["id"])
    except Exception as exc:  # noqa: BLE001 — the form is a convenience; PDF-only degrade, never die
        if not counters.get("quote_form_warned"):
            counters["quote_form_warned"] = 1
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"RFQ {rfq_number}: quote-form render/file failed for vendor "
                f"{vendor_key!r} — this vendor's RFQ files PDF-only (the form is a "
                f"convenience): {type(exc).__name__}: {exc!r}",
                error_code="rfq_quote_form_failed",
                correlation_id=correlation_id,
            )
        return None, ""


def _safe_review_queue_add(
    *,
    summary: str,
    payload: dict[str, Any],
    source_file: str,
    correlation_id: str,
    reason: review_queue.ReviewReason = review_queue.ReviewReason.POLICY_EDGE,
    severity: Severity = Severity.WARN,
    security_flag: bool = False,
) -> None:
    """Bind this daemon's constants onto `review_queue.safe_add`.

    The never-raise rationale lives on `safe_add` — read it there. This wrapper exists
    only so the fence sites below stay short; it adds no behaviour.
    """
    review_queue.safe_add(
        script_name=SCRIPT_NAME,
        workstream=WORKSTREAM,
        summary=summary,
        payload=payload,
        sla_tier=review_queue.SlaTier.RFQ_DRAFT,
        reason=reason,
        severity=severity,
        source_file=source_file,
        security_flag=security_flag,
        correlation_id=correlation_id,
        error_code="rfq_fence_ticket_failed",
    )


def _handle_rfq_hmac_failure(
    rfq_id: int, rfq_number: str, rfq: dict[str, Any],
    correlation_id: str, flags: dict[str, str],
) -> None:
    """Reject a bad-HMAC RFQ row — the RFQ twin of po_poll's _handle_po_hmac_failure.

    NEVER rendered, NEVER filed, NEVER mark-filed (the downgrade defense; the row
    stays queued in D1 for forensics). One-shot: anomaly-log + security Review-Queue
    row + CRITICAL fire only on the FIRST sighting; the flag set suppresses
    per-cycle re-flag spam."""
    # Tripwire (Invariant 2, Layer 5) — record the suspicious pattern.
    anomaly_logger.check({"rfq_hmac_failure": rfq_id, "rfq_number": rfq_number})
    _safe_review_queue_add(
        summary=(
            f"rfq: HMAC verification FAILED for RFQ {rfq_number or rfq_id} — "
            f"rejected, NOT rendered or filed"
        ),
        payload={
            "rfq_id": rfq_id,
            "rfq_number": rfq_number,
            "job_no": rfq.get("job_no"),
            "vendor_keys": rfq.get("vendor_keys"),
            # The HMAC value is deliberately NOT recorded (signature material —
            # same posture as the PO twin); the raw row stays in D1.
        },
        reason=review_queue.ReviewReason.SECURITY_TRIGGER,
        severity=Severity.CRITICAL,
        source_file=f"rfq:{rfq_id}",
        security_flag=True,
        correlation_id=correlation_id,
    )
    error_log.log(
        Severity.CRITICAL, SCRIPT_NAME,
        (
            f"rfq HMAC FAIL rfq_id={rfq_id} rfq_number={rfq_number!r} — rejected, "
            f"not rendered or filed (downgrade defense)"
        ),
        error_code="rfq_hmac_failure",
        correlation_id=correlation_id,
    )
    flags[str(rfq_id)] = "hmac"


def _fence_rfq(
    rfq_id: int,
    rfq_number: str,
    detail: str,
    error_code: str,
    correlation_id: str,
    flags: dict[str, str],
    flag_reason: str,
) -> None:
    """Route a PERMANENTLY-refused pending RFQ to the Review Queue + one-shot flag
    it. The row is never receipted; it stays queued in D1. Remediation: fix the
    cause, then delete the rfq_id entry from `rfq_poll_flagged.json` (the daemon
    retries it the next cycle)."""
    _safe_review_queue_add(
        summary=f"rfq: RFQ {rfq_number or rfq_id} refused before filing — {detail}",
        payload={"rfq_id": rfq_id, "rfq_number": rfq_number, "detail": detail},
        source_file=f"rfq:{rfq_id}",
        correlation_id=correlation_id,
    )
    error_log.log(
        Severity.WARN, SCRIPT_NAME,
        f"rfq fenced (permanent, {error_code}) rfq_id={rfq_id} "
        f"rfq_number={rfq_number!r}: {detail}",
        error_code=error_code,
        correlation_id=correlation_id,
    )
    flags[str(rfq_id)] = flag_reason


def _resolve_rfq_box_folder(job_name: str) -> str | None:
    """§45 find-or-create the RFQ filing folder: the PO lane's OWN Box ROOT
    (`po_naming.CFG_BOX_PORTAL_ROOT`) → per-job folder (the SAME
    `safety_naming.job_folder_name` as every other portal artifact) → 'RFQs'.
    None when the root is unconfigured (the caller leaves the RFQ queued +
    ERRORs — a config gap, not a per-row defect)."""
    root = _read_str_setting(
        po_naming.CFG_BOX_PORTAL_ROOT, "",
        workstream=po_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM,
    ).strip()
    if not root:
        return None
    job_folder = box_client.get_or_create_folder(
        root, safety_naming.job_folder_name(job_name)
    )
    return box_client.get_or_create_folder(job_folder, RFQS_SUBFOLDER)


def _attach_file_best_effort(
    row_id: int, filename: str, file_bytes: bytes, correlation_id: str,
    *, content_type: str = "application/pdf", sheet_id: int | None = None,
) -> None:
    """Attach the rendered file inline on a Smartsheet row, BEST-EFFORT (Box is the
    SoR; a failure is a WARN that never fails the filing — mirror po_poll). Used for
    BOTH the RFQ PDF (default content-type) and the R4 fillable ``.xlsx`` quote form
    (OpenXML content-type — so the attachment is not mislabeled). `sheet_id` picks
    the target sheet: None = the review sheet (the original R2 surface); the filing
    pass ALSO passes the flat RFQ_Log's id (PO-lane parity, operator ask
    2026-07-20 — the ledger row carries the same inline copies)."""
    try:
        smartsheet_client.attach_pdf_to_row(
            sheet_id if sheet_id is not None else rfq_review.sheet_id(),
            row_id, filename, file_bytes, content_type=content_type,
        )
    except Exception as exc:  # noqa: BLE001 — supplementary inline copy; Box is the SoR
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"row file attach failed (row {row_id}, {filename!r}, "
            f"sheet {'review' if sheet_id is None else sheet_id}): "
            f"{type(exc).__name__}: {exc!r}",
            error_code="rfq_row_file_attach_failed",
            correlation_id=correlation_id,
        )


# The per-job tracking sheet's name inside the job folder — sits beside po_poll's
# "Purchase Orders" sheet in the SAME "<Jobs>/<job>" folder (Feature A).
PERJOB_RFQ_SHEET_NAME = "RFQs"


def _append_perjob_rfq_row_best_effort(
    job_name: str, row_kwargs: dict[str, Any], correlation_id: str,
    *, pdf_filename: str, pdf_bytes: bytes,
    form_filename: str, form_bytes: bytes | None,
) -> None:
    """Mirror the freshly-filed ledger row into the job's per-job "RFQs" tracking
    sheet (Feature A parity with po_poll._append_perjob_row_best_effort),
    BEST-EFFORT — a failure is a WARN that never fails the filing (Box + the flat
    RFQ_Log are the SoR).

    Resolves the SAME job folder name the Box + PO per-job folders use
    (`safety_naming.job_folder_name`), find-or-creates the folder + "RFQs" sheet
    under sheet_ids.FOLDER_PO_JOBS (structure-cloned from the flat RFQ_Log, so
    `append_row` writes it unchanged), then appends unless the (rfq, vendor) is
    already present in the TARGET sheet (independent idempotency — a crash between
    the flat append and this mirror re-runs cleanly). The inline attaches (RFQ PDF
    + quote form, operator ask 2026-08-11) ride the SAME every-service self-heal
    posture as the ledger row's — deterministic filenames → replace, never
    duplicate. `form_bytes` is None when the form degraded to PDF-only."""
    try:
        sid = job_sheet.ensure_job_sheet(
            sheet_ids.FOLDER_PO_JOBS,
            sheet_ids.SHEET_RFQ_LOG,
            safety_naming.job_folder_name(job_name),
            PERJOB_RFQ_SHEET_NAME,
            workspace_id=sheet_ids.WORKSPACE_PURCHASE_ORDERS,  # §51 A1 margin-check target
            workstream=WORKSTREAM,
            correlation_id=correlation_id,
        )
        existing = rfq_log.find_row(
            str(row_kwargs["rfq_number"]), str(row_kwargs["vendor_key"]), sheet_id=sid
        )
        if existing is None:
            row_id = rfq_log.append_row(sheet_id=sid, **row_kwargs)
        else:
            row_id = int(existing["_row_id"])
        _attach_file_best_effort(row_id, pdf_filename, pdf_bytes, correlation_id, sheet_id=sid)
        if form_bytes is not None:
            _attach_file_best_effort(
                row_id, form_filename, form_bytes, correlation_id,
                content_type=_XLSX_MIME, sheet_id=sid,
            )
    except Exception as exc:  # noqa: BLE001 — supplementary per-job mirror; never fail the filing
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"per-job RFQ tracking sheet append failed (job {job_name!r}, "
            f"RFQ {row_kwargs.get('rfq_number')!r} / "
            f"{row_kwargs.get('vendor_key')!r}): {type(exc).__name__}: {exc!r}",
            error_code="rfq_perjob_sheet_failed",
            correlation_id=correlation_id,
        )


# ---- ② Status pass -----------------------------------------------------------------


def _status_pass(creds: _RfqCreds, counters: dict[str, int]) -> None:
    """Mirror review-sheet SENT stamps → Worker status-sync + RFQ_Log (FORWARD-ONLY,
    the po_poll pass-④ posture).

    Candidates are bounded by the RFQ_Log ledger state (a settled (rfq, vendor)
    generates no update), so the steady-state cycle POSTs nothing. RFQ_Log stamps
    apply ONLY after a successful POST (D1 first, then the mirror; a lost POST
    retries whole next cycle). F22 approval verification is PR-D's send poller's —
    this pass reports, it does not authorize."""
    try:
        review_rows = smartsheet_client.get_rows(rfq_review.sheet_id())
    except RuntimeError:
        # Placeholder-0 sheet id (builder not yet run) — the lane cannot have
        # staged rows either; a silent skip here would hide a real misconfig once
        # the gate is on, so WARN once per cycle.
        counters["status_errors"] += 1
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            "status pass skipped: SHEET_RFQ_PENDING_REVIEW is still the 0 "
            "placeholder (run build_rfq_pending_review_sheet.py + flip the id)",
            error_code="rfq_status_sheet_placeholder",
        )
        return
    except smartsheet_client.SmartsheetError as exc:
        counters["status_errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"status pass sheet read failed (retries next cycle): {exc!r}",
            error_code="rfq_status_read_failed",
        )
        return

    # Per-(rfq, vendor) candidates: (rfq_id, rfq_number, vendor_key). The Worker's
    # status-sync route takes ONE update per POST (unlike po_status_sync's batch).
    candidates: list[tuple[int, str, str]] = []
    for row in review_rows:
        tag = str(row.get(rfq_review.COL_WORKSTREAM) or "").strip()
        if tag and tag != rfq_review.WORKSTREAM_TAG:
            # Contamination signal (P1b) — a foreign-workstream row on the RFQ
            # review sheet is never status-synced; the send guard owns the HARD-HELD.
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"RFQ review row {row.get('_row_id')} carries foreign workstream "
                f"tag {tag!r}; ignored by the status pass",
                error_code="rfq_status_foreign_tag",
            )
            continue
        rfq_id = rfq_review.row_rfq_id(row)
        vendor_key = rfq_review.row_vendor_key(row)
        rfq_number = rfq_review.row_rfq_number(row)
        if rfq_id is None or not vendor_key or not rfq_number:
            continue  # a row without the full join (hand row) — nothing to sync
        if str(row.get(rfq_review.COL_SEND_STATUS) or "") != rfq_review.STATUS_SENT:
            continue  # forward-only: only SENT moves the machine
        try:
            ledger = rfq_log.find_row(rfq_number, vendor_key)
        except (RuntimeError, smartsheet_client.SmartsheetError) as exc:
            counters["status_errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"status pass ledger read failed for RFQ {rfq_number}/{vendor_key} "
                f"(retries next cycle): {type(exc).__name__}: {exc!r}",
                error_code="rfq_status_ledger_read_failed",
            )
            continue
        current = str((ledger or {}).get(rfq_log.COL_STATUS) or "").strip()
        if current in rfq_log.SETTLED_STATUSES:
            continue  # settled — nothing to move forward
        candidates.append((rfq_id, rfq_number, vendor_key))

    for rfq_id, rfq_number, vendor_key in candidates:
        # ONE update per POST (the Worker contract) — D1 first, ledger stamp after.
        try:
            portal_client.post_rfq_status_sync(
                creds.base_url, creds.bearer,
                rfq_id=rfq_id, vendor_key=vendor_key, status="sent",
            )
        except portal_client.PortalAuthError as exc:
            raise _BearerRejectedError from exc
        except portal_client.PortalTransportError as exc:
            counters["status_errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"rfq status-sync POST failed for {rfq_number}/{vendor_key} "
                f"(stamp deferred to next cycle): {exc!r}",
                error_code="rfq_status_sync_failed",
            )
            continue
        try:
            rfq_log.update_status(rfq_number, vendor_key, rfq_log.STATUS_SENT)
            counters["status_synced"] += 1
        except Exception as exc:  # noqa: BLE001 — Smartsheet/picklist errors; the mirror self-heals
            counters["status_errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"RFQ_Log sent-stamp failed for {rfq_number}/{vendor_key} (D1 "
                f"already synced — the ledger self-heals next cycle): "
                f"{type(exc).__name__}: {exc!r}",
                error_code="rfq_log_stamp_failed",
            )


if __name__ == "__main__":
    poll_once()
