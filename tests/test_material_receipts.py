"""Unit tests for progress_reports.material_receipts — per-job Material Receipts sheet
find-or-create + CHANGE-ONLY upsert (APPEND-ONLY delivery ledger; no retire). All Smartsheet /
capacity I/O is mocked; no test touches live state.

Cloned from tests/test_material_incidents.py (the sibling ledger) and extended where the receipts
mirror genuinely differs: the upsert key is `Event UUID`, and TWO columns are DERIVED — `Line Status`
and `Line Qty Received` — so unlike the incident ledger's single mutable field there are two
independent reasons a re-projection legitimately rewrites an otherwise-immutable event row.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from progress_reports import material_receipts


@pytest.fixture
def sc(mocker):
    return {
        "ensure_folder": mocker.patch(
            "progress_reports.material_receipts.hours_log._ensure_job_folder",
            return_value=7001,
        ),
        "find_sheet": mocker.patch(
            "progress_reports.material_receipts.smartsheet_client.find_sheet_by_name_in_folder",
            return_value=None,
        ),
        "create_sheet": mocker.patch(
            "progress_reports.material_receipts.smartsheet_client.create_sheet_in_folder",
            return_value=9001,
        ),
        "styles": mocker.patch(
            "progress_reports.material_receipts.smartsheet_client.apply_column_styles",
            return_value=None,
        ),
        "get_rows": mocker.patch(
            "progress_reports.material_receipts.smartsheet_client.get_rows", return_value=[]
        ),
        "add_rows": mocker.patch(
            "progress_reports.material_receipts.smartsheet_client.add_rows", return_value=[555]
        ),
        "update_rows": mocker.patch(
            "progress_reports.material_receipts.smartsheet_client.update_rows", return_value=None
        ),
        "find_folder": mocker.patch(
            "progress_reports.material_receipts.smartsheet_client.find_folder_by_name_in_workspace",
            return_value=7001,
        ),
        "capacity": mocker.patch(
            "progress_reports.material_receipts.sheet_capacity.check_create_headroom",
            return_value=SimpleNamespace(note="", ok=True, current=1, ceiling=100, margin=50),
        ),
        "route": mocker.patch(
            "progress_reports.material_receipts.sheet_capacity.route_breach_to_review_queue",
            return_value=None,
        ),
        "log": mocker.patch("progress_reports.material_receipts.error_log.log", return_value=None),
        "review": mocker.patch(
            "progress_reports.material_receipts.review_queue.add", return_value=1
        ),
        "get_setting": mocker.patch(
            "progress_reports.material_receipts.smartsheet_client.get_setting",
            return_value="15000",
        ),
    }


# ---- sheet name (50-char cap) --------------------------------------------


def test_sheet_name_short_is_verbatim():
    assert (
        material_receipts.material_receipts_sheet_name("Bradley 1")
        == "Bradley 1 — Material Receipts"
    )


def test_sheet_name_truncates_long_prefix_to_cap():
    name = material_receipts.material_receipts_sheet_name("X" * 80)
    assert len(name) <= material_receipts.SHEET_NAME_MAX
    assert name.endswith(material_receipts.SHEET_SUFFIX)


# ---- ensure_material_receipts_sheet --------------------------------------


def test_ensure_returns_existing_sheet_without_create(sc):
    sc["find_sheet"].return_value = 4242
    assert material_receipts.ensure_material_receipts_sheet("Job One") == 4242
    sc["create_sheet"].assert_not_called()
    sc["capacity"].assert_not_called()  # no create branch → no capacity check


def test_ensure_creates_sheet_when_missing(sc):
    sid = material_receipts.ensure_material_receipts_sheet("Job One")
    assert sid == 9001
    sc["create_sheet"].assert_called_once()
    sc["styles"].assert_called_once_with(9001, material_receipts.MATERIAL_RECEIPTS_STYLES)
    sc["capacity"].assert_called_once()  # A1 tripwire runs only on create


def test_ensure_delegates_folder_to_hours_log(sc):
    # The Material Receipts sheet reuses the Hours Log's per-job folder resolver (single authority),
    # which is what lands it BESIDE the other per-job trackers rather than in a folder of its own.
    material_receipts.ensure_material_receipts_sheet("Job One")
    sc["ensure_folder"].assert_called_once_with("Job One")


def test_ensure_capacity_breach_warns_but_still_creates(sc):
    sc["capacity"].return_value = SimpleNamespace(
        note="", ok=False, current=99, ceiling=100, margin=1
    )
    material_receipts.ensure_material_receipts_sheet("Job One")
    sc["route"].assert_called_once()          # breach enqueued to the Review Queue
    sc["create_sheet"].assert_called_once()   # advisory — the create STILL proceeds


def test_ensure_duplicate_race_adopts_first_match(sc):
    # create returns 9001 but a concurrent create landed 8888 first → adopt 8888, WARN for cleanup.
    sc["find_sheet"].side_effect = [None, 8888]  # pre-find miss, post-find hits the racer's sheet
    assert material_receipts.ensure_material_receipts_sheet("Job One") == 8888
    assert any(
        c.kwargs.get("error_code") == "material_receipts_sheet_race_duplicate"
        for c in sc["log"].call_args_list
    )


# ---- upsert_receipt_row (CHANGE-ONLY, APPEND-ONLY) -----------------------


def _upsert_kwargs(**over: Any) -> dict[str, Any]:
    kw: dict[str, Any] = dict(
        event_uuid="evt-10", material="Q.PEAK panels", kind="Partial", qty="40",
        unit="ea", part_number="7000153", line_uuid="u-10", line_status="partial",
        line_qty_expected="120", line_qty_received="40", bol="BOL-8891",
        note="2 pallets on this load", received_by="Mo Manager", event_date="2026-08-06",
    )
    kw.update(over)
    return kw


def _existing_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "_row_id": 888,
        material_receipts.COL_EVENT_UUID: "evt-10",
        material_receipts.COL_MATERIAL: "Q.PEAK panels",
        material_receipts.COL_KIND: "Partial",
        material_receipts.COL_QTY: "40",
        material_receipts.COL_UNIT: "ea",
        material_receipts.COL_PART_NUMBER: "7000153",
        material_receipts.COL_LINE_UUID: "u-10",
        material_receipts.COL_LINE_STATUS: "partial",
        material_receipts.COL_LINE_QTY_EXPECTED: "120",
        material_receipts.COL_LINE_QTY_RECEIVED: "40",
        material_receipts.COL_BOL: "BOL-8891",
        material_receipts.COL_NOTE: "2 pallets on this load",
        material_receipts.COL_RECEIVED_BY: "Mo Manager",
        material_receipts.COL_EVENT_DATE: "2026-08-06",
    }
    row.update(over)
    return row


def test_upsert_adds_new_row_when_absent(sc):
    sc["get_rows"].return_value = []  # find_receipt_row → None
    assert material_receipts.upsert_receipt_row(9001, **_upsert_kwargs()) == 555
    sc["add_rows"].assert_called_once()
    cells = sc["add_rows"].call_args.args[1][0]
    assert cells[material_receipts.COL_EVENT_UUID] == "evt-10"
    assert cells[material_receipts.COL_MATERIAL] == "Q.PEAK panels"
    assert cells[material_receipts.COL_KIND] == "Partial"
    assert cells[material_receipts.COL_BOL] == "BOL-8891"
    # APPEND-ONLY — there is NO On List / Removed concept on this ledger.
    assert "On List" not in cells


def test_upsert_change_only_noop_when_identical(sc):
    sc["get_rows"].return_value = [_existing_row()]
    assert material_receipts.upsert_receipt_row(9001, **_upsert_kwargs()) == 888
    sc["update_rows"].assert_not_called()  # immutable event, nothing changed → no needless write
    sc["add_rows"].assert_not_called()


def test_upsert_updates_when_line_status_flips_to_incident(sc):
    # DERIVED column #1: a problem flagged later against the referenced line moves its coarse status,
    # which is one of only two reasons an already-written event row is legitimately rewritten.
    sc["get_rows"].return_value = [_existing_row()]
    material_receipts.upsert_receipt_row(9001, **_upsert_kwargs(line_status="incident"))
    upd = sc["update_rows"].call_args.args[1][0]
    assert upd["_row_id"] == 888
    assert upd[material_receipts.COL_LINE_STATUS] == "incident"


def test_upsert_updates_when_line_qty_received_rollup_grows(sc):
    # DERIVED column #2: the Worker recomputes the rollup across ALL events for the line, so a LATER
    # truckload raises this event row's Line Qty Received even though the event itself never changed.
    # This is the field that would silently go stale under a write-once mirror.
    sc["get_rows"].return_value = [_existing_row()]
    material_receipts.upsert_receipt_row(9001, **_upsert_kwargs(line_qty_received="120"))
    upd = sc["update_rows"].call_args.args[1][0]
    assert upd["_row_id"] == 888
    assert upd[material_receipts.COL_LINE_QTY_RECEIVED] == "120"
    # The event's OWN qty is immutable and must not be dragged along by the rollup change.
    assert upd[material_receipts.COL_QTY] == "40"


def test_upsert_not_delivered_carries_blank_qty(sc):
    # A not-delivered mark records that nothing arrived: qty is NULL in D1 and lands blank here,
    # while the note (required on that kind) still files.
    sc["get_rows"].return_value = []
    material_receipts.upsert_receipt_row(
        9001, **_upsert_kwargs(kind="Not delivered", qty="", note="Truck never showed")
    )
    cells = sc["add_rows"].call_args.args[1][0]
    assert cells[material_receipts.COL_KIND] == "Not delivered"
    assert cells[material_receipts.COL_QTY] == ""
    assert cells[material_receipts.COL_NOTE] == "Truck never showed"


def test_upsert_unlinked_event_blank_line_fields(sc):
    # An event whose line was since deleted → Line UUID / Line Status / line qty blanks; still files.
    sc["get_rows"].return_value = []
    material_receipts.upsert_receipt_row(
        9001,
        **_upsert_kwargs(
            line_uuid="", line_status="", line_qty_expected="", line_qty_received=""
        ),
    )
    cells = sc["add_rows"].call_args.args[1][0]
    assert cells[material_receipts.COL_LINE_UUID] == ""
    assert cells[material_receipts.COL_LINE_STATUS] == ""
    assert cells[material_receipts.COL_MATERIAL] == "Q.PEAK panels"


def test_upsert_never_deletes_or_retires(sc):
    # Belt-and-suspenders: the module exposes no retire/delete surface — proving append-only, which
    # is what makes the #468 zero-drop class structurally impossible here.
    assert not hasattr(material_receipts, "retire_removed")
    assert not hasattr(material_receipts, "retire_receipts")


def test_event_uuid_is_excluded_from_change_detection():
    # The key must not participate in the change compare — if it did, every row would look "changed"
    # against itself only when the key differed, which is precisely the case that must MISS and append.
    assert material_receipts.COL_EVENT_UUID not in material_receipts._TRACKED_COLS
    # ...and every other data column MUST participate, or a real edit would silently no-op.
    for col in (
        material_receipts.COL_MATERIAL, material_receipts.COL_KIND, material_receipts.COL_QTY,
        material_receipts.COL_UNIT, material_receipts.COL_PART_NUMBER,
        material_receipts.COL_LINE_UUID, material_receipts.COL_LINE_STATUS,
        material_receipts.COL_LINE_QTY_EXPECTED, material_receipts.COL_LINE_QTY_RECEIVED,
        material_receipts.COL_BOL, material_receipts.COL_NOTE,
        material_receipts.COL_RECEIVED_BY, material_receipts.COL_EVENT_DATE,
    ):
        assert col in material_receipts._TRACKED_COLS


# ---- find_receipt_row ----------------------------------------------------


def test_find_receipt_row_matches_by_uuid(sc):
    sc["get_rows"].return_value = [{"_row_id": 5, material_receipts.COL_EVENT_UUID: "evt-10"}]
    assert material_receipts.find_receipt_row(9001, "evt-10") == {
        "_row_id": 5, material_receipts.COL_EVENT_UUID: "evt-10"
    }
    assert material_receipts.find_receipt_row(9001, "evt-99") is None
    assert material_receipts.find_receipt_row(9001, "") is None


# ---- check_row_cap (§51 A5 row-cap watchdog, SoR-safe WARN-only) ----------


def test_row_cap_noop_under_threshold(sc):
    material_receipts.check_row_cap(9001, "Job One — Material Receipts", row_count=100)  # < 15000
    sc["review"].assert_not_called()


def test_row_cap_warns_and_enqueues_over_threshold(sc):
    material_receipts.check_row_cap(9001, "Job One — Material Receipts", row_count=15000)
    sc["review"].assert_called_once()
    assert sc["review"].call_args.kwargs["workstream"] == "progress_reports"
    assert any(
        c.kwargs.get("error_code") == "material_receipts_row_cap_warn"
        for c in sc["log"].call_args_list
    )
    sc["update_rows"].assert_not_called()  # NEVER deletes/mutates rows on the cap path


def test_row_cap_check_never_raises_on_read_failure(sc):
    sc["get_setting"].side_effect = RuntimeError("smartsheet down")
    material_receipts.check_row_cap(9001, "Job One — Material Receipts", row_count=99999)
    assert any(
        c.kwargs.get("error_code") == "material_receipts_row_cap_check_failed"
        for c in sc["log"].call_args_list
    )
