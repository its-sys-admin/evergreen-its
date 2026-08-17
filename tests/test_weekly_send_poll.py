"""Unit tests for safety_reports/weekly_send_poll.py — the Phase-5 WSR dispatcher.

P1c: the dispatch body lives in `safety_reports/send_poll_core.py` (parameterized
by `DaemonConfig`); this module is the thin SAFETY entry. Data-plane mocks target
`send_poll_core.*` (where the body calls them); the heartbeat / watchdog / stamp /
scheduled-window SEAMS stay patched on the entry (the core resolves them by
injection from the entry at call time). Behavior is asserted byte-equivalent.
"""
from __future__ import annotations

from typing import Any

import pytest

from safety_reports import send_poll_core, weekly_send, weekly_send_poll, wsr_review
from safety_reports.weekly_send import SendResult
from safety_reports.weekly_send_poll import (
    DAEMON_NAME,
    _filter_dispatch_candidates,
    _is_scheduled_window,
    _poll_inside_lock,
    poll_once,
)
from shared.approval_verification import ApprovalVerdict, VerdictReason
from shared.smartsheet_client import (
    SmartsheetError,
    SmartsheetNotFoundError,
    SmartsheetTransientError,
)


def _row(
    *, row_id: int, send_now: bool = True, scheduled: bool = False,
    send_status: str = weekly_send.STATUS_PENDING, notes: str = "",
) -> dict[str, Any]:
    return {
        "_row_id": row_id,
        wsr_review.COL_JOB_PROJECT: "Bradley 1",
        wsr_review.COL_JOB_ID: "JOB-1",
        wsr_review.COL_WEEK_OF: "2026-05-30",
        wsr_review.COL_SEND_NOW: send_now,
        wsr_review.COL_APPROVE_SCHEDULED: scheduled,
        wsr_review.COL_SEND_STATUS: send_status,
        wsr_review.COL_NOTES: notes,
    }


def _get_setting_stub(key: str, *, workstream: str) -> str:
    """ITS_Config stub: the polling gate reads ENABLED; every other key falls back.

    The gate is stubbed EXPLICITLY rather than left to the module default, because
    `DEFAULT_POLLING_ENABLED` is now False (a send gate never fails open). Before that
    flip this whole suite leaned on the fail-open default to get past the gate — which
    is precisely the property being removed, so a test wanting a live cycle must now
    say so out loud. Non-gate keys still raise NotFound so their own fallbacks
    (scheduled_send_local, poll_interval) stay exercised.
    """
    if key == weekly_send_poll.CFG_POLLING_ENABLED:
        return "true"
    raise SmartsheetNotFoundError("default test stub")


@pytest.fixture
def _patch_all(mocker):
    return {
        # --- data-plane: now lives in send_poll_core ---
        "get_rows": mocker.patch("safety_reports.send_poll_core.smartsheet_client.get_rows", return_value=[]),
        "send_one_row": mocker.patch(
            "safety_reports.weekly_send.send_one_row",  # CONFIG.send_fn late-binds to this
            return_value=SendResult(status="sent", row_id=0, project_name="Bradley 1"),
        ),
        "get_setting": mocker.patch(
            "safety_reports.send_poll_core.smartsheet_client.get_setting",
            side_effect=_get_setting_stub,
        ),
        "workspace_shares": mocker.patch(
            "safety_reports.send_poll_core.smartsheet_client.list_workspace_share_emails",
            return_value=frozenset({"seths@evergreenmirror.com"}),
        ),
        "error_log": mocker.patch("safety_reports.send_poll_core.error_log.log", return_value=None),
        "verify_approval": mocker.patch(
            "safety_reports.send_poll_core.approval_verification.verify_approval",
            return_value=ApprovalVerdict(verified=True, reason=VerdictReason.AUTHORIZED, actor="seths@evergreenmirror.com"),
        ),
        "alert_critical": mocker.patch("safety_reports.send_poll_core.error_log._alert_critical", return_value=None),
        # --- injected seams: stay on the entry (core resolves them by injection) ---
        "write_heartbeat": mocker.patch("safety_reports.weekly_send_poll._write_heartbeat", return_value=None),
        "write_heartbeat_row": mocker.patch("safety_reports.weekly_send_poll._write_heartbeat_row", return_value=None),
        "write_watchdog_marker": mocker.patch("safety_reports.weekly_send_poll._write_watchdog_marker", return_value=None),
        "stamp": mocker.patch("safety_reports.weekly_send_poll._stamp_approval", return_value=None),
    }


# ---- filter --------------------------------------------------------------


def test_filter_requires_an_approval_checkbox():
    rows = [_row(row_id=1, send_now=True), _row(row_id=2, send_now=False, scheduled=False)]
    assert [r["_row_id"] for r in _filter_dispatch_candidates(rows)] == [1]


def test_filter_includes_scheduled_only_approval():
    rows = [_row(row_id=1, send_now=False, scheduled=True)]
    assert [r["_row_id"] for r in _filter_dispatch_candidates(rows)] == [1]


def test_filter_skips_sent():
    rows = [_row(row_id=1, send_status=weekly_send.STATUS_SENT), _row(row_id=2)]
    assert [r["_row_id"] for r in _filter_dispatch_candidates(rows)] == [2]


def test_filter_skips_terminally_failed():
    rows = [_row(row_id=1, send_status=weekly_send.STATUS_FAILED,
                 notes=f"[SEND_RETRY_COUNT: {weekly_send.MAX_SEND_RETRIES}]")]
    assert _filter_dispatch_candidates(rows) == []


def test_filter_includes_failed_under_cap():
    rows = [_row(row_id=1, send_status=weekly_send.STATUS_FAILED, notes="[SEND_RETRY_COUNT: 1]")]
    assert [r["_row_id"] for r in _filter_dispatch_candidates(rows)] == [1]


# ---- scheduled window ----------------------------------------------------


@pytest.mark.parametrize("spec,expected_wd,expected_h", [("MON 07:00", 0, 7), ("FRI 14:30", 4, 14)])
def test_parse_scheduled_spec(spec, expected_wd, expected_h):
    wd, t = weekly_send_poll._parse_scheduled_spec(spec)
    assert wd == expected_wd and t.hour == expected_h


def test_is_scheduled_window():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    pac = ZoneInfo("America/Los_Angeles")
    mon_8am = datetime(2026, 6, 1, 8, 0, tzinfo=pac)   # Monday 08:00
    mon_6am = datetime(2026, 6, 1, 6, 0, tzinfo=pac)   # Monday 06:00 (before window)
    tue_8am = datetime(2026, 6, 2, 8, 0, tzinfo=pac)   # Tuesday
    assert _is_scheduled_window(mon_8am, "MON 07:00") is True
    assert _is_scheduled_window(mon_6am, "MON 07:00") is False
    assert _is_scheduled_window(tue_8am, "MON 07:00") is False


# ---- F22 authorized-approver source (workspace membership) ---------------


def test_load_authorized_approvers_reads_workspace_shares(mocker):
    shares = mocker.patch(
        "safety_reports.send_poll_core.smartsheet_client.list_workspace_share_emails",
        return_value=frozenset({"a@x.com", "b@x.com"}),
    )
    out = weekly_send_poll._load_authorized_approvers()
    assert out == frozenset({"a@x.com", "b@x.com"})
    shares.assert_called_once_with(weekly_send_poll.sheet_ids.WORKSPACE_SAFETY_PORTAL)


def test_load_authorized_approvers_empty_workspace_is_fail_closed_empty(mocker):
    # No individual shares → empty set → verify_approval treats it as
    # EMPTY_ALLOWLIST → block all sends (fail-closed, never fail-open).
    mocker.patch(
        "safety_reports.send_poll_core.smartsheet_client.list_workspace_share_emails",
        return_value=frozenset(),
    )
    assert weekly_send_poll._load_authorized_approvers() == frozenset()


def test_load_authorized_approvers_smartsheet_error_propagates(mocker):
    # A membership-read infra failure must surface (→ @its_error_log CRITICAL,
    # cycle aborts with zero sends), never silently fail-open.
    mocker.patch(
        "safety_reports.send_poll_core.smartsheet_client.list_workspace_share_emails",
        side_effect=SmartsheetError("boom"),
    )
    with pytest.raises(SmartsheetError):
        weekly_send_poll._load_authorized_approvers()


# ---- dispatch ------------------------------------------------------------


def test_send_now_dispatches_immediately_and_stamps(_patch_all):
    _patch_all["get_rows"].return_value = [_row(row_id=10), _row(row_id=11)]
    result = _poll_inside_lock()
    assert result.dispatched == 2 and result.sent == 2
    assert _patch_all["send_one_row"].call_count == 2
    # F22 verified on the Send Now column, against the WSR sheet.
    call = _patch_all["verify_approval"].call_args
    from shared import sheet_ids
    assert call.args[0] == sheet_ids.SHEET_WSR_HUMAN_REVIEW
    assert call.args[2] == wsr_review.COL_SEND_NOW
    assert _patch_all["stamp"].call_count == 2  # approver stamped before dispatch


def test_scheduled_row_waits_outside_window(_patch_all, mocker):
    mocker.patch.object(weekly_send_poll, "_is_scheduled_window", return_value=False)
    _patch_all["get_rows"].return_value = [_row(row_id=20, send_now=False, scheduled=True)]
    result = _poll_inside_lock()
    assert result.dispatched == 0 and result.skipped == 1
    _patch_all["send_one_row"].assert_not_called()
    _patch_all["verify_approval"].assert_not_called()


def test_scheduled_row_dispatches_in_window_on_scheduled_column(_patch_all, mocker):
    mocker.patch.object(weekly_send_poll, "_is_scheduled_window", return_value=True)
    _patch_all["get_rows"].return_value = [_row(row_id=21, send_now=False, scheduled=True)]
    result = _poll_inside_lock()
    assert result.dispatched == 1
    assert _patch_all["verify_approval"].call_args.args[2] == wsr_review.COL_APPROVE_SCHEDULED


def test_per_row_fence_continues_after_error(_patch_all):
    _patch_all["get_rows"].return_value = [_row(row_id=30), _row(row_id=31), _row(row_id=32)]
    order: list[int] = []

    def _se(row_id, _cfg):  # CONFIG.send_fn calls send_one_row(row_id, weekly_send.CONFIG)
        order.append(row_id)
        if row_id == 31:
            raise SmartsheetError("transient")
        return SendResult(status="sent", row_id=row_id)

    _patch_all["send_one_row"].side_effect = _se
    result = _poll_inside_lock()
    assert order == [30, 31, 32] and result.errors == 1 and result.sent == 2


def test_counter_split(_patch_all):
    _patch_all["get_rows"].return_value = [_row(row_id=40), _row(row_id=41), _row(row_id=42)]
    _patch_all["send_one_row"].side_effect = [
        SendResult(status="sent", row_id=40),
        SendResult(status="skipped_already_sent", row_id=41),
        SendResult(status="send_failed", row_id=42, error="GraphError"),
    ]
    result = _poll_inside_lock()
    assert result.sent == 1 and result.skipped == 1 and result.failed == 1


# ---- heartbeat -----------------------------------------------------------


def test_heartbeat_file_and_ok_status(_patch_all):
    _poll_inside_lock()
    _patch_all["write_heartbeat"].assert_called_once()
    assert _patch_all["write_heartbeat_row"].call_args.kwargs["status"] == "OK"


def test_heartbeat_warn_on_failure(_patch_all):
    _patch_all["get_rows"].return_value = [_row(row_id=50)]
    _patch_all["send_one_row"].return_value = SendResult(status="send_failed", row_id=50)
    _poll_inside_lock()
    assert _patch_all["write_heartbeat_row"].call_args.kwargs["status"] == "WARN"


def test_heartbeat_degraded_on_exception(_patch_all):
    _patch_all["get_rows"].return_value = [_row(row_id=60)]
    _patch_all["send_one_row"].side_effect = SmartsheetError("boom")
    _poll_inside_lock()
    assert _patch_all["write_heartbeat_row"].call_args.kwargs["status"] == "DEGRADED"


def test_watchdog_marker_written(_patch_all):
    _poll_inside_lock()
    _patch_all["write_watchdog_marker"].assert_called_once()


# ---- read failure --------------------------------------------------------


def test_get_rows_failure_writes_error_heartbeat(_patch_all):
    # A 5xx/timeout is the TRANSIENT class → halted cycle + ERROR heartbeat (unchanged).
    # A non-transient SmartsheetError now re-raises to CRITICAL instead — see
    # tests/test_send_poll_core.py::test_review_read_*.
    _patch_all["get_rows"].side_effect = SmartsheetTransientError("HTTP 500")
    result = _poll_inside_lock()
    assert result.errors == 1
    assert _patch_all["write_heartbeat_row"].call_args.kwargs["status"] == "ERROR"
    _patch_all["write_watchdog_marker"].assert_called_once()


# ---- F22 block paths -----------------------------------------------------


def test_unverified_unauthorized_blocks_and_pages_critical(_patch_all):
    from shared.error_log import Severity
    _patch_all["get_rows"].return_value = [_row(row_id=70)]
    _patch_all["verify_approval"].return_value = ApprovalVerdict(
        verified=False, reason=VerdictReason.UNAUTHORIZED_ACTOR, actor="attacker@evil.com")
    result = _poll_inside_lock()
    _patch_all["send_one_row"].assert_not_called()
    _patch_all["stamp"].assert_not_called()
    assert result.blocked == 1 and result.dispatched == 0
    paged = [c for c in _patch_all["error_log"].call_args_list
             if c.args and c.args[0] == Severity.CRITICAL and c.kwargs.get("error_code") == "approval_unverified"]
    assert len(paged) == 1
    _patch_all["alert_critical"].assert_not_called()  # double-fire guard


def test_empty_allowlist_blocks_and_pages(_patch_all):
    from shared.error_log import Severity
    _patch_all["get_rows"].return_value = [_row(row_id=72)]
    _patch_all["verify_approval"].return_value = ApprovalVerdict(verified=False, reason=VerdictReason.EMPTY_ALLOWLIST)
    result = _poll_inside_lock()
    assert result.blocked == 1
    _patch_all["send_one_row"].assert_not_called()
    assert any(c.args and c.args[0] == Severity.CRITICAL and c.kwargs.get("error_code") == "approval_unverified"
               for c in _patch_all["error_log"].call_args_list)


def test_not_currently_approved_blocks_without_paging(_patch_all):
    from shared.error_log import Severity
    _patch_all["get_rows"].return_value = [_row(row_id=73)]
    _patch_all["verify_approval"].return_value = ApprovalVerdict(
        verified=False, reason=VerdictReason.NOT_CURRENTLY_APPROVED, actor="seths@evergreenmirror.com")
    result = _poll_inside_lock()
    assert result.blocked == 1
    blocked = [c for c in _patch_all["error_log"].call_args_list if c.kwargs.get("error_code") == "approval_unverified"]
    assert blocked and blocked[0].args[0] == Severity.WARN


def test_blocks_one_dispatches_other(_patch_all):
    _patch_all["get_rows"].return_value = [_row(row_id=80), _row(row_id=81)]
    _patch_all["verify_approval"].side_effect = [
        ApprovalVerdict(verified=False, reason=VerdictReason.UNAUTHORIZED_ACTOR, actor="x@y.com"),
        ApprovalVerdict(verified=True, reason=VerdictReason.AUTHORIZED, actor="seths@evergreenmirror.com"),
    ]
    result = _poll_inside_lock()
    assert result.blocked == 1 and result.dispatched == 1
    assert _patch_all["send_one_row"].call_args.args[0] == 81


def test_blocked_sets_warn_heartbeat(_patch_all):
    _patch_all["get_rows"].return_value = [_row(row_id=90)]
    _patch_all["verify_approval"].return_value = ApprovalVerdict(
        verified=False, reason=VerdictReason.UNAUTHORIZED_ACTOR, actor="x@y.com")
    _poll_inside_lock()
    assert _patch_all["write_heartbeat_row"].call_args.kwargs["status"] == "WARN"


# ---- poll_once gating ----------------------------------------------------


def test_poll_once_skipped_when_disabled(_patch_all):
    _patch_all["get_setting"].side_effect = None
    _patch_all["get_setting"].return_value = "false"
    result = poll_once()
    assert result.skipped_disabled is True
    _patch_all["get_rows"].assert_not_called()


def test_default_polling_enabled_is_false_fail_safe():
    # HOUSE_REFLEXES §5: a send daemon's row-absent default must be dark (False), never
    # fail-open to SENDING. Pins the constant AND its propagation into the DaemonConfig.
    assert weekly_send_poll.DEFAULT_POLLING_ENABLED is False
    assert weekly_send_poll.CONFIG.default_polling_enabled is False


@pytest.mark.parametrize(
    "exc",
    [
        SmartsheetNotFoundError("row absent"),
        send_poll_core.smartsheet_client.SmartsheetCircuitOpenError("breaker open"),
        SmartsheetError("5xx"),
    ],
)
def test_unreadable_polling_config_skips_the_cycle_without_sending(_patch_all, exc):
    # PROVE-IT-BITES, across ALL THREE `_read_str_setting` fallback branches (the NotFound
    # one logs nothing at all): with the gate row unreadable, the cycle short-circuits
    # fail-safe instead of dispatching an approved send_now row to the CUSTOMER. Before
    # this flip every one of these three cases would have SENT — which is how a paused
    # send daemon could silently re-arm itself.
    _patch_all["get_setting"].side_effect = exc
    _patch_all["get_rows"].return_value = [_row(row_id=90)]
    stats = poll_once()
    assert stats.skipped_disabled is True
    assert stats.dispatched == 0 and stats.sent == 0
    _patch_all["send_one_row"].assert_not_called()


# ---- wiring --------------------------------------------------------------


def test_daemon_name_is_stable():
    assert DAEMON_NAME == "safety_reports.weekly_send_poll"


def test_watchdog_job_slug_matches_watchdog_tracked_jobs():
    import sys
    from pathlib import Path
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import watchdog  # noqa: E402
    assert weekly_send_poll.WATCHDOG_JOB_SLUG in watchdog.TRACKED_JOBS


# ---- scheduled-window boundary + parse + stamp ---------------------------


def test_is_scheduled_window_exact_boundary_is_inclusive():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    pac = ZoneInfo("America/Los_Angeles")
    assert _is_scheduled_window(datetime(2026, 6, 1, 7, 0, 0, tzinfo=pac), "MON 07:00") is True
    assert _is_scheduled_window(datetime(2026, 6, 1, 6, 59, 59, tzinfo=pac), "MON 07:00") is False


@pytest.mark.parametrize("spec", ["", "GARBAGE", "MON", "XYZ 07:00", "MON 7", "MON ab:cd"])
def test_parse_scheduled_spec_defaults_on_malformed(spec):
    wd, t = weekly_send_poll._parse_scheduled_spec(spec)
    assert (wd, t.hour, t.minute) == (0, 7, 0)  # default MON 07:00


def test_stamp_approval_non_fatal_on_error(mocker):
    """A stamp write failure must NEVER raise (it would block a verified send)."""
    mocker.patch.object(send_poll_core.smartsheet_client, "update_rows", side_effect=RuntimeError("boom"))
    log = mocker.patch.object(send_poll_core.error_log, "log")
    v = ApprovalVerdict(verified=True, reason=VerdictReason.AUTHORIZED,
                        actor="a@b.com", modified_at="2026-06-01T09:00:00-07:00")
    weekly_send_poll._stamp_approval(5, v)  # must NOT raise
    assert any(c.kwargs.get("error_code") == "weekly_send_poll.stamp_failed" for c in log.call_args_list)
