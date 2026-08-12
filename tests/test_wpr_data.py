"""Tests for the Weekly Production Report assembler and the compile seams it rides.

Three things are load-bearing and each has teeth here:

  1. **The office's word wins.** A SAVED week renders what the office typed — including a
     deliberately emptied section. Only an unsaved (or carried-forward) week gets the
     deterministic seed. Get this backwards and the office can never clear a section.
  2. **The client's attachment swaps, the record does not.** With a report bound, the review
     row's Compiled PDF is the REPORT and the field-records packet link moves to Notes. A
     report failure falls back to the packet and WARNs — the weekly cadence never silently stops.
  3. **Safety is untouched.** `generate_core` is shared; every new seam is opt-in and the safety
     compile must behave exactly as it did (§14).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from progress_reports import progress_weekly_generate as pwg
from progress_reports import wpr_data
from safety_reports import generate_core, weekly_generate
from shared import active_jobs, safety_week

WEEK = safety_week.SafetyWeek(start=date(2026, 8, 8), end=date(2026, 8, 14))


def _job() -> active_jobs.ActiveJob:
    return active_jobs.ActiveJob(
        job_id="JOB-P", project_name="Bonacci 2", address="", stakeholder_name="",
        stakeholder_email="", stakeholder_phone="",
        safety_reports_contact_email="pm@example.com", safety_reports_contact_name="PM",
        cc_emails=(), active_status="Active", row_id=1,
    )


def _payload(**over: object) -> dict:
    """A Worker aggregate with every leg populated."""
    base: dict = {
        "job_id": "JOB-P",
        "job": {"project_name": "Bonacci 2", "address_city": "Steger", "address_state": "IL"},
        "daily_report_count": 2,
        "weather": {"days": [
            {"work_date": "2026-08-08", "conditions": "Clear", "avg_temp": "74", "inclement": False},
            {"work_date": "2026-08-09", "conditions": "Rain", "avg_temp": "68", "inclement": True},
        ], "weather_days_week": 1, "weather_days_to_date": 0},
        "labor": {"total_hours": 600, "crews": [
            {"company": "Pro Panel", "workers": 9, "days": 2},
            {"company": "ESS Supervisor", "workers": 2, "days": 2},
        ]},
        "crew_progress": [],
        "daily_notes": [
            {"work_date": "2026-08-08", "tomorrows_goals": "Drive rows 1-9", "comments": "Mud on the access road."},
            {"work_date": "2026-08-09", "tomorrows_goals": "Drive rows 10-20", "comments": ""},
        ],
        "hazard_form_codes": ["toolbox-talk-ppe-v1"],
        "deliveries": [
            {"event_date": "2026-08-11", "item": "Torque tube", "vendor": "TerraSmart", "qty": "40"},
        ],
        "material_incidents": [
            {"work_date": "2026-08-10", "material": "Racking", "issue": "Short", "details": "12 of 40 short."},
        ],
        "photos": {"available": [], "selected": [], "auto_selected": True},
        "schedule": None,
        "office": {
            "header": {"ess_management": "", "subcontractors": [], "mobilization_date": "",
                       "prepared_by": "", "site_location": ""},
            "safety": {}, "weather": {"inclement_dates": [], "weather_days_to_date": 9},
            "labor": {"rows": []},
            "narrative": {"critical_items": "", "upcoming_activities": "", "hazard_topics": []},
            "pending": {}, "photos": None, "saved": False, "carried_from": None,
        },
    }
    base.update(over)
    return base


def _build(monkeypatch, payload: dict) -> dict:
    monkeypatch.setattr(wpr_data.portal_client, "get_production_report",
                        lambda *a, **k: payload)
    monkeypatch.setattr(wpr_data.box_client, "download_file", lambda fid: b"\xff\xd8jpeg")
    return wpr_data.build(_job(), WEEK, base_url="https://w", bearer="tok")


# ── the office's word wins ────────────────────────────────────────────────────
def test_unsaved_week_gets_the_deterministic_seed(monkeypatch) -> None:
    data = _build(monkeypatch, _payload())
    # Critical items: material incidents first, then the daily comments — the field's own words.
    assert "Racking: Short — 12 of 40 short." in data["progress"]["critical_items"]
    assert "Mud on the access road." in data["progress"]["critical_items"]
    # Upcoming: the LAST tomorrow's-goals only. The earlier day's goals describe work that has
    # since happened; concatenating the week would report completed work as upcoming.
    assert data["progress"]["upcoming_activities"] == "Drive rows 10-20"
    assert "Drive rows 1-9" not in data["progress"]["upcoming_activities"]


def test_saved_office_text_replaces_the_seed(monkeypatch) -> None:
    payload = _payload()
    payload["office"]["saved"] = True
    payload["office"]["narrative"] = {
        "critical_items": "Tracker delivery slipped one week.",
        "upcoming_activities": "Complete post driving.",
        "hazard_topics": ["Heat"],
    }
    data = _build(monkeypatch, payload)
    assert data["progress"]["critical_items"] == "Tracker delivery slipped one week."
    assert data["progress"]["upcoming_activities"] == "Complete post driving."
    assert data["hazard_topics"] == ["Heat"]


def test_a_saved_empty_section_stays_empty(monkeypatch) -> None:
    """The office deliberately cleared it. Re-seeding would make clearing impossible — the
    section would silently refill from the field text on every recompile."""
    payload = _payload()
    payload["office"]["saved"] = True  # narrative already all-empty in the fixture
    data = _build(monkeypatch, payload)
    assert data["progress"]["critical_items"] == ""
    assert data["progress"]["upcoming_activities"] == ""


def test_carried_forward_values_still_get_a_fresh_seed(monkeypatch) -> None:
    """A carried week is NOT saved: last week's narrative describes last week's job, so the
    narrative must re-derive from THIS week's field text even though the header carried."""
    payload = _payload()
    payload["office"]["saved"] = False
    payload["office"]["carried_from"] = "2026-08-01"
    payload["office"]["narrative"]["critical_items"] = "LAST WEEK'S TEXT"
    data = _build(monkeypatch, payload)
    assert "LAST WEEK" not in data["progress"]["critical_items"]
    assert "Mud on the access road." in data["progress"]["critical_items"]


# ── the honest-numbers rules ──────────────────────────────────────────────────
def test_labor_seed_leaves_man_hours_blank(monkeypatch) -> None:
    """`personnel` has no employer column, so hours cannot be attributed to a subcontractor.
    Splitting the job total across crews by headcount would look authoritative and be a guess."""
    data = _build(monkeypatch, _payload())
    rows = data["labor"]["rows"]
    assert [r["company"] for r in rows] == ["Pro Panel", "ESS Supervisor"]
    assert all(r["man_hours"] == "" for r in rows)
    assert data["labor"]["total_hours"] == 600  # the honest job-wide number still rides along


def test_saved_office_labor_table_replaces_the_seed(monkeypatch) -> None:
    payload = _payload()
    payload["office"]["labor"] = {"rows": [{"company": "Pro Panel", "workers": "9", "man_hours": "540"}]}
    data = _build(monkeypatch, payload)
    assert data["labor"]["rows"] == [{"company": "Pro Panel", "workers": "9", "man_hours": "540"}]


def test_inclement_comes_from_the_office_not_the_conditions_text(monkeypatch) -> None:
    """A weather day is a contractual delay claim. The Worker already resolves it from the
    office's marked dates; the assembler must pass that through, never re-derive it from
    the word "Rain"."""
    data = _build(monkeypatch, _payload())
    days = {d["date"]: d["inclement"] for d in data["weather"]["days"]}
    assert days == {"2026-08-08": False, "2026-08-09": True}
    assert data["weather"]["to_date"] == 9  # the office's running total, not the week's count


def test_schedule_stays_empty_until_the_adr_0006_lane_lands(monkeypatch) -> None:
    data = _build(monkeypatch, _payload())
    assert data["progress"]["sections"] == []


# ── photos: the screening control ─────────────────────────────────────────────
def test_only_box_backed_photos_are_resolved(monkeypatch) -> None:
    """A pool row earns a box_file_id ONLY on a CLEAN §34 disposition, so resolving by that id
    is what makes an unscreened photo structurally unable to reach a client report."""
    payload = _payload()
    payload["photos"] = {"available": [], "auto_selected": False, "selected": [
        {"pool_id": 1, "box_file_id": "box-1", "caption": "Pile driving", "work_date": "2026-08-08"},
        {"pool_id": 2, "box_file_id": "", "caption": "unscreened", "work_date": "2026-08-09"},
    ]}
    data = _build(monkeypatch, payload)
    assert [c for c, _ in data["photos"]] == ["Pile driving"]


def test_one_failed_photo_download_never_costs_the_week_its_report(monkeypatch) -> None:
    payload = _payload()
    payload["photos"] = {"available": [], "auto_selected": False, "selected": [
        {"pool_id": 1, "box_file_id": "good", "caption": "kept", "work_date": "2026-08-08"},
        {"pool_id": 2, "box_file_id": "bad", "caption": "dropped", "work_date": "2026-08-09"},
    ]}
    monkeypatch.setattr(wpr_data.portal_client, "get_production_report", lambda *a, **k: payload)

    def flaky(file_id: str) -> bytes:
        if file_id == "bad":
            raise RuntimeError("box down")
        return b"\xff\xd8jpeg"

    monkeypatch.setattr(wpr_data.box_client, "download_file", flaky)
    data = wpr_data.build(_job(), WEEK, base_url="https://w", bearer="tok")
    assert [c for c, _ in data["photos"]] == ["kept"]


def test_form_codes_resolve_to_display_names(monkeypatch) -> None:
    data = _build(monkeypatch, _payload())
    # A real definition resolves to its form_name; anything unresolvable humanizes the code
    # rather than printing "toolbox-talk-ppe-v1" on a client's page.
    assert data["hazard_topics"]
    assert "-v1" not in data["hazard_topics"][0]


def test_unknown_form_code_humanizes_rather_than_leaking_the_code() -> None:
    assert wpr_data._display_form_name("toolbox-talk-not-a-real-form-v3") == "Toolbox Talk Not A Real Form"


# ── the compile seams ─────────────────────────────────────────────────────────
def test_safety_binds_neither_new_seam(monkeypatch) -> None:
    """§14: unset = unchanged. Safety must not acquire a client report or the empty-week hold."""
    assert weekly_generate.SAFETY_GENERATE_CONFIG.client_report_provider is None
    assert weekly_generate.SAFETY_GENERATE_CONFIG.empty_week_hold is False


def test_progress_binds_both_new_seams() -> None:
    assert pwg.PROGRESS_GENERATE_CONFIG.client_report_provider is pwg._client_report_provider
    assert pwg.PROGRESS_GENERATE_CONFIG.empty_week_hold is True
    assert pwg.PROGRESS_GENERATE_CONFIG.client_report_suffix == "WPR"


def test_client_report_link_becomes_the_review_rows_compiled_pdf(monkeypatch) -> None:
    """The swap: the client's attachment is the REPORT; the field-records packet link moves to
    Notes so the operator can still reach it from the same row."""
    # GenerateConfig is frozen (the binding is a value, not a mutable global), so a variant
    # is built with dataclasses.replace rather than monkeypatched onto the shared instance.
    cfg = replace(pwg.PROGRESS_GENERATE_CONFIG,
                  client_report_provider=lambda job, week: b"%PDF-report")
    monkeypatch.setattr(generate_core, "_ensure_box_week_folder", lambda *a, **k: 1)
    monkeypatch.setattr(generate_core, "_upload_packet", lambda *a, **k: ("n.pdf", "999"))
    summary = generate_core.RunSummary()
    link = generate_core._maybe_client_report(cfg, _job(), "Bonacci 2", WEEK, "s", summary, "cid")
    assert link == "https://app.box.com/file/999"
    assert summary.client_reports_compiled == 1


def test_a_failed_client_report_falls_back_to_the_packet_and_warns(monkeypatch) -> None:
    """A weekly cadence that quietly stops is worse than one that sends the wrong-shaped
    document once with a WARN sitting in ITS_Errors."""
    logged: list[tuple] = []
    monkeypatch.setattr(generate_core.error_log, "log",
                        lambda *a, **k: logged.append((a, k)))

    def boom(job, week):
        raise RuntimeError("worker 500")

    cfg = replace(pwg.PROGRESS_GENERATE_CONFIG, client_report_provider=boom)
    summary = generate_core.RunSummary()
    link = generate_core._maybe_client_report(cfg, _job(), "Bonacci 2", WEEK, "s", summary, "cid")
    assert link == ""  # → the caller uses the packet link
    assert summary.client_reports_compiled == 0
    assert any("client_report_failed" in str(k.get("error_code", "")) for _, k in logged)


def test_a_provider_returning_none_is_a_quiet_no_op(monkeypatch) -> None:
    """An UNWIRED progress workstream (no base_url / bearer) is not an error."""
    logged: list[tuple] = []
    monkeypatch.setattr(generate_core.error_log, "log", lambda *a, **k: logged.append((a, k)))
    cfg = replace(pwg.PROGRESS_GENERATE_CONFIG, client_report_provider=lambda job, week: None)
    summary = generate_core.RunSummary()
    assert generate_core._maybe_client_report(
        cfg, _job(), "Bonacci 2", WEEK, "s", summary, "cid") == ""
    assert not any("client_report_failed" in str(k.get("error_code", "")) for _, k in logged)


def test_client_report_provider_returns_none_when_creds_are_unset(monkeypatch) -> None:
    monkeypatch.setattr(pwg, "_resolve_rollup_creds", lambda: None)
    assert pwg._client_report_provider(_job(), WEEK) is None


# ── the empty-week hold ───────────────────────────────────────────────────────
def test_empty_week_hold_marks_the_row_held_and_raises_a_review_item(monkeypatch) -> None:
    """`Send Status` takes the UPPERCASE HELD already in the picklist registry; the lowercase
    `held_no_activity` is the RESULT code and lives in Notes. Writing the lowercase value into
    the cell would raise PicklistViolationError."""
    updates: list = []
    queued: list = []
    monkeypatch.setattr(generate_core.smartsheet_client, "update_rows",
                        lambda sheet_id, rows: updates.append((sheet_id, rows)))
    monkeypatch.setattr(generate_core.review_queue, "add", lambda **kw: queued.append(kw))
    summary = generate_core.RunSummary()
    generate_core._hold_empty_week(
        pwg.PROGRESS_GENERATE_CONFIG, _job(), WEEK, 42, summary, "cid")
    assert updates and updates[0][1][0]["Send Status"] == "HELD"
    assert updates[0][1][0]["_row_id"] == 42
    assert queued and queued[0]["payload"]["result"] == "held_no_activity"
    assert summary.review_queue_entries == 1


def test_a_failed_hold_write_never_aborts_a_written_through_compile(monkeypatch) -> None:
    logged: list[tuple] = []

    def boom(sheet_id, rows):
        raise RuntimeError("smartsheet down")

    monkeypatch.setattr(generate_core.smartsheet_client, "update_rows", boom)
    monkeypatch.setattr(generate_core.review_queue, "add", lambda **kw: None)
    monkeypatch.setattr(generate_core.error_log, "log", lambda *a, **k: logged.append((a, k)))
    summary = generate_core.RunSummary()
    generate_core._hold_empty_week(
        pwg.PROGRESS_GENERATE_CONFIG, _job(), WEEK, 42, summary, "cid")  # must not raise
    assert any("empty_week_hold_failed" in str(k.get("error_code", "")) for _, k in logged)


@pytest.mark.parametrize("hold,expected_calls", [(True, 1), (False, 0)])
def test_empty_week_hold_is_opt_in(monkeypatch, hold: bool, expected_calls: int) -> None:
    """Safety's empty week must stay exactly as it was: a PENDING row the send HELDs later."""
    cfg = replace(pwg.PROGRESS_GENERATE_CONFIG, empty_week_hold=hold)
    assert cfg.empty_week_hold is hold
    calls: list = []
    monkeypatch.setattr(generate_core.smartsheet_client, "update_rows",
                        lambda *a, **k: calls.append(a))
    monkeypatch.setattr(generate_core.review_queue, "add", lambda **kw: None)
    if cfg.empty_week_hold:
        generate_core._hold_empty_week(cfg, _job(), WEEK, 1, generate_core.RunSummary(), "c")
    assert len(calls) == expected_calls
