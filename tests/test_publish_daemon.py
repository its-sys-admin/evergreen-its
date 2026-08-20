"""Orchestration tests for the Mac publish daemon (slice 3b) — the privileged git/deploy
ops + portal_client HTTP are mocked; apply_publish runs against the REAL catalog. Verifies
the stage sequence + stamping + the fail+CRITICAL detect-and-alert (C12 mandate)."""
from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime

import pytest

import shared.kill_switch as ks
from safety_reports import publish_daemon as pd
from shared import sustained_failure

#: The REAL gate reader, captured at import BEFORE any fixture patches it. The `stub`
#: fixture mocks `pd._polling_enabled` wholesale, which is exactly why the fleet-window
#: suppressor below could ship: no test ever drove the real
#: `_polling_enabled → _read_str_setting → get_setting` chain. Tests that need the
#: production chain re-install this.
_REAL_POLLING_ENABLED = pd._polling_enabled


def _create_def() -> dict:
    return {
        "form_code": "incident-v1", "parent_form_code": "incident", "form_name": "Incident",
        "variant_label": None, "version": 1, "archetype": "rows_signatures",
        "source_pdf": "x.pdf", "sections": [{"type": "static_text", "text": "x"}],
    }


def _row(op: str, identity: str, parent: str, *, definition: dict | None = None,
         target: str | None = None, rid: int = 1) -> dict:
    return {
        "id": rid, "op": op, "identity": identity, "parent_form_code": parent,
        "target_form_code": target,
        "definition_json": json.dumps(definition) if definition is not None else None,
        "status": "queued",
    }


@pytest.fixture
def stub(mocker):
    mocker.patch.object(ks, "check_system_state", return_value=ks.SystemState.ACTIVE)
    return {
        "enabled": mocker.patch.object(pd, "_polling_enabled", return_value=True),
        "creds": mocker.patch.object(pd, "_resolve_creds",
                                     return_value=pd._Creds("https://portal.test", "tok")),
        "pending": mocker.patch.object(pd.portal_client, "get_publish_pending"),
        "claim": mocker.patch.object(pd.portal_client, "claim_publish"),
        "stamp": mocker.patch.object(pd.portal_client, "stamp_publish", return_value=True),
        # PR-2: publish_once now sweeps stale rows (calls get_publish_stuck). Default to none so
        # the existing tests' sweep is a no-op; the sweep tests below set a return value.
        "stuck": mocker.patch.object(pd.portal_client, "get_publish_stuck", return_value=[]),
        "reset": mocker.patch.object(pd, "_reset_to_main"),
        "unstrand": mocker.patch.object(pd, "_unstrand_if_needed"),
        "apply_wt": mocker.patch.object(pd, "_apply_to_worktree"),
        "commit": mocker.patch.object(pd, "_commit_test_merge"),
        "deploy": mocker.patch.object(pd, "_deploy_land_health"),
        "archive": mocker.patch.object(pd, "_regenerate_archive"),
        # PR-1: the daemon now passes required_content to apply_publish. These tests target the
        # state machine / stamping / error-handling, NOT the legal floor (tested in
        # test_publish_manifest + test_form_definitions), so stub the floor to an empty manifest
        # (no requirements → any definition passes the C3 re-check) to keep them decoupled.
        "req_content": mocker.patch.object(pd, "_load_required_content", return_value={}),
        # Slice 1 (R3-F1): publish_once now gates each cycle-with-work on remote D1 migration
        # state (wrangler shell-out). Default to "none pending" so existing tests proceed; the
        # deploy-gate tests below set a pending list / a failure.
        "migrations": mocker.patch.object(pd, "_pending_migrations", return_value=[]),
        # R4-F1: the daemon now writes an ITS_Daemon_Health heartbeat per cycle. Mock the
        # two thin-delegator seams so no test touches live state / Smartsheet.
        "hb": mocker.patch.object(pd, "_write_heartbeat"),
        "hb_row": mocker.patch.object(pd, "_write_heartbeat_row"),
        # MUST be patched: the real one touches ~/its/.watchdog/publish_daemon.last_run on
        # the LIVE host, so an unpatched suite run would fake Check-C freshness for a daemon
        # that never ran.
        "marker": mocker.patch.object(pd, "_write_watchdog_marker"),
        "circuit": mocker.patch.object(pd.circuit_breaker, "is_open", return_value=False),
        "log": mocker.patch.object(pd.error_log, "log"),
    }


def _statuses(stub) -> list[str]:
    return [c.kwargs["status"] for c in stub["stamp"].call_args_list]


def _critical_fired(stub) -> bool:
    return any(c.args and c.args[0] == pd.Severity.CRITICAL for c in stub["log"].call_args_list)


# ── happy paths ───────────────────────────────────────────────────────────────


def test_create_actuates_through_the_full_state_machine(stub):
    stub["pending"].return_value = [{"id": 1}]
    stub["claim"].return_value = _row("create", "incident", "incident", definition=_create_def())
    out = pd.publish_once()
    assert out.actuated == 1 and out.failed == 0
    assert _statuses(stub) == ["validated", "tested", "live", "archived"]
    stub["apply_wt"].assert_called_once()
    stub["commit"].assert_called_once()
    stub["deploy"].assert_called_once()
    stub["archive"].assert_called_once()
    assert not _critical_fired(stub)


def test_delete_actuates_without_a_definition(stub):
    stub["pending"].return_value = [{"id": 2}]
    stub["claim"].return_value = _row("delete", "jha", "jha", rid=2)  # jha exists → retire
    out = pd.publish_once()
    assert out.actuated == 1
    assert _statuses(stub) == ["validated", "tested", "live", "archived"]


def test_regenerate_archive_uses_venv_interpreter_not_bare_python(mocker):
    """Regression: the `archived` stage shells out with sys.executable, NOT a bare "python".
    Under launchd (minimal PATH; macOS ships only `python3`, and the interpreter is really
    ~/its/.venv/bin/python) a bare "python" raised FileNotFoundError, failing every publish
    at `archived` AFTER the form had already gone live."""
    run = mocker.patch.object(pd.subprocess, "run")
    mocker.patch.object(pd.tempfile, "mkdtemp", return_value="/tmp/its_form_archive_test")
    rmtree = mocker.patch.object(pd.shutil, "rmtree")
    pd._regenerate_archive()
    cmd = run.call_args.args[0]
    assert cmd[0] == pd.sys.executable
    assert cmd[0] != "python"  # the exact bug
    # renders into a throwaway tempdir (--out-dir), NOT ~/its/form_archive_out, and cleans up
    assert cmd[1:] == ["-m", "scripts.generate_form_archive", "--upload", "--out-dir", "/tmp/its_form_archive_test"]
    rmtree.assert_called_once_with("/tmp/its_form_archive_test", ignore_errors=True)


# ── failures stamp failed(stage) + fire the operator CRITICAL ────────────────────


def test_validation_failure_stamps_failed_and_fires_critical(stub):
    stub["pending"].return_value = [{"id": 3}]
    # op=create with identity 'jha' (already exists) → apply_publish raises at stage 1.
    stub["claim"].return_value = _row("create", "jha", "jha", definition=_create_def(), rid=3)
    out = pd.publish_once()
    assert out.failed == 1 and out.actuated == 0
    assert _statuses(stub) == ["failed"]
    assert stub["stamp"].call_args.kwargs["failed_stage"] == "validated"
    assert _critical_fired(stub)
    stub["commit"].assert_not_called()  # never reached actuation


def test_fail_redacts_a_secret_bearing_reason_before_egress(stub):
    """CE-1 (§54 parity with config_actuator._fail): `_fail`'s `reason` can carry a raw
    subprocess stderr tail (`_exc_reason` surfaces `(exc.stderr)[-600:]`), and `stamp_publish`'s
    `failure_reason` lands on the portal Status Monitor — a sink that BYPASSES error_log's redact
    choke. The token must be scrubbed on BOTH the stamp leg and the operator CRITICAL message."""
    secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    pd._fail(pd._Creds("https://portal.test", "tok"), 7, "live",
             f"wrangler exit 1: fatal: bad credentials, token {secret}")
    sent = stub["stamp"].call_args.kwargs["failure_reason"]
    assert secret not in sent          # RED on the pre-CE-1 unredacted `reason[:1800]`
    assert "<redacted>" in sent
    crit = [c for c in stub["log"].call_args_list
            if c.args and c.args[0] == pd.Severity.CRITICAL]
    assert crit, "expected a CRITICAL to fire"
    assert secret not in crit[-1].args[2]


def test_commit_failure_stamps_failed_tested_and_fires_critical(stub):
    stub["pending"].return_value = [{"id": 4}]
    stub["claim"].return_value = _row("create", "incident", "incident", definition=_create_def(), rid=4)
    stub["commit"].side_effect = subprocess.CalledProcessError(1, ["gh"], stderr="CI red")
    out = pd.publish_once()
    assert out.failed == 1
    assert _statuses(stub) == ["validated", "failed"]
    assert stub["stamp"].call_args.kwargs["failed_stage"] == "tested"
    assert _critical_fired(stub)
    stub["deploy"].assert_not_called()


def test_deploy_failure_stamps_failed_live(stub):
    stub["pending"].return_value = [{"id": 5}]
    stub["claim"].return_value = _row("create", "incident", "incident", definition=_create_def(), rid=5)
    stub["deploy"].side_effect = RuntimeError("wrangler boom")
    out = pd.publish_once()
    assert out.failed == 1
    assert _statuses(stub) == ["validated", "tested", "failed"]
    assert stub["stamp"].call_args.kwargs["failed_stage"] == "live"
    assert _critical_fired(stub)
    stub["archive"].assert_not_called()


# ── gating / fail-closed / lease ─────────────────────────────────────────────────


def test_polling_disabled_halts_without_polling(stub):
    stub["enabled"].return_value = False
    out = pd.publish_once()
    assert out.halted == "polling_disabled"
    stub["pending"].assert_not_called()


def test_unresolved_creds_halts_loud(stub):
    stub["creds"].return_value = None
    out = pd.publish_once()
    assert out.halted == "creds_unresolved"
    assert any(c.args and c.args[0] == pd.Severity.ERROR for c in stub["log"].call_args_list)
    stub["pending"].assert_not_called()


def _codes(stub) -> list[str]:
    return [c.kwargs.get("error_code") for c in stub["log"].call_args_list]


def test_transient_base_url_warns_and_skips_without_paging(stub):
    # A Smartsheet blip on the base-URL read is NOT a missing credential. The live po_poll
    # instance of this bug (2026-07-20 04:42Z) fell back to "" and alerted that credentials
    # were unset while both Keychain entries were fine — a false alarm that aimed the §43
    # repair at re-provisioning the internal bearer, a high-capability-class action, for a
    # condition needing none. Transient => WARN + skip, never the misconfig report.
    stub["creds"].return_value = pd.TransientUnavailable(
        reason="SmartsheetError: (<PreparedRequest [GET]>, None)"
    )
    out = pd.publish_once()
    assert out.halted == "creds_transient"
    stub["pending"].assert_not_called()  # still FAIL-CLOSED — it does not publish
    codes = _codes(stub)
    assert "publish_daemon.creds_transient" in codes
    assert "publish_daemon.creds_unresolved" not in codes, (
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
    stub["claim"].return_value = _row("create", "incident", "incident", definition=_create_def())
    assert pd.publish_once().actuated == 1
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
    assert pd.publish_once().halted == expected_halt
    stub["marker"].assert_not_called()


# ── transient Smartsheet fence (2026-07-21) ──────────────────────────────────────


@pytest.fixture
def fence_state(mocker, tmp_path):
    """Point the module-level fence at a per-test state file (never live state)."""
    fence = pd.sustained_failure.TransientFence(
        pd.SCRIPT_NAME,
        state_path=tmp_path / "publish_config_read.json",
        transient_error_code="publish_daemon.config_read_transient",
        sustained_error_code="publish_daemon.config_read_sustained",
        threshold=pd.sustained_failure.DEFAULT_CRITICAL_THRESHOLD,
        runbook="docs/runbooks/safety_portal_forms.md",
    )
    mocker.patch.object(pd, "_CONFIG_READ_FENCE", fence)
    return fence


# ── the fleet-window suppressor (2026-07-21 re-review) ──────────────────────────
#
# These are INTEGRATION-SHAPED on purpose. The unit tests above cannot see this class of
# bug: `stub` mocks `_polling_enabled` wholesale, and tests/test_transient_fence.py drives
# `handle()` in isolation. So the production chain — circuit-open raised inside
# `get_setting`, swallowed into a fail-open "false" by `_read_str_setting`, the cycle
# returning NORMALLY, and the "success" path then clearing SHARED fleet state — was
# exercised by nothing, and shipped a control that provably never fires in production.


@pytest.fixture
def real_gate(mocker):
    """Re-install the REAL `_polling_enabled` and mock the Smartsheet call under it."""
    mocker.patch.object(pd, "_polling_enabled", _REAL_POLLING_ENABLED)
    return mocker.patch.object(pd.smartsheet_client, "get_setting")


def _open_fleet_window() -> None:
    """Prime the shared circuit-open window as another daemon's observation would."""
    sustained_failure.record_circuit_open("some.other_daemon", "breaker OPEN")


def test_circuit_open_gate_read_never_wipes_the_shared_fleet_window(
    stub, fence_state, real_gate
):
    """THE regression: publish-daemon runs at StartInterval 120, so if its cycle clears the
    fleet circuit-open window the window can never mature to CIRCUIT_OPEN_SUSTAINED_SECONDS
    (600 s) and the fleet CRITICAL is unreachable in production — leaving watchdog Check J
    at 07:00 daily as the only page, i.e. up to ~24 h to notice frozen sends.

    A fail-open fallback is NOT evidence that the backend is reachable. Whatever this cycle
    concludes about its own gate, it has learned NOTHING that entitles it to close a
    fleet-scoped window opened by another process."""
    real_gate.side_effect = pd.smartsheet_client.SmartsheetCircuitOpenError("breaker OPEN")
    _open_fleet_window()
    assert sustained_failure.CIRCUIT_OPEN_STATE_PATH.exists()

    pd.publish_once()

    assert sustained_failure.CIRCUIT_OPEN_STATE_PATH.exists(), (
        "the 120 s publish daemon wiped the fleet circuit-open window during an outage — "
        "the fleet escalation can never mature"
    )


def test_circuit_open_gate_read_is_observed_not_silently_read_as_disabled(
    stub, fence_state, real_gate
):
    """Collapsing circuit-open into the gate's "false" fallback is the unobservable-config-
    resolution anti-pattern `_read_str_setting`'s own docstring names: the daemon reports
    `polling_disabled` (a lie — the operator never paused it) and contributes nothing to
    the fleet's outage picture."""
    real_gate.side_effect = pd.smartsheet_client.SmartsheetCircuitOpenError("breaker OPEN")

    out = pd.publish_once()

    assert out.halted != "polling_disabled"
    assert sustained_failure.CIRCUIT_OPEN_STATE_PATH.exists(), (
        "publish_daemon must reach a fenced site during an outage — otherwise the docs' "
        "claim that its 120 s cadence drives the fleet time-to-page is false"
    )
    stub["pending"].assert_not_called()  # still fail-closed


def test_one_daemon_resetting_cannot_erase_another_daemons_fleet_observation(tmp_path):
    """The interleaving that the isolated fence tests could not see: daemon A observes the
    outage; daemon B (a different process, different lane, possibly a fail-open read) calls
    reset(). B's local success says nothing about A's window."""
    observer = sustained_failure.TransientFence(
        "daemon_a",
        state_path=tmp_path / "a.json",
        transient_error_code="a.transient",
        sustained_error_code="a.sustained",
    )
    other = sustained_failure.TransientFence(
        "daemon_b",
        state_path=tmp_path / "b.json",
        transient_error_code="b.transient",
        sustained_error_code="b.sustained",
    )
    observer.handle(pd.smartsheet_client.SmartsheetCircuitOpenError("breaker OPEN"))
    assert sustained_failure.CIRCUIT_OPEN_STATE_PATH.exists()

    other.reset()

    assert sustained_failure.CIRCUIT_OPEN_STATE_PATH.exists(), (
        "TransientFence.reset() cleared fleet-scoped state on one daemon's local belief"
    )


def test_transient_gate_read_halts_without_paging(stub, fence_state):
    """The 14:37Z signature: a ReadTimeout inside get_setting escaped the pass and landed
    as CRITICAL uncaught_exception. Now: halted, ERROR, no page, recovered next cycle."""
    stub["enabled"].side_effect = pd.smartsheet_client.SmartsheetTransientError("ReadTimeout")
    out = pd.publish_once()
    assert out.halted == "smartsheet_transient"
    assert not _critical_fired(stub)
    stub["pending"].assert_not_called()
    stub["hb_row"].assert_called_once()
    assert stub["hb_row"].call_args.kwargs["status"] == "ERROR"


def test_transient_base_url_read_halts_without_naming_credentials(stub, fence_state):
    """A failed READ must not be reported as a MISSING credential — that misdirects the
    §43 repair at a high-capability-class secrets action."""
    stub["creds"].return_value = pd.creds_resolution.TransientUnavailable(
        reason="SmartsheetTransientError: HTTP 500", circuit_open=False
    )
    out = pd.publish_once()
    assert out.halted == "creds_transient"
    assert not _critical_fired(stub)
    messages = [c.args[2] for c in stub["log"].call_args_list if len(c.args) > 2]
    assert not any("bearer" in m or "credential" in m for m in messages)
    # …but it IS counted, so a sustained base-URL outage still escalates on the ladder.
    assert "publish_daemon.config_read_transient" in _codes(stub)


def test_circuit_open_base_url_read_halts_uncounted(stub, fence_state, tmp_path):
    stub["creds"].return_value = pd.creds_resolution.CREDS_TRANSIENT
    out = pd.publish_once()
    assert out.halted == "creds_transient"
    assert not (tmp_path / "publish_config_read.json").exists()


def test_sustained_transient_gate_read_escalates_to_critical(stub, fence_state):
    stub["enabled"].side_effect = pd.smartsheet_client.SmartsheetTransientError("HTTP 500")
    for _ in range(pd.sustained_failure.DEFAULT_CRITICAL_THRESHOLD):
        pd.publish_once()
    sustained = [c for c in stub["log"].call_args_list
                 if c.kwargs.get("error_code") == "publish_daemon.config_read_sustained"]
    assert len(sustained) == 1
    assert sustained[0].args[0] == pd.Severity.CRITICAL


def test_non_transient_gate_read_still_propagates(stub, fence_state):
    """Unwrapped so the propagation is visible: `publish_once` is @its_error_log-wrapped,
    and a propagated exception there IS the CRITICAL uncaught_exception path. Nothing
    about a real misconfig or bug got softened."""
    stub["enabled"].side_effect = pd.smartsheet_client.SmartsheetAuthError("401")
    with pytest.raises(pd.smartsheet_client.SmartsheetAuthError):
        pd.publish_once()


@pytest.mark.parametrize(
    "exc",
    [
        pd.smartsheet_client.SmartsheetRateLimitError("429"),
        pd.smartsheet_client.SmartsheetValidationError("400"),
    ],
)
def test_non_transient_base_url_read_is_not_softened_by_the_shared_sentinel(
    stub, fence_state, exc
):
    """`read_base_url`'s transient bucket is BROAD (it swallows 429 and any bodied
    SmartsheetError) because that is right for the five portal pullers. This daemon's own
    reader never did — a 429 propagated to CRITICAL — so routing the read through the
    shared sentinel must not soften it. The re-classification at the call site is what
    keeps that promise."""
    stub["creds"].return_value = pd.creds_resolution.TransientUnavailable(
        reason=f"{type(exc).__name__}: {exc!r}", circuit_open=False, exc=exc
    )

    with pytest.raises(type(exc)):
        pd.publish_once()

    stub["pending"].assert_not_called()


def test_gate_read_success_clears_the_ladder_even_when_polling_is_disabled(
    stub, fence_state, tmp_path
):
    """Resetting only on the FULL-success path left a stale count behind every
    `polling_disabled` return: a counter that reached 4 before the operator paused polling
    would fire CRITICAL on the very first transient after resume."""
    stub["enabled"].side_effect = pd.smartsheet_client.SmartsheetTransientError("HTTP 500")
    for _ in range(pd.sustained_failure.DEFAULT_CRITICAL_THRESHOLD - 1):
        pd.publish_once()

    stub["enabled"].side_effect = None
    stub["enabled"].return_value = False  # operator pauses polling; the gate read SUCCEEDS
    assert pd.publish_once().halted == "polling_disabled"

    stub["enabled"].return_value = True
    stub["enabled"].side_effect = pd.smartsheet_client.SmartsheetTransientError("HTTP 500")
    pd.publish_once()

    sustained = [c for c in stub["log"].call_args_list
                 if c.kwargs.get("error_code") == "publish_daemon.config_read_sustained"]
    assert sustained == []


def test_already_leased_row_is_skipped(stub):
    stub["pending"].return_value = [{"id": 6}]
    stub["claim"].return_value = None  # a concurrent run already leased it
    out = pd.publish_once()
    assert out.skipped_unclaimed == 1 and out.actuated == 0
    stub["commit"].assert_not_called()


# ── D1 pending-migrations deploy gate (Slice 1, R3-F1 — forensic class #2, publish #434) ──


def test_pending_migrations_refuse_the_cycle_before_claiming(stub):
    """Unapplied remote migrations REFUSE the whole cycle pre-claim: no lease burned, no row
    stamped (they stay `pending` on the Worker for the next cycle), and the refusal is LOUD —
    a CRITICAL under the distinct category naming the pending files."""
    stub["pending"].return_value = [{"id": 7}]
    stub["migrations"].return_value = ["0030_job_daily_requirements.sql", "0031_job_expected_materials.sql"]
    out = pd.publish_once()
    assert out.halted == "pending_migrations"
    assert out.polled == 1 and out.actuated == 0 and out.failed == 0
    stub["claim"].assert_not_called()
    stub["commit"].assert_not_called()
    stub["deploy"].assert_not_called()
    stub["stamp"].assert_not_called()  # nothing terminal-failed — the request survives
    crit = [
        c for c in stub["log"].call_args_list
        if c.args and c.args[0] == pd.Severity.CRITICAL
        and c.kwargs.get("error_code") == pd.ERR_PENDING_MIGRATIONS
    ]
    assert len(crit) == 1
    assert "0030_job_daily_requirements.sql" in crit[0].args[2]  # the pending list is named


def test_operator_apply_unblocks_the_next_cycle_automatically(stub):
    """The retry semantics the pre-claim placement buys: cycle 1 refuses (pending), the
    operator applies (no re-publish, no daemon poke), cycle 2 actuates the SAME queued row."""
    stub["pending"].return_value = [{"id": 8}]
    stub["claim"].return_value = _row("create", "incident", "incident", definition=_create_def(), rid=8)
    stub["migrations"].return_value = ["0032_job_daily_requirements_kinds.sql"]
    assert pd.publish_once().halted == "pending_migrations"
    stub["migrations"].return_value = []  # the operator ran `wrangler d1 migrations apply`
    out = pd.publish_once()
    assert out.halted is None and out.actuated == 1
    assert _statuses(stub) == ["validated", "tested", "live", "archived"]


def test_migration_check_failure_halts_fail_closed(stub):
    """Cannot verify ⇒ must not deploy: a wrangler-list failure halts the cycle (fail-closed)
    with a PAGING CRITICAL under its own category (a sustained failure blocks every publish —
    ERROR would be a silent stall; ops review), and nothing is claimed."""
    stub["pending"].return_value = [{"id": 9}]
    stub["migrations"].side_effect = subprocess.CalledProcessError(1, ["npx"], stderr="net down")
    out = pd.publish_once()
    assert out.halted == "migration_check_failed"
    stub["claim"].assert_not_called()
    assert any(
        c.args and c.args[0] == pd.Severity.CRITICAL
        and c.kwargs.get("error_code") == pd.ERR_MIGRATION_CHECK
        for c in stub["log"].call_args_list
    )


def test_idle_cycle_never_shells_out_to_wrangler(stub):
    """No rows → no deploy possible → the gate is skipped (a 60s-cadence daemon must not
    hit wrangler/network every idle cycle)."""
    stub["pending"].return_value = []
    out = pd.publish_once()
    assert out.halted is None
    stub["migrations"].assert_not_called()


# ── ITS_Daemon_Health heartbeat (R4-F1) ──────────────────────────────────────────


def test_cycle_writes_ok_heartbeat(stub):
    stub["pending"].return_value = [{"id": 1}]
    stub["claim"].return_value = _row("create", "incident", "incident", definition=_create_def())
    pd.publish_once()
    stub["hb"].assert_called_once()
    assert stub["hb_row"].call_args.kwargs["status"] == "OK"
    assert stub["hb_row"].call_args.kwargs["items_processed"] == 1
    assert stub["hb_row"].call_args.kwargs["error_summary"] is None


def test_failed_actuation_writes_degraded_heartbeat(stub):
    stub["pending"].return_value = [{"id": 5}]
    stub["claim"].return_value = _row("create", "incident", "incident", definition=_create_def(), rid=5)
    stub["deploy"].side_effect = RuntimeError("wrangler boom")
    pd.publish_once()
    assert stub["hb_row"].call_args.kwargs["status"] == "DEGRADED"
    assert "failed=1" in stub["hb_row"].call_args.kwargs["error_summary"]


def test_disabled_cycle_skips_heartbeat(stub):
    stub["enabled"].return_value = False
    pd.publish_once()
    stub["hb"].assert_not_called()
    stub["hb_row"].assert_not_called()


def test_unresolved_creds_write_error_heartbeat(stub):
    stub["creds"].return_value = None
    pd.publish_once()
    stub["hb"].assert_called_once()
    assert stub["hb_row"].call_args.kwargs["status"] == "ERROR"


def test_pending_migrations_write_warn_heartbeat(stub):
    # A deliberate, bounded refusal (rows stay queued; operator apply unblocks) → WARN,
    # not ERROR — mirrors portal_poll's halted_transient precedent.
    stub["pending"].return_value = [{"id": 7}]
    stub["migrations"].return_value = ["0033_x.sql"]
    pd.publish_once()
    assert stub["hb_row"].call_args.kwargs["status"] == "WARN"
    assert "deploy blocked" in stub["hb_row"].call_args.kwargs["error_summary"]


def test_open_circuit_writes_circuit_open_heartbeat(stub):
    stub["pending"].return_value = []
    stub["circuit"].return_value = True
    pd.publish_once()
    assert stub["hb_row"].call_args.kwargs["status"] == "CIRCUIT_OPEN"


def test_heartbeat_row_failure_never_blocks_the_cycle(stub):
    # Heartbeat-never-blocks: the outer-catch fence holds even if the delegator raises.
    stub["pending"].return_value = [{"id": 1}]
    stub["claim"].return_value = _row("create", "incident", "incident", definition=_create_def())
    stub["hb_row"].side_effect = RuntimeError("sheet down")
    out = pd.publish_once()
    assert out.actuated == 1  # primary work unharmed
    assert any(
        c.kwargs.get("error_code") == "daemon_health_write_failed"
        for c in stub["log"].call_args_list
    )


def test_reporter_registration_metadata_is_self_provisioning_config(stub):
    # A1 self-provision rides constructor config — pin the registration identity so the
    # ITS_Daemon_Health row this daemon creates is stable (shared row-state file, ARCH-2).
    r = pd._heartbeat_reporter
    assert r.daemon_name == "safety_reports.publish_daemon"
    assert r.workstream == "safety_reports"
    assert r.interval_seconds == 120
    assert r.row_state_path.name == "heartbeat_row_ids.json"


def test_pending_migrations_diffs_disk_against_remote_list(mocker, tmp_path):
    """_pending_migrations = on-disk *.sql names cross-checked against the wrangler
    `d1 migrations list --remote` output (which prints ONLY unapplied migrations), invoked
    exactly like the deploy stage (same cwd, local wrangler via npx)."""
    mocker.patch.object(pd, "_MIGRATIONS_DIR", tmp_path)
    for name in ("0030_a.sql", "0031_b.sql", "0032_c.sql"):
        (tmp_path / name).write_text("-- migration")
    (tmp_path / "notes.txt").write_text("not a migration")  # non-.sql ignored
    run = mocker.patch.object(pd.subprocess, "run")
    run.return_value.stdout = (
        "Migrations to be applied:\n"
        "┌──────────────┐\n│ 0031_b.sql │\n│ 0032_c.sql │\n└──────────────┘\n"
    )
    assert pd._pending_migrations() == ["0031_b.sql", "0032_c.sql"]
    cmd = run.call_args.args[0]
    assert cmd == ["npx", "wrangler", "d1", "migrations", "list", pd.D1_DATABASE_NAME, "--remote"]
    assert run.call_args.kwargs["cwd"] == pd._ROOT / "safety_portal"
    assert run.call_args.kwargs["check"] is True  # a wrangler failure raises (fail-closed)


def test_pending_migrations_empty_when_remote_is_current(mocker, tmp_path):
    mocker.patch.object(pd, "_MIGRATIONS_DIR", tmp_path)
    (tmp_path / "0030_a.sql").write_text("-- migration")
    run = mocker.patch.object(pd.subprocess, "run")
    run.return_value.stdout = "✅ No migrations to apply!\n"
    assert pd._pending_migrations() == []


def test_deploy_land_health_refuses_ahead_of_pending_migrations(mocker):
    """The authoritative in-stage gate: AFTER the pull, BEFORE `npm run deploy` — pending
    migrations raise PendingMigrationsError (the stage-3 fence stamps failed('live')) and the
    deploy subprocess is NEVER invoked. Fires the distinct CRITICAL naming the files."""
    mocker.patch.object(pd, "_git")
    mocker.patch.object(pd, "_pending_migrations", return_value=["0030_a.sql"])
    run = mocker.patch.object(pd.subprocess, "run")
    log = mocker.patch.object(pd.error_log, "log")
    ping = mocker.patch.object(pd.portal_client, "get_publish_pending")
    with pytest.raises(pd.PendingMigrationsError, match="0030_a.sql"):
        pd._deploy_land_health(pd._Creds("https://portal.test", "tok"), "incident-v1")
    run.assert_not_called()   # the deploy never ran
    ping.assert_not_called()  # nor the liveness ping
    assert any(
        c.args and c.args[0] == pd.Severity.CRITICAL
        and c.kwargs.get("error_code") == pd.ERR_PENDING_MIGRATIONS
        for c in log.call_args_list
    )


def test_deploy_land_health_deploys_when_remote_is_current(mocker):
    mocker.patch.object(pd, "_git")
    mocker.patch.object(pd, "_pending_migrations", return_value=[])
    run = mocker.patch.object(pd.subprocess, "run")
    mocker.patch.object(pd.portal_client, "get_publish_pending")
    pd._deploy_land_health(pd._Creds("https://portal.test", "tok"), "incident-v1")
    assert run.call_args.args[0] == ["npm", "run", "deploy"]


# ── stale-row sweep (PR-2: reclaim a crashed/stalled publish before it wedges a parent) ──


def test_sweep_reclaims_a_stale_row_and_fires_critical(stub):
    stub["pending"].return_value = []
    stub["stuck"].return_value = [
        {"id": 9, "status": "tested", "lease_owner": "deadmac:123", "parent_form_code": "jha"},
    ]
    out = pd.publish_once()
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
    out = pd.publish_once()
    assert out.reclaimed == 0
    assert not _critical_fired(stub)


def test_sweep_fetch_failure_is_logged_not_fatal(stub):
    # A sweep fetch failure must NOT wedge the cycle — log ERROR and let the pull proceed.
    stub["stuck"].side_effect = pd.portal_client.PortalTransportError("boom")
    stub["pending"].return_value = []
    out = pd.publish_once()
    assert out.reclaimed == 0
    stub["pending"].assert_called_once()  # the cycle continued past the sweep to the pull
    assert any(c.args and c.args[0] == pd.Severity.ERROR for c in stub["log"].call_args_list)


# ── _unstrand_if_needed (idle self-heal: recover a stranded tree at the top of a cycle) ──


def test_unstrand_recovers_a_stray_branch(mocker):
    """On a leftover publish/req-* branch (idle-stranded), recover via _reset_to_main."""
    mocker.patch.object(pd, "_git", return_value="publish/req-7-incident\n")
    reset = mocker.patch.object(pd, "_reset_to_main")
    pd._unstrand_if_needed()
    reset.assert_called_once()


def test_unstrand_is_a_noop_on_main(mocker):
    """The common idle case: already on main → no reset, no network pull (cheap rev-parse)."""
    mocker.patch.object(pd, "_git", return_value="main\n")
    reset = mocker.patch.object(pd, "_reset_to_main")
    pd._unstrand_if_needed()
    reset.assert_not_called()


def test_publish_once_unstrands_before_actuating(stub, mocker):
    """publish_once calls the idle self-heal at the top of every cycle (even with no rows)."""
    stub["pending"].return_value = []
    pd.publish_once()
    stub["unstrand"].assert_called_once()


def test_publish_once_halts_loud_when_unstrand_fails(stub):
    """A recovery failure halts the cycle + logs ERROR — never silently actuates from a
    stranded tree."""
    stub["unstrand"].side_effect = RuntimeError("git checkout main failed")
    out = pd.publish_once()
    assert out.halted == "unstrand_failed"
    stub["pending"].assert_not_called()
    assert any(c.args and c.args[0] == pd.Severity.ERROR for c in stub["log"].call_args_list)


# ── _wait_for_ci (the synchronous CI gate that replaced `gh pr merge --auto`) ────


def test_wait_for_ci_returns_when_clean(mocker):
    mocker.patch.object(pd, "_gh", return_value=json.dumps({"mergeStateStatus": "CLEAN", "statusCheckRollup": []}))
    pd._wait_for_ci("publish/req-1-jha")  # returns without raising


def test_wait_for_ci_raises_on_a_failed_check(mocker):
    mocker.patch.object(pd, "_gh", return_value=json.dumps({
        "mergeStateStatus": "BLOCKED",
        "statusCheckRollup": [{"name": "portal", "conclusion": "FAILURE"}],
    }))
    with pytest.raises(RuntimeError, match="CI failed"):
        pd._wait_for_ci("publish/req-1-jha")


def test_wait_for_ci_dedupes_and_surfaces_detail(mocker):
    """D2: a single failing job double-fires (push + pull_request) → the reason de-dupes by
    NAME (no 'portal, portal'), and each failing check carries its real log excerpt rather
    than a bare job name."""
    rollup = {
        "mergeStateStatus": "BLOCKED",
        "statusCheckRollup": [
            {"name": "test", "conclusion": "FAILURE", "detailsUrl": "https://x/actions/runs/1/job/111"},
            {"name": "test", "conclusion": "FAILURE", "detailsUrl": "https://x/actions/runs/2/job/222"},
            {"name": "portal", "conclusion": "FAILURE", "detailsUrl": "https://x/actions/runs/1/job/333"},
            {"name": "portal", "conclusion": "FAILURE", "detailsUrl": "https://x/actions/runs/2/job/444"},
        ],
    }
    fail_log = "test\tTests\t2026-06-09T05:15:55Z AssertionError: expected 11 to be 10\n"

    def fake_gh(*a):
        if a[:2] == ("pr", "view"):
            return json.dumps(rollup)
        if a[:2] == ("run", "view"):
            return fail_log
        return ""

    mocker.patch.object(pd, "_gh", side_effect=fake_gh)
    with pytest.raises(RuntimeError) as exc:
        pd._wait_for_ci("publish/req-1-jha")
    msg = str(exc.value)
    assert msg.count("test:") == 1 and msg.count("portal:") == 1  # de-duped by name
    assert "expected 11 to be 10" in msg  # the real reason, not a bare job name


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
    mocker.patch.object(pd, "_gh", side_effect=lambda *a: views.pop(0) if a[:2] == ("pr", "view") else "")
    mocker.patch.object(pd.time, "sleep")
    pd._wait_for_ci("publish/req-6-erosion-inspection")  # returns without raising


def test_wait_for_ci_still_raises_on_a_cancellation_with_no_successor(mocker):
    """Fail CLOSED: an operator cancelling the run (or a whole workflow cancelled outright)
    has no live or succeeding run of the same name, and must NOT read as green."""
    mocker.patch.object(pd, "_gh", return_value=json.dumps({
        "mergeStateStatus": "BLOCKED",
        "statusCheckRollup": [{"name": "test", "status": "COMPLETED", "conclusion": "CANCELLED"}],
    }))
    with pytest.raises(RuntimeError, match="CI failed"):
        pd._wait_for_ci("publish/req-1-jha")


def test_wait_for_ci_raises_on_a_real_failure_beside_a_superseded_cancellation(mocker):
    """The guard is narrow: neutralising a superseded CANCELLED must not mask a genuine
    FAILURE reported by the live run of that same job."""
    mocker.patch.object(pd, "_gh", return_value=json.dumps({
        "mergeStateStatus": "BLOCKED",
        "statusCheckRollup": [
            {"name": "test", "status": "COMPLETED", "conclusion": "CANCELLED"},
            {"name": "test", "status": "COMPLETED", "conclusion": "FAILURE"},
        ],
    }))
    with pytest.raises(RuntimeError, match="CI failed"):
        pd._wait_for_ci("publish/req-1-jha")


def test_wait_for_ci_updates_a_behind_branch_then_merges(mocker):
    views = [
        json.dumps({"mergeStateStatus": "BEHIND", "statusCheckRollup": []}),
        json.dumps({"mergeStateStatus": "CLEAN", "statusCheckRollup": []}),
    ]
    gh = mocker.patch.object(pd, "_gh", side_effect=lambda *a: views.pop(0) if a[:2] == ("pr", "view") else "")
    mocker.patch.object(pd.time, "sleep")
    pd._wait_for_ci("publish/req-1-jha")
    assert any(c.args[:2] == ("pr", "update-branch") for c in gh.call_args_list)


# ── _apply_to_worktree serialisation (diff-churn guard) ──────────────────────────


def test_apply_to_worktree_writes_literal_utf8_not_escapes(mocker, tmp_path):
    """The committed catalog/form files hold literal UTF-8 (em-dashes, curly quotes). A
    daemon write must not re-escape them: ensure_ascii=True turned every publish diff into
    dozens of \\uXXXX churn lines that buried the real change, and it ping-ponged because
    human edits put the literal characters back."""
    catalog = tmp_path / "catalog.json"
    forms = tmp_path / "forms"
    forms.mkdir()
    mocker.patch.object(pd, "_CATALOG_PATH", catalog)
    mocker.patch.object(pd, "_FORMS_DIR", forms)

    pd._apply_to_worktree(
        {"manifest_version": 1, "parents": [{"label": "Erosion \u2014 Inspection"}]},
        {"erosion-inspection-v1": {"form_name": "Erosion \u2014 Inspection", "quote": "\u201cok\u201d"}},
    )

    for written in (catalog, forms / "erosion-inspection-v1.json"):
        raw = written.read_text(encoding="utf-8")
        assert "\\u" not in raw, f"{written.name} re-escaped non-ASCII"
        assert "\u2014" in raw, f"{written.name} lost the literal em-dash"


def test_apply_to_worktree_write_is_idempotent_against_the_committed_catalog():
    """A daemon write of the LIVE manifest, unchanged, must reproduce the file on disk
    byte-for-byte — otherwise every publish carries a reformat the operator has to read
    past. Asserts round-trip equality, never the catalog's CONTENT (which the publish
    pipeline edits; pinning it would strand the next publish PR)."""
    import json as _json
    from pathlib import Path as _Path
    path = _Path(__file__).resolve().parents[1] / "safety_portal" / "catalog.json"
    on_disk = path.read_text(encoding="utf-8")
    rewritten = _json.dumps(_json.loads(on_disk), indent=2, ensure_ascii=False) + "\n"
    assert rewritten == on_disk, "a no-op daemon write would reformat catalog.json"


# ---------------------------------------------------------------------------
# Orphan actuator-branch cleanup (failed publishes used to strand a branch + PR)
# ---------------------------------------------------------------------------


def _ls_remote(*branches: str) -> str:
    """A `git ls-remote --heads` stdout block for the given branches."""
    return "\n".join(f"0000000000000000000000000000000000000000\trefs/heads/{b}" for b in branches)


@pytest.mark.parametrize(
    "branch,expected",
    [
        ("publish/req-5-erosion-inspection", 5),
        ("publish/req-12-a-b-c", 12),
        # Everything below must be UNMATCHED: this predicate gates a DELETE, so anything that is
        # not exactly `publish/req-<digits>-` has to be left alone rather than guessed at.
        ("publish/req-x-thing", None),          # non-numeric id
        ("publish/reqs-5-thing", None),         # near-miss prefix
        ("publish/req-5", None),                # no trailing separator
        ("feat/publish/req-5-thing", None),     # not anchored at the start
        ("publish/hotfix-by-hand", None),       # operator branch under our own prefix
        ("config/req-1-po_materials-purchaser", None),  # the OTHER daemon's branch
        ("main", None),
    ],
)
def test_branch_request_id_is_strict(branch, expected):
    assert pd._branch_request_id(branch) == expected


def test_close_and_delete_refuses_a_merged_pr(mocker):
    """A merged PR's branch belongs to the success path. Deleting on a mistaken premise is the
    destructive error worth refusing loudly, so nothing is removed and the answer is False."""
    mocker.patch.object(
        pd, "_gh",
        return_value=json.dumps([{"number": 181, "state": "MERGED",
                                  "mergedAt": "2026-08-19T17:18:51Z"}]),
    )
    refs = mocker.patch.object(pd, "_delete_branch_refs")
    assert pd._close_and_delete_branch("publish/req-7-erosion", "why") is False
    refs.assert_not_called()


def test_close_and_delete_comments_before_closing(mocker):
    """The reason must land on the PR BEFORE it closes — a bare closure is exactly what #178/#179
    left behind, and a closed PR has nowhere else to carry the explanation."""
    calls: list[tuple] = []

    def fake_gh(*args):
        calls.append(args)
        if args[:2] == ("pr", "list"):
            return json.dumps([{"number": 178, "state": "OPEN", "mergedAt": None}])
        return ""

    mocker.patch.object(pd, "_gh", side_effect=fake_gh)
    mocker.patch.object(pd, "_delete_branch_refs")
    mocker.patch.object(pd, "_git", return_value="")  # ls-remote: ref is gone
    assert pd._close_and_delete_branch("publish/req-5-erosion", "because X") is True
    verbs = [c[:2] for c in calls]
    assert verbs.index(("pr", "comment")) < verbs.index(("pr", "close"))


def test_close_and_delete_reports_failure_when_ref_survives(mocker):
    """Truth comes from re-reading the REMOTE, never from a delete command's exit status."""
    mocker.patch.object(pd, "_gh", return_value="[]")
    mocker.patch.object(pd, "_delete_branch_refs")
    mocker.patch.object(pd, "_git", return_value=_ls_remote("publish/req-5-erosion"))
    assert pd._close_and_delete_branch("publish/req-5-erosion", "r") is False


def test_close_and_delete_handles_a_branch_with_no_pr(mocker):
    """The 40-day-old `config/req-1` orphan had NO PR at all (the push landed, `pr create` never
    did). The publish lane must handle that same shape: delete the ref, no PR ops."""
    mocker.patch.object(pd, "_gh", return_value="[]")
    refs = mocker.patch.object(pd, "_delete_branch_refs")
    mocker.patch.object(pd, "_git", return_value="")
    assert pd._close_and_delete_branch("publish/req-9-orphan", "r") is True
    refs.assert_called_once_with("publish/req-9-orphan")


@pytest.fixture
def sweep_env(mocker):
    """A sweep with one candidate branch, old enough and not in flight — i.e. a true orphan."""
    creds = pd._Creds("https://portal.test", "tok")
    mocker.patch.object(pd.portal_client, "get_publish_pending", return_value=[])
    mocker.patch.object(pd.portal_client, "get_publish_stuck", return_value=[])
    mocker.patch.object(pd, "_git", return_value=_ls_remote("publish/req-5-erosion"))
    mocker.patch.object(pd, "_branch_tip_epoch",
                        return_value=time.time() - (pd.ORPHAN_MIN_AGE_S * 10))
    close = mocker.patch.object(pd, "_close_and_delete_branch", return_value=True)
    return {"creds": creds, "close": close}


def test_sweep_deletes_a_true_orphan(sweep_env):
    stats = pd.PublishStats()
    pd._sweep_orphaned_branches(sweep_env["creds"], stats)
    sweep_env["close"].assert_called_once()
    assert sweep_env["close"].call_args[0][0] == "publish/req-5-erosion"
    assert stats.orphans_cleaned == 1


def test_sweep_skips_a_request_still_in_flight(sweep_env, mocker):
    """The authoritative guard: an id the Worker still reports as non-terminal is live work."""
    mocker.patch.object(pd.portal_client, "get_publish_stuck", return_value=[{"id": 5}])
    stats = pd.PublishStats()
    pd._sweep_orphaned_branches(sweep_env["creds"], stats)
    sweep_env["close"].assert_not_called()
    assert stats.orphans_cleaned == 0


def test_sweep_skips_a_branch_younger_than_the_floor(sweep_env, mocker):
    """Second belt: even with the in-flight read saying 'terminal', a fresh branch is not debris."""
    mocker.patch.object(pd, "_branch_tip_epoch", return_value=time.time() - 5)
    stats = pd.PublishStats()
    pd._sweep_orphaned_branches(sweep_env["creds"], stats)
    sweep_env["close"].assert_not_called()


def test_sweep_skips_a_branch_of_unknown_age(sweep_env, mocker):
    """Unknown age is fail-CLOSED: under-clean rather than over-delete."""
    mocker.patch.object(pd, "_branch_tip_epoch", return_value=None)
    stats = pd.PublishStats()
    pd._sweep_orphaned_branches(sweep_env["creds"], stats)
    sweep_env["close"].assert_not_called()


def test_sweep_deletes_nothing_when_the_in_flight_read_fails(sweep_env, mocker):
    """If we cannot establish what is live, we must delete NOTHING — not fall back to 'probably
    terminal'. This is the difference between a housekeeping bug and destroying live work."""
    mocker.patch.object(pd.portal_client, "get_publish_stuck",
                        side_effect=RuntimeError("worker down"))
    stats = pd.PublishStats()
    pd._sweep_orphaned_branches(sweep_env["creds"], stats)
    sweep_env["close"].assert_not_called()


def test_sweep_ignores_branches_that_are_not_ours(sweep_env, mocker):
    mocker.patch.object(
        pd, "_git",
        return_value=_ls_remote("publish/hand-made", "feat/publish/req-1-x", "main"),
    )
    stats = pd.PublishStats()
    pd._sweep_orphaned_branches(sweep_env["creds"], stats)
    sweep_env["close"].assert_not_called()


def test_stage2_records_the_failure_before_cleaning_up(stub, mocker):
    """Ordering is the whole contract: `_fail` stamps the row and raises the CRITICAL, and only
    then is the branch removed. Reversed, a cleanup crash would erase the evidence of why the
    publish failed and leave no audit trail at all."""
    order: list[str] = []
    stub["commit"].side_effect = RuntimeError("CI failed for publish/req-5-erosion")
    mocker.patch.object(pd, "_fail", side_effect=lambda *a, **k: order.append("fail"))
    mocker.patch.object(pd, "_close_and_delete_branch",
                        side_effect=lambda *a, **k: order.append("cleanup"))
    stats = pd.PublishStats()
    pd._actuate(
        pd._Creds("https://portal.test", "tok"),
        _row("create", "incident", "incident", definition=_create_def(), rid=5),
        stats,
    )
    assert order == ["fail", "cleanup"]
    assert stats.failed == 1


def test_stage2_cleanup_targets_the_same_branch_commit_created(stub, mocker):
    """The delete must aim at the branch `_commit_test_merge` actually made — one `_branch_name`
    definition is what guarantees it, so assert the name the cleanup receives."""
    stub["commit"].side_effect = RuntimeError("CI red")
    mocker.patch.object(pd, "_fail")
    close = mocker.patch.object(pd, "_close_and_delete_branch")
    pd._actuate(
        pd._Creds("https://portal.test", "tok"),
        _row("create", "incident", "incident", definition=_create_def(), rid=42),
        pd.PublishStats(),
    )
    assert close.call_args[0][0] == pd._branch_name(42, "incident")


def test_sweep_leaves_a_default_visible_record_of_what_it_deleted(sweep_env, mocker):
    """A ref delete is DESTRUCTIVE and INFO rows are env-gated (default off), so a successful
    sweep must still land a WARN naming what went. The real production orphan had NO PR, so the
    GitHub comment could not be the audit trail for it."""
    logged = mocker.patch.object(pd.actuator_branches.error_log, "log")
    pd._sweep_orphaned_branches(sweep_env["creds"], pd.PublishStats())
    codes = [c.kwargs.get("error_code") for c in logged.call_args_list]
    assert "publish_daemon.orphan_sweep_removed" in codes
    said = [c for c in logged.call_args_list
            if c.kwargs.get("error_code") == "publish_daemon.orphan_sweep_removed"]
    assert "publish/req-5-erosion" in said[0].args[2]
    assert said[0].args[0] is pd.Severity.WARN


def test_sweep_stays_quiet_when_there_is_nothing_to_clean(sweep_env, mocker):
    """The normal case is an empty branch list; it must not emit a per-cycle line."""
    mocker.patch.object(pd, "_git", return_value="")
    logged = mocker.patch.object(pd.actuator_branches.error_log, "log")
    pd._sweep_orphaned_branches(sweep_env["creds"], pd.PublishStats())
    codes = [c.kwargs.get("error_code") for c in logged.call_args_list]
    assert "publish_daemon.orphan_sweep_removed" not in codes


# `branch_tip_epoch` is what the age floor RESTS on, and every sweep test above mocks it — so
# without these it would ship untested (the "mocked the layer under test" trap). Each bad-input
# case must yield None, because None is what makes the caller SKIP rather than delete.
def test_branch_tip_epoch_parses_a_real_github_timestamp(mocker):
    """branch_tip_epoch parses GitHub's ISO-8601 Z form to a UTC epoch."""
    mocker.patch.object(pd, "_gh", return_value="2026-07-10T16:16:18Z\n")
    got = pd._branch_tip_epoch("publish/req-5-x")
    assert got == datetime(2026, 7, 10, 16, 16, 18, tzinfo=UTC).timestamp()


@pytest.mark.parametrize(
    "gh_behaviour",
    [
        pytest.param({"side_effect": RuntimeError("gh exploded")}, id="gh-raises"),
        pytest.param({"return_value": ""}, id="empty-output"),
        pytest.param({"return_value": "   "}, id="whitespace-only"),
        pytest.param({"return_value": "not-a-date"}, id="unparseable"),
        pytest.param({"return_value": "2026-13-45T99:99:99Z"}, id="out-of-range"),
    ],
)
def test_branch_tip_epoch_is_fail_closed_on_bad_input(mocker, gh_behaviour):
    """Unknown age must be None — the caller then SKIPS the branch instead of deleting it."""
    mocker.patch.object(pd, "_gh", **gh_behaviour)
    assert pd._branch_tip_epoch("publish/req-5-x") is None


def test_sweep_survives_a_failure_to_list_remote_branches(sweep_env, mocker):
    """Housekeeping must never wedge the cycle, and must not delete on a blind read."""
    mocker.patch.object(pd, "_git", side_effect=RuntimeError("network"))
    stats = pd.PublishStats()
    pd._sweep_orphaned_branches(sweep_env["creds"], stats)
    sweep_env["close"].assert_not_called()
    assert stats.orphans_cleaned == 0


def test_close_and_delete_still_removes_the_ref_when_pr_lookup_fails(mocker):
    """A `gh pr list` failure must not strand the branch — fall through to a ref-only delete."""
    mocker.patch.object(pd, "_gh", side_effect=RuntimeError("gh down"))
    refs = mocker.patch.object(pd, "_delete_branch_refs")
    mocker.patch.object(pd, "_git", return_value="")
    assert pd._close_and_delete_branch("publish/req-5-x", "r") is True
    refs.assert_called_once_with("publish/req-5-x")
