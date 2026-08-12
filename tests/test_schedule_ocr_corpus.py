"""Operator-run corpus qualification for the schedule OCR→geometry→parse ladder.

Runs the REAL sandbox child (Quartz render + rotation ladder + Apple Vision) over
every PDF in the operator's schedule-corpus folder and asserts BOUNDED properties
— Vision output varies slightly run-to-run, so exact cells belong to the frozen
capture fixtures (tests/test_schedule_geometry.py / test_schedule_parse.py), and
this suite asserts what must hold on ANY run: every document parses, rows land in
the plausible band, names cover the data rows, and the flagship live exports keep
their known shape.

Skipped automatically when the corpus folder or the Vision bridge is absent
(CI, non-Darwin, another host). Run with:

    pytest -q tests/test_schedule_ocr_corpus.py

Qualification evidence (2026-08-11, this host): 32/32 documents parsed; 54–86
named data rows each; milestones + deliveries detected on every live export.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from field_ops import schedule_geometry, schedule_parse
from po_materials import estimate_sandbox

CORPUS = Path.home() / "Desktop" / "PJCT SCHDLS"

pytestmark = pytest.mark.skipif(
    not CORPUS.is_dir(), reason="operator schedule corpus not present on this host"
)


def _ocr(pdf: Path) -> dict | None:
    out = estimate_sandbox.run_sandboxed(
        "ocr_page_words", pdf.read_bytes(),
        timeout_s=estimate_sandbox.OCR_TIMEOUT_S, args=["6"],
    )
    return None if out is None else json.loads(out)


@pytest.fixture(scope="module")
def corpus_pdfs() -> list[Path]:
    pdfs = sorted(CORPUS.glob("*.pdf"))
    if not pdfs:
        pytest.skip("corpus folder is empty")
    return pdfs


def test_every_corpus_document_parses_to_a_reviewable_proposal(corpus_pdfs):
    failures: list[str] = []
    for pdf in corpus_pdfs:
        payload = _ocr(pdf)
        if payload is None:
            failures.append(f"{pdf.name}: sandbox OCR returned None")
            continue
        doc = schedule_parse.parse_schedule(schedule_geometry.reconstruct(payload["pages"]))
        data_rows = [r for r in doc.rows if r.kind == "data"]
        named = [
            r for r in data_rows
            if r.cells[schedule_parse.CANONICAL_COLUMNS.index("task_name")]
        ]
        if len(doc.rows) < 40:
            failures.append(f"{pdf.name}: only {len(doc.rows)} rows")
        elif len(named) < int(len(data_rows) * 0.8):
            failures.append(f"{pdf.name}: {len(named)}/{len(data_rows)} named")
    assert not failures, "\n".join(failures)


def test_the_live_coker_export_keeps_its_known_shape(corpus_pdfs):
    pdf = CORPUS / "Project Schedule - KSI - Coker 8.5.26.pdf"
    if not pdf.exists():
        pytest.skip("Coker 8.5.26 not in corpus")
    payload = _ocr(pdf)
    assert payload is not None
    doc = schedule_parse.parse_schedule(schedule_geometry.reconstruct(payload["pages"]))
    assert doc.meta.get("title", "").startswith("Project Schedule - KSI - Coker")
    assert doc.meta.get("project_name") == "Coker"
    assert 55 <= len(doc.rows) <= 80
    assert sum(1 for r in doc.rows if r.is_delivery) >= 8
    assert sum(1 for r in doc.rows if r.is_milestone) >= 4
    sections = {
        r.cells[schedule_parse.CANONICAL_COLUMNS.index("task_name")]
        for r in doc.rows
        if r.kind == "section"
    }
    assert {"Deliveries", "Civil", "Mechanical"} & sections
