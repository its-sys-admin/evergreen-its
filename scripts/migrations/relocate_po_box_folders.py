#!/usr/bin/env python3
"""Relocate the PO lane's Box content out of the safety tree into its OWN root (2026-08-11).

Why
---
Until 2026-08-11 the PO lane (po_poll / rfq_poll / estimate_poll) filed under the SHARED
safety mirror root: `<safety root>/<Job>/Purchase Orders/{*.pdf, RFQs/, Vendor Quotes/}`.
The operator directive split the lane onto its own root (`po_naming.CFG_BOX_PORTAL_ROOT`,
built by build_box_roots.py as "ITS Purchase Orders"), where the per-job folder holds the
PO PDFs directly with `RFQs/` and `Vendor Quotes/` as children — i.e. EXACTLY the contents
of the old "Purchase Orders" subfolder. This migration moves the existing content across;
without it, the first post-split filing find-or-creates a FRESH `<PO root>/<Job>` beside
the stranded old subtree and the job's PO history splits across two trees with no merge
primitive on either side.

What it does (three passes, each independently idempotent)
----------------------------------------------------------
  1. LIVE PO RELOCATION — for each `<Job>` child of the safety root that has a
     "Purchase Orders" child: ONE atomic Box move+rename
     (`<safety root>/<Job>/Purchase Orders` -> `<PO root>/<Job>`). Box's PUT carries
     parent+name together, so there is no moved-but-unrenamed window. If `<PO root>`
     already holds a `<Job>` folder the move is REFUSED for that job (no merge
     primitive; reconcile by hand) — this is why the migration runs BEFORE the lane
     files anything to the new root.
  2. ID-NAMED-FOLDER REPAIR — `manifest_poll` used to key the per-job folder off the
     raw job_id, growing e.g. `<safety root>/JOB-000031/Materials/` beside the job's
     real `<safety root>/Test/` folder (fixed in the same change: the Worker now joins
     jobs.project_name into the pending payload). For each safety-root child whose name
     matches a "Job ID" in ITS_Active_Jobs: move each of its children into the job's
     PROJECT-NAME folder (find-or-create). The emptied id-named folder is left behind
     and named in the summary — `shared/box_client` is MOVE-ONLY by design (no delete
     primitive), so trashing the husk is a manual Box-UI step.
  3. ARCHIVE-SIDE RELOCATION — for each `<Job>` child of the Box ARCHIVE root that has
     `Safety/Purchase Orders`: move it up to `<archive>/<Job>/Purchase Orders`, where
     the new `box:purchase_orders` archive slot's RESTORE direction looks for it. A job
     archived before the split would otherwise restore its PO content back into the
     safety tree.

Safety posture
--------------
  - PLAN-BY-DEFAULT: running with no flags prints the full plan and moves nothing.
    `--apply` actuates, behind a y/N confirmation naming the authenticated Box login.
  - MOVE-ONLY: no delete, no upload, no rename-in-place of anything outside the plan.
  - COLLISION = REFUSE-AND-REPORT, never merge (the job_archive / box_client posture).
  - Re-run safe: a completed pass finds nothing to do and prints `[skip]` lines.

Run AFTER build_box_roots.py has created "ITS Purchase Orders" and its ITS_Config row
is seeded, and BEFORE (or immediately after) the code that files to the new root goes
live — the collision refusal makes a late run safe but noisy.

Exit 0 on success/no-op/declined confirmation; 2 on missing config; 3 on Box error.
Auth: Box OAuth + Smartsheet PAT from macOS Keychain via the shared clients.
No send capability, no AI (imports neither graph_client/resend_client nor anthropic).
"""
from __future__ import annotations

import argparse
import re
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

from po_materials import po_naming  # noqa: E402
from safety_reports import safety_naming  # noqa: E402
from shared import box_client, smartsheet_client  # noqa: E402
from shared.sheet_ids import SHEET_ACTIVE_JOBS  # noqa: E402

PO_SUBFOLDER = "Purchase Orders"
MATERIALS_SUBFOLDER = "Materials"
SAFETY_ARCHIVE_LABEL = "Safety"
# The Track 6 Box archive root — job_archive.CFG_BOX_ARCHIVE_ROOT, referenced as a literal
# for the same reason job_archive itself avoids heavy imports: this is a leaf one-shot.
CFG_BOX_ARCHIVE_ROOT = "field_ops.box.archive_root_folder_id"

_JOB_ID_SHAPE = re.compile(r"^JOB-\d+$")


def _read_root(key: str, workstream: str, what: str) -> str:
    value = (smartsheet_client.get_setting(key, workstream=workstream) or "").strip()
    if not value:
        print(f"[error] {what} unset (ITS_Config {key!r}, Workstream {workstream!r}).", file=sys.stderr)
        raise SystemExit(2)
    return value


def _children(folder_id: str) -> list[dict[str, str]]:
    return [i for i in box_client.list_folder(folder_id, limit=1000) if i["type"] == "folder"]


def _active_job_names_by_id() -> dict[str, str]:
    """{Job ID -> Project Name} from ITS_Active_Jobs (rows missing either are skipped)."""
    out: dict[str, str] = {}
    for row in smartsheet_client.get_rows(SHEET_ACTIVE_JOBS):
        job_id = str(row.get("Job ID") or "").strip()
        name = str(row.get("Project Name") or "").strip()
        if job_id and name:
            out[job_id] = name
    return out


def plan_live_po_moves(safety_root: str, po_root: str) -> tuple[list[tuple[str, str, str]], list[str]]:
    """[(po_folder_id, job_folder_name, note)] to move, + collision refusals."""
    po_root_names = {c["name"] for c in _children(po_root)}
    moves: list[tuple[str, str, str]] = []
    refusals: list[str] = []
    for job in _children(safety_root):
        po_id = box_client.find_child_folder(job["id"], PO_SUBFOLDER)
        if po_id is None:
            continue
        if job["name"] in po_root_names:
            refusals.append(
                f"{job['name']}: <PO root> already holds a folder named {job['name']!r} — "
                f"REFUSING to merge; reconcile by hand (move the old contents in the Box UI)."
            )
            continue
        moves.append((po_id, job["name"], f"<safety>/{job['name']}/Purchase Orders -> <PO root>/{job['name']}"))
    return moves, refusals


def plan_id_folder_repairs(
    safety_root: str, names_by_id: dict[str, str]
) -> tuple[list[tuple[str, str, str, str]], list[str]]:
    """([(child_id, child_name, target_project_folder_name, husk_name)], refusals) for pass 2.

    Collision-checked at PLAN time like passes 1 and 3 (the REFUSE-AND-REPORT posture): if
    the job's project-name folder ALREADY holds a child of the same name — entirely
    plausible, since the manifest_poll fix ships in the same change and a manifest arriving
    before this migration runs find-or-creates a real `<project>/Materials` — the move is
    refused for hand-reconciliation, never merged."""
    repairs: list[tuple[str, str, str, str]] = []
    refusals: list[str] = []
    for child in _children(safety_root):
        if not _JOB_ID_SHAPE.match(child["name"]):
            continue
        project = names_by_id.get(child["name"])
        if not project:
            print(f"[warn] id-named folder {child['name']!r} has no ITS_Active_Jobs row — left untouched.")
            continue
        target = safety_naming.job_folder_name(project)
        target_id = box_client.find_child_folder(safety_root, target)
        occupied: set[str] = set()
        if target_id is not None:
            occupied = {c["name"] for c in _children(target_id)}
        for grand in _children(child["id"]):
            if grand["name"] in occupied:
                refusals.append(
                    f"{child['name']}/{grand['name']}: <safety>/{target} already holds a folder "
                    f"named {grand['name']!r} — REFUSING to merge; reconcile by hand in the Box UI."
                )
                continue
            repairs.append((grand["id"], grand["name"], target, child["name"]))
    return repairs, refusals


def plan_archive_moves(archive_root: str) -> list[tuple[str, str, str]]:
    """[(po_folder_id, archive_job_folder_id, job_name)] for pass 3."""
    moves: list[tuple[str, str, str]] = []
    for job in _children(archive_root):
        safety_id = box_client.find_child_folder(job["id"], SAFETY_ARCHIVE_LABEL)
        if safety_id is None:
            continue
        po_id = box_client.find_child_folder(safety_id, PO_SUBFOLDER)
        if po_id is None:
            continue
        if box_client.find_child_folder(job["id"], PO_SUBFOLDER) is not None:
            print(f"[warn] archive/{job['name']} already holds 'Purchase Orders' — REFUSING to merge.")
            continue
        moves.append((po_id, job["id"], job["name"]))
    return moves


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="actuate the plan (default: print it)")
    args = parser.parse_args()

    safety_root = _read_root(safety_naming.CFG_BOX_PORTAL_ROOT, "safety_reports", "safety Box root")
    po_root = _read_root(
        po_naming.CFG_BOX_PORTAL_ROOT, po_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM, "PO Box root"
    )
    archive_root = _read_root(CFG_BOX_ARCHIVE_ROOT, "field_ops", "Box archive root")

    live_moves, refusals = plan_live_po_moves(safety_root, po_root)
    repairs, repair_refusals = plan_id_folder_repairs(safety_root, _active_job_names_by_id())
    archive_moves = plan_archive_moves(archive_root)

    print(f"\n=== PLAN ({'APPLY' if args.apply else 'dry-run — pass --apply to actuate'}) ===")
    print(f"Pass 1 — live PO relocations: {len(live_moves)}")
    for _, _name, note in live_moves:
        print(f"  [move] {note}")
    for r in refusals:
        print(f"  [REFUSED] {r}")
    print(f"Pass 2 — id-named-folder repairs: {len(repairs)}")
    for _, child_name, target, husk in repairs:
        print(f"  [move] <safety>/{husk}/{child_name} -> <safety>/{target}/{child_name}")
    for r in repair_refusals:
        print(f"  [REFUSED] {r}")
    print(f"Pass 3 — archive-side PO relocations: {len(archive_moves)}")
    for _, _, job_name in archive_moves:
        print(f"  [move] <archive>/{job_name}/Safety/Purchase Orders -> <archive>/{job_name}/Purchase Orders")

    if not args.apply:
        return 0
    if not (live_moves or repairs or archive_moves):
        print("[skip] nothing to move — already migrated.")
        return 0
    try:
        answer = input("Apply the plan above? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer != "y":
        print("Declined — nothing moved.")
        return 0

    # Per-move fence: one failed move must never abort the run — the remaining moves (and
    # pass 3, and the husk summary) still happen, and every failure is re-listed at the end
    # so the operator has the full reconciliation list. Each move is independently
    # idempotent, so a re-run retries only what failed.
    failures: list[str] = []

    def _try_move(what: str, fn) -> None:  # type: ignore[no-untyped-def]
        try:
            fn()
            print(f"  [ok] {what}")
        except Exception as exc:  # noqa: BLE001 — per-move fence; report, never abort
            failures.append(f"{what}: {type(exc).__name__}: {exc}")
            print(f"  [FAILED] {what}: {type(exc).__name__}: {exc}")

    for po_id, job_name, note in live_moves:
        _try_move(note, lambda p=po_id, j=job_name: box_client.move_folder(p, po_root, new_name=j))
    husks: set[str] = set()
    for child_id, child_name, target, husk in repairs:
        def _repair(c=child_id, cn=child_name, t=target):  # type: ignore[no-untyped-def]
            target_id = box_client.get_or_create_folder(safety_root, t)
            # new_name is passed even though the name is unchanged: it is what lets
            # move_folder's 409 handler distinguish a replay (this very folder already
            # there — adopt) from a genuine collision (a DIFFERENT folder — re-raise).
            box_client.move_folder(c, target_id, new_name=cn)
        _try_move(f"{husk}/{child_name} -> {target}/{child_name}", _repair)
        husks.add(husk)
    for po_id, job_folder_id, job_name in archive_moves:
        _try_move(
            f"archive/{job_name}: Safety/Purchase Orders -> Purchase Orders",
            lambda p=po_id, f=job_folder_id: box_client.move_folder(p, f, new_name=PO_SUBFOLDER),
        )
    for husk in sorted(husks):
        print(f"[note] emptied id-named folder {husk!r} left in the safety root — box_client is "
              f"MOVE-ONLY (no delete primitive); trash it in the Box UI when convenient.")
    if failures:
        print(f"\n{len(failures)} move(s) FAILED — re-run retries them (each move is idempotent):")
        for f in failures:
            print(f"  [FAILED] {f}")
        return 3
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
