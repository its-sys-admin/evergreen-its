"""Unit tests for field_ops.fieldops_sync — the dual-sheet job up-sync daemon.

Fully mocked (no live Smartsheet / Worker): the data-plane seams (portal_client pending +
mark-mirrored, active_jobs_writer.upsert_job, review_queue.add) and the heartbeat / watchdog
seams are patched. Covers: sync_enabled gate OFF→noop, the dirty-job happy path (dual
find-or-create + per-sheet mark-mirrored, OK heartbeat + marker), the per-job permanent fence
(→ Review Queue, WARN), the partial-failure self-heal (safety committed + progress transient
→ only safety mark-mirrored, job left dirty, DEGRADED), the fail-closed no-creds halt, and a
malformed (no job_id) row skip.
"""
from __future__ import annotations

from typing import Any

import pytest

from field_ops import fieldops_sync
from shared import active_jobs_writer, sheet_ids, smartsheet_client
from shared.portal_client import FieldopsEquipmentSnapshot, FieldopsMaterialListSnapshot


def _job(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "job_id": "acme-solar-01",
        "project_name": "Acme Solar 01",
        "lifecycle": "active",
        "address": "1 Main St",
        "stakeholder_name": "Sam",
        "stakeholder_email": "sam@acme.example",
        "stakeholder_phone": "5551234",
        "safety_contact_name": "Pat",
        "safety_contact_email": "pat@acme.example",
        "safety_cc": ["a@x.com"],
        "progress_contact_name": "Riley",
        "progress_contact_email": "riley@acme.example",
        "progress_cc": ["c@x.com"],
        "mirror_version": 3,
    }
    base.update(over)
    return base


def _upsert_ok(config, job):
    """upsert_job side-effect: distinct (row_id, canonical) per sheet config."""
    if config is active_jobs_writer.SAFETY_WRITE_CONFIG:
        return (111, "JOB-0007")
    return (222, "JOB-PRG-0007")


@pytest.fixture
def _patch(mocker):
    return {
        "pending": mocker.patch(
            "field_ops.fieldops_sync.portal_client.get_fieldops_pending_jobs",
            return_value=[],
        ),
        "mark": mocker.patch(
            "field_ops.fieldops_sync.portal_client.mark_fieldops_jobs_mirrored",
            return_value={"ok": True, "updated": 1},
        ),
        "upsert": mocker.patch("field_ops.fieldops_sync.active_jobs_writer.upsert_job"),
        "review": mocker.patch("field_ops.fieldops_sync.review_queue.add", return_value=1),
        "creds": mocker.patch(
            "field_ops.fieldops_sync._resolve_credentials",
            return_value=("https://safety.example", "tok"),
        ),
        "hb": mocker.patch("field_ops.fieldops_sync._write_heartbeat", return_value=None),
        "hb_row": mocker.patch("field_ops.fieldops_sync._write_heartbeat_row", return_value=None),
        "marker": mocker.patch("field_ops.fieldops_sync._write_watchdog_marker", return_value=None),
        "log": mocker.patch("field_ops.fieldops_sync.error_log.log", return_value=None),
        "circuit": mocker.patch("field_ops.fieldops_sync.circuit_breaker.is_open", return_value=False),
        # Pending-fetch-outage counter seams — mocked so no test touches the live ~/its/state dir
        # and the sustained-outage escalation can be driven. Default 1 (single blip → ERROR).
        "record_fetch_fail": mocker.patch(
            "field_ops.fieldops_sync._record_pending_fetch_failure", return_value=1
        ),
        "reset_fetch_fail": mocker.patch(
            "field_ops.fieldops_sync._reset_pending_fetch_failures", return_value=None
        ),
        # P7 hours pass seams — hours_enabled defaults OFF so EVERY existing job-mirror test is
        # byte-identical (the pass is skipped); the hours tests below flip it on. All hours I/O is
        # mocked so no test touches live Smartsheet / the Worker.
        "hours_enabled": mocker.patch(
            "field_ops.fieldops_sync._hours_enabled", return_value=False
        ),
        "hours_pending": mocker.patch(
            "field_ops.fieldops_sync.portal_client.get_fieldops_pending_hours", return_value=[]
        ),
        "hours_mark": mocker.patch(
            "field_ops.fieldops_sync.portal_client.mark_fieldops_hours_mirrored",
            return_value={"ok": True, "updated": 1},
        ),
        "ensure_sheet": mocker.patch(
            "field_ops.fieldops_sync.hours_log.ensure_hours_log_sheet", return_value=999
        ),
        "upsert_entry": mocker.patch(
            "field_ops.fieldops_sync.hours_log.upsert_entry_row", return_value=1
        ),
        "supersede_entry": mocker.patch(
            "field_ops.fieldops_sync.hours_log.supersede_entry_row", return_value=True
        ),
        "row_cap": mocker.patch(
            "field_ops.fieldops_sync.hours_log.check_row_cap", return_value=None
        ),
        # P7 equipment pass seams — equipment_enabled defaults OFF so EVERY existing job/hours
        # test is byte-identical (the pass is skipped); the equipment tests below flip it on. All
        # equipment I/O is mocked so no test touches live Smartsheet / the Worker. SNAPSHOT — there
        # is no mark-mirrored seam (unlike hours).
        "equipment_enabled": mocker.patch(
            "field_ops.fieldops_sync._equipment_enabled", return_value=False
        ),
        "equipment_snapshot": mocker.patch(
            "field_ops.fieldops_sync.portal_client.get_fieldops_equipment_snapshot",
            return_value=FieldopsEquipmentSnapshot(equipment=[], jobs_with_equipment=[]),
        ),
        "ensure_equip_sheet": mocker.patch(
            "field_ops.fieldops_sync.equipment_status.ensure_equipment_sheet", return_value=777
        ),
        "find_equip_sheet": mocker.patch(
            "field_ops.fieldops_sync.equipment_status.find_equipment_sheet", return_value=777
        ),
        "upsert_equip": mocker.patch(
            "field_ops.fieldops_sync.equipment_status.upsert_equipment_row", return_value=1
        ),
        "retire_equip": mocker.patch(
            "field_ops.fieldops_sync.equipment_status.retire_off_job", return_value=0
        ),
        "equip_row_cap": mocker.patch(
            "field_ops.fieldops_sync.equipment_status.check_row_cap", return_value=None
        ),
        # P7 material-list pass seams — materials_enabled defaults OFF so EVERY existing
        # job/hours/equipment test is byte-identical (the pass is skipped); the material tests below
        # flip it on. All material I/O is mocked so no test touches live Smartsheet / the Worker.
        # SNAPSHOT — there is no mark-mirrored seam (like equipment).
        "materials_enabled": mocker.patch(
            "field_ops.fieldops_sync._materials_enabled", return_value=False
        ),
        "material_snapshot": mocker.patch(
            "field_ops.fieldops_sync.portal_client.get_fieldops_material_list_snapshot",
            return_value=FieldopsMaterialListSnapshot(lines=[], jobs_with_materials=[]),
        ),
        "ensure_mat_sheet": mocker.patch(
            "field_ops.fieldops_sync.material_list.ensure_material_list_sheet", return_value=555
        ),
        "find_mat_sheet": mocker.patch(
            "field_ops.fieldops_sync.material_list.find_material_list_sheet", return_value=555
        ),
        "upsert_mat": mocker.patch(
            "field_ops.fieldops_sync.material_list.upsert_line_row", return_value=1
        ),
        "retire_mat": mocker.patch(
            "field_ops.fieldops_sync.material_list.retire_removed", return_value=0
        ),
        "mat_row_cap": mocker.patch(
            "field_ops.fieldops_sync.material_list.check_row_cap", return_value=None
        ),
        # P7 material-incidents pass seams — incidents_enabled defaults OFF so EVERY existing
        # job/hours/equipment/material test is byte-identical (the pass is skipped); the incident
        # tests below flip it on. All incident I/O is mocked so no test touches live Smartsheet / the
        # Worker. APPEND-ONLY LEDGER — no snapshot roster, no retire, no mark-mirrored seam.
        "incidents_enabled": mocker.patch(
            "field_ops.fieldops_sync._incidents_enabled", return_value=False
        ),
        "incidents_list": mocker.patch(
            "field_ops.fieldops_sync.portal_client.get_fieldops_material_incidents", return_value=[]
        ),
        "ensure_inc_sheet": mocker.patch(
            "field_ops.fieldops_sync.material_incidents.ensure_material_incidents_sheet",
            return_value=666,
        ),
        "upsert_inc": mocker.patch(
            "field_ops.fieldops_sync.material_incidents.upsert_incident_row", return_value=1
        ),
        "inc_row_cap": mocker.patch(
            "field_ops.fieldops_sync.material_incidents.check_row_cap", return_value=None
        ),
        # §51 archive-on-closure seams — default to "an Hours Log exists" so the archived-job
        # tests exercise the move; the guard/idempotency tests below flip these to None.
        "arc_folder": mocker.patch(
            "field_ops.fieldops_sync.smartsheet_client.find_folder_by_name_in_workspace",
            return_value=4242,
        ),
        "arc_sheet": mocker.patch(
            "field_ops.fieldops_sync.smartsheet_client.find_sheet_by_name_in_folder",
            return_value=888,
        ),
        "arc_move": mocker.patch(
            "field_ops.fieldops_sync.smartsheet_client.move_sheet_to_folder",
            return_value=None,
        ),
    }


# ---- sync_enabled gate ----------------------------------------------------


def test_sync_disabled_is_noop(mocker, _patch):
    mocker.patch("field_ops.fieldops_sync._sync_enabled", return_value=False)
    assert fieldops_sync.sync_once() == 0
    _patch["pending"].assert_not_called()
    _patch["upsert"].assert_not_called()
    _patch["hb"].assert_not_called()


# ---- dirty-job happy path -------------------------------------------------


def test_dirty_job_mirrors_both_sheets_per_sheet_commit(_patch):
    _patch["pending"].return_value = [_job()]
    _patch["upsert"].side_effect = _upsert_ok

    stats = fieldops_sync._sync_inside_lock()

    assert stats.mirrored == 1
    assert stats.errors == 0 and stats.reviewed == 0
    # find-or-create on BOTH sheets
    assert _patch["upsert"].call_count == 2
    # per-sheet mark-mirrored: safety first, then progress (the version-vector commit order)
    assert _patch["mark"].call_count == 2
    safety_updates = _patch["mark"].call_args_list[0].args[2]
    assert safety_updates[0]["sheet"] == "safety"
    assert safety_updates[0]["row_id"] == 111
    assert safety_updates[0]["mirrored_version"] == 3
    assert safety_updates[0]["canonical_job_id"] == "JOB-0007"
    progress_updates = _patch["mark"].call_args_list[1].args[2]
    assert progress_updates[0]["sheet"] == "progress"
    assert progress_updates[0]["row_id"] == 222
    assert "canonical_job_id" not in progress_updates[0]  # canonical is safety-only
    # heartbeat OK + watchdog marker
    _patch["hb"].assert_called_once()
    assert _patch["hb_row"].call_args.kwargs["status"] == "OK"
    _patch["marker"].assert_called_once()


def test_malformed_mirror_version_warns_and_coerces_to_zero(_patch):
    # Never-silent: a missing/malformed mirror_version (a Worker payload defect) coerces to 0
    # — which would leave the job permanently dirty — so it must WARN, not silently coerce.
    _patch["pending"].return_value = [_job(mirror_version="not-an-int")]
    _patch["upsert"].side_effect = _upsert_ok
    fieldops_sync._sync_inside_lock()
    assert any(
        c.kwargs.get("error_code") == "fieldops_mirror_version_malformed"
        for c in _patch["log"].call_args_list
    )
    # the coerced 0 is what's sent to the Worker watermark (vector stays consistent).
    assert _patch["mark"].call_args_list[0].args[2][0]["mirrored_version"] == 0


# ---- per-job permanent fence → Review Queue -------------------------------


def test_permanent_failure_routes_to_review_queue(_patch):
    _patch["pending"].return_value = [_job()]
    _patch["upsert"].side_effect = smartsheet_client.SmartsheetValidationError("HTTP 400 reject")

    stats = fieldops_sync._sync_inside_lock()

    assert stats.reviewed == 1
    assert stats.mirrored == 0
    _patch["review"].assert_called_once()
    assert _patch["review"].call_args.kwargs["workstream"] == "progress_reports"
    # permanent failure on the safety upsert → nothing was ever mark-mirrored
    _patch["mark"].assert_not_called()
    # the ticket records the partial state: failed on the safety sheet, nothing mirrored yet.
    payload = _patch["review"].call_args.kwargs["payload"]
    assert payload["failed_sheet"] == "safety"
    assert payload["safety_mirrored"] is False
    assert _patch["hb_row"].call_args.kwargs["status"] == "WARN"
    _patch["marker"].assert_called_once()


def test_permanent_failure_on_progress_records_safety_already_mirrored(_patch):
    # Safety mirrors fine; the PROGRESS upsert permanently fails → the Review ticket must record
    # that safety is already live and ONLY progress failed (the operator's remediation differs).
    _patch["pending"].return_value = [_job()]

    def _upsert(config, job):
        if config is active_jobs_writer.SAFETY_WRITE_CONFIG:
            return (111, "JOB-0007")
        raise smartsheet_client.SmartsheetValidationError("HTTP 400 progress reject")

    _patch["upsert"].side_effect = _upsert

    stats = fieldops_sync._sync_inside_lock()

    assert stats.reviewed == 1 and stats.mirrored == 0
    # safety WAS committed (mark-mirrored once) before the progress upsert failed
    assert _patch["mark"].call_count == 1
    assert _patch["mark"].call_args_list[0].args[2][0]["sheet"] == "safety"
    payload = _patch["review"].call_args.kwargs["payload"]
    assert payload["failed_sheet"] == "progress"
    assert payload["safety_mirrored"] is True


def test_mark_mirrored_401_pages_critical_not_transient(_patch):
    # A 401 on the mark-mirrored write-back (bad/rotated field-ops bearer) is NOT transient — it
    # must page CRITICAL, not fall into the self-healing PortalTransportError bucket (its parent).
    _patch["pending"].return_value = [_job()]
    _patch["upsert"].side_effect = _upsert_ok
    _patch["mark"].side_effect = fieldops_sync.portal_client.PortalAuthError("401")

    stats = fieldops_sync._sync_inside_lock()

    assert stats.errors == 1
    assert stats.mirrored == 0 and stats.reviewed == 0
    _patch["upsert"].assert_called()  # the sheet write ran before the mark-mirrored 401
    assert any(
        c.args and c.args[0] == fieldops_sync.Severity.CRITICAL
        and c.kwargs.get("error_code") == "fieldops_mark_mirrored_unauthorized"
        for c in _patch["log"].call_args_list
    )
    # and it is NOT mis-classified as the self-healing transient error
    assert not any(
        c.kwargs.get("error_code") == "fieldops_job_transient"
        for c in _patch["log"].call_args_list
    )


# ---- partial-failure self-heal -------------------------------------------


def test_partial_failure_advances_only_safety_and_leaves_dirty(_patch):
    _patch["pending"].return_value = [_job()]

    def _upsert(config, job):
        if config is active_jobs_writer.SAFETY_WRITE_CONFIG:
            return (111, "JOB-0007")
        raise smartsheet_client.SmartsheetError("transient progress write")

    _patch["upsert"].side_effect = _upsert

    stats = fieldops_sync._sync_inside_lock()

    assert stats.errors == 1
    assert stats.mirrored == 0
    assert stats.reviewed == 0
    # ONLY the safety watermark was committed; progress was never mark-mirrored (job dirty).
    assert _patch["mark"].call_count == 1
    only = _patch["mark"].call_args_list[0].args[2]
    assert only[0]["sheet"] == "safety"
    _patch["review"].assert_not_called()
    assert _patch["hb_row"].call_args.kwargs["status"] == "DEGRADED"
    _patch["marker"].assert_called_once()


# ---- fail-closed credentials ----------------------------------------------


def test_fail_closed_when_credentials_missing(_patch):
    _patch["creds"].return_value = None

    stats = fieldops_sync._sync_inside_lock()

    assert stats.halted_no_creds is True
    _patch["pending"].assert_not_called()
    _patch["hb"].assert_called_once()
    assert _patch["hb_row"].call_args.kwargs["status"] == "ERROR"
    # No watchdog marker on a no-creds halt — let Check C go stale (mirror portal_poll).
    _patch["marker"].assert_not_called()


def test_transient_base_url_warns_and_skips_without_paging(_patch):
    # A Smartsheet blip on the base-URL read is NOT a missing credential. Live on
    # 2026-07-20 04:42Z a single GET failure fell back to "" and fired a CRITICAL
    # saying portal credentials were unset; the Keychain entry was fine and the
    # daemon self-healed on the next cycle. That page was false, and it aimed the
    # §43 repair at re-provisioning secrets (a high-capability-class action) at a
    # condition needing none. Transient => WARN + skip, never the misconfig CRITICAL.
    _patch["creds"].return_value = fieldops_sync.TransientUnavailable(
        reason="SmartsheetError: (<PreparedRequest [GET]>, None)"
    )

    stats = fieldops_sync._sync_inside_lock()

    assert stats.halted_transient is True
    assert stats.halted_no_creds is False
    _patch["pending"].assert_not_called()  # still FAIL-CLOSED — it does not sync
    codes = [c.kwargs.get("error_code") for c in _patch["log"].call_args_list]
    assert "fieldops_creds_transient" in codes
    assert "fieldops_creds_missing" not in codes, "a transient read failure must NOT page"
    assert _patch["hb_row"].call_args.kwargs["status"] == "WARN"  # not ERROR — nothing misconfigured
    # No watchdog marker, exactly like the no-creds halt — a SUSTAINED outage must still
    # surface via the Check-C staleness floor.
    _patch["marker"].assert_not_called()


def test_pending_auth_error_pages_and_writes_error_heartbeat(_patch):
    _patch["pending"].side_effect = fieldops_sync.portal_client.PortalAuthError("401")

    stats = fieldops_sync._sync_inside_lock()

    assert stats.errors == 1
    _patch["upsert"].assert_not_called()
    assert _patch["hb_row"].call_args.kwargs["status"] == "ERROR"
    _patch["marker"].assert_not_called()


def test_pending_transport_error_does_not_starve_the_hours_pass(_patch):
    # The live "logged time never reaches the Hours Log" bug: a TRANSIENT pending-JOBS transport
    # failure must NOT skip the hours pass — it hits an INDEPENDENT endpoint (/hours-pending) that
    # may well be reachable this cycle. The job error is recorded (never silent); the hours pass
    # still runs. (Reverting the fix — restoring the early-return on the transport branch — makes
    # this red: hours_pending would never be called.)
    _patch["pending"].side_effect = fieldops_sync.portal_client.PortalTransportError(
        "jobs endpoint blipped"
    )
    _patch["hours_enabled"].return_value = True
    _patch["hours_pending"].return_value = [
        {
            "uuid": "u1", "job_id": "JOB-1", "project_name": "Proj", "personnel_name": "Sam",
            "hours": 8, "task": "Framing", "notes": "",
            "amends_uuid": None, "created_at": 1_700_000_000,
        },
    ]

    stats = fieldops_sync._sync_inside_lock()

    # The hours pass RAN despite the job-fetch failure (the fix):
    _patch["hours_pending"].assert_called_once()
    _patch["ensure_sheet"].assert_called_once()
    _patch["hours_mark"].assert_called_once()
    # ...and the job-fetch failure is still observable (never silent): DEGRADED, no job mirror,
    # but the daemon IS alive (it did useful hours work) so the marker is written.
    assert stats.errors >= 1
    _patch["upsert"].assert_not_called()
    assert _patch["hb_row"].call_args.kwargs["status"] == "DEGRADED"
    _patch["marker"].assert_called_once()


def test_pending_transport_sustained_escalates_to_critical(_patch):
    # Because the decoupled cycle no longer goes Check-C-stale on a job-fetch outage, a SUSTAINED
    # outage (counter >= threshold) escalates the per-cycle ERROR to CRITICAL (the triple-fire push).
    _patch["pending"].side_effect = fieldops_sync.portal_client.PortalTransportError("down")
    _patch["record_fetch_fail"].return_value = fieldops_sync.PENDING_FETCH_FAIL_CRITICAL_THRESHOLD
    fieldops_sync._sync_inside_lock()
    crit = [
        c for c in _patch["log"].call_args_list
        if c.args[0] == fieldops_sync.Severity.CRITICAL
        and c.kwargs.get("error_code") == "fieldops_pending_fetch_sustained"
    ]
    assert crit, "a sustained pending-jobs outage must escalate to CRITICAL"


def test_pending_transport_transient_stays_error_not_critical(_patch):
    # A single transient blip (counter below threshold) self-heals → ERROR, never CRITICAL.
    _patch["pending"].side_effect = fieldops_sync.portal_client.PortalTransportError("blip")
    _patch["record_fetch_fail"].return_value = 1
    fieldops_sync._sync_inside_lock()
    crit = [c for c in _patch["log"].call_args_list if c.args[0] == fieldops_sync.Severity.CRITICAL]
    assert not crit, "a single transient blip must NOT page CRITICAL"


# ---- malformed row --------------------------------------------------------


def test_row_missing_job_id_is_skipped(_patch):
    _patch["pending"].return_value = [{"project_name": "no id"}]

    stats = fieldops_sync._sync_inside_lock()

    assert stats.errors == 1
    _patch["upsert"].assert_not_called()
    _patch["mark"].assert_not_called()


# ---- shared base-URL key is read under its owning workstream --------------


def test_worker_base_url_read_under_safety_reports_workstream(mocker):
    # The Worker base-URL key is SHARED with portal_poll and OWNED by safety_reports; reading
    # it under field_ops would force a duplicate ITS_Config row that can silently diverge from
    # the canonical safety_reports row. This proves the control bites: the base-URL key is
    # requested under workstream="safety_reports", while a field_ops-scoped key (the
    # sync_enabled gate) is still read under workstream="field_ops".
    def _get_setting(key, *, workstream):
        if key == fieldops_sync.CFG_WORKER_BASE_URL:
            return "https://safety.example"
        if key == fieldops_sync.CFG_SYNC_ENABLED:
            return "true"
        return None

    get_setting = mocker.patch(
        "field_ops.fieldops_sync.smartsheet_client.get_setting", side_effect=_get_setting
    )
    mocker.patch(
        "field_ops.fieldops_sync.keychain.get_secret", return_value="bearer-tok"
    )

    creds = fieldops_sync._resolve_credentials()
    assert creds == ("https://safety.example", "bearer-tok")

    base_url_calls = [
        c for c in get_setting.call_args_list if c.args[0] == fieldops_sync.CFG_WORKER_BASE_URL
    ]
    assert base_url_calls, "expected the Worker base-URL key to be read"
    assert all(c.kwargs["workstream"] == "safety_reports" for c in base_url_calls)

    # A field_ops-owned key stays under field_ops (byte-identical default behavior).
    assert fieldops_sync._sync_enabled() is True
    sync_calls = [
        c for c in get_setting.call_args_list if c.args[0] == fieldops_sync.CFG_SYNC_ENABLED
    ]
    assert sync_calls, "expected the sync_enabled gate to be read"
    assert all(c.kwargs["workstream"] == "field_ops" for c in sync_calls)


# ---- P7 hours pass (Track 2, Slice 1) ------------------------------------


def _hours_entry(uuid: str = "T1", job_id: str = "JOB-1", **over: Any) -> dict[str, Any]:
    # Mirrors the /hours-pending row shape after the 2026-07-05 Task-column change: `task`
    # (task_assignments.description) replaced the always-empty work_started_at/_ended_at fields.
    e: dict[str, Any] = {
        "uuid": uuid,
        "job_id": job_id,
        "project_name": "Job One",
        "hours": 8,
        "task": "Pour footings",
        "notes": "poured footings",
        "amends_uuid": None,
        "created_at": 1751030000,
        "personnel_name": "Alice Crew",
    }
    e.update(over)
    return e


def test_hours_pass_off_by_default(_patch):
    # hours_enabled defaults False (fixture) → the pass never touches the Worker or the sheets.
    _patch["hours_pending"].return_value = [_hours_entry()]
    stats = fieldops_sync._sync_inside_lock()
    _patch["hours_pending"].assert_not_called()
    _patch["ensure_sheet"].assert_not_called()
    assert stats.hours_mirrored == 0 and stats.hours_errors == 0 and stats.hours_reviewed == 0


def test_hours_pass_mirrors_and_marks(_patch):
    _patch["hours_enabled"].return_value = True
    _patch["hours_pending"].return_value = [_hours_entry("T1"), _hours_entry("T2")]
    stats = fieldops_sync._sync_inside_lock()
    assert stats.hours_mirrored == 2 and stats.hours_errors == 0 and stats.hours_reviewed == 0
    _patch["ensure_sheet"].assert_called_once_with("Job One")  # one sheet for the one job
    assert _patch["upsert_entry"].call_count == 2               # one upsert per entry
    # /hours-pending `task` maps to the Task column; the retired started/ended kwargs are gone and
    # work_date now derives from created_at (2026-07-05 Task-column change). Reverting the mapping
    # (restoring started=/ended=) makes this red.
    upsert_kwargs = _patch["upsert_entry"].call_args.kwargs
    assert upsert_kwargs["task"] == "Pour footings"
    assert "started" not in upsert_kwargs and "ended" not in upsert_kwargs
    assert upsert_kwargs["work_date"]  # non-empty, derived from created_at
    # commit point LAST — one mark-mirrored batch of the succeeded uuids
    _patch["hours_mark"].assert_called_once()
    assert _patch["hours_mark"].call_args.args[2] == ["T1", "T2"]
    assert _patch["hb_row"].call_args.kwargs["status"] == "OK"
    assert _patch["hb_row"].call_args.kwargs["items_processed"] == 2
    _patch["row_cap"].assert_called_once()  # §51 row-cap watchdog runs once per job-sheet


def test_hours_work_date_from_created_at_not_work_started(_patch):
    # work_date must derive from created_at (server record time), NOT any stale/rogue work_started_at
    # left on the payload. Reverting fieldops_sync to `_fmt_epoch_date(work_started_at) or created_at`
    # makes this RED (it would pick the work_started_at date).
    _patch["hours_enabled"].return_value = True
    _patch["hours_pending"].return_value = [
        _hours_entry("T1", work_started_at=1_600_000_000, created_at=1_751_030_000),
    ]
    fieldops_sync._sync_inside_lock()
    kw = _patch["upsert_entry"].call_args.kwargs
    assert kw["work_date"] == fieldops_sync._fmt_epoch_date(1_751_030_000)
    assert kw["work_date"] != fieldops_sync._fmt_epoch_date(1_600_000_000)


def test_hours_null_task_maps_to_empty_string(_patch):
    # a time entry that references no task → /hours-pending returns task=null → upsert gets "".
    _patch["hours_enabled"].return_value = True
    _patch["hours_pending"].return_value = [_hours_entry("T1", task=None)]
    fieldops_sync._sync_inside_lock()
    assert _patch["upsert_entry"].call_args.kwargs["task"] == ""


def test_hours_amend_supersedes_prior(_patch):
    _patch["hours_enabled"].return_value = True
    _patch["hours_pending"].return_value = [_hours_entry("T2", amends_uuid="T1")]
    fieldops_sync._sync_inside_lock()
    _patch["supersede_entry"].assert_called_once_with(999, "T1", "T2")


def test_hours_amend_prior_missing_warns_but_still_marks(_patch):
    _patch["hours_enabled"].return_value = True
    _patch["hours_pending"].return_value = [_hours_entry("T2", amends_uuid="T1")]
    _patch["supersede_entry"].return_value = False  # prior not on the sheet yet (out-of-order)
    stats = fieldops_sync._sync_inside_lock()
    assert stats.hours_mirrored == 1  # the amend's own row still filed + marked
    assert any(
        c.kwargs.get("error_code") == "fieldops_hours_amend_prior_missing"
        for c in _patch["log"].call_args_list
    )


def test_hours_permanent_failure_routes_review_and_not_marked(_patch):
    _patch["hours_enabled"].return_value = True
    _patch["hours_pending"].return_value = [_hours_entry("T1")]
    _patch["upsert_entry"].side_effect = smartsheet_client.SmartsheetValidationError("HTTP 400")
    stats = fieldops_sync._sync_inside_lock()
    assert stats.hours_reviewed == 1 and stats.hours_mirrored == 0
    _patch["review"].assert_called_once()
    assert _patch["review"].call_args.kwargs["workstream"] == "progress_reports"
    _patch["hours_mark"].assert_not_called()  # nothing succeeded → no mark-mirrored
    assert _patch["hb_row"].call_args.kwargs["status"] == "WARN"


def test_hours_per_entry_fence_one_bad_entry_does_not_block_others(_patch):
    _patch["hours_enabled"].return_value = True
    _patch["hours_pending"].return_value = [_hours_entry("T1"), _hours_entry("T2")]
    _patch["upsert_entry"].side_effect = [smartsheet_client.SmartsheetError("boom"), 5]
    stats = fieldops_sync._sync_inside_lock()
    assert stats.hours_errors == 1 and stats.hours_mirrored == 1  # T2 still mirrored
    assert _patch["hours_mark"].call_args.args[2] == ["T2"]        # only the succeeded uuid marked


def test_hours_pending_fetch_failure_leaves_unmarked_but_cycle_completes(_patch):
    _patch["hours_enabled"].return_value = True
    _patch["hours_pending"].side_effect = fieldops_sync.portal_client.PortalTransportError("down")
    stats = fieldops_sync._sync_inside_lock()
    assert stats.hours_errors == 1 and stats.hours_mirrored == 0
    _patch["hours_mark"].assert_not_called()
    # the hours failure NEVER aborts the cycle — heartbeat + marker still run.
    _patch["hb"].assert_called_once()
    _patch["marker"].assert_called_once()


def test_hours_malformed_entry_is_skipped_never_silent(_patch):
    _patch["hours_enabled"].return_value = True
    _patch["hours_pending"].return_value = [_hours_entry("T1", project_name="")]  # unfoldabe
    stats = fieldops_sync._sync_inside_lock()
    assert stats.hours_mirrored == 0
    _patch["ensure_sheet"].assert_not_called()
    assert any(
        c.kwargs.get("error_code") == "fieldops_hours_row_malformed"
        for c in _patch["log"].call_args_list
    )


# ---- §51 archive-on-closure — DISARMED on the mirror path (Track 6 PR-0) -----------


@pytest.mark.parametrize("lifecycle", ["active", "inactive", "archived"])
def test_job_mirror_never_archives(_patch, lifecycle):
    """THE STANDING FENCE: mirroring a job must never relocate anything, for ANY lifecycle.

    Until Track 6 PR-0 this file asserted the opposite — that mirroring a `lifecycle='archived'`
    job moved four tracker sheets into the flat Closed-Projects folder. That coupling was the
    defect, not the feature: the move fired from inside the job-dirty mirror fence, the job was
    mark-synced immediately after, so a failure was permanent with no retry — and the only
    trigger was an unconfirmed portal dropdown.

    This test is deliberately parametrized over ALL THREE lifecycles rather than just 'archived',
    so re-introducing the trigger under any guard (or none) red-lights here.
    """
    _patch["pending"].return_value = [_job(lifecycle=lifecycle)]
    _patch["upsert"].side_effect = _upsert_ok

    stats = fieldops_sync._sync_inside_lock()

    assert stats.mirrored == 1 and stats.errors == 0 and stats.reviewed == 0
    _patch["arc_folder"].assert_not_called()
    _patch["arc_sheet"].assert_not_called()
    _patch["arc_move"].assert_not_called()
    assert not any(
        c.kwargs.get("error_code") == "fieldops_archive_on_closure_failed"
        for c in _patch["log"].call_args_list
    )


# The helper itself is PRESERVED (§14) until the replacement path is live-proven. It has no
# caller, so these exercise it DIRECTLY — keeping the preserved code covered, and keeping the
# move semantics (all five trackers, pure relocation, fenced failure) pinned for the port.


def test_preserved_helper_moves_every_tracker(_patch):
    fieldops_sync._archive_closed_job_trackers("JOB-1", "Bradley 1", "corr-1")

    _patch["arc_folder"].assert_called_once()  # source folder resolved ONCE, find-no-create
    # Counted, not sampled: this assertion is the ONLY thing that notices a new per-job
    # tracker being added without enrolling it in the archive tuple — which would leave that
    # sheet behind in the live folder when the job closes, where every find-or-create path
    # would then re-grow around it.
    assert _patch["arc_sheet"].call_count == 5  # Hours + Equipment + Material List + Incidents + Receipts
    assert _patch["arc_move"].call_count == 5
    for call in _patch["arc_move"].call_args_list:
        assert call.args[0] == 888
        assert call.args[1] == sheet_ids.FOLDER_ARCHIVE_CLOSED_PROJECTS


def test_preserved_helper_no_folder_is_noop(_patch):
    _patch["arc_folder"].return_value = None

    fieldops_sync._archive_closed_job_trackers("JOB-1", "Bradley 1", "corr-1")

    _patch["arc_sheet"].assert_not_called()
    _patch["arc_move"].assert_not_called()


def test_preserved_helper_no_trackers_is_noop(_patch):
    # The old "natural idempotency": once moved out of the source folder they aren't found again.
    _patch["arc_sheet"].return_value = None

    fieldops_sync._archive_closed_job_trackers("JOB-1", "Bradley 1", "corr-1")

    _patch["arc_move"].assert_not_called()


def test_preserved_helper_move_failure_warns_and_never_raises(_patch):
    _patch["arc_move"].side_effect = smartsheet_client.SmartsheetError("move boom")

    fieldops_sync._archive_closed_job_trackers("JOB-1", "Bradley 1", "corr-1")  # must not raise

    warn = [
        c for c in _patch["log"].call_args_list
        if c.kwargs.get("error_code") == "fieldops_archive_on_closure_failed"
    ]
    assert warn, "expected a WARN with error_code=fieldops_archive_on_closure_failed"
    assert warn[0].args[0] == fieldops_sync.Severity.WARN


# ---- P7 equipment pass (Track 2, Slice 2) --------------------------------


def _equip_row(
    equipment_id: int = 10, job_id: str = "JOB-1", **over: Any
) -> dict[str, Any]:
    e: dict[str, Any] = {
        "equipment_id": equipment_id,
        "job_id": job_id,
        "project_name": "Job One",
        "name": "Unit Alpha",
        "kind": "skid-steer",
        "identifier": "SK-001",
        "status": "fmc",
        "status_note": "",
        "status_changed_at": 1751000000,
        "location_label": "North lot",
        "lat": 37.7749,
        "lon": -122.4194,
        "read_at": 1751020000,
        "recorded_at": 1751030000,
    }
    e.update(over)
    return e


def _snap(
    equipment: list[dict[str, Any]], roster: list[dict[str, Any]] | None = None
) -> FieldopsEquipmentSnapshot:
    """Build a FieldopsEquipmentSnapshot. If `roster` is None, derive one `{job_id, project_name}`
    entry per distinct job in the equipment rows (the Worker's real invariant: every job with
    current equipment appears in `jobs_with_equipment`)."""
    if roster is None:
        seen: dict[str, dict[str, Any]] = {}
        for e in equipment:
            jid = str(e.get("job_id") or "")
            if jid and jid not in seen:
                seen[jid] = {"job_id": jid, "project_name": str(e.get("project_name") or "")}
        roster = list(seen.values())
    return FieldopsEquipmentSnapshot(equipment=equipment, jobs_with_equipment=roster)


def test_equipment_pass_off_by_default(_patch):
    # equipment_enabled defaults False (fixture) → the pass never touches the Worker or the sheets.
    # This is ALSO the guard test: neutralizing `_equipment_enabled` (forcing it True) red-lights
    # here (get_fieldops_equipment_snapshot would be called). Prove-the-control-bites.
    _patch["equipment_snapshot"].return_value = _snap([_equip_row()])
    stats = fieldops_sync._sync_inside_lock()
    _patch["equipment_snapshot"].assert_not_called()
    _patch["ensure_equip_sheet"].assert_not_called()
    assert stats.equipment_upserted == 0 and stats.equipment_retired == 0
    assert stats.equipment_reviewed == 0 and stats.equipment_errors == 0


def test_equipment_pass_upserts_and_retires_no_mark_mirrored(_patch):
    _patch["equipment_enabled"].return_value = True
    _patch["equipment_snapshot"].return_value = _snap([_equip_row(10), _equip_row(11, name="Unit Beta")])
    _patch["retire_equip"].return_value = 1
    stats = fieldops_sync._sync_inside_lock()
    assert stats.equipment_upserted == 2 and stats.equipment_retired == 1
    assert stats.equipment_errors == 0 and stats.equipment_reviewed == 0
    _patch["ensure_equip_sheet"].assert_called_once_with("Job One")  # one sheet for the one job
    assert _patch["upsert_equip"].call_count == 2                    # one upsert per equipment
    # retire receives the FULL snapshot id-set (str-keyed), and the row-cap watchdog runs once
    _patch["retire_equip"].assert_called_once()
    assert _patch["retire_equip"].call_args.args[0] == 777
    assert _patch["retire_equip"].call_args.args[1] == {"10", "11"}
    _patch["equip_row_cap"].assert_called_once()
    _patch["find_equip_sheet"].assert_not_called()  # has-current path uses ensure, never find
    # SNAPSHOT — there is NO hours/jobs-style mark-mirrored for equipment.
    assert _patch["hb_row"].call_args.kwargs["status"] == "OK"
    assert _patch["hb_row"].call_args.kwargs["items_processed"] == 2


def test_equipment_retire_uses_full_snapshot_not_just_succeeded(_patch):
    # A transient upsert failure must NOT shrink the retire set (the item is still on the job).
    _patch["equipment_enabled"].return_value = True
    _patch["equipment_snapshot"].return_value = _snap([_equip_row(10), _equip_row(11, name="Unit Beta")])
    _patch["upsert_equip"].side_effect = [smartsheet_client.SmartsheetError("boom"), 5]
    fieldops_sync._sync_inside_lock()
    assert _patch["retire_equip"].call_args.args[1] == {"10", "11"}  # BOTH ids, not just the ok one


def test_equipment_permanent_failure_routes_review(_patch):
    _patch["equipment_enabled"].return_value = True
    _patch["equipment_snapshot"].return_value = _snap([_equip_row(10)])
    _patch["upsert_equip"].side_effect = smartsheet_client.SmartsheetValidationError("HTTP 400")
    stats = fieldops_sync._sync_inside_lock()
    assert stats.equipment_reviewed == 1 and stats.equipment_upserted == 0
    _patch["review"].assert_called_once()
    assert _patch["review"].call_args.kwargs["workstream"] == "progress_reports"
    assert _patch["hb_row"].call_args.kwargs["status"] == "WARN"


def test_equipment_per_item_fence_one_bad_does_not_block_others(_patch):
    _patch["equipment_enabled"].return_value = True
    _patch["equipment_snapshot"].return_value = _snap([_equip_row(10), _equip_row(11, name="Unit Beta")])
    _patch["upsert_equip"].side_effect = [smartsheet_client.SmartsheetError("boom"), 5]
    stats = fieldops_sync._sync_inside_lock()
    assert stats.equipment_errors == 1 and stats.equipment_upserted == 1  # Beta still mirrored
    _patch["retire_equip"].assert_called_once()  # retire still runs


def test_equipment_snapshot_fetch_failure_cycle_completes(_patch):
    _patch["equipment_enabled"].return_value = True
    _patch["equipment_snapshot"].side_effect = fieldops_sync.portal_client.PortalTransportError("x")
    stats = fieldops_sync._sync_inside_lock()
    assert stats.equipment_errors == 1 and stats.equipment_upserted == 0
    _patch["ensure_equip_sheet"].assert_not_called()
    # the equipment failure NEVER aborts the cycle — heartbeat + marker still run.
    _patch["hb"].assert_called_once()
    _patch["marker"].assert_called_once()


def test_equipment_malformed_row_is_skipped_never_silent(_patch):
    _patch["equipment_enabled"].return_value = True
    # A snapshot equipment row missing job_id can't be keyed → skipped + WARNed by the grouper. Its
    # roster (derived) is empty, so no job is visited.
    _patch["equipment_snapshot"].return_value = _snap([_equip_row(10, job_id="")], roster=[])
    stats = fieldops_sync._sync_inside_lock()
    assert stats.equipment_upserted == 0
    _patch["ensure_equip_sheet"].assert_not_called()
    assert any(
        c.kwargs.get("error_code") == "fieldops_equipment_row_malformed"
        for c in _patch["log"].call_args_list
    )


def test_equipment_retire_permanent_failure_routes_review(_patch):
    _patch["equipment_enabled"].return_value = True
    _patch["equipment_snapshot"].return_value = _snap([_equip_row(10)])
    _patch["retire_equip"].side_effect = smartsheet_client.SmartsheetValidationError("HTTP 400")
    stats = fieldops_sync._sync_inside_lock()
    assert stats.equipment_reviewed == 1
    assert _patch["review"].call_args.kwargs["workstream"] == "progress_reports"


# ---- reconcile roster: a job whose CURRENT equipment dropped to ZERO (the ops-stds BLOCK) ----


def test_equipment_zeroed_job_retires_all_without_recreate(_patch):
    # A job in the reconcile roster (has equipment_location history) but with ZERO current on-job
    # equipment this cycle: its sheet is FOUND (never re-created) and ALL its rows retired (retire
    # with the EMPTY id-set). This is the count-drops-to-zero fix — no ensure/create, no WARN/error.
    _patch["equipment_enabled"].return_value = True
    _patch["equipment_snapshot"].return_value = _snap(
        [], roster=[{"job_id": "JOB-1", "project_name": "Job One"}]
    )
    _patch["find_equip_sheet"].return_value = 777  # sheet exists from a prior cycle
    _patch["retire_equip"].return_value = 3        # 3 stale Active rows flipped Off Job
    stats = fieldops_sync._sync_inside_lock()
    assert stats.equipment_retired == 3 and stats.equipment_upserted == 0
    assert stats.equipment_reviewed == 0 and stats.equipment_errors == 0
    _patch["ensure_equip_sheet"].assert_not_called()             # NEVER create for a zeroed job
    _patch["find_equip_sheet"].assert_called_once_with("Job One")
    _patch["retire_equip"].assert_called_once()
    assert _patch["retire_equip"].call_args.args[0] == 777
    assert _patch["retire_equip"].call_args.args[1] == set()     # empty → retire ALL
    _patch["equip_row_cap"].assert_not_called()                  # no growth path for a zeroed job
    assert not any(
        str(c.kwargs.get("error_code", "")).startswith("fieldops_equipment_")
        for c in _patch["log"].call_args_list
    )


def test_equipment_zeroed_job_no_sheet_is_silent_noop(_patch):
    # A roster job that never had an Equipment sheet (find returns None) → skip; NEVER create an
    # empty sheet, no retire, no error. The common zero case.
    _patch["equipment_enabled"].return_value = True
    _patch["equipment_snapshot"].return_value = _snap(
        [], roster=[{"job_id": "JOB-1", "project_name": "Job One"}]
    )
    _patch["find_equip_sheet"].return_value = None
    stats = fieldops_sync._sync_inside_lock()
    assert stats.equipment_retired == 0 and stats.equipment_errors == 0 and stats.equipment_reviewed == 0
    _patch["ensure_equip_sheet"].assert_not_called()
    _patch["retire_equip"].assert_not_called()


def test_equipment_zeroed_find_sheet_transient_error_fenced(_patch):
    # A transient failure FINDING the sheet is fenced (errors++), never aborts the cycle.
    _patch["equipment_enabled"].return_value = True
    _patch["equipment_snapshot"].return_value = _snap(
        [], roster=[{"job_id": "JOB-1", "project_name": "Job One"}]
    )
    _patch["find_equip_sheet"].side_effect = smartsheet_client.SmartsheetError("boom")
    stats = fieldops_sync._sync_inside_lock()
    assert stats.equipment_errors == 1
    _patch["retire_equip"].assert_not_called()
    _patch["hb"].assert_called_once()  # cycle still completes


def test_equipment_roster_malformed_row_skipped_never_silent(_patch):
    # A roster row missing job_id/project_name is skipped + WARNed (never silent).
    _patch["equipment_enabled"].return_value = True
    _patch["equipment_snapshot"].return_value = _snap([], roster=[{"job_id": "", "project_name": ""}])
    stats = fieldops_sync._sync_inside_lock()
    assert stats.equipment_errors == 0
    _patch["find_equip_sheet"].assert_not_called()
    assert any(
        c.kwargs.get("error_code") == "fieldops_equipment_roster_malformed"
        for c in _patch["log"].call_args_list
    )


# ---- P7 material-list pass (Track 2, M2) ---------------------------------


def _mat_row(line_uuid: str = "u-10", job_id: str = "JOB-1", **over: Any) -> dict[str, Any]:
    e: dict[str, Any] = {
        "line_uuid": line_uuid,
        "job_id": job_id,
        "project_name": "Job One",
        "material_id": 3,
        "catalog_name": "Q.PEAK_DUO_XL",
        "description": "Solar panels",
        "qty": 120.0,
        "unit": "panels",
        "expected_date": "2026-07-10",
        "status": "expected",
        "received_at": None,
        "qty_received": None,
        "received_by_display": None,
        "note": None,
        "unplanned": 0,
        "seq": 10,
    }
    e.update(over)
    return e


def _mat_snap(
    lines: list[dict[str, Any]], roster: list[dict[str, Any]] | None = None
) -> FieldopsMaterialListSnapshot:
    """Build a FieldopsMaterialListSnapshot. If `roster` is None, derive one `{job_id, project_name}`
    entry per distinct job in the lines (the Worker's real invariant: every job with active lines
    appears in `jobs_with_materials`)."""
    if roster is None:
        seen: dict[str, dict[str, Any]] = {}
        for e in lines:
            jid = str(e.get("job_id") or "")
            if jid and jid not in seen:
                seen[jid] = {"job_id": jid, "project_name": str(e.get("project_name") or "")}
        roster = list(seen.values())
    return FieldopsMaterialListSnapshot(lines=lines, jobs_with_materials=roster)


def test_material_pass_off_by_default(_patch):
    # materials_enabled defaults False (fixture) → the pass never touches the Worker or the sheets.
    # This is ALSO the guard test: neutralizing `_materials_enabled` (forcing it True) red-lights
    # here (get_fieldops_material_list_snapshot would be called). Prove-the-control-bites.
    _patch["material_snapshot"].return_value = _mat_snap([_mat_row()])
    stats = fieldops_sync._sync_inside_lock()
    _patch["material_snapshot"].assert_not_called()
    _patch["ensure_mat_sheet"].assert_not_called()
    assert stats.materials_upserted == 0 and stats.materials_retired == 0
    assert stats.materials_reviewed == 0 and stats.materials_errors == 0


def test_material_pass_upserts_and_retires_no_mark_mirrored(_patch):
    _patch["materials_enabled"].return_value = True
    _patch["material_snapshot"].return_value = _mat_snap(
        [_mat_row("u-10"), _mat_row("u-11", description="Rebar", catalog_name=None, material_id=None)]
    )
    _patch["retire_mat"].return_value = 1
    stats = fieldops_sync._sync_inside_lock()
    assert stats.materials_upserted == 2 and stats.materials_retired == 1
    assert stats.materials_errors == 0 and stats.materials_reviewed == 0
    _patch["ensure_mat_sheet"].assert_called_once_with("Job One")  # one sheet for the one job
    assert _patch["upsert_mat"].call_count == 2                    # one upsert per line
    # retire receives the FULL snapshot uuid-set (str-keyed), and the row-cap watchdog runs once
    _patch["retire_mat"].assert_called_once()
    assert _patch["retire_mat"].call_args.args[0] == 555
    assert _patch["retire_mat"].call_args.args[1] == {"u-10", "u-11"}
    _patch["mat_row_cap"].assert_called_once()
    _patch["find_mat_sheet"].assert_not_called()  # has-current path uses ensure, never find
    # SNAPSHOT — there is NO hours/jobs-style mark-mirrored for materials.
    assert _patch["hb_row"].call_args.kwargs["status"] == "OK"
    assert _patch["hb_row"].call_args.kwargs["items_processed"] == 2


def test_material_free_text_line_maps_placeholder_and_primary(_patch):
    # A free-text (no-catalog) line → Line = description, Material = '—', and unplanned flag maps.
    _patch["materials_enabled"].return_value = True
    _patch["material_snapshot"].return_value = _mat_snap(
        [_mat_row("u-11", description="Rebar bundles", catalog_name=None, material_id=None, unplanned=1)]
    )
    fieldops_sync._sync_inside_lock()
    kw = _patch["upsert_mat"].call_args.kwargs
    assert kw["line"] == "Rebar bundles"
    assert kw["material"] == fieldops_sync.material_list.MATERIAL_NONE
    assert kw["unplanned"] == fieldops_sync.material_list.UNPLANNED_YES


def test_material_retire_uses_full_snapshot_not_just_succeeded(_patch):
    # A transient upsert failure must NOT shrink the retire set (the line is still on the list).
    _patch["materials_enabled"].return_value = True
    _patch["material_snapshot"].return_value = _mat_snap([_mat_row("u-10"), _mat_row("u-11")])
    _patch["upsert_mat"].side_effect = [smartsheet_client.SmartsheetError("boom"), 5]
    fieldops_sync._sync_inside_lock()
    assert _patch["retire_mat"].call_args.args[1] == {"u-10", "u-11"}  # BOTH ids, not just the ok one


def test_material_permanent_failure_routes_review(_patch):
    _patch["materials_enabled"].return_value = True
    _patch["material_snapshot"].return_value = _mat_snap([_mat_row("u-10")])
    _patch["upsert_mat"].side_effect = smartsheet_client.SmartsheetValidationError("HTTP 400")
    stats = fieldops_sync._sync_inside_lock()
    assert stats.materials_reviewed == 1 and stats.materials_upserted == 0
    _patch["review"].assert_called_once()
    assert _patch["review"].call_args.kwargs["workstream"] == "progress_reports"
    assert _patch["hb_row"].call_args.kwargs["status"] == "WARN"


def test_material_per_line_fence_one_bad_does_not_block_others(_patch):
    _patch["materials_enabled"].return_value = True
    _patch["material_snapshot"].return_value = _mat_snap([_mat_row("u-10"), _mat_row("u-11")])
    _patch["upsert_mat"].side_effect = [smartsheet_client.SmartsheetError("boom"), 5]
    stats = fieldops_sync._sync_inside_lock()
    assert stats.materials_errors == 1 and stats.materials_upserted == 1  # the other still mirrored
    _patch["retire_mat"].assert_called_once()  # retire still runs


def test_material_snapshot_fetch_failure_cycle_completes(_patch):
    _patch["materials_enabled"].return_value = True
    _patch["material_snapshot"].side_effect = fieldops_sync.portal_client.PortalTransportError("x")
    stats = fieldops_sync._sync_inside_lock()
    assert stats.materials_errors == 1 and stats.materials_upserted == 0
    _patch["ensure_mat_sheet"].assert_not_called()
    # the material failure NEVER aborts the cycle — heartbeat + marker still run.
    _patch["hb"].assert_called_once()
    _patch["marker"].assert_called_once()


def test_material_malformed_row_is_skipped_never_silent(_patch):
    _patch["materials_enabled"].return_value = True
    # A snapshot line missing line_uuid can't be keyed → skipped + WARNed by the grouper. Its roster
    # (explicit empty) means no job is visited.
    _patch["material_snapshot"].return_value = _mat_snap([_mat_row("", job_id="JOB-1")], roster=[])
    stats = fieldops_sync._sync_inside_lock()
    assert stats.materials_upserted == 0
    _patch["ensure_mat_sheet"].assert_not_called()
    assert any(
        c.kwargs.get("error_code") == "fieldops_material_row_malformed"
        for c in _patch["log"].call_args_list
    )


def test_material_retire_permanent_failure_routes_review(_patch):
    _patch["materials_enabled"].return_value = True
    _patch["material_snapshot"].return_value = _mat_snap([_mat_row("u-10")])
    _patch["retire_mat"].side_effect = smartsheet_client.SmartsheetValidationError("HTTP 400")
    stats = fieldops_sync._sync_inside_lock()
    assert stats.materials_reviewed == 1
    assert _patch["review"].call_args.kwargs["workstream"] == "progress_reports"


# ---- material reconcile roster: a job whose ACTIVE lines dropped to ZERO ----


def test_material_zeroed_job_retires_all_without_recreate(_patch):
    # A job in the reconcile roster (has material-line history) but with ZERO active lines this
    # cycle: its sheet is FOUND (never re-created) and ALL its rows marked Removed (retire with the
    # EMPTY uuid-set). This is the count-drops-to-zero fix — no ensure/create, no WARN/error.
    _patch["materials_enabled"].return_value = True
    _patch["material_snapshot"].return_value = _mat_snap(
        [], roster=[{"job_id": "JOB-1", "project_name": "Job One"}]
    )
    _patch["find_mat_sheet"].return_value = 555  # sheet exists from a prior cycle
    _patch["retire_mat"].return_value = 3        # 3 stale Active rows flipped Removed
    stats = fieldops_sync._sync_inside_lock()
    assert stats.materials_retired == 3 and stats.materials_upserted == 0
    assert stats.materials_reviewed == 0 and stats.materials_errors == 0
    _patch["ensure_mat_sheet"].assert_not_called()             # NEVER create for a zeroed job
    _patch["find_mat_sheet"].assert_called_once_with("Job One")
    _patch["retire_mat"].assert_called_once()
    assert _patch["retire_mat"].call_args.args[0] == 555
    assert _patch["retire_mat"].call_args.args[1] == set()     # empty → retire ALL
    _patch["mat_row_cap"].assert_not_called()                  # no growth path for a zeroed job
    assert not any(
        str(c.kwargs.get("error_code", "")).startswith("fieldops_material_")
        for c in _patch["log"].call_args_list
    )


def test_material_zeroed_job_no_sheet_is_silent_noop(_patch):
    # A roster job that never had a Material List sheet (find returns None) → skip; NEVER create an
    # empty sheet, no retire, no error. The common zero case.
    _patch["materials_enabled"].return_value = True
    _patch["material_snapshot"].return_value = _mat_snap(
        [], roster=[{"job_id": "JOB-1", "project_name": "Job One"}]
    )
    _patch["find_mat_sheet"].return_value = None
    stats = fieldops_sync._sync_inside_lock()
    assert stats.materials_retired == 0 and stats.materials_errors == 0 and stats.materials_reviewed == 0
    _patch["ensure_mat_sheet"].assert_not_called()
    _patch["retire_mat"].assert_not_called()


def test_material_zeroed_find_sheet_transient_error_fenced(_patch):
    # A transient failure FINDING the sheet is fenced (errors++), never aborts the cycle.
    _patch["materials_enabled"].return_value = True
    _patch["material_snapshot"].return_value = _mat_snap(
        [], roster=[{"job_id": "JOB-1", "project_name": "Job One"}]
    )
    _patch["find_mat_sheet"].side_effect = smartsheet_client.SmartsheetError("boom")
    stats = fieldops_sync._sync_inside_lock()
    assert stats.materials_errors == 1
    _patch["retire_mat"].assert_not_called()
    _patch["hb"].assert_called_once()  # cycle still completes


def test_material_roster_malformed_row_skipped_never_silent(_patch):
    # A roster row missing job_id/project_name is skipped + WARNed (never silent).
    _patch["materials_enabled"].return_value = True
    _patch["material_snapshot"].return_value = _mat_snap([], roster=[{"job_id": "", "project_name": ""}])
    stats = fieldops_sync._sync_inside_lock()
    assert stats.materials_errors == 0
    _patch["find_mat_sheet"].assert_not_called()
    assert any(
        c.kwargs.get("error_code") == "fieldops_material_roster_malformed"
        for c in _patch["log"].call_args_list
    )


# ---- P7 material-incidents pass (Track 2, M3 Slice 2) — APPEND-ONLY ledger ----


def _inc_row(
    submission_uuid: str = "sub-10", job_id: str = "JOB-1", **over: Any
) -> dict[str, Any]:
    e: dict[str, Any] = {
        "submission_uuid": submission_uuid,
        "job_id": job_id,
        "project_name": "Job One",
        "work_date": "2026-07-06",
        "created_at": 1_751_000_000,
        "box_link": "https://box/999",
        "material_description": "Q.PEAK panels",
        "delivery_ref": "PO-4471",
        "qty_expected": 120.0,
        "qty_received": 118.0,
        "issue": "Short",
        "details": "2 pallets short",
        "action_taken": "CM notified",
        "line_uuid": "u-10",
        "reported_by_display": "Mo Manager",
        "line_status": "incident",
    }
    e.update(over)
    return e


def test_incident_pass_off_by_default(_patch):
    # incidents_enabled defaults False (fixture) → the pass never touches the Worker or the sheets.
    # This is ALSO the guard test: forcing `_incidents_enabled` True red-lights here
    # (get_fieldops_material_incidents would be called). Prove-the-control-bites.
    _patch["incidents_list"].return_value = [_inc_row()]
    stats = fieldops_sync._sync_inside_lock()
    _patch["incidents_list"].assert_not_called()
    _patch["ensure_inc_sheet"].assert_not_called()
    assert stats.incidents_upserted == 0
    assert stats.incidents_reviewed == 0 and stats.incidents_errors == 0


def test_incident_pass_upserts_no_retire_no_mark_mirrored(_patch):
    _patch["incidents_enabled"].return_value = True
    _patch["incidents_list"].return_value = [_inc_row("sub-10"), _inc_row("sub-11", issue="Damaged")]
    stats = fieldops_sync._sync_inside_lock()
    assert stats.incidents_upserted == 2
    assert stats.incidents_errors == 0 and stats.incidents_reviewed == 0
    _patch["ensure_inc_sheet"].assert_called_once_with("Job One")  # one sheet for the one job
    assert _patch["upsert_inc"].call_count == 2                    # one upsert per incident
    _patch["inc_row_cap"].assert_called_once()                    # row-cap watchdog runs once
    # APPEND-ONLY — no retire / no mark-mirrored symbol exists to call.
    assert not hasattr(fieldops_sync.material_incidents, "retire_removed")
    assert _patch["hb_row"].call_args.kwargs["status"] == "OK"
    assert _patch["hb_row"].call_args.kwargs["items_processed"] == 2


def test_incident_fields_and_line_status_mapped(_patch):
    _patch["incidents_enabled"].return_value = True
    _patch["incidents_list"].return_value = [_inc_row("sub-10", line_status="received")]
    fieldops_sync._sync_inside_lock()
    kw = _patch["upsert_inc"].call_args.kwargs
    assert kw["incident_uuid"] == "sub-10"
    assert kw["material"] == "Q.PEAK panels"
    assert kw["issue"] == "Short"
    assert kw["line_uuid"] == "u-10"
    assert kw["line_status"] == "received"       # the live resolution signal
    assert kw["qty_expected"] == "120"           # _fmt_qty trims the trailing .0
    assert kw["qty_received"] == "118"
    assert kw["reported_by"] == "Mo Manager"     # DISPLAY name, never a username
    assert kw["reported_at"] == "2026-07-06"     # work_date preferred
    assert kw["report"] == "https://box/999"


def test_incident_unlinked_maps_blank_line_fields(_patch):
    # An incident with no referenced line (line_uuid/line_status None from the endpoint) → blanks.
    _patch["incidents_enabled"].return_value = True
    _patch["incidents_list"].return_value = [_inc_row("sub-10", line_uuid=None, line_status=None)]
    fieldops_sync._sync_inside_lock()
    kw = _patch["upsert_inc"].call_args.kwargs
    assert kw["line_uuid"] == "" and kw["line_status"] == ""


def test_incident_reported_at_falls_back_to_created_at(_patch):
    # A missing work_date → the Reported At falls back to the Pacific date of created_at (never blank).
    _patch["incidents_enabled"].return_value = True
    _patch["incidents_list"].return_value = [_inc_row("sub-10", work_date="")]
    fieldops_sync._sync_inside_lock()
    kw = _patch["upsert_inc"].call_args.kwargs
    assert kw["reported_at"]  # non-empty — derived from created_at


def test_incident_permanent_failure_routes_review(_patch):
    _patch["incidents_enabled"].return_value = True
    _patch["incidents_list"].return_value = [_inc_row("sub-10")]
    _patch["upsert_inc"].side_effect = smartsheet_client.SmartsheetValidationError("HTTP 400")
    stats = fieldops_sync._sync_inside_lock()
    assert stats.incidents_reviewed == 1 and stats.incidents_upserted == 0
    _patch["review"].assert_called_once()
    assert _patch["review"].call_args.kwargs["workstream"] == "progress_reports"
    assert _patch["hb_row"].call_args.kwargs["status"] == "WARN"


def test_incident_per_incident_fence_one_bad_does_not_block_others(_patch):
    _patch["incidents_enabled"].return_value = True
    _patch["incidents_list"].return_value = [_inc_row("sub-10"), _inc_row("sub-11")]
    _patch["upsert_inc"].side_effect = [smartsheet_client.SmartsheetError("boom"), 5]
    stats = fieldops_sync._sync_inside_lock()
    assert stats.incidents_errors == 1 and stats.incidents_upserted == 1  # the other still mirrored
    _patch["inc_row_cap"].assert_called_once()  # row-cap still runs


def test_incident_ensure_sheet_permanent_failure_routes_review(_patch):
    _patch["incidents_enabled"].return_value = True
    _patch["incidents_list"].return_value = [_inc_row("sub-10")]
    _patch["ensure_inc_sheet"].side_effect = smartsheet_client.SmartsheetValidationError("HTTP 400")
    stats = fieldops_sync._sync_inside_lock()
    assert stats.incidents_reviewed == 1 and stats.incidents_upserted == 0
    _patch["upsert_inc"].assert_not_called()
    assert _patch["review"].call_args.kwargs["workstream"] == "progress_reports"


def test_incident_fetch_failure_cycle_completes(_patch):
    _patch["incidents_enabled"].return_value = True
    _patch["incidents_list"].side_effect = fieldops_sync.portal_client.PortalTransportError("x")
    stats = fieldops_sync._sync_inside_lock()
    assert stats.incidents_errors == 1 and stats.incidents_upserted == 0
    _patch["ensure_inc_sheet"].assert_not_called()
    # the incident failure NEVER aborts the cycle — heartbeat + marker still run.
    _patch["hb"].assert_called_once()
    _patch["marker"].assert_called_once()


def test_incident_fetch_401_is_critical_and_cycle_completes(_patch):
    _patch["incidents_enabled"].return_value = True
    _patch["incidents_list"].side_effect = fieldops_sync.portal_client.PortalAuthError("401")
    stats = fieldops_sync._sync_inside_lock()
    assert stats.incidents_errors == 1
    assert any(
        c.kwargs.get("error_code") == "fieldops_incidents_fetch_auth_failed"
        and c.args[0] == fieldops_sync.Severity.CRITICAL
        for c in _patch["log"].call_args_list
    )
    _patch["hb"].assert_called_once()  # cycle still completes


def test_incident_malformed_row_is_skipped_never_silent(_patch):
    _patch["incidents_enabled"].return_value = True
    # An incident missing submission_uuid can't be keyed → skipped + WARNed by the grouper.
    _patch["incidents_list"].return_value = [_inc_row("", job_id="JOB-1")]
    stats = fieldops_sync._sync_inside_lock()
    assert stats.incidents_upserted == 0
    _patch["ensure_inc_sheet"].assert_not_called()
    assert any(
        c.kwargs.get("error_code") == "fieldops_incident_row_malformed"
        for c in _patch["log"].call_args_list
    )


def test_incident_multiple_jobs_each_get_own_sheet(_patch):
    _patch["incidents_enabled"].return_value = True
    _patch["incidents_list"].return_value = [
        _inc_row("sub-10", job_id="JOB-1", project_name="Job One"),
        _inc_row("sub-20", job_id="JOB-2", project_name="Job Two"),
    ]
    stats = fieldops_sync._sync_inside_lock()
    assert stats.incidents_upserted == 2
    assert _patch["ensure_inc_sheet"].call_count == 2
    ensured = {c.args[0] for c in _patch["ensure_inc_sheet"].call_args_list}
    assert ensured == {"Job One", "Job Two"}


# ---- config-read transient fence (DASH-14 port of PR #613) ----------------


def test_read_str_setting_transient_error_falls_open_with_warn(mocker):
    """A generic SmartsheetError from get_setting (read-timeout / 5xx) must NOT escape
    to @its_error_log as a spurious CRITICAL — WARN `config_read_error` + fallback,
    same disposition as the circuit-open branch."""
    mocker.patch(
        "field_ops.fieldops_sync.smartsheet_client.get_setting",
        side_effect=smartsheet_client.SmartsheetError("read timeout"),
    )
    log = mocker.patch("field_ops.fieldops_sync.error_log.log")

    result = fieldops_sync._read_str_setting("field_ops.some_key", "fallback-val")  # must not raise

    assert result == "fallback-val"
    codes = [kw.get("error_code") for _, kw in log.call_args_list]
    assert "config_read_error" in codes


def test_sync_gate_transient_error_resolves_to_default(mocker):
    """Cycle-entry proof: the sync gate read survives a transient and resolves to the
    ships-dark default (False) instead of crashing the cycle."""
    mocker.patch(
        "field_ops.fieldops_sync.smartsheet_client.get_setting",
        side_effect=smartsheet_client.SmartsheetError("HTTP 502"),
    )
    mocker.patch("field_ops.fieldops_sync.error_log.log")

    assert fieldops_sync._sync_enabled() is fieldops_sync.DEFAULT_SYNC_ENABLED
