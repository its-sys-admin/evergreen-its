"""Corpus tests for the materials-manifest parser (`field_ops/manifest_parse.py`).

Every grid below reproduces a STRUCTURE observed in a real Evergreen manifest, transcribed as a
literal rather than committed as a fixture file: the source documents are customer data that lives
outside the repo, and the parser's input is a cell grid anyway, so a literal is both the honest
boundary and the more legible test.

Each case names the trap it guards. The row counts and column indices were taken from an actual
parse of the ten real documents (see `scripts/eval_manifest_parse.py`, operator-run).

Run with: pytest -q tests/test_manifest_parse.py
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from field_ops import manifest_parse as mp

# ── Grids transcribed from the real documents ────────────────────────────────────────────────

# Customer BOM, PDF (Bradley / Brimfield). The header is on PAGE 1 ONLY; pages 2-3 are headerless
# data continuations. Extracted as one grid per page-table.
CUSTOMER_BOM_P1: list[list[Any]] = [
    ["PART NUMBER", "DESCRIPTION", "GROUPING", "QTY", "UoM", "WEIGHT (KG)", "WEIGHT TOTALS"],
    ["7000727", "Linear Actuator, PA 16", "ACTUATOR", "568", "EACH", "13.50", "7668"],
    ["7000120", "Cable, Actuator, 4x1.5", "CABLE", "33385", "METERS", "0.30", "10016"],
]
CUSTOMER_BOM_P2: list[list[Any]] = [  # NO header — the trap
    ["7000006", "Hex Flange Nut, 1/4-20", "HARDWARE", "54092", "EACH", "0.01", "541"],
    ["7000007", "Hex Flange Bolt", "HARDWARE", "54092", "EACH", "0.03", "1623"],
    # The observed bleed: a truncated description pushed a stray "3" into GROUPING. Every cell is
    # populated, which is exactly why a cell-count "ragged" heuristic would never have caught it.
    ["7000650", "Conical Washer, 17mm ID, DIN6796, Magni per ASTM F28", "3 HARDWARE", "6990", "EACH", "0.10", "699"],
]

# Customer BOM, XLSX (Roxbury). Two title rows, then a WHOLLY EMPTY row, then the header on row 4.
# Part numbers and quantities arrive as ints from openpyxl.
ROXBURY_XLSX: list[list[Any]] = [
    ["ESS-CPG Roxbury Project", None, None, None, None],
    ["14777 Roxbury Rd, Glenelg, MD 21737", None, None, None, None],
    [None, None, None, None, None],
    ["PART NUMBER", "DESCRIPTION", "UNIT OF MEASURE", "BOM CATEGORY", "TOTAL QTY"],
    [7006955, "ACTUATOR, 18MM END ROD, 240VAC", "EACH", "ACTUATOR", 214],
    [7000120, "Cable, Actuator, 4x1.5mm2", "METERS", "CABLE", 11100],
]

# DELTA BOM, PDF (Bonacci). The metadata block is its OWN table and carries the PRODUCT CODE row
# that labels the otherwise-identical QUANTITY columns. Header names repeat, so the map must be
# positional. Identity: sum(QUANTITY…) + OVERAGE == REV 2.
DELTA_META: list[list[Any]] = [
    ["CLIENT", "EVERGREEN ENERGY"],
    ["PROJECT NAME", "BONACCI 1"],
    ["PROJECT NUMBER", "25-35099"],
]
DELTA_PRODUCTS: list[list[Any]] = [
    ["PRODUCT CODE", "1P-PER-108M-15F-G90", "1P-PER-54M-9F-G90", "NETWORK CONTROLLER"],
    ["SITE QUANTITY", "65", "28", "1"],
]
DELTA_LINES: list[list[Any]] = [
    ["RELEASE", "PART NUMBER", "DESCRIPTION", "QUANTITY", "QUANTITY", "QUANTITY", "OVERAGE", "REV 2", "REV 1", "DELTA"],
    ["1", "119624", "GROUND SCREW", "0", "1", "2", "0", "3", "3", "0"],
    ["1", "805199", "1P DRIVEN PILE W6x9", "975", "252", "5", "12", "1244", "1244", "0"],
]

# Shipping log, XLSX (Deep Lake). Continuation rows blank the identity columns entirely, and the
# sheet over-declares its width — trailing Nones stand in for the 80 phantom columns.
DEEPLAKE: list[list[Any]] = [
    ["ProjectID", "Product", "Project Name", "Job Number", "Part Number", "Part Description",
     "Required Date", "Required Qty", "Issued Qty", "Ship date", "Delivery Day ", "LD#", None, None],
    [26838872, "TerraTrak", "Deep Lake-IL", "4005217-5-1", "805275", "1P DRIVEN PILE W8X10",
     dt.datetime(2026, 6, 26), 1255, 219, dt.datetime(2026, 6, 26), dt.datetime(2026, 6, 29), "LD0867264", None, None],
    [None, None, None, None, None, None, None, None, 294,
     dt.datetime(2026, 6, 26), dt.datetime(2026, 6, 29), "LD0867268", None, None],
    # A part with NO shipment at all — 13 of these exist in the real log. Must NOT be flagged.
    [26838872, "TerraTrak", "Deep Lake-IL", "4005217-5-10", "805763", "HAT CHANNEL",
     dt.datetime(2026, 8, 7), 9055, None, None, None, None, None, None],
]

# Shipping log, XLSX (Kiwi). Carries REQUIREMENT dates ("Job Ship By" / "Last Need By") ALONGSIDE
# the actual ship/delivery dates — the pair that must not be confused.
KIWI: list[list[Any]] = [
    ["Project Name", "JobNum", "Job Ship By", "Last Need By", "Part Num", "Description",
     "Job Req'd Qty", "Shipped Qty", "Ship Date", "Delivery Date", "BOL"],
    ["Kiwi-IL", "4005078-5-1", dt.datetime(2026, 7, 17), dt.datetime(2026, 7, 24), "805271-SHIP",
     "1P DRIVEN PILE", 503, 163, dt.datetime(2026, 7, 17), dt.datetime(2026, 7, 20), "LD0872247"],
]


def _parse(*grids, product_codes=None):
    labelled = [(f"g{i}", g) for i, g in enumerate(grids)]
    return mp.parse_manifest(labelled, product_codes=product_codes)


# ── normalize_cell ────────────────────────────────────────────────────────────────────────────

def test_int_part_numbers_do_not_gain_a_decimal_point() -> None:
    """openpyxl hands back 7006955 as a number; "7006955.0" would fail every downstream match."""
    assert mp.normalize_cell(7006955) == "7006955"
    assert mp.normalize_cell(7006955.0) == "7006955"
    assert mp.normalize_cell(13.5) == "13.5"  # a real fraction survives


def test_midnight_datetimes_render_as_iso_dates() -> None:
    """Both logs store dates as midnight datetimes; the schema stores YYYY-MM-DD."""
    assert mp.normalize_cell(dt.datetime(2026, 6, 26)) == "2026-06-26"
    assert mp.normalize_cell(None) == ""


# ── width / header detection ──────────────────────────────────────────────────────────────────

def test_grid_width_comes_from_content_not_the_sheets_claim() -> None:
    """Deep Lake declares 92 columns and holds 12. Trailing phantom columns must be trimmed."""
    trimmed = mp.trim_width([[mp.normalize_cell(c) for c in r] for r in DEEPLAKE])
    assert all(len(r) == 12 for r in trimmed)


def test_header_is_scanned_for_not_assumed_at_row_one() -> None:
    """Roxbury puts two title rows AND a wholly-empty row above its header."""
    grid = [[mp.normalize_cell(c) for c in r] for r in ROXBURY_XLSX]
    idx, hits = mp.find_header_row(grid)
    assert idx == 3  # 0-based → the 4th row
    assert hits >= mp.MIN_HEADER_HITS


def test_a_grid_with_no_header_reports_none() -> None:
    idx, _hits = mp.find_header_row([["just", "some", "values"], ["1", "2", "3"]])
    assert idx is None


# ── the four document shapes ──────────────────────────────────────────────────────────────────

def test_customer_bom_carries_its_column_map_onto_headerless_pages() -> None:
    """THE Customer-BOM trap: the header is on page 1 only.

    Re-inferring per page would find no header on page 2 and drop it — about 60% of every real
    Customer BOM. All five data rows must survive with the page-1 map.
    """
    r = _parse(CUSTOMER_BOM_P1, CUSTOMER_BOM_P2)
    assert r.profile == mp.PROFILE_CUSTOMER_BOM
    data = [row for row in r.rows if row.kind == mp.KIND_DATA]
    assert len(data) == 5, "page-2 rows were dropped — the column map did not carry"
    assert {row.source for row in data} == {"g0", "g1"}
    assert r.column_map.index_of(mp.PART_NUMBER) == 0
    assert r.column_map.index_of(mp.CATEGORY) == 2


def test_the_bled_cell_row_is_imported_intact_and_unflagged() -> None:
    """The truncated-description row keeps every cell verbatim for the human to fix.

    It is deliberately NOT flagged: every cell is populated, so no completeness heuristic sees it.
    Inventing a flag that misses its own motivating case would be noise.
    """
    r = _parse(CUSTOMER_BOM_P1, CUSTOMER_BOM_P2)
    bled = [row for row in r.rows if row.cells[0] == "7000650"][0]
    assert bled.cells[2] == "3 HARDWARE"  # verbatim, not "cleaned"
    assert bled.flags == []


def test_roxbury_xlsx_parses_from_the_scanned_header() -> None:
    r = _parse(ROXBURY_XLSX)
    assert r.profile == mp.PROFILE_CUSTOMER_BOM
    data = [row for row in r.rows if row.kind == mp.KIND_DATA]
    assert len(data) == 2
    assert data[0].cells[0] == "7006955"
    assert [row.kind for row in r.rows][:2] == [mp.KIND_META, mp.KIND_META]


def test_delta_bom_defaults_to_the_revision_column_and_proves_it() -> None:
    """The evidence-backed default: sum(QUANTITY…) + OVERAGE == REV 2 on every row."""
    codes = mp.product_codes_from_meta([("meta", DELTA_PRODUCTS)])
    r = _parse(DELTA_META, DELTA_PRODUCTS, DELTA_LINES, product_codes=codes)
    assert r.profile == mp.PROFILE_DELTA_BOM
    cmap = r.column_map
    assert cmap.labels[cmap.qty_default].startswith("REV 2"), "REV 2 must be the default quantity"
    agree, checked = mp.delta_arithmetic_check(r.rows, cmap)
    assert (agree, checked) == (2, 2)


def test_delta_quantity_columns_are_labelled_with_their_product_codes() -> None:
    """Otherwise the picker offers "QUANTITY #4" and the admin is guessing."""
    codes = mp.product_codes_from_meta([("meta", DELTA_PRODUCTS)])
    assert codes[:2] == ["1P-PER-108M-15F-G90", "1P-PER-54M-9F-G90"]
    r = _parse(DELTA_META, DELTA_PRODUCTS, DELTA_LINES, product_codes=codes)
    labelled = [v for v in r.column_map.labels.values() if "1P-PER-108M-15F-G90" in v]
    assert labelled, "the bare QUANTITY columns were not labelled with their product codes"


def test_delta_metadata_block_is_harvested_for_display() -> None:
    r = _parse(DELTA_META, DELTA_PRODUCTS, DELTA_LINES)
    assert r.meta.get("CLIENT") == "EVERGREEN ENERGY"
    assert r.meta.get("PROJECT NAME") == "BONACCI 1"


def test_shipping_log_continuation_rows_inherit_their_parents_identity() -> None:
    """A blank-identity row is another LOAD of the row above, not a new part."""
    r = _parse(DEEPLAKE)
    assert r.profile == mp.PROFILE_SHIPPING_LOG
    cont = [row for row in r.rows if row.kind == mp.KIND_CONTINUATION]
    assert len(cont) == 1
    part_col = r.column_map.index_of(mp.PART_NUMBER)
    assert cont[0].cells[part_col] == "805275"  # forward-filled from the parent
    assert cont[0].cells[11] == "LD0867268"  # its OWN load number, untouched


def test_a_part_with_no_shipment_is_normal_not_a_defect() -> None:
    """13 such rows exist in the real Deep Lake log — flagging them would be pure noise."""
    r = _parse(DEEPLAKE)
    no_ship = [row for row in r.rows if row.kind == mp.KIND_DATA and row.cells[4] == "805763"][0]
    assert no_ship.flags == []
    assert no_ship.cells[9] == "" and no_ship.cells[11] == ""


def test_requirement_dates_are_not_mistaken_for_actual_ship_dates() -> None:
    """Kiwi carries both. "Job Ship By" is a deadline; "Ship Date" is what happened.

    Letting the deadline claim ship_date would import a date the goods never moved on.
    """
    r = _parse(KIWI)
    cmap = r.column_map
    assert cmap.index_of(mp.SHIP_DATE) == 8
    assert cmap.index_of(mp.DELIVERY_DATE) == 9
    assert cmap.index_of(mp.REQUIRED_SHIP_DATE) == 2
    assert cmap.index_of(mp.REQUIRED_DATE) == 3
    assert cmap.index_of(mp.BOL) == 10


def test_the_quantity_picker_is_never_empty() -> None:
    """Kiwi's quantity column is "Job Req'd Qty" — it must still be offered as a candidate."""
    r = _parse(KIWI)
    assert r.column_map.qty_default == 6
    assert 6 in r.column_map.qty_candidates


# ── row classification edge cases ─────────────────────────────────────────────────────────────

def test_a_continuation_before_any_parent_is_flagged_not_silently_attached() -> None:
    """Guessing which part an orphan belongs to would invent field data."""
    orphan_first = [DEEPLAKE[0], DEEPLAKE[2], DEEPLAKE[1]]
    r = _parse(orphan_first)
    orphan = [row for row in r.rows if mp.FLAG_ORPHAN_CONTINUATION in row.flags]
    assert len(orphan) == 1


def test_duplicate_part_numbers_are_both_kept() -> None:
    """Universal in the real BOMs. Merging here would silently pick a winner."""
    dup = [CUSTOMER_BOM_P1[0], CUSTOMER_BOM_P1[1], CUSTOMER_BOM_P1[1]]
    r = _parse(dup)
    data = [row for row in r.rows if row.kind == mp.KIND_DATA]
    assert len(data) == 2


def test_an_unparseable_quantity_is_flagged() -> None:
    grid = [CUSTOMER_BOM_P1[0], ["7000999", "Mystery part", "HARDWARE", "n/a", "EACH", "", ""]]
    r = _parse(grid)
    row = [x for x in r.rows if x.kind == mp.KIND_DATA][0]
    assert mp.FLAG_NO_QTY in row.flags


def test_an_unrecognisable_document_says_so_rather_than_guessing() -> None:
    r = _parse([["alpha", "beta"], ["1", "2"]])
    assert r.profile == mp.PROFILE_UNKNOWN
    assert r.column_map.is_empty()
    assert any("no header row recognised" in n for n in r.notes)


def test_row_indices_are_stable_across_grids() -> None:
    """The validate page edits against row_index, so it must be whole-document and 1-based."""
    r = _parse(CUSTOMER_BOM_P1, CUSTOMER_BOM_P2)
    idxs = [row.index for row in r.rows]
    assert idxs == sorted(idxs)
    assert idxs[0] == 1
    assert len(set(idxs)) == len(idxs)
