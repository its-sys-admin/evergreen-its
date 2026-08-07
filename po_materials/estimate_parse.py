"""Tier-1 deterministic vendor-estimate parse (ADR-0004 E4 — the extraction ladder's
native-PDF tier).

Purpose
-------
Turns a native (text-layer) vendor-quote PDF into an `ExtractionResult` with ZERO AI:

* `parse_native(data)` — pdfplumber per-page text + word positions + tables +
  chars-per-page, run INSIDE the killable rlimited `estimate_sandbox` child
  (red-team #5); the parent computes the `is_scanned` heuristic (near-zero text
  chars/page → the OCR tier, `estimate_ocr`, is required).
* `load_vendor_templates(dir)` — data-driven per-vendor templates
  (`po_materials/estimate_templates/*.yaml`): match regexes, line/section/skip
  rules, per-UOM divisors, totals labels. Templates are DATA (yaml.safe_load),
  never exec'd — adding a vendor is adding a YAML file (ADR-0004 decision 1).
* `parse_with_template(parsed, tpl)` — the template tier (Platt-class layouts).
* `parse_generic_table(parsed)` — pdfplumber table extraction + column-name
  inference for clean-table vendors (Terratech-class); SOV/SOC section-header
  rows without qty+unit_price become SECTION LABELS, never $0 lines (the OnPoint
  rule, RED-tested).
* `check_math(result)` — per-line qty×unit_cost==extended (per-UOM divisor aware:
  Platt's 'M' = per-thousand) via the SAME `_js_round` mirror the PO composer uses,
  plus doc-level Σextended vs subtotal/grand_total. Sets `math_ok`/`math_flags`,
  NEVER raises — the math gate verifies internal CONSISTENCY, not fidelity
  (ADR-0004 decision 3: the human side-by-side accept is the fidelity control).
* `to_cents` / `family_key` — the lane's money normalization (Decimal
  ROUND_HALF_UP quantize-to-cents) and body-derived identity key (decision 7).

Invariants
----------
* NO AI (cloud or local), NO send, NO network — pure local parsing; enrolled in
  tests/test_capability_gating.py GATED_SCRIPTS (strict 7-needle list).
* Every hostile-byte parse runs in the sandbox child; THIS module only consumes
  the child's bounded JSON. Hostile input degrades (None / []), never raises out.
* Extraction output is ADVISORY (decision 2): nothing here writes anywhere —
  every dollar re-enters the trusted path only through the human-reviewed
  disposition accept.

Failure modes
-------------
* Sandbox timeout / crash / malformed child output → `parse_native` → None.
* Template that doesn't match / parses zero lines → `parse_with_template` → None
  (fall through the ladder). Same for `parse_generic_table`.
* A malformed template FILE is skipped with a stdlib-logging WARNING (visible in
  the daemon log) — one bad YAML never hides the other vendors' templates.
* `to_cents` raises ValueError on unparseable money — callers catch and treat the
  line/total as absent.

Consumers
---------
* `po_materials/estimate_poll.py` — the E4/E5 ladder wiring (sibling slice).
* `po_materials/estimate_extract.py` — reuses `ExtractionResult` + `check_math`.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from po_materials import estimate_sandbox
from po_materials.po_generate import _js_round

LOGGER = logging.getLogger(__name__)

# Default per-UOM extended-price divisors: Platt (and distributor pricing generally)
# quotes wire per-THOUSAND — 'UNT PR / UOM' of "$1,098.90 M" means
# extended = qty / 1000 × unit. FT/EA/etc. divide by 1 (the default).
DEFAULT_UOM_DIVISORS: dict[str, int] = {"M": 1000}

# is_scanned heuristic: a native text layer averages hundreds-to-thousands of text
# chars per page; a scanned image page has (near-)zero. Below this mean → scanned.
SCANNED_CHARS_PER_PAGE = 25

# Pages consumed by the Tier-1 parse (multi-page quotes are real; sandbox child
# hard-caps at 50).
DEFAULT_MAX_PAGES = 20

# Canonical repo location of the vendor templates.
TEMPLATES_DIR = Path(__file__).resolve().parent / "estimate_templates"

_DEFAULT_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d")

# Generic totals labels (the parse_generic_table fallback; templates carry their own).
_GENERIC_TOTALS: dict[str, re.Pattern[str]] = {
    "subtotal_cents": re.compile(
        r"sub\s*-?\s*total\s*:?\s*\$?\s*([\d,]+\.\d{2})", re.I
    ),
    "tax_cents": re.compile(r"(?:sales\s+)?tax\s*:?\s*\$?\s*([\d,]+\.\d{2})", re.I),
    "freight_cents": re.compile(
        r"(?:freight|shipping)\s*:?\s*\$?\s*([\d,]+\.\d{2})", re.I
    ),
    "grand_total_cents": re.compile(
        r"(?:grand\s+total|total\s+due|^\s*total)\s*:?\s*\$?\s*([\d,]+\.\d{2})",
        re.I | re.M,
    ),
}
_GENERIC_QUOTE_NUMBER = re.compile(
    r"\bquot(?:e|ation)\s*(?:#|no\.?|number)\s*:?\s*([A-Za-z0-9-]+)", re.I
)

# Column-name inference vocabulary for parse_generic_table. Order matters: the
# first concept whose keywords match a header cell claims that column.
_COLUMN_CONCEPTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("qty", ("qty", "quantity", "qnty")),
    ("unit_price", ("unit price", "unit cost", "unit pr", "price", "rate")),
    ("extended", ("ext price", "extended", "line total", "amount", "total", "ext")),
    ("unit", ("uom", "u/m", "um", "unit")),
    ("part_number", ("part #", "part", "item #", "item no", "code", "sku", "model")),
    ("description", ("description", "desc", "item description", "scope", "item", "work")),
)


# ---- Shapes ------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedPdf:
    """The sandbox child's Tier-1 parse payload, shape-validated in the parent."""

    pages_text: list[str]
    words: list[list[dict[str, Any]]]  # per page: [{"text","x0","x1","top","bottom"}...]
    tables: list[list[list[list[str]]]]  # per page: list of tables (rows of cell strs)
    chars_per_page: list[int]
    is_scanned: bool


@dataclass
class LineItem:
    """One extracted line — advisory data for the disposition screen."""

    description: str
    section: str | None = None
    part_number: str | None = None
    qty: float | None = None
    unit: str | None = None
    unit_cost_cents: int | None = None
    extended_cents: int | None = None
    line_note: str | None = None
    math_ok: bool = True


@dataclass
class ExtractionResult:
    """The union extraction shape every ladder tier produces (ADR-0004 corpus union).

    Mirrors schemas/vendor_estimate_extraction.json v1.0.0 field-for-field, plus the
    lane-internal `tier` / `math_*` / `anomaly_flags` / `needs_review` metadata.
    """

    doc_type: str
    confidence: float
    line_items: list[LineItem]
    vendor_name: str | None = None
    quote_number: str | None = None
    revision_label: str | None = None
    quote_date: str | None = None  # YYYY-MM-DD
    valid_until: str | None = None  # YYYY-MM-DD
    subtotal_cents: int | None = None
    tax_cents: int | None = None
    freight_cents: int | None = None
    misc_cents: int | None = None
    grand_total_cents: int | None = None
    not_to_exceed_cap_cents: int | None = None
    payment_terms: str | None = None
    notes: list[str] = field(default_factory=list)
    tier: str = "tier1_template"  # tier1_template | tier1_generic | tier2_llm
    math_ok: bool = True
    math_flags: list[str] = field(default_factory=list)
    anomaly_flags: list[str] = field(default_factory=list)
    needs_review: bool = False


@dataclass(frozen=True)
class VendorTemplate:
    """One data-driven vendor layout (compiled from an estimate_templates/*.yaml)."""

    name: str
    vendor_name: str
    doc_type: str
    match: tuple[re.Pattern[str], ...]  # ALL must hit page-1 text
    quote_number: re.Pattern[str] | None
    revision_label: re.Pattern[str] | None
    quote_date: re.Pattern[str] | None
    date_formats: tuple[str, ...]
    line_pattern: re.Pattern[str] | None
    section_pattern: re.Pattern[str] | None
    skip_patterns: tuple[re.Pattern[str], ...]
    uom_divisors: dict[str, int]
    totals: dict[str, re.Pattern[str]]  # keys: subtotal_cents/tax_cents/.../grand_total_cents
    source_path: str


# ---- Money + identity ---------------------------------------------------------------


def to_cents(value: Any) -> int | None:
    """Normalize a money value to integer cents (Decimal, ROUND_HALF_UP quantize).

    Accepts '1,098.90', '$993.12429' (over-precise unit prices quantize to cents:
    99312 — deterministic), '(12.50)' accounting negatives, ints/floats/Decimals.
    Returns None on None / unparseable / non-finite input — the ladder contract
    (`estimate_poll` + `quote_form` consume `int | None`; a bad cell degrades the
    field to absent, it never raises into the daemon).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        try:
            d = Decimal(str(value))
        except InvalidOperation:  # inf/nan spellings Decimal itself rejects
            return None
    elif isinstance(value, str):
        s = value.strip().replace("$", "").replace(",", "").replace(" ", "")
        negative = s.startswith("(") and s.endswith(")")
        if negative:
            s = s[1:-1]
        if not s:
            return None
        try:
            d = Decimal(s)
        except InvalidOperation:
            return None
        if negative:
            d = -d
    else:
        return None
    if not d.is_finite():
        return None
    cents = (d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def family_key(vendor_name: str | None, quote_number: str | None, sha256: str) -> str:
    """Body-derived revision-family identity (ADR-0004 decision 7).

    `normalize(vendor)|normalize(quote_number)`; a numberless (Brimfield-class)
    document falls back to the content sha256 — NEVER the filename (filenames lie).
    Normalization: casefold + strip all punctuation/whitespace.
    """
    v = _NORMALIZE_RE.sub("", (vendor_name or "").casefold())
    q = _NORMALIZE_RE.sub("", (quote_number or "").casefold())
    if q:
        return f"{v}|{q}"
    return f"{v}|{sha256.lower()}"


# ---- Tier-1 native parse ------------------------------------------------------------


def parse_native(data: bytes, *, max_pages: int = DEFAULT_MAX_PAGES) -> ParsedPdf | None:
    """pdfplumber parse of a (potentially hostile) PDF via the sandboxed child.

    Returns a shape-validated ParsedPdf, or None when the child timed out /
    crashed / produced malformed output — the caller's degrade signal (ladder
    falls through). `is_scanned` is computed HERE from chars-per-page: a document
    with (near-)zero text chars per page has no native text layer and needs the
    OCR tier. A zero-page payload is treated as scanned (Quartz may still render
    it for OCR). Never raises on hostile input.
    """
    out = estimate_sandbox.run_sandboxed(
        "parse_native",
        data,
        timeout_s=estimate_sandbox.PARSE_TIMEOUT_S,
        args=(str(max_pages),),
    )
    if out is None:
        return None
    try:
        parsed = json.loads(out)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    pages_raw = parsed.get("pages")
    chars_raw = parsed.get("chars_per_page")
    words_raw = parsed.get("words")
    tables_raw = parsed.get("tables")
    if not isinstance(pages_raw, list) or not isinstance(chars_raw, list):
        return None
    pages = [p for p in pages_raw if isinstance(p, str)][:max_pages]
    chars = [int(c) for c in chars_raw if isinstance(c, int | float)][:max_pages]
    words: list[list[dict[str, Any]]] = []
    if isinstance(words_raw, list):
        for page_words in words_raw[:max_pages]:
            words.append(
                [w for w in page_words if isinstance(w, dict)]
                if isinstance(page_words, list)
                else []
            )
    tables: list[list[list[list[str]]]] = []
    if isinstance(tables_raw, list):
        for page_tables in tables_raw[:max_pages]:
            clean_tables: list[list[list[str]]] = []
            if isinstance(page_tables, list):
                for table in page_tables:
                    if isinstance(table, list):
                        clean_tables.append(
                            [
                                [str(c) for c in row]
                                for row in table
                                if isinstance(row, list)
                            ]
                        )
            tables.append(clean_tables)
    mean_chars = (sum(chars) / len(chars)) if chars else 0.0
    is_scanned = mean_chars < SCANNED_CHARS_PER_PAGE
    return ParsedPdf(
        pages_text=pages,
        words=words,
        tables=tables,
        chars_per_page=chars,
        is_scanned=is_scanned,
    )


# ---- Tier-1 xlsx (vendor spreadsheets) ----------------------------------------------
#
# A worksheet IS already the shape `parse_generic_table` consumes — per page, a list of
# tables, each a list of rows of string cells — so the extraction half of this tier is an
# ADAPTER, not a second parser. The line-item logic, column inference, section-band
# handling, `check_math` and `to_worker_payload` are reused verbatim.
#
# The ONE thing that does not carry over is doc-level totals; see `_xlsx_totals_from_grid`.

# Label vocabulary for the cell-grid totals scan. Matched against a NORMALIZED label cell
# (lowercased, punctuation/whitespace squeezed) — deliberately not regexes over free text,
# which is exactly the trap this function exists to avoid.
_XLSX_TOTAL_LABELS: dict[str, tuple[str, ...]] = {
    "subtotal_cents": ("subtotal", "sub total"),
    "tax_cents": ("tax", "sales tax"),
    "freight_cents": ("freight", "shipping", "delivery"),
    "grand_total_cents": ("grand total", "total due", "order total", "total"),
}
# How far right of a label cell to look for its number. Vendors pad with blank/merged
# spacer columns; beyond a handful it stops being "the adjacent value".
_XLSX_TOTAL_SCAN_COLS = 6


def _normalize_label(cell: Any) -> str:
    if not isinstance(cell, str):
        return ""
    return re.sub(r"[\s:\-_.]+", " ", cell).strip().lower()


def _xlsx_totals_from_grid(raw_sheets: list[list[list[Any]]]) -> dict[str, int]:
    """Read doc-level totals off the CELL GRID, never by regexing flattened text.

    THIS IS THE LOAD-BEARING DIFFERENCE between the xlsx tier and the PDF tier, and it is a
    correctness control, not an optimization.

    `_GENERIC_TOTALS` (used by `parse_generic_table` on PDF text) requires exactly two
    decimals: ``([\\d,]+\\.\\d{2})``. A PDF text layer always carries FORMATTED currency
    ("$4,685.00"), so that holds. openpyxl hands back the STORED value instead, and a
    ROUND money amount stringifies with ONE decimal — ``str(4000.00) == "4000.0"`` — so the
    pattern misses it. (A non-round amount like ``386.51`` happens to survive, which makes
    the failure intermittent and therefore worse: it depends on the vendor's numbers.)

    A subtotal is very often round, and `check_math` deliberately SKIPS comparisons with
    absent operands, so the whole cross-check evaporates while `math_ok` stays True. Measured
    on a book whose subtotal was WRONG (3999.00 against lines summing to 4000.00):

        regex-over-text : math_ok=True,  flags=[]                      <- MISSES it
        this function   : math_ok=False, flags=['Σextended 400000 != subtotal 399900',
                                                'subtotal+tax+freight+misc 399900 != grand_total 400000']

    A document posting as `extracted` with every LINE correct and no doc-level check
    performed is silently unverified rather than visibly failed — the worst shape a money
    bug can take, and the reason this reads cells instead of text.

    So: find a label cell, take the nearest numeric cell to its right, and hand the NATIVE
    value to `to_cents` (which handles floats/Decimals exactly). Longest label wins, so
    "grand total" is not shadowed by the bare "total" alias.
    """
    found: dict[str, int] = {}
    for rows in raw_sheets:
        for row in rows:
            for col, cell in enumerate(row):
                label = _normalize_label(cell)
                if not label:
                    continue
                # Longest alias first so "grand total" beats "total".
                best: tuple[int, str] | None = None
                for key, aliases in _XLSX_TOTAL_LABELS.items():
                    for alias in aliases:
                        if label == alias or label.startswith(alias + " ") or label.endswith(" " + alias):
                            if best is None or len(alias) > best[0]:
                                best = (len(alias), key)
                if best is None:
                    continue
                key = best[1]
                if key in found:  # first occurrence wins (headers precede footers)
                    continue
                for probe in row[col + 1 : col + 1 + _XLSX_TOTAL_SCAN_COLS]:
                    if isinstance(probe, bool) or probe is None or probe == "":
                        continue
                    cents = to_cents(probe)
                    if cents is not None:
                        found[key] = cents
                        break
    return found


def parse_xlsx(data: bytes, *, max_sheets: int = 12) -> tuple[ParsedPdf, list[list[list[Any]]]] | None:
    """Parse a (potentially hostile) vendor workbook via the sandboxed child.

    Returns `(ParsedPdf, raw_sheets)` — the ParsedPdf feeds `parse_generic_table` unchanged,
    and `raw_sheets` (native-typed cells) feeds `_xlsx_totals_from_grid`. None on a child
    timeout / crash / malformed output — the caller's degrade signal. Never raises on
    hostile input.

    `is_scanned` is always False: a workbook has no text layer to be missing, and the
    scanned/OCR concept simply does not apply.
    """
    out = estimate_sandbox.run_sandboxed(
        "parse_xlsx_grid",
        data,
        timeout_s=estimate_sandbox.XLSX_TIMEOUT_S,
        args=(str(max_sheets),),
    )
    if out is None:
        return None
    try:
        parsed = json.loads(out)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    tables_raw = parsed.get("tables")
    text_raw = parsed.get("text")
    raw_raw = parsed.get("raw")
    if not isinstance(tables_raw, list) or not isinstance(text_raw, list):
        return None

    # One worksheet == one "page": tables[i] is that page's single table.
    tables: list[list[list[list[str]]]] = []
    for sheet in tables_raw[:max_sheets]:
        if isinstance(sheet, list):
            tables.append([[[str(c) for c in row] for row in sheet if isinstance(row, list)]])
        else:
            tables.append([])
    pages = [t for t in text_raw if isinstance(t, str)][:max_sheets]
    raw_sheets: list[list[list[Any]]] = []
    if isinstance(raw_raw, list):
        for sheet in raw_raw[:max_sheets]:
            raw_sheets.append(
                [row for row in sheet if isinstance(row, list)] if isinstance(sheet, list) else []
            )

    return (
        ParsedPdf(
            pages_text=pages,
            words=[[] for _ in pages],
            tables=tables,
            chars_per_page=[len(p) for p in pages],
            is_scanned=False,
        ),
        raw_sheets,
    )


def parse_xlsx_estimate(data: bytes, *, max_sheets: int = 12) -> ExtractionResult | None:
    """Full Tier-1 xlsx extraction: adapter -> generic table -> CELL totals -> check_math.

    The one composed entry point the ladder calls. Returns None when the workbook cannot be
    parsed or yields no line items (the ladder then falls through to needs_review).
    """
    adapted = parse_xlsx(data, max_sheets=max_sheets)
    if adapted is None:
        return None
    parsed, raw_sheets = adapted
    result = parse_generic_table(parsed)
    if result is None:
        return None

    # Override the (structurally unreliable) text-regex totals with cell-grid values.
    for key, cents in _xlsx_totals_from_grid(raw_sheets).items():
        setattr(result, key, cents)

    result.tier = "tier1_xlsx"
    return check_math(result)


# ---- Vendor templates ---------------------------------------------------------------


def load_vendor_templates(templates_dir: Path | str = TEMPLATES_DIR) -> list[VendorTemplate]:
    """Load + compile every `*.yaml` vendor template in `templates_dir`, sorted by name.

    Templates are pure DATA (yaml.safe_load) — never exec'd. A malformed file is
    SKIPPED with a logged WARNING (one bad template never hides the rest); an
    absent directory returns [].
    """
    root = Path(templates_dir)
    if not root.is_dir():
        return []
    templates: list[VendorTemplate] = []
    for path in sorted(root.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            templates.append(_template_from_mapping(raw, source_path=str(path)))
        except Exception as exc:  # noqa: BLE001 — one bad template is skipped, loudly
            LOGGER.warning("skipping malformed vendor template %s: %s", path, exc)
    return templates


def _compile(value: Any, *, where: str) -> re.Pattern[str]:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: expected a non-empty regex string")
    return re.compile(value, re.I | re.M)


def _template_from_mapping(raw: Any, *, source_path: str) -> VendorTemplate:
    """Validate + compile one template mapping. Raises ValueError on a bad shape."""
    if not isinstance(raw, dict):
        raise ValueError("template document must be a mapping")
    name = raw.get("name")
    vendor_name = raw.get("vendor_name")
    if not isinstance(name, str) or not name:
        raise ValueError("template missing 'name'")
    if not isinstance(vendor_name, str) or not vendor_name:
        raise ValueError("template missing 'vendor_name'")
    doc_type = raw.get("doc_type", "quote")
    if doc_type not in ("quote", "estimate", "proposal"):
        raise ValueError(f"template doc_type {doc_type!r} not an importable type")
    match_raw = raw.get("match")
    if not isinstance(match_raw, list) or not match_raw:
        raise ValueError("template missing non-empty 'match' list")
    match = tuple(_compile(m, where="match") for m in match_raw)

    fields_raw = raw.get("fields") or {}
    if not isinstance(fields_raw, dict):
        raise ValueError("'fields' must be a mapping")

    def _opt_field(key: str) -> re.Pattern[str] | None:
        val = fields_raw.get(key)
        return None if val is None else _compile(val, where=f"fields.{key}")

    date_formats_raw = raw.get("date_formats") or list(_DEFAULT_DATE_FORMATS)
    if not isinstance(date_formats_raw, list) or not all(
        isinstance(f, str) for f in date_formats_raw
    ):
        raise ValueError("'date_formats' must be a list of strptime format strings")

    lines_raw = raw.get("lines") or {}
    if not isinstance(lines_raw, dict):
        raise ValueError("'lines' must be a mapping")
    line_pattern = (
        _compile(lines_raw["pattern"], where="lines.pattern")
        if lines_raw.get("pattern") is not None
        else None
    )
    section_pattern = (
        _compile(lines_raw["section_pattern"], where="lines.section_pattern")
        if lines_raw.get("section_pattern") is not None
        else None
    )
    skip_raw = lines_raw.get("skip") or []
    if not isinstance(skip_raw, list):
        raise ValueError("'lines.skip' must be a list")
    skip_patterns = tuple(_compile(s, where="lines.skip") for s in skip_raw)

    divisors_raw = raw.get("uom_divisors") or {}
    if not isinstance(divisors_raw, dict):
        raise ValueError("'uom_divisors' must be a mapping")
    uom_divisors: dict[str, int] = {}
    for uom, div in divisors_raw.items():
        if not isinstance(uom, str) or not isinstance(div, int) or div < 1:
            raise ValueError(f"bad uom_divisor entry {uom!r}: {div!r}")
        uom_divisors[uom.upper()] = div

    totals_raw = raw.get("totals") or {}
    if not isinstance(totals_raw, dict):
        raise ValueError("'totals' must be a mapping")
    allowed_totals = {
        "subtotal": "subtotal_cents",
        "tax": "tax_cents",
        "freight": "freight_cents",
        "misc": "misc_cents",
        "grand_total": "grand_total_cents",
    }
    totals: dict[str, re.Pattern[str]] = {}
    for key, pattern in totals_raw.items():
        if key not in allowed_totals:
            raise ValueError(f"unknown totals key {key!r} (allowed: {sorted(allowed_totals)})")
        totals[allowed_totals[key]] = _compile(pattern, where=f"totals.{key}")

    return VendorTemplate(
        name=name,
        vendor_name=vendor_name,
        doc_type=doc_type,
        match=match,
        quote_number=_opt_field("quote_number"),
        revision_label=_opt_field("revision_label"),
        quote_date=_opt_field("quote_date"),
        date_formats=tuple(date_formats_raw),
        line_pattern=line_pattern,
        section_pattern=section_pattern,
        skip_patterns=skip_patterns,
        uom_divisors=uom_divisors,
        totals=totals,
        source_path=source_path,
    )


def _first_group(pattern: re.Pattern[str] | None, text: str) -> str | None:
    if pattern is None:
        return None
    m = pattern.search(text)
    if m is None:
        return None
    value = m.group(1) if m.groups() else m.group(0)
    value = value.strip()
    return value or None


def _parse_date(raw: str | None, formats: tuple[str, ...]) -> str | None:
    """Normalize a captured date string to YYYY-MM-DD; None when unparseable."""
    if raw is None:
        return None
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_qty(raw: str) -> float | None:
    try:
        qty = float(raw.replace(",", ""))
    except ValueError:
        return None
    if qty < 0 or qty > 1_000_000:  # schema bound mirrored
        return None
    return qty


def parse_with_template(parsed: ParsedPdf, tpl: VendorTemplate) -> ExtractionResult | None:
    """Apply one vendor template. None when the template doesn't match page-1 text
    or parses ZERO line rows (the ladder falls through) — a template hit with no
    lines is a layout drift, not a lump-sum doc. Never raises on hostile text."""
    page1 = parsed.pages_text[0] if parsed.pages_text else ""
    if not page1 or not all(m.search(page1) for m in tpl.match):
        return None
    if tpl.line_pattern is None:
        return None
    full_text = "\n".join(parsed.pages_text)

    lines: list[LineItem] = []
    current_section: str | None = None
    for text_line in full_text.splitlines():
        if not text_line.strip():
            continue
        if any(p.search(text_line) for p in tpl.skip_patterns):
            continue
        m = tpl.line_pattern.search(text_line)
        if m:
            groups = m.groupdict()
            qty = _parse_qty(groups.get("qty") or "")
            unit_cost = to_cents(groups.get("unit_price"))
            extended = to_cents(groups.get("extended"))
            if qty is None or unit_cost is None:
                # A row without qty+unit_price is a section header / free text,
                # never a $0 line (the corpus rule).
                continue
            description = (groups.get("description") or "").strip()
            if not description:
                continue
            unit = (groups.get("uom") or "").strip().upper() or None
            lines.append(
                LineItem(
                    description=description[:500],
                    section=current_section,
                    part_number=(groups.get("part_number") or "").strip()[:64] or None,
                    qty=qty,
                    unit=unit[:16] if unit else None,
                    unit_cost_cents=unit_cost,
                    extended_cents=extended,
                )
            )
            continue
        if tpl.section_pattern:
            sm = tpl.section_pattern.search(text_line)
            if sm:
                section = (
                    sm.groupdict().get("section")
                    or (sm.group(1) if sm.groups() else sm.group(0))
                ).strip()
                if section:
                    current_section = section[:120]
    if not lines:
        return None

    totals_values: dict[str, int | None] = {}
    for cents_key, pattern in tpl.totals.items():
        totals_values[cents_key] = to_cents(_first_group(pattern, full_text))

    result = ExtractionResult(
        doc_type=tpl.doc_type,
        confidence=1.0,  # deterministic template hit; the math gate + human review gate it
        line_items=lines,
        vendor_name=tpl.vendor_name,
        quote_number=_first_group(tpl.quote_number, full_text),
        revision_label=_first_group(tpl.revision_label, full_text),
        quote_date=_parse_date(_first_group(tpl.quote_date, full_text), tpl.date_formats),
        subtotal_cents=totals_values.get("subtotal_cents"),
        tax_cents=totals_values.get("tax_cents"),
        freight_cents=totals_values.get("freight_cents"),
        misc_cents=totals_values.get("misc_cents"),
        grand_total_cents=totals_values.get("grand_total_cents"),
        tier="tier1_template",
    )
    result = check_math(result, uom_divisors=tpl.uom_divisors)
    return dataclasses.replace(result, needs_review=not result.math_ok)


# ---- Generic table tier -------------------------------------------------------------


def _infer_columns(row: list[str]) -> dict[str, int] | None:
    """Map column concepts → cell index from a candidate header row.

    Requires at minimum description + qty + unit_price to call it a header.
    First-match-wins per concept; a cell claims at most one concept.
    """
    assigned: dict[str, int] = {}
    claimed: set[int] = set()
    for concept, keywords in _COLUMN_CONCEPTS:
        # Keywords OUTER, cells inner: a stronger label ("Description") must beat a
        # weaker same-concept keyword ("Item") appearing in an earlier column.
        for kw in keywords:
            hit = next(
                (
                    idx
                    for idx, cell in enumerate(row)
                    if idx not in claimed and kw in cell.strip().lower()
                ),
                None,
            )
            if hit is not None:
                assigned[concept] = hit
                claimed.add(hit)
                break
    if {"description", "qty", "unit_price"} <= assigned.keys():
        return assigned
    return None


def parse_generic_table(parsed: ParsedPdf) -> ExtractionResult | None:
    """Generic-table tier: pdfplumber table extraction + column-name inference.

    Walks the (sandbox-produced) per-page tables for header rows whose labels
    infer description/qty/unit-price columns, then parses the data rows below.
    A row WITHOUT a parseable qty + unit price is a section band (its description
    text becomes the running section label) — NEVER a $0 line (the OnPoint
    SOV/SOC rule, RED-tested). None when no table infers — the ladder falls
    through to Tier-2. Never raises on hostile text.
    """
    lines: list[LineItem] = []
    for page_tables in parsed.tables:
        for table in page_tables:
            cols: dict[str, int] | None = None
            current_section: str | None = None
            for row in table:
                if cols is None:
                    cols = _infer_columns(row)
                    continue

                def _cell(concept: str, row: list[str] = row, cols: dict[str, int] = cols) -> str:
                    idx = cols.get(concept)
                    if idx is None or idx >= len(row):
                        return ""
                    return row[idx].strip()

                description = _cell("description")
                qty = _parse_qty(_cell("qty")) if _cell("qty") else None
                unit_cost = to_cents(_cell("unit_price"))
                if qty is None or unit_cost is None:
                    # Section-header / narrative row (OnPoint SOV/SOC class):
                    # label, never a $0 line.
                    if description:
                        current_section = description[:120]
                    continue
                if not description:
                    continue
                extended = to_cents(_cell("extended"))
                unit = _cell("unit").upper() or None
                lines.append(
                    LineItem(
                        description=description[:500],
                        section=current_section,
                        part_number=_cell("part_number")[:64] or None,
                        qty=qty,
                        unit=unit[:16] if unit else None,
                        unit_cost_cents=unit_cost,
                        extended_cents=extended,
                    )
                )
    if not lines:
        return None

    full_text = "\n".join(parsed.pages_text)
    totals_values: dict[str, int | None] = {}
    for cents_key, pattern in _GENERIC_TOTALS.items():
        totals_values[cents_key] = to_cents(_first_group(pattern, full_text))
    page1_lines = [ln.strip() for ln in (parsed.pages_text[0] if parsed.pages_text else "").splitlines()]
    vendor_guess = next((ln for ln in page1_lines if ln), None)

    result = ExtractionResult(
        doc_type="quote",
        confidence=0.75,  # inferred columns, not a pinned layout — always reviewed
        line_items=lines,
        vendor_name=(vendor_guess[:200] if vendor_guess else None),
        quote_number=_first_group(_GENERIC_QUOTE_NUMBER, full_text),
        subtotal_cents=totals_values.get("subtotal_cents"),
        tax_cents=totals_values.get("tax_cents"),
        freight_cents=totals_values.get("freight_cents"),
        grand_total_cents=totals_values.get("grand_total_cents"),
        tier="tier1_generic",
    )
    result = check_math(result)
    return dataclasses.replace(result, needs_review=not result.math_ok)


# ---- Math gate ----------------------------------------------------------------------


def check_math(
    result: ExtractionResult, *, uom_divisors: dict[str, int] | None = None
) -> ExtractionResult:
    """The deterministic consistency gate (ADR-0004 decision 3 — consistency, NOT
    fidelity; the human side-by-side accept is the fidelity control).

    Per line: `_js_round((qty / uom_divisor) × unit_cost_cents) == extended_cents`
    (the SAME ECMA half-up mirror the PO composer's totals assert uses — 'M' is
    per-thousand, so 2,500 @ $1,098.90/M ⇒ 274725 exactly). Doc level: Σextended
    vs subtotal_cents, and subtotal+tax+freight+misc vs grand_total_cents (falling
    back to Σextended vs grand_total when no subtotal). Comparisons with absent
    operands are SKIPPED, not flagged. Returns a copy with `math_ok`/`math_flags`
    (+ per-line math_ok) set. NEVER raises.
    """
    divisors = dict(DEFAULT_UOM_DIVISORS)
    divisors.update({k.upper(): v for k, v in (uom_divisors or {}).items()})
    flags: list[str] = []
    new_lines: list[LineItem] = []
    try:
        for i, line in enumerate(result.line_items, start=1):
            line_ok = True
            if (
                line.qty is not None
                and line.unit_cost_cents is not None
                and line.extended_cents is not None
            ):
                divisor = divisors.get((line.unit or "").upper(), 1)
                expected = _js_round((line.qty / divisor) * float(line.unit_cost_cents))
                if expected != line.extended_cents:
                    line_ok = False
                    flags.append(
                        f"line {i}: qty×unit_cost != extended "
                        f"(expected {expected}, got {line.extended_cents})"
                    )
            new_lines.append(dataclasses.replace(line, math_ok=line_ok))

        extendeds = [
            ln.extended_cents for ln in new_lines if ln.extended_cents is not None
        ]
        sum_extended = sum(extendeds) if extendeds else None
        if sum_extended is not None and result.subtotal_cents is not None:
            if sum_extended != result.subtotal_cents:
                flags.append(
                    f"Σextended {sum_extended} != subtotal {result.subtotal_cents}"
                )
        if result.grand_total_cents is not None:
            if result.subtotal_cents is not None:
                expected_grand = (
                    result.subtotal_cents
                    + (result.tax_cents or 0)
                    + (result.freight_cents or 0)
                    + (result.misc_cents or 0)
                )
                if expected_grand != result.grand_total_cents:
                    flags.append(
                        f"subtotal+tax+freight+misc {expected_grand} != "
                        f"grand_total {result.grand_total_cents}"
                    )
            elif sum_extended is not None and sum_extended != result.grand_total_cents:
                flags.append(
                    f"Σextended {sum_extended} != grand_total {result.grand_total_cents}"
                )
    except Exception as exc:  # noqa: BLE001 — the gate NEVER raises; it flags
        flags.append(f"math check errored: {exc}")
        new_lines = new_lines or list(result.line_items)
    return dataclasses.replace(
        result, line_items=new_lines, math_ok=not flags, math_flags=flags
    )


# ---- Ladder-facing entry point + Worker payload -------------------------------------

# The Worker's parseExtraction bounds (safety_portal/worker/po_estimates.ts) this
# converter must respect: lines ≤ 500, anomalies text ≤ 4000, payload_json ≤ 400k.
WORKER_SCHEMA_VERSION = "1.0.0"
MAX_WORKER_LINES = 500
_MAX_WORKER_ANOMALIES_CHARS = 4000


def to_worker_payload(result: ExtractionResult) -> dict[str, Any]:
    """Convert an ExtractionResult into the Worker result-route extraction body
    (`parseExtraction` contract, minus `tier` — the daemon stamps that).

    Lines get 1-based positions; per-line and doc `math_ok` become the Worker's
    integer 0|1; the lane-internal advisory metadata (tier label, math/anomaly
    flags, needs_review, notes, NTE cap) rides `payload_json`; flags additionally
    surface in the `anomalies` text column (bounded).
    """
    lines: list[dict[str, Any]] = []
    for i, li in enumerate(result.line_items[:MAX_WORKER_LINES], start=1):
        lines.append(
            {
                "position": i,
                "section": li.section,
                "part_number": li.part_number,
                "description": li.description[:500],
                "qty": li.qty,
                "unit": li.unit,
                "unit_cost_cents": li.unit_cost_cents,
                "extended_cents": li.extended_cents,
                "math_ok": 1 if li.math_ok else 0,
                "line_note": li.line_note,
            }
        )
    advisory = {
        "tier_label": result.tier,
        "math_flags": result.math_flags,
        "anomaly_flags": result.anomaly_flags,
        "needs_review": result.needs_review,
        "notes": result.notes,
        "payment_terms": result.payment_terms,
        "not_to_exceed_cap_cents": result.not_to_exceed_cap_cents,
    }
    anomalies_text = "; ".join((*result.math_flags, *result.anomaly_flags))
    return {
        "schema_version": WORKER_SCHEMA_VERSION,
        "doc_type": result.doc_type,
        "vendor_name": result.vendor_name,
        "quote_number": result.quote_number,
        "revision_label": result.revision_label,
        "quote_date": result.quote_date,
        "valid_until": result.valid_until,
        "subtotal_cents": result.subtotal_cents,
        "tax_cents": result.tax_cents,
        "freight_cents": result.freight_cents,
        "misc_cents": result.misc_cents,
        "grand_total_cents": result.grand_total_cents,
        "math_ok": 1 if result.math_ok else 0,
        "confidence": max(0.0, min(1.0, float(result.confidence))),
        "payload_json": json.dumps(advisory, ensure_ascii=False, separators=(",", ":")),
        "anomalies": anomalies_text[:_MAX_WORKER_ANOMALIES_CHARS] or None,
        "lines": lines,
    }


def parse_estimate(
    pages: list[str], *, filename: str = "", data: bytes | None = None
) -> dict[str, Any] | None:
    """Tier-1 entry point for the `estimate_poll` ladder (the documented sibling
    contract: `parse_estimate(pages, *, filename) -> dict | None`, Worker
    extraction-body shape; the daemon stamps tier=1).

    Runs the vendor templates over the page text (first claiming template wins),
    then — when the caller also passes the raw `data` bytes — the generic-table
    tier over the sandbox-parsed tables (text alone carries no table geometry).
    Returns None when no deterministic tier extracts a line (the ladder falls
    through to Tier-2). `filename` is accepted for the call contract but UNUSED
    by design: identity is body-derived, filenames lie (ADR-0004 decision 7).
    Never raises on hostile text.
    """
    del filename  # interface compat only — see docstring
    if not pages or not any(p.strip() for p in pages):
        return None
    parsed = ParsedPdf(
        pages_text=list(pages),
        words=[[] for _ in pages],
        tables=[[] for _ in pages],
        chars_per_page=[len(p) for p in pages],
        is_scanned=False,
    )
    if data is not None:
        native = parse_native(data)
        if native is not None:
            parsed = native  # richer: word/table geometry enables the generic tier

    result: ExtractionResult | None = None
    for tpl in load_vendor_templates():
        result = parse_with_template(parsed, tpl)
        if result is not None:
            break
    if result is None:
        result = parse_generic_table(parsed)
    if result is None:
        return None
    return to_worker_payload(result)
