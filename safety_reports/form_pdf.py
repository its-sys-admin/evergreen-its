"""Render-parity PDF renderer for Safety Portal submissions (Phase 4 PR 3, Option B).

Purpose
-------
    Turn a structured portal submission + its form definition
    (`safety_portal/forms/<form_code>.json`) into a PDF that looks like the source
    paper form. The definition is the SINGLE source of truth shared with the TS
    display runtime (`safety_portal/src/forms/`), so the two renderers cannot drift.

Invariants
----------
    * Deterministic. NO AI, NO network, NO Smartsheet/Box/Graph — pure data → bytes.
    * Mandatory/legal text (`static_text`) is rendered verbatim from the definition,
      never user-editable (JHA "IF CONDITIONS CHANGE…"; equipment lock/tag-out).
    * N/A is rendered DISTINCT from blank (amendment 2026-06-05): a checklist item
      answered "N/A" prints "N/A"; an unanswered item prints an empty cell. Blank =
      not-yet-inspected (incomplete); N/A = deliberately-not-applicable (complete).
    * Obvious source typos are already corrected in the definitions (e.g. JHA
      "Crem"→"Crew", Skid Steer "WEARE"→"WEAR") — the renderer just prints the labels.

Failure modes
-------------
    An unknown section type or a malformed signature path is logged and skipped — the
    renderer never raises mid-document, so the rest of the PDF still renders. The
    caller (intake.py portal branch, Phase 5) is responsible for surfacing a
    render-degraded signal; `incomplete_checklist_items()` lets it flag blanks.

Consumers
---------
    Phase 5 intake renders on portal-submission arrival, then stores the PDF in Box
    ([Job]/[week of …]/). `weekly_generate` merges the per-submission PDFs Sat→Fri
    into the compiled weekly packet.

    PR-L (manual fallback): `render_blank_fillable(definition)` is the BLANK, fillable-
    AcroForm sibling of `render_submission_pdf` — same layout/branding/footer, empty
    fields where the submission renderer draws values. `render_cover_sheet()` renders
    the one manual-fallback instructions cover page. Both are pure bytes (no Box / send);
    `scripts/generate_form_archive.py` is the only thing that uploads them to Box.
"""
from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as _esc

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as _pdfcanvas
from reportlab.platypus import (
    Flowable,
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger(__name__)

# Signature-pad source coordinate space (SignaturePad.tsx viewBox 0 0 600 180).
_SIG_W, _SIG_H = 600.0, 180.0

# ── brand palette (2026-06-15 beautification) ──────────────────────────────────
# Greens echo the Evergreen logo's gradient stops (#00441b→#147e7f); gold is the
# requested section-divider accent. Kept as a small, named palette so every form
# (and the weekly packet) shares ONE visual system.
_BRG = colors.HexColor("#1f4d2e")   # deep evergreen — headlines / section titles
_BRG_SOFT = colors.HexColor("#3a5a40")  # softer green — group labels / field labels
_GOLD = colors.HexColor("#b8860b")  # divider rule + accents (operator-requested)
_INK = colors.HexColor("#1f2421")   # near-black body text (softer than pure black)
_LINE = colors.HexColor("#d7d9d6")  # light grid / hairline rules
_TINT = colors.HexColor("#e8f0e9")  # table header fill (pale green)
_ZEBRA = colors.HexColor("#f5f8f5")  # alternating table-row fill
_FOOT = colors.HexColor("#7a7f7b")  # footer text (muted grey-green)
# Response colours — improve scannability WITHOUT changing semantics. N/A stays a
# distinct word (never blank); blank renders empty as before.
_OK = colors.HexColor("#2c6e49")
_BAD = colors.HexColor("#b54708")
_NA = colors.HexColor("#6b6f6c")

# Usable content width (Letter minus the 0.6in side margins) — section rules and
# header bands span exactly this so everything lines up.
_MARGIN = 0.6 * inch
_CONTENT_W = letter[0] - 2 * _MARGIN

# Committed logo asset (built once from safety_portal/public/evergreen-logo.svg via
# scripts/rasterize_logo.py — qlmanage raster + autocrop). Embedding a committed PNG
# keeps this renderer deterministic/no-network. Cached ImageReader; a missing or
# unreadable asset falls back to a styled text wordmark so the renderer never fails.
_LOGO_PATH = Path(__file__).resolve().parent / "assets" / "evergreen-logo.png"
_BRAND_NAME = "EVERGREEN RENEWABLES"
_LOGO_CACHE: list[Any] = []  # [ImageReader|None] memo; [] = not yet probed (clear() to re-probe)


def _logo_reader() -> Any | None:
    """Memoized ImageReader for the brand logo, or None if the asset is unusable."""
    if not _LOGO_CACHE:
        try:
            _LOGO_CACHE.append(ImageReader(str(_LOGO_PATH)) if _LOGO_PATH.is_file() else None)
        except Exception:  # noqa: BLE001 — a bad asset must degrade to text, never raise
            logger.warning("form_pdf: logo asset unreadable (%s) — falling back to text wordmark",
                           _LOGO_PATH)
            _LOGO_CACHE.append(None)
    return _LOGO_CACHE[0]


# ── signatures ────────────────────────────────────────────────────────────────
def _parse_ml_path(d: str) -> list[list[tuple[float, float]]]:
    """Parse 'M x y L x y L x y M x y …' (the pad only emits M/L) into strokes."""
    strokes: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    toks = d.replace(",", " ").split()
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in ("M", "L"):
            try:
                x, y = float(toks[i + 1]), float(toks[i + 2])
            except (IndexError, ValueError):
                break
            if t == "M":
                if cur:
                    strokes.append(cur)
                cur = [(x, y)]
            else:
                cur.append((x, y))
            i += 3
        else:
            i += 1
    if cur:
        strokes.append(cur)
    return strokes


class SignatureDrawing(Flowable):
    """Draw SVG-path signature data scaled into a target box (Y flipped for PDF)."""

    @classmethod
    def for_signature(cls, path_d: str, width: float) -> SignatureDrawing:
        """Ink-bearing signature box: height DERIVED from width at the pad's exact
        600:180 capture aspect (``_SIG_W``:``_SIG_H``).

        ``draw()`` scales PER AXIS, so any other ratio bakes a permanent stretch into
        a FILED LEGAL DOCUMENT — and it is invisible, because the portal's on-screen
        preview re-normalises the same path into its own 600:180 viewBox and looks
        correct on screen. The table box was 140x44 (a 4.76% vertical over-stretch)
        until this existed; nothing structural had prevented it.

        EVERY call site that may receive real signature data MUST use this
        constructor — ``tests/test_form_pdf.py`` enforces that with an AST fence.
        Direct ``__init__`` with an explicit height is reserved for the blank-form
        ruled line (``_blank_field_cell``), which passes a literal ``""`` and so
        never draws strokes: ``draw()`` returns before ``sx``/``sy`` are computed,
        making the aspect irrelevant there. Deriving its height would inflate every
        blank form's row for zero fidelity gain.
        """
        return cls(path_d, width=width, height=width * _SIG_H / _SIG_W)

    def __init__(self, path_d: str, width: float = 200, height: float = 60) -> None:
        super().__init__()
        self.width = width
        self.height = height
        self._strokes = _parse_ml_path(path_d) if path_d else []
        if path_d and not any(len(s) >= 2 for s in self._strokes):
            # A non-empty signature value that yields no drawable stroke is a
            # malformed/dropped signature — never let it vanish silently. Log the
            # LENGTH only, never the path content: a signature is PII (and logging
            # it trips CodeQL clear-text-logging), so the debug signal is the size.
            logger.warning("form_pdf: signature value (len=%d) produced no drawable strokes — rendering blank",
                           len(path_d))

    def draw(self) -> None:
        c = self.canv
        c.setStrokeColor(_LINE)
        c.setLineWidth(0.5)
        c.line(0, 0, self.width, 0)  # signature baseline
        if not self._strokes:
            return
        sx, sy = self.width / _SIG_W, self.height / _SIG_H
        c.setStrokeColor(colors.black)
        c.setLineWidth(1.2)
        for stroke in self._strokes:
            if len(stroke) < 2:
                continue
            p = c.beginPath()
            x0, y0 = stroke[0]
            p.moveTo(x0 * sx, self.height - y0 * sy)
            for x, y in stroke[1:]:
                p.lineTo(x * sx, self.height - y * sy)
            c.drawPath(p)


# ── styles ──────────────────────────────────────────────────────────────────
def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    s = {
        # Brand wordmark — only used as the FALLBACK when the logo asset is missing.
        "brand": ParagraphStyle("brand", parent=base["Normal"], fontName="Helvetica-Bold",
                                fontSize=15, textColor=_BRG, spaceAfter=1, leading=17,
                                tracking=0.5),
        # Document title (the form name / branding.title) — the heading under the band.
        "title": ParagraphStyle("title", parent=base["Normal"], fontName="Helvetica-Bold",
                                 fontSize=16.5, textColor=_BRG, spaceBefore=2, spaceAfter=2,
                                 leading=20),
        # Letterhead-band doc-type label (right column, e.g. "PURCHASE ORDER").
        "band_label": ParagraphStyle("band_label", parent=base["Normal"],
                                     fontName="Helvetica-Bold", fontSize=14, textColor=_BRG,
                                     leading=17, alignment=2),  # TA_RIGHT
        # Letterhead-band meta line (right column, "Label  value").
        "band_meta": ParagraphStyle("band_meta", parent=base["Normal"], fontSize=9,
                                    textColor=_INK, leading=12.5, alignment=2),  # TA_RIGHT
        # Photo caption under a framed site photo.
        "photo_caption": ParagraphStyle("photo_caption", parent=base["Normal"],
                                        fontName="Helvetica-Oblique", fontSize=8,
                                        leading=10.5, textColor=_FOOT),
        # Section headline — paired with a gold rule by _section_header().
        "section": ParagraphStyle("section", parent=base["Normal"], fontName="Helvetica-Bold",
                                   fontSize=11.5, textColor=_BRG, leading=14),
        # Group headline (a tier below section) — also paired with a (thinner) gold rule.
        "group": ParagraphStyle("group", parent=base["Normal"], fontName="Helvetica-Bold",
                                 fontSize=10, textColor=_BRG_SOFT, leading=12.5),
        "cell": ParagraphStyle("cell", parent=base["Normal"], fontSize=8.5, leading=11,
                               textColor=_INK),
        "cellb": ParagraphStyle("cellb", parent=base["Normal"], fontName="Helvetica-Bold",
                                 fontSize=8.5, leading=11, textColor=_INK),
        # Column-header text inside tables (green, on the pale-green header fill).
        "colhead": ParagraphStyle("colhead", parent=base["Normal"], fontName="Helvetica-Bold",
                                   fontSize=8.5, leading=11, textColor=_BRG),
        "meta": ParagraphStyle("meta", parent=base["Normal"], fontSize=9.5, leading=14,
                               textColor=_INK),
        # Small muted caption (e.g. a checklist group's response-scale legend).
        "caption": ParagraphStyle("caption", parent=base["Normal"], fontSize=8,
                                  leading=10, textColor=_FOOT),
        "body": ParagraphStyle("body", parent=base["Normal"], fontSize=9.3, leading=13.2,
                               textColor=_INK, spaceAfter=5),
        "bullet": ParagraphStyle("bullet", parent=base["Normal"], fontSize=9.3, leading=13.2,
                                 textColor=_INK, leftIndent=14, bulletIndent=2, spaceAfter=2),
        "legal": ParagraphStyle("legal", parent=base["Normal"], fontName="Helvetica-Bold",
                                 fontSize=9, leading=12.5, textColor=colors.HexColor("#5a4500")),
        "heading": ParagraphStyle("heading", parent=base["Normal"], fontName="Helvetica-Bold",
                                   fontSize=10.5, textColor=_INK, spaceBefore=6, spaceAfter=2,
                                   leading=13),
    }
    return s


_ENVELOPE_KEYS = frozenset({"work_date", "job"})


def _p(text: str, st: ParagraphStyle) -> Paragraph:
    return Paragraph(_esc(str(text)), st)


# ── shared visual primitives (logo header · gold-rule headings · rich body) ─────
def _logo_flowable(max_w: float = 2.3 * inch, max_h: float = 0.52 * inch) -> Flowable | None:
    """The brand logo scaled to fit within (max_w, max_h), preserving aspect ratio and
    LEFT-aligned as a letterhead. Returns None when the asset is unusable (caller falls
    back to the text wordmark)."""
    reader = _logo_reader()
    if reader is None:
        return None
    try:
        iw, ih = reader.getSize()
        if not iw or not ih:
            return None
        scale = min(max_w / iw, max_h / ih)
        img = Image(_LOGO_PATH, width=iw * scale, height=ih * scale, lazy=0)
        img.hAlign = "LEFT"
        return img
    except Exception:  # noqa: BLE001 — never let a logo glitch abort a document
        logger.warning("form_pdf: logo flowable build failed — falling back to text wordmark")
        return None


def _brand_header(title: str, st: dict, *, variant_label: str = "",
                  doc_label: str = "", meta_lines: list[tuple[str, str]] | None = None,
                  ) -> list[Flowable]:
    """The top-of-document LETTERHEAD band, shared by every renderer so all documents
    open identically: logo (or text wordmark fallback) on the left; the document's
    identity right-aligned in the band — `doc_label` (a short doc-type headline like
    "PURCHASE ORDER") and/or `meta_lines` ("Label", "value") pairs (a form's
    Job / Date / Type; a PO's number + date) — closed by a thick gold rule spanning
    the content width. A long-form `title` (the form name) renders as the heading
    BELOW the rule, where it can wrap freely. `variant_label` is shown ONLY when it
    adds information the title doesn't already carry (avoids 'Skid Steer … — Skid
    Steer'); callers may also surface it as a meta line. All values flow through the
    escaping path (`_esc`) — band text is display-only, never markup."""
    logo = _logo_flowable()
    left: Flowable = logo if logo is not None else _p(_BRAND_NAME, st["brand"])
    right: list[Flowable] = []
    if doc_label:
        right.append(_p(doc_label, st["band_label"]))
    for label, value in (meta_lines or []):
        right.append(Paragraph(
            f'<font color="#1f4d2e"><b>{_esc(str(label))}</b></font>'
            f'&nbsp;&nbsp;{_esc(str(value))}',
            st["band_meta"]))
    head: list[Flowable] = []
    if right:
        band = Table([[left, right]], colWidths=[_CONTENT_W * 0.5, _CONTENT_W * 0.5])
        band.setStyle(TableStyle([
            ("VALIGN", (0, 0), (0, 0), "MIDDLE"),
            ("VALIGN", (1, 0), (1, 0), "BOTTOM"),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        head.append(band)
    else:
        head.append(left)
    # Thick gold rule under the masthead — the document's primary divider.
    rule = Table([[""]], colWidths=[_CONTENT_W], rowHeights=[0.5])
    rule.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, -1), 2.0, _GOLD),
                              ("TOPPADDING", (0, 0), (-1, -1), 0),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    head.append(Spacer(1, 5))
    head.append(rule)
    title_txt = title or ""
    if variant_label and variant_label.lower() not in title_txt.lower():
        title_txt = f"{title_txt} — {variant_label}" if title_txt else variant_label
    if title_txt:
        head.append(Spacer(1, 8))
        head.append(_p(title_txt, st["title"]))
        head.append(Spacer(1, 3))
    else:
        head.append(Spacer(1, 6))
    return head


def _section_header(text: str, st: dict, *, level: str = "section") -> Flowable:
    """A headline + gold underline as ONE keep-together unit (operator's requested
    'headline text and gold underline' divider). `level` selects section vs group
    weight. An optional right-aligned caption (e.g. a response scale) is appended via
    _section_header_with_caption()."""
    style = st["section"] if level == "section" else st["group"]
    weight = 1.4 if level == "section" else 0.8
    gap = 12 if level == "section" else 9
    cell = Table([[_p(text, style)]], colWidths=[_CONTENT_W])
    cell.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), weight, _GOLD),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return KeepTogether([Spacer(1, gap), cell, Spacer(1, 4)])


def _group_header(text: str, caption: str, st: dict) -> Flowable:
    """Group headline (green) with the response-scale legend as a right-aligned muted
    caption, underlined by a thin gold rule — keeps the scale legible without crowding
    the headline the way the old inline '(response: …)' did."""
    left = _p(text, st["group"])
    right = _p(caption, st["caption"]) if caption else _p("", st["caption"])
    row = Table([[left, right]], colWidths=[_CONTENT_W * 0.62, _CONTENT_W * 0.38])
    row.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), 0.8, _GOLD),
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return KeepTogether([Spacer(1, 9), row, Spacer(1, 4)])


def _callout(text: str, st: dict) -> Table:
    """The gold-edged emphasis box shared by every renderer (legal static_text, SOP
    guidance callouts, the PO invoice-routing / supersession clauses, the RFQ submit
    block): pale-gold fill, thick gold left rule, the `legal` bold amber-brown text.
    Text is escaped via `_p` — display-only, never markup."""
    box = Table([[_p(text, st["legal"])]], colWidths=[_CONTENT_W])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fbf6e9")),
        ("LINEBEFORE", (0, 0), (0, -1), 2.2, _GOLD),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return box


def _rich_body(text: str, st: dict) -> list[Flowable]:
    """Render a body string with structure preserved: blank lines split paragraphs,
    leading '-'/'•' lines become hanging-indent bullets. The TEXT is never altered —
    only laid out (fixes toolbox-talk bullet lists running together as one blob)."""
    out: list[Flowable] = []
    if not text:
        return out
    for raw in str(text).split("\n"):
        line = raw.strip()
        if not line:
            out.append(Spacer(1, 4))
            continue
        if line[0] in "-•*" and line[1:2] == " ":
            out.append(Paragraph(_esc(line[2:].strip()), st["bullet"], bulletText="•"))
        else:
            out.append(Paragraph(_esc(line), st["body"]))
    return out


# Recognizably-affirmative / -negative response vocabulary. Colour is applied ONLY
# to words in these sets (plus the lone value of a single-value "Confirmed"-style
# scale) — a first-scale answer on a NON-pass/fail scale (e.g. an incident report's
# "EMS" on EMS/WORKER/N-A) must NOT read as a green "pass" (2026-07-23 polish fix;
# previously any scale[0] answer coloured green).
_OK_WORDS = frozenset({"OK", "YES", "ACCEPTABLE", "PASS", "GOOD", "CONFIRMED",
                       "COMPLETE", "COMPLETED", "DONE", "NONE", "NONE TODAY", "SAFE"})
_BAD_WORDS = frozenset({"NOT OK", "NO", "FAIL", "BAD", "DEFECT", "UNSAFE",
                        "NOT ACCEPTABLE", "NEEDS REPAIR"})
_NA_WORDS = frozenset({"N/A", "NA", "N/A TODAY"})


def _is_confirm_scale(scale: list[str]) -> bool:
    """True for a 'confirm-style' scale — every value is affirmative or an N/A
    variant (['Confirmed'], ['Confirmed', 'N/A today'], ['None today']). Such
    groups render as compact confirm rows (`_confirm_group`); any scale carrying a
    real negative/graded option keeps the full Item/Response/Comments table."""
    if not scale:
        return False
    return all(str(s).strip().upper() in _OK_WORDS | _NA_WORDS for s in scale)


def _response_hex(resp: str, scale: list[str]) -> str:
    """Scannability colour (hex) for a checklist response: recognizably-affirmative
    vocabulary → green; recognizably-negative → amber; explicit N/A → grey; the lone
    value of a single-value scale → green (a solitary confirm option is affirmative
    by construction). Anything else — a numeric or free-text answer (hours, fuel
    '3/4'), or a non-pass/fail scale word — stays neutral ink. Purely cosmetic —
    never changes whether an item counts as answered."""
    r = (resp or "").strip().upper()
    if r in _NA_WORDS:
        return _NA.hexval().replace("0x", "#")
    if r in _OK_WORDS or (scale is not None and len(scale) == 1 and resp == scale[0]):
        return _OK.hexval().replace("0x", "#")
    if r in _BAD_WORDS:
        return _BAD.hexval().replace("0x", "#")
    return _INK.hexval().replace("0x", "#")  # numeric / free-text / non-pass-fail — neutral


def _resp_cell(resp: str, scale: list[str], st: dict) -> Paragraph:
    """A checklist Response cell: empty when blank (distinct from N/A), else the
    response word in its scannability colour. N/A-vs-blank distinction preserved."""
    if not resp:
        return _p("", st["cell"])
    return Paragraph(f'<font color="{_response_hex(resp, scale)}">{_esc(str(resp))}</font>',
                     st["cellb"])


class _CheckMark(Flowable):
    """A small VECTOR check mark (two strokes) in the given colour. Drawn as path
    strokes — not a glyph — because the base-14 fonts carry no reliable check
    character and a font-substituted ✓ renders viewer-dependently; a vector mark is
    byte-deterministic and identical in every viewer."""

    def __init__(self, size: float = 7.5, color: Any = None) -> None:
        super().__init__()
        self.width = size
        self.height = size
        self._color = color if color is not None else _OK

    def draw(self) -> None:
        c = self.canv
        c.setStrokeColor(self._color)
        c.setLineWidth(1.3)
        c.setLineCap(1)
        s = self.width
        p = c.beginPath()
        p.moveTo(0.08 * s, 0.45 * s)
        p.lineTo(0.36 * s, 0.14 * s)
        p.lineTo(0.92 * s, 0.80 * s)
        c.drawPath(p)


def _confirm_cell(resp: str, scale: list[str], st: dict) -> Flowable:
    """A COMPACT confirm-row response cell: blank stays an empty cell (distinct from
    N/A — the module invariant); an affirmative answer gets a vector check mark
    beside the word (the word alone stays in the text layer); any other answer
    renders as the plain coloured word."""
    if not resp:
        return _p("", st["cell"])
    hexc = _response_hex(resp, scale)
    word = Paragraph(f'<font color="{hexc}">{_esc(str(resp))}</font>', st["cellb"])
    if hexc == _OK.hexval().replace("0x", "#"):
        pair = Table([[_CheckMark(7.5, _OK), word]], colWidths=[12, None])
        pair.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        return pair
    return word


def _confirm_group(g: dict, cl: dict, st: dict) -> list[Flowable]:
    """Compact rendering for a SINGLE-VALUE ('Confirmed'-style) checklist group —
    the 2026-07-23 density fix for the SOP daily form, which carries dozens of 1–2
    item confirm checklists that each rendered as a full Item/Response/Comments
    table (a third of the document was table furniture). Layout only: lean
    label + response rows, hairline separators, comments (when present) as a muted
    line under their item, NO column-header row, NO redundant one-word scale
    legend. Semantics unchanged — blank still renders an EMPTY cell (distinct from
    N/A), responses still flow through `_response_hex`/`_esc`, and
    `incomplete_checklist_items` never looks at layout. Multi-value scales keep
    the full table (`_checklist_section`)."""
    rows: list[list[Any]] = []
    for it in g["items"]:
        cur = cl.get(it["key"], {}) if isinstance(cl, dict) else {}
        resp = cur.get("response", "")
        comment = cur.get("comment", "")
        label_cell: Any = _p(it["label"], st["cell"])
        if comment:
            label_cell = [_p(it["label"], st["cell"]),
                          _p(f"— {comment}", st["photo_caption"])]
        rows.append([label_cell, _confirm_cell(resp, g["scale"], st)])
    if not rows:
        return []
    t = Table(rows, colWidths=[_CONTENT_W - 1.5 * inch, 1.5 * inch])
    t.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, _LINE),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, -1), 2), ("RIGHTPADDING", (1, 0), (1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ]))
    return [_group_header(g["label"], "", st), t]


def _grid_style(n_rows: int, n_cols: int) -> TableStyle:
    """Shared data-table look: pale-green header row, zebra body rows, soft hairline
    grid. One definition so every table (header/repeating/checklist) matches."""
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _TINT),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, _ZEBRA]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, _LINE),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, _GOLD),  # gold rule under the column heads
        ("LINEAFTER", (0, 0), (-2, -1), 0.3, _LINE),
        ("BOX", (0, 0), (-1, -1), 0.6, _LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ])


# ── footer (brand · form · Page X of Y) ────────────────────────────────────────
class _FooterCanvas(_pdfcanvas.Canvas):
    """Canvas that stamps a footer (brand wordmark · form label · Page X of Y) on every
    page. Two-pass save() defers drawing until the total page count is known. The brand
    wordmark in the footer keeps the company name in the text layer on every page (the
    masthead is now an image), which also satisfies the render-fidelity tests."""

    def __init__(self, *args: Any, footer_label: str = "",
                 show_page_numbers: bool = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._saved: list[dict] = []
        self._footer_label = footer_label
        self._show_page_numbers = show_page_numbers

    def showPage(self) -> None:  # noqa: N802 — overrides reportlab Canvas.showPage (camelCase API)
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._saved)
        for i, state in enumerate(self._saved, start=1):
            self.__dict__.update(state)
            self._draw_footer(i, total)
            super().showPage()
        super().save()

    def _draw_footer(self, page: int, total: int) -> None:
        w = self._pagesize[0]
        y = 0.42 * inch
        self.saveState()
        self.setStrokeColor(_LINE)
        self.setLineWidth(0.5)
        self.line(_MARGIN, y + 9, w - _MARGIN, y + 9)
        # A short gold tick over the hairline, left-anchored — the footer's echo of
        # the masthead's gold rule (pure decoration; the text layer is unchanged).
        self.setStrokeColor(_GOLD)
        self.setLineWidth(1.4)
        self.line(_MARGIN, y + 9, _MARGIN + 24, y + 9)
        self.setFont("Helvetica-Bold", 7)
        self.setFillColor(_BRG_SOFT)
        self.drawString(_MARGIN, y, _BRAND_NAME)
        self.setFillColor(_FOOT)
        if self._footer_label:
            self.setFont("Helvetica", 7)
            label = self._footer_label if len(self._footer_label) <= 64 else self._footer_label[:61] + "…"
            self.drawCentredString(w / 2.0, y, label)
        if self._show_page_numbers:
            self.setFont("Helvetica", 7)
            self.drawRightString(w - _MARGIN, y, f"Page {page} of {total}")
        self.restoreState()


def _canvas_maker(label: str, *, show_page_numbers: bool = True) -> Any:
    """A canvasmaker bound to a footer label, for SimpleDocTemplate.build(canvasmaker=)."""
    def make(*args: Any, **kwargs: Any) -> _FooterCanvas:
        return _FooterCanvas(*args, footer_label=label,
                             show_page_numbers=show_page_numbers, **kwargs)
    return make


# ── section flowables ─────────────────────────────────────────────────────────
def _header_section(fields: list[dict], values: dict, st: dict) -> list[Flowable]:
    rows: list[list[Any]] = []
    for f in fields:
        if f["key"] in _ENVELOPE_KEYS:
            continue
        if f.get("input") == "photo":
            # Photos are rendered separately as a screened image grid (see
            # _photo_grid / render_submission_pdf). NEVER inline the raw value here —
            # it is a list of base64 PhotoValue objects, and the renderer must never
            # parse or dump untrusted upload bytes (form_pdf is deterministic; the
            # §34-screened bytes arrive out-of-band via submission['screened_photos']).
            continue
        val = values.get(f["key"], "")
        if f["input"] == "signature":
            # 200x60 — byte-identical to the previous default, now self-evidently 600:180.
            cell: Any = SignatureDrawing.for_signature(str(val), 200) if val else _p("", st["cell"])
        else:
            cell = _p(val, st["cell"])
        rows.append([_p(f["label"], st["colhead"]), cell])
    if not rows:
        return []
    t = Table(rows, colWidths=[2.2 * inch, _CONTENT_W - 2.2 * inch])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, _LINE),
        ("LINEBEFORE", (0, 0), (0, -1), 1.4, _TINT),  # subtle green accent rail
        ("LEFTPADDING", (0, 0), (0, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
    ]))
    return [t]


def _table_section(section: dict, values: dict, st: dict) -> list[Flowable]:
    cols = section["columns"]
    head = [_p(c["label"], st["colhead"]) for c in cols]
    body: list[list[Any]] = [head]
    for row in values.get(section["key"], []) or []:
        cells: list[Any] = []
        for c in cols:
            v = row.get(c["key"], "")
            if c["input"] == "signature" and v:
                # Was 140x44 — a 4.76% vertical over-stretch of every table signature.
                # 140 * 180/600 = 42.0 exactly; the row shrinks 51pt -> 49pt and no
                # active form's pagination changes.
                cells.append(SignatureDrawing.for_signature(str(v), 140))
            elif c["input"] == "photo":
                # Photos are header-level only (publishValidation-enforced); a photo in a
                # table column is an illegal/malformed definition. NEVER dump the raw
                # base64 PhotoValue list as text — emit a placeholder instead.
                cells.append(_p("[photo omitted]", st["cell"]))
            else:
                cells.append(_p(v, st["cell"]))
        body.append(cells)
    t = Table(body, repeatRows=1)
    t.setStyle(_grid_style(len(body), len(cols)))
    out: list[Flowable] = []
    if section.get("title"):
        out.append(_section_header(section["title"], st))
    out.append(t)
    return out


def _checklist_section(section: dict, values: dict, st: dict) -> list[Flowable]:
    out: list[Flowable] = []
    if section.get("title"):
        out.append(_section_header(section["title"], st))
    cl = values.get(section["key"], {}) or {}
    for g in section["groups"]:
        scale = g["scale"]
        if _is_confirm_scale(scale):
            # Confirm-style scale → the compact row treatment (see _confirm_group).
            out.extend(_confirm_group(g, cl, st))
            continue
        rows: list[list[Any]] = [[_p("Item", st["colhead"]), _p("Response", st["colhead"]),
                                  _p("Comments", st["colhead"])]]
        for it in g["items"]:
            cur = cl.get(it["key"], {}) if isinstance(cl, dict) else {}
            resp = cur.get("response", "")
            # N/A prints "N/A" (coloured grey); blank prints an EMPTY cell — distinct.
            rows.append([
                _p(it["label"], st["cell"]),
                _resp_cell(resp, scale, st),
                _p(cur.get("comment", ""), st["cell"]),
            ])
        t = Table(rows, colWidths=[_CONTENT_W - 2.85 * inch, 1.05 * inch, 1.8 * inch],
                  repeatRows=1)
        t.setStyle(_grid_style(len(rows), 3))
        # group label (green) + the response-scale legend as a muted right-aligned caption
        out.append(_group_header(g["label"], " / ".join(scale), st))
        out.append(t)
    return out


def _section_flowables(section: dict, values: dict, st: dict) -> list[Flowable]:
    typ = section["type"]
    if typ == "header":
        return _header_section(section["fields"], values, st)
    if typ in ("repeating_table", "signature_table"):
        return _table_section(section, values, st)
    if typ == "checklist":
        return _checklist_section(section, values, st)
    if typ == "freeform":
        out: list[Flowable] = [_section_header(section["label"], st, level="group")]
        out.extend(_rich_body(values.get(section["key"], "") or "", st))
        return out
    if typ == "static_text":
        # Legal/mandatory wording rendered VERBATIM. A legal/footer emphasis gets a
        # subtle gold-edged callout box; plain emphasis stays a heading. Text unchanged.
        if section.get("emphasis") in ("legal", "footer"):
            return [Spacer(1, 8), _callout(section["text"], st)]
        return [Spacer(1, 4), _p(section["text"], st["heading"])]
    if typ == "content_blocks":
        out2: list[Flowable] = []
        if section.get("title"):
            out2.append(_section_header(section["title"], st))
        for b in section["blocks"]:
            if b.get("heading"):
                out2.append(_p(b["heading"], st["heading"]))
            out2.extend(_rich_body(b.get("body", ""), st))
        return out2
    if typ == "guidance":
        # SOP guidance (slice D1) renders as its HEADING + callout one-liners ONLY —
        # DELIBERATE truncation: the full p/bullets prose is the on-screen SOP walk in
        # the portal; reprinting it in every daily PDF would bloat the weekly packet
        # (the SOP daily form carries ~20 guidance sections). The callouts (CRITICAL
        # RULE / QUALITY RULE / NOTE / FINAL STATEMENT) are the safety-critical
        # one-liners and stay in the document of record, in the gold callout box the
        # legal static_text sections already use. Text VERBATIM, never altered.
        out3: list[Flowable] = [_section_header(section["heading"], st)]
        for b in section.get("blocks", []):
            if b.get("type") == "callout":
                out3.extend([Spacer(1, 6), _callout(b.get("text", ""), st)])
        return out3
    if typ == "form_link":
        # Deep link to another form type (slice D1): the submission payload carries no
        # link state (the linked form files as its OWN submission), so render just the
        # label + a pointer to where the linked record lives. The live filed-indicator
        # is an on-screen (Daily tab, D2) affordance only.
        return [
            _section_header(section["label"], st, level="group"),
            _p("Linked form — see the forms filed for this job and date.", st["caption"]),
        ]
    if typ == "job_requirements":
        # Per-job daily-form requirements (slice D4). The section itself is an EMPTY
        # placeholder in the definition — the content is the submission's SELF-DESCRIBING
        # values array (values[<key>] = [{label, kind, response}], captured by the portal
        # from the job's D1 overlay at fill time). Rendered GENERICALLY as label→response
        # rows, so the filed PDF shows the client requirements + answers exactly as
        # answered, stable regardless of later requirement edits — and new kinds render
        # for free (D5 / migration 0032 added number/date/select; every kind's response is
        # a plain string, so no per-kind branch exists or is needed here). Absent/empty →
        # the whole section is skipped (a job with no requirements adds nothing to the PDF).
        entries = values.get(section.get("key", "job_requirements"))
        if not isinstance(entries, list) or not entries:
            return []
        rows: list[list[Any]] = [[_p("Requirement", st["colhead"]),
                                  _p("Response", st["colhead"])]]
        for entry in entries:
            if not isinstance(entry, dict):
                continue  # defensive: a malformed entry is dropped, never fatal
            rows.append([
                _p(str(entry.get("label", "") or ""), st["cell"]),
                _p(str(entry.get("response", "") or ""), st["cell"]),
            ])
        if len(rows) == 1:
            return []
        out4: list[Flowable] = [_section_header(section.get("title", "Job-specific requirements"), st)]
        t = Table(rows, colWidths=[_CONTENT_W - 2.2 * inch, 2.2 * inch], repeatRows=1)
        t.setStyle(_grid_style(len(rows), 2))
        out4.append(t)
        return out4
    if typ == "expected_materials":
        # Expected-materials receipt list (Material receipts M2). DELIBERATELY a note line
        # only: the section is an on-screen affordance (the Daily tab renders the job's live
        # D1 expected-materials rows with Confirm-receipt / Report-a-problem actions) and
        # files NO values under its own key — the receipt DATA the document of record needs
        # already lands in the Deliveries Received table (the confirm action appends a row
        # there) and in the material-incident form's OWN filed submission. Reprinting the
        # live D1 list here would snapshot mutable state the submission never carried.
        return [
            _section_header(section.get("title", "Expected materials"), st, level="group"),
            _p("Receipts recorded under Deliveries Received above; delivery problems are "
               "filed as Material Incident Report submissions for this job and date.",
               st["caption"]),
        ]
    if typ == "additional_photos":
        # Additional-photos pool mount (DR-photo-pool Slice 1). The submission carries only
        # POOL REFERENCES (values[<key>] = [{pool_id, caption?}]) — the photo BYTES were
        # uploaded individually to daily_photo_pool (the inline site_photos field is
        # payload-budgeted) and are §34-screened Mac-side, then filed to Box (Slice 2). This
        # renderer therefore lists the referenced photos by caption — display text only,
        # UNTRUSTED downstream like the EXIF sidecar — plus a pointer to where the screened
        # bytes live. Malformed entries are dropped, never fatal (the job_requirements
        # discipline). An empty/absent list still renders the header + pointer (the section
        # was part of the form the manager saw — never a silent omission).
        out5: list[Flowable] = [
            _section_header(section.get("title", "Additional site photos"), st, level="group"),
        ]
        entries = values.get(section.get("key", "additional_photos"))
        rows5: list[list[Any]] = [[_p("#", st["colhead"]), _p("Caption", st["colhead"])]]
        n_refs = 0
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue  # defensive: a malformed entry is dropped, never fatal
                n_refs += 1
                caption = str(entry.get("caption", "") or "")
                rows5.append([_p(str(n_refs), st["cell"]), _p(caption, st["cell"])])
        if n_refs > 0:
            out5.append(_p(f"{n_refs} additional photo(s) attached — screened and filed with "
                           "this report's records.", st["caption"]))
            t5 = Table(rows5, colWidths=[0.6 * inch, _CONTENT_W - 0.6 * inch], repeatRows=1)
            t5.setStyle(_grid_style(len(rows5), 2))
            out5.append(t5)
        else:
            out5.append(_p("No additional photos attached (the inline Site photos field above "
                           "carries up to four).", st["caption"]))
        return out5
    logger.warning("form_pdf: unknown section type %r — skipped", typ)
    return []


# ── site photos ───────────────────────────────────────────────────────────────
_PHOTO_USABLE_W = 7.0 * inch   # letter minus the 0.6in side margins
_PHOTO_COL_W = _PHOTO_USABLE_W / 2.0
_PHOTO_BOX_W = _PHOTO_COL_W - 0.18 * inch
_PHOTO_BOX_H = 2.6 * inch


def _photo_cell(caption: str, jpeg: bytes, st: dict) -> list[Flowable] | None:
    """One photo + caption as a Table cell (a list of flowables), scaled to fit the 2-up
    grid cell preserving aspect ratio.

    `jpeg` is ALREADY §34-screened, re-encoded clean JPEG bytes (safety_reports.
    photo_screen) — the renderer never touches raw upload bytes. A photo that fails to
    render is dropped + logged (length only, never the bytes or caption text), mirroring
    the signature-PII discipline; one bad photo must never abort the document.

    Dimensions are read via ImageReader; the Image flowable is then built from a FRESH
    BytesIO with lazy=0 — lazy=1 (the default) re-opens the file at draw time, which a
    consumed in-memory BytesIO cannot satisfy, yielding a degenerate, unplaceable height.
    """
    try:
        iw, ih = ImageReader(io.BytesIO(jpeg)).getSize()
        if not iw or not ih:
            raise ValueError("zero dimension")
        scale = min((_PHOTO_BOX_W - 8) / iw, _PHOTO_BOX_H / ih)
        img = Image(io.BytesIO(jpeg), width=iw * scale, height=ih * scale, lazy=0)
    except Exception:  # noqa: BLE001 — unrenderable photo is skipped, never fatal
        logger.warning("form_pdf: site photo skipped (caption_len=%d, jpeg_len=%d) — unrenderable",
                       len(caption or ""), len(jpeg or b""))
        return None
    # Hairline frame around the photo (a mat, not a border-heavy box) + an italic
    # muted caption — presentation only, the screened bytes are untouched.
    frame = Table([[img]])
    frame.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.75, _LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    frame.hAlign = "LEFT"
    cell: list[Flowable] = [frame]
    if caption:
        cell.append(Spacer(1, 2))
        cell.append(_p(caption, st["photo_caption"]))
    return cell


def _photo_grid(photos: list[tuple[str, bytes]], st: dict) -> list[Flowable]:
    """A 'Site Photos' heading + a 2-up grid of screened photos with captions."""
    cells = [c for c in (_photo_cell(cap, jpeg, st) for cap, jpeg in photos) if c is not None]
    if not cells:
        return []
    grid: list[list[Any]] = [cells[i:i + 2] for i in range(0, len(cells), 2)]
    if len(grid[-1]) == 1:
        grid[-1].append("")  # pad the final row so the Table is rectangular
    table = Table(grid, colWidths=[_PHOTO_COL_W, _PHOTO_COL_W])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return [_section_header("Site Photos", st), table]


# ── public API ────────────────────────────────────────────────────────────────
def render_submission_pdf(definition: dict, submission: dict) -> bytes:
    """Render one submission to PDF bytes.

    submission keys: job_name (resolved by intake), work_date, values (the portal
    fill state), and optional screened_photos — a list of (caption, clean_jpeg_bytes)
    tuples that have ALREADY passed §34 screening + re-encode (safety_reports.
    photo_screen). The renderer embeds those out-of-band; it never decodes the raw
    base64 photos in `values` (form_pdf is deterministic and must not parse untrusted
    input). Deterministic; raises only on a totally malformed definition.
    """
    st = _styles()
    values = submission.get("values", {}) or {}
    form_title = definition.get("branding", {}).get("title") or definition.get("form_name", "")
    # Variant is surfaced as a band "Type" meta line, so it is NOT appended to the
    # masthead title (avoids 'Skid Steer … — Skid Steer' / a doubled toolbox topic).
    # Job / Date / Type ride the letterhead band's right column (values escaped inside
    # _brand_header) — the document identity sits IN the letterhead, not a line below.
    meta = [("Job / Project", submission.get("job_name", "")),
            ("Date", submission.get("work_date", ""))]
    if definition.get("variant_label"):
        meta.insert(1, ("Type", definition["variant_label"]))
    flow: list[Flowable] = _brand_header(form_title, st, meta_lines=meta)
    flow.append(Spacer(1, 2))

    for section in definition.get("sections", []):
        try:
            flow.extend(_section_flowables(section, values, st))
        except Exception:  # one bad section must not abort the whole document
            logger.exception("form_pdf: section render failed (type=%s) — skipped",
                             section.get("type"))

    screened_photos = submission.get("screened_photos") or []
    if screened_photos:
        try:
            flow.extend(_photo_grid(screened_photos, st))
        except Exception:  # the photo grid must never abort the document of record
            logger.exception("form_pdf: site-photo grid render failed — skipped")

    # DR-photo-pool Slice 2 — grid-adjacent status notes for additional-photo pool
    # references that could NOT be embedded (intake passes the counts via
    # `additional_photo_notes`: {'pending','refused','unavailable'}). Rendered even
    # when the grid itself is empty (a report whose ONLY photos were refused still
    # says so — never a silent omission). Counts only, never bytes or captions.
    ap_notes = submission.get("additional_photo_notes") or {}
    try:
        note_lines: list[str] = []
        if ap_notes.get("pending"):
            note_lines.append(
                f"{ap_notes['pending']} additional photo(s) were still pending security "
                "screening at render time — filed without them; once screened, the "
                "photos remain in this job's Box photo records."
            )
        if ap_notes.get("refused"):
            note_lines.append(
                f"{ap_notes['refused']} additional photo(s) refused by security "
                "screening and not included (see the review queue)."
            )
        if ap_notes.get("unavailable"):
            note_lines.append(
                f"{ap_notes['unavailable']} additional photo(s) unavailable at render "
                "time and not included."
            )
        for line in note_lines:
            flow.append(_p(line, st["caption"]))
    except Exception:  # notes are supplementary — never abort the document of record
        logger.exception("form_pdf: additional-photo notes render failed — skipped")

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, title=definition.get("form_name", "Safety form"),
                            leftMargin=_MARGIN, rightMargin=_MARGIN,
                            topMargin=_MARGIN, bottomMargin=0.7 * inch)
    doc.build(flow, canvasmaker=_canvas_maker(form_title))
    return buf.getvalue()


def merge_pdfs(pdfs: list[bytes]) -> bytes:
    """Concatenate per-submission PDFs into one weekly packet, in the given order.

    The caller (weekly_generate, Phase 5) orders `pdfs` oldest-first — Sat→Fri
    ascending by work-date, intra-date by submission time. Deterministic; no AI /
    network. Raises ValueError on an empty list (an empty week takes the
    'no submissions this week' rollup path, never a zero-page merge).
    """
    from pypdf import (  # runtime dep; lazy so the renderer alone needn't load it
        PdfReader,
        PdfWriter,
    )

    if not pdfs:
        raise ValueError("merge_pdfs: no PDFs to merge")
    writer = PdfWriter()
    for b in pdfs:
        writer.append(PdfReader(io.BytesIO(b)))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def page_count(pdf: bytes) -> int:
    """Number of pages in a PDF byte string (used to compute weekly-index page numbers).
    Deterministic; no AI / network."""
    from pypdf import PdfReader
    return len(PdfReader(io.BytesIO(pdf)).pages)


# ── weekly-packet front matter (cover + date-grouped index) ─────────────────────
def render_weekly_cover(project_name: str, week_label: str, submission_count: int,
                        *, compiled_display: str = "",
                        title: str = "WEEKLY SAFETY REPORT") -> bytes:
    """Render the weekly packet's branded COVER page (page 1 of the compiled packet).

    2026-07-23 polish: a DESIGNED cover — letterhead band, then a deep-evergreen
    title panel (white report title + project name, the week label in pale green)
    closed by a gold rule, then a stats strip (submission count + compiled stamp)
    and the packet note. `title` is parameterized so the PROGRESS binding can label
    its packet correctly (GenerateConfig.cover_title; the default preserves the
    safety packet's wording — the old hardcoded title mislabeled progress covers).

    Pure data → bytes — NO AI, NO network. The packet itself is assembled by
    weekly_generate (cover + index + per-submission PDFs via merge_pdfs); this renders
    only the cover. weekly_generate fences the call so a cover-render failure degrades
    to the plain forms-only packet, never an aborted compile."""
    st = _styles()
    base = getSampleStyleSheet()["Normal"]
    panel_title = ParagraphStyle("panel_title", parent=base, fontName="Helvetica-Bold",
                                 fontSize=25, textColor=colors.white, leading=30)
    panel_job = ParagraphStyle("panel_job", parent=base, fontName="Helvetica-Bold",
                               fontSize=15.5, textColor=colors.white, leading=20)
    panel_week = ParagraphStyle("panel_week", parent=base, fontSize=11.5,
                                textColor=colors.HexColor("#cfe0d2"), leading=15)
    stat_num = ParagraphStyle("stat_num", parent=base, fontName="Helvetica-Bold",
                              fontSize=21, textColor=_BRG, leading=25)
    stat_lab = ParagraphStyle("stat_lab", parent=base, fontSize=9, textColor=_FOOT,
                              leading=12)
    flow: list[Flowable] = _brand_header("", st)  # letterhead band, no title here
    flow.append(Spacer(1, 1.1 * inch))
    panel = Table([[[
        _p(title, panel_title),
        Spacer(1, 10),
        _p(project_name, panel_job),
        Spacer(1, 4),
        _p(week_label, panel_week),
    ]]], colWidths=[_CONTENT_W])
    panel.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _BRG),
        ("LINEBELOW", (0, 0), (-1, -1), 2.4, _GOLD),
        ("LEFTPADDING", (0, 0), (-1, -1), 26), ("RIGHTPADDING", (0, 0), (-1, -1), 26),
        ("TOPPADDING", (0, 0), (-1, -1), 26), ("BOTTOMPADDING", (0, 0), (-1, -1), 24),
    ]))
    flow.append(panel)
    flow.append(Spacer(1, 26))
    n = submission_count
    stat_cells: list[Any] = [[
        _p(str(n), stat_num),
        _p(f"{'submission' if n == 1 else 'submissions'} filed this week", stat_lab),
    ]]
    if compiled_display:
        stat_cells.append([
            _p(compiled_display, ParagraphStyle("stat_val", parent=base,
                                                fontName="Helvetica-Bold", fontSize=12,
                                                textColor=_BRG, leading=25)),
            _p("compiled", stat_lab),
        ])
    stats = Table([stat_cells], colWidths=[_CONTENT_W / 2] * len(stat_cells))
    stats.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), 1.0, _GOLD),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
    ]))
    flow.append(stats)
    flow.append(Spacer(1, 0.5 * inch))
    note = Table([[_p("This packet is the compiled record of the forms filed for the job "
                      "and week named above. See the next page for contents.", st["caption"])]],
                 colWidths=[_CONTENT_W])
    note.setStyle(TableStyle([("LINEBEFORE", (0, 0), (0, -1), 2.2, _GOLD),
                              ("LEFTPADDING", (0, 0), (-1, -1), 8),
                              ("TOPPADDING", (0, 0), (-1, -1), 4),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    flow.append(note)

    buf = io.BytesIO()
    # The report title lives on THREE surfaces — the panel text, the footer label,
    # and the PDF /Title metadata — all derived from the ONE `title` parameter
    # (multi-surface fan-out; the safety default keeps its historical metadata).
    display_title = title.title() if title.isupper() else title
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            title=f"{display_title} — {project_name}",
                            leftMargin=_MARGIN, rightMargin=_MARGIN,
                            topMargin=_MARGIN, bottomMargin=0.7 * inch)
    doc.build(flow, canvasmaker=_canvas_maker(display_title, show_page_numbers=False))
    return buf.getvalue()


def render_weekly_index(project_name: str, week_label: str,
                        entries: list[dict[str, Any]]) -> bytes:
    """Render the weekly packet's CONTENTS index (page 2+), grouped by work date then
    form, each row carrying its absolute page number IN THE PACKET (matches the PDF
    viewer's page counter). `entries` are in packet order; each is
    {"date_display": str, "form_name": str, "start_page": int}.

    Pure data → bytes — NO AI, NO network. weekly_generate computes start_page (it owns
    the page-count arithmetic) and fences this call."""
    st = _styles()
    flow: list[Flowable] = _brand_header("Contents", st)
    flow.append(_p(f"{project_name} — {week_label}", st["meta"]))
    flow.append(Spacer(1, 4))
    last_date = object()  # sentinel so the first date always emits a sub-header
    for e in entries:
        date_display = e.get("date_display", "")
        if date_display != last_date:
            flow.append(_group_header(date_display or "—", "", st))
            last_date = date_display
        # Form name · dotted leader · page number (classic TOC row — the leader is a
        # dotted rule on its own middle cell, sized to sit near the text baseline).
        row = Table([[_p(e.get("form_name", ""), st["cell"]),
                      "",
                      _p(f"Page {e.get('start_page', '')}", st["cell"])]],
                    colWidths=[3.6 * inch, _CONTENT_W - 4.6 * inch, 1.0 * inch])
        row.setStyle(TableStyle([
            ("ALIGN", (2, 0), (2, 0), "RIGHT"),
            ("LINEBELOW", (1, 0), (1, 0), 0.7, _FOOT, "round", (0.5, 2.6)),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (0, -1), 10),
            ("BOTTOMPADDING", (1, 0), (1, 0), 6.5),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (0, 0), 3.5),
            ("BOTTOMPADDING", (2, 0), (2, 0), 3.5),
        ]))
        flow.append(row)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, title=f"Contents — {project_name}",
                            leftMargin=_MARGIN, rightMargin=_MARGIN,
                            topMargin=_MARGIN, bottomMargin=0.7 * inch)
    doc.build(flow, canvasmaker=_canvas_maker("Contents", show_page_numbers=False))
    return buf.getvalue()


def render_progress_rollup(project_name: str, week_label: str, numbers: dict[str, Any],
                           *, include_costs: bool = False) -> bytes:
    """Render the PROGRESS packet's rollup-numbers page (P6) — a single branded page
    spliced AFTER the cover, BEFORE the index by `generate_core._build_weekly_packet`
    (progress binding only; safety never passes a provider, so the safety packet is
    byte-identical).

    Pure data → bytes — NO AI, NO network (the module-wide contract). `numbers` is the
    already-fetched aggregate dict from the SEND-FREE Worker rollup route
    (`shared.portal_client.get_progress_rollup`): `{labor_hours, equipment:[{name,kind}],
    open_tasks, materials, ...}`. The Worker owns the D1 aggregation; this owns only layout.

    Adversarial Input Handling (Invariant 2): every value is interpolated as PLAIN,
    reportlab-escaped text via `_p` (equipment names can carry field-reported free text — a
    hostile name CANNOT inject markup). Every field is defensively coerced because `numbers`
    rides untrusted JSON transport (a malformed shape degrades, never raises).

    Sections: **Labor hours / Equipment on site / Open tasks / Materials (coming soon)**.
    There is deliberately **NO progress-%** section (operator decision 2026-06-30: a single
    current `jobs.progress` value is a misleading guess, not a measurement). Graceful
    zero-state: no labor + no equipment + no open tasks → "No field-ops activity recorded
    for this week." (matches the Worker's graceful-zeros contract), never an empty grid.

    `include_costs` (default False) reserves the cost-flip gate (Open Question 3): labor-cost
    / materials-value columns stay OFF until the cost-flip ITS_Config + M2 `material_list`
    land. v1 carries no cost data in `numbers`, so True today only annotates that costs are
    pending — it never fabricates a number.
    """
    st = _styles()
    flow: list[Flowable] = _brand_header("Weekly Progress Rollup", st)
    flow.append(_p(f"{project_name} — {week_label}", st["meta"]))
    flow.append(Spacer(1, 6))

    # Defensive coercion — `numbers` is the Worker's JSON aggregate over untrusted transport.
    try:
        hours = float(numbers.get("labor_hours") or 0)
    except (TypeError, ValueError):
        hours = 0.0
    raw_equipment = numbers.get("equipment")
    equipment = (
        [e for e in raw_equipment if isinstance(e, dict)]
        if isinstance(raw_equipment, list) else []
    )
    try:
        open_tasks = int(numbers.get("open_tasks") or 0)
    except (TypeError, ValueError):
        open_tasks = 0

    if hours == 0 and not equipment and open_tasks == 0:
        flow.append(Spacer(1, 12))
        flow.append(_p("No field-ops activity recorded for this week.", st["body"]))
    else:
        base = getSampleStyleSheet()["Normal"]
        stat_num = ParagraphStyle("rollup_stat", parent=base, fontName="Helvetica-Bold",
                                  fontSize=19, textColor=_BRG, leading=23)
        # Labor hours (total for the Sat→Fri window, amend-collapsed server-side).
        # The number is the story — render it stat-tile large, the unit as the label.
        flow.append(_section_header("Labor hours", st))
        flow.append(Paragraph(
            f'{_esc(f"{hours:g}")}'
            f'<font size="9.5" color="{_FOOT.hexval().replace("0x", "#")}">'
            f'&nbsp;&nbsp;{"hour" if hours == 1 else "hours"} logged this week</font>',
            stat_num))
        if include_costs:
            flow.append(_p("Labor cost: pending (enable the cost-flip config + M2).",
                           st["caption"]))

        # Equipment on site (DISTINCT equipment seen on the job in the window).
        flow.append(_section_header("Equipment on site", st))
        if equipment:
            rows: list[list[Any]] = [[_p("Equipment", st["colhead"]), _p("Type", st["colhead"])]]
            for e in equipment:
                rows.append([_p(str(e.get("name") or "—"), st["cell"]),
                             _p(str(e.get("kind") or "—"), st["cell"])])
            table = Table(rows, colWidths=[_CONTENT_W * 0.6, _CONTENT_W * 0.4], repeatRows=1)
            table.setStyle(_grid_style(len(rows), 2))
            flow.append(table)
        else:
            flow.append(_p("No equipment recorded on site this week.", st["body"]))

        # Open tasks (current bounded status != 'done' count; NOT windowed — no completed-this-week).
        flow.append(_section_header("Open tasks", st))
        flow.append(Paragraph(
            f'{open_tasks}'
            f'<font size="9.5" color="{_FOOT.hexval().replace("0x", "#")}">'
            f'&nbsp;&nbsp;open {"task" if open_tasks == 1 else "tasks"} (not yet done)</font>',
            stat_num))

        # Materials — placeholder until M2 wires material data into the rollup numbers.
        flow.append(_section_header("Materials", st))
        flow.append(_p("Materials reporting is not yet included in this packet.",
                       st["caption"]))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, title=f"Progress Rollup — {project_name}",
                            leftMargin=_MARGIN, rightMargin=_MARGIN,
                            topMargin=_MARGIN, bottomMargin=0.7 * inch)
    doc.build(flow, canvasmaker=_canvas_maker("Progress Rollup", show_page_numbers=False))
    return buf.getvalue()


# ── form-definition loader ──────────────────────────────────────────────────────
# The Phase-4 definitions are the single source of truth shared with the TS display
# runtime; they live as JSON under safety_portal/forms/<form_code>.json. The TS side
# loads them via a Vite glob (src/forms/registry.ts); this is the Python side's loader
# for the Phase-5 intake portal branch (`form_code` arrives in the HMAC-verified payload).
_FORMS_DIR = Path(__file__).resolve().parent.parent / "safety_portal" / "forms"
# Form codes are lowercase-kebab + version (e.g. "jha-v1", "toolbox-talk-ppe-v1").
# The strict charset is also the path-traversal guard: `form_code` originates in the
# portal payload, so no "/" / "." / ".." can reach the filesystem path.
_FORM_CODE_RE = re.compile(r"[a-z0-9-]+")


def load_definition(form_code: str) -> dict | None:
    """Load the Phase-4 form definition for `form_code`, or None if unresolvable.

    Returns None (never raises, never returns a partial) on: an unsafe/empty
    form_code, a missing file, malformed JSON, or a non-object top level. The
    caller (intake portal branch) treats None as "unknown/invalid form → flag to
    the Review Queue, do NOT render" — a blank form must never be filed silently.
    """
    if not form_code or not _FORM_CODE_RE.fullmatch(form_code):
        return None
    path = _FORMS_DIR / f"{form_code}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def incomplete_checklist_items(definition: dict, submission: dict) -> list[tuple[str, str, str]]:
    """Checklist items left BLANK (not-yet-inspected). N/A is a complete answer and is
    NOT returned. Phase-5 intake uses this to flag an incomplete submission — blank
    must never be silently treated as answered.
    """
    values = submission.get("values", {}) or {}
    blanks: list[tuple[str, str, str]] = []
    for section in definition.get("sections", []):
        if section.get("type") != "checklist":
            continue
        cl = values.get(section["key"], {}) or {}
        for g in section["groups"]:
            for it in g["items"]:
                resp = (cl.get(it["key"], {}) if isinstance(cl, dict) else {}).get("response", "")
                if resp == "" or resp is None:
                    blanks.append((section["key"], it["key"], it["label"]))
    return blanks


# ════════════════════════════════════════════════════════════════════════════════
# Blank fillable-form renderer (PR-L — manual fallback archive)
# ════════════════════════════════════════════════════════════════════════════════
# WHY a SIBLING, not a flag through the submission helpers: the submission renderer
# draws *values*; the blank renderer draws *empty AcroForm fields* at the same layout
# positions. Those are different per-cell operations, so a single helper would branch
# on `blank` in every cell — more churn, more risk to the live filing path. Instead we
# add blank-mode sibling section-builders that reuse the EXACT same _styles(), branding
# constants, page geometry, section order, and footer. The two text-only sections that
# MUST stay byte-identical between digital and manual — `static_text` (legal/mandatory
# wording) and `content_blocks` (the toolbox-talk body) — are rendered by delegating to
# the *existing* `_section_flowables` (with empty values), so they CANNOT diverge from
# the submission renderer. Preservation-over-refactor (Op Stds §14): render_submission_pdf
# and its helpers are untouched.
#
# WHY custom Flowables for fields: reportlab emits AcroForm widgets only via
# `canvas.acroForm.{textfield,checkbox,choice}`, which need an absolute canvas position.
# A platypus Flowable gets its canvas + origin at draw() time, so wrapping each field in
# a tiny Flowable lets the normal frame/table layout place it. Field NAMES must be unique
# per document (a repeated name makes one shared field), so a per-document counter
# (_FieldNamer) guarantees uniqueness.

# AcroForm field geometry. Kept modest so fields sit inside table cells / header rows.
_FIELD_H = 14.0  # single-line text/choice field height (pt)
_CHECK_SZ = 10.0  # checkbox side (pt)
# Leading dropdown option / initial value (see _ChoiceFieldFlowable for why ASCII + non-falsy).
_SELECT_PROMPT = "(select)"


class _FieldNamer:
    """Per-document monotonic field-name source — AcroForm names must be unique."""

    def __init__(self) -> None:
        self._n = 0

    def next(self, base: str) -> str:
        self._n += 1
        # Sanitize to a safe AcroForm name; the counter guarantees uniqueness even
        # when two cells share a base (e.g. repeated "Other:" rows).
        safe = re.sub(r"[^A-Za-z0-9_]", "_", base)[:40]
        return f"f{self._n}_{safe}"


class _TextFieldFlowable(Flowable):
    """An empty AcroForm text field laid out as a flowable (single- or multi-line)."""

    def __init__(self, namer: _FieldNamer, base: str, width: float,
                 *, multiline: bool = False, height: float | None = None) -> None:
        super().__init__()
        self._namer = namer
        self._base = base
        self.width = width
        # A multiline field is given a taller box so handwriting / typed notes fit.
        self.height = height if height is not None else (_FIELD_H * 2.4 if multiline else _FIELD_H)
        self._multiline = multiline

    def draw(self) -> None:
        af = self.canv.acroForm
        af.textfield(
            name=self._namer.next(self._base),
            x=0, y=0, width=self.width, height=self.height,
            borderWidth=0.5, borderColor=_LINE, fillColor=None,
            fontName="Helvetica", fontSize=8.5,
            fieldFlags="multiline" if self._multiline else "",
        )


class _ChoiceFieldFlowable(Flowable):
    """An empty AcroForm dropdown (choice) field carrying the definition's options."""

    def __init__(self, namer: _FieldNamer, base: str, width: float, options: list[str]) -> None:
        super().__init__()
        self._namer = namer
        self._base = base
        self.width = width
        self.height = _FIELD_H
        # An ASCII "(select)" leading option so the dropdown starts on a clearly-empty
        # prompt rather than pre-selecting a real value. reportlab's acroform.choice
        # raises UnboundLocalError on a FALSY initial `value` (a library bug), so the
        # initial value is this non-falsy ASCII sentinel — NOT a non-ASCII placeholder
        # (an em-dash trips reportlab's Helvetica encoding with KeyError).
        self._options = [_SELECT_PROMPT] + [str(o) for o in options]

    def draw(self) -> None:
        af = self.canv.acroForm
        af.choice(
            name=self._namer.next(self._base),
            x=0, y=0, width=self.width, height=self.height,
            options=self._options, value=_SELECT_PROMPT,
            borderWidth=0.5, borderColor=_LINE, fillColor=None,
            fontName="Helvetica", fontSize=8.5,
        )


class _CheckboxFieldFlowable(Flowable):
    """A row of one or more labelled AcroForm checkboxes (e.g. a rating scale)."""

    def __init__(self, namer: _FieldNamer, base: str, labels: list[str]) -> None:
        super().__init__()
        self._namer = namer
        self._base = base
        self._labels = [str(label) for label in labels]
        self._gap = 6.0
        self._label_pad = 3.0
        self._font = "Helvetica"
        self._fs = 8.0
        from reportlab.pdfbase.pdfmetrics import stringWidth
        self._w = stringWidth
        self.height = max(_CHECK_SZ, self._fs + 2)
        self.width = sum(
            _CHECK_SZ + self._label_pad + self._w(label, self._font, self._fs) + self._gap
            for label in self._labels
        )

    def draw(self) -> None:
        af = self.canv.acroForm
        c = self.canv
        x = 0.0
        y_box = (self.height - _CHECK_SZ) / 2
        for label in self._labels:
            af.checkbox(
                name=self._namer.next(f"{self._base}_{label}"),
                x=x, y=y_box, size=_CHECK_SZ,
                borderWidth=0.5, borderColor=_LINE, fillColor=None, checked=False,
            )
            x += _CHECK_SZ + self._label_pad
            c.setFont(self._font, self._fs)
            c.setFillColor(colors.black)
            c.drawString(x, (self.height - self._fs) / 2 + 1, label)
            x += self._w(label, self._font, self._fs) + self._gap


def _blank_field_cell(field: dict, namer: _FieldNamer, width: float) -> Flowable:
    """Map one Field (header col / table col) onto its fillable widget by `input`.

    text→text · textarea→multiline · date→labelled text · time→labelled text(HH:MM) ·
    number→text · select→dropdown(choice w/ options) · signature→sign-by-hand LINE.
    The label hints (e.g. "(MM/DD/YYYY)") are placed BEFORE the field so the printed
    or on-screen form tells the filler the expected format.
    """
    inp = field.get("input", "text")
    base = field.get("key", "field")
    if inp == "signature":
        # No typed signature on a manual form — a sign-by-hand baseline (matches the
        # submission renderer's signature baseline) rather than an AcroForm field.
        return SignatureDrawing("", width=min(width, 200), height=28)
    if inp == "select":
        return _ChoiceFieldFlowable(namer, base, width, field.get("options", []) or [])
    if inp == "textarea":
        return _TextFieldFlowable(namer, base, width, multiline=True)
    # text / date / time / number all become single-line text fields. date/time carry
    # a format hint in the label rendered by the caller; the field itself is free text.
    return _TextFieldFlowable(namer, base, width)


def _format_hint(inp: str) -> str:
    """A short, human format hint appended to date/time/number field labels."""
    return {"date": "  (MM/DD/YYYY)", "time": "  (HH:MM)", "number": "  (#)"}.get(inp, "")


def _blank_header_section(fields: list[dict], st: dict, namer: _FieldNamer) -> list[Flowable]:
    """Header Field[] as fillable meta rows. Manual fallback = NO system-resolved
    job/PM/date, so the envelope keys (work_date/job) ARE shown as fillable here
    (unlike the submission header, which hides them because intake fills them)."""
    rows: list[list[Any]] = []
    for f in fields:
        label = f.get("label", "") + _format_hint(f.get("input", "text"))
        rows.append([_p(label, st["colhead"]), _blank_field_cell(f, namer, 4.0 * inch)])
    if not rows:
        return []
    t = Table(rows, colWidths=[2.2 * inch, _CONTENT_W - 2.2 * inch])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, _LINE),
        ("LINEBEFORE", (0, 0), (0, -1), 1.4, _TINT),
        ("LEFTPADDING", (0, 0), (0, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return [t]


def _blank_table_section(section: dict, st: dict, namer: _FieldNamer) -> list[Flowable]:
    """repeating_table / signature_table → header row + EXACTLY min_rows blank rows of
    fillable fields. NO add/delete affordance (a static paper form). Per-column width
    is the available width split evenly across the columns."""
    cols = section["columns"]
    n_rows = _min_rows(section)
    col_w = _CONTENT_W / max(len(cols), 1)
    head = [_p(c["label"] + _format_hint(c.get("input", "text")), st["colhead"]) for c in cols]
    body: list[list[Any]] = [head]
    for _ in range(n_rows):
        body.append([_blank_field_cell(c, namer, col_w - 8) for c in cols])
    t = Table(body, colWidths=[col_w] * len(cols), repeatRows=1)
    style = _grid_style(len(body), len(cols))
    style.add("VALIGN", (0, 1), (-1, -1), "MIDDLE")  # center the fillable widgets
    style.add("TOPPADDING", (0, 1), (-1, -1), 5)
    style.add("BOTTOMPADDING", (0, 1), (-1, -1), 5)
    t.setStyle(style)
    out: list[Flowable] = []
    if section.get("title"):
        out.append(_section_header(section["title"], st))
    out.append(t)
    return out


def _blank_checklist_item_response(it: dict, group: dict, namer: _FieldNamer,
                                   st: dict) -> Flowable:
    """The fillable response widget for one checklist Item, by Item.kind.

    rated (default) → one checkbox per group/item scale value (N/A is an explicit box,
        DISTINCT from blank — matches the submission renderer's N/A-vs-blank rule);
    circle_one → checkbox set over the item's own options/scale (print-and-circle);
    numeric → a number text field;
    text → a text field.
    """
    kind = it.get("kind", "rated")
    base = it.get("key", "item")
    if kind == "numeric":
        return _TextFieldFlowable(namer, base, 0.9 * inch)
    if kind == "text":
        return _TextFieldFlowable(namer, base, 1.7 * inch)
    if kind == "circle_one":
        opts = it.get("options") or it.get("scale") or group.get("scale", [])
        return _CheckboxFieldFlowable(namer, base, opts)
    # rated (default): the group's scale (item may override its own scale).
    scale = it.get("scale") or group.get("scale", [])
    return _CheckboxFieldFlowable(namer, base, scale)


def _blank_checklist_section(section: dict, st: dict, namer: _FieldNamer) -> list[Flowable]:
    """checklist → render EVERY group/item as a fixed list (not a row table), each item
    with its kind-appropriate fillable response + (when comment/comment_per_item) a
    comment text field. Matches the submission checklist's per-group table shape."""
    out: list[Flowable] = []
    if section.get("title"):
        out.append(_section_header(section["title"], st))
    for g in section["groups"]:
        want_comment = bool(g.get("comment_per_item")) or any(
            it.get("comment") for it in g["items"]
        )
        head = [_p("Item", st["colhead"]), _p("Response", st["colhead"])]
        if not want_comment:
            col_w = [_CONTENT_W - 1.9 * inch, 1.9 * inch]
        else:
            col_w = [_CONTENT_W - 3.5 * inch, 1.7 * inch, 1.8 * inch]
        if want_comment:
            head.append(_p("Comments", st["colhead"]))
        rows: list[list[Any]] = [head]
        for it in g["items"]:
            # Per-item override: an explicit "comment": false suppresses the comment
            # field for that one item even in a comment_per_item group.
            item_comment = want_comment and it.get("comment", True)
            row: list[Any] = [
                _p(it["label"], st["cell"]),
                _blank_checklist_item_response(it, g, namer, st),
            ]
            if want_comment:
                row.append(
                    _TextFieldFlowable(namer, f"{it.get('key', 'item')}_comment", 1.8 * inch)
                    if item_comment else _p("", st["cell"])
                )
            rows.append(row)
        t = Table(rows, colWidths=col_w, repeatRows=1)
        style = _grid_style(len(rows), len(col_w))
        style.add("VALIGN", (0, 1), (-1, -1), "MIDDLE")
        t.setStyle(style)
        out.append(_group_header(g["label"], " / ".join(g["scale"]), st))
        out.append(t)
    return out


def _min_rows(section: dict) -> int:
    """The number of blank rows to emit for a static row table.

    Reads `min_rows` from the definition (the DEFINITION wins). Falls back to the SPA
    repeating-table component's default of 1 visible row when a table omits `min_rows`,
    rather than guessing a number. No add/delete affordance is rendered either way.
    """
    mr = section.get("min_rows")
    return int(mr) if isinstance(mr, int) and mr > 0 else 1


def _blank_section_flowables(section: dict, st: dict, namer: _FieldNamer) -> list[Flowable]:
    """Blank-mode dispatcher. The two text-only sections (`static_text`,
    `content_blocks`) delegate to the EXISTING submission `_section_flowables` (empty
    values) so legal/mandatory wording and the toolbox-talk body are byte-identical to
    the digital form — they cannot diverge."""
    typ = section["type"]
    if typ == "header":
        return _blank_header_section(section["fields"], st, namer)
    if typ in ("repeating_table", "signature_table"):
        return _blank_table_section(section, st, namer)
    if typ == "checklist":
        return _blank_checklist_section(section, st, namer)
    if typ == "freeform":
        multiline = section.get("input", "textarea") == "textarea"
        return [_section_header(section["label"], st, level="group"),
                _TextFieldFlowable(namer, section.get("key", "freeform"), _CONTENT_W,
                                   multiline=multiline, height=_FIELD_H * 3 if multiline else None)]
    if typ in ("static_text", "content_blocks", "guidance", "form_link"):
        # Value-free section types render VERBATIM via the submission path —
        # guarantees no divergence (guidance/form_link added for the SOP daily form,
        # slice D1: same heading + callout-only / label + pointer rendering).
        return _section_flowables(section, {}, st)
    if typ == "job_requirements":
        # Per-job daily-form requirements (slice D4): the items are per-JOB runtime data
        # (D1 overlay), unknowable to a blank template — render the title + an explicit
        # placeholder line so the manual-fallback form says what belongs here instead of
        # silently omitting the section.
        return [_section_header(section.get("title", "Job-specific requirements"), st),
                _p("(job-specific items appear here)", st["caption"])]
    if typ == "expected_materials":
        # Expected-materials receipt list (Material receipts M2): the rows are per-JOB
        # runtime data (D1 job_expected_materials), unknowable to a blank template — title
        # + an explicit placeholder, mirroring job_requirements (never a silent omission).
        return [_section_header(section.get("title", "Expected materials"), st),
                _p("(this job's expected materials appear here on screen — record receipts "
                   "under Deliveries Received)", st["caption"])]
    if typ == "additional_photos":
        # Additional-photos pool mount (DR-photo-pool Slice 1): a live-upload affordance
        # (each extra photo uploads individually to the portal's screening pool),
        # meaningless on a hand-filled blank — title + an explicit placeholder, mirroring
        # job_requirements/expected_materials (never a silent omission).
        return [_section_header(section.get("title", "Additional site photos"), st),
                _p("(additional photos are attached on screen — the portal screens and "
                   "files them with the report)", st["caption"])]
    logger.warning("form_pdf: unknown section type %r — skipped (blank)", typ)
    return []


def render_blank_fillable(definition: dict) -> bytes:
    """Render a BLANK, fillable-AcroForm PDF of `definition` to bytes (PR-L).

    The manual-fallback sibling of `render_submission_pdf`: same platypus layout,
    section order, branding, footer, and page geometry, but every place the submission
    renderer draws a value, this emits an empty AcroForm field (text / checkbox /
    choice) at the layout position. `static_text` and `content_blocks` render through
    the SAME code path as the submission renderer (verbatim — no divergence).

    Deterministic; NO AI, NO network, NO Smartsheet/Box/Graph — pure data → bytes,
    same as `render_submission_pdf`. The Box upload of these blanks lives only in
    `scripts/generate_form_archive.py`, NOT here, so this module stays send-free and
    outside the network-capability gate (tests/test_capability_gating.py).
    """
    st = _styles()
    namer = _FieldNamer()
    title = definition.get("branding", {}).get("title") or definition.get("form_name", "")
    flow: list[Flowable] = _brand_header(title, st)
    if definition.get("variant_label"):
        flow.append(_p(f"Type: {definition['variant_label']}", st["meta"]))
    # Manual-fallback hint line: this is the BLANK form, fill by hand or on screen.
    flow.append(_p("BLANK FORM — manual fallback. Fill on screen or print and complete by hand.",
                   st["legal"]))
    flow.append(Spacer(1, 6))

    for section in definition.get("sections", []):
        try:
            flow.extend(_blank_section_flowables(section, st, namer))
        except Exception:  # one bad section must not abort the whole document
            logger.exception("form_pdf: blank section render failed (type=%s) — skipped",
                             section.get("type"))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            title=f"{definition.get('form_name', 'Safety form')} (fillable)",
                            leftMargin=_MARGIN, rightMargin=_MARGIN,
                            topMargin=_MARGIN, bottomMargin=0.7 * inch)
    doc.build(flow, canvasmaker=_canvas_maker(f"{title} — fillable"))
    return buf.getvalue()


# The manual-fallback instructions, one numbered step per line. Kept as data so the
# wording is reviewable in one place; the steps describe the SEND-FREE manual path
# (email the completed PDF to the job's Safety-Reports contact) — ITS does not send it.
_COVER_STEPS: list[str] = [
    "1. The portal is the normal way to file a safety report. Use this archive only "
    "when the portal is unavailable.",
    "2. Open the blank form you need (in this same Box folder). Each form name ends "
    "with “(fillable).pdf”.",
    "3. Fill it in — either on screen in a PDF reader, or print it and complete it "
    "by hand. Always include the job name, the date, and your name.",
    "4. Email the completed PDF to the job’s Safety-Reports contact and CC the "
    "office. Look the contact up in ITS_Active_Jobs (it stays up even when the "
    "portal is down).",
    "5. The emailed PDF is the official record — there is NO need to re-enter it "
    "into the portal once the portal is back.",
    "6. If a paper form runs out of rows, continue on an additional copy of the same "
    "form and staple them together.",
]


def render_cover_sheet() -> bytes:
    """Render the ONE manual-fallback cover/instructions sheet (PR-L) to bytes.

    Lives once in the archive's 00_Form_Archive folder (not per-form). Plain steps for
    a field PM when the portal is unavailable. Pure bytes — NO AI, NO network, NO send.
    """
    st = _styles()
    flow: list[Flowable] = _brand_header("Safety Forms — Manual Fallback (portal unavailable)", st)
    flow.append(_p("Use these blank fillable forms only when the Safety Portal is down. "
                   "Normal filing is through the portal.", st["body"]))
    flow.append(_section_header("What to do", st))
    for step in _COVER_STEPS:
        flow.append(_p(step, st["body"]))
    flow.append(Spacer(1, 10))
    flow.append(_p("The manual record stands. No re-entry when the portal returns.", st["legal"]))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, title="Safety Forms — Manual Fallback",
                            leftMargin=_MARGIN, rightMargin=_MARGIN,
                            topMargin=_MARGIN, bottomMargin=0.7 * inch)
    doc.build(flow, canvasmaker=_canvas_maker("Manual Fallback"))
    return buf.getvalue()
