"""Estimate_Log access — the operator-visible ledger of the vendor-estimate importer.

One row per uploaded estimate (ADR-0004 E2). D1 (`po_estimates`, via the Worker)
remains the authoritative estimate status machine; this ITS-owned sheet (§51) is the
ledger the office reads without portal access — the PO_Log posture, mirror-not-master.

`estimate_poll` APPENDS the row at servicing time (Status=needs_review for a filed
doc, refused for a screen/doc-type rejection, extracted when the PR-B ladder fills
Vendor Name / Quote Number) and inline-attaches the filed original (#79 parity,
2026-08-12). Those filing-time statuses are TERMINAL on this sheet today: the SPA
dispose flow flips only the D1 `po_estimates` row (imported / rejected /
superseded), and NO pass mirrors those dispositions back here — the picklist
carries the values, but their sole writers would be a future status-sync pass
(recorded in docs/tech_debt.md). D1 is authoritative; read dispositions there.

Write discipline
----------------
* `append_row` is APPEND-ONLY and idempotent-by-caller: check `find_row_by_uuid`
  first (the crash-retry guard — re-servicing a claimed row must not duplicate).
* `update_status` writes ONLY LEGAL_STATUSES values (the D1 vocabulary verbatim;
  `shared.picklist_validation` gates the actual write once the sheet registers)
  and SKIPS the update when the row is already at the target.
* FLIP precedes SEED: every write refuses while `sheet_ids.SHEET_ESTIMATE_LOG` is
  the 0 placeholder — run scripts/migrations/build_estimate_log_sheet.py and flip
  the printed id first (the PO-trio builder pattern).
* Smartsheet failures propagate typed — the caller's fence classifies them.
* `Received At` is a NAIVE PACIFIC WALL-CLOCK string in a TEXT_NUMBER cell
  ("YYYY-MM-DD HH:MM:SS") — ABSTRACT_DATETIME is not API-creatable (errorCode 1142)
  and offset-carrying strings are rejected; the po_review/WSR convention.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from shared import sheet_ids, smartsheet_client

# ---- Column titles (mirror scripts/migrations/build_estimate_log_sheet.py) ----
COL_EST_UUID = "Estimate UUID"    # primary — the pool row identity
COL_JOB_NO = "Job #"
COL_FILENAME = "Filename"
COL_DOC_TYPE = "Doc Type"         # PICKLIST — estimate_classify.DOC_TYPES
COL_STATUS = "Status"             # PICKLIST — lowercase D1 vocabulary
COL_VENDOR_NAME = "Vendor Name"   # body-derived (PR-B extraction); blank in PR-A
COL_QUOTE_NUMBER = "Quote Number"  # body-derived (PR-B extraction); blank in PR-A
COL_SHA256 = "SHA-256"
COL_BOX_FILE_ID = "Box File ID"
COL_DETAIL = "Detail"
COL_RECEIVED_AT = "Received At"   # naive Pacific wall-clock string
COL_WORKSTREAM = "Workstream"     # PICKLIST — always 'po_materials'

WORKSTREAM_TAG = "po_materials"

# The D1 po_estimates.status vocabulary this ledger mirrors, plus 'received' (the
# ledger-only initial marker available to hand rows). Matches the builder's
# STATUS_OPTIONS verbatim.
STATUS_RECEIVED = "received"
STATUS_REFUSED = "refused"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_EXTRACTED = "extracted"
STATUS_IMPORTED = "imported"
STATUS_REJECTED = "rejected"
STATUS_SUPERSEDED = "superseded"
LEGAL_STATUSES: frozenset[str] = frozenset({
    STATUS_RECEIVED, STATUS_REFUSED, STATUS_NEEDS_REVIEW, STATUS_EXTRACTED,
    STATUS_IMPORTED, STATUS_REJECTED, STATUS_SUPERSEDED,
})

_PACIFIC = ZoneInfo("America/Los_Angeles")


def _sheet_id() -> int:
    """The live Estimate_Log sheet id — refuses the 0 placeholder (FLIP precedes SEED)."""
    sheet_id = sheet_ids.SHEET_ESTIMATE_LOG
    if not sheet_id:
        raise RuntimeError(
            "sheet_ids.SHEET_ESTIMATE_LOG is still the 0 placeholder — run "
            "scripts/migrations/build_estimate_log_sheet.py and flip the printed id "
            "before any Estimate_Log write (builder-precedes-seed)."
        )
    return sheet_id


def received_at_now() -> str:
    """Naive Pacific wall-clock 'YYYY-MM-DD HH:MM:SS' for the Received At cell."""
    return datetime.now(_PACIFIC).strftime("%Y-%m-%d %H:%M:%S")


def find_row_by_uuid(est_uuid: str) -> dict[str, Any] | None:
    """The ledger row whose Estimate UUID == `est_uuid`, or None — the caller-side
    idempotency guard for crash-retried servicing."""
    rows = smartsheet_client.get_rows(_sheet_id(), filters={COL_EST_UUID: est_uuid})
    return rows[0] if rows else None


def append_row(
    *,
    est_uuid: str,
    job_no: str,
    filename: str,
    doc_type: str,
    status: str,
    sha256: str,
    box_file_id: str = "",
    detail: str = "",
    vendor_name: str = "",
    quote_number: str = "",
    received_at: str | None = None,
) -> int:
    """APPEND one estimate ledger row; return its Smartsheet row ID.

    `status` must be REGISTRY-legal (`LEGAL_STATUSES`); the Workstream cell is
    hard-populated `po_materials` (the red-team #8 posture — a brand-new sheet has
    no pre-backfill excuse for an absent tag). Caller guards duplicates via
    `find_row_by_uuid` first.
    """
    if status not in LEGAL_STATUSES:
        raise ValueError(
            f"illegal Estimate_Log status {status!r} (legal: {sorted(LEGAL_STATUSES)})"
        )
    [row_id] = smartsheet_client.add_rows(
        _sheet_id(),
        [{
            COL_EST_UUID: est_uuid,
            COL_JOB_NO: job_no,
            COL_FILENAME: filename,
            COL_DOC_TYPE: doc_type,
            COL_STATUS: status,
            COL_VENDOR_NAME: vendor_name,
            COL_QUOTE_NUMBER: quote_number,
            COL_SHA256: sha256,
            COL_BOX_FILE_ID: box_file_id,
            COL_DETAIL: detail,
            COL_RECEIVED_AT: received_at if received_at is not None else received_at_now(),
            COL_WORKSTREAM: WORKSTREAM_TAG,
        }],
    )
    return row_id


def update_status(
    est_uuid: str,
    status: str,
    detail: str | None = None,
    *,
    box_file_id: str | None = None,
    vendor_name: str | None = None,
    quote_number: str | None = None,
) -> bool:
    """Stamp a ledger row's Status (+ optional Detail / Box File ID / body-derived
    identity). Returns True iff an update was written (False = row missing OR
    already at target with nothing new to add) — the po_log.stamp_status contract.
    """
    if status not in LEGAL_STATUSES:
        raise ValueError(
            f"illegal Estimate_Log status {status!r} (legal: {sorted(LEGAL_STATUSES)})"
        )
    row = find_row_by_uuid(est_uuid)
    if row is None:
        return False

    cells: dict[str, Any] = {}
    if str(row.get(COL_STATUS) or "") != status:
        cells[COL_STATUS] = status
    if detail is not None and str(row.get(COL_DETAIL) or "") != detail:
        cells[COL_DETAIL] = detail
    if box_file_id is not None and str(row.get(COL_BOX_FILE_ID) or "") != box_file_id:
        cells[COL_BOX_FILE_ID] = box_file_id
    if vendor_name is not None and str(row.get(COL_VENDOR_NAME) or "") != vendor_name:
        cells[COL_VENDOR_NAME] = vendor_name
    if quote_number is not None and str(row.get(COL_QUOTE_NUMBER) or "") != quote_number:
        cells[COL_QUOTE_NUMBER] = quote_number
    if not cells:
        return False

    smartsheet_client.update_rows(
        _sheet_id(), [{"_row_id": int(row["_row_id"]), **cells}]
    )
    return True
