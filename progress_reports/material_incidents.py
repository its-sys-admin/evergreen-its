"""Per-job Material Incidents tracker — the P7 standing ledger (Track 2, M3 Slice 2).

One-way-up mirror of the D1 filed `material-incident%` submissions (an M3 Slice-1-linked delivery
problem: damaged / short / wrong item / other) into a per-job **standing** "Material Incidents"
Smartsheet in the `ITS — Progress Reporting` workspace, in the SAME per-job folder as the Hours Log +
Equipment + Material List + week sheets (so they sit side by side). SEND-FREE + AI-FREE (Op Stds v20
§51 — ITS-owned structured-SoR write-back); the daemon that drives it (`field_ops.fieldops_sync`
material-incidents pass) is the capability-gated actuator.

Design (M3 Slice 2 — PORTAL-AUTHORED, ONE-WAY-UP, APPEND-ONLY):
- **Single standing sheet per job** (`<Job> — Material Incidents`) — ONE row per FILED incident
  submission (`box_verified=1`, §34-screened), keyed by `Incident UUID` (== submission_uuid).
- **APPEND-ONLY EVENT LEDGER, NOT a re-projected snapshot.** A reported incident is an immutable
  historical event — it is NEVER removed. This is the deliberate contrast with `material_list` /
  `equipment_status` (which re-project mutable line/equipment STATE and mark rows Removed / Off Job):
  there is **no `retire_removed`, no `On List` column, and no reconcile-zeroed branch** here, so the
  count-drops-to-zero / #468 zero-drop class is STRUCTURALLY IMPOSSIBLE (there is no retire path to
  wrongly zero). An archived job's incident history is preserved and archive-MOVED on closure (the
  §51 archive-on-closure move in `fieldops_sync`), never lost.
- Because the same filed set is re-projected each cycle, `upsert_incident_row` is **CHANGE-ONLY**: on
  a find-hit it compares the incoming values against the row's current cells and issues an
  `update_rows` ONLY when something actually changed. An incident's own fields are immutable, so in
  steady state this is a no-op; the ONE field that legitimately changes is `Line Status` — the CURRENT
  status of the referenced expected-materials line (M3 Slice 1 `line_uuid` join). The flag is STICKY —
  a delivery mark never clears it — so the flip off `incident` happens exactly when a manager/admin
  runs the explicit **Resolve problem** action (the Worker's `resolve-incident` route, 2026-08-11):
  the line then lands where the delivery ledger points (`received` on a latest full delivery, else
  back to `expected`), and THAT resolution is what this column mirrors. There is NO watermark /
  mark-mirrored (the Worker route is a re-projected filed-set read, not an event drain).
- **ONE-WAY-UP ONLY.** The incident is authored IN THE PORTAL (the manager-side material-incident
  form); this mirror is read-only reflection UP. No down-sync, no `smartsheet_row_id` reverse link.
- **Progress workspace only**, single-destination. The per-job FOLDER + capacity tripwire are reused
  from `hours_log` so all the per-job trackers share one folder-resolution authority.
- `check_row_cap` is MORE relevant here than for the bounded Material List: an incident ledger grows
  monotonically (append-only), so the §51 A5 WARN + operator period-split guards the Smartsheet
  ~20k/sheet ceiling.

The module is a thin Smartsheet write helper (like `material_list` / `hours_log` / `equipment_status`)
— NOT a daemon entry point, so it is not itself in `GATED_SCRIPTS`; its sole caller `fieldops_sync`
is. It imports no raw network / AI / send capability (F02) — egress is the daemon's `portal_client`.
"""
from __future__ import annotations

from typing import Any

from progress_reports import hours_log
from shared import error_log, review_queue, sheet_capacity, sheet_ids, smartsheet_client
from shared.error_log import Severity

SCRIPT_NAME = "progress_reports.material_incidents"

WORKSPACE_ID = sheet_ids.WORKSPACE_PROGRESS_REPORTING

# Smartsheet sheet-name cap (HTTP 400 errorCode 1041) — same constant as material_list.SHEET_NAME_MAX.
SHEET_NAME_MAX = 50
SHEET_SUFFIX = " — Material Incidents"

# §51 A5 row-cap watchdog — an append-only ledger grows monotonically, so this is a REAL guard (unlike
# the bounded Material List where it ~never fires). WARN + Review-Queue an operator period-split as the
# sheet nears the Smartsheet ~20k/sheet row cap; NEVER delete (SoR — a reported incident is permanent).
CFG_ROW_CAP_WARN = "progress_reports.material_incidents.row_cap_warn_threshold"
DEFAULT_ROW_CAP_WARN = 15000  # conservative WARN mark well under the ~20k Smartsheet per-sheet cap

# ---- Column titles (single source of truth for reads + writes) ----
COL_MATERIAL = "Material"            # primary (TEXT_NUMBER): the reported material_description
COL_INCIDENT_UUID = "Incident UUID"  # upsert key (== filed submission's submission_uuid)
COL_ISSUE = "Issue"                  # Damaged | Short | Wrong item | Other (as reported)
COL_LINE_UUID = "Line UUID"          # referenced expected-materials line (M3 Slice 1), '' if unlinked
COL_LINE_STATUS = "Line Status"      # the referenced line's CURRENT status (expected|received|incident)
COL_QTY_EXPECTED = "Qty Expected"    # pre-formatted string
COL_QTY_RECEIVED = "Qty Received"    # pre-formatted string
COL_DELIVERY_REF = "Delivery Ref"    # PO / delivery reference
COL_DETAILS = "Details"              # the narrative (what is wrong)
COL_ACTION_TAKEN = "Action Taken"    # remediation noted by the reporter
COL_REPORTED_BY = "Reported By"      # DISPLAY NAME (personnel.name) — NEVER a username (Reflex §5)
COL_REPORTED_AT = "Reported At"      # DATE (the incident work day)
COL_REPORT = "Report"                # the filed Box PDF link (box_link)

# Data columns compared for CHANGE-ONLY upsert (Incident UUID is the key; it does not participate in
# change detection). An incident's own fields are immutable; `Line Status` is the one that changes.
_TRACKED_COLS: tuple[str, ...] = (
    COL_MATERIAL, COL_ISSUE, COL_LINE_UUID, COL_LINE_STATUS, COL_QTY_EXPECTED, COL_QTY_RECEIVED,
    COL_DELIVERY_REF, COL_DETAILS, COL_ACTION_TAKEN, COL_REPORTED_BY, COL_REPORTED_AT, COL_REPORT,
)

# Schema passed to create_sheet_in_folder. Order = left-to-right UI order. Exactly one primary
# (TEXT_NUMBER, Smartsheet requirement).
MATERIAL_INCIDENTS_COLUMNS: list[dict[str, Any]] = [
    {"title": COL_MATERIAL, "type": "TEXT_NUMBER", "primary": True},
    {"title": COL_INCIDENT_UUID, "type": "TEXT_NUMBER"},
    {"title": COL_ISSUE, "type": "TEXT_NUMBER"},
    {"title": COL_LINE_UUID, "type": "TEXT_NUMBER"},
    {"title": COL_LINE_STATUS, "type": "TEXT_NUMBER"},
    {"title": COL_QTY_EXPECTED, "type": "TEXT_NUMBER"},
    {"title": COL_QTY_RECEIVED, "type": "TEXT_NUMBER"},
    {"title": COL_DELIVERY_REF, "type": "TEXT_NUMBER"},
    {"title": COL_DETAILS, "type": "TEXT_NUMBER"},
    {"title": COL_ACTION_TAKEN, "type": "TEXT_NUMBER"},
    {"title": COL_REPORTED_BY, "type": "TEXT_NUMBER"},
    {"title": COL_REPORTED_AT, "type": "DATE"},
    {"title": COL_REPORT, "type": "TEXT_NUMBER"},
]

# Cosmetic styling — Smartsheet format-descriptor strings (mirror material_list's palette).
# Applied AFTER create, best-effort (the API ignores width/format at POST).
FMT_PRIMARY = ",,1,,,,,,38,7,,,,,,,"       # bold + dark-green text + light-green bg
FMT_DATE = ",,,,,,,,,,,,,,,,2"             # MMM_D_YYYY

MATERIAL_INCIDENTS_STYLES: list[dict[str, Any]] = [
    {"title": COL_MATERIAL, "width": 220, "format": FMT_PRIMARY},
    {"title": COL_INCIDENT_UUID, "width": 90},
    {"title": COL_ISSUE, "width": 110},
    {"title": COL_LINE_UUID, "width": 90},
    {"title": COL_LINE_STATUS, "width": 100},
    {"title": COL_QTY_EXPECTED, "width": 100},
    {"title": COL_QTY_RECEIVED, "width": 100},
    {"title": COL_DELIVERY_REF, "width": 130},
    {"title": COL_DETAILS, "width": 300},
    {"title": COL_ACTION_TAKEN, "width": 260},
    {"title": COL_REPORTED_BY, "width": 170},
    {"title": COL_REPORTED_AT, "width": 120, "format": FMT_DATE},
    {"title": COL_REPORT, "width": 220},
]


def material_incidents_sheet_name(project_name: str) -> str:
    """`'<Job> — Material Incidents'`, prefix-truncated to the 50-char cap (errorCode 1041).

    The ` — Material Incidents` suffix is preserved WHOLE (it names the sheet within the per-job
    folder); the project prefix is truncated when needed — no identity is lost because the per-job
    FOLDER already carries the full `project_name` and the sheet is only ever resolved find-or-create
    WITHIN that folder. Names ≤50 chars are byte-identical to the untruncated form.
    """
    prefix = project_name.strip()
    budget = SHEET_NAME_MAX - len(SHEET_SUFFIX)
    if len(prefix) > budget:
        prefix = prefix[:budget].rstrip()
    return f"{prefix}{SHEET_SUFFIX}"


def _apply_styles_best_effort(sheet_id: int) -> None:
    """Apply `MATERIAL_INCIDENTS_STYLES` to a freshly-created sheet. Cosmetic only — a failure WARNs
    (never silent) but must NOT fail the already-created sheet (the data path is unaffected)."""
    try:
        smartsheet_client.apply_column_styles(sheet_id, MATERIAL_INCIDENTS_STYLES)
    except Exception as exc:  # noqa: BLE001 — cosmetic; never fail sheet creation
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"material-incidents styling failed (sheet {sheet_id}): {type(exc).__name__}: {exc!r}",
            error_code="material_incidents_style_failed",
        )


def _folder_name(project_name: str) -> str:
    """Per-job folder title/key — the SAME source of truth the Hours Log / Equipment / Material List /
    week sheets use, so the Material Incidents sheet lands in the job's existing folder."""
    return hours_log._folder_name(project_name)


def _ensure_job_folder(project_name: str) -> int:
    """Find-or-create the per-job folder in the progress workspace (idempotent, race-tolerant).

    Delegates to `hours_log._ensure_job_folder` so the Material Incidents sheet sits BESIDE the Hours
    Log + Equipment + Material List + week sheets — one folder-resolution authority (DRY). The rare
    duplicate-folder WARN is logged under the Hours Log's script name; that is acceptable given the
    shared resolver.
    """
    return hours_log._ensure_job_folder(project_name)


def _warn_on_thin_headroom(sheet_name: str) -> None:
    """A1 sheet-count tripwire, run before each CREATE. ADVISORY, never blocking (mirrors
    material_list._warn_on_thin_headroom): a margin breach WARNs + enqueues the operator signal, then
    the create PROCEEDS. Belt-and-suspenders fail-open — any exception is reduced to a WARN."""
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


def ensure_material_incidents_sheet(project_name: str) -> int:
    """Find-or-create the job's single standing Material Incidents sheet; return its sheet ID.

    Idempotent: a second call returns the same sheet with no write. Race-tolerant at both the folder
    and sheet levels (re-find after create, adopt first, WARN the duplicate). The A1 capacity tripwire
    runs ONLY on the create branch.
    """
    folder_id = _ensure_job_folder(project_name)
    name = material_incidents_sheet_name(project_name)

    existing = smartsheet_client.find_sheet_by_name_in_folder(folder_id, name)
    if existing is not None:
        return existing

    _warn_on_thin_headroom(name)
    sheet_id = smartsheet_client.create_sheet_in_folder(folder_id, name, MATERIAL_INCIDENTS_COLUMNS)
    post_find = smartsheet_client.find_sheet_by_name_in_folder(folder_id, name)
    if post_find is not None and post_find != sheet_id:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"duplicate Material Incidents sheet {name!r} under folder {folder_id} "
            f"(project={project_name!r}); using first match {post_find}, manual cleanup "
            f"needed for {sheet_id}.",
            error_code="material_incidents_sheet_race_duplicate",
        )
        return post_find
    _apply_styles_best_effort(sheet_id)  # cosmetic; create path only
    return sheet_id


def find_incident_row(sheet_id: int, incident_uuid: str) -> dict[str, Any] | None:
    """Return the row whose Incident UUID == `incident_uuid`, or None. The upsert authority — every
    re-projection resolves its target through this."""
    key = (incident_uuid or "").strip()
    if not key:
        return None
    for row in smartsheet_client.get_rows(sheet_id):
        if str(row.get(COL_INCIDENT_UUID) or "").strip() == key:
            return row
    return None


def upsert_incident_row(
    sheet_id: int,
    *,
    incident_uuid: str,
    material: str,
    issue: str,
    line_uuid: str,
    line_status: str,
    qty_expected: str,
    qty_received: str,
    delivery_ref: str,
    details: str,
    action_taken: str,
    reported_by: str,
    reported_at: str,
    report: str,
) -> int:
    """CHANGE-ONLY find-or-create of one filed incident as a ledger row; return its row ID.

    - MISS → append a new row and return its id.
    - HIT → compare the incoming values against the row's current cells for the tracked data columns.
      If NOTHING changed, this is a NO-OP that returns the existing row id — so the re-projection does
      NOT churn the sheet. If ANYTHING changed (in practice only `Line Status`, when a later receipt
      resolves the referenced line), issue an `update_rows` refreshing all tracked cells.

    All values are pre-formatted strings; `material` is the primary label (the reported
    material_description); `reported_by` is the DISPLAY name only; `report` is the filed Box PDF link.
    NEVER deletes and NEVER marks a row Removed — an incident is a permanent historical event.
    """
    desired: dict[str, str] = {
        COL_MATERIAL: material,
        COL_ISSUE: issue,
        COL_LINE_UUID: line_uuid,
        COL_LINE_STATUS: line_status,
        COL_QTY_EXPECTED: qty_expected,
        COL_QTY_RECEIVED: qty_received,
        COL_DELIVERY_REF: delivery_ref,
        COL_DETAILS: details,
        COL_ACTION_TAKEN: action_taken,
        COL_REPORTED_BY: reported_by,
        COL_REPORTED_AT: reported_at,
        COL_REPORT: report,
    }

    existing = find_incident_row(sheet_id, incident_uuid)
    if existing is None:
        [row_id] = smartsheet_client.add_rows(
            sheet_id,
            [{**desired, COL_INCIDENT_UUID: incident_uuid}],
        )
        return row_id

    row_id = int(existing["_row_id"])
    if _row_matches(existing, desired):
        return row_id  # nothing changed — skip the needless write

    smartsheet_client.update_rows(
        sheet_id,
        [{"_row_id": row_id, **desired}],
    )
    return row_id


def _row_matches(existing: dict[str, Any], desired: dict[str, str]) -> bool:
    """True iff every tracked data column on `existing` already equals `desired` (normalized
    str-strip compare). `Incident UUID` (the key) is excluded."""
    for col in _TRACKED_COLS:
        if str(existing.get(col) or "").strip() != str(desired.get(col) or "").strip():
            return False
    return True


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
    """SoR-safe row-cap monitor — the §51 "A5 row-cap watchdog" for the standing Material Incidents
    sheet.

    An append-only incident ledger grows monotonically, so this is a REAL guard (unlike the bounded
    Material List where it ~never fires). As the sheet nears the Smartsheet per-sheet row cap (~20k),
    WARN (`ITS_Errors`) + enqueue an operator period-split to `ITS_Review_Queue`. **NEVER deletes**
    (§51 — a reported incident is a permanent record). ADVISORY: any read/enqueue failure is reduced
    to a WARN and never blocks the mirror.

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
                f"Material Incidents sheet {sheet_name!r} (sheet {sheet_id}) has {count} rows, at/over "
                f"the row-cap WARN threshold {threshold} (Smartsheet ~20k/sheet) — operator should "
                f"period-split it (archive this sheet, start a fresh one); NEVER delete rows."
            ),
            error_code="material_incidents_row_cap_warn",
        )
        review_queue.add(
            workstream="progress_reports",
            summary=(
                f"Material Incidents sheet {sheet_name!r} nearing the Smartsheet row cap "
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
            error_code="material_incidents_row_cap_check_failed",
        )
