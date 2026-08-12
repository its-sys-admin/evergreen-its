"""Tests for the CL-12 production-repoint actuator (production_repoint.py + its value map).

Prove-the-control-bites discipline, all-mock (zero live API calls): every
fail-closed guard is exercised with a synthetic violation — a section-E gate
setting smuggled into the map, a mirror-domain to_production, a DRIFTED current
value (the review's explicit bite case: a typo'd production value that VC-03
would PASS), a missing row, loaded daemons, a declined phrase — and each test
asserts the REFUSAL and that the write seam was never reachable, not just a
green path.

The map's production values are asserted by COMPOSING identities from a bare
domain constant at runtime — the CI secrets job's production-identity guard
(.gitleaks-identity.toml) blocks literal @-emails on the production domain in
.py files (see memory: production-identity-ci-guard).
"""
from __future__ import annotations

import builtins
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# sys.path-driven import (scripts/ has no __init__.py) — mirrors tests/test_standup_tools.py.
_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "scripts" / "migrations"
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))

import production_repoint as pr  # noqa: E402
import standup  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
# Bare domain, no local part — the identity guard's regex matches @-emails only;
# tests COMPOSE expected mailbox values at runtime instead of embedding them.
PROD_DOMAIN = "evergreenrenewables.com"


# ---- fixtures / helpers ----------------------------------------------------


def _boom(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("this seam must never be reached in this test")


_COLUMNS: list[dict[str, Any]] = [
    {"title": "Setting", "id": 101},
    {"title": "Workstream", "id": 102},
    {"title": "Value", "id": 103},
]


def _sheet_rows(values: dict[tuple[str, str], str]) -> list[dict[str, Any]]:
    rows = []
    for i, ((setting, workstream), value) in enumerate(values.items()):
        rows.append({
            "id": 1000 + i,
            "cells": [
                {"columnId": 101, "value": setting},
                {"columnId": 102, "value": workstream},
                {"columnId": 103, "value": value},
            ],
        })
    return rows


def _mock_sheet(
    monkeypatch: pytest.MonkeyPatch, values: dict[tuple[str, str], str],
) -> None:
    monkeypatch.setattr(pr, "_get_sheet", lambda: (_COLUMNS, _sheet_rows(values)))


def _mirror_tenant_values(specs: list[pr.RowSpec]) -> dict[tuple[str, str], str]:
    """A synthetic tenant sitting entirely at its pre-sweep (mirror) values."""
    values: dict[tuple[str, str], str] = {}
    for spec in specs:
        if spec.from_mirror is not None:
            values[spec.key] = spec.from_mirror
        elif spec.resolve_box_root is not None:
            values[spec.key] = "999999999999"  # a mirror-tenant Box folder id
        else:  # prompt_operator (heartbeat placeholder)
            values[spec.key] = "https://heartbeat.invalid/placeholder"
    return values


def _raw_map() -> dict[str, Any]:
    return json.loads(
        (_MIGRATIONS_DIR / "production_repoint_map.json").read_text(encoding="utf-8"))


def _write_map(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "map.json"
    path.write_text(json.dumps({"version": 1, "rows": rows}), encoding="utf-8")
    return path


def _std_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "setting": "example.worker_base_url",
        "workstream": "safety_reports",
        "from_mirror": "https://x.evergreenmirror.com",
        "to_production": "https://x.example.com",
        "category": "A",
        "notes": "synthetic test row",
    }
    row.update(overrides)
    return row


# ---- map data tests --------------------------------------------------------


def test_map_loads_rows_unique() -> None:
    specs = pr.load_map()
    assert len(specs) == 15
    keys = [s.key for s in specs]
    assert len(set(keys)) == len(keys)


def test_map_no_mirror_domain_in_any_to_production() -> None:
    for row in _raw_map()["rows"]:
        to_production = row.get("to_production")
        assert to_production is None or "evergreenmirror" not in to_production, row["setting"]


def test_map_contains_no_section_e_gate_settings() -> None:
    # Raw-JSON scan, independent of the loader's own refusal (belt and braces).
    for row in _raw_map()["rows"]:
        setting = row["setting"]
        assert not setting.endswith("_enabled"), setting
        assert ".polling_enabled" not in setting, setting
        assert "scheduled_send_local" not in setting, setting  # section-E sibling


def test_map_categories_valid() -> None:
    for row in _raw_map()["rows"]:
        assert row["category"] in {"A", "B", "C", "D"}, row["setting"]


def test_section_a_is_exactly_the_worker_base_url_trio() -> None:
    specs = pr.load_map()
    trio = [s for s in specs if s.setting == "safety_reports.portal.worker_base_url"]
    assert len(trio) == 3
    # Parity with the verify_cutover PROFILES skip-set — the same three physical rows.
    assert {s.key for s in trio} == set(pr.PROFILES["phase1-hybrid"])
    assert all(s.category == "A" for s in trio)
    assert all(s.to_production == "https://safety." + PROD_DOMAIN for s in trio)


def test_section_b_mailbox_rows_match_changeset_plus_vc03_enrollment() -> None:
    specs = pr.load_map()
    # Phase-1 single-mailbox model (operator decision 2026-07-23, AMENDED
    # 2026-07-24): four of the five from_mailbox lanes send from its@; the
    # safety lane alone sends from safety@, an SMTP ALIAS on the its@ mailbox
    # (not a second mailbox — the D3 single-mailbox model holds). Per-lane
    # shared MAILBOXES (progress@/procurement@) remain the later step.
    expected = {
        ("safety_reports.weekly_send.from_mailbox", "safety_reports",
         f"safety@{PROD_DOMAIN}"),
        ("progress_reports.progress_send.from_mailbox", "progress_reports",
         f"its@{PROD_DOMAIN}"),
        ("po_materials.po_send.from_mailbox", "po_materials",
         f"its@{PROD_DOMAIN}"),
        # Built after the changeset doc; both verified enrolled in verify_cutover
        # CONFIG_ROWS with sandbox_scan=True (asserted below, not just claimed).
        ("subcontracts.subcontract_send.from_mailbox", "subcontracts",
         f"its@{PROD_DOMAIN}"),
        ("po_materials.rfq_send.from_mailbox", "po_materials",
         f"its@{PROD_DOMAIN}"),
    }
    actual = {(s.setting, s.workstream, s.to_production) for s in specs if s.category == "B"}
    assert actual == expected

    # Every section-B row must be a VC-03 sandbox-scanned row — the enrollment
    # that justifies its presence in the sweep.
    scanned = {
        (cr.key, cr.workstream)
        for cr in pr.verify_cutover.CONFIG_ROWS
        if cr.sandbox_scan
    }
    for setting, workstream, _ in expected:
        assert (setting, workstream) in scanned, (setting, workstream)


def test_section_d_matches_standup_box_root_names() -> None:
    specs = pr.load_map()
    d_rows = {(s.resolve_box_root, s.setting, s.workstream)
              for s in specs if s.category == "D"}
    assert d_rows == set(standup.BOX_ROOT_CONFIG_ROWS)
    assert all(s.to_production is None and s.from_mirror is None
               for s in specs if s.category == "D")


def test_profile_skip_set_targets_real_map_rows() -> None:
    keys = {s.key for s in pr.load_map()}
    for pair in pr.PROFILES["phase1-hybrid"]:
        assert pair in keys, pair


# ---- validator bites (load must REFUSE, whole run) -------------------------


def test_validator_refuses_polling_enabled_gate(tmp_path: Path) -> None:
    # THE named review requirement: a send-gate flip smuggled into the map.
    path = _write_map(tmp_path, [
        _std_row(setting="po_materials.po_send.polling_enabled",
                 from_mirror="false", to_production="true"),
    ])
    with pytest.raises(pr.MapValidationError, match="section-E"):
        pr.load_map(path)


def test_validator_refuses_any_enabled_suffix_gate(tmp_path: Path) -> None:
    path = _write_map(tmp_path, [
        _std_row(setting="field_ops.fieldops_sync.materials_enabled",
                 from_mirror="false", to_production="true"),
    ])
    with pytest.raises(pr.MapValidationError, match="section-E"):
        pr.load_map(path)


def test_validator_refuses_scheduled_send_local(tmp_path: Path) -> None:
    """The third section-E class (2026-07-23 adversarial-review finding — the
    original blocklist missed it): scheduled_send_local is send-scope and must
    never load through this tool."""
    path = _write_map(tmp_path, [
        _std_row(setting="po_materials.po_send.scheduled_send_local",
                 from_mirror="07:00", to_production="07:00"),
    ])
    with pytest.raises(pr.MapValidationError, match="section-E"):
        pr.load_map(path)


def test_validator_refuses_unlisted_setting_class(tmp_path: Path) -> None:
    """The allowlist catch-all: a setting outside the reviewed A-D name classes
    (here, a hypothetical future send-scope name no blocklist anticipates)
    refuses — adding a new repoint class is a code+data change."""
    path = _write_map(tmp_path, [
        _std_row(setting="po_materials.po_send.send_window_local",
                 from_mirror="x", to_production="y"),
    ])
    with pytest.raises(pr.MapValidationError, match="allowlist"):
        pr.load_map(path)


def test_validator_refuses_mirror_domain_in_to_production(tmp_path: Path) -> None:
    path = _write_map(tmp_path, [
        _std_row(to_production="https://safety.evergreenmirror.com"),
    ])
    with pytest.raises(pr.MapValidationError, match="sandbox domain"):
        pr.load_map(path)


def test_validator_refuses_shape_defects(tmp_path: Path) -> None:
    # Duplicate (setting, workstream) pair.
    with pytest.raises(pr.MapValidationError, match="duplicate"):
        pr.load_map(_write_map(tmp_path, [_std_row(), _std_row()]))
    # Literal-value row without from_mirror (no drift detection possible).
    with pytest.raises(pr.MapValidationError, match="from_mirror"):
        pr.load_map(_write_map(tmp_path, [_std_row(from_mirror=None)]))
    # to_production null without exactly one resolution mechanism.
    with pytest.raises(pr.MapValidationError, match="EXACTLY ONE"):
        pr.load_map(_write_map(tmp_path, [_std_row(to_production=None, from_mirror=None)]))
    # Bad category.
    with pytest.raises(pr.MapValidationError, match="category"):
        pr.load_map(_write_map(tmp_path, [_std_row(category="Z")]))


# ---- classification --------------------------------------------------------


def test_classify_row_all_ways() -> None:
    spec = pr.RowSpec(setting="s.x", workstream="w", from_mirror="mirror-value",
                      to_production="prod-value", category="A", notes="n")
    empty: frozenset[tuple[str, str]] = frozenset()
    assert pr.classify_row(spec, "prod-value", empty) == pr.CLASS_ALREADY
    assert pr.classify_row(spec, "mirror-value", empty) == pr.CLASS_REPOINT
    assert pr.classify_row(spec, "typo-value", empty) == pr.CLASS_DRIFTED
    assert pr.classify_row(spec, None, empty) == pr.CLASS_MISSING
    # A BLANK value is drift (half-written row), never silently overwritten.
    assert pr.classify_row(spec, "", empty) == pr.CLASS_DRIFTED
    # Profile skip wins over everything, including missing.
    skip = frozenset({("s.x", "w")})
    assert pr.classify_row(spec, "typo-value", skip) == pr.CLASS_SKIP
    assert pr.classify_row(spec, None, skip) == pr.CLASS_SKIP


def test_drifted_current_value_redlights_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE review bite: a typo'd production value (mirror-free, non-empty — VC-03
    would PASS it) must refuse the WHOLE commit before the first write."""
    specs = pr.load_map()
    values = _mirror_tenant_values(specs)
    # 'evergreenrenewable.com' (missing 's') — the exact typo class from the review.
    values[("safety_reports.weekly_send.from_mailbox", "safety_reports")] = (
        "safety@evergreenrenewable.com")
    _mock_sheet(monkeypatch, values)
    monkeypatch.setattr(pr, "_loaded_its_daemons", lambda: [])
    # Refusal must precede Box resolution, the phrase gate, and any write.
    monkeypatch.setattr(pr, "_resolve_box_root", _boom)
    monkeypatch.setattr(pr, "_confirm_phrase", _boom)
    monkeypatch.setattr(pr, "_write_value", _boom)
    with pytest.raises(pr.RepointRefusedError, match="drifted or missing"):
        pr.run_commit(specs, frozenset(), allow_loaded_daemons=False)


def test_missing_row_redlights_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    specs = pr.load_map()
    values = _mirror_tenant_values(specs)
    del values[("system.operator_email", "global")]
    _mock_sheet(monkeypatch, values)
    monkeypatch.setattr(pr, "_loaded_its_daemons", lambda: [])
    monkeypatch.setattr(pr, "_resolve_box_root", _boom)
    monkeypatch.setattr(pr, "_confirm_phrase", _boom)
    monkeypatch.setattr(pr, "_write_value", _boom)
    with pytest.raises(pr.RepointRefusedError, match="drifted or missing"):
        pr.run_commit(specs, frozenset(), allow_loaded_daemons=False)


# ---- no-write proofs -------------------------------------------------------


def test_plan_mode_never_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    specs = pr.load_map()
    _mock_sheet(monkeypatch, _mirror_tenant_values(specs))
    monkeypatch.setattr(pr, "_put_json", _boom)
    monkeypatch.setattr(pr, "_write_value", _boom)
    assert pr.run_plan(specs, frozenset()) == 0
    # And through the CLI entry (default = plan): still zero writes.
    assert pr.main([]) == 0


def test_plan_mode_flags_drift_without_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    specs = pr.load_map()
    values = _mirror_tenant_values(specs)
    values[("system.operator_email", "global")] = "someone@example.com"  # drifted
    _mock_sheet(monkeypatch, values)
    monkeypatch.setattr(pr, "_put_json", _boom)
    monkeypatch.setattr(pr, "_write_value", _boom)
    assert pr.run_plan(specs, frozenset()) == 1  # abort-preview, still zero writes


def test_declined_phrase_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    specs = pr.load_map()
    _mock_sheet(monkeypatch, _mirror_tenant_values(specs))
    monkeypatch.setattr(pr, "_loaded_its_daemons", lambda: [])
    monkeypatch.setattr(pr, "_resolve_box_root", lambda name: "424242")
    monkeypatch.setattr(
        pr, "_prompt_operator_value", lambda spec: "https://hb.example.com/ping")
    monkeypatch.setattr(pr, "_confirm_phrase", lambda: False)
    monkeypatch.setattr(pr, "_write_value", _boom)
    assert pr.run_commit(specs, frozenset(), allow_loaded_daemons=False) == 1


def test_confirm_phrase_exact_match_and_eof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builtins, "input", lambda *_: "REPOINT TO PRODUCTION")
    assert pr._confirm_phrase() is True
    monkeypatch.setattr(builtins, "input", lambda *_: "repoint to production")
    assert pr._confirm_phrase() is False

    def _eof(*_args: Any) -> str:
        raise EOFError

    monkeypatch.setattr(builtins, "input", _eof)
    assert pr._confirm_phrase() is False  # EOF = decline, no bypass


def test_daemons_loaded_without_override_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pr, "_loaded_its_daemons", lambda: ["org.solutionsmith.its.portal-poll"])
    # The guard must fire BEFORE any tenant read or write.
    monkeypatch.setattr(pr, "_get_sheet", _boom)
    monkeypatch.setattr(pr, "_write_value", _boom)
    with pytest.raises(pr.RepointRefusedError, match="daemons loaded"):
        pr.run_commit(pr.load_map(), frozenset(), allow_loaded_daemons=False)


def test_daemons_loaded_with_override_warns_and_proceeds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    specs = pr.load_map()
    _mock_sheet(monkeypatch, _mirror_tenant_values(specs))
    monkeypatch.setattr(
        pr, "_loaded_its_daemons", lambda: ["org.solutionsmith.its.portal-poll"])
    monkeypatch.setattr(pr, "_resolve_box_root", lambda name: "424242")
    monkeypatch.setattr(
        pr, "_prompt_operator_value", lambda spec: "https://hb.example.com/ping")
    monkeypatch.setattr(pr, "_confirm_phrase", lambda: True)
    written: list[tuple[int, str]] = []
    monkeypatch.setattr(
        pr, "_write_value",
        lambda row_id, col_id, value: written.append((row_id, value)))
    assert pr.run_commit(specs, frozenset(), allow_loaded_daemons=True) == 0
    assert written  # proceeded
    out = capsys.readouterr().out
    assert "[WARN] --allow-loaded-daemons" in out


# ---- profile skip ----------------------------------------------------------


def test_profile_skip_trio_never_written_under_commit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    specs = pr.load_map()
    skip = pr.PROFILES["phase1-hybrid"]
    _mock_sheet(monkeypatch, _mirror_tenant_values(specs))
    monkeypatch.setattr(pr, "_loaded_its_daemons", lambda: [])
    monkeypatch.setattr(pr, "_resolve_box_root", lambda name: "424242")
    monkeypatch.setattr(
        pr, "_prompt_operator_value", lambda spec: "https://hb.example.com/ping")
    monkeypatch.setattr(pr, "_confirm_phrase", lambda: True)
    written: list[str] = []
    monkeypatch.setattr(
        pr, "_write_value", lambda row_id, col_id, value: written.append(value))
    assert pr.run_commit(specs, frozenset(skip), allow_loaded_daemons=False) == 0
    # 15 map rows - the 3 skipped worker_base_url rows = 12 writes.
    assert len(written) == 12
    trio_value = next(s.to_production for s in specs if s.category == "A")
    assert trio_value is not None and trio_value not in written
    assert capsys.readouterr().out.count(pr.CLASS_SKIP) >= 3


# ---- commit happy path + idempotency ---------------------------------------


def test_commit_happy_path_writes_all_and_gates_verify(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    specs = pr.load_map()
    _mock_sheet(monkeypatch, _mirror_tenant_values(specs))
    monkeypatch.setattr(pr, "_loaded_its_daemons", lambda: [])
    monkeypatch.setattr(pr, "_resolve_box_root", lambda name: "424242")
    heartbeat = "https://heartbeat.uptimerobot.example/ping/abc"
    monkeypatch.setattr(pr, "_prompt_operator_value", lambda spec: heartbeat)
    monkeypatch.setattr(pr, "_confirm_phrase", lambda: True)
    written: list[str] = []
    monkeypatch.setattr(
        pr, "_write_value", lambda row_id, col_id, value: written.append(value))
    assert pr.run_commit(specs, frozenset(), allow_loaded_daemons=False) == 0
    assert len(written) == 15  # every map row repointed
    assert written.count("424242") == 5  # every section-D row carries the RESOLVED id
    assert heartbeat in written
    out = capsys.readouterr().out
    # Post-run instruction printed, never executed (the operator gates).
    assert "python -m scripts.verify_cutover --only config" in out


def test_commit_already_production_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    specs = pr.load_map()
    values: dict[tuple[str, str], str] = {}
    for spec in specs:
        if spec.to_production is not None:
            values[spec.key] = spec.to_production
        elif spec.resolve_box_root is not None:
            values[spec.key] = "424242"  # already the resolved production id
        else:
            values[spec.key] = "https://hb.example.com/ping"
    _mock_sheet(monkeypatch, values)
    monkeypatch.setattr(pr, "_loaded_its_daemons", lambda: [])
    monkeypatch.setattr(pr, "_resolve_box_root", lambda name: "424242")
    monkeypatch.setattr(pr, "_prompt_operator_value", lambda spec: None)  # declined
    # No writes pending -> the phrase gate must never even be prompted.
    monkeypatch.setattr(pr, "_confirm_phrase", _boom)
    monkeypatch.setattr(pr, "_write_value", _boom)
    assert pr.run_commit(specs, frozenset(), allow_loaded_daemons=False) == 0


# ---- section-D + prompt seams ----------------------------------------------


def test_resolve_box_root_refuses_ambiguity_and_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared import box_client

    monkeypatch.setattr(box_client, "list_folder", lambda fid, limit=100: [
        {"id": "1", "name": "ITS Safety Reports", "type": "folder"},
        {"id": "2", "name": "ITS Safety Reports", "type": "folder"},
    ])
    with pytest.raises(pr.RepointRefusedError, match="AMBIGUOUS"):
        pr._resolve_box_root("ITS Safety Reports")

    monkeypatch.setattr(box_client, "list_folder", lambda fid, limit=100: [])
    with pytest.raises(pr.RepointRefusedError, match="not found"):
        pr._resolve_box_root("ITS Safety Reports")

    # Exact-name match; same-named FILES are ignored.
    monkeypatch.setattr(box_client, "list_folder", lambda fid, limit=100: [
        {"id": "3", "name": "ITS Safety Reports", "type": "file"},
        {"id": "4", "name": "ITS Safety Reports", "type": "folder"},
        {"id": "5", "name": "ITS Safety Reports (old)", "type": "folder"},
    ])
    assert pr._resolve_box_root("ITS Safety Reports") == "4"


def test_prompt_operator_value_validates_https(monkeypatch: pytest.MonkeyPatch) -> None:
    heartbeat_spec = next(s for s in pr.load_map() if s.prompt_operator)

    answers = iter(["http://not-tls.example.com", "https://ok.example.com/ping"])
    monkeypatch.setattr(builtins, "input", lambda *_: next(answers))
    assert pr._prompt_operator_value(heartbeat_spec) == "https://ok.example.com/ping"

    monkeypatch.setattr(builtins, "input", lambda *_: "")
    assert pr._prompt_operator_value(heartbeat_spec) is None  # blank = skip

    def _eof(*_args: Any) -> str:
        raise EOFError

    monkeypatch.setattr(builtins, "input", _eof)
    assert pr._prompt_operator_value(heartbeat_spec) is None  # EOF = skip
