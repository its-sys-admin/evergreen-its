"""ROADMAP Track 6 — relocate a closed job's containers into the archive, and back.

WHAT THIS REPLACES, AND WHY
---------------------------
The pre-Track-6 archive fired from inside `fieldops_sync._mirror_job`: any mirror of a job whose
lifecycle read `archived` moved four tracker SHEETS into a flat `Closed Projects` folder. Its
LOCATION was the defect. That is the job-dirty path, and the job is mark-synced immediately after,
so a failed move was permanent — `_warn_archive_move_failed` said "no auto-retry" in its own WARN
text. Coupled to an unconfirmed portal dropdown, it was a one-click, un-retryable, four-sheet
relocation whose only feedback was a WARN row. It never fired against live data; it was one
selection away.

This pass is the fifth instance of an established shape (`_mirror_hours_pass`,
`_mirror_equipment_pass`, `_mirror_material_list_pass`, `_mirror_material_incidents_pass`): its own
drained queue, its own gate, its own fences, and a commit point that reports back. Failures now
genuinely retry, because the queue keeps serving a job until its archive is terminal.

THE EIGHT CONTAINERS
--------------------
A job owns a FOLDER per workstream, not a loose set of sheets — so this moves folders, and one
move relocates the whole subtree.

    Smartsheet  ITS — Archive / <Job> / {Safety, Progress, Purchase Orders, Subcontracts}/
    Box         ITS Archive   / <Job> / {Safety, Progress, Purchase Orders, Subcontracts}/

Eight, not eleven: the PO lane owns its OWN Box root since 2026-08-11
(`po_naming.CFG_BOX_PORTAL_ROOT` — its per-job folder holds the PO PDFs plus the `RFQs/` and
`Vendor Quotes/` subtrees, so ONE slot moves all three) and the subcontract lane since
2026-08-12 (`subcontract_naming.CFG_BOX_PORTAL_ROOT`), while
`safety_reports.box.portal_root_folder_id` remains the SHARED Box root for safety AND the
materials manifests, so moving `<safety root>/<Job>` still carries those.

THE TWO SYSTEMS FAIL DIFFERENTLY
--------------------------------
Smartsheet's move endpoint CANNOT rename (`newName` is a Copy-Folder parameter that `/move`
silently ignores), so each Smartsheet container is a two-call sequence — non-atomic, with a real
crash window in between. Box's `Item.move(parent, name=)` does both in one PUT, so it has no such
window. That asymmetry is why the resume logic differs per system, and it is the single most
important thing to understand before editing this file.

BOTH DIRECTIONS, AND THE ORDER INVERTS
--------------------------------------
`run_archive_pass` is the entry point; it dispatches on the queue row's `archive_direction`
because `/archive-pending` serves both and running the wrong one is SILENT (an un-archive row put
through `archive_job` finds nothing live, reports eight clean "nothing to move" successes, and
posts `complete` while every folder stays archived).

On Smartsheet the two-call order is opposite per direction, and both orders are chosen so the
crash window can never leave a MIS-NAMED folder in the LIVE tree — because every live path
(`week_sheet._ensure_job_folder`, `hours_log._ensure_job_folder`, `job_sheet.ensure_job_sheet`)
find-or-CREATEs by job name, so a live folder under the wrong name is invisible to them and they
will grow a duplicate beside it:

    archive    move → rename    (renaming first would hide it from the live finders mid-flight)
    unarchive  rename → move    (moving first would drop a folder named `Safety` into the live tree)

Each direction's residual window is therefore confined to the ARCHIVE side, where nothing
find-or-creates, and each is repaired by re-issuing the second call. Box needs neither order.

RESUME KEYS OFF IDS, NEVER NAMES
--------------------------------
After a crash a container may have moved but not been renamed, so "did this finish?" cannot be
answered by looking for a name in the source: `week_sheet._ensure_job_folder`,
`hours_log._ensure_job_folder` and `job_sheet.ensure_job_sheet` all find-or-CREATE by job name, so
the live tree re-grows that name the moment anything is filed. The probe therefore reads the
RECORDED folder id's current name (`smartsheet_client.get_folder_name`) and compares it to the
expected label.

CAPABILITY GATED (Invariant 1): no AI, no send path. Enrolled in `tests/test_capability_gating.py`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from po_materials import po_naming
from progress_reports import equipment_status, hours_log, material_incidents, material_list
from safety_reports import safety_naming
from shared import box_client, error_log, sheet_ids, smartsheet_client
from shared.error_log import Severity
from shared.required_config import ConfigKey
from subcontracts import subcontract_naming

SCRIPT_NAME = "job_archive"

# ITS_Config key + Workstream cell for the Box ARCHIVE root — the Box twin of
# `sheet_ids.WORKSPACE_ARCHIVE`. Owned here because the archive is its only consumer; the folder
# is built by `scripts/migrations/build_box_roots.py` and seeded by `scripts/migrations/standup.py`.
#
# The `.archive_root_folder_id` SUFFIX is load-bearing at cutover: `production_repoint.py` repoints
# only Setting names matching its `ALLOWED_SETTING_SUFFIXES` allowlist, so this suffix is enrolled
# there too. Renaming this key without touching that allowlist would leave the PRODUCTION tenant
# holding a SANDBOX folder id — a repoint that reports success and then archives into the mirror.
CFG_BOX_ARCHIVE_ROOT = "field_ops.box.archive_root_folder_id"
WORKSTREAM_FIELD_OPS = "field_ops"

# The progress Box root's ITS_Config key. Deliberately a LITERAL rather than
# `progress_weekly_generate.CFG_BOX_PORTAL_ROOT`: that module drags `form_pdf`, `generate_core` and
# the network-capable `portal_client` into the import graph of what is otherwise a leaf relocation
# module, and a folder-mover has no business importing a report generator to read one string. The
# literal is kept honest by `test_job_archive.py::test_box_root_keys_match_their_owning_modules`,
# which does the heavy import in TEST scope and RED-lights if an owner renames its key.
CFG_BOX_PROGRESS_ROOT = "progress_reports.box.portal_root_folder_id"
WORKSTREAM_PROGRESS = "progress_reports"

# The SHARED safety root (see the module docstring) is owned by `safety_naming`, which this module
# already imports for the naming rule — so it is referenced, never copied. The PO lane's root is
# likewise referenced from its owner `po_naming` — a LEAF module (stdlib + safety_naming only), so
# unlike the progress root there is no import-weight reason for a literal here.
WORKSTREAM_SAFETY = "safety_reports"

# The #336 observable-config ledger for the five Box roots this module resolves (the archive
# destination + the four workstream SOURCE roots). Declared HERE,
# on the module that actually reads them, following the `weekly_send` / `po_send` / `intake`
# precedent (a REQUIRED_CONFIG declaration is not daemon-only — it marks the resolving module, and
# `scripts/generate_config_dictionary.py` discovers it by scanning for this declaration).
#
# Note what is NOT here: a `resolve_and_log` call. That belongs to a daemon STARTUP, and this
# module has no entry point — it is a pass invoked per job. The startup log lands when
# `fieldops_sync` gains the archive pass and adds these keys to its own resolve_and_log sweep;
# until then the declaration's job is to keep the key documented and dashboard-editable rather
# than invisible.
REQUIRED_CONFIG: list[ConfigKey] = [
    ConfigKey(
        CFG_BOX_ARCHIVE_ROOT, WORKSTREAM_FIELD_OPS, "", "str",
        description=(
            "Box root the Track 6 job archive relocates a closed job's Safety, Progress, "
            "Purchase Orders and Subcontracts containers beneath "
            "(ITS Archive/<Job>/<Workstream>). Built by build_box_roots.py."
        ),
    ),
    # The SOURCE roots carry NO description on purpose. The config dictionary is a single
    # global table keyed by Setting name, and a `description` here OVERWRITES the owning
    # workstream's prose for every reader — a first regen with one attached rewrote the shared
    # safety root's entry (read by several daemons) as "read here as an archive SOURCE", which is
    # true of this module and useless to everyone else. Declaring the key without a description
    # is what adds `field_ops.job_archive` to the "Read by" column while leaving the owner's
    # wording intact.
    ConfigKey(safety_naming.CFG_BOX_PORTAL_ROOT, WORKSTREAM_SAFETY, "", "str"),
    ConfigKey(CFG_BOX_PROGRESS_ROOT, WORKSTREAM_PROGRESS, "", "str"),
    ConfigKey(
        po_naming.CFG_BOX_PORTAL_ROOT, po_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM, "", "str",
    ),
    ConfigKey(
        subcontract_naming.CFG_BOX_PORTAL_ROOT,
        subcontract_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM, "", "str",
    ),
]

# The per-workstream labels the containers take inside the archive. These are the folder NAMES an
# operator will see, so they are deliberately plain English rather than internal keys.
LABEL_SAFETY = "Safety"
LABEL_PROGRESS = "Progress"
LABEL_PURCHASE_ORDERS = "Purchase Orders"
LABEL_SUBCONTRACTS = "Subcontracts"

# Stop auto-retrying a job whose archive keeps failing. Without a cap, a PERMANENT condition (the
# token lacking ADMIN on the Archive workspace, an operator-deleted destination) would re-fire the
# whole eight-container sequence every cycle forever. The operator's "Try again" resets the counter.
MAX_ARCHIVE_ATTEMPTS = 20

# Access levels that satisfy the ADMIN_WORKSPACES requirement for a folder move.
_ADMIN_LEVELS = frozenset({"ADMIN", "OWNER"})


@dataclass(frozen=True)
class ArchiveSlot:
    """One relocatable container.

    A declarative tuple rather than four hand-written blocks: the fan-out across workspaces and
    roots is exactly the multi-surface shape that produced the old four-tracker tuple, and the
    reflex (HOUSE_REFLEXES §1) is to make the enumeration ONE datum you can grep.
    """

    system: str  # "smartsheet" | "box"
    key: str  # stable id in the D1 container report — never rendered to an operator
    label: str  # the folder name inside the archive, and the operator-facing name
    #: For Smartsheet: the WORKSPACE the per-job folder sits directly under, or None when it
    #: instead sits under a parent folder (`parent_folder`). Exactly one of the two is set.
    workspace: int | None = None
    parent_folder: int | None = None
    #: For Box: the ITS_Config Setting naming this workstream's Box ROOT, and the Workstream cell
    #: that row is read under. Box roots are tenant-specific folder ids resolved at RUNTIME from
    #: config — unlike the Smartsheet ids, which are `sheet_ids` constants — so a Box slot carries
    #: the config coordinates rather than an id.
    box_root_key: str | None = None
    box_root_workstream: str | None = None


SLOTS: tuple[ArchiveSlot, ...] = (
    # Safety + Progress per-job folders sit directly under their WORKSPACE root.
    ArchiveSlot("smartsheet", "smartsheet:safety", LABEL_SAFETY,
                workspace=sheet_ids.WORKSPACE_SAFETY_PORTAL),
    ArchiveSlot("smartsheet", "smartsheet:progress", LABEL_PROGRESS,
                workspace=sheet_ids.WORKSPACE_PROGRESS_REPORTING),
    # PO/RFQ and Subcontract per-job folders sit under a "Jobs" PARENT FOLDER.
    ArchiveSlot("smartsheet", "smartsheet:purchase_orders", LABEL_PURCHASE_ORDERS,
                parent_folder=sheet_ids.FOLDER_PO_JOBS),
    ArchiveSlot("smartsheet", "smartsheet:subcontracts", LABEL_SUBCONTRACTS,
                parent_folder=sheet_ids.FOLDER_SC_JOBS),
    # Box: FOUR containers — the safety root still carries the materials manifests inside its
    # per-job folder (see the module docstring); the PO lane's own root (2026-08-11) carries the
    # PO PDFs + RFQs/ + Vendor Quotes/ inside its per-job folder; the subcontract lane's own
    # root (2026-08-12) carries the .docx/.xlsx package + send ZIP.
    ArchiveSlot("box", "box:safety", LABEL_SAFETY,
                box_root_key=safety_naming.CFG_BOX_PORTAL_ROOT,
                box_root_workstream=WORKSTREAM_SAFETY),
    ArchiveSlot("box", "box:progress", LABEL_PROGRESS,
                box_root_key=CFG_BOX_PROGRESS_ROOT,
                box_root_workstream=WORKSTREAM_PROGRESS),
    ArchiveSlot("box", "box:purchase_orders", LABEL_PURCHASE_ORDERS,
                box_root_key=po_naming.CFG_BOX_PORTAL_ROOT,
                box_root_workstream=po_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM),
    ArchiveSlot("box", "box:subcontracts", LABEL_SUBCONTRACTS,
                box_root_key=subcontract_naming.CFG_BOX_PORTAL_ROOT,
                box_root_workstream=subcontract_naming.CFG_BOX_PORTAL_ROOT_WORKSTREAM),
)

#: The four standing tracker sheets the OLD path moved individually. Retained only so the §43
#: runbook and any operator repair can name them; the folder move now carries them implicitly.
TRACKER_SHEET_NAMES = (
    hours_log.hours_log_sheet_name,
    equipment_status.equipment_sheet_name,
    material_list.material_list_sheet_name,
    material_incidents.material_incidents_sheet_name,
)


@dataclass
class ContainerResult:
    """One container's outcome, as reported back to D1 and rendered to the operator."""

    key: str
    label: str
    moved: bool
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "moved": self.moved, "note": self.note}


def verify_archive_capability(correlation_id: str | None = None) -> bool:
    """Pre-flight: does the token identity hold ADMIN on every workspace a move touches?

    Smartsheet's folder-move endpoint requires the `ADMIN_WORKSPACES` scope, and ITS authenticates
    with a PAT — which carries the acting user's own permissions. Without this probe the shortfall
    surfaces as a 403 only AFTER an operator pressed Archive, and only for whichever containers got
    that far: a half-archived job caused purely by a permissions gap.

    Returns False (and WARNs, naming the deficient workspace) rather than raising, so the caller
    skips the pass for the cycle instead of failing it. Never silent — a skipped pass with no log
    line would be indistinguishable from an empty queue.

    SMARTSHEET ONLY, deliberately. Box is absent from this probe for two reasons, and adding it
    would make the archive worse, not safer. (1) There is nothing to probe: Box has no ownership
    discriminator — every user owns their own root, so a read succeeds for ANY valid identity
    (the same finding that makes `build_box_roots._resolve_identity` a WARN rather than a block).
    (2) The failure it could plausibly pre-flight — an unset root config row — already fails
    BEFORE any Box write, in `_read_box_root`, so it cannot leave a half-relocated tree. Skipping
    the whole pass for it would strand the four healthy Smartsheet containers too; letting the
    per-container fence report 4-of-8 `partial` is strictly more honest and more recoverable.
    """
    required = (
        sheet_ids.WORKSPACE_ARCHIVE,
        sheet_ids.WORKSPACE_SAFETY_PORTAL,
        sheet_ids.WORKSPACE_PROGRESS_REPORTING,
        sheet_ids.WORKSPACE_PURCHASE_ORDERS,
        sheet_ids.WORKSPACE_SUBCONTRACTS,
    )
    for workspace_id in required:
        try:
            level = smartsheet_client.get_workspace_access_level(workspace_id)
        except Exception as exc:  # noqa: BLE001 — a probe failure must not fail the cycle
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"archive pre-flight could not read access level on workspace {workspace_id}; "
                f"skipping the archive pass this cycle: {type(exc).__name__}: {exc!r}",
                error_code="archive_preflight_unreadable",
                correlation_id=correlation_id,
            )
            return False
        if level.upper() not in _ADMIN_LEVELS:
            error_log.log(
                Severity.WARN, SCRIPT_NAME,
                f"archive pre-flight FAILED: the ITS Smartsheet identity holds {level!r} on "
                f"workspace {workspace_id}, but a folder move needs ADMIN_WORKSPACES. Every "
                f"archive would 403 partway through, leaving jobs half-relocated. Skipping the "
                f"archive pass until the share is fixed.",
                error_code="archive_preflight_not_admin",
                correlation_id=correlation_id,
            )
            return False
    return True


def ensure_archive_job_folder(folder_key: str) -> int:
    """Find-or-create `ITS — Archive / <folder_key>`, returning its folder id.

    Race-tolerant in the house pattern (`job_sheet.ensure_job_sheet`,
    `week_sheet._ensure_job_folder`): Smartsheet does not enforce folder-name uniqueness, so two
    creators can both pass the find step. We re-find after create, adopt the FIRST match, and WARN
    the duplicate for operator cleanup. Worst case is one empty orphan folder.

    Folder titles are NOT length-capped by Smartsheet (only sheet names are, at 50 — errorCode
    1041), so `folder_key` needs no truncation here.
    """
    existing = smartsheet_client.find_folder_by_name_in_workspace(
        sheet_ids.WORKSPACE_ARCHIVE, folder_key
    )
    if existing is not None:
        return existing

    created = smartsheet_client.create_folder_in_workspace(sheet_ids.WORKSPACE_ARCHIVE, folder_key)
    post_find = smartsheet_client.find_folder_by_name_in_workspace(
        sheet_ids.WORKSPACE_ARCHIVE, folder_key
    )
    if post_find is not None and post_find != created:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"duplicate archive job folder {folder_key!r} under WORKSPACE_ARCHIVE "
            f"(using first match {post_find}; manual cleanup needed for {created})",
            error_code="archive_job_folder_duplicate",
        )
        return post_find
    return created


def resolve_source_container(slot: ArchiveSlot, folder_key: str) -> int | None:
    """Find the per-job folder in its LIVE location. Find-only — never creates.

    None means "nothing to move": either the job never produced anything in that workstream, or a
    previous cycle already relocated it. The caller distinguishes those two via the durable record,
    not by guessing here.
    """
    if slot.workspace is not None:
        return smartsheet_client.find_folder_by_name_in_workspace(slot.workspace, folder_key)
    assert slot.parent_folder is not None, "a smartsheet slot needs a workspace or a parent folder"
    return smartsheet_client.find_folder_by_name_in_folder(slot.parent_folder, folder_key)


def archive_smartsheet_container(
    slot: ArchiveSlot, folder_key: str, archive_job_folder: int
) -> ContainerResult:
    """Move one Smartsheet per-job folder into the archive and label it, or explain why not.

    MOVE-THEN-RENAME, and the order is load-bearing. Renaming first would make the folder invisible
    to the live find-or-create paths, which would then create a fresh empty folder under the job's
    name beside it — and the archive would go on to move the wrong tree. Moving first removes it
    from the source parent in a single call, so the source-side find immediately returns None and
    no live creator can be pointed at a half-archived container.

    The residual crash window is "moved but not renamed": a folder still called <Job> sitting
    inside Archive/<Job>/. Benign, detectable, and repaired by re-issuing the rename — which is
    idempotent, so the resume path just does it.
    """
    source = resolve_source_container(slot, folder_key)
    if source is None:
        return ContainerResult(slot.key, slot.label, moved=True, note="nothing to move")

    smartsheet_client.move_folder_to_folder(source, archive_job_folder)
    # Separate call — /move cannot rename. Idempotent, so a retry after a crash here is safe.
    if smartsheet_client.get_folder_name(source) != slot.label:
        smartsheet_client.rename_folder(source, slot.label)
    return ContainerResult(slot.key, slot.label, moved=True)


def find_archive_job_folder(folder_key: str) -> int | None:
    """Find `ITS — Archive / <folder_key>` WITHOUT creating it. The un-archive counterpart.

    `ensure_archive_job_folder` would create the folder, and on the restore path an absent archive
    folder is the answer — the job was never archived, so there is nothing to bring back. Creating
    one would leave an empty folder in the archive for every restore of a never-archived job.
    """
    return smartsheet_client.find_folder_by_name_in_workspace(
        sheet_ids.WORKSPACE_ARCHIVE, folder_key
    )


def resolve_archived_container(
    slot: ArchiveSlot, folder_key: str, archive_job_folder: int
) -> int | None:
    """Find one container inside the archive, tolerating a crash mid-restore.

    Two names are searched, and the second is not redundant. A container normally sits in the
    archive under its workstream LABEL (`Safety`), but the restore renames BEFORE it moves, so a
    crash in between leaves it named `<folder_key>` still inside the archive folder. Searching only
    the label would read that as "nothing to move" and report the restore complete with the
    container still archived — the exact silent-success class this module exists to avoid.
    """
    found = smartsheet_client.find_folder_by_name_in_folder(archive_job_folder, slot.label)
    if found is not None:
        return found
    return smartsheet_client.find_folder_by_name_in_folder(archive_job_folder, folder_key)


def _move_to_live_location(slot: ArchiveSlot, folder_id: int) -> None:
    """Move a folder back to whichever live parent this slot lives under."""
    if slot.workspace is not None:
        smartsheet_client.move_folder_to_workspace(folder_id, slot.workspace)
        return
    assert slot.parent_folder is not None, "a smartsheet slot needs a workspace or a parent folder"
    smartsheet_client.move_folder_to_folder(folder_id, slot.parent_folder)


def unarchive_smartsheet_container(
    slot: ArchiveSlot, folder_key: str, archive_job_folder: int
) -> ContainerResult:
    """Restore one Smartsheet container from the archive, or explain why not.

    RENAME-THEN-MOVE — the mirror of `archive_smartsheet_container`, and inverted for the mirror
    reason. Moving first would drop a folder still named `Safety` into the live workspace, where it
    is invisible to every find-or-create path (they all look for `<Job>`): the next filing would
    create a fresh `<Job>` beside it, and the rename would then produce TWO folders with that name
    and filing split across them. Renaming first means the folder only ever enters the live tree
    already carrying the name those paths look for.

    The residual crash window is "renamed but not moved" — a folder named `<Job>` still sitting in
    `Archive/<Job>/`. That is strictly safer than the alternative window: it is confined to the
    archive, invisible to the live tree, and `resolve_archived_container` finds it by that very
    name so the retry completes the move.

    A live folder ALREADY holding `<folder_key>` refuses loudly rather than merging. Neither
    Smartsheet nor Box has a merge primitive (the same reasoning as `box_client.move_folder`'s
    409 re-raise), so fusing an archived tree into a live one is unrecoverable. This happens for
    real: a job archived and then written to re-grows its live folder through the ordinary
    find-or-create paths.
    """
    source = resolve_archived_container(slot, folder_key, archive_job_folder)
    if source is None:
        return ContainerResult(slot.key, slot.label, moved=True, note="nothing to move")

    # `source` was found INSIDE the archive folder, so any live hit is necessarily a different
    # folder — there is no "it is already home" case to special-case here (a fully restored
    # container leaves nothing in the archive, so `source` would have been None above).
    collision = resolve_source_container(slot, folder_key)
    if collision is not None:
        raise RuntimeError(
            f"cannot restore {slot.label!r} for {folder_key!r}: folder {collision} already holds "
            f"that name in the live tree, and neither system can merge two folders. The job was "
            f"almost certainly written to while archived, which re-grows the live folder through "
            f"the ordinary find-or-create paths. Reconcile the two by hand — see "
            f"docs/runbooks/job_archive.md."
        )

    # Rename BEFORE the move (see the docstring). Idempotent, so a retry after a crash is safe.
    if smartsheet_client.get_folder_name(source) != folder_key:
        smartsheet_client.rename_folder(source, folder_key)
    _move_to_live_location(slot, source)
    return ContainerResult(slot.key, slot.label, moved=True)


def _read_box_root(key: str, workstream: str) -> str:
    """Resolve one Box ROOT folder id from ITS_Config, or RAISE.

    Raising — rather than returning None and letting the caller treat it as an empty tree — is the
    whole point. An unset root makes every find-by-name match nothing, which is byte-identical to a
    clean "nothing to move"; the archive would report the container relocated and the operator's
    documents would still be sitting in the live tree. The raise routes into `archive_job`'s
    per-container fence, so the container reports NOT moved, the job stays on the queue, and the
    WARN names the missing key.
    """
    value = (smartsheet_client.get_setting(key, workstream=workstream) or "").strip()
    if not value:
        raise RuntimeError(
            f"Box root unset (ITS_Config {key!r}, Workstream {workstream!r}) — refusing to treat "
            f"an unreadable tree as an empty one. Seed the row (build_box_roots.py prints the id) "
            f"and the archive retries next cycle."
        )
    return value


def ensure_archive_box_job_folder(folder_key: str) -> str:
    """Find-or-create `ITS Archive / <folder_key>` in Box, returning its folder id.

    Thinner than its Smartsheet twin (`ensure_archive_job_folder`) on purpose, and the asymmetry is
    real rather than an oversight: Box answers a duplicate create with a 409, so
    `box_client.get_or_create_folder` already does the race-tolerant find → create → adopt-on-409 →
    re-find → re-raise-if-still-missing dance internally. Smartsheet returns 200 for a duplicate
    create, so its side has to hand-roll a find-AFTER-create and WARN the loser. Copying that
    ceremony here would add a second, weaker race guard on top of a working one.
    """
    return box_client.get_or_create_folder(
        _read_box_root(CFG_BOX_ARCHIVE_ROOT, WORKSTREAM_FIELD_OPS), folder_key
    )


def resolve_source_box_container(slot: ArchiveSlot, folder_key: str) -> str | None:
    """Find the per-job Box folder under its workstream ROOT. Find-only — never creates.

    `box_client.find_child_folder`, NOT `get_or_create_folder`: creating here would manufacture the
    very folder whose absence means "nothing to move", and the archive would then relocate a brand
    new empty container while the real documents stayed put.
    """
    assert slot.box_root_key is not None, "a box slot needs a root config key"
    assert slot.box_root_workstream is not None, "a box slot needs a root workstream"
    root = _read_box_root(slot.box_root_key, slot.box_root_workstream)
    return box_client.find_child_folder(root, folder_key)


def archive_box_container(
    slot: ArchiveSlot, folder_key: str, archive_box_job_folder: str
) -> ContainerResult:
    """Move one Box per-job folder into the archive and label it, or explain why not.

    ONE call, not two. Box's `PUT /folders/{id}` carries both the new parent and the new name, so
    unlike the Smartsheet path there is no moved-but-unrenamed crash window and therefore nothing
    to resume from — do not port the Smartsheet resume probe here, it would only add a round trip
    to answer a question this side cannot ask.

    A prior successful run leaves the source root with no `<Job>` child, so the re-run reads
    "nothing to move" and is idempotent. The one case that deliberately fails LOUD instead: a job
    archived and then written to again, which makes the live find-or-create paths re-grow
    `<root>/<Job>` beside the archived copy. Moving that second folder onto the first is a genuine
    collision — `box_client.move_folder` re-raises the 409 rather than adopting, because neither
    Box nor Smartsheet has a merge primitive and silently fusing two job trees is unrecoverable.
    """
    source = resolve_source_box_container(slot, folder_key)
    if source is None:
        return ContainerResult(slot.key, slot.label, moved=True, note="nothing to move")

    box_client.move_folder(source, archive_box_job_folder, new_name=slot.label)
    return ContainerResult(slot.key, slot.label, moved=True)


def find_archive_box_job_folder(folder_key: str) -> str | None:
    """Find `ITS Archive / <folder_key>` in Box WITHOUT creating it — the restore counterpart.

    Same reasoning as `find_archive_job_folder`: on the restore path an absent archive folder means
    the job was never archived, and creating one would litter the archive with empty folders.
    """
    return box_client.find_child_folder(
        _read_box_root(CFG_BOX_ARCHIVE_ROOT, WORKSTREAM_FIELD_OPS), folder_key
    )


def unarchive_box_container(
    slot: ArchiveSlot, folder_key: str, archive_box_job_folder: str
) -> ContainerResult:
    """Restore one Box container from the archive, or explain why not.

    One call again, so the rename-then-move ordering that the Smartsheet side needs has no
    analogue here: Box's PUT carries the new parent and the new name together, and the folder never
    exists in the live tree under the wrong name for even an instant.

    The live-collision case is handled by `box_client.move_folder` itself, which re-raises a 409
    when a DIFFERENT folder already holds the target name rather than adopting it — the same
    refusal `unarchive_smartsheet_container` has to implement by hand.
    """
    root = _read_box_root(slot.box_root_key or "", slot.box_root_workstream or "")
    source = box_client.find_child_folder(archive_box_job_folder, slot.label)
    if source is None:
        # The renamed-but-not-moved crash window does not exist on this side (one atomic call), so
        # unlike the Smartsheet path there is no second name to search for.
        return ContainerResult(slot.key, slot.label, moved=True, note="nothing to move")

    box_client.move_folder(source, root, new_name=folder_key)
    return ContainerResult(slot.key, slot.label, moved=True)


def _log_container_failure(
    job_id: str, slot: ArchiveSlot, exc: BaseException, correlation_id: str
) -> None:
    """WARN naming the JOB, the SYSTEM and the CONTAINER.

    The old `_warn_archive_move_failed` named only a sheet. With eight containers across two systems
    that is not actionable — an operator reading `ITS_Errors` has to be able to tell which folder
    in which system is stuck without opening a runbook first.
    """
    error_log.log(
        Severity.WARN, SCRIPT_NAME,
        f"archive container FAILED for job_id={job_id!r}: {slot.system}/{slot.label} "
        f"({slot.key}): {type(exc).__name__}: {exc!r}. The other containers are attempted "
        f"independently; a partial or failed result is TERMINAL for the daemon and resumes only "
        f"when the operator presses Try again — see docs/runbooks/job_archive.md.",
        error_code="archive_container_failed",
        correlation_id=correlation_id,
    )


#: Sentinel for "this system's archive folder was looked up and is genuinely absent", as distinct
#: from "not looked up yet" (None). Without it the restore re-probes the archive once per slot.
_ABSENT = "__absent__"


def _never_archived(slot: ArchiveSlot) -> ContainerResult:
    """A restore of a container that was never archived is success, not failure.

    Reported as moved with a note rather than as a failure, for the same reason
    `archive_smartsheet_container` treats an absent source that way: a job that never produced
    anything in a workstream must not hold its restore at `partial` forever.
    """
    return ContainerResult(slot.key, slot.label, moved=True, note="nothing to move")


def _resolve_folder_key(
    job: dict[str, Any], job_id: str, correlation_id: str, *, direction: str
) -> str | None:
    """The SNAPSHOTTED per-job folder key, or None after WARNing.

    Shared by both directions because the trap is identical and subtle in both: an empty key makes
    every find-by-name match nothing, so a naive implementation reports eight clean "nothing to move"
    successes and marks the pass COMPLETE without touching a thing.
    """
    folder_key = str(job.get("archive_folder_key") or "").strip()
    if folder_key:
        return folder_key
    # Defensive: the browser route always stamps this.
    error_log.log(
        Severity.WARN, SCRIPT_NAME,
        f"{direction} skipped for job_id={job_id!r}: archive_folder_key is empty, so no container "
        f"could be resolved. This should be impossible — the request route stamps it.",
        error_code="archive_folder_key_missing",
        correlation_id=correlation_id,
    )
    return None


def archive_job(job: dict[str, Any], correlation_id: str | None = None) -> list[ContainerResult]:
    """Relocate every container for one job INTO the archive. Per-container fenced; never raises.

    Each container is attempted independently, so one failure never blocks the other seven — a
    partial archive is a normal, resumable outcome rather than an all-or-nothing collapse. The
    caller turns these results into the D1 state the operator sees.

    Callers should prefer `run_archive_pass`, which dispatches on the queue row's direction;
    calling this directly on an un-archive row reports success while nothing moves.
    """
    correlation_id = correlation_id or uuid.uuid4().hex[:12]
    job_id = str(job.get("job_id") or "")
    folder_key = _resolve_folder_key(job, job_id, correlation_id, direction="archive")
    if folder_key is None:
        return [ContainerResult(s.key, s.label, moved=False, note="no folder key") for s in SLOTS]

    results: list[ContainerResult] = []
    # One destination per SYSTEM, resolved lazily and at most once per job. Lazy because a
    # destination is a WRITE (find-or-create): a job whose every container is already relocated
    # should not leave a fresh empty `<Job>` folder behind in either archive as a side effect of
    # asking. The two are tracked separately because a Smartsheet outage must not stop the Box
    # containers from moving, and vice versa — the systems fail independently, so they resolve
    # independently.
    smartsheet_destination: int | None = None
    box_destination: str | None = None
    for slot in SLOTS:
        try:
            if slot.system == "box":
                if box_destination is None:
                    box_destination = ensure_archive_box_job_folder(folder_key)
                results.append(archive_box_container(slot, folder_key, box_destination))
            else:
                if smartsheet_destination is None:
                    smartsheet_destination = ensure_archive_job_folder(folder_key)
                results.append(
                    archive_smartsheet_container(slot, folder_key, smartsheet_destination)
                )
        except Exception as exc:  # noqa: BLE001 — per-container fence; one failure never blocks the rest
            _log_container_failure(job_id, slot, exc, correlation_id)
            results.append(
                ContainerResult(slot.key, slot.label, moved=False, note=f"{type(exc).__name__}")
            )
    return results


def unarchive_job(job: dict[str, Any], correlation_id: str | None = None) -> list[ContainerResult]:
    """Restore every container for one job out of the archive. Per-container fenced; never raises.

    The exact structural mirror of `archive_job` — same fence, same per-system lazy destination,
    same result shape — with two differences that are not stylistic:

    1. The archive folders are FOUND, never created. On this path an absent archive folder is a
       real answer ("this job was never archived"), so creating one would litter the archive with
       an empty folder for every restore of a never-archived job.
    2. A missing archive folder short-circuits the whole system's slots to "nothing to move"
       rather than failing them, because there is genuinely nothing there to bring back.
    """
    correlation_id = correlation_id or uuid.uuid4().hex[:12]
    job_id = str(job.get("job_id") or "")
    folder_key = _resolve_folder_key(job, job_id, correlation_id, direction="unarchive")
    if folder_key is None:
        return [ContainerResult(s.key, s.label, moved=False, note="no folder key") for s in SLOTS]

    results: list[ContainerResult] = []
    # Sentinels distinguish "not looked up yet" (None) from "looked up and genuinely absent"
    # (_ABSENT). Without that distinction a job that was never archived would re-probe the archive
    # once per slot instead of once per system.
    smartsheet_source: int | str | None = None
    box_source: str | None = None
    for slot in SLOTS:
        try:
            if slot.system == "box":
                if box_source is None:
                    box_source = find_archive_box_job_folder(folder_key) or _ABSENT
                if box_source is _ABSENT:
                    results.append(_never_archived(slot))
                    continue
                results.append(unarchive_box_container(slot, folder_key, str(box_source)))
            else:
                if smartsheet_source is None:
                    smartsheet_source = find_archive_job_folder(folder_key) or _ABSENT
                if smartsheet_source is _ABSENT:
                    results.append(_never_archived(slot))
                    continue
                results.append(
                    unarchive_smartsheet_container(slot, folder_key, int(smartsheet_source))
                )
        except Exception as exc:  # noqa: BLE001 — per-container fence; one failure never blocks the rest
            _log_container_failure(job_id, slot, exc, correlation_id)
            results.append(
                ContainerResult(slot.key, slot.label, moved=False, note=f"{type(exc).__name__}")
            )
    return results


def run_archive_pass(
    job: dict[str, Any], correlation_id: str | None = None
) -> list[ContainerResult]:
    """THE entry point for one queued job. Dispatches on the row's `archive_direction`.

    The dispatch lives here rather than in the calling daemon because getting it wrong is silent.
    `/archive-pending` serves BOTH directions from one queue, and running `archive_job` against an
    un-archive row would find nothing in the live tree (the containers are in the archive), report
    eight clean "nothing to move" successes, and post `complete` — telling the operator their job was
    restored while every folder stayed archived.

    An unrecognised direction REFUSES rather than defaulting. Defaulting to `archive` is the same
    silent-wrong-result class in the other direction, and the Worker's commit point already
    rejects any direction outside the pair, so a refusal here can never wedge a row it could
    otherwise have advanced.
    """
    direction = str(job.get("archive_direction") or "").strip()
    if direction == "archive":
        return archive_job(job, correlation_id)
    if direction == "unarchive":
        return unarchive_job(job, correlation_id)

    error_log.log(
        Severity.WARN, SCRIPT_NAME,
        f"archive pass skipped for job_id={str(job.get('job_id') or '')!r}: archive_direction is "
        f"{direction!r}, which is neither 'archive' nor 'unarchive'. Refusing to guess — running "
        f"the wrong direction reports success while nothing moves. The row stays on the queue; see "
        f"docs/runbooks/job_archive.md.",
        error_code="archive_direction_unknown",
        correlation_id=correlation_id or uuid.uuid4().hex[:12],
    )
    return [ContainerResult(s.key, s.label, moved=False, note="unknown direction") for s in SLOTS]


def state_from_results(results: list[ContainerResult]) -> str:
    """Collapse per-container outcomes into the D1 archive_state the operator reads.

    `partial` is deliberately distinct from `failed`: an operator seeing "4 of 8 moved" needs to
    know something DID move, because the repair differs from "nothing happened".
    """
    moved = sum(1 for r in results if r.moved)
    if moved == len(results):
        return "complete"
    return "partial" if moved else "failed"


def folder_key_for(project_name: str) -> str:
    """The per-job folder key for a project name — the one naming rule, shared with Box.

    Thin delegate to `safety_naming.job_folder_name` so this module never grows a second copy;
    the Worker mirrors the SAME rule in TypeScript, with cross-language parity asserted in
    `tests/test_job_archive_guard.py`.
    """
    return safety_naming.job_folder_name(project_name)
