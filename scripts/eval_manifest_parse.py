"""Replay the manifest parser over a folder of REAL manifest documents and print what it made of
each one. Operator-run, never CI.

Why it exists: `tests/test_manifest_parse.py` pins the parser against literal grids transcribed
from real documents, which is the right boundary for CI — the source files are customer data and
must not enter the repo. But a transcription is a model of a document, not the document. This
script closes that gap by running the whole extract → parse path over the actual files, so a
change to pdfplumber, openpyxl, or the parser itself can be checked against reality before it
reaches anyone.

Treat a clean run over the sample corpus as the acceptance gate before enabling manifest import.

    python -m scripts.eval_manifest_parse --corpus "~/Desktop/evergreen project/manifests"
    python -m scripts.eval_manifest_parse --corpus <dir> --rows 5      # also dump sample rows

Reads only. Writes nothing, uploads nothing, and needs no credentials — the parser is pure and the
extraction runs locally. Nothing here is on the daemon path.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from field_ops import manifest_parse as mp

PDF_SUFFIXES = {".pdf"}
XLSX_SUFFIXES = {".xlsx"}


def grids_from_pdf(path: Path) -> list[tuple[str, list[list[Any]]]]:
    """Every table on every page, in order, labelled with its provenance."""
    import pdfplumber  # noqa: PLC0415 — heavy optional import, kept off module load

    out: list[tuple[str, list[list[Any]]]] = []
    with pdfplumber.open(str(path)) as pdf:
        for page_no, page in enumerate(pdf.pages, 1):
            for table_no, table in enumerate(page.extract_tables()):
                out.append((f"pdf:p{page_no}:t{table_no}", table))
    return out


def grids_from_xlsx(path: Path) -> list[tuple[str, list[list[Any]]]]:
    import openpyxl  # noqa: PLC0415

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        return [
            (f"xlsx:{ws.title}", [list(r) for r in ws.iter_rows(values_only=True)])
            for ws in wb.worksheets
        ]
    finally:
        wb.close()


def describe(path: Path, sample_rows: int) -> bool:
    """Parse one document and print a summary. Returns False if it produced no usable lines."""
    suffix = path.suffix.lower()
    try:
        if suffix in PDF_SUFFIXES:
            grids = grids_from_pdf(path)
        elif suffix in XLSX_SUFFIXES:
            grids = grids_from_xlsx(path)
        else:
            return True  # not a manifest format — silently skipped, not a failure
    except Exception as exc:  # noqa: BLE001 — an unreadable document is a RESULT, not a crash
        print(f"\n{path.name}\n  EXTRACTION FAILED: {type(exc).__name__}: {exc}")
        return False

    codes = mp.product_codes_from_meta(grids)
    parsed = mp.parse_manifest(grids, product_codes=codes)
    cmap = parsed.column_map

    kinds: dict[str, int] = {}
    flags: dict[str, int] = {}
    for row in parsed.rows:
        kinds[row.kind] = kinds.get(row.kind, 0) + 1
        for flag in row.flags:
            flags[flag] = flags.get(flag, 0) + 1

    print(f"\n{path.name}")
    print(f"  profile      {parsed.profile}")
    print(f"  rows         {kinds or '-'}")
    print(f"  flags        {flags or '-'}")
    qty = cmap.qty_default
    qty_label = cmap.labels.get(qty, "?") if qty is not None else "(none identified)"
    print(f"  quantity     col {qty} = {qty_label!r}   candidates={cmap.qty_candidates}")
    mapped = {c: i for c, i in sorted(cmap.mapping.items(), key=lambda kv: kv[1])}
    print(f"  columns      {mapped or '-'}")
    if parsed.meta:
        head = ", ".join(f"{k}={v}" for k, v in list(parsed.meta.items())[:4])
        print(f"  metadata     {head}")
    for note in parsed.notes:
        print(f"  note         {note}")

    data_rows = [r for r in parsed.rows if r.kind in (mp.KIND_DATA, mp.KIND_CONTINUATION)]
    for row in data_rows[:sample_rows]:
        cells = " | ".join(c[:18] for c in row.cells[:8])
        print(f"    r{row.index:<5} {row.kind:<12} {cells}")

    if not data_rows:
        print("  ⚠ NO DATA ROWS — this document would import nothing")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", required=True, help="directory of manifest documents")
    ap.add_argument("--rows", type=int, default=0, help="sample data rows to print per document")
    args = ap.parse_args(argv)

    corpus = Path(args.corpus).expanduser()
    if not corpus.is_dir():
        print(f"not a directory: {corpus}", file=sys.stderr)
        return 2

    files = sorted(p for p in corpus.iterdir() if p.suffix.lower() in PDF_SUFFIXES | XLSX_SUFFIXES)
    if not files:
        print(f"no .pdf/.xlsx documents in {corpus}", file=sys.stderr)
        return 2

    ok = sum(1 for path in files if describe(path, args.rows))
    print(f"\n{ok}/{len(files)} documents produced importable rows")
    return 0 if ok == len(files) else 1


if __name__ == "__main__":
    raise SystemExit(main())
