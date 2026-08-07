"""Offline corpus-replay eval for the vendor-estimate extraction ladder (ADR-0004 E6).

OPERATOR-RUN, ENTIRELY LOCAL. Walks the Evergreen vendor-quote corpus on disk and
replays each document through screen → classify → the extraction ladder, exactly as
`estimate_poll` would — with ZERO Worker / Smartsheet / Box egress: this script
imports NOTHING that could send (no `portal_client`, no `box_client`, no
`smartsheet_client`, no `graph_client` — the capability-gating suite pins that).
The only network touched is localhost Ollama, and only under `--tier2`.

The corpus BYTES never enter the repo. The expectations fixture
(`tests/fixtures/estimate_corpus_expectations.json`) holds only sha256-keyed
EXPECTED METADATA (doc_type / tier_reached / line_count / math_pass / field
coverage) — the eval diffs live results against it, and `--write-expectations`
snapshots the current results as the new baseline. This is the acceptance gate the
tier-gate ITS_Config rows name: a tier flips true only after this eval qualifies
it (and a MODEL swap re-runs it, ADR-0004 decision 1).

Usage (from ~/its with the venv active):

    python3 scripts/eval_estimate_ladder.py                       # tiers 0+1 only
    python3 scripts/eval_estimate_ladder.py --tier2 --ocr         # full ladder
    python3 scripts/eval_estimate_ladder.py --write-expectations  # snapshot baseline

Exit code: 0 when every file matches expectations (or `--write-expectations`);
1 on any diff/regression; 2 on a usage error (missing corpus dir).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from po_materials import estimate_classify, po_attach_screen  # noqa: E402

DEFAULT_CORPUS = Path.home() / "Desktop" / "Evergreen project" / "Z. Quotes 1"
DEFAULT_EXPECTATIONS = (
    Path(__file__).resolve().parents[1]
    / "tests" / "fixtures" / "estimate_corpus_expectations.json"
)

# Mirrors the estimate_poll / seed defaults — CLI-overridable (offline: the eval
# reads NO ITS_Config).
DEFAULT_MODEL = "qwen3.5:9b"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_CONFIDENCE_THRESHOLD = 0.75
DEFAULT_TIMEOUT_S = 600

_EXTS_MIME = {
    ".pdf": "application/pdf",
    ".xlsx": po_attach_screen.MIME_XLSX,
}

# The comparable metadata keys (the fixture schema). `_README` is the fixture's
# self-documentation key, never a file entry.
RESULT_KEYS = ("doc_type", "tier_reached", "line_count", "math_pass", "field_coverage")
README_KEY = "_README"

# Header fields counted toward field coverage (the extraction-contract identity +
# money fields a good extraction should populate).
_COVERAGE_FIELDS = (
    "vendor_name", "quote_number", "quote_date", "subtotal_cents", "grand_total_cents",
)


def _eval_one(
    path: Path, *, tier2: bool, ocr: bool, model: str, base_url: str,
    confidence_threshold: float, timeout_s: int,
) -> dict[str, Any]:
    """Replay ONE corpus file through screen → classify → ladder, all local.

    Returns the per-file result record (the fixture value shape). Mirrors the
    `estimate_poll` pipeline order — the §34 screen ALWAYS precedes any parse.
    """
    data = path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    mime = _EXTS_MIME[path.suffix.lower()]
    record: dict[str, Any] = {
        "filename": path.name,  # informational only; identity is the sha256 key
        "sha256": sha256,
        "doc_type": None,
        "tier_reached": None,
        "line_count": 0,
        "math_pass": None,
        "field_coverage": [],
    }

    screen = po_attach_screen.screen_attachment(path.name, mime, data)
    if screen.disposition != "clean":
        record["doc_type"] = f"screen_{screen.disposition}"
        return record

    pages: list[str] = []
    if mime == "application/pdf":
        pages = estimate_classify.extract_pages_text(data)
        doc_type, _confidence = estimate_classify.classify_doc_type(pages)
    else:
        doc_type = "other"
    record["doc_type"] = doc_type
    if doc_type in estimate_classify.REFUSED_DOC_TYPES:
        record["tier_reached"] = "refused"
        return record

    payload = _run_ladder(
        data, pages, mime, path.name,
        tier2=tier2, ocr=ocr, model=model, base_url=base_url,
        confidence_threshold=confidence_threshold, timeout_s=timeout_s,
        record=record,
    )
    if payload is None:
        record["tier_reached"] = record.get("tier_reached") or "needs_review"
        return record

    lines = payload.get("lines") or []
    record["tier_reached"] = payload.get("tier")
    record["line_count"] = len(lines)
    record["math_pass"] = bool(
        payload.get("math_ok") in (1, True)
        and all(ln.get("math_ok") in (1, True) for ln in lines)
    )
    record["field_coverage"] = sorted(
        f for f in _COVERAGE_FIELDS if payload.get(f) not in (None, "")
    )
    if payload.get("doc_type"):
        record["doc_type"] = payload["doc_type"]
    return record


def _metric_payload(result: Any, tier: int) -> dict[str, Any]:
    """Project an extraction-core ExtractionResult down to the eval's metric shape
    (a plain dict — the same keys `_eval_one` reads)."""
    import dataclasses  # noqa: PLC0415 — stdlib, local to keep the top import surface lean

    return {
        "tier": tier,
        "doc_type": result.doc_type,
        "math_ok": 1 if result.math_ok else 0,
        "confidence": result.confidence,
        "vendor_name": result.vendor_name,
        "quote_number": result.quote_number,
        "quote_date": result.quote_date,
        "subtotal_cents": result.subtotal_cents,
        "grand_total_cents": result.grand_total_cents,
        "lines": [dataclasses.asdict(li) for li in result.line_items],
    }


def _run_ladder(
    data: bytes, pages: list[str], mime: str, filename: str, *,
    tier2: bool, ocr: bool, model: str, base_url: str,
    confidence_threshold: float, timeout_s: int, record: dict[str, Any],
) -> dict[str, Any] | None:
    """The estimate_poll ladder shape, replayed with CLI flags instead of gates.
    Extraction-core modules import lazily; an absent one is reported, never fatal."""
    # Tier 0 — a filled quote form. Offline the eval has no per-RFQ secret, so a
    # corpus form can parse but never token-VERIFY; the eval scores PARSABILITY
    # (structure + math), counting any parsed form as tier 0 — identity
    # verification is a runtime concern the daemon enforces (verified-only) and
    # tests/test_quote_form.py proves with a known secret.
    if mime == po_attach_screen.MIME_XLSX:
        try:
            from po_materials import quote_form  # noqa: PLC0415 — lazy sibling-adjacent import
            parsed = quote_form.parse_quote_form(data, secret=b"eval-no-secret")
        except ImportError:
            record["note"] = "quote_form/estimate_parse not importable (sibling PR)"
            return None
        except Exception as exc:  # noqa: BLE001 — hostile bytes; degrade like the daemon
            record["note"] = f"tier0 crash: {type(exc).__name__}"
            return None
        if parsed is not None and parsed.lines:
            return {
                "tier": 0,
                "doc_type": "filled_form",
                "math_ok": parsed.math_ok,
                "subtotal_cents": parsed.subtotal_cents,
                "grand_total_cents": parsed.subtotal_cents,
                "lines": parsed.lines,
            }
        # Not ITS's own form (or unparsable as one) — fall through to the VENDOR
        # spreadsheet tier, mirroring the daemon's `_tier1_xlsx` branch. Without this the
        # eval scores every vendor .xlsx as unextractable, and the corpus gate could never
        # qualify the very tier it exists to qualify.
        try:
            from po_materials import estimate_parse  # noqa: PLC0415 — lazy sibling-adjacent import
            xl = estimate_parse.parse_xlsx_estimate(data)
        except ImportError:
            record["note"] = "estimate_parse not importable (sibling PR)"
            return None
        except Exception as exc:  # noqa: BLE001 — hostile bytes; degrade like the daemon
            record["note"] = f"tier1 xlsx crash: {type(exc).__name__}"
            return None
        if xl is not None and xl.line_items:
            return _metric_payload(xl, tier=1)
        return None
    if mime != "application/pdf":
        return None

    scanned = not any(p.strip() for p in pages)

    # Tier 1 — deterministic template→generic parse (always attempted in the
    # eval; the ITS_Config gate is a runtime concern, not an eval one).
    if not scanned:
        result: Any = None
        try:
            from po_materials import estimate_parse  # noqa: PLC0415 — lazy extraction-core import
            pdf_parsed = estimate_parse.parse_native(data)
            if pdf_parsed is not None and not pdf_parsed.is_scanned:
                for tpl in estimate_parse.load_vendor_templates():
                    result = estimate_parse.parse_with_template(pdf_parsed, tpl)
                    if result is not None:
                        break
                if result is None:
                    result = estimate_parse.parse_generic_table(pdf_parsed)
        except ImportError:
            record["note"] = "estimate_parse not importable (extraction-core PR)"
            result = None
        except Exception as exc:  # noqa: BLE001 — degrade like the daemon
            record["note"] = f"tier1 crash: {type(exc).__name__}"
            result = None
        if result is not None and result.line_items:
            if result.needs_review or not result.math_ok:
                record["note"] = f"tier1 math-flagged: {result.math_flags[:2]}"
            elif result.confidence >= confidence_threshold:
                return _metric_payload(result, 1)

    # Tier 2 — local Ollama (only under --tier2; localhost only).
    if not tier2:
        return None
    text_pages = pages
    if scanned:
        if not ocr:
            return None
        try:
            from po_materials import estimate_ocr  # noqa: PLC0415 — lazy extraction-core import
            text_pages = estimate_ocr.ocr_pages(data)
        except ImportError:
            record["note"] = "estimate_ocr not importable (extraction-core PR)"
            return None
        except Exception as exc:  # noqa: BLE001 — degrade like the daemon
            record["note"] = f"ocr crash: {type(exc).__name__}"
            return None
        if not text_pages:
            return None
    try:
        from po_materials import estimate_extract  # noqa: PLC0415 — lazy extraction-core import
        llm_result = estimate_extract.extract(
            text_pages, model=model, base_url=base_url, timeout_s=timeout_s,
            confidence_threshold=confidence_threshold,
        )
    except ImportError:
        record["note"] = "estimate_extract not importable (extraction-core PR)"
        return None
    except Exception as exc:  # noqa: BLE001 — a wedged/absent model degrades
        record["note"] = f"tier2 crash: {type(exc).__name__}"
        return None
    if llm_result is None:
        record["note"] = "tier2 inference failed (Ollama down / model missing?)"
        return None
    if llm_result.needs_review:
        record["note"] = (
            f"tier2 flagged needs_review (confidence={llm_result.confidence}, "
            f"math_ok={llm_result.math_ok})"
        )
        return None
    return _metric_payload(llm_result, 2)


def _comparable(record: dict[str, Any]) -> dict[str, Any]:
    return {k: record.get(k) for k in RESULT_KEYS}


def _print_table(results: list[dict[str, Any]]) -> None:
    print()
    print(f"{'file':<52} {'doc_type':<14} {'tier':<12} {'lines':>5} {'math':>5}  coverage")
    print("-" * 110)
    for r in results:
        cov = ",".join(r.get("field_coverage") or []) or "-"
        print(
            f"{r['filename'][:50]:<52} {str(r['doc_type']):<14} "
            f"{str(r['tier_reached']):<12} {r['line_count']:>5} "
            f"{str(r['math_pass']):>5}  {cov}"
        )
    tiers: dict[str, int] = {}
    for r in results:
        tiers[str(r["tier_reached"])] = tiers.get(str(r["tier_reached"]), 0) + 1
    print("-" * 110)
    print(f"{len(results)} file(s); tier outcomes: "
          + ", ".join(f"{k}={v}" for k, v in sorted(tiers.items())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--expectations", type=Path, default=DEFAULT_EXPECTATIONS)
    parser.add_argument("--tier2", action=argparse.BooleanOptionalAction, default=False,
                        help="run the Tier-2 local-Ollama extraction (localhost only)")
    parser.add_argument("--ocr", action=argparse.BooleanOptionalAction, default=False,
                        help="OCR scanned documents into Tier-2 (needs --tier2)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--ollama-base-url", default=DEFAULT_OLLAMA_BASE_URL)
    parser.add_argument("--confidence-threshold", type=float,
                        default=DEFAULT_CONFIDENCE_THRESHOLD)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--write-expectations", action="store_true",
                        help="snapshot the current results as the new baseline")
    args = parser.parse_args(argv)

    if not args.corpus.is_dir():
        print(f"[error] corpus directory not found: {args.corpus}", file=sys.stderr)
        return 2

    files = sorted(
        p for p in args.corpus.rglob("*")
        if p.is_file() and p.suffix.lower() in _EXTS_MIME
    )
    if not files:
        print(f"[error] no PDF/xlsx files under {args.corpus}", file=sys.stderr)
        return 2
    print(f"[info] corpus: {args.corpus} ({len(files)} file(s)); "
          f"tier2={'on' if args.tier2 else 'off'} ocr={'on' if args.ocr else 'off'}")

    results = [
        _eval_one(
            path, tier2=args.tier2, ocr=args.ocr, model=args.model,
            base_url=args.ollama_base_url,
            confidence_threshold=args.confidence_threshold,
            timeout_s=args.timeout_seconds,
        )
        for path in files
    ]
    _print_table(results)

    if args.write_expectations:
        expectations: dict[str, Any] = {
            README_KEY: (
                "sha256-keyed EXPECTED METADATA for the offline estimate-ladder "
                "eval (scripts/eval_estimate_ladder.py). The corpus bytes NEVER "
                "enter the repo — each key is a corpus file's sha256; each value "
                "is the expected {doc_type, tier_reached, line_count, math_pass, "
                "field_coverage}. Regenerate with --write-expectations after a "
                "qualified ladder/model change."
            ),
        }
        for r in results:
            expectations[r["sha256"]] = _comparable(r)
        args.expectations.parent.mkdir(parents=True, exist_ok=True)
        args.expectations.write_text(json.dumps(expectations, indent=2, sort_keys=True) + "\n")
        print(f"[ok] wrote {len(results)} expectation(s) → {args.expectations}")
        return 0

    try:
        expectations = json.loads(args.expectations.read_text())
    except (OSError, json.JSONDecodeError):
        expectations = {}
    known = {k: v for k, v in expectations.items() if k != README_KEY}
    if not known:
        print("[warn] no expectations recorded yet — run --write-expectations to "
              "snapshot a qualified baseline. Nothing to diff; exiting 0.")
        return 0

    diffs: list[str] = []
    seen: set[str] = set()
    for r in results:
        seen.add(r["sha256"])
        expected = known.get(r["sha256"])
        if expected is None:
            diffs.append(f"NEW file (no expectation): {r['filename']} ({r['sha256'][:12]})")
            continue
        actual = _comparable(r)
        for key in RESULT_KEYS:
            if expected.get(key) != actual.get(key):
                diffs.append(
                    f"{r['filename']} ({r['sha256'][:12]}): {key} expected "
                    f"{expected.get(key)!r} got {actual.get(key)!r}"
                )
    for sha in known:
        if sha not in seen:
            diffs.append(f"MISSING corpus file for expectation {sha[:12]}")

    if diffs:
        print(f"\n[FAIL] {len(diffs)} diff(s) vs {args.expectations}:")
        for d in diffs:
            print(f"  - {d}")
        return 1
    print(f"\n[ok] all {len(results)} file(s) match expectations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
