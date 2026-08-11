"""Tests for the wipe/stand-up tool family (wipe_tenant / sheet_ids_regen /
build_legacy_workspaces / standup).

Prove-the-control-bites discipline: every fail-closed guard is exercised with a
synthetic violation — allowlist drift, loaded daemons, refused phrase, duplicate
names, a corrupted em dash — and the test asserts the REFUSAL (and that no delete
was reachable), not just a green path.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, cast

import pytest

# sys.path-driven import (scripts/ has no __init__.py) — mirrors tests/test_gap_builders.py.
_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "scripts" / "migrations"
if str(_MIGRATIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_MIGRATIONS_DIR))

import _rest_retry  # noqa: E402
import build_legacy_workspaces as legacy  # noqa: E402
import requests  # noqa: E402
import sheet_ids_regen as regen  # noqa: E402
import standup  # noqa: E402
import wipe_tenant as wipe  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _redirect_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here gets the stand-up ACT-fence marker (PR #674) redirected
    to tmp_path — unit tests must never touch ~/its/state (the live-state
    tripwire enforces it; forensic class #8)."""
    monkeypatch.setattr(standup, "STANDUP_MARKER_PATH",
                        tmp_path / "standup_in_progress.json")
    monkeypatch.setattr(wipe, "STANDUP_MARKER_PATH",
                        tmp_path / "standup_in_progress.json")


# ---- wipe_tenant: allowlist double-match (guard 1) ------------------------


def test_allowlist_exact_match_is_deletable() -> None:
    # Fixture derived FROM the allowlist: the pins are historical (the wiped
    # tenant's ids, deliberately never remapped), so the test must never
    # hardcode an id that can drift from them.
    pin_name, pin_id = wipe.SMARTSHEET_WORKSPACE_ALLOWLIST[0]
    live = [{"name": pin_name, "id": pin_id}]
    deletable, mismatches, unlisted = wipe.match_allowlist(
        live, wipe.SMARTSHEET_WORKSPACE_ALLOWLIST)
    assert [d["id"] for d in deletable] == [pin_id]
    assert not mismatches and not unlisted


def test_allowlist_name_id_drift_refuses() -> None:
    # Same name, DIFFERENT id — the drifted-tenant case must land in mismatches.
    live = [{"name": wipe.SMARTSHEET_WORKSPACE_ALLOWLIST[0][0], "id": 999}]
    deletable, mismatches, _ = wipe.match_allowlist(
        live, wipe.SMARTSHEET_WORKSPACE_ALLOWLIST)
    assert not deletable
    assert len(mismatches) == 1


def test_allowlist_id_reused_under_new_name_refuses() -> None:
    live = [{"name": "Totally Different",
             "id": wipe.SMARTSHEET_WORKSPACE_ALLOWLIST[0][1]}]
    deletable, mismatches, _ = wipe.match_allowlist(
        live, wipe.SMARTSHEET_WORKSPACE_ALLOWLIST)
    assert not deletable
    assert len(mismatches) == 1


def test_allowlist_unlisted_never_deletable() -> None:
    live = [{"name": "Customer Production Workspace", "id": 123456}]
    deletable, mismatches, unlisted = wipe.match_allowlist(
        live, wipe.SMARTSHEET_WORKSPACE_ALLOWLIST)
    assert not deletable and not mismatches
    assert unlisted == ["'Customer Production Workspace' (id=123456)"]


def test_wipe_main_refuses_on_mismatch_even_with_phrase(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard 1 bites in main(): drifted pins abort BEFORE the phrase/dump/delete."""
    monkeypatch.setattr(wipe, "require_daemons_down", lambda: None)
    monkeypatch.setattr(wipe, "_list_workspaces",
                        lambda: [{"name": wipe.SMARTSHEET_WORKSPACE_ALLOWLIST[0][0],
                                  "id": 999}])
    monkeypatch.setattr(wipe, "_box_root_items", lambda: [])
    monkeypatch.setattr(wipe, "_confirm_phrase", lambda: True)

    def _boom(*a: Any, **k: Any) -> None:
        raise AssertionError("delete reached despite allowlist mismatch")

    monkeypatch.setattr(wipe, "_delete_workspace", _boom)
    monkeypatch.setattr(wipe, "_delete_box_folder", _boom)
    monkeypatch.setattr(sys, "argv", ["wipe_tenant.py", "--commit"])
    assert wipe.main() == 1


def test_wipe_main_refuses_without_phrase(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard 3 bites: a declined phrase deletes nothing."""
    monkeypatch.setattr(wipe, "require_daemons_down", lambda: None)
    monkeypatch.setattr(wipe, "_list_workspaces",
                        lambda: [dict(zip(("name", "id"),
                                          wipe.SMARTSHEET_WORKSPACE_ALLOWLIST[0],
                                          strict=True))])
    monkeypatch.setattr(wipe, "_box_root_items", lambda: [])
    monkeypatch.setattr(wipe, "_confirm_phrase", lambda: False)

    def _boom(*a: Any, **k: Any) -> None:
        raise AssertionError("delete reached despite refused phrase")

    monkeypatch.setattr(wipe, "_delete_workspace", _boom)
    monkeypatch.setattr(wipe, "_delete_box_folder", _boom)
    monkeypatch.setattr(wipe, "dump_workspace", _boom)
    monkeypatch.setattr(sys, "argv", ["wipe_tenant.py", "--commit"])
    assert wipe.main() == 1


def test_wipe_refuses_while_daemons_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard 2 bites: loaded fleet -> WipeRefusedError before anything else."""
    monkeypatch.setattr(wipe, "_loaded_its_daemons",
                        lambda: ["org.solutionsmith.its.portal-poll"])
    with pytest.raises(wipe.WipeRefusedError):
        wipe.require_daemons_down()
    monkeypatch.setattr(wipe, "_loaded_its_daemons", lambda: [])
    wipe.require_daemons_down()  # empty fleet passes


def test_wipe_plan_mode_never_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    # plan mode calls _loaded_its_daemons directly (WARN-only branch) — stub it
    # rather than require_daemons_down, or Linux CI dies on a missing launchctl.
    monkeypatch.setattr(wipe, "_loaded_its_daemons", lambda: [])
    monkeypatch.setattr(wipe, "_list_workspaces",
                        lambda: [dict(zip(("name", "id"),
                                          wipe.SMARTSHEET_WORKSPACE_ALLOWLIST[0],
                                          strict=True))])
    monkeypatch.setattr(wipe, "_box_root_items", lambda: [])

    def _boom(*a: Any, **k: Any) -> None:
        raise AssertionError("plan mode reached a destructive call")

    monkeypatch.setattr(wipe, "_delete_workspace", _boom)
    monkeypatch.setattr(wipe, "_delete_box_folder", _boom)
    monkeypatch.setattr(wipe, "_confirm_phrase", _boom)
    monkeypatch.setattr(wipe, "dump_workspace", _boom)
    monkeypatch.setattr(sys, "argv", ["wipe_tenant.py"])
    assert wipe.main() == 0


# ---- sheet_ids_regen: resolution ------------------------------------------

_TREE: dict[str, Any] = {
    "id": 100,
    "name": "ITS — System",
    "folders": [
        {"id": 200, "name": "01 — Config",
         "folders": [],
         "sheets": [{"id": 300, "name": "ITS_Config"}]},
        {"id": 201, "name": "02 — Logs",
         "folders": [],
         "sheets": [{"id": 301, "name": "ITS_Errors"},
                    {"id": 302, "name": "ITS_Errors"}]},  # duplicate leaf
    ],
    "sheets": [],
}


def test_resolve_workspace_folder_sheet() -> None:
    assert regen.resolve_in_tree(_TREE, regen.Target("ITS — System")) == 100
    assert regen.resolve_in_tree(
        _TREE, regen.Target("ITS — System", ("01 — Config",))) == 200
    assert regen.resolve_in_tree(
        _TREE, regen.Target("ITS — System", ("01 — Config",), "ITS_Config")) == 300


def test_resolve_duplicate_leaf_is_ambiguous_not_first_match() -> None:
    res = regen.resolve_in_tree(
        _TREE, regen.Target("ITS — System", ("02 — Logs",), "ITS_Errors"))
    assert res == regen.AMBIGUOUS


def test_resolve_missing_required_vs_optional() -> None:
    required = regen.Target("ITS — System", ("01 — Config",), "Nope")
    optional = regen.Target("ITS — System", ("01 — Config",), "Nope", optional=True)
    assert regen.resolve_in_tree(_TREE, required) == regen.MISSING
    assert regen.resolve_in_tree(_TREE, optional) == regen.ABSENT_OPTIONAL


def test_missing_required_excludes_ambiguous_and_optional() -> None:
    """Only MISSING drives the propagation probe: AMBIGUOUS is a real conflict
    (retrying cannot fix a duplicate name) and absent-optional is by design."""
    res: dict[str, int | str] = {"A": regen.MISSING, "B": regen.AMBIGUOUS,
                                 "C": regen.ABSENT_OPTIONAL, "D": 123}
    ext: dict[str, dict[str, int | str]] = {"f.py": {"E": regen.MISSING, "F": 9}}
    assert regen.missing_required(res, ext) == {"A", "f.py:E"}


def test_resolve_all_duplicate_workspace_names_ambiguous() -> None:
    workspaces = [{"name": "ITS — System", "id": 100},
                  {"name": "ITS — System", "id": 101}]
    out = regen.resolve_all({"WORKSPACE_SYSTEM": regen.Target("ITS — System")},
                            workspaces, {"ITS — System": _TREE})
    assert out["WORKSPACE_SYSTEM"] == regen.AMBIGUOUS


# ---- sheet_ids_regen: rewrite mechanics -----------------------------------

_FIXTURE = """# header comment
WORKSPACE_SYSTEM       = 111   # ITS — System (operator-only)
SHEET_CONFIG              = 222  # ITS_Config
ALIAS = WORKSPACE_SYSTEM  # no digits — untouched
DAEMON_HEALTH_COLUMNS: dict[str, int] = {
    "daemon_name":                  333,
    "workstream":                  444,
}
"""


def test_rewrite_constants_preserves_structure() -> None:
    text, changed = regen.rewrite_constants(
        _FIXTURE, {"WORKSPACE_SYSTEM": 911, "SHEET_CONFIG": 222})
    assert "WORKSPACE_SYSTEM       = 911   # ITS — System (operator-only)" in text
    assert "SHEET_CONFIG              = 222  # ITS_Config" in text  # unchanged value
    assert "ALIAS = WORKSPACE_SYSTEM" in text
    assert changed == ["WORKSPACE_SYSTEM"]


def test_rewrite_dict_values() -> None:
    text, changed = regen.rewrite_dict_values(_FIXTURE, {"daemon_name": 999})
    assert '"daemon_name":                  999,' in text
    assert '"workstream":                  444,' in text
    assert changed == ["daemon_name"]


def test_integer_remap_word_boundaries() -> None:
    text = "a = 123\nb = 51234\nc = 1123\nnode(sheet_id=123)\n"
    out, _n = regen.rewrite_integer_remap(text, {123: 777})
    assert "a = 777" in out
    assert "b = 51234" in out  # 123 inside a longer number untouched
    assert "c = 1123" in out
    assert "sheet_id=777" in out


def test_integer_remap_chained_pairs_never_double_replace() -> None:
    # One pair's NEW id equals another pair's OLD id: two-phase must keep them
    # independent — sequential replacement would turn the first 1 into 3.
    out, n = regen.rewrite_integer_remap("x = 1\ny = 2\n", {1: 2, 2: 3})
    assert "x = 2" in out and "y = 3" in out
    assert n == 2


def test_read_current_values() -> None:
    vals = regen.read_current_values(_FIXTURE, ["WORKSPACE_SYSTEM", "SHEET_CONFIG",
                                                "ALIAS", "MISSING_CONST"])
    assert vals == {"WORKSPACE_SYSTEM": 111, "SHEET_CONFIG": 222}


# ---- sheet_ids_regen: registry parity teeth -------------------------------


def test_registry_covers_every_sheet_ids_constant() -> None:
    """A new WORKSPACE_/FOLDER_/SHEET_ constant in shared/sheet_ids.py MUST get a
    REGISTRY entry in the same PR — otherwise the auto-FLIP silently skips it and
    the constant goes stale on the next rebuild. This test is the teeth."""
    text = (REPO_ROOT / "shared" / "sheet_ids.py").read_text(encoding="utf-8")
    declared = set(re.findall(
        r"^((?:WORKSPACE|FOLDER|SHEET)_[A-Z0-9_]+)\s*=\s*\d+", text, re.MULTILINE))
    missing = declared - set(regen.REGISTRY)
    assert not missing, (
        f"sheet_ids.py constants missing a sheet_ids_regen.REGISTRY entry: "
        f"{sorted(missing)}")


def test_registry_daemon_health_keys_match_sheet_ids_dict() -> None:
    text = (REPO_ROOT / "shared" / "sheet_ids.py").read_text(encoding="utf-8")
    dict_body = text.split("DAEMON_HEALTH_COLUMNS", 1)[1].split("}", 1)[0]
    keys = set(re.findall(r'"([a-z_]+)":\s*\d+', dict_body))
    assert keys == set(regen.DAEMON_HEALTH_TITLE_BY_KEY)


def test_remap_scope_includes_doctrine_manifest() -> None:
    """docs/doctrine_manifest.yaml MUST stay in the --write remap scope: its
    canonical_sheets ids are what check_doctrine_drift M4 (CI-BLOCKING)
    compares against sheet_ids.py, and the *.py-only remap missed it on the
    2026-07-23 rebuild (PR #670 hand-fixed it). Same parity-teeth pattern as
    test_regen_expect_table_matches_registry_and_stages."""
    paths = regen.remap_file_paths()
    assert regen.DOCTRINE_MANIFEST_PATH in paths
    assert regen.DOCTRINE_MANIFEST_PATH.is_file()
    # The wipe tool must ALSO be in the list (its exclusion happens at the
    # write loop, never by dropping it from the glob — a reordering that
    # silently removed the exemption comment's anchor would be invisible).
    assert (REPO_ROOT / "scripts" / "migrations" / "wipe_tenant.py") in paths


def test_integer_remap_rewrites_yaml_content() -> None:
    """The two-phase remap is format-agnostic — prove it flips a manifest-shaped
    yaml fragment (the PR #670 miss, synthetically re-created)."""
    yaml_text = (
        "canonical_sheets:\n"
        "  SHEET_CONFIG:\n"
        "    id: 8933909738770308\n"
        "    source: \"shared/sheet_ids.py:SHEET_CONFIG\"\n"
    )
    out, n = regen.rewrite_integer_remap(yaml_text, {8933909738770308: 12345})
    assert "id: 12345" in out
    assert n == 1


def test_sweep_covers_yaml_and_md_report_only(tmp_path: Path) -> None:
    """The widened sweep surfaces replaced ids in yaml/yml/md (report-only) —
    and never rewrites them (file content asserted unchanged)."""
    (tmp_path / "runbook.md").write_text("old sheet id 111222333 lives here\n")
    (tmp_path / "conf.yaml").write_text("sheet_id: 111222333\n")
    (tmp_path / "pin.py").write_text("SHEET = 111222333\n")
    (tmp_path / "other.txt").write_text("111222333\n")  # outside SWEEP_GLOBS
    hits = regen.sweep_repo_for_old_ids(
        {111222333: 999}, skip=set(), root=tmp_path)
    hit_files = {h.split(":")[0] for h in hits}
    assert hit_files == {"runbook.md", "conf.yaml", "pin.py"}
    # report-only: nothing rewritten
    assert "111222333" in (tmp_path / "runbook.md").read_text()
    assert "111222333" in (tmp_path / "conf.yaml").read_text()


# ---- sheet_ids_regen: scoped propagation-probe retry ------------------------


def test_constant_workspaces_resolves_plain_and_external() -> None:
    out = regen._constant_workspaces({
        "WORKSPACE_SYSTEM",
        "safety_reports/week_folder.py:TEMPLATE_DAILY_REPORTS_SHEET_ID",
        "NOT_A_REAL_CONSTANT",
    })
    assert out == {"ITS — System", "Forefront Portfolio — ITS Demo"}


def test_filtered_retry_never_degrades_unfetched_constants() -> None:
    """The overlay teeth: a workspace-filtered re-resolve reports MISSING for
    every unfetched workspace's constants — merging it naively would flip
    already-resolved constants back to MISSING. Only refetched workspaces may
    take fresh values."""
    old_res: dict[str, int | str] = {c: 1000 + i for i, c in enumerate(regen.REGISTRY)}
    old_ext: dict[str, dict[str, int | str]] = {
        path: {c: 2000 for c in consts}
        for path, consts in regen.EXTERNAL_CONSTANTS.items()}
    old_cols = {"daemon_name": 42}
    fresh_res: dict[str, int | str] = {
        c: (7777 if t.workspace == "ITS — System" else regen.MISSING)
        for c, t in regen.REGISTRY.items()}
    fresh_ext: dict[str, dict[str, int | str]] = {
        path: {c: regen.MISSING for c in consts}
        for path, consts in regen.EXTERNAL_CONSTANTS.items()}
    fresh_cols = {"daemon_name": 99}
    merged_res, merged_ext, merged_cols = regen._overlay_resolutions(
        (old_res, old_ext, old_cols),
        (fresh_res, fresh_ext, fresh_cols),
        refetched={"ITS — System"})
    # System-workspace constants take the fresh value...
    assert merged_res["WORKSPACE_SYSTEM"] == 7777
    assert merged_res["SHEET_CONFIG"] == 7777
    # ...every other workspace's constants KEEP their prior resolution.
    assert merged_res["WORKSPACE_SAFETY_PORTAL"] == old_res["WORKSPACE_SAFETY_PORTAL"]
    assert all(v == 2000 for cmap in merged_ext.values() for v in cmap.values())
    # DAEMON_HEALTH columns ride the System workspace -> fresh here.
    assert merged_cols == {"daemon_name": 99}
    # ...and stay OLD when System was not refetched.
    _, _, cols_kept = regen._overlay_resolutions(
        (old_res, old_ext, old_cols),
        (fresh_res, fresh_ext, fresh_cols),
        refetched={"ITS — Purchase Orders"})
    assert cols_kept == {"daemon_name": 42}


def test_retry_loop_scopes_resolve_to_expected_workspaces(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Wiring: attempt 1 resolves FULL (drift-self-heal); the retry burst calls
    _resolve_live with exactly the unresolved --expect constants' workspaces."""
    calls: list[set[str] | None] = []
    all_missing: dict[str, int | str] = dict.fromkeys(regen.REGISTRY, regen.MISSING)
    resolved_system = dict(all_missing) | {
        c: 5000 + i for i, (c, t) in enumerate(regen.REGISTRY.items())
        if t.workspace == "ITS — System"}
    ext_missing = {path: dict.fromkeys(consts, regen.MISSING)
                   for path, consts in regen.EXTERNAL_CONSTANTS.items()}

    def _fake_resolve(workspace_filter: set[str] | None = None) -> Any:
        calls.append(workspace_filter)
        res = all_missing if workspace_filter is None else resolved_system
        return dict(res), {p: dict(c) for p, c in ext_missing.items()}, {}

    monkeypatch.setattr(regen, "_resolve_live", _fake_resolve)
    monkeypatch.setattr(regen.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        sys, "argv",
        ["sheet_ids_regen.py", "--retry-missing", "3",
         "--expect", "WORKSPACE_SYSTEM", "--expect", "SHEET_CONFIG"])
    rc = regen.main()
    assert rc == 0  # plan mode; the expected constants resolved on the retry
    assert calls[0] is None                     # initial resolve is FULL
    assert calls[1] == {"ITS — System"}         # retry scoped to the expect set
    assert len(calls) == 2                      # resolved -> no further attempts
    assert "re-resolving 1 workspace(s)" in capsys.readouterr().out


# ---- build_legacy_workspaces ----------------------------------------------


def test_legacy_dash_assertion_bites(monkeypatch: pytest.MonkeyPatch) -> None:
    """A silently-normalized dash (em -> en) must fail CLOSED at the assert."""
    bad = (legacy.WorkspaceSpec("ITS – Human Review", ()),)  # en dash
    monkeypatch.setattr(legacy, "WORKSPACES", bad)
    with pytest.raises(ValueError, match="canonical_name_dash_corrupted"):
        legacy._assert_canonical_dashes()


def test_legacy_schemas_match_live_capture_shape() -> None:
    """Column counts + single-primary invariant, pinned to the 2026-07-22 capture."""
    expected = {
        "WPR_PENDING_REVIEW_COLUMNS": 12,
        "TIME_OFF_COLUMNS": 6,
        "SUBCONTRACTOR_DB_COLUMNS": 11,
        "VENDOR_DB_COLUMNS": 9,
        "EQUIPMENT_MASTER_COLUMNS": 8,
        "TEMPLATE_DAILY_COLUMNS": 9,
        "TEMPLATE_ROLLUP_COLUMNS": 4,
    }
    for attr, count in expected.items():
        columns = getattr(legacy, attr)
        assert len(columns) == count, attr
        assert sum(1 for c in columns if c.get("primary")) == 1, attr


def test_legacy_not_owner_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        legacy, "_find_workspaces",
        lambda name: [{"id": 1, "name": name, "accessLevel": "ADMIN",
                       "permalink": "https://x"}])
    runner = legacy.Runner(dry_run=False)
    with pytest.raises(legacy.BuildRefusedError):
        runner.ensure_workspace(legacy.WORKSPACES[0])


def test_legacy_duplicate_workspace_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        legacy, "_find_workspaces",
        lambda name: [{"id": 1, "name": name, "accessLevel": "OWNER"},
                      {"id": 2, "name": name, "accessLevel": "OWNER"}])
    runner = legacy.Runner(dry_run=False)
    with pytest.raises(legacy.BuildRefusedError):
        runner.ensure_workspace(legacy.WORKSPACES[0])


def test_legacy_demo_tree_contains_week_templates() -> None:
    demo = next(w for w in legacy.WORKSPACES
                if w.name == "Forefront Portfolio — ITS Demo")
    field_reports = next(f for f in demo.folders
                         if f.name == "03 — Field Reports (JHA/TBT)")
    bradley = next(f for f in field_reports.folders
                   if f.name == "Bradley 1 (BBCHS 1)")
    week = next(f for f in bradley.folders if f.name == "Week of 2026-03-09")
    names = {s.name for s in week.sheets}
    assert names == {"Daily Reports — Week of 2026-03-09",
                     "Weekly Rollup — Week of 2026-03-09"}


# ---- standup: stage-graph invariants --------------------------------------


def _stage_names(dump: Path | None) -> list[str]:
    return [n for n, _ in standup.build_stages(dump, skip_shares=False)]


def test_stage_names_unique_and_final_verify_last() -> None:
    names = _stage_names(Path("/tmp/x"))
    assert len(names) == len(set(names))
    assert names[-1] == "final-verify"


def test_flip_precedes_seed_ordering() -> None:
    names = _stage_names(Path("/tmp/x"))
    # SEED follows FLIP: the config baseline seed runs only after the System
    # sheets exist AND their ids are regenerated.
    assert names.index("seed-config-baseline") > names.index("regen-system-sheets")
    # Box-root config auto-paste needs ITS_Config to exist.
    assert names.index("box-roots") > names.index("seed-config-baseline")
    # Row restore happens before the seeders (seeders skip-existing against it).
    assert names.index("restore-rows") < names.index("seeders")
    # Every builder stage is followed by a regen before any dependent stage runs.
    assert names.index("regen-system") == names.index("system-workspace") + 1
    assert names.index("regen-po") == names.index("po-workspace") + 1


def test_fresh_tenant_mode_drops_restore_stages() -> None:
    names = _stage_names(None)
    assert "restore-rows" not in names
    assert "restore-shares" not in names
    assert names[-1] == "final-verify"


def test_skipped_seeders_stay_skipped() -> None:
    """seed_its_active_jobs / seed_its_project_routing / trusted-contacts must NOT
    be in the stage table (documented skips — dropdown pollution / dead Box ids /
    dormant lane). If someone adds them, this fails and forces the conversation."""
    import inspect
    source = inspect.getsource(standup.build_stages)
    for forbidden in ("seed_its_active_jobs", "seed_its_project_routing",
                      "seed_its_trusted_contacts", "build_its_trusted_contacts_sheet"):
        assert forbidden not in source, forbidden


def test_regen_expect_table_matches_registry_and_stages() -> None:
    """Every REGEN_EXPECT key is a real stage; every expected constant is a real
    regen REGISTRY / EXTERNAL_CONSTANTS name (a typo would make --expect abort
    the whole stand-up at that stage); every regen stage has an expect list."""
    names = _stage_names(Path("/tmp/x"))
    known = set(regen.REGISTRY) | {
        f"{path}:{c}" for path, consts in regen.EXTERNAL_CONSTANTS.items()
        for c in consts}
    for stage, expects in standup.REGEN_EXPECT.items():
        assert stage in names, stage
        for const in expects:
            assert const in known, (stage, const)
    regen_stages = {n for n in names if n.startswith("regen-")}
    assert regen_stages == set(standup.REGEN_EXPECT)


def test_every_vc03_config_row_has_a_seeder_that_standup_runs() -> None:
    """Every load-bearing ITS_Config row verify_cutover VC-03 asserts must be
    seeded by a script the STAND-UP ACTUALLY EXECUTES — not merely one that
    exists on disk. The 2026-07-23 rehearsal found 15 rows that had only ever
    been hand-created; the 2026-08-10 sequel found the opposite failure: the
    old guard grepped the whole migrations DIRECTORY, so seed_manifest_config.py
    satisfied it while standup's seeders stage never ran it — a rebuilt tenant
    had no field_ops.manifest_poll.* rows and VC-03 failed with no scripted
    remedy. Corpus = seed-config-baseline (scripts/seed_its_config.py) + the
    scripts in standup.SEEDER_STAGE_SCRIPTS + the builder/one-shot scripts
    standup's OTHER stages run (box-roots paste, docs-index self-record)."""
    vc_text = (REPO_ROOT / "scripts" / "verify_cutover.py").read_text(encoding="utf-8")
    settings = set(re.findall(r'ConfigRow\(\s*"([^"]+)"', vc_text))
    assert len(settings) > 30, "ConfigRow parse regressed"
    # Every listed seeder must actually exist on disk...
    for name in standup.SEEDER_STAGE_SCRIPTS:
        assert (REPO_ROOT / "scripts" / "migrations" / name).is_file(), (
            f"standup lists a seeder that does not exist: {name}")
    # ...and the corpus is exactly what a stand-up runs, not what the directory holds.
    corpus = (REPO_ROOT / "scripts" / "seed_its_config.py").read_text(encoding="utf-8")
    for name in standup.SEEDER_STAGE_SCRIPTS:
        corpus += (REPO_ROOT / "scripts" / "migrations" / name).read_text(encoding="utf-8")
    # Non-seeder stages that legitimately create VC-03 rows: the box-roots paste
    # and the docs-index self-record (both standup stages, different scripts).
    for extra in ("build_box_roots.py", "build_docs_index_sheet.py"):
        p = REPO_ROOT / "scripts" / "migrations" / extra
        if p.is_file():
            corpus += p.read_text(encoding="utf-8")
    corpus += (REPO_ROOT / "scripts" / "migrations" / "standup.py").read_text(encoding="utf-8")
    unseeded = {s for s in settings if s not in corpus}
    assert not unseeded, (
        f"VC-03 config rows no stand-up-run seeder creates: {sorted(unseeded)}")


def test_restore_sheet_targets_are_valid_constants() -> None:
    text = (REPO_ROOT / "shared" / "sheet_ids.py").read_text(encoding="utf-8")
    for _ws, _sheet, constant in standup.RESTORE_SHEETS:
        assert re.search(rf"^{constant}\s*=", text, re.MULTILINE), constant


# ---- standup: --skip-restore-sheet selector (jobless reference restore) ----


def test_skip_restore_sheet_accepts_every_restore_sheet_name() -> None:
    """Validity guard: every RESTORE_SHEETS name is a legal selector value
    (the flag's domain IS the tuple's sheet-name column), dedupe preserves
    first occurrence, and the empty set stays empty."""
    names = [sheet for _ws, sheet, _const in standup.RESTORE_SHEETS]
    assert standup._validate_skip_restore_sheets(names) == tuple(names)
    assert standup._validate_skip_restore_sheets(
        [names[0], names[0]]) == (names[0],)
    assert standup._validate_skip_restore_sheets([]) == ()


def test_skip_restore_sheet_unknown_name_refused() -> None:
    """A typo'd --skip-restore-sheet refuses at startup and lists the valid
    names — silently restoring a sheet the operator meant to hold back is the
    failure mode."""
    with pytest.raises(standup.StageFailedError, match="unknown sheet name") as exc:
        standup._validate_skip_restore_sheets(["ITS_Active_Jobz"])
    msg = str(exc.value)
    assert "ITS_Active_Jobz" in msg
    for _ws, sheet, _const in standup.RESTORE_SHEETS:
        assert sheet in msg  # the refusal enumerates every valid name


def test_skip_restore_sheet_leaves_stage_graph_unchanged() -> None:
    """The selector filters INSIDE restore-rows; the stage list (membership
    and order — restore-rows included) is identical with and without it."""
    base = [n for n, _ in standup.build_stages(
        Path("/tmp/x"), skip_shares=False, skip_restore_sheets=())]
    skipped = [n for n, _ in standup.build_stages(
        Path("/tmp/x"), skip_shares=False,
        skip_restore_sheets=("ITS_Active_Jobs", "ITS_Active_Jobs_Progress"))]
    assert skipped == base
    assert "restore-rows" in skipped


def test_skip_restore_sheet_filters_only_named_sheet(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """The control bites BOTH ways: the skipped sheet gets ZERO add-rows posts
    (plus the [skip] line), while the non-skipped sheet still restores."""
    monkeypatch.setattr(standup, "RESTORE_SHEETS", (
        ("WS", "SheetA", "SHEET_A"), ("WS", "SheetB", "SHEET_B")))
    ids = types.SimpleNamespace(SHEET_A=111, SHEET_B=222)
    monkeypatch.setattr(standup, "_fresh_sheet_ids", lambda: ids)
    dump = {"columns": [{"title": "Name", "primary": True}],
            "rows": [{"Name": "row-1"}]}
    monkeypatch.setattr(standup, "_find_dump_sheet",
                        lambda _dir, _ws, _sheet: dump)
    from shared import smartsheet_client
    monkeypatch.setattr(smartsheet_client, "list_columns_with_options",
                        lambda _sid: [{"title": "Name", "id": 1}])
    monkeypatch.setattr(smartsheet_client, "get_rows", lambda _sid: [])
    posted: list[str] = []

    def _fake_post(method: str, url: str, **kwargs: Any) -> Any:
        posted.append(url)
        return _FakeResponse(200)

    monkeypatch.setattr(standup, "request_with_retry", _fake_post)
    monkeypatch.setattr(standup, "_reconcile_progress_job_ids",
                        lambda _ids: None)
    standup._stage_restore_rows(tmp_path, ("SheetA",))
    assert posted == [f"{standup.BASE}/sheets/222/rows"]
    out = capsys.readouterr().out
    assert "[skip] restore skipped by --skip-restore-sheet: 'SheetA'" in out
    assert "'SheetB': restored 1 row(s)" in out


def test_resolve_dump_dir_refusals(tmp_path: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """No dump -> refuse (never a silent fresh-tenant fallback); two dumps ->
    refuse auto-pick (a partial re-wipe's second dump is incomplete)."""
    monkeypatch.setattr(standup, "DUMP_ROOT", tmp_path)
    with pytest.raises(standup.StageFailedError, match="no prewipe"):
        standup._resolve_dump_dir(None)
    (tmp_path / "prewipe_A").mkdir()
    assert standup._resolve_dump_dir(None) == tmp_path / "prewipe_A"
    (tmp_path / "prewipe_B").mkdir()
    with pytest.raises(standup.StageFailedError, match="auto-picking is unsafe"):
        standup._resolve_dump_dir(None)
    # explicit --dump always wins, but must exist
    assert standup._resolve_dump_dir(tmp_path / "prewipe_B") == tmp_path / "prewipe_B"
    with pytest.raises(standup.StageFailedError, match="does not exist"):
        standup._resolve_dump_dir(tmp_path / "prewipe_missing")


# ---- _rest_retry: transient-vs-permanent + the wipe-dump fail-closed fix ----
#
# The 2026-07-23 review finding: dump_workspace classified 429/5xx/timeouts and
# the sheet_dump_truncated RuntimeError as "unreadable — skip and delete anyway"
# (a fail-open data-loss path; the dump is the sole row-restore source). These
# tests prove the new polarity BITES: transient exhaustion ABORTS the wipe,
# while the genuinely-permanent signatures (404 / errorCode 1006/1115 — the
# four broken ITS_Errors shells) still classify unreadable-and-continue.


class _FakeResponse:
    def __init__(self, status: int, body: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = str(self._body)

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise _http_error(f"{self.status_code} Client Error", self)


def _http_error(message: str, response: _FakeResponse) -> requests.HTTPError:
    """Typed shim — types-requests pins HTTPError.response to Response | None."""
    return requests.HTTPError(message, response=cast(Any, response))


def _scripted_requests(monkeypatch: pytest.MonkeyPatch,
                       responses: list[Any]) -> list[float]:
    """Feed request_with_retry a scripted response sequence; capture sleeps.

    Each entry is a _FakeResponse (returned) or an Exception (raised).
    """
    sleeps: list[float] = []
    calls = iter(responses)

    def _fake_request(method: str, url: str, **kwargs: Any) -> Any:
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(_rest_retry.requests, "request", _fake_request)
    monkeypatch.setattr(_rest_retry.time, "sleep", sleeps.append)
    return sleeps


def test_retry_429_then_success_honors_retry_after(
        monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = _scripted_requests(monkeypatch, [
        _FakeResponse(429, headers={"Retry-After": "7"}),
        _FakeResponse(200, {"data": []}),
    ])
    r = _rest_retry.request_with_retry("get", "https://x/sheets/1")
    assert r.status_code == 200
    assert sleeps == [7.0]


def test_retry_persistent_429_raises_after_budget(
        monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = _scripted_requests(monkeypatch, [_FakeResponse(429)] * 3)
    with pytest.raises(requests.HTTPError, match="429 transient"):
        _rest_retry.request_with_retry("get", "https://x/sheets/1", attempts=3)
    assert len(sleeps) == 2  # no sleep after the final attempt


def test_retry_permanent_404_raises_immediately_no_retries(
        monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = _scripted_requests(monkeypatch, [_FakeResponse(404)])
    with pytest.raises(requests.HTTPError):
        _rest_retry.request_with_retry("get", "https://x/sheets/1")
    assert sleeps == []  # a permanent 4xx must never burn the retry budget


def test_retry_timeout_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = _scripted_requests(monkeypatch, [
        requests.Timeout("read timed out"),
        _FakeResponse(200, {"ok": True}),
    ])
    r = _rest_retry.request_with_retry("get", "https://x/ws", backoff_seconds=1.5)
    assert r.json() == {"ok": True}
    assert sleeps == [1.5]


def test_retry_no_raise_mode_returns_permanent_but_raises_transient(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """raise_for_status=False hands a permanent 4xx back for caller inspection
    (the shares-POST already-shared WARN path) — but transient exhaustion STILL
    raises, so a 429 can never masquerade as a caller-visible non-200."""
    _scripted_requests(monkeypatch, [_FakeResponse(400, {"errorCode": 1025})])
    r = _rest_retry.request_with_retry(
        "post", "https://x/shares", raise_for_status=False)
    assert r.status_code == 400
    _scripted_requests(monkeypatch, [_FakeResponse(503)] * 2)
    with pytest.raises(requests.HTTPError, match="503 transient"):
        _rest_retry.request_with_retry(
            "post", "https://x/shares", attempts=2, raise_for_status=False)


def test_is_permanent_read_failure_classification() -> None:
    perm_404 = _http_error("404", _FakeResponse(404))
    perm_1115 = _http_error("400", _FakeResponse(400, {"errorCode": 1115}))
    perm_1006 = _http_error("400", _FakeResponse(400, {"errorCode": 1006}))
    transient_429 = _http_error("429", _FakeResponse(429))
    no_response = requests.ConnectionError("boom")
    assert _rest_retry.is_permanent_read_failure(perm_404)
    assert _rest_retry.is_permanent_read_failure(perm_1115)
    assert _rest_retry.is_permanent_read_failure(perm_1006)
    assert not _rest_retry.is_permanent_read_failure(transient_429)
    assert not _rest_retry.is_permanent_read_failure(no_response)


_WS_FIXTURE = {"id": 1, "name": "ITS — System"}


def _dumpable_workspace(monkeypatch: pytest.MonkeyPatch,
                        sheet_dump: Any) -> None:
    """Wire dump_workspace's collaborators so only _sheet_dump varies."""
    monkeypatch.setattr(wipe, "_workspace_tree", lambda ws_id: {
        "id": 1, "name": "ITS — System", "folders": [],
        "sheets": [{"id": 42, "name": "ITS_Config"}]})
    monkeypatch.setattr(wipe, "_workspace_shares", lambda ws_id: [])
    monkeypatch.setattr(wipe, "_sheet_dump", sheet_dump)


def test_dump_workspace_permanent_failure_classified_unreadable(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _raise_404(sheet_id: int) -> dict[str, Any]:
        raise _http_error("404 Client Error", _FakeResponse(404))

    _dumpable_workspace(monkeypatch, _raise_404)
    dumped, unreadable = wipe.dump_workspace(_WS_FIXTURE, tmp_path)
    assert dumped == 0
    assert len(unreadable) == 1 and "id=42" in unreadable[0]


def test_dump_workspace_transient_exhaustion_propagates(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An exhausted 429 must ABORT (guard 4), never classify-and-proceed."""
    def _raise_429(sheet_id: int) -> dict[str, Any]:
        raise _http_error("429 transient error", _FakeResponse(429))

    _dumpable_workspace(monkeypatch, _raise_429)
    with pytest.raises(requests.HTTPError, match="429"):
        wipe.dump_workspace(_WS_FIXTURE, tmp_path)


def test_dump_workspace_truncation_propagates(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """sheet_dump_truncated is a transient pagination artifact — it was
    misclassified as 'unreadable' before 2026-07-23; now it aborts."""
    def _raise_truncated(sheet_id: int) -> dict[str, Any]:
        raise RuntimeError("sheet_dump_truncated: sheet 42 totalRowCount=10 "
                           "but 7 rows fetched — refusing a lossy dump")

    _dumpable_workspace(monkeypatch, _raise_truncated)
    with pytest.raises(RuntimeError, match="sheet_dump_truncated"):
        wipe.dump_workspace(_WS_FIXTURE, tmp_path)


def test_wipe_main_aborts_on_transient_dump_failure(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """End-to-end polarity proof: a transient dump failure aborts the WHOLE
    wipe with nothing deleted (exit 1, no delete call reachable)."""
    monkeypatch.setattr(wipe, "require_daemons_down", lambda: None)
    monkeypatch.setattr(wipe, "_list_workspaces",
                        lambda: [dict(zip(("name", "id"),
                                          wipe.SMARTSHEET_WORKSPACE_ALLOWLIST[0],
                                          strict=True))])
    monkeypatch.setattr(wipe, "_box_root_items", lambda: [])
    monkeypatch.setattr(wipe, "_confirm_phrase", lambda: True)
    monkeypatch.setattr(wipe, "DUMP_ROOT", tmp_path)
    # past the phrase gate main() writes the stand-up ACT-fence marker; wipe is
    # a bare scripts/ module the conftest live-state sweep cannot see, so
    # redirect explicitly or the guard refuses the live ~/its/state write.
    monkeypatch.setattr(wipe, "STANDUP_MARKER_PATH", tmp_path / "marker.json")

    def _transient_dump(ws: dict[str, Any], dump_dir: Path) -> tuple[int, list[str]]:
        raise _http_error("503 transient error", _FakeResponse(503))

    monkeypatch.setattr(wipe, "dump_workspace", _transient_dump)

    def _boom(*a: Any, **k: Any) -> None:
        raise AssertionError("delete reached despite a failed dump")

    monkeypatch.setattr(wipe, "_delete_workspace", _boom)
    monkeypatch.setattr(wipe, "_delete_box_folder", _boom)
    monkeypatch.setattr(sys, "argv", ["wipe_tenant.py", "--commit"])
    assert wipe.main() == 1


# ---- standup: non-interactive contract + streamed output + run-state -------
#
# 2026-07-23 review items 2/3/4: the blind `input="y\n"*8` feed would silently
# confirm ANY prompt a builder grows later (including a destructive one); child
# output block-buffered illegibly (the mid-run PYTHONUNBUFFERED fix lived only
# in the operator's shell); and 5 resume points meant 5 hand-typed --start-at
# values. These tests prove the replacement contract BITES.


def _child_script(tmp_path: Path, name: str, body: str) -> str:
    (tmp_path / name).write_text(body, encoding="utf-8")
    return name


def test_run_script_streams_prefixed_output(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(standup, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(standup, "_current_stage", "tstage")
    rel = _child_script(tmp_path, "child.py", "print('hello')\nprint('world')\n")
    standup._run_script(rel)
    out = capsys.readouterr().out
    assert "[tstage/child] hello" in out
    assert "[tstage/child] world" in out


def test_run_script_sets_contract_env(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(standup, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(standup, "_current_stage", "env")
    rel = _child_script(
        tmp_path, "env_child.py",
        "import os\nprint(os.environ.get('STANDUP_NONINTERACTIVE'),"
        " os.environ.get('PYTHONUNBUFFERED'))\n")
    standup._run_script(rel)
    assert "[env/env_child] 1 1" in capsys.readouterr().out


def test_run_script_unexpected_prompt_fails_loudly(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """THE contract's teeth: a brand-new prompt in a child (here, a synthetic
    destructive one) hits the CLOSED stdin, raises EOFError, and FAILS the
    stage — under the old blind y-feed it would have been silently confirmed."""
    monkeypatch.setattr(standup, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(standup, "_current_stage", "prompt")
    rel = _child_script(
        tmp_path, "prompt_child.py",
        "answer = input('Delete conflicting sheet? [y/N] ')\n"
        "print('CONFIRMED', answer)\n")
    with pytest.raises(standup.StageFailedError, match="exited"):
        standup._run_script(rel)
    assert "CONFIRMED" not in capsys.readouterr().out


def test_run_script_tees_to_run_log(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(standup, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(standup, "_current_stage", "tee")
    monkeypatch.setattr(standup, "_run_log_path", tmp_path / "run.log")
    rel = _child_script(tmp_path, "tee_child.py", "print('logged-line')\n")
    standup._run_script(rel)
    assert "logged-line" in (tmp_path / "run.log").read_text(encoding="utf-8")


def test_confirm_seams_auto_approve_only_under_env(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Each of the six builder gates auto-approves ONLY under
    STANDUP_NONINTERACTIVE=1 and NEVER touches stdin while doing so (input is
    booby-trapped); without the env var the prompt remains the control."""
    import build_box_roots as bbr
    import build_safety_portal_workspace as bspw
    import build_system_sheets as bss
    import build_system_workspace as bsw
    scripts_dir = REPO_ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import seed_its_config as sic

    def _boom(*a: Any, **k: Any) -> str:
        raise AssertionError("stdin touched under STANDUP_NONINTERACTIVE")

    monkeypatch.setenv("STANDUP_NONINTERACTIVE", "1")
    monkeypatch.setattr("builtins.input", _boom)
    assert bsw._confirm("x") is True
    assert bss._confirm("x") is True
    assert legacy._confirm("x") is True
    assert sic._confirm("x") is True
    assert bbr._confirm_live_writes(["A"], "its@example.com") is True
    gate = bspw.LiveWriteGate(dry_run=False)
    assert gate.allow("create X") is True
    # dry-run stays dry even under the env var — auto-approve must never
    # convert a dry run into live writes.
    dry_gate = bspw.LiveWriteGate(dry_run=True)
    assert dry_gate.allow("create X") is False

    # Without the env var the prompt is the control again.
    monkeypatch.delenv("STANDUP_NONINTERACTIVE")
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    assert bsw._confirm("x") is True
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    assert bsw._confirm("x") is False
    assert sic._confirm("x") is False


def test_resume_derives_first_incomplete_stage(tmp_path: Path) -> None:
    names = ["a", "b", "c", "d"]
    standup._write_state({
        "run_id": "r1", "status": "failed",
        "flags": {"no_restore": False, "skip_shares": False,
                  "dump_dir": str(tmp_path)},
        "completed": ["a", "b"], "stage_names": names, "stages": {},
    }, tmp_path)
    assert standup._resume_start_stage(
        tmp_path, no_restore=False, skip_shares=False,
            no_run_branch=False, stage_names=names) == "c"


def test_resume_refusals(tmp_path: Path) -> None:
    names = ["a", "b"]
    # no state file
    with pytest.raises(standup.StageFailedError, match="no run state"):
        standup._resume_start_stage(
            tmp_path, no_restore=False, skip_shares=False,
            no_run_branch=False, stage_names=names)
    # completed run
    standup._write_state({
        "status": "complete",
        "flags": {"no_restore": False, "skip_shares": False},
        "completed": names,
    }, tmp_path)
    with pytest.raises(standup.StageFailedError, match="COMPLETE"):
        standup._resume_start_stage(
            tmp_path, no_restore=False, skip_shares=False,
            no_run_branch=False, stage_names=names)
    # conflicting flags (recorded skip_shares=False, supplied True)
    standup._write_state({
        "status": "failed",
        "flags": {"no_restore": False, "skip_shares": False},
        "completed": ["a"],
    }, tmp_path)
    with pytest.raises(standup.StageFailedError, match="conflict"):
        standup._resume_start_stage(
            tmp_path, no_restore=False, skip_shares=True,
            no_run_branch=False, stage_names=names)


def test_resume_refuses_skip_restore_sheet_conflict(tmp_path: Path) -> None:
    """--resume with a different --skip-restore-sheet set REFUSES in BOTH
    directions (a different set produces different restore work); the same
    set in a different order is NOT a conflict (membership, not order); a
    pre-selector state file (flag never recorded) == [] resumes fine."""
    names = ["a", "b"]
    both = ["ITS_Active_Jobs", "ITS_Active_Jobs_Progress"]
    standup._write_state({
        "status": "failed",
        "flags": {"no_restore": False, "skip_shares": False,
                  "no_run_branch": False, "skip_restore_sheets": []},
        "completed": ["a"], "stage_names": names, "stages": {},
    }, tmp_path)
    with pytest.raises(standup.StageFailedError, match="conflict"):
        standup._resume_start_stage(
            tmp_path, no_restore=False, skip_shares=False, no_run_branch=False,
            skip_restore_sheets=both, stage_names=names)
    standup._write_state({
        "status": "failed",
        "flags": {"no_restore": False, "skip_shares": False,
                  "no_run_branch": False, "skip_restore_sheets": both},
        "completed": ["a"], "stage_names": names, "stages": {},
    }, tmp_path)
    with pytest.raises(standup.StageFailedError, match="conflict"):
        standup._resume_start_stage(
            tmp_path, no_restore=False, skip_shares=False, no_run_branch=False,
            skip_restore_sheets=[], stage_names=names)
    # same membership, reversed order -> no conflict, resumes at 'b'
    assert standup._resume_start_stage(
        tmp_path, no_restore=False, skip_shares=False, no_run_branch=False,
        skip_restore_sheets=list(reversed(both)), stage_names=names) == "b"
    # pre-selector state file (flag never recorded) == default [], no conflict
    standup._write_state({
        "status": "failed",
        "flags": {"no_restore": False, "skip_shares": False},
        "completed": ["a"], "stage_names": names, "stages": {},
    }, tmp_path)
    assert standup._resume_start_stage(
        tmp_path, no_restore=False, skip_shares=False, no_run_branch=False,
        skip_restore_sheets=[], stage_names=names) == "b"


def test_write_state_is_best_effort(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """A state-write failure WARNs and continues — bookkeeping must never kill
    an otherwise-healthy attended run."""
    def _oser(*a: Any, **k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(standup.state_io, "atomic_write_json", _oser)
    standup._write_state({"status": "running"}, tmp_path)  # must not raise
    assert "run_state_write_failed" in capsys.readouterr().out


# ---- standup finish + marker + dashboard exemption (PR: finish subcommand) --


def test_send_dispatch_labels_match_shipped_plists() -> None:
    """Parity teeth: every SEND_DISPATCH label must be a shipped plist (a
    renamed plist would silently drop from the dark-posture exclusion and a
    send daemon would load), and the set is exactly the five dispatch lanes."""
    shipped = {p.name.removesuffix(".plist")
               for p in (REPO_ROOT / "scripts" / "launchd").glob(
                   "org.solutionsmith.its.*.plist")}
    missing = standup.SEND_DISPATCH_LABELS - shipped
    assert not missing, f"SEND_DISPATCH label(s) with no shipped plist: {missing}"
    assert standup.SEND_DISPATCH_LABELS == {
        "org.solutionsmith.its.po-send",
        "org.solutionsmith.its.rfq-send",
        "org.solutionsmith.its.subcontract-send",
        "org.solutionsmith.its.weekly-send",
        "org.solutionsmith.its.progress-send",
    }
    assert standup.DASHBOARD_LABEL in shipped


def test_reload_fleet_dark_never_loads_send_or_dashboard(
        monkeypatch: pytest.MonkeyPatch) -> None:
    loads: list[str] = []

    def _record(plist: str) -> int:
        loads.append(plist)
        return 0

    monkeypatch.setattr(standup, "_install_load", _record)
    failures = standup._reload_fleet("dark")
    assert failures == []
    labels = {name.removesuffix(".plist") for name in loads}
    assert not labels & standup.SEND_DISPATCH_LABELS, (
        "dark posture loaded a SEND-DISPATCH plist — External-Send-Gate violation")
    assert standup.DASHBOARD_LABEL not in labels  # restarted last, not here
    assert len(labels) >= 10  # the working fleet actually loads


def test_reload_fleet_full_loads_send(monkeypatch: pytest.MonkeyPatch) -> None:
    loads: list[str] = []

    def _record(plist: str) -> int:
        loads.append(plist)
        return 0

    monkeypatch.setattr(standup, "_install_load", _record)
    standup._reload_fleet("full")
    labels = {name.removesuffix(".plist") for name in loads}
    assert standup.SEND_DISPATCH_LABELS <= labels
    assert standup.DASHBOARD_LABEL not in labels


def _boom_named(what: str) -> Any:
    def _inner(*a: Any, **k: Any) -> None:
        raise AssertionError(f"{what} reached — must not run in this mode")
    return _inner


def test_finish_verify_only_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """--verify-only runs only the read-only legs: no gate, no cleanup, no
    reload, no dashboard restart, no daemons-down requirement."""
    monkeypatch.setattr(standup, "_verify_git_clean", lambda: True)
    monkeypatch.setattr(standup, "_verify_regen_parity", lambda: True)
    monkeypatch.setattr(standup, "_await_heartbeats", lambda *a, **k: [])
    monkeypatch.setattr(standup, "_post_reload_error_sweep", lambda: 0)
    monkeypatch.setattr(standup, "_gate_report", lambda dump_dir: None)
    for seam in ("_require_daemons_down", "_state_cleanup", "_reload_fleet",
                 "_restart_dashboard", "_confirm", "_confirm_full_posture"):
        monkeypatch.setattr(standup, seam, _boom_named(seam))
    assert standup.finish_main(["--verify-only", "--no-restore"]) == 0


def test_finish_full_posture_requires_phrase(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A declined LOAD SEND DAEMONS phrase aborts BEFORE any cleanup/reload."""
    monkeypatch.setattr(standup, "_verify_git_clean", lambda: True)
    monkeypatch.setattr(standup, "_verify_regen_parity", lambda: True)
    monkeypatch.setattr(standup, "_require_daemons_down", lambda: True)
    monkeypatch.setattr(standup, "_confirm_full_posture", lambda: False)
    for seam in ("_state_cleanup", "_reload_fleet", "_restart_dashboard",
                 "_confirm"):
        monkeypatch.setattr(standup, seam, _boom_named(seam))
    assert standup.finish_main(["--posture", "full", "--no-restore"]) == 1


def test_finish_precondition_failures_abort(
        monkeypatch: pytest.MonkeyPatch) -> None:
    for seam in ("_require_daemons_down", "_state_cleanup", "_reload_fleet",
                 "_restart_dashboard", "_confirm", "_await_heartbeats",
                 "_post_reload_error_sweep", "_gate_report"):
        monkeypatch.setattr(standup, seam, _boom_named(seam))
    # dirty tree refuses first
    monkeypatch.setattr(standup, "_verify_git_clean", lambda: False)
    monkeypatch.setattr(standup, "_verify_regen_parity", _boom_named("regen"))
    assert standup.finish_main(["--no-restore"]) == 1
    # regen mismatch refuses second
    monkeypatch.setattr(standup, "_verify_git_clean", lambda: True)
    monkeypatch.setattr(standup, "_verify_regen_parity", lambda: False)
    assert standup.finish_main(["--no-restore"]) == 1


def test_dashboard_exempt_from_daemon_guards(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Both daemon-down guards EXEMPT the dashboard (read-only panels stay up;
    ACT verbs are marker-fenced, its#677) but still refuse on any other job."""
    dash = standup.DASHBOARD_LABEL
    monkeypatch.setattr(standup, "_loaded_its_labels", lambda: [dash])
    assert standup._require_daemons_down() is True
    monkeypatch.setattr(standup, "_loaded_its_labels",
                        lambda: [dash, "org.solutionsmith.its.portal-poll"])
    assert standup._require_daemons_down() is False

    monkeypatch.setattr(wipe, "_loaded_its_daemons", lambda: [dash])
    wipe.require_daemons_down()  # exempt -> no raise
    monkeypatch.setattr(wipe, "_loaded_its_daemons",
                        lambda: [dash, "org.solutionsmith.its.portal-poll"])
    with pytest.raises(wipe.WipeRefusedError):
        wipe.require_daemons_down()


def test_wipe_commit_sets_fence_marker_and_leaves_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wipe sets the ACT-fence marker BEFORE the dump and does NOT clear it
    — only a COMPLETED standup run clears the marker (PR #674 lifecycle: the
    wipe->rebuild window stays fenced end to end)."""
    marker = tmp_path / "standup_in_progress.json"
    monkeypatch.setattr(wipe, "STANDUP_MARKER_PATH", marker)
    monkeypatch.setattr(wipe, "require_daemons_down", lambda: None)
    monkeypatch.setattr(wipe, "_list_workspaces",
                        lambda: [dict(zip(("name", "id"),
                                          wipe.SMARTSHEET_WORKSPACE_ALLOWLIST[0],
                                          strict=True))])
    monkeypatch.setattr(wipe, "_box_root_items", lambda: [])
    monkeypatch.setattr(wipe, "_confirm_phrase", lambda: True)
    monkeypatch.setattr(wipe, "DUMP_ROOT", tmp_path / "dumps")
    (tmp_path / "dumps").mkdir()
    seen: dict[str, bool] = {}

    def _dump(ws: dict[str, Any], dump_dir: Path) -> tuple[int, list[str]]:
        seen["marker_during_dump"] = marker.is_file()
        return 1, []

    def _delete(ws_id: int) -> None:
        seen["marker_during_delete"] = marker.is_file()

    monkeypatch.setattr(wipe, "dump_workspace", _dump)
    monkeypatch.setattr(wipe, "_delete_workspace", _delete)
    monkeypatch.setattr(wipe, "_delete_box_folder", _boom_named("box delete"))
    monkeypatch.setattr(sys, "argv", ["wipe_tenant.py", "--commit"])
    assert wipe.main() == 0
    assert seen == {"marker_during_dump": True, "marker_during_delete": True}
    assert marker.is_file()  # NOT cleared — standup completion owns the clear


def test_gate_report_shows_dump_vs_live(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    ws_dir = tmp_path / "smartsheet" / "ITS — System"
    ws_dir.mkdir(parents=True)
    (ws_dir / "cfg__1.sheet.json").write_text(json.dumps({
        "name": "ITS_Config",
        "columns": [], "rows": [
            {"Setting": "x.polling_enabled", "Workstream": "x", "Value": "true"},
        ]}), encoding="utf-8")
    live_rows = [
        {"Setting": "x.polling_enabled", "Workstream": "x", "Value": "false",
         "Description": "Do NOT set true until the rider merges."},
        {"Setting": "x.worker_base_url", "Workstream": "x", "Value": "https://x"},
    ]
    import shared.smartsheet_client as sc
    monkeypatch.setattr(sc, "get_rows", lambda sheet_id: live_rows)
    standup._gate_report(tmp_path)
    out = capsys.readouterr().out
    assert "x.polling_enabled [x]" in out
    assert "dump='true'  live='false'" in out
    assert "DIFFERS from pre-wipe" in out
    assert "Do NOT set true until the rider merges." in out
    assert "READ-ONLY" in out
    assert "x.worker_base_url" not in out  # non-gate rows stay out of the report


# ---- standup: run-branch mode (checkpoints on a per-run git branch) ---------


def _init_repo(tmp_path: Path) -> Path:
    """A real throwaway git repo — the run-branch ops are git-semantics-bearing
    (pathspec excludes, dirty detection), so mocks would prove nothing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True,
                   capture_output=True)
    return repo


def test_start_run_branch_refuses_dirty_tree_then_creates(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(standup, "REPO_ROOT", repo)
    (repo / "tracked.py").write_text("x = 2\n", encoding="utf-8")
    with pytest.raises(standup.StageFailedError, match="DIRTY"):
        standup._start_run_branch("r1")
    # clean tree -> branch created and checked out
    subprocess.run(["git", "checkout", "-q", "--", "tracked.py"], cwd=repo,
                   check=True, capture_output=True)
    branch = standup._start_run_branch("r1")
    assert branch == "standup/run-r1"
    assert standup._current_branch() == branch


def test_commit_stage_checkpoint_commits_repo_files_but_never_logs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE pathspec teeth: the prewipe dumps + transcripts live under logs/
    (untracked, NOT gitignored — the live repo shows them as ??), and a naive
    `git add -A` would land multi-MB dump JSON on the run branch."""
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(standup, "REPO_ROOT", repo)
    branch = standup._start_run_branch("r2")
    (repo / "tracked.py").write_text("x = 3\n", encoding="utf-8")
    (repo / "logs").mkdir()
    (repo / "logs" / "prewipe_dump.json").write_text("{}", encoding="utf-8")
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                            capture_output=True, text=True).stdout.strip()
    standup._commit_stage_checkpoint("regen-system", "r2", branch)
    after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                           capture_output=True, text=True).stdout.strip()
    assert after != before  # a checkpoint commit landed
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                            check=True, capture_output=True, text=True).stdout
    assert "?? logs/" in status  # the dump was NOT committed
    assert "tracked.py" not in status  # the repo file WAS
    # clean pass -> no empty commit
    standup._commit_stage_checkpoint("noop-stage", "r2", branch)
    final = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                           capture_output=True, text=True).stdout.strip()
    assert final == after
    # run-branch off (None) -> untouched even when dirty
    (repo / "tracked.py").write_text("x = 4\n", encoding="utf-8")
    standup._commit_stage_checkpoint("any", "r2", None)
    assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_resume_run_branch_refusals(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(standup, "REPO_ROOT", repo)
    # recorded branch not checked out (HEAD is main) -> refuse
    with pytest.raises(standup.StageFailedError, match="not checked out"):
        standup._resume_run_branch("standup/run-old")
    # pre-run-branch state (None) -> WARN + continue without checkpoints
    monkeypatch.setattr(standup, "_sync_run_branch_with_main",
                        _boom_named("sync"))
    assert standup._resume_run_branch(None) is None
    assert "predates run-branch mode" in capsys.readouterr().out


def test_sync_with_main_conflict_stops_never_auto_resolves(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def _fake_git(*args: str) -> Any:
        calls.append(args)
        if args[0] == "fetch":
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "merge":
            return subprocess.CompletedProcess(args, 1, "CONFLICT", "")
        if args[:2] == ("diff", "--name-only"):
            return subprocess.CompletedProcess(args, 0, "shared/sheet_ids.py", "")
        raise AssertionError(f"unexpected git {args}")

    monkeypatch.setattr(standup, "_git", _fake_git)
    with pytest.raises(standup.StageFailedError, match="CONFLICTED"):
        standup._sync_run_branch_with_main()
    # the conflict surface names the files and no resolution command ran
    assert not any(a[0] in {"checkout", "reset", "commit"} for a in calls)


def test_start_run_branch_ignores_untracked_logs_dump(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P1 regression (2026-07-23 verify pass): the default restore flow REQUIRES
    an untracked-not-ignored prewipe dump under logs/migrations — the dirty
    gate must judge dirt with the same :(exclude)logs pathspec the checkpoints
    use, or run-branch mode self-disables at the canonical wipe->standup flow."""
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(standup, "REPO_ROOT", repo)
    dump = repo / "logs" / "migrations" / "prewipe_20260807T000000Z"
    dump.mkdir(parents=True)
    (dump / "dump.json").write_text("{}", encoding="utf-8")
    branch = standup._start_run_branch("r3")  # must NOT refuse
    assert standup._current_branch() == branch
    # real repo-file dirt beside the dump still refuses
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True,
                   capture_output=True)
    (repo / "tracked.py").write_text("x = 99\n", encoding="utf-8")
    with pytest.raises(standup.StageFailedError, match="DIRTY"):
        standup._start_run_branch("r4")


def test_resume_refuses_no_run_branch_flag_conflict(tmp_path: Path) -> None:
    """--resume --no-run-branch on a branch-mode run must REFUSE (it would
    silently drop the recorded branch + checkpoints), and vice versa; a
    pre-#687 state file (flag never recorded) resumes fine without the flag."""
    names = ["a", "b"]
    standup._write_state({
        "status": "failed",
        "flags": {"no_restore": False, "skip_shares": False,
                  "no_run_branch": False},
        "completed": ["a"], "stage_names": names, "stages": {},
    }, tmp_path)
    with pytest.raises(standup.StageFailedError, match="conflict"):
        standup._resume_start_stage(
            tmp_path, no_restore=False, skip_shares=False,
            no_run_branch=True, stage_names=names)
    standup._write_state({
        "status": "failed",
        "flags": {"no_restore": False, "skip_shares": False,
                  "no_run_branch": True},
        "completed": ["a"], "stage_names": names, "stages": {},
    }, tmp_path)
    with pytest.raises(standup.StageFailedError, match="conflict"):
        standup._resume_start_stage(
            tmp_path, no_restore=False, skip_shares=False,
            no_run_branch=False, stage_names=names)
    # pre-#687 state (no_run_branch never recorded) == default False, no conflict
    standup._write_state({
        "status": "failed",
        "flags": {"no_restore": False, "skip_shares": False},
        "completed": ["a"], "stage_names": names, "stages": {},
    }, tmp_path)
    assert standup._resume_start_stage(
        tmp_path, no_restore=False, skip_shares=False,
        no_run_branch=False, stage_names=names) == "b"


def test_resume_corrupt_state_file_clean_refusal(tmp_path: Path) -> None:
    """A truncated/corrupt state file must exit via the polished [abort]
    StageFailedError refusal, never a raw JSONDecodeError traceback."""
    (tmp_path / "standup_state.json").write_text('{"status": "fail',
                                                 encoding="utf-8")
    with pytest.raises(standup.StageFailedError, match="UNREADABLE"):
        standup._resume_start_stage(
            tmp_path, no_restore=False, skip_shares=False,
            no_run_branch=False, stage_names=["a"])


def test_sync_with_main_non_conflict_failure_is_not_labeled_conflicted(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A merge that fails BEFORE it begins (rc!=0, no unmerged paths — e.g.
    local changes the merge would overwrite) must not claim CONFLICTED with an
    empty file list; it names the real refusal and quotes git."""

    def _fake_git(*args: str) -> Any:
        if args[0] == "fetch":
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "merge":
            return subprocess.CompletedProcess(
                args, 1, "", "error: Your local changes to the following files "
                "would be overwritten by merge:\n  shared/sheet_ids.py")
        if args[:2] == ("diff", "--name-only"):
            return subprocess.CompletedProcess(args, 0, "", "")  # no unmerged
        raise AssertionError(f"unexpected git {args}")

    monkeypatch.setattr(standup, "_git", _fake_git)
    with pytest.raises(standup.StageFailedError,
                       match="FAILED before it began") as exc:
        standup._sync_run_branch_with_main()
    assert "CONFLICTED" not in str(exc.value)
    assert "would be overwritten" in str(exc.value)


def test_finish_refuses_when_daemons_still_loaded(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """P2 tooth (2026-07-23 verify pass): the third finish precondition —
    daemons still loaded -> rc 1 BEFORE any cleanup/reload seam runs."""
    monkeypatch.setattr(standup, "_verify_git_clean", lambda: True)
    monkeypatch.setattr(standup, "_verify_regen_parity", lambda: True)
    monkeypatch.setattr(standup, "_require_daemons_down", lambda: False)
    for seam in ("_state_cleanup", "_reload_fleet", "_restart_dashboard",
                 "_confirm", "_confirm_full_posture", "_await_heartbeats",
                 "_post_reload_error_sweep", "_gate_report"):
        monkeypatch.setattr(standup, seam, _boom_named(seam))
    assert standup.finish_main(["--no-restore"]) == 1


def test_finish_master_confirm_decline_runs_nothing(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Declining the master finish confirm exits 0 with every mutating seam
    untouched (the operator said no; nothing happened)."""
    monkeypatch.setattr(standup, "_verify_git_clean", lambda: True)
    monkeypatch.setattr(standup, "_verify_regen_parity", lambda: True)
    monkeypatch.setattr(standup, "_require_daemons_down", lambda: True)
    monkeypatch.setattr(standup, "_confirm", lambda *a, **k: False)
    for seam in ("_state_cleanup", "_reload_fleet", "_restart_dashboard",
                 "_await_heartbeats", "_post_reload_error_sweep",
                 "_gate_report", "_confirm_full_posture"):
        monkeypatch.setattr(standup, seam, _boom_named(seam))
    assert standup.finish_main(["--no-restore"]) == 0


def test_finish_notes_leftover_fence_marker(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """A clean finish over a leftover ACT-fence marker (abandoned run) prints
    the fenced-until-age-out note; without the marker it stays silent. The
    note never changes the exit code."""
    monkeypatch.setattr(standup, "_verify_git_clean", lambda: True)
    monkeypatch.setattr(standup, "_verify_regen_parity", lambda: True)
    monkeypatch.setattr(standup, "_await_heartbeats", lambda *a, **k: [])
    monkeypatch.setattr(standup, "_post_reload_error_sweep", lambda: 0)
    monkeypatch.setattr(standup, "_gate_report", lambda dump_dir: None)
    assert standup.finish_main(["--verify-only", "--no-restore"]) == 0
    assert "fence marker" not in capsys.readouterr().out
    standup.STANDUP_MARKER_PATH.write_text("{}", encoding="utf-8")
    assert standup.finish_main(["--verify-only", "--no-restore"]) == 0
    assert "fence marker still present" in capsys.readouterr().out
