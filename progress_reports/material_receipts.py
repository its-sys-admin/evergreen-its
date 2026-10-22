"""Per-job Material Receipts tracker — the delivery ledger's §51 mirror (PR4).

One-way-up mirror of the D1 `material_receipt_events` ledger (migration 0059 — the append-only
three-way delivery mark: delivered / partial / not_delivered) into a per-job **standing**
"Material Receipts" Smartsheet in the `ITS — Progress Reporting` workspace, in the SAME per-job
folder as the Hours Log + Equipment + Material List + Material Incidents + week sheets (so they sit
side by side). SEND-FREE + AI-FREE (Op Stds §51 — ITS-owned structured-SoR write-back); the daemon
that drives it (`field_ops.fieldops_sync` material-receipts pass) is the capability-gated actuator.

WHY THIS MIRROR EXISTS. Until PR2 a line was received ONCE — the receive route guarded
`status='expected'` IN-WHERE, so a second mark was a clean 409 and a partial delivery could never be
completed. `material_receipt_events` replaced that flag with an append-only ledger: one row per mark,
each carrying its own qty, note, actor and event date, with the line's delivery state DERIVED as a
rollup. The office reads Smartsheet, so without this mirror that ledger is portal-only and the
question "what actually showed up, and when" has no answer outside the app.

Design (PORTAL-AUTHORED, ONE-WAY-UP, APPEND-ONLY — the `material_incidents` posture, not
`material_list`'s):
- **Single standing sheet per job** (`<Job> — Material Receipts`) — ONE row per receipt EVENT, keyed
  by `Event UUID` (== `material_receipt_events.event_uuid`, minted at the one insert path).
- **APPEND-ONLY EVENT LEDGER, NOT a re-projected snapshot.** A delivery mark is an immutable
  historical event — somebody signed for something on a date. It is NEVER removed. This is the
  deliberate contrast with `material_list` / `equipment_status` (which re-project mutable line /
  equipment STATE and mark rows Removed / Off Job): there is **no `retire_removed`, no `On List`
  column, and no reconcile-zeroed branch** here, so the count-drops-to-zero / #468 zero-drop class is
  STRUCTURALLY IMPOSSIBLE — there is no retire path to wrongly zero. An archived job's delivery
  history is preserved and archive-MOVED on closure (the §51 archive-on-closure move in
  `fieldops_sync`), never lost.
- Because the same event set is re-projected each cycle, `upsert_receipt_row` is **CHANGE-ONLY**: on
  a find-hit it compares the incoming values against the row's current cells and issues an
  `update_rows` ONLY when something actually changed. A receipt event's own fields are immutable, so
  in steady state this is a no-op. The fields that legitimately change are the two DERIVED ones —
  `Line Status` (the referenced line's coarse status, which goes `incident` if a problem is later
  flagged) and `Line Qty Received` (the ledger rollup, which grows as further partials land) — plus
  the VOID annotation below. That live-resolution signal is the same payoff `material_incidents`
  gets from its line reference.
- **A VOIDED mark is ANNOTATED, never removed (migration 0080).** A manager can void a mark entered
  in error (a double count, the wrong line) and optionally record the correct entry linked to it.
  The void is a further fact ABOUT the mark — who struck it, when, and why — not an erasure of it,
  so the row stays and the no-retire property above still holds: there is still no delete and no
  retire path anywhere in this module. Six columns appended at the END of the schema carry it:
  `Entry Status` (`Active` | `Voided`, a TEXT controlled vocabulary like the Hours Log's Status —
  NOT a PICKLIST, so nothing here enters `picklist_validation.REGISTRY`), `Voided By` (the route's
  DISPLAY name, never a username), `Voided At` (Pacific wall-clock), `Void Reason`, and the
  correction links `Supersedes` / `Superseded By` (Event UUIDs, the sheet's key — a D1 row id means
  nothing here). A voided row is grayed whole-row with the Hours Log's Superseded descriptor, so an
  office reader sees at a glance that it no longer counts. The Worker already excludes voided marks
  from `Line Qty Received`, so the sheet can never total a line differently from the portal.
- **Schema back-fill.** A sheet's columns are fixed at CREATE, so every sheet made before 0080 lacks
  the six. `backfill_columns` adds them through `smartsheet_client.ensure_columns` — called by the
  pass AFTER its index read (`read_receipt_index`), whose `get_rows` seeds the column map, so on an
  up-to-date sheet the back-fill check costs no read at all. If the back-fill fails, or a column is
  still absent after it, the pass skips that job's upserts for the cycle (every one would raise on
  the absent column): a transient failure's WARN is the one signal, and a permanent one (a 403, a
  deleted sheet) is ticketed to the Review Queue. Each row mirrored before 0080 is then re-written
  ONCE, its blank `Entry Status` becoming `Active` — a one-time per-row write in the same cycle as
  the back-fill, steady again from the next.
- **ONE-WAY-UP ONLY.** The mark is authored IN THE PORTAL (the per-job Materials tracking page; the
  daily form's receipt controls were removed 2026-08-11, #74); this mirror is read-only reflection
  UP. No down-sync, no `smartsheet_row_id` reverse link.
- **Progress workspace only**, single-destination. The per-job FOLDER + capacity tripwire are reused
  from `hours_log` so all the per-job trackers share one folder-resolution authority.
- `check_row_cap` matters MORE here than for the bounded Material List, and arguably more than for
  incidents: a job with 500 expected lines delivered across several truckloads each generates a row,
  so the ledger grows faster than the incident one. The §51 A5 WARN + operator period-split guards
  the Smartsheet ~20k/sheet ceiling.

The module is a thin Smartsheet write helper (like `material_incidents` / `material_list` /
`hours_log` / `equipment_status`) — NOT a daemon entry point, so it is not itself in `GATED_SCRIPTS`;
its sole caller `fieldops_sync` is. It imports no raw network / AI / send capability (F02) — egress
is the daemon's `portal_client`.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from progress_reports import hours_log
from shared import error_log, review_queue, sheet_capacity, sheet_ids, smartsheet_client
from shared.error_log import Severity

SCRIPT_NAME = "progress_reports.material_receipts"

WORKSPACE_ID = sheet_ids.WORKSPACE_PROGRESS_REPORTING

# Smartsheet sheet-name cap (HTTP 400 errorCode 1041) — same constant as material_list.SHEET_NAME_MAX.
SHEET_NAME_MAX = 50
SHEET_SUFFIX = " — Material Receipts"

# §51 A5 row-cap watchdog — an append-only ledger grows monotonically, and this one grows faster than
# the incident ledger (every truckload of every line is an event). WARN + Review-Queue an operator
# period-split as the sheet nears the Smartsheet ~20k/sheet row cap; NEVER delete (SoR — a signed-for
# delivery is permanent).
CFG_ROW_CAP_WARN = "progress_reports.material_receipts.row_cap_warn_threshold"
DEFAULT_ROW_CAP_WARN = 15000  # conservative WARN mark well under the ~20k Smartsheet per-sheet cap

# ---- Column titles (single source of truth for reads + writes) ----
COL_MATERIAL = "Material"              # primary (TEXT_NUMBER): the line's description
COL_EVENT_UUID = "Event UUID"          # upsert key (== material_receipt_events.event_uuid)
COL_KIND = "Kind"                      # Delivered | Partial | Not delivered (display-cased)
COL_QTY = "Qty"                        # received on THIS event (blank for not_delivered)
COL_UNIT = "Unit"                      # the line's unit of measure
COL_PART_NUMBER = "Part Number"        # the line's BOM part number (0059), '' when unset
COL_LINE_UUID = "Line UUID"            # the expected-materials line this event marks
COL_LINE_STATUS = "Line Status"        # the referenced line's CURRENT coarse status — DERIVED
COL_LINE_QTY_EXPECTED = "Line Qty Expected"  # the line's expected qty, for context
COL_LINE_QTY_RECEIVED = "Line Qty Received"  # ledger rollup across ALL events — DERIVED
COL_BOL = "BOL"                        # the shipment's BOL / load number when the event names one
COL_NOTE = "Note"                      # the marker's note (required on not_delivered)
COL_RECEIVED_BY = "Received By"        # DISPLAY NAME (personnel.name) — NEVER a username (Reflex §5)
COL_EVENT_DATE = "Event Date"          # DATE (the day the delivery happened)
# 0080 void annotation. APPENDED at the end of the schema rather than slotted beside a logical
# neighbour: `ensure_columns` back-fills pre-existing sheets by APPENDING, so keeping the creation
# order identical is what stops the two sheet populations diverging visually forever.
COL_ENTRY_STATUS = "Entry Status"      # Active | Voided — TEXT controlled-vocab, NOT a PICKLIST
COL_VOIDED_BY = "Voided By"            # DISPLAY NAME, else the role label (D6) — NEVER a username
COL_VOIDED_AT = "Voided At"            # Pacific wall-clock 'YYYY-MM-DD HH:MM' (pre-formatted)
COL_VOID_REASON = "Void Reason"        # the voider's required reason
COL_SUPERSEDES = "Supersedes"          # Event UUID of the voided mark this correction replaces
COL_SUPERSEDED_BY = "Superseded By"    # Event UUID of the correction that replaced this mark

# Controlled vocabulary (TEXT cells, NOT picklist — avoids the REGISTRY-parity footgun; mirrors
# hours_log's Status choice).
ENTRY_ACTIVE = "Active"
ENTRY_VOIDED = "Voided"

# Data columns compared for CHANGE-ONLY upsert (Event UUID is the key; it does not participate in
# change detection). A receipt event's own fields are immutable; `Line Status` and
# `Line Qty Received` are the DERIVED pair that legitimately change as later events land, and the
# six void columns change when the mark is voided or a correction is linked to it — a void can come
# days after the mark, so without them here a voided row would never be re-written.
_TRACKED_COLS: tuple[str, ...] = (
    COL_MATERIAL, COL_KIND, COL_QTY, COL_UNIT, COL_PART_NUMBER, COL_LINE_UUID, COL_LINE_STATUS,
    COL_LINE_QTY_EXPECTED, COL_LINE_QTY_RECEIVED, COL_BOL, COL_NOTE, COL_RECEIVED_BY, COL_EVENT_DATE,
    COL_ENTRY_STATUS, COL_VOIDED_BY, COL_VOIDED_AT, COL_VOID_REASON, COL_SUPERSEDES,
    COL_SUPERSEDED_BY,
)

# Schema passed to create_sheet_in_folder. Order = left-to-right UI order. Exactly one primary
# (TEXT_NUMBER, Smartsheet requirement).
MATERIAL_RECEIPTS_COLUMNS: list[dict[str, Any]] = [
    {"title": COL_MATERIAL, "type": "TEXT_NUMBER", "primary": True},
    {"title": COL_EVENT_UUID, "type": "TEXT_NUMBER"},
    {"title": COL_KIND, "type": "TEXT_NUMBER"},
    {"title": COL_QTY, "type": "TEXT_NUMBER"},
    {"title": COL_UNIT, "type": "TEXT_NUMBER"},
    {"title": COL_PART_NUMBER, "type": "TEXT_NUMBER"},
    {"title": COL_LINE_UUID, "type": "TEXT_NUMBER"},
    {"title": COL_LINE_STATUS, "type": "TEXT_NUMBER"},
    {"title": COL_LINE_QTY_EXPECTED, "type": "TEXT_NUMBER"},
    {"title": COL_LINE_QTY_RECEIVED, "type": "TEXT_NUMBER"},
    {"title": COL_BOL, "type": "TEXT_NUMBER"},
    {"title": COL_NOTE, "type": "TEXT_NUMBER"},
    {"title": COL_RECEIVED_BY, "type": "TEXT_NUMBER"},
    {"title": COL_EVENT_DATE, "type": "DATE"},
    # 0080 — keep these LAST (see the column-title block): back-fill appends.
    {"title": COL_ENTRY_STATUS, "type": "TEXT_NUMBER"},
    {"title": COL_VOIDED_BY, "type": "TEXT_NUMBER"},
    {"title": COL_VOIDED_AT, "type": "TEXT_NUMBER"},
    {"title": COL_VOID_REASON, "type": "TEXT_NUMBER"},
    {"title": COL_SUPERSEDES, "type": "TEXT_NUMBER"},
    {"title": COL_SUPERSEDED_BY, "type": "TEXT_NUMBER"},
]

# What `backfill_columns` adds to an ALREADY-CREATED sheet: EVERY non-primary column, derived from
# the creation schema so the two cannot drift (material_list.BACKFILL_COLUMNS' reasoning — a
# hand-cut tail slice goes silently wrong on the next addition). It costs nothing once the pass's
# index read has seeded the column map: `ensure_columns` resolves every title against that ONE map
# and makes no API call when none is missing. When some are, they go in ONE call under ONE shared
# index — a body whose columns carry different indices is rejected (HTTP 400, errorCode 1135, live
# 2026-08-10).
BACKFILL_COLUMNS: list[dict[str, Any]] = [
    c for c in MATERIAL_RECEIPTS_COLUMNS if not c.get("primary")
]

# Cosmetic styling — Smartsheet format-descriptor strings (mirror material_incidents' palette).
# Applied AFTER create, best-effort (the API ignores width/format at POST).
FMT_PRIMARY = ",,1,,,,,,38,7,,,,,,,"       # bold + dark-green text + light-green bg
FMT_DATE = ",,,,,,,,,,,,,,,,2"             # MMM_D_YYYY
# A VOIDED row's per-cell format: the Hours Log's Superseded gray, reused rather than re-typed so
# the two "struck from the ledger" looks cannot drift. Written on EVERY cell of the row (key
# included), riding the same `_formats` mechanism hours_log uses on its Status cell. A per-cell
# format replaces the column's, so the date cell carries the gray AND its date display (field 16 —
# FMT_DATE's) or it would fall back to the sheet's default date rendering.
FMT_VOIDED = hours_log.STATUS_SUPERSEDED_FMT  # light-gray bg
FMT_VOIDED_DATE = ",,,,,,,,,18,,,,,,,2"        # light-gray bg + MMM_D_YYYY

MATERIAL_RECEIPTS_STYLES: list[dict[str, Any]] = [
    {"title": COL_MATERIAL, "width": 220, "format": FMT_PRIMARY},
    {"title": COL_EVENT_UUID, "width": 90},
    {"title": COL_KIND, "width": 110},
    {"title": COL_QTY, "width": 80},
    {"title": COL_UNIT, "width": 70},
    {"title": COL_PART_NUMBER, "width": 120},
    {"title": COL_LINE_UUID, "width": 90},
    {"title": COL_LINE_STATUS, "width": 100},
    {"title": COL_LINE_QTY_EXPECTED, "width": 120},
    {"title": COL_LINE_QTY_RECEIVED, "width": 120},
    {"title": COL_BOL, "width": 130},
    {"title": COL_NOTE, "width": 300},
    {"title": COL_RECEIVED_BY, "width": 170},
    {"title": COL_EVENT_DATE, "width": 120, "format": FMT_DATE},
    {"title": COL_ENTRY_STATUS, "width": 100},
    {"title": COL_VOIDED_BY, "width": 170},
    {"title": COL_VOIDED_AT, "width": 140},
    {"title": COL_VOID_REASON, "width": 260},
    {"title": COL_SUPERSEDES, "width": 90},
    {"title": COL_SUPERSEDED_BY, "width": 90},
]


def material_receipts_sheet_name(project_name: str) -> str:
    """`'<Job> — Material Receipts'`, prefix-truncated to the 50-char cap (errorCode 1041).

    The ` — Material Receipts` suffix is preserved WHOLE (it names the sheet within the per-job
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
    """Apply `MATERIAL_RECEIPTS_STYLES` to a freshly-created sheet. Cosmetic only — a failure WARNs
    (never silent) but must NOT fail the already-created sheet (the data path is unaffected)."""
    try:
        smartsheet_client.apply_column_styles(sheet_id, MATERIAL_RECEIPTS_STYLES)
    except Exception as exc:  # noqa: BLE001 — cosmetic; never fail sheet creation
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"material-receipts styling failed (sheet {sheet_id}): {type(exc).__name__}: {exc!r}",
            error_code="material_receipts_style_failed",
        )


def _folder_name(project_name: str) -> str:
    """Per-job folder title/key — the SAME source of truth the Hours Log / Equipment / Material List /
    Material Incidents / week sheets use, so the Material Receipts sheet lands in the job's existing
    folder."""
    return hours_log._folder_name(project_name)


def _ensure_job_folder(project_name: str) -> int:
    """Find-or-create the per-job folder in the progress workspace (idempotent, race-tolerant).

    Delegates to `hours_log._ensure_job_folder` so the Material Receipts sheet sits BESIDE the Hours
    Log + Equipment + Material List + Material Incidents + week sheets — one folder-resolution
    authority (DRY). The rare duplicate-folder WARN is logged under the Hours Log's script name; that
    is acceptable given the shared resolver.
    """
    return hours_log._ensure_job_folder(project_name)


def _warn_on_thin_headroom(sheet_name: str) -> None:
    """A1 sheet-count tripwire, run before each CREATE. ADVISORY, never blocking (mirrors
    material_incidents._warn_on_thin_headroom): a margin breach WARNs + enqueues the operator signal,
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


def backfill_columns(sheet_id: int, sheet_name: str) -> bool:
    """Add any `BACKFILL_COLUMNS` title missing from the sheet; True iff every one is present after.

    Raises ONLY the permanent Smartsheet refusals — `SmartsheetPermissionError` (adding a column
    needs Admin on the sheet, so an Editor share fails here after the read succeeded),
    `SmartsheetNotFoundError` and `SmartsheetValidationError` — so the caller can ticket them like
    every other permanent arm of the pass. Retrying those every cycle with a WARN would leave the
    job unmirrored indefinitely with no Review-Queue row and no one told. Everything else is
    reported through the return value.

    CALL IT AFTER `read_receipt_index`. That read seeds the process column map, so on an up-to-date
    sheet this is a dict lookup with no API call. Called first — as the find branch of
    `ensure_material_receipts_sheet` used to — it ran on a cold map and paid a full-sheet read of
    its own every cycle (fieldops_sync is one-shot per launchd interval, so the map is never warm).

    False means a row write would fail: the add raised a transient error (breaker OPEN, a rate
    limit, an API blip), or a column is STILL absent afterwards (`smartsheet_client.missing_columns` is the post-condition —
    after an add it is the re-read the first write would have made anyway, so it costs nothing
    extra). The pass then skips this job's upserts for the cycle: each one names every tracked
    column, so each would raise `KeyError` out of `_resolve_cells` — one ERROR per event, every
    cycle, for one cause. This WARN is that cause's ONE signal; the next cycle retries.
    """
    titles = [str(c["title"]) for c in BACKFILL_COLUMNS]
    try:
        added = smartsheet_client.ensure_columns(sheet_id, BACKFILL_COLUMNS)
        absent = smartsheet_client.missing_columns(sheet_id, titles)
    except (
        smartsheet_client.SmartsheetPermissionError,
        smartsheet_client.SmartsheetNotFoundError,
        smartsheet_client.SmartsheetValidationError,
    ):
        raise  # permanent — the caller tickets it (see the docstring)
    except Exception as exc:  # noqa: BLE001 — transient schema top-up; reported via the return
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"could not back-fill columns on Material Receipts {sheet_name!r} (sheet {sheet_id}); "
            f"this job's receipts are NOT mirrored until it succeeds (retried every cycle): "
            f"{type(exc).__name__}: {exc!r}",
            error_code="material_receipts_column_backfill_failed",
        )
        return False
    if added:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"added {len(added)} missing column(s) to Material Receipts {sheet_name!r} "
            f"(sheet {sheet_id}): {', '.join(added)} — a pre-existing sheet was brought up to the "
            f"current tracker schema.",
            error_code="material_receipts_columns_backfilled",
        )
    if absent:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"Material Receipts {sheet_name!r} (sheet {sheet_id}) still lacks column(s) "
            f"{', '.join(absent)} after the back-fill; this job's receipts are NOT mirrored until "
            f"they exist (retried every cycle).",
            error_code="material_receipts_column_backfill_failed",
        )
        return False
    return True


def ensure_material_receipts_sheet(project_name: str) -> int:
    """Find-or-create the job's single standing Material Receipts sheet; return its sheet ID.

    Idempotent: a second call returns the same sheet with no write. Race-tolerant at both the folder
    and sheet levels (re-find after create, adopt first, WARN the duplicate). The A1 capacity tripwire
    runs ONLY on the create branch.

    Find-or-create ONLY — the column back-fill is the caller's next step after its index read
    (`backfill_columns`), so that it runs on the column map the read seeded rather than fetching one.
    """
    folder_id = _ensure_job_folder(project_name)
    name = material_receipts_sheet_name(project_name)

    existing = smartsheet_client.find_sheet_by_name_in_folder(folder_id, name)
    if existing is not None:
        return existing

    _warn_on_thin_headroom(name)
    sheet_id = smartsheet_client.create_sheet_in_folder(folder_id, name, MATERIAL_RECEIPTS_COLUMNS)
    post_find = smartsheet_client.find_sheet_by_name_in_folder(folder_id, name)
    if post_find is not None and post_find != sheet_id:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"duplicate Material Receipts sheet {name!r} under folder {folder_id} "
            f"(project={project_name!r}); using first match {post_find}, manual cleanup "
            f"needed for {sheet_id}.",
            error_code="material_receipts_sheet_race_duplicate",
        )
        return post_find
    _apply_styles_best_effort(sheet_id)  # cosmetic; create path only
    return sheet_id


def read_receipt_index(sheet_id: int) -> smartsheet_client.RowIndex:
    """Read the ledger ONCE and index it by Event UUID — the read-once authority (D10) a pass threads
    through every `upsert_receipt_row` and the row-cap count.

    Before D10 each upsert re-read the WHOLE ledger to find its own row, so a job with N receipts
    cost N+1 full-sheet reads per cycle — on the fastest-growing per-job tracker, whose reads only get
    bigger. It is now ONE per sheet per pass."""
    return smartsheet_client.RowIndex(smartsheet_client.get_rows(sheet_id), COL_EVENT_UUID)


def find_receipt_row(
    sheet_id: int, event_uuid: str, *, index: smartsheet_client.RowIndex | None = None
) -> dict[str, Any] | None:
    """Return the row whose Event UUID == `event_uuid`, or None. The upsert authority — every
    re-projection resolves its target through this.

    Pass the pass's `index` (from `read_receipt_index`); without one this does its own full read —
    the pre-D10 behaviour, kept so a caller outside a pass still works. Matching is unchanged:
    stripped text compare, FIRST row wins on a duplicate key, a blank key is None."""
    if index is None:
        index = read_receipt_index(sheet_id)
    return index.find(event_uuid)


def upsert_receipt_row(
    sheet_id: int,
    *,
    event_uuid: str,
    material: str,
    kind: str,
    qty: str,
    unit: str,
    part_number: str,
    line_uuid: str,
    line_status: str,
    line_qty_expected: str,
    line_qty_received: str,
    bol: str,
    note: str,
    received_by: str,
    event_date: str,
    entry_status: str,
    voided_by: str,
    voided_at: str,
    void_reason: str,
    supersedes: str,
    superseded_by: str,
    index: smartsheet_client.RowIndex | None = None,
) -> int:
    """CHANGE-ONLY find-or-create of one receipt event as a ledger row; return its row ID.

    - MISS → append a new row and return its id.
    - HIT → compare the incoming values against the row's current cells for the tracked data columns.
      If NOTHING changed, this is a NO-OP that returns the existing row id — so the re-projection does
      NOT churn the sheet. If ANYTHING changed (the DERIVED pair `Line Status` and
      `Line Qty Received` as later events land against the same line, or the void annotation when
      the mark is voided or a correction is linked to it), issue an `update_rows` refreshing all
      tracked cells — IN PLACE, on the same row.

    All values are pre-formatted strings; `material` is the primary label (the line's description);
    `received_by` / `voided_by` are DISPLAY names only. The six void values are required on purpose:
    a caller that forgot them would write a voided mark back as `Active`, putting a struck delivery
    back in the office's count. NEVER deletes and NEVER marks a row Removed — a signed-for delivery
    is a permanent historical event, and a VOIDED one (`entry_status == ENTRY_VOIDED`) stays as an
    annotated, grayed row (`_write_row`).

    `index` (D10) is the pass's single read of the ledger (`read_receipt_index`); the upsert keeps it
    truthful about its own writes, so an event repeated in the same pass is FOUND and updated, never
    appended twice (a duplicate row here would read as a delivery counted twice, kept forever).
    Omitted, the upsert reads the sheet itself — one full read per call, as before.
    """
    desired: dict[str, str] = {
        COL_MATERIAL: material,
        COL_KIND: kind,
        COL_QTY: qty,
        COL_UNIT: unit,
        COL_PART_NUMBER: part_number,
        COL_LINE_UUID: line_uuid,
        COL_LINE_STATUS: line_status,
        COL_LINE_QTY_EXPECTED: line_qty_expected,
        COL_LINE_QTY_RECEIVED: line_qty_received,
        COL_BOL: bol,
        COL_NOTE: note,
        COL_RECEIVED_BY: received_by,
        COL_EVENT_DATE: event_date,
        COL_ENTRY_STATUS: entry_status,
        COL_VOIDED_BY: voided_by,
        COL_VOIDED_AT: voided_at,
        COL_VOID_REASON: void_reason,
        COL_SUPERSEDES: supersedes,
        COL_SUPERSEDED_BY: superseded_by,
    }
    voided = entry_status == ENTRY_VOIDED

    if index is None:
        index = read_receipt_index(sheet_id)
    existing = index.find(event_uuid)
    if existing is None:
        cells = {**desired, COL_EVENT_UUID: event_uuid}
        try:
            [row_id] = _write_row(smartsheet_client.add_rows, sheet_id, cells, voided=voided)
        except Exception:
            # The add may have COMMITTED (no idempotency key), so the index must not offer this event
            # as "absent" again this pass — a repeat raises instead of risking a duplicate ledger row.
            index.record_add_failed(event_uuid)
            raise
        index.record_added(row_id, cells)  # a same-pass repeat of this event is now FOUND
        return row_id

    row_id = int(existing["_row_id"])
    if _row_matches(existing, desired):
        return row_id  # nothing changed — skip the needless write

    update: dict[str, Any] = {"_row_id": row_id, **desired}
    if voided:
        # The key is written back unchanged so the gray reaches EVERY cell — the whole row reads
        # struck, not just its data cells.
        update[COL_EVENT_UUID] = event_uuid
    _write_row(smartsheet_client.update_rows, sheet_id, update, voided=voided)
    # `existing` IS the index's cached row: keep it equal to the sheet so a same-pass repeat compares
    # against what was just written (exactly what the pre-D10 fresh re-read would have shown).
    existing.update(desired)
    return row_id


def _voided_formats(cells: dict[str, Any]) -> dict[str, str]:
    """The `_formats` meta for a VOIDED row: gray on every written cell (the date keeps its date
    display — see FMT_VOIDED_DATE). `_`-prefixed meta keys (`_row_id`) are not cells."""
    return {
        title: FMT_VOIDED_DATE if title == COL_EVENT_DATE else FMT_VOIDED
        for title in cells
        if not title.startswith("_")
    }


def _write_row(
    write: Callable[[int, list[dict[str, Any]]], Any],
    sheet_id: int,
    cells: dict[str, Any],
    *,
    voided: bool,
) -> Any:
    """Issue ONE `add_rows` / `update_rows` for `cells`; a VOIDED row also carries its gray.

    The gray rides the SAME write as the annotation (hours_log's `_formats` mechanism) rather than a
    second format-only call, because a cell format is invisible to `get_rows`: a separate call that
    failed could never be noticed, and change-only would never re-issue it. It is still COSMETIC —
    the annotation is the record — so it is best-effort: a write REJECTED as invalid (HTTP 400, the
    class a malformed format descriptor raises) WARNs and is re-issued plain. A 400 means the first
    write did not commit, so the re-issue cannot double-append. If the plain write is rejected too,
    the fault is the data, and it propagates to the caller's per-event fence like any other write.
    """
    if not voided:
        return write(sheet_id, [cells])
    try:
        return write(sheet_id, [{**cells, "_formats": _voided_formats(cells)}])
    except smartsheet_client.SmartsheetValidationError as exc:
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"voided-row gray format rejected on Material Receipts sheet {sheet_id}; writing the void "
            f"annotation without it (cosmetic only): {type(exc).__name__}: {exc!r}",
            error_code="material_receipts_void_format_failed",
        )
        return write(sheet_id, [cells])


def _row_matches(existing: dict[str, Any], desired: dict[str, str]) -> bool:
    """True iff every tracked data column on `existing` already equals `desired` (normalized
    str-strip compare). `Event UUID` (the key) is excluded."""
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


def read_row_cap_threshold() -> int:
    """The row-cap WARN threshold, for a caller to read ONCE per pass and hand to every
    `check_row_cap`. Never raises.

    `get_setting` is an uncached full read of ITS_Config, so reading it inside `check_row_cap` cost
    every job its own ITS_Config read on every cycle — a second full-sheet read per job beside the
    job's own index read. It falls back to `DEFAULT_ROW_CAP_WARN`, so the tripwire stays armed at
    the conservative mark rather than being skipped for the whole pass. An UNEXPECTED read error
    WARNs; a missing row, an open breaker or a non-integer value falls back silently (that is
    `_read_int_setting`'s pre-existing contract)."""
    try:
        return _read_int_setting(CFG_ROW_CAP_WARN, DEFAULT_ROW_CAP_WARN)
    except Exception as exc:  # noqa: BLE001 — advisory tripwire; never block the mirror
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            f"row-cap threshold read failed; using the default {DEFAULT_ROW_CAP_WARN} for this "
            f"pass: {exc!r}",
            error_code="material_receipts_row_cap_check_failed",
        )
        return DEFAULT_ROW_CAP_WARN


def check_row_cap(
    sheet_id: int,
    sheet_name: str,
    *,
    row_count: int | None = None,
    threshold: int | None = None,
) -> None:
    """SoR-safe row-cap monitor — the §51 "A5 row-cap watchdog" for the standing Material Receipts
    sheet.

    An append-only receipt ledger grows monotonically and FASTER than the incident one (a job with
    many lines delivered across many truckloads writes an event per load). As the sheet nears the
    Smartsheet per-sheet row cap (~20k), WARN (`ITS_Errors`) + enqueue an operator period-split to
    `ITS_Review_Queue`. **NEVER deletes** (§51 — a signed-for delivery is a permanent record).
    ADVISORY: any read/enqueue failure is reduced to a WARN and never blocks the mirror.

    `row_count` may be passed by a caller that already read the sheet's rows this cycle (avoids a
    second full read); when None, a `get_rows` count is taken. `threshold` likewise, from
    `read_row_cap_threshold` once per pass (avoids an ITS_Config read per job); when None, it is
    read here.
    """
    try:
        if threshold is None:
            threshold = _read_int_setting(CFG_ROW_CAP_WARN, DEFAULT_ROW_CAP_WARN)
        count = row_count if row_count is not None else len(smartsheet_client.get_rows(sheet_id))
        if count < threshold:
            return
        error_log.log(
            Severity.WARN, SCRIPT_NAME,
            (
                f"Material Receipts sheet {sheet_name!r} (sheet {sheet_id}) has {count} rows, at/over "
                f"the row-cap WARN threshold {threshold} (Smartsheet ~20k/sheet) — operator should "
                f"period-split it (archive this sheet, start a fresh one); NEVER delete rows."
            ),
            error_code="material_receipts_row_cap_warn",
        )
        review_queue.add(
            workstream="progress_reports",
            summary=(
                f"Material Receipts sheet {sheet_name!r} nearing the Smartsheet row cap "
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
            error_code="material_receipts_row_cap_check_failed",
        )
