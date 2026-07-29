"""Render-parity tests for safety_reports/form_pdf.py.

Renders each landed form definition to PDF, extracts the text back out (pypdf), and
asserts the source labels / mandatory legal text / N/A-vs-blank render faithfully.
"""
from __future__ import annotations

import ast
import io
import json
from pathlib import Path

import pypdf
import pytest

from safety_reports import form_pdf
from safety_reports.form_pdf import (
    _parse_ml_path,
    incomplete_checklist_items,
    load_definition,
    merge_pdfs,
    render_submission_pdf,
)

_ROOT = Path(__file__).resolve().parents[1]
FORMS_DIR = _ROOT / "safety_portal" / "forms"
DEF_PATHS = sorted(p for p in FORMS_DIR.glob("*.json") if p.name != "meta-schema.json")


def _load(name: str) -> dict:
    return json.loads((FORMS_DIR / name).read_text())


def _pdf_text(pdf_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return " ".join(page.extract_text() for page in reader.pages)


def _norm(s: str) -> str:
    return " ".join(s.split())


# ── every form renders to a real PDF ──────────────────────────────────────────
@pytest.mark.parametrize("path", DEF_PATHS, ids=lambda p: p.stem)
def test_every_form_renders_to_pdf(path: Path) -> None:
    definition = json.loads(path.read_text())
    out = render_submission_pdf(
        definition, {"job_name": "Bradley 1", "work_date": "2026-06-03", "values": {}}
    )
    assert out[:5] == b"%PDF-", "not a PDF"
    assert len(out) > 1200, "implausibly small PDF"


# ── header + envelope + a field label ─────────────────────────────────────────
def test_jha_header_and_mandatory_footer() -> None:
    out = render_submission_pdf(
        _load("jha-v1.json"),
        {
            "job_name": "Bradley 1",
            "work_date": "2026-06-03",
            "values": {
                "work_location": "Array B",
                "crew_members": "A. Smith, B. Jones",
                "hazard_analysis": [{"task": "Lift panels", "hazards": "Strain", "mitigation": "2-person"}],
            },
        },
    )
    text = _norm(_pdf_text(out))
    assert "EVERGREEN RENEWABLES" in text
    assert "Bradley 1" in text and "2026-06-03" in text
    assert "Array B" in text and "Lift panels" in text
    # mandatory footer baked into the definition (non-editable)
    assert "REVIEW AND REVISE THE PLAN" in text
    assert "Worker Acknowledgement" in text


# ── equipment: lock/tag-out legal text + N/A distinct from blank ───────────────
def test_skid_steer_lockout_and_na_vs_blank() -> None:
    definition = _load("equipment-skid-steer-v1.json")
    submission = {
        "job_name": "Huntley",
        "work_date": "2026-06-03",
        "values": {
            "operator": "Daniel Field",
            "inspection": {
                "bs_bucket": {"response": "OK"},
                "bs_tracks": {"response": "N/A"},   # deliberately not applicable
                # bs_belts left BLANK (not inspected)
                "as_fuel": {"response": "1/2"},
            },
        },
    }
    text = _norm(_pdf_text(render_submission_pdf(definition, submission)))
    assert "ALWAYS lock/tag-out unsafe equipment" in text
    assert "N/A" in text  # the N/A answer rendered
    assert "Daniel Field" in text

    # N/A is a COMPLETE answer; blank is incomplete. Only the blank is flagged.
    blanks = incomplete_checklist_items(definition, submission)
    blank_keys = {item_key for _sec, item_key, _label in blanks}
    assert "bs_belts" in blank_keys, "a blank item must be flagged incomplete"
    assert "bs_tracks" not in blank_keys, "N/A must NOT be flagged as blank/incomplete"
    assert "bs_bucket" not in blank_keys


def test_skid_steer_is_tristate() -> None:
    definition = _load("equipment-skid-steer-v1.json")
    checklist = next(s for s in definition["sections"] if s["type"] == "checklist")
    for g in checklist["groups"]:
        assert g["scale"] == ["OK", "NOT OK", "N/A"], "Skid Steer must be tri-state"


# ── toolbox content + sign-in render ──────────────────────────────────────────
def test_toolbox_content_and_signin_render() -> None:
    out = render_submission_pdf(
        _load("toolbox-talk-back-sprains-v1.json"),
        {"job_name": "Rockford", "work_date": "2026-06-03",
         "values": {"sign_in": [{"instructor_worker_name": "D. Field", "signature": "M 1 1 L 50 40"}]}},
    )
    text = _norm(_pdf_text(out))
    assert "5-MINUTE SAFETY TALK" in text
    assert "BACK SPRAINS AND STRAINS" in text
    assert "D. Field" in text


# ── signature path parsing ────────────────────────────────────────────────────
def test_parse_ml_path_splits_strokes() -> None:
    strokes = _parse_ml_path("M 1 2 L 3 4 L 5 6 M 7 8 L 9 10")
    assert len(strokes) == 2
    assert strokes[0] == [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]
    assert strokes[1] == [(7.0, 8.0), (9.0, 10.0)]


def test_parse_ml_path_tolerates_garbage() -> None:
    assert _parse_ml_path("") == []
    assert _parse_ml_path("M 1") == []  # truncated → no complete point


def test_signature_renders_without_error() -> None:
    # A JHA acknowledgement row carrying a real signature path must render.
    out = render_submission_pdf(
        _load("jha-v1.json"),
        {"job_name": "Bradley 1", "work_date": "2026-06-03",
         "values": {"worker_acknowledgement": [
             {"worker_name": "A. Smith", "company": "Evergreen", "signature": "M 10 20 L 30 40 L 60 25"}]}},
    )
    assert out[:5] == b"%PDF-"


# ── HSS&E sectioned assessment renders all sections ───────────────────────────
# ── weekly-packet merge (Sat→Fri) ─────────────────────────────────────────────
def _one_page(form: str, job: str) -> bytes:
    return render_submission_pdf(_load(form), {"job_name": job, "work_date": "2026-06-03", "values": {}})


def test_merge_concatenates_in_order() -> None:
    a = _one_page("jha-v1.json", "Bradley 1")
    b = _one_page("visitor-sign-in-v1.json", "Bradley 1")
    merged = merge_pdfs([a, b])
    reader = pypdf.PdfReader(io.BytesIO(merged))
    pa = len(pypdf.PdfReader(io.BytesIO(a)).pages)
    pb = len(pypdf.PdfReader(io.BytesIO(b)).pages)
    assert len(reader.pages) == pa + pb, "merged page count must equal the sum"
    text = _norm(" ".join(p.extract_text() for p in reader.pages))
    assert "JOB HAZARD ANALYSIS" in text and "VISITOR SIGN" in text.upper()


def test_merge_empty_raises() -> None:
    with pytest.raises(ValueError):
        merge_pdfs([])


def test_merge_single_pdf_roundtrips() -> None:
    a = _one_page("jha-v1.json", "Bradley 1")
    merged = merge_pdfs([a])
    assert merged[:5] == b"%PDF-"
    assert len(pypdf.PdfReader(io.BytesIO(merged)).pages) == len(pypdf.PdfReader(io.BytesIO(a)).pages)


def test_hsse_sections_render() -> None:
    out = render_submission_pdf(
        _load("hsse-work-observation-v1.json"),
        {"job_name": "Bradley 1", "work_date": "2026-06-03",
         "values": {"observer": "J. Lee", "risk_rating": "Medium",
                    "section_1": {"s1_1": {"response": "Acceptable", "comment": "ok"}}}},
    )
    text = _norm(_pdf_text(out))
    assert "SECTION 2: JOB PLAN EVALUATION" in text
    assert "J. Lee" in text


# ── load_definition (Phase-5 Python-side form-definition loader) ───────────────
def test_load_definition_known_form_returns_dict() -> None:
    d = load_definition("jha-v1")
    assert d is not None
    assert d["form_code"] == "jha-v1"
    assert "sections" in d


def test_load_definition_matches_every_shipped_form() -> None:
    # Every form file (except the meta-schema) must load by its filename stem.
    for path in DEF_PATHS:
        d = load_definition(path.stem)
        assert d is not None, f"{path.stem} failed to load"
        assert d.get("form_code") == path.stem


def test_load_definition_unknown_form_returns_none() -> None:
    assert load_definition("does-not-exist-v9") is None


@pytest.mark.parametrize(
    "bad",
    [
        "",                       # empty
        "jha-v1.json",            # has a dot → blocked (and would double-suffix)
        "../jha-v1",              # path traversal
        "jha/../../etc/passwd",   # path traversal
        "JHA-V1",                 # uppercase not in the charset
        "jha_v1",                 # underscore not in the charset
        "foo bar",                # space not in the charset
    ],
)
def test_load_definition_rejects_unsafe_form_codes(bad: str) -> None:
    assert load_definition(bad) is None


def test_load_definition_malformed_json_returns_none(tmp_path, monkeypatch) -> None:
    # Point the loader at a temp dir holding a malformed file.
    import safety_reports.form_pdf as fp

    bad = tmp_path / "broken-v1.json"
    bad.write_text("{ not valid json ")
    monkeypatch.setattr(fp, "_FORMS_DIR", tmp_path)
    assert fp.load_definition("broken-v1") is None


def test_load_definition_non_object_json_returns_none(tmp_path, monkeypatch) -> None:
    """Valid JSON whose top level is not an object (e.g. an array) → None."""
    import safety_reports.form_pdf as fp

    (tmp_path / "arr-v1.json").write_text("[1, 2, 3]")
    monkeypatch.setattr(fp, "_FORMS_DIR", tmp_path)
    assert fp.load_definition("arr-v1") is None


# ── site photos (PR-2) ────────────────────────────────────────────────────────
def _tiny_jpeg() -> bytes:
    from PIL import Image as _PILImage

    buf = io.BytesIO()
    _PILImage.new("RGB", (48, 36), (12, 100, 60)).save(buf, format="JPEG")
    return buf.getvalue()


_PHOTO_DEF = {
    "form_name": "JHA",
    "sections": [
        {
            "type": "header",
            "fields": [
                {"key": "job", "input": "text", "label": "Job"},
                {"key": "site_photos", "input": "photo", "label": "Site Photos", "max_count": 4},
            ],
        }
    ],
}


def test_additional_photo_notes_render_without_grid() -> None:
    # DR-photo-pool Slice 2: pending/refused pool-reference notes render even when
    # the photo grid itself is empty (a report whose only photos were refused still
    # SAYS so — never a silent omission); a zero count renders no line.
    out = render_submission_pdf(
        {"form_code": "daily-report-v6", "form_name": "Daily Field Report", "sections": []},
        {
            "job_name": "Bradley 1",
            "work_date": "2026-07-03",
            "values": {},
            "additional_photo_notes": {"pending": 2, "refused": 1, "unavailable": 0},
        },
    )
    assert out[:5] == b"%PDF-"
    text = _norm(_pdf_text(out))
    assert "2 additional photo(s) were still pending security screening at render time" in text
    assert "1 additional photo(s) refused by security screening" in text
    assert "unavailable" not in text  # zero count → no line


def test_additional_photo_notes_absent_render_nothing() -> None:
    out = render_submission_pdf(
        {"form_code": "daily-report-v6", "form_name": "Daily Field Report", "sections": []},
        {"job_name": "Bradley 1", "work_date": "2026-07-03", "values": {}},
    )
    text = _norm(_pdf_text(out))
    assert "additional photo(s)" not in text


def test_screened_photos_render_grid_with_caption() -> None:
    out = render_submission_pdf(
        _PHOTO_DEF,
        {
            "job_name": "Bradley 1",
            "work_date": "2026-06-12",
            "values": {},
            "screened_photos": [("front.jpg · 2026-06-12 09:30", _tiny_jpeg())],
        },
    )
    assert out[:5] == b"%PDF-"
    text = _norm(_pdf_text(out))
    assert "Site Photos" in text
    assert "front.jpg" in text


def test_header_photo_field_never_dumps_base64() -> None:
    """A header photo field whose value is raw base64 PhotoValue objects must NOT be
    rendered inline (the renderer skips input=='photo' header fields). Without
    screened_photos, no photo content appears and the base64 never leaks into the PDF."""
    import base64

    blob = base64.b64encode(_tiny_jpeg()).decode()
    out = render_submission_pdf(
        _PHOTO_DEF,
        {
            "job_name": "Bradley 1",
            "work_date": "2026-06-12",
            "values": {"site_photos": [{"data": blob, "name": "x.jpg", "taken_at": "", "gps": ""}]},
        },
    )
    text = _pdf_text(out)
    assert blob[:40] not in text          # the base64 payload never reaches the PDF
    assert "Site Photos" not in _norm(text)  # no grid without screened_photos


def test_unrenderable_screened_photo_is_skipped_not_fatal() -> None:
    """A corrupt JPEG in screened_photos is dropped (logged) — the document still renders."""
    out = render_submission_pdf(
        _PHOTO_DEF,
        {
            "job_name": "Bradley 1",
            "work_date": "2026-06-12",
            "values": {},
            "screened_photos": [("bad", b"\xff\xd8\xffnotanimage")],
        },
    )
    assert out[:5] == b"%PDF-"  # no crash; the bad photo is simply absent


def test_illegal_photo_table_column_never_dumps_base64() -> None:
    """A photo column in a repeating table is an illegal definition (photos are header-
    only) — the table renderer must emit a placeholder, never the raw base64 list."""
    import base64

    blob = base64.b64encode(_tiny_jpeg()).decode()
    definition = {
        "form_name": "JHA",
        "sections": [{
            "type": "repeating_table",
            "key": "rows",
            "title": "Crew",
            "columns": [
                {"key": "name", "input": "text", "label": "Name"},
                {"key": "pic", "input": "photo", "label": "Pic"},
            ],
        }],
    }
    submission = {
        "job_name": "Bradley 1", "work_date": "2026-06-12",
        "values": {"rows": [{"name": "Pat", "pic": [{"data": blob, "name": "x", "taken_at": "", "gps": ""}]}]},
    }
    out = render_submission_pdf(definition, submission)
    text = _pdf_text(out)
    assert blob[:40] not in text
    assert "photo omitted" in _norm(text)


# ── beautification (2026-06-15): branding · footer · weekly cover/index ─────────
def test_brand_name_present_via_footer_on_every_page() -> None:
    """The masthead is now a logo IMAGE (no extractable text), but the company name must
    still appear in the text layer — the footer carries 'EVERGREEN RENEWABLES' on every
    page (also what the legacy render-fidelity assertions rely on)."""
    out = render_submission_pdf(
        _load("equipment-skid-steer-v1.json"),
        {"job_name": "Bradley 1", "work_date": "2026-06-03", "values": {}},
    )
    reader = pypdf.PdfReader(io.BytesIO(out))
    assert len(reader.pages) >= 2
    for page in reader.pages:
        assert "EVERGREEN RENEWABLES" in _norm(page.extract_text())


def test_footer_has_page_x_of_y() -> None:
    out = render_submission_pdf(
        _load("equipment-skid-steer-v1.json"),
        {"job_name": "Bradley 1", "work_date": "2026-06-03", "values": {}},
    )
    n = len(pypdf.PdfReader(io.BytesIO(out)).pages)
    text = _norm(_pdf_text(out))
    assert f"Page {n} of {n}" in text  # last page's footer
    assert "Page 1 of" in text


def test_logo_missing_falls_back_to_text_wordmark(monkeypatch) -> None:
    """A missing/unreadable logo asset must degrade to the text wordmark, never raise —
    so the masthead still shows the brand and the renderer never fails on a bad asset."""
    import safety_reports.form_pdf as fp

    monkeypatch.setattr(fp, "_LOGO_PATH", Path("/nonexistent/evergreen-logo.png"))
    monkeypatch.setattr(fp, "_LOGO_CACHE", [])  # fresh memo so the bad path is re-probed
    out = fp.render_submission_pdf(_load("jha-v1.json"),
                                   {"job_name": "B1", "work_date": "2026-06-03", "values": {}})
    assert out[:5] == b"%PDF-"
    # the masthead wordmark + the footer both carry the brand name
    assert "EVERGREEN RENEWABLES" in _norm(_pdf_text(out))
    fp._LOGO_CACHE.clear()  # don't leak the patched-None memo into later tests


def test_na_vs_blank_still_distinct_after_colour_coding() -> None:
    """Colour-coding responses must NOT change the N/A-vs-blank semantics: an N/A item
    still PRINTS 'N/A' and is NOT flagged incomplete; a blank item prints nothing and IS
    flagged incomplete."""
    definition = _load("equipment-skid-steer-v1.json")
    submission = {
        "job_name": "B1", "work_date": "2026-06-03",
        "values": {"inspection": {
            "bs_tracks": {"response": "N/A"},     # deliberately not-applicable
            "bs_belts": {},                        # blank / not-yet-inspected
            "bs_bucket": {"response": "OK"},
        }},
    }
    text = _norm(_pdf_text(render_submission_pdf(definition, submission)))
    assert "N/A" in text
    blanks = {k for _, k, _ in incomplete_checklist_items(definition, submission)}
    assert "bs_belts" in blanks          # blank → incomplete
    assert "bs_tracks" not in blanks     # N/A → complete


def test_weekly_cover_and_index_render() -> None:
    from safety_reports.form_pdf import render_weekly_cover, render_weekly_index
    cover = render_weekly_cover("Bradley 1 Solar", "Week of Jun 7 – 13, 2026", 3,
                                compiled_display="Jun 13, 2026 2:02 PM")
    assert cover[:5] == b"%PDF-"
    ctext = _norm(_pdf_text(cover))
    assert "WEEKLY SAFETY REPORT" in ctext and "Bradley 1 Solar" in ctext
    idx = render_weekly_index("Bradley 1 Solar", "Week of Jun 7 – 13, 2026", [
        {"date_display": "Mon, Jun 8, 2026", "form_name": "Skid Steer", "start_page": 3},
        {"date_display": "Wed, Jun 10, 2026", "form_name": "JHA", "start_page": 5},
    ])
    itext = _norm(_pdf_text(idx))
    assert "Contents" in itext
    assert "Mon, Jun 8, 2026" in itext and "Wed, Jun 10, 2026" in itext
    assert "Page 3" in itext and "Page 5" in itext


def test_page_count_helper() -> None:
    from safety_reports.form_pdf import page_count
    one = render_submission_pdf(_load("jha-v1.json"),
                                {"job_name": "B1", "work_date": "2026-06-03", "values": {}})
    multi = render_submission_pdf(_load("equipment-skid-steer-v1.json"),
                                  {"job_name": "B1", "work_date": "2026-06-03", "values": {}})
    assert page_count(one) == len(pypdf.PdfReader(io.BytesIO(one)).pages)
    assert page_count(merge_pdfs([one, multi])) == page_count(one) + page_count(multi)


# ── P6 progress rollup-numbers page ─────────────────────────────────────────────
def test_progress_rollup_renders_all_sections() -> None:
    from safety_reports.form_pdf import render_progress_rollup
    out = render_progress_rollup(
        "Bradley 1 Solar", "Week of Jun 7 – 13, 2026",
        {"labor_hours": 42.5,
         "equipment": [{"name": "Skid Steer 3", "kind": "skid-steer"},
                       {"name": "Telehandler A", "kind": "telehandler"}],
         "open_tasks": 4, "materials": None},
    )
    assert out[:5] == b"%PDF-"
    text = _norm(_pdf_text(out))
    assert "Weekly Progress Rollup" in text and "Bradley 1 Solar" in text
    assert "Labor hours" in text and "42.5" in text
    assert "Equipment on site" in text and "Skid Steer 3" in text and "Telehandler A" in text
    assert "Open tasks" in text and "4 open" in text
    assert "Materials" in text and "not yet included" in text
    # NO progress-% anywhere (operator decision 2026-06-30).
    assert "% complete" not in text.lower() and "progress %" not in text.lower()


def test_progress_rollup_graceful_zero_state() -> None:
    from safety_reports.form_pdf import render_progress_rollup
    out = render_progress_rollup(
        "Empty Job", "Week of Jun 7 – 13, 2026",
        {"labor_hours": 0, "equipment": [], "open_tasks": 0, "materials": None},
    )
    assert out[:5] == b"%PDF-"
    text = _norm(_pdf_text(out))
    assert "No field-ops activity recorded for this week" in text


def test_progress_rollup_tolerates_malformed_numbers() -> None:
    # Untrusted JSON transport: a malformed shape must degrade, never raise.
    from safety_reports.form_pdf import render_progress_rollup
    out = render_progress_rollup(
        "Job X", "Week", {"labor_hours": "not-a-number", "equipment": "oops", "open_tasks": None},
    )
    assert out[:5] == b"%PDF-"
    # All-unparseable → treated as zeros → graceful zero-state.
    assert "No field-ops activity recorded for this week" in _norm(_pdf_text(out))


def test_progress_rollup_escapes_equipment_names() -> None:
    # Invariant 2: a field-reported equipment name with markup must render as plain text.
    from safety_reports.form_pdf import render_progress_rollup
    out = render_progress_rollup(
        "Job X", "Week",
        {"labor_hours": 8, "equipment": [{"name": "<b>Loader</b>", "kind": "&amp;"}],
         "open_tasks": 0},
    )
    assert out[:5] == b"%PDF-"  # no XML-parse blow-up from the raw markup
    assert "Loader" in _norm(_pdf_text(out))


# ── guidance + form_link sections (SOP daily form, slice D1) ────────────────────
_GUIDANCE_FIXTURE: dict = {
    "form_code": "sop-fixture-v1",
    "parent_form_code": "sop-fixture",
    "form_name": "SOP Fixture",
    "variant_label": None,
    "version": 1,
    "archetype": "sectioned_assessment",
    "source_pdf": "daily-field-report.pdf",
    "sections": [
        {
            "type": "guidance",
            "heading": "Trenching Duties",
            "blocks": [
                {"type": "p", "text": "Prose paragraph that stays on screen only."},
                {"type": "bullets", "items": ["Bullet alpha stays on screen.",
                                              "Bullet beta stays on screen."]},
                {"type": "callout", "style": "critical",
                 "text": "CRITICAL RULE: never enter an unprotected trench."},
            ],
        },
        {"type": "form_link", "label": "Create Job Hazard Analysis",
         "parent_form_code": "jha", "helper": "File before work begins."},
    ],
}


def test_guidance_renders_heading_and_callouts_only() -> None:
    """A guidance section renders its HEADING + CALLOUT one-liners ONLY — the p/bullets
    prose is the on-screen SOP walk and must NOT bloat the PDF / weekly packet
    (form_pdf._section_flowables, slice D1)."""
    out = render_submission_pdf(
        _GUIDANCE_FIXTURE, {"job_name": "B1", "work_date": "2026-07-02", "values": {}}
    )
    assert out[:5] == b"%PDF-"
    text = _norm(_pdf_text(out))
    assert "Trenching Duties" in text                                       # heading
    assert "CRITICAL RULE: never enter an unprotected trench." in text      # callout
    assert "Prose paragraph that stays on screen only." not in text        # p dropped
    assert "Bullet alpha stays on screen." not in text                     # bullets dropped


def test_form_link_renders_label_and_status_line() -> None:
    """A form_link renders the label + the fixed 'see filed forms' pointer — the
    submission payload carries no link state (the linked form files separately)."""
    out = render_submission_pdf(
        _GUIDANCE_FIXTURE, {"job_name": "B1", "work_date": "2026-07-02", "values": {}}
    )
    text = _norm(_pdf_text(out))
    assert "Create Job Hazard Analysis" in text
    assert "Linked form — see the forms filed for this job and date." in text


def test_guidance_and_form_link_render_blank_mode_identically() -> None:
    """Blank/fillable mode routes guidance + form_link through the SAME submission
    path (value-free) — heading + callout + label + pointer, no prose."""
    from safety_reports.form_pdf import render_blank_fillable
    out = render_blank_fillable(_GUIDANCE_FIXTURE)
    assert out[:5] == b"%PDF-"
    text = _norm(_pdf_text(out))
    assert "Trenching Duties" in text
    assert "CRITICAL RULE: never enter an unprotected trench." in text
    assert "Prose paragraph that stays on screen only." not in text
    assert "Create Job Hazard Analysis" in text


def test_daily_report_v2_renders_sop_structure_and_values() -> None:
    """The shipped daily-report-v2: SOP guidance headings + a filled duty confirm + the
    carried-over DFR tables all render; guidance prose stays out."""
    definition = _load("daily-report-v2.json")
    submission = {
        "job_name": "Bradley 1", "work_date": "2026-07-02",
        "values": {
            "weather": "Sunny", "average_temp": "88", "prepared_by": "Casey PM",
            "arrival": {"arrived_walkthrough": {"response": "Confirmed"},
                        "walkthrough_notes": {"response": "Gate lock replaced"}},
            "crew_progress": [{"crew_subcontractor": "Sun Crew", "manpower": "12",
                               "todays_progress": "Rows 40-44 racked"}],
            "tomorrows_goals": "Finish rows 45-48.",
            "comments": "None.",
        },
    }
    out = render_submission_pdf(definition, submission)
    assert out[:5] == b"%PDF-"
    text = _norm(_pdf_text(out))
    # SOP guidance headings (verbatim) render…
    assert "7:30 AM — Arrive On Site — You Set the Tone" in text
    assert "END OF DAY — Before Leaving the Site" in text
    # …with the safety-critical callouts kept…
    assert "CRITICAL RULE: Never allow workers in an unprotected trench." in text
    assert "Hold the line." in text
    # …but the guidance prose deliberately dropped (heading + callouts only).
    assert "Unlock the site and open all access points before workers arrive." not in text
    # form_link label + pointer line render.
    assert "Create Job Hazard Analysis" in text
    assert "Linked form — see the forms filed" in text
    # Filled values + the carried DFR fields render.
    assert "Casey PM" in text and "Sun Crew" in text and "Rows 40-44 racked" in text
    assert "Finish rows 45-48." in text


def test_daily_report_v3_photo_section_renders_without_minimum() -> None:
    """daily-report-v3 (slice D3): the D.12 photo minimum is gone from the rendered PDF;
    the manager's uploaded photos render as the screened-photos grid (out-of-band, §34),
    never inline from `values`."""
    definition = _load("daily-report-v3.json")
    submission = {
        "job_name": "Bradley 1", "work_date": "2026-07-02",
        "values": {
            "weather": "Sunny", "average_temp": "88", "prepared_by": "Casey PM",
            "site_photos": [],  # the SPA initialValues shape for an untouched photo field
            "tomorrows_goals": "Finish rows 45-48.",
        },
        "screened_photos": [("rows40-44.jpg · 2026-07-02 15:10", _tiny_jpeg())],
    }
    out = render_submission_pdf(definition, submission)
    assert out[:5] == b"%PDF-"
    text = _norm(_pdf_text(out))
    # The D.12 guidance heading renders WITHOUT the minimum clause…
    assert "12. Photo Documentation" in text
    assert "Minimum 50" not in text and "50+ photos" not in text and "at least 50" not in text
    # …the manager's screened photos render as the grid with their caption…
    assert "Site Photos" in text and "rows40-44.jpg" in text
    # …and the SOP structure + values still render (same contract as the v2 test).
    assert "END OF DAY — Before Leaving the Site" in text
    assert "Create Job Hazard Analysis" in text
    assert "Casey PM" in text and "Finish rows 45-48." in text


def test_daily_report_v1_still_renders_unchanged() -> None:
    """Regression: the v1 definition stays in-tree (append-only) and historical
    submissions must keep rendering with their own field set."""
    definition = _load("daily-report-v1.json")
    submission = {
        "job_name": "Bradley 1", "work_date": "2026-06-03",
        "values": {
            "job_name": "Bradley 1", "report_date": "2026-06-03",
            "prepared_by": "Casey PM", "weather": "Overcast", "average_temp": "71",
            "crew_progress": [{"crew_subcontractor": "Old Crew", "manpower": "8",
                               "todays_progress": "Piles driven"}],
            "tomorrows_goals": "Keep driving piles.",
            "comments": "Historical record.",
        },
    }
    out = render_submission_pdf(definition, submission)
    assert out[:5] == b"%PDF-"
    text = _norm(_pdf_text(out))
    assert "Crew / Subcontractor Progress" in text and "Old Crew" in text
    assert "Tomorrow's Progress Goals" in text and "Keep driving piles." in text
    assert "Historical record." in text
    # v1 has no guidance/form_link — none of the D1 chrome may appear.
    assert "Linked form — see the forms filed" not in text


# ── job_requirements section (per-job daily-form requirements, slice D4) ─────────
_REQS_FIXTURE: dict = {
    "form_code": "reqs-fixture-v1",
    "parent_form_code": "reqs-fixture",
    "form_name": "Requirements Fixture",
    "variant_label": None,
    "version": 1,
    "archetype": "sectioned_assessment",
    "source_pdf": "daily-field-report.pdf",
    "sections": [
        {"type": "job_requirements", "key": "job_requirements",
         "title": "Job-specific requirements"},
    ],
}


def test_job_requirements_renders_values_array_generically() -> None:
    """The filed values array (values.job_requirements = [{label, kind, response}]) renders
    as generic label→response rows under the section title — the filed PDF shows the client
    requirements + answers exactly as answered (self-describing; stable regardless of later
    requirement edits). The D5 kinds (number / date / select — migration 0032) ride the SAME
    generic rows: every kind's response is a string, so the renderer needs no per-kind code
    and new kinds render for free."""
    submission = {
        "job_name": "B1", "work_date": "2026-07-02",
        "values": {"job_requirements": [
            {"label": "Client requires FR clothing", "kind": "note", "response": ""},
            {"label": "Badge in at the client gate", "kind": "confirm",
             "response": "Confirmed"},
            {"label": "Client rep spoken to today", "kind": "text", "response": "Ana R."},
            {"label": "Crew headcount at the gate", "kind": "number", "response": "12"},
            {"label": "Client walkthrough date", "kind": "date", "response": "2026-07-10"},
            {"label": "Shift worked", "kind": "select", "response": "Night shift"},
        ]},
    }
    out = render_submission_pdf(_REQS_FIXTURE, submission)
    assert out[:5] == b"%PDF-"
    text = _norm(_pdf_text(out))
    assert "Job-specific requirements" in text
    assert "Client requires FR clothing" in text          # a note rides along (label only)
    assert "Badge in at the client gate" in text and "Confirmed" in text
    assert "Client rep spoken to today" in text and "Ana R." in text
    # D5 kinds — label: response rows, no per-kind rendering needed.
    assert "Crew headcount at the gate" in text and "12" in text
    assert "Client walkthrough date" in text and "2026-07-10" in text
    assert "Shift worked" in text and "Night shift" in text


def test_job_requirements_absent_or_empty_is_skipped() -> None:
    """A job with no requirements adds NOTHING to the PDF: an absent key, an empty array,
    and a malformed (non-list) value all skip the whole section — title included."""
    cases: list[dict] = [{}, {"job_requirements": []}, {"job_requirements": "garbage"},
                         {"job_requirements": ["not-a-dict"]}]
    for values in cases:
        out = render_submission_pdf(
            _REQS_FIXTURE, {"job_name": "B1", "work_date": "2026-07-02", "values": values}
        )
        assert out[:5] == b"%PDF-"
        assert "Job-specific requirements" not in _norm(_pdf_text(out)), values


def test_job_requirements_blank_mode_renders_title_and_placeholder() -> None:
    """Blank/fillable mode can't know a job's runtime overlay — it renders the title + an
    explicit placeholder line instead of silently omitting the section."""
    from safety_reports.form_pdf import render_blank_fillable
    out = render_blank_fillable(_REQS_FIXTURE)
    assert out[:5] == b"%PDF-"
    text = _norm(_pdf_text(out))
    assert "Job-specific requirements" in text
    assert "(job-specific items appear here)" in text


def test_daily_report_v4_renders_with_requirements_and_v3_content() -> None:
    """The shipped daily-report-v4: the v3 SOP content still renders AND the filed
    requirements array renders in the new section; a submission WITHOUT the array (a job
    with no requirements) renders the same document minus that section."""
    definition = _load("daily-report-v4.json")
    base_values = {
        "prepared_by": "Casey PM", "weather": "Sunny", "average_temp": "88",
        "comments": "All good.",
    }
    with_reqs = {
        "job_name": "Bradley 1", "work_date": "2026-07-02",
        "values": {**base_values, "job_requirements": [
            {"label": "Badge in at the client gate", "kind": "confirm",
             "response": "Confirmed"},
        ]},
    }
    text = _norm(_pdf_text(render_submission_pdf(definition, with_reqs)))
    assert "SITE SUPERVISOR — STANDARD OPERATING PROCEDURE" in text  # v3 content intact
    assert "Job-specific requirements" in text
    assert "Badge in at the client gate" in text and "Confirmed" in text

    without = {"job_name": "Bradley 1", "work_date": "2026-07-02", "values": base_values}
    text2 = _norm(_pdf_text(render_submission_pdf(definition, without)))
    assert "SITE SUPERVISOR — STANDARD OPERATING PROCEDURE" in text2
    assert "Job-specific requirements" not in text2


# ── expected_materials section (Material receipts M2) ────────────────────────────
def test_expected_materials_renders_note_line_only_in_submission_mode() -> None:
    """The daily PDF's expected_materials section is DELIBERATELY a title + note line
    pointing at Deliveries Received / the material-incident filings — the section is an
    on-screen receipt affordance that files NO values of its own (the receipt data the
    document of record needs already lands in the deliveries_received table + the incident
    form's own submission). Even a stray value under the section's key changes nothing."""
    definition = _load("daily-report-v5.json")
    submission = {
        "job_name": "Bradley 1", "work_date": "2026-07-02",
        "values": {
            "deliveries_received": [
                {"item_material": "Q.PEAK DUO", "condition": "Received OK",
                 "notes": "qty 40 panels"},
            ],
            # Defensive: nothing should ever write here (the section files no values) —
            # and if something does, the renderer must NOT dump it into the document.
            "expected_materials_receipt": [{"label": "should never render"}],
        },
    }
    text = _norm(_pdf_text(render_submission_pdf(definition, submission)))
    assert "SITE SUPERVISOR — STANDARD OPERATING PROCEDURE" in text  # v4 content intact
    assert "Expected materials" in text
    assert "Receipts recorded under Deliveries Received above" in text
    assert "Material Incident Report submissions" in text
    assert "should never render" not in text
    # The receipt row the confirm action appended renders in the deliveries table.
    assert "Q.PEAK DUO" in text and "Received OK" in text and "qty 40 panels" in text


def test_expected_materials_blank_mode_renders_title_and_placeholder() -> None:
    """Blank/fillable mode can't know a job's live D1 expected-materials rows — it renders
    the title + an explicit placeholder (mirroring job_requirements, never silent)."""
    from safety_reports.form_pdf import render_blank_fillable
    out = render_blank_fillable(_load("daily-report-v5.json"))
    assert out[:5] == b"%PDF-"
    text = _norm(_pdf_text(out))
    assert "Expected materials" in text
    assert "this job's expected materials appear here on screen" in text


def test_material_incident_v1_renders_filled_values() -> None:
    """The shipped material-incident-v1 renders through the standard machinery: header
    fields (incl. the `select` issue as its chosen text value) + the details/action
    textareas; the blank mode renders too (the render-parity glob covers it — this is the
    filled-values leg)."""
    definition = _load("material-incident-v1.json")
    submission = {
        "job_name": "Bradley 1", "work_date": "2026-07-02",
        "values": {
            "material_description": "Q.PEAK DUO panels",
            "delivery_ref": "PO-4471",
            "qty_expected": "40",
            "qty_received": "37",
            "issue": "Damaged",
            "details": "Three pallets arrived with crushed corners; glass cracked on 3 modules.",
            "action_taken": "Flagged on the delivery receipt and notified the CM before signing.",
        },
    }
    out = render_submission_pdf(definition, submission)
    assert out[:5] == b"%PDF-"
    text = _norm(_pdf_text(out))
    assert "MATERIAL INCIDENT REPORT" in text
    assert "Q.PEAK DUO panels" in text
    assert "PO-4471" in text
    assert "Damaged" in text
    assert "crushed corners" in text
    assert "notified the CM before signing" in text


# ── signature-box aspect: the 600:180 contract with the portal's SignaturePad ──────
#
# form_pdf scales signature paths PER AXIS (`sx, sy = width/_SIG_W, height/_SIG_H`),
# so a box whose ratio is not exactly 600:180 permanently stretches the ink in a
# FILED LEGAL DOCUMENT. It is invisible on screen — the portal preview re-normalises
# the same path into its own 600:180 viewBox — so only a test can hold this.
# The table box was 140x44 (4.76% vertical over-stretch) before `for_signature`.

def test_for_signature_derives_height_at_the_pad_aspect() -> None:
    for width in (140.0, 200.0, 97.12, 333.0):
        d = form_pdf.SignatureDrawing.for_signature("M 0 0 L 10 10", width)
        assert d.width == width
        assert d.height / d.width == pytest.approx(form_pdf._SIG_H / form_pdf._SIG_W)
        # the per-axis scales draw() computes must be EQUAL — that is the whole point
        assert d.width / form_pdf._SIG_W == pytest.approx(d.height / form_pdf._SIG_H)


def test_table_signature_box_is_140x42_not_140x44() -> None:
    """Regression pin for the actual defect (was height=44)."""
    d = form_pdf.SignatureDrawing.for_signature("M 0 0 L 10 10", 140)
    assert (d.width, d.height) == (140, 42.0)


def test_class_default_box_is_already_on_aspect() -> None:
    """200x60 is exactly 600:180 — header signatures were never distorted."""
    d = form_pdf.SignatureDrawing("M 0 0 L 10 10")
    assert d.width / form_pdf._SIG_W == pytest.approx(d.height / form_pdf._SIG_H)


def test_blank_form_ruled_line_never_draws_strokes() -> None:
    """_blank_field_cell's off-aspect 28pt box is legitimate BECAUSE it is inert.

    It passes a literal "" so draw() returns at the baseline before sx/sy exist.
    If that ever changes, the AST fence below must start covering it.
    """
    d = form_pdf.SignatureDrawing("", width=200, height=28)
    assert d._strokes == []


def test_ast_fence_every_ink_bearing_signature_box_uses_for_signature() -> None:
    """No call site can hand-pick a height for real signature data again.

    Any `SignatureDrawing(...)` whose first argument is not a literal empty string is
    a potential ink-bearing box and MUST go through `for_signature`. Mirrors the
    AST-enrollment precedent in tests/test_transient_fence.py.
    """
    src = (_ROOT / "safety_reports" / "form_pdf.py").read_text()
    tree = ast.parse(src)
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # `SignatureDrawing(...)` — the raw constructor. `SignatureDrawing.for_signature(...)`
        # is an ast.Attribute and is exempt by construction.
        if not (isinstance(func, ast.Name) and func.id == "SignatureDrawing"):
            continue
        first = node.args[0] if node.args else None
        inert = isinstance(first, ast.Constant) and first.value == ""
        if not inert:
            offenders.append(node.lineno)
    assert offenders == [], (
        "SignatureDrawing(...) constructed directly with non-empty path data at "
        f"lines {offenders} — use SignatureDrawing.for_signature(path, width) so the "
        "height is derived at the 600:180 capture aspect (see the classmethod docstring)."
    )
