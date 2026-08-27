"""Tests for the Phase-5 portal pull path in safety_reports/intake.py
(process_portal_submission). All Smartsheet/Box/active_jobs/render calls are mocked.

Live coverage is the deploy-gated end-to-end smoke (next session).
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from safety_reports import intake, week_sheet
from shared import box_client
from shared.smartsheet_client import SmartsheetError, SmartsheetValidationError

BASE_SUB = {
    "submission_uuid": "u1",
    "job_id": "JOB-1",
    "form_code": "jha-v1",
    "work_date": "2026-06-05",
    "payload_json": '{"work_location": "Array A"}',
    "amends_uuid": None,
    "created_at": 1_717_600_000,
}

DEFINITION = {
    "form_code": "jha-v1",
    "parent_form_code": "jha",
    "form_name": "Job Hazard Analysis",
    "sections": [],
}


@pytest.fixture
def stub(mocker) -> dict[str, MagicMock]:
    """Mock every external boundary process_portal_submission touches."""
    job = SimpleNamespace(
        project_name="Bradley 1", is_active=True, active_status="Active"
    )
    s = {
        "get_job": mocker.patch.object(intake.active_jobs, "get_job", return_value=job),
        "ensure": mocker.patch.object(intake.week_sheet, "ensure_week_sheet", return_value=8001),
        "find": mocker.patch.object(intake.week_sheet, "find_submission_row", return_value=None),
        "write": mocker.patch.object(intake.week_sheet, "write_submission_row", return_value=555),
        "attach": mocker.patch.object(intake.smartsheet_client, "attach_pdf_to_row", return_value=42),
        "supersede": mocker.patch.object(intake.week_sheet, "supersede_row", return_value=True),
        "load_def": mocker.patch.object(intake.form_pdf, "load_definition", return_value=DEFINITION),
        "render": mocker.patch.object(intake.form_pdf, "render_submission_pdf", return_value=b"%PDF-1.4"),
        "incomplete": mocker.patch.object(intake.form_pdf, "incomplete_checklist_items", return_value=[]),
        "box_root": mocker.patch.object(intake, "_portal_box_root", return_value=""),  # gated OFF → legacy
        "get_folder_id": mocker.patch.object(intake.project_routing, "get_folder_id", return_value="root1"),
        "resolve_sub": mocker.patch.object(intake, "_resolve_box_subfolder", return_value="leaf1"),
        "upload": mocker.patch.object(intake.box_client, "upload_bytes", return_value={"id": "f9", "name": "x", "size": 5}),
        "get_or_create": mocker.patch.object(intake.box_client, "get_or_create_folder", return_value="fb1"),
        # The already-filed re-attach self-heal (wiring audit 2026-08-12) reads the
        # filed original back from Box; stubbed so the dedupe test is deterministic.
        "file_meta": mocker.patch.object(
            intake.box_client, "get_file_metadata",
            return_value={"id": "old", "name": "Bradley 1_2026-05-27_form.pdf", "size": 5},
        ),
        "download": mocker.patch.object(
            intake.box_client, "download_file", return_value=b"%PDF-recovered"
        ),
        "review": mocker.patch.object(intake.review_queue, "add"),
        "log": mocker.patch.object(intake.error_log, "log"),
        "clamav": mocker.patch.object(intake, "_photo_clamav_enabled", return_value=False),
    }
    return s


# ---- happy path ----------------------------------------------------------


def test_success_files_box_and_sheet_returns_processed(stub):
    result = intake.process_portal_submission(dict(BASE_SUB))
    assert result.status == "processed"
    assert result.box_link == "https://app.box.com/file/f9"
    # PR-4: the structural Box file id rides on the receipt (id in hand from upload).
    assert result.box_file_id == "f9"
    # Wrote the per-submission row with the parsed date + Box link.
    kwargs = stub["write"].call_args.kwargs
    assert kwargs["submission_uuid"] == "u1"
    assert kwargs["work_date"] == date(2026, 6, 5)
    assert kwargs["box_link"] == "https://app.box.com/file/f9"
    assert kwargs["form_code"] == "jha-v1"
    stub["review"].assert_not_called()


def test_success_attaches_rendered_pdf_to_submission_row(stub):
    intake.process_portal_submission(dict(BASE_SUB))
    stub["attach"].assert_called_once()
    sheet_id, row_id, filename, pdf_bytes = stub["attach"].call_args.args
    assert sheet_id == 8001 and row_id == 555   # the week sheet + the submission row id
    assert filename == "Bradley 1_2026-06-05_jha.pdf"  # job-prefixed; matches the Box-filed name
    assert pdf_bytes == b"%PDF-1.4"             # the rendered bytes, inline on the row


def test_attach_failure_does_not_fail_filing(stub):
    # The inline attachment is supplementary (Box is the SoR) — a failure is a WARN,
    # never a filing failure.
    stub["attach"].side_effect = SmartsheetError("attach boom")
    result = intake.process_portal_submission(dict(BASE_SUB))
    assert result.status == "processed"


def test_success_uploads_to_category_subfolder_named_by_job_date_and_type(stub):
    intake.process_portal_submission(dict(BASE_SUB))
    folder_id, name, content = (
        stub["upload"].call_args.args[0],
        stub["upload"].call_args.args[1],
        stub["upload"].call_args.args[2],
    )
    assert folder_id == "leaf1"  # the resolved JSAs category subfolder
    assert name == "Bradley 1_2026-06-05_jha.pdf"  # <job>_<work_date>_<type>.pdf (2026-06-17 rule)
    assert content == b"%PDF-1.4"


def test_box_fallback_when_category_subfolder_missing(stub):
    stub["resolve_sub"].return_value = None  # category subfolder not found
    result = intake.process_portal_submission(dict(BASE_SUB))
    assert result.status == "processed"
    # Filed into the auto-created ITS fallback folder instead.
    stub["get_or_create"].assert_called_once_with("root1", intake.PORTAL_BOX_FALLBACK_FOLDER)
    assert stub["upload"].call_args.args[0] == "fb1"
    notes = stub["write"].call_args.kwargs["notes"]
    assert "fallback" in notes


# ---- Box mirror tree (PR-K) ----------------------------------------------


def test_mirror_tree_files_into_root_job_week_when_configured(stub):
    stub["box_root"].return_value = "ROOT9"  # mirror tree ON
    stub["get_or_create"].side_effect = ["job9", "week9"]  # ROOT→job, job→week
    result = intake.process_portal_submission(dict(BASE_SUB))
    assert result.status == "processed"
    calls = stub["get_or_create"].call_args_list
    assert calls[0].args == ("ROOT9", "Bradley 1")  # ROOT → per-job folder
    assert calls[1].args == ("job9", "week of 2026-05-30")  # job → per-week folder
    assert stub["upload"].call_args.args[0] == "week9"  # PDF filed into the week folder
    assert "[box:mirror_tree]" in stub["write"].call_args.kwargs["notes"]
    # Legacy category path bypassed entirely.
    stub["get_folder_id"].assert_not_called()
    stub["resolve_sub"].assert_not_called()


def test_mirror_tree_new_job_does_not_strand(stub):
    # Headline fix: with the root configured, a brand-new job (no project_routing
    # entry) self-provisions in Box — NEVER strands with project_box_root_unresolved.
    stub["box_root"].return_value = "ROOT9"
    stub["get_folder_id"].return_value = ""  # new job: no legacy routing
    stub["get_or_create"].side_effect = ["jobN", "weekN"]
    result = intake.process_portal_submission(dict(BASE_SUB))
    assert result.status == "processed"
    stub["review"].assert_not_called()


# ---- dedupe (re-pull) ----------------------------------------------------


def test_dedupe_already_filed_skips_refile_and_recovers_link(stub, mocker):
    stub["find"].return_value = {
        "_row_id": 7, week_sheet.COL_SUBMISSION_PDF: "https://app.box.com/file/old",
        "Row Type": week_sheet.ROW_TYPE_SUBMISSION,
    }
    attach = mocker.patch.object(intake, "_attach_pdf_best_effort")
    result = intake.process_portal_submission(dict(BASE_SUB))
    assert result.status == "already_filed"
    assert result.box_link == "https://app.box.com/file/old"
    # PR-4: id derived from the recovered link (split on /file/) → no structural id stored.
    assert result.box_file_id == "old"
    stub["write"].assert_not_called()
    stub["upload"].assert_not_called()
    # Every-service self-heal (wiring audit 2026-08-12): the replay re-downloads the
    # filed original and re-attaches it on the EXISTING row under its own filed name —
    # a crash between the fresh-path attach and the receipt is no longer permanent.
    stub["download"].assert_called_once_with("old")
    attach.assert_called_once()
    assert attach.call_args.args[1] == 7
    assert attach.call_args.args[2] == "Bradley 1_2026-05-27_form.pdf"
    assert attach.call_args.args[3] == b"%PDF-recovered"


def test_dedupe_reattach_failure_never_fails_the_replay(stub, mocker):
    """The re-attach is WHOLLY fenced: a Box outage on the self-heal path must not
    turn an already-filed replay into an error (the receipt still posts)."""
    stub["find"].return_value = {
        "_row_id": 7, week_sheet.COL_SUBMISSION_PDF: "https://app.box.com/file/old",
        "Row Type": week_sheet.ROW_TYPE_SUBMISSION,
    }
    stub["download"].side_effect = box_client.BoxError("503")
    result = intake.process_portal_submission(dict(BASE_SUB))
    assert result.status == "already_filed"
    assert result.box_link == "https://app.box.com/file/old"


# ---- permanent refusals → review_queue (drain) ---------------------------


def test_unknown_job_routes_to_review(stub):
    stub["get_job"].return_value = None
    result = intake.process_portal_submission(dict(BASE_SUB))
    assert result.status == "review_queue"
    stub["review"].assert_called_once()
    assert stub["review"].call_args.kwargs["payload"]["reason"] == "job_not_found"
    stub["write"].assert_not_called()


def test_inactive_job_routes_to_review(stub):
    stub["get_job"].return_value = SimpleNamespace(
        project_name="Bradley 1", is_active=False, active_status="Archived"
    )
    result = intake.process_portal_submission(dict(BASE_SUB))
    assert result.status == "review_queue"
    assert stub["review"].call_args.kwargs["payload"]["reason"] == "job_inactive"


def test_malformed_work_date_routes_to_review_before_job_lookup(stub):
    sub = dict(BASE_SUB, work_date="not-a-date")
    result = intake.process_portal_submission(sub)
    assert result.status == "review_queue"
    assert stub["review"].call_args.kwargs["payload"]["reason"] == "malformed_work_date"
    stub["get_job"].assert_not_called()


def test_unknown_form_routes_to_review(stub):
    stub["load_def"].return_value = None
    result = intake.process_portal_submission(dict(BASE_SUB))
    assert result.status == "review_queue"
    assert stub["review"].call_args.kwargs["payload"]["reason"] == "unknown_form"
    stub["render"].assert_not_called()


def test_malformed_payload_routes_to_review(stub):
    sub = dict(BASE_SUB, payload_json="{not valid json")
    result = intake.process_portal_submission(sub)
    assert result.status == "review_queue"
    assert stub["review"].call_args.kwargs["payload"]["reason"] == "malformed_payload"


def test_payload_not_object_routes_to_review(stub):
    sub = dict(BASE_SUB, payload_json="[1, 2, 3]")  # valid JSON, not an object
    result = intake.process_portal_submission(sub)
    assert result.status == "review_queue"
    assert stub["review"].call_args.kwargs["payload"]["reason"] == "malformed_payload"


def test_unresolved_box_root_routes_to_review(stub):
    stub["get_folder_id"].return_value = ""  # no Box root for the project
    result = intake.process_portal_submission(dict(BASE_SUB))
    assert result.status == "review_queue"
    assert stub["review"].call_args.kwargs["payload"]["reason"] == "project_box_root_unresolved"


def test_missing_submission_uuid_routes_to_review(stub):
    sub = dict(BASE_SUB, submission_uuid="")
    result = intake.process_portal_submission(sub)
    assert result.status == "review_queue"
    assert stub["review"].call_args.kwargs["payload"]["reason"] == "missing_submission_uuid"


# ---- amend ---------------------------------------------------------------


def test_amend_supersedes_prior_row(stub):
    sub = dict(BASE_SUB, submission_uuid="u2", amends_uuid="u1")
    result = intake.process_portal_submission(sub)
    assert result.status == "processed"
    stub["supersede"].assert_called_once_with(8001, "u1", "u2")


def test_amend_missing_prior_still_files(stub):
    stub["supersede"].return_value = False  # prior not on the sheet
    sub = dict(BASE_SUB, submission_uuid="u2", amends_uuid="u1")
    result = intake.process_portal_submission(sub)
    assert result.status == "processed"  # Box keeps both; supersede pointer just absent
    # The missing-prior case WARNs — never silent (CLAUDE.md "never silent").
    amend_logs = [c for c in stub["log"].call_args_list if c.kwargs.get("error_code") == "portal_amend"]
    assert amend_logs and amend_logs[0].args[0] == intake.Severity.WARN


# ---- transient infra → error (NOT drained) -------------------------------


def test_transient_smartsheet_error_returns_error(stub):
    stub["ensure"].side_effect = SmartsheetError("503")
    result = intake.process_portal_submission(dict(BASE_SUB))
    assert result.status == "error"  # portal_poll will NOT mark-filed → re-pull


def test_permanent_smartsheet_validation_error_routes_to_review(stub):
    """A 400 (e.g. errorCode 1041 sheet-name-too-long) is PERMANENT — re-pull can
    never fix it, so it must DRAIN to the Review Queue, not loop as transient
    'error'. This is the regression for the live JOB-000013 infinite-loop."""
    stub["ensure"].side_effect = SmartsheetValidationError(
        "HTTP 400 (code 1041): The value for sheet.name must be 50 characters in "
        "length or less, but was 57."
    )
    result = intake.process_portal_submission(dict(BASE_SUB))
    assert result.status == "review_queue"  # drains (mark-filed), no infinite re-pull
    assert stub["review"].call_args.kwargs["payload"]["reason"] == "smartsheet_validation"


def test_transient_box_error_returns_error(stub):
    stub["upload"].side_effect = box_client.BoxRateLimitError("429")
    result = intake.process_portal_submission(dict(BASE_SUB))
    assert result.status == "error"


def test_permanent_box_error_routes_to_review(stub):
    stub["upload"].side_effect = box_client.BoxError("weird 400")
    result = intake.process_portal_submission(dict(BASE_SUB))
    assert result.status == "review_queue"
    assert stub["review"].call_args.kwargs["payload"]["reason"] == "box_error"


# ---- incomplete checklist is flagged, not silent -------------------------


def test_incomplete_checklist_tagged_in_notes_but_still_files(stub):
    stub["incomplete"].return_value = [("sec", "item1", "Item 1"), ("sec", "item2", "Item 2")]
    result = intake.process_portal_submission(dict(BASE_SUB))
    assert result.status == "processed"
    assert "[incomplete: 2 items]" in stub["write"].call_args.kwargs["notes"]


# ---- _file_portal_pdf conflict → suffix → recover (idempotent retry) -------


def test_file_portal_pdf_base_name_success(mocker):
    up = mocker.patch.object(intake.box_client, "upload_bytes",
                             return_value={"id": "f1", "name": "n", "size": 1})
    link, file_id = intake._file_portal_pdf("fld", "Bradley 1", "2026-06-05", "jha", "u1abcdef00", b"x")
    assert link == "https://app.box.com/file/f1"
    assert file_id == "f1"  # PR-4: the structural id rides alongside the link
    assert up.call_args.args[1] == "Bradley 1_2026-06-05_jha.pdf"  # job-prefixed clean base name


def test_file_portal_pdf_conflict_then_suffix(mocker):
    up = mocker.patch.object(
        intake.box_client, "upload_bytes",
        side_effect=[box_client.BoxConflictError("dup"), {"id": "f2", "name": "n", "size": 1}],
    )
    link, file_id = intake._file_portal_pdf("fld", "Bradley 1", "2026-06-05", "jha", "u1abcdef00", b"x")
    assert link == "https://app.box.com/file/f2"
    assert file_id == "f2"
    assert up.call_args_list[1].args[1] == "Bradley 1_2026-06-05_jha-u1abcdef.pdf"  # short-uuid suffix


def test_file_portal_pdf_suffix_conflict_recovers_existing_link(mocker):
    mocker.patch.object(
        intake.box_client, "upload_bytes",
        side_effect=[box_client.BoxConflictError("a"), box_client.BoxConflictError("b")],
    )
    mocker.patch.object(
        intake.box_client, "list_folder",
        return_value=[{"id": "r9", "name": "Bradley 1_2026-06-05_jha-u1abcdef.pdf", "type": "file"}],
    )
    link, file_id = intake._file_portal_pdf("fld", "Bradley 1", "2026-06-05", "jha", "u1abcdef00", b"x")
    assert link == "https://app.box.com/file/r9"  # recovered the prior partial upload
    assert file_id == "r9"  # the recovered file's id rides too


def test_file_portal_pdf_suffix_conflict_no_recovery_reraises(mocker):
    mocker.patch.object(
        intake.box_client, "upload_bytes",
        side_effect=[box_client.BoxConflictError("a"), box_client.BoxConflictError("b")],
    )
    mocker.patch.object(intake.box_client, "list_folder", return_value=[])
    with pytest.raises(box_client.BoxConflictError):
        intake._file_portal_pdf("fld", "Bradley 1", "2026-06-05", "jha", "u1abcdef00", b"x")


# ---- _box_file_id_from_link (already_filed id recovery) -------------------


@pytest.mark.parametrize(
    "link,expected",
    [
        ("https://app.box.com/file/12345", "12345"),
        ("https://app.box.com/file/old", "old"),
        ("", None),
        ("https://app.box.com/folder/9", None),  # no /file/ segment
        ("https://app.box.com/file/", None),     # trailing slash, empty id
    ],
)
def test_box_file_id_from_link(link, expected):
    assert intake._box_file_id_from_link(link) == expected


# ---- _resolve_portal_box_folder ------------------------------------------


def test_resolve_box_folder_category_hit(mocker):
    mocker.patch.object(intake, "_portal_box_root", return_value="")  # legacy path
    mocker.patch.object(intake.project_routing, "get_folder_id", return_value="root1")
    mocker.patch.object(intake, "_resolve_box_subfolder", return_value="leaf1")
    fid, note = intake._resolve_portal_box_folder("Bradley 1", "jha", date(2026, 6, 5))
    assert fid == "leaf1" and note.startswith("category:")


def test_resolve_box_folder_unknown_parent_uses_fallback(mocker):
    mocker.patch.object(intake, "_portal_box_root", return_value="")
    mocker.patch.object(intake.project_routing, "get_folder_id", return_value="root1")
    goc = mocker.patch.object(intake.box_client, "get_or_create_folder", return_value="fb1")
    fid, note = intake._resolve_portal_box_folder("Bradley 1", "unknown-parent", date(2026, 6, 5))
    assert fid == "fb1" and "fallback" in note
    goc.assert_called_once_with("root1", intake.PORTAL_BOX_FALLBACK_FOLDER)


def test_resolve_box_folder_unresolved_root_returns_none(mocker):
    mocker.patch.object(intake, "_portal_box_root", return_value="")
    mocker.patch.object(intake.project_routing, "get_folder_id", return_value="")
    fid, note = intake._resolve_portal_box_folder("Bradley 1", "jha", date(2026, 6, 5))
    assert fid is None and note == "project_box_root_unresolved"


def test_resolve_box_folder_mirror_tree_when_root_set(mocker):
    mocker.patch.object(intake, "_portal_box_root", return_value="ROOT9")
    goc = mocker.patch.object(
        intake.box_client, "get_or_create_folder", side_effect=["jobX", "weekX"]
    )
    fid, note = intake._resolve_portal_box_folder("A/B Site", "jha", date(2026, 6, 5))
    assert fid == "weekX" and note == "mirror_tree"
    assert goc.call_args_list[0].args == ("ROOT9", "A-B Site")  # sanitized job folder
    assert goc.call_args_list[1].args == ("jobX", "week of 2026-05-30")


# ---- _portal_review payload completeness ----------------------------------


def test_portal_review_constructs_complete_payload(mocker):
    add = mocker.patch.object(intake.review_queue, "add")
    mocker.patch.object(intake.error_log, "log")
    res = intake._portal_review(
        dict(BASE_SUB), machine_reason="unknown_form", summary="s",
        reason=intake.review_queue.ReviewReason.STRUCTURED_OUTPUT_EDGE, correlation_id="c",
    )
    assert res.status == "review_queue"
    payload = add.call_args.kwargs["payload"]
    for k in ("submission_uuid", "job_id", "form_code", "work_date", "amends_uuid", "reason", "payload_json"):
        assert k in payload
    assert payload["reason"] == "unknown_form"
    assert add.call_args.kwargs["source_file"] == "u1"


# ---- job-orphan routing → Orphaned Reports (Part C) ----------------------


@pytest.fixture
def orphan_on(stub, mocker):
    """Activate Part C: SHEET_ORPHANED_REPORTS set + a portal Box root + add_rows mocked."""
    mocker.patch.object(intake.sheet_ids, "SHEET_ORPHANED_REPORTS", 7777)
    stub["box_root"].return_value = "boxroot1"
    stub["add_rows"] = mocker.patch.object(
        intake.smartsheet_client, "add_rows", return_value=[111]
    )
    return stub


def test_unknown_job_routes_to_orphaned_reports_when_enabled(orphan_on):
    orphan_on["get_job"].return_value = None
    result = intake.process_portal_submission(dict(BASE_SUB))
    # Rendered + filed to the Orphaned Reports Box folder + a sheet row written; NOT the queue.
    orphan_on["render"].assert_called_once()
    orphan_on["add_rows"].assert_called_once()
    assert orphan_on["add_rows"].call_args.args[0] == 7777  # SHEET_ORPHANED_REPORTS
    row = orphan_on["add_rows"].call_args.args[1][0]
    assert row["Reason"] == "job_not_found" and row["Status"] == "Pending"
    assert row["Box Link"] and row["Submission UUID"] == "u1"
    orphan_on["review"].assert_not_called()
    assert result.status == "review_queue" and result.box_link  # drains (filed)


def test_inactive_job_routes_to_orphaned_reports_when_enabled(orphan_on):
    orphan_on["get_job"].return_value = SimpleNamespace(
        project_name="P", is_active=False, active_status="Inactive"
    )
    intake.process_portal_submission(dict(BASE_SUB))
    orphan_on["add_rows"].assert_called_once()
    assert orphan_on["add_rows"].call_args.args[1][0]["Reason"] == "job_inactive"
    orphan_on["review"].assert_not_called()


def test_orphan_falls_back_to_review_when_disabled(stub, mocker):
    # Part C OFF → the generic Review Queue (pre-Part-C). EXPLICITLY pin SHEET_ORPHANED_REPORTS
    # to 0 — never rely on the module default, which the operator FLIPS at activation (a stale
    # coupling to that default red-CI'd the activation PR #235).
    mocker.patch.object(intake.sheet_ids, "SHEET_ORPHANED_REPORTS", 0)
    stub["get_job"].return_value = None
    stub["box_root"].return_value = "boxroot1"  # box root set, but sheet id 0 → OFF
    result = intake.process_portal_submission(dict(BASE_SUB))
    stub["review"].assert_called_once()
    assert result.status == "review_queue"


def test_empty_job_id_stays_in_review_not_orphan(orphan_on):
    # no_job_id is NOT a job-orphan (brief C3 split) → Review Queue even when Part C is ON.
    result = intake.process_portal_submission(dict(BASE_SUB, job_id=""))
    orphan_on["review"].assert_called_once()
    orphan_on["add_rows"].assert_not_called()
    assert result.status == "review_queue"


def test_orphan_unrenderable_form_falls_back_to_review(orphan_on):
    # A structurally-bad submission (unknown form) is not a clean orphan → Review Queue.
    orphan_on["get_job"].return_value = None
    orphan_on["load_def"].return_value = None
    intake.process_portal_submission(dict(BASE_SUB))
    orphan_on["review"].assert_called_once()
    orphan_on["add_rows"].assert_not_called()


# ==========================================================================
# §34 portal photo screening (PR-2)
# ==========================================================================
import base64  # noqa: E402 — grouped with the photo tests for locality
import io  # noqa: E402

from safety_reports import photo_screen  # noqa: E402

# A form definition with a header-level photo field.
PHOTO_DEFINITION = {
    "form_code": "jha-v1",
    "parent_form_code": "jha",
    "form_name": "Job Hazard Analysis",
    "sections": [
        {
            "type": "header",
            "fields": [
                {"key": "work_location", "input": "text", "label": "Location"},
                {"key": "site_photos", "input": "photo", "label": "Site Photos", "max_count": 4},
            ],
        }
    ],
}


def _jpeg_b64(size=(48, 36), color=(10, 110, 60)) -> str:
    from PIL import Image as _PILImage

    buf = io.BytesIO()
    _PILImage.new("RGB", size, color).save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def _photo_obj(data: str, name="front.jpg", taken_at="2026-06-12T09:30:00", gps="34.0,-118.2"):
    return {"data": data, "name": name, "taken_at": taken_at, "gps": gps}


def _payload(photos: list[dict]) -> str:
    import json

    return json.dumps({"work_location": "Array A", "site_photos": photos})


# ---- clean photo: files + embeds + uploads originals ---------------------
def test_clean_photo_files_embeds_and_uploads(stub, mocker):
    stub["load_def"].return_value = PHOTO_DEFINITION
    new_ver = mocker.patch.object(
        intake.box_client, "upload_bytes_or_new_version", return_value={"id": "p1"}
    )
    # Let the REAL renderer run so we exercise the photo-grid embed end-to-end.
    real_render = mocker.patch.object(
        intake.form_pdf, "render_submission_pdf", wraps=intake.form_pdf.render_submission_pdf
    )
    sub = dict(BASE_SUB, payload_json=_payload([_photo_obj(_jpeg_b64())]))
    result = intake.process_portal_submission(sub)

    assert result.status == "processed"
    # render received the SCREENED photos (a re-encoded JPEG + caption) grouped per
    # photo FIELD under the field's label, not raw base64.
    rendered = real_render.call_args.args[1]
    groups = rendered["photo_groups"]
    assert len(groups) == 1
    label, photos = groups[0]
    assert label == "Site Photos" and len(photos) == 1
    caption, jpeg = photos[0]
    assert "front.jpg" in caption and jpeg.startswith(b"\xff\xd8\xff")
    # Box originals filed under ITS Photos/<uuid>/ via version-on-conflict.
    new_ver.assert_called_once()
    folder_arg, name_arg, bytes_arg = new_ver.call_args.args
    assert name_arg == "01.jpg" and bytes_arg.startswith(b"\xff\xd8\xff")
    stub["review"].assert_not_called()


def test_clean_photo_creates_its_photos_subtree(stub, mocker):
    stub["load_def"].return_value = PHOTO_DEFINITION
    mocker.patch.object(intake.box_client, "upload_bytes_or_new_version", return_value={"id": "p1"})
    intake.process_portal_submission(dict(BASE_SUB, payload_json=_payload([_photo_obj(_jpeg_b64())])))
    folder_names = [c.args[1] for c in stub["get_or_create"].call_args_list]
    assert "ITS Photos" in folder_names
    assert "u1" in folder_names  # the per-submission subfolder


# ---- malicious photo: refused, paged, not filed --------------------------
def test_malicious_photo_refused_paged_not_filed(stub, mocker):
    stub["load_def"].return_value = PHOTO_DEFINITION
    # A solid PNG whose dimensions exceed the decompression-bomb cap (small encoded size).
    buf = io.BytesIO()
    from PIL import Image as _PILImage

    _PILImage.new("RGB", (5001, 5001), (1, 1, 1)).save(buf, format="PNG")
    bomb_b64 = base64.b64encode(buf.getvalue()).decode()
    sub = dict(BASE_SUB, payload_json=_payload([_photo_obj(bomb_b64)]), actor_username="pm.jones")
    result = intake.process_portal_submission(sub)

    assert result.status == "review_queue"
    assert result.notes == "reason=photo_malicious"
    # Refused BEFORE render/file: neither the renderer nor the week-sheet writer ran.
    stub["render"].assert_not_called()
    stub["write"].assert_not_called()
    # Review Queue row: security-flagged, CRITICAL, SECURITY_TRIGGER.
    kw = stub["review"].call_args.kwargs
    assert kw["security_flag"] is True
    assert kw["severity"] is intake.Severity.CRITICAL
    assert kw["reason"] is intake.review_queue.ReviewReason.SECURITY_TRIGGER
    assert "pm.jones" in kw["summary"] and "DISABLE" in kw["summary"]
    # CRITICAL page fired naming the account for operator disable.
    crit = [c for c in stub["log"].call_args_list if c.args[0] is intake.Severity.CRITICAL]
    assert crit and "disable this portal account" in crit[0].args[2]


# ---- suspicious photo: refused to review, NOT paged ----------------------
def test_suspicious_photo_routed_to_review_not_paged(stub):
    stub["load_def"].return_value = PHOTO_DEFINITION
    # Wrong magic (GIF) → suspicious (L1 magic_mismatch).
    bad = base64.b64encode(b"GIF89a" + b"\x00" * 64).decode()
    result = intake.process_portal_submission(dict(BASE_SUB, payload_json=_payload([_photo_obj(bad)])))

    assert result.status == "review_queue"
    assert result.notes == "reason=photo_suspicious"
    kw = stub["review"].call_args.kwargs
    assert kw["security_flag"] is True
    assert kw["severity"] is intake.Severity.WARN          # suspicious does NOT page
    assert kw["reason"] is intake.review_queue.ReviewReason.SECURITY_TRIGGER
    crit = [c for c in stub["log"].call_args_list if c.args[0] is intake.Severity.CRITICAL]
    assert not crit
    stub["write"].assert_not_called()


def test_undecodable_base64_is_suspicious(stub):
    stub["load_def"].return_value = PHOTO_DEFINITION
    sub = dict(BASE_SUB, payload_json=_payload([_photo_obj("!!! not base64 !!!")]))
    result = intake.process_portal_submission(sub)
    assert result.status == "review_queue"
    assert result.notes == "reason=photo_suspicious"


# ---- best-effort Box upload (never sinks the filed submission) -----------
def test_photo_box_upload_failure_is_best_effort(stub, mocker):
    stub["load_def"].return_value = PHOTO_DEFINITION
    mocker.patch.object(
        intake.box_client, "upload_bytes_or_new_version",
        side_effect=box_client.BoxError("boom"),
    )
    result = intake.process_portal_submission(dict(BASE_SUB, payload_json=_payload([_photo_obj(_jpeg_b64())])))
    assert result.status == "processed"  # the PDF-of-record already filed; photo upload WARNs
    warns = [c for c in stub["log"].call_args_list
             if c.kwargs.get("error_code") == "portal_photo_upload_failed"]
    assert warns


# ---- no photo field: unchanged behavior ----------------------------------
# ---- 0074: pool registrations returned for the site-photos bridge ---------
def test_clean_photo_returns_pool_registrations(stub, mocker):
    stub["load_def"].return_value = PHOTO_DEFINITION
    mocker.patch.object(
        intake.box_client, "upload_bytes_or_new_version", return_value={"id": "p1"}
    )
    sub = dict(BASE_SUB, payload_json=_payload([_photo_obj(_jpeg_b64())]))
    result = intake.process_portal_submission(sub)

    assert result.status == "processed"
    regs = result.site_photo_registrations
    assert regs is not None and len(regs) == 1
    assert regs[0]["box_file_id"] == "p1"
    assert "front.jpg" in regs[0]["caption"]
    # The thumb decodes to a real JPEG derived from the clean re-encode, bounded.
    import base64 as _b64
    thumb = _b64.b64decode(regs[0]["thumb_b64"])
    assert thumb[:3] == b"\xff\xd8\xff" and len(thumb) <= 40_000


def test_photo_box_failure_returns_no_registrations(stub, mocker):
    # The best-effort fence: upload fails → WARN (existing behaviour) AND the result
    # carries NO registrations — portal_poll must not register photos Box never got.
    stub["load_def"].return_value = PHOTO_DEFINITION
    mocker.patch.object(
        intake.box_client, "upload_bytes_or_new_version",
        side_effect=RuntimeError("box down"),
    )
    sub = dict(BASE_SUB, payload_json=_payload([_photo_obj(_jpeg_b64())]))
    result = intake.process_portal_submission(sub)
    assert result.status == "processed"
    assert result.site_photo_registrations is None


def test_no_photos_yields_no_registrations(stub):
    result = intake.process_portal_submission(dict(BASE_SUB))
    assert result.status == "processed"
    assert result.site_photo_registrations is None


def test_no_photo_field_files_normally(stub):
    # The default DEFINITION has no photo field → photo_groups empty, no Box photo tree.
    result = intake.process_portal_submission(dict(BASE_SUB))
    assert result.status == "processed"
    render_sub = stub["render"].call_args.args[1]
    assert render_sub["photo_groups"] == []
    # The render dict carries GROUPS only — the flat screened_photos key is gone
    # (form_pdf keeps a legacy fallback for old callers; intake no longer uses it).
    assert "screened_photos" not in render_sub


# ---- _screen_portal_photos refuses a submission past the per-submission cap ----
def test_screen_over_cap_refuses_whole_submission(mocker):
    mocker.patch.object(intake, "_photo_clamav_enabled", return_value=False)
    review = mocker.patch.object(intake.review_queue, "add")
    mocker.patch.object(intake.error_log, "log")
    # Three photo fields × 3 photos = 9 > MAX_PHOTOS_PER_SUBMISSION (8). A submission past
    # the Worker's cap can only arrive by bypassing the Worker → refuse the whole thing.
    img = _jpeg_b64()
    definition = {"sections": [{"type": "header", "fields": [
        {"key": "a", "input": "photo", "label": "A", "max_count": 4},
        {"key": "b", "input": "photo", "label": "B", "max_count": 4},
        {"key": "c", "input": "photo", "label": "C", "max_count": 4},
    ]}]}
    values = {k: [_photo_obj(img) for _ in range(3)] for k in ("a", "b", "c")}
    refusal, screened = intake._screen_portal_photos(
        definition, values, dict(BASE_SUB), correlation_id="t"
    )
    assert refusal is not None
    assert refusal.status == "review_queue"
    assert refusal.notes == "reason=photo_suspicious"
    assert screened == []
    kw = review.call_args.kwargs
    assert kw["security_flag"] is True
    assert "over_submission_cap" in kw["payload"]["detail"]


def test_screen_at_cap_is_accepted(mocker):
    # Exactly 8 photos (2 fields × 4) is allowed — no refusal. One group per field,
    # in definition order, labels preserved.
    mocker.patch.object(intake, "_photo_clamav_enabled", return_value=False)
    img = _jpeg_b64()
    definition = {"sections": [{"type": "header", "fields": [
        {"key": "a", "input": "photo", "label": "A", "max_count": 4},
        {"key": "b", "input": "photo", "label": "B", "max_count": 4},
    ]}]}
    values = {k: [_photo_obj(img) for _ in range(4)] for k in ("a", "b")}
    refusal, groups = intake._screen_portal_photos(
        definition, values, dict(BASE_SUB), correlation_id="t"
    )
    assert refusal is None
    assert [label for label, _photos in groups] == ["A", "B"]
    total = sum(len(photos) for _label, photos in groups)
    assert total == photo_screen.MAX_PHOTOS_PER_SUBMISSION


# ---- per-field grouping (photos-pdf-grouping): labels thread to the renderer ----
TWO_FIELD_DEFINITION = {
    "form_code": "jha-v1",
    "parent_form_code": "jha",
    "form_name": "Job Hazard Analysis",
    "sections": [
        {
            "type": "header",
            "fields": [
                {"key": "before_photos", "input": "photo", "label": "Before Work",
                 "max_count": 4},
                {"key": "after_photos", "input": "photo", "label": "After Work",
                 "max_count": 4},
            ],
        }
    ],
}


def test_two_photo_fields_group_in_definition_order(stub, mocker):
    """Two labeled photo fields → two render groups in DEFINITION order with the
    labels preserved; Box 01..NN numbering runs CONTINUOUSLY across the groups
    (the flattened order is byte-identical to the pre-grouping single list)."""
    stub["load_def"].return_value = TWO_FIELD_DEFINITION
    new_ver = mocker.patch.object(
        intake.box_client, "upload_bytes_or_new_version", return_value={"id": "p1"}
    )
    payload = json.dumps({
        "work_location": "Array A",
        "before_photos": [_photo_obj(_jpeg_b64(), name=f"b{i}.jpg") for i in range(4)],
        "after_photos": [_photo_obj(_jpeg_b64(), name=f"a{i}.jpg") for i in range(4)],
    })
    result = intake.process_portal_submission(dict(BASE_SUB, payload_json=payload))
    assert result.status == "processed"
    groups = stub["render"].call_args.args[1]["photo_groups"]
    assert [label for label, _photos in groups] == ["Before Work", "After Work"]
    assert [len(photos) for _label, photos in groups] == [4, 4]
    # Field order within each group is upload order; captions carry the client names.
    captions = [cap for _label, photos in groups for cap, _jpeg in photos]
    assert "b0.jpg" in captions[0] and "a0.jpg" in captions[4]
    # Box numbering spans the groups continuously: 01..08.jpg, one flat sequence.
    filenames = [c.args[1] for c in new_ver.call_args_list]
    assert filenames == [f"{i:02d}.jpg" for i in range(1, 9)]
    assert "[photos:8]" in stub["write"].call_args.kwargs["notes"]


def test_photo_field_with_no_photos_contributes_no_group(stub, mocker):
    """A photo field whose value is an empty list contributes NO group (a heading
    over nothing would claim photos that are not there) — the other field's group
    still renders under its own label."""
    stub["load_def"].return_value = TWO_FIELD_DEFINITION
    mocker.patch.object(
        intake.box_client, "upload_bytes_or_new_version", return_value={"id": "p1"}
    )
    payload = json.dumps({
        "before_photos": [],
        "after_photos": [_photo_obj(_jpeg_b64())],
    })
    result = intake.process_portal_submission(dict(BASE_SUB, payload_json=payload))
    assert result.status == "processed"
    groups = stub["render"].call_args.args[1]["photo_groups"]
    assert [label for label, _photos in groups] == ["After Work"]


# ── P3: workstream category routing (safety|progress) ──────────────────────


def test_progress_form_routes_to_progress_when_flag_on(stub, mocker):
    """A progress-category form (daily-report) routes to the PROGRESS workspace's
    week-sheet when progress_reports.intake_enabled is ON."""
    mocker.patch.object(intake, "_progress_intake_enabled", return_value=True)
    res = intake.process_portal_submission(dict(BASE_SUB, form_code="daily-report-v1"))
    assert res.status == "processed"
    assert stub["ensure"].call_args.args[0] is week_sheet.PROGRESS_WEEK_SHEET_CONFIG


def test_progress_form_routes_to_safety_when_flag_off(stub, mocker):
    """Built-dark default: flag OFF → a progress submission files into the SAFETY
    workspace exactly as it does today (byte-identical pre-P3 behavior)."""
    mocker.patch.object(intake, "_progress_intake_enabled", return_value=False)
    res = intake.process_portal_submission(dict(BASE_SUB, form_code="daily-report-v1"))
    assert res.status == "processed"
    assert stub["ensure"].call_args.args[0] is week_sheet.SAFETY_WEEK_SHEET_CONFIG


def test_safety_form_routes_to_safety_and_skips_the_flag_read(stub, mocker):
    """A safety form ALWAYS routes safety; the `and` short-circuits the flag read (so the
    safety path is byte-identical and never even hits ITS_Config), even with the flag ON."""
    gate = mocker.patch.object(intake, "_progress_intake_enabled", return_value=True)
    res = intake.process_portal_submission(dict(BASE_SUB, form_code="jha-v1"))
    assert res.status == "processed"
    assert stub["ensure"].call_args.args[0] is week_sheet.SAFETY_WEEK_SHEET_CONFIG
    gate.assert_not_called()


def test_unknown_form_routes_to_safety_when_flag_on(stub, mocker):
    """An uncatalogued form_code defaults to safety (deny-by-route) even with the flag ON."""
    mocker.patch.object(intake, "_progress_intake_enabled", return_value=True)
    res = intake.process_portal_submission(dict(BASE_SUB, form_code="totally-unknown-form"))
    assert res.status == "processed"
    assert stub["ensure"].call_args.args[0] is week_sheet.SAFETY_WEEK_SHEET_CONFIG


def test_golden_mixed_week_routes_each_submission_by_category(stub, mocker):
    """One Sat→Fri week, a safety + a progress submission, flag ON: each routes to its own
    workspace's week-sheet, and the resolved sheet id flows downstream to the row write."""
    mocker.patch.object(intake, "_progress_intake_enabled", return_value=True)
    stub["ensure"].side_effect = lambda cfg, proj, wd: (
        9001 if cfg is week_sheet.PROGRESS_WEEK_SHEET_CONFIG else 8001
    )
    safety_sub = dict(BASE_SUB, submission_uuid="s1", form_code="jha-v1", work_date="2026-06-05")
    progress_sub = dict(  # SAME Sat→Fri week (opens 2026-05-30)
        BASE_SUB, submission_uuid="p1", form_code="daily-report-v1", work_date="2026-06-04"
    )

    assert intake.process_portal_submission(safety_sub).status == "processed"
    assert intake.process_portal_submission(progress_sub).status == "processed"

    assert [c.args[0] for c in stub["ensure"].call_args_list] == [
        week_sheet.SAFETY_WEEK_SHEET_CONFIG,
        week_sheet.PROGRESS_WEEK_SHEET_CONFIG,
    ]
    # The routed sheet id flows downstream: each per-submission row write hit its own sheet.
    assert [c.args[0] for c in stub["write"].call_args_list] == [8001, 9001]


def test_progress_intake_enabled_flag_read_contract(mocker):
    """The gate reads progress_reports.intake_enabled under the intake daemon's
    (safety_reports) workstream — NOT progress_reports. 'true' → ON; a missing row → OFF
    (the built-dark default); a non-NotFound SmartsheetError (e.g. circuit-open) PROPAGATES
    so the submission soft-fails to 'error' and re-pulls, never a silent misroute."""
    from shared import smartsheet_client

    g = mocker.patch.object(intake.smartsheet_client, "get_setting")

    g.return_value = "true"
    assert intake._progress_intake_enabled() is True
    assert g.call_args.kwargs.get("workstream") == "safety_reports"  # the footgun guard

    g.return_value = "false"
    assert intake._progress_intake_enabled() is False

    g.side_effect = smartsheet_client.SmartsheetNotFoundError("no row")
    assert intake._progress_intake_enabled() is False  # missing row → built-dark default

    g.side_effect = smartsheet_client.SmartsheetCircuitOpenError("breaker open")
    with pytest.raises(smartsheet_client.SmartsheetError):
        intake._progress_intake_enabled()  # propagates → process_portal_submission → 'error'


# ---- DR-photo-pool Slice 2: additional-photo pool reference resolution -----
#
# The daily report's ADDITIONAL photos never ride the payload — the submission
# carries HMAC-covered REFERENCES (values.additional_photos) and the pulled row
# carries the Worker-resolved CLAIM MANIFEST (`daily_photos`). intake resolves:
# clean → download the §34 re-encode from Box (box_client seam) into the render
# grid AFTER the inline photos; pending → bounded defer (status 'deferred');
# refused/unavailable → PDF notes. See intake._resolve_additional_photos.

DAILY_DEFINITION = {
    "form_code": "daily-report-v6",
    "parent_form_code": "daily-report",
    "form_name": "Daily Field Report",
    "sections": [
        {"type": "additional_photos", "key": "additional_photos",
         "title": "Additional site photos"},
    ],
}

POOL_JPEG = b"\xff\xd8\xff\xe0" + b"j" * 64  # JPEG magic + body


def _daily_sub(
    refs: list[dict] | None = None,
    manifest: list[dict] | None = None,
    created_at: int | float | None = None,
) -> dict[str, Any]:
    """A pulled daily-report row. `manifest` None means NO daily_photos key at all
    (the old-Worker case); pass a list for the normal enriched row. `created_at`
    None keeps BASE_SUB's 2024 epoch — ancient, i.e. the defer bound is EXPIRED."""
    sub: dict[str, Any] = dict(BASE_SUB)
    sub["form_code"] = "daily-report-v6"
    values: dict = {"prepared_by": "Mo"}
    if refs is not None:
        values["additional_photos"] = refs
    sub["payload_json"] = json.dumps(values)
    if manifest is not None:
        sub["daily_photos"] = manifest
    if created_at is not None:
        sub["created_at"] = created_at
    return sub


@pytest.fixture
def daily_stub(stub, mocker) -> dict[str, MagicMock]:
    """The portal stub pointed at the daily definition + a Box download seam.

    daily-report-v6 is a PROGRESS-category form — the P3 routing gate reads
    ITS_Config, so it is pinned OFF here (safety week-sheet, the built-dark
    default); pool resolution is orthogonal to workstream routing."""
    stub["load_def"].return_value = DAILY_DEFINITION
    stub["progress_enabled"] = mocker.patch.object(
        intake, "_progress_intake_enabled", return_value=False
    )
    stub["download"] = mocker.patch.object(
        intake.box_client, "download_file", return_value=POOL_JPEG
    )
    return stub


def test_pool_clean_refs_download_box_and_join_render_grid(daily_stub):
    sub = _daily_sub(
        refs=[{"pool_id": 11, "caption": "Trench shoring"}],
        manifest=[{"id": 11, "status": "clean", "box_file_id": "bx11"}],
    )
    result = intake.process_portal_submission(sub)
    assert result.status == "processed"
    # Downloaded from Box by the manifest's box_file_id (the PR-4 precedent — the
    # bytes left D1 at screen time; Box is the permanent record).
    daily_stub["download"].assert_called_once_with("bx11")
    render_sub = daily_stub["render"].call_args.args[1]
    # The pool photos render as their OWN final group, headed by the section title
    # (this form has no inline photo fields, so it is the only group).
    assert render_sub["photo_groups"] == [
        ("Additional site photos", [("Trench shoring", POOL_JPEG)]),
    ]
    assert "screened_photos" not in render_sub  # groups replaced the flat key
    assert render_sub["additional_photo_notes"] == {
        "pending": 0, "refused": 0, "unavailable": 0,
    }
    assert "[pool_photos:1]" in daily_stub["write"].call_args.kwargs["notes"]


def test_pool_pending_ref_defers_young_submission(daily_stub):
    sub = _daily_sub(
        refs=[{"pool_id": 11}],
        manifest=[{"id": 11, "status": "pending", "box_file_id": None}],
        created_at=datetime.now(UTC).timestamp(),  # young → inside the defer bound
    )
    result = intake.process_portal_submission(sub)
    assert result.status == "deferred"
    assert "1 pool photo(s) pending screening" in (result.notes or "")
    # Nothing filed, nothing rendered — the re-pull next cycle re-resolves.
    daily_stub["render"].assert_not_called()
    daily_stub["write"].assert_not_called()
    daily_stub["upload"].assert_not_called()
    codes = [c.kwargs.get("error_code") for c in daily_stub["log"].call_args_list]
    assert "portal_daily_photo_deferred" in codes


def test_pool_pending_ref_defer_bound_expired_files_without(daily_stub):
    # BASE_SUB's created_at is a 2024 epoch — far past DAILY_PHOTO_DEFER_MAX_SECONDS.
    sub = _daily_sub(
        refs=[{"pool_id": 11}, {"pool_id": 12, "caption": "Panel run"}],
        manifest=[
            {"id": 11, "status": "pending", "box_file_id": None},
            {"id": 12, "status": "clean", "box_file_id": "bx12"},
        ],
    )
    result = intake.process_portal_submission(sub)
    assert result.status == "processed"  # bounded: filing is never blocked forever
    render_sub = daily_stub["render"].call_args.args[1]
    assert render_sub["additional_photo_notes"]["pending"] == 1
    assert render_sub["photo_groups"] == [
        ("Additional site photos", [("Panel run", POOL_JPEG)]),
    ]
    codes = [c.kwargs.get("error_code") for c in daily_stub["log"].call_args_list]
    assert "portal_daily_photo_defer_expired" in codes
    notes = daily_stub["write"].call_args.kwargs["notes"]
    assert "pending:1" in notes


def test_pool_refused_ref_files_with_refused_note(daily_stub):
    sub = _daily_sub(
        refs=[{"pool_id": 11}],
        manifest=[{"id": 11, "status": "refused", "box_file_id": None}],
    )
    result = intake.process_portal_submission(sub)
    assert result.status == "processed"
    # Refused = a PDF note; the CRITICAL/WARN fired at screen time — nothing re-pages,
    # and no Box download is attempted for a refused ref.
    daily_stub["download"].assert_not_called()
    render_sub = daily_stub["render"].call_args.args[1]
    assert render_sub["additional_photo_notes"]["refused"] == 1
    assert render_sub["photo_groups"] == []  # no photos → no pool group at all
    daily_stub["review"].assert_not_called()


def test_pool_absent_refs_are_a_noop(daily_stub):
    # A daily report with NO additional photos (the overwhelmingly common case)
    # takes the pre-slice path untouched: no download, no defer, zero notes.
    sub = _daily_sub(refs=None, manifest=[])
    result = intake.process_portal_submission(sub)
    assert result.status == "processed"
    daily_stub["download"].assert_not_called()
    render_sub = daily_stub["render"].call_args.args[1]
    assert render_sub["additional_photo_notes"] == {
        "pending": 0, "refused": 0, "unavailable": 0,
    }
    assert "[pool_photos" not in daily_stub["write"].call_args.kwargs["notes"]


def test_pool_manifest_missing_files_with_warn_not_defer_loop(daily_stub):
    # Refs but NO daily_photos key on the pulled row (a Worker predating the slice):
    # file WITHOUT the photos + WARN — never a defer loop on state that can't arrive.
    sub = _daily_sub(refs=[{"pool_id": 11}], manifest=None)
    result = intake.process_portal_submission(sub)
    assert result.status == "processed"
    render_sub = daily_stub["render"].call_args.args[1]
    assert render_sub["additional_photo_notes"]["unavailable"] == 1
    codes = [c.kwargs.get("error_code") for c in daily_stub["log"].call_args_list]
    assert "portal_daily_photo_state_missing" in codes


def test_pool_box_download_transient_error_soft_fails_to_error(daily_stub):
    # A transient Box failure must re-pull the WHOLE submission (status 'error'),
    # exactly like every other transient in the pipeline — never file half a grid.
    daily_stub["download"].side_effect = box_client.BoxRateLimitError("429")
    sub = _daily_sub(
        refs=[{"pool_id": 11}],
        manifest=[{"id": 11, "status": "clean", "box_file_id": "bx11"}],
    )
    result = intake.process_portal_submission(sub)
    assert result.status == "error"
    daily_stub["write"].assert_not_called()


def test_pool_box_download_permanent_error_renders_without(daily_stub):
    # A permanent Box error (404 …) degrades ONE photo to 'unavailable' — the
    # submission still files (per-photo fence, never a whole-report loss).
    daily_stub["download"].side_effect = box_client.BoxError("404 not found")
    sub = _daily_sub(
        refs=[{"pool_id": 11}],
        manifest=[{"id": 11, "status": "clean", "box_file_id": "bx11"}],
    )
    result = intake.process_portal_submission(sub)
    assert result.status == "processed"
    render_sub = daily_stub["render"].call_args.args[1]
    assert render_sub["additional_photo_notes"]["unavailable"] == 1
    assert render_sub["photo_groups"] == []
    codes = [c.kwargs.get("error_code") for c in daily_stub["log"].call_args_list]
    assert "portal_daily_photo_box_download_failed" in codes


def test_pool_downloaded_bytes_are_validated_before_render(daily_stub):
    # The manifest is NOT HMAC-covered: a tampered box_file_id must never push
    # arbitrary Box bytes into the PDF-of-record — magic+size validated, else
    # degraded to 'unavailable' (the bytes never reach the renderer).
    daily_stub["download"].return_value = b"%PDF-1.4 definitely not a jpeg"
    sub = _daily_sub(
        refs=[{"pool_id": 11}],
        manifest=[{"id": 11, "status": "clean", "box_file_id": "bx11"}],
    )
    result = intake.process_portal_submission(sub)
    assert result.status == "processed"
    render_sub = daily_stub["render"].call_args.args[1]
    assert render_sub["photo_groups"] == []
    assert render_sub["additional_photo_notes"]["unavailable"] == 1
    codes = [c.kwargs.get("error_code") for c in daily_stub["log"].call_args_list]
    assert "portal_daily_photo_invalid_bytes" in codes


def test_pool_manifest_row_missing_for_ref_renders_without(daily_stub):
    # Claimed at submit but gone from the manifest (pruned orphan / manual cleanup).
    sub = _daily_sub(refs=[{"pool_id": 99}], manifest=[])
    result = intake.process_portal_submission(sub)
    assert result.status == "processed"
    render_sub = daily_stub["render"].call_args.args[1]
    assert render_sub["additional_photo_notes"]["unavailable"] == 1
    codes = [c.kwargs.get("error_code") for c in daily_stub["log"].call_args_list]
    assert "portal_daily_photo_ref_unresolved" in codes


def test_pool_photos_render_after_inline_photos(daily_stub, mocker):
    # "AFTER the inline photos": the pool photos render as their OWN final group,
    # behind every inline per-field group, headed by the section title.
    mocker.patch.object(
        intake, "_screen_portal_photos",
        return_value=(None, [("Inline Field", [("inline cap", b"\xff\xd8\xffINLINE")])]),
    )
    sub = _daily_sub(
        refs=[{"pool_id": 11, "caption": "pool cap"}],
        manifest=[{"id": 11, "status": "clean", "box_file_id": "bx11"}],
    )
    result = intake.process_portal_submission(sub)
    assert result.status == "processed"
    render_sub = daily_stub["render"].call_args.args[1]
    assert render_sub["photo_groups"] == [
        ("Inline Field", [("inline cap", b"\xff\xd8\xffINLINE")]),
        ("Additional site photos", [("pool cap", POOL_JPEG)]),
    ]


def test_pool_group_label_reads_the_section_title(daily_stub):
    # The pool group's heading is the definition's additional_photos section TITLE.
    daily_stub["load_def"].return_value = {
        **DAILY_DEFINITION,
        "sections": [{"type": "additional_photos", "key": "additional_photos",
                      "title": "Extra inspection photos"}],
    }
    sub = _daily_sub(
        refs=[{"pool_id": 11, "caption": "cap"}],
        manifest=[{"id": 11, "status": "clean", "box_file_id": "bx11"}],
    )
    result = intake.process_portal_submission(sub)
    assert result.status == "processed"
    render_sub = daily_stub["render"].call_args.args[1]
    assert render_sub["photo_groups"] == [("Extra inspection photos", [("cap", POOL_JPEG)])]


def test_pool_group_label_defaults_when_section_untitled(daily_stub):
    # No title on the additional_photos section → the default pool heading.
    daily_stub["load_def"].return_value = {
        **DAILY_DEFINITION,
        "sections": [{"type": "additional_photos", "key": "additional_photos"}],
    }
    sub = _daily_sub(
        refs=[{"pool_id": 11, "caption": "cap"}],
        manifest=[{"id": 11, "status": "clean", "box_file_id": "bx11"}],
    )
    result = intake.process_portal_submission(sub)
    assert result.status == "processed"
    render_sub = daily_stub["render"].call_args.args[1]
    assert render_sub["photo_groups"] == [("Additional site photos", [("cap", POOL_JPEG)])]
