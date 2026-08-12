"""schedule_parse — semantic extraction over reconstructed rows (ADR-0006).

Each test pins a rule EARNED against the real corpus (named in the module's
docstring): %-before-dates is a task-name suffix, digit-debris after dates is
dropped unless a Predecessors column exists, shredded dates are kept-and-flagged,
the chart-region % harvest merges with a conflict flag, grid-view sub-section
prefixes become phase context, and milestones/deliveries are proposed — never
silently invented (§4)."""
from __future__ import annotations

import json
from pathlib import Path

from field_ops import schedule_geometry as geo
from field_ops import schedule_parse as sp

FIXTURES = Path(__file__).parent / "fixtures" / "schedule_corpus"


def _word(text: str, c0: float, r: float = 0.5, conf: float = 1.0) -> geo.Word:
    return geo.Word(text=text, r0=r, r1=r + 0.012, c0=c0, c1=c0 + 0.05, conf=conf)


def _row(*items: tuple[str, float], gantt_percent: str | None = None, conf: float = 1.0) -> geo.RowWords:
    words = [_word(t, c, conf=conf) for t, c in items]
    return geo.RowWords(
        words=words,
        row_band=(0.5, 0.512),
        indent=min(wd.c0 for wd in words),
        gantt_percent=gantt_percent,
        min_conf=min(wd.conf for wd in words),
    )


def _page(rows: list[geo.RowWords], *, header_labels: list[str] | None = None,
          header_bands: list[tuple[float, float]] | None = None,
          pre_header: list[str] | None = None) -> geo.PageRows:
    return geo.PageRows(
        header_labels=header_labels or ["task name", "start date", "completion date"],
        header_bands=header_bands or [(0.05, 0.13), (0.30, 0.37), (0.40, 0.47)],
        rows=rows,
        pre_header_texts=pre_header or [],
        orientation="upright",
        notes=[],
    )


def _cells(row: sp.ParsedScheduleRow) -> dict[str, str]:
    return dict(zip(sp.CANONICAL_COLUMNS, row.cells, strict=True))


def test_basic_task_row():
    doc = sp.parse_schedule([_page([
        _row(("2", 0.02), ("Pile Installation", 0.07), ("01/12/26 02/06/26", 0.30)),
    ])])
    c = _cells(doc.rows[0])
    assert doc.rows[0].kind == "data"
    assert c["row_number"] == "2"
    assert c["task_name"] == "Pile Installation"
    assert c["start_date"] == "2026-01-12"
    assert c["finish_date"] == "2026-02-06"
    assert doc.rows[0].flags == []


def test_percent_before_dates_is_part_of_the_task_name():
    """The design-milestone tasks are literally NAMED 'Electrical 30%' — the
    corpus bug that misclassified them as sections with 30% progress."""
    doc = sp.parse_schedule([_page([
        _row(("Electrical 30%", 0.07), ("09/01/25 11/09/25", 0.30)),
    ])])
    c = _cells(doc.rows[0])
    assert doc.rows[0].kind == "data"
    assert c["task_name"] == "Electrical 30%"
    assert c["percent_done"] == ""


def test_percent_after_dates_is_completion():
    doc = sp.parse_schedule([_page([
        _row(("Fencing", 0.07), ("01/12/26 02/06/26 75%", 0.30)),
    ])])
    assert _cells(doc.rows[0])["percent_done"] == "75"


def test_gantt_percent_merges_and_conflicts_flag():
    merged = sp.parse_schedule([_page([
        _row(("Fencing", 0.07), ("01/12/26 02/06/26", 0.30), gantt_percent="50"),
    ])])
    assert _cells(merged.rows[0])["percent_done"] == "50"

    conflict = sp.parse_schedule([_page([
        _row(("Fencing", 0.07), ("01/12/26 02/06/26 75%", 0.30), gantt_percent="50"),
    ])])
    assert _cells(conflict.rows[0])["percent_done"] == "75"
    assert sp.FLAG_PERCENT_CONFLICT in conflict.rows[0].flags


def test_out_of_range_percent_flags_even_from_the_chart_harvest():
    """'195%' — a bar edge fused to a label. The range check runs on the FINAL
    value, after the harvest merge (the corpus bug that let 195 through)."""
    doc = sp.parse_schedule([_page([
        _row(("Deliveries", 0.07), ("12/14/25 08/12/26", 0.30), gantt_percent="195"),
    ])])
    assert sp.FLAG_PERCENT_OUT_OF_RANGE in doc.rows[0].flags


def test_shredded_date_kept_and_flagged():
    """'12125125' — slashes read as ones at confidence 1.0. Kept positionally so
    the human fixes exactly what the OCR saw; never silently dropped or guessed."""
    doc = sp.parse_schedule([_page([
        _row(("CAB Delivery", 0.07), ("12125125 07/23/26", 0.30)),
    ])])
    c = _cells(doc.rows[0])
    assert c["start_date"] == "12125125"
    assert c["finish_date"] == "2026-07-23"
    assert sp.FLAG_UNPARSEABLE_DATE in doc.rows[0].flags


def test_implausible_year_flags():
    doc = sp.parse_schedule([_page([
        _row(("Fencing", 0.07), ("07/01/72 08/01/72", 0.30)),
    ])])
    assert sp.FLAG_IMPLAUSIBLE_YEAR in doc.rows[0].flags


def test_finish_before_start_flags():
    doc = sp.parse_schedule([_page([
        _row(("Fencing", 0.07), ("03/06/26 02/09/26", 0.30)),
    ])])
    assert sp.FLAG_FINISH_BEFORE_START in doc.rows[0].flags


def test_milestone_proposed_from_equal_dates():
    doc = sp.parse_schedule([_page([
        _row(("NTP Construction Milestone", 0.07), ("04/03/26 04/03/26", 0.30)),
        _row(("Fencing", 0.07), ("01/12/26 02/06/26", 0.30)),
    ])])
    assert doc.rows[0].is_milestone is True
    assert doc.rows[1].is_milestone is False


def test_section_carry_marks_deliveries():
    doc = sp.parse_schedule([_page([
        _row(("- Deliveries", 0.05), ("12/14/25 08/12/26", 0.30)),
        _row(("Pile Delivery", 0.07), ("02/03/26 07/17/26", 0.30)),
        _row(("- Civil", 0.05), ("04/04/26 07/07/26", 0.30)),
        _row(("Grubbing and Grading", 0.07), ("04/14/26 07/07/26", 0.30)),
    ])])
    assert doc.rows[0].kind == "section"
    assert doc.rows[1].is_delivery is True
    assert _cells(doc.rows[1])["phase"] == "Deliveries"
    assert doc.rows[3].is_delivery is False
    assert _cells(doc.rows[3])["phase"] == "Civil"


def test_fused_group_glyph_still_marks_a_section():
    doc = sp.parse_schedule([_page([
        _row(("-Mechanical", 0.05), ("07/27/26 10/23/26", 0.30)),
    ])])
    assert doc.rows[0].kind == "section"
    assert _cells(doc.rows[0])["task_name"] == "Mechanical"


def test_digit_debris_after_dates_dropped_without_predecessor_column():
    """'| 0%' reads as '1 0%' — the bare '1' must not become a predecessor in a
    Gantt-view export (no Predecessors header anywhere)."""
    doc = sp.parse_schedule([_page([
        _row(("Site Energizing", 0.07), ("11/16/26 11/16/26 1 0%", 0.30)),
    ])])
    c = _cells(doc.rows[0])
    assert c["predecessors"] == ""
    assert c["percent_done"] == "0"


def test_predecessors_believed_with_the_grid_view_header():
    page = _page(
        [
            _row(("5", 0.02), ("IFC Engineering", 0.14), ("52d", 0.30),
                 ("04/13/26 06/23/26 0%", 0.36), ("2,", 0.60), ("3FS", 0.63), ("-30d", 0.66)),
        ],
        header_labels=["task name", "duration", "start date", "completion date", "predecessors"],
        header_bands=[(0.14, 0.22), (0.30, 0.34), (0.36, 0.42), (0.44, 0.50), (0.60, 0.68)],
    )
    doc = sp.parse_schedule([page])
    c = _cells(doc.rows[0])
    assert c["duration"] == "52d"
    assert c["predecessors"] == "2 3FS -30d"
    assert doc.profile == "grid_export"


def test_grid_view_sub_section_prefix_becomes_phase_context():
    """'Deep Lake _ Deliveries | Pile Delivery' — the Project & Sub-Section column
    is nesting context, never part of the task's own name."""
    page = _page(
        [
            _row(("9", 0.02), ("Deep Lake _ Deliveries", 0.06), ("Pile Delivery", 0.20),
                 ("137d", 0.34), ("01/15/26 07/24/26", 0.40)),
        ],
        header_labels=["project name & sub-section", "task name", "duration", "start date", "completion date"],
        header_bands=[(0.06, 0.16), (0.20, 0.28), (0.34, 0.38), (0.40, 0.46), (0.48, 0.54)],
    )
    doc = sp.parse_schedule([page])
    c = _cells(doc.rows[0])
    assert c["task_name"] == "Pile Delivery"
    assert c["phase"] == "Deliveries"
    assert doc.rows[0].is_delivery is True


def test_low_confidence_flags_as_evidence():
    doc = sp.parse_schedule([_page([
        _row(("Shaky Task", 0.07), ("01/12/26 02/06/26", 0.30), conf=0.3),
    ])])
    assert sp.FLAG_LOW_CONFIDENCE in doc.rows[0].flags


def test_title_and_project_meta():
    doc = sp.parse_schedule([_page(
        [
            _row(("1", 0.02), ("- Coker", 0.05), ("08/31/25 04/28/27", 0.30)),
            _row(("Buyout", 0.07), ("11/10/25 04/23/26", 0.30)),
        ],
        pre_header=["Project Schedule - KSI - Coker 8.5.26"],
    )])
    assert doc.meta["title"] == "Project Schedule - KSI - Coker 8.5.26"
    assert doc.meta["project_name"] == "Coker"
    assert doc.rows[0].kind == "section"


def test_column_map_is_the_canonical_mapping():
    doc = sp.parse_schedule([_page([_row(("T", 0.07), ("01/12/26 02/06/26", 0.30))])])
    mapping = doc.column_map["mapping"]
    assert mapping == {name: i for i, name in enumerate(sp.CANONICAL_COLUMNS)}


def test_captured_fixtures_parse_to_reviewable_documents():
    """Every committed corpus capture parses: a majority of data rows carry a
    name + two ISO dates, and the profile resolves. Exact-cell truth stays with
    the operator-run corpus suite — Vision varies run-to-run, but a CAPTURE is
    frozen, so these bounds are deterministic."""
    payloads = sorted(FIXTURES.glob("*_gantt.json"))
    assert payloads, "corpus fixture captures missing"
    for path in payloads:
        payload = json.loads(path.read_text())
        doc = sp.parse_schedule(geo.reconstruct(payload["pages"]))
        assert doc.profile in ("gantt_export", "grid_export"), path.name
        data_rows = [r for r in doc.rows if r.kind == "data"]
        assert len(data_rows) >= 10, path.name
        named = [r for r in data_rows if _cells(r)["task_name"]]
        dated = [
            r for r in data_rows
            if len(_cells(r)["start_date"]) == 10 and len(_cells(r)["finish_date"]) == 10
        ]
        assert len(named) >= int(len(data_rows) * 0.8), path.name
        # Measured floor across the frozen captures is 63% (the Bonacci pair —
        # shredded-date flags the validate screen exists for); 0.6 pins against
        # regression without demanding what the OCR never delivered.
        assert len(dated) >= int(len(data_rows) * 0.6), path.name
