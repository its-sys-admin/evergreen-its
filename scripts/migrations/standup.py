"""Stand up the full Smartsheet/Box tenant from zero — builders + auto-FLIP + seeds.

The orchestrator that turns the ~25-script manual cutover sequence (interleaved with
hand-pasted FLIP edits of shared/sheet_ids.py) into ONE attended run. The circle the
operator asked for (2026-07-22): seed/builder scripts create every sheet, and
sheet_ids_regen.py rewrites the constants automatically between stages — no ID is
ever hand-pasted. FLIP still precedes SEED; the flip is just mechanical now.

How the interleave works
    Builders run as SUBPROCESSES (each fresh import of shared/sheet_ids.py picks up
    the values the preceding regen stage wrote — zero builder refactoring) under the
    STANDUP_NONINTERACTIVE=1 contract: their confirm seams auto-approve, stdin is
    CLOSED, and any unexpected prompt fails the stage loudly (see NONINTERACTIVE_ENV).
    THIS process gives ONE master y/N gate up front; the per-builder prompts remain
    the control when scripts run standalone, and the master gate is the control here.
    Child output is line-streamed with a [stage/script] prefix (PYTHONUNBUFFERED
    baked in) and teed to a per-run transcript beside the dump. sheet_ids_regen runs
    with --write (non-strict) between stages: it flips what exists, leaves later-stage
    constants untouched, and the FINAL stage runs it with --check (strict parity).

What is deliberately SKIPPED (with reasons, so nobody hunts for a phantom):
    - seed_its_active_jobs.py    — seeds the 6 pre-portal demo projects; the live job
                                   set is restored from the pre-wipe dump instead
                                   (seeding both would pollute the portal dropdown).
    - seed_its_project_routing.py — seeds Box folder ids from defaults.BOX_PROJECT_FOLDERS,
                                   which are DEAD after the full Box wipe (ITS DATA and
                                   the 1111A/1111B templates are gone). The routing lane
                                   is dormant until projects are re-cloned; seeding dead
                                   ids would be worse than an empty sheet.
    - build_its_trusted_contacts_sheet.py + seed — the trusted-contacts lane is dormant
                                   (SHEET_TRUSTED_CONTACTS=0 by design, pre-wipe parity).
    - The ~72 demo tracker sheets — content, not structure (in the dump if ever needed).

Manual gates: NONE remain. The former ITS_Active_Jobs AUTO_NUMBER gate was stale
pre-Slice-6 doctrine — since P2.5 Slice 6 the portal assigns the canonical
JOB-###### (Worker job_counter) and the mirror WRITES it, so 'Job ID' and
'Portal Job Key' are plain TEXT columns extend_its_active_jobs_phase3.py now
creates via the API. (The _manual_gate machinery stays for any future genuinely
UI-only step.)

Preconditions: all org.solutionsmith.its.* daemons UNLOADED (verified, same guard as
wipe_tenant.py); Box OAuth as the dedicated ITS identity; ITS_SMARTSHEET_TOKEN in
Keychain. Post-run: commit the regenerated ID surfaces via PR, delete
~/its/state/heartbeat_row_ids.json, reload the fleet dark (see the printed epilogue).

Run from ~/its while daemons are down:
    python3 scripts/migrations/standup.py --list
    python3 scripts/migrations/standup.py [--dump DIR] [--start-at STAGE] [--skip-shares]
    python3 scripts/migrations/standup.py --resume      # restart from the run state
    # PRODUCTION stand-up: restore the reference sheets only (vendors /
    # subcontractors / master DBs / equipment), both Active-Jobs sheets empty:
    python3 scripts/migrations/standup.py --dump DIR --skip-shares \
        --skip-restore-sheet ITS_Active_Jobs --skip-restore-sheet ITS_Active_Jobs_Progress
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

# Family-lib sibling (this dir is sys.path[0] when run as a script; tests insert
# it explicitly — the test_standup_tools.py import pattern).
from _rest_retry import request_with_retry  # noqa: E402

from shared import keychain, state_io  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATIONS = "scripts/migrations"
DUMP_ROOT = pathlib.Path.home() / "its" / "logs" / "migrations"
BASE = "https://api.smartsheet.com/2.0"

# Every migrations-dir seeder the stand-up RUNS, in run order. Hoisted to a module constant so
# tests/test_standup_tools.py can check VC-03's rows against the scripts this orchestrator
# actually executes (plus seed-config-baseline's scripts/seed_its_config.py) — the old guard
# grepped the whole migrations DIRECTORY, so a seeder that existed on disk but was never run
# passed it. That is exactly how seed_manifest_config.py shipped 2026-08-07 and a rebuilt
# tenant had no field_ops.manifest_poll.* rows until final-verify VC-03 failed with no scripted
# remedy (2026-08-10). A new seeder is not DONE until it appears HERE.
SEEDER_STAGE_SCRIPTS: tuple[str, ...] = (
    "seed_its_forms_catalog.py",
    "extend_its_forms_catalog_parent_variant.py",
    "seed_its_vendors.py",
    "seed_its_subcontractors.py",
    "seed_safety_intake_config.py",
    "seed_safety_intake_polling_config.py",
    "seed_safety_recipients_config.py",
    "seed_po_materials_config.py",
    "seed_estimates_config.py",
    "seed_rfq_config.py",
    "seed_rfq_send_config.py",
    "seed_subcontracts_config.py",
    "seed_subcontracts_send_config.py",
    "seed_config_actuator_config.py",
    "seed_daemon_gate_config.py",
    "seed_manifest_config.py",
    "seed_schedule_config.py",
    "seed_generate_and_interval_config.py",
)

# The non-interactive contract with the builder family (2026-07-23 review):
# _run_script sets this env var and closes the child's stdin. The six builder
# confirm seams auto-approve ONLY under it (this process's master y/N gate is
# the documented control for orchestrated runs), and any UNEXPECTED prompt — a
# second gate in a gated builder, a brand-new input() in a promptless seeder —
# hits EOF on the closed stdin and fails the stage LOUDLY. The old blind
# `input="y\n"*8` feed would have silently confirmed a future destructive
# prompt ("delete conflicting sheet? [y/N]" being the feared class).
NONINTERACTIVE_ENV = "STANDUP_NONINTERACTIVE"

# The one launchd job the daemon-down guards EXEMPT (read-only dashboard; its
# ACT verbs are fenced by the stand-up marker — see DAEMON_DOWN_EXEMPT below).
DASHBOARD_LABEL = "org.solutionsmith.its.dashboard"

# Run-state file (--resume bookkeeping + per-stage run forensics). Lives INSIDE
# the prewipe dump dir — a run is 1:1 with a dump, so it rides the same
# ambiguity guards _resolve_dump_dir enforces and stays out of ~/its/state/
# (it is orchestrator bookkeeping, not daemon state; state_io is used anyway —
# costs nothing, crash-safe). A --no-restore run has no dump; its state lives
# at one well-known DUMP_ROOT path (a fresh-tenant stand-up happens ~once per
# tenant, and the refuse-on-complete rule covers staleness).
STATE_FILE_NAME = "standup_state.json"
NORESTORE_STATE_PATH = DUMP_ROOT / "standup_norestore_state.json"

# ---- `standup.py finish` posture table (the post-merge fleet reload) --------
#
# The five SEND-DISPATCH plists. `--posture dark` (default) NEVER loads them —
# a rebuilt tenant comes back with zero running external-send dispatchers
# regardless of the pre-wipe posture; loading any of them is a FIXED
# External-Send-Gate operator action (--posture full + typed phrase, or
# per-plist by hand, either way Seth). This is deliberately a POSTURE table in
# code, never inferred from ITS_Config — a gate row reading `true` must not be
# able to pull a send daemon up (fail-dark).
# test_send_dispatch_labels_match_shipped_plists is the parity teeth.
SEND_DISPATCH_LABELS: frozenset[str] = frozenset({
    "org.solutionsmith.its.po-send",
    "org.solutionsmith.its.rfq-send",
    "org.solutionsmith.its.subcontract-send",
    "org.solutionsmith.its.weekly-send",
    "org.solutionsmith.its.progress-send",
})
FULL_POSTURE_PHRASE = "LOAD SEND DAEMONS"
LAUNCHD_DIR = REPO_ROOT / "scripts" / "launchd"
INSTALL_SH = LAUNCHD_DIR / "install.sh"
HEARTBEAT_ROW_IDS_PATH = pathlib.Path.home() / "its" / "state" / "heartbeat_row_ids.json"
# Cycle-wait only daemons at <= this interval; the hourly/daily jobs
# (picklist-sync, watchdog) are reported as not-waited, not treated as laggards.
HEARTBEAT_WAIT_MAX_INTERVAL_SECONDS = 900.0

# Sheets whose ROWS are restored from the pre-wipe dump (workspace name, sheet name,
# sheet_ids constant of the rebuilt target). Everything else restarts empty by design
# (pending review rows discarded per the 2026-07-22 operator decision).
RESTORE_SHEETS: tuple[tuple[str, str, str], ...] = (
    ("ITS –– Safety Portal", "ITS_Active_Jobs", "SHEET_ACTIVE_JOBS"),
    ("ITS — Progress Reporting", "ITS_Active_Jobs_Progress", "SHEET_ACTIVE_JOBS_PROGRESS"),
    ("ITS — Purchase Orders", "ITS_Vendors", "SHEET_ITS_VENDORS"),
    ("ITS — Subcontracts", "ITS_Subcontractors", "SHEET_ITS_SUBCONTRACTORS"),
    ("ITS — Operations", "Subcontractor DB", "SHEET_SUBCONTRACTOR_DB"),
    ("ITS — Operations", "Vendor DB", "SHEET_VENDOR_DB"),
    ("ITS — Operations", "Equipment Master", "SHEET_EQUIPMENT_MASTER"),
)

# Workspaces whose SHARE lists (F22 approver sets) are restored from the dump.
RESHARE_WORKSPACES: tuple[str, ...] = (
    "ITS –– Safety Portal",
    "ITS — Progress Reporting",
    "ITS — Purchase Orders",
    "ITS — Subcontracts",
    "ITS — Human Review",
    "Forefront Portfolio — ITS Demo",
)

BOX_ROOT_CONFIG_ROWS: tuple[tuple[str, str, str], ...] = (
    # (Box folder name, ITS_Config Setting, Workstream)
    ("ITS Safety Reports", "safety_reports.box.portal_root_folder_id", "safety_reports"),
    ("ITS Progress Reports", "progress_reports.box.portal_root_folder_id",
     "progress_reports"),
    # The PO lane's own root (2026-08-11) — po PDFs + RFQs/ + Vendor Quotes/ per job.
    ("ITS Purchase Orders", "po_materials.box.portal_root_folder_id", "po_materials"),
    # Track 6 job archive destination — a sibling root, not a child of any tree.
    ("ITS Archive", "field_ops.box.archive_root_folder_id", "field_ops"),
)


def _confirm(prompt: str) -> bool:
    """Master y/N gate (tests monkeypatch). EOF counts as a decline."""
    try:
        return input(f"{prompt} [y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def _manual_gate(instruction: str) -> None:
    """Pause for a UI-only step; loops until the operator confirms it is done.

    EOF (a piped/non-interactive run reaching a gate) aborts the stage cleanly —
    the resume hint then names this stage, the operator does the UI step, and
    the run continues with --start-at the FOLLOWING stage.
    """
    print(f"\n=== MANUAL STEP ===\n{instruction}")
    while True:
        try:
            answer = input("Done? [y/N] ").strip().lower()
        except EOFError as exc:
            raise StageFailedError(
                "manual step reached with no interactive stdin — do the step above, "
                "then resume at the NEXT stage") from exc
        if answer == "y":
            return
        print("Waiting — complete the step above, then answer y.")


# Streaming context, set by the stage loop / run setup. `_current_stage` rides
# every child-output prefix; `_run_log_path` (when set) tees emitted lines to
# the per-run transcript beside the dump — the 2026-07-23 retro's evidence was
# scrollback, and the mid-run PYTHONUNBUFFERED discovery lived only in the
# operator's shell. Both fixes now live in the tool.
_current_stage: str = "?"
_run_log_path: pathlib.Path | None = None


def _emit(line: str) -> None:
    """Print + tee to the per-run log (tee is best-effort, never fatal)."""
    print(line)
    if _run_log_path is not None:
        try:
            with _run_log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass  # the console stream is primary; a full disk must not kill the run


def _run_script(rel_path: str, *args: str) -> None:
    """Run a builder/seeder subprocess under the non-interactive contract.

    - ``STANDUP_NONINTERACTIVE=1`` + ``stdin=DEVNULL``: the six builder confirm
      seams auto-approve (the master gate above is the control); any UNEXPECTED
      ``input()`` in a child raises EOFError -> nonzero exit -> StageFailedError,
      loud instead of silently fed a 'y' (see NONINTERACTIVE_ENV).
    - ``PYTHONUNBUFFERED=1`` + line-streamed output, each line prefixed
      ``[stage/script]`` and teed to the per-run log.
    """
    _run_child([sys.executable, str(REPO_ROOT / rel_path), *args],
               pathlib.Path(rel_path).stem, rel_path)


def _run_module(module: str, *args: str) -> None:
    """`_run_script` for `-m`-invoked modules (e.g. scripts.verify_cutover,
    whose imports need package execution) — same non-interactive contract,
    same transcript tee."""
    _run_child([sys.executable, "-m", module, *args],
               module.rsplit(".", 1)[-1], module)


def _run_child(cmd: list[str], base: str, what: str) -> None:
    _emit(f"\n--- $ {' '.join(cmd[1:])}")
    env = {**os.environ, "PYTHONUNBUFFERED": "1", NONINTERACTIVE_ENV: "1"}
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, cwd=REPO_ROOT, env=env)
    assert proc.stdout is not None  # PIPE above guarantees it; narrows the type
    for line in proc.stdout:
        _emit(f"[{_current_stage}/{base}] {line.rstrip()}")
    returncode = proc.wait()
    if returncode != 0:
        raise StageFailedError(f"{what} exited {returncode}")


class StageFailedError(RuntimeError):
    pass


def _run_many(*rel_paths: str) -> None:
    for rel_path in rel_paths:
        _run_script(rel_path)


# The builder->flip contract: each interleaved regen names the constants its
# preceding builder stage JUST created and retries until THOSE resolve —
# Smartsheet's create->read propagation lag can outlast any fixed number of
# retries' heuristics (the 2026-07-23 run saw a >3s window leave
# WORKSPACE_SAFETY_PORTAL stale-dead), so the expected set is explicit and an
# unresolved expected constant FAILS the stage instead of drifting.
_HR_FOLDERS = tuple(f"FOLDER_HR_{s}" for s in (
    "SAFETY_REPORTS", "SUBCONTRACTS", "PURCHASE_ORDERS_AND_MATERIALS",
    "EMAIL_TRIAGE", "AI_EMPLOYEE", "PERSONNEL"))
_PROJECTS = ("BRADLEY_1", "BRADLEY_2", "BRIMFIELD_1", "BRIMFIELD_2",
             "HUNTLEY", "ROCKFORD")
REGEN_EXPECT: dict[str, tuple[str, ...]] = {
    "regen-system": ("WORKSPACE_SYSTEM", "FOLDER_SYSTEM_CONFIG",
                     "FOLDER_SYSTEM_LOGS", "FOLDER_SYSTEM_QUEUES",
                     "FOLDER_SYSTEM_DAEMONS"),
    "regen-system-sheets": ("SHEET_CONFIG", "SHEET_ERRORS", "SHEET_QUARANTINE",
                            "SHEET_REVIEW_QUEUE", "SHEET_DAEMON_HEALTH"),
    "regen-safety-portal": ("WORKSPACE_SAFETY_PORTAL", "FOLDER_SAFETY_PORTAL",
                            "FOLDER_FORM_CATALOG"),
    "regen-safety-sheets": ("SHEET_ACTIVE_JOBS", "SHEET_FORMS_CATALOG",
                            "SHEET_WSR_HUMAN_REVIEW", "SHEET_ORPHANED_REPORTS"),
    "regen-progress": ("WORKSPACE_PROGRESS_REPORTING", "FOLDER_PROGRESS_CONTROL"),
    "regen-progress-sheets": ("SHEET_WPR_HUMAN_REVIEW", "SHEET_ACTIVE_JOBS_PROGRESS"),
    "regen-po": ("WORKSPACE_PURCHASE_ORDERS", "FOLDER_PO_CONTROL"),
    "regen-po-sheets": ("SHEET_ITS_VENDORS", "SHEET_PO_LOG",
                        "SHEET_PO_PENDING_REVIEW", "SHEET_ESTIMATE_LOG",
                        "SHEET_RFQ_LOG", "SHEET_RFQ_PENDING_REVIEW"),
    "regen-subcontracts": ("WORKSPACE_SUBCONTRACTS", "FOLDER_SC_CONTROL"),
    "regen-subcontract-sheets": ("SHEET_ITS_SUBCONTRACTORS", "SHEET_SUBCONTRACT_LOG",
                                 "SHEET_SUBCONTRACT_PENDING_REVIEW"),
    "regen-job-folders": ("FOLDER_PO_JOBS", "FOLDER_SC_JOBS"),
    "regen-legacy": (
        "WORKSPACE_HUMAN_REVIEW", "WORKSPACE_OPERATIONS", "WORKSPACE_ARCHIVE",
        "WORKSPACE_DEMO", *_HR_FOLDERS, "FOLDER_OPERATIONS_MASTER_DBS",
        "FOLDER_ARCHIVE_CLOSED_PROJECTS", "FOLDER_ACTIVE_PROJECTS",
        "FOLDER_PORTFOLIO_ROLLUPS", "FOLDER_FIELD_REPORTS",
        *(f"FOLDER_PROJECT_{p}" for p in _PROJECTS),
        *(f"FOLDER_FIELD_REPORTS_{p}" for p in _PROJECTS),
        "SHEET_TIME_OFF", "SHEET_WPR_PENDING_REVIEW", "SHEET_SUBCONTRACTOR_DB",
        "SHEET_VENDOR_DB", "SHEET_EQUIPMENT_MASTER",
        "safety_reports/week_folder.py:TEMPLATE_DAILY_REPORTS_SHEET_ID",
        "safety_reports/week_folder.py:TEMPLATE_WEEKLY_ROLLUP_SHEET_ID",
    ),
    "regen-config-sheets": ("SHEET_PROJECT_ROUTING", "SHEET_PICKLIST_SYNC_CONFIG"),
}


def _regen_stage(stage_name: str) -> None:
    expect_args: list[str] = []
    for const in REGEN_EXPECT[stage_name]:
        expect_args += ["--expect", const]
    _run_script(f"{MIGRATIONS}/sheet_ids_regen.py", "--write",
                "--retry-missing", "10", *expect_args)


def _regen(*args: str) -> None:
    _run_script(f"{MIGRATIONS}/sheet_ids_regen.py", *args)


def _regen_entry(stage_name: str) -> Stage:
    return (stage_name, lambda: _regen_stage(stage_name))



def _fresh_sheet_ids() -> Any:
    """Re-import shared.sheet_ids so post-regen constants are visible in-process."""
    import shared.sheet_ids
    return importlib.reload(shared.sheet_ids)


def _headers() -> dict[str, str]:
    token = keychain.get_secret("ITS_SMARTSHEET_TOKEN")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ---- composite stage bodies ----------------------------------------------


def _stage_box_roots() -> None:
    """build_box_roots + auto-paste of the two ITS_Config rows (circle closed).

    The builder's own identity gate is auto-fed like every other prompt, so the
    Box login is printed HERE first — the attended operator sees which account
    the roots land in before the subprocess runs (the builder prints it again).
    """
    from shared import box_client, smartsheet_client
    me = box_client.get_client().user().get()
    print(f"[info] Box identity for this stage: {me.login} ({me.name})")
    _run_script(f"{MIGRATIONS}/build_box_roots.py")
    sheet_ids = _fresh_sheet_ids()
    all_roots = [i for i in box_client.list_folder("0", limit=1000)
                 if i.get("type") == "folder"]
    for folder_name, setting, workstream in BOX_ROOT_CONFIG_ROWS:
        matches = [str(i.get("id")) for i in all_roots if i.get("name") == folder_name]
        if len(matches) > 1:
            raise StageFailedError(
                f"Box root {folder_name!r} is AMBIGUOUS ({len(matches)} folders: "
                f"{matches}) — reconcile before the config paste")
        if not matches:
            raise StageFailedError(f"Box root {folder_name!r} not found after builder run")
        folder_id = matches[0]
        existing = smartsheet_client.get_rows(
            sheet_ids.SHEET_CONFIG,
            filters={"Setting": setting, "Workstream": workstream})
        if existing:
            row = existing[0]
            if str(row.get("Value")) != folder_id:
                smartsheet_client.update_rows(sheet_ids.SHEET_CONFIG, [{
                    "_row_id": row["_row_id"], "Value": folder_id}])
                print(f"[ok] ITS_Config {setting} updated -> {folder_id}")
            else:
                print(f"[skip] ITS_Config {setting} already {folder_id}")
        else:
            smartsheet_client.add_rows(sheet_ids.SHEET_CONFIG, [{
                "Setting": setting, "Value": folder_id, "Workstream": workstream,
                "Description": f"Box root folder id for {folder_name!r} "
                               "(auto-pasted by standup.py from build_box_roots)."}])
            print(f"[ok] ITS_Config {setting} added -> {folder_id}")


def _find_dump_sheet(dump_dir: pathlib.Path, workspace: str, sheet: str,
                     ) -> dict[str, Any] | None:
    ws_dir = dump_dir / "smartsheet" / workspace.replace("/", "_")
    if not ws_dir.is_dir():
        return None
    matches = []
    for path in sorted(ws_dir.glob("*.sheet.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("name") == sheet:
            matches.append(data)
    if len(matches) > 1:
        raise StageFailedError(
            f"dump ambiguity: {len(matches)} sheets named {sheet!r} in {workspace!r}")
    return matches[0] if matches else None


def _validate_skip_restore_sheets(names: Sequence[str]) -> tuple[str, ...]:
    """Refuse LOUDLY at startup on a --skip-restore-sheet name not in
    RESTORE_SHEETS — a typo would silently RESTORE the sheet the operator
    meant to hold back (the production stand-up's Active-Jobs case)."""
    valid = [sheet for _ws, sheet, _const in RESTORE_SHEETS]
    unknown = sorted(set(names) - set(valid))
    if unknown:
        raise StageFailedError(
            f"--skip-restore-sheet: unknown sheet name(s) {unknown} — "
            f"valid names: {valid}")
    return tuple(dict.fromkeys(names))


def _stage_restore_rows(dump_dir: pathlib.Path,
                        skip_restore_sheets: Sequence[str] = ()) -> None:
    """Restore data-bearing SoR rows from the pre-wipe dump (idempotent by primary).

    Raw REST (not smartsheet_client.add_rows) so structured objectValues —
    MULTI_PICKLIST cells like ITS_Vendors "Supply Categories" and
    ITS_Subcontractors "Trades" — round-trip intact; a title-keyed plain value
    would flatten them to a display string the API rejects. Cells map dump
    title -> REBUILT sheet column id; dump columns absent on the rebuilt
    schema are reported, never silently dropped. Sheets named in
    skip_restore_sheets (--skip-restore-sheet, validated at startup) are held
    back entirely — they stay as the builders left them (empty).
    """
    from shared import smartsheet_client
    sheet_ids = _fresh_sheet_ids()
    for workspace, sheet_name, constant in RESTORE_SHEETS:
        if sheet_name in skip_restore_sheets:
            print(f"[skip] restore skipped by --skip-restore-sheet: {sheet_name!r}")
            continue
        target_id = getattr(sheet_ids, constant, 0)
        if not target_id:
            print(f"[WARN] restore skipped: {constant} unresolved (sheet {sheet_name!r})")
            continue
        dump = _find_dump_sheet(dump_dir, workspace, sheet_name)
        if dump is None:
            print(f"[WARN] restore skipped: {sheet_name!r} not in dump {workspace!r}")
            continue
        dump_writable = {
            c["title"] for c in dump.get("columns", [])
            if c.get("title") and not c.get("systemColumnType")
        }
        target_cols = smartsheet_client.list_columns_with_options(target_id)
        target_id_by_title = {c.get("title"): int(c["id"]) for c in target_cols}
        writable = dump_writable & set(target_id_by_title)
        dropped = dump_writable - set(target_id_by_title)
        if dropped:
            print(f"[WARN] {sheet_name!r}: dump column(s) absent on the rebuilt sheet, "
                  f"values NOT restored: {sorted(dropped)}")
        primary = next(
            (c["title"] for c in dump.get("columns", []) if c.get("primary")), None)
        if primary is None:
            print(f"[WARN] restore skipped: {sheet_name!r} has no primary column in dump")
            continue
        existing_primaries = {
            r.get(primary) for r in smartsheet_client.get_rows(target_id)}
        payload: list[dict[str, Any]] = []
        for row in dump.get("rows", []):
            if row.get(primary) in existing_primaries:
                continue
            object_values = row.get("_ov") or {}
            cells: list[dict[str, Any]] = []
            for title in sorted(writable):
                col_id = target_id_by_title[title]
                if title in object_values:
                    cells.append({"columnId": col_id,
                                  "objectValue": object_values[title]})
                elif row.get(title) is not None:
                    cells.append({"columnId": col_id, "value": row[title]})
            if cells:
                payload.append({"toBottom": True, "cells": cells})
        if not payload:
            print(f"[skip] {sheet_name!r}: nothing to restore "
                  f"({len(dump.get('rows', []))} dumped, all present or empty).")
            continue
        for i in range(0, len(payload), 300):
            # raise_for_status=False: transient 429/5xx are retried inside the
            # helper (exhaustion propagates -> stage fails with its resume
            # hint); a non-transient failure surfaces WITH the response body.
            r = request_with_retry(
                "post", f"{BASE}/sheets/{target_id}/rows", headers=_headers(),
                json=payload[i:i + 300], timeout=120, raise_for_status=False)
            if r.status_code != 200:
                raise StageFailedError(
                    f"restore add-rows failed for {sheet_name!r}: "
                    f"HTTP {r.status_code} {r.text[:300]}")
        print(f"[ok] {sheet_name!r}: restored {len(payload)} row(s) from dump.")
    # Unconditional even when --skip-restore-sheet held back the Active-Jobs
    # sheets: both rebuilt sheets are then simply EMPTY, so the reconcile reads
    # two empty row sets and no-ops ("already aligned") — cheaper and less
    # drift-prone than a special case.
    _reconcile_progress_job_ids(sheet_ids)


def _reconcile_progress_job_ids(sheet_ids: Any) -> None:
    """Verify ITS_Active_Jobs_Progress 'Job ID' matches the safety sheet's values.

    Both sheets' 'Job ID' are plain TEXT restored VERBATIM from the dump (the
    portal owns the JOB-###### number — Slice 6), so this is a belt-and-braces
    consistency check, expected to be a no-op: it reconciles by 'Project Name',
    rewrites any drifted progress value, and WARNs on rows with no safety
    counterpart. (Its original premise — AUTO_NUMBER renumbering on the safety
    side — was stale pre-Slice-6 doctrine; kept because a cheap cross-sheet
    verify is worth keeping either way.)
    """
    from shared import smartsheet_client
    if not (getattr(sheet_ids, "SHEET_ACTIVE_JOBS", 0)
            and getattr(sheet_ids, "SHEET_ACTIVE_JOBS_PROGRESS", 0)):
        print("[WARN] job_id_reconcile skipped: an Active-Jobs constant is unresolved.")
        return
    safety = smartsheet_client.get_rows(sheet_ids.SHEET_ACTIVE_JOBS)
    progress = smartsheet_client.get_rows(sheet_ids.SHEET_ACTIVE_JOBS_PROGRESS)
    safety_by_project = {r.get("Project Name"): r.get("Job ID") for r in safety
                        if r.get("Project Name")}
    updates = []
    for row in progress:
        project = row.get("Project Name")
        want = safety_by_project.get(project)
        if want is None:
            print(f"[WARN] job_id_reconcile: progress row {project!r} has no safety "
                  "counterpart — reconcile manually.")
            continue
        if row.get("Job ID") != want:
            updates.append({"_row_id": row["_row_id"], "Job ID": want})
    if updates:
        smartsheet_client.update_rows(sheet_ids.SHEET_ACTIVE_JOBS_PROGRESS, updates)
        print(f"[ok] job_id_reconcile: {len(updates)} progress Job ID(s) re-aligned "
              "to the safety sheet's regenerated auto-numbers.")
    else:
        print("[ok] job_id_reconcile: safety and progress Job IDs already aligned.")


def _stage_restore_shares(dump_dir: pathlib.Path) -> None:
    """Re-share rebuilt workspaces from the dumped share lists (F22 approver sets).

    §46: workspace membership IS approval authority, so this write is guarded
    like the builders — the target workspace must be OWNER-access and uniquely
    named, and every dumped share this tool CANNOT restore (a GROUP share has
    no email) is WARNed, never silently dropped: a silently narrower approver
    set on Purchase Orders / Subcontracts would fail-close the send gates with
    no explanation.
    """
    ws_listing = request_with_retry("get", f"{BASE}/workspaces?includeAll=true",
                                    headers=_headers(), timeout=30)
    live_by_name: dict[str, list[dict[str, Any]]] = {}
    for ws in ws_listing.json().get("data", []):
        live_by_name.setdefault(str(ws.get("name")), []).append(ws)
    for name in RESHARE_WORKSPACES:
        meta_path = (dump_dir / "smartsheet" / name.replace("/", "_")
                     / "_workspace.json")
        if not meta_path.is_file():
            print(f"[WARN] shares skipped: no dump for {name!r}")
            continue
        shares = json.loads(meta_path.read_text(encoding="utf-8")).get("shares", [])
        targets = live_by_name.get(name, [])
        if len(targets) != 1:
            print(f"[WARN] shares skipped: {len(targets)} live workspaces named {name!r}")
            continue
        target = targets[0]
        if target.get("accessLevel") != "OWNER":
            print(f"[WARN] shares skipped: workspace {name!r} accessLevel="
                  f"{target.get('accessLevel')} != OWNER — refusing to write shares "
                  "into a workspace this token does not own.")
            continue
        added = 0
        for share in shares:
            email = share.get("email")
            level = share.get("accessLevel")
            if level == "OWNER":
                continue  # ownership is implicit on the rebuilt workspace
            if not email:
                print(f"  [WARN] share_not_restorable: {name!r} had a non-email share "
                      f"(type={share.get('type')}, name={share.get('name')!r}, "
                      f"accessLevel={level}) — GROUP shares must be re-added by hand "
                      "or the approver set is narrower than pre-wipe.")
                continue
            # raise_for_status=False: a permanent 4xx (already-shared dup,
            # invalid user) keeps the loud-but-non-fatal WARN below, but a
            # transient 429/5xx is retried and PROPAGATES on exhaustion — it
            # must never ride the WARN path, or the F22 approver set silently
            # narrows behind a rate-limit blip.
            r = request_with_retry(
                "post", f"{BASE}/workspaces/{int(target['id'])}/shares?sendEmail=false",
                headers=_headers(),
                json=[{"email": email, "accessLevel": level}], timeout=30,
                raise_for_status=False)
            if r.status_code == 200:
                added += 1
            else:
                # already-shared or invalid — loud but non-fatal (operator reconciles)
                print(f"  [WARN] share {email} ({level}) on {name!r}: "
                      f"HTTP {r.status_code} {r.text[:120]}")
        print(f"[ok] {name!r}: {added} share(s) restored.")


def _stage_final_verify() -> None:
    _regen("--check")
    _emit("\n--- verify_cutover (config rows; sandbox values allowed — rehearsal, "
          "not a phase verdict)")
    _run_module("scripts.verify_cutover", "--only", "config", "--allow-sandbox")


# ---- the stage table ------------------------------------------------------

Stage = tuple[str, Callable[[], None]]


def build_stages(dump_dir: pathlib.Path | None, *, skip_shares: bool,
                 skip_restore_sheets: Sequence[str] = ()) -> list[Stage]:
    def run(script: str, *args: str) -> Callable[[], None]:
        return lambda: _run_script(script, *args)

    stages: list[Stage] = [
        ("system-workspace", run(f"{MIGRATIONS}/build_system_workspace.py")),
        _regen_entry("regen-system"),
        ("system-sheets", run(f"{MIGRATIONS}/build_system_sheets.py")),
        _regen_entry("regen-system-sheets"),
        ("seed-config-baseline", run("scripts/seed_its_config.py")),
        ("safety-portal-workspace", run(f"{MIGRATIONS}/build_safety_portal_workspace.py")),
        _regen_entry("regen-safety-portal"),
        ("safety-portal-sheets", lambda: _run_many(
            f"{MIGRATIONS}/build_its_active_jobs_sheet.py",
            f"{MIGRATIONS}/build_its_forms_catalog_sheet.py",
            f"{MIGRATIONS}/build_wsr_human_review_sheet.py",
            f"{MIGRATIONS}/build_orphaned_reports_sheet.py",
        )),
        _regen_entry("regen-safety-sheets"),
        # phase3 now also creates the TEXT 'Job ID' + 'Portal Job Key' columns —
        # both plain writable columns (the portal assigns JOB-######, Slice 6),
        # so the former manual AUTO_NUMBER UI gate is GONE (it was stale
        # pre-Slice-6 doctrine; an AUTO_NUMBER would reject every mirror write).
        ("active-jobs-phase3", run(f"{MIGRATIONS}/extend_its_active_jobs_phase3.py")),
        ("active-jobs-contact-cols",
         run(f"{MIGRATIONS}/add_active_jobs_contact_routing_columns.py")),
        ("progress-workspace", run(f"{MIGRATIONS}/build_progress_reporting_workspace.py")),
        _regen_entry("regen-progress"),
        ("progress-sheets", lambda: _run_many(
            f"{MIGRATIONS}/build_wpr_human_review_sheet.py",
            f"{MIGRATIONS}/build_its_active_jobs_progress_sheet.py",
        )),
        _regen_entry("regen-progress-sheets"),
        ("po-workspace", run(f"{MIGRATIONS}/build_purchase_orders_workspace.py")),
        _regen_entry("regen-po"),
        ("po-sheets", lambda: _run_many(
            f"{MIGRATIONS}/build_its_vendors_sheet.py",
            f"{MIGRATIONS}/build_po_log_sheet.py",
            f"{MIGRATIONS}/build_po_pending_review_sheet.py",
            f"{MIGRATIONS}/build_estimate_log_sheet.py",
            f"{MIGRATIONS}/build_rfq_log_sheet.py",
            f"{MIGRATIONS}/build_rfq_pending_review_sheet.py",
        )),
        _regen_entry("regen-po-sheets"),
        ("subcontracts-workspace", run(f"{MIGRATIONS}/build_subcontracts_workspace.py")),
        _regen_entry("regen-subcontracts"),
        ("subcontract-sheets", lambda: _run_many(
            f"{MIGRATIONS}/build_its_subcontractors_sheet.py",
            f"{MIGRATIONS}/build_subcontract_log_sheet.py",
            f"{MIGRATIONS}/build_subcontract_pending_review_sheet.py",
        )),
        _regen_entry("regen-subcontract-sheets"),
        ("job-folders", run(f"{MIGRATIONS}/build_job_folders.py")),
        _regen_entry("regen-job-folders"),
        ("legacy-workspaces", run(f"{MIGRATIONS}/build_legacy_workspaces.py")),
        _regen_entry("regen-legacy"),
        ("system-config-sheets", lambda: _run_many(
            f"{MIGRATIONS}/build_its_project_routing_sheet.py",
            f"{MIGRATIONS}/create_picklist_sync_config.py",
        )),
        _regen_entry("regen-config-sheets"),
        ("box-roots", _stage_box_roots),
        ("docs-index", run(f"{MIGRATIONS}/build_docs_index_sheet.py")),
    ]
    if dump_dir is not None:
        stages.append(("restore-rows",
                       lambda: _stage_restore_rows(dump_dir, skip_restore_sheets)))
    stages.append(("seeders", lambda: _run_many(
        *(f"{MIGRATIONS}/{name}" for name in SEEDER_STAGE_SCRIPTS))))
    if dump_dir is not None and not skip_shares:
        stages.append(("restore-shares", lambda: _stage_restore_shares(dump_dir)))
    stages.append(("final-verify", _stage_final_verify))
    return stages


def _resolve_dump_dir(explicit: pathlib.Path | None) -> pathlib.Path:
    """Resolve the restore source, refusing every ambiguous case LOUDLY.

    - No dump found and --no-restore not passed -> abort (a silent fresh-tenant
      fallback would quietly skip the row/share restore the operator expects).
    - MULTIPLE prewipe dumps -> abort and demand an explicit --dump: a partial
      re-wipe after a failed first attempt creates a second, INCOMPLETE dump,
      and auto-picking "newest" would restore from the wrong one.
    """
    if explicit is not None:
        if not explicit.is_dir():
            raise StageFailedError(f"dump dir {explicit} does not exist")
        return explicit
    candidates = sorted(DUMP_ROOT.glob("prewipe_*"))
    if not candidates:
        raise StageFailedError(
            f"no prewipe_* dump under {DUMP_ROOT} — pass --dump DIR, or pass "
            "--no-restore explicitly for a genuine fresh-tenant stand-up")
    if len(candidates) > 1:
        listing = ", ".join(c.name for c in candidates)
        raise StageFailedError(
            f"{len(candidates)} prewipe dumps exist ({listing}) — a partial re-wipe "
            "produces an incomplete second dump, so auto-picking is unsafe; pass "
            "--dump with the FULL (usually first) dump explicitly")
    return candidates[0]


def _loaded_its_labels() -> list[str]:
    """Labels of loaded org.solutionsmith.its.* launchd jobs (raw — no exemption)."""
    out = subprocess.run(["launchctl", "list"], capture_output=True, text=True,
                         timeout=30, check=True).stdout
    return sorted(line.split()[-1] for line in out.splitlines()
                  if "org.solutionsmith.its." in line)


# Same exemption as wipe_tenant.DAEMON_DOWN_EXEMPT: the read-only dashboard
# stays up (no background tenant writes; ACT verbs fenced by the stand-up
# marker below — operator_dashboard/auth.py, PR #674; `finish` restarts it
# LAST so it re-imports the merged sheet_ids).
DAEMON_DOWN_EXEMPT = frozenset({DASHBOARD_LABEL})

# Written at LIVE-run start (refreshed on every resume — the fence max-age
# window restarts), cleared ONLY on full completion so an aborted run stays
# fenced across the fix->resume gap. operator_dashboard/auth.py reads it.
STANDUP_MARKER_PATH = pathlib.Path.home() / "its" / "state" / "standup_in_progress.json"


def _write_standup_marker() -> None:
    state_io.atomic_write_json(STANDUP_MARKER_PATH, {
        "started_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "tool": "standup",
    })
    print(f"[info] stand-up marker set ({STANDUP_MARKER_PATH}) — dashboard ACT "
          "verbs fenced until this run completes.")


def _clear_standup_marker() -> None:
    STANDUP_MARKER_PATH.unlink(missing_ok=True)
    print("[info] stand-up marker cleared — dashboard ACT verbs unfenced.")


def _require_daemons_down() -> bool:
    loaded = _loaded_its_labels()
    exempt_loaded = [d for d in loaded if d in DAEMON_DOWN_EXEMPT]
    if exempt_loaded:
        print(f"[info] exempt_daemons_loaded: {', '.join(exempt_loaded)} — the "
              "read-only dashboard may stay up (ACT verbs are fenced by the "
              "stand-up marker).")
    loaded = [d for d in loaded if d not in DAEMON_DOWN_EXEMPT]
    if loaded:
        print(f"[abort] daemons_loaded: {len(loaded)} org.solutionsmith.its.* job(s) "
              "still loaded — the stand-up rewrites shared/sheet_ids.py on the live "
              "tree; a running fleet would pick up half-flipped constants mid-run. "
              "Unload first (same precondition as wipe_tenant.py).")
        return False
    return True


# ---- run-branch mode (opt_simplify #2) --------------------------------------
#
# The 2026-07-23 run needed 6 fix-PR cycles, each forcing a stash/pull/pop dance
# on the live tree while uncommitted regen output sat across the ID surfaces. A
# per-run branch turns every one of those into a plain `git merge origin/main`,
# makes every resume point a COMMITTED checkpoint, and leaves the landing PR one
# push away. Committing mid-run is safe exactly and only inside this tool's
# precondition envelope: the daemons-down guard removes both live-tree hazards
# (no daemon picks up half-flipped constants; no publish daemon to strand).
# Never touches main: the branch is created from current HEAD and only ever
# pushed as itself. Post-run cleanup follows MERGED-verify-then-update-ref.


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args],
                          capture_output=True, text=True, check=False, timeout=120)


def _current_branch() -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _start_run_branch(run_id: str) -> str:
    """Create standup/run-<UTC> from current HEAD. Refuses a DIRTY tree — the
    branch must be created BEFORE the first regen write, or the first
    checkpoint silently includes pre-run dirt. Dirt is judged with the SAME
    ``:(exclude)logs`` pathspec the checkpoints use: the untracked prewipe
    dump under logs/migrations is REQUIRED by the default restore flow and can
    never land in a checkpoint, so it must not trip this gate."""
    if _git("status", "--porcelain", "--", ".", ":(exclude)logs").stdout.strip():
        raise StageFailedError(
            "run-branch: the working tree is DIRTY (repo files outside logs/) — "
            "commit/stash first so the first checkpoint holds ONLY regen output "
            "(or pass --no-run-branch to manage git yourself)")
    branch = f"standup/run-{run_id}"
    result = _git("checkout", "-b", branch)
    if result.returncode != 0:
        raise StageFailedError(f"run-branch: checkout -b {branch} failed: "
                               f"{result.stderr.strip()}")
    print(f"[info] run branch {branch!r} created — every stage that changes "
          "repo files commits a checkpoint here; a mid-run fix PR on main is "
          "picked up by `git fetch && git merge origin/main` (--resume does "
          "this for you).")
    return branch


def _resume_run_branch(recorded: str | None) -> str | None:
    """--resume path: the recorded run branch must be checked out; then sync
    main so subprocess stages pick up any mid-run fix PR from disk."""
    if recorded is None:
        print("[WARN] run-branch: the recorded run predates run-branch mode "
              "(or ran --no-run-branch) — continuing without checkpoints.")
        return None
    current = _current_branch()
    if current != recorded:
        raise StageFailedError(
            f"--resume: the recorded run branch {recorded!r} is not checked out "
            f"(HEAD is {current!r}) — check it out, or --start-at explicitly")
    _sync_run_branch_with_main()
    return recorded


def _sync_run_branch_with_main() -> None:
    """fetch + merge origin/main onto the run branch. CONFLICTS surface and
    STOP — never auto-resolved (the operator owns the merge)."""
    fetched = _git("fetch", "origin")
    if fetched.returncode != 0:
        print(f"[WARN] run-branch: git fetch failed ({fetched.stderr.strip()[:120]}) "
              "— SKIPPING the origin/main sync this resume (a mid-run fix PR "
              "will NOT be picked up; re-run --resume once the network is back).")
        return
    merged = _git("merge", "--no-edit", "origin/main")
    if merged.returncode != 0:
        conflicted = _git("diff", "--name-only", "--diff-filter=U").stdout.strip()
        if conflicted:
            raise StageFailedError(
                "run-branch: merging origin/main CONFLICTED — resolve by hand "
                "(never auto-resolved), `git add` + `git commit` the merge, then "
                f"--resume. Conflicted files:\n{conflicted}")
        raise StageFailedError(
            "run-branch: the origin/main merge FAILED before it began (no "
            "conflicted files — likely uncommitted output the merge would "
            "overwrite; commit or stash it, then --resume). git said:\n"
            f"{(merged.stderr or merged.stdout).strip()[:400]}")
    if "Already up to date" not in merged.stdout:
        print("[info] run-branch: merged origin/main — subprocess stages pick "
              "the fixes up from disk at the next stage.")


def _commit_stage_checkpoint(stage: str, run_id: str, branch: str | None) -> None:
    """Commit any repo-file changes a completed stage produced. Best-effort —
    checkpointing must never fail a healthy run. The pathspec EXCLUDES logs/
    (the prewipe dumps + per-run transcripts live under logs/migrations and are
    NOT repo content — committing a multi-MB dump would be the bug)."""
    if branch is None:
        return
    if not _git("status", "--porcelain", "--", ".", ":(exclude)logs").stdout.strip():
        return
    _git("add", "-A", "--", ".", ":(exclude)logs")
    result = _git("commit", "-m", f"standup {run_id}: after {stage}")
    if result.returncode != 0:
        print(f"[WARN] run-branch: checkpoint commit failed after {stage!r}: "
              f"{result.stderr.strip()[:160]} (run continues)")
    else:
        print(f"[info] run-branch: checkpoint committed — after {stage}")


def _push_run_branch(branch: str, run_id: str) -> None:
    """End-of-run: push the branch and print the landing-PR command (printed,
    never executed — the operator reviews the diff first)."""
    pushed = _git("push", "-u", "origin", branch)
    if pushed.returncode != 0:
        print(f"[WARN] run-branch: push failed ({pushed.stderr.strip()[:160]}) — "
              f"push by hand: git push -u origin {branch}")
    else:
        print(f"[ok] run branch pushed: {branch}")
    print("Open the landing PR (review the diff first):\n"
          f"    gh pr create --base main --head {branch} "
          f'--title "feat(standup): land the regenerated ID surfaces (run {run_id})"')


# ---- run-state + --resume -------------------------------------------------


def _state_path(dump_dir: pathlib.Path | None) -> pathlib.Path:
    return (dump_dir / STATE_FILE_NAME) if dump_dir is not None else NORESTORE_STATE_PATH


def _write_state(state: dict[str, Any], dump_dir: pathlib.Path | None) -> None:
    """Persist run state after every transition. Best-effort — bookkeeping must
    never kill an otherwise-healthy attended run; a failed write just means
    --resume is unavailable (the operator falls back to --start-at)."""
    try:
        state_io.atomic_write_json(_state_path(dump_dir), state)
    except OSError as exc:
        print(f"[WARN] run_state_write_failed: {exc} — run continues; "
              "--resume will not see this transition (use --start-at).")


def _resume_start_stage(dump_dir: pathlib.Path | None, *, no_restore: bool,
                        skip_shares: bool, no_run_branch: bool,
                        skip_restore_sheets: Sequence[str] = (),
                        stage_names: list[str]) -> str:
    """Derive the restart stage from the persisted run state, refusing LOUDLY on
    every ambiguous case: no state, a corrupt state file, a completed run, or
    flags that conflict with the recorded run (a different flag set produces a
    different stage list — or, for run-branch mode, silently drops the recorded
    branch's checkpoints — so 'first incomplete stage' would skip or re-run
    work)."""
    path = _state_path(dump_dir)
    if not path.is_file():
        raise StageFailedError(
            f"--resume: no run state at {path} — nothing to resume (a run older "
            "than the state feature, or a fresh tenant): use --start-at explicitly")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise StageFailedError(
            f"--resume: run state UNREADABLE at {path} ({exc}) — "
            "use --start-at explicitly") from exc
    if state.get("status") == "complete":
        raise StageFailedError(
            "--resume: the recorded run is COMPLETE — start a fresh run, or pass "
            "--start-at explicitly to re-run a stage")
    recorded = state.get("flags", {})
    # .get(k, <default>): older state files never recorded the newer flags
    # (no_run_branch pre-#687, skip_restore_sheets pre-selector); absent means
    # the run used the flag's default, not a conflict. skip sets compare SORTED —
    # repeat order is meaningless, only membership changes the restore work.
    supplied: dict[str, Any] = {
        "no_restore": no_restore, "skip_shares": skip_shares,
        "no_run_branch": no_run_branch,
        "skip_restore_sheets": sorted(skip_restore_sheets)}
    recorded_norm: dict[str, Any] = {
        "no_restore": recorded.get("no_restore", False),
        "skip_shares": recorded.get("skip_shares", False),
        "no_run_branch": recorded.get("no_run_branch", False),
        "skip_restore_sheets": sorted(recorded.get("skip_restore_sheets") or [])}
    conflicts = {k: (recorded_norm[k], v) for k, v in supplied.items()
                 if recorded_norm[k] != v}
    if conflicts:
        raise StageFailedError(
            f"--resume: supplied flags conflict with the recorded run "
            f"({conflicts}, recorded->supplied) — rerun with the recorded flags "
            "or start fresh")
    completed = set(state.get("completed", []))
    for name in stage_names:
        if name not in completed:
            return name
    raise StageFailedError(
        "--resume: every stage is recorded complete but status != complete — "
        "inspect the state file before resuming")



# ---- `standup.py finish` — the post-merge epilogue as mechanism -------------
#
# Runs ONLY after the regen landing PR has merged and ~/its is pulled (it
# verifies both). Codifies the 4-step printed epilogue the 2026-07-23 run
# executed by hand at resume-point fatigue: state cleanup -> posture-driven
# fleet reload -> bounded heartbeat wait -> post-reload error sweep -> the
# §44 gate-flip report (READ-ONLY) -> dashboard restart LAST. `--verify-only`
# re-runs just the read-only checks (safe any time, the sheet_ids_regen
# --check posture). Runbook: docs/runbooks/tenant_standup.md.


def _verify_git_clean() -> bool:
    """Precondition: ~-checkout is a clean origin/main tree (VC-07, subprocessed)."""
    rc = subprocess.run(
        [sys.executable, "-m", "scripts.verify_cutover", "--only", "git"],
        cwd=REPO_ROOT, check=False).returncode
    return rc == 0


def _verify_regen_parity() -> bool:
    """Precondition: shared/sheet_ids.py matches the live tenant (regen --check)."""
    rc = subprocess.run(
        [sys.executable, str(REPO_ROOT / MIGRATIONS / "sheet_ids_regen.py"), "--check"],
        cwd=REPO_ROOT, check=False).returncode
    return rc == 0


def _state_cleanup() -> bool:
    """Delete heartbeat_row_ids.json under its sidecar lock; print the Check-U note.

    Returns False (abort) on a lock timeout — a held lock means something is
    STILL writing heartbeat state, i.e. a daemon survived the daemons-down
    precondition.
    """
    if HEARTBEAT_ROW_IDS_PATH.exists():
        try:
            with state_io.with_path_lock(HEARTBEAT_ROW_IDS_PATH):
                HEARTBEAT_ROW_IDS_PATH.unlink(missing_ok=True)
        except state_io.StateLockTimeoutError:
            print(f"[abort] {HEARTBEAT_ROW_IDS_PATH} is LOCKED — something is still "
                  "writing heartbeat state (a daemon survived the precondition?).")
            return False
        print(f"[ok] removed {HEARTBEAT_ROW_IDS_PATH} — daemons re-provision their "
              "ITS_Daemon_Health rows on first cycle.")
    else:
        print(f"[skip] {HEARTBEAT_ROW_IDS_PATH} already absent.")
    print("[note] review ~/its/state/approver_set_baseline.json (watchdog Check U) "
          "after any share change — the rebuilt workspaces re-baseline on first sweep.")
    return True


def _install_load(plist_name: str) -> int:
    """One install.sh load — THE reload seam (tests record calls here)."""
    return subprocess.run(["bash", str(INSTALL_SH), "load", plist_name],
                          cwd=REPO_ROOT, check=False).returncode


def _reload_fleet(posture: str) -> list[str]:
    """Load every shipped plist per the posture table. Returns failed labels.

    dark  — every plist EXCEPT the send-dispatch five (SEND_DISPATCH_LABELS)
            and the dashboard (restarted LAST by _restart_dashboard).
    full  — everything except the dashboard; the caller has ALREADY passed the
            typed-phrase gate (never gate here — tests drive this directly).
    """
    failures: list[str] = []
    for plist_path in sorted(LAUNCHD_DIR.glob("org.solutionsmith.its.*.plist")):
        label = plist_path.name.removesuffix(".plist")
        if label == DASHBOARD_LABEL:
            continue  # restarted last, after the verifies
        if posture == "dark" and label in SEND_DISPATCH_LABELS:
            print(f"[skip] {label} — send-dispatch plist stays UNLOADED "
                  "(--posture dark; loading it is a FIXED External-Send-Gate action)")
            continue
        if _install_load(plist_path.name) != 0:
            failures.append(label)
            print(f"[ERROR] load failed: {label}")
    return failures


def _confirm_full_posture() -> bool:
    """Typed-phrase gate for --posture full (EOF declines; no bypass flag)."""
    print("\n--posture full will ALSO load the send-dispatch daemons:")
    for label in sorted(SEND_DISPATCH_LABELS):
        print(f"    {label}")
    print("Loading a send daemon is a FIXED External-Send-Gate operator action "
          "(Seth). Gates in ITS_Config still runtime-gate each dispatcher, but "
          "dark posture is the rebuilt-tenant default for a reason.")
    print(f'Type exactly "{FULL_POSTURE_PHRASE}" to proceed.')
    try:
        return input("> ").strip() == FULL_POSTURE_PHRASE
    except EOFError:
        return False


def _parse_heartbeat(raw: Any) -> dt.datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def _await_heartbeats(reload_start: dt.datetime, timeout_seconds: float) -> list[str]:
    """Poll ITS_Daemon_Health until every fast-interval Enabled row heartbeats
    past the reload, or the bounded timeout expires. Returns the laggards.

    Rows self-provision (Enabled=True) on each daemon's FIRST cycle, so the set
    GROWS during the wait — zero rows early on is normal, not success. Long-
    interval daemons (> HEARTBEAT_WAIT_MAX_INTERVAL_SECONDS: picklist-sync,
    watchdog) are named once as not-waited; they heartbeat within their own
    interval and VC-04 remains the steady-state gate.
    """
    from shared import smartsheet_client
    sheet_ids = _fresh_sheet_ids()
    deadline = time.monotonic() + timeout_seconds
    not_waited_reported = False
    while True:
        rows = smartsheet_client.get_rows(sheet_ids.SHEET_DAEMON_HEALTH)
        laggards: list[str] = []
        fresh = 0
        slow_rows: list[str] = []
        for row in rows:
            if not row.get("Enabled"):
                continue
            name = str(row.get("Daemon Name") or f"row {row.get('_row_id')}")
            try:
                interval = float(str(row.get("Interval Seconds")))
            except (TypeError, ValueError):
                interval = 0.0
            if interval > HEARTBEAT_WAIT_MAX_INTERVAL_SECONDS:
                slow_rows.append(name)
                continue
            heartbeat = _parse_heartbeat(row.get("Last Heartbeat"))
            if heartbeat is not None and heartbeat >= reload_start:
                fresh += 1
            else:
                laggards.append(name)
        if slow_rows and not not_waited_reported:
            print(f"[info] not cycle-waited (interval > "
                  f"{HEARTBEAT_WAIT_MAX_INTERVAL_SECONDS:g}s — they heartbeat "
                  f"within their own interval): {', '.join(sorted(slow_rows))}")
            not_waited_reported = True
        if fresh and not laggards:
            print(f"[ok] heartbeat_wait: all {fresh} fast-interval daemon row(s) "
                  "fresh since the reload.")
            return []
        if time.monotonic() > deadline:
            named = laggards if laggards else ["<no ITS_Daemon_Health rows yet>"]
            print(f"[WARN] heartbeat_wait: {len(named)} daemon(s) not yet fresh "
                  f"after {timeout_seconds:g}s: {', '.join(named)}")
            return named
        print(f"[wait] {len(laggards) if laggards else 'no rows yet'} — "
              f"{fresh} fresh; re-checking in 30s "
              f"(bounded: {max(0, int(deadline - time.monotonic()))}s left)")
        time.sleep(30)


def _post_reload_error_sweep() -> int:
    """Summarize today's ITS_Errors rows by (Script, Error, Severity); return
    the CRITICAL count. Date-granular — ITS_Errors' Timestamp carries no time,
    so same-day pre-reload rows are included (said so in the output)."""
    from shared import smartsheet_client
    sheet_ids = _fresh_sheet_ids()
    today = dt.date.today().isoformat()
    rows = smartsheet_client.get_rows(sheet_ids.SHEET_ERRORS)
    recent = [r for r in rows if str(r.get("Timestamp") or "") >= today]
    if not recent:
        print("[ok] error sweep: zero ITS_Errors rows dated today.")
        return 0
    counts: dict[tuple[str, str, str], int] = {}
    for r in recent:
        key = (str(r.get("Script") or "?"), str(r.get("Error") or "?"),
               str(r.get("Severity") or "?"))
        counts[key] = counts.get(key, 0) + 1
    criticals = 0
    print(f"[info] error sweep: {len(recent)} ITS_Errors row(s) dated {today} "
          "(date-granular — the sheet carries no time-of-day; pre-reload "
          "same-day rows are included):")
    for (script, error, severity), n in sorted(counts.items()):
        marker = " <-- CRITICAL" if severity == "CRITICAL" else ""
        if severity == "CRITICAL":
            criticals += n
        print(f"    {n:>4}x  {severity:<8} {script} / {error}{marker}")
    return criticals


def _seeded_gate_defaults() -> dict[str, str]:
    """Best-effort (setting -> seeded Value) scanned from the seeder corpus.

    Advisory third column for the gate report only — the regex reads the
    literal {"Setting": ..., "Value": ...} dict shape the seeder family uses;
    a seeder shaped differently just yields no entry (shown as '?')."""
    corpus = [REPO_ROOT / "scripts" / "seed_its_config.py",
              *sorted((REPO_ROOT / "scripts" / "migrations").glob("seed_*.py"))]
    pattern = re.compile(r'"Setting":\s*"([^"]+)"[^{}]*?"Value":\s*"([^"]*)"', re.S)
    out: dict[str, str] = {}
    for path in corpus:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in pattern.finditer(text):
            out.setdefault(m.group(1), m.group(2))
    return out


def _gate_report(dump_dir: pathlib.Path | None) -> None:
    """The §44 gate-flip worksheet: every *_enabled ITS_Config row's pre-wipe
    dump value vs live value vs seeded default, with its FULL Description
    inline (an in-cell precondition is doctrine — CL-13). REPORT ONLY, zero
    writes — every flip stays a human decision and send-adjacent flips are a
    FIXED high-capability class (Seth)."""
    from shared import smartsheet_client
    sheet_ids = _fresh_sheet_ids()
    live_rows = smartsheet_client.get_rows(sheet_ids.SHEET_CONFIG)
    gates = [r for r in live_rows if str(r.get("Setting") or "").endswith("_enabled")]
    baseline: dict[tuple[str, str], str] = {}
    if dump_dir is not None:
        dump = _find_dump_sheet(dump_dir, "ITS — System", "ITS_Config")
        if dump is None:
            print("[WARN] gate report: no ITS_Config sheet in the dump — "
                  "dump column shows '-'")
        else:
            baseline = {
                (str(row.get("Setting")), str(row.get("Workstream"))):
                    str(row.get("Value"))
                for row in dump.get("rows", [])
                if str(row.get("Setting") or "").endswith("_enabled")}
    else:
        print("[info] gate report: no dump baseline (--no-restore) — "
              "dump column shows '-'")
    seeded = _seeded_gate_defaults()
    print(f"\n== GATE-FLIP REPORT ({len(gates)} *_enabled row(s)) — READ-ONLY: "
          "every flip is a human decision; send-adjacent flips are FIXED "
          "high-class (Seth). Read each Description FIRST (CL-13). ==")
    for row in sorted(gates, key=lambda r: (str(r.get("Setting")),
                                            str(r.get("Workstream")))):
        setting = str(row.get("Setting"))
        workstream = str(row.get("Workstream"))
        live = str(row.get("Value"))
        dump_value = baseline.get((setting, workstream), "-")
        seed_value = seeded.get(setting, "?")
        drift = ""
        if dump_value not in ("-", live):
            drift = "   <-- DIFFERS from pre-wipe"
        print(f"  {setting} [{workstream}]")
        print(f"      dump={dump_value!r}  live={live!r}  seeded={seed_value!r}{drift}")
        description = str(row.get("Description") or "").strip()
        if description:
            print(f"      Description: {description}")


def _restart_dashboard() -> list[str]:
    """Restart (or first-load) the dashboard LAST so it re-imports the merged
    sheet_ids — KeepAlive never restarts a healthy process, so without this it
    serves dead frozen ids indefinitely after a rebuild."""
    failures: list[str] = []
    if DASHBOARD_LABEL in _loaded_its_labels():
        rc = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{DASHBOARD_LABEL}"],
            check=False).returncode
        if rc == 0:
            print(f"[ok] {DASHBOARD_LABEL} kickstarted (re-imports merged sheet_ids).")
        else:
            failures.append(DASHBOARD_LABEL)
            print(f"[ERROR] dashboard kickstart failed (rc={rc}).")
    else:
        if _install_load(f"{DASHBOARD_LABEL}.plist") == 0:
            print(f"[ok] {DASHBOARD_LABEL} loaded.")
        else:
            failures.append(DASHBOARD_LABEL)
            print("[ERROR] dashboard load failed.")
    return failures


def finish_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="standup.py finish",
        description="Post-merge finish: state cleanup + posture-driven fleet "
                    "reload + heartbeat wait + error sweep + gate report + "
                    "dashboard restart LAST. Runs AFTER the regen PR merged "
                    "and ~/its was pulled.")
    parser.add_argument("--posture", choices=("dark", "full"), default="dark",
                        help="dark (default): send-dispatch plists stay UNLOADED. "
                             "full: load everything — extra typed confirmation.")
    parser.add_argument("--verify-only", action="store_true",
                        help="read-only re-run: preconditions + heartbeat check + "
                             "error sweep + gate report; no cleanup, no reload, "
                             "no dashboard restart. Safe any time.")
    parser.add_argument("--dump", type=pathlib.Path, default=None,
                        help="prewipe dump dir for the gate-report baseline "
                             "(default: auto-resolve; missing dump degrades the "
                             "report, never aborts).")
    parser.add_argument("--no-restore", action="store_true",
                        help="no dump baseline (fresh-tenant finish).")
    parser.add_argument("--heartbeat-timeout", type=float, default=1200.0,
                        help="bounded heartbeat wait in seconds (default 1200).")
    args = parser.parse_args(argv)

    dump_dir: pathlib.Path | None = None
    if not args.no_restore:
        try:
            dump_dir = _resolve_dump_dir(args.dump)
        except StageFailedError as exc:
            print(f"[WARN] no dump baseline: {exc} — the gate report will show "
                  "live values only.")

    print("== standup finish: preconditions ==")
    if not _verify_git_clean():
        print("[abort] finish precondition failed: the checkout is not a clean "
              "origin/main tree (has the landing PR merged and this tree pulled?).")
        return 1
    if not _verify_regen_parity():
        print("[abort] finish precondition failed: sheet_ids_regen --check "
              "MISMATCH — the ID surfaces on disk do not match the live tenant. "
              "Merge + pull the landing PR first; never hand-run --write here.")
        return 1
    if not args.verify_only:
        if not _require_daemons_down():
            print("[abort] finish precondition failed: daemons still loaded "
                  "(finish performs the reload itself).")
            return 1
        if args.posture == "full" and not _confirm_full_posture():
            print("[abort] --posture full not confirmed; nothing was done "
                  "(re-run with --posture dark, or confirm the phrase).")
            return 1
        if not _confirm("Proceed with the post-merge finish (state cleanup + "
                        f"fleet reload, posture={args.posture})?"):
            print("[skip] Operator declined; nothing run.")
            return 0

    failures: list[str] = []
    reload_start = dt.datetime.now(dt.UTC)
    if not args.verify_only:
        print("\n== state cleanup ==")
        if not _state_cleanup():
            return 1
        print(f"\n== fleet reload (--posture {args.posture}) ==")
        failures += _reload_fleet(args.posture)
        # Postcondition (dark): no send-dispatch daemon may be live.
        if args.posture == "dark":
            hot = sorted(set(_loaded_its_labels()) & SEND_DISPATCH_LABELS)
            if hot:
                failures += hot
                print(f"[ERROR] send-dispatch daemon LOADED under dark posture "
                      f"(External-Send-Gate violation): {', '.join(hot)} — "
                      "unload immediately (launchctl bootout) and investigate.")

    print("\n== heartbeat wait ==")
    laggards = _await_heartbeats(reload_start if not args.verify_only
                                 else dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
                                 args.heartbeat_timeout if not args.verify_only else 0)

    print("\n== post-reload error sweep ==")
    criticals = _post_reload_error_sweep()

    _gate_report(dump_dir)

    if not args.verify_only:
        print("\n== dashboard restart (last) ==")
        failures += _restart_dashboard()

    print("\n== finish summary ==")
    # The ACT-fence marker is cleared only on full stand-up completion (#674);
    # a permanently-abandoned run leaves it behind, and a clean finish would
    # otherwise restart a dashboard whose ACT verbs stay 503-fenced for up to
    # 6h with no hint here. Informational only — never changes the exit code.
    if STANDUP_MARKER_PATH.exists():
        print(f"[note] stand-up fence marker still present at "
              f"{STANDUP_MARKER_PATH} — dashboard ACT verbs stay fenced until "
              "its 6h age-out (an abandoned run never cleared it). Remove the "
              "file to unfence once the tenant is verified consistent.")
    if failures:
        print(f"[FAIL] {len(failures)} load/restart failure(s): {', '.join(failures)}")
    if laggards:
        print(f"[WARN] heartbeat laggards: {', '.join(laggards)} — "
              "re-check with: standup.py finish --verify-only")
    if criticals:
        print(f"[FAIL] {criticals} CRITICAL ITS_Errors row(s) dated today — "
              "escalate with the sweep summary above (do not clear the rows).")
    if not failures and not laggards and not criticals:
        print("[ok] finish clean: fleet reloaded, heartbeats fresh, no CRITICALs. "
              "Gate flips (if any) remain the operator's §44 decision — see the "
              "report above.")
        return 0
    return 1


def main() -> int:
    # Subcommand dispatch: `standup.py finish [...]` is the post-merge epilogue
    # (a SEPARATE invocation by design — it runs only after the regen landing
    # PR merged and this tree was pulled, which the run itself cannot outlive).
    if len(sys.argv) > 1 and sys.argv[1] == "finish":
        return finish_main(sys.argv[2:])
    global _current_stage, _run_log_path
    parser = argparse.ArgumentParser(
        description="Orchestrated tenant stand-up: builders + auto-FLIP + seeds.")
    parser.add_argument("--dump", type=pathlib.Path, default=None,
                        help="prewipe dump dir for row/share restore "
                             "(default: newest logs/migrations/prewipe_*).")
    parser.add_argument("--no-restore", action="store_true",
                        help="Fresh-tenant mode: skip row/share restore entirely.")
    parser.add_argument("--skip-shares", action="store_true",
                        help="Skip the workspace share restore stage.")
    parser.add_argument("--skip-restore-sheet", action="append", default=[],
                        metavar="NAME", dest="skip_restore_sheet",
                        help="Hold back ONE named RESTORE_SHEETS sheet from the row "
                             "restore (repeatable; unknown names abort at startup). "
                             "Production stand-up shape: skip ITS_Active_Jobs and "
                             "ITS_Active_Jobs_Progress to restore the reference "
                             "sheets while both Active-Jobs sheets start empty.")
    parser.add_argument("--start-at", default=None, metavar="STAGE",
                        help="Resume from a named stage (see --list). The explicit "
                             "manual override — wins over --resume.")
    parser.add_argument("--resume", action="store_true",
                        help="Derive the restart stage from the persisted run state "
                             "(standup_state.json beside the dump). Refuses on a "
                             "completed run or conflicting flags.")
    parser.add_argument("--no-run-branch", action="store_true",
                        help="Skip the per-run git branch (checkpoints + "
                             "merge-main + landing-PR push); you manage git "
                             "yourself. Default: run-branch mode ON.")
    parser.add_argument("--list", action="store_true", help="Print stages and exit.")
    args = parser.parse_args()

    try:
        skip_restore_sheets = _validate_skip_restore_sheets(args.skip_restore_sheet)
    except StageFailedError as exc:
        print(f"[abort] {exc}")
        return 1
    dump_dir: pathlib.Path | None = None
    if not args.no_restore:
        try:
            dump_dir = _resolve_dump_dir(args.dump)
        except StageFailedError as exc:
            print(f"[abort] {exc}")
            return 1
    stages = build_stages(dump_dir, skip_shares=args.skip_shares,
                          skip_restore_sheets=skip_restore_sheets)

    if args.list:
        for name, _fn in stages:
            print(name)
        return 0

    names = [n for n, _ in stages]
    start = 0
    if args.start_at:
        if args.start_at not in names:
            print(f"[abort] unknown stage {args.start_at!r}. --list shows stages.")
            return 1
        start = names.index(args.start_at)
        if args.resume:
            print(f"[info] --start-at {args.start_at!r} OVERRIDES --resume "
                  "(explicit wins).")
    elif args.resume:
        try:
            resume_stage = _resume_start_stage(
                dump_dir, no_restore=args.no_restore,
                skip_shares=args.skip_shares,
                no_run_branch=args.no_run_branch,
                skip_restore_sheets=skip_restore_sheets, stage_names=names)
        except StageFailedError as exc:
            print(f"[abort] {exc}")
            return 1
        start = names.index(resume_stage)
        print(f"[info] --resume: run state says the first incomplete stage is "
              f"{resume_stage!r}.")

    if not _require_daemons_down():
        return 1
    print(f"[info] {len(stages)} stages; starting at {names[start]!r}. "
          f"Dump: {dump_dir if dump_dir else 'NONE (fresh-tenant mode)'}")
    if not _confirm("Proceed with the LIVE tenant stand-up?"):
        print("[skip] Operator declined; nothing run.")
        return 0

    _write_standup_marker()
    run_id = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_branch: str | None = None
    if not args.no_run_branch:
        try:
            if args.resume and not args.start_at:
                try:
                    prior = json.loads(
                        _state_path(dump_dir).read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as exc:
                    raise StageFailedError(
                        f"--resume: run state unreadable re-reading "
                        f"{_state_path(dump_dir)} ({exc})") from exc
                run_branch = _resume_run_branch(prior.get("run_branch"))
            else:
                run_branch = _start_run_branch(run_id)
        except StageFailedError as exc:
            print(f"[abort] {exc}")
            return 1
    log_dir = dump_dir if dump_dir is not None else DUMP_ROOT
    _run_log_path = log_dir / (f"standup_{run_id}.log" if dump_dir is not None
                               else f"standup_norestore_{run_id}.log")
    print(f"[info] per-run transcript: {_run_log_path}")
    # Positional truth: stages run strictly in order, so 'completed' is always
    # the prefix before the start index (a --start-at override treats earlier
    # stages as done — the operator's explicit call).
    state: dict[str, Any] = {
        "run_id": run_id,
        "status": "running",
        "started_utc": run_id,
        "flags": {"no_restore": args.no_restore, "skip_shares": args.skip_shares,
                  "no_run_branch": args.no_run_branch,
                  "skip_restore_sheets": sorted(skip_restore_sheets),
                  "dump_dir": str(dump_dir) if dump_dir else None},
        "stage_names": names,
        "run_branch": run_branch,
        "completed": names[:start],
        "current_stage": None,
        "stages": {},
        "log_file": str(_run_log_path),
    }
    _write_state(state, dump_dir)

    # The whole mutating stretch runs under the stand-up ACT-fence marker
    # (PR #674): written/refreshed here, cleared ONLY on full completion below,
    # so the dashboard's fenced window covers the fix->resume gap too.
    _write_standup_marker()
    for name, fn in stages[start:]:
        _current_stage = name
        state["current_stage"] = name
        state["stages"][name] = {
            "started_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
        _write_state(state, dump_dir)
        _emit(f"\n=== STAGE {name} ===")
        try:
            fn()
        except KeyboardInterrupt:
            state["status"] = "failed"
            _write_state(state, dump_dir)
            _emit(f"\n[abort] interrupted during stage {name!r}. Every builder is "
                  f"idempotent — resume: python3 {MIGRATIONS}/standup.py --resume "
                  f"(--start-at {name} also works, but mints a FRESH run branch "
                  "and skips the origin/main fix-PR sync)")
            return 1
        except Exception as exc:  # noqa: BLE001 — ANY stage failure gets the resume hint
            state["status"] = "failed"
            _write_state(state, dump_dir)
            label = type(exc).__name__ if not isinstance(exc, StageFailedError) else "stage"
            _emit(f"\n[abort] stage {name!r} failed ({label}): {exc}\n"
                  f"Fix, then resume: python3 {MIGRATIONS}/standup.py --resume "
                  f"(--start-at {name} also works, but mints a FRESH run branch "
                  "and skips the origin/main fix-PR sync)")
            return 1
        state["completed"].append(name)
        state["stages"][name]["finished_utc"] = dt.datetime.now(
            dt.UTC).isoformat(timespec="seconds")
        _write_state(state, dump_dir)
        _commit_stage_checkpoint(name, run_id, run_branch)
    state["status"] = "complete"
    state["current_stage"] = None
    _write_state(state, dump_dir)
    # Marker clears ONLY here (full completion) — an aborted run above returns
    # early and stays fenced across the fix->resume gap (PR #674 lifecycle).
    _clear_standup_marker()
    if run_branch is not None:
        _push_run_branch(run_branch, run_id)
    print(
        "\n[ok] Stand-up complete. Epilogue:\n"
        "  1. git diff — review the regenerated ID surfaces (sheet_ids.py, "
        "week_folder.py); reconcile any sweep-reported pins; land it all via PR.\n"
        "  2. After the PR merges and ~/its is pulled:\n"
        f"       python3 {MIGRATIONS}/standup.py finish\n"
        "     (state cleanup -> DARK fleet reload [send-dispatch plists stay "
        "unloaded] -> heartbeat wait -> error sweep -> the read-only gate-flip "
        "report -> dashboard restart LAST).\n"
        "  3. Gate flips stay §44 human decisions — read each Description first; "
        "send-gate flips + --posture full escalate to Seth. Runbook: "
        "docs/runbooks/tenant_standup.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
