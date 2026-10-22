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
from unittest.mock import MagicMock

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
        # The void columns' back-fill (`backfill_columns`, called by the pass AFTER its index read).
        # Default: nothing was missing, and nothing is missing afterwards either.
        "ensure_columns": mocker.patch(
            "progress_reports.material_receipts.smartsheet_client.ensure_columns",
            return_value=[],
        ),
        "missing_columns": mocker.patch(
            "progress_reports.material_receipts.smartsheet_client.missing_columns",
            return_value=[],
        ),
        # Never called by this module — patched so a test can PROVE that (a voided mark is
        # annotated, never deleted).
        "delete_rows": mocker.patch(
            "progress_reports.material_receipts.smartsheet_client.delete_rows",
            return_value=None,
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
        # 0080 void annotation — a counting (never-voided, never-linked) mark.
        entry_status=material_receipts.ENTRY_ACTIVE, voided_by="", voided_at="",
        void_reason="", supersedes="", superseded_by="",
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
        material_receipts.COL_ENTRY_STATUS: material_receipts.ENTRY_ACTIVE,
        material_receipts.COL_VOIDED_BY: "",
        material_receipts.COL_VOIDED_AT: "",
        material_receipts.COL_VOID_REASON: "",
        material_receipts.COL_SUPERSEDES: "",
        material_receipts.COL_SUPERSEDED_BY: "",
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
        *_VOID_COLS,
    ):
        assert col in material_receipts._TRACKED_COLS


# ---- 0080 VOID annotation: annotate in place, never delete -----------------
#
# A void is a further fact ABOUT a mark, not its removal, so the ledger row stays and is annotated.
# Six TEXT_NUMBER columns carry it, appended LAST so a back-filled sheet and a freshly-created one
# hold the same columns in the same order (`ensure_columns` appends).

_VOID_COLS: tuple[str, ...] = (
    "Entry Status", "Voided By", "Voided At", "Void Reason", "Supersedes", "Superseded By",
)


def _voided_kwargs(**over: Any) -> dict[str, Any]:
    """Synthetic voided-mark kwargs (no real job / vendor / person names — the repo is public)."""
    return _t_kwargs(
        "evt-t1",
        entry_status=material_receipts.ENTRY_VOIDED, voided_by="Test Crew Lead",
        voided_at="2026-10-21 14:05", void_reason="Counted twice", **over,
    )


def _t_row(**over: Any) -> dict[str, Any]:
    """The sheet row `_t_kwargs("evt-t1")` writes — a counting mark, all synthetic values."""
    row = _existing_row(**{
        material_receipts.COL_EVENT_UUID: "evt-t1", material_receipts.COL_MATERIAL: "Test panel",
        material_receipts.COL_PART_NUMBER: "TP-0001", material_receipts.COL_LINE_UUID: "u-t1",
        material_receipts.COL_BOL: "BOL-T0001", material_receipts.COL_NOTE: "",
        material_receipts.COL_RECEIVED_BY: "Test Crew Lead",
    })
    row.update(over)
    return row


def _t_voided_row(**over: Any) -> dict[str, Any]:
    """The sheet row `_voided_kwargs()` writes — the same mark, annotated."""
    return _t_row(**{
        material_receipts.COL_ENTRY_STATUS: "Voided",
        material_receipts.COL_VOIDED_BY: "Test Crew Lead",
        material_receipts.COL_VOIDED_AT: "2026-10-21 14:05",
        material_receipts.COL_VOID_REASON: "Counted twice",
        **over,
    })


def test_void_columns_are_the_six_appended_last_as_plain_text():
    tail = material_receipts.MATERIAL_RECEIPTS_COLUMNS[-6:]
    assert tuple(c["title"] for c in tail) == _VOID_COLS
    # TEXT_NUMBER, never PICKLIST: a controlled vocabulary in a text cell is the hours_log Status
    # precedent, and it keeps these columns out of picklist_validation.REGISTRY entirely.
    assert {c["type"] for c in tail} == {"TEXT_NUMBER"}
    assert (
        material_receipts.COL_ENTRY_STATUS, material_receipts.COL_VOIDED_BY,
        material_receipts.COL_VOIDED_AT, material_receipts.COL_VOID_REASON,
        material_receipts.COL_SUPERSEDES, material_receipts.COL_SUPERSEDED_BY,
    ) == _VOID_COLS
    assert (material_receipts.ENTRY_ACTIVE, material_receipts.ENTRY_VOIDED) == ("Active", "Voided")
    # Every column has a style entry (a missing one would leave it at the default width).
    styled = {s["title"] for s in material_receipts.MATERIAL_RECEIPTS_STYLES}
    assert set(_VOID_COLS) <= styled


def test_backfill_set_is_every_non_primary_creation_column():
    titles = [c["title"] for c in material_receipts.BACKFILL_COLUMNS]
    assert titles == [
        c["title"] for c in material_receipts.MATERIAL_RECEIPTS_COLUMNS if not c.get("primary")
    ]
    assert set(_VOID_COLS) <= set(titles)
    assert material_receipts.COL_MATERIAL not in titles  # a sheet's primary is fixed at create


def test_backfill_adds_all_six_void_columns_in_one_call(sc):
    assert material_receipts.backfill_columns(4242, "Job One — Material Receipts") is True
    # ONE call carrying every spec — never one call per column — so the six land under the single
    # shared index `ensure_columns` assigns (a split call is fine for the API but would not be the
    # material_list shape; a multi-index body is what 400'd live with errorCode 1135).
    sc["ensure_columns"].assert_called_once_with(4242, material_receipts.BACKFILL_COLUMNS)
    passed = [c["title"] for c in sc["ensure_columns"].call_args.args[1]]
    assert passed[-6:] == list(_VOID_COLS)


def test_ensure_never_backfills_the_pass_does_it_after_its_index_read(sc):
    # P2: ensure is find-or-create ONLY. Back-filling here ran BEFORE the pass's index read, on a
    # cold column map — a full-sheet read of its own, every cycle, for a sheet about to be read
    # anyway. The pass now reads first (seeding the map) and back-fills after, for free.
    sc["find_sheet"].return_value = 4242
    assert material_receipts.ensure_material_receipts_sheet("Job One") == 4242
    sc["ensure_columns"].assert_not_called()
    sc["create_sheet"].assert_not_called()


def test_ensure_does_not_backfill_on_the_create_branch(sc):
    material_receipts.ensure_material_receipts_sheet("Job One")
    sc["create_sheet"].assert_called_once()
    assert sc["create_sheet"].call_args.args[2] is material_receipts.MATERIAL_RECEIPTS_COLUMNS
    sc["ensure_columns"].assert_not_called()


def test_backfill_logs_what_it_added_and_is_silent_when_nothing_was_missing(sc):
    sc["ensure_columns"].return_value = list(_VOID_COLS)
    assert material_receipts.backfill_columns(4242, "Job One — Material Receipts") is True
    codes = [c.kwargs.get("error_code") for c in sc["log"].call_args_list]
    assert codes.count("material_receipts_columns_backfilled") == 1

    sc["log"].reset_mock()
    sc["ensure_columns"].return_value = []
    assert material_receipts.backfill_columns(4242, "Job One — Material Receipts") is True
    assert not sc["log"].call_args_list  # steady state: no WARN every cycle for every job


def test_backfill_failure_warns_once_and_reports_false(sc):
    # P3: False tells the pass to skip this job's upserts — every one would KeyError on an absent
    # column — so this WARN is the job's ONE signal, not the first of one ERROR per event.
    sc["ensure_columns"].side_effect = material_receipts.smartsheet_client.SmartsheetError("boom")
    assert material_receipts.backfill_columns(4242, "Job One — Material Receipts") is False
    codes = [c.kwargs.get("error_code") for c in sc["log"].call_args_list]
    assert codes == ["material_receipts_column_backfill_failed"]


@pytest.mark.parametrize("exc_name", [
    "SmartsheetPermissionError", "SmartsheetNotFoundError", "SmartsheetValidationError",
])
def test_backfill_raises_the_permanent_refusals_for_the_caller_to_ticket(sc, exc_name):
    # Retrying these every cycle behind a WARN would leave the job unmirrored for good with no
    # ticket — they propagate, and the pass routes them to the Review Queue.
    exc_type = getattr(material_receipts.smartsheet_client, exc_name)
    sc["ensure_columns"].side_effect = exc_type("refused")
    with pytest.raises(exc_type):
        material_receipts.backfill_columns(4242, "Job One — Material Receipts")
    codes = [c.kwargs.get("error_code") for c in sc["log"].call_args_list]
    assert "material_receipts_column_backfill_failed" not in codes


def test_backfill_reports_false_when_a_column_is_still_absent_after_it(sc):
    # The add "succeeded" but the post-condition does not hold — the same outcome for the upserts
    # (every write would KeyError), so the same one WARN and the same skip.
    sc["ensure_columns"].return_value = list(_VOID_COLS)
    sc["missing_columns"].return_value = ["Entry Status"]
    assert material_receipts.backfill_columns(4242, "Job One — Material Receipts") is False
    sc["missing_columns"].assert_called_once_with(
        4242, [c["title"] for c in material_receipts.BACKFILL_COLUMNS]
    )
    failed = [
        c for c in sc["log"].call_args_list
        if c.kwargs.get("error_code") == "material_receipts_column_backfill_failed"
    ]
    assert len(failed) == 1 and "Entry Status" in failed[0].args[2]


def test_backfill_post_condition_read_failure_reports_false(sc):
    sc["missing_columns"].side_effect = material_receipts.smartsheet_client.SmartsheetError("boom")
    assert material_receipts.backfill_columns(4242, "Job One — Material Receipts") is False
    assert any(
        c.kwargs.get("error_code") == "material_receipts_column_backfill_failed"
        for c in sc["log"].call_args_list
    )


def test_backfill_through_the_real_ensure_columns_is_one_add_at_one_shared_index(mocker):
    # Drives the REAL ensure_columns against a pre-void sheet (the 14 PR4 columns), faking only the
    # SDK client: the six must reach the API as ONE add_columns call whose bodies all carry the SAME
    # index — the errorCode 1135 shape — in creation order.
    pre_void = {
        c["title"]: n for n, c in enumerate(material_receipts.MATERIAL_RECEIPTS_COLUMNS[:-6], 1)
    }
    post_void = {c["title"]: n for n, c in enumerate(material_receipts.MATERIAL_RECEIPTS_COLUMNS, 1)}
    # Pre-add check, pre-add re-read (the duplicate-title guard), then the post-condition read.
    mocker.patch.object(
        material_receipts.smartsheet_client, "_column_map",
        side_effect=[pre_void, pre_void, post_void],
    )
    mocker.patch.object(material_receipts.smartsheet_client, "invalidate_column_cache")
    client = MagicMock()
    mocker.patch.object(material_receipts.smartsheet_client, "get_client", return_value=client)
    mocker.patch.object(material_receipts.error_log, "log")

    assert material_receipts.backfill_columns(4242, "Job One — Material Receipts") is True

    client.Sheets.add_columns.assert_called_once()
    bodies = client.Sheets.add_columns.call_args.args[1]
    assert tuple(b.title for b in bodies) == _VOID_COLS
    assert {b.index for b in bodies} == {len(pre_void)}


def test_upsert_new_active_row_carries_active_and_blank_void_cells(sc):
    material_receipts.upsert_receipt_row(9001, **_t_kwargs("evt-t1"))
    cells = sc["add_rows"].call_args.args[1][0]
    assert cells[material_receipts.COL_ENTRY_STATUS] == "Active"
    for col in _VOID_COLS[1:]:
        assert cells[col] == ""
    assert "_formats" not in cells  # a counting mark keeps the sheet's ordinary formatting


def test_upsert_voids_the_existing_row_in_place_never_deleted(sc):
    # (C) The mark was first mirrored Active; the route now reports it voided → the SAME row is
    # updated in place. No add (a second row would read as another delivery), no delete (a void is
    # annotated, not erased — the signed-for history stays).
    sc["get_rows"].return_value = [_t_row()]
    assert material_receipts.upsert_receipt_row(9001, **_voided_kwargs()) == 888
    sc["add_rows"].assert_not_called()
    sc["delete_rows"].assert_not_called()
    sc["update_rows"].assert_called_once()
    upd = sc["update_rows"].call_args.args[1][0]
    assert upd["_row_id"] == 888
    assert upd[material_receipts.COL_ENTRY_STATUS] == "Voided"
    assert upd[material_receipts.COL_VOIDED_BY] == "Test Crew Lead"
    assert upd[material_receipts.COL_VOIDED_AT] == "2026-10-21 14:05"
    assert upd[material_receipts.COL_VOID_REASON] == "Counted twice"
    # The event's own fields ride along unchanged — the void annotates, it does not rewrite history.
    assert upd[material_receipts.COL_KIND] == "Partial" and upd[material_receipts.COL_QTY] == "40"


def test_voided_row_is_grayed_whole_row_with_the_hours_log_superseded_format(sc):
    sc["get_rows"].return_value = [_t_row()]
    material_receipts.upsert_receipt_row(9001, **_voided_kwargs())
    upd = sc["update_rows"].call_args.args[1][0]
    fmts = upd["_formats"]
    # The hours_log Superseded precedent — the SAME descriptor, reused rather than re-typed.
    assert material_receipts.FMT_VOIDED == material_receipts.hours_log.STATUS_SUPERSEDED_FMT
    # Every written cell is grayed, the key included, so the whole row reads as struck.
    written = {k for k in upd if not k.startswith("_")}
    assert material_receipts.COL_EVENT_UUID in written
    assert set(fmts) == written
    assert fmts[material_receipts.COL_ENTRY_STATUS] == material_receipts.FMT_VOIDED
    # The DATE cell keeps its date display format under the gray (descriptor field 16).
    date_fmt = fmts[material_receipts.COL_EVENT_DATE].split(",")
    assert date_fmt[9] == "18" and date_fmt[16] == material_receipts.FMT_DATE.split(",")[16]


def test_first_seen_voided_mark_is_appended_already_grayed(sc):
    # A mark voided before the mirror ever saw it lands as ONE row, already annotated.
    material_receipts.upsert_receipt_row(9001, **_voided_kwargs())
    cells = sc["add_rows"].call_args.args[1][0]
    assert cells[material_receipts.COL_ENTRY_STATUS] == "Voided"
    assert cells["_formats"][material_receipts.COL_EVENT_UUID] == material_receipts.FMT_VOIDED


def test_voided_row_steady_state_is_a_noop(sc):
    # Once annotated, an identical re-projection writes nothing (format included — it rides the
    # same write as the annotation, so there is no separate format state to re-assert).
    sc["get_rows"].return_value = [_t_voided_row()]
    material_receipts.upsert_receipt_row(9001, **_voided_kwargs())
    sc["update_rows"].assert_not_called()
    sc["add_rows"].assert_not_called()


def test_upsert_rewrites_a_voided_row_when_its_correction_links_it(sc):
    # (D) The original was voided cycles ago; the correction lands later and the route now names it
    # in `superseded_by_uuid` — change-only must see that and re-write the row.
    sc["get_rows"].return_value = [_t_voided_row()]
    material_receipts.upsert_receipt_row(9001, **_voided_kwargs(superseded_by="evt-t2"))
    upd = sc["update_rows"].call_args.args[1][0]
    assert upd["_row_id"] == 888 and upd[material_receipts.COL_SUPERSEDED_BY] == "evt-t2"
    assert upd["_formats"]  # still voided → still gray on the re-write

    # ...and the correction itself carries the back-link on its own row.
    material_receipts.upsert_receipt_row(
        9001, **_t_kwargs("evt-t2", qty="20", supersedes="evt-t1")
    )
    cells = sc["add_rows"].call_args.args[1][0]
    assert cells[material_receipts.COL_SUPERSEDES] == "evt-t1"
    assert cells[material_receipts.COL_ENTRY_STATUS] == "Active"


def test_a_backfilled_legacy_row_is_rewritten_once_to_active(sc):
    # A row mirrored before 0080 has blank void cells after the back-fill. It is re-written ONCE to
    # carry Entry Status=Active (a one-time migration write per row), then is steady again.
    legacy = _t_row()
    for col in _VOID_COLS:
        legacy.pop(col)
    sc["get_rows"].return_value = [legacy]
    index = material_receipts.read_receipt_index(9001)
    material_receipts.upsert_receipt_row(9001, **_t_kwargs("evt-t1"), index=index)
    upd = sc["update_rows"].call_args.args[1][0]
    assert upd[material_receipts.COL_ENTRY_STATUS] == "Active" and "_formats" not in upd
    material_receipts.upsert_receipt_row(9001, **_t_kwargs("evt-t1"), index=index)
    assert sc["update_rows"].call_count == 1


def test_a_rejected_gray_format_never_blocks_the_void_annotation(sc):
    # The gray is cosmetic; the annotation is the record. A format the API rejects (HTTP 400) WARNs
    # and the annotation is written plain, in the same upsert.
    sc["get_rows"].return_value = [_t_row()]
    sc["update_rows"].side_effect = [
        material_receipts.smartsheet_client.SmartsheetValidationError("HTTP 400 format"), None,
    ]
    assert material_receipts.upsert_receipt_row(9001, **_voided_kwargs()) == 888
    first, second = (c.args[1][0] for c in sc["update_rows"].call_args_list)
    assert "_formats" in first and "_formats" not in second
    assert second[material_receipts.COL_ENTRY_STATUS] == "Voided"
    assert any(
        c.kwargs.get("error_code") == "material_receipts_void_format_failed"
        for c in sc["log"].call_args_list
    )


def test_a_plain_write_failure_still_raises_after_the_format_fallback(sc):
    # The fallback is for the FORMAT only: if the plain write is rejected too, the cause is the data
    # and it must reach the caller's per-event fence (→ Review Queue), never be swallowed here.
    sc["get_rows"].return_value = [_t_row()]
    sc["update_rows"].side_effect = material_receipts.smartsheet_client.SmartsheetValidationError("x")
    with pytest.raises(material_receipts.smartsheet_client.SmartsheetValidationError):
        material_receipts.upsert_receipt_row(9001, **_voided_kwargs())
    assert sc["update_rows"].call_count == 2


def test_an_active_row_write_failure_is_not_retried(sc):
    # No format → nothing to fall back from: a counting row's rejected write raises at once.
    sc["get_rows"].return_value = [_t_row()]
    sc["update_rows"].side_effect = material_receipts.smartsheet_client.SmartsheetValidationError("x")
    with pytest.raises(material_receipts.smartsheet_client.SmartsheetValidationError):
        material_receipts.upsert_receipt_row(9001, **_t_kwargs("evt-t1", line_qty_received="120"))
    assert sc["update_rows"].call_count == 1


# ---- find_receipt_row ----------------------------------------------------


def test_find_receipt_row_matches_by_uuid(sc):
    sc["get_rows"].return_value = [{"_row_id": 5, material_receipts.COL_EVENT_UUID: "evt-10"}]
    assert material_receipts.find_receipt_row(9001, "evt-10") == {
        "_row_id": 5, material_receipts.COL_EVENT_UUID: "evt-10"
    }
    assert material_receipts.find_receipt_row(9001, "evt-99") is None
    assert material_receipts.find_receipt_row(9001, "") is None


# ---- read-once index (D10: one full-sheet read per pass, not one per receipt) ----
#
# Before D10 every upsert re-read the WHOLE ledger to find its row — ~138 receipts meant ~138 full
# reads per 90 s cycle, on the fastest-growing per-job tracker. The pass now reads the sheet ONCE.


def _t_kwargs(event_uuid: str, **over: Any) -> dict[str, Any]:
    """Synthetic upsert kwargs (no real job / vendor / person names — the repo is public)."""
    return _upsert_kwargs(
        event_uuid=event_uuid, material="Test panel", part_number="TP-0001", line_uuid="u-t1",
        bol="BOL-T0001", note="", received_by="Test Crew Lead", **over,
    )


def test_read_receipt_index_is_one_read_and_first_match_wins(sc):
    sc["get_rows"].return_value = [
        {"_row_id": 1, material_receipts.COL_EVENT_UUID: "evt-t1"},
        {"_row_id": 2, material_receipts.COL_EVENT_UUID: "evt-t1"},  # duplicate — first hit, as before
    ]
    index = material_receipts.read_receipt_index(9001)
    assert material_receipts.find_receipt_row(9001, "evt-t1", index=index)["_row_id"] == 1
    assert material_receipts.find_receipt_row(9001, "evt-t9", index=index) is None
    assert sc["get_rows"].call_count == 1


def test_upserts_through_one_index_read_the_ledger_once(sc):
    sc["get_rows"].return_value = [_existing_row(**{material_receipts.COL_EVENT_UUID: "evt-t0"})]
    index = material_receipts.read_receipt_index(9001)
    for n in range(5):
        material_receipts.upsert_receipt_row(9001, **_t_kwargs(f"evt-t{n}"), index=index)
    assert sc["get_rows"].call_count == 1


def test_same_pass_repeat_event_updates_never_double_appends(sc):
    sc["get_rows"].return_value = []
    index = material_receipts.read_receipt_index(9001)
    assert material_receipts.upsert_receipt_row(
        9001, **_t_kwargs("evt-t1", line_qty_received="40"), index=index
    ) == 555
    # The same event again with its DERIVED rollup moved: an update of the appended row 555, never a
    # second append — a duplicate ledger row would be a delivery counted twice, kept forever.
    assert material_receipts.upsert_receipt_row(
        9001, **_t_kwargs("evt-t1", line_qty_received="120"), index=index
    ) == 555
    sc["add_rows"].assert_called_once()
    upd = sc["update_rows"].call_args.args[1][0]
    assert upd["_row_id"] == 555 and upd[material_receipts.COL_LINE_QTY_RECEIVED] == "120"
    material_receipts.upsert_receipt_row(
        9001, **_t_kwargs("evt-t1", line_qty_received="120"), index=index
    )
    assert sc["update_rows"].call_count == 1  # the index tracked the update → identical is a no-op
    assert sc["get_rows"].call_count == 1


def test_add_with_unknown_outcome_is_not_re_added_in_the_same_pass(sc):
    sc["get_rows"].return_value = []
    sc["add_rows"].side_effect = material_receipts.smartsheet_client.SmartsheetError("timed out")
    index = material_receipts.read_receipt_index(9001)
    with pytest.raises(material_receipts.smartsheet_client.SmartsheetError, match="timed out"):
        material_receipts.upsert_receipt_row(9001, **_t_kwargs("evt-t1"), index=index)
    sc["add_rows"].side_effect = None
    with pytest.raises(material_receipts.smartsheet_client.SmartsheetError, match="UNKNOWN"):
        material_receipts.upsert_receipt_row(9001, **_t_kwargs("evt-t1"), index=index)
    sc["add_rows"].assert_called_once()


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


def test_read_row_cap_threshold_is_the_its_config_value(sc):
    assert material_receipts.read_row_cap_threshold() == 15000
    sc["get_setting"].assert_called_once_with(
        material_receipts.CFG_ROW_CAP_WARN, workstream="progress_reports"
    )


def test_read_row_cap_threshold_falls_back_loudly_never_raises(sc):
    # Read ONCE per pass for every job, so it must not raise into the pass: a failure WARNs (the
    # same code the per-job check uses) and the conservative default keeps the tripwire armed.
    sc["get_setting"].side_effect = RuntimeError("smartsheet down")
    assert material_receipts.read_row_cap_threshold() == material_receipts.DEFAULT_ROW_CAP_WARN
    assert [c.kwargs.get("error_code") for c in sc["log"].call_args_list] == [
        "material_receipts_row_cap_check_failed"
    ]


def test_row_cap_with_a_pass_resolved_threshold_reads_no_config(sc):
    # The pass hands over the threshold it read once — a per-job ITS_Config read was a full-sheet
    # read of ITS_Config for every job, every cycle.
    material_receipts.check_row_cap(
        9001, "Job One — Material Receipts", row_count=60, threshold=50
    )
    sc["get_setting"].assert_not_called()
    sc["review"].assert_called_once()
    assert sc["review"].call_args.kwargs["payload"]["threshold"] == 50


# ---- P4: the voided gray reaches the SDK Cell objects ---------------------
#
# Every other gray test stops at the `_formats` dict handed to a MOCKED add_rows / update_rows, so
# none proves the descriptor survives `_resolve_cells` into the Cell the API receives. This drives
# the REAL add_rows / update_rows / _resolve_cells, faking only the SDK client.


def _sdk_receipts_sheet(rows: list[dict[str, Any]]) -> tuple[SimpleNamespace, dict[int, str]]:
    columns = [
        SimpleNamespace(id=100 + n, title=c["title"])
        for n, c in enumerate(material_receipts.MATERIAL_RECEIPTS_COLUMNS)
    ]
    title_by_id = {c.id: c.title for c in columns}
    id_by_title = {c.title: c.id for c in columns}
    sdk_rows = [
        SimpleNamespace(
            id=r["_row_id"],
            cells=[
                SimpleNamespace(column_id=id_by_title[k], value=v)
                for k, v in r.items() if not k.startswith("_")
            ],
        )
        for r in rows
    ]
    return SimpleNamespace(columns=columns, rows=sdk_rows), title_by_id


def _formats_by_title(sdk_row: Any, title_by_id: dict[int, str]) -> dict[str, Any]:
    return {title_by_id[c.column_id]: getattr(c, "format", None) for c in sdk_row.cells}


def test_voided_row_formats_reach_the_sdk_cells_through_the_real_resolve_cells(mocker, monkeypatch):
    sc_mod = material_receipts.smartsheet_client
    monkeypatch.setattr(sc_mod, "_column_maps", {})  # a cold, test-local cache
    client = MagicMock()
    mocker.patch.object(sc_mod, "get_client", return_value=client)
    client.Sheets.add_rows.return_value = SimpleNamespace(result=[SimpleNamespace(id=555)])

    # MISS → add_rows: a mark first seen already voided lands grayed.
    sheet, title_by_id = _sdk_receipts_sheet([])
    client.Sheets.get_sheet.return_value = sheet
    material_receipts.upsert_receipt_row(9001, **_voided_kwargs())
    [added] = client.Sheets.add_rows.call_args.args[1]
    fmts = _formats_by_title(added, title_by_id)
    assert fmts.pop(material_receipts.COL_EVENT_DATE) == material_receipts.FMT_VOIDED_DATE
    assert set(fmts.values()) == {material_receipts.FMT_VOIDED}
    assert material_receipts.COL_EVENT_UUID in fmts  # the key cell is grayed too

    # HIT → update_rows: an Active row the route now reports voided is re-written grayed, whole-row.
    sc_mod.invalidate_column_cache()
    sheet, title_by_id = _sdk_receipts_sheet([_t_row()])
    client.Sheets.get_sheet.return_value = sheet
    material_receipts.upsert_receipt_row(9001, **_voided_kwargs())
    [updated] = client.Sheets.update_rows.call_args.args[1]
    assert updated.id == 888
    fmts = _formats_by_title(updated, title_by_id)
    assert fmts.pop(material_receipts.COL_EVENT_DATE) == material_receipts.FMT_VOIDED_DATE
    assert set(fmts.values()) == {material_receipts.FMT_VOIDED}
    assert set(fmts) | {material_receipts.COL_EVENT_DATE} == {
        c["title"] for c in material_receipts.MATERIAL_RECEIPTS_COLUMNS
    }
