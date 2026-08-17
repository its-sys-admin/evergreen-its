"""Unit tests for progress_reports/progress_send_poll.py — the P5 PROGRESS dispatcher.

The dispatch body lives in `safety_reports/send_poll_core.py` (parameterized by
`DaemonConfig`) and is exhaustively tested by tests/test_send_poll_core.py +
tests/test_weekly_send_poll.py. These tests pin the thin PROGRESS entry's BINDING — it
polls the WPR sheet, gates F22 against the Progress Reporting workspace, and dispatches
through `progress_send.send_one_row` — plus the no-double-send SENDING exclusion.

Data-plane mocks target `send_poll_core.*`; the heartbeat / watchdog / stamp / window
SEAMS stay patched on the entry (the core resolves them by injection from the entry).
"""
from __future__ import annotations

from typing import Any

import pytest

from progress_reports import progress_send_poll, wpr_review
from progress_reports.progress_send_poll import (
    _filter_dispatch_candidates,
    poll_once,
)
from safety_reports import send_poll_core
from safety_reports.weekly_send import SendResult
from shared import sheet_ids
from shared.approval_verification import ApprovalVerdict, VerdictReason


def _row(
    *, row_id: int, send_now: bool = True, scheduled: bool = False,
    send_status: str = wpr_review.STATUS_PENDING, notes: str = "",
) -> dict[str, Any]:
    return {
        "_row_id": row_id,
        wpr_review.COL_JOB_PROJECT: "Solar Ridge",
        wpr_review.COL_JOB_ID: "JOB-9",
        wpr_review.COL_WEEK_OF: "2026-06-26",
        wpr_review.COL_SEND_NOW: send_now,
        wpr_review.COL_APPROVE_SCHEDULED: scheduled,
        wpr_review.COL_SEND_STATUS: send_status,
        wpr_review.COL_NOTES: notes,
    }


def _get_setting_stub(key: str, *, workstream: str) -> str:
    """ITS_Config stub: the polling gate reads ENABLED; every other key falls back.

    Stubbed EXPLICITLY rather than left to the module default, because
    `DEFAULT_POLLING_ENABLED` is now False (a send gate never fails open) — this suite
    previously leaned on the fail-open default to get past the gate, which is the very
    property being removed. Non-gate keys still raise NotFound so their fallbacks stay
    exercised.
    """
    if key == progress_send_poll.CFG_POLLING_ENABLED:
        return "true"
    raise send_poll_core.smartsheet_client.SmartsheetNotFoundError("default test stub")


@pytest.fixture
def _patch_all(mocker):
    return {
        "get_rows": mocker.patch("safety_reports.send_poll_core.smartsheet_client.get_rows", return_value=[]),
        "send_one_row": mocker.patch(
            "progress_reports.progress_send.send_one_row",  # CONFIG.send_fn late-binds here
            return_value=SendResult(status="sent", row_id=0, project_name="Solar Ridge"),
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
        "write_heartbeat": mocker.patch("progress_reports.progress_send_poll._write_heartbeat", return_value=None),
        "write_heartbeat_row": mocker.patch("progress_reports.progress_send_poll._write_heartbeat_row", return_value=None),
        "write_watchdog_marker": mocker.patch("progress_reports.progress_send_poll._write_watchdog_marker", return_value=None),
        "stamp": mocker.patch("progress_reports.progress_send_poll._stamp_approval", return_value=None),
    }


# ---- binding correctness -------------------------------------------------


def test_config_polls_wpr_and_gates_f22_against_progress_workspace():
    cfg = progress_send_poll.CONFIG
    assert cfg.poll_sheet_id == sheet_ids.SHEET_WPR_HUMAN_REVIEW
    assert cfg.f22_workspace_id == sheet_ids.WORKSPACE_PROGRESS_REPORTING
    assert cfg.config_workstream == "progress_reports"
    assert cfg.daemon_name == "progress_reports.progress_send_poll"


def test_sending_excluded_from_dispatch_statuses():
    # The load-bearing no-double-send exclusion (also enforced by DaemonConfig.__post_init__).
    assert wpr_review.STATUS_SENDING not in progress_send_poll.DISPATCH_STATUSES
    assert progress_send_poll.DISPATCH_STATUSES == frozenset(
        {wpr_review.STATUS_PENDING, wpr_review.STATUS_FAILED}
    )


def test_default_polling_enabled_is_false_fail_safe():
    # HOUSE_REFLEXES §5: a send daemon's row-absent default must be dark (False), never
    # fail-open to SENDING. Pins the constant AND its propagation into the DaemonConfig.
    assert progress_send_poll.DEFAULT_POLLING_ENABLED is False
    assert progress_send_poll.CONFIG.default_polling_enabled is False


@pytest.mark.parametrize(
    "exc",
    [
        send_poll_core.smartsheet_client.SmartsheetNotFoundError("row absent"),
        send_poll_core.smartsheet_client.SmartsheetCircuitOpenError("breaker open"),
        send_poll_core.smartsheet_client.SmartsheetError("5xx"),
    ],
)
def test_unreadable_polling_config_skips_the_cycle_without_sending(_patch_all, exc):
    # PROVE-IT-BITES, across ALL THREE `_read_str_setting` fallback branches (the NotFound
    # one logs nothing at all): with the gate row unreadable, the cycle short-circuits
    # fail-safe instead of dispatching an approved send_now row to the CUSTOMER.
    _patch_all["get_setting"].side_effect = exc
    _patch_all["get_rows"].return_value = [_row(row_id=90)]
    stats = poll_once()
    assert stats.skipped_disabled is True
    assert stats.dispatched == 0 and stats.sent == 0
    _patch_all["send_one_row"].assert_not_called()


# ---- filter (delegates to the shared core) -------------------------------


def test_filter_requires_an_approval_checkbox():
    rows = [_row(row_id=1, send_now=True), _row(row_id=2, send_now=False, scheduled=False)]
    assert [r["_row_id"] for r in _filter_dispatch_candidates(rows)] == [1]


def test_filter_skips_sent():
    rows = [_row(row_id=1, send_status=wpr_review.STATUS_SENT), _row(row_id=2)]
    assert [r["_row_id"] for r in _filter_dispatch_candidates(rows)] == [2]


def test_filter_skips_sending_no_double_send():
    rows = [_row(row_id=1, send_status=wpr_review.STATUS_SENDING), _row(row_id=2)]
    assert [r["_row_id"] for r in _filter_dispatch_candidates(rows)] == [2]


# ---- a happy poll cycle dispatches through progress_send ------------------


def test_poll_dispatches_candidate_through_progress_send(_patch_all):
    _patch_all["get_rows"].return_value = [_row(row_id=5)]
    stats = poll_once()
    # Dispatched via progress_send.send_one_row (the bound send_fn), row id forwarded.
    _patch_all["send_one_row"].assert_called_once_with(5)
    assert stats.dispatched == 1 and stats.sent == 1


def test_poll_loads_approvers_from_progress_workspace(_patch_all):
    _patch_all["get_rows"].return_value = [_row(row_id=5)]
    poll_once()
    # F22 approver authority = the PROGRESS workspace's membership, never safety's.
    _patch_all["workspace_shares"].assert_called_once_with(
        sheet_ids.WORKSPACE_PROGRESS_REPORTING
    )


def test_poll_writes_watchdog_marker(_patch_all):
    poll_once()
    _patch_all["write_watchdog_marker"].assert_called_once()


# NOTE: no `WATCHDOG_JOB_SLUG in watchdog.TRACKED_JOBS` assertion here — registering the
# progress_send_poll + progress_weekly_generate slugs in TRACKED_JOBS (Check-C staleness)
# is the P5 watchdog slice, deferred exactly as P4 deferred progress_weekly_generate. The
# daemon writes the marker now; Check-C begins monitoring it once that slice lands.
