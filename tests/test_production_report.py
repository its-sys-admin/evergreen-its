"""Render tests for the client-facing Evergreen Weekly Production Report.

`form_pdf.render_production_report` is pure data → bytes, so these render it and read the
text back out (pypdf) to assert three things the office and the client depend on:

  1. every section of the office's Word template is present, with the values it was given;
  2. a section with NO source renders an explicit empty state — never a plausible-looking
     zero, and never a fabricated percentage;
  3. a hostile or malformed value renders as characters or degrades, and NEVER raises —
     the weekly document must not be blocked by one bad cell.
"""
from __future__ import annotations

import io

import pypdf
import pytest

from safety_reports import form_pdf


def _pdf_text(pdf_bytes: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return " ".join(page.extract_text() for page in reader.pages)


def _norm(s: str) -> str:
    return " ".join(s.split())


def _jpeg(w: int = 600, h: int = 400, color: tuple[int, int, int] = (80, 110, 90)) -> bytes:
    from PIL import Image  # local import: Pillow is a renderer dep, not a test-module one

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    return buf.getvalue()


def _full_data() -> dict:
    """A complete week — every section populated, the shape wpr_data assembles."""
    return {
        "job_name": "Bonacci 2",
        "site_location": "Village of Steger, IL",
        "ess_management": "Ben Finkhousen",
        "mobilization_date": "2026-05-01",
        "week_label": "Sat 2026-08-08 - Fri 2026-08-14",
        "report_submitted": "2026-08-14",
        "prepared_by": "Teala Paradise",
        "subcontractors": ["Pro Panel", "ER Electrical"],
        "safety": {
            "lost_time": {"month": 0, "to_date": 0},
            "lost_work_days": {"month": 0, "to_date": 0},
            "job_transfer": {"month": 0, "to_date": 0},
            "near_miss": {"month": 1, "to_date": 3},
            "other_recordable": {"month": 0, "to_date": 0},
            "first_aid": {"month": 0, "to_date": 1},
        },
        "hazard_topics": ["P.P.E.", "Working Near Heavy Equipment", "Proper Hydration"],
        "weather": {
            "days": [
                {"date": "2026-08-08", "conditions": "Clear, breezy", "avg_temp": "74", "inclement": False},
                {"date": "2026-08-09", "conditions": "Overcast, light rain", "avg_temp": "68", "inclement": True},
            ],
            "week": 1,
            "to_date": 9,
        },
        "labor": {
            "rows": [
                {"company": "ESS Supervisor", "workers": 2, "man_hours": 60},
                {"company": "Pro Panel", "workers": 9, "man_hours": 540},
            ],
            "total_hours": 600,
        },
        "progress": {
            "sections": [
                {"name": "Deliveries", "items": [
                    {"label": "Piers", "percent": 100}, {"label": "Modules", "percent": 0},
                ]},
                {"name": "Mechanical", "items": [
                    {"label": "Piles", "percent": 95}, {"label": "QA/QC", "percent": None},
                ]},
            ],
            "critical_items": "Six inches of snow / mud. Tracker delivery slipped one week.",
            "upcoming_activities": "Complete post driving on rows 20-38.",
        },
        "photos": [("Pile driving, rows 12-18", _jpeg())],
        "materials": {"deliveries": [
            {"item": "Torque tube", "vendor": "TerraSmart", "qty": "40", "delivered": "2026-08-11"},
        ]},
        "pending": {
            "rfis": "RFI-014 tracker embed depth",
            "submittals": "",
            "ifc_review": "Rev C under review",
            "change_orders": "CO-3 pending client",
        },
    }


# ── structure: the office's template, section for section ─────────────────────
def test_renders_the_five_template_pages() -> None:
    pdf = form_pdf.render_production_report(_full_data())
    assert pdf.startswith(b"%PDF")
    # Five logical pages, one per part of the Word template the office has sent since 2022.
    assert form_pdf.page_count(pdf) == 5


@pytest.mark.parametrize("heading", [
    "Project Safety Status",
    "Safety Hazards Addressed in Daily Safety Meetings",
    "Weather Report",
    "Construction Labor Report",
    "Construction Progress / Delays",
    "Critical Items / Delays",
    "Upcoming Activities",
    "Progress Photos",
    "Material Delivery Tracking Log",
    "Pending Requests",
])
def test_every_template_section_is_present(heading: str) -> None:
    text = _norm(_pdf_text(form_pdf.render_production_report(_full_data())))
    assert heading in text


def test_header_meta_and_footer_identify_the_job_and_week() -> None:
    text = _norm(_pdf_text(form_pdf.render_production_report(_full_data())))
    assert "WEEKLY PRODUCTION REPORT" in text
    assert "Bonacci 2" in text
    assert "Village of Steger, IL" in text
    assert "Ben Finkhousen" in text
    assert "Teala Paradise" in text
    assert "Pro Panel, ER Electrical" in text
    # The brand wordmark is in the text layer on every page (the footer), not just the logo image.
    assert "EVERGREEN RENEWABLES" in text


def test_safety_grid_carries_all_six_osha_rows_with_their_counts() -> None:
    text = _norm(_pdf_text(form_pdf.render_production_report(_full_data())))
    for label in ("Lost Time Accident Cases", "Lost Work Days", "Job Transfer or Restriction",
                  "Near Misses", "Other Recordable Cases", "First Aid Cases"):
        assert label in text
    assert "Monthly Total Incidents" in text
    assert "Project Start to Date Total" in text


def test_weather_carries_the_running_weather_day_count() -> None:
    text = _norm(_pdf_text(form_pdf.render_production_report(_full_data())))
    assert "Clear, breezy" in text
    assert "Overcast, light rain" in text
    # The contractual claim the office marks, and its running total.
    assert "Weather days this week: 1" in text
    assert "Total weather days to date: 9" in text


def test_labor_report_is_grouped_by_company() -> None:
    text = _norm(_pdf_text(form_pdf.render_production_report(_full_data())))
    assert "Company" in text and "# of Workers" in text and "Man Hours" in text
    assert "ESS Supervisor" in text and "Pro Panel" in text
    assert "540" in text


def test_progress_renders_percentages_by_discipline() -> None:
    text = _norm(_pdf_text(form_pdf.render_production_report(_full_data())))
    assert "Deliveries" in text and "Mechanical" in text
    assert "100%" in text and "95%" in text and "0%" in text


def test_a_missing_percent_is_a_dash_not_zero() -> None:
    """`percent: None` means NOT REPORTED. Printing 0% would assert no work was done —
    a different and possibly false claim that a client reads as a fact."""
    text = _norm(_pdf_text(form_pdf.render_production_report(_full_data())))
    # QA/QC carries percent=None in the fixture; Piles carries 95.
    assert "95%" in text
    assert "QA/QC" in text
    assert "QA/QC 0%" not in text


def test_materials_and_pending_render_their_values() -> None:
    text = _norm(_pdf_text(form_pdf.render_production_report(_full_data())))
    assert "Torque tube" in text and "TerraSmart" in text and "2026-08-11" in text
    assert "RFI-014 tracker embed depth" in text
    assert "CO-3 pending client" in text


# ── honest empty states: never invent, never imply ────────────────────────────
def test_no_schedule_says_so_and_prints_no_percentage() -> None:
    """The ADR-0006 schedule lane has not landed. Until a job has a committed schedule the
    page must say that plainly rather than showing a fabricated or zeroed figure — the same
    discipline that keeps `jobs.progress` off the rollup page."""
    data = _full_data()
    data["progress"] = {"sections": [], "critical_items": "", "upcoming_activities": ""}
    text = _norm(_pdf_text(form_pdf.render_production_report(data)))
    assert "No schedule imported for this job" in text
    assert "%" not in text.split("Construction Progress / Delays")[1].split("Critical Items")[0]


@pytest.mark.parametrize("key,replacement,expected", [
    ("photos", [], "No progress photos filed this week."),
    ("weather", {"days": [], "week": 0, "to_date": 0}, "No daily reports were filed for this week."),
    ("labor", {"rows": [], "total_hours": 0}, "No crew activity recorded for this week."),
    ("materials", {"deliveries": []}, "No material deliveries recorded this week."),
    ("hazard_topics", [], "No safety meetings recorded for this week."),
])
def test_each_empty_section_states_its_emptiness(key: str, replacement: object, expected: str) -> None:
    data = _full_data()
    data[key] = replacement
    assert expected in _norm(_pdf_text(form_pdf.render_production_report(data)))


def test_unreported_narrative_says_none_reported() -> None:
    data = _full_data()
    data["progress"]["critical_items"] = ""
    data["progress"]["upcoming_activities"] = ""
    assert "None reported." in _norm(_pdf_text(form_pdf.render_production_report(data)))


def test_a_completely_empty_report_still_renders_five_pages() -> None:
    """A brand-new job with nothing filed anywhere. Every section degrades to its empty
    state and the document still has its full structure — the office gets a reviewable
    draft, not a crash."""
    pdf = form_pdf.render_production_report({})
    assert pdf.startswith(b"%PDF")
    assert form_pdf.page_count(pdf) == 5


# ── Invariant 2: hostile / malformed values must not raise or become markup ────
def test_markup_in_field_text_is_escaped_not_interpreted() -> None:
    """Crew names, captions and every office field are free text. A hostile value must
    render as characters — the renderer must never treat transported text as markup."""
    data = _full_data()
    data["labor"]["rows"] = [{"company": "<b>Bold Co</b>", "workers": 1, "man_hours": 8}]
    data["photos"] = [("<i>caption</i> & co", _jpeg())]
    data["progress"]["critical_items"] = "5 < 6 & 7 > 2"
    text = _pdf_text(form_pdf.render_production_report(data))
    # The literal tag characters survive into the text layer; reportlab never saw markup.
    assert "<b>Bold Co</b>" in _norm(text)
    assert "<i>caption</i> & co" in _norm(text)
    assert "5 < 6 & 7 > 2" in _norm(text)


@pytest.mark.parametrize("data", [
    {"safety": "not-a-dict"},
    {"weather": {"days": "not-a-list"}},
    {"weather": {"days": [None, 5, "x"]}},
    {"labor": {"rows": [None, 5, {"company": None}]}},
    {"labor": {"rows": [{"company": "A", "workers": {"nested": 1}}]}},
    {"progress": {"sections": "nope", "critical_items": None}},
    {"progress": {"sections": [{"name": None, "items": "nope"}]}},
    {"progress": {"sections": [{"name": "S", "items": [{"label": "x", "percent": "abc"}]}]}},
    {"materials": {"deliveries": 42}},
    {"pending": None},
    {"hazard_topics": "not-a-list"},
    {"subcontractors": [None, 5]},
    {"safety": {"near_miss": {"month": -5, "to_date": "twelve"}}},
    {"photos": "not-a-list"},
    {"photos": ["not-a-tuple", ("cap", "not-bytes"), ("cap", b"not-a-jpeg")]},
])
def test_malformed_input_degrades_and_never_raises(data: dict) -> None:
    """The dict arrives over untrusted JSON transport from the Worker aggregate. A
    malformed shape must degrade to a blank cell or an empty state — never a traceback
    that costs the office its weekly document."""
    pdf = form_pdf.render_production_report(data)
    assert pdf.startswith(b"%PDF")
    assert form_pdf.page_count(pdf) == 5


def test_an_unrenderable_photo_is_dropped_not_fatal() -> None:
    """One corrupt image must not take the document down — `_photo_cell`'s per-photo fence
    already does this for submission PDFs and the report reuses it."""
    data = _full_data()
    data["photos"] = [("good", _jpeg()), ("corrupt", b"\xff\xd8not-a-jpeg")]
    text = _norm(_pdf_text(form_pdf.render_production_report(data)))
    assert "good" in text
    # The good photo still rendered, so the page is NOT in its empty state.
    assert "No progress photos filed this week." not in text


def test_negative_osha_counts_are_clamped_to_zero() -> None:
    """A count is never negative; a minus sign in a client's safety statistics is a defect
    that reads as a data-integrity problem."""
    data = _full_data()
    data["safety"]["near_miss"] = {"month": -5, "to_date": -1}
    text = _norm(_pdf_text(form_pdf.render_production_report(data)))
    # Assert the ROW positively rather than the absence of "-1": dates like 2026-08-14
    # contain that substring, so a bare not-in check passes for the wrong reason.
    assert "Near Misses 0 0" in text
    assert "-5" not in text


# ── determinism ───────────────────────────────────────────────────────────────
def test_render_is_deterministic_for_the_same_input() -> None:
    """Same data in, same document out (modulo the PDF's own timestamp): the office must be
    able to recompile after an edit and see only what they changed."""
    a = form_pdf.render_production_report(_full_data())
    b = form_pdf.render_production_report(_full_data())
    assert _pdf_text(a) == _pdf_text(b)
    assert form_pdf.page_count(a) == form_pdf.page_count(b)
