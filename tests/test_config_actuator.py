"""Orchestration tests for the Mac config actuator (§50 config editor, slice 2) — the
privileged git/deploy ops + portal_client HTTP + the Stage-1 config_apply write are mocked.
Verifies the state-machine stamp sequence + per-stage fencing + the fail+CRITICAL detect-and-
alert, BOTH D1 migration-gate sites, the stale sweep, the idle self-heal, and the heartbeat
status. Mirrors tests/test_publish_daemon.py's stub seam."""
from __future__ import annotations

import json
import subprocess
import time

import pytest

import shared.kill_switch as ks
from po_materials import config_actuator as ca


def _tax_payload() -> dict:
    return {"rates_bp": {"IL": 900}, "state_names": {"IL": "Illinois"}}


def _row(artifact: str = "tax", op: str = "edit", *, target: str | None = None,
         payload: dict | None = None, rid: int = 1) -> dict:
    return {
        "id": rid, "workstream": "po_materials", "artifact_key": artifact, "op": op,
        "target_version": target,
        "payload": json.dumps(payload if payload is not None else _tax_payload()),
        "status": "queued",
    }


@pytest.fixture
def stub(mocker):
    mocker.patch.object(ks, "check_system_state", return_value=ks.SystemState.ACTIVE)
    return {
        "enabled": mocker.patch.object(ca, "_polling_enabled", return_value=True),
        "creds": mocker.patch.object(ca, "_resolve_creds",
                                     return_value=ca._Creds("https://portal.test", "tok")),
        "pending": mocker.patch.object(ca.portal_client, "get_config_pending"),
        "claim": mocker.patch.object(ca.portal_client, "claim_config"),
        "stamp": mocker.patch.object(ca.portal_client, "stamp_config", return_value=True),
        "stuck": mocker.patch.object(ca.portal_client, "get_config_stuck", return_value=[]),
        "reset": mocker.patch.object(ca, "_reset_to_main"),
        "unstrand": mocker.patch.object(ca, "_unstrand_if_needed"),
        # Stage-1 domain write is mocked so orchestration tests never touch the live tree
        # (the REAL apply_config is exercised in test_config_apply.py against a tmp root).
        "apply": mocker.patch.object(ca, "_apply_config", return_value="tax: 1 state rate(s) -> config_version 2"),
        "commit": mocker.patch.object(ca, "_commit_test_merge"),
        "deploy": mocker.patch.object(ca, "_deploy_land_health"),
        "migrations": mocker.patch.object(ca, "_pending_migrations", return_value=[]),
        "hb": mocker.patch.object(ca, "_write_heartbeat"),
        "hb_row": mocker.patch.object(ca, "_write_heartbeat_row"),
        # MUST be patched: the real one touches ~/its/.watchdog/config_actuator.last_run on
        # the LIVE host, so an unpatched suite run would fake Check-C freshness for a daemon
        # that never ran.
        "marker": mocker.patch.object(ca, "_write_watchdog_marker"),
        "circuit": mocker.patch.object(ca.circuit_breaker, "is_open", return_value=False),
        "log": mocker.patch.object(ca.error_log, "log"),
    }


def _statuses(stub) -> list[str]:
    return [c.kwargs["status"] for c in stub["stamp"].call_args_list]


def _critical_fired(stub) -> bool:
    return any(c.args and c.args[0] == ca.Severity.CRITICAL for c in stub["log"].call_args_list)


# ── happy paths ───────────────────────────────────────────────────────────────


def test_actuates_through_the_full_state_machine(stub):
    stub["pending"].return_value = [{"id": 1}]
    stub["claim"].return_value = _row()
    out = ca.config_once()
    assert out.actuated == 1 and out.failed == 0
    assert _statuses(stub) == ["validated", "tested", "live", "archived"]
    stub["apply"].assert_called_once()
    stub["commit"].assert_called_once()
    stub["deploy"].assert_called_once()
    assert not _critical_fired(stub)


def test_terms_add_version_actuates(stub):
    stub["pending"].return_value = [{"id": 2}]
    stub["claim"].return_value = _row(
        "terms", "add_version", target="standard_17_v2",
        payload={"profile_id": "standard_17", "text": "x"}, rid=2,
    )
    out = ca.config_once()
    assert out.actuated == 1
    assert _statuses(stub) == ["validated", "tested", "live", "archived"]


# ── failures stamp failed(stage) + fire the operator CRITICAL ────────────────────


def test_validation_failure_stamps_failed_and_fires_critical(stub):
    stub["pending"].return_value = [{"id": 3}]
    stub["claim"].return_value = _row(rid=3)
    stub["apply"].side_effect = ca.config_apply.ConfigApplyError("bad rate")
    out = ca.config_once()
    assert out.failed == 1 and out.actuated == 0
    assert _statuses(stub) == ["failed"]
    assert stub["stamp"].call_args.kwargs["failed_stage"] == "validated"
    assert _critical_fired(stub)
    stub["commit"].assert_not_called()  # never reached actuation


def test_commit_failure_stamps_failed_tested_and_fires_critical(stub):
    stub["pending"].return_value = [{"id": 4}]
    stub["claim"].return_value = _row(rid=4)
    stub["commit"].side_effect = subprocess.CalledProcessError(1, ["gh"], stderr="CI red")
    out = ca.config_once()
    assert out.failed == 1
    assert _statuses(stub) == ["validated", "failed"]
    assert stub["stamp"].call_args.kwargs["failed_stage"] == "tested"
    assert _critical_fired(stub)
    stub["deploy"].assert_not_called()


def test_deploy_failure_stamps_failed_live(stub):
    stub["pending"].return_value = [{"id": 5}]
    stub["claim"].return_value = _row(rid=5)
    stub["deploy"].side_effect = RuntimeError("wrangler boom")
    out = ca.config_once()
    assert out.failed == 1
    assert _statuses(stub) == ["validated", "tested", "failed"]
    assert stub["stamp"].call_args.kwargs["failed_stage"] == "live"
    assert _critical_fired(stub)


# ── gating / fail-closed / lease ─────────────────────────────────────────────────


def test_polling_disabled_halts_without_polling(stub):
    stub["enabled"].return_value = False
    out = ca.config_once()
    assert out.halted == "polling_disabled"
    stub["pending"].assert_not_called()


def test_unresolved_creds_halts_loud(stub):
    stub["creds"].return_value = None
    out = ca.config_once()
    assert out.halted == "creds_unresolved"
    assert any(c.args and c.args[0] == ca.Severity.ERROR for c in stub["log"].call_args_list)
    stub["pending"].assert_not_called()


def _codes(stub) -> list[str]:
    return [c.kwargs.get("error_code") for c in stub["log"].call_args_list]


def test_transient_base_url_warns_and_skips_without_paging(stub):
    # A Smartsheet blip on the base-URL read is NOT a missing credential. The live po_poll
    # instance of this bug (2026-07-20 04:42Z) fell back to "" and alerted that credentials
    # were unset while both Keychain entries were fine — a false alarm that aimed the §43
    # repair at re-provisioning the privileged config bearer, a high-capability-class action,
    # for a condition needing none. Transient => WARN + skip, never the misconfig report.
    stub["creds"].return_value = ca.TransientUnavailable(
        reason="SmartsheetError: (<PreparedRequest [GET]>, None)"
    )
    out = ca.config_once()
    assert out.halted == "creds_transient"
    stub["pending"].assert_not_called()  # still FAIL-CLOSED — it does not actuate
    codes = _codes(stub)
    assert "config_actuator.creds_transient" in codes
    assert "config_actuator.creds_unresolved" not in codes, (
        "a transient read failure must NOT report unresolved credentials"
    )
    assert not _critical_fired(stub)
    _, hb_kwargs = stub["hb_row"].call_args
    assert hb_kwargs["status"] == "WARN"  # not ERROR — nothing is misconfigured
    # The marker is the load-bearing half: a cycle that never polled must let Check C go
    # stale, so a SUSTAINED outage still surfaces instead of being masked by fake freshness.
    stub["marker"].assert_not_called()


def test_completed_cycle_writes_the_watchdog_marker(stub):
    stub["pending"].return_value = [{"id": 1}]
    stub["claim"].return_value = _row()
    assert ca.config_once().actuated == 1
    stub["marker"].assert_called_once()


@pytest.mark.parametrize(
    "arrange, expected_halt",
    [
        (lambda s: s["enabled"].configure_mock(return_value=False), "polling_disabled"),
        (lambda s: s["creds"].configure_mock(return_value=None), "creds_unresolved"),
        (lambda s: s["unstrand"].configure_mock(side_effect=RuntimeError("stranded")),
         "unstrand_failed"),
    ],
)
def test_halted_cycles_never_fake_check_c_freshness(stub, arrange, expected_halt):
    # Check C's whole value is noticing that this daemon stopped doing its job. A cycle that
    # bailed before the pull did NOT do its job, so it must not touch the marker — otherwise
    # a permanently gated-off / miscredentialed / stranded actuator looks eternally healthy.
    arrange(stub)
    assert ca.config_once().halted == expected_halt
    stub["marker"].assert_not_called()


def test_already_leased_row_is_skipped(stub):
    stub["pending"].return_value = [{"id": 6}]
    stub["claim"].return_value = None  # a concurrent run already leased it
    out = ca.config_once()
    assert out.skipped_unclaimed == 1 and out.actuated == 0
    stub["commit"].assert_not_called()


# ── D1 pending-migrations deploy gate (forensic class #2) — BOTH sites ────────────


def test_pending_migrations_refuse_the_cycle_before_claiming(stub):
    """Unapplied remote migrations REFUSE the whole cycle pre-claim: no lease burned, no row
    stamped (they stay `queued` on the Worker), and the refusal is LOUD (a CRITICAL naming
    the pending files)."""
    stub["pending"].return_value = [{"id": 7}]
    stub["migrations"].return_value = ["0046_x.sql", "0047_y.sql"]
    out = ca.config_once()
    assert out.halted == "pending_migrations"
    assert out.polled == 1 and out.actuated == 0 and out.failed == 0
    stub["claim"].assert_not_called()
    stub["commit"].assert_not_called()
    stub["deploy"].assert_not_called()
    stub["stamp"].assert_not_called()  # nothing terminal-failed — the request survives
    crit = [
        c for c in stub["log"].call_args_list
        if c.args and c.args[0] == ca.Severity.CRITICAL
        and c.kwargs.get("error_code") == ca.ERR_PENDING_MIGRATIONS
    ]
    assert len(crit) == 1
    assert "0046_x.sql" in crit[0].args[2]  # the pending list is named


def test_operator_apply_unblocks_the_next_cycle_automatically(stub):
    stub["pending"].return_value = [{"id": 8}]
    stub["claim"].return_value = _row(rid=8)
    stub["migrations"].return_value = ["0046_x.sql"]
    assert ca.config_once().halted == "pending_migrations"
    stub["migrations"].return_value = []  # the operator ran `wrangler d1 migrations apply`
    out = ca.config_once()
    assert out.halted is None and out.actuated == 1
    assert _statuses(stub) == ["validated", "tested", "live", "archived"]


def test_migration_check_failure_halts_fail_closed(stub):
    """Cannot verify ⇒ must not deploy: a wrangler-list failure halts the cycle (fail-closed)
    with a PAGING CRITICAL under its own category, and nothing is claimed."""
    stub["pending"].return_value = [{"id": 9}]
    stub["migrations"].side_effect = subprocess.CalledProcessError(1, ["npx"], stderr="net down")
    out = ca.config_once()
    assert out.halted == "migration_check_failed"
    stub["claim"].assert_not_called()
    assert any(
        c.args and c.args[0] == ca.Severity.CRITICAL
        and c.kwargs.get("error_code") == ca.ERR_MIGRATION_CHECK
        for c in stub["log"].call_args_list
    )


def test_idle_cycle_never_shells_out_to_wrangler(stub):
    """No rows → no deploy possible → the gate is skipped (a 120s-cadence daemon must not hit
    wrangler/network every idle cycle)."""
    stub["pending"].return_value = []
    out = ca.config_once()
    assert out.halted is None
    stub["migrations"].assert_not_called()


def test_pending_migrations_diffs_disk_against_remote_list(mocker, tmp_path):
    """_pending_migrations = on-disk *.sql names cross-checked against the wrangler
    `d1 migrations list --remote` output (which prints ONLY unapplied migrations), invoked
    exactly like the deploy stage (same cwd, local wrangler via npx)."""
    mocker.patch.object(ca, "_MIGRATIONS_DIR", tmp_path)
    for name in ("0046_a.sql", "0047_b.sql"):
        (tmp_path / name).write_text("-- migration")
    (tmp_path / "notes.txt").write_text("not a migration")
    run = mocker.patch.object(ca.subprocess, "run")
    run.return_value.stdout = (
        "Migrations to be applied:\n┌──────────────┐\n│ 0047_b.sql │\n└──────────────┘\n"
    )
    assert ca._pending_migrations() == ["0047_b.sql"]
    cmd = run.call_args.args[0]
    assert cmd == ["npx", "wrangler", "d1", "migrations", "list", ca.D1_DATABASE_NAME, "--remote"]
    assert run.call_args.kwargs["cwd"] == ca._ROOT / "safety_portal"
    assert run.call_args.kwargs["check"] is True  # a wrangler failure raises (fail-closed)


def test_deploy_land_health_refuses_ahead_of_pending_migrations(mocker):
    """The authoritative in-stage gate (AFTER the pull, BEFORE `npm run deploy`): pending
    migrations raise PendingMigrationsError and the deploy subprocess is NEVER invoked. Fires
    the distinct CRITICAL naming the files — the row NOT stamped live."""
    mocker.patch.object(ca, "_git")
    mocker.patch.object(ca, "_pending_migrations", return_value=["0046_a.sql"])
    run = mocker.patch.object(ca.subprocess, "run")
    log = mocker.patch.object(ca.error_log, "log")
    ping = mocker.patch.object(ca.portal_client, "get_config_pending")
    with pytest.raises(ca.PendingMigrationsError, match="0046_a.sql"):
        ca._deploy_land_health(ca._Creds("https://portal.test", "tok"))
    run.assert_not_called()   # the deploy never ran
    ping.assert_not_called()  # nor the liveness ping
    assert any(
        c.args and c.args[0] == ca.Severity.CRITICAL
        and c.kwargs.get("error_code") == ca.ERR_PENDING_MIGRATIONS
        for c in log.call_args_list
    )


def test_deploy_land_health_deploys_when_remote_is_current(mocker):
    mocker.patch.object(ca, "_git")
    mocker.patch.object(ca, "_pending_migrations", return_value=[])
    run = mocker.patch.object(ca.subprocess, "run")
    mocker.patch.object(ca.portal_client, "get_config_pending")
    ca._deploy_land_health(ca._Creds("https://portal.test", "tok"))
    assert run.call_args.args[0] == ["npm", "run", "deploy"]


# ── ITS_Daemon_Health heartbeat ──────────────────────────────────────────────────


def test_cycle_writes_ok_heartbeat(stub):
    stub["pending"].return_value = [{"id": 1}]
    stub["claim"].return_value = _row()
    ca.config_once()
    stub["hb"].assert_called_once()
    assert stub["hb_row"].call_args.kwargs["status"] == "OK"
    assert stub["hb_row"].call_args.kwargs["items_processed"] == 1
    assert stub["hb_row"].call_args.kwargs["error_summary"] is None


def test_failed_actuation_writes_degraded_heartbeat(stub):
    stub["pending"].return_value = [{"id": 5}]
    stub["claim"].return_value = _row(rid=5)
    stub["deploy"].side_effect = RuntimeError("wrangler boom")
    ca.config_once()
    assert stub["hb_row"].call_args.kwargs["status"] == "DEGRADED"
    assert "failed=1" in stub["hb_row"].call_args.kwargs["error_summary"]


def test_disabled_cycle_skips_heartbeat(stub):
    stub["enabled"].return_value = False
    ca.config_once()
    stub["hb"].assert_not_called()
    stub["hb_row"].assert_not_called()


def test_unresolved_creds_write_error_heartbeat(stub):
    stub["creds"].return_value = None
    ca.config_once()
    stub["hb"].assert_called_once()
    assert stub["hb_row"].call_args.kwargs["status"] == "ERROR"


def test_pending_migrations_write_warn_heartbeat(stub):
    stub["pending"].return_value = [{"id": 7}]
    stub["migrations"].return_value = ["0046_x.sql"]
    ca.config_once()
    assert stub["hb_row"].call_args.kwargs["status"] == "WARN"
    assert "deploy blocked" in stub["hb_row"].call_args.kwargs["error_summary"]


def test_open_circuit_writes_circuit_open_heartbeat(stub):
    stub["pending"].return_value = []
    stub["circuit"].return_value = True
    ca.config_once()
    assert stub["hb_row"].call_args.kwargs["status"] == "CIRCUIT_OPEN"


def test_heartbeat_row_failure_never_blocks_the_cycle(stub):
    stub["pending"].return_value = [{"id": 1}]
    stub["claim"].return_value = _row()
    stub["hb_row"].side_effect = RuntimeError("sheet down")
    out = ca.config_once()
    assert out.actuated == 1  # primary work unharmed
    assert any(
        c.kwargs.get("error_code") == "daemon_health_write_failed"
        for c in stub["log"].call_args_list
    )


def test_reporter_registration_metadata_is_self_provisioning_config(stub):
    r = ca._heartbeat_reporter
    assert r.daemon_name == "po_materials.config_actuator"
    assert r.workstream == "po_materials"
    assert r.interval_seconds == 120
    assert r.row_state_path.name == "heartbeat_row_ids.json"


def test_stale_reclaim_window_exceeds_ci_plus_worker_lease():
    """STALE_RECLAIM_S must be strictly greater than CI_TIMEOUT_S + the Worker's LEASE_TTL_S
    (1800) so a legitimately in-progress config publish is never reclaimed."""
    assert ca.STALE_RECLAIM_S > ca.CI_TIMEOUT_S + 1800


# ── stale-row sweep ───────────────────────────────────────────────────────────────


def test_sweep_reclaims_a_stale_row_and_fires_critical(stub):
    stub["pending"].return_value = []
    stub["stuck"].return_value = [
        {"id": 9, "status": "tested", "lease_owner": "deadmac:123", "artifact_key": "tax"},
    ]
    out = ca.config_once()
    assert out.reclaimed == 1
    failed = [c for c in stub["stamp"].call_args_list if c.kwargs.get("status") == "failed"]
    assert any(
        c.kwargs.get("request_id") == 9 and c.kwargs.get("failed_stage") == "stale_reclaimed"
        for c in failed
    )
    assert _critical_fired(stub)


def test_sweep_is_a_noop_when_nothing_is_stuck(stub):
    stub["pending"].return_value = []
    stub["stuck"].return_value = []
    out = ca.config_once()
    assert out.reclaimed == 0
    assert not _critical_fired(stub)


def test_sweep_fetch_failure_is_logged_not_fatal(stub):
    stub["stuck"].side_effect = ca.portal_client.PortalTransportError("boom")
    stub["pending"].return_value = []
    out = ca.config_once()
    assert out.reclaimed == 0
    stub["pending"].assert_called_once()  # the cycle continued past the sweep to the pull
    assert any(c.args and c.args[0] == ca.Severity.ERROR for c in stub["log"].call_args_list)


# ── _unstrand_if_needed (idle self-heal) ──────────────────────────────────────────


def test_unstrand_recovers_a_stray_branch(mocker):
    mocker.patch.object(ca, "_git", return_value="config/req-7-po_materials-tax\n")
    reset = mocker.patch.object(ca, "_reset_to_main")
    ca._unstrand_if_needed()
    reset.assert_called_once()


def test_unstrand_is_a_noop_on_main(mocker):
    mocker.patch.object(ca, "_git", return_value="main\n")
    reset = mocker.patch.object(ca, "_reset_to_main")
    ca._unstrand_if_needed()
    reset.assert_not_called()


def test_config_once_unstrands_before_actuating(stub):
    stub["pending"].return_value = []
    ca.config_once()
    stub["unstrand"].assert_called_once()


def test_config_once_halts_loud_when_unstrand_fails(stub):
    stub["unstrand"].side_effect = RuntimeError("git checkout main failed")
    out = ca.config_once()
    assert out.halted == "unstrand_failed"
    stub["pending"].assert_not_called()
    assert any(c.args and c.args[0] == ca.Severity.ERROR for c in stub["log"].call_args_list)


# ── _commit_test_merge branch naming + empty-diff backstop ────────────────────────


def test_commit_test_merge_branch_name_and_empty_diff_backstop(mocker):
    """Branch is config/req-{id}-{workstream}-{artifact}; a no-op apply (empty staged diff)
    raises a clean reason rather than a confusing `git commit` exit-1."""
    git = mocker.patch.object(ca, "_git")
    mocker.patch.object(ca, "_gh")
    # bare subprocess.run: branch -D / push --delete (no-op) then `diff --cached --quiet` → 0 (no diff)
    run = mocker.patch.object(ca.subprocess, "run")
    run.return_value.returncode = 0
    with pytest.raises(RuntimeError, match="no config change"):
        ca._commit_test_merge(7, "po_materials", "tax", "tax: ...")
    checkout = [c for c in git.call_args_list if c.args[:1] == ("checkout",)]
    assert any("config/req-7-po_materials-tax" in c.args for c in checkout)


# ── _wait_for_ci (the synchronous CI gate) ────────────────────────────────────────


def test_wait_for_ci_returns_when_clean(mocker):
    mocker.patch.object(ca, "_gh", return_value=json.dumps({"mergeStateStatus": "CLEAN", "statusCheckRollup": []}))
    ca._wait_for_ci("config/req-1-po_materials-tax")


def test_wait_for_ci_raises_on_a_failed_check(mocker):
    mocker.patch.object(ca, "_gh", return_value=json.dumps({
        "mergeStateStatus": "BLOCKED",
        "statusCheckRollup": [{"name": "test", "conclusion": "FAILURE"}],
    }))
    with pytest.raises(RuntimeError, match="CI failed"):
        ca._wait_for_ci("config/req-1-po_materials-tax")


# ── _read_str_setting fail-soft (alert-hygiene) ───────────────────────────────────
# prove-the-control-bites (HOUSE_REFLEXES §2): a transient SmartsheetError USED to escape
# _read_str_setting → _polling_enabled → @its_error_log as an "unhandled" CRITICAL (paged
# every cycle during the 2026-07-14 token flap). These assert it is now a fail-soft WARN.


def test_read_str_setting_fail_soft_on_transient_smartsheet_error(mocker):
    log = mocker.patch.object(ca.error_log, "log")
    mocker.patch.object(
        ca.smartsheet_client, "get_setting",
        side_effect=ca.smartsheet_client.SmartsheetError("503 transient"),
    )
    # returns the fallback (no raise) ...
    assert ca._read_str_setting("po_materials.config_actuator.polling_enabled", "false") == "false"
    # ... and WARNs once with the distinct alert-hygiene code (never CRITICAL)
    assert log.call_count == 1
    assert log.call_args.args[0] is ca.Severity.WARN
    assert log.call_args.kwargs["error_code"] == "config_actuator.config_read_error"


def test_read_str_setting_auth_error_does_not_page(mocker):
    # SmartsheetAuthError (the invalid-token storm) is a SmartsheetError subclass → fail-soft.
    log = mocker.patch.object(ca.error_log, "log")
    mocker.patch.object(
        ca.smartsheet_client, "get_setting",
        side_effect=ca.smartsheet_client.SmartsheetAuthError("401 invalid api key"),
    )
    assert ca._read_str_setting(ca.CFG_WORKER_BASE_URL, "") == ""
    assert not any(c.args and c.args[0] is ca.Severity.CRITICAL for c in log.call_args_list)


def test_polling_enabled_is_false_not_raise_on_transient(mocker):
    # The load-bearing outcome: a token flap makes the cycle a clean no-op, not a CRITICAL.
    mocker.patch.object(ca.error_log, "log")
    mocker.patch.object(
        ca.smartsheet_client, "get_setting",
        side_effect=ca.smartsheet_client.SmartsheetError("timeout"),
    )
    assert ca._polling_enabled() is False


def test_read_str_setting_expected_errors_stay_silent(mocker):
    # Regression guard: a MISSING row / OPEN breaker still falls back WITHOUT a log line
    # (resolve_and_log owns the missing-row WARN; the breaker logs when it opens).
    log = mocker.patch.object(ca.error_log, "log")
    for exc in (
        ca.smartsheet_client.SmartsheetNotFoundError("no row"),
        ca.smartsheet_client.SmartsheetCircuitOpenError("open"),
    ):
        mocker.patch.object(ca.smartsheet_client, "get_setting", side_effect=exc)
        assert ca._read_str_setting("k", "fb") == "fb"
    assert log.call_count == 0


# ── DASH-6 legibility (2026-08-13) ─────────────────────────────────────────────────────────────
#
# The entry asked for a blanket "label every broad except" pass. Most sites already carried a
# distinct code; only these three were genuinely illegible, and each mis-directed remediation in a
# specific way. Fixing the three beats a blanket sweep that would churn nine healthy handlers.


def test_a_keychain_read_error_is_not_reported_as_an_absent_secret(stub, mocker):
    """A Keychain FAILURE and an ABSENT secret need different remediation.

    `_resolve_creds` swallowed both and returned None, so the caller logged `creds_unresolved`
    ("missing Worker base URL or config bearer") either way — aiming the §43 runbook at
    re-provisioning a secret that may exist and merely could not be read.
    """
    mocker.stopall()
    mocker.patch.object(ks, "check_system_state", return_value=ks.SystemState.ACTIVE)
    log = mocker.patch.object(ca.error_log, "log")
    mocker.patch.object(ca.creds_resolution, "read_base_url", return_value="https://portal.test")
    mocker.patch.object(ca.keychain, "get_secret", side_effect=RuntimeError("keychain locked"))

    assert ca._resolve_creds() is None  # still fails CLOSED

    codes = [c.kwargs.get("error_code") for c in log.call_args_list]
    assert "config_actuator.keychain_read_failed" in codes, (
        f"a Keychain read error must be distinguishable from an absent secret; got {codes}"
    )


def test_a_failed_portal_stamp_is_never_silent(stub):
    """The only fully-silent swallow in the file: `except Exception: pass` around the stamp.

    A failed stamp leaves the portal Status Monitor showing the request in flight forever with
    zero trace. The WARN must not displace the original CRITICAL — both are asserted.
    """
    stub["pending"].return_value = [{"id": 7}]
    stub["claim"].return_value = _row(rid=7)
    stub["apply"].side_effect = ca.config_apply.ConfigApplyError("bad rate")
    stub["stamp"].side_effect = RuntimeError("portal unreachable")

    out = ca.config_once()

    assert out.failed == 1
    codes = _codes(stub)
    assert "config_actuator.stamp_failed" in codes, f"silent stamp failure; got {codes}"
    assert any(c and c.startswith("config_actuator.failed.") for c in codes), (
        "the stamp WARN must not mask the original failure CRITICAL"
    )


def test_a_stage0_git_failure_does_not_borrow_the_bad_edit_data_code(stub):
    """A git-sync fault is a code/deploy-surface fault (Seth), not a bad-edit-data fault (Tier-2).

    Stage 0 reported through `config_actuator.failed.validated`, whose runbook entry says
    "re-do the edit in the portal" — wrong, and unsafe routing for a git failure. The PORTAL
    stamp must still say `validated` (the Worker's stage enum is fixed); only the error_code splits.
    """
    stub["pending"].return_value = [{"id": 9}]
    stub["claim"].return_value = _row(rid=9)
    stub["reset"].side_effect = ca.subprocess.CalledProcessError(1, "git", stderr=b"detached HEAD")

    out = ca.config_once()

    assert out.failed == 1
    codes = _codes(stub)
    assert "config_actuator.failed.sync_main" in codes, f"got {codes}"
    assert "config_actuator.failed.validated" not in codes, (
        "a git-sync failure must not route to the bad-edit-data runbook entry"
    )
    # …and the portal still receives the stage name its enum knows.
    assert stub["stamp"].call_args.kwargs["failed_stage"] == "validated"


def test_wait_for_ci_ignores_a_superseded_cancellation(mocker):
    """The req-6 shape (2026-08-19): `ci.yml`'s `concurrency: cancel-in-progress` cancels the
    older of two runs sharing a ref, and its jobs sit in the rollup as CANCELLED beside the
    live ones. Treating that as failure aborted a healthy publish ~20s in and reported bare
    job names ("test; portal; secrets") with no detail, because a cancelled job has no
    failing step to quote. The daemon must keep waiting for the live run instead."""
    views = [
        json.dumps({"mergeStateStatus": "BLOCKED", "statusCheckRollup": [
            {"name": "test", "status": "COMPLETED", "conclusion": "CANCELLED"},
            {"name": "test", "status": "IN_PROGRESS", "conclusion": ""},
            {"name": "portal", "status": "COMPLETED", "conclusion": "CANCELLED"},
            {"name": "portal", "status": "IN_PROGRESS", "conclusion": ""},
            {"name": "secrets", "status": "COMPLETED", "conclusion": "CANCELLED"},
            {"name": "secrets", "status": "COMPLETED", "conclusion": "SUCCESS"},
        ]}),
        json.dumps({"mergeStateStatus": "CLEAN", "statusCheckRollup": []}),
    ]
    mocker.patch.object(ca, "_gh", side_effect=lambda *a: views.pop(0) if a[:2] == ("pr", "view") else "")
    mocker.patch.object(ca.time, "sleep")
    ca._wait_for_ci("config/req-6-po_materials-tax")  # returns without raising


def test_wait_for_ci_still_raises_on_a_cancellation_with_no_successor(mocker):
    """Fail CLOSED: an operator cancelling the run (or a whole workflow cancelled outright)
    has no live or succeeding run of the same name, and must NOT read as green."""
    mocker.patch.object(ca, "_gh", return_value=json.dumps({
        "mergeStateStatus": "BLOCKED",
        "statusCheckRollup": [{"name": "test", "status": "COMPLETED", "conclusion": "CANCELLED"}],
    }))
    with pytest.raises(RuntimeError, match="CI failed"):
        ca._wait_for_ci("config/req-1-po_materials-tax")


def test_wait_for_ci_raises_on_a_real_failure_beside_a_superseded_cancellation(mocker):
    """The guard is narrow: neutralising a superseded CANCELLED must not mask a genuine
    FAILURE reported by the live run of that same job."""
    mocker.patch.object(ca, "_gh", return_value=json.dumps({
        "mergeStateStatus": "BLOCKED",
        "statusCheckRollup": [
            {"name": "test", "status": "COMPLETED", "conclusion": "CANCELLED"},
            {"name": "test", "status": "COMPLETED", "conclusion": "FAILURE"},
        ],
    }))
    with pytest.raises(RuntimeError, match="CI failed"):
        ca._wait_for_ci("config/req-1-po_materials-tax")


# ---------------------------------------------------------------------------
# Orphan actuator-branch cleanup — mirrors tests/test_publish_daemon.py's block.
# `config/req-1-po_materials-purchaser` sat on the remote for 40 days carrying NO PR at all,
# which is the shape these cover.
# ---------------------------------------------------------------------------


def _ls_remote(*branches: str) -> str:
    return "\n".join(f"0000000000000000000000000000000000000000\trefs/heads/{b}" for b in branches)


@pytest.mark.parametrize(
    "branch,expected",
    [
        ("config/req-1-po_materials-purchaser", 1),
        ("config/req-12-po_materials-tax", 12),
        # Everything below gates a DELETE and must stay unmatched.
        ("config/req-x-thing", None),
        ("config/reqs-1-thing", None),
        ("config/req-1", None),
        ("feat/config/req-1-thing", None),
        ("config/hand-edited", None),
        ("publish/req-5-erosion-inspection", None),  # the OTHER daemon's branch
        ("main", None),
    ],
)
def test_branch_request_id_is_strict(branch, expected):
    assert ca._branch_request_id(branch) == expected


def test_close_and_delete_refuses_a_merged_pr(mocker):
    mocker.patch.object(
        ca, "_gh",
        return_value=json.dumps([{"number": 99, "state": "MERGED",
                                  "mergedAt": "2026-07-10T16:16:18Z"}]),
    )
    refs = mocker.patch.object(ca, "_delete_branch_refs")
    assert ca._close_and_delete_branch("config/req-1-po_materials-purchaser", "why") is False
    refs.assert_not_called()


def test_close_and_delete_handles_a_branch_with_no_pr(mocker):
    """The real 40-day orphan's exact shape: a pushed branch that never got a PR."""
    mocker.patch.object(ca, "_gh", return_value="[]")
    refs = mocker.patch.object(ca, "_delete_branch_refs")
    mocker.patch.object(ca, "_git", return_value="")
    assert ca._close_and_delete_branch("config/req-1-po_materials-purchaser", "r") is True
    refs.assert_called_once_with("config/req-1-po_materials-purchaser")


def test_close_and_delete_reports_failure_when_ref_survives(mocker):
    mocker.patch.object(ca, "_gh", return_value="[]")
    mocker.patch.object(ca, "_delete_branch_refs")
    mocker.patch.object(ca, "_git",
                        return_value=_ls_remote("config/req-1-po_materials-purchaser"))
    assert ca._close_and_delete_branch("config/req-1-po_materials-purchaser", "r") is False


@pytest.fixture
def sweep_env(mocker):
    creds = ca._Creds("https://portal.test", "tok")
    mocker.patch.object(ca.portal_client, "get_config_pending", return_value=[])
    mocker.patch.object(ca.portal_client, "get_config_stuck", return_value=[])
    mocker.patch.object(ca, "_git",
                        return_value=_ls_remote("config/req-1-po_materials-purchaser"))
    mocker.patch.object(ca, "_branch_tip_epoch",
                        return_value=time.time() - (ca.ORPHAN_MIN_AGE_S * 10))
    close = mocker.patch.object(ca, "_close_and_delete_branch", return_value=True)
    return {"creds": creds, "close": close}


def test_sweep_deletes_a_true_orphan(sweep_env):
    stats = ca.ConfigStats()
    ca._sweep_orphaned_branches(sweep_env["creds"], stats)
    sweep_env["close"].assert_called_once()
    assert sweep_env["close"].call_args[0][0] == "config/req-1-po_materials-purchaser"
    assert stats.orphans_cleaned == 1


def test_sweep_skips_a_request_still_in_flight(sweep_env, mocker):
    mocker.patch.object(ca.portal_client, "get_config_stuck", return_value=[{"id": 1}])
    stats = ca.ConfigStats()
    ca._sweep_orphaned_branches(sweep_env["creds"], stats)
    sweep_env["close"].assert_not_called()


def test_sweep_skips_a_branch_younger_than_the_floor(sweep_env, mocker):
    mocker.patch.object(ca, "_branch_tip_epoch", return_value=time.time() - 5)
    ca._sweep_orphaned_branches(sweep_env["creds"], ca.ConfigStats())
    sweep_env["close"].assert_not_called()


def test_sweep_skips_a_branch_of_unknown_age(sweep_env, mocker):
    mocker.patch.object(ca, "_branch_tip_epoch", return_value=None)
    ca._sweep_orphaned_branches(sweep_env["creds"], ca.ConfigStats())
    sweep_env["close"].assert_not_called()


def test_sweep_deletes_nothing_when_the_in_flight_read_fails(sweep_env, mocker):
    mocker.patch.object(ca.portal_client, "get_config_stuck",
                        side_effect=RuntimeError("worker down"))
    ca._sweep_orphaned_branches(sweep_env["creds"], ca.ConfigStats())
    sweep_env["close"].assert_not_called()


def test_sweep_ignores_branches_that_are_not_ours(sweep_env, mocker):
    mocker.patch.object(
        ca, "_git",
        return_value=_ls_remote("config/hand-edited", "publish/req-5-erosion", "main"),
    )
    ca._sweep_orphaned_branches(sweep_env["creds"], ca.ConfigStats())
    sweep_env["close"].assert_not_called()


def test_stage2_records_the_failure_before_cleaning_up(stub, mocker):
    """Ordering is the contract: stamp + CRITICAL first, remove the branch second."""
    order: list[str] = []
    stub["commit"].side_effect = RuntimeError("CI red")
    mocker.patch.object(ca, "_fail", side_effect=lambda *a, **k: order.append("fail"))
    mocker.patch.object(ca, "_close_and_delete_branch",
                        side_effect=lambda *a, **k: order.append("cleanup"))
    stats = ca.ConfigStats()
    ca._actuate(ca._Creds("https://portal.test", "tok"), _row(rid=7), stats)
    assert order == ["fail", "cleanup"]
    assert stats.failed == 1


def test_stage2_cleanup_targets_the_same_branch_commit_created(stub, mocker):
    stub["commit"].side_effect = RuntimeError("CI red")
    mocker.patch.object(ca, "_fail")
    close = mocker.patch.object(ca, "_close_and_delete_branch")
    ca._actuate(ca._Creds("https://portal.test", "tok"), _row(rid=7), ca.ConfigStats())
    assert close.call_args[0][0] == ca._branch_name(7, "po_materials", "tax")
