"""Class-A config editor (WS2 D1-2) — the security-critical ACT surface.

Prove-it-bites: denylisted keys refused, out-of-bounds values rejected with NO
write, send-poller first-activation escalated (not applied), pause applied,
audit row written on every write, PIN fail-closed, Origin allowlist enforced,
and the outcome render is escaped.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from operator_dashboard.act import config_write, registry
from operator_dashboard.act.config_write import apply_edit
from operator_dashboard.act.validators import (
    ConfigValidationError,
    v_bool,
    v_email_list,
    v_float01,
    v_int,
    v_schedule,
)
from operator_dashboard.app import create_app
from operator_dashboard.auth import OriginError, PinError, check_origin, verify_pin


@pytest.fixture(autouse=True)
def _reset_pin_throttle(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The PIN brute-force throttle is process-global; reset it around each test
    # and zero the per-failure sleep so tests stay fast + order-independent.
    from operator_dashboard import auth

    monkeypatch.setattr(auth, "_FAIL_SLEEP_SECONDS", 0.0)
    auth.reset_pin_throttle()
    yield
    auth.reset_pin_throttle()


@pytest.fixture
def fake_smartsheet(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch the shared read/write/audit surfaces the config editor lazily
    imports. `rows` maps (Setting, Workstream) -> row dict; `updates` records
    every update_rows payload; `audits` records every error_log.log call."""
    import shared.error_log as el
    import shared.smartsheet_client as ss

    state: dict[str, Any] = {"rows": {}, "updates": [], "audits": []}

    def get_rows(sheet_id: int, *, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if filters:
            key = (filters.get("Setting"), filters.get("Workstream"))
            row = state["rows"].get(key)
            return [row] if row else []
        return list(state["rows"].values())

    def update_rows(sheet_id: int, updates: list[dict[str, Any]]) -> None:
        state["updates"].extend(updates)

    def log(severity: Any, script: str, message: str, **kw: Any) -> None:
        state["audits"].append((str(severity), kw.get("error_code"), message))

    monkeypatch.setattr(ss, "get_rows", get_rows)
    monkeypatch.setattr(ss, "update_rows", update_rows)
    monkeypatch.setattr(el, "log", log)
    return state


def _seed(state: dict[str, Any], setting: str, ws: str, value: str, row_id: int = 1) -> None:
    state["rows"][(setting, ws)] = {
        "_row_id": row_id,
        "Setting": setting,
        "Workstream": ws,
        "Value": value,
    }


# ---------------------------------------------------------------- registry ----
def test_class_e_and_interval_keys_are_not_editable() -> None:
    # external_send_gate (Class E) is NEVER editable on any surface
    assert not registry.is_editable("safety_reports.external_send_gate", "safety_reports")
    # interval keys are install-time (never hot-reload) → deliberately not editable here
    assert not registry.is_editable("safety_reports.intake.poll_interval_seconds", "safety_reports")
    assert not registry.is_editable("po_materials.po_send.poll_interval_seconds", "po_materials")
    # and the fixed denylist never leaks into REGISTRY under any workstream
    for name in registry.NON_EDITABLE_SETTINGS:
        assert all(k[0] != name for k in registry.REGISTRY), f"{name} leaked into REGISTRY"


def test_system_state_and_config_actuator_are_class_b_elevated() -> None:
    # D1-3 moved these from non-editable to Class-B (elevated-confirm required)
    for setting, ws in (
        ("system.state", "global"),
        ("po_materials.config_actuator.polling_enabled", "po_materials"),
    ):
        entry = registry.get_entry(setting, ws)
        assert entry is not None and entry.tier == "B" and entry.elevated_confirm


def test_registry_is_keyed_on_pair_not_setting_name() -> None:
    # the footgun row is editable under safety_reports (intake's workstream) only
    assert registry.is_editable("progress_reports.intake_enabled", "safety_reports")
    assert not registry.is_editable("progress_reports.intake_enabled", "progress_reports")
    # worker_base_url exists 3x under different workstreams — each a distinct
    # Class-B (D1-3) editable pair; pair-keying is load-bearing
    for ws in ("safety_reports", "progress_reports", "po_materials"):
        entry = registry.get_entry("safety_reports.portal.worker_base_url", ws)
        assert entry is not None and entry.tier == "B"


# -------------------------------------------------------------- validators ----
def test_validators_reject_bad_values() -> None:
    assert v_bool("TRUE") == "true"
    with pytest.raises(ConfigValidationError):
        v_bool("maybe")
    assert v_int(1, 100)("7") == "7"
    with pytest.raises(ConfigValidationError):
        v_int(1, 100)("0")  # below range
    with pytest.raises(ConfigValidationError):
        v_int(1, 100)("1.0")  # float rejected in the money/int path
    with pytest.raises(ConfigValidationError):
        v_float01("2.0")  # above 1.0
    assert v_float01("0.75") == "0.75"
    assert v_schedule("mon 07:00") == "MON 07:00"
    with pytest.raises(ConfigValidationError):
        v_schedule("MON 25:00")  # hour out of range
    assert v_email_list('["a@b.com"]') == '["a@b.com"]'
    assert v_email_list("[]") == "[]"  # empty list is valid (the _default fallback)
    with pytest.raises(ConfigValidationError):
        v_email_list('["not-an-email"]')


# --------------------------------------------------------------- apply_edit ----
def test_denylisted_key_refused_no_write(fake_smartsheet: dict[str, Any]) -> None:
    out = apply_edit("safety_reports.external_send_gate", "safety_reports", "OFF", "op")
    assert out.kind == config_write.NOT_EDITABLE
    assert fake_smartsheet["updates"] == []


def test_out_of_bounds_rejected_no_write(fake_smartsheet: dict[str, Any]) -> None:
    _seed(fake_smartsheet, "safety_reports.intake.confidence_threshold", "safety_reports", "0.75")
    out = apply_edit("safety_reports.intake.confidence_threshold", "safety_reports", "2.0", "op")
    assert out.kind == config_write.REJECTED
    _seed(fake_smartsheet, "alerting.max_alerts_per_hour", "global", "15", row_id=2)
    out2 = apply_edit("alerting.max_alerts_per_hour", "global", "-1", "op")
    assert out2.kind == config_write.REJECTED
    assert fake_smartsheet["updates"] == []  # neither reached a write


def test_send_poller_first_activation_escalates_not_applied(fake_smartsheet: dict[str, Any]) -> None:
    _seed(fake_smartsheet, "po_materials.po_send.polling_enabled", "po_materials", "false", row_id=5)
    out = apply_edit("po_materials.po_send.polling_enabled", "po_materials", "true", "op")
    assert out.kind == config_write.ESCALATED
    assert fake_smartsheet["updates"] == []  # NOT applied
    assert any(a[1] == "config_audit" for a in fake_smartsheet["audits"])  # but audited


def test_send_poller_pause_is_applied(fake_smartsheet: dict[str, Any]) -> None:
    _seed(fake_smartsheet, "po_materials.po_send.polling_enabled", "po_materials", "true", row_id=5)
    out = apply_edit("po_materials.po_send.polling_enabled", "po_materials", "false", "op")
    assert out.kind == config_write.APPLIED
    assert fake_smartsheet["updates"] == [{"_row_id": 5, "Value": "false"}]


def test_apply_writes_and_audits(fake_smartsheet: dict[str, Any]) -> None:
    _seed(fake_smartsheet, "safety_reports.intake.box_filing_enabled", "safety_reports", "false", row_id=9)
    out = apply_edit("safety_reports.intake.box_filing_enabled", "safety_reports", "true", "op")
    assert out.kind == config_write.APPLIED
    assert fake_smartsheet["updates"] == [{"_row_id": 9, "Value": "true"}]
    audit = [a for a in fake_smartsheet["audits"] if a[1] == "config_audit"]
    assert audit and "WARN" in audit[0][0]  # durable WARN audit row


def test_missing_row_is_not_editable(fake_smartsheet: dict[str, Any]) -> None:
    # editable key but no seeded ITS_Config row → refuse (seed first), no write
    out = apply_edit("circuit_breaker.enabled", "global", "false", "op")
    assert out.kind == config_write.NOT_EDITABLE
    assert fake_smartsheet["updates"] == []


# --------------------------------------------------------------------- auth ----
def test_verify_pin_fail_closed_when_unprovisioned(monkeypatch: pytest.MonkeyPatch) -> None:
    import shared.keychain as kc

    def missing(name: str, account: str | None = None) -> str:
        raise kc.KeychainError("not found")

    monkeypatch.setattr(kc, "get_secret", missing)
    with pytest.raises(PinError):
        verify_pin("1234")


def test_verify_pin_correct_and_incorrect(monkeypatch: pytest.MonkeyPatch) -> None:
    import shared.keychain as kc

    monkeypatch.setattr(kc, "get_secret", lambda name, account=None: "s3cr3t")
    verify_pin("s3cr3t")  # no raise
    with pytest.raises(PinError):
        verify_pin("wrong")
    with pytest.raises(PinError):
        verify_pin("")


def test_check_origin_allowlist() -> None:
    check_origin(None, None)  # curl / non-browser → allowed (PIN still required)
    check_origin("http://127.0.0.1:8484", None)  # localhost → allowed
    with pytest.raises(OriginError):
        check_origin("https://evil.example", None)  # cross-origin CSRF → refused


# ---------------------------------------------------------- HTTP integration ----
def _client_with_pin(monkeypatch: pytest.MonkeyPatch, pin: str = "1234") -> TestClient:
    import shared.keychain as kc

    monkeypatch.setattr(kc, "get_secret", lambda name, account=None: pin)
    return TestClient(create_app())


def test_http_apply_flow(fake_smartsheet: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(fake_smartsheet, "circuit_breaker.failure_threshold", "global", "5", row_id=3)
    client = _client_with_pin(monkeypatch)
    resp = client.post(
        "/act/config",
        data={"setting": "circuit_breaker.failure_threshold", "workstream": "global", "value": "7", "pin": "1234"},
    )
    assert resp.status_code == 200
    assert "outcome-applied" in resp.text
    assert fake_smartsheet["updates"] == [{"_row_id": 3, "Value": "7"}]


def test_http_denylist_refused(fake_smartsheet: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client_with_pin(monkeypatch)
    resp = client.post(
        "/act/config",
        data={
            "setting": "safety_reports.external_send_gate",
            "workstream": "safety_reports",
            "value": "OFF",
            "pin": "1234",
        },
    )
    assert "outcome-not_editable" in resp.text
    assert fake_smartsheet["updates"] == []


def test_http_bad_pin_denied_no_write(fake_smartsheet: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(fake_smartsheet, "circuit_breaker.failure_threshold", "global", "5", row_id=3)
    client = _client_with_pin(monkeypatch, pin="realpin")
    resp = client.post(
        "/act/config",
        data={"setting": "circuit_breaker.failure_threshold", "workstream": "global", "value": "7", "pin": "WRONG"},
    )
    assert "outcome-rejected" in resp.text
    assert "denied" in resp.text
    assert fake_smartsheet["updates"] == []


def test_http_outcome_is_escaped(fake_smartsheet: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    # A rejected value containing a script tag must render inert (autoescape).
    _seed(fake_smartsheet, "circuit_breaker.enabled", "global", "true", row_id=4)
    client = _client_with_pin(monkeypatch)
    resp = client.post(
        "/act/config",
        data={
            "setting": "circuit_breaker.enabled",
            "workstream": "global",
            "value": "<script>alert(1)</script>",
            "pin": "1234",
        },
    )
    assert resp.status_code == 200
    assert "<script>alert(1)</script>" not in resp.text
    assert "&lt;script&gt;" in resp.text
    assert fake_smartsheet["updates"] == []


def test_http_config_page_renders(fake_smartsheet: dict[str, Any]) -> None:
    _seed(fake_smartsheet, "circuit_breaker.enabled", "global", "true", row_id=1)
    client = TestClient(create_app())
    resp = client.get("/config")
    assert resp.status_code == 200
    assert "Config editor" in resp.text
    assert "circuit_breaker.enabled" in resp.text


# ------------------------------------------------ review-hardening regressions ----
def test_pin_throttle_locks_out_and_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    import shared.error_log as el
    import shared.keychain as kc
    from operator_dashboard import auth

    monkeypatch.setattr(kc, "get_secret", lambda name, account=None: "correcthorse")
    alerts: list[str | None] = []
    monkeypatch.setattr(el, "log", lambda sev, script, msg, **kw: alerts.append(kw.get("error_code")))
    for _ in range(auth._MAX_PIN_FAILS):  # exhaust the allowance with wrong guesses
        with pytest.raises(PinError):
            verify_pin("wrong")
    # now locked out — even the CORRECT PIN is refused during the lockout window
    with pytest.raises(PinError) as ei:
        verify_pin("correcthorse")
    assert "locked out" in str(ei.value)
    assert "config_pin_lockout" in alerts  # CRITICAL paged on the lockout trip


def test_first_activation_escalates_from_blank_state(fake_smartsheet: dict[str, Any]) -> None:
    # a gated gate whose row exists but Value is BLANK → ->true must escalate (fail-safe)
    _seed(fake_smartsheet, "po_materials.po_send.polling_enabled", "po_materials", "", row_id=7)
    out = apply_edit("po_materials.po_send.polling_enabled", "po_materials", "true", "op")
    assert out.kind == config_write.ESCALATED
    assert fake_smartsheet["updates"] == []


def test_noop_on_canonical_equivalent_no_write_no_audit(fake_smartsheet: dict[str, Any]) -> None:
    # stored 'TRUE' vs submitted 'true' is a TRUE no-op (not a cosmetic rewrite)
    _seed(fake_smartsheet, "circuit_breaker.enabled", "global", "TRUE", row_id=3)
    out = apply_edit("circuit_breaker.enabled", "global", "true", "op")
    assert out.kind == config_write.NOOP
    assert fake_smartsheet["updates"] == []
    assert fake_smartsheet["audits"] == []


def test_giant_int_rejected_not_500(fake_smartsheet: dict[str, Any]) -> None:
    _seed(fake_smartsheet, "alerting.max_alerts_per_hour", "global", "15", row_id=2)
    out = apply_edit("alerting.max_alerts_per_hour", "global", "9" * 5000, "op")
    assert out.kind == config_write.REJECTED
    assert fake_smartsheet["updates"] == []


def test_unexpected_validator_error_becomes_rejected(
    fake_smartsheet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # a validator raising a non-ConfigValidationError must NOT escape as a 500
    from operator_dashboard.act.registry import REGISTRY, ConfigEntry

    def boom(value: str) -> str:
        raise RuntimeError("kaboom")

    monkeypatch.setitem(REGISTRY, ("x.test", "global"), ConfigEntry("x.test", "global", "x", "g", boom))
    out = apply_edit("x.test", "global", "anything", "op")
    assert out.kind == config_write.REJECTED
    assert "RuntimeError" in out.message
    assert fake_smartsheet["updates"] == []


def test_email_list_strips_each_item(fake_smartsheet: dict[str, Any]) -> None:
    # a smuggled trailing newline in a list item is stripped, not persisted
    from operator_dashboard.act.validators import v_email_list

    assert v_email_list('["a@b.com\\n"]') == '["a@b.com"]'


# ---------------------------------------- D1-4 registry reconcile (post-SC/PO) ----
# The subcontracts workstream + PO Features A/B/C landed AFTER the registry was
# frozen; these enroll the live-but-unauthorized gate/behaviour keys so the
# dashboard can pause/resume them. Each is verified LIVE in ITS_Config.
def test_reconcile_enrolls_live_gate_and_behavior_keys() -> None:
    # plain Class-A gates/behaviour — editable, NOT first-activation-gated
    for setting, ws in (
        ("safety_reports.compile_now_poll.polling_enabled", "safety_reports"),
        ("progress_reports.compile_now_poll.polling_enabled", "progress_reports"),
        ("safety_reports.photo_screen.clamav_enabled", "safety_reports"),
        ("po_materials.po_attach_screen.clamav_enabled", "po_materials"),
        ("progress_reports.progress_send.scheduled_send_local", "progress_reports"),
    ):
        entry = registry.get_entry(setting, ws)
        assert entry is not None, f"{setting} [{ws}] should be enrolled"
        assert entry.tier == "A" and not entry.first_activation_gated

    # send-adjacent pollers — Class-A pause, but false->true activation escalates
    for setting, ws in (
        ("progress_reports.progress_send.polling_enabled", "progress_reports"),
        ("subcontracts.subcontract_poll.polling_enabled", "subcontracts"),
        ("subcontracts.subcontract_poll.subcontractors_sync_enabled", "subcontracts"),
        ("subcontracts.subcontract_poll.status_sync_enabled", "subcontracts"),
    ):
        entry = registry.get_entry(setting, ws)
        assert entry is not None, f"{setting} [{ws}] should be enrolled"
        assert entry.tier == "A" and entry.first_activation_gated, f"{setting} must escalate on activation"


def test_subcontract_interval_key_still_not_editable() -> None:
    # the poll-interval is install-time (no hot-reload) — enrolling the gates must
    # NOT accidentally enroll the interval key.
    assert not registry.is_editable("subcontracts.subcontract_poll.poll_interval_seconds", "subcontracts")


def test_enrolled_clamav_toggle_applies_and_audits(fake_smartsheet: dict[str, Any]) -> None:
    # a newly-enrolled plain Class-A behaviour key: false->true applies + audits.
    _seed(fake_smartsheet, "po_materials.po_attach_screen.clamav_enabled", "po_materials", "false", row_id=11)
    out = apply_edit("po_materials.po_attach_screen.clamav_enabled", "po_materials", "true", "op")
    assert out.kind == config_write.APPLIED
    assert fake_smartsheet["updates"] == [{"_row_id": 11, "Value": "true"}]
    assert any(a[1] == "config_audit" for a in fake_smartsheet["audits"])


def test_enrolled_subcontract_gate_activation_escalates_pause_applies(fake_smartsheet: dict[str, Any]) -> None:
    # a newly-enrolled send-adjacent gate: false->true escalates (NOT applied)...
    _seed(fake_smartsheet, "subcontracts.subcontract_poll.polling_enabled", "subcontracts", "false", row_id=12)
    out = apply_edit("subcontracts.subcontract_poll.polling_enabled", "subcontracts", "true", "op")
    assert out.kind == config_write.ESCALATED
    assert fake_smartsheet["updates"] == []
    # ...while pausing an ON gate applies normally.
    _seed(fake_smartsheet, "subcontracts.subcontract_poll.polling_enabled", "subcontracts", "true", row_id=12)
    out2 = apply_edit("subcontracts.subcontract_poll.polling_enabled", "subcontracts", "false", "op")
    assert out2.kind == config_write.APPLIED
    assert fake_smartsheet["updates"] == [{"_row_id": 12, "Value": "false"}]


def test_unknown_key_never_in_registry_refused_no_write(fake_smartsheet: dict[str, Any]) -> None:
    # A key that is in NO tier (not Class A/B, not a secret, not display) is refused.
    # `*.poll_interval_seconds` is the PRINCIPLED example: it is install-time (baked
    # into the plist, no hot-reload), so it stays out of the config registry forever
    # and is retuned only through the Class-B interval verb (daemon_ops.edit_interval).
    # This previously used subcontracts.subcontract_send.polling_enabled — a key that
    # merely happened to be unregistered before the SC-S4 send lane shipped, never a
    # policy statement; it is now a registered send gate.
    assert not registry.is_editable("safety_reports.weekly_send.poll_interval_seconds", "safety_reports")
    out = apply_edit("safety_reports.weekly_send.poll_interval_seconds", "safety_reports", "120", "op")
    assert out.kind == config_write.NOT_EDITABLE
    assert fake_smartsheet["updates"] == []


# ------------------------------------- self-documenting purpose wiring (D1-4) ----
def test_no_registry_note_asserts_a_live_gate_state() -> None:
    """A static note must never claim what a gate is CURRENTLY set to.

    The editor renders each row's live value right beside its note, so a
    hardcoded "currently dark" is redundant on the day it is written and a lie
    afterwards. It became one: po_send, subcontract_poll and the whole ADR-0004
    lane all read 'true' on the mirror host on 2026-07-19 while their notes still
    said "currently dark" / "ships dark". Notes describe SEMANTICS (what pausing
    and activating mean); the value column describes state (§55).
    """
    banned = ("currently dark", "ships dark", "currently on", "currently off", "currently live")
    offenders = [
        f"{e.setting} [{e.workstream}]: {phrase!r}"
        for e in registry.REGISTRY.values()
        for phrase in banned
        if phrase in e.note.lower()
    ]
    # The same ban covers the section intros — a group intro is static text too.
    offenders += [
        f"GROUP_INTROS[{group!r}]: {phrase!r}"
        for group, intro in registry.GROUP_INTROS.items()
        for phrase in banned
        if phrase in intro.lower()
    ]
    assert not offenders, (
        "config-editor notes assert a live gate state, which goes stale: "
        f"{offenders} — describe what the edit MEANS; the value column shows the state"
    )


# ------------------------------------------------ curated ordering / layout ----
def test_group_order_names_every_registry_group_exactly_once() -> None:
    """GROUP_ORDER is the page's curated section order — a new display group must
    be slotted there deliberately, or it silently sorts to the bottom."""
    ordered = list(registry.GROUP_ORDER)
    assert len(ordered) == len(set(ordered)), "duplicate group in GROUP_ORDER"
    live_groups = {e.group for e in registry.REGISTRY.values()}
    assert set(ordered) == live_groups, (
        f"GROUP_ORDER out of sync with registry groups — missing: "
        f"{sorted(live_groups - set(ordered))}, stale: {sorted(set(ordered) - live_groups)}"
    )
    # Intros and accents key real groups only.
    assert set(registry.GROUP_INTROS) <= live_groups
    assert set(registry.GROUP_ACCENTS) <= live_groups


def test_groups_are_tier_homogeneous() -> None:
    """The section header derives its ceremony text ('+ PIN' vs '+ re-PIN +
    confirm') from rows[0].tier — valid only while no group mixes tiers."""
    tiers_by_group: dict[str, set[str]] = {}
    for e in registry.REGISTRY.values():
        tiers_by_group.setdefault(e.group, set()).add(e.tier)
    mixed = {g: sorted(t) for g, t in tiers_by_group.items() if len(t) > 1}
    assert not mixed, f"tier-mixed display groups (header ceremony text would lie): {mixed}"


def test_registry_state_sorted_by_curated_group_order(fake_smartsheet: dict[str, Any]) -> None:
    rows = config_write.read_registry_state()
    ranks = [config_write._GROUP_RANK[r["group"]] for r in rows]
    assert ranks == sorted(ranks), "read_registry_state not in GROUP_ORDER order"
    # First section is the daily-driver gates, last is the Class-B endpoint group.
    assert rows[0]["group"] == registry.GROUP_ORDER[0]
    assert rows[-1]["group"] == registry.GROUP_ORDER[-1]


def test_config_page_renders_slug_anchors_intros_and_pills(
    fake_smartsheet: dict[str, Any],
) -> None:
    """The reorg's render contract: stable slug anchors (not positional), the
    group intro under each head, and live boolean values as ON/OFF pills."""
    _seed(fake_smartsheet, "circuit_breaker.enabled", "global", "true", row_id=1)
    _seed(
        fake_smartsheet,
        "safety_reports.portal_poll.polling_enabled",
        "safety_reports",
        "false",
        row_id=2,
    )
    client = TestClient(create_app())
    resp = client.get("/config")
    assert resp.status_code == 200
    slug = config_write.group_slug(registry.GROUP_ORDER[0])
    assert f'id="grp-{slug}"' in resp.text
    assert f'href="#grp-{slug}"' in resp.text
    assert 'id="grp-1"' not in resp.text  # positional anchors are gone
    assert registry.GROUP_INTROS[registry.GROUP_ORDER[0]][:40] in resp.text
    assert 'class="pill pill-on"' in resp.text
    assert 'class="pill pill-off"' in resp.text


def test_display_state_carries_description_and_reads_the_sheet_once(
    fake_smartsheet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Class-E rows must surface the ITS_Config DESCRIPTION, on ONE fetch.

    The Description cell is where a dark gate's go-live PRECONDITION lives, and
    it is the text an operator needs most for a gate the console refuses to edit.
    The single-fetch half matters because this list grew from 2 to 5 rows when the
    extraction-ladder gates were surfaced: a per-row fetch would make every
    /config render N full-sheet reads.
    """
    import shared.smartsheet_client as ss

    calls: list[int] = []
    real_get_rows = ss.get_rows

    def counting_get_rows(sheet_id: int, **kw: Any) -> list[dict[str, Any]]:
        calls.append(sheet_id)
        return real_get_rows(sheet_id, **kw)

    monkeypatch.setattr(ss, "get_rows", counting_get_rows)
    _seed(
        fake_smartsheet,
        "po_materials.estimate_extract.tier1_enabled",
        "po_materials",
        "false",
        row_id=41,
    )
    fake_smartsheet["rows"][("po_materials.estimate_extract.tier1_enabled", "po_materials")][
        "Description"
    ] = "Do NOT set true until the ladder eval qualifies a model."

    rows = {(r["setting"], r["workstream"]): r for r in config_write.read_display_state()}
    row = rows[("po_materials.estimate_extract.tier1_enabled", "po_materials")]
    assert row["value"] == "false"
    assert "Do NOT set true" in row["description"]
    # a row with no live ITS_Config row degrades visibly, never silently
    assert rows[("safety_reports.external_send_gate", "safety_reports")]["value"] == "(unavailable)"
    assert len(calls) == 1, f"expected ONE ITS_Config fetch for all Class-E rows, got {len(calls)}"


def test_purpose_map_reads_config_defaults() -> None:
    # the generated data dictionary supplies human 'purpose' prose per key.
    pm = config_write._purpose_map()
    assert pm  # non-empty (the file is present + parseable)
    assert pm.get(("field_ops.fieldops_sync.sync_enabled", "field_ops"))


def test_read_registry_state_carries_purpose(fake_smartsheet: dict[str, Any]) -> None:
    _seed(fake_smartsheet, "field_ops.fieldops_sync.sync_enabled", "field_ops", "true", row_id=20)
    rows = {(r["setting"], r["workstream"]): r for r in config_write.read_registry_state()}
    row = rows[("field_ops.fieldops_sync.sync_enabled", "field_ops")]
    assert "purpose" in row and row["purpose"]  # self-documenting prose surfaced


def test_purpose_map_fail_soft_on_bad_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # a missing/malformed dictionary file must degrade to an empty map (never raise)
    # AND log a WARN so a broken data-dictionary is visible, not silent.
    from pathlib import Path

    import shared.error_log as el

    logs: list[str | None] = []
    monkeypatch.setattr(el, "log", lambda sev, script, msg, **kw: logs.append(kw.get("error_code")))
    config_write._purpose_map.cache_clear()
    monkeypatch.setattr(config_write, "_CONFIG_DEFAULTS_PATH", Path("/nonexistent/config_defaults.json"))
    assert config_write._purpose_map() == {}
    assert "config_purpose_map_unreadable" in logs  # broken dictionary surfaced, not silent
    config_write._purpose_map.cache_clear()  # restore for other tests


def test_lockout_message_shows_remaining_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    # after the bucket locks, the denial surfaces the honest remaining seconds
    # instead of a bare "locked out" (WS2 Block-5 lockout UX).
    import shared.keychain as kc
    from operator_dashboard import auth

    monkeypatch.setattr(kc, "get_secret", lambda name, account=None: "correcthorse")
    for _ in range(auth._MAX_PIN_FAILS):
        with pytest.raises(PinError):
            verify_pin("wrong")
    with pytest.raises(PinError) as ei:
        verify_pin("correcthorse")  # locked out — even the correct PIN is refused
    assert "locked out for ~" in str(ei.value) and "s;" in str(ei.value)


def test_every_send_lane_exposes_all_three_runtime_config_siblings() -> None:
    """PARITY TOOTH: a send lane in the registry carries ALL of its runtime keys.

    Every `*_send` lane resolves the same three ITS_Config settings at runtime —
    `polling_enabled` (the External Send Gate), `from_mailbox` (the From identity)
    and `scheduled_send_local` (the send window). Enrolling only some of them is a
    silent, per-lane console gap: the operator retunes one lane from the dashboard
    and has to hand-edit ITS_Config for its structural twin, with nothing announcing
    the difference.

    That is exactly what happened — PR #627 gave the RFQ lane all three and
    `subcontract_send` only `polling_enabled`, so the subcontract mailbox and send
    window were console-invisible until this was noticed by hand. Deriving the
    expectation from the lanes actually present makes the NEXT omission fail here,
    for whichever lane is added next, rather than waiting to be spotted.

    Structural only — this asserts which KEYS exist, never their VALUES. Pinning
    editable-config content is its own recurring self-defeating-test bug.
    """
    from operator_dashboard.act.registry import REGISTRY

    siblings = ("polling_enabled", "from_mailbox", "scheduled_send_local")
    lanes: dict[str, set[str]] = {}
    for key, _workstream in REGISTRY:
        for sibling in siblings:
            suffix = f".{sibling}"
            if key.endswith(suffix):
                lane = key[: -len(suffix)]
                if lane.endswith("_send"):
                    lanes.setdefault(lane, set()).add(sibling)

    assert lanes, "no *_send lanes found in REGISTRY — did the key shape change?"
    incomplete = {
        lane: sorted(set(siblings) - present)
        for lane, present in lanes.items()
        if set(siblings) - present
    }
    assert not incomplete, (
        "send lanes enrolled in the dashboard config registry without all three runtime "
        f"siblings: {incomplete}. Add the missing key(s) to operator_dashboard/act/registry.py "
        "(and seed the ITS_Config row) so the console covers the lane uniformly."
    )
