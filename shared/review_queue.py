"""ITS_Review_Queue helpers — write items to the queue, read their status.

Wires to the live `ITS_Review_Queue` Smartsheet sheet (id in
`shared.sheet_ids.SHEET_REVIEW_QUEUE`). Schema verified live
2026-05-18 — see `docs/session_logs/2026-05-18_sentry_and_phase1_unblock.md`
for the schema-drift notes vs. the original brief.

Concepts (per Operational Standards v11):

Status enum:
    PENDING / IN_REVIEW / APPROVED / REJECTED / ESCALATED.

SLA tiers:
    safety intake review: 4 business hours
    RFQ drafts:           24 hours
    subcontract drafts:   48 hours

Items past 2x SLA auto-escalate (mechanism TBD — gated on a future
scheduled-walker script that reads SLA Tier + Created At and bumps
Status to ESCALATED).

Reason picklist:
    Standardized so reviewers can filter by why-it-landed. See the
    `ReviewReason` enum below.

Severity:
    Reuses `shared.error_log.Severity` because the live ITS_Review_Queue
    `Severity` picklist (INFO / WARN / ERROR / CRITICAL) matches that
    enum exactly. Most review items are WARN; security-flagged items
    typically CRITICAL.

Security flag:
    Items routed here because of an anomaly_logger sentinel (per
    Foundation Mission v8 Invariant 2) set `security_flag=True`. The
    owner is notified separately for security-flagged items.

Failure isolation:
    Smartsheet failures propagate. Callers (workstream code) need to
    know if the queue write failed so they can log CRITICAL via
    `error_log` and fire the triple-fire alert path.
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from . import error_log, sheet_ids, smartsheet_client
from .error_log import Severity

# 2× SLA expressed as days, for date-arithmetic comparison against the
# Created At DATE column. Threshold "more than N days past creation".
# Created At is DATE (not DATETIME) per Smartsheet API constraint — see
# docs/tech_debt.md. Day-level precision is intentional; false-positive
# cost (operator looks at a borderline row) is higher than missed-late
# cost.
_SLA_HOURS_2X_DAYS: dict[str, int] = {
    "4h": 0,   # 2×SLA = 8h; any partial day past creation date
    "24h": 1,  # 2×SLA = 48h; 2+ days past creation date
    "48h": 3,  # 2×SLA = 96h; 4+ days past creation date
}


class ReviewStatus(StrEnum):
    """Values for the `Status` picklist column."""

    PENDING = "PENDING"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ESCALATED = "ESCALATED"


class SlaTier(StrEnum):
    """Values for the `SLA Tier` picklist column."""

    SAFETY_INTAKE = "4h"
    RFQ_DRAFT = "24h"
    SUBCONTRACT_DRAFT = "48h"


class ReviewReason(StrEnum):
    """Values for the `Reason` picklist column.

    Surfaced 2026-05-18 during live schema inspection — the original brief
    documented `Reason` as TEXT_NUMBER (free-text), but the live column is
    PICKLIST with the 9 options below.

    The 2026-05-23 trusted-contacts cluster added three more values
    (HEADER_SOFT_FAIL_TRUSTED / SENDER_PENDING_VERIFICATION /
    PROJECT_OUT_OF_SCOPE). The operator must add those three to the live
    Smartsheet picklist via UI; Smartsheet accepts unknown picklist
    values as plain strings so writes succeed even before the UI add,
    but pivot views won't bucket them until then.
    """

    LOW_CONFIDENCE_EXTRACTION = "low-confidence-extraction"
    AMBIGUOUS_CLASSIFICATION = "ambiguous-classification"
    STRUCTURED_OUTPUT_EDGE = "structured-output-edge"
    ZERO_DATA_WINDOW = "zero-data-window"
    MISMATCHED_REFERENCE = "mismatched-reference"
    SECURITY_TRIGGER = "security-trigger"
    POLICY_EDGE = "policy-edge"
    MANUAL = "manual"
    OTHER = "other"
    HEADER_SOFT_FAIL_TRUSTED = "header-soft-fail-trusted"
    SENDER_PENDING_VERIFICATION = "sender-pending-verification"
    PROJECT_OUT_OF_SCOPE = "project-out-of-scope"


# Valid Workstream picklist values for the `Workstream` column.
# Same set as ITS_Errors / ITS_Config workstream coverage, plus `global`.
# `progress_reports` joined at P5 (the Progress Reporting workstream): the progress
# weekly compile's per-job fence (generate_core, GenerateConfig.workstream=
# "progress_reports") and the P5 recipient_health send-time hook both call
# review_queue.add(workstream="progress_reports", ...) — omitting it here raised a
# ValueError at the first progress per-job fence (latent since P4; closed at P5).
VALID_WORKSTREAMS = frozenset({
    "safety_reports",
    "progress_reports",
    "po_materials",
    "subcontracts",
    "email_triage",
    "ai_employee",
    "global",
})


class ReviewQueueError(Exception):
    """Base exception for review_queue helpers."""


class ItemNotFoundError(ReviewQueueError):
    """get_status() couldn't find a row with the given Item ID."""


def _generate_item_id(workstream: str) -> str:
    """Build an operator-friendly stable item ID.

    Format: `<workstream>-<YYYYMMDD>-<HHMMSS>` in UTC. Sortable; unique
    enough for sandbox volume (collisions only if the same workstream
    enqueues two items within the same second, which is fine for
    operator-facing review queues).
    """
    return f"{workstream}-{datetime.now(UTC):%Y%m%d-%H%M%S}"


def add(
    *,
    workstream: str,
    summary: str,
    payload: dict[str, Any],
    sla_tier: SlaTier,
    reason: ReviewReason = ReviewReason.OTHER,
    severity: Severity = Severity.WARN,
    source_file: str | None = None,
    security_flag: bool = False,
) -> int:
    """Add an item to ITS_Review_Queue. Returns the Smartsheet row ID.

    Args:
        workstream: Which workstream owns this item. Must be a valid
            Workstream picklist value (see `VALID_WORKSTREAMS`).
        summary: One-line human-readable description.
        payload: Structured data the reviewer needs to make the decision.
            JSON-encoded into the `Payload` cell.
        sla_tier: SLA tier per Operational Standards v11.
        reason: Why this is in the queue (default `OTHER` if unspecified).
        severity: Item severity. Defaults to `WARN`; use `CRITICAL` for
            security-flagged items and `INFO` for manual nudges.
        source_file: Optional reference to a source document path or
            inbox URL. Lands in the `Source File` cell.
        security_flag: True if a `shared.anomaly_logger` sentinel fired.
            Reviewers may filter on this to triage suspected prompt
            injection items first.

    Returns:
        Smartsheet row ID of the newly-added row. The operator-facing
        identifier is the `Item ID` cell value (returned via `get_status`
        and visible in the Smartsheet UI); the row ID returned here is
        for code-side lookups (e.g., `smartsheet_client.update_rows`).

    Raises:
        ValueError: workstream is not in `VALID_WORKSTREAMS`.
        SmartsheetError: from `smartsheet_client.add_rows`. Propagated —
            callers should log CRITICAL via `error_log` if the queue
            write is itself a failure-mode signal.
    """
    if workstream not in VALID_WORKSTREAMS:
        raise ValueError(
            f"workstream={workstream!r} not in {sorted(VALID_WORKSTREAMS)}"
        )

    item_id = _generate_item_id(workstream)
    row = {
        "Item ID": item_id,
        "Created At": date.today().isoformat(),
        "Workstream": workstream,
        "Summary": summary,
        "Reason": reason.value,
        "Severity": severity.value,
        "SLA Tier": sla_tier.value,
        "Source File": source_file or "",
        "Payload": json.dumps(payload, separators=(",", ":")),
        "Status": ReviewStatus.PENDING.value,
        "Security Flag": security_flag,
        # Assigned To / Resolved By / Resolved At / Resolution Notes are
        # operator-workflow cells left blank at write time.
    }
    [row_id] = smartsheet_client.add_rows(sheet_ids.SHEET_REVIEW_QUEUE, [row])
    return row_id


def safe_add(
    *,
    script_name: str,
    workstream: str,
    summary: str,
    payload: dict[str, Any],
    sla_tier: SlaTier,
    reason: ReviewReason,
    severity: Severity = Severity.WARN,
    source_file: str = "",
    security_flag: bool = False,
    correlation_id: str = "",
    error_code: str = "review_queue_ticket_failed",
) -> bool:
    """`add`, but it NEVER raises. Returns True iff the ticket landed.

    Use this from any PERMANENT-refusal path that also records a one-shot flag. `add`
    propagates `SmartsheetError` by contract, and every such path wrote its ticket BEFORE
    its flag — so a Smartsheet blip at fence time skipped the flag, the item re-served
    next cycle, and a PERMANENT one-shot fence degraded into unbounded per-cycle
    re-ticketing. Observed 2026-08-10 on `rfq_poll`: four PENDING rows for ONE RFQ across
    two cycles, and the first ticket had actually COMMITTED (Smartsheet writes are
    non-idempotent, so the client cannot tell a lost response from a lost write) — the
    "retry" duplicated a row that was already there.

    Not raising keeps the caller's flag write reachable, which is what makes the fence
    genuinely one-shot. A lost ticket escalates to CRITICAL rather than passing quietly:
    the item IS fenced either way, so an operator with no ticket would never learn it
    exists — exactly what the out-of-band push surface is for. Callers that want to know
    may branch on the return value; most simply proceed to write their flag.

    (Generalised from `safety_reports.generate_core._safe_review_queue`, which earned the
    same never-raise property the same way: 189 duplicate rows for one job in one day.)
    """
    try:
        add(
            workstream=workstream,
            summary=summary,
            payload=payload,
            sla_tier=sla_tier,
            reason=reason,
            severity=severity,
            source_file=source_file,
            security_flag=security_flag,
        )
    except Exception as exc:  # noqa: BLE001 — a lost ticket must never cost the flag
        error_log.log(
            Severity.CRITICAL, script_name,
            f"Review-Queue ticket FAILED for {source_file or workstream} — the item is "
            f"still fenced (one-shot flag set, it will NOT retry) but has NO operator "
            f"ticket. The row may or may not have committed (Smartsheet writes are "
            f"non-idempotent): check ITS_Review_Queue for {source_file or workstream} "
            f"before re-ticketing by hand. Intended summary: {summary!r}. "
            f"Cause: {type(exc).__name__}: {exc!r}",
            error_code=error_code,
            correlation_id=correlation_id,
        )
        return False
    return True


def get_status(item_id: str) -> ReviewStatus:
    """Read the current `Status` for the given `Item ID`.

    Args:
        item_id: The operator-facing string identifier returned in the
            `Item ID` cell when `add()` ran.

    Returns:
        The parsed `ReviewStatus` enum value.

    Raises:
        ItemNotFoundError: No row with that Item ID in ITS_Review_Queue.
        SmartsheetError: from `smartsheet_client.get_rows` on any
            underlying API failure.
    """
    rows = smartsheet_client.get_rows(
        sheet_ids.SHEET_REVIEW_QUEUE,
        filters={"Item ID": item_id},
    )
    if not rows:
        raise ItemNotFoundError(
            f"no ITS_Review_Queue row with Item ID={item_id!r}"
        )
    raw = rows[0].get("Status")
    if not isinstance(raw, str):
        # PICKLIST cells should always come back as str, but guard anyway
        # so type narrowing is explicit.
        raise ReviewQueueError(
            f"Item ID={item_id!r} has non-string Status cell: {raw!r}"
        )
    return ReviewStatus(raw)


def get_pending(workstream: str | None = None) -> list[dict[str, Any]]:
    """Return PENDING rows from ITS_Review_Queue.

    Optional `workstream` narrows to one workstream's pending items. If
    provided, must be a valid `VALID_WORKSTREAMS` value — same validation
    surface as `add()` so callers get a single failure shape.

    Raises:
        ValueError: workstream is not in `VALID_WORKSTREAMS`.
        SmartsheetError: propagated from `smartsheet_client.get_rows`.
    """
    filters: dict[str, Any] = {"Status": ReviewStatus.PENDING.value}
    if workstream is not None:
        if workstream not in VALID_WORKSTREAMS:
            raise ValueError(
                f"workstream={workstream!r} not in {sorted(VALID_WORKSTREAMS)}"
            )
        filters["Workstream"] = workstream
    return smartsheet_client.get_rows(sheet_ids.SHEET_REVIEW_QUEUE, filters=filters)


def is_past_sla(row: dict[str, Any], *, now: date | None = None) -> bool:
    """Return True if the row is past 2× its SLA tier.

    Args:
        row: A ITS_Review_Queue row dict, as returned by `get_pending()`.
            Must contain `Created At` (ISO date string) and `SLA Tier`
            (`"4h"` / `"24h"` / `"48h"`).
        now: Override `date.today()` for test determinism.

    Raises:
        KeyError: required columns missing from `row`.
        ValueError: `SLA Tier` not in the SLA tier set, or `Created At`
            is not a parseable ISO date.
    """
    created_str = row["Created At"]
    sla = row["SLA Tier"]
    try:
        threshold_days = _SLA_HOURS_2X_DAYS[sla]
    except KeyError:
        raise ValueError(f"unknown SLA tier: {sla!r}") from None
    created = date.fromisoformat(created_str)
    current = now if now is not None else date.today()
    return (current - created).days > threshold_days
