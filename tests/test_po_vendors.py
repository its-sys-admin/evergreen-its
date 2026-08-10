"""Tests for po_materials/vendors.py — SoR read, down-sync projection, up-sync
writer. Smartsheet mocked.

Run with: pytest -q tests/test_po_vendors.py
"""
from __future__ import annotations

from typing import Any

import pytest

from po_materials import vendors


def _sheet_row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "_row_id": 100,
        vendors.COL_VENDOR_NAME: "Chint Power Systems",
        vendors.COL_VENDOR_KEY: "VEN-000001",
        vendors.COL_ADDRESS: "2801 N State Hwy 78 Ste 100, Wylie TX",
        vendors.COL_CONTACT_NAME: "Jordan Lee",
        vendors.COL_CONTACT_EMAIL: "orders@chint.example",
        vendors.COL_CONTACT_PHONE: "555-0101",
        vendors.COL_REGION: "National",
        vendors.COL_SUPPLY_CATEGORIES: ["inverters"],
        vendors.COL_TERMS_PROFILE: "chint_vendor",
        vendors.COL_GTC_REFERENCE: "",
        vendors.COL_ACTIVE: "Active",
        vendors.COL_NOTES: "",
    }
    base.update(over)
    return base


# ---- SoR read ----------------------------------------------------------------


def test_get_vendor_by_key_filters_on_key(mocker) -> None:
    get_rows = mocker.patch(
        "po_materials.vendors.smartsheet_client.get_rows",
        return_value=[_sheet_row()],
    )
    row = vendors.get_vendor_by_key(" VEN-000001 ")
    assert row is not None and row[vendors.COL_VENDOR_NAME] == "Chint Power Systems"
    _, kwargs = get_rows.call_args
    assert kwargs["filters"] == {vendors.COL_VENDOR_KEY: "VEN-000001"}


def test_get_vendor_by_key_blank_key_is_none_without_read(mocker) -> None:
    get_rows = mocker.patch("po_materials.vendors.smartsheet_client.get_rows")
    assert vendors.get_vendor_by_key("") is None
    get_rows.assert_not_called()


# ---- down-sync projection ------------------------------------------------------


def test_down_sync_projects_worker_shape(mocker) -> None:
    mocker.patch(
        "po_materials.vendors.smartsheet_client.get_rows",
        return_value=[_sheet_row()],
    )
    payload = vendors.build_down_sync_payload()
    assert payload.skipped == []
    [v] = payload.vendors
    assert v == {
        "vendor_key": "VEN-000001",
        "vendor_name": "Chint Power Systems",
        "address": "2801 N State Hwy 78 Ste 100, Wylie TX",
        "contact_name": "Jordan Lee",
        "contact_email": "orders@chint.example",
        "contact_phone": "555-0101",
        "region": "National",
        "supply_categories": ["inverters"],
        "default_terms_profile": "chint_vendor",
        "gtc_reference": "",
        "active": 1,
        "notes": "",
    }


def test_down_sync_maps_lifecycle_and_comma_string_categories(mocker) -> None:
    mocker.patch(
        "po_materials.vendors.smartsheet_client.get_rows",
        return_value=[
            _sheet_row(**{vendors.COL_ACTIVE: "Archived",
                          vendors.COL_SUPPLY_CATEGORIES: "modules, racking"}),
        ],
    )
    [v] = vendors.build_down_sync_payload().vendors
    assert v["active"] == 0  # Inactive AND Archived both arrive 0
    assert v["supply_categories"] == ["modules", "racking"]  # display-string level


def test_down_sync_skips_malformed_rows_with_reason(mocker) -> None:
    """A bad Vendor Key / blank name would make the Worker reject the WHOLE batch —
    such rows are skipped-with-reason, never shipped."""
    mocker.patch(
        "po_materials.vendors.smartsheet_client.get_rows",
        return_value=[
            _sheet_row(),
            _sheet_row(_row_id=101, **{vendors.COL_VENDOR_KEY: "VENDOR-1"}),
            _sheet_row(_row_id=102, **{vendors.COL_VENDOR_KEY: ""}),
            _sheet_row(_row_id=103, **{vendors.COL_VENDOR_NAME: "  "}),
        ],
    )
    payload = vendors.build_down_sync_payload()
    assert len(payload.vendors) == 1
    assert sorted(row_id for row_id, _ in payload.skipped) == [101, 102, 103]


# ---- up-sync writer -------------------------------------------------------------


def _portal_vendor(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "vendor_key": "VEN-000002",
        "vendor_name": "VSUN Solar USA Inc",
        "address": "909 Corporate Way, Fremont CA",
        "contact_name": "Sam Chen",
        "contact_email": "po@vsun.example",
        "contact_phone": "555-0102",
        "region": "West",
        "supply_categories": ["modules"],
        "default_terms_profile": "negotiated_gtc",
        "gtc_reference": "https://app.box.com/file/9",
        "active": 1,
        "notes": "negotiated GTC on file",
        "origin": "portal",
        "mirror_version": 3,
        "mirrored_version": 1,
    }
    base.update(over)
    return base


def test_upsert_creates_when_absent(mocker) -> None:
    mocker.patch("po_materials.vendors.get_vendor_by_key", return_value=None)
    add = mocker.patch(
        "po_materials.vendors.smartsheet_client.add_rows", return_value=[555]
    )
    row_id = vendors.upsert_vendor(_portal_vendor())
    assert row_id == 555
    (sheet_id, [cells]), _ = add.call_args
    assert sheet_id == vendors.SHEET_ID
    assert cells[vendors.COL_VENDOR_KEY] == "VEN-000002"
    assert cells[vendors.COL_ACTIVE] == "Active"
    assert cells[vendors.COL_SUPPLY_CATEGORIES] == ["modules"]


def test_upsert_updates_existing_by_bridge_key(mocker) -> None:
    mocker.patch(
        "po_materials.vendors.get_vendor_by_key",
        return_value=_sheet_row(_row_id=777, **{vendors.COL_VENDOR_KEY: "VEN-000002"}),
    )
    update = mocker.patch("po_materials.vendors.smartsheet_client.update_rows")
    add = mocker.patch("po_materials.vendors.smartsheet_client.add_rows")
    row_id = vendors.upsert_vendor(_portal_vendor(active=0))
    assert row_id == 777
    add.assert_not_called()
    (sheet_id, [payload]), _ = update.call_args
    assert sheet_id == vendors.SHEET_ID
    assert payload["_row_id"] == 777
    assert payload[vendors.COL_ACTIVE] == "Inactive"  # deactivate, never delete (D4)


def test_upsert_preserves_archived_on_inactive_edit(mocker) -> None:
    """'Archived' is operator-only vocabulary the portal's 0/1 cannot express — an
    inactive portal edit must not downgrade it to 'Inactive' (§51 non-clobber)."""
    mocker.patch(
        "po_materials.vendors.get_vendor_by_key",
        return_value=_sheet_row(
            _row_id=778,
            **{vendors.COL_VENDOR_KEY: "VEN-000002", vendors.COL_ACTIVE: "Archived"},
        ),
    )
    update = mocker.patch("po_materials.vendors.smartsheet_client.update_rows")
    vendors.upsert_vendor(_portal_vendor(active=0))
    (_, [payload]), _ = update.call_args
    assert vendors.COL_ACTIVE not in payload


def test_upsert_drops_blank_picklist_scalars(mocker) -> None:
    """A blank Region / terms profile is DROPPED from the payload (an '' write would
    trip the picklist REGISTRY), leaving the cell untouched."""
    mocker.patch("po_materials.vendors.get_vendor_by_key", return_value=None)
    add = mocker.patch(
        "po_materials.vendors.smartsheet_client.add_rows", return_value=[1]
    )
    vendors.upsert_vendor(_portal_vendor(region="", default_terms_profile=""))
    (_, [cells]), _ = add.call_args
    assert vendors.COL_REGION not in cells
    assert vendors.COL_TERMS_PROFILE not in cells


def test_upsert_drops_empty_multipicklist(mocker) -> None:
    """An EMPTY Supply Categories list is DROPPED from the payload, exactly like a
    blank picklist scalar. The Smartsheet API rejects an empty MULTI_PICKLIST
    (`objectValue.values: []`) with errorCode 1012 "Required object attribute(s) are
    missing … multiPickList.values[]", so emitting the key at all is an unwritable
    payload — the vendor could never up-sync. Regression: 25 of 33 vendors carry no
    categories and every one of them permanently fenced on 2026-08-10."""
    mocker.patch("po_materials.vendors.get_vendor_by_key", return_value=None)
    add = mocker.patch(
        "po_materials.vendors.smartsheet_client.add_rows", return_value=[1]
    )
    vendors.upsert_vendor(_portal_vendor(supply_categories=[]))
    (_, [cells]), _ = add.call_args
    assert vendors.COL_SUPPLY_CATEGORIES not in cells
    # A NON-empty list is still written through unchanged.
    add.reset_mock()
    vendors.upsert_vendor(_portal_vendor(supply_categories=["modules"]))
    (_, [cells]), _ = add.call_args
    assert cells[vendors.COL_SUPPLY_CATEGORIES] == ["modules"]


def test_upsert_rejects_unkeyable_payload(mocker) -> None:
    add = mocker.patch("po_materials.vendors.smartsheet_client.add_rows")
    with pytest.raises(ValueError):
        vendors.upsert_vendor(_portal_vendor(vendor_key="nope"))
    add.assert_not_called()
