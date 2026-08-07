"""Unit tests for scripts/verify_cutover.py — pass/fail plumbing + check units.

NO live calls: every Smartsheet / Keychain / subprocess touchpoint is
monkeypatched. The live gate run is operator-executed at cutover (§53); these
tests lock the harness contract (exit codes, --only/--skip, failure isolation)
and the per-check decision logic.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# scripts/ is not a Python package; use the same sys.path-insert idiom as
# tests/test_check_doctrine_drift.py so the module imports as the top-level
# `verify_cutover` (a `from scripts import …` would make mypy see the file
# under two module names — "found twice").
SCRIPTS_DIR = REPO / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import verify_cutover as vc  # noqa: E402  — sys.path-driven import

OPTS = vc.Options()


def _spec(check_id: str, slug: str, outcome: vc.CheckOutcome) -> vc.CheckSpec:
    return vc.CheckSpec(check_id, slug, f"fake {slug}", lambda opts: outcome)


PASS = vc.CheckOutcome(passed=True, summary="ok")
FAIL = vc.CheckOutcome(passed=False, summary="bad", details="why it failed")


# ---- harness: exit codes, selection, isolation ---------------------------


def test_all_pass_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        vc, "CHECKS", (_spec("VC-01", "alpha", PASS), _spec("VC-02", "beta", PASS))
    )
    assert vc.main([]) == 0
    out = capsys.readouterr().out
    assert "[PASS] VC-01 alpha" in out
    assert "2 passed, 0 failed, 0 skipped" in out


def test_one_failure_exits_one_and_prints_details(monkeypatch, capsys):
    monkeypatch.setattr(
        vc, "CHECKS", (_spec("VC-01", "alpha", PASS), _spec("VC-02", "beta", FAIL))
    )
    assert vc.main([]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] VC-02 beta" in out
    assert "why it failed" in out
    assert "1 passed, 1 failed" in out


def test_only_selects_by_slug_and_flags_partial(monkeypatch, capsys):
    monkeypatch.setattr(
        vc, "CHECKS", (_spec("VC-01", "alpha", PASS), _spec("VC-02", "beta", FAIL))
    )
    assert vc.main(["--only", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "PARTIAL RUN" in out
    assert "beta" not in out.split("PARTIAL RUN")[1].split("verify_cutover:")[0]
    assert "1 skipped" in out


def test_skip_selects_by_check_id(monkeypatch, capsys):
    monkeypatch.setattr(
        vc, "CHECKS", (_spec("VC-01", "alpha", FAIL), _spec("VC-02", "beta", PASS))
    )
    assert vc.main(["--skip", "VC-01"]) == 0
    assert "1 skipped" in capsys.readouterr().out


def test_unknown_handle_exits_two(monkeypatch, capsys):
    monkeypatch.setattr(vc, "CHECKS", (_spec("VC-01", "alpha", PASS),))
    assert vc.main(["--only", "nope"]) == 2
    assert "unknown check" in capsys.readouterr().err


def test_check_exception_is_isolated_as_fail(monkeypatch, capsys):
    def boom(opts: vc.Options) -> vc.CheckOutcome:
        raise RuntimeError("sheet unreachable")

    monkeypatch.setattr(
        vc,
        "CHECKS",
        (vc.CheckSpec("VC-01", "alpha", "boom", boom), _spec("VC-02", "beta", PASS)),
    )
    assert vc.main([]) == 1
    out = capsys.readouterr().out
    assert "[FAIL] VC-01 alpha" in out
    assert "RuntimeError" in out
    assert "[PASS] VC-02 beta" in out  # later checks still ran


def test_list_mode(capsys):
    assert vc.main(["--list"]) == 0
    out = capsys.readouterr().out
    for spec in vc.CHECKS:
        assert spec.check_id in out
        assert spec.slug in out


# ---- VC-01 keychain -------------------------------------------------------


def test_keychain_all_present(monkeypatch):
    monkeypatch.setattr(vc.keychain, "get_secret", lambda name: "x" * 12)
    outcome = vc._check_keychain(OPTS)
    assert outcome.passed
    assert f"{len(vc.REQUIRED_SECRETS)}/{len(vc.REQUIRED_SECRETS)}" in outcome.summary


def test_keychain_missing_named_but_value_never_leaked(monkeypatch):
    def fake(name: str) -> str:
        if name == "ITS_PORTAL_PO_TOKEN":
            raise vc.keychain.KeychainError("not found")
        return "s3cret-value"

    monkeypatch.setattr(vc.keychain, "get_secret", fake)
    outcome = vc._check_keychain(OPTS)
    assert not outcome.passed
    assert "ITS_PORTAL_PO_TOKEN" in outcome.details
    assert "s3cret-value" not in outcome.summary + outcome.details  # §54


# ---- VC-02 launchd --------------------------------------------------------


def _fake_launchctl(labels: set[str]) -> str:
    return "\n".join(f"123\t0\t{label}" for label in sorted(labels))


def test_launchd_exact_match_passes(monkeypatch):
    expected = vc._expected_labels()
    assert expected, "repo should ship org.solutionsmith.its.*.plist files"
    monkeypatch.setattr(vc, "_launchctl_list", lambda: _fake_launchctl(expected))
    assert vc._check_launchd(OPTS).passed


def test_launchd_missing_and_orphan_fail(monkeypatch):
    expected = sorted(vc._expected_labels())
    loaded = set(expected[1:]) | {"org.solutionsmith.its.ghost"}
    monkeypatch.setattr(vc, "_launchctl_list", lambda: _fake_launchctl(loaded))
    outcome = vc._check_launchd(OPTS)
    assert not outcome.passed
    assert expected[0] in outcome.details
    assert "ghost" in outcome.details


def test_launchd_dark_unloaded_send_daemon_excluded_from_expected():
    """po-send is a dark-unloaded SEND daemon (operator decision 2026-07-12) — shipped as
    a plist but NOT required loaded at cutover (send-gate defense-in-depth)."""
    assert "org.solutionsmith.its.po-send" in vc.DARK_UNLOADED_LABELS
    assert "org.solutionsmith.its.po-send" not in vc._expected_labels()


def test_launchd_dark_send_daemon_loaded_is_send_gate_violation(monkeypatch):
    """If po-send IS loaded at cutover, VC-02 FAILS — a dark external-send path running
    is a high-class External-Send-Gate event, distinct from a plain orphan."""
    loaded = vc._expected_labels() | {"org.solutionsmith.its.po-send"}
    monkeypatch.setattr(vc, "_launchctl_list", lambda: _fake_launchctl(loaded))
    outcome = vc._check_launchd(OPTS)
    assert not outcome.passed
    assert "send-gate violation" in outcome.details
    assert "po-send" in outcome.details


# ---- VC-03 config ---------------------------------------------------------


def _config_values(overrides: dict[str, str | None]) -> object:
    def fake(key: str, *, workstream: str) -> str | None:
        if key in overrides:
            value = overrides[key]
            if value == "MISSING":
                raise vc.SmartsheetNotFoundError(key)
            return value
        row = next(r for r in vc.CONFIG_ROWS if r.key == key)
        return "true" if row.requirement == "true" else "https://portal.example.com"

    return fake


def test_config_all_good_passes(monkeypatch):
    monkeypatch.setattr(vc.smartsheet_client, "get_setting", _config_values({}))
    assert vc._check_config(OPTS).passed


def test_config_missing_row_and_false_gate_fail(monkeypatch):
    monkeypatch.setattr(
        vc.smartsheet_client,
        "get_setting",
        _config_values(
            {
                "safety_reports.weekly_send.from_mailbox": "MISSING",
                "field_ops.fieldops_sync.sync_enabled": "false",
            }
        ),
    )
    outcome = vc._check_config(OPTS)
    assert not outcome.passed
    assert "row MISSING" in outcome.details
    assert "field_ops.fieldops_sync.sync_enabled" in outcome.details


def test_config_sandbox_value_fails_unless_allowed(monkeypatch):
    monkeypatch.setattr(
        vc.smartsheet_client,
        "get_setting",
        _config_values(
            {"safety_reports.portal.worker_base_url": "https://safety.evergreenmirror.com"}
        ),
    )
    assert not vc._check_config(OPTS).passed
    assert vc._check_config(vc.Options(allow_sandbox=True)).passed


# ---- PO / worker_base_url enrollment (po_send landed, PR #500) -----------------------


def test_po_send_from_mailbox_enrolled_and_sandbox_scanned():
    """po_send landed → its FROM address must be a production-swept, sandbox-scanned row."""
    row = next(
        (r for r in vc.CONFIG_ROWS if r.key == "po_materials.po_send.from_mailbox"),
        None,
    )
    assert row is not None, "po_materials.po_send.from_mailbox must be enrolled in CONFIG_ROWS"
    assert row.workstream == "po_materials"
    assert row.requirement == "non_empty"
    assert row.sandbox_scan is True


def test_all_three_worker_base_url_copies_enrolled_and_scanned():
    """worker_base_url is one Setting name under 3 Workstream cells = 3 physical rows; every copy
    must be sandbox-scanned (previously only the safety_reports copy was)."""
    copies = {
        r.workstream
        for r in vc.CONFIG_ROWS
        if r.key == "safety_reports.portal.worker_base_url"
    }
    assert copies == {"safety_reports", "progress_reports", "po_materials"}, copies
    for r in vc.CONFIG_ROWS:
        if r.key == "safety_reports.portal.worker_base_url":
            assert r.sandbox_scan is True, f"{r.workstream} copy must be sandbox-scanned"


def test_po_send_polling_gate_not_enrolled():
    """Enrolling po_send.polling_enabled='true' would DEMAND a send-enable at cutover — a
    high-class External-Send-Gate decision (Seth). It must stay OUT until PO send is in scope."""
    keys = {r.key for r in vc.CONFIG_ROWS}
    assert "po_materials.po_send.polling_enabled" not in keys
    assert "po_materials.po_send.scheduled_send_local" not in keys


def test_operator_email_enrolled_and_sandbox_scanned():
    """CO-3: system.operator_email (the last-resort Resend page recipient) must be a
    production, sandbox-scanned global row — a mirror residue fails the cutover gate."""
    row = next(
        (r for r in vc.CONFIG_ROWS if r.key == "system.operator_email"),
        None,
    )
    assert row is not None, "system.operator_email must be enrolled in CONFIG_ROWS"
    assert row.workstream == "global"
    assert row.requirement == "non_empty"
    assert row.sandbox_scan is True


def test_operator_email_mirror_value_fails_unless_allowed(monkeypatch):
    """A mirror seths@evergreenmirror.com residue on the operator email fails the
    production gate but passes the --allow-sandbox dress rehearsal."""
    monkeypatch.setattr(
        vc.smartsheet_client,
        "get_setting",
        _config_values({"system.operator_email": "seths@evergreenmirror.com"}),
    )
    assert not vc._check_config(OPTS).passed
    assert vc._check_config(vc.Options(allow_sandbox=True)).passed


def test_subcontract_gate_rows_enrolled_present_not_forced_true():
    """Subcontracts scoped fully-in (2026-07-12). The three subcontract_poll gate rows are
    asserted SEEDED PRESENT (non_empty — the dark-ship reflex), never forced 'true' (that
    would demand the dark daemon go live). subcontract_poll reuses the safety_reports
    worker_base_url row, so no new worker_base_url copy is enrolled."""
    by_key = {r.key: r for r in vc.CONFIG_ROWS}
    for gate in (
        "subcontracts.subcontract_poll.polling_enabled",
        "subcontracts.subcontract_poll.subcontractors_sync_enabled",
        "subcontracts.subcontract_poll.status_sync_enabled",
    ):
        assert gate in by_key, f"{gate} must be enrolled in CONFIG_ROWS"
        assert by_key[gate].workstream == "subcontracts"
        assert by_key[gate].requirement == "non_empty", f"{gate} must be non_empty, not forced-true"


def test_subcontract_send_rows_enrolled_after_sc_s4():
    """The SC-S4 subcontract SEND half is BUILT (2026-07-15, ships dark) — its config rows are
    now enrolled: from_mailbox sandbox-scanned; the gate + window asserted SEEDED PRESENT
    (non_empty, NOT forced 'true' — flipping the send gate is a FIXED high-class
    External-Send-Gate decision, so it is never demanded 'true' by VC-03)."""
    by_key = {r.key: r for r in vc.CONFIG_ROWS}
    assert "subcontracts.subcontract_send.from_mailbox" in by_key
    assert by_key["subcontracts.subcontract_send.from_mailbox"].sandbox_scan is True
    # The gate + window are present-checked, NOT forced true.
    assert by_key["subcontracts.subcontract_send.polling_enabled"].requirement == "non_empty"
    assert by_key["subcontracts.subcontract_send.scheduled_send_local"].requirement == "non_empty"


def test_rfq_send_dark_unloaded_and_rows_enrolled_after_r3():
    """The ADR-0004 R3 RFQ SEND half is BUILT (ships dark): its plist is a dark-unloaded
    send daemon (excluded from the expected-loaded set, like po-send), and its config rows
    are enrolled — from_mailbox sandbox-scanned; the gate + window asserted SEEDED PRESENT
    (non_empty, NOT forced 'true' — flipping the RFQ send gate is a FIXED high-class
    External-Send-Gate decision, never demanded 'true' by VC-03)."""
    assert "org.solutionsmith.its.rfq-send" in vc.DARK_UNLOADED_LABELS
    assert "org.solutionsmith.its.rfq-send" not in vc._expected_labels()
    by_key = {r.key: r for r in vc.CONFIG_ROWS}
    assert "po_materials.rfq_send.from_mailbox" in by_key
    assert by_key["po_materials.rfq_send.from_mailbox"].sandbox_scan is True
    assert by_key["po_materials.rfq_send.polling_enabled"].requirement == "non_empty"
    assert by_key["po_materials.rfq_send.scheduled_send_local"].requirement == "non_empty"


def test_po_from_mailbox_mirror_value_fails_unless_allowed(monkeypatch):
    """A mirror procurement@evergreenmirror.com residue on the PO FROM address fails the
    production gate but passes the --allow-sandbox dress rehearsal (the enrollment's teeth)."""
    monkeypatch.setattr(
        vc.smartsheet_client,
        "get_setting",
        _config_values(
            {"po_materials.po_send.from_mailbox": "procurement@evergreenmirror.com"}
        ),
    )
    assert not vc._check_config(OPTS).passed
    assert vc._check_config(vc.Options(allow_sandbox=True)).passed


# ---- --profile phase1-hybrid (cutover checklist §3.5) ----------------------


def _phase1_opts() -> vc.Options:
    return vc.Options(
        profile="phase1-hybrid", sandbox_exempt=vc.PROFILES["phase1-hybrid"]
    )


def test_phase1_hybrid_profile_names_exactly_the_three_worker_base_url_rows():
    """The exemption set is data (checklist §3.5): exactly the three physical
    worker_base_url rows — the portal deliberately stays on the mirror Worker this
    phase. Nothing else (in particular no from_mailbox row) is exempted."""
    assert vc.PROFILES["phase1-hybrid"] == frozenset({
        ("safety_reports.portal.worker_base_url", "safety_reports"),
        ("safety_reports.portal.worker_base_url", "progress_reports"),
        ("safety_reports.portal.worker_base_url", "po_materials"),
    })


def test_phase1_hybrid_allows_mirror_worker_base_url_and_names_the_exemption(monkeypatch):
    """Under the profile, mirror worker_base_url values pass — and the PASS summary
    names each exempted row (the exemption is observable, never silent)."""
    monkeypatch.setattr(
        vc.smartsheet_client,
        "get_setting",
        _config_values(
            {"safety_reports.portal.worker_base_url": "https://safety.evergreenmirror.com"}
        ),
    )
    assert not vc._check_config(OPTS).passed  # no profile → still the hard gate
    outcome = vc._check_config(_phase1_opts())
    assert outcome.passed
    assert "phase1-hybrid" in outcome.summary
    assert "worker_base_url" in outcome.summary


def test_phase1_hybrid_still_scans_everything_else(monkeypatch):
    """The profile's teeth: a mirror residue on any NON-exempted row (here the safety
    from_mailbox) still fails the profile gate — a profile is not --allow-sandbox."""
    monkeypatch.setattr(
        vc.smartsheet_client,
        "get_setting",
        _config_values(
            {
                "safety_reports.portal.worker_base_url": "https://safety.evergreenmirror.com",
                "safety_reports.weekly_send.from_mailbox": "safety@evergreenmirror.com",
            }
        ),
    )
    outcome = vc._check_config(_phase1_opts())
    assert not outcome.passed
    assert "from_mailbox" in outcome.details
    assert "worker_base_url" not in outcome.details


def test_profile_and_allow_sandbox_mutually_exclusive():
    """A profile gate must never be silently degraded to the blanket waiver."""
    with pytest.raises(SystemExit) as excinfo:
        vc.main(["--profile", "phase1-hybrid", "--allow-sandbox"])
    assert excinfo.value.code == 2


def test_unknown_profile_rejected():
    with pytest.raises(SystemExit) as excinfo:
        vc.main(["--profile", "phase99-nope"])
    assert excinfo.value.code == 2


def test_profile_banner_printed(monkeypatch, capsys):
    monkeypatch.setattr(vc, "CHECKS", (_spec("VC-01", "alpha", PASS),))
    assert vc.main(["--profile", "phase1-hybrid"]) == 0
    out = capsys.readouterr().out
    assert "--profile phase1-hybrid" in out
    assert "3 named row(s)" in out


# ---- VC-04 daemon-health --------------------------------------------------


def _health_row(name: str, *, enabled: bool, age_seconds: float | None, interval: float):
    heartbeat = (
        (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
        if age_seconds is not None
        else None
    )
    return {
        "Daemon Name": name,
        "Enabled": enabled,
        "Interval Seconds": interval,
        "Last Heartbeat": heartbeat,
    }


def test_daemon_health_fresh_rows_pass(monkeypatch):
    rows = [
        _health_row("safety_reports.portal_poll", enabled=True, age_seconds=30, interval=60),
        _health_row("scripts.watchdog", enabled=True, age_seconds=3600, interval=86400),
        _health_row("dark.daemon", enabled=False, age_seconds=10**7, interval=60),  # ignored
    ]
    monkeypatch.setattr(vc.smartsheet_client, "get_rows", lambda sheet_id: rows)
    outcome = vc._check_daemon_health(OPTS)
    assert outcome.passed
    assert "2 enabled" in outcome.summary


def test_daemon_health_stale_and_heartbeatless_fail(monkeypatch):
    rows = [
        _health_row("stale.daemon", enabled=True, age_seconds=500, interval=60),
        _health_row("silent.daemon", enabled=True, age_seconds=None, interval=60),
    ]
    monkeypatch.setattr(vc.smartsheet_client, "get_rows", lambda sheet_id: rows)
    outcome = vc._check_daemon_health(OPTS)
    assert not outcome.passed
    assert "stale.daemon" in outcome.details
    assert "silent.daemon" in outcome.details


def test_daemon_health_zero_enabled_rows_fail(monkeypatch):
    monkeypatch.setattr(vc.smartsheet_client, "get_rows", lambda sheet_id: [])
    assert not vc._check_daemon_health(OPTS).passed


# ---- VC-08 d1-migrations --------------------------------------------------


def _completed(rc: int, stdout: str, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["npx"], returncode=rc, stdout=stdout, stderr=stderr)


def test_d1_no_pending_passes(monkeypatch):
    monkeypatch.setattr(
        vc,
        "_run_wrangler_migrations_list",
        lambda: _completed(0, "✅ No migrations to apply!"),
    )
    assert vc._check_d1_migrations(OPTS).passed


def test_d1_pending_fails(monkeypatch):
    monkeypatch.setattr(
        vc,
        "_run_wrangler_migrations_list",
        lambda: _completed(0, "Migrations to be applied:\n0042_po_vendors.sql"),
    )
    outcome = vc._check_d1_migrations(OPTS)
    assert not outcome.passed
    assert "0042_po_vendors.sql" in outcome.details


def test_d1_transient_7403_retries_once_then_passes(monkeypatch):
    calls: list[int] = []

    def fake() -> subprocess.CompletedProcess[str]:
        calls.append(1)
        if len(calls) == 1:
            return _completed(1, "", "A request to the Cloudflare API failed. [code: 7403]")
        return _completed(0, "No migrations to apply")

    monkeypatch.setattr(vc, "_run_wrangler_migrations_list", fake)
    outcome = vc._check_d1_migrations(OPTS)
    assert outcome.passed
    assert len(calls) == 2


def test_d1_persistent_7403_fails_after_one_retry(monkeypatch):
    calls: list[int] = []

    def fake() -> subprocess.CompletedProcess[str]:
        calls.append(1)
        return _completed(1, "", "[code: 7403]")

    monkeypatch.setattr(vc, "_run_wrangler_migrations_list", fake)
    assert not vc._check_d1_migrations(OPTS).passed
    assert len(calls) == 2  # exactly one retry, not an infinite loop


# ---- VC-09 heartbeat-url --------------------------------------------------


def test_heartbeat_url_https_passes(monkeypatch):
    monkeypatch.setattr(
        vc.smartsheet_client,
        "get_setting",
        lambda key, *, workstream: "https://heartbeat.uptimerobot.com/abc",
    )
    assert vc._check_heartbeat_url(OPTS).passed


@pytest.mark.parametrize("value", ["", "http://insecure.example.com"])
def test_heartbeat_url_blank_or_http_fails(monkeypatch, value):
    monkeypatch.setattr(
        vc.smartsheet_client, "get_setting", lambda key, *, workstream: value
    )
    assert not vc._check_heartbeat_url(OPTS).passed


def test_heartbeat_url_missing_row_fails(monkeypatch):
    def fake(key: str, *, workstream: str) -> str:
        raise vc.SmartsheetNotFoundError(key)

    monkeypatch.setattr(vc.smartsheet_client, "get_setting", fake)
    outcome = vc._check_heartbeat_url(OPTS)
    assert not outcome.passed
    assert "MISSING" in outcome.summary


# ---- registry sanity ------------------------------------------------------


def test_check_ids_unique_and_sequential():
    ids = [spec.check_id for spec in vc.CHECKS]
    assert len(ids) == len(set(ids))
    assert ids == [f"VC-{i:02d}" for i in range(1, len(ids) + 1)]


def test_required_secrets_cover_program_list():
    # 11 non-Box + Box triplet + PO token + 4 dark-daemon bearers + operator PIN = 20
    # (docs/2026-07-09_aug7_delivery_program.md WS4; +3 per operator directive 2026-07-12;
    # + ITS_PORTAL_ESTIMATE_TOKEN with the ADR-0004 estimate lane, PR-A;
    # + ITS_PORTAL_RFQ_TOKEN with the ADR-0004 RFQ lane, PR-C — decision 4's
    # per-lane bearer separation).
    assert len(vc.NON_BOX_SECRETS) == 11
    assert set(vc.BOX_SECRETS) == {
        "ITS_BOX_CLIENT_ID",
        "ITS_BOX_CLIENT_SECRET",
        "ITS_BOX_REFRESH_TOKEN",
    }
    assert "ITS_PORTAL_PO_TOKEN" in vc.REQUIRED_SECRETS
    # 21 with PR3b's ITS_PORTAL_MANIFEST_TOKEN (its own per-lane bearer).
    assert len(vc.REQUIRED_SECRETS) == 21


def test_dark_daemon_bearers_and_operator_pin_enrolled():
    """Operator directive 2026-07-12: the config-actuator + subcontract-poll daemon
    bearers and the operator-dashboard PIN are cutover-required even though their
    consumers ship dark (same provision-even-while-dark rationale as ITS_PORTAL_PO_TOKEN).
    ITS_PORTAL_ESTIMATE_TOKEN joined at ADR-0004 PR-A (red-team #1 — the estimate
    lane's OWN bearer, deliberately separate from the RFQ token);
    ITS_PORTAL_RFQ_TOKEN joined at ADR-0004 PR-C (decision 4 — the RFQ lane's OWN
    bearer, scoping only /api/po/rfqs/internal/*)."""
    for name in (
        "ITS_PORTAL_CONFIG_TOKEN",
        "ITS_PORTAL_SUB_TOKEN",
        "ITS_PORTAL_ESTIMATE_TOKEN",
        "ITS_PORTAL_RFQ_TOKEN",
        "ITS_OPERATOR_PIN",
    ):
        assert name in vc.REQUIRED_SECRETS, f"{name} must be enrolled in REQUIRED_SECRETS"
