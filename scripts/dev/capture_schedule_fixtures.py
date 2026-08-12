#!/usr/bin/env python3
"""Capture schedule-corpus OCR fixtures + a parse survey (operator-run, ADR-0006).

Renders + OCRs every PDF in the operator's schedule-corpus folder through the REAL
sandbox child (`estimate_sandbox.ocr_page_words`), then runs geometry + parse, and:

* writes the OCR payload of the FIXTURE SET (a small, named subset — committing
  all 32 would bloat the repo for no marginal coverage) to
  tests/fixtures/schedule_corpus/<slug>.json, ANONYMIZED — these captured payloads
  are what the CI geometry/parse suites run on, deterministically, with no OCR /
  corpus / Darwin dependency;
* prints a per-document survey (profile, row counts by kind, flag histogram,
  delivery/milestone counts) for the whole corpus — the qualification evidence the
  operator-run corpus test (tests/test_schedule_ocr_corpus.py) re-derives.

The corpus PDFs themselves are NEVER committed (customer data), and neither is
their raw text: captures are ANONYMIZED AT WRITE TIME (`_ANONYMIZE` below) —
client and project identifiers are substituted (Coker→Kestrel, KSI→Acme, …)
while the Vision GEOMETRY, the industry-standard task vocabulary, the dates and
the real OCR misread patterns ('12125125') all survive, because those are what
the tests exercise. This keeps the repo consistent with the manifest/estimate
lanes' no-customer-content-in-fixtures precedent (docs/tech_debt.md;
tests/test_estimate_parse.py) — the 2026-08-11 ops-stds review finding that
created this rule. Vision output is not perfectly deterministic across macOS
runs, which is exactly why the CI suites run on the CAPTURED payloads while the
live corpus test asserts bounded properties (row-count ranges, section names)
rather than exact cells.

Usage:
    .venv/bin/python scripts/dev/capture_schedule_fixtures.py \
        [--corpus ~/Desktop/PJCT\\ SCHDLS] [--capture-only] [--survey-only]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from field_ops import schedule_geometry, schedule_parse  # noqa: E402
from po_materials import estimate_sandbox  # noqa: E402

# The committed fixture set: one of each corpus shape — a live rotated Gantt-view
# export, a fresh 0% one, the lone text-layer grid-view export (captured via
# parse_native, not OCR), and a revision pair for the future reconcile suites.
# Slugs use the ANONYMIZED project names (the keys are on-disk filenames, needed
# to find the operator's PDFs; only what gets COMMITTED is anonymized).
FIXTURE_SET = {
    "Project Schedule - KSI - Coker 8.5.26.pdf": "kestrel_2026-08-05_gantt",
    "Project Schedule - KSI - Deeplake 7.22.pdf": "clearlake_2026-07-22_gantt",
    "Project Schedule - Generate - Bonacci 1- 11.19.pdf": "baseline1_2025-11-19_gantt",
    "Project Schedule - Generate - Bonacci 1- 1.16.26.pdf": "baseline1_2026-01-16_gantt",
}
TEXT_LAYER_FIXTURES = {
    "Project Schedule - KSI - Deeplake.pdf": "clearlake_2026-06-02_grid_textlayer",
}

# Customer/project identifier substitutions applied to every string in a payload
# BEFORE it is written. Word-boundary anchored; the lookahead keeps the task verb
# 'Generated' intact while replacing the client name 'Generate'.
_ANONYMIZE = [
    (re.compile(r"\bCoker\b"), "Kestrel"),
    (re.compile(r"\bDeeplake\b"), "Clearlake"),
    (re.compile(r"\bDeep Lake\b"), "Clear Lake"),
    (re.compile(r"\bBonacci\b"), "Baseline"),
    (re.compile(r"\bKSI\b"), "Acme"),
    (re.compile(r"\bGenerate\b(?!d)"), "GridCo"),
    (re.compile(r"\bColfax\b"), "Crossfield"),
    (re.compile(r"\bKiwi\b"), "Kite"),
    (re.compile(r"\bMinooka\b"), "Midfield"),
    (re.compile(r"\bIndian Creek\b"), "Iron Creek"),
    (re.compile(r"\bRoxbury\b"), "Rockvale"),
    (re.compile(r"\bSteger\b"), "Stonegate"),
    (re.compile(r"\bLuminace\b"), "Lumen"),
]


def _anon_payload(obj: object) -> object:
    if isinstance(obj, dict):
        return {k: _anon_payload(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_anon_payload(v) for v in obj]
    if isinstance(obj, str):
        for rx, sub in _ANONYMIZE:
            obj = rx.sub(sub, obj)
        return obj
    return obj

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "schedule_corpus"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def survey_one(pdf: Path, *, capture_as: str | None, textlayer_as: str | None) -> dict:
    data = pdf.read_bytes()
    out = estimate_sandbox.run_sandboxed(
        "ocr_page_words", data, timeout_s=estimate_sandbox.OCR_TIMEOUT_S, args=["6"]
    )
    if out is None:
        return {"file": pdf.name, "error": "ocr_sandbox_none"}
    payload = json.loads(out)

    if capture_as:
        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        (FIXTURES_DIR / f"{capture_as}.json").write_text(
            json.dumps(_anon_payload(payload), separators=(",", ":")) + "\n"
        )
    if textlayer_as:
        native = estimate_sandbox.run_sandboxed(
            "parse_native", data, timeout_s=estimate_sandbox.PARSE_TIMEOUT_S, args=["6"]
        )
        if native:
            FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
            (FIXTURES_DIR / f"{textlayer_as}.json").write_text(
                json.dumps(_anon_payload(json.loads(native)), separators=(",", ":")) + "\n"
            )

    pages = schedule_geometry.reconstruct(payload["pages"])
    doc = schedule_parse.parse_schedule(pages)
    kinds = Counter(r.kind for r in doc.rows)
    flags = Counter(f for r in doc.rows for f in r.flags)
    return {
        "file": pdf.name,
        "rotations": payload.get("rotations"),
        "words": [len(p) for p in payload["pages"]],
        "profile": doc.profile,
        "title": doc.meta.get("title", ""),
        "rows": len(doc.rows),
        "kinds": dict(kinds),
        "flags": dict(flags),
        "milestones": sum(1 for r in doc.rows if r.is_milestone),
        "deliveries": sum(1 for r in doc.rows if r.is_delivery),
        "named": sum(
            1 for r in doc.rows
            if r.kind == "data" and r.cells[schedule_parse.CANONICAL_COLUMNS.index("task_name")]
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=str(Path.home() / "Desktop" / "PJCT SCHDLS"))
    ap.add_argument("--capture-only", action="store_true")
    ap.add_argument("--survey-only", action="store_true")
    args = ap.parse_args()
    corpus = Path(args.corpus).expanduser()
    if not corpus.is_dir():
        print(f"corpus folder not found: {corpus}", file=sys.stderr)
        return 2

    pdfs = sorted(corpus.glob("*.pdf"))
    results = []
    for pdf in pdfs:
        capture_as = None if args.survey_only else FIXTURE_SET.get(pdf.name)
        textlayer_as = None if args.survey_only else TEXT_LAYER_FIXTURES.get(pdf.name)
        if args.capture_only and not (capture_as or textlayer_as):
            continue
        res = survey_one(pdf, capture_as=capture_as, textlayer_as=textlayer_as)
        results.append(res)
        print(json.dumps(res))
    ok = sum(1 for r in results if "error" not in r)
    print(f"\n{ok}/{len(results)} documents parsed", file=sys.stderr)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
