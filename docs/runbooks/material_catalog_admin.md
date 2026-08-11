---
type: runbook
workstream: field_ops
capability: material_catalog (P3 Materials M1)
audience: Successor-Operator
status: skeleton
---

# Runbook — Materials Catalog admin (`material_catalog`)

> §43 successor-remediation skeleton. The polished §6a operator/user PDF guide + manifest
> registration land with P2 (the §6a enablement-doc program); this is the in-repo skeleton.

## What it is

The **Materials Catalog** is the admin-editable list of material **types** (the datasheet-backed
vocabulary the per-job Material List draws from). It is a portal surface (D1 only — no Smartsheet,
no send): the **Materials Catalog** card on the home, gated by `cap.materials.manage` (admin-only).
Reads are gated `cap.materials.receive` (field PMs can browse types when receiving). Retire is a
**soft-delete** (`active=0`) — a type is never hard-deleted, so receipts/incidents that reference a
`catalog_id` keep their target.

## Symptoms → low-class repair (Tier-2)

| Symptom | Check | Repair |
|---|---|---|
| "Materials Catalog" card missing from the home | The account lacks `cap.materials.manage` | Grant the cap (admin role already holds it); confirm the user is an admin account |
| Add / edit returns "forbidden" (403) | The acting account lost `cap.materials.manage` | Re-confirm the account's role/capabilities in Accounts |
| A retired type still appears in pickers | Soft-retire sets `active=0`; default reads exclude it | Confirm the row's `active=0`; the receive picker reads active-only — no repair needed |
| A type was retired by mistake | No "un-retire" button in the UI yet | **Escalate** (a re-activate is a code/UI change) |

## Escalate to Seth (Developer-Operator) — high-capability-class

- Any change to the catalog **schema** or a need to **hard-delete** / re-activate a type (code).
- A capability-vocabulary change (`cap.materials.*` is seeded in migration 0013 — Seth-only).
- Anything touching the migration / the seed data (`0019_material_catalog.sql`).

## Expected materials — per-job receipt list (Material receipts M1)

The **Expected materials** section on a job's detail (Job Tracker) is the per-job list of what
that job is waiting on (`job_expected_materials`, migration 0031). The office
(`cap.materials.manage`, admin) adds rows — picked from this catalog or typed free-text — with
qty/unit/expected date, edits them while still *Expected*, reorders, and removes (a soft
deactivate; history is kept). Managers and field PMs (`cap.materials.receive`) see the list
**read-only** on their own job; their receive action lives in the **daily form** (below).

### Manager receipt flow — the daily form (Material receipts M2)

The manager's action surface is the **Daily tab** (My Tasks → Daily): the daily report form
(catalog-current `daily-report`; the `expected_materials` section is value-bearing since **v7**,
which also files the day's snapshot into the PDF) carries an **Expected materials** section in
the D.13 deliveries region that renders the placed job's pending rows live. Both actions are
send-free D1 writes through the routes below (per-job ownership-scoped). Read
`safety_portal/catalog.json` for the current form code — never this file.

> **Changed 2026-08-07 (materials tracking, migration 0059).** "Confirm receipt" is no longer a
> one-shot flip. It appends to an **append-only delivery ledger** (`material_receipt_events`), so
> **a repeat is a legal second event returning 200 — not a 409.** That guard was exactly what made
> a *partial* delivery impossible to finish: one part number routinely arrives across several
> truckloads. The line's displayed state is now a ROLLUP over the ledger and `qty_received` is the
> running SUM. The fuller three-way mark (Delivered / Partially delivered / Not delivered, each
> with a note) lives on the new per-job **Materials tracking** page — see
> `docs/runbooks/job_materials.md`.

- **Confirm receipt** — flips the row *Expected → Received* (stamps who/when) **and appends a
  row to the form's "Deliveries Received" table** (`item_material` = the material,
  `condition` = "Received OK", `notes` = qty/unit), so the **filed daily PDF records the
  receipt**. The append behaves like typed work (draft-persisted; editable before submit).
- **Report a problem →** — prompts for a short **required** note, flips the row
  *Expected → Incident*, then opens the **Material Incident Report** form pre-filled with the
  material and expected qty (job/date carried automatically). The incident files as its own
  submission; the daily form shows a live "Filed ✓" indicator for it once filed.

Received/Incident rows show status pills + who/when. A job with no expected materials shows
"No expected materials for this job." — the daily form is otherwise unaffected. The
material-incident form is also normally pickable from Submit-a-Form (category: Progress).

### Incident → material line reference (M3 Slice 1)

When a manager taps **Report a problem →** the deep-linked Material Incident Report carries a
hidden reference to the exact expected-materials line that was flagged (the line's stable
`line_uuid`, a submission VALUE — NOT a visible form field). The Worker `/api/submit` validates it:
a present `line_uuid` MUST be an **active** expected-materials line of **that job**, else the
submission is refused `422 unknown_material_line` before anything is filed. An incident with **no**
line reference is a valid **unlinked** incident (e.g. filed straight from Submit-a-Form, or after a
page refresh dropped the in-memory reference). This ships **dark** on the existing
`progress_reports.intake_enabled` gate (no new switch).

### Symptoms → low-class repair (Tier-2)

| Symptom | Check | Repair |
|---|---|---|
| "Expected materials" section missing from a job's detail | The account holds neither `cap.materials.manage` nor `cap.materials.receive` | Confirm the account's role in Accounts (all three roles hold `receive`; only admin holds `manage`) |
| A manager sees "Failed to load expected materials" on a job (403 `forbidden_job` in the network tab) | Non-admins only read the job they are **placed on** (`personnel.current_job`) | Check the person's placement on the Personnel page / job crew — place them on the job (this is the designed scope, not a fault) |
| "already_actioned" (409) when **flagging an incident** | The row was already flagged by someone else | No repair — the first flag won; the row shows who and when. Since 0059 this no longer applies to **confirming a receipt**: repeats are legal and append a second ledger event |
| A manager confirms a receipt twice and BOTH are recorded | **Expected since 0059** — receipts are an append-only ledger so a partial delivery can be completed later | Not a fault. Open the job's **Materials tracking** page to see every event with its qty, note, date and who recorded it. An event cannot be deleted (append-only by design); record a correcting event, and escalate if the running total must be corrected at source |
| A row can't be edited ("not_editable", 409) | Received/incident rows are receipt **records** — content edits are locked | Expected behavior. If the record itself is wrong, escalate |
| The catalog picker fails but free-text add works | The catalog read failed (transient) | Retry; if persistent check the Materials Catalog page loads |
| The daily form shows "Couldn't load this job's expected materials" | A transient read failure — the form stays fillable (never silent) | Tap **Retry** next to the warning; if persistent, check the job's Expected materials section loads on the Job Tracker detail |
| No "Expected materials" section in the manager's daily form | The job has no expected rows → the section shows the explicit empty copy; if even THAT is absent, the read failed (see the Retry warn) or the account isn't a placed manager | Confirm placement + that the office added expected rows on the job's detail |
| "Confirm receipt" errors with "already received" | **Pre-0059 behaviour — should no longer occur** | If it is still seen, the deployed Worker predates migration 0059. Check the deploy; escalate if the deployed code is current |
| The manager cancelled the incident prompt / form but the row already shows *Incident* | The flag lands **before** the incident form files (deliberate — the D1 record carries the note either way) | File the Material Incident Report from Submit-a-Form (it prefills nothing then, but job/date/details can be re-entered); the row's note already says what's wrong |
| A valid Material Incident Report is refused **`422 unknown_material_line`** | The incident is trying to reference a specific expected-materials line, but that line is not an **active** line of the same job (it was removed/soft-deactivated, or the wrong job is selected) | Confirm the line still shows on the job's **Expected materials** list (active) and the incident's job matches. If the operator wants an **unlinked** incident, file the Material Incident Report from **Submit-a-Form** (no line reference is carried) — it files cleanly. If a genuinely active line is still refused, **escalate** |

### Escalate to Seth (Developer-Operator) — high-capability-class

- Correcting a **received/incident row's recorded facts** (stamps, status un-flip) — a data/code change.
- Anything touching migration `0031_job_expected_materials.sql` or the status model.
- Any change to the daily-report / material-incident **form definitions** (the publish pipeline,
  the daily report's `expected_materials` mount — value-bearing since v7, the material-incident
  required-content floor) — definition + legal-floor changes are code/doctrine class.
- The **incident → material line** validation itself (M3 Slice 1: the `/api/submit`
  `unknown_material_line` gate, the `line_uuid` deep-link threading) — a code change.

## Notes

- The authoritative monetary value of a material comes from the **per-job Material List line** (M2),
  not the catalog's optional `unit_cost` reference field.
- Per-job expectations live on each job's detail in the **Job Tracker** ("Expected materials");
  this catalog stays the type vocabulary those rows pick from.
- **Data loss / restore (Seth-only):** `job_expected_materials` is **D1-primary** (no
  Smartsheet/Box mirror) — the restore path is Cloudflare **D1 Time Travel** (30-day
  point-in-time restore of the whole database, `npx wrangler d1 time-travel …`). Receipt
  EVIDENCE survives outside D1 regardless (the filed daily PDF's "Deliveries Received" rows +
  filed material-incident submissions); beyond the window the blast radius is re-enterable
  admin data (the office re-keys the expected rows). See `docs/tech_debt.md` "D1-primary
  tables have no ITS-side backup" (R3-F7).
