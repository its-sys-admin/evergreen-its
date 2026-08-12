"""Tests for safety_reports/weekly_generate.py — the Phase-5 DETERMINISTIC compile.

All Smartsheet/Box/active_jobs calls are mocked. The legacy LLM-path tests were
retired with the Anthropic core (Phase-5 rewrite). Live coverage is the deploy-gated
tests/test_weekly_generate_integration.py.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from safety_reports import generate_core, week_sheet, weekly_generate

ANCHOR = date(2026, 6, 5)  # Friday → Sat→Fri week 2026-05-30 … 2026-06-05


def _job(**kw):
    base = dict(
        project_name="Bradley 1", job_id="JOB-1",
        safety_reports_contact_email="pm@evergreenmirror.com",
        safety_reports_contact_name="Dana PM",
        cc_emails=("a@x.com", "b@x.com"), is_active=True, active_status="Active",
    )
    base.update(kw)
    # generate_core reads the Slice-1 workstream-neutral aliases; mirror them on the mock
    # (the real ActiveJob aliases these to the same underlying value).
    base["reports_contact_email"] = base["safety_reports_contact_email"]
    base["reports_contact_name"] = base["safety_reports_contact_name"]
    return SimpleNamespace(**base)


def _sub(form_code="jha-v1", link="https://app.box.com/file/11", submitted="2026-06-05T08:00:00-07:00"):
    return {
        week_sheet.COL_FORM_CODE: form_code,
        week_sheet.COL_SUBMISSION_PDF: link,
        week_sheet.COL_SUBMITTED_AT: submitted,
        week_sheet.COL_ROW_TYPE: week_sheet.ROW_TYPE_SUBMISSION,
        week_sheet.COL_STATUS: week_sheet.STATUS_ACTIVE,
    }


@pytest.fixture
def stub(mocker) -> dict[str, MagicMock]:
    return {
        "list_jobs": mocker.patch.object(generate_core.active_jobs, "list_active_jobs", return_value=[_job()]),
        "ensure": mocker.patch.object(generate_core.week_sheet, "ensure_week_sheet", return_value=8001),
        # Append-only (operator decision 2026-06-09): list_rollup_rows returns the full
        # (possibly multi-row) Rollup history; the REAL any_compile_now_requested runs on it
        # (NOT mocked), so a {COMPILE_NOW: True} rollup drives `force`. append_rollup_row ADDS a
        # new snapshot (never updates); clear_compile_now_on_rollups clears the trigger (no-op mock).
        "rollup": mocker.patch.object(generate_core.week_sheet, "list_rollup_rows", return_value=[]),
        "subs": mocker.patch.object(generate_core.week_sheet, "list_submission_rows", return_value=[_sub()]),
        "append_rollup": mocker.patch.object(generate_core.week_sheet, "append_rollup_row", return_value=99),
        "clear_rollup": mocker.patch.object(generate_core.week_sheet, "clear_compile_now_on_rollups"),
        "attach": mocker.patch.object(generate_core.smartsheet_client, "attach_pdf_to_row", return_value=1),
        "download": mocker.patch.object(generate_core.box_client, "download_file", return_value=b"%PDF-1.4 one"),
        "merge": mocker.patch.object(generate_core.form_pdf, "merge_pdfs", return_value=b"%PDF-merged"),
        "box_root": mocker.patch.object(generate_core, "_portal_box_root", return_value=""),  # gated OFF → legacy
        "get_root": mocker.patch.object(generate_core.project_routing, "get_folder_id", return_value="root1"),
        "mkfolder": mocker.patch.object(generate_core.box_client, "get_or_create_folder", return_value="wk1"),
        # Append-only: each compile files a DISTINCT packet via _upload_packet — the clean
        # <Job>_week of <Sat>_WSR.pdf, bumping _v2/_v3 on a recompile 409. A clean first compile
        # is a single non-conflicting upload_bytes call, which this return_value models.
        "upload": mocker.patch.object(generate_core.box_client, "upload_bytes", return_value={"id": "pkt9", "name": "x", "size": 9}),
        "wsr": mocker.patch.object(weekly_generate.wsr_review, "add_wsr_row", return_value=123),
        "evergreen": mocker.patch.object(generate_core, "_read_str_setting", return_value="the office"),
        "review": mocker.patch.object(generate_core.review_queue, "add"),
        # Dedupe read inside _safe_review_queue (2026-07-19): no PENDING rows by default.
        "pending": mocker.patch.object(generate_core.review_queue, "get_pending", return_value=[]),
        "marker": mocker.patch.object(generate_core, "_write_watchdog_marker"),
        "log": mocker.patch.object(generate_core.error_log, "log"),
    }


# ---- pure helpers --------------------------------------------------------


@pytest.mark.parametrize("link,expected", [
    ("https://app.box.com/file/123", "123"),
    ("https://app.box.com/file/9?x=1", "9"),
    ("", None),
    ("not a link", None),
    ("https://app.box.com/folder/5", None),
])
def test_box_file_id(link, expected):
    assert generate_core._box_file_id(link) == expected


def test_its_week_folder_name_and_packet_basename():
    wk = generate_core.safety_week.week_bounds(ANCHOR)
    assert generate_core._its_week_folder_name(wk) == "ITS Week of 2026-05-30 to 2026-06-05"
    # Operator naming rule (2026-06-17): clean job-prefixed packet name <Job>_week of <Sat>_WSR.
    # §14 preservation: SAFETY's packet name is byte-identical to its pre-parameterization self.
    assert (generate_core._packet_basename(weekly_generate.SAFETY_GENERATE_CONFIG, "Bradley 1", wk)
            == "Bradley 1_week of 2026-05-30_WSR")
    # PROGRESS no longer files under safety's initials — the two artifacts in one Box week folder
    # are now distinguishable by name rather than by file size.
    from progress_reports.progress_weekly_generate import PROGRESS_GENERATE_CONFIG
    assert (generate_core._packet_basename(PROGRESS_GENERATE_CONFIG, "Bradley 1", wk)
            == "Bradley 1_week of 2026-05-30_FieldRecords")


def test_upload_packet_first_compile_uses_clean_unversioned_name(mocker):
    up = mocker.patch.object(generate_core.box_client, "upload_bytes",
                             return_value={"id": "p1", "name": "n", "size": 1})
    name, file_id = generate_core._upload_packet(
        "fld", "Bradley 1_week of 2026-05-30_WSR", b"x", "20260605-090000-abc123"
    )
    assert (name, file_id) == ("Bradley 1_week of 2026-05-30_WSR.pdf", "p1")
    assert up.call_args.args[1] == "Bradley 1_week of 2026-05-30_WSR.pdf"  # no _v suffix on first compile


def test_upload_packet_recompile_bumps_to_next_version(mocker):
    # base.pdf + _v2.pdf already in the folder (two prior compiles) → next DISTINCT file is _v3.pdf.
    up = mocker.patch.object(
        generate_core.box_client, "upload_bytes",
        side_effect=[
            generate_core.box_client.BoxConflictError("base exists"),
            generate_core.box_client.BoxConflictError("_v2 exists"),
            {"id": "p3", "name": "n", "size": 1},
        ],
    )
    name, file_id = generate_core._upload_packet(
        "fld", "Bradley 1_week of 2026-05-30_WSR", b"x", "20260605-090000-abc123"
    )
    assert (name, file_id) == ("Bradley 1_week of 2026-05-30_WSR_v3.pdf", "p3")
    assert [c.args[1] for c in up.call_args_list] == [
        "Bradley 1_week of 2026-05-30_WSR.pdf",
        "Bradley 1_week of 2026-05-30_WSR_v2.pdf",
        "Bradley 1_week of 2026-05-30_WSR_v3.pdf",
    ]


def test_recipient_display():
    to, cc = generate_core._recipient_display(_job())
    assert to == "pm@evergreenmirror.com"
    assert cc == "a@x.com, b@x.com"


# ---- happy compile -------------------------------------------------------


def test_compile_merges_files_and_dual_writes(stub):
    out = weekly_generate._run_pipeline(week_start_override=ANCHOR)
    assert out["packets_compiled"] == 1 and out["wsr_written"] == 1
    assert out["week_start"] == "2026-05-30" and out["week_end"] == "2026-06-05"
    stub["merge"].assert_called_once()
    stub["mkfolder"].assert_called_once_with("root1", "ITS Week of 2026-05-30 to 2026-06-05")
    stub["upload"].assert_called_once()
    assert stub["append_rollup"].call_args.kwargs["packet_link"] == "https://app.box.com/file/pkt9"
    assert stub["wsr"].call_args.kwargs["compiled_pdf_link"] == "https://app.box.com/file/pkt9"
    assert stub["wsr"].call_args.kwargs["job_id"] == "JOB-1"
    assert stub["wsr"].call_args.kwargs["recipient_to"] == "pm@evergreenmirror.com"


def test_compile_files_packet_into_mirror_tree_when_root_configured(stub):
    # PR-K: with the Box root configured, the packet files into the SAME
    # ROOT → per-job → per-week tree as the submission PDFs (legacy bypassed).
    stub["box_root"].return_value = "ROOT9"
    stub["mkfolder"].side_effect = ["jobP", "weekP"]  # ROOT→job, job→week
    weekly_generate._run_pipeline(week_start_override=ANCHOR)
    calls = stub["mkfolder"].call_args_list
    assert calls[0].args[0] == "ROOT9"  # ROOT → per-job folder
    assert calls[1].args[0] == "jobP" and calls[1].args[1].startswith("week of ")
    assert stub["upload"].call_args.args[0] == "weekP"  # packet into the week folder
    stub["get_root"].assert_not_called()  # legacy project_routing bypassed


def test_compile_attaches_packet_to_rollup_and_wsr_rows(stub):
    weekly_generate._run_pipeline(week_start_override=ANCHOR)
    # The merged packet is attached inline on BOTH the week-sheet Rollup row and
    # the WSR_human_review row (the Box-link cells stay; this is the supplementary copy).
    targets = {(c.args[0], c.args[1]) for c in stub["attach"].call_args_list}
    assert (8001, 99) in targets                                   # week sheet ▸ Rollup row
    assert (weekly_generate.wsr_review.SHEET_ID, 123) in targets   # WSR_human_review row
    for c in stub["attach"].call_args_list:
        assert c.args[3] == b"%PDF-merged" and c.args[2].endswith(".pdf")


def test_empty_week_does_not_attach_a_packet(stub):
    stub["subs"].return_value = []  # no submissions → no packet → nothing to attach
    weekly_generate._run_pipeline(week_start_override=ANCHOR)
    stub["attach"].assert_not_called()


def test_attach_failure_does_not_fail_compile(stub):
    # The inline attachment is supplementary (Box is the SoR) — a failure is a WARN,
    # never a compile failure: the packet is already in Box + linked on the rows.
    from shared.smartsheet_client import SmartsheetError
    stub["attach"].side_effect = SmartsheetError("attach boom")
    out = weekly_generate._run_pipeline(week_start_override=ANCHOR)
    assert out["packets_compiled"] == 1 and out["wsr_written"] == 1  # compile unaffected


def test_two_submissions_merged_in_order(stub):
    stub["subs"].return_value = [
        _sub("jha-v1", "https://app.box.com/file/11"),
        _sub("toolbox-talk-ppe-v1", "https://app.box.com/file/22"),
    ]
    stub["download"].side_effect = [b"%PDF-A", b"%PDF-B"]
    weekly_generate._run_pipeline(week_start_override=ANCHOR)
    assert stub["merge"].call_args.args[0] == [b"%PDF-A", b"%PDF-B"]


# ---- skip-no-change + compile-now ----------------------------------------


def test_skip_when_already_compiled_no_new_docs(stub):
    stub["rollup"].return_value = [{
        "_row_id": 5, week_sheet.COL_ROW_TYPE: week_sheet.ROW_TYPE_ROLLUP,
        week_sheet.COL_SUBMITTED_AT: "2026-06-05T09:00:00-07:00",
        week_sheet.COL_COMPILE_NOW: False,
    }]
    out = weekly_generate._run_pipeline(week_start_override=ANCHOR)
    assert out["skipped_no_change"] == 1 and out["packets_compiled"] == 0
    stub["merge"].assert_not_called()
    stub["upload"].assert_not_called()
    stub["append_rollup"].assert_not_called()  # append-only: no snapshot on an idle re-run


def test_compile_now_forces_recompile(stub):
    stub["rollup"].return_value = [{
        "_row_id": 5, week_sheet.COL_ROW_TYPE: week_sheet.ROW_TYPE_ROLLUP,
        week_sheet.COL_SUBMITTED_AT: "2026-06-05T09:00:00-07:00",
        week_sheet.COL_COMPILE_NOW: True,
    }]
    out = weekly_generate._run_pipeline(week_start_override=ANCHOR)
    assert out["packets_compiled"] == 1
    # Append-only: Compile Now APPENDS a new Rollup snapshot (never updates the prior one) and
    # clears the trigger on the prior row(s).
    stub["append_rollup"].assert_called_once()
    assert "existing_rollup_row_id" not in stub["append_rollup"].call_args.kwargs
    stub["clear_rollup"].assert_called_once()


def test_new_doc_since_compile_triggers_recompile(stub):
    stub["rollup"].return_value = [{
        "_row_id": 5, week_sheet.COL_ROW_TYPE: week_sheet.ROW_TYPE_ROLLUP,
        week_sheet.COL_SUBMITTED_AT: "2026-06-04T09:00:00-07:00",
        week_sheet.COL_COMPILE_NOW: False,
    }]
    out = weekly_generate._run_pipeline(week_start_override=ANCHOR)
    assert out["packets_compiled"] == 1


def test_recompile_files_distinct_packet_never_overwrites(stub):
    """APPEND-ONLY (operator decision 2026-06-09, naming refined 2026-06-17): a recompile files a
    NEW, DISTINCT Box packet — the clean `<Job>_week of <Sat>_WSR.pdf`, then `_v2`/`_v3`… on a
    recompile 409 — NEVER overwriting the prior packet (Box is the master record). Two compiles →
    distinct versioned filenames into the SAME job/week folder, and a NEW Rollup snapshot each
    time (never an in-place update)."""
    stub["box_root"].return_value = "ROOT9"
    stub["mkfolder"].side_effect = ["jobP", "weekP", "jobP", "weekP"]  # two compiles
    base = "Bradley 1_week of 2026-05-30_WSR.pdf"
    v2 = "Bradley 1_week of 2026-05-30_WSR_v2.pdf"
    # 1st compile: base.pdf is free. 2nd compile: base.pdf 409s (already there) → _upload_packet
    # retries the next version and files the DISTINCT _v2.pdf.
    stub["upload"].side_effect = [
        {"id": "pkt1", "name": base, "size": 9},
        generate_core.box_client.BoxConflictError("base exists"),
        {"id": "pkt2", "name": v2, "size": 9},
    ]

    weekly_generate._run_pipeline(week_start_override=ANCHOR)  # first compile
    stub["rollup"].return_value = [{  # a prior Rollup now exists; Compile Now forces recompile
        "_row_id": 5, week_sheet.COL_ROW_TYPE: week_sheet.ROW_TYPE_ROLLUP,
        week_sheet.COL_SUBMITTED_AT: "2026-06-05T09:00:00-07:00",
        week_sheet.COL_COMPILE_NOW: True,
    }]
    weekly_generate._run_pipeline(week_start_override=ANCHOR)  # recompile

    assert stub["upload"].call_count == 3  # 1 (first) + 2 (recompile: base 409 → _v2)
    names = [c.args[1] for c in stub["upload"].call_args_list]
    assert names == [base, base, v2]  # recompile retries the base name, then files the distinct _v2
    for c in stub["upload"].call_args_list:
        assert c.args[0] == "weekP", "packet must file into the job/week folder"
    assert stub["append_rollup"].call_count == 2  # a NEW Rollup snapshot per compile


# ---- empty week ----------------------------------------------------------


def test_empty_week_still_writes_rollup_and_wsr(stub):
    stub["subs"].return_value = []
    out = weekly_generate._run_pipeline(week_start_override=ANCHOR)
    assert out["empty_weeks"] == 1
    stub["merge"].assert_not_called()
    stub["append_rollup"].assert_called_once()
    stub["wsr"].assert_called_once()
    assert stub["wsr"].call_args.kwargs["compiled_pdf_link"] == ""


# ---- download failures ---------------------------------------------------


def test_partial_download_failure_still_compiles_available(stub):
    stub["subs"].return_value = [
        _sub("jha-v1", "https://app.box.com/file/11"),
        _sub("toolbox-talk-ppe-v1", "https://app.box.com/file/22"),
    ]
    stub["download"].side_effect = [b"%PDF-A", generate_core.box_client.BoxError("404")]
    out = weekly_generate._run_pipeline(week_start_override=ANCHOR)
    assert out["packets_compiled"] == 1 and out["download_errors"] == 1
    assert stub["merge"].call_args.args[0] == [b"%PDF-A"]


def test_all_downloads_fail_writes_wsr_without_packet(stub):
    stub["subs"].return_value = [_sub("jha-v1", "https://app.box.com/file/11")]
    stub["download"].side_effect = generate_core.box_client.BoxError("404")
    out = weekly_generate._run_pipeline(week_start_override=ANCHOR)
    stub["merge"].assert_not_called()
    stub["wsr"].assert_called_once()
    assert stub["wsr"].call_args.kwargs["compiled_pdf_link"] == ""
    assert out["packets_compiled"] == 0  # all-downloads-failed is NOT a real packet
    assert out["wsr_written"] == 1       # …but the WSR row IS still written


# ---- blank Submitted At must NOT silently skip (critical) -----------------


def test_blank_submitted_at_forces_recompile_not_skip(stub):
    # A prior compile exists; a submission row carries a BLANK Submitted At →
    # we cannot prove "no new docs", so we MUST recompile (never silently skip).
    stub["rollup"].return_value = [{
        "_row_id": 5, week_sheet.COL_ROW_TYPE: week_sheet.ROW_TYPE_ROLLUP,
        week_sheet.COL_SUBMITTED_AT: "2026-06-05T09:00:00-07:00",
        week_sheet.COL_COMPILE_NOW: False,
    }]
    stub["subs"].return_value = [_sub("jha-v1", "https://app.box.com/file/11", submitted="")]
    out = weekly_generate._run_pipeline(week_start_override=ANCHOR)
    assert out["packets_compiled"] == 1 and out["skipped_no_change"] == 0
    warns = [c for c in stub["log"].call_args_list
             if c.kwargs.get("error_code") == "weekly_generate.missing_submitted_at"]
    assert warns, "a blank Submitted At with submissions present must WARN, not skip silently"


# ---- missing-contact flagged on every append (append-only) -----------------


def test_missing_to_contact_flagged_on_every_append(stub):
    # Append-only (operator decision 2026-06-09): there is no "update" case any more — every
    # compilation appends a new WSR row, so a job with no safety-reports contact is surfaced
    # to the Review Queue on each compile (each unsendable row flagged, never silently lost).
    stub["list_jobs"].return_value = [_job(safety_reports_contact_email="")]
    weekly_generate._run_pipeline(week_start_override=ANCHOR)
    stub["review"].assert_called_once()  # missing TO flagged


def test_submission_with_no_box_link_excluded(stub):
    stub["subs"].return_value = [_sub("jha-v1", link="")]
    out = weekly_generate._run_pipeline(week_start_override=ANCHOR)
    assert out["download_errors"] == 1
    stub["download"].assert_not_called()


# ---- WSR missing-contact + per-job fence ---------------------------------


def test_missing_to_contact_on_create_flags_review(stub):
    stub["list_jobs"].return_value = [_job(safety_reports_contact_email="")]
    weekly_generate._run_pipeline(week_start_override=ANCHOR)
    stub["review"].assert_called_once()


def test_no_review_when_to_present(stub):
    weekly_generate._run_pipeline(week_start_override=ANCHOR)
    stub["review"].assert_not_called()


def test_per_job_fence_routes_failure_to_review_and_continues(stub):
    from shared.smartsheet_client import SmartsheetError
    stub["list_jobs"].return_value = [_job(project_name="Bradley 1", job_id="JOB-1"),
                                       _job(project_name="Huntley", job_id="JOB-2")]
    stub["ensure"].side_effect = [SmartsheetError("boom"), 8002]
    out = weekly_generate._run_pipeline(week_start_override=ANCHOR)
    assert "Bradley 1" in out["errors_per_job"]
    assert out["packets_compiled"] == 1  # JOB-2 still compiled
    stub["review"].assert_called()


def test_unresolved_box_root_surfaces_to_review(stub):
    stub["get_root"].return_value = ""
    out = weekly_generate._run_pipeline(week_start_override=ANCHOR)
    assert out["errors_per_job"]
    stub["review"].assert_called()


# ---- watchdog marker always written --------------------------------------


def test_watchdog_marker_written(stub):
    weekly_generate._run_pipeline(week_start_override=ANCHOR)
    stub["marker"].assert_called_once()


# ---- A6 hardening: resumable watermark + per-job timeout + memory guard ----


def test_wsr_row_written_before_rollup_watermark(stub):
    # A6 commit-point ordering: the WSR row must be written BEFORE the Rollup snapshot row
    # (whose compiled_at is the no-new-docs watermark), so a crash mid-compile recompiles next
    # run instead of leaving an advanced watermark with no WSR row.
    order: list[str] = []
    stub["wsr"].side_effect = lambda *a, **k: order.append("wsr") or 123
    stub["append_rollup"].side_effect = lambda *a, **k: order.append("rollup") or 99
    weekly_generate._run_pipeline(week_start_override=ANCHOR)
    assert order == ["wsr", "rollup"]  # WSR first, watermark last


def test_empty_week_writes_wsr_before_rollup_watermark(stub):
    stub["subs"].return_value = []
    order: list[str] = []
    stub["wsr"].side_effect = lambda *a, **k: order.append("wsr") or 123
    stub["append_rollup"].side_effect = lambda *a, **k: order.append("rollup") or 99
    weekly_generate._run_pipeline(week_start_override=ANCHOR)
    assert order == ["wsr", "rollup"]


def test_per_job_timeout_fenced_to_review_and_continues(stub, mocker):
    # A SIGALRM timeout (simulated by raising CompileJobTimeoutError) on one job is fenced to the
    # Review Queue + counted, and the run continues to the next job (never a silent hang).
    stub["list_jobs"].return_value = [
        _job(project_name="Bradley 1", job_id="JOB-1"),
        _job(project_name="Huntley", job_id="JOB-2"),
    ]
    mocker.patch.object(
        generate_core, "_compile_job_week",
        side_effect=[generate_core.compile_core.CompileJobTimeoutError("hung"), None],
    )
    out = weekly_generate._run_pipeline(week_start_override=ANCHOR)
    assert out["timed_out"] == 1
    assert out["jobs_processed"] == 2  # JOB-2 still attempted after JOB-1 timed out
    assert "Bradley 1" in out["errors_per_job"]
    stub["review"].assert_called()  # timeout fenced to the Review Queue
    assert any(
        c.kwargs.get("error_code") == "weekly_generate.compile_timeout"
        for c in stub["log"].call_args_list
    )


def test_memory_guard_fences_oversized_week_before_merge(stub, mocker):
    # A tiny memory ceiling makes the gathered PDF breach the budget → the job is fenced to the
    # Review Queue BEFORE merge_pdfs, never OOMing; no packet is produced.
    mocker.patch.object(
        generate_core, "_read_int_setting",
        side_effect=lambda config, key, fallback: (
            1 if key == weekly_generate.SAFETY_GENERATE_CONFIG.cfg_memory_ceiling else fallback
        ),
    )
    out = weekly_generate._run_pipeline(week_start_override=ANCHOR)
    assert out["packets_compiled"] == 0
    stub["merge"].assert_not_called()  # fenced BEFORE the merge step
    stub["review"].assert_called()     # routed to the Review Queue, not OOM
    assert "Bradley 1" in out["errors_per_job"]


# ---- P4-core: host-level compile mutex (fail-open for safety) -------------
# The UNCONTENDED happy path is covered by the entire suite above — every test runs
# _run_pipeline with the lock free and still passes, proving the mutex wrap is transparent.


def test_compile_mutex_contended_safety_compiles_anyway(stub, tmp_path, mocker):
    # When another compile holds the host mutex, the safety compile proceeds UNLOCKED
    # (fail-open — never blocked) and logs a single contention WARN.
    import fcntl

    from shared import compile_mutex

    # Anchor the mutex at a tmp sidecar so the test never touches ~/its/state.
    anchor = tmp_path / "host_compile"
    mocker.patch.object(compile_mutex, "_HOST_COMPILE_LOCK_ANCHOR", anchor)
    mutex_warn = mocker.patch.object(compile_mutex, "log")
    run_per_job = mocker.patch.object(generate_core.compile_core, "run_per_job")

    # Another compile holds the lock (a separate fd → flock contends in-process).
    sidecar = anchor.with_name(anchor.name + ".lock")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    holder = sidecar.open("a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        weekly_generate._run_pipeline(week_start_override=ANCHOR)
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

    run_per_job.assert_called_once()  # fail-open: the compile ran despite contention
    assert mutex_warn.call_count == 1
    assert mutex_warn.call_args.kwargs["error_code"] == "compile_mutex.contended"


# ---- review-row dedupe in _safe_review_queue (error-hygiene 2026-07-19) ----


def _dedupe_args():
    week = generate_core.safety_week.week_bounds(ANCHOR)
    return weekly_generate.SAFETY_GENERATE_CONFIG, _job(), week, generate_core.RunSummary()


def test_review_row_dedupe_suppresses_duplicate_pending(mocker):
    """A stuck Compile-Now retrying every ~90 s once appended a NEW PENDING row per attempt
    (189 rows for one job-week in one day) — a matching PENDING row suppresses the add, with
    a WARN (never silent). The match is per (job, week), NOT per error class: the pending
    row's SmartsheetError vs this attempt's BoxError is still a duplicate."""
    config, job, week, summary = _dedupe_args()
    pending = [{
        "Summary": f"weekly compile failed for {job.project_name} (job {job.job_id}) "
                   f"week {week.start} (SmartsheetError)",
    }]
    get_pending = mocker.patch.object(
        generate_core.review_queue, "get_pending", return_value=pending
    )
    add = mocker.patch.object(generate_core.review_queue, "add")
    log = mocker.patch.object(generate_core.error_log, "log")

    generate_core._safe_review_queue(config, job, week, "BoxError", "cid-1", summary)

    get_pending.assert_called_once_with(config.workstream)
    add.assert_not_called()
    assert summary.review_queue_entries == 0
    codes = [kw.get("error_code") for _, kw in log.call_args_list]
    assert "weekly_generate.review_queue_deduped" in codes


def test_review_row_appended_when_no_matching_pending(mocker):
    """NOT suppressed: a PENDING row for a DIFFERENT job does not match — the add proceeds."""
    config, job, week, summary = _dedupe_args()
    pending = [{
        "Summary": f"weekly compile failed for Other Job (job JOB-9) week {week.start} (X)",
    }]
    mocker.patch.object(generate_core.review_queue, "get_pending", return_value=pending)
    add = mocker.patch.object(generate_core.review_queue, "add")
    mocker.patch.object(generate_core.error_log, "log")

    generate_core._safe_review_queue(config, job, week, "BoxError", "cid-1", summary)

    add.assert_called_once()
    assert summary.review_queue_entries == 1


def test_review_row_dedupe_read_failure_appends_anyway(mocker):
    """FAIL-OPEN: a dedupe-read error must never lose the first signal — append anyway."""
    from shared.smartsheet_client import SmartsheetError

    config, job, week, summary = _dedupe_args()
    mocker.patch.object(
        generate_core.review_queue, "get_pending",
        side_effect=SmartsheetError("read timeout"),
    )
    add = mocker.patch.object(generate_core.review_queue, "add")
    log = mocker.patch.object(generate_core.error_log, "log")

    generate_core._safe_review_queue(config, job, week, "BoxError", "cid-1", summary)

    add.assert_called_once()
    assert summary.review_queue_entries == 1
    codes = [kw.get("error_code") for _, kw in log.call_args_list]
    assert "weekly_generate.review_queue_dedupe_read_failed" in codes


# ---- config-read transient fence (DASH-14 port of PR #613) -----------------


def test_read_str_setting_transient_error_falls_open_with_warn(mocker):
    """A generic SmartsheetError from get_setting (read-timeout / 5xx) must NOT escape
    to @its_error_log as a spurious CRITICAL — WARN `config_read_error` + fallback,
    same disposition as the circuit-open branch."""
    from shared.smartsheet_client import SmartsheetError
    mocker.patch.object(
        generate_core.smartsheet_client, "get_setting",
        side_effect=SmartsheetError("read timeout"),
    )
    log = mocker.patch.object(generate_core.error_log, "log")

    result = generate_core._read_str_setting(
        weekly_generate.SAFETY_GENERATE_CONFIG, "safety_reports.some_key", "fallback-val"
    )  # must not raise

    assert result == "fallback-val"
    codes = [kw.get("error_code") for _, kw in log.call_args_list]
    assert "config_read_error" in codes
