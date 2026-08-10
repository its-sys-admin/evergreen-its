"""ITS_Subcontractors access — the subcontractor SoR read + the §51 bidirectional sync builders (SC S1).

ITS_Subcontractors (S1, ITS — Subcontracts / Control) is the SOLE subcontractor
source-of-record (D4); the Worker's `subcontractors` D1 table is a portal CACHE. This
module owns the three Mac-side halves of that contract:

* **SoR read at render time** — `get_subcontractor_by_key` resolves the subcontractor
  whose fields are EMBEDDED in the rendered subcontract (the #494 security-review
  decision: the rendered document snapshots the Smartsheet SoR at render time, not the D1
  cache — see `subcontract_docx.render_package`). Also the send-time recipient source.
* **Down-sync payload builder** — `build_down_sync_payload` projects the FULL sheet
  into the Worker's subcontractors/sync shape (full-replace; the Worker's dirty-row
  fence protects un-mirrored portal edits). Malformed rows are SKIPPED-with-reason
  rather than sent: the Worker rejects the WHOLE batch on any bad row, so one operator
  typo in a Sub Key must not silently halt the entire subcontractor sync.
* **Up-sync writer** — `upsert_subcontractor` mirrors one portal-edited (dirty)
  subcontractor UP into the sheet by a column-scoped find-or-create on the **Sub Key**
  bridge column (the `shared.active_jobs_writer` pattern): it writes ONLY the
  subcontractor field columns, never a system/operator column, and a row is matched
  ONLY by its immutable key.

Never-delete (D4): deactivation rides the Active picklist. Down-sync maps
{Active→1, Inactive/Archived→0}; up-sync maps {1→Active, 0→Inactive} EXCEPT that an
existing 'Archived' row stays 'Archived' when the portal says inactive — 'Archived'
is an operator-only distinction the 0/1 portal vocabulary cannot express, and
clobbering it to 'Inactive' would lose it (non-clobber, §51).

Failure modes (typed, never silent): `PicklistViolationError` (bad State/
trade/profile value) and `SmartsheetValidationError` propagate as PERMANENT;
any other `SmartsheetError` as TRANSIENT. The daemon (`subcontract_poll`) fences per subcontractor.
"""
from __future__ import annotations

from typing import Any, NamedTuple

from shared import sheet_ids, smartsheet_client

SHEET_ID = sheet_ids.SHEET_ITS_SUBCONTRACTORS

# ---- Column titles (mirror scripts/migrations/build_its_subcontractors_sheet.py) ----
COL_SUB_NAME = "Subcontractor Name"  # primary
COL_SUB_KEY = "Sub Key"            # SUB-###### — the immutable bridge key (D4)
COL_ADDRESS = "Address"
COL_CONTACT_NAME = "Contact Name"
COL_CONTACT_EMAIL = "Contact Email"  # THE send-time recipient (TO) — the send resolves it here
COL_CONTACT_PHONE = "Contact Phone"
COL_STATE = "State"                # PICKLIST — 2-letter USPS (jurisdiction grouping)
COL_TRADES = "Trades"              # MULTI_PICKLIST
COL_TERMS_PROFILE = "Default Terms Profile"  # PICKLIST — terms-manifest profile ids
COL_MSA_REFERENCE = "MSA Reference"
COL_COI_REFERENCE = "COI Reference"
COL_LICENSE_NUMBER = "License #"
COL_ACTIVE = "Active"              # PICKLIST {Active, Inactive, Archived}
COL_NOTES = "Notes"

ACTIVE = "Active"
INACTIVE = "Inactive"
ARCHIVED = "Archived"


class DownSyncPayload(NamedTuple):
    """`build_down_sync_payload` result: the Worker-shaped rows + the skipped rows
    (each `(row_id, reason)`) the caller WARNs about — a skip is never silent."""

    subcontractors: list[dict[str, Any]]
    skipped: list[tuple[int, str]]


def _cell_str(row: dict[str, Any], column: str) -> str:
    value = row.get(column)
    if isinstance(value, str):
        return value.strip()
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _trades_list(row: dict[str, Any]) -> list[str]:
    """Normalize the Trades MULTI_PICKLIST cell to a list of strings.

    The SDK read (`get_rows` → `cell.value`) yields a list at objectValue-aware
    levels and a comma-joined display string at the backward-compatible level —
    handle both so the projection never depends on which level the SDK negotiated.
    """
    value = row.get(COL_TRADES)
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def get_subcontractor_by_key(sub_key: str) -> dict[str, Any] | None:
    """The ITS_Subcontractors row whose Sub Key == `sub_key`, or None.

    THE SoR resolution the render pipeline embeds in the PDF (#494: Smartsheet at
    render time, never the D1 cache) and the send resolves the recipient from.
    Returns the raw `{_row_id, <title>: value, ...}` row — callers read the COL_*
    constants above.
    """
    key = (sub_key or "").strip()
    if not key:
        return None
    rows = smartsheet_client.get_rows(SHEET_ID, filters={COL_SUB_KEY: key})
    return rows[0] if rows else None


def build_down_sync_payload() -> DownSyncPayload:
    """Project the FULL ITS_Subcontractors sheet into the Worker's subcontractors/sync shape.

    Full-replace semantics live Worker-side (upsert-all + dirty-row fence + never-
    delete); this builder's job is a faithful, PRE-VALIDATED projection: a row with a
    malformed/blank Sub Key or a blank Subcontractor Name would make the Worker reject
    the ENTIRE batch (400 invalid_row), so such rows are returned in `skipped` for the
    caller to WARN about instead of shipped. An EMPTY `subcontractors` result must never
    be POSTed (the Worker refuses it; a read-miss must never wipe the cache) — the
    caller enforces that refusal.
    """
    subcontractors: list[dict[str, Any]] = []
    skipped: list[tuple[int, str]] = []
    for row in smartsheet_client.get_rows(SHEET_ID):
        row_id = int(row["_row_id"])
        sub_key = _cell_str(row, COL_SUB_KEY)
        if not _is_valid_sub_key(sub_key):
            skipped.append((row_id, f"malformed sub_key {sub_key!r}"))
            continue
        sub_name = _cell_str(row, COL_SUB_NAME)
        if not sub_name:
            skipped.append((row_id, f"blank sub_name for {sub_key}"))
            continue
        subcontractors.append({
            "sub_key": sub_key,
            "sub_name": sub_name,
            "address": _cell_str(row, COL_ADDRESS),
            "contact_name": _cell_str(row, COL_CONTACT_NAME),
            "contact_email": _cell_str(row, COL_CONTACT_EMAIL),
            "contact_phone": _cell_str(row, COL_CONTACT_PHONE),
            "state": _cell_str(row, COL_STATE),
            "trades": _trades_list(row),
            "default_terms_profile": _cell_str(row, COL_TERMS_PROFILE),
            "msa_reference": _cell_str(row, COL_MSA_REFERENCE),
            "coi_reference": _cell_str(row, COL_COI_REFERENCE),
            "license_number": _cell_str(row, COL_LICENSE_NUMBER),
            # Sheet lifecycle → the portal's 0/1: only 'Active' is active; Inactive
            # AND Archived both arrive 0 (the cache hides them from the picker).
            "active": 1 if _cell_str(row, COL_ACTIVE) == ACTIVE else 0,
            "notes": _cell_str(row, COL_NOTES),
        })
    return DownSyncPayload(subcontractors=subcontractors, skipped=skipped)


def _is_valid_sub_key(key: str) -> bool:
    """Mirror the Worker's SUB_KEY_RE (`^SUB-\\d{6}$`)."""
    return (
        len(key) == 10
        and key.startswith("SUB-")
        and key[4:].isdigit()
    )


def upsert_subcontractor(subcontractor: dict[str, Any]) -> int:
    """Column-scoped find-or-create of one portal-edited subcontractor; returns the row id.

    The §51 up-sync writer (the `active_jobs_writer.upsert_job` pattern): find by the
    immutable Sub Key bridge column; on a hit `update_rows` ONLY the subcontractor field
    columns of that row (system/operator columns untouched — and see the Archived
    carve-out in the module docstring); on a miss `add_rows` a new row (a subcontractor
    CREATED in the portal self-provisions its SoR row). `subcontractor` is the Worker's
    subcontractors/pending shape (trades already a list; active 0/1).

    Raises:
        ValueError: the payload has no valid `sub_key` (unkeyable — the caller
            fences it to the Review Queue; it can never be marked mirrored).
        PicklistViolationError / SmartsheetValidationError: PERMANENT (Review Queue).
        SmartsheetError: any other failure — TRANSIENT (left dirty, retried).
    """
    sub_key = str(subcontractor.get("sub_key") or "").strip()
    if not _is_valid_sub_key(sub_key):
        raise ValueError(
            f"subcontracts.subcontractors.upsert_subcontractor: payload has no valid "
            f"sub_key (got {sub_key!r})"
        )

    active_raw = subcontractor.get("active")
    is_active = active_raw in (1, True, "1")
    trades = subcontractor.get("trades")
    cells: dict[str, Any] = {
        COL_SUB_NAME: str(subcontractor.get("sub_name") or "").strip(),
        COL_SUB_KEY: sub_key,
        COL_ADDRESS: str(subcontractor.get("address") or "").strip(),
        COL_CONTACT_NAME: str(subcontractor.get("contact_name") or "").strip(),
        COL_CONTACT_EMAIL: str(subcontractor.get("contact_email") or "").strip(),
        COL_CONTACT_PHONE: str(subcontractor.get("contact_phone") or "").strip(),
        COL_STATE: str(subcontractor.get("state") or "").strip(),
        COL_TRADES: (
            [str(t).strip() for t in trades if str(t).strip()]
            if isinstance(trades, (list, tuple)) else []
        ),
        COL_TERMS_PROFILE: str(subcontractor.get("default_terms_profile") or "").strip(),
        COL_MSA_REFERENCE: str(subcontractor.get("msa_reference") or "").strip(),
        COL_COI_REFERENCE: str(subcontractor.get("coi_reference") or "").strip(),
        COL_LICENSE_NUMBER: str(subcontractor.get("license_number") or "").strip(),
        COL_ACTIVE: ACTIVE if is_active else INACTIVE,
        COL_NOTES: str(subcontractor.get("notes") or "").strip(),
    }
    # Blank picklist scalars must not be WRITTEN as '' (the REGISTRY would reject a
    # non-member ''); dropping the key leaves the cell untouched on update and blank
    # on create — the picklist analogue of a None cell.
    for col in (COL_STATE, COL_TERMS_PROFILE):
        if not cells[col]:
            del cells[col]
    # Same rule for the MULTI_PICKLIST: an EMPTY Trades list is UNWRITABLE (the API
    # rejects an empty values[] with errorCode 1012), so an empty list means "leave the
    # cell alone". Without this a subcontractor carrying no trades can NEVER up-sync —
    # it fences PERMANENTLY every cycle. Twin of the ITS_Vendors Supply Categories fix.
    if not cells[COL_TRADES]:
        del cells[COL_TRADES]

    existing = get_subcontractor_by_key(sub_key)
    if existing is not None:
        # Non-clobber carve-out: 'Archived' is operator-only vocabulary the portal's
        # 0/1 cannot express — an inactive portal edit must not downgrade it.
        if not is_active and _cell_str(existing, COL_ACTIVE) == ARCHIVED:
            del cells[COL_ACTIVE]
        row_id = int(existing["_row_id"])
        smartsheet_client.update_rows(SHEET_ID, [{"_row_id": row_id, **cells}])
        return row_id

    [row_id] = smartsheet_client.add_rows(SHEET_ID, [cells])
    return row_id
