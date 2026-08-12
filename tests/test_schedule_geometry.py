"""schedule_geometry — row reconstruction over OCR word payloads (ADR-0006).

Synthetic payloads pin the corpus-earned rules deterministically (Vision output is
not perfectly deterministic run-to-run, so CI never invokes OCR — the captured
fixture payloads + these synthetics are the contract). The operator-run corpus
suite (tests/test_schedule_ocr_corpus.py) covers the live end-to-end.
"""
from __future__ import annotations

import json
from pathlib import Path

from field_ops import schedule_geometry as geo

FIXTURES = Path(__file__).parent / "fixtures" / "schedule_corpus"


def w(text: str, x: float, y: float, ww: float, hh: float, conf: float = 1.0) -> dict:
    return {"text": text, "conf": conf, "x": x, "y": y, "w": ww, "h": hh}


def _upright_page() -> list[dict]:
    """A miniature upright Gantt-view page in Vision coordinates (origin
    BOTTOM-left): title above the header, a wrapped 'Date' header line, three data
    rows, a chart-region month band and a per-row % label."""
    return [
        w("Project Schedule - KSI - Testville 1.1.26", 0.05, 0.95, 0.3, 0.02),
        # Header row (r ≈ 0.90): known labels + chart quarter labels that must NOT
        # extend the table region.
        w("Task Name", 0.05, 0.90, 0.08, 0.015),
        w("Start Date", 0.30, 0.90, 0.07, 0.015),
        w("Completion", 0.40, 0.90, 0.07, 0.015),
        w("Q3", 0.55, 0.90, 0.03, 0.015),
        w("Q4", 0.70, 0.90, 0.03, 0.015),
        # The wrapped second header line — must be absorbed, never a data row.
        w("Date", 0.40, 0.88, 0.04, 0.012),
        w("Jul", 0.52, 0.88, 0.02, 0.012),
        w("Aug", 0.60, 0.88, 0.02, 0.012),
        # Project row.
        w("1", 0.02, 0.84, 0.01, 0.012),
        w("- Testville", 0.05, 0.84, 0.08, 0.012),
        w("01/05/26 06/30/26", 0.30, 0.84, 0.15, 0.012),
        # Data row with its Gantt bar label beyond the table region.
        w("2", 0.02, 0.80, 0.01, 0.012),
        w("Pile Delivery", 0.07, 0.80, 0.09, 0.012),
        w("01/12/26 02/06/26", 0.30, 0.80, 0.15, 0.012),
        w("75%", 0.60, 0.80, 0.03, 0.012),
        # A pure chart row (bar dates floating mid-chart) — not a table row.
        w("02/06/26", 0.62, 0.76, 0.05, 0.012),
        # Data row whose words merged into one observation.
        w("3 Fencing 02/09/26 03/06/26", 0.02, 0.72, 0.4, 0.012),
    ]


def test_upright_reconstruction_end_to_end():
    page = geo.reconstruct_page(geo.words_from_ocr(_upright_page()), orientation="upright")
    assert page.header_labels == ["task name", "start date", "completion date"]
    assert page.pre_header_texts == ["Project Schedule - KSI - Testville 1.1.26"]
    # The wrap line and the pure chart row are gone; three table rows remain.
    texts = [" ".join(x.text for x in r.words) for r in page.rows]
    assert texts == [
        "1 - Testville 01/05/26 06/30/26",
        "2 Pile Delivery 01/12/26 02/06/26",
        "3 Fencing 02/09/26 03/06/26",
    ]
    assert page.rows[1].gantt_percent == "75"
    assert page.rows[0].gantt_percent is None


def test_quarter_labels_do_not_extend_the_table_region():
    page = geo.reconstruct_page(geo.words_from_ocr(_upright_page()), orientation="upright")
    # Table ends just past 'Completion' (c1=0.47) + margin — far left of Q3 at 0.55.
    assert all(x.c0 < 0.50 for r in page.rows for x in r.words)


def test_orientation_vote_rotated_vs_upright():
    rotated = [w("Some Long Task Name", 0.1, 0.2, 0.01, 0.2) for _ in range(5)]
    upright = [w("Some Long Task Name", 0.1, 0.2, 0.2, 0.01) for _ in range(5)]
    assert geo.detect_orientation(rotated) == "rotated"
    assert geo.detect_orientation(upright) == "upright"
    # Single glyphs abstain — a page of dots must not vote.
    dots = [w("=", 0.1, 0.2, 0.01, 0.01) for _ in range(50)]
    assert geo.detect_orientation(dots + upright) == "upright"


def test_headerless_page_keeps_all_words_and_notes_it():
    words = [
        w("Fencing", 0.05, 0.80, 0.06, 0.012),
        w("02/09/26 03/06/26", 0.30, 0.80, 0.15, 0.012),
    ]
    page = geo.reconstruct_page(geo.words_from_ocr(words), orientation="upright")
    assert "no_header_row" in page.notes
    assert len(page.rows) == 1
    assert len(page.rows[0].words) == 2


def test_min_conf_rides_through_as_evidence():
    words = [
        w("Task Name", 0.05, 0.90, 0.08, 0.015),
        w("Start Date", 0.30, 0.90, 0.07, 0.015),
        w("Shaky Row", 0.05, 0.80, 0.08, 0.012, conf=0.4),
        w("01/12/26", 0.30, 0.80, 0.06, 0.012, conf=1.0),
    ]
    page = geo.reconstruct_page(geo.words_from_ocr(words), orientation="upright")
    assert page.rows[0].min_conf == 0.4


def test_words_from_pdfplumber_adapts_and_normalizes():
    words = geo.words_from_pdfplumber(
        [
            {"text": "Task", "x0": 79.2, "x1": 158.4, "top": 61.2, "bottom": 73.44},
        ],
        page_width=792,
        page_height=612,
    )
    assert len(words) == 1
    assert abs(words[0].c0 - 0.1) < 1e-6
    assert abs(words[0].c1 - 0.2) < 1e-6
    assert abs(words[0].r0 - 0.1) < 1e-6
    assert abs(words[0].r1 - 0.12) < 1e-3


def test_row_cap_bounds_a_hostile_word_bag():
    words = [
        w(f"Row {i}", 0.05, 0.001 * i, 0.05, 0.0004) for i in range(2000)
    ]
    page = geo.reconstruct_page(geo.words_from_ocr(words), orientation="upright")
    assert len(page.rows) <= geo.MAX_ROWS_PER_PAGE


def test_captured_fixture_payloads_reconstruct():
    """Every committed corpus capture reconstructs: rows come back, and a header
    was found on page 1 (exact cells are the parse suite's business)."""
    payloads = sorted(FIXTURES.glob("*_gantt.json"))
    assert payloads, "corpus fixture captures missing"
    for path in payloads:
        payload = json.loads(path.read_text())
        pages = geo.reconstruct(payload["pages"])
        assert pages, path.name
        assert pages[0].header_labels, f"{path.name}: no header on page 1"
        assert sum(len(p.rows) for p in pages) >= 10, path.name
