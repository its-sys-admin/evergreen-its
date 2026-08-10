"""ITS_Vendors access — the vendor SoR read + the §51 bidirectional sync builders (PO S4).

ITS_Vendors (S1, ITS — Purchase Orders / Control) is the SOLE vendor source-of-record
(D4); the Worker's `po_vendors` D1 table is a portal CACHE. This module owns the three
Mac-side halves of that contract:

* **SoR read at render time** — `get_vendor_by_key` resolves the vendor whose fields
  are EMBEDDED in the rendered PO (the #494 security-review decision: the PDF snapshots
  the Smartsheet SoR at render time, not the D1 cache — see
  `po_generate.render_po_pdf`). Also the S5 send-time recipient source.
* **Down-sync payload builder** — `build_down_sync_payload` projects the FULL sheet
  into the Worker's vendors/sync shape (full-replace; the Worker's dirty-row fence
  protects un-mirrored portal edits). Malformed rows are SKIPPED-with-reason rather
  than sent: the Worker rejects the WHOLE batch on any bad row, so one operator typo
  in a Vendor Key must not silently halt the entire vendor sync.
* **Up-sync writer** — `upsert_vendor` mirrors one portal-edited (dirty) vendor UP
  into the sheet by a column-scoped find-or-create on the **Vendor Key** bridge column
  (the `shared.active_jobs_writer` pattern): it writes ONLY the vendor field columns,
  never a system/operator column, and a row is matched ONLY by its immutable key.

Never-delete (D4): deactivation rides the Active picklist. Down-sync maps
{Active→1, Inactive/Archived→0}; up-sync maps {1→Active, 0→Inactive} EXCEPT that an
existing 'Archived' row stays 'Archived' when the portal says inactive — 'Archived'
is an operator-only distinction the 0/1 portal vocabulary cannot express, and
clobbering it to 'Inactive' would lose it (non-clobber, §51).

Failure modes (typed, never silent): `PicklistViolationError` (bad Region/
category/profile value) and `SmartsheetValidationError` propagate as PERMANENT;
any other `SmartsheetError` as TRANSIENT. The daemon (`po_poll`) fences per vendor.
"""
from __future__ import annotations

from typing import Any, NamedTuple

from shared import sheet_ids, smartsheet_client

SHEET_ID = sheet_ids.SHEET_ITS_VENDORS

# ---- Column titles (mirror scripts/migrations/build_its_vendors_sheet.py) ----
COL_VENDOR_NAME = "Vendor Name"    # primary
COL_VENDOR_KEY = "Vendor Key"      # VEN-###### — the immutable bridge key (D4)
COL_ADDRESS = "Address"
COL_CONTACT_NAME = "Contact Name"
COL_CONTACT_EMAIL = "Contact Email"  # THE send-time recipient (TO) — S5 resolves it here
COL_CONTACT_PHONE = "Contact Phone"
COL_REGION = "Region"              # PICKLIST
COL_SUPPLY_CATEGORIES = "Supply Categories"  # MULTI_PICKLIST
COL_TERMS_PROFILE = "Default Terms Profile"  # PICKLIST — terms-manifest profile ids
COL_GTC_REFERENCE = "GTC Reference"
COL_ACTIVE = "Active"              # PICKLIST {Active, Inactive, Archived}
COL_NOTES = "Notes"

ACTIVE = "Active"
INACTIVE = "Inactive"
ARCHIVED = "Archived"


class DownSyncPayload(NamedTuple):
    """`build_down_sync_payload` result: the Worker-shaped rows + the skipped rows
    (each `(row_id, reason)`) the caller WARNs about — a skip is never silent."""

    vendors: list[dict[str, Any]]
    skipped: list[tuple[int, str]]


def _cell_str(row: dict[str, Any], column: str) -> str:
    value = row.get(column)
    if isinstance(value, str):
        return value.strip()
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _categories_list(row: dict[str, Any]) -> list[str]:
    """Normalize the Supply Categories MULTI_PICKLIST cell to a list of strings.

    The SDK read (`get_rows` → `cell.value`) yields a list at objectValue-aware
    levels and a comma-joined display string at the backward-compatible level —
    handle both so the projection never depends on which level the SDK negotiated.
    """
    value = row.get(COL_SUPPLY_CATEGORIES)
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def get_vendor_by_key(vendor_key: str) -> dict[str, Any] | None:
    """The ITS_Vendors row whose Vendor Key == `vendor_key`, or None.

    THE SoR resolution the render pipeline embeds in the PDF (#494: Smartsheet at
    render time, never the D1 cache) and the S5 send resolves the recipient from.
    Returns the raw `{_row_id, <title>: value, ...}` row — callers read the COL_*
    constants above.
    """
    key = (vendor_key or "").strip()
    if not key:
        return None
    rows = smartsheet_client.get_rows(SHEET_ID, filters={COL_VENDOR_KEY: key})
    return rows[0] if rows else None


def build_down_sync_payload() -> DownSyncPayload:
    """Project the FULL ITS_Vendors sheet into the Worker's vendors/sync shape.

    Full-replace semantics live Worker-side (upsert-all + dirty-row fence + never-
    delete); this builder's job is a faithful, PRE-VALIDATED projection: a row with a
    malformed/blank Vendor Key or a blank Vendor Name would make the Worker reject the
    ENTIRE batch (400 invalid_row), so such rows are returned in `skipped` for the
    caller to WARN about instead of shipped. An EMPTY `vendors` result must never be
    POSTed (the Worker refuses it; a read-miss must never wipe the cache) — the
    caller enforces that refusal.
    """
    vendors: list[dict[str, Any]] = []
    skipped: list[tuple[int, str]] = []
    for row in smartsheet_client.get_rows(SHEET_ID):
        row_id = int(row["_row_id"])
        vendor_key = _cell_str(row, COL_VENDOR_KEY)
        if not _is_valid_vendor_key(vendor_key):
            skipped.append((row_id, f"malformed vendor_key {vendor_key!r}"))
            continue
        vendor_name = _cell_str(row, COL_VENDOR_NAME)
        if not vendor_name:
            skipped.append((row_id, f"blank vendor_name for {vendor_key}"))
            continue
        vendors.append({
            "vendor_key": vendor_key,
            "vendor_name": vendor_name,
            "address": _cell_str(row, COL_ADDRESS),
            "contact_name": _cell_str(row, COL_CONTACT_NAME),
            "contact_email": _cell_str(row, COL_CONTACT_EMAIL),
            "contact_phone": _cell_str(row, COL_CONTACT_PHONE),
            "region": _cell_str(row, COL_REGION),
            "supply_categories": _categories_list(row),
            "default_terms_profile": _cell_str(row, COL_TERMS_PROFILE),
            "gtc_reference": _cell_str(row, COL_GTC_REFERENCE),
            # Sheet lifecycle → the portal's 0/1: only 'Active' is active; Inactive
            # AND Archived both arrive 0 (the cache hides them from the picker).
            "active": 1 if _cell_str(row, COL_ACTIVE) == ACTIVE else 0,
            "notes": _cell_str(row, COL_NOTES),
        })
    return DownSyncPayload(vendors=vendors, skipped=skipped)


def _is_valid_vendor_key(key: str) -> bool:
    """Mirror the Worker's VENDOR_KEY_RE (`^VEN-\\d{6}$`)."""
    return (
        len(key) == 10
        and key.startswith("VEN-")
        and key[4:].isdigit()
    )


def upsert_vendor(vendor: dict[str, Any]) -> int:
    """Column-scoped find-or-create of one portal-edited vendor; returns the row id.

    The §51 up-sync writer (the `active_jobs_writer.upsert_job` pattern): find by the
    immutable Vendor Key bridge column; on a hit `update_rows` ONLY the vendor field
    columns of that row (system/operator columns untouched — and see the Archived
    carve-out in the module docstring); on a miss `add_rows` a new row (a vendor
    CREATED in the portal self-provisions its SoR row). `vendor` is the Worker's
    vendors/pending shape (supply_categories already a list; active 0/1).

    Raises:
        ValueError: the payload has no valid `vendor_key` (unkeyable — the caller
            fences it to the Review Queue; it can never be marked mirrored).
        PicklistViolationError / SmartsheetValidationError: PERMANENT (Review Queue).
        SmartsheetError: any other failure — TRANSIENT (left dirty, retried).
    """
    vendor_key = str(vendor.get("vendor_key") or "").strip()
    if not _is_valid_vendor_key(vendor_key):
        raise ValueError(
            f"po_materials.vendors.upsert_vendor: payload has no valid vendor_key "
            f"(got {vendor_key!r})"
        )

    active_raw = vendor.get("active")
    is_active = active_raw in (1, True, "1")
    categories = vendor.get("supply_categories")
    cells: dict[str, Any] = {
        COL_VENDOR_NAME: str(vendor.get("vendor_name") or "").strip(),
        COL_VENDOR_KEY: vendor_key,
        COL_ADDRESS: str(vendor.get("address") or "").strip(),
        COL_CONTACT_NAME: str(vendor.get("contact_name") or "").strip(),
        COL_CONTACT_EMAIL: str(vendor.get("contact_email") or "").strip(),
        COL_CONTACT_PHONE: str(vendor.get("contact_phone") or "").strip(),
        COL_REGION: str(vendor.get("region") or "").strip(),
        COL_SUPPLY_CATEGORIES: (
            [str(c).strip() for c in categories if str(c).strip()]
            if isinstance(categories, (list, tuple)) else []
        ),
        COL_TERMS_PROFILE: str(vendor.get("default_terms_profile") or "").strip(),
        COL_GTC_REFERENCE: str(vendor.get("gtc_reference") or "").strip(),
        COL_ACTIVE: ACTIVE if is_active else INACTIVE,
        COL_NOTES: str(vendor.get("notes") or "").strip(),
    }
    # Blank picklist scalars must not be WRITTEN as '' (the REGISTRY would reject a
    # non-member ''); dropping the key leaves the cell untouched on update and blank
    # on create — the picklist analogue of a None cell.
    for col in (COL_REGION, COL_TERMS_PROFILE):
        if not cells[col]:
            del cells[col]
    # Same rule for the MULTI_PICKLIST: an EMPTY Supply Categories list is UNWRITABLE
    # (the API rejects an empty values[] with errorCode 1012), so an empty list means
    # "leave the cell alone". Without this a vendor carrying no categories can NEVER
    # up-sync — it fences PERMANENTLY every cycle (25 of 33 vendors, 2026-08-10).
    if not cells[COL_SUPPLY_CATEGORIES]:
        del cells[COL_SUPPLY_CATEGORIES]

    existing = get_vendor_by_key(vendor_key)
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
