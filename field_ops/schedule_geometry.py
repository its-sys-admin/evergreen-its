"""OCR word-geometry → schedule row reconstruction (ADR-0006, pure — no I/O, no AI).

Purpose
-------
The schedule corpus is Smartsheet Gantt PDF exports whose table text is VECTOR
OUTLINES — no text layer — so extraction is Apple Vision OCR
(`estimate_sandbox.ocr_page_words`), which returns a flat bag of text-line
observations with normalized boxes. This module turns that bag back into ROWS in
reading order, separates the structured table region from the Gantt chart region
(whose month labels and bar dates would otherwise pollute every row), and
harvests the chart's per-row percent labels as evidence.

What it deliberately does NOT do: assign words to rigid column cells. Vision
merges spatially-close tokens into one observation ("09/06/26 10/05/26 0%" rides
as a single line item), so cell-level banding is unreliable at exactly the
moments it matters. Rows carry their ordered words + positions; the SEMANTIC
extraction (which token is a date, a duration, a percent, the task name) is
`field_ops.schedule_parse`'s job, by pattern — robust to Vision's merging.

Coordinate model (measured against the real corpus, 2026-08-11)
---------------------------------------------------------------
`ocr_page_words` rotates the bitmap upright BEFORE the winning OCR pass (Vision
drops most 90°-rotated text — measured 64 vs 135 words on Coker p2 — and reads
180°-flipped text convincingly enough that only the 'Task Name'-position layout
invariant picks the true orientation). Words therefore arrive UPRIGHT in Vision
normalized coordinates, origin BOTTOM-LEFT:

    R (reading-down)  = 1 - (y + h) … 1 - y
    C (reading-right) = x … x + w

`words_from_pdfplumber` adapts the rare text-layer export's
`estimate_sandbox.parse_native` word dicts ({text, x0, x1, top, bottom},
absolute points, origin TOP-left) into the same shape, so one reconstruction
path serves both tiers. A residual aspect-vote (`detect_orientation`) rides
along as a diagnostic for the parse notes; reconstruction itself is upright-only
by contract.

Table-vs-Gantt split
--------------------
The header row (>= 2 known header tokens) seeds the split: the table region ends
just past the LAST known header label's box ('Completion Date' in the Gantt-view
exports; 'Contract Milestone' in grid-view). Words beyond it are chart clutter —
EXCEPT a bare percent token R-aligned with a row, which is that row's Gantt bar
label, harvested as `gantt_percent` (the only per-task % the Gantt-view exports
carry). Header labels that wrap onto a second text line ('Completion' / 'Date')
are absorbed so the wrapped fragment can't masquerade as a data row.

Invariants
----------
    * PURE — no I/O, no network, no AI, no send capability (enforced by
      tests/test_capability_gating.py). The words originate from untrusted bytes
      (Invariant 2), but by the time they reach here they are sandbox-laundered,
      parent-validated STRINGS + floats; this module adds no decode surface.
    * OCR confidence rides through as evidence for the validate screen and is
      NEVER a filter: the corpus shows digit misreads at confidence 1.0
      (ADR-0006 decision 2).
    * Output is bounded (MAX_ROWS_PER_PAGE) — a hostile word bag cannot
      fabricate an unbounded grid.

Failure modes
-------------
Never raises on malformed word dicts — a word missing coordinates is skipped; an
empty page reconstructs to an empty PageRows with an `empty_page` note; a page
with no recognizable header keeps every word (`no_header_row` note) so the parse
layer's patterns still get a shot. Degradation is always visible via `notes`.

Consumers
---------
`field_ops.schedule_parse` (concept inference over the reconstructed rows) and
the PR-3 `field_ops.schedule_poll` daemon.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from statistics import median
from typing import Any

# A word must be at least this long to vote on orientation — single glyphs
# ('=', '-', bullet dots) have near-square boxes and carry no signal.
_ORIENTATION_MIN_CHARS = 4
# Aspect margin for the orientation vote: decisively taller-than-wide votes
# rotated, decisively wider votes upright, borderline abstains.
_ORIENTATION_MARGIN = 1.5
# Row clustering: two words share a row when their R-centers sit within this
# fraction of the median word thickness.
_ROW_GAP_FACTOR = 0.75
# The Gantt region starts this far (in C units) past the last header label's end.
_GANTT_MARGIN = 0.02
# Bound on reconstructed rows per page (a hostile word bag cannot fabricate an
# unbounded grid; mirrors the Worker's row ceiling scale).
MAX_ROWS_PER_PAGE = 500

_PERCENT_RE = re.compile(r"^\d{1,3}%$")

# The header labels the corpus exports actually carry (both view styles), in the
# canonical spelling the parse layer keys on. Matched lowercase over Vision line
# items, longest-first so 'completion date' beats 'date'.
HEADER_LABELS = (
    "project name & sub-section",
    "completion date",
    "contract milestone",
    "filter by list",
    "start date",
    "task name",
    "predecessors",
    "duration",
    "% done",
)
# Wrapped second-line fragments of the labels above — a row made ONLY of these is
# the header's continuation, never data ('Completion' wraps over 'Date'; 'Contract'
# over 'Milestone'; 'Filter By' over 'List').
_HEADER_FRAGMENTS = frozenset(
    {"date", "name", "milestone", "list", "one", "sub-section", "done", "by"}
)
_HEADER_MIN_HITS = 2
_HEADER_SCAN_ROWS = 8


@dataclass(frozen=True)
class Word:
    """One OCR (or adapted pdfplumber) observation in logical coordinates.

    R runs down the page, C runs right. `conf` is evidence, never a gate.
    """

    text: str
    r0: float
    r1: float
    c0: float
    c1: float
    conf: float

    @property
    def r_center(self) -> float:
        return (self.r0 + self.r1) / 2.0


@dataclass(frozen=True)
class RowWords:
    """One reconstructed table row: its in-table words in reading order, plus the
    Gantt-region evidence the parse layer folds in."""

    words: list[Word]  # in-table observations, C-ordered
    row_band: tuple[float, float]
    indent: float  # C-start of the first in-table word (project/section/task nesting cue)
    gantt_percent: str | None  # bare % label R-aligned with this row in the chart region
    min_conf: float


@dataclass(frozen=True)
class PageRows:
    """One page's reconstruction."""

    header_labels: list[str]  # canonical labels found, in C order
    header_bands: list[tuple[float, float]]  # each label's C band
    rows: list[RowWords]  # data rows, reading order (header + its wrap excluded)
    pre_header_texts: list[str]  # text of rows ABOVE the header (the title line)
    orientation: str  # diagnostic aspect-vote of the RAW payload ('upright'|'rotated')
    notes: list[str]


def words_from_ocr(page_words: list[dict[str, Any]]) -> list[Word]:
    """Adapt one page of `ocr_page_words` output (upright bitmap, Vision
    normalized boxes, origin bottom-left) into logical-coordinate Words."""
    out: list[Word] = []
    for w in page_words:
        text = str(w.get("text", "")).strip()
        if not text:
            continue
        try:
            x = float(w["x"])
            y = float(w["y"])
            ww = float(w["w"])
            hh = float(w["h"])
            conf = float(w.get("conf", 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        out.append(Word(text=text, r0=1.0 - (y + hh), r1=1.0 - y, c0=x, c1=x + ww, conf=conf))
    out.sort(key=lambda w: (w.r_center, w.c0))
    return out


def words_from_pdfplumber(
    page_words: list[dict[str, Any]], *, page_width: float, page_height: float
) -> list[Word]:
    """Adapt `estimate_sandbox.parse_native` word dicts (absolute points, origin
    TOP-left) into normalized logical Words — the text-layer tier's entry."""
    if page_width <= 0 or page_height <= 0:
        return []
    out: list[Word] = []
    for w in page_words:
        text = str(w.get("text", "")).strip()
        if not text:
            continue
        try:
            x0 = float(w["x0"]) / page_width
            x1 = float(w["x1"]) / page_width
            top = float(w["top"]) / page_height
            bottom = float(w["bottom"]) / page_height
        except (KeyError, TypeError, ValueError):
            continue
        out.append(Word(text=text, r0=top, r1=bottom, c0=x0, c1=x1, conf=1.0))
    out.sort(key=lambda w: (w.r_center, w.c0))
    return out


def detect_orientation(page_words: list[dict[str, Any]]) -> str:
    """Aspect-vote over the RAW payload boxes — a diagnostic (the OCR child
    already rotated the bitmap upright; a 'rotated' vote HERE means the ladder
    failed and the parse notes should say so)."""
    votes_rotated = 0
    votes_upright = 0
    for w in page_words:
        text = str(w.get("text", "")).strip()
        if len(text) < _ORIENTATION_MIN_CHARS:
            continue
        try:
            ww = float(w["w"])
            hh = float(w["h"])
        except (KeyError, TypeError, ValueError):
            continue
        if ww <= 0 or hh <= 0:
            continue
        if hh >= ww * _ORIENTATION_MARGIN:
            votes_rotated += 1
        elif ww >= hh * _ORIENTATION_MARGIN:
            votes_upright += 1
    return "rotated" if votes_rotated > votes_upright else "upright"


# ---- Row clustering -----------------------------------------------------------------


def _cluster_rows(words: list[Word]) -> list[list[Word]]:
    """Group words into visual rows by R-center proximity; the gap threshold
    derives from the words' own median thickness, so it adapts to render scale."""
    if not words:
        return []
    thicknesses = [w.r1 - w.r0 for w in words if w.r1 > w.r0]
    gap = (median(thicknesses) if thicknesses else 0.01) * _ROW_GAP_FACTOR
    rows: list[list[Word]] = []
    current: list[Word] = []
    for w in sorted(words, key=lambda w: w.r_center):
        if not current:
            current = [w]
            continue
        anchor = median([cw.r_center for cw in current])
        if abs(w.r_center - anchor) <= gap:
            current.append(w)
        else:
            rows.append(sorted(current, key=lambda cw: cw.c0))
            current = [w]
        if len(rows) >= MAX_ROWS_PER_PAGE:
            break
    if current and len(rows) < MAX_ROWS_PER_PAGE:
        rows.append(sorted(current, key=lambda cw: cw.c0))
    return rows


# ---- Header detection ---------------------------------------------------------------


def _match_labels(row: list[Word]) -> list[tuple[str, float, float]]:
    """Known header labels present in a row: (canonical label, c0, c1) per hit.
    A Vision line item may hold several labels merged ('Start Date Completion') —
    each known label found inside an item claims the item's band once."""
    hits: list[tuple[str, float, float]] = []
    for w in row:
        lowered = " ".join(w.text.lower().split())
        for label in HEADER_LABELS:
            words = label.split()
            # Full label inside the item, OR the item IS the label's head with the
            # tail wrapped onto the next text line ('Completion' over 'Date').
            if label in lowered or (len(words) > 1 and lowered == words[0]):
                hits.append((label, w.c0, w.c1))
    # Deduplicate by label, keeping the leftmost claim; order by position.
    seen: dict[str, tuple[str, float, float]] = {}
    for h in hits:
        if h[0] not in seen or h[1] < seen[h[0]][1]:
            seen[h[0]] = h
    return sorted(seen.values(), key=lambda h: h[1])


def _find_header_row(rows: list[list[Word]]) -> tuple[int | None, list[tuple[str, float, float]]]:
    for idx, row in enumerate(rows[:_HEADER_SCAN_ROWS]):
        labels = _match_labels(row)
        if len(labels) >= _HEADER_MIN_HITS:
            return idx, labels
    return None, []


def _is_header_wrap(row: list[Word]) -> bool:
    """True when a row consists ONLY of wrapped header fragments ('Date', 'List',
    possibly Gantt month/quarter labels riding the same text line)."""
    saw_fragment = False
    for w in row:
        token = w.text.lower().strip()
        if token in _HEADER_FRAGMENTS:
            saw_fragment = True
            continue
        # Month / quarter labels share the header band in the Gantt-view exports.
        if re.fullmatch(r"(q[1-4]|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", token):
            continue
        if all(t.lower().strip() in _HEADER_FRAGMENTS or len(t) <= 3 for t in w.text.split()):
            saw_fragment = True
            continue
        return False
    return saw_fragment


def reconstruct_page(words: list[Word], *, orientation: str = "upright") -> PageRows:
    """The whole reconstruction for one page of logical-coordinate words."""
    notes: list[str] = []
    rows = _cluster_rows(words)
    if not rows:
        return PageRows(
            header_labels=[], header_bands=[], rows=[], pre_header_texts=[],
            orientation=orientation, notes=["empty_page"],
        )

    header_idx, labels = _find_header_row(rows)
    pre_header_texts: list[str] = []
    if header_idx is None:
        notes.append("no_header_row")
        table_end = 1.0  # headerless: keep everything; the parse layer's patterns cope
        header_labels: list[str] = []
        header_bands: list[tuple[float, float]] = []
        data_rows = rows
    else:
        header_labels = [label for label, _, _ in labels]
        header_bands = [(c0, c1) for _, c0, c1 in labels]
        table_end = labels[-1][2] + _GANTT_MARGIN
        pre_header_texts = [" ".join(w.text for w in r) for r in rows[:header_idx]]
        data_rows = rows[header_idx + 1 :]
        # Absorb wrapped header continuation line(s) so 'Date' can't be a data row.
        while data_rows and _is_header_wrap(data_rows[0]):
            data_rows = data_rows[1:]

    out_rows: list[RowWords] = []
    for row in data_rows:
        in_table = [w for w in row if w.c0 < table_end]
        gantt = [w for w in row if w.c0 >= table_end]
        if not in_table:
            continue  # a pure chart-region row (month band, bar labels) is not a table row
        pct = next((w.text for w in gantt if _PERCENT_RE.match(w.text.strip())), None)
        out_rows.append(
            RowWords(
                words=in_table,
                row_band=(min(w.r0 for w in in_table), max(w.r1 for w in in_table)),
                indent=min(w.c0 for w in in_table),
                gantt_percent=pct.rstrip("%") if pct else None,
                min_conf=min(w.conf for w in in_table),
            )
        )
    return PageRows(
        header_labels=header_labels,
        header_bands=header_bands,
        rows=out_rows,
        pre_header_texts=pre_header_texts,
        orientation=orientation,
        notes=notes,
    )


def reconstruct(pages: list[list[dict[str, Any]]]) -> list[PageRows]:
    """OCR payload pages → per-page reconstructions (the daemon's one call)."""
    return [
        reconstruct_page(words_from_ocr(page_words), orientation=detect_orientation(page_words))
        for page_words in pages
    ]


__all__ = [
    "HEADER_LABELS",
    "MAX_ROWS_PER_PAGE",
    "PageRows",
    "RowWords",
    "Word",
    "detect_orientation",
    "reconstruct",
    "reconstruct_page",
    "words_from_ocr",
    "words_from_pdfplumber",
]
