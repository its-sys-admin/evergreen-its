"""Vendor-estimate pull daemon — the Mac half of the estimate importer (ADR-0004 E2).

Purpose
-------
The Worker (safety_portal/worker/po_estimates.ts) accepts office-uploaded vendor
estimates SEND-FREE into the D1 pool (`po_estimates` + chunks, migration 0054),
signing each with the est:v1 HMAC at upload; this launchd daemon (120s,
`org.solutionsmith.its.estimate-poll`) is the Mac-side consumer — a SINGLE pass
behind ONE gate (`po_materials.estimate_poll.polling_enabled`, shipped false):

  GET /api/po/estimates/internal/pending → per row: claim FIRST (crash recovery)
  → pull chunks → STRICT reassembly (one chunk_total, gap-free, strict b64 — any
  malformation is an INTEGRITY failure: one-shot flag + security Review-Queue row +
  CRITICAL, NO result post, bytes left in D1 for forensics) → est:v1 HMAC verify
  (`shared.portal_hmac.verify_po_estimate`, constant-time) + len/sha256 recompute
  vs the SIGNED values → §34 doc screen (`po_attach_screen.screen_attachment` —
  the SAME screener as PO attachments; ClamAV layer reuses
  `po_materials.po_attach_screen.clamav_enabled`) → deterministic DOC-TYPE gate
  (`estimate_classify`, pdfplumber INSIDE the killable rlimited sandbox child —
  red-team #5): invoice/ap_report are REFUSED from the PO path, visibly
  (Estimate_Log 'refused' + POLICY_EDGE Review-Queue row + result post
  `wrong_doc_type:<t>`; the Worker deletes the chunks on refused) → surviving docs
  file the ORIGINAL bytes to Box (PO ROOT→job→"Vendor Quotes" — the lane's own root,
  §45 find-or-create / §47 version-on-conflict, name "<est_uuid> - <filename>")
  → Estimate_Log row (needs_review) → page-preview PNGs (Quartz via the sandbox;
  Pillow re-encoded; best-effort — failure never blocks filing) → result post
  LAST (status needs_review + box_file_id; a crash before it re-serves the claimed
  row and every prior step is idempotent).

PR-B adds the EXTRACTION LADDER HEAD between the doc-type gate and the
needs_review default (ADR-0004 decision 1 — LOCAL-ONLY, no cloud AI anywhere):

  Tier 0 (always on — our own deterministic round-trip): an .xlsx upload whose
  container carries the hidden `_ITS_META` sheet parses through
  `quote_form.parse_quote_form` (formula cells REJECT the whole form — red-team
  #3); a VERIFIED `rfq-form:v1` token → doc_type 'filled_form', a tier-0
  math-checked extraction posted `extracted`. The verified rfq_number/vendor_key
  ride INSIDE payload_json plus a top-level `detail` note — the Worker result
  route does not yet accept top-level rfq_id/rfq_vendor_key auto-bind fields
  (the 0054 columns exist; PR-D wires the additive Worker extension + D1
  auto-bind — the documented PR-D TODO; NO Worker edits in PR-B).
  Tier 1 (gate `po_materials.estimate_extract.tier1_enabled`, seeded false):
  deterministic template→generic parse (`estimate_parse`) over the native-text
  pages already extracted for classification.
  Tier 2 (gate `.tier2_enabled`, seeded false; at most ONE Tier-2 document per
  cycle): scanned docs OCR first (`estimate_ocr`, gate `.ocr_enabled`), then the
  local-Ollama schema-constrained extract (`estimate_extract.extract`; model /
  base URL / confidence threshold / timeout are ITS_Config-pinned).

Any tier success posts `extracted` with the advisory extraction payload (the
extraction is ADVISORY — every dollar re-enters the trusted path only through
the human disposition accept, ADR decision 2/3); `anomaly_logger.check` runs on
the payload's STRING fields only (decision 8) and a hit degrades the doc to
needs_review with a security-flagged review row. Every failure / low-confidence
/ gate-off path lands needs_review exactly as PR-A did; the sibling extraction
modules (`estimate_parse` / `estimate_ocr` / `estimate_extract`) are imported
LAZILY inside their gated tiers, so the dark-shipped daemon never needs them.

Invariants
----------
- GENERATION-side of the External Send Gate (FM Invariant 1): AI-FREE (cloud AND
  local — no `anthropic*`, no `ollama`) and customer-SEND-FREE — no
  `graph_client`/`send_mail`/`resend`/`smtplib`/`email.mime` (enroll in
  tests/test_capability_gating.py GATED_SCRIPTS). All egress rides the
  F02-allowlisted `shared.portal_client` (our Worker) + `shared.box_client`
  (filing) + `shared.smartsheet_client` (ledger writes).
- Invariant 2: a /pending row is UNTRUSTED until its est:v1 HMAC verifies AND the
  reassembled bytes match the signed sha256/size; §34 screening precedes every
  parse; every hostile-byte parse (pdfplumber / Quartz) runs in the killable
  rlimited `estimate_sandbox` child — the daemon NEVER dies from a hostile
  document (a wedged parse degrades the doc, never the cycle).
- Bearer privilege separation (ADR-0004 red-team #1): the Keychain
  `ITS_PORTAL_ESTIMATE_TOKEN` mirrors the Worker's PORTAL_ESTIMATE_API_TOKEN and
  scopes ONLY /api/po/estimates/internal/* — this highest-exposure process holds
  no other tier's bearer.
- Kill-switch first (`@require_active`) + `@its_error_log`; observable config
  resolution (`REQUIRED_CONFIG` + `resolve_and_log`, #336).

Failure modes
-------------
- PAUSED/MAINTENANCE → `@require_active` exits cleanly. Gate false (the shipped
  default) → pure no-op (no per-cycle spam; the seeded ITS_Config row is the
  operator's switch — scripts/migrations/seed_estimates_config.py).
- Missing base URL / bearer / HMAC secret → FAIL-CLOSED: CRITICAL + ERROR
  heartbeat, nothing polled.
- 401 anywhere → the SAME bearer fails every estimate route, so the cycle STOPS:
  CRITICAL (`estimate_bearer_rejected`) + ERROR heartbeat.
- Per-row fences: PERMANENT (integrity / screen-refused / wrong doc type) →
  Review-Queue row + one-shot flag (state `estimate_poll_flagged.json` — delete an
  entry to retry after fixing the cause); TRANSIENT (SmartsheetError / BoxError /
  PortalTransportError) → ERROR-logged, row stays claimed/serviceable, next cycle
  retries. One bad row never kills the cycle (`estimate_service_failed`).

Consumers
---------
- launchd `org.solutionsmith.its.estimate-poll` (StartInterval 120s default;
  RunAtLoad).
- Watchdog Check C marker (`estimate_poll`) + ITS_Daemon_Health row
  (shared.heartbeat).
- §43 runbook: docs/runbooks/estimate_import_path.md.
"""
from __future__ import annotations

import base64
import binascii
import dataclasses
import fcntl
import hashlib
import io
import json
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from po_materials import (
    estimate_classify,
    estimate_log,
    estimate_preview,
    po_attach_screen,
    po_naming,
)
from safety_reports import safety_naming
from shared import (
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
from shared.creds_resolution import TransientUnavailable
from shared.error_log import Severity, its_error_log
from shared.heartbeat import HeartbeatReporter, HeartbeatStatus
from shared.kill_switch import require_active
from shared.required_config import ConfigKey, resolve_and_log

SCRIPT_NAME = "po_materials.estimate_poll"
WORKSTREAM = "po_materials"

# ITS_Config keys (read under Workstream='po_materials' except the two SHARED
# safety_reports-owned keys — the po_poll ownership pattern).
CFG_POLLING_ENABLED = "po_materials.estimate_poll.polling_enabled"
CFG_MAX_PAGES_PREVIEW = "po_materials.estimate_poll.max_pages_preview"
# Extraction-ladder keys (PR-B / E4-E6). The three gates ship FALSE (dark);
# model/base-URL/threshold/timeout are the Tier-2 local-Ollama pins.
CFG_TIER1_ENABLED = "po_materials.estimate_extract.tier1_enabled"
# NOTE the namespace: every tier key is prefixed `estimate_extract.*`, but Tier 1 lives in
# estimate_parse.py — estimate_extract.py is the TIER-2 local-LLM module. Keeping the new
# xlsx key in the same namespace is deliberate (one prefix for the whole ladder); do not
# infer a tier's implementation from its config prefix.
CFG_TIER1_XLSX_ENABLED = "po_materials.estimate_extract.tier1_xlsx_enabled"
CFG_TIER2_ENABLED = "po_materials.estimate_extract.tier2_enabled"
CFG_OCR_ENABLED = "po_materials.estimate_extract.ocr_enabled"
CFG_EXTRACT_MODEL = "po_materials.estimate_extract.model"
CFG_OLLAMA_BASE_URL = "po_materials.estimate_extract.ollama_base_url"
CFG_CONFIDENCE_THRESHOLD = "po_materials.estimate_extract.confidence_threshold"
CFG_EXTRACT_TIMEOUT = "po_materials.estimate_extract.timeout_seconds"
# The §34 screener's optional ClamAV layer — REUSES the existing PO gate (one
# scanner posture across both doc pools; seeded false by seed_po_materials_config).
CFG_ATTACH_CLAMAV = "po_materials.po_attach_screen.clamav_enabled"
CFG_WORKER_BASE_URL = "safety_reports.portal.worker_base_url"  # shared with portal_poll
CFG_WORKER_BASE_URL_WORKSTREAM = "safety_reports"

# Keychain entry names (NOT secrets). The estimate bearer mirrors the Worker's
# PORTAL_ESTIMATE_API_TOKEN (privilege-separated per red-team #1); the HMAC secret
# is the SAME payload secret the Worker signs with (domain separation, not key
# separation, isolates the est:v1 protocol).
KC_EST_TOKEN = "ITS_PORTAL_ESTIMATE_TOKEN"  # noqa: S105 — Keychain entry NAME, not a secret
KC_HMAC_SECRET = "ITS_PORTAL_HMAC_SECRET"  # noqa: S105 — Keychain entry NAME, not a secret

DEFAULT_POLLING_ENABLED = False  # ships dark; the operator flips the seeded row
DEFAULT_MAX_PAGES_PREVIEW = 12
POLL_INTERVAL_SECONDS = 120  # registration metadata; mirrors the launchd StartInterval

# Extraction-ladder defaults (PR-B). Gates FALSE (dark-ship reflex — the seeded
# ITS_Config rows are the operator's switches, scripts/migrations/
# seed_estimates_config.py); the Tier-2 pins mirror the seeded values.
DEFAULT_TIER1_ENABLED = False
DEFAULT_TIER1_XLSX_ENABLED = False
DEFAULT_TIER2_ENABLED = False
DEFAULT_OCR_ENABLED = False
DEFAULT_EXTRACT_MODEL = "qwen3.5:9b"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_CONFIDENCE_THRESHOLD = 0.75
DEFAULT_EXTRACT_TIMEOUT_S = 600
# At most ONE Tier-2 (local-LLM) document per cycle — an attempt consumes the
# budget whether or not it succeeds (a wedged model must not serialize the pool).
TIER2_DOCS_PER_CYCLE = 1

# Box filing leaf under the PO root's per-job folder (§45 find-or-create at every
# level; the PO lane's OWN tree — po_naming.CFG_BOX_PORTAL_ROOT → <job> →
# "Vendor Quotes").
VENDOR_QUOTES_SUBFOLDER = "Vendor Quotes"

# #336 — every ITS_Config key this daemon resolves at RUNTIME. The declared-but-not-
# runtime-read *.poll_interval_seconds key is deliberately EXCLUDED (install.sh bakes
# it into the plist; the daemon never reads it) — same posture as po_poll.
REQUIRED_CONFIG: list[ConfigKey] = [
    ConfigKey(CFG_POLLING_ENABLED, WORKSTREAM, DEFAULT_POLLING_ENABLED, "bool"),
    ConfigKey(
        CFG_MAX_PAGES_PREVIEW, WORKSTREAM, DEFAULT_MAX_PAGES_PREVIEW, "int",
        description=(
            "Max pages rendered as disposition-screen previews per estimate "
            "(Quartz via the estimate_sandbox child)."
        ),
    ),
    ConfigKey(
        CFG_ATTACH_CLAMAV, WORKSTREAM, False, "bool",
        description=(
            "Optional ClamAV layer of the §34 doc screener (po_attach_screen L3), "
            "SHARED with po_poll's attachment pass. Default OFF."
        ),
    ),
    ConfigKey(
        CFG_WORKER_BASE_URL, CFG_WORKER_BASE_URL_WORKSTREAM, "", "str",
        description="Shared Worker base URL; owned by safety_reports, read here too.",
    ),
    # The PO lane's OWN Box root; owned by po_naming (the config-dictionary
    # description lives on po_poll's declaration — one owner's prose per key).
    # Clean estimates file under ROOT→<job>→'Vendor Quotes'.
    ConfigKey(
        po_naming.CFG_BOX_PORTAL_ROOT, po_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM, "", "str",
    ),
    ConfigKey(
        CFG_TIER1_ENABLED, WORKSTREAM, DEFAULT_TIER1_ENABLED, "bool",
        description=(
            "Gate for the Tier-1 deterministic native-text extraction "
            "(estimate_parse template→generic ladder). Ships FALSE (dark)."
        ),
    ),
    ConfigKey(
        CFG_TIER1_XLSX_ENABLED, WORKSTREAM, DEFAULT_TIER1_XLSX_ENABLED, "bool",
        description=(
            "Gate for the Tier-1 deterministic VENDOR-SPREADSHEET extraction "
            "(estimate_parse.parse_xlsx_estimate: sandboxed openpyxl grid -> the same "
            "generic-table line logic as the PDF tier, with doc-level totals read from "
            "the CELL grid). NO AI. Separate from tier1_enabled because it is a distinct "
            "parse path with its own failure modes. Turning it ON lets a vendor .xlsx "
            "quote be extracted instead of hand-keyed; turning it OFF returns those "
            "documents to needs_review — no other lane is affected. Independent of "
            "Tier 0, which claims ITS's own HMAC-verified quote form first."
        ),
    ),
    ConfigKey(
        CFG_TIER2_ENABLED, WORKSTREAM, DEFAULT_TIER2_ENABLED, "bool",
        description=(
            "Gate for the Tier-2 LOCAL-Ollama schema-constrained extraction "
            "(estimate_extract; at most one Tier-2 doc per cycle). Ships FALSE (dark)."
        ),
    ),
    ConfigKey(
        CFG_OCR_ENABLED, WORKSTREAM, DEFAULT_OCR_ENABLED, "bool",
        description=(
            "Gate for the macOS-Vision OCR pass (estimate_ocr) feeding Tier-2 on "
            "SCANNED documents. Ships FALSE (dark)."
        ),
    ),
    ConfigKey(
        CFG_EXTRACT_MODEL, WORKSTREAM, DEFAULT_EXTRACT_MODEL, "str",
        description=(
            "Pinned local Ollama model for Tier-2 extraction; swapping it re-runs "
            "the offline corpus eval to re-qualify (ADR-0004 decision 1)."
        ),
    ),
    ConfigKey(
        CFG_OLLAMA_BASE_URL, WORKSTREAM, DEFAULT_OLLAMA_BASE_URL, "str",
        description=(
            "Local Ollama base URL for Tier-2 extraction (localhost-only — vendor "
            "pricing never leaves the machine)."
        ),
    ),
    ConfigKey(
        CFG_CONFIDENCE_THRESHOLD, WORKSTREAM, DEFAULT_CONFIDENCE_THRESHOLD, "float",
        description=(
            "Minimum extraction confidence to post 'extracted'; below it the doc "
            "degrades to needs_review (the disposition screen's manual Tier-3)."
        ),
    ),
    ConfigKey(
        CFG_EXTRACT_TIMEOUT, WORKSTREAM, DEFAULT_EXTRACT_TIMEOUT_S, "int",
        description=(
            "Wall-clock budget in seconds for one Tier-2 extraction call "
            "(keep_alive=0 load-on-demand can make the first call slow)."
        ),
    ),
]

# State paths. HEARTBEAT_ROW_STATE_PATH is the SHARED row-id cache (ARCH-2).
STATE_DIR = Path.home() / "its" / "state"
HEARTBEAT_PATH = STATE_DIR / "estimate_poll_heartbeat.txt"
LOCK_PATH = STATE_DIR / "estimate_poll.lock"
# Sustained pending-fetch escalation (2026-07-20 forensic: a 21h every-cycle ERROR storm
# was invisible on every CRITICAL-keyed fire surface — shared/sustained_failure.py).
_FETCH_FAILS = sustained_failure.SustainedFailureCounter(
    STATE_DIR / "estimate_pending_fetch_failures.json", SCRIPT_NAME, "estimate_pending_fetch_counter_failed",
)
HEARTBEAT_ROW_STATE_PATH = STATE_DIR / "heartbeat_row_ids.json"
# One-shot flag state for PERMANENTLY-refused pool rows (`{estimate_id: reason}`).
# A flagged row is skipped every subsequent cycle (no 120s Review-Queue spam); the
# operator remediates by fixing the cause and deleting the entry (or the file).
EST_FLAGGED_PATH = STATE_DIR / "estimate_poll_flagged.json"
MAX_EST_FLAGS = 500  # drained/settled entries are dead weight only — cap the file

DAEMON_NAME = "po_materials.estimate_poll"
_REGISTRATION_SOURCE_ID = "Safety Portal Worker /api/po/estimates/internal/pending"

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
WATCHDOG_JOB_SLUG = "estimate_poll"

MIME_PDF = "application/pdf"


@dataclass(frozen=True)
class EstimatePollStats:
    """Summary of one poll_once() invocation."""

    skipped_disabled: bool = False
    skipped_locked: bool = False
    halted_no_creds: bool = False
    # base URL temporarily unreadable (Smartsheet blip / circuit OPEN) — transient,
    # distinct from halted_no_creds, which is a genuine misconfig that pages.
    halted_transient: bool = False
    bearer_rejected: bool = False
    scanned: int = 0
    filed: int = 0              # docs filed to Box + Estimate_Log + result posted
    extracted: int = 0          # of filed: docs posted 'extracted' (a ladder tier hit)
    refused: int = 0            # screen/doc-type refusals posted
    integrity_failures: int = 0  # bad HMAC / digest / chunk-set (no result post)
    skipped_flagged: int = 0    # rows already one-shot-flagged in a prior cycle
    previews_posted: int = 0
    errors: int = 0             # transient failures (row stays serviceable)


class _BearerRejectedError(Exception):
    """Internal: a 401 anywhere — the SAME bearer fails every estimate route, stop
    the cycle."""


@dataclass(frozen=True)
class _EstCreds:
    """Resolved credentials with NAMED fields (the portal_poll CodeQL taint
    rationale: named fields keep the bearer/secret taint off base_url and
    everything logged)."""

    base_url: str
    bearer: str
    secret: str


# ---- Config readers (replicated per preservation, mirror po_poll) ----------------


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


def _read_int_setting(key: str, fallback: int) -> int:
    raw = _read_str_setting(key, str(fallback))
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return fallback


def _polling_enabled() -> bool:
    return _read_bool_setting(CFG_POLLING_ENABLED, DEFAULT_POLLING_ENABLED)


def _max_pages_preview() -> int:
    value = _read_int_setting(CFG_MAX_PAGES_PREVIEW, DEFAULT_MAX_PAGES_PREVIEW)
    return max(1, min(value, 50))


def _attach_clamav_enabled() -> bool:
    """SHARED gate `po_materials.po_attach_screen.clamav_enabled` (default OFF)."""
    return _read_bool_setting(CFG_ATTACH_CLAMAV, False)


def _read_float_setting(key: str, fallback: float) -> float:
    raw = _read_str_setting(key, str(fallback))
    try:
        return float(raw.strip())
    except (TypeError, ValueError):
        return fallback


@dataclass(frozen=True)
class _TierConfig:
    """Per-cycle snapshot of the extraction-ladder ITS_Config knobs (PR-B)."""

    tier1_enabled: bool
    tier1_xlsx_enabled: bool
    tier2_enabled: bool
    ocr_enabled: bool
    model: str
    ollama_base_url: str
    confidence_threshold: float
    timeout_seconds: int


def _resolve_tier_config() -> _TierConfig:
    """One read of every ladder knob per cycle (startup observability is the
    #336 resolve_and_log pass; this is the runtime behavior read)."""
    return _TierConfig(
        tier1_enabled=_read_bool_setting(CFG_TIER1_ENABLED, DEFAULT_TIER1_ENABLED),
        tier1_xlsx_enabled=_read_bool_setting(
            CFG_TIER1_XLSX_ENABLED, DEFAULT_TIER1_XLSX_ENABLED
        ),
        tier2_enabled=_read_bool_setting(CFG_TIER2_ENABLED, DEFAULT_TIER2_ENABLED),
        ocr_enabled=_read_bool_setting(CFG_OCR_ENABLED, DEFAULT_OCR_ENABLED),
        model=_read_str_setting(CFG_EXTRACT_MODEL, DEFAULT_EXTRACT_MODEL),
        ollama_base_url=_read_str_setting(CFG_OLLAMA_BASE_URL, DEFAULT_OLLAMA_BASE_URL),
        confidence_threshold=_read_float_setting(
            CFG_CONFIDENCE_THRESHOLD, DEFAULT_CONFIDENCE_THRESHOLD
        ),
        timeout_seconds=max(
            1, _read_int_setting(CFG_EXTRACT_TIMEOUT, DEFAULT_EXTRACT_TIMEOUT_S)
        ),
    )


# ---- Lock + heartbeat + marker seams (mirror po_poll) -----------------------------


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


# ---- One-shot flag state (the po_poll flag pattern) -------------------------------


def _load_flags() -> dict[str, str]:
    """Load the one-shot flag set `{estimate_id: reason}`. {} on any read error
    (fail-open: the only cost is one redundant re-flag, never a missed alert)."""
    if not EST_FLAGGED_PATH.exists():
        return {}
    try:
        parsed = json.loads(EST_FLAGGED_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _persist_flags(flags: dict[str, str]) -> None:
    """Atomically persist the flag set (capped). Lock-timeout fails OPEN with a WARN —
    a lost flag set costs a duplicate Review-Queue flag next cycle, never a missed one."""
    if len(flags) > MAX_EST_FLAGS:
        flags = dict(list(flags.items())[-MAX_EST_FLAGS:])
    EST_FLAGGED_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with state_io.with_path_lock(EST_FLAGGED_PATH):
            state_io.atomic_write_json(EST_FLAGGED_PATH, flags)
    except state_io.StateLockTimeoutError:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"could not acquire lock on {EST_FLAGGED_PATH} after retries; "
            f"estimate flag set not persisted",
            error_code="estimate_flags_persist_failed",
        )


# ---- Credential resolution (fail-CLOSED) ------------------------------------------


def _resolve_credentials() -> _EstCreds | TransientUnavailable | None:
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
        bearer = keychain.get_secret(KC_EST_TOKEN)
    except keychain.KeychainError:
        bearer = ""
    try:
        secret = keychain.get_secret(KC_HMAC_SECRET)
    except keychain.KeychainError:
        secret = ""
    if not (base_url and bearer and secret):
        return None
    return _EstCreds(base_url=base_url, bearer=bearer, secret=secret)


# ---- Chunk reassembly (the po_poll strictness, replicated per preservation) --------


def _reassemble_chunks(chunks: list[dict[str, Any]]) -> bytes:
    """Concatenate the decoded chunk bytes into the original file.

    STRICT: every chunk must agree on chunk_total, the index set must be exactly
    {0..n-1} (gap-free), and every chunk_b64 must strictly decode. Raises ValueError
    on ANY malformation — the caller treats that as an INTEGRITY failure (the chunk
    set was written atomically with the row, so a broken set is tamper or a serving
    defect, never a benign partial)."""
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


# ---- Public API -------------------------------------------------------------------


@its_error_log(SCRIPT_NAME)
@require_active
def poll_once() -> EstimatePollStats:
    """Run one estimate-servicing cycle. launchd invokes this once per StartInterval;
    idempotent across crashes (the result post is LAST — see the module docstring)."""
    # #336 startup observability (after @require_active, fail-open — never blocks).
    resolve_and_log(SCRIPT_NAME, REQUIRED_CONFIG)

    if not _polling_enabled():
        # Shipped default — an intentional dark state, not an anomaly: no
        # heartbeat/marker/log spam every 120s. The seeded ITS_Config row is the
        # operator's switch (scripts/migrations/seed_estimates_config.py).
        return EstimatePollStats(skipped_disabled=True)

    with _file_lock(LOCK_PATH) as acquired:
        if not acquired:
            error_log.log(
                Severity.INFO, SCRIPT_NAME,
                "another estimate cycle holds the lock; skipping this cycle",
                error_code="estimate_poll_lock_held",
            )
            return EstimatePollStats(skipped_locked=True)
        try:
            return _poll_inside_lock()
        finally:
            # D3 — see portal_poll: one summarized WARN row per pass that recovered on retry.
            sustained_failure.flush_retry_recovery(SCRIPT_NAME)


def _poll_inside_lock() -> EstimatePollStats:
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
            f"estimate Worker base URL temporarily unreadable ({creds.reason}) — "
            f"skipping this cycle; will retry next interval (transient, self-heals)",
            error_code="estimate_creds_transient",
        )
        _write_heartbeat()
        _write_heartbeat_row(
            status="WARN", items_processed=0,
            error_summary=f"base URL unreadable ({creds.reason}) — transient",
        )
        return EstimatePollStats(halted_transient=True)
    if creds is None:
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            (
                # Deliberately does NOT interpolate the ITS_Config key or the Keychain
                # entry NAMES (secret-store names in a log are a CodeQL clear-text trip).
                "fail-closed: missing estimate portal credentials — the Worker base "
                "URL (ITS_Config) and/or the estimate bearer + HMAC-secret Keychain "
                "entries are unset; NOT polling until fixed"
            ),
            error_code="estimate_creds_missing",
        )
        _write_heartbeat()
        _write_heartbeat_row(
            status="ERROR", items_processed=0,
            error_summary="fail-closed: estimate portal credentials missing",
        )
        return EstimatePollStats(halted_no_creds=True)

    counters: dict[str, int] = {
        "scanned": 0, "filed": 0, "extracted": 0, "refused": 0,
        "integrity_failures": 0, "skipped_flagged": 0, "previews_posted": 0,
        "errors": 0,
    }
    bearer_rejected = False
    try:
        _estimates_pass(creds, counters)
    except _BearerRejectedError:
        # A 401 anywhere: the SAME bearer fails every estimate route, so nothing
        # else can work this cycle. A bad/rotated bearer will NOT self-heal → page.
        bearer_rejected = True
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            "estimate bearer UNAUTHORIZED (401) — rejected by the Worker's "
            "requireEstimateToken tier; cycle STOPPED until the token is fixed "
            "(everything stays queued — safe re-attempt)",
            error_code="estimate_bearer_rejected",
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
    if counters["errors"] == 0 and total_flagged == 0 and not bearer_rejected:
        error_summary = None
    else:
        error_summary = (
            f"errors={counters['errors']} flagged={total_flagged}"
            + (f" standing={counters['skipped_flagged']}"
               if counters["skipped_flagged"] else "")
            + (" bearer_rejected" if bearer_rejected else "")
        )
    try:
        _write_heartbeat_row(
            status=cycle_status,
            items_processed=counters["filed"],
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
            f"estimate cycle: scanned={counters['scanned']} filed={counters['filed']} "
            f"extracted={counters['extracted']} refused={counters['refused']} "
            f"integrity={counters['integrity_failures']} "
            f"flag-skipped={counters['skipped_flagged']} "
            f"previews={counters['previews_posted']} errors={counters['errors']}"
        ),
        error_code="estimate_cycle_summary",
    )
    return EstimatePollStats(
        bearer_rejected=bearer_rejected,
        scanned=counters["scanned"],
        filed=counters["filed"],
        extracted=counters["extracted"],
        refused=counters["refused"],
        integrity_failures=counters["integrity_failures"],
        skipped_flagged=counters["skipped_flagged"],
        previews_posted=counters["previews_posted"],
        errors=counters["errors"],
    )


# ---- The single estimates pass ----------------------------------------------------


def _estimates_pass(creds: _EstCreds, counters: dict[str, int]) -> None:
    """Drain the estimate pool: claim → verify → screen → classify → file → post."""
    try:
        pending = portal_client.get_estimates_pending(creds.base_url, creds.bearer)
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
                f"(rows left queued; see docs/runbooks/estimate_import_path.md): {exc!r}",
                error_code="estimate_pending_fetch_sustained",
            )
        else:
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"failed to GET estimates pending (rows left for next cycle); {n} consecutive: {exc!r}",
            error_code="estimate_pending_fetch_failed",
            )
        return
    _FETCH_FAILS.reset()  # a successful fetch clears the sustained-outage counter
    if not pending:
        return
    clamav_enabled = _attach_clamav_enabled()
    max_pages = _max_pages_preview()
    tiers = _resolve_tier_config()
    # The Tier-2 per-cycle budget (ONE local-LLM doc per cycle; an attempt
    # consumes it) — mutable so every row in this pass shares the ledger.
    tier2_budget = {"remaining": TIER2_DOCS_PER_CYCLE}
    flags = _load_flags()
    flags_before = dict(flags)
    try:
        for row in pending:
            counters["scanned"] += 1
            _service_one_estimate(
                row, creds, counters, flags, clamav_enabled, max_pages,
                tiers, tier2_budget,
            )
    finally:
        # Persist-on-mutation via snapshot compare, in a finally — NOT a
        # loop-exit bool. A bearer abort (_BearerRejectedError out of the
        # refused-post / preview-post helpers) leaves the loop AFTER
        # _refuse_*/_handle_integrity_failure already wrote the in-flight row's
        # one-shot flag, and a return-value protocol cannot see that mutation
        # (the raise skips the return). The finally guarantees every flag
        # written this cycle — including the one the aborting row just earned —
        # reaches disk, so a fixed-later bearer never replays a 120s
        # CRITICAL/Review-Queue re-alert storm for already-flagged rows.
        if flags != flags_before:
            _persist_flags(flags)


def _service_one_estimate(
    row: dict[str, Any],
    creds: _EstCreds,
    counters: dict[str, int],
    flags: dict[str, str],
    clamav_enabled: bool,
    max_pages: int,
    tiers: _TierConfig,
    tier2_budget: dict[str, int],
) -> bool:
    """Claim, verify, screen, classify, and disposition ONE pool row. Returns True
    iff the one-shot flag set was mutated (informational — the caller persists via
    a snapshot compare in a finally, so a flag written just before a bearer abort
    is persisted even though the raise skips this return).

    Verify-before-anything (Invariant 2): the est:v1 HMAC binds the row's fields
    AND the content digest; the sha256 recompute over the reassembled chunks
    extends the signature to the bytes. Either failing → CRITICAL + security
    Review-Queue row + one-shot flag; the bytes stay in D1 for forensics (NO
    result post)."""
    raw_id = row.get("id")
    if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0:
        counters["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"pending estimate row has a missing/malformed id ({raw_id!r}); skipping",
            error_code="estimate_row_no_id",
        )
        return False
    est_id = raw_id
    flag_key = str(est_id)
    if flag_key in flags:
        counters["skipped_flagged"] += 1
        return False
    est_uuid = str(row.get("est_uuid") or "")
    job_no = str(row.get("job_no") or "")
    job_name = str(row.get("job_name") or "")
    filename = str(row.get("filename") or "")
    declared_mime = str(row.get("declared_mime") or "")
    uploaded_by = str(row.get("uploaded_by") or "")
    provided_hmac = str(row.get("hmac") or "")
    raw_size = row.get("size_bytes")
    size_bytes = raw_size if isinstance(raw_size, int) and not isinstance(raw_size, bool) else -1
    signed_sha256 = str(row.get("sha256") or "")
    correlation_id = uuid.uuid4().hex[:12]

    try:
        # 1 — claim FIRST (the attachment-pool claim semantics): a crash after this
        # leaves an observable 'claimed' row that re-serves next cycle.
        portal_client.claim_estimate(creds.base_url, creds.bearer, estimate_id=est_id)

        # 2 — pull + reassemble the bytes (the ONLY Mac-ward byte flow).
        chunks = portal_client.get_estimate_chunks(
            creds.base_url, creds.bearer, estimate_id=est_id
        )
        try:
            data = _reassemble_chunks(chunks)
        except ValueError as exc:
            counters["integrity_failures"] += 1
            _handle_integrity_failure(
                est_id, est_uuid, filename, uploaded_by,
                f"chunk reassembly failed: {exc}", correlation_id, flags,
            )
            return True

        # 3 — verify: the est:v1 HMAC over the served fields, then the content
        # digest + size against the SIGNED values (never screen unverified bytes).
        if not portal_hmac.verify_po_estimate(
            creds.secret, provided_hmac,
            est_uuid=est_uuid, job_no=job_no, filename=filename,
            declared_mime=declared_mime, size_bytes=size_bytes, sha256=signed_sha256,
        ):
            counters["integrity_failures"] += 1
            _handle_integrity_failure(
                est_id, est_uuid, filename, uploaded_by,
                "HMAC verification FAILED", correlation_id, flags,
            )
            return True
        if len(data) != size_bytes or hashlib.sha256(data).hexdigest() != signed_sha256:
            counters["integrity_failures"] += 1
            _handle_integrity_failure(
                est_id, est_uuid, filename, uploaded_by,
                "content digest/size disagrees with the signed values",
                correlation_id, flags,
            )
            return True

        # 4 — §34 screen (the SAME doc screener as PO attachments: L1
        # magic/consistency → L2 structural → L3 config-gated ClamAV on raw bytes).
        result = po_attach_screen.screen_attachment(
            filename, declared_mime, data, clamav_enabled=clamav_enabled
        )
        if result.disposition != "clean":
            _refuse_screened(
                est_id, est_uuid, job_no, filename, uploaded_by, signed_sha256,
                result, correlation_id, flags,
            )
            counters["refused"] += 1
            _post_refused_result(
                creds, est_id, est_uuid,
                f"screen:{result.disposition}:{result.layer}:{result.detail}"[:200],
                correlation_id,
                error_counter=counters,
            )
            return True

        # 5 — deterministic doc-type gate (pdfplumber INSIDE the sandbox child;
        # non-PDF uploads classify 'other' → the ladder head / needs_review,
        # never refused).
        if declared_mime == MIME_PDF:
            pages = estimate_classify.extract_pages_text(data)
            doc_type, confidence = estimate_classify.classify_doc_type(pages)
        else:
            pages = []
            doc_type, confidence = ("other", 0.0)
        if doc_type in estimate_classify.REFUSED_DOC_TYPES:
            _refuse_wrong_doc_type(
                est_id, est_uuid, job_no, filename, uploaded_by, signed_sha256,
                doc_type, confidence, correlation_id, flags,
            )
            counters["refused"] += 1
            _post_refused_result(
                creds, est_id, est_uuid, f"wrong_doc_type:{doc_type}", correlation_id,
                error_counter=counters,
            )
            return True

        # 5b — the EXTRACTION LADDER HEAD (PR-B, ADR-0004 E4-E6): Tier-0 filled
        # form → Tier-1 deterministic parse → Tier-2 local Ollama, all gated;
        # None = every tier declined/failed → the PR-A needs_review default.
        ladder = _attempt_extraction_ladder(
            est_uuid=est_uuid, filename=filename, declared_mime=declared_mime,
            data=data, pages=pages, doc_type=doc_type, secret=creds.secret,
            tiers=tiers, tier2_budget=tier2_budget, correlation_id=correlation_id,
        )

        # 6 — Box filing: §45 find-or-create the PO ROOT→job→"Vendor
        # Quotes", §47 version-on-conflict under the est_uuid-prefixed name (the
        # uuid disambiguates same-named uploads AND makes a crash-retry version
        # instead of duplicate).
        folder_id = _resolve_quotes_box_folder(job_name or job_no)
        if folder_id is None:
            counters["errors"] += 1
            error_log.log(
                Severity.ERROR, SCRIPT_NAME,
                f"PO Box root unresolved (ITS_Config "
                f"{po_naming.CFG_BOX_PORTAL_ROOT} unset) — estimate {est_uuid} "
                f"left claimed until the root is configured",
                error_code="estimate_box_root_unresolved",
                correlation_id=correlation_id,
            )
            return False
        filed_name = f"{est_uuid} - {filename}"
        file_info = box_client.upload_bytes_or_new_version(folder_id, filed_name, data)
        box_file_id = str(file_info["id"])

        # 7 — Estimate_Log ledger row (idempotent by uuid — a crash-retry stamps
        # instead of duplicating). A ladder hit stamps 'extracted' + the
        # body-derived vendor/quote identity; otherwise needs_review as PR-A.
        if ladder is not None:
            ledger_status = estimate_log.STATUS_EXTRACTED
            ledger_doc_type = str(ladder.payload.get("doc_type") or doc_type)
            ledger_detail = ladder.detail
        else:
            ledger_status = estimate_log.STATUS_NEEDS_REVIEW
            ledger_doc_type = doc_type
            ledger_detail = f"doc_type={doc_type} confidence={confidence}"
        existing_ledger = estimate_log.find_row_by_uuid(est_uuid)
        if existing_ledger is None:
            ledger_row_id = estimate_log.append_row(
                est_uuid=est_uuid,
                job_no=job_no,
                filename=filename,
                doc_type=ledger_doc_type,
                status=ledger_status,
                sha256=signed_sha256,
                box_file_id=box_file_id,
                detail=ledger_detail,
                vendor_name=ladder.vendor_name if ladder else "",
                quote_number=ladder.quote_number if ladder else "",
            )
        else:
            estimate_log.update_status(
                est_uuid, ledger_status, box_file_id=box_file_id,
                vendor_name=ladder.vendor_name if ladder else None,
                quote_number=ladder.quote_number if ladder else None,
            )
            ledger_row_id = int(existing_ledger["_row_id"])
        # Inline attach of the filed ORIGINAL on the ledger row (#79/#98 attach
        # parity, wiring audit 2026-08-12 — every other procurement ledger carries
        # its document; Estimate_Log held only a bare Box file id). Every service,
        # self-healing: the est_uuid-prefixed name is deterministic, so a replay
        # replaces rather than duplicates. Content-typed by the verified MIME so an
        # xlsx quote is not mislabeled application/pdf.
        _attach_estimate_best_effort(
            ledger_row_id, filed_name, data, declared_mime, correlation_id
        )

        # 8 — disposition-screen previews (Quartz via the sandbox; Pillow
        # re-encoded), BEST-EFFORT: a preview failure degrades the doc to the
        # explicit no-preview path — it never blocks filing.
        if declared_mime == MIME_PDF:
            counters["previews_posted"] += _post_previews_best_effort(
                creds, est_id, est_uuid, data, max_pages, correlation_id
            )

        # 9 — the result post, LAST (claimed→extracted|needs_review; a crash
        # before this line re-serves the row and every step above is idempotent).
        if ladder is not None:
            portal_client.post_estimate_result(
                creds.base_url, creds.bearer,
                estimate_id=est_id, status="extracted", box_file_id=box_file_id,
                detail=ladder.detail, extraction=ladder.payload,
                # R4 — the verified Tier-0 rfq-form:v1 binding (None for Tiers 1/2).
                # The Worker auto-binds only when BOTH are present AND resolve.
                rfq_number=ladder.rfq_number, rfq_vendor_key=ladder.rfq_vendor_key,
            )
            counters["extracted"] += 1
        else:
            portal_client.post_estimate_result(
                creds.base_url, creds.bearer,
                estimate_id=est_id, status="needs_review", box_file_id=box_file_id,
            )
        counters["filed"] += 1
        error_log.log(
            Severity.INFO, SCRIPT_NAME,
            f"filed estimate {est_uuid} ({filename!r}, job {job_no}, "
            f"doc_type={ledger_doc_type}) → Box + "
            f"Estimate_Log + {'extracted' if ladder is not None else 'needs_review'}",
            error_code="estimate_filed",
            correlation_id=correlation_id,
        )
        return False
    except _BearerRejectedError:
        # Already-translated 401 re-raised by the helpers (_post_refused_result /
        # _post_previews_best_effort). MUST propagate to _poll_inside_lock's
        # cycle-stop handler — the generic per-row fence below would otherwise
        # swallow it as an ordinary estimate_service_failed, leave
        # stats.bearer_rejected False, and let the dead bearer re-alert every
        # 120s instead of firing the one estimate_bearer_rejected CRITICAL.
        raise
    except portal_client.PortalAuthError as exc:
        raise _BearerRejectedError from exc
    except (
        smartsheet_client.SmartsheetError,
        box_client.BoxError,
        portal_client.PortalTransportError,
    ) as exc:
        counters["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"transient failure servicing estimate {est_id} ({est_uuid}; stays "
            f"serviceable for next cycle): {type(exc).__name__}: {exc!r}",
            error_code="estimate_transient",
            correlation_id=correlation_id,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — per-row fence; one bad row never kills the cycle
        counters["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"unexpected failure servicing estimate {est_id} ({est_uuid}): "
            f"{type(exc).__name__}: {exc!r}",
            error_code="estimate_service_failed",
            correlation_id=correlation_id,
        )
        return False


# ---- The extraction ladder (PR-B, ADR-0004 E4-E6) ---------------------------------
#
# Extraction-core module contracts (the sibling E4/E5 slice; imported LAZILY
# inside their gated tiers so the dark-shipped daemon never needs them — an
# absent module is a WARN + needs_review degrade, never a daemon death):
#   estimate_parse.parse_native(data) -> ParsedPdf | None    (sandboxed pdfplumber)
#   estimate_parse.load_vendor_templates() + parse_with_template(parsed, tpl) +
#     parse_generic_table(parsed) -> ExtractionResult | None (Tier 1 — check_math
#     applied inside; needs_review = not math_ok)
#   estimate_ocr.ocr_pages(data) -> list[str]                (Tier-2 scan feed)
#   estimate_extract.extract(pages, *, model, base_url, timeout_s,
#     confidence_threshold) -> ExtractionResult | None       (Tier 2 — LOCAL Ollama;
#     schema-gated + math-checked + string-field-anomaly-swept internally)
# The daemon converts an ACCEPTED ExtractionResult into the Worker result-route
# extraction body (`_worker_payload_from_result`) and stamps the INTEGER tier.


@dataclass(frozen=True)
class _LadderOutcome:
    """One successful ladder extraction, ready to post.

    `payload` is the Worker result-route extraction body (parseExtraction
    contract); `detail` the ≤200-char top-level note (for Tier 0 it carries the
    verified rfq binding); `vendor_name`/`quote_number` feed the Estimate_Log identity
    stamp. `rfq_number`/`rfq_vendor_key` (R4) are populated ONLY for a VERIFIED Tier-0
    filled-form parse (an `rfq-form:v1` token that matched) — they are promoted to the
    top-level result-post fields feeding the Worker's `po_estimates.rfq_id` /
    `rfq_vendor_key` auto-bind (which flips the `rfq_vendors` row to responded). Tiers
    1/2 leave them None (an ordinary vendor document has no signed RFQ identity)."""

    payload: dict[str, Any]
    detail: str
    vendor_name: str
    quote_number: str
    rfq_number: str | None = None
    rfq_vendor_key: str | None = None


def _xlsx_has_form_meta(data: bytes) -> bool:
    """Cheap Tier-0 pre-check: does this (screen-clean) xlsx container carry the
    `_ITS_META` sheet? Reads only xl/workbook.xml, bounded; False on anything odd."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            info = zf.getinfo("xl/workbook.xml")
            if info.file_size > 2_000_000:
                return False
            return b"_ITS_META" in zf.read("xl/workbook.xml")
    except (zipfile.BadZipFile, KeyError, OSError, ValueError):
        return False


def _is_scanned(pages: list[str]) -> bool:
    """A PDF with no native text on any extracted page is treated as SCANNED —
    the corpus's Nassau/Apricus class (ADR-0004: ~20% of the corpus)."""
    return not any(p.strip() for p in pages)


def _strings_only(node: Any) -> Any:
    """Project an extraction payload down to its STRING fields for the Layer-5
    anomaly sweep (ADR-0004 decision 8 — cents integers would trip the numeric
    sentinel and burn the tripwire; string fields are where injection text lives)."""
    if isinstance(node, dict):
        return {k: _strings_only(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_strings_only(v) for v in node]
    return node if isinstance(node, str) else None


# Worker result-route contract bounds (safety_portal/worker/po_estimates.ts
# parseExtraction) — the converter clamps/refuses BEFORE the post so a malformed
# extraction degrades to needs_review instead of 400-looping the transient fence.
_WORKER_DOC_TYPES = frozenset(
    {"quote", "estimate", "proposal", "invoice", "ap_report", "filled_form", "other"}
)
_WORKER_MAX_LINES = 500
_WORKER_MAX_QTY = 1_000_000_000
_WORKER_MAX_PAYLOAD_JSON = 400_000
_WORKER_MAX_ANOMALIES = 4000


def _opt_cents_bounded(value: Any) -> int | None:
    """A non-negative int survives; anything else (None/float/negative/bool) → None
    (the Worker's optCents would 400 the whole post otherwise)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 9_007_199_254_740_991 else None


def _worker_payload_from_result(
    result: Any, tier: int, est_uuid: str, correlation_id: str,
) -> dict[str, Any] | None:
    """Convert an extraction-core `ExtractionResult` into the Worker result-route
    extraction body, defensively bounded to the parseExtraction contract.

    None (degrade to needs_review) when the result has no usable lines, too many
    lines, a NEGATIVE money value anywhere (credit/adjustment lines need human
    eyes, and the Worker would reject the post), or an oversized payload. The
    Worker remains the authoritative validator — its 400 surfaces as
    PortalTransportError — but this converter keeps a malformed extraction from
    retry-looping a claimed row."""

    def _degrade(reason: str) -> None:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"tier-{tier} extraction for {est_uuid} not postable ({reason}) — "
            f"degrading to needs_review",
            error_code="estimate_extraction_malformed",
            correlation_id=correlation_id,
        )

    line_items = list(getattr(result, "line_items", []) or [])
    if not line_items:
        _degrade("no lines")
        return None
    if len(line_items) > _WORKER_MAX_LINES:
        _degrade(f"{len(line_items)} lines > {_WORKER_MAX_LINES}")
        return None

    lines: list[dict[str, Any]] = []
    for index, li in enumerate(line_items):
        description = str(li.description or "").strip()[:512]
        if not description:
            _degrade(f"line {index + 1} has no description")
            return None
        for money in (li.unit_cost_cents, li.extended_cents):
            if money is not None and (not isinstance(money, int) or money < 0):
                _degrade(f"line {index + 1} money out of contract ({money!r})")
                return None
        qty = li.qty
        if qty is not None and not (
            isinstance(qty, (int, float)) and 0 <= float(qty) <= _WORKER_MAX_QTY
        ):
            qty = None
        lines.append({
            "position": index + 1,
            "section": (str(li.section).strip()[:256] or None) if li.section else None,
            "part_number": (str(li.part_number).strip()[:64] or None) if li.part_number else None,
            "description": description,
            "qty": float(qty) if qty is not None else None,
            "unit": (str(li.unit).strip()[:32] or None) if li.unit else None,
            "unit_cost_cents": li.unit_cost_cents,
            "extended_cents": li.extended_cents,
            "math_ok": 1 if li.math_ok else 0,  # parseMathOk: INTEGER 0|1, never bool
            "line_note": (str(li.line_note).strip()[:256] or None) if li.line_note else None,
        })

    header_money: dict[str, int | None] = {}
    for key in ("subtotal_cents", "tax_cents", "freight_cents", "misc_cents",
                "grand_total_cents"):
        raw_value = getattr(result, key, None)
        if raw_value is not None and (not isinstance(raw_value, int) or raw_value < 0):
            _degrade(f"{key} out of contract ({raw_value!r})")
            return None
        header_money[key] = _opt_cents_bounded(raw_value)

    confidence = getattr(result, "confidence", None)
    if not isinstance(confidence, (int, float)) or not (0 <= float(confidence) <= 1):
        confidence = None

    payload_json = json.dumps(
        dataclasses.asdict(result), ensure_ascii=False, separators=(",", ":"),
        default=str,
    )
    if len(payload_json) > _WORKER_MAX_PAYLOAD_JSON:
        _degrade(f"payload_json {len(payload_json)} bytes > cap")
        return None

    anomalies = "; ".join(
        [*getattr(result, "math_flags", []), *getattr(result, "anomaly_flags", [])]
    ).strip()[:_WORKER_MAX_ANOMALIES] or None

    doc_type = str(getattr(result, "doc_type", "") or "other")
    if doc_type not in _WORKER_DOC_TYPES:
        doc_type = "other"

    def _opt_short(value: Any, cap: int) -> str | None:
        if value is None:
            return None
        text = str(value).strip()[:cap]
        return text or None

    return {
        "tier": tier,
        "schema_version": "1.0.0",
        "doc_type": doc_type,
        "vendor_name": _opt_short(getattr(result, "vendor_name", None), 256),
        "quote_number": _opt_short(getattr(result, "quote_number", None), 64),
        "revision_label": _opt_short(getattr(result, "revision_label", None), 64),
        "quote_date": _opt_short(getattr(result, "quote_date", None), 64),
        "valid_until": _opt_short(getattr(result, "valid_until", None), 64),
        **header_money,
        "math_ok": 1 if getattr(result, "math_ok", False) else 0,
        "confidence": float(confidence) if confidence is not None else None,
        "payload_json": payload_json,
        "anomalies": anomalies,
        "lines": lines,
    }


def _anomaly_gate(
    payload: dict[str, Any], tier: int, est_uuid: str, correlation_id: str,
) -> bool:
    """Layer-5 sweep over the payload's STRING fields (decision 8). True = clean;
    False = flagged → the caller degrades the doc to needs_review + a
    security-flagged review row. NOT credited as a price-manipulation control —
    the human disposition accept remains the fidelity boundary (decision 3)."""
    anomalies = anomaly_logger.check(_strings_only(payload))
    if not anomalies:
        return True
    review_queue.safe_add(
        script_name=SCRIPT_NAME,
        workstream=WORKSTREAM,
        summary=(
            f"estimate: tier-{tier} extraction for {est_uuid} tripped the anomaly "
            f"tripwire ({len(anomalies)} hit(s)) — extraction DISCARDED, doc "
            f"degraded to needs_review (manual disposition)"
        ),
        payload={"est_uuid": est_uuid, "tier": tier, "anomalies": anomalies[:20]},
        sla_tier=review_queue.SlaTier.RFQ_DRAFT,
        reason=review_queue.ReviewReason.SECURITY_TRIGGER,
        severity=Severity.WARN,
        source_file=f"est:{est_uuid}",
        security_flag=True,
    )
    error_log.log(
        Severity.WARN, SCRIPT_NAME,
        f"anomaly tripwire hit on tier-{tier} extraction for {est_uuid}: "
        f"{anomalies[:5]} — extraction discarded",
        error_code="estimate_extraction_anomaly",
        correlation_id=correlation_id,
    )
    return False


def _attempt_extraction_ladder(
    *,
    est_uuid: str,
    filename: str,
    declared_mime: str,
    data: bytes,
    pages: list[str],
    doc_type: str,
    secret: str,
    tiers: _TierConfig,
    tier2_budget: dict[str, int],
    correlation_id: str,
) -> _LadderOutcome | None:
    """Run the tiered extraction ladder over ONE screened, classified document.

    None = no tier produced a posting-worthy extraction (gates off, sibling
    modules absent, parse/OCR/model failure, low confidence, anomaly hit) — the
    caller lands the doc needs_review exactly as PR-A did. NEVER raises on
    hostile input (every tier is fenced; a tier failure degrades, the outer
    per-row fence stays the backstop)."""
    # Tier 0 — our own filled quote form (always on: a deterministic round-trip
    # of OUR artifact, not an inference tier).
    if declared_mime == po_attach_screen.MIME_XLSX and _xlsx_has_form_meta(data):
        return _tier0_filled_form(data, secret, est_uuid, correlation_id)

    # Tier 1 (xlsx) — a VENDOR workbook (no _ITS_META sheet, so Tier 0 declined it).
    # Before this branch existed, the `!= MIME_PDF` bail below sat directly under Tier 0
    # and no extraction tier could ever see a spreadsheet: a real Excel quote was screened,
    # filed and then hand-keyed by the office, despite already being a typed numeric grid.
    # Gated separately from the PDF tier because it is a distinct parse path with its own
    # failure modes (openpyxl, merged cells, cell-typed totals).
    if declared_mime == po_attach_screen.MIME_XLSX:
        if tiers.tier1_xlsx_enabled:
            return _tier1_xlsx(data, tiers, est_uuid, correlation_id)
        return None

    if declared_mime != MIME_PDF:
        return None

    scanned = _is_scanned(pages)

    # Tier 1 — deterministic native-text parse (template→generic ladder).
    if tiers.tier1_enabled and not scanned:
        outcome = _tier1_parse(data, tiers, est_uuid, correlation_id)
        if outcome is not None:
            return outcome

    # Tier 2 — local Ollama (ONE doc per cycle; an attempt consumes the budget).
    if tiers.tier2_enabled and tier2_budget["remaining"] > 0:
        tier2_budget["remaining"] -= 1
        text_pages = pages
        if scanned:
            if not tiers.ocr_enabled:
                return None
            text_pages = _tier2_ocr(data, est_uuid, correlation_id)
            if not text_pages:
                return None
        return _tier2_extract(text_pages, tiers, est_uuid, correlation_id)
    return None


def _tier0_filled_form(
    data: bytes, secret: str, est_uuid: str, correlation_id: str,
) -> _LadderOutcome | None:
    """Tier 0: parse a returned quote form (quote_form.parse_quote_form — the
    red-team #3 hardened parse). Only a VERIFIED rfq-form:v1 token takes the
    fast path; a rejected/unverified form degrades to the ordinary ladder
    (needs_review for an xlsx — Tiers 1/2 are PDF-only)."""
    try:
        from po_materials import quote_form  # noqa: PLC0415 — lazy: not needed while dark
        parsed = quote_form.parse_quote_form(data, secret=secret.encode("utf-8"))
    except ImportError:
        _warn_tier_module_missing("quote_form/estimate_parse", 0, est_uuid, correlation_id)
        return None
    except Exception as exc:  # noqa: BLE001 — hostile bytes; degrade, never die
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"tier-0 form parse crashed for {est_uuid} (degrading to "
            f"needs_review): {type(exc).__name__}: {exc!r}",
            error_code="estimate_tier0_failed",
            correlation_id=correlation_id,
        )
        return None
    if parsed is None:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"tier-0: {est_uuid} carries _ITS_META but the form REJECTED "
            f"(formula in a numeric cell, or unreadable) — needs_review",
            error_code="estimate_form_rejected",
            correlation_id=correlation_id,
        )
        return None
    if not parsed.verified:
        error_log.log(
            Severity.INFO, SCRIPT_NAME,
            f"tier-0: {est_uuid} form token absent/tampered — ordinary ladder "
            f"upload (no auto-bind)",
            error_code="estimate_form_unverified",
            correlation_id=correlation_id,
        )
        return None
    if not parsed.lines:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"tier-0: verified form {est_uuid} has no parsable lines — needs_review",
            error_code="estimate_form_rejected",
            correlation_id=correlation_id,
        )
        return None

    payload_json = json.dumps(
        {
            # The verified RFQ binding rides HERE (the provenance record) AND is
            # promoted to the top-level result-post fields (R4) feeding the Worker's
            # 0054 rfq_id / rfq_vendor_key auto-bind — see the _LadderOutcome return.
            "rfq_number": parsed.rfq_number,
            "vendor_key": parsed.vendor_key,
            "form_verified": True,
            "subtotal_cents": parsed.subtotal_cents,
            "math_ok": parsed.math_ok,
            "lines": parsed.lines,
        },
        ensure_ascii=False, separators=(",", ":"),
    )
    payload: dict[str, Any] = {
        "tier": 0,
        "schema_version": quote_form.FORM_SCHEMA_VERSION,
        "doc_type": "filled_form",
        "quote_number": parsed.rfq_number,
        "subtotal_cents": parsed.subtotal_cents,
        "grand_total_cents": parsed.subtotal_cents,
        "math_ok": parsed.math_ok,
        "confidence": 1.0,
        "payload_json": payload_json,
        "lines": parsed.lines,
    }
    if not _anomaly_gate(payload, 0, est_uuid, correlation_id):
        return None
    detail = f"rfq_form:{parsed.rfq_number}:{parsed.vendor_key}"[:200]
    error_log.log(
        Severity.INFO, SCRIPT_NAME,
        f"tier-0 filled form VERIFIED for {est_uuid} (rfq {parsed.rfq_number}, "
        f"vendor {parsed.vendor_key}): {len(parsed.lines)} line(s), "
        f"math_ok={parsed.math_ok}",
        error_code="estimate_tier0_extracted",
        correlation_id=correlation_id,
    )
    return _LadderOutcome(
        payload=payload, detail=detail,
        vendor_name=parsed.vendor_key or "", quote_number=parsed.rfq_number or "",
        # R4 — promote the VERIFIED rfq-form:v1 binding to the top-level result post so
        # the Worker auto-binds this estimate to (rfq, vendor). Reached ONLY when
        # parsed.verified (guarded above), so these are never a spoofed binding.
        rfq_number=parsed.rfq_number, rfq_vendor_key=parsed.vendor_key,
    )


def _tier1_parse(
    data: bytes, tiers: _TierConfig, est_uuid: str, correlation_id: str,
) -> _LadderOutcome | None:
    """Tier 1: the deterministic template→generic parse (estimate_parse).

    parse_native re-parses inside the sandbox child (richer than the classify
    pass: words + tables); templates are data-driven YAML — first match wins,
    then the generic-table tier. A math-flagged result (needs_review) degrades
    to the disposition screen rather than burning the Tier-2 budget on a doc a
    human should eyeball."""
    try:
        from po_materials import estimate_parse  # noqa: PLC0415 — lazy gated-tier import
        parsed = estimate_parse.parse_native(data)
        if parsed is None or parsed.is_scanned:
            return None
        result = None
        for tpl in estimate_parse.load_vendor_templates():
            result = estimate_parse.parse_with_template(parsed, tpl)
            if result is not None:
                break
        if result is None:
            result = estimate_parse.parse_generic_table(parsed)
    except ImportError:
        _warn_tier_module_missing("estimate_parse", 1, est_uuid, correlation_id)
        return None
    except Exception as exc:  # noqa: BLE001 — hostile text; degrade, never die
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"tier-1 parse failed for {est_uuid} (degrading): "
            f"{type(exc).__name__}: {exc!r}",
            error_code="estimate_tier1_failed",
            correlation_id=correlation_id,
        )
        return None
    if result is None:
        return None
    if getattr(result, "needs_review", False) or not result.math_ok:
        error_log.log(
            Severity.INFO, SCRIPT_NAME,
            f"tier-1 parse for {est_uuid} math-flagged "
            f"({getattr(result, 'math_flags', [])[:3]}) — needs_review",
            error_code="estimate_tier1_math_flagged",
            correlation_id=correlation_id,
        )
        return None
    if result.confidence < tiers.confidence_threshold:
        return None
    payload = _worker_payload_from_result(result, 1, est_uuid, correlation_id)
    if payload is None or not _anomaly_gate(payload, 1, est_uuid, correlation_id):
        return None
    error_log.log(
        Severity.INFO, SCRIPT_NAME,
        f"tier-1 extraction for {est_uuid} ({result.tier}): "
        f"{len(payload['lines'])} line(s)",
        error_code="estimate_tier1_extracted",
        correlation_id=correlation_id,
    )
    return _LadderOutcome(
        payload=payload, detail=f"tier1_extracted:{result.tier}"[:200],
        vendor_name=str(payload.get("vendor_name") or ""),
        quote_number=str(payload.get("quote_number") or ""),
    )


def _tier1_xlsx(
    data: bytes, tiers: _TierConfig, est_uuid: str, correlation_id: str,
) -> _LadderOutcome | None:
    """Tier 1 (xlsx): the deterministic vendor-spreadsheet parse (estimate_parse).

    The spreadsheet twin of `_tier1_parse`, deliberately structured identically — same
    lazy gated import, same ImportError / broad-except degrade, same math-flag and
    confidence gates, same anomaly gate, same `_LadderOutcome`. Only the parse call
    differs: a worksheet already IS the grid `parse_generic_table` consumes, so the
    extraction reuses that tier wholesale via `parse_xlsx_estimate`.

    NO AI — `openpyxl` plus the existing deterministic table/line logic, run inside the
    killable sandbox child. Reached only for a VENDOR workbook: ITS's own filled quote
    form is claimed earlier by Tier 0 (HMAC-verified) and never arrives here.
    """
    try:
        from po_materials import estimate_parse  # noqa: PLC0415 — lazy gated-tier import
        result = estimate_parse.parse_xlsx_estimate(data)
    except ImportError:
        _warn_tier_module_missing("estimate_parse", 1, est_uuid, correlation_id)
        return None
    except Exception as exc:  # noqa: BLE001 — hostile workbook; degrade, never die
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"tier-1 xlsx parse failed for {est_uuid} (degrading): "
            f"{type(exc).__name__}: {exc!r}",
            error_code="estimate_tier1_xlsx_failed",
            correlation_id=correlation_id,
        )
        return None
    if result is None or not result.line_items:
        return None
    if getattr(result, "needs_review", False) or not result.math_ok:
        error_log.log(
            Severity.INFO, SCRIPT_NAME,
            f"tier-1 xlsx parse for {est_uuid} math-flagged "
            f"({getattr(result, 'math_flags', [])[:3]}) — needs_review",
            error_code="estimate_tier1_xlsx_math_flagged",
            correlation_id=correlation_id,
        )
        return None
    if result.confidence < tiers.confidence_threshold:
        return None
    payload = _worker_payload_from_result(result, 1, est_uuid, correlation_id)
    if payload is None or not _anomaly_gate(payload, 1, est_uuid, correlation_id):
        return None
    error_log.log(
        Severity.INFO, SCRIPT_NAME,
        f"tier-1 xlsx extraction for {est_uuid} ({result.tier}): "
        f"{len(payload['lines'])} line(s)",
        error_code="estimate_tier1_xlsx_extracted",
        correlation_id=correlation_id,
    )
    return _LadderOutcome(
        payload=payload, detail=f"tier1_extracted:{result.tier}"[:200],
        vendor_name=str(payload.get("vendor_name") or ""),
        quote_number=str(payload.get("quote_number") or ""),
    )


def _tier2_ocr(data: bytes, est_uuid: str, correlation_id: str) -> list[str]:
    """OCR a scanned document (sibling-owned macOS-Vision pass). [] = degrade."""
    try:
        from po_materials import estimate_ocr  # noqa: PLC0415 — lazy gated-tier import
        pages = estimate_ocr.ocr_pages(data)
    except ImportError:
        _warn_tier_module_missing("estimate_ocr", 2, est_uuid, correlation_id)
        return []
    except Exception as exc:  # noqa: BLE001 — hostile bytes; degrade, never die
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"tier-2 OCR failed for {est_uuid} (degrading): "
            f"{type(exc).__name__}: {exc!r}",
            error_code="estimate_ocr_failed",
            correlation_id=correlation_id,
        )
        return []
    return [p for p in pages if isinstance(p, str)] if isinstance(pages, list) else []


def _tier2_extract(
    pages: list[str], tiers: _TierConfig, est_uuid: str, correlation_id: str,
) -> _LadderOutcome | None:
    """Tier 2: the LOCAL-Ollama schema-constrained extraction (estimate_extract;
    localhost only — vendor pricing never leaves the machine, decision 1).

    estimate_extract runs the schema gate + check_math + the string-field
    anomaly sweep internally and returns needs_review=True on any flag (low
    confidence / math / anomaly) — a flagged result degrades to the disposition
    screen, only a clean one posts `extracted`."""
    try:
        from po_materials import estimate_extract  # noqa: PLC0415 — lazy gated-tier import
        result = estimate_extract.extract(
            pages,
            model=tiers.model,
            base_url=tiers.ollama_base_url,
            timeout_s=tiers.timeout_seconds,
            confidence_threshold=tiers.confidence_threshold,
        )
    except ImportError:
        _warn_tier_module_missing("estimate_extract", 2, est_uuid, correlation_id)
        return None
    except Exception as exc:  # noqa: BLE001 — a wedged/absent model degrades, never dies
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"tier-2 extraction failed for {est_uuid} (model {tiers.model!r} at "
            f"{tiers.ollama_base_url!r}; degrading to needs_review): "
            f"{type(exc).__name__}: {exc!r}",
            error_code="estimate_tier2_failed",
            correlation_id=correlation_id,
        )
        return None
    if result is None:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"tier-2 inference produced nothing usable for {est_uuid} (model "
            f"{tiers.model!r} at {tiers.ollama_base_url!r} — down/missing model "
            f"degrades every doc; see the §43 runbook) — needs_review",
            error_code="estimate_tier2_failed",
            correlation_id=correlation_id,
        )
        return None
    if getattr(result, "needs_review", False):
        error_log.log(
            Severity.INFO, SCRIPT_NAME,
            f"tier-2 extraction for {est_uuid} flagged needs_review "
            f"(confidence={result.confidence}, math_ok={result.math_ok}, "
            f"anomalies={len(getattr(result, 'anomaly_flags', []))}) — manual "
            f"disposition",
            error_code="estimate_tier2_low_confidence",
            correlation_id=correlation_id,
        )
        return None
    payload = _worker_payload_from_result(result, 2, est_uuid, correlation_id)
    if payload is None or not _anomaly_gate(payload, 2, est_uuid, correlation_id):
        return None
    error_log.log(
        Severity.INFO, SCRIPT_NAME,
        f"tier-2 extraction for {est_uuid}: {len(payload['lines'])} line(s), "
        f"confidence={result.confidence}",
        error_code="estimate_tier2_extracted",
        correlation_id=correlation_id,
    )
    return _LadderOutcome(
        payload=payload, detail="tier2_extracted",
        vendor_name=str(payload.get("vendor_name") or ""),
        quote_number=str(payload.get("quote_number") or ""),
    )


def _warn_tier_module_missing(
    module: str, tier: int, est_uuid: str, correlation_id: str,
) -> None:
    """A gated tier is ON but its sibling module hasn't landed — loud, specific,
    and non-fatal (the doc degrades to needs_review)."""
    error_log.log(
        Severity.WARN, SCRIPT_NAME,
        f"tier-{tier} enabled but module {module!r} is not importable — "
        f"estimate {est_uuid} degrades to needs_review (land the extraction-core "
        f"PR or flip the tier gate off)",
        error_code="estimate_tier_module_missing",
        correlation_id=correlation_id,
    )


# ---- Refusal + integrity handlers -------------------------------------------------


def _handle_integrity_failure(
    est_id: int, est_uuid: str, filename: str, uploaded_by: str,
    detail: str, correlation_id: str, flags: dict[str, str],
) -> None:
    """Reject an estimate whose transport integrity failed (bad HMAC / digest
    mismatch / malformed chunk set) — the estimate twin of po_poll's
    _handle_attachment_integrity_failure.

    NEVER screened, NEVER parsed, NEVER filed, NO result post (the bytes stay in
    D1 for forensics). One-shot: anomaly-log + security Review-Queue row + CRITICAL
    only on the FIRST sighting; the flag set suppresses per-cycle re-flag spam."""
    anomaly_logger.check({"estimate_integrity": est_id, "est_uuid": est_uuid})
    review_queue.safe_add(
        script_name=SCRIPT_NAME,
        workstream=WORKSTREAM,
        summary=(
            f"estimate: INTEGRITY FAILURE (est {est_id}, uuid {est_uuid or est_id}, "
            f"file {filename!r}) — {detail}; rejected, NOT screened or filed"
        ),
        payload={
            "estimate_id": est_id,
            "est_uuid": est_uuid,
            "filename": filename,
            "uploaded_by": uploaded_by,
            "detail": detail,
            # The HMAC value is deliberately NOT recorded (signature material);
            # the raw row + chunks stay in D1.
        },
        sla_tier=review_queue.SlaTier.RFQ_DRAFT,
        reason=review_queue.ReviewReason.SECURITY_TRIGGER,
        severity=Severity.CRITICAL,
        source_file=f"est:{est_id}",
        security_flag=True,
    )
    error_log.log(
        Severity.CRITICAL, SCRIPT_NAME,
        f"estimate integrity FAIL est_id={est_id} est_uuid={est_uuid!r}: {detail} "
        f"(downgrade defense — never screened or filed)",
        error_code="estimate_integrity_failure",
        correlation_id=correlation_id,
    )
    flags[str(est_id)] = "integrity"


def _refuse_screened(
    est_id: int, est_uuid: str, job_no: str, filename: str, uploaded_by: str,
    sha256: str, result: po_attach_screen.ScreenResult,
    correlation_id: str, flags: dict[str, str],
) -> None:
    """Route a §34-refused estimate (suspicious/malicious) to the Review Queue +
    Estimate_Log, one-shot flagged. MALICIOUS fires CRITICAL NAMING THE ACCOUNT
    (the photo_screen/intake posture); suspicious structural-active-content gets
    the security flag; plainer inconsistencies stay ordinary review items."""
    detail = f"{result.layer}:{result.detail}"
    if result.disposition == "malicious":
        review_queue.safe_add(
            script_name=SCRIPT_NAME,
            workstream=WORKSTREAM,
            summary=(
                f"estimate: MALICIOUS upload {filename!r} (uuid {est_uuid}, job "
                f"{job_no}, uploaded by {uploaded_by!r}) — refused before filing "
                f"({detail})"
            ),
            payload={
                "estimate_id": est_id, "est_uuid": est_uuid, "job_no": job_no,
                "filename": filename, "uploaded_by": uploaded_by, "detail": detail,
            },
            sla_tier=review_queue.SlaTier.RFQ_DRAFT,
            reason=review_queue.ReviewReason.SECURITY_TRIGGER,
            severity=Severity.CRITICAL,
            source_file=f"est:{est_id}",
            security_flag=True,
        )
        error_log.log(
            Severity.CRITICAL, SCRIPT_NAME,
            f"MALICIOUS estimate refused (est {est_id}, uuid {est_uuid}, account "
            f"{uploaded_by!r}): {detail} — review the account before re-enabling "
            f"uploads",
            error_code="estimate_malicious",
            correlation_id=correlation_id,
        )
    else:  # suspicious
        security = po_attach_screen.is_structural_active_content(result)
        review_queue.safe_add(
            script_name=SCRIPT_NAME,
            workstream=WORKSTREAM,
            summary=(
                f"estimate: upload {filename!r} (uuid {est_uuid}, job {job_no}) "
                f"refused as SUSPICIOUS ({detail}) — not filed; operator review"
            ),
            payload={
                "estimate_id": est_id, "est_uuid": est_uuid, "job_no": job_no,
                "filename": filename, "uploaded_by": uploaded_by, "detail": detail,
            },
            sla_tier=review_queue.SlaTier.RFQ_DRAFT,
            reason=(
                review_queue.ReviewReason.SECURITY_TRIGGER
                if security else review_queue.ReviewReason.POLICY_EDGE
            ),
            severity=Severity.WARN,
            source_file=f"est:{est_id}",
            security_flag=security,
        )
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"suspicious estimate refused (est {est_id}, uuid {est_uuid}): {detail}",
            error_code="estimate_suspicious",
            correlation_id=correlation_id,
        )
    _log_refused_row_best_effort(
        est_uuid, job_no, filename, sha256, f"screen:{result.disposition}:{detail}",
        correlation_id,
    )
    # ONE-SHOT FLAG BEFORE the result post-back (the po_poll refused posture): the
    # alert + Review-Queue row above already fired; if the post fails transiently
    # the flag IS the dedupe against a 120s re-fire storm.
    flags[str(est_id)] = "refused"


def _refuse_wrong_doc_type(
    est_id: int, est_uuid: str, job_no: str, filename: str, uploaded_by: str,
    sha256: str, doc_type: str, confidence: float,
    correlation_id: str, flags: dict[str, str],
) -> None:
    """Refuse an invoice/AP-report from the PO path, VISIBLY (ADR-0004 decision 6):
    Estimate_Log 'refused' + a POLICY_EDGE Review-Queue WARN row + one-shot flag.
    Never silently dropped, never into the PO path."""
    detail = f"wrong_doc_type:{doc_type}"
    review_queue.safe_add(
        script_name=SCRIPT_NAME,
        workstream=WORKSTREAM,
        summary=(
            f"estimate: {filename!r} (uuid {est_uuid}, job {job_no}) classified as "
            f"{doc_type.upper()} — refused from the PO path (an invoice/AP report "
            f"is never parsed as new line items)"
        ),
        payload={
            "estimate_id": est_id, "est_uuid": est_uuid, "job_no": job_no,
            "filename": filename, "uploaded_by": uploaded_by,
            "doc_type": doc_type, "confidence": confidence,
        },
        sla_tier=review_queue.SlaTier.RFQ_DRAFT,
        reason=review_queue.ReviewReason.POLICY_EDGE,
        severity=Severity.WARN,
        source_file=f"est:{est_id}",
    )
    error_log.log(
        Severity.WARN, SCRIPT_NAME,
        f"estimate refused (wrong doc type) est_id={est_id} uuid={est_uuid!r} "
        f"doc_type={doc_type} confidence={confidence}",
        error_code="estimate_wrong_doc_type",
        correlation_id=correlation_id,
    )
    _log_refused_row_best_effort(
        est_uuid, job_no, filename, sha256, detail, correlation_id, doc_type=doc_type
    )
    flags[str(est_id)] = "refused"


def _log_refused_row_best_effort(
    est_uuid: str, job_no: str, filename: str, sha256: str, detail: str,
    correlation_id: str, *, doc_type: str = "other",
) -> None:
    """Record the refusal in Estimate_Log, BEST-EFFORT (the Review-Queue row is the
    operator signal of record; a ledger miss is a WARN, never a blocked refusal)."""
    try:
        if estimate_log.find_row_by_uuid(est_uuid) is None:
            estimate_log.append_row(
                est_uuid=est_uuid,
                job_no=job_no,
                filename=filename,
                doc_type=doc_type,
                status=estimate_log.STATUS_REFUSED,
                sha256=sha256,
                detail=detail,
            )
        else:
            estimate_log.update_status(est_uuid, estimate_log.STATUS_REFUSED, detail)
    except Exception as exc:  # noqa: BLE001 — supplementary ledger; the review row is the record
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"Estimate_Log refused-row write failed (uuid {est_uuid}): "
            f"{type(exc).__name__}: {exc!r}",
            error_code="estimate_log_write_failed",
            correlation_id=correlation_id,
        )


def _post_refused_result(
    creds: _EstCreds, est_id: int, est_uuid: str, detail: str, correlation_id: str,
    *, error_counter: dict[str, int],
) -> None:
    """Post the refused disposition, handled LOCALLY (not the outer transient fence)
    so a failed post keeps the already-set one-shot flag as the re-alert dedupe —
    the po_poll refused-post posture. The Worker deletes the chunks on refused."""
    try:
        portal_client.post_estimate_result(
            creds.base_url, creds.bearer,
            estimate_id=est_id, status="refused", detail=detail,
        )
    except portal_client.PortalAuthError as exc:
        raise _BearerRejectedError from exc
    except portal_client.PortalTransportError as exc:
        error_counter["errors"] += 1
        error_log.log(
            Severity.ERROR, SCRIPT_NAME,
            f"refused-disposition post failed for estimate {est_id} (uuid "
            f"{est_uuid}; row stays claimed in D1, one-shot flag prevents re-alert; "
            f"clear '{est_id}' from {EST_FLAGGED_PATH.name} to retry after the "
            f"transport recovers): {exc!r}",
            error_code="estimate_result_post_failed",
            correlation_id=correlation_id,
        )


# ---- Previews + Box folder --------------------------------------------------------


def _post_previews_best_effort(
    creds: _EstCreds, est_id: int, est_uuid: str, data: bytes,
    max_pages: int, correlation_id: str,
) -> int:
    """Render + post the disposition-screen previews. Returns the count posted.

    WHOLLY best-effort: a render failure yields zero previews (the SPA's forced
    no-preview acknowledgment path takes over); a per-page post failure is WARNed
    and the rest continue. Auth failures still stop the cycle (bearer contract)."""
    try:
        pngs = estimate_preview.render_page_pngs(data, max_pages=max_pages)
    except Exception as exc:  # noqa: BLE001 — previews must never block filing
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"preview render failed for estimate {est_uuid} (doc degrades to "
            f"no-preview): {type(exc).__name__}: {exc!r}",
            error_code="estimate_preview_failed",
            correlation_id=correlation_id,
        )
        return 0
    if not pngs:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"no previews rendered for estimate {est_uuid} (sandbox degrade or "
            f"unrenderable PDF) — the disposition screen takes the no-preview path",
            error_code="estimate_preview_empty",
            correlation_id=correlation_id,
        )
        return 0
    posted = 0
    for index, png in enumerate(pngs):
        try:
            portal_client.post_estimate_preview(
                creds.base_url, creds.bearer,
                estimate_id=est_id, page=index + 1,
                png_b64=base64.b64encode(png).decode("ascii"),
            )
            posted += 1
        except portal_client.PortalAuthError as exc:
            raise _BearerRejectedError from exc
        except portal_client.PortalTransportError as exc:
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"preview post failed for estimate {est_uuid} page {index + 1} "
                f"(page skipped): {exc!r}",
                error_code="estimate_preview_post_failed",
                correlation_id=correlation_id,
            )
    return posted


def _attach_estimate_best_effort(
    row_id: int, filename: str, data: bytes, content_type: str, correlation_id: str
) -> None:
    """Attach the filed original inline on the Estimate_Log row, BEST-EFFORT (Box is
    the SoR; a failure is a WARN that never fails the filing — the po_poll posture)."""
    try:
        smartsheet_client.attach_pdf_to_row(
            estimate_log._sheet_id(), row_id, filename, data, content_type=content_type
        )
    except Exception as exc:  # noqa: BLE001 — supplementary inline copy; Box is the SoR
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"ledger-row attach failed (row {row_id}, {filename!r}): "
            f"{type(exc).__name__}: {exc!r}",
            error_code="estimate_row_attach_failed",
            correlation_id=correlation_id,
        )


def _resolve_quotes_box_folder(job_name: str) -> str | None:
    """§45 find-or-create the estimate filing folder: the PO lane's OWN Box ROOT
    (`po_naming.CFG_BOX_PORTAL_ROOT`) → per-job folder (the SAME
    `safety_naming.job_folder_name` as every other portal artifact) → 'Vendor
    Quotes'. None when the root is unconfigured (the caller leaves the row
    claimed + ERRORs — a config gap, not a per-row defect)."""
    root = _read_str_setting(
        po_naming.CFG_BOX_PORTAL_ROOT, "",
        workstream=po_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM,
    ).strip()
    if not root:
        return None
    job_folder = box_client.get_or_create_folder(
        root, safety_naming.job_folder_name(job_name)
    )
    return box_client.get_or_create_folder(job_folder, VENDOR_QUOTES_SUBFOLDER)


if __name__ == "__main__":
    poll_once()
