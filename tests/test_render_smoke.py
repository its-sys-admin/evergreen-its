"""Auto-publish render smoke net (Phase-2 slice 3c, design brief C5).

Every ACTIVE form in the catalog manifest (`safety_portal/catalog.json`) must render
NON-DEGRADED through BOTH Python renderers:

  1. `render_submission_pdf` — with a REALISTIC, fully-populated submission fixture
     synthesized from the form definition (every field / table row / checklist item /
     freeform / signature filled with a plausible value).
  2. `render_blank_fillable` — the blank, fillable-AcroForm sibling.

"Non-degraded" is asserted as real CONTENT, not "bytes start with %PDF-": the renderer's
output text (extracted with pypdf, the text-extraction lib already in deps) must contain a
representative subset of the definition's structural strings — section titles, field labels,
checklist item labels, table column headers, static/legal text — plus the synthesized
values themselves for the submission renderer. This is the safety net that makes the
no-human-merge-gate auto-publish (brief C12) safe: a renderer that silently drops a section
or label is caught here, not in production.

The form list is driven by the MANIFEST active set — each parent's `forms` with
`status == "active"`, taking `current_form_code` — NOT a `safety_portal/forms/*.json` glob,
so a retired/inactive form (or a stray reference file) is never smoke-tested.

Preservation (Op Stds §14): reuses the `_pdf_text` / `_norm` extraction helpers' shape from
tests/test_form_pdf.py and the `_fields` shape from tests/test_form_archive.py; form_pdf.py
itself is untouched. Additive test file only.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

import pypdf
import pytest

from safety_reports import photo_screen
from safety_reports.form_pdf import (
    _ENVELOPE_KEYS,
    load_definition,
    render_blank_fillable,
    render_submission_pdf,
)

_ROOT = Path(__file__).resolve().parents[1]
_CATALOG = _ROOT / "safety_portal" / "catalog.json"

# A small, valid signature value in the pad's "M x y L x y …" path grammar (the same
# shape SignaturePad.tsx emits and tests/test_form_pdf.py uses). Two strokes so the
# signature renders at least one drawable line.
_SIG_VALUE = "M 10 20 L 30 40 L 60 25 M 80 30 L 120 60"

# Leading bullet marker on a content_blocks body line — stripped by the renderer (see the
# content_blocks branch of _expected_structural_strings).
_BULLET_LEAD = re.compile(r"^\s*[-•*]\s+")


# ── manifest-driven active-form discovery ──────────────────────────────────────
def _active_form_codes() -> list[str]:
    """Every ACTIVE form's `current_form_code`, in manifest order.

    Drives the smoke set from the catalog's active set (NOT a glob), so retired or
    inactive forms are never smoke-tested. Mirrors the portal's own form-resolution.
    """
    manifest = json.loads(_CATALOG.read_text())
    codes: list[str] = []
    for parent in manifest.get("parents", []):
        for form in parent.get("forms", []):
            if form.get("status") == "active":
                code = form.get("current_form_code")
                assert code, f"active form in {parent.get('parent_form_code')!r} has no current_form_code"
                codes.append(code)
    return codes


_ACTIVE_FORM_CODES = _active_form_codes()


def _pdf_text(pdf_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return " ".join(page.extract_text() for page in reader.pages)


def _norm(s: str) -> str:
    return " ".join(s.split())


def _fields(pdf_bytes: bytes) -> dict:
    return pypdf.PdfReader(io.BytesIO(pdf_bytes)).get_fields() or {}


# ── realistic submission fixture, synthesized from the definition ──────────────
def _tiny_jpeg() -> bytes:
    """A tiny runtime-generated JPEG standing in for a §34-screened photo (generated
    with Pillow at test time — binaries are never committed)."""
    from PIL import Image as _PILImage

    buf = io.BytesIO()
    _PILImage.new("RGB", (48, 36), (12, 100, 60)).save(buf, format="JPEG")
    return buf.getvalue()


def _field_value(field: dict) -> Any:
    """A plausible value for ONE field, by its `input` type.

    text/textarea → a marker token, date → an ISO date, time → HH:MM, number → a number,
    select → the FIRST option, signature → a valid pad path.
    """
    inp = field.get("input", "text")
    if inp == "signature":
        return _SIG_VALUE
    if inp == "date":
        return "2026-06-03"
    if inp == "time":
        return "08:30"
    if inp == "number":
        return "42"
    if inp == "select":
        opts = field.get("options") or []
        return opts[0] if opts else "X"
    # text / textarea / anything else: a short, recognizable token.
    return "X"


def _scale_value(item: dict, group: dict) -> str:
    """A plausible checklist response for one item.

    circle_one → first of the item's own options; numeric → a number; text → a token;
    rated (default) → the FIRST value on the (item-override-or-group) scale. Always a
    concrete answer (never blank) so the synthesized submission is COMPLETE.
    """
    kind = item.get("kind", "rated")
    if kind == "numeric":
        return "100"
    if kind == "text":
        return "note"
    if kind == "circle_one":
        opts = item.get("options") or item.get("scale") or group.get("scale", [])
        return str(opts[0]) if opts else "X"
    scale = item.get("scale") or group.get("scale", [])
    return str(scale[0]) if scale else "X"


def _synthesize_submission(definition: dict) -> dict:
    """Build a REALISTIC, fully-populated submission `values` map from the definition.

    Fills EVERY field / table (≥1 row) / checklist item / freeform with a plausible
    value so the submission renderer exercises every value-bearing branch — the point of
    a non-degraded smoke test. Static text / content blocks consume no values.

    Photo fields additionally synthesize `photo_groups` — one (label, [(caption,
    tiny_jpeg)]) group per header photo field, mirroring what intake's
    `_screen_portal_photos` hands the renderer — so every photo field's label is
    exercised as a rendered group heading (and asserted as a submission-mode needle).
    """
    values: dict[str, Any] = {}
    for section in definition.get("sections", []):
        typ = section.get("type")
        if typ == "header":
            for f in section.get("fields", []):
                values[f["key"]] = _field_value(f)
        elif typ in ("repeating_table", "signature_table"):
            # ≥1 row (use min_rows when >1 so the table is realistically populated).
            n = max(int(section.get("min_rows") or 1), 1)
            cols = section.get("columns", [])
            values[section["key"]] = [
                {c["key"]: _field_value(c) for c in cols} for _ in range(n)
            ]
        elif typ == "checklist":
            cl: dict[str, Any] = {}
            for g in section.get("groups", []):
                for it in g.get("items", []):
                    cl[it["key"]] = {
                        "response": _scale_value(it, g),
                        "comment": "ok",
                    }
            values[section["key"]] = cl
        elif typ == "freeform":
            values[section["key"]] = "Synthesized freeform answer."
        elif typ == "additional_photos":
            # DR-photo-pool Slice 1: the submission carries POOL REFERENCES only (the bytes
            # went to daily_photo_pool via their own bounded uploads) — synthesize two refs so
            # the renderer's caption-table branch renders (empty would take the no-photos line).
            values[section["key"]] = [
                {"pool_id": 101, "caption": "Synthesized pool photo caption"},
                {"pool_id": 102},
            ]
        elif typ == "job_requirements":
            # Slice D4: the per-job overlay's SELF-DESCRIBING answers array — the portal
            # captures it at fill time; the smoke synthesizes one of each answerable kind
            # so the generic label→response table branch renders (empty → section skipped,
            # which would silently drop the title needle below).
            values[section["key"]] = [
                {"label": "Synthesized client note", "kind": "note", "response": ""},
                {"label": "Synthesized confirm requirement", "kind": "confirm",
                 "response": "Confirmed"},
                {"label": "Synthesized text requirement", "kind": "text",
                 "response": "Synthesized requirement answer."},
            ]
        # static_text / content_blocks: nothing to fill.
    sub: dict[str, Any] = {"job_name": "Bradley 1", "work_date": "2026-06-03", "values": values}
    # One screened photo per header photo field, grouped per field — the shape intake's
    # _screen_portal_photos produces. Renders each field's label as a group heading.
    groups = [
        (label, [(f"synth-{key}.jpg", _tiny_jpeg())])
        for key, label, _max_count in photo_screen.iter_photo_fields(definition)
    ]
    if groups:
        sub["photo_groups"] = groups
    return sub


# ── expected structural strings the rendered text MUST contain ─────────────────
def _expected_structural_strings(definition: dict, *, mode: str) -> list[str]:
    """A representative subset of strings that MUST survive into the rendered text.

    Section titles, header field labels, table column headers, checklist group labels +
    item labels, freeform labels, and static/legal/content text. If the renderer silently
    drops a section or label, one of these goes missing and the smoke test fails. We
    normalize and substring-match (some labels wrap / get escaped), and we cap very long
    labels to a stable prefix so PDF line-wrapping inside a cell can't split the needle.

    Three renderer facts are modeled so the needles match ACTUAL renderer behavior (a
    needle the renderer is designed never to draw would be a false positive):
      * A `header` section's `title` is NOT rendered by either header builder
        (`_header_section` / `_blank_header_section` draw only the field table) — so a
        header title is never an expected needle.
      * The envelope-key field labels (work_date / job) are SKIPPED by the submission
        header builder (intake resolves those), but the BLANK builder DOES render them.
        So they're needles in blank mode, not in submission mode.
      * A `photo` header field never renders as a labeled header row in submission mode
        (`_header_section` never inlines the untrusted base64 value) — instead its label
        renders as the HEADING of that field's §34 screened photo-group grid
        (`_photo_grid`; the fixture synthesizes one tiny screened JPEG per photo field).
        The BLANK builder renders its label beside a bordered, NON-interactive portal
        note (no AcroForm widget — photos cannot ride a paper form). So a photo label
        (e.g. daily-report "Site photos") is a needle in BOTH modes.
    """
    out: list[str] = []

    def add(s: str | None) -> None:
        if not s:
            return
        norm = _norm(s)
        # Cap to a prefix short enough to survive in-cell wrapping but long enough to be
        # a meaningful structural needle (labels in these forms are unique within ~40 ch).
        out.append(norm[:40])

    # Always-present branding (proves the document chrome rendered at all).
    out.append("EVERGREEN RENEWABLES")

    for section in definition.get("sections", []):
        typ = section.get("type")
        # A header section's title is never rendered (see docstring); other sections
        # DO render their title, so only add it for non-header sections.
        if typ != "header":
            add(section.get("title"))
        if typ == "header":
            for f in section.get("fields", []):
                # In submission mode the envelope-key labels are intentionally skipped.
                # A photo field's label IS a needle in both modes: in submission mode it
                # renders as the photo-group grid heading (the fixture synthesizes one
                # screened JPEG per photo field), in blank mode as the labeled note row
                # — see the renderer facts in the docstring.
                if mode == "submission" and f.get("key") in _ENVELOPE_KEYS:
                    continue
                add(f.get("label"))
        elif typ in ("repeating_table", "signature_table"):
            for c in section.get("columns", []):
                add(c.get("label"))
        elif typ == "checklist":
            for g in section.get("groups", []):
                add(g.get("label"))
                # A representative subset of item labels (first 3 of each group is enough
                # to catch a dropped group/section; all of them would be brittle to wrap).
                for it in g.get("items", [])[:3]:
                    add(it.get("label"))
        elif typ == "freeform":
            add(section.get("label"))
        elif typ == "static_text":
            add(section.get("text"))
        elif typ == "content_blocks":
            for b in section.get("blocks", [])[:2]:
                add(b.get("heading"))
                # A body line LEADING with a bullet marker ("- x" / "• x" / "* x") is drawn
                # by form_pdf._prose_flowables as a hanging-indent bullet whose text is
                # line[2:].strip() — the marker is STRIPPED and redrawn as the bulletText
                # glyph (form_pdf.py:400-401). The needle must drop it too, or any
                # heading-less bullet-leading block (the shape of every OSHA toolbox talk
                # whose source is a bare bulleted list) reports a false degradation.
                add(_BULLET_LEAD.sub("", b.get("body") or "", count=1))
        elif typ == "guidance":
            # Both PDF renderers deliberately render a guidance section as its HEADING
            # + CALLOUT one-liners ONLY (form_pdf._section_flowables — the p/bullets
            # prose is on-screen-only, or the weekly packet would bloat). So heading +
            # callouts are the needles; p/bullets text must NOT be expected here.
            add(section.get("heading"))
            for b in section.get("blocks", []):
                if b.get("type") == "callout":
                    add(b.get("text"))
        elif typ == "form_link":
            # Rendered as the label + a fixed "see filed forms" pointer line.
            add(section.get("label"))
    # De-dupe while preserving order; drop empties.
    seen: set[str] = set()
    uniq: list[str] = []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def _assert_non_degraded(text: str, definition: dict, code: str, *, mode: str) -> None:
    """Assert the rendered text carries a representative subset of the expected structure.

    Not a heuristic on byte size: a true content assertion that each expected structural
    needle (section title / label / static text) survived into the extracted text. A
    renderer that drops a section or mangles a label fails HERE.
    """
    needles = _expected_structural_strings(definition, mode=mode)
    assert needles, f"{code}: definition produced no structural needles to assert"
    missing = [n for n in needles if n not in text]
    assert not missing, (
        f"{code} [{mode}]: render DEGRADED — these expected structural strings are "
        f"absent from the extracted PDF text: {missing[:8]}"
        + (f" (+{len(missing) - 8} more)" if len(missing) > 8 else "")
    )


# ── the manifest is non-empty (guards against a glob/parse regression) ─────────
def test_manifest_active_set_non_empty() -> None:
    assert _ACTIVE_FORM_CODES, "no active forms discovered in the catalog manifest"
    # Every active code must resolve to a real definition on disk.
    for code in _ACTIVE_FORM_CODES:
        assert load_definition(code) is not None, f"active form {code!r} has no definition file"


# ── submission renderer: realistic fixture → non-degraded ──────────────────────
@pytest.mark.parametrize("code", _ACTIVE_FORM_CODES, ids=lambda c: c)
def test_active_form_submission_renders_non_degraded(code: str) -> None:
    definition = load_definition(code)
    assert definition is not None, f"{code}: definition did not load"

    submission = _synthesize_submission(definition)
    out = render_submission_pdf(definition, submission)
    assert out[:5] == b"%PDF-", f"{code}: submission render is not a PDF"

    reader = pypdf.PdfReader(io.BytesIO(out))
    assert len(reader.pages) >= 1, f"{code}: zero-page submission PDF"

    text = _norm(_pdf_text(out))
    _assert_non_degraded(text, definition, code, mode="submission")

    # The synthesized header values themselves must survive (proves field VALUES, not
    # just labels, render — the failure mode where labels print but data drops).
    assert "Bradley 1" in text, f"{code}: synthesized job_name did not render"


# ── blank fillable renderer: non-degraded structure + AcroForm fields ──────────
@pytest.mark.parametrize("code", _ACTIVE_FORM_CODES, ids=lambda c: c)
def test_active_form_blank_renders_non_degraded(code: str) -> None:
    definition = load_definition(code)
    assert definition is not None, f"{code}: definition did not load"

    out = render_blank_fillable(definition)
    assert out[:5] == b"%PDF-", f"{code}: blank render is not a PDF"

    reader = pypdf.PdfReader(io.BytesIO(out))
    assert len(reader.pages) >= 1, f"{code}: zero-page blank PDF"

    text = _norm(_pdf_text(out))
    _assert_non_degraded(text, definition, code, mode="blank")

    # A blank fillable form MUST carry AcroForm widgets when the definition has at
    # least one WIDGET-BEARING input — a dropped section that removed all fields would
    # be a degraded blank render. `photo` (bordered portal note) and `signature`
    # (sign-by-hand line) deliberately consume NO AcroForm field, so a form whose only
    # inputs are those two legitimately renders widget-free (matches
    # tests/test_form_archive.py's predicate).
    widgetless = {"photo", "signature"}
    has_widget_bearing_input = any(
        s.get("type") in ("checklist", "freeform")
        or (s.get("type") == "header"
            and any(f.get("input", "text") not in widgetless for f in s.get("fields", [])))
        or (s.get("type") in ("repeating_table", "signature_table")
            and any(c.get("input", "text") not in widgetless for c in s.get("columns", [])))
        for s in definition.get("sections", [])
    )
    if has_widget_bearing_input:
        assert _fields(out), f"{code}: blank fillable produced no AcroForm fields (degraded)"
