"""Seed the hand-created-only ITS_Config rows: the 5 daemon-gate rows whose ABSENCE
caused the 2026-07-13 config-WARN storm, plus the 15 rows the 2026-07-23 tenant
stand-up rehearsal proved had NO seeder anywhere (VC-03 failed 15/46 on the rebuilt
tenant — every one had been hand-created over the project's life: the worker_base_url
repoint rows, the weekly/progress send mailboxes + schedule, and the portal/fieldops
runtime gates).

Each of these keys was read every daemon cycle via the #336 `resolve_and_log` startup pass,
and the MISSING row made every cycle WARN `config_row_missing` — ~1,400–4,500 ITS_Errors
rows/day across the five keys — which filled ITS_Errors to the Smartsheet 20,000-row hard
cap on 2026-07-13 (errorCode 5634 on every add_rows; see the watchdog Check O STORM-MODE
rationale block, scripts/watchdog.py). The dark-ship gate reflex (HOUSE_REFLEXES §5)
already required seeding gate rows at merge time; these five predated / escaped it.

The rows were ALREADY applied to the live ITS_Config on 2026-07-13 via an ad-hoc idempotent
script during the incident response — this migration makes the seed DURABLE in the repo
(fresh installs, sandbox rebuilds, re-runs). Every value below is the LIVE DEFAULT the
daemons were already resolving to, so seeding changes no behavior; it only silences the
per-cycle missing-row WARN.

Companion pattern: scripts/migrations/seed_config_actuator_config.py (per-row idempotency
on Setting+Workstream — get_setting is workstream-scoped, so BOTH cells must match).

What it seeds (Setting / Workstream / Value) — the 2026-07-13 storm batch:

    safety_reports.photo_screen.clamav_enabled        / safety_reports   / false
    safety_reports.compile_now_poll.polling_enabled   / safety_reports   / true
    progress_reports.compile_now_poll.polling_enabled / progress_reports / true
    progress_reports.progress_send.polling_enabled    / progress_reports / true
    progress_reports.progress_send.scheduled_send_local / progress_reports / MON 07:00

— and the 2026-07-23 rehearsal batch (values from the pre-wipe dump; every GATE row
seeds 'false' per the dark-ship posture — activation is a deliberate, visible
cell-flip, and re-activating a send-dispatch gate escalates per §44; the mirror
identity values flip to production at the CL-12 cutover sweep, caught by VC-03's
sandbox scan):

    safety_reports.portal.worker_base_url             / safety_reports   / https://safety.evergreenmirror.com
    safety_reports.portal.worker_base_url             / progress_reports / https://safety.evergreenmirror.com
    safety_reports.portal.worker_base_url             / po_materials     / https://safety.evergreenmirror.com
    safety_reports.weekly_send.from_mailbox           / safety_reports   / safety@evergreenmirror.com
    progress_reports.progress_send.from_mailbox       / progress_reports / progress@evergreenmirror.com
    safety_reports.weekly_send.scheduled_send_local   / safety_reports   / MON 07:00
    safety_reports.portal_poll.polling_enabled        / safety_reports   / false
    safety_reports.weekly_send.polling_enabled        / safety_reports   / false
    safety_reports.publish_daemon.polling_enabled     / safety_reports   / false
    progress_reports.intake_enabled                   / safety_reports   / false
    field_ops.fieldops_sync.sync_enabled              / field_ops        / false
    field_ops.fieldops_sync.hours_enabled             / field_ops        / false
    field_ops.fieldops_sync.equipment_enabled         / field_ops        / false
    field_ops.fieldops_sync.materials_enabled         / field_ops        / false
    field_ops.fieldops_sync.incidents_enabled         / field_ops        / false
    field_ops.fieldops_sync.receipts_enabled          / field_ops        / false
    field_ops.fieldops_sync.archive_enabled           / field_ops        / false

Blank-value repair: a row that EXISTS with an empty Value is a half-written
misconfiguration (seen live: seed_config_actuator_config mirrored the po_materials
worker_base_url copy from a then-absent source row) — the seeder backfills the
seeded Value into it, WARN-loud, instead of skipping.

Auth: ITS_SMARTSHEET_TOKEN from macOS Keychain (same path the runtime SDK uses).

Run from `~/its` with the venv activated:

    python3 scripts/migrations/seed_daemon_gate_config.py

Exit code 0 on success or no-op; nonzero on any error.
"""
from __future__ import annotations

import sys
from typing import Any

import requests  # type: ignore[import-untyped]

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

from shared import keychain, sheet_ids  # noqa: E402

BASE = "https://api.smartsheet.com/2.0"

_SEEDED_NOTE = (
    "Seeded 2026-07-13 at the live default — the missing row made every daemon cycle WARN "
    "config_row_missing, which filled ITS_Errors to the 20k row cap (the Check O storm-mode "
    "incident)."
)

_REHEARSAL_NOTE = (
    "Seeded by seed_daemon_gate_config.py (2026-07-23 stand-up rehearsal: this row had "
    "only ever been hand-created — VC-03 caught the gap on the rebuilt tenant). Gate rows "
    "seed dark; ITS_Config is the single source of live state."
)

CONFIG_ROWS: list[dict[str, Any]] = [
    {
        "Setting": "safety_reports.photo_screen.clamav_enabled",
        "Workstream": "safety_reports",
        "Value": "false",
        "Description": (
            "Gates the L3 ClamAV leg of the §34 photo screen "
            "(safety_reports/photo_screen.py, read via intake.CFG_PHOTO_CLAMAV): 'true' scans "
            "each RAW uploaded photo with clamd before render/Box filing; 'false' (default) "
            "skips the scan leg — L1 magic/size and L2 Pillow verify/bomb-cap/re-encode still "
            "run. Leave false until ClamAV is installed on the Mac. " + _SEEDED_NOTE
        ),
    },
    {
        "Setting": "safety_reports.compile_now_poll.polling_enabled",
        "Workstream": "safety_reports",
        "Value": "true",
        "Description": (
            "Runtime gate for the SAFETY workstream pass of the Compile Now poller "
            "(safety_reports/compile_now_poll.py): 'true' polls each Active job's current "
            "week-sheet Rollup row for the operator Compile Now checkbox and fires the shared "
            "deterministic compile inline; 'false' skips the safety pass (the checkbox goes "
            "unserviced until the Friday calendar run). " + _SEEDED_NOTE
        ),
    },
    {
        "Setting": "progress_reports.compile_now_poll.polling_enabled",
        "Workstream": "progress_reports",
        "Value": "true",
        "Description": (
            "Runtime gate for the PROGRESS workstream pass of the Compile Now poller "
            "(safety_reports/compile_now_poll.py, workstream-scoped key): 'true' polls each "
            "Active progress job's current week-sheet Rollup row for the operator Compile Now "
            "checkbox and fires the progress compile inline; 'false' skips the progress pass "
            "(the safety pass keeps running). " + _SEEDED_NOTE
        ),
    },
    {
        "Setting": "progress_reports.progress_send.polling_enabled",
        "Workstream": "progress_reports",
        "Value": "true",
        "Description": (
            "Runtime gate for the progress send-dispatch poller "
            "(progress_reports/progress_send_poll.py): 'true' polls WPR_human_review for "
            "approved rows (Send Now / Approve for Scheduled Send, F22-verified) and "
            "dispatches progress_send.send_one_row; 'false' pauses dispatch (approved rows "
            "wait, nothing sends). " + _SEEDED_NOTE
        ),
    },
    {
        "Setting": "progress_reports.progress_send.scheduled_send_local",
        "Workstream": "progress_reports",
        "Value": "MON 07:00",
        "Description": (
            "Scheduled-send window for approved WPR rows "
            "(progress_reports/progress_send_poll.py): rows checked 'Approve for Scheduled "
            "Send' dispatch on/after this local wall-clock moment each week "
            "(America/Los_Angeles), format 'DDD HH:MM'. " + _SEEDED_NOTE
        ),
    },
    # ---- 2026-07-23 rehearsal batch (see module docstring) -------------------
    {
        "Setting": "safety_reports.portal.worker_base_url",
        "Workstream": "safety_reports",
        "Value": "https://safety.evergreenmirror.com",
        "Description": (
            "Safety Portal Cloudflare Worker base URL; portal_poll GETs "
            "/api/internal/pending here. Mirror value — repointed to the production "
            "Worker at the CL-12 cutover sweep. " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "safety_reports.portal.worker_base_url",
        "Workstream": "progress_reports",
        "Value": "https://safety.evergreenmirror.com",
        "Description": (
            "Progress rollup (P6) reads the SEND-FREE Worker /api/internal/progress-rollup "
            "via this base_url (get_setting is workstream-scoped, so the progress copy is a "
            "separate row). Mirror value — CL-12 repoints. " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "safety_reports.portal.worker_base_url",
        "Workstream": "po_materials",
        "Value": "https://safety.evergreenmirror.com",
        "Description": (
            "Worker base URL for the config_actuator daemon (workstream-scoped copy). "
            "Mirror value — CL-12 repoints. " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "safety_reports.weekly_send.from_mailbox",
        "Workstream": "safety_reports",
        "Value": "safety@evergreenmirror.com",
        "Description": (
            "Graph send-from mailbox for the weekly safety-report email (weekly_send). "
            "Sandbox/mirror sender; flips to the production mailbox at the CL-12 sweep. "
            + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "progress_reports.progress_send.from_mailbox",
        "Workstream": "progress_reports",
        "Value": "progress@evergreenmirror.com",
        "Description": (
            "Graph send-from mailbox for the weekly progress-report email (progress_send). "
            "Sandbox/mirror sender; flips to the production mailbox at the CL-12 sweep. "
            + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "safety_reports.weekly_send.scheduled_send_local",
        "Workstream": "safety_reports",
        "Value": "MON 07:00",
        "Description": (
            "Scheduled-send window for 'Approve for Scheduled Send' WSR rows "
            "(weekday HH:MM Pacific). 'Send Now' rows dispatch immediately regardless. "
            + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "safety_reports.portal_poll.polling_enabled",
        "Workstream": "safety_reports",
        "Value": "false",
        "Description": (
            "Runtime kill switch for portal_poll (true/false) — the canonical on/off gate "
            "(NOT the ITS_Daemon_Health Enabled checkbox). Pause anytime; turning ON starts "
            "the portal intake pull. " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "safety_reports.weekly_send.polling_enabled",
        "Workstream": "safety_reports",
        "Value": "false",
        "Description": (
            "Runtime kill switch for weekly_send_poll (true/false) — the WSR send-dispatch "
            "poller. Turning ON activates a send path: §44 high-class, escalate to Seth. "
            + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "safety_reports.publish_daemon.polling_enabled",
        "Workstream": "safety_reports",
        "Value": "false",
        "Description": (
            "Phase-2 form-editor publish daemon (publish_daemon.py) on/off gate. true = "
            "actuate queued publishes; false/missing = queue holds. " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "progress_reports.intake_enabled",
        "Workstream": "safety_reports",
        "Value": "false",
        "Description": (
            "P3 progress-routing gate — routes progress-category forms (daily-report) to the "
            "ITS — Progress Reporting workspace. NOTE: read under Workstream=safety_reports "
            "(intake's own workstream), a documented footgun (HOUSE_REFLEXES §5). "
            + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "field_ops.fieldops_sync.sync_enabled",
        "Workstream": "field_ops",
        "Value": "false",
        "Description": (
            "P2.5 Slice 5 — job-tracker to Smartsheet mirror daemon gate (true = ON). "
            + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "field_ops.fieldops_sync.hours_enabled",
        "Workstream": "field_ops",
        "Value": "false",
        "Description": (
            "P7/M3 — per-job Hours Log one-way-up mirror pass inside fieldops_sync "
            "(true = ON). " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "field_ops.fieldops_sync.equipment_enabled",
        "Workstream": "field_ops",
        "Value": "false",
        "Description": (
            "P7 Slice 2 — per-job Equipment Status & Location snapshot mirror pass inside "
            "fieldops_sync (true = ON). " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "field_ops.fieldops_sync.materials_enabled",
        "Workstream": "field_ops",
        "Value": "false",
        "Description": (
            "P7 M2 — per-job Material List one-way-up snapshot mirror pass in fieldops_sync "
            "(true = ON; the §51 one-way rider applies). " + _REHEARSAL_NOTE
        ),
    },
    # ---- 2026-07-23 config-parity batch (post-reload sweep: rows read by the
    # runtime but present only by hand — the fieldops row-cap thresholds were
    # WARNing config_row_missing EVERY 90s cycle, the 2026-07-13 storm class) --
    {
        "Setting": "progress_reports.hours_log.row_cap_warn_threshold",
        "Workstream": "progress_reports",
        "Value": "15000",
        "Description": (
            "Per-job Hours Log sheet row-count WARN threshold (fieldops_sync mirror pass). " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "progress_reports.equipment_status.row_cap_warn_threshold",
        "Workstream": "progress_reports",
        "Value": "15000",
        "Description": (
            "Per-job Equipment Status sheet row-count WARN threshold (fieldops_sync mirror pass). " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "progress_reports.material_list.row_cap_warn_threshold",
        "Workstream": "progress_reports",
        "Value": "15000",
        "Description": (
            "Per-job Material List sheet row-count WARN threshold (fieldops_sync mirror pass). " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "progress_reports.material_incidents.row_cap_warn_threshold",
        "Workstream": "progress_reports",
        "Value": "15000",
        "Description": (
            "Per-job Material Incidents sheet row-count WARN threshold (fieldops_sync mirror pass). " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "alerting.dedupe_window_minutes",
        "Workstream": "global",
        "Value": "60",
        "Description": (
            "Push-leg (Resend + Sentry) alert-dedupe window in minutes (shared/alert_dedupe.py). " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "smartsheet.sheet_count_ceiling",
        "Workstream": "global",
        "Value": "1500",
        "Description": (
            "Smartsheet plan sheet-count ceiling for the §51 A1 sheet_capacity margin check. " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "smartsheet.sheet_count_margin",
        "Workstream": "global",
        "Value": "50",
        "Description": (
            "Margin below the sheet-count ceiling at which the capacity check WARNs. " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "daemons.health_report_id",
        "Workstream": "global",
        "Value": "TBD",
        "Description": (
            "Placeholder for a future ITS_Daemon_Health report id (unbuilt; TBD by design). " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "mail_intake.safety.max_idle_hours",
        "Workstream": "global",
        "Value": "96",
        "Description": (
            "Legacy email-intake idle ceiling (lane retired 2026-06-05; kept for parity). " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "safety_reports.authorized_approvers",
        "Workstream": "safety_reports",
        "Value": "daniels@evergreenmirror.com,seths@evergreenmirror.com,benf@evergreenmirror.com",
        "Description": (
            "LEGACY approver list — NOT authoritative since F22 (workspace membership = approval authority); kept for parity only. " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "daemons.heartbeat_sheet_id",
        "Workstream": "global",
        # Seeded from the CURRENT sheet_ids constant, never a hardcoded id — the
        # rebuilt tenant's Daemon_Health sheet id is whatever the regen flipped.
        "Value": str(sheet_ids.SHEET_DAEMON_HEALTH),
        "Description": (
            "ITS_Daemon_Health sheet id (data copy of sheet_ids.SHEET_DAEMON_HEALTH "
            "for report/reference use). " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "field_ops.fieldops_sync.incidents_enabled",
        "Workstream": "field_ops",
        "Value": "false",
        "Description": (
            "M3 Slice 2 — the Material Incidents append-only ledger up-sync pass in "
            "fieldops_sync (true = ON; false = pass skipped). " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "field_ops.fieldops_sync.receipts_enabled",
        "Workstream": "field_ops",
        "Value": "false",
        "Description": (
            "PR4 — the Material Receipts append-only delivery-ledger up-sync pass in "
            "fieldops_sync (true = ON; false = pass skipped). Mirrors every delivery mark "
            "(delivered / partial / not delivered) into the per-job Material Receipts sheet so "
            "\"what actually showed up, and when\" is answerable outside the portal. Pause "
            "anytime by setting it back to false — the ledger re-projects in full on the next "
            "cycle, so nothing is lost while it is off. " + _REHEARSAL_NOTE
        ),
    },
    {
        "Setting": "field_ops.fieldops_sync.archive_enabled",
        "Workstream": "field_ops",
        "Value": "false",
        "Description": (
            "ROADMAP Track 6 — the job-archive pass in fieldops_sync (true = ON; false = pass "
            "skipped). Drains the portal's archive queue and RELOCATES a closed job's six "
            "containers (four Smartsheet folders, two Box) into the archive, and back on "
            "un-archive. Turning this ON is what makes the portal's Archive button move folders; "
            "while it is off a press records intent that nothing acts on. Pause anytime by "
            "setting it back to false — in-flight jobs stay queued and resume. " + _REHEARSAL_NOTE
        ),
    },
]


def _headers() -> dict[str, str]:
    token = keychain.get_secret("ITS_SMARTSHEET_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _post_json(path: str, body: Any) -> dict[str, Any]:
    r = requests.post(BASE + path, headers=_headers(), json=body)
    r.raise_for_status()
    json_body: dict[str, Any] = r.json()
    return json_body


def _get_json(path: str) -> dict[str, Any]:
    r = requests.get(BASE + path, headers=_headers())
    r.raise_for_status()
    json_body: dict[str, Any] = r.json()
    return json_body


def _find_config_row(
    rows: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    setting: str,
    workstream: str,
) -> dict[str, Any] | None:
    col_id_by_title = {c["title"]: c["id"] for c in columns}
    setting_col = col_id_by_title["Setting"]
    workstream_col = col_id_by_title["Workstream"]
    for row in rows:
        s = w = None
        for cell in row.get("cells", []):
            if cell.get("columnId") == setting_col:
                s = cell.get("value")
            elif cell.get("columnId") == workstream_col:
                w = cell.get("value")
        if s == setting and w == workstream:
            return row
    return None


def _row_value(row: dict[str, Any], columns: list[dict[str, Any]]) -> Any:
    value_col = next(c["id"] for c in columns if c["title"] == "Value")
    for cell in row.get("cells", []):
        if cell.get("columnId") == value_col:
            return cell.get("value")
    return None


def _put_json(path: str, body: Any) -> dict[str, Any]:
    r = requests.put(BASE + path, headers=_headers(), json=body)
    r.raise_for_status()
    json_body: dict[str, Any] = r.json()
    return json_body


def seed_config_rows() -> list[tuple[str, str]]:
    """Seed every row in CONFIG_ROWS. Idempotent per row (Setting+Workstream match).

    Returns: list of (setting, status) tuples — "created", "exists", or
    "backfilled" (existing row with a blank Value repaired).
    """
    sheet = _get_json(f"/sheets/{sheet_ids.SHEET_CONFIG}?include=columns")
    columns = sheet["columns"]
    rows = sheet["rows"]
    col_id_by_title = {c["title"]: c["id"] for c in columns}

    results: list[tuple[str, str]] = []
    for row_spec in CONFIG_ROWS:
        existing = _find_config_row(
            rows, columns, row_spec["Setting"], row_spec["Workstream"]
        )
        if existing is not None:
            current = _row_value(existing, columns)
            if current is None or str(current).strip() == "":
                # Blank-value repair (see module docstring): an existing row with an
                # EMPTY Value is a half-written misconfiguration, not a seed to respect.
                value_col = next(c["id"] for c in columns if c["title"] == "Value")
                _put_json(
                    f"/sheets/{sheet_ids.SHEET_CONFIG}/rows",
                    [{"id": existing["id"],
                      "cells": [{"columnId": value_col, "value": row_spec["Value"]}]}],
                )
                print(
                    f"[WARN] blank_value_backfilled: Setting={row_spec['Setting']!r} "
                    f"Workstream={row_spec['Workstream']!r} existed with an EMPTY Value — "
                    f"backfilled {row_spec['Value']!r}."
                )
                results.append((row_spec["Setting"], "backfilled"))
                continue
            print(
                f"[skip] ITS_Config row Setting={row_spec['Setting']!r} "
                f"Workstream={row_spec['Workstream']!r} already present."
            )
            results.append((row_spec["Setting"], "exists"))
            continue

        cells = []
        for title, value in row_spec.items():
            if title in col_id_by_title:
                cells.append({"columnId": col_id_by_title[title], "value": value})
        payload = [{"toBottom": True, "cells": cells}]
        result = _post_json(f"/sheets/{sheet_ids.SHEET_CONFIG}/rows", payload)
        new_id = result["result"][0]["id"]
        print(
            f"[ok] Seeded ITS_Config row id={new_id}: "
            f"Setting={row_spec['Setting']!r} Value={row_spec['Value']!r}"
        )
        results.append((row_spec["Setting"], "created"))
    return results


def main() -> int:
    print(f"[info] ITS_Config sheet = {sheet_ids.SHEET_CONFIG}")
    print(
        f"[info] Seeding {len(CONFIG_ROWS)} hand-created-only config rows "
        "(2026-07-13 storm batch + 2026-07-23 rehearsal batch; idempotent — "
        "expect [skip] on an already-seeded tenant, [WARN] backfill on blank values)"
    )
    print()

    row_results = seed_config_rows()

    print()
    print("Summary:")
    for setting, status in row_results:
        print(f"  {setting}: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
