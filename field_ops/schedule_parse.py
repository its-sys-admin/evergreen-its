"""Semantic schedule extraction over reconstructed rows (ADR-0006, pure — no I/O, no AI).

Purpose
-------
`field_ops.schedule_geometry` hands back visual ROWS of OCR line items; this module
extracts what each row MEANS: which tokens are the task name, the dates, a duration,
a percent, predecessors — and classifies each row (project / section / task /
milestone), carries phase-section context, normalizes dates to ISO, and attaches the
consistency flags that point the human validate screen at likely OCR misreads
(which arrive at confidence 1.0 — ADR-0006 decision 2: every automated check here
verifies internal consistency, never fidelity; the human side-by-side is the only
fidelity control).

Extraction is TOKEN-PATTERN driven, not column-band driven, because Vision merges
spatially-close tokens into one observation ("09/06/26 10/05/26 0%" rides as a
single item) — the same row can arrive as one, two, or five items depending on
render scale. Patterns are stable where bands are not. The forked-and-renamed
lesson of `po_materials/estimate_parse.py` applies: the SHAPE of that module's
inference (ordered vocabulary, first-match-wins, flags-never-raises) transfers;
its money vocabulary and arithmetic do not (ADR-0006 §Decisions).

Output contract
---------------
`ParsedSchedule.rows` is the grid the daemon posts to `job_schedule_rows`
(cells_json + kind + flags per the 0066 CHECK); `column_map` / `header_meta` /
`notes` ride the result post. Cells are the CANONICAL columns in fixed order
(`CANONICAL_COLUMNS`), so the validate screen and the PR-4 commit key on stable
indices regardless of export style.

Invariants
----------
    * PURE — no I/O, no network, no AI, no send capability (enforced by
      tests/test_capability_gating.py). Upstream of it sits Invariant-2 handling
      (sandbox decode + parent validation); this module adds no decode surface.
    * §4 no-invented-field-data: every automated check verifies internal
      CONSISTENCY, never fidelity — a shredded date is kept verbatim +
      flagged, never guessed; a percent is never synthesized; the human
      side-by-side is the only fidelity control (ADR-0004 red-team #2,
      inherited).
    * Flags, never raises: malformed content degrades to flagged cells.
    * Two-digit years pivot per Python's %y (1969–2068). Corpus dates run
      2025–2027; the implausible-year flag (outside PLAUSIBLE_YEARS) catches the
      OCR digit-misread class ('12/01/25' → '72/01/25') long before the pivot
      could matter.

Failure modes
-------------
Never raises on any row content — unclassifiable tokens either extend the task
name (long tokens) or drop (short debris); rows with no dates flag
`missing_dates`; a document with zero reconstructable rows parses to an empty
proposal with profile 'unknown'. All degradation is visible in flags/notes.

Consumers
---------
The PR-3 `field_ops.schedule_poll` daemon (result-post payload), and the PR-4
validate screen via the D1 grid it produces.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from field_ops.schedule_geometry import PageRows, RowWords

# The canonical grid columns, in stored order. `phase` is the section context the
# parser CARRIES (not a source column in Gantt-view exports); `percent_bars` is the
# Gantt-region % label harvested as separate evidence from an in-table % token.
CANONICAL_COLUMNS = (
    "row_number",
    "task_name",
    "duration",
    "start_date",
    "finish_date",
    "percent_done",
    "predecessors",
    "phase",
)
_COL_INDEX = {name: i for i, name in enumerate(CANONICAL_COLUMNS)}

ROW_KIND_HEADER = "header"
ROW_KIND_DATA = "data"
ROW_KIND_SECTION = "section"
ROW_KIND_META = "meta"

# Consistency-flag vocabulary (comma-joined into job_schedule_rows.flags; the
# validate screen's badge copy keys on these).
FLAG_IMPLAUSIBLE_YEAR = "implausible_year"
FLAG_UNPARSEABLE_DATE = "unparseable_date"
FLAG_FINISH_BEFORE_START = "finish_before_start"
FLAG_PERCENT_OUT_OF_RANGE = "percent_out_of_range"
FLAG_MISSING_DATES = "missing_dates"
FLAG_PERCENT_CONFLICT = "percent_conflict"
FLAG_LOW_CONFIDENCE = "low_confidence"

# A schedule date is plausible in this year window; outside it the token is far more
# likely an OCR digit misread than a real date (the corpus runs 2025–2027).
PLAUSIBLE_YEARS = range(2020, 2036)

_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$")
# OCR date debris: a token that is clearly a shredded M/D/Y — mostly digits with
# slash/pipe/l/O substitutions ('12125125', '12125/25', '07107/26'). Kept as a date
# so the row's dates stay positional, flagged so the human fixes it.
_DATE_DEBRIS_RE = re.compile(r"^[\dloO|/]{6,10}$")
_DURATION_RE = re.compile(r"^(\d{1,4})d$")
_PERCENT_RE = re.compile(r"^(\d{1,3})%$")
_ROW_NUMBER_RE = re.compile(r"^\d{1,3}$")
_PREDECESSOR_RE = re.compile(r"^\d{1,3}(FS|SS|FF|SF)?([+-]\d{1,3}d)?,?$")
# A standalone lag token ('-30d', '+10d') continues a predecessor list the row has
# already started ('2, 3FS -30d' arrives word-split).
_PRED_LAG_RE = re.compile(r"^[+-]\d{1,3}d$")
# Junk a token sheds before classification — bar-edge / bullet / expander debris the
# OCR picks up around real tokens.
_TOKEN_TRIM = "|°*_—–·,"
# Leading glyphs that mark a GROUP row (the export's expand/collapse affordance).
_GROUP_GLYPHS = ("-", "•", "▪", "■", "□", "◦", "·")

# The phase vocabulary the corpus exports nest tasks under (the 'Filter By List'
# tags in grid-view; the group rows in Gantt-view). Used for section detection AND
# the is_delivery proposal.
PHASE_SECTIONS = frozenset(
    {"deliveries", "civil", "mechanical", "electrical", "engineering", "commissioning"}
)
_DELIVERY_SECTION = "deliveries"

_MONTHS_JUNK = frozenset(
    {"jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
     "q1", "q2", "q3", "q4"}
)


@dataclass(frozen=True)
class ParsedScheduleRow:
    """One grid row, ready for the D1 rows post."""

    index: int  # 1-based, whole-document, stable
    kind: str  # header | data | section | meta (0066 CHECK vocabulary)
    cells: list[str]  # CANONICAL_COLUMNS order
    flags: list[str]
    source_page: str  # 'pdf:p1'
    is_milestone: bool
    is_delivery: bool


@dataclass(frozen=True)
class ParsedSchedule:
    """The whole document's proposal."""

    profile: str  # 'gantt_export' | 'grid_export' | 'unknown'
    rows: list[ParsedScheduleRow]
    column_map: dict[str, object]  # {'mapping': concept→index, 'labels': {}}
    meta: dict[str, str]  # title / project_name when found
    notes: list[str]


@dataclass
class _RowScratch:
    row_number: str = ""
    name_parts: list[str] = field(default_factory=list)
    sub_section_parts: list[str] = field(default_factory=list)  # grid-view project/sub-section column
    name_indent: float | None = None  # C-start of the first NAME token (not the row number)
    duration: str = ""
    dates: list[str] = field(default_factory=list)
    percent: str = ""
    predecessors: list[str] = field(default_factory=list)
    phase_tag: str = ""
    flags: list[str] = field(default_factory=list)
    group_glyph: bool = False


@dataclass(frozen=True)
class _Bands:
    """Header-band cut points for a page — None where the export lacks the band.

    With the text-layer tier's precise per-word positions these make column
    attribution exact; with Vision's merged line items they still separate the
    grid-view's project/sub-section prefix from the task name (items start inside
    their own column even when their tails merge)."""

    name_start: float | None  # 'task name' band C-start
    filter_band: tuple[float, float] | None  # 'filter by list' band

    @staticmethod
    def from_page(page: PageRows) -> _Bands:
        name_start = None
        filter_band = None
        for label, band in zip(page.header_labels, page.header_bands, strict=True):
            if label == "task name":
                name_start = band[0]
            elif label == "filter by list":
                filter_band = band
        return _Bands(name_start=name_start, filter_band=filter_band)


def _classify_tokens(
    row: RowWords, *, has_predecessor_column: bool, bands: _Bands
) -> _RowScratch:
    """Walk the row's tokens in reading order, claiming each by pattern.

    Load-bearing ordering rules, each earned against the corpus:
    * A percent BEFORE any date is part of the TASK NAME — the design-milestone
      tasks are literally named 'Electrical 30%' / 'Civil 90%'. Only a percent
      after the dates (grid-view's % Done column) or the chart-region harvest is
      a completion value.
    * Plain-digit tokens after the dates are predecessor references ONLY when the
      page actually has a Predecessors column (grid-view) — in Gantt-view they are
      bar-edge debris ('|' read as '1') and are dropped.
    * The row number is position 1 only; the NAME indent is measured from the
      first name token so a row number's left-margin position can't masquerade as
      shallow (project-level) indentation.
    """
    s = _RowScratch()
    position = 0
    for item in row.words:
        in_filter_band = (
            bands.filter_band is not None
            and bands.filter_band[0] - 0.01 <= item.c0 <= bands.filter_band[1] + 0.03
        )
        before_name_band = (
            bands.name_start is not None and item.c1 < bands.name_start + 0.005
        )
        for raw in item.text.split():
            stripped = raw.strip(_TOKEN_TRIM)
            token = stripped
            if not token:
                # The one meaningful all-trim token: the grid-view sub-section's
                # '_' phase delimiter ('Deep Lake _ Deliveries').
                if before_name_band and raw.strip() == "_":
                    s.sub_section_parts.append("_")
                continue
            position += 1
            if position <= 2 and raw in _GROUP_GLYPHS:
                s.group_glyph = True
                continue
            # A glyph FUSED to the name ('-Civil') still marks a group row.
            if not s.name_parts and len(raw) > 1 and raw[0] in _GROUP_GLYPHS and raw[1].isalpha():
                s.group_glyph = True
                token = raw[1:]
            if _ROW_NUMBER_RE.match(token) and position == 1:
                s.row_number = token
                continue
            # Grid-view 'Filter By List' tags ('LNTP Work', 'MC', 'SC') — claimed by
            # BAND, because half the vocabulary ('Work', 'MC') matches no pattern
            # and would otherwise pollute the task name.
            if in_filter_band and not _DATE_RE.match(token):
                s.phase_tag = f"{s.phase_tag} {token}".strip() if s.phase_tag else token
                continue
            # Grid-view 'Project Name & Sub-Section' prefix — left of the Task Name
            # band, it is nesting context, never part of the task's own name. The
            # RAW token is kept: the '_' separator ('Deep Lake _ Deliveries') is
            # the phase delimiter and must survive the trim.
            if before_name_band and not s.dates and not _DATE_RE.match(token):
                s.sub_section_parts.append(raw)
                continue
            if _DATE_RE.match(token):
                s.dates.append(token)
                continue
            m = _DURATION_RE.match(token)
            if m and not s.dates:
                s.duration = token
                continue
            m = _PERCENT_RE.match(token)
            if m and s.dates:
                s.percent = m.group(1)
                continue
            if s.dates and _PREDECESSOR_RE.match(token):
                if has_predecessor_column:
                    s.predecessors.append(token.rstrip(","))
                continue  # Gantt-view: bar-edge debris, dropped
            if s.dates and s.predecessors and _PRED_LAG_RE.match(token):
                s.predecessors[-1] = f"{s.predecessors[-1]} {token}"
                continue
            if s.dates and token.lower() in PHASE_SECTIONS:
                s.phase_tag = token
                continue
            if s.dates and token.lower() in _MONTHS_JUNK:
                continue  # chart-band debris that leaked into the table region
            digits = sum(ch.isdigit() for ch in token)
            if _DATE_DEBRIS_RE.match(token) and digits >= 4:
                s.dates.append(token)  # a date the OCR shredded — kept, flagged later
                s.flags.append(FLAG_UNPARSEABLE_DATE)
                continue
            if not s.dates:
                if s.name_indent is None:
                    s.name_indent = item.c0
                s.name_parts.append(token if s.group_glyph and not s.name_parts else raw)
            # Tokens after the dates that match nothing are dropped only if short
            # debris; otherwise they extend the name (a parenthetical tail Vision
            # split off rides the same row).
            elif len(token) > 3:
                s.name_parts.append(raw)
    return s


def _normalize_date(token: str, flags: list[str]) -> str:
    """'M/D/YY' → ISO, flagging what fails; the raw token is preserved on failure so
    the human sees exactly what the OCR saw."""
    m = _DATE_RE.match(token)
    if not m:
        flags.append(FLAG_UNPARSEABLE_DATE)
        return token
    month, day, year = m.groups()
    fmt = "%m/%d/%Y" if len(year) == 4 else "%m/%d/%y"
    try:
        parsed = datetime.strptime(f"{month}/{day}/{year}", fmt).date()
    except ValueError:
        flags.append(FLAG_UNPARSEABLE_DATE)
        return token
    if parsed.year not in PLAUSIBLE_YEARS:
        flags.append(FLAG_IMPLAUSIBLE_YEAR)
    return parsed.isoformat()


def _finalize_row(
    s: _RowScratch,
    row: RowWords,
    *,
    index: int,
    page_label: str,
    section: str,
) -> ParsedScheduleRow:
    flags = list(s.flags)
    start_iso = finish_iso = ""
    if len(s.dates) >= 2:
        start_iso = _normalize_date(s.dates[0], flags)
        finish_iso = _normalize_date(s.dates[1], flags)
        if (
            len(start_iso) == 10
            and len(finish_iso) == 10
            and finish_iso < start_iso
        ):
            flags.append(FLAG_FINISH_BEFORE_START)
    elif len(s.dates) == 1:
        start_iso = _normalize_date(s.dates[0], flags)
        flags.append(FLAG_MISSING_DATES)

    percent = s.percent
    if row.gantt_percent is not None:
        if not percent:
            percent = row.gantt_percent
        elif percent != row.gantt_percent:
            flags.append(FLAG_PERCENT_CONFLICT)
    # Range-check the FINAL value (the chart-region harvest can shred too — a bar
    # edge fused to a label reads '195%').
    if percent and not 0 <= int(percent) <= 100:
        flags.append(FLAG_PERCENT_OUT_OF_RANGE)
    if row.min_conf < 0.5:
        flags.append(FLAG_LOW_CONFIDENCE)

    name = " ".join(s.name_parts).strip().strip(_TOKEN_TRIM).strip()
    lowered = name.lower()
    sub_section = " ".join(s.sub_section_parts).strip()
    # Grid-view nesting context: 'Deep Lake _ Deliveries' → the part after the
    # separator is the phase; a bare project name carries no phase.
    sub_phase = ""
    if "_" in sub_section:
        sub_phase = sub_section.rsplit("_", 1)[-1].strip()

    # Kind: group-glyph rows and bare phase names are structure, not tasks. A
    # grid-view summary row puts its label in the SUB-SECTION column with an empty
    # task name — also structure. (No indent heuristics — a row number's
    # left-margin position poisons them.)
    kind = ROW_KIND_DATA
    if not name and not s.dates and not sub_section:
        kind = ROW_KIND_META
    elif s.group_glyph or lowered in PHASE_SECTIONS:
        kind = ROW_KIND_SECTION
    elif not name and sub_section:
        kind = ROW_KIND_SECTION
        name = sub_phase or sub_section

    is_milestone = bool(
        kind == ROW_KIND_DATA
        and start_iso
        and finish_iso
        and start_iso == finish_iso
    ) or s.duration in ("0d", "1d")
    is_delivery = kind == ROW_KIND_DATA and (
        section.lower() == _DELIVERY_SECTION
        or sub_phase.lower() == _DELIVERY_SECTION
        or lowered.endswith("delivery")
    )

    if len(s.dates) == 0 and kind == ROW_KIND_DATA:
        flags.append(FLAG_MISSING_DATES)

    cells = ["" for _ in CANONICAL_COLUMNS]
    cells[_COL_INDEX["row_number"]] = s.row_number
    cells[_COL_INDEX["task_name"]] = name
    cells[_COL_INDEX["duration"]] = s.duration
    cells[_COL_INDEX["start_date"]] = start_iso
    cells[_COL_INDEX["finish_date"]] = finish_iso
    cells[_COL_INDEX["percent_done"]] = percent
    cells[_COL_INDEX["predecessors"]] = " ".join(s.predecessors)
    cells[_COL_INDEX["phase"]] = s.phase_tag or sub_phase or (
        section if kind == ROW_KIND_DATA else ""
    )

    return ParsedScheduleRow(
        index=index,
        kind=kind,
        cells=cells,
        flags=sorted(set(flags)),
        source_page=page_label,
        is_milestone=is_milestone,
        is_delivery=is_delivery,
    )


def parse_schedule(pages: list[PageRows]) -> ParsedSchedule:
    """The document-level parse: geometry pages in, the reviewable proposal out."""
    notes: list[str] = []
    meta: dict[str, str] = {}
    rows: list[ParsedScheduleRow] = []
    section = ""
    saw_duration = False
    index = 0
    # Plain-digit predecessor refs are believed only when SOME page carries the
    # grid-view Predecessors header; Gantt-view digit debris is dropped instead.
    has_preds = any("predecessors" in p.header_labels for p in pages)

    for page_no, page in enumerate(pages, start=1):
        page_label = f"pdf:p{page_no}"
        for note in page.notes:
            notes.append(f"p{page_no}:{note}")
        if page.orientation == "rotated":
            # The OCR child's ladder should have rotated the bitmap upright; a
            # rotated aspect-vote surviving to here means it failed.
            notes.append(f"p{page_no}:rotation_ladder_missed")
        if page_no == 1 and page.pre_header_texts:
            # The pre-header band holds the title line AND the letterhead logo text;
            # prefer the line that names the document.
            title = next(
                (t for t in page.pre_header_texts if t.lower().startswith("project")),
                page.pre_header_texts[0],
            )
            meta["title"] = title[:200]
        bands = _Bands.from_page(page)
        for raw_row in page.rows:
            scratch = _classify_tokens(raw_row, has_predecessor_column=has_preds, bands=bands)
            index += 1
            parsed = _finalize_row(
                scratch, raw_row,
                index=index, page_label=page_label,
                section=section,
            )
            if parsed.kind == ROW_KIND_SECTION:
                name = parsed.cells[_COL_INDEX["task_name"]]
                if name.lower() in PHASE_SECTIONS:
                    section = name
                elif index == 1 and name:
                    meta.setdefault("project_name", name)
            if parsed.cells[_COL_INDEX["duration"]]:
                saw_duration = True
            rows.append(parsed)

    profile = "unknown"
    if rows:
        profile = "grid_export" if saw_duration else "gantt_export"

    column_map: dict[str, object] = {
        "mapping": dict(_COL_INDEX),
        "labels": {},
    }
    return ParsedSchedule(profile=profile, rows=rows, column_map=column_map, meta=meta, notes=notes)
