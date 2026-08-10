"""Per-job Material List tracker — the P7 standing tracker (Track 2, M2).

One-way-up mirror of the D1 expected-materials SoR (`job_expected_materials`, migration 0031) into
a per-job **standing** "Material List" Smartsheet in the `ITS — Progress Reporting` workspace, in
the SAME per-job folder as the Hours Log + Equipment + week sheets (so they sit side by side).
SEND-FREE + AI-FREE (Op Stds v19 §51 — ITS-owned structured-SoR write-back); the daemon that drives
it (`field_ops.fieldops_sync` material pass) is the capability-gated actuator.

Design (operator-ratified — PORTAL-AUTHORED, ONE-WAY-UP; structurally identical to the Slice 2
Equipment tracker):
- **Single standing sheet per job** (`<Job> — Material List`) — ONE row per operator-authored
  expected-materials LINE, showing its expected content + delivery state (`Status`) + an off-manifest
  `Unplanned` flag. This is a SNAPSHOT re-projected every cycle: a row is UPDATED IN PLACE as the
  line's content / receipt state changes, and a line REMOVED from the list (deactivated,
  `active=0`) is marked `On List → Removed` — **NEVER deleted** (§51 SoR rule).
- Because it re-projects the live snapshot each cycle, `upsert_line_row` is **CHANGE-ONLY**: on a
  find-hit it compares the incoming values against the row's current cells and issues an
  `update_rows` ONLY when something actually changed — avoiding a needless write every cycle. There
  is NO watermark / mark-mirrored (the Worker route is a re-projected snapshot, not an event drain).
- **ONE-WAY-UP ONLY.** The operator authors + edits the list IN THE PORTAL (the #426
  `cap.materials.manage` CRUD); this mirror is read-only reflection UP. There is NO down-sync / no
  bidirectional write-back (a deferred future model) — and so NO `smartsheet_row_id` reverse link.
- **Progress workspace only**, single-destination. The per-job FOLDER + capacity tripwire are reused
  from `hours_log` so all the per-job trackers share one folder-resolution authority.
- `check_row_cap` is kept for parity/safety, though a per-job material list is bounded by line count
  so it ~never fires.

The module is a thin Smartsheet write helper (like `hours_log` / `equipment_status`) — NOT a daemon
entry point, so it is not itself in `GATED_SCRIPTS`; its sole caller `fieldops_sync` is.
"""
from __future__ import annotations

from typing import Any

from progress_reports import hours_log
from shared import error_log, review_queue, sheet_capacity, sheet_ids, smartsheet_client
from shared.error_log import Severity

SCRIPT_NAME = "progress_reports.material_list"

WORKSPACE_ID = sheet_ids.WORKSPACE_PROGRESS_REPORTING

# Smartsheet sheet-name cap (HTTP 400 errorCode 1041) — same constant as hours_log.SHEET_NAME_MAX.
SHEET_NAME_MAX = 50
SHEET_SUFFIX = " — Material List"

# §51 A5 row-cap watchdog — a per-job list is bounded by line count so this ~never fires, but it is
# kept for parity with the other standing trackers (defence in depth). WARN + Review-Queue an
# operator period-split as the sheet nears the Smartsheet ~20k/sheet row cap; NEVER delete (SoR).
CFG_ROW_CAP_WARN = "progress_reports.material_list.row_cap_warn_threshold"
DEFAULT_ROW_CAP_WARN = 15000  # conservative WARN mark well under the ~20k Smartsheet per-sheet cap

# ---- Column titles (single source of truth for reads + writes) ----
COL_LINE = "Line"                    # primary (TEXT_NUMBER): catalog name or description
COL_LINE_UUID = "Line UUID"          # upsert key (== job_expected_materials.line_uuid)
COL_MATERIAL = "Material"            # catalog model_id, or '—' for a free-text line
COL_DESCRIPTION = "Description"
COL_QTY = "Qty"                      # expected quantity (pre-formatted string)
COL_UNIT = "Unit"
COL_EXPECTED_DATE = "Expected Date"  # DATE — the expected DELIVERY date (0031 meaning, unchanged)
COL_STATUS = "Status"                # expected | received | incident — TEXT controlled-vocab
COL_DELIVERED_QTY = "Delivered Qty"  # actual quantity recorded at receipt (pre-formatted string)
COL_RECEIVED_AT = "Received At"      # DATE (receipt stamped)
COL_RECEIVED_BY = "Received By"      # DISPLAY NAME (personnel.name) — NEVER a username (Reflex §5)
COL_NOTE = "Note"
COL_UNPLANNED = "Unplanned"          # Yes | blank (off-manifest field-added line)
COL_ON_LIST = "On List"              # Active | Removed — TEXT controlled-vocab, NOT a PICKLIST
# PR2 (migration 0059) line additions, mirrored from PR4 on. APPENDED at the end of the schema
# rather than slotted beside their logical neighbours: `ensure_columns` back-fills pre-existing
# sheets by APPENDING, so keeping the creation order identical is what stops the two sheet
# populations diverging visually forever. Column order is a display property — an operator can
# reorder in the Smartsheet UI without touching data.
COL_PART_NUMBER = "Part Number"      # BOM part no.; the key PR3's manifest importer matches loads on
COL_CATEGORY = "Category"            # BOM grouping (e.g. 'HARDWARE') — display-only
COL_EXPECTED_SHIP_DATE = "Expected Ship Date"  # DATE — the SHIP date; Expected Date is DELIVERY

# Controlled vocabulary (TEXT cells, NOT picklist — avoids the REGISTRY-parity footgun; mirrors
# equipment_status's On Job choice).
ON_LIST_ACTIVE = "Active"
ON_LIST_REMOVED = "Removed"

# Delivery status values (D1 `job_expected_materials.status` domain). TEXT cells (not picklist).
STATUS_EXPECTED = "expected"
STATUS_RECEIVED = "received"
STATUS_INCIDENT = "incident"

# The 'Material' cell placeholder for a free-text (no-catalog) line.
MATERIAL_NONE = "—"

# Unplanned cell value (an off-manifest line); blank ('') otherwise.
UNPLANNED_YES = "Yes"

# Data columns compared for CHANGE-ONLY upsert (Line UUID is the key; it does not participate in
# change detection). `On List` IS compared so a re-added line flips back to Active. There is no
# 'Updated At' metadata column on this tracker, so change-only just avoids needless writes.
_TRACKED_COLS: tuple[str, ...] = (
    COL_LINE, COL_MATERIAL, COL_DESCRIPTION, COL_QTY, COL_UNIT, COL_EXPECTED_DATE, COL_STATUS,
    COL_DELIVERED_QTY, COL_RECEIVED_AT, COL_RECEIVED_BY, COL_NOTE, COL_UNPLANNED, COL_ON_LIST,
    COL_PART_NUMBER, COL_CATEGORY, COL_EXPECTED_SHIP_DATE,
)

# Schema passed to create_sheet_in_folder. Order = left-to-right UI order. Exactly one primary
# (TEXT_NUMBER, Smartsheet requirement).
MATERIAL_LIST_COLUMNS: list[dict[str, Any]] = [
    {"title": COL_LINE, "type": "TEXT_NUMBER", "primary": True},
    {"title": COL_LINE_UUID, "type": "TEXT_NUMBER"},
    {"title": COL_MATERIAL, "type": "TEXT_NUMBER"},
    {"title": COL_DESCRIPTION, "type": "TEXT_NUMBER"},
    {"title": COL_QTY, "type": "TEXT_NUMBER"},
    {"title": COL_UNIT, "type": "TEXT_NUMBER"},
    {"title": COL_EXPECTED_DATE, "type": "DATE"},
    {"title": COL_STATUS, "type": "TEXT_NUMBER"},
    {"title": COL_DELIVERED_QTY, "type": "TEXT_NUMBER"},
    {"title": COL_RECEIVED_AT, "type": "DATE"},
    {"title": COL_RECEIVED_BY, "type": "TEXT_NUMBER"},
    {"title": COL_NOTE, "type": "TEXT_NUMBER"},
    {"title": COL_UNPLANNED, "type": "TEXT_NUMBER"},
    {"title": COL_ON_LIST, "type": "TEXT_NUMBER"},
    # 0059 additions — APPENDED, which is what lets a back-filled sheet end up with the identical
    # layout (`ensure_columns` appends too). See BACKFILL_COLUMNS below.
    {"title": COL_PART_NUMBER, "type": "TEXT_NUMBER"},
    {"title": COL_CATEGORY, "type": "TEXT_NUMBER"},
    {"title": COL_EXPECTED_SHIP_DATE, "type": "DATE"},
]

# What `ensure_material_list_sheet` back-fills onto an ALREADY-CREATED sheet: EVERY non-primary
# column, not just the newest three. Derived from the creation schema so the two cannot drift.
#
# Deliberately the whole set rather than a tail slice. A slice would have to be re-cut by hand on
# every future addition, and getting it wrong is silent — appending a fourth column while the slice
# still says "last three" would quietly stop back-filling `Part Number` onto sheets that never got
# it. Passing everything costs nothing, because `ensure_columns` resolves all titles against ONE
# cached map and issues no API call when none are missing; it also self-heals a column an operator
# deleted by hand. The primary is excluded because a sheet's primary is fixed at create.
BACKFILL_COLUMNS: list[dict[str, Any]] = [
    c for c in MATERIAL_LIST_COLUMNS if not c.get("primary")
]

# Cosmetic styling — Smartsheet format-descriptor strings (mirror equipment_status's palette).
# Applied AFTER create, best-effort (the API ignores width/format at POST).
FMT_PRIMARY = ",,1,,,,,,38,7,,,,,,,"       # bold + dark-green text + light-green bg
FMT_DATE = ",,,,,,,,,,,,,,,,2"             # MMM_D_YYYY
ON_LIST_ACTIVE_FMT = ",,,,,,,,,7,,,,,,,"   # light-green bg
ON_LIST_REMOVED_FMT = ",,,,,,,,,18,,,,,,,"  # light-gray bg

MATERIAL_LIST_STYLES: list[dict[str, Any]] = [
    {"title": COL_LINE, "width": 220, "format": FMT_PRIMARY},
    {"title": COL_LINE_UUID, "width": 90},
    {"title": COL_MATERIAL, "width": 180},
    {"title": COL_DESCRIPTION, "width": 260},
    {"title": COL_QTY, "width": 80},
    {"title": COL_UNIT, "width": 90},
    {"title": COL_EXPECTED_DATE, "width": 120, "format": FMT_DATE},
    {"title": COL_STATUS, "width": 100},
    {"title": COL_DELIVERED_QTY, "width": 110},
    {"title": COL_RECEIVED_AT, "width": 120, "format": FMT_DATE},
    {"title": COL_RECEIVED_BY, "width": 170},
    {"title": COL_NOTE, "width": 260},
    {"title": COL_UNPLANNED, "width": 100},
    {"title": COL_ON_LIST, "width": 100},
    {"title": COL_PART_NUMBER, "width": 130},
    {"title": COL_CATEGORY, "width": 130},
    {"title": COL_EXPECTED_SHIP_DATE, "width": 130, "format": FMT_DATE},
]


def material_list_sheet_name(project_name: str) -> str:
    """`'<Job> — Material List'`, prefix-truncated to the 50-char cap (errorCode 1041).

    The ` — Material List` suffix is preserved WHOLE (it names the sheet within the per-job folder);
    the project prefix is truncated when needed — no identity is lost because the per-job FOLDER
    already carries the full `project_name` and the sheet is only ever resolved find-or-create WITHIN
    that folder. Names ≤50 chars are byte-identical to the untruncated form.
    """
    prefix = project_name.strip()
    budget = SHEET_NAME_MAX - len(SHEET_SUFFIX)
    if len(prefix) > budget:
        prefix = prefix[:budget].rstrip()
    return f"{prefix}{SHEET_SUFFIX}"


def _apply_styles_best_effort(sheet_id: int) -> None:
    """Apply `MATERIAL_LIST_STYLES` to a freshly-created sheet. Cosmetic only — a failure WARNs
    (never silent) but must NOT fail the already-created sheet (the data path is unaffected)."""
    try:
        smartsheet_client.apply_column_styles(sheet_id, MATERIAL_LIST_STYLES)
    except Exception as exc:  # noqa: BLE001 — cosmetic; never fail sheet creation
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"material-list styling failed (sheet {sheet_id}): {type(exc).__name__}: {exc!r}",
            error_code="material_list_style_failed",
        )


def _folder_name(project_name: str) -> str:
    """Per-job folder title/key — the SAME source of truth the Hours Log / Equipment / week sheets
    use, so the Material List sheet lands in the job's existing folder rather than a parallel one."""
    return hours_log._folder_name(project_name)


def _ensure_job_folder(project_name: str) -> int:
    """Find-or-create the per-job folder in the progress workspace (idempotent, race-tolerant).

    Delegates to `hours_log._ensure_job_folder` so the Material List sheet sits BESIDE the Hours Log
    + Equipment + week sheets — one folder-resolution authority (DRY). The rare duplicate-folder WARN
    is logged under the Hours Log's script name; that is acceptable given the shared resolver.
    """
    return hours_log._ensure_job_folder(project_name)


def _warn_on_thin_headroom(sheet_name: str) -> None:
    """A1 sheet-count tripwire, run before each CREATE. ADVISORY, never blocking (mirrors
    equipment_status._warn_on_thin_headroom): a margin breach WARNs + enqueues the operator signal,
    then the create PROCEEDS. Belt-and-suspenders fail-open — any exception is reduced to a WARN."""
    try:
        headroom = sheet_capacity.check_create_headroom(WORKSPACE_ID)
    except Exception as exc:  # noqa: BLE001 — advisory tripwire; never block the create
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"sheet-capacity headroom check raised (create proceeds unguarded): {exc!r}",
            error_code="sheet_capacity_check_failed",
        )
        return
    if headroom.note:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"sheet-capacity check fail-open before creating {sheet_name!r}: {headroom.note}",
            error_code="sheet_capacity_check_failed",
        )
        return
    if headroom.ok:
        return
    error_log.log(
        Severity.WARN, SCRIPT_NAME,
        (
            f"sheet-count margin breach in workspace {WORKSPACE_ID}: "
            f"{headroom.current}/{headroom.ceiling} (margin {headroom.margin}) — creating "
            f"{sheet_name!r} anyway (advisory tripwire; see the Review-Queue row)."
        ),
        error_code="sheet_capacity_margin_breach",
    )
    try:
        sheet_capacity.route_breach_to_review_queue(
            WORKSPACE_ID, headroom, workstream="progress_reports"
        )
    except Exception as exc:  # noqa: BLE001 — the enqueue failing must not block the create
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"could not enqueue the sheet-capacity breach to ITS_Review_Queue: {exc!r}",
            error_code="sheet_capacity_rq_failed",
        )


def _backfill_columns_best_effort(sheet_id: int, sheet_name: str) -> None:
    """Add any `BACKFILL_COLUMNS` title missing from an existing sheet. Never raises.

    Best-effort on purpose. A back-fill failure (breaker OPEN, a 403, an API blip) must NOT stop the
    mirror: a sheet whose schema is one column behind still mirrors every column it does have, and
    the next cycle retries. The cells for a genuinely-absent column still surface loudly through the
    per-line fence rather than being silently dropped — this closes the common cause, it does not
    hide the symptom.
    """
    try:
        added = smartsheet_client.ensure_columns(sheet_id, BACKFILL_COLUMNS)
    except Exception as exc:  # noqa: BLE001 — schema top-up; never block the mirror
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"could not back-fill columns on Material List {sheet_name!r} (sheet {sheet_id}); "
            f"cells for any missing column will fail per-line until this succeeds: "
            f"{type(exc).__name__}: {exc!r}",
            error_code="material_list_column_backfill_failed",
        )
        return
    if added:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"added {len(added)} missing column(s) to Material List {sheet_name!r} "
            f"(sheet {sheet_id}): {', '.join(added)} — a pre-existing sheet was brought up to the "
            f"current tracker schema.",
            error_code="material_list_columns_backfilled",
        )


def ensure_material_list_sheet(project_name: str) -> int:
    """Find-or-create the job's single standing Material List sheet; return its sheet ID.

    Idempotent: a second call returns the same sheet with no write. Race-tolerant at both the folder
    and sheet levels (re-find after create, adopt first, WARN the duplicate). The A1 capacity
    tripwire runs ONLY on the create branch.

    On the FIND branch it also back-fills any missing column (`BACKFILL_COLUMNS`). A sheet's columns
    are fixed at CREATE, so every tracker addition splits the estate in two — sheets new enough to
    have the column, and sheets that predate it, where writing the new cell raises `KeyError` out of
    `_resolve_cells`. That error is what deferred the 0059 Part Number / Category / Expected Ship
    Date columns; closing it here means the mirror never has to choose between erroring on old
    sheets and silently under-writing them. Costs nothing when nothing is missing (see
    `smartsheet_client.ensure_columns`), and is best-effort: a back-fill failure WARNs and returns
    the sheet, because a stale-schema sheet still mirrors every column it DOES have.
    """
    folder_id = _ensure_job_folder(project_name)
    name = material_list_sheet_name(project_name)

    existing = smartsheet_client.find_sheet_by_name_in_folder(folder_id, name)
    if existing is not None:
        _backfill_columns_best_effort(existing, name)
        return existing

    _warn_on_thin_headroom(name)
    sheet_id = smartsheet_client.create_sheet_in_folder(folder_id, name, MATERIAL_LIST_COLUMNS)
    post_find = smartsheet_client.find_sheet_by_name_in_folder(folder_id, name)
    if post_find is not None and post_find != sheet_id:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"duplicate Material List sheet {name!r} under folder {folder_id} "
            f"(project={project_name!r}); using first match {post_find}, manual cleanup "
            f"needed for {sheet_id}.",
            error_code="material_list_sheet_race_duplicate",
        )
        return post_find
    _apply_styles_best_effort(sheet_id)  # cosmetic; create path only
    return sheet_id


def find_material_list_sheet(project_name: str) -> int | None:
    """Find (NEVER create) the job's standing Material List sheet; return its sheet ID or None.

    Resolves the per-job PROGRESS folder find-ONLY then the sheet find-ONLY; None if EITHER is
    absent. Used by the reconcile-zeroed path in `fieldops_sync`: when a job's active line count
    drops to ZERO it produces no snapshot lines, so the daemon reconciles it by finding (not
    creating) its sheet and marking every remaining Active row Removed. This NEVER creates an empty
    sheet — a job that never had a Material List sheet is simply skipped.
    """
    folder = smartsheet_client.find_folder_by_name_in_workspace(WORKSPACE_ID, _folder_name(project_name))
    if folder is None:
        return None
    return smartsheet_client.find_sheet_by_name_in_folder(folder, material_list_sheet_name(project_name))


def find_line_row(sheet_id: int, line_uuid: str) -> dict[str, Any] | None:
    """Return the row whose Line UUID == `line_uuid`, or None. The upsert/retire authority — every
    snapshot re-projection resolves its target through this."""
    key = (line_uuid or "").strip()
    if not key:
        return None
    for row in smartsheet_client.get_rows(sheet_id):
        if str(row.get(COL_LINE_UUID) or "").strip() == key:
            return row
    return None


def upsert_line_row(
    sheet_id: int,
    *,
    line_uuid: str,
    line: str,
    material: str,
    description: str,
    qty: str,
    unit: str,
    expected_date: str,
    status: str,
    delivered_qty: str,
    received_at: str,
    received_by: str,
    note: str,
    unplanned: str,
    part_number: str = "",
    category: str = "",
    expected_ship_date: str = "",
) -> int:
    """CHANGE-ONLY find-or-create of one material line as an On-List row; return its row ID.

    - MISS → append a new `On List=Active` row and return its id.
    - HIT → compare the incoming values against the row's current cells for the tracked data columns
      (and require `On List == Active`). If NOTHING changed, this is a NO-OP that returns the
      existing row id — so the re-projection does NOT churn the sheet. If ANYTHING changed (incl. a
      re-added line whose `On List` was `Removed`), issue an `update_rows` refreshing all tracked
      cells + `On List=Active`.

    All values are pre-formatted strings; `line` is the primary label (catalog name or description);
    `received_by` is the DISPLAY name only; `unplanned` is `'Yes'` or `''`. NEVER deletes — a removed
    line is marked in place by `retire_removed`.

    `part_number` / `category` / `expected_ship_date` (0059) default to `''` — the ONLY defaulted
    parameters here. They are defaulted because a line predating the migration genuinely has no value
    for them, so `''` is the truthful reading rather than a placeholder; every other field is
    required precisely so a caller cannot forget one. `expected_ship_date` is the SHIP date and is
    distinct from `expected_date`, which keeps its 0031 meaning of expected DELIVERY.
    """
    desired: dict[str, str] = {
        COL_LINE: line,
        COL_MATERIAL: material,
        COL_DESCRIPTION: description,
        COL_QTY: qty,
        COL_UNIT: unit,
        COL_EXPECTED_DATE: expected_date,
        COL_STATUS: status,
        COL_DELIVERED_QTY: delivered_qty,
        COL_RECEIVED_AT: received_at,
        COL_RECEIVED_BY: received_by,
        COL_NOTE: note,
        COL_UNPLANNED: unplanned,
        COL_ON_LIST: ON_LIST_ACTIVE,
        COL_PART_NUMBER: part_number,
        COL_CATEGORY: category,
        COL_EXPECTED_SHIP_DATE: expected_ship_date,
    }

    existing = find_line_row(sheet_id, line_uuid)
    if existing is None:
        [row_id] = smartsheet_client.add_rows(
            sheet_id,
            [
                {
                    **desired,
                    COL_LINE_UUID: line_uuid,
                    "_formats": {COL_ON_LIST: ON_LIST_ACTIVE_FMT},  # green On-List cell
                }
            ],
        )
        return row_id

    row_id = int(existing["_row_id"])
    if _row_matches(existing, desired):
        return row_id  # nothing changed — skip the needless write

    smartsheet_client.update_rows(
        sheet_id,
        [
            {
                "_row_id": row_id,
                **desired,
                "_formats": {COL_ON_LIST: ON_LIST_ACTIVE_FMT},  # green On-List cell
            }
        ],
    )
    return row_id


def _row_matches(existing: dict[str, Any], desired: dict[str, str]) -> bool:
    """True iff every tracked data column on `existing` already equals `desired` (normalized
    str-strip compare). `Line UUID` (the key) is excluded."""
    for col in _TRACKED_COLS:
        if str(existing.get(col) or "").strip() != str(desired.get(col) or "").strip():
            return False
    return True


def retire_removed(sheet_id: int, current_line_uuids: set[str]) -> int:
    """Mark every sheet row whose Line UUID is NOT in `current_line_uuids` as `On List=Removed`
    (only if not already Removed); return the number of rows newly retired.

    A snapshot re-projection: any line previously on the list but absent from THIS cycle's snapshot
    was removed (deactivated) from the operator's list → mark it Removed IN PLACE. **NEVER deletes**
    (§51 SoR rule) — the row stays as the historical record of a line once on this job's list.
    Idempotent: a row already `Removed` is skipped, so a steady state issues no write.
    """
    current = {(u or "").strip() for u in current_line_uuids}
    updates: list[dict[str, Any]] = []
    for row in smartsheet_client.get_rows(sheet_id):
        line_uuid = str(row.get(COL_LINE_UUID) or "").strip()
        if line_uuid in current:
            continue
        if str(row.get(COL_ON_LIST) or "").strip() == ON_LIST_REMOVED:
            continue  # already retired — no needless write
        updates.append(
            {
                "_row_id": row["_row_id"],
                COL_ON_LIST: ON_LIST_REMOVED,
                "_formats": {COL_ON_LIST: ON_LIST_REMOVED_FMT},  # gray On-List cell
            }
        )
    if updates:
        smartsheet_client.update_rows(sheet_id, updates)
    return len(updates)


# ---- §51 A5 row-cap watchdog (SoR-safe, WARN-only, never delete) ---------


def _read_int_setting(key: str, fallback: int) -> int:
    """Defensive int ITS_Config read under the progress_reports workstream (missing row /
    circuit-open / non-int all resolve to `fallback`, never raising into the mirror)."""
    try:
        raw = smartsheet_client.get_setting(key, workstream="progress_reports")
    except (smartsheet_client.SmartsheetNotFoundError, smartsheet_client.SmartsheetCircuitOpenError):
        return fallback
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback


def check_row_cap(sheet_id: int, sheet_name: str, *, row_count: int | None = None) -> None:
    """SoR-safe row-cap monitor — the §51 "A5 row-cap watchdog" for the standing Material List sheet.

    A per-job material list is bounded by line count, so this ~never fires — it is kept for PARITY
    with the other standing trackers (defence in depth). As the sheet nears the Smartsheet per-sheet
    row cap (~20k), WARN (`ITS_Errors`) + enqueue an operator period-split to `ITS_Review_Queue`.
    **NEVER deletes** (§51). ADVISORY: any read/enqueue failure is reduced to a WARN and never blocks
    the mirror.

    `row_count` may be passed by a caller that already read the sheet's rows this cycle (avoids a
    second full read); when None, a `get_rows` count is taken.
    """
    try:
        threshold = _read_int_setting(CFG_ROW_CAP_WARN, DEFAULT_ROW_CAP_WARN)
        count = row_count if row_count is not None else len(smartsheet_client.get_rows(sheet_id))
        if count < threshold:
            return
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            (
                f"Material List sheet {sheet_name!r} (sheet {sheet_id}) has {count} rows, at/over "
                f"the row-cap WARN threshold {threshold} (Smartsheet ~20k/sheet) — operator should "
                f"period-split it (archive this sheet, start a fresh one); NEVER delete rows."
            ),
            error_code="material_list_row_cap_warn",
        )
        review_queue.add(
            workstream="progress_reports",
            summary=(
                f"Material List sheet {sheet_name!r} nearing the Smartsheet row cap "
                f"({count}/{threshold}) — period-split needed (archive + start fresh, never delete)"
            ),
            payload={
                "sheet_id": sheet_id,
                "sheet_name": sheet_name,
                "row_count": count,
                "threshold": threshold,
                "action": "period-split (archive this sheet, start a new one); never delete rows",
            },
            sla_tier=review_queue.SlaTier.SAFETY_INTAKE,
            reason=review_queue.ReviewReason.POLICY_EDGE,
            severity=Severity.WARN,
            source_file=str(sheet_id),
        )
    except Exception as exc:  # noqa: BLE001 — advisory tripwire; never block the mirror
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"row-cap check failed for {sheet_name!r} (sheet {sheet_id}): {exc!r}",
            error_code="material_list_row_cap_check_failed",
        )
