"""Tests for scripts/watchdog.py.

`scripts/` is not a Python package (per pyproject.toml comment); we use
the same sys.path-insert pattern as tests/test_migration_import_hygiene.py
so `import watchdog` resolves the script as a top-level module.

All Smartsheet, Resend, and Sentry boundaries are mocked. LOG_DIR is
redirected to a per-test tmp_path so the decorator's started/completed
INFO lines don't pollute ~/its/logs/.

Run with: pytest -q tests/test_watchdog.py
"""
from __future__ import annotations

import inspect
import json
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared.error_log import Severity
from shared.kill_switch import SystemState
from shared.smartsheet_client import SmartsheetError

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import watchdog  # noqa: E402  — must come after sys.path insertion above

# Captured at import time, BEFORE the autouse fixture below patches the attr —
# the location-pin test asserts the REAL path.
_REAL_RESULTS_PATH = watchdog.WATCHDOG_RESULTS_PATH


@pytest.fixture(autouse=True)
def _sweep_results_to_tmp(tmp_path, monkeypatch):
    """No watchdog test may ever write the LIVE ~/its/state results file —
    main() persists the sweep at the end of every run, so redirect it for the
    whole module (the write itself is exercised against tmp)."""
    monkeypatch.setattr(
        watchdog, "WATCHDOG_RESULTS_PATH", tmp_path / "watchdog_results.json"
    )


@pytest.fixture(autouse=True)
def isolate_error_log(tmp_path, monkeypatch, mocker):
    """Autouse: route error_log filesystem + side-channel writes to mocks.

    The watchdog's `main()` is wrapped in `@its_error_log`, which emits
    INFO started/completed lines through `shared.error_log.log`. Those go
    to a local file (redirected here) and skip Smartsheet/Resend/Sentry
    by default (INFO is env-gated and triple-fire is CRITICAL-only). On
    a CRITICAL path (e.g., main raises), the side channels fire — mock
    those so tests are hermetic.

    Also redirects `watchdog.WATCHDOG_MARKER_DIR` to tmp_path so
    `write_last_run_marker("watchdog")` at the end of main() doesn't
    write into the operator's real ~/its/.watchdog/.
    """
    monkeypatch.setattr("shared.error_log.LOG_DIR", tmp_path)
    monkeypatch.setattr("watchdog.WATCHDOG_MARKER_DIR", tmp_path / ".watchdog")
    mocker.patch("shared.error_log.smartsheet_client.add_rows")
    mocker.patch("shared.resend_client.send_alert")
    mocker.patch("shared.sentry_client.capture_exception")
    import shared.error_log as el
    el._in_smartsheet_write = False
    el._in_resend_alert = False
    el._in_sentry_capture = False
    yield
    el._in_smartsheet_write = False
    el._in_resend_alert = False
    el._in_sentry_capture = False


@pytest.fixture
def mock_log(mocker):
    """Mock `watchdog.log` — captures main's preamble + _run_check routing.

    Does NOT capture the @its_error_log decorator's started/completed lines
    (those bind to `shared.error_log.log`, not `watchdog.log`).
    """
    return mocker.patch("watchdog.log")


@pytest.fixture
def mock_check_state(mocker):
    return mocker.patch("watchdog.check_system_state")


@pytest.fixture
def mock_get_pending(mocker):
    return mocker.patch("watchdog.review_queue.get_pending")


@pytest.fixture
def mock_is_past_sla(mocker):
    return mocker.patch("watchdog.review_queue.is_past_sla")


@pytest.fixture
def mock_get_rows(mocker):
    return mocker.patch("watchdog.smartsheet_client.get_rows")


@pytest.fixture
def mock_get_setting(mocker):
    """Mock the ITS_Config read main() uses for the F16 heartbeat URL."""
    return mocker.patch("watchdog.smartsheet_client.get_setting")


@pytest.fixture
def mock_ping(mocker):
    """Mock the outbound heartbeat beacon so main() makes no real GET."""
    return mocker.patch("watchdog.heartbeat_client.ping")


# ---- Group A: module shape ----------------------------------------------


def test_checks_list_has_all_session_1_2_3_checks():
    """CHECKS registry in priority order. Check E is deferred to its
    shipping PR (Admin API key prerequisite) and intentionally absent
    here. Check G (alert-dedupe summary sweep) shipped in PR β
    (Session 3). Check I (weekly_generate catch-up) is registered last and
    runs after Check C (it recovers what Check C only detects). There is no
    Check H — the marker-file Check C is the staleness floor doctrine once
    named "Check H" (2026-06-01 doctrine correction). Checks J (circuit-breaker
    prolonged-open page) and K (guaranteed F09 cap-window summary sweep) shipped
    in F08/F09 PR 2. Check L (token write-capability probe) shipped in B2.
    Check N (WSR rows stuck in SENDING) is the weekly_send write-ahead-marker
    safety net. Check O (A5, growth Slice 1) is the ITS_Errors +
    ITS_Review_Queue row-cap rotation. Check P (A3) is the Box OAuth
    refresh-token freshness probe; Checks Q/R (A4) are the portal_poll
    fetch-outage + unfiled-backlog probes. Check S
    (origin/main required-CI-green detector) shipped from the 2026-06-28 forensic
    lessons-learned (class #13, partial-PR-landed)."""
    assert watchdog.CHECKS == [
        watchdog._check_stale_review_queue,
        watchdog._check_open_criticals,
        watchdog._check_scheduled_jobs,
        watchdog._check_reviewer_chain_forward,
        # _check_mail_intake_silent_disable RETIRED 2026-06-05 (safety email intake retired).
        watchdog._check_alert_dedupe_summaries,
        watchdog._check_weekly_generate_catchup,
        watchdog._check_progress_generate_catchup,  # Check I (progress, P5) — progress compile catch-up
        watchdog._check_circuit_breaker_prolonged_open,
        watchdog._check_alert_rate_cap_window,
        watchdog._check_token_write_capability,  # Check L (B2)
        watchdog._check_blueprint_guard_symlinks,  # Check M (C3)
        watchdog._check_stuck_wsr_send,  # Check N (WSR write-ahead-marker safety net)
        watchdog._check_row_cap_rotation,  # Check O (A5, growth Slice 1) — row-cap rotation
        watchdog._check_box_token_freshness,  # Check P (A3) — Box OAuth freshness
        watchdog._check_portal_poll_fetch_outage,  # Check Q (A4 fetch-outage escalation)
        watchdog._check_portal_poll_pending_backlog,  # Check R (A4 unfiled-backlog)
        watchdog._check_main_branch_ci_green,  # Check S — origin/main required CI green (class #13)
        watchdog._check_stale_held_rows,  # Check T (P5) — WSR+WPR HELD-row staleness backstop
        watchdog._check_approver_drift,  # Check U (P5) — send-workspace approver-set drift/empty
        watchdog._check_portal_prune_health,  # Check V (GS2) — D1 prune heartbeat
        # Check W (growth Slice 2) — ~/its/logs directory-growth bound (archive, never
        # delete). Registered LAST on purpose: the only filesystem-walking + gzipping
        # check, self-limited by its own per-run cap + monotonic deadline, so an overrun
        # can never delay an alerting check ahead of it.
        watchdog._check_stale_job_archives,  # Check X (#25) — stopped / never-picked-up job archives
        watchdog._check_log_dir_rotation,  # Check W (growth Slice 2) — log-dir archive bound
        watchdog._check_cutover_config,  # Check Y (#27) — verify_cutover VC-03, run daily
    ]


# ---- Check L: token write-capability probe (B2) --------------------------


def test_token_write_capability_ok(mocker):
    mocker.patch("watchdog.smartsheet_client.verify_write_capability", return_value=55)
    delete = mocker.patch("watchdog.smartsheet_client.delete_sheet_settling")
    result = watchdog._check_token_write_capability()
    assert result.severity is Severity.INFO
    delete.assert_called_once_with(55)  # throwaway probe sheet cleaned up (with retry)


def test_token_write_capability_critical_on_write_error(mocker):
    mocker.patch(
        "watchdog.smartsheet_client.verify_write_capability",
        side_effect=watchdog.smartsheet_client.SmartsheetWriteCapabilityError(
            "read-only token"
        ),
    )
    result = watchdog._check_token_write_capability()
    assert result.severity is Severity.CRITICAL  # _run_check pages this (post-A3)
    assert "cannot write" in result.summary


def test_token_write_capability_skips_on_breaker_open(mocker):
    # A Smartsheet OUTAGE is not a token verdict — INFO-skip, never CRITICAL.
    mocker.patch(
        "watchdog.smartsheet_client.verify_write_capability",
        side_effect=watchdog.smartsheet_client.SmartsheetCircuitOpenError("open"),
    )
    result = watchdog._check_token_write_capability()
    assert result.severity is Severity.INFO
    assert "breaker OPEN" in result.summary


def test_token_write_capability_warn_on_delete_failure(mocker):
    # Create proved write capability; a cleanup-delete failure is WARN, not CRITICAL.
    mocker.patch("watchdog.smartsheet_client.verify_write_capability", return_value=77)
    mocker.patch(
        "watchdog.smartsheet_client.delete_sheet_settling",
        side_effect=SmartsheetError("delete boom"),  # all settle retries exhausted
    )
    result = watchdog._check_token_write_capability()
    assert result.severity is Severity.WARN
    assert "77" in result.summary


def test_tracked_jobs_contains_safety_weekly_generate():
    """R3 Session 2 registered safety_weekly_generate as the first
    non-watchdog tracked job. Per-job freshness window is 8 days (it runs
    Friday 14:00 — 1-day-late survives, missed-week surfaces)."""
    assert "safety_weekly_generate" in watchdog.TRACKED_JOBS
    assert watchdog.TRACKED_JOB_WINDOWS["safety_weekly_generate"].days == 8


# test_tracked_jobs_contains_safety_intake + test_safety_intake_slug_matches_intake_poll_module
# REMOVED 2026-06-05: the safety email-intake poller (intake_poll) is RETIRED to a
# tombstone (Safety Portal PULL model supersedes it), so safety_intake is no longer a
# Check-C TRACKED_JOBS entry and the tombstone writes no marker / has no WATCHDOG_JOB_SLUG.


def test_picklist_marker_slugs_match_their_writers():
    """C4 consistency guard (same class as the intake one above): every tracked
    picklist slug must be written by an actual scheduled job, or the watchdog
    tracks a marker nothing writes → permanent false WARN.

    - safety_picklist_audit ← scripts/audit_picklist_drift (weekly plist, C4).
    - safety_picklist_sync  ← scripts/run_picklist_sync (hourly plist, C4 adds
      the marker so the previously-untracked job's death becomes visible).
    """
    import audit_picklist_drift
    import run_picklist_sync

    assert audit_picklist_drift.WATCHDOG_JOB_NAME == "safety_picklist_audit"
    assert audit_picklist_drift.WATCHDOG_JOB_NAME in watchdog.TRACKED_JOBS
    assert run_picklist_sync.WATCHDOG_JOB_NAME == "safety_picklist_sync"
    assert run_picklist_sync.WATCHDOG_JOB_NAME in watchdog.TRACKED_JOBS


def test_portal_poll_marker_slug_matches_writer_and_window():
    """Same Check-C consistency guard for the Safety Portal pull daemon: the
    slug portal_poll writes (safety_portal_poll.last_run) must match the slug
    the watchdog tracks, or Check C either watches a marker nothing writes
    (permanent false WARN) or — worse — a dead puller goes unnoticed.
    Registered at the 2026-06-06 deploy session (previously a deferred
    "future addition"). 5-min window == ~5 poll cycles at the 60s default."""
    from safety_reports import portal_poll

    assert portal_poll.WATCHDOG_JOB_SLUG == "safety_portal_poll"
    assert portal_poll.WATCHDOG_JOB_SLUG in watchdog.TRACKED_JOBS
    assert watchdog.TRACKED_JOB_WINDOWS[portal_poll.WATCHDOG_JOB_SLUG] == timedelta(
        minutes=5
    )


def test_fieldops_sync_marker_slug_matches_writer_and_window():
    """Same Check-C consistency guard for the P2.5 job up-sync dual-sheet mirror
    daemon: the slug fieldops_sync writes (fieldops_sync.last_run) must match the
    slug the watchdog tracks, or Check C either watches a marker nothing writes
    (permanent false WARN) or — worse — a dead mirror daemon goes unnoticed.
    Registered at the 2026-07-01 cutover deploy (the daemon is already loaded +
    live, so it does NOT WARN spuriously). 8-min window == ~5 poll cycles at the
    90s default — same high-frequency-poller tolerance as safety_compile_now_poll."""
    from field_ops import fieldops_sync

    assert fieldops_sync.WATCHDOG_JOB_SLUG == "fieldops_sync"
    assert fieldops_sync.WATCHDOG_JOB_SLUG in watchdog.TRACKED_JOBS
    assert watchdog.TRACKED_JOB_WINDOWS[fieldops_sync.WATCHDOG_JOB_SLUG] == timedelta(
        minutes=8
    )


def test_po_poll_marker_slug_matches_writer_and_window():
    """Same Check-C consistency guard for the Purchase-Order pull daemon (PO S4):
    the slug po_poll writes (po_poll.last_run) must match the slug the watchdog
    tracks, or Check C either watches a marker nothing writes (permanent false
    WARN) or — worse — a dead PO daemon goes unnoticed. Registered at S4 landing;
    like the compile-now/progress entries it WARNs until the operator loads the
    plist AND flips a po_materials.po_poll.* gate (register + activate together).
    8-min window == ~5 poll cycles at the 90s default — same high-frequency-poller
    tolerance as fieldops_sync."""
    from po_materials import po_poll

    assert po_poll.WATCHDOG_JOB_SLUG == "po_poll"
    assert po_poll.WATCHDOG_JOB_SLUG in watchdog.TRACKED_JOBS
    assert watchdog.TRACKED_JOB_WINDOWS[po_poll.WATCHDOG_JOB_SLUG] == timedelta(
        minutes=8
    )


def test_privileged_actuator_marker_slugs_match_writers_and_windows():
    """Same Check-C consistency guard for the TWO privileged actuators.

    These are the highest-consequence entries in TRACKED_JOBS: config_actuator is the §50
    SOLE privileged code-actuator (commit → CI → merge → deploy) and publish_daemon runs the
    same rail for form definitions. Before enrollment NOTHING reported their death — a dead
    actuator is indistinguishable from an empty queue, so privileged actuation just silently
    stops. This asserts each writer's slug is the slug the watchdog tracks (a mismatch means
    Check C watches a marker nothing writes — permanent false WARN — or misses a dead daemon).

    The 90-min window is deliberately NOT the 10-min high-frequency window their 120s
    StartInterval would imply: one cycle can legitimately block for CI (CI_TIMEOUT_S=900s)
    plus deploy, serially per claimed request, and the marker lands only at cycle end. 90 min
    clears each daemon's own STALE_RECLAIM_S ceiling, and costs nothing — Check C is evaluated
    by the once-daily 07:00 watchdog either way.
    """
    from po_materials import config_actuator
    from safety_reports import publish_daemon

    assert config_actuator.WATCHDOG_JOB_SLUG == "config_actuator"
    assert config_actuator.WATCHDOG_JOB_SLUG in watchdog.TRACKED_JOBS
    assert watchdog.TRACKED_JOB_WINDOWS[config_actuator.WATCHDOG_JOB_SLUG] == timedelta(
        minutes=90
    )
    assert publish_daemon.WATCHDOG_JOB_SLUG == "publish_daemon"
    assert publish_daemon.WATCHDOG_JOB_SLUG in watchdog.TRACKED_JOBS
    assert watchdog.TRACKED_JOB_WINDOWS[publish_daemon.WATCHDOG_JOB_SLUG] == timedelta(
        minutes=90
    )
    # The window must exceed the daemon's own in-flight staleness ceiling, or a healthy
    # long actuation false-positives. Derived from the daemons, not hardcoded.
    for mod in (config_actuator, publish_daemon):
        window = watchdog.TRACKED_JOB_WINDOWS[mod.WATCHDOG_JOB_SLUG]
        assert window.total_seconds() > mod.STALE_RECLAIM_S, (
            f"{mod.WATCHDOG_JOB_SLUG}: Check-C window {window} is tighter than its own "
            f"STALE_RECLAIM_S ({mod.STALE_RECLAIM_S}s) — a legitimately in-flight "
            "actuation would fire a false CRITICAL"
        )


def test_privileged_actuator_markers_round_trip(tmp_path, monkeypatch):
    """Each actuator's marker writer produces a parseable ISO timestamp at <slug>.last_run —
    the exact filename Check C stats. The OSError fence is silent, so 'no exception' does not
    prove the write happened; assert the file."""
    from po_materials import config_actuator
    from safety_reports import publish_daemon

    for mod in (config_actuator, publish_daemon):
        d = tmp_path / mod.WATCHDOG_JOB_SLUG
        monkeypatch.setattr(mod, "WATCHDOG_MARKER_DIR", d)
        mod._write_watchdog_marker()
        marker = d / f"{mod.WATCHDOG_JOB_SLUG}.last_run"
        assert marker.exists(), f"{mod.WATCHDOG_JOB_SLUG} wrote no marker"
        datetime.fromisoformat(marker.read_text().strip())  # raises if not valid ISO


def test_run_picklist_sync_write_marker_round_trips(monkeypatch, tmp_path):
    """C4: run_picklist_sync writes a parseable ISO timestamp to its marker
    (fail-soft path mirrors audit_picklist_drift's, proven elsewhere)."""
    import run_picklist_sync

    marker = tmp_path / "safety_picklist_sync.last_run"
    monkeypatch.setattr(run_picklist_sync, "_watchdog_marker_path", lambda: marker)
    run_picklist_sync._write_marker()
    assert marker.exists()
    datetime.fromisoformat(marker.read_text().strip())  # raises if not valid ISO


# ---- Check M: blueprint guard-symlink resolution (C3) --------------------


def test_blueprint_guard_symlinks_ok(monkeypatch, tmp_path):
    bp = tmp_path / "its-blueprint"
    (bp / ".claude" / "agents").mkdir(parents=True)
    (bp / ".claude" / "hooks").mkdir(parents=True)
    monkeypatch.setattr("watchdog._BLUEPRINT_ROOT", bp)
    result = watchdog._check_blueprint_guard_symlinks()
    assert result.severity is Severity.INFO
    assert "resolve OK" in result.summary


def test_blueprint_guard_symlinks_dangling_warns(monkeypatch, tmp_path):
    # Blueprint root exists, but the .claude guard symlinks dangle (target gone).
    bp = tmp_path / "its-blueprint"
    (bp / ".claude").mkdir(parents=True)
    (bp / ".claude" / "agents").symlink_to(tmp_path / "missing-agents")  # dangling
    (bp / ".claude" / "hooks").symlink_to(tmp_path / "missing-hooks")    # dangling
    monkeypatch.setattr("watchdog._BLUEPRINT_ROOT", bp)
    result = watchdog._check_blueprint_guard_symlinks()
    assert result.severity is Severity.WARN
    assert "fail-open" in result.summary
    assert ".claude/agents" in result.summary


def test_blueprint_guard_symlinks_skipped_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr("watchdog._BLUEPRINT_ROOT", tmp_path / "no-blueprint-here")
    result = watchdog._check_blueprint_guard_symlinks()
    assert result.severity is Severity.INFO
    assert "not on this host" in result.summary


# ---- Check P (A3): Box OAuth refresh-token freshness --------------------


def _write_box_freshness_marker(path: Path, *, days_ago: int) -> None:
    """Write a Box freshness marker whose last_refresh is `days_ago` days old."""
    import json

    last = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    path.write_text(json.dumps({"last_refresh_utc": last, "refresh_count": 1}))


@pytest.fixture
def box_probe(monkeypatch):
    """Control Check P's LIVE Box read.

    Defaults to a successful probe. Every Check-P test must go through this — without it
    the check would make a real authenticated Box call (and hit Keychain) under pytest.
    """
    def _set(items=None, exc=None):
        def _fake(folder_id, *, limit=100):
            if exc is not None:
                raise exc
            return items or []
        monkeypatch.setattr("watchdog.box_client.list_folder", _fake)

    _set()
    return _set


def test_check_box_token_freshness_fresh_is_info(monkeypatch, tmp_path, box_probe):
    marker = tmp_path / "box_oauth_last_refresh.json"
    _write_box_freshness_marker(marker, days_ago=3)
    monkeypatch.setattr("watchdog.box_client.BOX_TOKEN_REFRESH_MARKER", marker)
    result = watchdog._check_box_token_freshness()
    assert result.severity is Severity.INFO


def test_check_box_token_freshness_warns_at_50d(monkeypatch, tmp_path, box_probe):
    marker = tmp_path / "box_oauth_last_refresh.json"
    _write_box_freshness_marker(marker, days_ago=52)
    monkeypatch.setattr("watchdog.box_client.BOX_TOKEN_REFRESH_MARKER", marker)
    result = watchdog._check_box_token_freshness()
    assert result.severity is Severity.WARN


def test_check_box_token_freshness_absent_marker_warns(monkeypatch, tmp_path, box_probe):
    monkeypatch.setattr(
        "watchdog.box_client.BOX_TOKEN_REFRESH_MARKER", tmp_path / "missing.json"
    )
    result = watchdog._check_box_token_freshness()
    assert result.severity is Severity.WARN
    assert "ABSENT" in result.summary


def test_check_box_token_freshness_unreadable_marker_warns(monkeypatch, tmp_path, box_probe):
    marker = tmp_path / "box_oauth_last_refresh.json"
    marker.write_text("not valid json {{{")
    monkeypatch.setattr("watchdog.box_client.BOX_TOKEN_REFRESH_MARKER", marker)
    result = watchdog._check_box_token_freshness()
    assert result.severity is Severity.WARN
    assert "UNREADABLE" in result.summary


# ---- Check P: the LIVENESS probe (#26) ----------------------------------
#
# THE BUG THIS CLOSES. Check P read the marker and nothing else, so it logged
# "Box OAuth refresh token fresh (idle 2d)" every hour straight through a window in
# which live Box calls were failing with invalid_grant (2026-08-10). The first test
# below is the direct regression: a FRESH marker plus a DEAD token must be CRITICAL,
# and it is exactly the combination the old implementation called INFO.


def test_dead_token_is_critical_even_when_the_marker_looks_fresh(
    monkeypatch, tmp_path, box_probe
):
    """#26 regression: marker freshness must never outvote a live rejection."""
    marker = tmp_path / "box_oauth_last_refresh.json"
    _write_box_freshness_marker(marker, days_ago=2)  # the exact "idle 2d" of the incident
    monkeypatch.setattr("watchdog.box_client.BOX_TOKEN_REFRESH_MARKER", marker)
    box_probe(exc=watchdog.box_client.BoxRefreshTokenRejectedError("invalid_grant"))

    result = watchdog._check_box_token_freshness()

    assert result.severity is Severity.CRITICAL
    assert "DEAD" in result.summary
    # The marker must be reported as NOT-evidence, never as reassurance.
    assert "not evidence of health" in result.summary.lower()


def test_live_read_ok_downgrades_a_stale_marker_from_critical_to_warn(
    monkeypatch, tmp_path, box_probe
):
    """A successful authenticated read is positive proof the 60d wall was not hit.

    The old check called 59d-idle CRITICAL unconditionally. With ground truth available,
    paging "re-auth NOW" for a credential that demonstrably works would send a Tier-2
    operator into an unnecessary high-class escalation.
    """
    marker = tmp_path / "box_oauth_last_refresh.json"
    _write_box_freshness_marker(marker, days_ago=59)
    monkeypatch.setattr("watchdog.box_client.BOX_TOKEN_REFRESH_MARKER", marker)

    result = watchdog._check_box_token_freshness()

    assert result.severity is Severity.WARN
    assert "AUTHENTICATES NOW" in result.summary


def test_probe_transport_failure_falls_back_to_the_marker_and_says_so(
    monkeypatch, tmp_path, box_probe
):
    """Our own inability to reach Box must never be reported as a dead token."""
    marker = tmp_path / "box_oauth_last_refresh.json"
    _write_box_freshness_marker(marker, days_ago=1)
    monkeypatch.setattr("watchdog.box_client.BOX_TOKEN_REFRESH_MARKER", marker)
    box_probe(exc=watchdog.box_client.BoxRateLimitError("429"))

    result = watchdog._check_box_token_freshness()

    assert result.severity is Severity.INFO  # the marker verdict, unchanged
    assert "SKIPPED" in result.summary  # never mistakable for "probe passed"


def test_missing_credentials_warn_but_do_not_claim_an_expired_token(
    monkeypatch, tmp_path, box_probe
):
    """A generic auth failure is NOT evidence the refresh token aged out."""
    marker = tmp_path / "box_oauth_last_refresh.json"
    _write_box_freshness_marker(marker, days_ago=1)
    monkeypatch.setattr("watchdog.box_client.BOX_TOKEN_REFRESH_MARKER", marker)
    box_probe(exc=watchdog.box_client.BoxAuthError("credentials missing from Keychain"))

    result = watchdog._check_box_token_freshness()

    assert result.severity is Severity.WARN
    assert "NOT an expired refresh token" in result.summary


def test_probe_never_crashes_the_sweep_on_an_unexpected_exception(
    monkeypatch, tmp_path, box_probe
):
    marker = tmp_path / "box_oauth_last_refresh.json"
    _write_box_freshness_marker(marker, days_ago=1)
    monkeypatch.setattr("watchdog.box_client.BOX_TOKEN_REFRESH_MARKER", marker)
    box_probe(exc=RuntimeError("boxsdk exploded in a novel way"))

    result = watchdog._check_box_token_freshness()

    assert result.severity is Severity.INFO
    assert "SKIPPED" in result.summary


def test_probe_reads_root_with_a_bounded_page(monkeypatch, tmp_path, box_probe):
    """Pins the probe's shape: root folder (always exists for a valid credential, so a
    Box reorg cannot masquerade as a token failure) and a single-item page."""
    seen: dict = {}

    def _fake(folder_id, *, limit=100):
        seen["folder_id"] = folder_id
        seen["limit"] = limit
        return []

    monkeypatch.setattr("watchdog.box_client.list_folder", _fake)
    marker = tmp_path / "box_oauth_last_refresh.json"
    _write_box_freshness_marker(marker, days_ago=1)
    monkeypatch.setattr("watchdog.box_client.BOX_TOKEN_REFRESH_MARKER", marker)

    watchdog._check_box_token_freshness()

    assert seen == {"folder_id": "0", "limit": 1}


# ---- Group B: _check_stale_review_queue ---------------------------------


def test_no_pending_items(mock_get_pending, mock_is_past_sla):
    mock_get_pending.return_value = []

    result = watchdog._check_stale_review_queue()

    assert result.severity is Severity.INFO
    assert "No stale items" in result.summary
    mock_is_past_sla.assert_not_called()


def test_pending_but_none_stale(mock_get_pending, mock_is_past_sla):
    mock_get_pending.return_value = [
        {"Item ID": "safety_reports-20260519-100000", "Created At": "2026-05-19",
         "SLA Tier": "4h"},
        {"Item ID": "po_materials-20260519-110000", "Created At": "2026-05-19",
         "SLA Tier": "24h"},
    ]
    mock_is_past_sla.return_value = False

    result = watchdog._check_stale_review_queue()

    assert result.severity is Severity.INFO
    assert result.details == ""
    assert mock_is_past_sla.call_count == 2


def test_pending_and_stale_under_cap(mock_get_pending, mock_is_past_sla):
    mock_get_pending.return_value = [
        {"Item ID": "safety_reports-20260515-100000", "Created At": "2026-05-15",
         "SLA Tier": "4h"},
        {"Item ID": "po_materials-20260515-110000", "Created At": "2026-05-15",
         "SLA Tier": "4h"},
    ]
    mock_is_past_sla.return_value = True

    result = watchdog._check_stale_review_queue()

    assert result.severity is Severity.WARN
    assert "2 item(s)" in result.summary
    assert "safety_reports-20260515-100000" in result.details
    assert "po_materials-20260515-110000" in result.details
    assert "showing first" not in result.details


def test_pending_and_stale_over_cap(mock_get_pending, mock_is_past_sla):
    over_cap = watchdog.REVIEW_QUEUE_ITEM_CAP + 1
    rows = [
        {"Item ID": f"safety_reports-2026051{i}-100000", "Created At": "2026-05-15",
         "SLA Tier": "4h"}
        for i in range(over_cap)
    ]
    mock_get_pending.return_value = rows
    mock_is_past_sla.return_value = True

    result = watchdog._check_stale_review_queue()

    assert result.severity is Severity.WARN
    assert f"{over_cap} item(s)" in result.summary
    # Cap-many IDs shown; "showing first N of M" suffix appended.
    capped_ids = [r["Item ID"] for r in rows[:watchdog.REVIEW_QUEUE_ITEM_CAP]]
    for cid in capped_ids:
        assert cid in result.details
    # The (cap+1)-th ID must NOT appear.
    assert rows[watchdog.REVIEW_QUEUE_ITEM_CAP]["Item ID"] not in result.details
    assert f"first {watchdog.REVIEW_QUEUE_ITEM_CAP} of {over_cap}" in result.details


def test_stale_check_smartsheet_failure_propagates(mock_get_pending):
    # The check itself raises; the harness (Group D) catches separately.
    mock_get_pending.side_effect = SmartsheetError("HTTP 500: server error")
    with pytest.raises(SmartsheetError, match="500"):
        watchdog._check_stale_review_queue()


# ---- Group C: _check_open_criticals -------------------------------------


def test_no_criticals_in_errors(mock_get_rows):
    mock_get_rows.return_value = []

    result = watchdog._check_open_criticals()

    assert result.severity is Severity.INFO
    assert "No open CRITICAL" in result.summary


def test_all_criticals_resolved(mock_get_rows):
    mock_get_rows.return_value = [
        {"Error": "uncaught_exception", "Severity": "CRITICAL",
         "Resolved At": "2026-05-18"},
        {"Error": "smartsheet-write-failed", "Severity": "CRITICAL",
         "Resolved At": "2026-05-19"},
    ]

    result = watchdog._check_open_criticals()

    assert result.severity is Severity.INFO


def test_open_critical_under_cap(mock_get_rows):
    mock_get_rows.return_value = [
        {"Error": "uncaught_exception", "Severity": "CRITICAL",
         "Resolved At": None},
        {"Error": "smartsheet-write-failed", "Severity": "CRITICAL"},  # missing key
    ]

    result = watchdog._check_open_criticals()

    assert result.severity is Severity.WARN
    assert "2 open CRITICAL" in result.summary
    assert "uncaught_exception" in result.details
    assert "smartsheet-write-failed" in result.details
    assert "showing first" not in result.details


# ---- Group N: _check_stuck_wsr_send (write-ahead-marker safety net) -------


def test_no_rows_stuck_in_sending(mock_get_rows):
    mock_get_rows.return_value = []

    result = watchdog._check_stuck_wsr_send()

    assert result.severity is Severity.INFO
    assert "No review rows stuck in SENDING" in result.summary


def test_rows_stuck_in_sending_warn_across_every_lane(mock_get_rows):
    # The mock answers for EVERY review sheet, so a check that still scanned only
    # one lane would report 2 rows; scanning all five reports 10, each labelled.
    mock_get_rows.return_value = [{"_row_id": 50}, {"_row_id": 51}]

    result = watchdog._check_stuck_wsr_send()

    assert result.severity is Severity.WARN
    assert "10 review row(s) stuck in SENDING" in result.summary
    for label in ("WSR", "WPR", "PO", "RFQ", "SUB"):
        assert f"{label}:50" in result.details, f"{label} lane not scanned"


def test_stuck_sending_scan_is_per_sheet_fail_soft(mocker):
    # One lane's read blowing up must not hide the other four — the Check-T pattern.
    calls = {"n": 0}

    def flaky(sheet_id, filters=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("smartsheet boom")
        return [{"_row_id": 77}]

    mocker.patch("watchdog.smartsheet_client.get_rows", side_effect=flaky)
    result = watchdog._check_stuck_wsr_send()

    assert result.severity is Severity.WARN
    assert "4 review row(s) stuck in SENDING" in result.summary  # 5 lanes, 1 failed
    assert "read errors" in result.details and "smartsheet boom" in result.details


def test_open_critical_over_cap(mock_get_rows):
    over_cap = watchdog.CRITICAL_ITEMS_CAP + 1
    mock_get_rows.return_value = [
        {"Error": f"code-{i}", "Severity": "CRITICAL", "Resolved At": ""}
        for i in range(over_cap)
    ]

    result = watchdog._check_open_criticals()

    assert result.severity is Severity.WARN
    assert f"{over_cap} open CRITICAL" in result.summary
    for i in range(watchdog.CRITICAL_ITEMS_CAP):
        assert f"code-{i}" in result.details
    assert f"code-{watchdog.CRITICAL_ITEMS_CAP}" not in result.details
    assert f"first {watchdog.CRITICAL_ITEMS_CAP} of {over_cap}" in result.details


def test_critical_missing_error_code_renders_placeholder(mock_get_rows):
    mock_get_rows.return_value = [
        {"Error": None, "Severity": "CRITICAL", "Resolved At": None},
        {"Error": "", "Severity": "CRITICAL", "Resolved At": None},
        {"Severity": "CRITICAL", "Resolved At": None},  # Error key missing
    ]

    result = watchdog._check_open_criticals()

    assert result.severity is Severity.WARN
    # All three rows render <no-code>; details should contain three of them.
    assert result.details.count("<no-code>") == 3


def test_critical_filter_applied(mock_get_rows):
    # The Severity filter must be passed to get_rows — we don't fetch
    # everything and filter client-side here.
    mock_get_rows.return_value = []
    watchdog._check_open_criticals()
    mock_get_rows.assert_called_once_with(
        watchdog.sheet_ids.SHEET_ERRORS,
        filters={"Severity": "CRITICAL"},
    )


# ---- Group D: _run_check harness ----------------------------------------


def _result(severity: Severity, summary: str = "ok", details: str = "") -> watchdog.CheckResult:
    return watchdog.CheckResult(severity=severity, summary=summary, details=details)


def test_info_result_logs_info(mock_log):
    def info_check() -> watchdog.CheckResult:
        return _result(Severity.INFO, "all clear")
    watchdog._run_check(info_check, alerts_suppressed=False)
    severity, _script, _msg = mock_log.call_args.args
    assert severity is Severity.INFO


def test_warn_result_logs_warn(mock_log):
    def warn_check() -> watchdog.CheckResult:
        return _result(Severity.WARN, "stale items", details="X, Y")
    watchdog._run_check(warn_check, alerts_suppressed=False)
    severity, _script, msg = mock_log.call_args.args
    assert severity is Severity.WARN
    assert "stale items" in msg
    assert "X, Y" in msg


def test_critical_result_logs_critical(mock_log):
    def crit_check() -> watchdog.CheckResult:
        return _result(Severity.CRITICAL, "house is on fire")
    watchdog._run_check(crit_check, alerts_suppressed=False)
    severity, _script, _msg = mock_log.call_args.args
    assert severity is Severity.CRITICAL


def test_alerts_suppressed_downgrades_warn_to_info(mock_log):
    def warn_check() -> watchdog.CheckResult:
        return _result(Severity.WARN, "stale items")
    watchdog._run_check(warn_check, alerts_suppressed=True)
    severity, _script, _msg = mock_log.call_args.args
    assert severity is Severity.INFO


def test_alerts_suppressed_downgrades_critical_to_info(mock_log):
    def crit_check() -> watchdog.CheckResult:
        return _result(Severity.CRITICAL, "house is on fire")
    watchdog._run_check(crit_check, alerts_suppressed=True)
    severity, _script, _msg = mock_log.call_args.args
    assert severity is Severity.INFO


def test_alerts_suppressed_passes_info_through(mock_log):
    def info_check() -> watchdog.CheckResult:
        return _result(Severity.INFO, "all clear")
    watchdog._run_check(info_check, alerts_suppressed=True)
    severity, _script, _msg = mock_log.call_args.args
    assert severity is Severity.INFO


def test_check_raising_emits_marker_line(mock_log):
    def boom_check() -> watchdog.CheckResult:
        raise SmartsheetError("HTTP 500: server error")
    # Must NOT propagate.
    watchdog._run_check(boom_check, alerts_suppressed=False)
    severity, _script, msg = mock_log.call_args.args
    assert severity is Severity.ERROR
    assert "[watchdog-check-failed:boom_check]" in msg
    assert "HTTP 500" in msg


def test_check_failure_does_not_block_next_check(mock_log):
    calls: list[str] = []

    def failing_check() -> watchdog.CheckResult:
        calls.append("failing")
        raise RuntimeError("oops")

    def working_check() -> watchdog.CheckResult:
        calls.append("working")
        return _result(Severity.INFO, "ok")

    for check in (failing_check, working_check):
        watchdog._run_check(check, alerts_suppressed=False)

    # Both checks ran; the second wasn't blocked by the first's failure.
    assert calls == ["failing", "working"]
    # Two log calls: 1 ERROR marker line, 1 INFO from working_check.
    severities = [c.args[0] for c in mock_log.call_args_list]
    assert severities == [Severity.ERROR, Severity.INFO]


# ---- Group E: main() integration ----------------------------------------


def test_paused_skips_all_checks(mock_check_state, mock_log, mocker):
    mock_check_state.return_value = SystemState.PAUSED
    # Spy on the checks via the run_check entry point.
    run_check_spy = mocker.patch("watchdog._run_check")

    watchdog.main()

    run_check_spy.assert_not_called()
    # The single PAUSED preamble line was logged.
    messages = [c.args[2] for c in mock_log.call_args_list]
    assert any("PAUSED" in m for m in messages)


def test_maintenance_runs_with_alerts_suppressed(
    mock_check_state, mock_log, mock_get_setting, mock_ping, mocker
):
    mock_check_state.return_value = SystemState.MAINTENANCE
    mock_get_setting.return_value = "https://hc-ping.com/test-uuid"
    run_check_spy = mocker.patch("watchdog._run_check")

    watchdog.main()

    # All registered checks (Session 1 + Session 2 minus Check E) ran
    # with alerts_suppressed=True.
    assert run_check_spy.call_count == len(watchdog.CHECKS)
    for call in run_check_spy.call_args_list:
        assert call.kwargs == {"alerts_suppressed": True}
    # Preamble line present.
    messages = [c.args[2] for c in mock_log.call_args_list]
    assert any("MAINTENANCE" in m for m in messages)


def test_active_runs_normally(
    mock_check_state, mock_log, mock_get_setting, mock_ping, mocker
):
    mock_check_state.return_value = SystemState.ACTIVE
    mock_get_setting.return_value = "https://hc-ping.com/test-uuid"
    run_check_spy = mocker.patch("watchdog._run_check")

    watchdog.main()

    assert run_check_spy.call_count == len(watchdog.CHECKS)
    for call in run_check_spy.call_args_list:
        assert call.kwargs == {"alerts_suppressed": False}
    # No PAUSED/MAINTENANCE preamble line.
    messages = [c.args[2] for c in mock_log.call_args_list]
    assert not any("PAUSED" in m or "MAINTENANCE" in m for m in messages)


def test_main_writes_watchdog_marker_at_end(
    mock_check_state, mock_get_setting, mock_ping, mocker
):
    """`main()` calls write_last_run_marker('watchdog') after the check loop.

    PAUSED short-circuits before checks AND before the marker, so the
    marker is only written when state was ACTIVE or MAINTENANCE.
    """
    mock_check_state.return_value = SystemState.ACTIVE
    mock_get_setting.return_value = "https://hc-ping.com/test-uuid"
    mocker.patch("watchdog._run_check")

    watchdog.main()

    marker = watchdog.WATCHDOG_MARKER_DIR / "watchdog.last_run"
    assert marker.exists()
    # ISO 8601 with tz info.
    contents = marker.read_text()
    assert "T" in contents and ("+00:00" in contents or contents.endswith("Z"))


def test_main_paused_does_not_write_marker(mock_check_state, mocker):
    mock_check_state.return_value = SystemState.PAUSED
    mocker.patch("watchdog._run_check")

    watchdog.main()

    marker = watchdog.WATCHDOG_MARKER_DIR / "watchdog.last_run"
    assert not marker.exists()


# ---- Group E2: main() F16 heartbeat beacon ------------------------------


def test_main_pings_heartbeat_with_configured_url(
    mock_check_state, mock_get_setting, mock_ping, mocker
):
    """ACTIVE run reads system.heartbeat_url and pings exactly that URL."""
    mock_check_state.return_value = SystemState.ACTIVE
    mocker.patch("watchdog._run_check")
    mock_get_setting.return_value = "https://hc-ping.com/real-uuid"

    watchdog.main()

    # #336: resolve_and_log adds a startup config-observability pass (it resolves the 3 declared
    # watchdog keys, incl. heartbeat_url), so get_setting is no longer called exactly once — assert
    # the heartbeat read HAPPENED among the calls. The ping URL below is the load-bearing assertion.
    mock_get_setting.assert_any_call("system.heartbeat_url", workstream="global")
    mock_ping.assert_called_once_with("https://hc-ping.com/real-uuid")


def test_main_pings_heartbeat_during_maintenance(
    mock_check_state, mock_get_setting, mock_ping, mocker
):
    """MAINTENANCE still pings — the host is alive during a maintenance
    window, so suppressing the beacon would trip a false 'host dead' alert.
    Alert suppression applies to the checks' own alerts, not the beacon."""
    mock_check_state.return_value = SystemState.MAINTENANCE
    mocker.patch("watchdog._run_check")
    mock_get_setting.return_value = "https://hc-ping.com/real-uuid"

    watchdog.main()

    mock_ping.assert_called_once_with("https://hc-ping.com/real-uuid")


@pytest.mark.parametrize(
    "value",
    [None, "", "PLACEHOLDER_uptimerobot_heartbeat_url"],
)
def test_main_skips_ping_when_url_missing_or_placeholder(
    value, mock_check_state, mock_get_setting, mock_ping, mock_log, mocker
):
    """Unconfigured (missing/blank/seed-placeholder) URL → no ping, one INFO
    'not configured' line. The placeholder token MUST match the seed Value."""
    mock_check_state.return_value = SystemState.ACTIVE
    mocker.patch("watchdog._run_check")
    mock_get_setting.return_value = value

    watchdog.main()

    mock_ping.assert_not_called()
    messages = [c.args[2] for c in mock_log.call_args_list]
    assert any("not configured" in m for m in messages)


def test_main_paused_does_not_ping(
    mock_check_state, mock_get_setting, mock_ping, mocker
):
    """PAUSED returns before the marker AND the heartbeat block — no config
    read, no ping (a deliberately-paused system does not claim liveness)."""
    mock_check_state.return_value = SystemState.PAUSED
    mocker.patch("watchdog._run_check")

    watchdog.main()

    mock_ping.assert_not_called()
    mock_get_setting.assert_not_called()


def test_main_heartbeat_read_failure_is_swallowed(
    mock_check_state, mock_get_setting, mock_ping, mock_log, mocker
):
    """A SmartsheetError reading the URL is caught (WARN), the ping is
    skipped, and main() completes without raising — fail-soft per §3.1."""
    mock_check_state.return_value = SystemState.ACTIVE
    mocker.patch("watchdog._run_check")
    mock_get_setting.side_effect = SmartsheetError("config unreachable")

    # Must NOT raise.
    watchdog.main()

    mock_ping.assert_not_called()
    severities = [c.args[0] for c in mock_log.call_args_list]
    assert Severity.WARN in severities
    warn_msgs = [
        c.args[2] for c in mock_log.call_args_list if c.args[0] is Severity.WARN
    ]
    assert any("heartbeat_url read failed" in m for m in warn_msgs), warn_msgs


# ---- Group F: Check C — scheduled jobs scaffold + marker writes ---------


def test_write_last_run_marker_writes_iso_timestamp():
    watchdog.write_last_run_marker("smoke_test_job")
    marker = watchdog.WATCHDOG_MARKER_DIR / "smoke_test_job.last_run"
    assert marker.exists()
    from datetime import datetime as dt
    parsed = dt.fromisoformat(marker.read_text())
    assert parsed.tzinfo is not None


def test_write_last_run_marker_creates_dir_on_demand():
    """Marker dir is created if missing — first scheduled job after fresh
    install should not crash because ~/its/.watchdog/ doesn't exist."""
    # The autouse isolate fixture redirects WATCHDOG_MARKER_DIR to a path
    # under tmp_path that doesn't exist yet.
    assert not watchdog.WATCHDOG_MARKER_DIR.exists()
    watchdog.write_last_run_marker("fresh_install_test")
    assert watchdog.WATCHDOG_MARKER_DIR.is_dir()


def test_write_last_run_marker_fail_soft_on_oserror(mocker, mock_log):
    """Marker write failures WARN, do not raise. A successful job must not
    fail just because the marker write hit an OS error (full disk, etc.)."""
    mocker.patch(
        "watchdog.Path.write_text",
        side_effect=OSError("No space left on device"),
    )
    # Must NOT raise.
    watchdog.write_last_run_marker("disk_full_job")
    # Exactly one WARN was logged with the failure detail.
    severities = [c.args[0] for c in mock_log.call_args_list]
    assert Severity.WARN in severities
    warn_msg = next(c.args[2] for c in mock_log.call_args_list if c.args[0] is Severity.WARN)
    assert "disk_full_job" in warn_msg
    assert "No space left" in warn_msg


def test_check_scheduled_jobs_returns_info_when_tracked_jobs_empty(monkeypatch):
    """The empty-TRACKED_JOBS branch still exists and returns INFO.

    R3 Session 2 added safety_weekly_generate to TRACKED_JOBS, so this
    test temporarily empties the list to exercise the original no-op
    branch — useful coverage for the brief period before the first
    tracked job ships in any future forked customer repo.
    """
    monkeypatch.setattr("watchdog.TRACKED_JOBS", [])
    result = watchdog._check_scheduled_jobs()
    assert result.severity is Severity.INFO
    assert "empty by design" in result.summary or "No scheduled jobs" in result.summary


def test_check_scheduled_jobs_warns_on_missing_marker(monkeypatch):
    monkeypatch.setattr("watchdog.TRACKED_JOBS", ["nonexistent_job"])
    result = watchdog._check_scheduled_jobs()
    assert result.severity is Severity.WARN
    assert "nonexistent_job" in result.details
    assert "no marker" in result.details


def test_check_scheduled_jobs_warns_on_stale_marker(monkeypatch):
    """A marker older than 24 hours is stale."""
    from datetime import datetime as dt
    from datetime import timedelta as td
    monkeypatch.setattr("watchdog.TRACKED_JOBS", ["stale_job"])
    watchdog.WATCHDOG_MARKER_DIR.mkdir(parents=True, exist_ok=True)
    stale_marker = watchdog.WATCHDOG_MARKER_DIR / "stale_job.last_run"
    stale_ts = (dt.now(watchdog.UTC) - td(hours=25)).isoformat()
    stale_marker.write_text(stale_ts)

    result = watchdog._check_scheduled_jobs()

    assert result.severity is Severity.WARN
    assert "stale_job" in result.details
    assert stale_ts in result.details


def test_check_scheduled_jobs_ok_when_marker_fresh(monkeypatch):
    monkeypatch.setattr("watchdog.TRACKED_JOBS", ["fresh_job"])
    watchdog.write_last_run_marker("fresh_job")
    result = watchdog._check_scheduled_jobs()
    assert result.severity is Severity.INFO
    assert "fresh" in result.summary.lower()


# ---- Group G: Check D — reviewer-chain forward scan ---------------------


@pytest.fixture
def mock_review_queue_add(mocker):
    """review_queue.add as imported into watchdog — captures the row write."""
    return mocker.patch("watchdog.review_queue.add", return_value=12345)


@pytest.fixture
def mock_resolve_chain(mocker):
    return mocker.patch("watchdog.resolve_chain")


@pytest.fixture
def mock_is_federal_holiday(mocker):
    return mocker.patch("watchdog.is_federal_holiday", return_value=False)


@pytest.fixture
def mock_time_off_client(mocker):
    """Replace TimeOffClient with a no-op factory so Check D doesn't try
    to hit Smartsheet through _live_fetcher."""
    instance = mocker.MagicMock()
    return mocker.patch("watchdog.TimeOffClient", return_value=instance)


def _chain(*emails) -> object:
    """Build a minimal ReviewerChain-shaped object for Check D's needs."""
    from types import SimpleNamespace
    return SimpleNamespace(slots=tuple(emails), is_empty=not emails)


def test_check_reviewer_chain_no_gaps(
    mock_resolve_chain, mock_is_federal_holiday, mock_time_off_client,
    mock_review_queue_add,
):
    """All 14 days have a non-empty chain → no anomaly rows written."""
    mock_resolve_chain.return_value = _chain("p@x", "s@x", "t@x")

    result = watchdog._check_reviewer_chain_forward()

    assert result.severity is Severity.INFO
    assert "No reviewer-chain gaps" in result.summary
    mock_review_queue_add.assert_not_called()


def test_check_reviewer_chain_single_gap(
    mock_resolve_chain, mock_is_federal_holiday, mock_time_off_client,
    mock_review_queue_add,
):
    """One day has an empty chain → one anomaly row per workstream."""
    # First call → empty (gap), rest → full chain.
    mock_resolve_chain.side_effect = [_chain(), *[_chain("p", "s", "t")] * 13]

    result = watchdog._check_reviewer_chain_forward()

    assert result.severity is Severity.INFO
    assert "Logged 1 reviewer-chain anomaly row" in result.summary
    mock_review_queue_add.assert_called_once()
    kwargs = mock_review_queue_add.call_args.kwargs
    assert kwargs["workstream"] == "global"  # not "watchdog" — see preflight resolution
    assert kwargs["reason"] is watchdog.ReviewReason.OTHER
    assert kwargs["sla_tier"] is watchdog.SlaTier.SUBCONTRACT_DRAFT
    assert kwargs["severity"] is Severity.INFO
    assert "reviewer-chain gap" in kwargs["summary"]
    # The actual workstream name lives in the payload, not the row's workstream cell.
    assert kwargs["payload"]["workstream"] == "safety_reports"
    assert len(kwargs["payload"]["gap_dates"]) == 1


def test_check_reviewer_chain_multiple_gaps_collapse_into_one_row(
    mock_resolve_chain, mock_is_federal_holiday, mock_time_off_client,
    mock_review_queue_add,
):
    """3 gap days → 1 anomaly row with 3 dates in payload."""
    mock_resolve_chain.side_effect = (
        [_chain()] * 3 + [_chain("p", "s", "t")] * 11
    )

    watchdog._check_reviewer_chain_forward()

    mock_review_queue_add.assert_called_once()
    assert len(mock_review_queue_add.call_args.kwargs["payload"]["gap_dates"]) == 3


def test_check_reviewer_chain_federal_holiday_skipped(
    mock_resolve_chain, mock_is_federal_holiday, mock_time_off_client,
    mock_review_queue_add,
):
    """Federal holidays don't count as reviewer gaps."""
    mock_is_federal_holiday.return_value = True  # every day is a holiday
    # resolve_chain would return empty if called — but Check D should skip it.
    mock_resolve_chain.return_value = _chain()

    result = watchdog._check_reviewer_chain_forward()

    assert result.severity is Severity.INFO
    assert "No reviewer-chain gaps" in result.summary
    # resolve_chain never called because every day was a holiday.
    mock_resolve_chain.assert_not_called()
    mock_review_queue_add.assert_not_called()


def test_check_reviewer_chain_constructs_one_time_off_client(
    mock_resolve_chain, mock_is_federal_holiday, mock_time_off_client,
    mock_review_queue_add,
):
    """Per-instance caching → one TimeOffClient() per check run, which
    means a single Smartsheet read regardless of how many days scanned."""
    mock_resolve_chain.return_value = _chain("p", "s", "t")

    watchdog._check_reviewer_chain_forward()

    # One TimeOffClient construction per check run (one workstream × one client).
    assert mock_time_off_client.call_count == 1


def test_check_reviewer_chain_payload_dates_iso_formatted(
    mock_resolve_chain, mock_is_federal_holiday, mock_time_off_client,
    mock_review_queue_add,
):
    """Gap dates are serialized as ISO 8601 strings (date.isoformat)."""
    from datetime import date
    mock_resolve_chain.side_effect = [_chain(), *[_chain("p", "s", "t")] * 13]

    watchdog._check_reviewer_chain_forward()

    payload = mock_review_queue_add.call_args.kwargs["payload"]
    [gap_str] = payload["gap_dates"]
    # Round-trips through date.fromisoformat without error.
    date.fromisoformat(gap_str)


# ---- Group H: Check F — RETIRED 2026-06-05 (safety mail-intake silent-disable removed) ----


# ---- Group I: Check G — alert-dedupe summary sweep ---------------------


@pytest.fixture
def dedupe_state(tmp_path, monkeypatch):
    """Redirect alert_dedupe state file to tmp_path for hermetic tests.

    Mirrors the pattern in tests/test_alert_dedupe.py::state_in_tmp. The
    watchdog imports `alert_dedupe` from `shared`, so monkeypatching the
    module attributes is sufficient — the check function reads
    `alert_dedupe.STATE_FILE` indirectly through the public helpers.
    """
    import shared.alert_dedupe as ad
    state_dir = tmp_path / "state"
    monkeypatch.setattr(ad, "STATE_DIR", state_dir)
    monkeypatch.setattr(ad, "STATE_FILE", state_dir / "alert_dedupe.json")
    return state_dir


@pytest.fixture
def frozen_clock(monkeypatch):
    """Pin alert_dedupe._now() to a known UTC moment with mutation support."""
    from datetime import UTC, datetime, timedelta

    import shared.alert_dedupe as ad

    class _Clock:
        def __init__(self):
            self.now = datetime(2026, 5, 20, 15, 0, 0, tzinfo=UTC)

        def advance(self, **kwargs):
            self.now = self.now + timedelta(**kwargs)

    clock = _Clock()
    monkeypatch.setattr(ad, "_now", lambda: clock.now)
    return clock


@pytest.fixture(autouse=True)
def settings_mock_for_dedupe(mocker):
    """Default ITS_Config window = 60 min so record_fire works in tests."""
    return mocker.patch(
        "shared.smartsheet_client.get_setting", return_value="60"
    )


@pytest.fixture
def mock_send_alert(mocker):
    return mocker.patch("watchdog.resend_client.send_alert")


def _seed_expired_entry(
    key: str, suppressed_count: int = 0, summarized: bool = False
):
    """Write one entry to the redirected state file with window already expired."""
    import json as _json

    import shared.alert_dedupe as ad
    ad.STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = (
        _json.loads(ad.STATE_FILE.read_text())
        if ad.STATE_FILE.exists() and ad.STATE_FILE.stat().st_size > 0
        else {}
    )
    state[key] = {
        "first_fired_at": "2026-05-20T14:00:00+00:00",
        "last_fired_at":  "2026-05-20T14:05:00+00:00",
        "suppressed_count": suppressed_count,
        "window_ends_at": "2026-05-20T14:30:00+00:00",  # before frozen_clock = 15:00
        "summarized": summarized,
    }
    ad.STATE_FILE.write_text(_json.dumps(state))


def test_summary_sweep_no_expired_returns_info(dedupe_state, frozen_clock, mock_send_alert):
    result = watchdog._check_alert_dedupe_summaries()

    assert result.severity is Severity.INFO
    assert "No expired" in result.summary
    mock_send_alert.assert_not_called()


def test_summary_sweep_fires_resend_for_suppressed_expired_entry(
    dedupe_state, frozen_clock, mock_send_alert
):
    _seed_expired_entry("safety.intake::uncaught_exception", suppressed_count=4)

    result = watchdog._check_alert_dedupe_summaries()

    mock_send_alert.assert_called_once()
    subject, body = mock_send_alert.call_args.args
    assert subject.startswith("[ITS CRITICAL SUMMARY]")
    assert "safety.intake" in subject
    assert "4 suppressed" in subject
    assert "Script:           safety.intake" in body
    assert "Error code:       uncaught_exception" in body
    assert "Suppressed count: 4" in body
    assert "Filter ITS_Errors by:" in body
    assert result.severity is Severity.INFO
    assert "fired 1 summary" in result.summary


def test_summary_sweep_marks_summarized_after_fire(
    dedupe_state, frozen_clock, mock_send_alert
):
    import json as _json

    import shared.alert_dedupe as ad

    _seed_expired_entry("script::uncaught_exception", suppressed_count=2)

    watchdog._check_alert_dedupe_summaries()

    state = _json.loads(ad.STATE_FILE.read_text())
    assert state["script::uncaught_exception"]["summarized"] is True


def test_summary_sweep_does_not_delete_freshly_summarized(
    dedupe_state, frozen_clock, mock_send_alert
):
    """Two-phase deletion: a just-summarized entry stays for next sweep."""
    import json as _json

    import shared.alert_dedupe as ad

    _seed_expired_entry("script::uncaught_exception", suppressed_count=2)
    watchdog._check_alert_dedupe_summaries()

    state = _json.loads(ad.STATE_FILE.read_text())
    assert "script::uncaught_exception" in state


def test_summary_sweep_deletes_already_summarized_entry(
    dedupe_state, frozen_clock, mock_send_alert
):
    """Phase 2 of two-phase delete: summarized entries get removed."""
    import json as _json

    import shared.alert_dedupe as ad

    _seed_expired_entry("script::uncaught_exception", suppressed_count=2, summarized=True)

    result = watchdog._check_alert_dedupe_summaries()

    state = _json.loads(ad.STATE_FILE.read_text())
    assert "script::uncaught_exception" not in state
    mock_send_alert.assert_not_called()
    assert "deleted 1 entry" in result.summary


def test_summary_sweep_deletes_clean_expired_entry(
    dedupe_state, frozen_clock, mock_send_alert
):
    """suppressed_count == 0 means no flapping happened; delete on first sweep
    without firing a summary."""
    import json as _json

    import shared.alert_dedupe as ad

    _seed_expired_entry("script::uncaught_exception", suppressed_count=0)

    result = watchdog._check_alert_dedupe_summaries()

    state = _json.loads(ad.STATE_FILE.read_text())
    assert "script::uncaught_exception" not in state
    mock_send_alert.assert_not_called()
    assert "deleted 1 entry" in result.summary


def test_summary_sweep_skips_open_windows(
    dedupe_state, frozen_clock, mock_send_alert
):
    """Open-window entries are excluded from the sweep entirely (Phase 0).

    list_expired_summaries filters on window_ends_at < now; open windows
    never reach the check function's loop.
    """
    import json as _json

    import shared.alert_dedupe as ad

    # Seed a state entry with a future window_ends_at.
    ad.STATE_DIR.mkdir(parents=True, exist_ok=True)
    ad.STATE_FILE.write_text(_json.dumps({
        "script::uncaught_exception": {
            "first_fired_at": "2026-05-20T14:55:00+00:00",
            "last_fired_at":  "2026-05-20T14:55:30+00:00",
            "suppressed_count": 3,
            "window_ends_at": "2026-05-20T15:55:00+00:00",  # 55 min after frozen
            "summarized": False,
        }
    }))

    result = watchdog._check_alert_dedupe_summaries()

    mock_send_alert.assert_not_called()
    state = _json.loads(ad.STATE_FILE.read_text())
    # Still present — sweep didn't touch it.
    assert "script::uncaught_exception" in state
    assert "No expired" in result.summary


def test_summary_sweep_resend_failure_leaves_entry_unmarked(
    dedupe_state, frozen_clock, mock_send_alert, mock_log
):
    """If Resend raises, the entry stays unmarked so the next sweep retries."""
    import json as _json

    import shared.alert_dedupe as ad

    _seed_expired_entry("script::uncaught_exception", suppressed_count=2)
    mock_send_alert.side_effect = RuntimeError("resend down")

    watchdog._check_alert_dedupe_summaries()

    state = _json.loads(ad.STATE_FILE.read_text())
    assert state["script::uncaught_exception"]["summarized"] is False
    # WARN line via watchdog.log captures the send failure.
    severities = [c.args[0] for c in mock_log.call_args_list]
    assert Severity.WARN in severities


def test_summary_sweep_state_read_failure_logs_marker_no_crash(
    dedupe_state, frozen_clock, mock_send_alert, monkeypatch
):
    """If list_expired_summaries returns empty (e.g., due to internal failure),
    the check still completes cleanly with the no-work message."""
    import shared.alert_dedupe as ad
    monkeypatch.setattr(ad, "list_expired_summaries", lambda: [])

    result = watchdog._check_alert_dedupe_summaries()

    assert result.severity is Severity.INFO
    mock_send_alert.assert_not_called()


def test_summary_sweep_mixed_expired_entries(
    dedupe_state, frozen_clock, mock_send_alert
):
    """One needs summarizing, one needs deleting (clean expiry), one is
    summarized-and-pending-delete. All three handled in one sweep."""
    import json as _json

    import shared.alert_dedupe as ad

    _seed_expired_entry("a::uncaught_exception", suppressed_count=3)
    _seed_expired_entry("b::uncaught_exception", suppressed_count=0)
    _seed_expired_entry(
        "c::uncaught_exception", suppressed_count=5, summarized=True
    )

    result = watchdog._check_alert_dedupe_summaries()

    # `a` got a summary fired + marked; `b` and `c` deleted.
    mock_send_alert.assert_called_once()
    subject, _ = mock_send_alert.call_args.args
    assert "a::" not in subject  # subject uses just the script half
    assert subject.startswith("[ITS CRITICAL SUMMARY] a:")

    state = _json.loads(ad.STATE_FILE.read_text())
    assert "a::uncaught_exception" in state  # stays this sweep (phase 1)
    assert state["a::uncaught_exception"]["summarized"] is True
    assert "b::uncaught_exception" not in state  # deleted
    assert "c::uncaught_exception" not in state  # deleted

    assert "fired 1 summary" in result.summary
    assert "deleted 2 entries" in result.summary


def test_summary_subject_format_matches_spec(
    dedupe_state, frozen_clock, mock_send_alert
):
    _seed_expired_entry("safety_reports.intake::uncaught_exception", suppressed_count=7)
    watchdog._check_alert_dedupe_summaries()

    subject, _ = mock_send_alert.call_args.args
    assert subject == "[ITS CRITICAL SUMMARY] safety_reports.intake: 7 suppressed occurrences"


def test_summary_body_includes_filter_criteria(
    dedupe_state, frozen_clock, mock_send_alert
):
    _seed_expired_entry("safety_reports.intake::uncaught_exception", suppressed_count=2)
    watchdog._check_alert_dedupe_summaries()

    _, body = mock_send_alert.call_args.args
    # Filter criteria pointing operator at ITS_Errors.
    assert "Filter ITS_Errors by:" in body
    assert "Script = safety_reports.intake" in body
    assert "Surfaced At BETWEEN" in body
    # Sheet ID reference (so operator can copy/paste).
    from shared import sheet_ids
    assert str(sheet_ids.SHEET_ERRORS) in body


def test_summary_sweep_check_registered_in_checks_list():
    """The new check is registered in CHECKS so main() runs it."""
    assert watchdog._check_alert_dedupe_summaries in watchdog.CHECKS


def test_summary_sweep_check_failure_isolated_by_run_check(
    dedupe_state, frozen_clock, mock_send_alert, mock_log, monkeypatch
):
    """A raise inside the check is caught by _run_check, marker logged,
    other checks would still run (here we only assert no propagation)."""
    def _boom():
        raise RuntimeError("unexpected sweep failure")

    monkeypatch.setattr(watchdog, "_check_alert_dedupe_summaries", _boom)
    # Patch the CHECKS list to only include the broken one for clarity.
    monkeypatch.setattr(watchdog, "CHECKS", [_boom])

    watchdog._run_check(_boom, alerts_suppressed=False)

    severities = [c.args[0] for c in mock_log.call_args_list]
    assert Severity.ERROR in severities
    err_line = next(
        c.args[2] for c in mock_log.call_args_list if c.args[0] is Severity.ERROR
    )
    assert "[watchdog-check-failed:_boom]" in err_line


# ---- Group J: Check G — MAINTENANCE defer (V1 fix) ----------------------


def test_check_g_skips_summary_fire_during_maintenance(
    dedupe_state, frozen_clock, mock_send_alert
):
    """MAINTENANCE: phase-1 entry must not fire Resend; summarized stays False;
    entry persists in state for the post-MAINTENANCE sweep."""
    import json as _json

    import shared.alert_dedupe as ad
    _seed_expired_entry("safety.intake::uncaught_exception", suppressed_count=4)

    result = watchdog._check_alert_dedupe_summaries(alerts_suppressed=True)

    mock_send_alert.assert_not_called()
    state = _json.loads(ad.STATE_FILE.read_text())
    # Entry persists, still unsummarized.
    assert "safety.intake::uncaught_exception" in state
    assert state["safety.intake::uncaught_exception"]["summarized"] is False
    # Result summary names the deferred count.
    assert "deferred 1 summar" in result.summary
    assert "MAINTENANCE" in result.summary
    assert "safety.intake::uncaught_exception" in result.details


def test_check_g_processes_phase2_delete_during_maintenance(
    dedupe_state, frozen_clock, mock_send_alert
):
    """Phase-2 deletion has no push side-effect, so it proceeds during
    MAINTENANCE — otherwise the state file would grow unboundedly
    while the kill switch is engaged."""
    import json as _json

    import shared.alert_dedupe as ad
    _seed_expired_entry(
        "safety.intake::uncaught_exception", suppressed_count=4, summarized=True
    )

    result = watchdog._check_alert_dedupe_summaries(alerts_suppressed=True)

    state = _json.loads(ad.STATE_FILE.read_text())
    assert "safety.intake::uncaught_exception" not in state
    mock_send_alert.assert_not_called()
    assert "deleted 1 entry" in result.summary


def test_check_g_processes_clean_expiry_delete_during_maintenance(
    dedupe_state, frozen_clock, mock_send_alert
):
    """A clean-expiry entry (suppressed_count==0) also lands in phase 2
    and deletes during MAINTENANCE — same no-push-side-effect rationale."""
    import json as _json

    import shared.alert_dedupe as ad
    _seed_expired_entry("safety.intake::uncaught_exception", suppressed_count=0)

    watchdog._check_alert_dedupe_summaries(alerts_suppressed=True)

    state = _json.loads(ad.STATE_FILE.read_text())
    assert "safety.intake::uncaught_exception" not in state
    mock_send_alert.assert_not_called()


def test_check_g_fires_normally_after_maintenance_ends(
    dedupe_state, frozen_clock, mock_send_alert
):
    """Sequence: first sweep during MAINTENANCE defers; second sweep
    after MAINTENANCE clears fires the deferred digest."""
    import json as _json

    import shared.alert_dedupe as ad
    _seed_expired_entry("safety.intake::uncaught_exception", suppressed_count=4)

    # First sweep — MAINTENANCE on.
    watchdog._check_alert_dedupe_summaries(alerts_suppressed=True)
    mock_send_alert.assert_not_called()
    state = _json.loads(ad.STATE_FILE.read_text())
    assert state["safety.intake::uncaught_exception"]["summarized"] is False

    # Second sweep — MAINTENANCE off.
    result = watchdog._check_alert_dedupe_summaries(alerts_suppressed=False)
    mock_send_alert.assert_called_once()
    subject, _ = mock_send_alert.call_args.args
    assert "safety.intake" in subject
    assert "4 suppressed" in subject
    state = _json.loads(ad.STATE_FILE.read_text())
    assert state["safety.intake::uncaught_exception"]["summarized"] is True
    assert "fired 1 summary" in result.summary


def test_check_g_default_arg_is_alerts_suppressed_false(
    dedupe_state, frozen_clock, mock_send_alert
):
    """Calling without the kwarg must NOT defer — safety default per spec."""
    _seed_expired_entry("safety.intake::uncaught_exception", suppressed_count=4)

    # No alerts_suppressed kwarg.
    watchdog._check_alert_dedupe_summaries()

    mock_send_alert.assert_called_once()


def test_check_g_mixed_entries_during_maintenance(
    dedupe_state, frozen_clock, mock_send_alert
):
    """Phase-1 entries defer; phase-2 entries delete; both happen in one
    MAINTENANCE sweep."""
    import json as _json

    import shared.alert_dedupe as ad

    _seed_expired_entry("phase1::uncaught_exception", suppressed_count=4)
    _seed_expired_entry("phase2_clean::uncaught_exception", suppressed_count=0)
    _seed_expired_entry(
        "phase2_summarized::uncaught_exception", suppressed_count=2, summarized=True
    )

    result = watchdog._check_alert_dedupe_summaries(alerts_suppressed=True)

    mock_send_alert.assert_not_called()
    state = _json.loads(ad.STATE_FILE.read_text())
    # Phase 1 deferred → still present, still unsummarized.
    assert "phase1::uncaught_exception" in state
    assert state["phase1::uncaught_exception"]["summarized"] is False
    # Both phase-2 entries deleted.
    assert "phase2_clean::uncaught_exception" not in state
    assert "phase2_summarized::uncaught_exception" not in state

    assert "fired 0 summary" in result.summary
    assert "deleted 2 entries" in result.summary
    assert "deferred 1 summary during MAINTENANCE" in result.summary


def test_run_check_threads_alerts_suppressed_to_check_g(
    dedupe_state, frozen_clock, mock_send_alert
):
    """_run_check must detect Check G's alerts_suppressed kwarg and pass it.

    Regression guard against the signature-inspection fork in _run_check
    (V1 fix). If the threading regresses, Check G would always run as if
    alerts_suppressed=False (default), re-introducing the V1 bug.
    """
    _seed_expired_entry("safety.intake::uncaught_exception", suppressed_count=4)

    watchdog._run_check(
        watchdog._check_alert_dedupe_summaries, alerts_suppressed=True
    )
    # Send NOT called — proves alerts_suppressed=True was threaded in.
    mock_send_alert.assert_not_called()


def test_run_check_does_not_pass_alerts_suppressed_to_legacy_checks(mocker):
    """_run_check inspects signature; legacy checks that take no args
    must still be called with no args (no TypeError from unexpected kwarg).
    """
    legacy_check = mocker.MagicMock(
        return_value=watchdog.CheckResult(
            severity=Severity.INFO, summary="ok"
        )
    )
    legacy_check.__name__ = "legacy_check"
    # Make signature inspection return zero params (mirrors the real
    # legacy checks).
    legacy_check.__signature__ = inspect.Signature(parameters=[])

    watchdog._run_check(legacy_check, alerts_suppressed=True)

    legacy_check.assert_called_once_with()  # zero args, NOT alerts_suppressed kwarg


# ---- Group I: Check I — weekly_generate catch-up recovery ----------------
#
# weekly_generate is the lone tracked daemon on a calendar schedule
# (StartCalendarInterval Friday 14:00), so a crashed Friday cycle is not
# re-invoked by launchd until the next Friday. Check I re-fires the missed
# generation on a subsequent daily watchdog run while inside a short window.

# Fixed offset standing in for the local timezone (the trigger fires in
# local time). Anchor scenario: the Friday 2026-06-05 14:00 trigger, whose
# ISO week's Monday is 2026-06-01.
_LOCAL = timezone(timedelta(hours=-7))
_LAST_TRIGGER = datetime(2026, 6, 5, 14, 0, tzinfo=_LOCAL)
_TARGET_MONDAY = date(2026, 6, 1)
# Saturday morning after the trigger — squarely inside the catch-up window.
_SAT_AFTER_TRIGGER = datetime(2026, 6, 6, 7, 0, tzinfo=_LOCAL)
# Tuesday morning — past the 3-day (through-Monday) catch-up window.
_TUE_OUT_OF_WINDOW = datetime(2026, 6, 9, 7, 0, tzinfo=_LOCAL)


@pytest.fixture
def catchup_now(monkeypatch):
    """Pin Check I's clock (_local_now). Returns a setter for each test."""
    def _set(now: datetime) -> None:
        monkeypatch.setattr("watchdog._local_now", lambda: now)
    return _set


@pytest.fixture
def mock_run_pipeline(mocker):
    """Mock weekly_generate._run_pipeline at the watchdog import path.

    Default return = the REAL generate_core.run_generate dict shape (RunSummary.__dict__ + week
    bounds + correlation_id) for a healthy run (5 packets). Tests override return_value or
    side_effect (failure).
    """
    return mocker.patch(
        "watchdog.weekly_generate._run_pipeline",
        return_value={
            "jobs_processed": 1,
            "packets_compiled": 5,
            "skipped_no_change": 0,
            "empty_weeks": 0,
            "wsr_written": 5,
            "review_queue_entries": 0,
            "timed_out": 0,
            "download_errors": 0,
            "errors_per_job": {},
            "week_start": _TARGET_MONDAY.isoformat(),
            "week_end": (_TARGET_MONDAY + timedelta(days=6)).isoformat(),
            "correlation_id": "abc123def456",
        },
    )


@pytest.fixture
def mock_alert_critical(mocker):
    """Mock the triple-fire push leg so no real Resend/Sentry page fires."""
    return mocker.patch("watchdog._alert_critical")


def _write_wg_marker(ts: datetime) -> None:
    """Write the safety_weekly_generate marker with an explicit timestamp."""
    watchdog.WATCHDOG_MARKER_DIR.mkdir(parents=True, exist_ok=True)
    (watchdog.WATCHDOG_MARKER_DIR / "safety_weekly_generate.last_run").write_text(
        ts.isoformat()
    )


@pytest.mark.parametrize(
    "now, expect_trigger, expect_target_monday",
    [
        # Friday before 14:00 → most recent trigger is LAST Friday (this
        # week's run hasn't fired yet; last week is the most recent trigger).
        (
            datetime(2026, 6, 5, 7, 0, tzinfo=_LOCAL),
            datetime(2026, 5, 29, 14, 0, tzinfo=_LOCAL),
            "2026-05-25",
        ),
        # Friday after 14:00 → this Friday.
        (
            datetime(2026, 6, 5, 15, 0, tzinfo=_LOCAL),
            datetime(2026, 6, 5, 14, 0, tzinfo=_LOCAL),
            "2026-06-01",
        ),
        # Saturday → yesterday's Friday.
        (
            datetime(2026, 6, 6, 7, 0, tzinfo=_LOCAL),
            datetime(2026, 6, 5, 14, 0, tzinfo=_LOCAL),
            "2026-06-01",
        ),
        # Monday → the previous Friday.
        (
            datetime(2026, 6, 8, 7, 0, tzinfo=_LOCAL),
            datetime(2026, 6, 5, 14, 0, tzinfo=_LOCAL),
            "2026-06-01",
        ),
    ],
)
def test_most_recent_friday_trigger(now, expect_trigger, expect_target_monday):
    trigger = watchdog._most_recent_friday_trigger(now)
    assert trigger == expect_trigger
    # Target week = the Monday of the trigger Friday's ISO week (Friday - 4d).
    assert (trigger - timedelta(days=4)).date().isoformat() == expect_target_monday


def test_check_i_registered_in_checks_after_check_c():
    assert watchdog._check_weekly_generate_catchup in watchdog.CHECKS
    # Check I recovers what Check C only detects, so it must run after it.
    assert watchdog.CHECKS.index(
        watchdog._check_weekly_generate_catchup
    ) > watchdog.CHECKS.index(watchdog._check_scheduled_jobs)


def test_catchup_no_fire_when_marker_fresh(catchup_now, mock_run_pipeline):
    """(a) Generation ran this week (marker fresh) → no catch-up.

    Marker written in UTC (as weekly_generate actually does) AFTER the local
    Friday 14:00 trigger — exercises the cross-timezone marker comparison.
    """
    catchup_now(_SAT_AFTER_TRIGGER)
    # 2026-06-05 22:00 UTC == 15:00 -07:00, i.e. after the 14:00 local trigger.
    _write_wg_marker(datetime(2026, 6, 5, 22, 0, tzinfo=UTC))

    result = watchdog._check_weekly_generate_catchup()

    assert result.severity is Severity.INFO
    assert "ran for week" in result.summary
    mock_run_pipeline.assert_not_called()


def test_catchup_fires_when_missing_in_window(
    catchup_now, mock_run_pipeline, mock_get_rows
):
    """(b) No marker + no rows + in window → catch-up fires exactly once."""
    catchup_now(_SAT_AFTER_TRIGGER)
    mock_get_rows.return_value = []  # no WPR rows for the target week

    result = watchdog._check_weekly_generate_catchup()

    mock_run_pipeline.assert_called_once_with(week_start_override=_TARGET_MONDAY)
    assert result.severity is Severity.INFO
    assert "catch-up fired" in result.summary
    assert "5 packet(s) compiled" in result.summary  # real run_generate counter


def test_catchup_no_fire_outside_window(
    catchup_now, mock_run_pipeline, mock_get_rows
):
    """(c) Missed, but now is past the catch-up window → no catch-up."""
    catchup_now(_TUE_OUT_OF_WINDOW)
    mock_get_rows.return_value = []

    result = watchdog._check_weekly_generate_catchup()

    assert result.severity is Severity.INFO
    assert "past the catch-up window" in result.summary
    mock_run_pipeline.assert_not_called()


def test_catchup_no_fire_when_rows_present_marker_stale(
    catchup_now, mock_run_pipeline, mock_get_rows
):
    """Robustness: marker stale/missing BUT WPR rows exist (fail-soft marker
    write left a stale marker on an otherwise-successful run) → no re-fire."""
    catchup_now(_SAT_AFTER_TRIGGER)
    mock_get_rows.return_value = [{"_row_id": 1, "Week": _TARGET_MONDAY.isoformat()}]

    result = watchdog._check_weekly_generate_catchup()

    assert result.severity is Severity.INFO
    assert "rows present" in result.summary
    mock_run_pipeline.assert_not_called()


def test_catchup_failure_triple_fires_critical_no_loop(
    catchup_now, mock_run_pipeline, mock_get_rows, mock_alert_critical, mock_log
):
    """(d) Catch-up generation raises → CRITICAL triple-fire, fired once."""
    catchup_now(_SAT_AFTER_TRIGGER)
    mock_get_rows.return_value = []
    mock_run_pipeline.side_effect = RuntimeError("generation boom")

    result = watchdog._check_weekly_generate_catchup()

    # No loop — generation attempted exactly once.
    mock_run_pipeline.assert_called_once()
    # Operator paged via the triple-fire push legs.
    mock_alert_critical.assert_called_once()
    assert (
        mock_alert_critical.call_args.kwargs["error_code"]
        == "weekly_generate_catchup_failed"
    )
    # CRITICAL record row written with a matching distinct error_code.
    crit_calls = [c for c in mock_log.call_args_list if c.args[0] is Severity.CRITICAL]
    assert len(crit_calls) == 1
    assert crit_calls[0].kwargs["error_code"] == "weekly_generate_catchup_failed"
    # A3: the record log MUST opt out of auto-paging (alert=False) so the
    # explicit, MAINTENANCE-deferrable page below is the ONLY page. Dropping
    # this kwarg would double-fire and page during MAINTENANCE (live, not in
    # these mocked tests) — this assertion is the regression lock.
    assert crit_calls[0].kwargs["alert"] is False
    # Row + page share one correlation_id (so a single grep recovers all legs).
    assert (
        crit_calls[0].kwargs["correlation_id"]
        == mock_alert_critical.call_args.kwargs["correlation_id"]
    )
    assert result.severity is Severity.INFO  # alerting already fired explicitly
    assert "FAILED" in result.summary


def test_catchup_paused_skips_via_main(mock_check_state, mock_run_pipeline):
    """(e) PAUSED → main() returns before the checks loop; no catch-up."""
    mock_check_state.return_value = SystemState.PAUSED

    watchdog.main()

    mock_run_pipeline.assert_not_called()


def test_catchup_maintenance_runs_but_defers_page(
    catchup_now, mock_run_pipeline, mock_get_rows, mock_alert_critical, mock_log
):
    """(f) MAINTENANCE: generation RUNS but a failure's page is DEFERRED;
    the CRITICAL record row is still written (push-vs-record, Op Stds §3.1)."""
    catchup_now(_SAT_AFTER_TRIGGER)
    mock_get_rows.return_value = []
    mock_run_pipeline.side_effect = RuntimeError("boom")

    result = watchdog._check_weekly_generate_catchup(alerts_suppressed=True)

    mock_run_pipeline.assert_called_once()  # generation RAN during MAINTENANCE
    mock_alert_critical.assert_not_called()  # operator page DEFERRED
    # Record row still written at CRITICAL (forensic trail preserved).
    severities = [c.args[0] for c in mock_log.call_args_list]
    assert Severity.CRITICAL in severities
    assert "page deferred" in result.summary


def test_catchup_maintenance_success_runs(
    catchup_now, mock_run_pipeline, mock_get_rows
):
    """(f) MAINTENANCE happy path: generation still runs (not @require_active
    blocked) and succeeds."""
    catchup_now(_SAT_AFTER_TRIGGER)
    mock_get_rows.return_value = []

    result = watchdog._check_weekly_generate_catchup(alerts_suppressed=True)

    mock_run_pipeline.assert_called_once_with(week_start_override=_TARGET_MONDAY)
    assert result.severity is Severity.INFO
    assert "catch-up fired" in result.summary


def test_run_check_threads_alerts_suppressed_to_check_i(
    catchup_now, mock_run_pipeline, mock_get_rows, mock_alert_critical
):
    """_run_check must detect Check I's alerts_suppressed kwarg and thread it
    (signature-inspection fork). If it regresses, the MAINTENANCE page would
    fire."""
    catchup_now(_SAT_AFTER_TRIGGER)
    mock_get_rows.return_value = []
    mock_run_pipeline.side_effect = RuntimeError("boom")

    watchdog._run_check(
        watchdog._check_weekly_generate_catchup, alerts_suppressed=True
    )

    mock_run_pipeline.assert_called_once()  # generation ran
    mock_alert_critical.assert_not_called()  # threaded → page deferred


def test_check_i_default_arg_alerts_suppressed_false(
    catchup_now, mock_run_pipeline, mock_get_rows, mock_alert_critical
):
    """Calling Check I without the kwarg must NOT defer — safety default."""
    catchup_now(_SAT_AFTER_TRIGGER)
    mock_get_rows.return_value = []
    mock_run_pipeline.side_effect = RuntimeError("boom")

    watchdog._check_weekly_generate_catchup()  # no kwarg

    mock_alert_critical.assert_called_once()  # NOT deferred


def test_catchup_summary_reports_real_run_counts(
    catchup_now, mock_run_pipeline, mock_get_rows, monkeypatch
):
    """The catch-up INFO summary reports the REAL run_generate counters (packets_compiled /
    wsr_written / errors_per_job), not the never-produced drafts_written/drafts_failed keys.
    (Replaces the old empty-chain test, which exercised a dead branch reading a key
    run_generate never returns.)"""
    catchup_now(_SAT_AFTER_TRIGGER)
    monkeypatch.setattr(watchdog, "_read_marker_datetime", lambda slug: None)
    mock_get_rows.return_value = []
    mock_run_pipeline.return_value = {
        "packets_compiled": 4,
        "wsr_written": 4,
        "errors_per_job": {"JOB-7": "timeout"},
        "correlation_id": "zzz",
    }
    result = watchdog._check_weekly_generate_catchup()
    assert result.severity is Severity.INFO
    assert "4 packet(s) compiled" in result.summary
    assert "4 review row(s) written" in result.summary
    assert "1 job error(s)" in result.summary  # len(errors_per_job)


def test_wsr_rows_exist_failsoft_returns_false(mock_get_rows, mock_log):
    """A Smartsheet read failure during evaluation fails soft → False (the
    decision falls back to the marker signal) and logs a WARN. (P5: the helper is now
    sheet-parameterized — `_review_rows_exist_for_week(sheet_id, week_start)`.)"""
    mock_get_rows.side_effect = SmartsheetError("boom")

    assert (
        watchdog._review_rows_exist_for_week(
            watchdog.sheet_ids.SHEET_WSR_HUMAN_REVIEW, _TARGET_MONDAY
        )
        is False
    )

    severities = [c.args[0] for c in mock_log.call_args_list]
    assert Severity.WARN in severities


# ---- Check J: circuit-breaker prolonged-open (F08/F09 PR 2) --------------


def test_prolonged_open_pages_past_threshold(mocker):
    mocker.patch("watchdog.circuit_breaker.seconds_open", return_value=700.0)
    mocker.patch("watchdog.smartsheet_client.get_setting", return_value="600")
    alert = mocker.patch("watchdog._alert_critical")
    log_mock = mocker.patch("watchdog.log")

    result = watchdog._check_circuit_breaker_prolonged_open()

    alert.assert_called_once()
    assert alert.call_args.kwargs["error_code"] == "circuit_breaker_prolonged_open"
    assert result.severity is Severity.INFO
    # A3: the record log MUST opt out of auto-paging (alert=False); the page
    # fires explicitly under bypass below. Regression lock (the live double-fire
    # / page-in-MAINTENANCE would otherwise be invisible to these mocked tests).
    crit = [c for c in log_mock.call_args_list if c.args[0] is Severity.CRITICAL]
    assert crit and crit[0].kwargs["alert"] is False


def test_prolonged_open_silent_under_threshold(mocker):
    mocker.patch("watchdog.circuit_breaker.seconds_open", return_value=120.0)
    mocker.patch("watchdog.smartsheet_client.get_setting", return_value="600")
    alert = mocker.patch("watchdog._alert_critical")

    result = watchdog._check_circuit_breaker_prolonged_open()

    alert.assert_not_called()
    assert result.severity is Severity.WARN


def test_prolonged_open_not_open_no_alert(mocker):
    mocker.patch("watchdog.circuit_breaker.seconds_open", return_value=None)
    alert = mocker.patch("watchdog._alert_critical")

    result = watchdog._check_circuit_breaker_prolonged_open()

    alert.assert_not_called()
    assert result.severity is Severity.INFO


def test_prolonged_open_threshold_read_fails_open_to_default(mocker):
    """The threshold read short-circuits during the outage; the check must fail
    open to the default (600), so a 700s outage still pages."""
    mocker.patch("watchdog.circuit_breaker.seconds_open", return_value=700.0)
    mocker.patch(
        "watchdog.smartsheet_client.get_setting",
        side_effect=watchdog.smartsheet_client.SmartsheetCircuitOpenError("open"),
    )
    alert = mocker.patch("watchdog._alert_critical")
    mocker.patch("watchdog.log")

    watchdog._check_circuit_breaker_prolonged_open()

    alert.assert_called_once()  # 700 > default 600 → page


def test_prolonged_open_page_runs_under_bypass(mocker):
    """The page MUST fire inside circuit_breaker.bypass() — otherwise the Resend
    leg's operator_email read short-circuits on the very-OPEN breaker."""
    import shared.circuit_breaker as cb

    mocker.patch("watchdog.circuit_breaker.seconds_open", return_value=700.0)
    mocker.patch("watchdog.smartsheet_client.get_setting", return_value="600")
    mocker.patch("watchdog.log")
    seen: dict[str, int] = {}
    mocker.patch(
        "watchdog._alert_critical",
        side_effect=lambda *a, **k: seen.update(depth=cb._bypass_depth),
    )

    watchdog._check_circuit_breaker_prolonged_open()

    assert seen.get("depth", 0) > 0


def test_prolonged_open_maintenance_defers_page(mocker):
    mocker.patch("watchdog.circuit_breaker.seconds_open", return_value=700.0)
    mocker.patch("watchdog.smartsheet_client.get_setting", return_value="600")
    alert = mocker.patch("watchdog._alert_critical")
    mocker.patch("watchdog.log")

    result = watchdog._check_circuit_breaker_prolonged_open(alerts_suppressed=True)

    alert.assert_not_called()  # page deferred during MAINTENANCE
    assert "deferred" in result.summary.lower()


# ---- Check K: guaranteed F09 cap-window-summary sweep (F08/F09 PR 2) -----


def test_cap_window_sweep_calls_maybe_fire(mocker):
    fire = mocker.patch("watchdog._maybe_fire_window_summary")

    result = watchdog._check_alert_rate_cap_window()

    fire.assert_called_once()
    assert result.severity is Severity.INFO


def test_cap_window_sweep_deferred_in_maintenance(mocker):
    fire = mocker.patch("watchdog._maybe_fire_window_summary")

    result = watchdog._check_alert_rate_cap_window(alerts_suppressed=True)

    fire.assert_not_called()
    assert "defer" in result.summary.lower()


# ---- Check S: origin/main required CI green (forensic class #13) ---------


def _fake_gh(
    monkeypatch, *, which="/usr/bin/gh", returncode=0, stdout="", stderr="", head_stdout=None
):
    """Fake BOTH Check S gh calls: `gh run list` (run-list JSON via stdout) and
    `gh api .../commits/main` (HEAD-staleness probe via head_stdout). head_stdout=None
    simulates a HEAD-resolution failure (gh exits 1) — the fail-safe default, so the
    pre-staleness tests keep exercising the green path unchanged."""
    monkeypatch.setattr(watchdog.shutil, "which", lambda _name: which)

    def _run(cmd, *_a, **_k):
        if "api" in cmd:
            if head_stdout is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout=head_stdout, stderr="")
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(watchdog.subprocess, "run", _run)


def test_check_s_registered_in_checks():
    assert watchdog._check_main_branch_ci_green in watchdog.CHECKS


def test_check_s_repo_matches_origin_remote():
    """Check S must watch THIS repo's main — the one whose code the daemons run.

    Until 2026-08-07 `GH_MAIN_CI_REPO` named `SolutionSmith-debug/its`, a different and
    still-active repository, so Check S — the mechanical step 4 of the four-part landing
    verify — reported green about somebody else's code and could never WARN about ours.
    A silent false-green on a landing gate is worse than no gate.

    This pins the slug to the actual `origin` remote so a rename / re-point RED-lights in
    CI instead of quietly re-breaking the check. Skips (never fails) when origin cannot be
    resolved — a shallow tarball or a remote-less checkout must not fail the suite.
    """
    import re
    import subprocess as sp

    try:
        proc = sp.run(
            ["git", "remote", "get-url", "origin"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, sp.TimeoutExpired):  # pragma: no cover — environment-dependent
        pytest.skip("git unavailable")
    if proc.returncode != 0 or not proc.stdout.strip():  # pragma: no cover
        pytest.skip("no origin remote configured")

    # Accept both https://github.com/OWNER/REPO(.git) and git@github.com:OWNER/REPO(.git)
    m = re.search(r"github\.com[:/]([^/]+/[^/\s]+?)(?:\.git)?$", proc.stdout.strip())
    if m is None:  # pragma: no cover — non-GitHub remote
        pytest.skip(f"origin is not a GitHub remote: {proc.stdout.strip()!r}")

    assert watchdog.GH_MAIN_CI_REPO == m.group(1), (
        f"Check S watches {watchdog.GH_MAIN_CI_REPO!r} but origin is {m.group(1)!r} — "
        "Check S would report main-CI status for the WRONG repository and could never "
        "warn about this one. Update GH_MAIN_CI_REPO in scripts/watchdog.py."
    )


def test_check_s_green_main_is_info(monkeypatch):
    _fake_gh(
        monkeypatch,
        stdout=json.dumps([{"status": "completed", "conclusion": "success", "headSha": "abcdef1234"}]),
    )
    result = watchdog._check_main_branch_ci_green()
    assert result.severity is Severity.INFO
    assert "green" in result.summary.lower()


def test_check_s_red_main_is_critical(monkeypatch):
    _fake_gh(
        monkeypatch,
        stdout=json.dumps([{"status": "completed", "conclusion": "failure", "headSha": "deadbeef99"}]),
    )
    result = watchdog._check_main_branch_ci_green()
    assert result.severity is Severity.CRITICAL
    assert "[main-ci-red]" in result.summary
    assert "deadbee" in result.summary  # truncated headSha surfaced


def test_check_s_cancelled_main_is_critical(monkeypatch):
    _fake_gh(
        monkeypatch,
        stdout=json.dumps([{"status": "completed", "conclusion": "cancelled", "headSha": "0011223344"}]),
    )
    assert watchdog._check_main_branch_ci_green().severity is Severity.CRITICAL


def test_check_s_in_progress_is_info_not_critical(monkeypatch):
    _fake_gh(
        monkeypatch,
        stdout=json.dumps([{"status": "in_progress", "conclusion": None, "headSha": "aabbccddee"}]),
    )
    assert watchdog._check_main_branch_ci_green().severity is Severity.INFO


def test_check_s_gh_missing_is_info(monkeypatch):
    _fake_gh(monkeypatch, which=None)
    result = watchdog._check_main_branch_ci_green()
    assert result.severity is Severity.INFO
    assert "gh" in result.summary.lower()


def test_check_s_gh_nonzero_is_info(monkeypatch):
    _fake_gh(monkeypatch, returncode=1, stderr="auth required")
    assert watchdog._check_main_branch_ci_green().severity is Severity.INFO


def test_check_s_no_runs_is_info(monkeypatch):
    _fake_gh(monkeypatch, stdout="[]")
    assert watchdog._check_main_branch_ci_green().severity is Severity.INFO


def test_check_s_timeout_is_info(monkeypatch):
    monkeypatch.setattr(watchdog.shutil, "which", lambda _name: "/usr/bin/gh")

    def _boom(*_a, **_k):
        raise watchdog.subprocess.TimeoutExpired(cmd="gh", timeout=30)

    monkeypatch.setattr(watchdog.subprocess, "run", _boom)
    assert watchdog._check_main_branch_ci_green().severity is Severity.INFO


# ---- Check S HEAD-staleness (2026-07-19 push-event delivery gap) ----------


def test_check_s_green_and_head_match_is_info(monkeypatch):
    """Green latest run AND its sha IS origin/main HEAD → INFO (the true green path)."""
    sha = "a" * 40
    _fake_gh(
        monkeypatch,
        stdout=json.dumps([{"status": "completed", "conclusion": "success", "headSha": sha}]),
        head_stdout=sha + "\n",
    )
    result = watchdog._check_main_branch_ci_green()
    assert result.severity is Severity.INFO
    assert "green" in result.summary.lower()
    assert "[main-ci-stale]" not in result.summary


def test_check_s_green_but_stale_head_is_warn(monkeypatch):
    """Green latest run for a sha that is NOT origin/main HEAD → WARN naming both shas
    + the workflow_dispatch remediation. (2026-07-19: GitHub stopped delivering push
    events for main merges — three merge commits got ZERO runs and the older green run
    read as current indefinitely.) WARN, not CRITICAL — the tree may be verified by
    other means."""
    run_sha = "a" * 40
    head_sha = "b" * 40
    _fake_gh(
        monkeypatch,
        stdout=json.dumps(
            [{"status": "completed", "conclusion": "success", "headSha": run_sha}]
        ),
        head_stdout=head_sha + "\n",
    )
    result = watchdog._check_main_branch_ci_green()
    assert result.severity is Severity.WARN
    assert "[main-ci-stale]" in result.summary
    assert run_sha[:7] in result.summary
    assert head_sha[:7] in result.summary
    assert "gh workflow run ci --ref main" in result.summary


def test_check_s_head_resolution_failure_stays_info(monkeypatch):
    """HEAD-resolution failure is fail-SAFE: the staleness probe is skipped and the
    green path stays INFO — our OWN inability to resolve HEAD never pages."""
    _fake_gh(
        monkeypatch,
        stdout=json.dumps(
            [{"status": "completed", "conclusion": "success", "headSha": "abcdef1234"}]
        ),
        head_stdout=None,  # gh api fails → probe skipped
    )
    result = watchdog._check_main_branch_ci_green()
    assert result.severity is Severity.INFO
    assert "green" in result.summary.lower()


# ---- Check P: portal_poll pending-fetch outage escalation (A4) ------------


def test_portal_poll_fetch_outage_absent_is_info(monkeypatch, tmp_path):
    monkeypatch.setattr(watchdog.portal_poll, "FETCH_FAIL_STATE_PATH", tmp_path / "ff.json")
    result = watchdog._check_portal_poll_fetch_outage()
    assert result.severity is Severity.INFO


def test_portal_poll_fetch_outage_below_threshold_is_info(monkeypatch, tmp_path):
    p = tmp_path / "ff.json"
    p.write_text(json.dumps({"count": 3}))
    monkeypatch.setattr(watchdog.portal_poll, "FETCH_FAIL_STATE_PATH", p)
    result = watchdog._check_portal_poll_fetch_outage()
    assert result.severity is Severity.INFO


def test_portal_poll_fetch_outage_at_threshold_is_critical(monkeypatch, tmp_path):
    p = tmp_path / "ff.json"
    p.write_text(json.dumps({"count": watchdog.portal_poll.FETCH_FAIL_CRITICAL_THRESHOLD}))
    monkeypatch.setattr(watchdog.portal_poll, "FETCH_FAIL_STATE_PATH", p)
    result = watchdog._check_portal_poll_fetch_outage()
    assert result.severity is Severity.CRITICAL
    assert "OUTAGE" in result.summary


def test_portal_poll_fetch_outage_unreadable_is_info(monkeypatch, tmp_path):
    p = tmp_path / "ff.json"
    p.write_text("{not json")
    monkeypatch.setattr(watchdog.portal_poll, "FETCH_FAIL_STATE_PATH", p)
    result = watchdog._check_portal_poll_fetch_outage()
    assert result.severity is Severity.INFO


# ---- Check Q: portal_poll unfiled pending-backlog (A4) -------------------


def test_portal_poll_backlog_absent_is_info(monkeypatch, tmp_path):
    monkeypatch.setattr(watchdog.portal_poll, "PENDING_BACKLOG_STATE_PATH", tmp_path / "bl.json")
    result = watchdog._check_portal_poll_pending_backlog()
    assert result.severity is Severity.INFO


def test_portal_poll_backlog_unlatched_is_info(monkeypatch, tmp_path):
    p = tmp_path / "bl.json"
    p.write_text(json.dumps({"count": 0, "drained": 5, "high_since_utc": None}))
    monkeypatch.setattr(watchdog.portal_poll, "PENDING_BACKLOG_STATE_PATH", p)
    result = watchdog._check_portal_poll_pending_backlog()
    assert result.severity is Severity.INFO


def test_portal_poll_backlog_latched_recent_is_info(monkeypatch, tmp_path):
    recent = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    p = tmp_path / "bl.json"
    p.write_text(json.dumps({"count": 50, "drained": 0, "high_since_utc": recent}))
    monkeypatch.setattr(watchdog.portal_poll, "PENDING_BACKLOG_STATE_PATH", p)
    result = watchdog._check_portal_poll_pending_backlog()
    assert result.severity is Severity.INFO


def test_portal_poll_backlog_latched_sustained_is_warn(monkeypatch, tmp_path):
    old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    p = tmp_path / "bl.json"
    p.write_text(json.dumps({"count": 50, "drained": 0, "high_since_utc": old}))
    monkeypatch.setattr(watchdog.portal_poll, "PENDING_BACKLOG_STATE_PATH", p)
    result = watchdog._check_portal_poll_pending_backlog()
    assert result.severity is Severity.WARN
    assert "STUCK" in result.summary


def test_portal_poll_backlog_unreadable_is_info(monkeypatch, tmp_path):
    p = tmp_path / "bl.json"
    p.write_text("{not json")
    monkeypatch.setattr(watchdog.portal_poll, "PENDING_BACKLOG_STATE_PATH", p)
    result = watchdog._check_portal_poll_pending_backlog()
    assert result.severity is Severity.INFO


def test_portal_poll_backlog_unparseable_ts_is_info(monkeypatch, tmp_path):
    p = tmp_path / "bl.json"
    p.write_text(json.dumps({"count": 50, "drained": 0, "high_since_utc": "not-a-timestamp"}))
    monkeypatch.setattr(watchdog.portal_poll, "PENDING_BACKLOG_STATE_PATH", p)
    result = watchdog._check_portal_poll_pending_backlog()
    assert result.severity is Severity.INFO


# ============================================================================
# P5 watchdog operability — progress Check-I catch-up, Check-C wiring,
# Check T (HELD-row staleness), Check U (approver-drift).
# ============================================================================


# ---- Check-C wiring: both progress slugs tracked + windowed ---------------


def test_progress_slugs_tracked_for_check_c():
    assert "progress_weekly_generate" in watchdog.TRACKED_JOBS
    assert "progress_send_poll" in watchdog.TRACKED_JOBS
    # Weekly compile mirrors safety (8-day window); the 15-min poll gets a 30-min (2-cycle) window.
    assert watchdog.TRACKED_JOB_WINDOWS["progress_weekly_generate"] == timedelta(days=8)
    assert watchdog.TRACKED_JOB_WINDOWS["progress_send_poll"] == timedelta(minutes=30)


# ---- Check I (progress) catch-up ------------------------------------------


@pytest.fixture
def mock_progress_run_generate(mocker):
    """Mock the PROGRESS refire (generate_core.run_generate) at the watchdog import path —
    the REAL dict shape (RunSummary.__dict__ + week + corr id)."""
    return mocker.patch(
        "watchdog.generate_core.run_generate",
        return_value={
            "jobs_processed": 1,
            "packets_compiled": 3,
            "skipped_no_change": 0,
            "empty_weeks": 0,
            "wsr_written": 3,
            "review_queue_entries": 0,
            "timed_out": 0,
            "download_errors": 0,
            "errors_per_job": {},
            "week_start": _TARGET_MONDAY.isoformat(),
            "week_end": (_TARGET_MONDAY + timedelta(days=6)).isoformat(),
            "correlation_id": "prog123",
        },
    )


def _write_pg_marker(ts: datetime) -> None:
    watchdog.WATCHDOG_MARKER_DIR.mkdir(parents=True, exist_ok=True)
    (watchdog.WATCHDOG_MARKER_DIR / "progress_weekly_generate.last_run").write_text(ts.isoformat())


def test_progress_catchup_registered_after_safety_catchup():
    assert watchdog._check_progress_generate_catchup in watchdog.CHECKS
    assert watchdog.CHECKS.index(watchdog._check_progress_generate_catchup) > watchdog.CHECKS.index(
        watchdog._check_weekly_generate_catchup
    )


def test_progress_catchup_fires_when_missing_in_window(
    catchup_now, mock_progress_run_generate, mock_get_rows, monkeypatch
):
    catchup_now(_SAT_AFTER_TRIGGER)
    # No progress marker → "did not run"; WPR has no rows → "produced nothing".
    monkeypatch.setattr(
        watchdog, "_read_marker_datetime", lambda slug: None
    )
    mock_get_rows.return_value = []
    result = watchdog._check_progress_generate_catchup()
    assert result.severity is Severity.INFO
    # Re-fired the PROGRESS compile (generate_core.run_generate with the progress config).
    mock_progress_run_generate.assert_called_once()
    assert "progress_weekly_generate catch-up fired" in result.summary


def test_progress_catchup_no_fire_when_marker_fresh(
    catchup_now, mock_progress_run_generate
):
    catchup_now(_SAT_AFTER_TRIGGER)
    _write_pg_marker(_SAT_AFTER_TRIGGER)  # marker >= trigger → ran
    result = watchdog._check_progress_generate_catchup()
    assert result.severity is Severity.INFO
    mock_progress_run_generate.assert_not_called()
    assert "progress_weekly_generate ran" in result.summary


def test_progress_catchup_failure_uses_progress_error_code(
    catchup_now, mock_progress_run_generate, mock_get_rows, mock_alert_critical, mock_log, monkeypatch
):
    catchup_now(_SAT_AFTER_TRIGGER)
    monkeypatch.setattr(watchdog, "_read_marker_datetime", lambda slug: None)
    mock_get_rows.return_value = []
    mock_progress_run_generate.side_effect = RuntimeError("progress generation boom")
    watchdog._check_progress_generate_catchup()
    # The escalation error_code is label-derived → progress, NOT the safety constant.
    codes = [c.kwargs.get("error_code") for c in mock_log.call_args_list]
    assert "progress_weekly_generate_catchup_failed" in codes
    assert "weekly_generate_catchup_failed" not in codes


# ---- Check T: HELD-row staleness (WSR + WPR) ------------------------------


@pytest.fixture
def _held_state(monkeypatch, tmp_path):
    """Redirect the Check-T first-seen state file to a tmp path."""
    p = tmp_path / "held_first_seen.json"
    monkeypatch.setattr(watchdog, "HELD_ROW_FIRST_SEEN_PATH", p)
    return p


def _held_rows_side_effect(wsr_held=(), wpr_held=()):
    """get_rows side effect returning HELD rows keyed by which sheet is queried."""
    def _se(sheet_id, filters=None):
        if sheet_id == watchdog.sheet_ids.SHEET_WSR_HUMAN_REVIEW:
            return [{"_row_id": r} for r in wsr_held]
        if sheet_id == watchdog.sheet_ids.SHEET_WPR_HUMAN_REVIEW:
            return [{"_row_id": r} for r in wpr_held]
        return []
    return _se


def test_check_t_no_held_rows_is_info(mock_get_rows, _held_state):
    mock_get_rows.side_effect = _held_rows_side_effect()
    result = watchdog._check_stale_held_rows()
    assert result.severity is Severity.INFO
    assert "No review rows stuck HELD" in result.summary


def test_check_t_fresh_held_not_yet_stale_is_info(mock_get_rows, _held_state):
    # A just-seen HELD (state empty → stamped now) is under the threshold → INFO, not WARN.
    mock_get_rows.side_effect = _held_rows_side_effect(wsr_held=[101], wpr_held=[202])
    result = watchdog._check_stale_held_rows()
    assert result.severity is Severity.INFO
    assert "none past" in result.summary


def test_check_t_stale_held_warns(mock_get_rows, _held_state):
    # Pre-seed first-seen with an OLD timestamp → past HELD_ROW_STALE_AFTER → WARN.
    old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    _held_state.write_text(json.dumps({"WSR:101": old, "WPR:202": old}))
    mock_get_rows.side_effect = _held_rows_side_effect(wsr_held=[101], wpr_held=[202])
    result = watchdog._check_stale_held_rows()
    assert result.severity is Severity.WARN
    assert "stuck HELD" in result.summary


def test_check_t_total_read_failure_is_info(mock_get_rows, _held_state):
    mock_get_rows.side_effect = SmartsheetError("both sheets down")
    result = watchdog._check_stale_held_rows()
    assert result.severity is Severity.INFO  # no data → no false WARN


def test_check_t_partial_read_failure_preserves_other_sheets_clock(mock_get_rows, _held_state):
    # BLOCK regression: WSR:101 is genuinely stale (48h). WSR read FAILS this round; WPR succeeds.
    # A partial read failure must NOT reset/prune the stale WSR clock — it must still WARN, and
    # WSR:101 must survive in the persisted state (only successfully-scanned sheets get pruned).
    old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
    _held_state.write_text(json.dumps({"WSR:101": old}))

    def _se(sheet_id, filters=None):
        if sheet_id == watchdog.sheet_ids.SHEET_WSR_HUMAN_REVIEW:
            raise SmartsheetError("transient WSR read failure")
        return [{"_row_id": 202}]  # WPR scans fine, fresh HELD

    mock_get_rows.side_effect = _se
    result = watchdog._check_stale_held_rows()
    assert result.severity is Severity.WARN
    assert "WSR:101" in (result.details or "")
    persisted = json.loads(_held_state.read_text())
    assert "WSR:101" in persisted  # clock preserved through the WSR read failure


# ---- Check U: send-workspace approver-set drift --------------------------


@pytest.fixture
def _approver_state(monkeypatch, tmp_path):
    p = tmp_path / "approver_baseline.json"
    monkeypatch.setattr(watchdog, "APPROVER_BASELINE_PATH", p)
    return p


@pytest.fixture
def mock_share_emails(mocker):
    return mocker.patch("watchdog.smartsheet_client.list_workspace_share_emails")


def test_review_scan_covers_every_send_lane():
    """PARITY TOOTH: Checks N and T must scan every lane's review sheet.

    Both are backstops for the SHARED send engine, which writes its SENDING
    write-ahead marker and every HELD disposition to `cfg.review.SHEET_ID` — i.e.
    whichever review sheet the lane's SendConfig names. So the set of review sheets
    the send lanes use IS the set these checks must scan.

    They fell behind: Check N scanned only WSR and Check T only WSR+WPR, leaving the
    three vendor-facing procurement lanes with no stuck-SENDING alarm at all and no
    24h stale-HELD backstop. Derived from the SendConfigs rather than pinned, because
    a pinned list is exactly how that happened.
    """
    from po_materials import po_send, rfq_send
    from progress_reports import progress_send
    from safety_reports import weekly_send
    from subcontracts import subcontract_send

    required: dict[int, str] = {}
    for name, mod in {
        "weekly_send": weekly_send, "progress_send": progress_send,
        "po_send": po_send, "rfq_send": rfq_send, "subcontract_send": subcontract_send,
    }.items():
        cfg = next(v for v in vars(mod).values() if getattr(v, "review", None) is not None)
        required[cfg.review.SHEET_ID] = name

    for table, label in ((watchdog._REVIEW_SHEETS, "Check N/T review scan"),
                         (watchdog._HELD_SCAN_SHEETS, "Check T _HELD_SCAN_SHEETS")):
        scanned = {row[1] for row in table}
        missing = {sid: lane for sid, lane in required.items() if sid not in scanned}
        assert not missing, (
            f"{label} does not cover these live send lanes: {missing} — add them to "
            "_REVIEW_SHEETS in scripts/watchdog.py, or a row stuck SENDING/HELD on "
            "that lane is never reported"
        )


def test_approver_workspaces_cover_every_send_lane():
    """PARITY TOOTH: Check U must watch every workspace that authorizes a send.

    F22 resolves a lane's approver set from the membership of ONE workspace —
    `send_poll_core.DaemonConfig.f22_workspace_id`. Whatever set of workspaces the
    send daemons verify against is exactly the set Check U has to watch, or a lane's
    approval authority can change with nothing reporting it.

    Derived from the send daemons rather than pinned, because a pinned list is how
    this drifted: `_APPROVER_WORKSPACES` carried only the safety + progress
    workspaces while po_send, rfq_send and subcontract_send went live against the
    procurement workspaces — the three lanes that transmit to outside VENDORS.

    The derivation lives HERE and not in watchdog.py on purpose: importing the send
    daemons into the watchdog would give a monitoring process `send_mail` capability
    (Invariant 1). A test process has no such constraint.
    """
    from po_materials import po_send_poll, rfq_send_poll
    from progress_reports import progress_send_poll
    from safety_reports import weekly_send_poll
    from subcontracts import subcontract_send_poll

    send_daemons = {
        "weekly_send": weekly_send_poll,
        "progress_send": progress_send_poll,
        "po_send": po_send_poll,
        "rfq_send": rfq_send_poll,
        "subcontract_send": subcontract_send_poll,
    }
    required: dict[int, list[str]] = {}
    for name, mod in send_daemons.items():
        cfg = next(
            v for v in vars(mod).values()
            if isinstance(getattr(v, "f22_workspace_id", None), int)
        )
        required.setdefault(cfg.f22_workspace_id, []).append(name)

    watched = {ws_id for _label, ws_id in watchdog._APPROVER_WORKSPACES}
    missing = {
        ws: lanes for ws, lanes in required.items() if ws not in watched
    }
    assert not missing, (
        "send lanes whose F22 approver workspace Check U does NOT watch: "
        f"{ {ws: lanes for ws, lanes in missing.items()} } — add the workspace to "
        "_APPROVER_WORKSPACES in scripts/watchdog.py, or approver drift on that "
        "send authority goes unreported (Op Stds §46)"
    )


def test_check_u_empty_approver_set_warns(mock_share_emails, _approver_state):
    mock_share_emails.return_value = frozenset()  # no approvers shared anywhere
    result = watchdog._check_approver_drift()
    assert result.severity is Severity.WARN
    assert "EMPTY send-approver set" in result.summary


def test_check_u_first_run_seeds_baseline_no_drift(mock_share_emails, _approver_state):
    mock_share_emails.return_value = frozenset({"approver@evergreenmirror.com"})
    result = watchdog._check_approver_drift()
    assert result.severity is Severity.INFO  # first run seeds the baseline; no drift reported
    assert _approver_state.exists()  # baseline persisted


def test_check_u_membership_change_warns_drift(mock_share_emails, _approver_state):
    # Seed a baseline, then change membership → WARN naming the delta.
    _approver_state.write_text(json.dumps({
        "Safety Portal": ["a@x.com"],
        "Progress Reporting": ["a@x.com"],
    }))
    mock_share_emails.return_value = frozenset({"a@x.com", "b@x.com"})  # b@ added
    result = watchdog._check_approver_drift()
    assert result.severity is Severity.WARN
    assert "CHANGED" in result.summary


def test_check_u_read_failure_is_info(mock_share_emails, _approver_state):
    mock_share_emails.side_effect = SmartsheetError("workspace read down")
    result = watchdog._check_approver_drift()
    assert result.severity is Severity.INFO  # no data → never invents drift


# ---- Check X: stale / stopped job archives (#25) --------------------------
#
# THE GAP THIS CLOSES. Before Check X, NO watchdog check covered the job archive —
# `grep archive scripts/watchdog.py` hit only Check W (log rotation). On 2026-08-10
# JOB-000030 sat at `requested` with attempts=0 for hours while the portal showed a
# green "Waiting for the office Mac to pick this up" banner, and nothing fired.
#
# The two shapes under test are NOT interchangeable, and the second is the one an
# archive-pending-based check could never have seen: `partial`/`failed` are TERMINAL
# for the daemon, so a half-relocated job leaves the queue and waits for a human.


def _archive_row(**over):
    """One `/archive-health` row. Defaults: a request raised ~2h ago, still queued."""
    row = {
        "job_id": "JOB-000030",
        "project_name": "Production test",
        "job_no": "30",
        "archive_folder_key": "Production test",
        "archive_direction": "archive",
        "archive_state": "requested",
        "archive_attempts": 0,
        "archive_requested_at": int(datetime.now(UTC).timestamp()) - 7200,
    }
    row.update(over)
    return row


@pytest.fixture
def archive_env(monkeypatch, mocker, tmp_path):
    """Creds resolved, gate ON, counter isolated. Returns the health-route mock."""
    monkeypatch.setattr(
        watchdog, "_resolve_fieldops_creds", lambda: ("https://worker.test", "tok")
    )
    monkeypatch.setattr(watchdog.fieldops_sync, "_archive_enabled", lambda: True)
    monkeypatch.setattr(
        watchdog,
        "_ARCHIVE_STALE_RUNS",
        watchdog.sustained_failure.SustainedFailureCounter(
            tmp_path / "archive_stale_runs.json", "test", "x"
        ),
    )
    return mocker.patch("watchdog.portal_client.get_fieldops_archive_health")


def test_check_x_registered_in_checks():
    assert watchdog._check_stale_job_archives in watchdog.CHECKS
    assert watchdog.CHECK_LETTERS["_check_stale_job_archives"] == "X"


def test_check_x_is_on_the_daily_tier(archive_env):
    """The conditions are standing, not transient — hourly buys no earlier remedy."""
    assert watchdog._check_stale_job_archives in watchdog.DAILY_ONLY_CHECKS


def test_a_terminal_partial_escalates_even_though_the_queue_has_dropped_it(archive_env):
    """THE regression for the blind spot: partial/failed never resume on their own.

    A check reading /archive-pending would see nothing here and report healthy.
    """
    archive_env.return_value = [_archive_row(archive_state="partial", archive_attempts=1)]

    result = watchdog._check_stale_job_archives()

    assert result.severity is Severity.WARN
    assert "STOPPED" in result.summary
    assert "will NOT retry" in result.summary
    assert "Try again" in result.summary  # the actual remedy, named
    assert "JOB-000030" in result.summary


def test_a_failed_archive_is_reported_regardless_of_age(archive_env):
    """Terminal is terminal — a fresh `failed` is no less stuck than an old one."""
    archive_env.return_value = [
        _archive_row(
            archive_state="failed",
            archive_requested_at=int(datetime.now(UTC).timestamp()) - 5,
        )
    ]
    assert watchdog._check_stale_job_archives().severity is Severity.WARN


def test_a_request_aging_in_the_queue_escalates_and_reports_the_facts(archive_env):
    archive_env.return_value = [_archive_row()]  # 2h old, still `requested`

    result = watchdog._check_stale_job_archives()

    assert result.severity is Severity.WARN
    assert "STUCK in the queue" in result.summary
    # job_id, direction, attempts and age all present — what the operator acts on.
    assert "JOB-000030" in result.summary
    assert "archive/requested" in result.summary
    assert "attempts=0" in result.summary
    assert "2.0h" in result.summary


def test_a_freshly_raised_request_is_not_yet_stuck(archive_env):
    """A healthy relocation completes in one ~90s cycle; don't page on the normal window."""
    archive_env.return_value = [
        _archive_row(archive_requested_at=int(datetime.now(UTC).timestamp()) - 60)
    ]
    result = watchdog._check_stale_job_archives()
    assert result.severity is Severity.INFO
    assert "in flight" in result.summary


def test_gate_off_is_named_as_the_likely_cause(archive_env, monkeypatch):
    """The 2026-08-10 incident exactly: the pass was not draining the queue at all."""
    monkeypatch.setattr(watchdog.fieldops_sync, "_archive_enabled", lambda: False)
    archive_env.return_value = [_archive_row()]

    result = watchdog._check_stale_job_archives()

    assert "GATED OFF" in result.summary
    assert watchdog.fieldops_sync.CFG_ARCHIVE_ENABLED in result.summary


def test_gate_on_says_so_rather_than_leaving_the_operator_guessing(archive_env):
    archive_env.return_value = [_archive_row()]
    result = watchdog._check_stale_job_archives()
    assert "IS enabled" in result.summary
    assert "docs/runbooks/job_archive.md" in result.summary


def test_no_stalled_archive_is_info_and_resets_the_streak(archive_env):
    archive_env.return_value = []
    result = watchdog._check_stale_job_archives()
    assert result.severity is Severity.INFO
    assert "healthy" in result.summary


def test_sustained_stall_escalates_on_the_capped_ladder(archive_env):
    """WARN daily, CRITICAL on the crossing run then 2x/4x/8x — NOT every run.

    An open CRITICAL is never terminal (`shared/errors_rotation`), so a CRITICAL per
    daily sweep while a job sits unarchived would mint permanent unrotatable rows
    against the 20k cap that has locked out before.
    """
    archive_env.return_value = [_archive_row(archive_state="partial")]
    sev = [watchdog._check_stale_job_archives().severity for _ in range(7)]
    # Threshold 3: runs 3 and 6 fire, the rest stay WARN.
    assert sev == [
        Severity.WARN, Severity.WARN, Severity.CRITICAL,
        Severity.WARN, Severity.WARN, Severity.CRITICAL, Severity.WARN,
    ]


def test_a_malformed_request_time_counts_as_stuck_rather_than_silently_healthy(archive_env):
    """Bad data must not make the detector go quiet — that is the failure it exists for."""
    archive_env.return_value = [_archive_row(archive_requested_at=None)]
    result = watchdog._check_stale_job_archives()
    assert result.severity is Severity.WARN
    assert "age unknown" in result.summary


def test_transport_failure_is_info_not_a_manufactured_outage(archive_env):
    archive_env.side_effect = watchdog.portal_client.PortalTransportError("boom")
    result = watchdog._check_stale_job_archives()
    assert result.severity is Severity.INFO
    assert "unreachable" in result.summary


def test_rejected_bearer_warns_because_the_check_is_now_blind(archive_env):
    archive_env.side_effect = watchdog.portal_client.PortalAuthError("401")
    result = watchdog._check_stale_job_archives()
    assert result.severity is Severity.WARN
    assert "unobserved" in result.summary


def test_unresolved_creds_skip_quietly(monkeypatch, archive_env):
    monkeypatch.setattr(watchdog, "_resolve_fieldops_creds", lambda: None)
    result = watchdog._check_stale_job_archives()
    assert result.severity is Severity.INFO


def test_per_job_detail_is_bounded(archive_env):
    """One bad day must not write a multi-KB summary into the 20k-capped ITS_Errors."""
    archive_env.return_value = [
        _archive_row(job_id=f"JOB-{i:06d}", archive_state="failed") for i in range(50)
    ]
    result = watchdog._check_stale_job_archives()
    assert "50 STOPPED" in result.summary  # the COUNT is honest
    named = sum(1 for i in range(50) if f"JOB-{i:06d}" in result.summary)
    assert named == watchdog.ARCHIVE_REPORT_MAX_JOBS


def test_reads_the_health_route_and_never_the_work_queue(archive_env, mocker):
    """Check X observes; it must never look like the pass claiming work."""
    pending = mocker.patch("watchdog.portal_client.get_fieldops_pending_archives")
    progress = mocker.patch("watchdog.portal_client.post_fieldops_archive_progress")
    archive_env.return_value = [_archive_row(archive_state="partial")]

    watchdog._check_stale_job_archives()

    archive_env.assert_called_once()
    pending.assert_not_called()
    progress.assert_not_called()


# ---- Check Y: verify_cutover VC-03, run daily instead of never (#27) ------
#
# THE GAP THIS CLOSES. verify_cutover is the ONLY tool comparing declared load-bearing
# config to the LIVE tenant, and it ran nowhere — not CI, not launchd, not install.sh.
# On 2026-08-10 the Track 6 archive sat inert three days because two ITS_Config rows did
# not exist; VC-03 names both precisely. Detection was never the gap: required_config
# WARNed `NO ROW` 3,442 times and a WARN never triple-fires.
#
# The tests that matter most here are the FAIL-SOFT ones. This check resolves ~53 enrolled
# rows from ONE sheet read, so a broken read makes every row look missing — and firing 53
# CRITICALs into a 20,000-row-capped sheet that has locked out before would be a far worse
# outage than the one being detected.


def _config_row(setting, workstream, value):
    return {"Setting": setting, "Workstream": workstream, "Value": value}


def _rows_for(cfg_rows, *, value="seeded-value", pad=True):
    """A healthy ITS_Config row set covering every enrolled ConfigRow.

    `requirement == "true"` rows get "true" so the healthy baseline is genuinely clean.
    `pad` adds filler rows so the index clears CUTOVER_CONFIG_MIN_INDEX_ROWS even when a
    test enrolls only a couple of rows.
    """
    rows = [
        _config_row(c.key, c.workstream, "true" if c.requirement == "true" else value)
        for c in cfg_rows
    ]
    if pad:
        rows += [
            _config_row(f"filler.key_{i}", "global", "x")
            for i in range(watchdog.CUTOVER_CONFIG_MIN_INDEX_ROWS)
        ]
    return rows


@pytest.fixture
def cutover_env(monkeypatch, mocker, tmp_path):
    """Counter isolated to tmp_path. Returns the ITS_Config get_rows mock."""
    monkeypatch.setattr(
        watchdog,
        "_CUTOVER_CONFIG_FAILS",
        watchdog.sustained_failure.SustainedFailureCounter(
            tmp_path / "cutover_config_fails.json", "test", "y"
        ),
    )
    return mocker.patch("watchdog.smartsheet_client.get_rows")


@pytest.fixture
def vc(cutover_env):
    """The live verify_cutover module, imported the same bare way Check Y imports it."""
    import verify_cutover

    return verify_cutover


def test_check_y_registered_in_checks():
    assert watchdog._check_cutover_config in watchdog.CHECKS
    assert watchdog.CHECK_LETTERS["_check_cutover_config"] == "Y"


def test_check_y_is_on_the_daily_tier():
    """The enrolled set only changes when CODE merges; hourly buys nothing and costs a
    full-sheet cross-network read 24x/day."""
    assert watchdog._check_cutover_config in watchdog.DAILY_ONLY_CHECKS


def test_healthy_tenant_is_info(cutover_env, vc):
    cutover_env.return_value = _rows_for(vc.CONFIG_ROWS)

    result = watchdog._check_cutover_config()

    assert result.severity is Severity.INFO
    assert "clean" in result.summary


def test_a_missing_row_is_critical_on_the_first_sweep(cutover_env, vc):
    """THE 2026-08-10 case: a load-bearing row that does not exist is an INVISIBLE
    off-switch, and it must page the very first time it is seen — not on day three."""
    rows = [r for r in _rows_for(vc.CONFIG_ROWS)
            if r["Setting"] != "field_ops.fieldops_sync.archive_enabled"]
    cutover_env.return_value = rows

    result = watchdog._check_cutover_config()

    assert result.severity is Severity.CRITICAL
    assert "field_ops.fieldops_sync.archive_enabled" in result.summary
    assert "MISSING" in result.summary


def test_a_blank_value_is_critical_exactly_like_a_missing_row(cutover_env, vc):
    """`_read_bool_setting(default=False)` cannot tell blank from missing, so neither
    may this check — splitting them would let the quieter half of one bug through."""
    rows = _rows_for(vc.CONFIG_ROWS)
    for r in rows:
        if r["Setting"] == "field_ops.fieldops_sync.archive_enabled":
            r["Value"] = "   "

    cutover_env.return_value = rows

    result = watchdog._check_cutover_config()

    assert result.severity is Severity.CRITICAL
    assert "BLANK" in result.summary


def test_a_paused_true_gate_is_only_a_warn(cutover_env, vc):
    """A gate an operator deliberately turned off is a CHOICE, not a defect — VC-03
    itself refuses to force 'true'. Paging for it is how a runner earns being ignored."""
    true_row = next(c for c in vc.CONFIG_ROWS if c.requirement == "true")
    rows = _rows_for(vc.CONFIG_ROWS)
    for r in rows:
        if r["Setting"] == true_row.key and r["Workstream"] == true_row.workstream:
            r["Value"] = "false"
    cutover_env.return_value = rows

    result = watchdog._check_cutover_config()

    assert result.severity is Severity.WARN
    assert "PAUSED" in result.summary
    assert true_row.key in result.summary


def test_sandbox_residue_is_only_a_warn(cutover_env, vc):
    """Three rows carry the mirror value today. Expected pre-cutover -> WARN, and no
    `system.cutover_profile` mute row: that would be an operator-editable silencer for
    this very check, and a dark-shipped row reproducing the bug being fixed."""
    scanned = next(c for c in vc.CONFIG_ROWS if c.sandbox_scan)
    rows = _rows_for(vc.CONFIG_ROWS)
    for r in rows:
        if r["Setting"] == scanned.key and r["Workstream"] == scanned.workstream:
            r["Value"] = "https://safety.evergreenmirror.com"
    cutover_env.return_value = rows

    result = watchdog._check_cutover_config()

    assert result.severity is Severity.WARN
    assert "sandbox" in result.summary


def test_a_read_exception_asserts_nothing(cutover_env):
    """FAIL-SOFT. Our own inability to read ITS_Config is not evidence of drift, and a
    breaker short-circuit here would otherwise mint ~53 CRITICALs in one sweep."""
    cutover_env.side_effect = watchdog.smartsheet_client.SmartsheetError("breaker open")

    result = watchdog._check_cutover_config()

    assert result.severity is Severity.INFO
    assert "asserting nothing" in result.summary


def test_a_renamed_column_asserts_nothing_despite_a_healthy_row_count(cutover_env, vc):
    """THE guard that a row-count check could never provide.

    A renamed `Setting`/`Workstream` column returns ~118 perfectly healthy-looking rows
    that index to NOTHING. Any `len(rows) >= N` guard sails straight past it and reports
    every enrolled row as missing. The floor is on the RESOLVED INDEX for this reason.
    """
    cutover_env.return_value = [
        {"SettingName": f"k{i}", "Stream": "global", "Value": "v"} for i in range(118)
    ]

    result = watchdog._check_cutover_config()

    assert result.severity is Severity.INFO
    assert "asserting nothing" in result.summary
    assert "renamed" in result.summary


def test_an_empty_sheet_asserts_nothing(cutover_env):
    cutover_env.return_value = []

    assert watchdog._check_cutover_config().severity is Severity.INFO


def test_missing_rows_ride_the_capped_ladder_not_a_daily_page(cutover_env, vc):
    """An open CRITICAL is NEVER terminal (shared/errors_rotation), so paging every daily
    sweep while a row stays unseeded mints permanent unrotatable rows against the 20,000
    cap that has locked out before. Rungs at 1, 2, 4, 8 then every 8."""
    cutover_env.return_value = [
        r for r in _rows_for(vc.CONFIG_ROWS)
        if r["Setting"] != "field_ops.fieldops_sync.archive_enabled"
    ]

    sev = [watchdog._check_cutover_config().severity for _ in range(9)]

    assert [s is Severity.CRITICAL for s in sev] == [
        True, True, False, True, False, False, False, True, False
    ]


def test_the_streak_resets_once_every_row_is_seeded(cutover_env, vc):
    """A re-seeded row must not leave the ladder mid-climb — the next regression has to
    page on ITS first sweep, not on whatever rung the previous one left behind."""
    cutover_env.return_value = [
        r for r in _rows_for(vc.CONFIG_ROWS)
        if r["Setting"] != "field_ops.fieldops_sync.archive_enabled"
    ]
    for _ in range(3):
        watchdog._check_cutover_config()

    cutover_env.return_value = _rows_for(vc.CONFIG_ROWS)
    assert watchdog._check_cutover_config().severity is Severity.INFO

    cutover_env.return_value = [
        r for r in _rows_for(vc.CONFIG_ROWS)
        if r["Setting"] != "field_ops.fieldops_sync.archive_enabled"
    ]
    assert watchdog._check_cutover_config().severity is Severity.CRITICAL


def test_the_named_rows_are_capped(cutover_env):
    """One bad day must not write a multi-KB summary into ITS_Errors."""
    cutover_env.return_value = [
        _config_row(f"filler.key_{i}", "global", "x")
        for i in range(watchdog.CUTOVER_CONFIG_MIN_INDEX_ROWS)
    ]

    result = watchdog._check_cutover_config()

    assert result.severity is Severity.CRITICAL
    assert result.summary.count("row MISSING") == watchdog.CUTOVER_CONFIG_REPORT_MAX_ROWS


def test_check_y_reads_the_sheet_exactly_once(cutover_env, vc):
    """`get_setting` does a FULL sheet fetch per call; resolving ~53 rows key-by-key
    would be ~53 fetches per sweep."""
    cutover_env.return_value = _rows_for(vc.CONFIG_ROWS)

    watchdog._check_cutover_config()

    cutover_env.assert_called_once()


def test_check_y_never_writes_to_its_config(cutover_env, vc, mocker):
    """Check Y observes the switchboard; it must never edit it."""
    update = mocker.patch("watchdog.smartsheet_client.update_rows")
    add = mocker.patch("watchdog.smartsheet_client.add_rows")
    cutover_env.return_value = _rows_for(vc.CONFIG_ROWS)

    watchdog._check_cutover_config()

    update.assert_not_called()
    add.assert_not_called()


def test_verify_cutover_is_imported_lazily_not_at_module_scope():
    """⛔ THE BLOCKING CONSTRAINT. `scripts/` has no `__init__.py`, the editable install
    declares 8 packages and none is named `scripts`, and the plist runs
    `.venv/bin/python __ITS_HOME__/scripts/watchdog.py` — so a module-scope import here
    would raise ModuleNotFoundError under launchd and take down ALL of CHECKS, the entire
    observability spine, silently (the Healthchecks.io dead-man's switch is not armed).

    Asserts BOTH properties mechanically: no module-scope import of verify_cutover, and
    the import that does exist is the BARE form inside the check function. A
    `from scripts import verify_cutover` also makes mypy see one file under two module
    names (tests/test_job_archive.py records that).
    """
    import ast
    import inspect
    from pathlib import Path

    source = Path(watchdog.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:  # MODULE SCOPE only — nested imports are the point
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("verify_cutover") for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module not in ("verify_cutover", "scripts", "scripts.verify_cutover")

    fn = ast.parse(inspect.getsource(watchdog._check_cutover_config).lstrip())
    imports = [n for n in ast.walk(fn) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert any(
        isinstance(n, ast.Import) and any(a.name == "verify_cutover" for a in n.names)
        for n in imports
    ), "Check Y must `import verify_cutover` BARE, inside the function"
    assert not any(isinstance(n, ast.ImportFrom) for n in imports), (
        "`from scripts import verify_cutover` breaks under launchd (sys.path[0] is scripts/) "
        "and makes mypy see the file under two module names"
    )


# ---- Check V: D1 prune heartbeat (GS2) ------------------------------------


def _now_epoch() -> float:
    return datetime.now(UTC).timestamp()


def _prune_meta(**overrides):
    """A healthy prune_meta dict; override fields per scenario."""
    meta = {
        "last_run_at": int(_now_epoch()) - 3600,  # 1h ago — fresh
        "db_size_bytes": 4096,
        "size_warn": False,
        "counters": {"submissions": 0, "jobs": 0},
        "failed_stages": [],
    }
    meta.update(overrides)
    return meta


@pytest.fixture
def _prune_creds(monkeypatch):
    """Resolve creds deterministically — the checks under test mock the transport."""
    monkeypatch.setattr(
        watchdog, "_resolve_prune_status_creds", lambda: ("https://worker.test", "tok")
    )


@pytest.fixture
def mock_prune_status(mocker):
    return mocker.patch("watchdog.portal_client.get_prune_status")


def test_check_v_registered_in_checks():
    assert watchdog._check_portal_prune_health in watchdog.CHECKS


def test_check_v_healthy_is_info(_prune_creds, mock_prune_status):
    mock_prune_status.return_value = _prune_meta()
    result = watchdog._check_portal_prune_health()
    assert result.severity is Severity.INFO
    assert "healthy" in result.summary


def test_check_v_stale_last_run_warns(_prune_creds, mock_prune_status):
    # 49h ago — past the 48h staleness window (the daily cron missed ~2 runs).
    mock_prune_status.return_value = _prune_meta(
        last_run_at=int(_now_epoch()) - 49 * 3600
    )
    result = watchdog._check_portal_prune_health()
    assert result.severity is Severity.WARN
    assert "STALE" in result.summary


def test_check_v_failed_stages_is_critical(_prune_creds, mock_prune_status):
    mock_prune_status.return_value = _prune_meta(failed_stages=["audit", "jobs"])
    result = watchdog._check_portal_prune_health()
    assert result.severity is Severity.CRITICAL
    assert "audit" in result.summary and "jobs" in result.summary


def test_check_v_failed_stages_beats_staleness(_prune_creds, mock_prune_status):
    # Both conditions present → CRITICAL (the failure flag) wins over the stale WARN.
    mock_prune_status.return_value = _prune_meta(
        failed_stages=["strip"], last_run_at=int(_now_epoch()) - 100 * 3600
    )
    result = watchdog._check_portal_prune_health()
    assert result.severity is Severity.CRITICAL


def test_check_v_db_size_over_6gb_is_critical(_prune_creds, mock_prune_status):
    mock_prune_status.return_value = _prune_meta(db_size_bytes=6_000_000_001)
    result = watchdog._check_portal_prune_health()
    assert result.severity is Severity.CRITICAL
    assert "D1 size" in result.summary


def test_check_v_db_size_at_threshold_is_not_critical(_prune_creds, mock_prune_status):
    # Strictly-greater-than: exactly 6GB does not page.
    mock_prune_status.return_value = _prune_meta(db_size_bytes=6_000_000_000)
    result = watchdog._check_portal_prune_health()
    assert result.severity is Severity.INFO


def test_check_v_meta_absent_warns(_prune_creds, mock_prune_status):
    mock_prune_status.return_value = None  # Worker reported prune: null — never ran
    result = watchdog._check_portal_prune_health()
    assert result.severity is Severity.WARN
    assert "ABSENT" in result.summary


def test_check_v_malformed_last_run_warns(_prune_creds, mock_prune_status):
    mock_prune_status.return_value = _prune_meta(last_run_at="not-a-number")
    result = watchdog._check_portal_prune_health()
    assert result.severity is Severity.WARN
    assert "malformed" in result.summary


def test_check_v_malformed_failed_stages_is_critical(_prune_creds, mock_prune_status):
    # A corrupted failure flag must never read as a clean run.
    mock_prune_status.return_value = _prune_meta(failed_stages="not-a-list")
    result = watchdog._check_portal_prune_health()
    assert result.severity is Severity.CRITICAL


def test_check_v_transport_error_is_info(_prune_creds, mock_prune_status):
    from shared import portal_client

    mock_prune_status.side_effect = portal_client.PortalTransportError("worker down")
    result = watchdog._check_portal_prune_health()
    assert result.severity is Severity.INFO  # transient — never masks, never invents


def test_check_v_auth_error_warns(_prune_creds, mock_prune_status):
    from shared import portal_client

    mock_prune_status.side_effect = portal_client.PortalAuthError("401")
    result = watchdog._check_portal_prune_health()
    assert result.severity is Severity.WARN  # deterministic misconfig — will not self-heal
    assert "401" in result.summary


def test_check_v_unresolved_creds_is_info(monkeypatch, mock_prune_status):
    monkeypatch.setattr(watchdog, "_resolve_prune_status_creds", lambda: None)
    result = watchdog._check_portal_prune_health()
    assert result.severity is Severity.INFO
    mock_prune_status.assert_not_called()  # no creds → no HTTP attempt


def test_resolve_prune_creds_fail_soft(monkeypatch):
    # Smartsheet down → None, never raises (portal_poll owns the missing-creds page).
    def _boom(*a, **k):
        raise SmartsheetError("config read down")

    monkeypatch.setattr(watchdog.smartsheet_client, "get_setting", _boom)
    assert watchdog._resolve_prune_status_creds() is None


def test_resolve_prune_creds_happy_path(monkeypatch):
    monkeypatch.setattr(
        watchdog.smartsheet_client, "get_setting", lambda *a, **k: "https://worker.test"
    )
    monkeypatch.setattr(watchdog.keychain, "get_secret", lambda name: "tok")
    assert watchdog._resolve_prune_status_creds() == ("https://worker.test", "tok")

# ---- Check O: ITS_Errors + ITS_Review_Queue row-cap rotation (A5) --------
#
# Seams match the existing suite: `watchdog.smartsheet_client.get_rows` /
# `.delete_rows` mocked; the rotation-summary record asserts against
# `watchdog.log` (the same seam _run_check routing tests use). Thresholds are
# monkeypatched small so the tests never build 16,000-row fixtures — the
# check reads them from `watchdog.defaults` at call time.


def _shrink_row_caps(mocker, *, warn=5, rotate=8, batch=200, max_batches=10):
    mocker.patch.object(watchdog.defaults, "SHEET_ROW_WARN_THRESHOLD", warn)
    mocker.patch.object(watchdog.defaults, "SHEET_ROW_ROTATE_THRESHOLD", rotate)
    mocker.patch.object(watchdog.defaults, "SHEET_ROW_ROTATION_DELETE_BATCH", batch)
    mocker.patch.object(
        watchdog.defaults, "SHEET_ROW_ROTATION_MAX_BATCHES_PER_RUN", max_batches
    )


def _iso_days_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def _errors_row(row_id, *, severity="WARN", days_old=200, resolved_at=None, ts=...):
    row = {
        "_row_id": row_id,
        "Severity": severity,
        "Error": "some_code",
        "Timestamp": _iso_days_ago(days_old) if ts is ... else ts,
    }
    if resolved_at is not None:
        row["Resolved At"] = resolved_at
    return row


def _rq_row(row_id, *, status="APPROVED", days_old=200):
    return {
        "_row_id": row_id,
        "Status": status,
        "Created At": _iso_days_ago(days_old),
    }


@pytest.fixture
def rotation_rows(mocker):
    """Route get_rows by sheet id; both sheets default to empty (healthy)."""
    per_sheet: dict[int, object] = {
        watchdog.sheet_ids.SHEET_ERRORS: [],
        watchdog.sheet_ids.SHEET_REVIEW_QUEUE: [],
    }

    def _get_rows(sheet_id, **kwargs):
        value = per_sheet[sheet_id]
        if isinstance(value, Exception):
            raise value
        return value

    mocker.patch.object(watchdog.smartsheet_client, "get_rows", side_effect=_get_rows)
    return per_sheet


@pytest.fixture
def mock_delete_rows(mocker):
    return mocker.patch.object(watchdog.smartsheet_client, "delete_rows")


# -- terminality predicates (the load-bearing eligibility filter) ----------


def test_errors_terminality_non_critical_is_terminal():
    assert watchdog._errors_row_is_terminal(_errors_row(1, severity="WARN"))
    assert watchdog._errors_row_is_terminal(_errors_row(1, severity="ERROR"))
    assert watchdog._errors_row_is_terminal(_errors_row(1, severity="INFO"))


def test_errors_terminality_critical_needs_resolved_at():
    # Open CRITICAL (blank/missing Resolved At) is NEVER terminal — Check B's
    # working set survives every rotation.
    assert not watchdog._errors_row_is_terminal(_errors_row(1, severity="CRITICAL"))
    assert not watchdog._errors_row_is_terminal(
        _errors_row(1, severity="CRITICAL", resolved_at="")
    )
    assert watchdog._errors_row_is_terminal(
        _errors_row(1, severity="CRITICAL", resolved_at="2026-01-01")
    )


def test_review_queue_terminality_only_drained_states():
    assert watchdog._review_queue_row_is_terminal(_rq_row(1, status="APPROVED"))
    assert watchdog._review_queue_row_is_terminal(_rq_row(1, status="REJECTED"))
    # PENDING / IN_REVIEW / ESCALATED are open work — never terminal.
    assert not watchdog._review_queue_row_is_terminal(_rq_row(1, status="PENDING"))
    assert not watchdog._review_queue_row_is_terminal(_rq_row(1, status="IN_REVIEW"))
    assert not watchdog._review_queue_row_is_terminal(_rq_row(1, status="ESCALATED"))


# -- under-mark / warn-band behavior ---------------------------------------


def test_row_caps_under_mark_is_info_noop(rotation_rows, mock_delete_rows, mocker):
    _shrink_row_caps(mocker, warn=5, rotate=8)
    rotation_rows[watchdog.sheet_ids.SHEET_ERRORS] = [
        _errors_row(i) for i in range(3)
    ]
    result = watchdog._check_row_cap_rotation()
    assert result.severity is Severity.INFO
    mock_delete_rows.assert_not_called()


def test_row_caps_warn_band_warns_without_rotating(
    rotation_rows, mock_delete_rows, mocker
):
    _shrink_row_caps(mocker, warn=5, rotate=8)
    rotation_rows[watchdog.sheet_ids.SHEET_ERRORS] = [
        _errors_row(i) for i in range(6)  # 5 ≤ 6 < 8
    ]
    result = watchdog._check_row_cap_rotation()
    assert result.severity is Severity.WARN
    assert "warn mark" in result.details
    mock_delete_rows.assert_not_called()


# -- rotation: eligibility, ordering, survivors, summary record -------------


def test_row_caps_rotation_deletes_only_eligible_and_writes_summary(
    rotation_rows, mock_delete_rows, mock_log, mocker
):
    _shrink_row_caps(mocker, warn=3, rotate=5)
    rotation_rows[watchdog.sheet_ids.SHEET_ERRORS] = [
        _errors_row(1, severity="WARN", days_old=200),                      # eligible
        _errors_row(2, severity="CRITICAL", days_old=300),                   # OPEN CRITICAL — survives
        _errors_row(3, severity="CRITICAL", days_old=250, resolved_at="x"),  # resolved — eligible
        _errors_row(4, severity="ERROR", days_old=10),                       # too young — survives
        _errors_row(5, severity="WARN", ts=None),                            # unprovable age — survives
        _errors_row(6, severity="WARN", ts="not-a-date"),                    # unparseable — survives
    ]
    result = watchdog._check_row_cap_rotation()

    assert result.severity is Severity.WARN
    mock_delete_rows.assert_called_once()
    sheet_id, deleted_ids = mock_delete_rows.call_args.args
    assert sheet_id == watchdog.sheet_ids.SHEET_ERRORS
    # Oldest first: row 3 (250d-old RESOLVED critical), then row 1 (200d WARN).
    # Row 2 — the 300d-old OPEN critical — survives despite being oldest.
    assert deleted_ids == [3, 1]
    # The never-silent rotation record landed with the stable error code.
    rotation_records = [
        c for c in mock_log.call_args_list
        if c.kwargs.get("error_code") == "row_cap_rotation"
    ]
    assert len(rotation_records) == 1
    assert "rotated 2 of 2" in rotation_records[0].args[2]


def test_row_caps_rotation_never_deletes_open_work_in_review_queue(
    rotation_rows, mock_delete_rows, mocker
):
    _shrink_row_caps(mocker, warn=3, rotate=5)
    rotation_rows[watchdog.sheet_ids.SHEET_REVIEW_QUEUE] = [
        _rq_row(11, status="PENDING", days_old=400),    # NEVER
        _rq_row(12, status="IN_REVIEW", days_old=400),  # never
        _rq_row(13, status="ESCALATED", days_old=400),  # never (open escalation)
        _rq_row(14, status="APPROVED", days_old=120),   # eligible
        _rq_row(15, status="REJECTED", days_old=100),   # eligible
        _rq_row(16, status="APPROVED", days_old=5),     # too young
    ]
    watchdog._check_row_cap_rotation()
    mock_delete_rows.assert_called_once()
    sheet_id, deleted_ids = mock_delete_rows.call_args.args
    assert sheet_id == watchdog.sheet_ids.SHEET_REVIEW_QUEUE
    assert deleted_ids == [14, 15]  # oldest-first, drained rows only


def test_row_caps_nothing_deletable_is_critical(
    rotation_rows, mock_delete_rows, mocker
):
    _shrink_row_caps(mocker, warn=3, rotate=5)
    rotation_rows[watchdog.sheet_ids.SHEET_ERRORS] = [
        _errors_row(i, severity="CRITICAL", days_old=300) for i in range(1, 7)
    ]  # six OPEN criticals — over the mark, zero eligible
    result = watchdog._check_row_cap_rotation()
    assert result.severity is Severity.CRITICAL
    assert "NOTHING is deletable" in result.details
    mock_delete_rows.assert_not_called()


def test_row_caps_check_b_still_sees_open_criticals_after_rotation(
    rotation_rows, mock_delete_rows, mocker
):
    # The acceptance line "Check B never goes blind": rotate a mixed sheet,
    # then run Check B against the survivors — the open CRITICAL is intact.
    _shrink_row_caps(mocker, warn=2, rotate=3)
    open_critical = _errors_row(2, severity="CRITICAL", days_old=300)
    rows = [
        _errors_row(1, severity="WARN", days_old=200),
        open_critical,
        _errors_row(3, severity="ERROR", days_old=150),
    ]
    rotation_rows[watchdog.sheet_ids.SHEET_ERRORS] = rows
    watchdog._check_row_cap_rotation()
    deleted = set(mock_delete_rows.call_args.args[1])
    assert open_critical["_row_id"] not in deleted

    survivors = [r for r in rows if r["_row_id"] not in deleted]
    rotation_rows[watchdog.sheet_ids.SHEET_ERRORS] = [
        r for r in survivors if r["Severity"] == "CRITICAL"
    ]
    # Check B filters Severity=CRITICAL via get_rows(filters=...); our routing
    # fixture ignores filters, so hand it the pre-filtered survivor set.
    result = watchdog._check_open_criticals()
    assert result.severity is Severity.WARN
    assert "1 open CRITICAL" in result.summary


# -- batching, per-run cap, partial failure ---------------------------------


def test_row_caps_rotation_batches_and_per_run_cap(
    rotation_rows, mock_delete_rows, mocker
):
    _shrink_row_caps(mocker, warn=1, rotate=2, batch=2, max_batches=2)
    rotation_rows[watchdog.sheet_ids.SHEET_ERRORS] = [
        _errors_row(i, days_old=100 + i) for i in range(1, 7)  # 6 eligible
    ]
    watchdog._check_row_cap_rotation()
    # Per-run cap = 2 batches × 2 = 4 rows; two delete calls of ≤ batch size.
    assert mock_delete_rows.call_count == 2
    sizes = [len(c.args[1]) for c in mock_delete_rows.call_args_list]
    assert sizes == [2, 2]
    all_deleted = [rid for c in mock_delete_rows.call_args_list for rid in c.args[1]]
    # Oldest first = highest days_old first → row 6 (106d) ... row 3 (103d).
    assert all_deleted == [6, 5, 4, 3]


def test_row_caps_delete_failure_is_warn_partial_never_raises(
    rotation_rows, mock_delete_rows, mock_log, mocker
):
    _shrink_row_caps(mocker, warn=1, rotate=2, batch=2, max_batches=5)
    rotation_rows[watchdog.sheet_ids.SHEET_ERRORS] = [
        _errors_row(i, days_old=100 + i) for i in range(1, 6)
    ]
    mock_delete_rows.side_effect = [None, SmartsheetError("429")]
    result = watchdog._check_row_cap_rotation()  # must not raise
    assert result.severity is Severity.WARN
    assert "delete failed after 2 rows" in result.details
    # Partial rotation still writes the never-silent record.
    rotation_records = [
        c for c in mock_log.call_args_list
        if c.kwargs.get("error_code") == "row_cap_rotation"
    ]
    assert len(rotation_records) == 1


# -- dry run, read failures, breaker ----------------------------------------


def test_row_caps_dry_run_deletes_nothing_and_writes_no_record(
    rotation_rows, mock_delete_rows, mock_log, mocker
):
    _shrink_row_caps(mocker, warn=1, rotate=2)
    rotation_rows[watchdog.sheet_ids.SHEET_ERRORS] = [
        _errors_row(i, days_old=200) for i in range(1, 5)
    ]
    result = watchdog._check_row_cap_rotation(dry_run=True)
    assert result.severity is Severity.WARN
    assert "DRY RUN" in result.details
    assert "would delete 4 of 4" in result.details
    mock_delete_rows.assert_not_called()
    assert not [
        c for c in mock_log.call_args_list
        if c.kwargs.get("error_code") == "row_cap_rotation"
    ]


def test_row_caps_read_failure_is_warn_never_raises(rotation_rows, mock_delete_rows):
    rotation_rows[watchdog.sheet_ids.SHEET_ERRORS] = SmartsheetError("read down")
    result = watchdog._check_row_cap_rotation()
    assert result.severity is Severity.WARN
    assert "row-count read failed" in result.details
    mock_delete_rows.assert_not_called()


def test_row_caps_breaker_open_is_info_skip(rotation_rows, mock_delete_rows):
    rotation_rows[watchdog.sheet_ids.SHEET_ERRORS] = (
        watchdog.smartsheet_client.SmartsheetCircuitOpenError("breaker OPEN")
    )
    result = watchdog._check_row_cap_rotation()
    assert result.severity is Severity.INFO
    assert "breaker OPEN" in result.details
    mock_delete_rows.assert_not_called()


# -- storm-mode fallback (2026-07-13 ITS_Errors cap incident) ----------------
#
# The 90d retention exceeds the system's entire age during its first 90 days,
# so a config-WARN storm could fill a sheet to the 20k hard cap while the
# rotation fired CRITICAL "nothing deletable" and deleted nothing. Storm-mode
# re-selects with the SHEET_ROW_STORM_FLOOR_DAYS (2d) floor when the 90d pass
# yields zero eligible rows. These tests use the REAL storm floor (2d) and
# real retention (90d); only the count thresholds are shrunk.


def test_row_caps_storm_mode_engages_when_retention_pins_rotation(
    rotation_rows, mock_delete_rows, mock_log, mocker
):
    # The incident shape: over the rotate mark, zero rows older than the 90d
    # retention (system younger than retention), plenty of terminal storm
    # debris older than the 2d storm floor. Pre-storm-mode code returned
    # CRITICAL here and deleted nothing (the prove-it-bites test).
    _shrink_row_caps(mocker, warn=3, rotate=5)
    rotation_rows[watchdog.sheet_ids.SHEET_ERRORS] = [
        _errors_row(1, severity="WARN", days_old=5),
        _errors_row(2, severity="WARN", days_old=4),
        _errors_row(3, severity="ERROR", days_old=6),
        _errors_row(4, severity="CRITICAL", days_old=10),  # OPEN — survives
        _errors_row(5, severity="WARN", days_old=3),
        _errors_row(6, severity="WARN", days_old=5),
    ]
    result = watchdog._check_row_cap_rotation()

    assert result.severity is Severity.WARN  # storm rotation is WARN, not CRITICAL
    assert "STORM-MODE" in result.details
    mock_delete_rows.assert_called_once()
    sheet_id, deleted_ids = mock_delete_rows.call_args.args
    assert sheet_id == watchdog.sheet_ids.SHEET_ERRORS
    # Oldest-first (date, then row id); the OPEN CRITICAL (row 4) survives
    # despite being the oldest row on the sheet.
    assert deleted_ids == [3, 1, 6, 2, 5]
    # The never-silent rotation record landed, LOUD, at WARN.
    rotation_records = [
        c for c in mock_log.call_args_list
        if c.kwargs.get("error_code") == "row_cap_rotation"
    ]
    assert len(rotation_records) == 1
    assert rotation_records[0].args[0] is Severity.WARN
    assert "STORM-MODE" in rotation_records[0].args[2]
    assert "OVERRIDDEN" in rotation_records[0].args[2]


def test_row_caps_storm_mode_floor_holds_rows_younger_than_floor_survive(
    rotation_rows, mock_delete_rows, mocker
):
    _shrink_row_caps(mocker, warn=3, rotate=5)
    rotation_rows[watchdog.sheet_ids.SHEET_ERRORS] = [
        _errors_row(1, severity="WARN", days_old=0),  # today — survives
        _errors_row(2, severity="WARN", days_old=1),  # yesterday — survives
        _errors_row(3, severity="WARN", days_old=2),  # AT the floor — survives (strict >)
        _errors_row(4, severity="WARN", days_old=3),  # older than floor — deleted
        _errors_row(5, severity="WARN", days_old=4),  # older than floor — deleted
        _errors_row(6, severity="WARN", ts=None),     # unprovable age — survives even in storm-mode
    ]
    watchdog._check_row_cap_rotation()
    mock_delete_rows.assert_called_once()
    _, deleted_ids = mock_delete_rows.call_args.args
    assert deleted_ids == [5, 4]  # oldest-first; nothing at/younger than the floor


def test_row_caps_storm_mode_never_deletes_open_criticals(
    rotation_rows, mock_delete_rows, mocker
):
    _shrink_row_caps(mocker, warn=2, rotate=3)
    rotation_rows[watchdog.sheet_ids.SHEET_ERRORS] = [
        _errors_row(1, severity="CRITICAL", days_old=30),                   # OPEN — survives
        _errors_row(2, severity="CRITICAL", days_old=20),                   # OPEN — survives
        _errors_row(3, severity="CRITICAL", days_old=25, resolved_at="x"),  # resolved — eligible
        _errors_row(4, severity="WARN", days_old=10),                       # eligible
    ]
    watchdog._check_row_cap_rotation()
    mock_delete_rows.assert_called_once()
    _, deleted_ids = mock_delete_rows.call_args.args
    assert deleted_ids == [3, 4]
    assert 1 not in deleted_ids
    assert 2 not in deleted_ids


def test_row_caps_storm_mode_truly_empty_is_critical_naming_the_floor(
    rotation_rows, mock_delete_rows, mocker
):
    _shrink_row_caps(mocker, warn=3, rotate=5)
    rotation_rows[watchdog.sheet_ids.SHEET_ERRORS] = [
        _errors_row(1, severity="CRITICAL", days_old=300),   # open — protected
        _errors_row(2, severity="CRITICAL", days_old=200),   # open — protected
        _errors_row(3, severity="CRITICAL", days_old=100),   # open — protected
        _errors_row(4, severity="WARN", days_old=1),         # terminal but younger than the floor
        _errors_row(5, severity="WARN", days_old=0),         # terminal but younger than the floor
        _errors_row(6, severity="WARN", ts="not-a-date"),    # unprovable age
    ]
    result = watchdog._check_row_cap_rotation()
    assert result.severity is Severity.CRITICAL
    assert "NOTHING is deletable" in result.details
    assert "storm floor" in result.details
    mock_delete_rows.assert_not_called()


def test_row_caps_storm_mode_dry_run_previews_without_deleting(
    rotation_rows, mock_delete_rows, mock_log, mocker
):
    _shrink_row_caps(mocker, warn=1, rotate=2)
    rotation_rows[watchdog.sheet_ids.SHEET_ERRORS] = [
        _errors_row(i, days_old=5) for i in range(1, 5)  # storm-eligible only
    ]
    result = watchdog._check_row_cap_rotation(dry_run=True)
    assert result.severity is Severity.WARN
    assert "DRY RUN" in result.details
    assert "STORM-MODE" in result.details
    assert "would delete 4 of 4" in result.details
    mock_delete_rows.assert_not_called()
    assert not [
        c for c in mock_log.call_args_list
        if c.kwargs.get("error_code") == "row_cap_rotation"
    ]


def test_compose_summary_plain_resend_key_untagged():
    entry = watchdog.alert_dedupe.ExpiredEntry(
        key="safety_reports.intake::uncaught_exception",
        first_fired_at="2026-07-01T00:00:00+00:00",
        last_fired_at="2026-07-01T00:30:00+00:00",
        suppressed_count=2,
        window_ends_at="2026-07-01T01:00:00+00:00",
        summarized=False,
    )
    subject, _ = watchdog._compose_summary(entry, "2026-07-02T07:00:00+00:00")
    assert "safety_reports.intake" in subject
    # A plain (Resend-leg) key must NOT carry the Sentry-leg tag.
    assert "[sentry-leg]" not in subject


def test_compose_summary_sentry_key_tagged_and_prefix_stripped():
    # A `sentry::`-namespaced dedupe key (Sentry reclassified
    # record→deduped-push, operator-ratified 2026-07-03): the summary
    # subject is tagged `[sentry-leg]` and the prefix is stripped so the
    # script/error_code display like the Resend-leg summaries.
    entry = watchdog.alert_dedupe.ExpiredEntry(
        key="sentry::safety_reports.intake::uncaught_exception",
        first_fired_at="2026-07-01T00:00:00+00:00",
        last_fired_at="2026-07-01T00:30:00+00:00",
        suppressed_count=3,
        window_ends_at="2026-07-01T01:00:00+00:00",
        summarized=False,
    )
    subject, body = watchdog._compose_summary(entry, "2026-07-02T07:00:00+00:00")
    assert "[sentry-leg]" in subject
    assert "safety_reports.intake" in subject
    assert "sentry::" not in subject  # prefix stripped, not displayed raw
    assert "Error code:       uncaught_exception" in body
    assert "Script:           safety_reports.intake" in body


# ---- Check W: ~/its/logs directory-growth bound (growth Slice 2) ----------
#
# The pure pruner ENGINE (classification, planners, archive primitives, the
# never-unlink/rename-a-launchd-path invariant) is tested in
# tests/test_log_rotation.py against a temp logs dir. These tests cover the
# WATCHDOG-check wiring that lives here alongside every other check: the
# INFO-routine vs WARN-abnormal severity split, the exactly-one never-silent
# ITS_Errors record, the open-CRITICAL incident-hold of the launchd lane, and
# the sustained-failure CRITICAL escalation. Seams: `watchdog._rotate_log_dir`
# (the engine compose) and `watchdog._open_critical_rows` (Check B's reader) are
# mocked; the record row is asserted against `watchdog.log` (the mock_log seam).
#
# NOTE ON THE ESCALATION PREDICATE (F2, 2026-07-21 — verified against live HEAD): the check
# escalates via the CAPPED ladder `sustained_failure.is_escalation_cycle`, NOT
# `has_crossed_threshold`. `has_crossed_threshold` is True for EVERY cycle at/past the
# threshold, so a persistently-wedged pruner minted one NEW open-CRITICAL ITS_Errors row
# EVERY daily run — and an open CRITICAL is never terminal (`errors_rotation`), hence
# UNROTATABLE at every floor including Check O's storm floor: exactly the 20k-cap lockout
# (19,975/20,000 on 2026-07-13) the error-triage work exists to prevent. `is_escalation_cycle`
# fires on the CROSSING cycle then at 2×/4×/8× the threshold (threshold 3 ⇒ days 3, 6, 12, 24,
# then every 24), writing the between-rung cycles at Severity.WARN (terminal, reclaimable). The
# tests below assert that capped-ladder behaviour. watchdog is now a genuine `is_escalation_cycle`
# CALLER, enrolled in LADDER_CONSUMERS in tests/test_transient_fence.py in this same landing.

_ROT = watchdog.log_rotation.RotationOutcome
_LogEntry = watchdog.log_rotation.LogEntry
_LogKind = watchdog.log_rotation.LogKind


def test_log_dir_rotation_routine_is_info_and_writes_no_record(mock_log, mocker):
    """A fully-completed, no-defer run is INFO and writes NO ITS_Errors row — the property
    that keeps Check W from adding a row every day to the 20k-capped sheet."""
    mocker.patch("watchdog._open_critical_rows", return_value=[])
    mocker.patch(
        "watchdog._rotate_log_dir", return_value=_ROT(daily_gzipped=1, launchd_truncated=2)
    )
    result = watchdog._check_log_dir_rotation()
    assert result.severity is Severity.INFO
    assert not [
        c for c in mock_log.call_args_list
        if c.kwargs.get("error_code") == "log_dir_rotation"
    ]


def test_log_dir_rotation_abnormal_writes_exactly_one_warn_record(mock_log, mocker):
    """An abnormal (partial/deferred) run is WARN and writes the never-silent record EXACTLY
    once — INFO never reaches Smartsheet, so the explicit row is how an operator sees it."""
    mocker.patch("watchdog._open_critical_rows", return_value=[])
    mocker.patch(
        "watchdog._rotate_log_dir", return_value=_ROT(daily_gzipped=1, daily_pending=3)
    )
    result = watchdog._check_log_dir_rotation()
    assert result.severity is Severity.WARN
    recs = [
        c for c in mock_log.call_args_list
        if c.kwargs.get("error_code") == "log_dir_rotation"
    ]
    assert len(recs) == 1
    assert recs[0].args[0] is Severity.WARN


def test_log_dir_rotation_dry_run_is_info_and_writes_no_record(mock_log, mocker):
    mocker.patch("watchdog._open_critical_rows", return_value=[])
    mocker.patch(
        "watchdog._rotate_log_dir", return_value=_ROT(daily_gzipped=2, dry_run=True)
    )
    result = watchdog._check_log_dir_rotation(dry_run=True)
    assert result.severity is Severity.INFO
    assert not [
        c for c in mock_log.call_args_list
        if c.kwargs.get("error_code") == "log_dir_rotation"
    ]


def test_log_dir_rotation_incident_hold_skips_launchd_but_runs_daily(
    tmp_path, mock_log, mocker
):
    """An open CRITICAL (operator likely mid-incident, maybe tailing a .out.log) holds the
    ENTIRE launchd truncate lane while the always-safe daily .gz lane still runs. Exercises
    the real `_rotate_log_dir(skip_launchd=True)` path with the archive primitives spied."""
    mocker.patch("watchdog._open_critical_rows", return_value=[{"Severity": "CRITICAL"}])
    daily = _LogEntry(
        path=tmp_path / "2000-01-01.log",
        kind=_LogKind.DAILY,
        size_bytes=500,
        mtime=0.0,
        daily_date=date(2000, 1, 1),
    )
    launchd = _LogEntry(
        path=tmp_path / "launchd" / "d.out.log",
        kind=_LogKind.LAUNCHD_OUT,
        size_bytes=900,
        mtime=0.0,
    )
    mocker.patch("watchdog.log_rotation.scan_entries", return_value=[daily, launchd])
    gzip_spy = mocker.patch("watchdog.log_rotation.gzip_in_place", return_value=500)
    trunc_spy = mocker.patch("watchdog.log_rotation.archive_and_truncate", return_value=900)

    result = watchdog._check_log_dir_rotation()

    gzip_spy.assert_called_once_with(daily.path)  # daily .gz lane ran
    trunc_spy.assert_not_called()  # launchd truncate lane HELD
    assert result.severity is Severity.WARN  # abnormal — launchd deferred
    assert "HELD" in result.details or "held" in result.details.lower()


def test_log_dir_rotation_fail_safe_holds_launchd_when_signal_unreadable(
    tmp_path, mocker
):
    """If the 'am I on fire?' open-CRITICAL read itself throws, ASSUME an incident and hold
    the launchd lane (the daily .gz lane is always safe) rather than truncating blind."""
    mocker.patch("watchdog._open_critical_rows", side_effect=RuntimeError("smartsheet down"))
    launchd = _LogEntry(
        path=tmp_path / "launchd" / "d.out.log",
        kind=_LogKind.LAUNCHD_OUT,
        size_bytes=900,
        mtime=0.0,
    )
    mocker.patch("watchdog.log_rotation.scan_entries", return_value=[launchd])
    trunc_spy = mocker.patch("watchdog.log_rotation.archive_and_truncate", return_value=900)
    result = watchdog._check_log_dir_rotation()
    trunc_spy.assert_not_called()  # held: unreadable signal ⇒ assume incident
    assert result.severity is Severity.WARN


def test_log_dir_rotation_sustained_failure_escalates_on_the_capped_ladder(mock_log, mocker):
    """F2: drive N consecutive FAILURE cycles (the pruner itself erroring). The CRITICAL
    escalation fires ONLY on the `is_escalation_cycle` rungs — for threshold 3 that is
    n=3, 6, 12, 24, then every 24 — NOT every cycle past 3 (which was the
    `has_crossed_threshold` bug that minted a NEW unrotatable open-CRITICAL row every
    daily run). Every between-rung cycle records at WARN (terminal, reclaimable). Uses the
    REAL `_LOG_ROTATION_FAILS` counter (conftest redirects its state path to tmp)."""
    threshold = watchdog.defaults.LOG_DIR_ROTATION_CRITICAL_THRESHOLD
    assert threshold == 3
    mocker.patch("watchdog._open_critical_rows", return_value=[])
    failed = _ROT(errors=("daily 2020-01-01.log: OSError('boom')",))
    mocker.patch("watchdog._rotate_log_dir", return_value=failed)

    # Cross past the doubling rungs AND into the fixed-interval tail (cap = 3×8 = 24).
    n_cycles = 30
    severities = []
    for _ in range(n_cycles):
        mock_log.reset_mock()  # so each cycle's record is read in isolation
        watchdog._check_log_dir_rotation()
        rec = [
            c for c in mock_log.call_args_list
            if c.kwargs.get("error_code") == "log_dir_rotation"
        ][-1]
        severities.append(rec.args[0])

    critical_cycles = {
        n for n, sev in enumerate(severities, start=1) if sev is Severity.CRITICAL
    }
    warn_cycles = {
        n for n, sev in enumerate(severities, start=1) if sev is Severity.WARN
    }
    # Rungs for threshold 3: crossing (3), then 2×/4×/8× (6, 12, 24), then every 24 (48…).
    assert critical_cycles == {3, 6, 12, 24}
    # Below the threshold AND every between-rung cycle is a reclaimable WARN — NOT a CRITICAL.
    assert warn_cycles == set(range(1, n_cycles + 1)) - critical_cycles
    # The specific regression: cycles 4, 5, 7 past the threshold do NOT re-page.
    assert severities[3] is Severity.WARN  # n=4
    assert severities[4] is Severity.WARN  # n=5
    assert severities[6] is Severity.WARN  # n=7
    # And the decision really routes through the shared ladder predicate.
    for n in (3, 6, 12, 24):
        assert watchdog.sustained_failure.is_escalation_cycle(n, threshold)
    for n in (4, 5, 7, 11, 13, 23, 25):
        assert not watchdog.sustained_failure.is_escalation_cycle(n, threshold)


def test_log_dir_rotation_recovery_resets_the_failure_counter(mock_log, mocker):
    """A non-failure cycle RESETS the streak, so a later isolated failure is WARN (n=1),
    not an immediate CRITICAL — the escalation requires *consecutive* failures."""
    mocker.patch("watchdog._open_critical_rows", return_value=[])
    failed = _ROT(errors=("boom",))
    healthy = _ROT(daily_gzipped=1)
    seq = [failed, failed, failed, healthy, failed]  # 3 fails (→CRITICAL), recover, 1 fail
    mocker.patch("watchdog._rotate_log_dir", side_effect=seq)

    severities: list[object] = []
    for _ in seq:
        mock_log.reset_mock()  # each cycle's record read in isolation (healthy cycle → none)
        watchdog._check_log_dir_rotation()
        recs = [
            c for c in mock_log.call_args_list
            if c.kwargs.get("error_code") == "log_dir_rotation"
        ]
        severities.append(recs[-1].args[0] if recs else None)

    assert severities[2] is Severity.CRITICAL  # 3rd consecutive failure escalated (rung n=3)
    assert severities[3] is None  # healthy cycle wrote NO record (INFO)
    assert severities[4] is Severity.WARN  # streak reset ⇒ isolated failure is WARN again


def test_log_dir_rotation_maintenance_records_critical_without_paging(mock_log, mocker):
    """F3: during a MAINTENANCE window (alerts_suppressed=True) a SUSTAINED failure still
    RECORDS its CRITICAL to ITS_Errors (so Check B resurfaces the still-open row post-window),
    but the push legs are NOT fired — the CRITICAL log call carries alert=False. The inline
    log bypasses `_run_check`'s WARN/CRITICAL→INFO MAINTENANCE downgrade, so without this
    opt-out it would page during maintenance."""
    threshold = watchdog.defaults.LOG_DIR_ROTATION_CRITICAL_THRESHOLD
    assert threshold == 3
    mocker.patch("watchdog._open_critical_rows", return_value=[])
    mocker.patch("watchdog._rotate_log_dir", return_value=_ROT(errors=("boom",)))

    crit = None
    for _ in range(threshold):  # drive to the crossing rung (n=3 → CRITICAL)
        mock_log.reset_mock()
        watchdog._check_log_dir_rotation(alerts_suppressed=True)
        recs = [
            c for c in mock_log.call_args_list
            if c.kwargs.get("error_code") == "log_dir_rotation"
        ]
        crit = recs[-1]
    assert crit is not None
    assert crit.args[0] is Severity.CRITICAL  # the CRITICAL is RECORDED...
    assert crit.kwargs.get("alert") is False  # ...but push legs are SUPPRESSED (record-only)


def test_log_dir_rotation_sustained_failure_pages_outside_maintenance(mock_log, mocker):
    """F3 (contrast): outside a maintenance window (alerts_suppressed=False, the default) the
    same sustained-failure CRITICAL PAGES — alert=True."""
    threshold = watchdog.defaults.LOG_DIR_ROTATION_CRITICAL_THRESHOLD
    mocker.patch("watchdog._open_critical_rows", return_value=[])
    mocker.patch("watchdog._rotate_log_dir", return_value=_ROT(errors=("boom",)))

    crit = None
    for _ in range(threshold):
        mock_log.reset_mock()
        watchdog._check_log_dir_rotation(alerts_suppressed=False)
        recs = [
            c for c in mock_log.call_args_list
            if c.kwargs.get("error_code") == "log_dir_rotation"
        ]
        crit = recs[-1]
    assert crit is not None
    assert crit.args[0] is Severity.CRITICAL
    assert crit.kwargs.get("alert") is True  # pages the operator


def test_log_dir_rotation_incident_hold_only_writes_no_row_and_no_increment(mock_log, mocker):
    """F5: when the ONLY abnormality is the open-CRITICAL launchd HOLD (no real per-file
    failure, no deadline, no cap-driven daily overflow, no oversized skip), the check keeps
    the CheckResult WARN for dashboard visibility but writes NO explicit `log_dir_rotation`
    ITS_Errors row and does NOT touch the failure streak — otherwise it would pile one WARN
    row per daily run onto the already-stressed 20k-capped sheet exactly while the open
    CRITICAL that Check B ALREADY surfaces is what triggered the hold."""
    mocker.patch("watchdog._open_critical_rows", return_value=[{"Severity": "CRITICAL"}])
    # incident-hold-only outcome: launchd deferred (held), daily lane clean, no failure.
    mocker.patch(
        "watchdog._rotate_log_dir",
        return_value=_ROT(daily_gzipped=1, launchd_pending=2),
    )
    record_spy = mocker.spy(watchdog._LOG_ROTATION_FAILS, "record")

    result = watchdog._check_log_dir_rotation()

    assert result.severity is Severity.WARN  # still WARN for the dashboard
    assert not [
        c for c in mock_log.call_args_list
        if c.kwargs.get("error_code") == "log_dir_rotation"
    ]  # NO explicit never-silent row
    record_spy.assert_not_called()  # failure streak NOT incremented


def test_log_dir_rotation_real_failure_does_write_row_and_increment(mock_log, mocker):
    """F5 (contrast): a REAL failure (the pruner erroring) DOES write the never-silent row
    AND increments the failure streak — the property the incident-hold-only path suppresses."""
    mocker.patch("watchdog._open_critical_rows", return_value=[])
    mocker.patch("watchdog._rotate_log_dir", return_value=_ROT(errors=("boom",)))
    record_spy = mocker.spy(watchdog._LOG_ROTATION_FAILS, "record")

    result = watchdog._check_log_dir_rotation()

    assert result.severity is Severity.WARN
    recs = [
        c for c in mock_log.call_args_list
        if c.kwargs.get("error_code") == "log_dir_rotation"
    ]
    assert len(recs) == 1  # the never-silent row IS written
    record_spy.assert_called_once()  # failure streak incremented


# ---- Sweep-results records + letters (dashboard sweep-panel source) -------


def test_check_letters_cover_every_registered_check():
    """Registry-reconciliation teeth: registering a check in CHECKS adds its
    letter to CHECK_LETTERS in the same PR — the sweep panel's vocabulary."""
    registered = {c.__name__ for c in watchdog.CHECKS}
    assert registered == set(watchdog.CHECK_LETTERS), (
        "CHECK_LETTERS out of sync with CHECKS — reconcile both in the same PR"
    )
    assert set(watchdog.CHECK_LETTERS.values()) == set("ABCDGIJKLMNOPQRSTUVWXY")


def test_run_check_returns_record_with_raw_severity(mock_log):
    def crit_check() -> watchdog.CheckResult:
        return _result(Severity.CRITICAL, "house is on fire")

    record = watchdog._run_check(crit_check, alerts_suppressed=True)
    # The ROUTED severity downgrades under MAINTENANCE (asserted by the group-D
    # tests above); the sweep RECORD keeps the raw verdict — the file carries
    # alerts_suppressed at the top level so the panel can annotate instead.
    assert record["severity"] == "CRITICAL"
    assert record["summary"] == "house is on fire"
    assert record["check"] == "crit_check"
    assert record["letter"] == "?"  # not a registered check → placeholder letter


def test_run_check_harness_failure_yields_error_record(mock_log):
    def boom() -> watchdog.CheckResult:
        raise RuntimeError("nope")

    record = watchdog._run_check(boom, alerts_suppressed=False)
    assert record["severity"] == "ERROR"
    assert "RuntimeError" in record["summary"]


def test_sweep_results_path_lives_under_state_dir():
    """Location pin: the sweep file rides ~/its/state via shared.state_io (the
    state-write discipline; the write mechanism is CI-enforced separately)."""
    assert _REAL_RESULTS_PATH.parent == Path.home() / "its" / "state"


# ---- Group Y: cadence tiering — hourly sweep vs the daily-only tier -----------
#
# The watchdog went hourly on 2026-08-07 after a 12.7h Smartsheet outage paged ZERO
# times. Six checks are harmful at that cadence and stay daily (DAILY_ONLY_CHECKS);
# these tests pin BOTH halves — that the hourly tier really runs every sweep, and that
# the daily tier really is skipped in between.
#
# NOTE: the autouse fixture redirects WATCHDOG_MARKER_DIR to tmp_path, so the daily
# marker is ABSENT by default and `_daily_tier_due()` fail-opens to True. That is why
# the pre-existing Group E tests still legitimately see all 21 checks run.


def _stamp_daily_marker(age: timedelta) -> Path:
    """Write a `watchdog_daily.last_run` marker `age` in the past (tmp-redirected dir)."""
    watchdog.WATCHDOG_MARKER_DIR.mkdir(parents=True, exist_ok=True)
    marker = watchdog.WATCHDOG_MARKER_DIR / f"{watchdog.DAILY_TIER_JOB}.last_run"
    marker.write_text((datetime.now(UTC) - age).isoformat())
    return marker


def test_daily_only_checks_are_all_registered_in_checks():
    """Parity: a DAILY_ONLY entry absent from CHECKS would silently never run at all."""
    assert set(watchdog.DAILY_ONLY_CHECKS) <= set(watchdog.CHECKS)


def test_daily_tier_holds_exactly_the_harmful_at_hourly_letters():
    """Pins the tier split. Changing it is a deliberate act, not a drive-by edit."""
    letters = {watchdog.CHECK_LETTERS[c.__name__] for c in watchdog.DAILY_ONLY_CHECKS}
    # P joined 2026-08-10 (#26): its liveness probe spends a single-use Box refresh token
    # per run, so hourly would worsen the rotation race the same issue's retry absorbs.
    # Y joined 2026-08-11 (#27): a full-sheet read whose enrolled set only changes when code
    # merges, and whose ladder is calibrated in DAYS.
    assert letters == {"D", "G", "I", "L", "O", "P", "U", "W", "X", "Y"}


def test_hourly_tier_keeps_the_outage_detectors():
    """The whole point: J (breaker-open), C (marker staleness), Q/R (portal outage and
    backlog) and B (open CRITICALs) must stay HOURLY or the 2026-08-06 blind spot returns."""
    hourly = {
        watchdog.CHECK_LETTERS[c.__name__]
        for c in watchdog.CHECKS
        if c not in watchdog.DAILY_ONLY_CHECKS
    }
    assert {"B", "C", "J", "Q", "R"} <= hourly


def test_daily_tier_due_when_marker_absent():
    assert watchdog._daily_tier_due() is True


def test_daily_tier_due_when_marker_older_than_window():
    _stamp_daily_marker(watchdog.DAILY_TIER_WINDOW + timedelta(minutes=1))
    assert watchdog._daily_tier_due() is True


def test_daily_tier_not_due_when_marker_is_fresh():
    _stamp_daily_marker(timedelta(minutes=5))
    assert watchdog._daily_tier_due() is False


def test_daily_tier_fails_open_on_unparseable_marker():
    """A corrupt marker must RUN the tier — a redundant daily pass is cheap, a silently
    skipped one is the failure this change exists to remove."""
    watchdog.WATCHDOG_MARKER_DIR.mkdir(parents=True, exist_ok=True)
    (watchdog.WATCHDOG_MARKER_DIR / f"{watchdog.DAILY_TIER_JOB}.last_run").write_text("garbage")
    assert watchdog._daily_tier_due() is True


def test_daily_tier_tolerates_a_naive_timestamp():
    """`write_last_run_marker` writes tz-aware, but an older/foreign writer may not."""
    watchdog.WATCHDOG_MARKER_DIR.mkdir(parents=True, exist_ok=True)
    naive = (datetime.now(UTC) - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    (watchdog.WATCHDOG_MARKER_DIR / f"{watchdog.DAILY_TIER_JOB}.last_run").write_text(naive)
    assert watchdog._daily_tier_due() is False


def test_fresh_marker_defers_only_the_daily_tier(
    mock_check_state, mock_log, mock_get_setting, mock_ping, mocker
):
    """The load-bearing assertion: an hourly sweep runs the hourly checks and NONE of
    the daily-tier callables."""
    mock_check_state.return_value = SystemState.ACTIVE
    mock_get_setting.return_value = "https://hc-ping.com/test-uuid"
    _stamp_daily_marker(timedelta(minutes=5))
    run_check_spy = mocker.patch("watchdog._run_check")
    run_check_spy.return_value = {
        "check": "x", "letter": "?", "severity": "INFO", "summary": "ok"
    }

    watchdog.main()

    hourly = [c for c in watchdog.CHECKS if c not in watchdog.DAILY_ONLY_CHECKS]
    ran = {call.args[0] for call in run_check_spy.call_args_list}
    assert run_check_spy.call_count == len(hourly)
    assert ran == set(hourly)
    assert not (ran & set(watchdog.DAILY_ONLY_CHECKS))


def test_stale_marker_runs_every_check(
    mock_check_state, mock_log, mock_get_setting, mock_ping, mocker
):
    mock_check_state.return_value = SystemState.ACTIVE
    mock_get_setting.return_value = "https://hc-ping.com/test-uuid"
    _stamp_daily_marker(watchdog.DAILY_TIER_WINDOW + timedelta(minutes=1))
    run_check_spy = mocker.patch("watchdog._run_check")
    run_check_spy.return_value = {
        "check": "x", "letter": "?", "severity": "INFO", "summary": "ok"
    }

    watchdog.main()

    assert run_check_spy.call_count == len(watchdog.CHECKS)


def test_daily_marker_is_stamped_only_when_the_tier_actually_ran(
    mock_check_state, mock_log, mock_get_setting, mock_ping, mocker
):
    """A deferred sweep must NOT refresh the marker — otherwise the daily tier is pushed
    out an hour on every sweep and never runs again."""
    mock_check_state.return_value = SystemState.ACTIVE
    mock_get_setting.return_value = "https://hc-ping.com/test-uuid"
    mocker.patch("watchdog._run_check").return_value = {
        "check": "x", "letter": "?", "severity": "INFO", "summary": "ok"
    }
    marker = _stamp_daily_marker(timedelta(minutes=5))
    before = marker.read_text()

    watchdog.main()  # deferred sweep — must not touch the marker
    assert marker.read_text() == before

    stale = datetime.now(UTC) - watchdog.DAILY_TIER_WINDOW - timedelta(minutes=1)
    marker.write_text(stale.isoformat())
    watchdog.main()  # due sweep — must refresh it
    assert marker.read_text() != stale.isoformat()
    assert watchdog._daily_tier_due() is False


def test_deferred_checks_still_appear_in_the_sweep_record(
    mock_check_state, mock_log, mock_get_setting, mock_ping, mocker
):
    """The dashboard sweep panel must show a deferred check as deferred — never as a
    check that silently vanished from the registry."""
    mock_check_state.return_value = SystemState.ACTIVE
    mock_get_setting.return_value = "https://hc-ping.com/test-uuid"
    _stamp_daily_marker(timedelta(minutes=5))
    mocker.patch("watchdog._run_check").return_value = {
        "check": "ran", "letter": "?", "severity": "INFO", "summary": "ok"
    }

    watchdog.main()

    results = json.loads(watchdog.WATCHDOG_RESULTS_PATH.read_text())["results"]
    assert len(results) == len(watchdog.CHECKS)  # every check accounted for
    deferred = [r for r in results if "deferred" in r["summary"]]
    assert len(deferred) == len(watchdog.DAILY_ONLY_CHECKS)
    assert {r["check"] for r in deferred} == {c.__name__ for c in watchdog.DAILY_ONLY_CHECKS}
    assert all(r["severity"] == "INFO" for r in deferred)


def test_check_x_reads_the_worker_url_under_the_owning_workstream(monkeypatch):
    """`_resolve_fieldops_creds` must read the Worker base URL under `safety_reports`.

    `get_setting` matches on (Setting, Workstream) BOTH, and the Worker base-URL row is owned by
    safety_reports — portal_poll's copy, shared rather than duplicated, which is why
    `fieldops_sync` exports a SECOND constant naming that workstream. Reading it under
    `field_ops` raises SmartsheetNotFoundError, which the resolver turns into "creds unresolved",
    which makes Check X report INFO and skip forever: a detector that never detects.

    Every other Check X test monkeypatches `_resolve_fieldops_creds` wholesale, so this bug is
    structurally unreachable by them. This one patches `get_setting` and asserts the workstream
    it was actually called with.
    """
    from field_ops import fieldops_sync  # noqa: PLC0415

    seen: dict[str, str] = {}

    def _fake_get_setting(key, *, workstream):
        seen["key"], seen["workstream"] = key, workstream
        if workstream != fieldops_sync.CFG_WORKER_BASE_URL_WORKSTREAM:
            raise watchdog.smartsheet_client.SmartsheetNotFoundError(
                f"ITS_Config has no row for Setting={key!r} Workstream={workstream!r}"
            )
        return "https://worker.test"

    monkeypatch.setattr(watchdog.smartsheet_client, "get_setting", _fake_get_setting)
    monkeypatch.setattr(watchdog.keychain, "get_secret", lambda _n: "tok")

    creds = watchdog._resolve_fieldops_creds()

    assert seen["key"] == fieldops_sync.CFG_WORKER_BASE_URL
    assert seen["workstream"] == "safety_reports", (
        "read under the wrong workstream — the row does not exist there, so Check X skips forever"
    )
    assert creds == ("https://worker.test", "tok")
