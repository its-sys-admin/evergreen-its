"""Track 6 — `field_ops/job_archive.py`.

The behaviours under test are the ones that make a PARTIAL archive safe rather than a wedge:
per-container fencing, move-BEFORE-rename ordering, the ADMIN pre-flight refusing loudly, and the
resume probe keying off the recorded folder id instead of a re-creatable name.

Both SYSTEMS are exercised here, and the asymmetry between them is itself under test: Smartsheet
moves then renames (two calls, a resumable window), Box does both in one. A test that asserted the
same shape on both sides would be asserting a bug.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# sys.path-driven imports (scripts/ has no __init__.py) — mirrors tests/test_production_repoint.py
# and tests/test_verify_cutover.py. Both dirs must be on the path as TOP-LEVEL roots: a
# `from scripts import verify_cutover` would make mypy see that file under two module names
# ("Source file found twice") and fail the blocking type-check.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = _REPO_ROOT / "scripts" / "migrations"
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _dir in (_MIGRATIONS_DIR, _SCRIPTS_DIR):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

from field_ops import job_archive  # noqa: E402
from shared import box_client, sheet_ids, smartsheet_client  # noqa: E402


@pytest.fixture
def _seams(mocker):
    """Patch every external edge the module touches. Nothing here reaches a live API."""
    return {
        "find_ws": mocker.patch.object(
            smartsheet_client, "find_folder_by_name_in_workspace", return_value=None
        ),
        "find_folder": mocker.patch.object(
            smartsheet_client, "find_folder_by_name_in_folder", return_value=None
        ),
        "create_ws": mocker.patch.object(
            smartsheet_client, "create_folder_in_workspace", return_value=9000
        ),
        "move": mocker.patch.object(smartsheet_client, "move_folder_to_folder", return_value=None),
        # The un-archive destination for Safety/Progress: those sit directly under a
        # WORKSPACE, so restoring one needs the workspace variant, not the folder one.
        "move_ws": mocker.patch.object(
            smartsheet_client, "move_folder_to_workspace", return_value=None
        ),
        "rename": mocker.patch.object(smartsheet_client, "rename_folder", return_value=None),
        "name": mocker.patch.object(smartsheet_client, "get_folder_name", return_value="Coker"),
        "access": mocker.patch.object(
            smartsheet_client, "get_workspace_access_level", return_value="ADMIN"
        ),
        "log": mocker.patch.object(job_archive.error_log, "log"),
        # --- the Box edges -------------------------------------------------------------
        # `get_setting` MUST be patched even in the Smartsheet-only tests. It is how the Box
        # slots resolve their roots, so leaving it live means every archive_job() call in this
        # file makes a real Smartsheet request that happens to fail into the per-container
        # fence — green, slow, and network-dependent.
        "setting": mocker.patch.object(
            smartsheet_client, "get_setting",
            side_effect=lambda key, **_: {
                job_archive.CFG_BOX_ARCHIVE_ROOT: "900",
                "safety_reports.box.portal_root_folder_id": "100",
                "progress_reports.box.portal_root_folder_id": "200",
                "po_materials.box.portal_root_folder_id": "300",
            }.get(key),
        ),
        "box_find": mocker.patch.object(box_client, "find_child_folder", return_value=None),
        "box_ensure": mocker.patch.object(
            box_client, "get_or_create_folder", return_value="950"
        ),
        "box_move": mocker.patch.object(box_client, "move_folder", return_value={}),
    }


def _job(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "job_id": "JOB-000017",
        "project_name": "Coker",
        "archive_folder_key": "Coker",
        "archive_direction": "archive",
        "archive_state": "requested",
        "archive_attempts": 0,
    }
    base.update(over)
    return base


# ---- the slot table ------------------------------------------------------


def test_slots_cover_seven_containers_four_smartsheet_three_box():
    # SEVEN, not eleven: the PO lane's own Box root (2026-08-11) carries PO PDFs + RFQs/ +
    # Vendor Quotes/ in ONE per-job folder, and the safety root still carries the subcontract
    # files + materials manifests. A future reader "fixing" the apparent asymmetry by adding
    # rfq/vendor-quote/subcontract Box slots would double-move those subtrees.
    assert len(job_archive.SLOTS) == 7
    assert sum(1 for s in job_archive.SLOTS if s.system == "smartsheet") == 4
    assert sum(1 for s in job_archive.SLOTS if s.system == "box") == 3


def test_every_smartsheet_slot_names_exactly_one_source_parent():
    # Safety/Progress sit directly under a WORKSPACE; PO/Subcontracts under a Jobs FOLDER.
    # A slot with neither (or both) would resolve against the wrong tree.
    for slot in job_archive.SLOTS:
        if slot.system != "smartsheet":
            continue
        assert (slot.workspace is None) != (slot.parent_folder is None), slot.key


def test_slot_keys_are_unique():
    # The keys index the D1 container report; a duplicate would make one container's outcome
    # silently overwrite another's in the operator's view.
    keys = [s.key for s in job_archive.SLOTS]
    assert len(keys) == len(set(keys))


# ---- the ADMIN pre-flight ------------------------------------------------


def test_preflight_passes_when_the_identity_is_admin_everywhere(_seams):
    assert job_archive.verify_archive_capability() is True
    # All five workspaces probed: Archive + the four sources.
    assert _seams["access"].call_count == 5


def test_preflight_refuses_loudly_on_insufficient_access(_seams):
    # Without this, the shortfall surfaces as a 403 only AFTER the operator pressed Archive, and
    # only for whichever containers got that far — a half-archived job caused purely by sharing.
    _seams["access"].return_value = "EDITOR"

    assert job_archive.verify_archive_capability() is False

    warn = [c for c in _seams["log"].call_args_list
            if c.kwargs.get("error_code") == "archive_preflight_not_admin"]
    assert warn, "an insufficient access level must never be a silent skip"


def test_preflight_refuses_on_a_probe_error_rather_than_assuming_ok(_seams):
    _seams["access"].side_effect = smartsheet_client.SmartsheetError("boom")

    assert job_archive.verify_archive_capability() is False

    assert any(c.kwargs.get("error_code") == "archive_preflight_unreadable"
               for c in _seams["log"].call_args_list)


# ---- the archive destination --------------------------------------------


def test_ensure_archive_job_folder_reuses_an_existing_one(_seams):
    _seams["find_ws"].return_value = 4242
    assert job_archive.ensure_archive_job_folder("Coker") == 4242
    _seams["create_ws"].assert_not_called()


def test_ensure_archive_job_folder_creates_on_miss(_seams):
    _seams["find_ws"].side_effect = [None, 9000]  # pre-find miss, post-find confirms our create
    assert job_archive.ensure_archive_job_folder("Coker") == 9000
    _seams["create_ws"].assert_called_once_with(sheet_ids.WORKSPACE_ARCHIVE, "Coker")


def test_ensure_archive_job_folder_adopts_the_race_winner_and_warns(_seams):
    # Smartsheet does not enforce folder-name uniqueness, so two creators can both pass the find.
    _seams["find_ws"].side_effect = [None, 7777]  # someone else's folder won
    _seams["create_ws"].return_value = 9000

    assert job_archive.ensure_archive_job_folder("Coker") == 7777  # first match adopted

    assert any(c.kwargs.get("error_code") == "archive_job_folder_duplicate"
               for c in _seams["log"].call_args_list)


# ---- moving one container ------------------------------------------------


def test_container_move_happens_before_the_rename(_seams):
    """Ordering is load-bearing, not cosmetic.

    Renaming first would hide the folder from week_sheet / hours_log / job_sheet, which all
    find-or-CREATE by job name — so the next filing would grow a fresh empty folder beside it and
    the archive would go on to move the wrong tree.
    """
    calls: list[str] = []
    _seams["find_ws"].return_value = 555
    _seams["move"].side_effect = lambda *a, **k: calls.append("move")
    _seams["rename"].side_effect = lambda *a, **k: calls.append("rename")

    slot = next(s for s in job_archive.SLOTS if s.key == "smartsheet:safety")
    res = job_archive.archive_smartsheet_container(slot, "Coker", 9000)

    assert calls == ["move", "rename"]
    assert res.moved is True


def test_container_rename_is_skipped_when_the_label_is_already_right(_seams):
    # The resume path: a crash between move and rename leaves a moved-but-unrenamed folder, so the
    # probe reads the RECORDED id's current name rather than searching for a name in the source
    # (which the live creators re-grow the moment anything is filed).
    _seams["find_ws"].return_value = 555
    _seams["name"].return_value = "Safety"  # a prior cycle already renamed it

    slot = next(s for s in job_archive.SLOTS if s.key == "smartsheet:safety")
    job_archive.archive_smartsheet_container(slot, "Coker", 9000)

    _seams["rename"].assert_not_called()


def test_absent_container_counts_as_moved_with_a_note(_seams):
    # "Nothing to move" is success, not failure: a job that never produced anything in a workstream
    # must not hold its archive at 'partial' forever.
    _seams["find_ws"].return_value = None

    slot = next(s for s in job_archive.SLOTS if s.key == "smartsheet:safety")
    res = job_archive.archive_smartsheet_container(slot, "Coker", 9000)

    assert res.moved is True and res.note == "nothing to move"
    _seams["move"].assert_not_called()


# ---- the Box leg --------------------------------------------------------


def _slot(key: str) -> job_archive.ArchiveSlot:
    return next(s for s in job_archive.SLOTS if s.key == key)


def test_every_box_slot_carries_its_root_config_coordinates():
    # A Box root is a tenant-specific id read from ITS_Config at runtime, not a sheet_ids
    # constant — so a Box slot without both coordinates cannot resolve a source at all.
    for slot in job_archive.SLOTS:
        if slot.system == "box":
            assert slot.box_root_key and slot.box_root_workstream, slot.key
        else:
            assert slot.box_root_key is None and slot.box_root_workstream is None, slot.key


def test_box_root_keys_match_their_owning_modules():
    """The parity tooth behind job_archive's deliberate string literal.

    `CFG_BOX_PROGRESS_ROOT` is written out rather than imported, to keep `form_pdf` /
    `generate_core` / the network-capable `portal_client` out of a leaf relocation module's
    import graph. The heavy import happens HERE instead, so a rename in the owning module
    RED-lights rather than silently pointing the archive at a key nobody writes.
    """
    from po_materials import po_naming
    from progress_reports import progress_weekly_generate
    from safety_reports import safety_naming

    assert job_archive.CFG_BOX_PROGRESS_ROOT == progress_weekly_generate.CFG_BOX_PORTAL_ROOT
    assert _slot("box:safety").box_root_key == safety_naming.CFG_BOX_PORTAL_ROOT
    assert _slot("box:progress").box_root_key == job_archive.CFG_BOX_PROGRESS_ROOT
    # The PO slot's key is IMPORTED (po_naming is a leaf module — no literal needed), but the
    # parity assertion stays: it pins the slot to the key the three lane daemons actually read.
    assert _slot("box:purchase_orders").box_root_key == po_naming.CFG_BOX_PORTAL_ROOT
    assert _slot("box:purchase_orders").box_root_workstream == po_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM


def test_the_box_leg_moves_and_renames_in_a_single_call(_seams):
    """The asymmetry with Smartsheet, asserted rather than assumed.

    Box's PUT carries parent AND name, so there is no moved-but-unrenamed window and nothing to
    resume. If this ever becomes two calls, the archive grows a crash window that the Box side
    has no probe for — the Smartsheet resume logic does not apply here.
    """
    _seams["box_find"].return_value = "777"

    res = job_archive.archive_box_container(_slot("box:safety"), "Coker", "950")

    assert res.moved is True
    _seams["box_move"].assert_called_once_with("777", "950", new_name="Safety")
    # No separate rename primitive is even reachable — box_client is MOVE-ONLY by construction.
    assert not hasattr(box_client, "rename_folder")


def test_the_box_source_lookup_never_creates(_seams):
    """Creating the source would manufacture the very folder whose absence means 'nothing to
    move', and the archive would then relocate a brand-new empty container while the real
    documents stayed in the live tree."""
    _seams["box_find"].return_value = None

    res = job_archive.archive_box_container(_slot("box:progress"), "Coker", "950")

    assert res.moved is True and res.note == "nothing to move"
    _seams["box_find"].assert_called_once_with("200", "Coker")
    _seams["box_move"].assert_not_called()
    # The DESTINATION is find-or-create; the SOURCE never is.
    _seams["box_ensure"].assert_not_called()


def test_an_unset_box_root_fails_the_container_instead_of_looking_clean(_seams):
    """The Box twin of the empty-folder-key trap, and just as quiet.

    An unset root makes every find-by-name match nothing — byte-identical to a clean tree. A
    module that returned None here would report both Box containers relocated and leave the
    operator's documents where they were.
    """
    _seams["setting"].side_effect = lambda key, **_: None

    results = job_archive.archive_job(_job())

    box = [r for r in results if r.key.startswith("box:")]
    assert len(box) == 3
    assert all(r.moved is False for r in box)
    assert job_archive.state_from_results(results) == "partial"
    _seams["box_move"].assert_not_called()
    assert any(c.kwargs.get("error_code") == "archive_container_failed"
               for c in _seams["log"].call_args_list)


def test_a_box_failure_never_blocks_the_smartsheet_containers(_seams):
    # The two systems are independently fenced: a Box outage must still let the four Smartsheet
    # folders relocate, and the job reports 4-of-7 `partial` rather than failing whole.
    _seams["find_ws"].return_value = 555
    _seams["find_folder"].return_value = 556
    _seams["box_ensure"].side_effect = box_client.BoxError("box down")

    results = job_archive.archive_job(_job())

    assert [r.moved for r in results if r.key.startswith("smartsheet:")] == [True] * 4
    assert [r.moved for r in results if r.key.startswith("box:")] == [False, False, False]
    assert job_archive.state_from_results(results) == "partial"


def test_a_smartsheet_failure_never_blocks_the_box_containers(_seams):
    # The converse. The destinations resolve independently precisely so one system's outage
    # cannot strand the other's containers.
    _seams["find_ws"].side_effect = smartsheet_client.SmartsheetError("smartsheet down")
    _seams["box_find"].return_value = "777"

    results = job_archive.archive_job(_job())

    assert [r.moved for r in results if r.key.startswith("box:")] == [True, True, True]
    assert all(r.moved is False for r in results if r.key.startswith("smartsheet:"))
    assert _seams["box_move"].call_count == 3


def test_the_box_destination_is_resolved_once_per_job(_seams):
    # Both Box containers land in the SAME `ITS Archive/<Job>/` folder. Resolving per container
    # is an extra find-or-create round trip and a second chance to lose the create race.
    _seams["box_find"].return_value = "777"

    job_archive.archive_job(_job())

    _seams["box_ensure"].assert_called_once_with("900", "Coker")


def test_the_box_leg_moves_three_containers_not_six(_seams):
    """Seven-not-eleven, asserted at the call level rather than only in the slot count.

    The PO root's per-job folder carries `RFQs/` + `Vendor Quotes/` along with the PO PDFs, and
    the shared safety root carries the subcontract files + materials manifests. A future reader
    "fixing" the apparent asymmetry by adding rfq/vendor-quote/subcontract Box slots would move
    those trees TWICE — once inside their parent folder and once on their own — which is exactly
    the collision `move_folder` refuses to merge.
    """
    _seams["box_find"].return_value = "777"

    job_archive.archive_job(_job())

    assert _seams["box_move"].call_count == 3
    assert [c.kwargs["new_name"] for c in _seams["box_move"].call_args_list] == [
        "Safety", "Progress", "Purchase Orders",
    ]


# ---- archiving a whole job ----------------------------------------------


def test_one_container_failure_never_blocks_the_others(_seams):
    """The defining property of a resumable partial.

    The old path moved four sheets in a loop with a single outer fence; a failure part-way left no
    record of what HAD moved. Here each container is fenced and reported independently.
    """
    _seams["find_ws"].return_value = 555
    _seams["find_folder"].return_value = 556
    # Fail only the second smartsheet container.
    _seams["move"].side_effect = [None, smartsheet_client.SmartsheetError("boom"), None, None]

    results = job_archive.archive_job(_job())

    smartsheet = [r for r in results if r.key.startswith("smartsheet:")]
    assert len(smartsheet) == 4
    assert sum(1 for r in smartsheet if r.moved) == 3  # the other three still ran
    assert any(c.kwargs.get("error_code") == "archive_container_failed"
               for c in _seams["log"].call_args_list)


def test_a_container_failure_never_raises(_seams):
    _seams["find_ws"].return_value = 555
    _seams["move"].side_effect = RuntimeError("unexpected")
    job_archive.archive_job(_job())  # must not raise — the caller reports, it does not crash


def test_the_failure_warn_names_the_job_system_and_container(_seams):
    # The old WARN named only a sheet. With seven containers across two systems an operator must be
    # able to tell WHICH folder in WHICH system is stuck straight from ITS_Errors.
    _seams["find_ws"].return_value = 555
    _seams["move"].side_effect = smartsheet_client.SmartsheetError("boom")

    job_archive.archive_job(_job(job_id="JOB-000042"))

    msg = next(c.args[2] for c in _seams["log"].call_args_list
               if c.kwargs.get("error_code") == "archive_container_failed")
    assert "JOB-000042" in msg
    assert "smartsheet" in msg
    assert "Safety" in msg


def test_an_empty_folder_key_refuses_loudly_instead_of_looking_clean(_seams):
    """The subtlest failure this module can have.

    An empty key makes every find-by-name match nothing, so a naive implementation would report
    six 'nothing to move' successes and mark the archive COMPLETE without touching a thing.
    """
    results = job_archive.archive_job(_job(archive_folder_key=""))

    assert all(r.moved is False for r in results)
    assert any(c.kwargs.get("error_code") == "archive_folder_key_missing"
               for c in _seams["log"].call_args_list)
    _seams["move"].assert_not_called()


def test_the_archive_destination_folder_is_resolved_once_per_job(_seams):
    # Four smartsheet containers, one destination — resolving per container would be four extra
    # round trips and four chances to lose the create race.
    _seams["find_ws"].side_effect = [None, 9000] + [555] * 8
    job_archive.archive_job(_job())
    assert _seams["create_ws"].call_count == 1


# ---- collapsing results to the operator-visible state --------------------


@pytest.mark.parametrize(
    "moved_flags, expected",
    [
        ([True] * 6, "complete"),
        ([True, True, False, True, True, True], "partial"),
        ([False] * 6, "failed"),
    ],
)
def test_state_from_results(moved_flags, expected):
    # 'partial' is deliberately distinct from 'failed': an operator seeing "4 of 7 moved" needs to
    # know something DID move, because the repair differs from "nothing happened".
    results = [
        job_archive.ContainerResult(f"k{i}", f"L{i}", moved=flag)
        for i, flag in enumerate(moved_flags)
    ]
    assert job_archive.state_from_results(results) == expected


def test_folder_key_delegates_to_the_one_naming_rule():
    # Not a second copy: the Worker mirrors the SAME rule in TS, with parity asserted in
    # tests/test_job_archive_guard.py.
    from safety_reports import safety_naming

    for raw in ("Coker", "  Bradley 1  ", "A/B", "Bradley Solar"):
        assert job_archive.folder_key_for(raw) == safety_naming.job_folder_name(raw)


# ---- the archive Box root's registry fan-out ----------------------------
#
# A new ITS_Config row reconciles ALL its registries in the same PR (HOUSE_REFLEXES §1). These
# assert the surfaces rather than trusting a checklist, because "added the thing, forgot a
# registry" is the recurring miss and every one of these is silent when wrong.


def test_the_archive_root_key_is_enrolled_in_the_repoint_suffix_allowlist():
    """The highest-consequence surface, and the quietest failure on this page.

    `production_repoint` repoints only Setting names matching its allowlist and SKIPS the rest
    WITHOUT error. This key does not end in `.portal_root_folder_id` like its two siblings, so
    absent an explicit entry the cutover sweep reports success while the production tenant keeps
    a SANDBOX Box folder id — and the first archived job files a customer's closed-out documents
    into the mirror tenant.
    """
    import production_repoint as pr  # noqa: PLC0415 — sys.path is primed at module scope

    key = job_archive.CFG_BOX_ARCHIVE_ROOT
    assert key not in pr.ALLOWED_SETTINGS_EXACT
    assert any(key.endswith(suffix) for suffix in pr.ALLOWED_SETTING_SUFFIXES), (
        f"{key!r} matches no ALLOWED_SETTING_SUFFIXES entry — production_repoint would skip it "
        f"silently and production would keep the sandbox folder id"
    )


def test_the_archive_root_has_a_reviewed_repoint_map_row():
    # The allowlist only permits the row; the MAP is what actually carries it through the sweep.
    rows = json.loads((_MIGRATIONS_DIR / "production_repoint_map.json").read_text())["rows"]
    row = next(r for r in rows if r["setting"] == job_archive.CFG_BOX_ARCHIVE_ROOT)
    assert row["category"] == "D"
    assert row["workstream"] == job_archive.WORKSTREAM_FIELD_OPS
    # Section D resolves the id LIVE at commit from the folder NAME — a Box id is never typed
    # by hand into this file.
    assert row["resolve_box_root"] == "ITS Archive"
    assert row["from_mirror"] is None and row["to_production"] is None


def test_the_archive_root_is_built_seeded_and_verified():
    """The builder that creates the folder, the stand-up that seeds the row, and the cutover
    gate that proves it landed — one datum, three registries that each fail silently alone."""
    import build_box_roots as d4  # noqa: PLC0415 — sys.path is primed at module scope
    import standup  # noqa: PLC0415
    import verify_cutover  # noqa: PLC0415 — sys.path-driven; see the module-scope note

    key, workstream = job_archive.CFG_BOX_ARCHIVE_ROOT, job_archive.WORKSTREAM_FIELD_OPS

    built = {k: (n, w) for n, k, w in d4.ROOT_FOLDERS}
    assert built[key] == ("ITS Archive", workstream)
    # The stand-up seeds the row from the SAME (name, key, workstream) triple the builder
    # creates from; a drift between them seeds a row nothing writes.
    assert ("ITS Archive", key, workstream) in standup.BOX_ROOT_CONFIG_ROWS

    row = next(r for r in verify_cutover.CONFIG_ROWS
               if r.key == key and r.workstream == workstream)
    # `non_empty`, never a forced value: the id is tenant-specific and not ours to pin.
    assert row.requirement == "non_empty"


def test_the_archive_root_is_operator_editable_and_documented():
    # Without the dashboard row a Tier-2 operator has no sanctioned way to correct a bad paste;
    # without the dictionary entry the editor renders it with no purpose text.
    from operator_dashboard.act import registry  # noqa: PLC0415

    key = job_archive.CFG_BOX_ARCHIVE_ROOT
    # REGISTRY is keyed by the (Setting, Workstream) PAIR — the same pair get_setting matches on.
    assert (key, job_archive.WORKSTREAM_FIELD_OPS) in registry.REGISTRY

    defaults = json.loads(
        (_REPO_ROOT / "operator_dashboard" / "config_defaults.json").read_text()
    )
    documented = {(e["setting"], e["workstream"]) for e in defaults["keys"]}
    assert (key, job_archive.WORKSTREAM_FIELD_OPS) in documented, (
        "regenerate with scripts/generate_config_dictionary.py"
    )


def test_required_config_declares_the_roots_this_module_resolves():
    # The #336 observable-config ledger. Every key `_read_box_root` can be asked for is declared,
    # so the config dictionary lists `field_ops.job_archive` under each one's "Read by".
    declared = {(k.setting, k.workstream) for k in job_archive.REQUIRED_CONFIG}
    assert (job_archive.CFG_BOX_ARCHIVE_ROOT, job_archive.WORKSTREAM_FIELD_OPS) in declared
    for slot in job_archive.SLOTS:
        if slot.system == "box":
            assert (slot.box_root_key, slot.box_root_workstream) in declared


def test_the_source_root_declarations_carry_no_description():
    """The config dictionary is ONE global table keyed by Setting name, so a description here
    overwrites the owning workstream's prose for every reader. A first pass with one attached
    rewrote the shared safety root's entry — read by seven daemons — as "read here as an archive
    SOURCE", which is true of this module and useless to everyone else."""
    owned = job_archive.CFG_BOX_ARCHIVE_ROOT
    for declared in job_archive.REQUIRED_CONFIG:
        if declared.setting != owned:
            assert declared.description == "", declared.setting


# ---- the un-archive (restore) leg ---------------------------------------
#
# Never exercised live on either system. These assert the two properties that make the reverse
# direction safe rather than a duplicate-folder generator: the INVERTED Smartsheet call order, and
# a refusal to merge when the live tree has re-grown the job's folder.


def test_the_restore_renames_before_it_moves(_seams):
    """The order inverts relative to archiving, and both orders exist for the SAME reason.

    Every live path find-or-CREATEs by job name, so a folder sitting in the live tree under the
    wrong name is invisible to them and they grow a duplicate beside it. Archiving moves first
    (nothing mis-named ever lands live); restoring must rename first, or a folder called `Safety`
    briefly occupies the safety workspace and the next filing forks the job in two.
    """
    calls: list[str] = []
    _seams["find_folder"].side_effect = lambda parent, name: 555 if name == "Safety" else None
    _seams["find_ws"].side_effect = lambda ws, name: 4242 if ws == sheet_ids.WORKSPACE_ARCHIVE else None
    _seams["name"].return_value = "Safety"
    _seams["rename"].side_effect = lambda *a, **k: calls.append("rename")
    mv_ws = _seams["move_ws"]
    mv_ws.side_effect = lambda *a, **k: calls.append("move")

    res = job_archive.unarchive_smartsheet_container(_slot("smartsheet:safety"), "Coker", 4242)

    assert res.moved is True
    assert calls == ["rename", "move"], "restoring must rename BEFORE it moves"
    mv_ws.assert_called_once_with(555, sheet_ids.WORKSPACE_SAFETY_PORTAL)


def test_the_restore_resumes_from_a_renamed_but_unmoved_container(_seams):
    """The residual crash window of rename-then-move.

    A folder already renamed to <Job> but still in the archive must be found by THAT name — a
    label-only search would read it as 'nothing to move' and report the restore complete with the
    container still archived.
    """
    seen: list[str] = []

    def _find(parent: int, name: str) -> int | None:
        seen.append(name)
        return 555 if name == "Coker" else None

    _seams["find_folder"].side_effect = _find

    found = job_archive.resolve_archived_container(_slot("smartsheet:safety"), "Coker", 4242)

    assert found == 555
    assert seen == ["Safety", "Coker"], "label first, then the mid-restore name"


def test_the_restore_refuses_to_merge_onto_a_regrown_live_folder(_seams):
    """The case that actually happens: a job archived and then written to.

    The ordinary find-or-create paths re-grow `<root>/<Job>`, so the restore would have two folders
    claiming one name. Neither system has a merge primitive, so fusing them is unrecoverable —
    this must refuse loudly and leave both trees intact.
    """
    _seams["find_folder"].side_effect = lambda parent, name: 555 if name == "Safety" else None
    _seams["find_ws"].side_effect = lambda ws, name: 4242 if ws == sheet_ids.WORKSPACE_ARCHIVE else 999

    with pytest.raises(RuntimeError, match="already holds that name"):
        job_archive.unarchive_smartsheet_container(_slot("smartsheet:safety"), "Coker", 4242)

    _seams["move_ws"].assert_not_called()
    _seams["rename"].assert_not_called()  # refuse BEFORE mutating anything


def test_the_box_restore_is_one_call_with_no_rename_ordering(_seams):
    # Box's PUT carries parent AND name, so the ordering problem the Smartsheet side has does not
    # exist here — and the Smartsheet resume logic must not be ported onto it.
    _seams["box_find"].return_value = "777"

    res = job_archive.unarchive_box_container(_slot("box:progress"), "Coker", "950")

    assert res.moved is True
    _seams["box_move"].assert_called_once_with("777", "200", new_name="Coker")
    _seams["rename"].assert_not_called()


def test_restoring_a_never_archived_job_creates_no_archive_folders(_seams):
    """An absent archive folder is a real answer, not a failure.

    `ensure_*` would CREATE it, littering the archive with an empty folder for every restore of a
    job that was never archived — and reporting a move that never happened.
    """
    _seams["find_ws"].return_value = None   # no ITS — Archive/<Job>
    _seams["box_find"].return_value = None  # no ITS Archive/<Job>

    results = job_archive.unarchive_job(_job(archive_direction="unarchive"))

    assert all(r.moved is True and r.note == "nothing to move" for r in results)
    assert job_archive.state_from_results(results) == "complete"
    _seams["create_ws"].assert_not_called()
    _seams["box_ensure"].assert_not_called()
    _seams["move"].assert_not_called()
    _seams["box_move"].assert_not_called()


def test_the_restore_probes_each_archive_folder_once_per_system(_seams):
    # The _ABSENT sentinel distinguishes "not looked up yet" from "looked up and absent"; without
    # it a never-archived job re-probes the archive once per slot.
    _seams["find_ws"].return_value = None
    _seams["box_find"].return_value = None

    job_archive.unarchive_job(_job(archive_direction="unarchive"))

    archive_probes = [c for c in _seams["find_ws"].call_args_list
                      if c.args[0] == sheet_ids.WORKSPACE_ARCHIVE]
    assert len(archive_probes) == 1
    assert _seams["box_find"].call_count == 1


def test_a_restore_failure_never_raises_and_stays_fenced(_seams):
    _seams["find_ws"].side_effect = lambda ws, name: 4242 if ws == sheet_ids.WORKSPACE_ARCHIVE else None
    _seams["find_folder"].side_effect = lambda parent, name: 555 if name == "Safety" else None
    _seams["move_ws"].side_effect = smartsheet_client.SmartsheetError("boom")

    results = job_archive.unarchive_job(_job(archive_direction="unarchive"))

    assert any(r.moved is False for r in results)
    assert any(c.kwargs.get("error_code") == "archive_container_failed"
               for c in _seams["log"].call_args_list)


# ---- direction dispatch -------------------------------------------------


@pytest.mark.parametrize("direction, expected", [("archive", "archive_job"), ("unarchive", "unarchive_job")])
def test_the_pass_dispatches_on_the_rows_direction(mocker, _seams, direction, expected):
    """`/archive-pending` serves BOTH directions from one queue.

    Running the wrong one is SILENT: an un-archive row put through archive_job finds nothing live,
    reports six clean 'nothing to move' successes, and posts complete while every folder stays
    archived.
    """
    spy = mocker.patch.object(job_archive, expected, return_value=[])

    job_archive.run_archive_pass(_job(archive_direction=direction))

    spy.assert_called_once()


def test_an_unknown_direction_refuses_instead_of_defaulting(_seams):
    # Defaulting to 'archive' is the same silent-wrong-result class in the other direction.
    results = job_archive.run_archive_pass(_job(archive_direction=""))

    assert all(r.moved is False and r.note == "unknown direction" for r in results)
    assert job_archive.state_from_results(results) == "failed"
    _seams["move"].assert_not_called()
    _seams["box_move"].assert_not_called()
    assert any(c.kwargs.get("error_code") == "archive_direction_unknown"
               for c in _seams["log"].call_args_list)


def test_the_directions_the_pass_accepts_match_the_workers_contract():
    """The Worker's commit point validates `direction === "archive" || "unarchive"` and 400s
    anything else. A third value accepted here would post updates that route rejects."""
    worker = (_REPO_ROOT / "safety_portal" / "worker" / "index.ts").read_text()
    assert 'row.direction === "archive" || row.direction === "unarchive"' in worker
