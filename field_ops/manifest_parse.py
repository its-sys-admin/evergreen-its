"""Deterministic parsing of materials MANIFEST documents (BOMs + shipping logs) into a
normalized grid, a PROPOSED column map, and a document profile.

NO AI, no network, no I/O: every function here is pure over cell grids that a caller has already
extracted (the sandboxed pdfplumber / openpyxl children in ``po_materials.estimate_sandbox``).
Hostile bytes never reach this module.

WHY THIS EMITS A GRID + A PROPOSAL, NOT LINES
---------------------------------------------
The vendor-estimate lane can collapse a document straight to line items because its output schema
is fixed. Here the SHAPE is the judgement. A DELTA BOM carries seven ``QUANTITY`` columns (one per
product code) plus ``OVERAGE``/``REV 2``/``REV 1``/``DELTA``, and which of them is "the expected
quantity" is a domain call. If this module picked one and emitted lines, the human validate step
could only rubber-stamp it. So it proposes, and the admin disposes.

The proposal is still evidence-backed rather than a guess — see ``delta_arithmetic_check``.

FOUR SHAPES, FOUR TRAPS (each observed in real Evergreen documents, each with a test)
------------------------------------------------------------------------------------
1. **Customer BOM (PDF).** ``PART NUMBER | DESCRIPTION | GROUPING | QTY | UoM | WEIGHT…``. The
   header appears on **page 1 ONLY**; pages 2-3 are headerless data continuations. A per-table
   column-inference reset would silently drop ~60% of the document, so the map is established once
   and CARRIED (``parse_manifest`` threads it across grids).
2. **Customer BOM (XLSX).** Title rows first, a wholly-empty row, then the header on **row 4** —
   so a fixed offset is wrong and the header must be SCANNED for. Part numbers arrive as ints
   (``7006955``), so normalization must not render ``7006955.0``.
3. **DELTA BOM (PDF).** A metadata block in its own table (``CLIENT``/``PROJECT NAME``/…, plus the
   ``PRODUCT CODE`` row that LABELS the otherwise-indistinguishable ``QUANTITY`` columns), then the
   line table. Duplicate header names mean the map must be POSITIONAL, not name-keyed.
4. **Shipping log (XLSX).** One row per truckload: when a part ships in several loads, rows 2..n of
   the group blank their identity columns and are CONTINUATIONS of the row above. Both sample logs
   also over-declare their size wildly — the Deep Lake sheet reports 92 columns × 1,247 rows and
   really holds 12 columns × 57 non-empty rows (51 parts, 5 continuations) — so width and length
   both come from actual content, never from the sheet's own claim.

Two things this module deliberately does NOT do:

*It does not flag "ragged" rows.* A cell-count heuristic fires constantly on legitimate sparseness
(a line that has not shipped yet has no ship date, delivery date or BOL — 13 and 15 false flags on
the two real logs) and still misses the case that motivated it: the observed Customer-BOM row whose
truncated description bled a stray ``3`` into the GROUPING cell has every cell populated. The
content is wrong, not missing. Only a precisely-definable problem (an unparseable quantity) is
flagged; the grid reaches the human intact.

*It does not merge by part number.* Duplicates are universal — one part appears twice in three of
the four sample BOMs, under different groupings — so collapsing them here would silently pick a
winner. That is the validate page's explicit, per-row decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── Concepts the importer understands ─────────────────────────────────────────────────────────
# Ordered: earlier concepts claim a column first (the estimate lane's first-match-claims idiom),
# so a sheet carrying both "Part Number" and "Item" resolves the more specific one.
PART_NUMBER = "part_number"
DESCRIPTION = "description"
QTY = "qty"
UNIT = "unit"
CATEGORY = "category"
SHIP_DATE = "ship_date"
DELIVERY_DATE = "delivery_date"
BOL = "bol"
SHIPPED_QTY = "shipped_qty"
REQUIRED_DATE = "required_date"
REQUIRED_SHIP_DATE = "required_ship_date"
LOCATION = "location"
PROJECT = "project"
JOB_NUMBER = "job_number"
RELEASE = "release"
WEIGHT = "weight"
IGNORE = "ignore"

# Header-token → concept. Matched against a NORMALIZED header cell (lowercased, punctuation and
# runs of whitespace collapsed) — the Deep Lake log's "Delivery Day " has a trailing space, and
# "Job Req'd Qty" carries an apostrophe.
_HEADER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Dates before quantities: "required date" must not be claimed by the "required" qty rule.
    #
    # REQUIREMENT dates are deliberately DISTINCT from ACTUAL ship/delivery dates. The Kiwi log
    # carries both — "Job Ship By" / "Last Need By" (when it MUST move) alongside "Ship Date" /
    # "Delivery Date" (when it DID). Folding the first pair into ship/delivery would let them claim
    # those concepts first and silently import a deadline as though it were a real shipment date.
    (REQUIRED_SHIP_DATE, re.compile(r"^(job ship by|ship by|required ship date)$")),
    (REQUIRED_DATE, re.compile(r"^(required date|last need by|need by|due date)$")),
    (SHIP_DATE, re.compile(r"^(ship date|shipped date|date shipped)$")),
    (DELIVERY_DATE, re.compile(r"^(delivery day|delivery date|arrival date|date delivered)$")),
    (BOL, re.compile(r"^(bol|ld#|ld number|load#|load number|bill of lading)$")),
    (SHIPPED_QTY, re.compile(r"^(shipped qty|issued qty|qty shipped)$")),
    (PART_NUMBER, re.compile(r"^(part number|part num|part#|part no|item number|item#|item)$")),
    (DESCRIPTION, re.compile(r"^(description|part description|item description)$")),
    (CATEGORY, re.compile(r"^(bom category|grouping|category|group)$")),
    (UNIT, re.compile(r"^(uom|unit of measure|unit|u of m)$")),
    (WEIGHT, re.compile(r"^weight.*$")),
    (LOCATION, re.compile(r"^(location|site|warehouse)$")),
    (PROJECT, re.compile(r"^(project name|project|name)$")),
    (JOB_NUMBER, re.compile(r"^(job number|jobnum|job no|job#|document number|project id)$")),
    (RELEASE, re.compile(r"^(release|rel)$")),
    # Quantity LAST among the qty-ish rules so the more specific ones claim first.
    (QTY, re.compile(r"^(total qty|qty|quantity|required qty|job req'?d qty|ordered qty)$")),
)

# DELTA-BOM revision columns, matched separately: they are quantity CANDIDATES, not the qty.
_REV_RE = re.compile(r"^rev ?\d+$")
_OVERAGE_RE = re.compile(r"^overage$")
_DELTA_RE = re.compile(r"^delta$")

# Row/flag vocabulary
KIND_HEADER = "header"
KIND_DATA = "data"
KIND_CONTINUATION = "continuation"
KIND_SECTION = "section"
KIND_META = "meta"

# (a generic ragged-row flag was deliberately removed — see classify_rows)
FLAG_ORPHAN_CONTINUATION = "orphan_continuation"
FLAG_NO_QTY = "qty_unparseable"

# Profiles
PROFILE_CUSTOMER_BOM = "customer_bom"
PROFILE_DELTA_BOM = "delta_bom"
PROFILE_MASTER_BOM = "master_bom"
PROFILE_SHIPPING_LOG = "shipping_log"
PROFILE_GENERIC = "generic"
PROFILE_UNKNOWN = "unknown"

# A header row must match at least this many known tokens. Two is too loose — a data row whose
# description happens to read "Description" would qualify.
MIN_HEADER_HITS = 3
# How far into a grid to look for the header before giving up (Roxbury's is on row 4).
MAX_HEADER_SCAN_ROWS = 12


def normalize_cell(value: object) -> str:
    """A cell as TEXT, without float artifacts.

    openpyxl hands back ints/floats/datetimes; a part number read as ``7006955`` must not become
    ``"7006955.0"``, which would fail every downstream part-number match. Datetimes render as
    ISO dates because that is what the expected-materials schema stores.
    """
    if value is None:
        return ""
    if isinstance(value, bool):  # bool before int — bool IS an int in Python
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value)
    # datetime/date repr → ISO date ("2026-06-26 00:00:00" → "2026-06-26")
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[ T]00:00:00(\.\d+)?$", text)
    if m:
        return m.group(1)
    return text.strip()


def _norm_header(text: str) -> str:
    """Header cell → its matching key: lowercased, punctuation stripped, whitespace collapsed."""
    t = text.lower().strip()
    t = re.sub(r"[.:()\[\]]", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def trim_width(rows: list[list[str]]) -> list[list[str]]:
    """Trim a grid to its widest row of ACTUAL content.

    openpyxl's ``max_column`` over-reports badly — the Deep Lake shipping log declares 92 columns
    and really has 12 — and a parser that trusts it emits 80 columns of empty cells for the human
    to wade through. Rows are then padded to that one width so every row is indexable.
    """
    width = 0
    for row in rows:
        last = 0
        for i, cell in enumerate(row):
            if cell.strip():
                last = i + 1
        width = max(width, last)
    return [list(r[:width]) + [""] * max(0, width - len(r[:width])) for r in rows]


@dataclass(frozen=True)
class ColumnMap:
    """A PROPOSED concept→column-index mapping, plus per-column display labels.

    ``mapping`` is index-keyed by concept. ``labels`` names columns the header alone cannot
    distinguish — a DELTA BOM's seven identical ``QUANTITY`` headers get their product codes here,
    so the validate screen can offer "QUANTITY — 1P-PER-108M-15F-G90" rather than "QUANTITY #4".
    ``qty_candidates`` lists every column that could plausibly BE the quantity, in the order the
    UI should offer them; ``qty_default`` is the evidence-backed pre-selection.
    """

    mapping: dict[str, int] = field(default_factory=dict)
    labels: dict[int, str] = field(default_factory=dict)
    qty_candidates: list[int] = field(default_factory=list)
    qty_default: int | None = None

    def index_of(self, concept: str) -> int | None:
        return self.mapping.get(concept)

    def is_empty(self) -> bool:
        return not self.mapping


@dataclass(frozen=True)
class ParsedRow:
    """One source row, verbatim, with its classification and any problems worth a human's eye."""

    index: int  # 1-based, whole-document — the stable address the validate page edits against
    cells: list[str]
    kind: str
    flags: list[str] = field(default_factory=list)
    source: str = ""  # e.g. 'xlsx:Export' / 'pdf:p2:t0' — provenance for the preview pane


@dataclass(frozen=True)
class ParsedManifest:
    profile: str
    column_map: ColumnMap
    rows: list[ParsedRow]
    meta: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def find_header_row(rows: list[list[str]]) -> tuple[int | None, int]:
    """Scan for the header row; return ``(index, hit_count)``.

    SCANNED, never assumed at row 0: the Roxbury Customer BOM puts two title rows and a wholly
    empty row above its header. Takes the best-scoring row in the scan window rather than the first
    qualifying one, so a title row that happens to contain one known token cannot win.
    """
    best_idx: int | None = None
    best_hits = 0
    for i, row in enumerate(rows[:MAX_HEADER_SCAN_ROWS]):
        hits = 0
        for cell in row:
            key = _norm_header(cell)
            if not key:
                continue
            if any(pat.match(key) for _c, pat in _HEADER_PATTERNS):
                hits += 1
            elif _REV_RE.match(key) or _OVERAGE_RE.match(key) or _DELTA_RE.match(key):
                hits += 1
        if hits > best_hits:
            best_idx, best_hits = i, hits
    if best_hits < MIN_HEADER_HITS:
        return None, best_hits
    return best_idx, best_hits


def infer_columns(header: list[str]) -> ColumnMap:
    """Header row → a proposed ColumnMap. First match claims a concept; later duplicates don't.

    Positional by construction — a DELTA BOM's seven ``QUANTITY`` headers are indistinguishable by
    name, so nothing here may key on the text alone.
    """
    mapping: dict[str, int] = {}
    labels: dict[int, str] = {}
    qty_candidates: list[int] = []
    rev_cols: dict[str, int] = {}

    for idx, raw in enumerate(header):
        key = _norm_header(raw)
        if not key:
            continue
        labels[idx] = raw.strip()
        claimed = False
        for concept, pat in _HEADER_PATTERNS:
            if pat.match(key) and concept not in mapping:
                mapping[concept] = idx
                claimed = True
                break
        # Every quantity-ish column is a CANDIDATE even when it didn't claim the qty concept —
        # on a DELTA BOM the real answer is usually REV 2, not the first bare QUANTITY column.
        # The column that DID claim `qty` is always a candidate too, whatever it was called
        # (the Kiwi log's is "Job Req'd Qty"), so the picker never renders empty.
        if (
            mapping.get(QTY) == idx
            or key in ("quantity", "qty", "total qty", "required qty", "ordered qty")
            or _REV_RE.match(key)
            or _OVERAGE_RE.match(key)
        ):
            if idx not in qty_candidates:
                qty_candidates.append(idx)
        if _REV_RE.match(key):
            rev_cols[key] = idx
        if not claimed and (_DELTA_RE.match(key) or _REV_RE.match(key) or _OVERAGE_RE.match(key)):
            continue

    # Evidence-backed default: on a revision BOM the highest REV column is the CURRENT quantity —
    # the earlier bare QUANTITY columns are per-product splits and OVERAGE is an adder, not a total.
    qty_default: int | None = mapping.get(QTY)
    if rev_cols:
        highest = max(rev_cols, key=lambda k: int(re.sub(r"\D", "", k) or 0))
        qty_default = rev_cols[highest]

    return ColumnMap(mapping=mapping, labels=labels, qty_candidates=qty_candidates, qty_default=qty_default)


def label_delta_quantity_columns(cmap: ColumnMap, product_codes: list[str]) -> ColumnMap:
    """Name a DELTA BOM's ``QUANTITY`` columns with their product codes.

    The codes live in a SEPARATE metadata table ("PRODUCT CODE" row), so they arrive out of band.
    Applied left-to-right across the bare quantity columns; extra codes are ignored rather than
    guessed at, and a short list simply leaves later columns unlabelled.
    """
    if not product_codes:
        return cmap
    bare = [i for i in cmap.qty_candidates if _norm_header(cmap.labels.get(i, "")) in ("quantity", "qty")]
    labels = dict(cmap.labels)
    # strict=False deliberately: a document may declare more product codes than it has bare
    # QUANTITY columns (or fewer). Extra codes are ignored rather than guessed at, and a short
    # list simply leaves later columns unlabelled — never a crash on a real-world mismatch.
    for col, code in zip(bare, product_codes, strict=False):
        labels[col] = f"{labels.get(col, 'QUANTITY')} — {code}"
    return ColumnMap(
        mapping=cmap.mapping, labels=labels,
        qty_candidates=cmap.qty_candidates, qty_default=cmap.qty_default,
    )


def parse_number(text: str) -> float | None:
    """A cell → a number, or None. Tolerates thousands separators; refuses anything else."""
    t = text.strip().replace(",", "")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def delta_arithmetic_check(rows: list[ParsedRow], cmap: ColumnMap) -> tuple[int, int]:
    """Verify a DELTA BOM's internal identity: ``Σ(bare QUANTITY) + OVERAGE == REV n``.

    Returns ``(agreeing_rows, checked_rows)``. This is what turns "which column is the quantity"
    from a blind pick into an evidence-backed default the validate screen can SHOW its working
    for. It is a CONSISTENCY signal, never a gate — a document that fails it still imports, with
    the disagreement surfaced.
    """
    bare = [i for i in cmap.qty_candidates if _norm_header(cmap.labels.get(i, "")).startswith(("quantity", "qty"))]
    overage = next(
        (i for i in cmap.qty_candidates if _OVERAGE_RE.match(_norm_header(cmap.labels.get(i, "")))), None
    )
    target = cmap.qty_default
    if target is None or not bare:
        return 0, 0
    agree = checked = 0
    for row in rows:
        if row.kind != KIND_DATA:
            continue
        want = parse_number(row.cells[target]) if target < len(row.cells) else None
        if want is None:
            continue
        total = 0.0
        for i in bare:
            total += parse_number(row.cells[i]) or 0.0 if i < len(row.cells) else 0.0
        if overage is not None and overage < len(row.cells):
            total += parse_number(row.cells[overage]) or 0.0
        checked += 1
        if abs(total - want) < 1e-6:
            agree += 1
    return agree, checked


def detect_profile(cmap: ColumnMap, header: list[str]) -> str:
    """Classify the document from its HEADER SHAPE, never its filename (filenames lie)."""
    keys = {_norm_header(h) for h in header if h.strip()}
    has = cmap.mapping.__contains__
    if any(_REV_RE.match(k) for k in keys) and has(PART_NUMBER):
        return PROFILE_DELTA_BOM
    if has(SHIP_DATE) or has(DELIVERY_DATE) or has(BOL):
        return PROFILE_SHIPPING_LOG
    if has(PART_NUMBER) and has(LOCATION) and has(JOB_NUMBER):
        return PROFILE_MASTER_BOM
    if has(PART_NUMBER) and has(QTY) and has(CATEGORY):
        return PROFILE_CUSTOMER_BOM
    if has(PART_NUMBER) and has(QTY):
        return PROFILE_GENERIC
    return PROFILE_UNKNOWN


def _identity_columns(cmap: ColumnMap) -> list[int]:
    """The columns a shipping-log continuation row leaves blank and inherits from its parent."""
    out = []
    for concept in (PART_NUMBER, DESCRIPTION, PROJECT, JOB_NUMBER, REQUIRED_DATE, QTY):
        idx = cmap.index_of(concept)
        if idx is not None:
            out.append(idx)
    return out


def classify_rows(
    grid: list[list[str]], cmap: ColumnMap, header_idx: int | None, *, profile: str, source: str, offset: int
) -> list[ParsedRow]:
    """Classify + flag each row, forward-filling shipping-log continuations.

    A row is a CONTINUATION when its part-number cell is empty but a parent has been seen: on the
    Deep Lake log every continuation blanks columns 0-7 entirely, so there is nothing to read and
    the identity is inherited. A continuation appearing BEFORE any parent is an orphan — flagged
    for the human, never silently attached to whatever came next.
    """
    part_col = cmap.index_of(PART_NUMBER)
    qty_col = cmap.qty_default if cmap.qty_default is not None else cmap.index_of(QTY)
    identity = _identity_columns(cmap)
    width = max((len(r) for r in grid), default=0)

    out: list[ParsedRow] = []
    parent: list[str] | None = None
    for i, raw in enumerate(grid):
        cells = list(raw) + [""] * max(0, width - len(raw))
        idx = offset + i + 1
        if header_idx is not None and i == header_idx:
            out.append(ParsedRow(index=idx, cells=cells, kind=KIND_HEADER, source=source))
            continue
        if not any(c.strip() for c in cells):
            continue  # wholly-empty row: structural padding, not content
        if header_idx is not None and i < header_idx:
            out.append(ParsedRow(index=idx, cells=cells, kind=KIND_META, source=source))
            continue

        flags: list[str] = []
        has_part = bool(part_col is not None and part_col < len(cells) and cells[part_col].strip())

        if profile == PROFILE_SHIPPING_LOG and part_col is not None and not has_part:
            if parent is None:
                flags.append(FLAG_ORPHAN_CONTINUATION)
                out.append(ParsedRow(index=idx, cells=cells, kind=KIND_CONTINUATION, flags=flags, source=source))
                continue
            filled = list(cells)
            for col in identity:
                if col < len(filled) and not filled[col].strip() and col < len(parent):
                    filled[col] = parent[col]
            out.append(ParsedRow(index=idx, cells=filled, kind=KIND_CONTINUATION, flags=flags, source=source))
            continue

        # A row with no parseable quantity is a SECTION LABEL only where such labels actually
        # occur (master/generic listings). On a Customer BOM every real line carries a qty, so a
        # missing one is a defect the human must see — not a row to quietly reclassify.
        qty_val = parse_number(cells[qty_col]) if qty_col is not None and qty_col < len(cells) else None
        if qty_val is None:
            if profile in (PROFILE_MASTER_BOM, PROFILE_GENERIC, PROFILE_UNKNOWN) and not has_part:
                out.append(ParsedRow(index=idx, cells=cells, kind=KIND_SECTION, source=source))
                continue
            flags.append(FLAG_NO_QTY)

        # NO generic "ragged row" flag. A cell-count heuristic fires constantly on legitimate
        # sparseness — a shipping-log line that has not shipped yet leaves its ship/delivery/BOL
        # cells empty by definition, and on the real Deep Lake and Kiwi logs that produced 13 and
        # 15 false flags respectively. Worse, it does not even catch its motivating example: the
        # observed Customer-BOM row whose truncated description bled a stray "3" into the GROUPING
        # cell has EVERY cell populated — the content is wrong, not missing. Noise that misses the
        # case it was written for is worse than nothing, so the grid goes to the human intact and
        # only the precisely-definable problem (an unparseable quantity) is flagged.

        if has_part:
            parent = cells
        out.append(ParsedRow(index=idx, cells=cells, kind=KIND_DATA, flags=flags, source=source))
    return out


def parse_manifest(grids: list[tuple[str, list[list[str]]]], *, product_codes: list[str] | None = None) -> ParsedManifest:
    """Parse a whole document from its extracted grids, in order.

    ``grids`` is ``[(source_label, rows), …]`` — one entry per sheet (xlsx) or per page-table (pdf).

    THE COLUMN MAP IS ESTABLISHED ONCE AND CARRIED. This is the Customer-BOM fix: its header is on
    page 1 only, and a per-grid re-inference would find no header on pages 2-3 and drop them.
    """
    notes: list[str] = []
    meta: dict[str, str] = {}
    cmap = ColumnMap()
    profile = PROFILE_UNKNOWN
    header_cells: list[str] = []
    rows: list[ParsedRow] = []
    offset = 0

    for source, raw_rows in grids:
        grid = trim_width([[normalize_cell(c) for c in r] for r in raw_rows])
        if not grid:
            continue

        local_header: int | None = None
        if cmap.is_empty():
            local_header, hits = find_header_row(grid)
            if local_header is not None:
                header_cells = grid[local_header]
                cmap = infer_columns(header_cells)
                profile = detect_profile(cmap, header_cells)
                notes.append(f"header found in {source} at row {local_header + 1} ({hits} known columns)")
            else:
                # No header yet and none here: a metadata table (the DELTA block) or a preamble.
                # Harvest label→value pairs so the validate screen can show CLIENT / PROJECT NAME.
                for r in grid:
                    if len(r) >= 2 and r[0].strip() and r[1].strip():
                        meta.setdefault(r[0].strip(), r[1].strip())
                rows.extend(
                    ParsedRow(index=offset + i + 1, cells=r, kind=KIND_META, source=source)
                    for i, r in enumerate(grid) if any(c.strip() for c in r)
                )
                offset += len(grid)
                continue
        else:
            # Map already known. A repeat of the SAME header (some exporters print it per page)
            # is skipped as a header row rather than imported as a line.
            first = grid[0] if grid else []
            if first and _norm_header(" ".join(first)) == _norm_header(" ".join(header_cells)):
                local_header = 0

        rows.extend(classify_rows(grid, cmap, local_header, profile=profile, source=source, offset=offset))
        offset += len(grid)

    if product_codes:
        cmap = label_delta_quantity_columns(cmap, product_codes)

    if profile == PROFILE_DELTA_BOM:
        agree, checked = delta_arithmetic_check(rows, cmap)
        if checked:
            notes.append(
                f"revision cross-check: sum(quantities)+overage equals the chosen revision on "
                f"{agree}/{checked} rows"
            )

    if cmap.is_empty():
        notes.append("no header row recognised — every column needs mapping by hand")

    return ParsedManifest(profile=profile, column_map=cmap, rows=rows, meta=meta, notes=notes)


def product_codes_from_meta(grids: list[tuple[str, list[list[str]]]]) -> list[str]:
    """Pull the DELTA BOM's ``PRODUCT CODE`` row out of its metadata table, if present."""
    for _source, raw_rows in grids:
        for raw in raw_rows:
            cells = [normalize_cell(c) for c in raw]
            if cells and _norm_header(cells[0]) == "product code":
                return [c for c in cells[1:] if c.strip()]
    return []
