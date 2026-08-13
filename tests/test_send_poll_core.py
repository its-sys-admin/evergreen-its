"""Unit tests for safety_reports/send_poll_core.py — the parameterized dispatch
core (P1c). Covers the no-default contamination gate + the positive proof that the
core dispatches against the CONFIG's ids/columns (never a hardcoded safety value).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from safety_reports import send_poll_core, weekly_send, weekly_send_poll, wsr_review
from safety_reports.send_poll_core import DaemonConfig
from safety_reports.weekly_send import SendResult
from shared import sheet_ids
from shared.approval_verification import ApprovalVerdict, VerdictReason


def _base_kwargs() -> dict:
    """Minimal VALID DaemonConfig kwargs (progress-shaped ids); override per test."""
    return dict(
        script_name="test.send_poll",
        config_workstream="test_ws",
        daemon_name="test.send_poll",
        lock_path=Path("/tmp/_t.lock"),
        watchdog_marker_dir=Path("/tmp/_wd"),
        watchdog_job_slug="test_slug",
        cfg_polling_enabled="test.polling_enabled",
        default_polling_enabled=True,
        cfg_scheduled_send_local="test.scheduled_send_local",
        default_scheduled_send_local="MON 07:00",
        send_tz="America/Los_Angeles",
        poll_sheet_id=9999,
        f22_workspace_id=8888,
        col_send_now="P Send Now",
        col_approve_scheduled="P Approve",
        col_send_status="P Status",
        col_notes="P Notes",
        col_approved_by="P Approved By",
        col_approved_at="P Approved At",
        dispatch_statuses=frozenset({"PENDING", "FAILED"}),
        status_pending="PENDING",
        status_failed="FAILED",
        max_send_retries=3,
        parse_retry_count=lambda n: 0,
        to_datetime=lambda d: "2026-06-01",
        wake_reasons=frozenset({VerdictReason.UNAUTHORIZED_ACTOR}),
        send_fn=lambda row_id: SendResult(status="sent", row_id=row_id),
    )


def _cfg(**overrides) -> DaemonConfig:
    return DaemonConfig(**{**_base_kwargs(), **overrides})


# ---- no-default contamination gate ---------------------------------------


def test_daemonconfig_rejects_all_missing():
    with pytest.raises(TypeError):
        DaemonConfig()  # type: ignore[call-arg]


@pytest.mark.parametrize("missing", ["send_fn", "poll_sheet_id", "f22_workspace_id", "col_send_now"])
def test_daemonconfig_rejects_a_missing_required_field(missing):
    kw = _base_kwargs()
    del kw[missing]
    with pytest.raises(TypeError):
        DaemonConfig(**kw)  # type: ignore[arg-type]


def test_post_init_rejects_non_callable_send_fn():
    with pytest.raises(TypeError):
        _cfg(send_fn="not-callable")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_id", [0, -1])
def test_post_init_rejects_non_positive_sheet_or_workspace(bad_id):
    with pytest.raises(ValueError):
        _cfg(poll_sheet_id=bad_id)
    with pytest.raises(ValueError):
        _cfg(f22_workspace_id=bad_id)


def test_post_init_rejects_sending_in_dispatch_statuses():
    # The load-bearing no-double-send exclusion — SENDING must never dispatch.
    with pytest.raises(ValueError):
        _cfg(dispatch_statuses=frozenset({"PENDING", "SENDING"}))


# ---- positive contamination proof ----------------------------------------


def test_core_dispatches_against_config_ids_not_a_safety_default(mocker):
    """A progress-shaped config drives the WHOLE cycle against ITS ids/columns —
    no hardcoded safety sheet/workspace leaks in."""
    sent: list[int] = []
    cfg = _cfg(send_fn=lambda rid: (sent.append(rid), SendResult(status="sent", row_id=rid))[1])

    get_rows = mocker.patch(
        "safety_reports.send_poll_core.smartsheet_client.get_rows",
        return_value=[{"_row_id": 1, "P Send Now": True, "P Status": "PENDING"}],
    )
    shares = mocker.patch(
        "safety_reports.send_poll_core.smartsheet_client.list_workspace_share_emails",
        return_value=frozenset({"a@b.com"}),
    )
    mocker.patch(
        "safety_reports.send_poll_core.smartsheet_client.get_setting",
        side_effect=send_poll_core.smartsheet_client.SmartsheetNotFoundError("stub"),
    )
    verify = mocker.patch(
        "safety_reports.send_poll_core.approval_verification.verify_approval",
        return_value=ApprovalVerdict(verified=True, reason=VerdictReason.AUTHORIZED, actor="a@b.com"),
    )
    mocker.patch("safety_reports.send_poll_core.error_log.log")

    result = send_poll_core.poll_inside_lock(
        cfg,
        write_liveness=lambda: None,
        write_row=lambda **k: None,
        write_watchdog_marker=lambda: None,
        stamp_approval=lambda rid, v: None,
        is_scheduled_window=lambda now, spec: True,
    )

    get_rows.assert_called_once_with(9999)              # the PROGRESS sheet, not WSR
    shares.assert_called_once_with(8888)                # the PROGRESS workspace, not WORKSPACE_SAFETY_PORTAL
    assert verify.call_args.args[0] == 9999             # F22 against the progress sheet
    assert verify.call_args.args[2] == "P Send Now"     # the progress column
    assert sent == [1] and result.sent == 1


# ---- safety binding sanity (the entry's CONFIG) --------------------------


def test_safety_config_binds_safety_values():
    c = weekly_send_poll.CONFIG
    assert c.poll_sheet_id == sheet_ids.SHEET_WSR_HUMAN_REVIEW
    assert c.f22_workspace_id == sheet_ids.WORKSPACE_SAFETY_PORTAL
    assert c.col_send_now == wsr_review.COL_SEND_NOW
    assert "SENDING" not in {s.upper() for s in c.dispatch_statuses}
    assert c.dispatch_statuses == frozenset({weekly_send.STATUS_PENDING, weekly_send.STATUS_FAILED})
    # send_fn late-binds weekly_send.send_one_row (patchable) — call shape (row_id, cfg).
    rec: list = []
    import safety_reports.weekly_send as ws

    def _capture(rid, cfg):
        rec.append((rid, cfg))
        return SendResult(status="sent", row_id=rid)

    orig = ws.send_one_row
    ws.send_one_row = _capture
    try:
        c.send_fn(42)
    finally:
        ws.send_one_row = orig
    assert rec == [(42, weekly_send.CONFIG)]


# ---- config-read transient fence (error-hygiene 2026-07-19) ---------------


def test_cycle_config_read_transient_error_falls_open_with_warn(mocker):
    """A generic SmartsheetError from get_setting (read-timeout / 5xx) during the cycle's
    scheduled-window config read must NOT escape to @its_error_log as a spurious CRITICAL:
    WARN `config_read_error` + the fallback spec, same disposition as the circuit-open
    branch. (_load_authorized_approvers — the F22 security gate — stays fail-CLOSED and is
    untouched by this fence.)"""
    cfg = _cfg()
    mocker.patch(
        "safety_reports.send_poll_core.smartsheet_client.get_rows",
        # Approve-scheduled checked (a dispatch candidate), no Send Now → the scheduled
        # path consults is_scheduled_window with the RESOLVED spec.
        return_value=[{"_row_id": 1, "P Approve": True, "P Status": "PENDING"}],
    )
    mocker.patch(
        "safety_reports.send_poll_core.smartsheet_client.list_workspace_share_emails",
        return_value=frozenset({"a@b.com"}),
    )
    mocker.patch(
        "safety_reports.send_poll_core.smartsheet_client.get_setting",
        side_effect=send_poll_core.smartsheet_client.SmartsheetError("read timeout"),
    )
    log = mocker.patch("safety_reports.send_poll_core.error_log.log")

    seen_specs: list[str] = []

    def _window(now, spec):
        seen_specs.append(spec)
        return False  # outside the window → row skipped, cycle completes

    result = send_poll_core.poll_inside_lock(
        cfg,
        write_liveness=lambda: None,
        write_row=lambda **k: None,
        write_watchdog_marker=lambda: None,
        stamp_approval=lambda rid, v: None,
        is_scheduled_window=_window,
    )  # must not raise

    codes = [kw.get("error_code") for _, kw in log.call_args_list]
    assert "config_read_error" in codes
    warn_calls = [
        (a, kw) for a, kw in log.call_args_list if kw.get("error_code") == "config_read_error"
    ]
    assert all(a[0] == send_poll_core.Severity.WARN for a, _ in warn_calls)
    assert seen_specs == [cfg.default_scheduled_send_local]  # fallback used
    assert result.skipped == 1 and result.errors == 0


# ---- F22 approver-read transient fence (2026-07-21, operator decision D1) ----
#
# THE INVARIANT UNDER TEST, stated once: an approver-load failure of ANY class performs
# ZERO sends. The 2026-07-21 change softened the SEVERITY of one precisely-typed class
# (transient), not the gate. Each test below asserts send_fn was NEVER called — proof, not
# inspection — because this is an External-Send-Gate-adjacent surface.


def _run(cfg, **seams):
    defaults = dict(
        write_liveness=lambda: None,
        write_row=lambda **k: None,
        write_watchdog_marker=lambda: None,
        stamp_approval=lambda rid, v: None,
        is_scheduled_window=lambda now, spec: True,
    )
    return send_poll_core.poll_inside_lock(cfg, **{**defaults, **seams})


@pytest.fixture
def approver_scenario(mocker, tmp_path):
    """A cycle with one dispatchable row, everything mocked except the approver read.

    `lock_path` points at tmp_path, so the fences' state files land there too (they are
    derived from `lock_path.parent`) and never touch live state.
    """
    sent: list[int] = []
    cfg = _cfg(
        lock_path=tmp_path / "t.lock",
        send_fn=lambda rid: (sent.append(rid), SendResult(status="sent", row_id=rid))[1],
    )
    mocker.patch(
        "safety_reports.send_poll_core.smartsheet_client.get_rows",
        return_value=[{"_row_id": 1, "P Send Now": True, "P Status": "PENDING"}],
    )
    mocker.patch(
        "safety_reports.send_poll_core.smartsheet_client.get_setting",
        side_effect=send_poll_core.smartsheet_client.SmartsheetNotFoundError("stub"),
    )
    mocker.patch(
        "safety_reports.send_poll_core.approval_verification.verify_approval",
        return_value=ApprovalVerdict(
            verified=True, reason=VerdictReason.AUTHORIZED, actor="a@b.com"
        ),
    )
    shares = mocker.patch(
        "safety_reports.send_poll_core.smartsheet_client.list_workspace_share_emails"
    )
    log = mocker.patch("safety_reports.send_poll_core.error_log.log")
    return {"cfg": cfg, "sent": sent, "shares": shares, "log": log, "tmp": tmp_path}


def _codes(log):
    return [kw.get("error_code") for _, kw in log.call_args_list]


def test_transient_approver_read_halts_with_error_and_zero_sends(approver_scenario):
    """The 05:36Z progress_send_poll signature: a ReadTimeout inside
    list_workspace_share_emails used to escape as CRITICAL uncaught_exception."""
    s = approver_scenario
    s["shares"].side_effect = send_poll_core.smartsheet_client.SmartsheetTransientError(
        "ReadTimeout"
    )

    result = _run(s["cfg"])

    assert s["sent"] == []                       # FAIL-CLOSED, the load-bearing assertion
    assert result.errors == 1 and result.sent == 0
    severities = [a[0] for a, kw in s["log"].call_args_list
                  if kw.get("error_code") == "test.send_poll.approver_read_transient"]
    assert severities == [send_poll_core.Severity.ERROR]
    assert send_poll_core.Severity.CRITICAL not in [a[0] for a, _ in s["log"].call_args_list]


@pytest.mark.parametrize(
    "exc",
    [
        send_poll_core.smartsheet_client.SmartsheetAuthError("401"),
        send_poll_core.smartsheet_client.SmartsheetPermissionError("403"),
        send_poll_core.smartsheet_client.SmartsheetRateLimitError("429"),
        RuntimeError("a genuine bug"),
    ],
)
def test_non_transient_approver_read_propagates_with_zero_sends(approver_scenario, exc):
    """Fail-closed for the OTHER failure class too: it re-raises (→ @its_error_log
    CRITICAL at the daemon entry) and still dispatches nothing."""
    s = approver_scenario
    s["shares"].side_effect = exc

    with pytest.raises(type(exc)):
        _run(s["cfg"])

    assert s["sent"] == []


def test_sustained_approver_read_escalates_at_three_cycles(approver_scenario):
    """Threshold 3, not the shared 5 — every send poller is 15-minute cadence (D2)."""
    s = approver_scenario
    s["shares"].side_effect = send_poll_core.smartsheet_client.SmartsheetTransientError("HTTP 500")

    for _ in range(3):
        _run(s["cfg"])

    sustained = [a for a, kw in s["log"].call_args_list
                 if kw.get("error_code") == "test.send_poll.approver_read_sustained"]
    assert len(sustained) == 1
    assert sustained[0][0] == send_poll_core.Severity.CRITICAL
    assert s["sent"] == []


def test_circuit_open_approver_read_warns_uncounted_with_zero_sends(approver_scenario):
    s = approver_scenario
    s["shares"].side_effect = send_poll_core.smartsheet_client.SmartsheetCircuitOpenError("open")

    for _ in range(5):
        _run(s["cfg"])

    severities = [a[0] for a, kw in s["log"].call_args_list
                  if kw.get("error_code") == "test.send_poll.approver_read_transient"]
    assert severities == [send_poll_core.Severity.WARN] * 5
    assert not (s["tmp"] / "test.send_poll_approver_read_failures.json").exists()
    assert s["sent"] == []


def test_successful_approver_read_clears_the_counter(approver_scenario):
    s = approver_scenario
    s["shares"].side_effect = [
        send_poll_core.smartsheet_client.SmartsheetTransientError("HTTP 500"),
        send_poll_core.smartsheet_client.SmartsheetTransientError("HTTP 500"),
        frozenset({"a@b.com"}),
        send_poll_core.smartsheet_client.SmartsheetTransientError("HTTP 500"),
        send_poll_core.smartsheet_client.SmartsheetTransientError("HTTP 500"),
    ]

    for _ in range(5):
        _run(s["cfg"])

    # The good cycle in the middle resets the ladder — 2 + 2 never reaches 3.
    assert "test.send_poll.approver_read_sustained" not in _codes(s["log"])
    assert s["sent"] == [1]  # only the healthy cycle dispatched


# ---- review-sheet read fence ----------------------------------------------


def test_transient_review_read_halts_with_error(approver_scenario, mocker):
    s = approver_scenario
    mocker.patch(
        "safety_reports.send_poll_core.smartsheet_client.get_rows",
        side_effect=send_poll_core.smartsheet_client.SmartsheetTransientError("HTTP 503"),
    )

    result = _run(s["cfg"])

    assert result.errors == 1 and s["sent"] == []
    assert "test.send_poll.review_read_transient" in _codes(s["log"])


def test_non_transient_review_read_now_propagates(approver_scenario, mocker):
    """Behaviour CHANGE, deliberate: a 404 on the review sheet (deleted / wrong id) used
    to log the same ERROR forever. It will not self-heal, so it must page."""
    s = approver_scenario
    mocker.patch(
        "safety_reports.send_poll_core.smartsheet_client.get_rows",
        side_effect=send_poll_core.smartsheet_client.SmartsheetNotFoundError("404"),
    )

    with pytest.raises(send_poll_core.smartsheet_client.SmartsheetNotFoundError):
        _run(s["cfg"])

    assert s["sent"] == []


@pytest.mark.parametrize("site", ["review_read", "approver_read"])
def test_a_propagating_read_failure_still_reports_liveness_first(
    approver_scenario, mocker, site
):
    """One root cause must yield ONE alert, not two.

    On origin/main every SmartsheetError at these sites refreshed the Check-C watchdog
    marker and wrote an ITS_Daemon_Health row before returning. Re-raising ahead of those
    writes means a persistent 404 / revoked token ALSO trips watchdog Check C (marker
    staleness) and leaves the health row stale — so the operator gets a second,
    MISLEADING "this daemon is dead" CRITICAL layered on the real one, aiming the §43
    repair at a daemon that is alive and correctly refusing to work."""
    s = approver_scenario
    exc = send_poll_core.smartsheet_client.SmartsheetNotFoundError("404")
    if site == "review_read":
        mocker.patch(
            "safety_reports.send_poll_core.smartsheet_client.get_rows", side_effect=exc
        )
    else:
        s["shares"].side_effect = exc
    marker = mocker.Mock()
    rows = mocker.Mock()
    liveness = mocker.Mock()

    with pytest.raises(send_poll_core.smartsheet_client.SmartsheetNotFoundError):
        _run(s["cfg"], write_liveness=liveness, write_row=rows,
             write_watchdog_marker=marker)

    liveness.assert_called_once()
    marker.assert_called_once()
    rows.assert_called_once()
    assert rows.call_args.kwargs["status"] == "ERROR"
    assert s["sent"] == []  # fail-closed is untouched


def test_recovered_retries_are_summarized_once_per_pass(approver_scenario, mocker):
    s = approver_scenario
    s["shares"].return_value = frozenset({"a@b.com"})
    mocker.patch.object(
        send_poll_core.sustained_failure.smartsheet_client, "drain_retry_recovery",
        return_value={"get_rows": {"sequences": 1, "attempts": 2}},
    )

    _run(s["cfg"])

    assert _codes(s["log"]).count("smartsheet_retry_recovered") == 1


# ---- malformed scheduled window: fail OPEN, but never SILENT (2026-08-13) -----
#
# `_parse_scheduled_spec` swallowed every parse failure and returned MON 07:00 with no log, no
# error_code, and no way to tell a typo from a deliberate Monday setting. This engine is shared by
# ALL FIVE external-send daemons (weekly_send, progress_send, po_send, rfq_send, subcontract_send),
# so a mistyped window silently moved that lane's approval window. It still falls back — wedging a
# send lane on a bad string would be worse — but it now says so.


@pytest.mark.parametrize(
    "spec,expect_reason_fragment",
    [
        ("", "empty value"),
        ("   ", "empty value"),
        ("MON", "expected '<DAY> <HH:MM>'"),
        ("MON 07:00 EXTRA", "expected '<DAY> <HH:MM>'"),
        ("MONDAY 07:00", "unknown weekday"),
        ("XYZ 07:00", "unknown weekday"),
        ("MON 0700", "malformed time"),
        ("MON 25:00", "malformed time"),
        ("MON aa:bb", "malformed time"),
    ],
)
def test_a_malformed_window_reports_why_and_still_falls_back(spec, expect_reason_fragment):
    wd, tod, reason = send_poll_core.parse_scheduled_spec_checked(spec)
    assert (wd, tod) == send_poll_core.DEFAULT_SCHEDULED_WINDOW, "must still fail OPEN"
    assert expect_reason_fragment in reason, f"reason {reason!r} does not name the fault"


@pytest.mark.parametrize("spec", ["MON 07:00", "fri 14:30", "  SUN 23:59  "])
def test_a_well_formed_window_reports_no_reason(spec):
    _, _, reason = send_poll_core.parse_scheduled_spec_checked(spec)
    assert reason == "", f"clean spec reported a fault: {reason!r}"


def test_a_malformed_window_is_warned_once_naming_the_lane_and_the_fallback(mocker):
    """The WARN must identify WHICH lane and WHAT window is actually in effect.

    Without the lane's own config key an operator with five send daemons cannot tell which
    ITS_Config cell to fix.
    """
    log = mocker.patch.object(send_poll_core.error_log, "log")
    config = mocker.Mock()
    config.script_name = "weekly_send_poll"
    config.cfg_scheduled_send_local = "safety_reports.weekly_send.scheduled_send_local"

    send_poll_core.warn_if_scheduled_spec_malformed(config, "TEU 09:00")

    log.assert_called_once()
    kwargs, args = log.call_args.kwargs, log.call_args.args
    assert kwargs.get("error_code") == "scheduled_send_window_malformed"
    message = " ".join(str(a) for a in args)
    assert "safety_reports.weekly_send.scheduled_send_local" in message, "must name the cell"
    assert "MON 07:00" in message, "must name the window actually in effect"


def test_a_well_formed_window_logs_nothing(mocker):
    log = mocker.patch.object(send_poll_core.error_log, "log")
    config = mocker.Mock()
    config.script_name = "weekly_send_poll"
    config.cfg_scheduled_send_local = "safety_reports.weekly_send.scheduled_send_local"

    send_poll_core.warn_if_scheduled_spec_malformed(config, "TUE 09:00")

    log.assert_not_called()
