"""Unit tests for progress_reports.material_list — per-job Material List sheet find-or-create +
CHANGE-ONLY upsert + retire-removed (snapshot re-projection). All Smartsheet / capacity I/O is
mocked; no test touches live state."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from progress_reports import material_list


@pytest.fixture
def sc(mocker):
    return {
        "ensure_folder": mocker.patch(
            "progress_reports.material_list.hours_log._ensure_job_folder",
            return_value=7001,
        ),
        "find_sheet": mocker.patch(
            "progress_reports.material_list.smartsheet_client.find_sheet_by_name_in_folder",
            return_value=None,
        ),
        "create_sheet": mocker.patch(
            "progress_reports.material_list.smartsheet_client.create_sheet_in_folder",
            return_value=9001,
        ),
        "styles": mocker.patch(
            "progress_reports.material_list.smartsheet_client.apply_column_styles",
            return_value=None,
        ),
        "get_rows": mocker.patch(
            "progress_reports.material_list.smartsheet_client.get_rows", return_value=[]
        ),
        "add_rows": mocker.patch(
            "progress_reports.material_list.smartsheet_client.add_rows", return_value=[555]
        ),
        "update_rows": mocker.patch(
            "progress_reports.material_list.smartsheet_client.update_rows", return_value=None
        ),
        "find_folder": mocker.patch(
            "progress_reports.material_list.smartsheet_client.find_folder_by_name_in_workspace",
            return_value=7001,
        ),
        "capacity": mocker.patch(
            "progress_reports.material_list.sheet_capacity.check_create_headroom",
            return_value=SimpleNamespace(note="", ok=True, current=1, ceiling=100, margin=50),
        ),
        "route": mocker.patch(
            "progress_reports.material_list.sheet_capacity.route_breach_to_review_queue",
            return_value=None,
        ),
        "log": mocker.patch("progress_reports.material_list.error_log.log", return_value=None),
        "review": mocker.patch(
            "progress_reports.material_list.review_queue.add", return_value=1
        ),
        "get_setting": mocker.patch(
            "progress_reports.material_list.smartsheet_client.get_setting", return_value="15000"
        ),
        # 0059 column back-fill on the FIND branch. Defaults to "nothing was missing" so every
        # pre-existing test stays byte-identical; the back-fill tests below make it return titles.
        "ensure_columns": mocker.patch(
            "progress_reports.material_list.smartsheet_client.ensure_columns", return_value=[]
        ),
    }


# ---- sheet name (50-char cap) --------------------------------------------


def test_sheet_name_short_is_verbatim():
    assert material_list.material_list_sheet_name("Bradley 1") == "Bradley 1 — Material List"


def test_sheet_name_truncates_long_prefix_to_cap():
    name = material_list.material_list_sheet_name("X" * 80)
    assert len(name) <= material_list.SHEET_NAME_MAX
    assert name.endswith(material_list.SHEET_SUFFIX)


# ---- ensure_material_list_sheet ------------------------------------------


def test_ensure_returns_existing_sheet_without_create(sc):
    sc["find_sheet"].return_value = 4242
    assert material_list.ensure_material_list_sheet("Job One") == 4242
    sc["create_sheet"].assert_not_called()
    sc["capacity"].assert_not_called()  # no create branch → no capacity check


def test_ensure_creates_sheet_when_missing(sc):
    sid = material_list.ensure_material_list_sheet("Job One")
    assert sid == 9001
    sc["create_sheet"].assert_called_once()
    sc["styles"].assert_called_once_with(9001, material_list.MATERIAL_LIST_STYLES)
    sc["capacity"].assert_called_once()  # A1 tripwire runs only on create


def test_ensure_delegates_folder_to_hours_log(sc):
    # The Material List sheet reuses the Hours Log's per-job folder resolver (single authority).
    material_list.ensure_material_list_sheet("Job One")
    sc["ensure_folder"].assert_called_once_with("Job One")


def test_ensure_capacity_breach_warns_but_still_creates(sc):
    sc["capacity"].return_value = SimpleNamespace(note="", ok=False, current=99, ceiling=100, margin=1)
    material_list.ensure_material_list_sheet("Job One")
    sc["route"].assert_called_once()          # breach enqueued to the Review Queue
    sc["create_sheet"].assert_called_once()   # advisory — the create STILL proceeds


# ---- find_material_list_sheet (find, NEVER create) -----------------------


def test_find_sheet_returns_none_when_no_folder(sc):
    sc["find_folder"].return_value = None
    assert material_list.find_material_list_sheet("Job One") is None
    sc["create_sheet"].assert_not_called()


def test_find_sheet_returns_id_when_present(sc):
    sc["find_folder"].return_value = 7001
    sc["find_sheet"].return_value = 4242
    assert material_list.find_material_list_sheet("Job One") == 4242
    sc["create_sheet"].assert_not_called()


# ---- upsert_line_row (CHANGE-ONLY) ---------------------------------------


def _upsert_kwargs(**over: Any) -> dict[str, Any]:
    kw: dict[str, Any] = dict(
        line_uuid="u-10", line="Q.PEAK_DUO_XL", material="Q.PEAK_DUO_XL",
        description="Solar panels", qty="120", unit="panels", expected_date="2026-07-10",
        status="expected", delivered_qty="", received_at="", received_by="", note="",
        unplanned="",
    )
    kw.update(over)
    return kw


def _existing_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "_row_id": 888,
        material_list.COL_LINE_UUID: "u-10",
        material_list.COL_LINE: "Q.PEAK_DUO_XL",
        material_list.COL_MATERIAL: "Q.PEAK_DUO_XL",
        material_list.COL_DESCRIPTION: "Solar panels",
        material_list.COL_QTY: "120",
        material_list.COL_UNIT: "panels",
        material_list.COL_EXPECTED_DATE: "2026-07-10",
        material_list.COL_STATUS: "expected",
        material_list.COL_DELIVERED_QTY: "",
        material_list.COL_RECEIVED_AT: "",
        material_list.COL_RECEIVED_BY: "",
        material_list.COL_NOTE: "",
        material_list.COL_UNPLANNED: "",
        material_list.COL_ON_LIST: material_list.ON_LIST_ACTIVE,
    }
    row.update(over)
    return row


def test_upsert_adds_new_row_when_absent(sc):
    sc["get_rows"].return_value = []  # find_line_row → None
    assert material_list.upsert_line_row(9001, **_upsert_kwargs()) == 555
    sc["add_rows"].assert_called_once()
    cells = sc["add_rows"].call_args.args[1][0]
    assert cells[material_list.COL_LINE_UUID] == "u-10"
    assert cells[material_list.COL_LINE] == "Q.PEAK_DUO_XL"
    assert cells[material_list.COL_ON_LIST] == material_list.ON_LIST_ACTIVE


def test_upsert_change_only_noop_when_identical(sc):
    sc["get_rows"].return_value = [_existing_row()]
    assert material_list.upsert_line_row(9001, **_upsert_kwargs()) == 888
    sc["update_rows"].assert_not_called()  # nothing changed → no needless write
    sc["add_rows"].assert_not_called()


def test_upsert_change_updates_when_a_value_differs(sc):
    sc["get_rows"].return_value = [_existing_row()]
    material_list.upsert_line_row(
        9001, **_upsert_kwargs(status="received", delivered_qty="120", received_at="2026-07-11",
                               received_by="Mo Manager")
    )
    upd = sc["update_rows"].call_args.args[1][0]
    assert upd["_row_id"] == 888
    assert upd[material_list.COL_STATUS] == "received"
    assert upd[material_list.COL_DELIVERED_QTY] == "120"
    assert upd[material_list.COL_RECEIVED_BY] == "Mo Manager"
    assert upd[material_list.COL_ON_LIST] == material_list.ON_LIST_ACTIVE


def test_upsert_reactivates_row_that_was_removed(sc):
    # A re-added line: identical data but its On List cell is Removed → the mismatch drives an
    # update that flips it back to Active.
    sc["get_rows"].return_value = [_existing_row(**{material_list.COL_ON_LIST: material_list.ON_LIST_REMOVED})]
    material_list.upsert_line_row(9001, **_upsert_kwargs())
    upd = sc["update_rows"].call_args.args[1][0]
    assert upd[material_list.COL_ON_LIST] == material_list.ON_LIST_ACTIVE


def test_upsert_free_text_line_material_placeholder(sc):
    # A free-text (no-catalog) line carries '—' in Material and its description as the primary Line.
    sc["get_rows"].return_value = []
    material_list.upsert_line_row(
        9001, **_upsert_kwargs(line="Rebar bundles", material=material_list.MATERIAL_NONE,
                               description="Rebar bundles")
    )
    cells = sc["add_rows"].call_args.args[1][0]
    assert cells[material_list.COL_MATERIAL] == "—"
    assert cells[material_list.COL_LINE] == "Rebar bundles"


def test_upsert_unplanned_yes_written(sc):
    sc["get_rows"].return_value = []
    material_list.upsert_line_row(9001, **_upsert_kwargs(unplanned=material_list.UNPLANNED_YES))
    cells = sc["add_rows"].call_args.args[1][0]
    assert cells[material_list.COL_UNPLANNED] == "Yes"


# ---- retire_removed (never delete) ---------------------------------------


def test_retire_marks_rows_not_in_snapshot_removed(sc):
    sc["get_rows"].return_value = [
        {"_row_id": 1, material_list.COL_LINE_UUID: "u-10", material_list.COL_ON_LIST: "Active"},
        {"_row_id": 2, material_list.COL_LINE_UUID: "u-11", material_list.COL_ON_LIST: "Active"},
    ]
    retired = material_list.retire_removed(9001, {"u-10"})  # u-11 was removed from the list
    assert retired == 1
    upd = sc["update_rows"].call_args.args[1]
    assert len(upd) == 1
    assert upd[0]["_row_id"] == 2
    assert upd[0][material_list.COL_ON_LIST] == material_list.ON_LIST_REMOVED
    # NEVER deletes — it is an update, not a delete
    assert sc["add_rows"].call_count == 0


def test_retire_is_idempotent_skips_already_removed(sc):
    sc["get_rows"].return_value = [
        {"_row_id": 2, material_list.COL_LINE_UUID: "u-11", material_list.COL_ON_LIST: "Removed"},
    ]
    retired = material_list.retire_removed(9001, {"u-10"})
    assert retired == 0
    sc["update_rows"].assert_not_called()  # already Removed → no needless write


def test_retire_leaves_in_snapshot_rows_untouched(sc):
    sc["get_rows"].return_value = [
        {"_row_id": 1, material_list.COL_LINE_UUID: "u-10", material_list.COL_ON_LIST: "Active"},
    ]
    assert material_list.retire_removed(9001, {"u-10", "u-11"}) == 0
    sc["update_rows"].assert_not_called()


def test_retire_empty_current_marks_all_removed(sc):
    # The reconcile-zeroed case: empty current set → every Active row marked Removed.
    sc["get_rows"].return_value = [
        {"_row_id": 1, material_list.COL_LINE_UUID: "u-10", material_list.COL_ON_LIST: "Active"},
        {"_row_id": 2, material_list.COL_LINE_UUID: "u-11", material_list.COL_ON_LIST: "Active"},
    ]
    assert material_list.retire_removed(9001, set()) == 2


# ---- find_line_row -------------------------------------------------------


def test_find_line_row_matches_by_uuid(sc):
    sc["get_rows"].return_value = [{"_row_id": 5, material_list.COL_LINE_UUID: "u-10"}]
    assert material_list.find_line_row(9001, "u-10") == {
        "_row_id": 5, material_list.COL_LINE_UUID: "u-10"
    }
    assert material_list.find_line_row(9001, "u-99") is None
    assert material_list.find_line_row(9001, "") is None


# ---- check_row_cap (§51 A5 row-cap watchdog, SoR-safe WARN-only) ----------


def test_row_cap_noop_under_threshold(sc):
    material_list.check_row_cap(9001, "Job One — Material List", row_count=100)  # < 15000
    sc["review"].assert_not_called()


def test_row_cap_warns_and_enqueues_over_threshold(sc):
    material_list.check_row_cap(9001, "Job One — Material List", row_count=15000)  # >= threshold
    sc["review"].assert_called_once()
    assert sc["review"].call_args.kwargs["workstream"] == "progress_reports"
    assert any(
        c.kwargs.get("error_code") == "material_list_row_cap_warn"
        for c in sc["log"].call_args_list
    )
    sc["update_rows"].assert_not_called()  # NEVER deletes/mutates rows on the cap path


def test_row_cap_check_never_raises_on_read_failure(sc):
    sc["get_setting"].side_effect = RuntimeError("smartsheet down")
    material_list.check_row_cap(9001, "Job One — Material List", row_count=99999)
    assert any(
        c.kwargs.get("error_code") == "material_list_row_cap_check_failed"
        for c in sc["log"].call_args_list
    )


# ---- 0059 columns (Part Number / Category / Expected Ship Date) ------------


def test_new_columns_are_in_the_creation_schema_and_tracked():
    for col in (
        material_list.COL_PART_NUMBER,
        material_list.COL_CATEGORY,
        material_list.COL_EXPECTED_SHIP_DATE,
    ):
        assert col in [c["title"] for c in material_list.MATERIAL_LIST_COLUMNS]
        # In _TRACKED_COLS too, or a change to one of them would never trigger an update_rows.
        assert col in material_list._TRACKED_COLS


def test_expected_ship_date_is_a_date_column_and_distinct_from_expected_date():
    by_title = {c["title"]: c for c in material_list.MATERIAL_LIST_COLUMNS}
    assert by_title[material_list.COL_EXPECTED_SHIP_DATE]["type"] == "DATE"
    # Two SEPARATE columns: 0059 keeps Expected Date meaning DELIVERY. One column cannot carry both.
    assert material_list.COL_EXPECTED_SHIP_DATE != material_list.COL_EXPECTED_DATE
    assert material_list.COL_EXPECTED_DATE in by_title


def test_backfill_set_is_every_non_primary_creation_column():
    # A tail slice would have to be re-cut by hand on each future addition, and getting it wrong is
    # silent. Deriving it means a new column is back-filled automatically.
    titles = {c["title"] for c in material_list.BACKFILL_COLUMNS}
    assert titles == {
        c["title"] for c in material_list.MATERIAL_LIST_COLUMNS if not c.get("primary")
    }
    # The primary is excluded — a sheet's primary column is fixed at create and cannot be added.
    assert material_list.COL_LINE not in titles


def test_new_columns_appended_last_so_old_and_new_sheets_match():
    # ensure_columns APPENDS. If the creation schema slotted these mid-list, a back-filled sheet and
    # a freshly-created one would hold the same columns in a different ORDER, forever.
    tail = [c["title"] for c in material_list.MATERIAL_LIST_COLUMNS[-3:]]
    assert tail == [
        material_list.COL_PART_NUMBER,
        material_list.COL_CATEGORY,
        material_list.COL_EXPECTED_SHIP_DATE,
    ]


def test_upsert_writes_the_new_columns(sc):
    sc["get_rows"].return_value = []
    material_list.upsert_line_row(
        9001, line_uuid="u-1", line="Piles", material="—", description="piles",
        qty="120", unit="ea", expected_date="2026-07-10", status="expected",
        delivered_qty="", received_at="", received_by="", note="", unplanned="",
        part_number="7000153", category="HARDWARE", expected_ship_date="2026-07-02",
    )
    cells = sc["add_rows"].call_args.args[1][0]
    assert cells[material_list.COL_PART_NUMBER] == "7000153"
    assert cells[material_list.COL_CATEGORY] == "HARDWARE"
    assert cells[material_list.COL_EXPECTED_SHIP_DATE] == "2026-07-02"
    assert cells[material_list.COL_EXPECTED_DATE] == "2026-07-10"  # delivery, untouched


def test_upsert_defaults_the_new_columns_blank_for_a_pre_0059_line(sc):
    # A line written before the migration has no value for these; '' is the truthful reading.
    sc["get_rows"].return_value = []
    material_list.upsert_line_row(
        9001, line_uuid="u-1", line="Piles", material="—", description="piles",
        qty="120", unit="ea", expected_date="2026-07-10", status="expected",
        delivered_qty="", received_at="", received_by="", note="", unplanned="",
    )
    cells = sc["add_rows"].call_args.args[1][0]
    assert cells[material_list.COL_PART_NUMBER] == ""
    assert cells[material_list.COL_CATEGORY] == ""
    assert cells[material_list.COL_EXPECTED_SHIP_DATE] == ""


def test_ensure_backfills_columns_on_the_find_branch(sc):
    sc["find_sheet"].return_value = 4242
    assert material_list.ensure_material_list_sheet("Job One") == 4242
    sc["ensure_columns"].assert_called_once_with(4242, material_list.BACKFILL_COLUMNS)
    sc["create_sheet"].assert_not_called()


def test_ensure_does_not_backfill_on_the_create_branch(sc):
    # A sheet created from MATERIAL_LIST_COLUMNS already has every column — a back-fill there would
    # be a pointless read on the hot create path.
    material_list.ensure_material_list_sheet("Job One")
    sc["create_sheet"].assert_called_once()
    sc["ensure_columns"].assert_not_called()


def test_backfill_logs_what_it_added(sc):
    sc["find_sheet"].return_value = 4242
    sc["ensure_columns"].return_value = ["Part Number", "Category"]
    material_list.ensure_material_list_sheet("Job One")
    assert any(
        c.kwargs.get("error_code") == "material_list_columns_backfilled"
        for c in sc["log"].call_args_list
    )


def test_backfill_is_silent_when_nothing_was_missing(sc):
    # The steady-state path must not WARN every cycle for every job — that is how a log stops being
    # read.
    sc["find_sheet"].return_value = 4242
    sc["ensure_columns"].return_value = []
    material_list.ensure_material_list_sheet("Job One")
    assert not any(
        c.kwargs.get("error_code") == "material_list_columns_backfilled"
        for c in sc["log"].call_args_list
    )


def test_backfill_failure_warns_but_still_returns_the_sheet(sc):
    # A stale-schema sheet still mirrors every column it DOES have; a top-up failure must not stop
    # the mirror.
    sc["find_sheet"].return_value = 4242
    sc["ensure_columns"].side_effect = RuntimeError("breaker open")
    assert material_list.ensure_material_list_sheet("Job One") == 4242
    assert any(
        c.kwargs.get("error_code") == "material_list_column_backfill_failed"
        for c in sc["log"].call_args_list
    )
