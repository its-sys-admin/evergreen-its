---
type: reference
date: 2026-07-14
status: active
workstream: null
tags: [documentation-corpus, tier-1]
---

# ITS Data Model Reference

## Purpose

<!-- src: CLAUDE.md (Architectural model + What's stubbed vs. real) | verified 2026-07-14 -->
This is the operator-facing catalog of **every place ITS stores data**. ITS reads and writes
three stores, each with a distinct role, and this document names the sheets, tables, folders,
columns, caps, and rotation policies an operator needs to reason about them. It is a reference,
not a runbook — for *what a daemon does* see `daemon_reference.md`; for *how the pieces connect*
see `system_architecture.md`.

<!-- src: CLAUDE.md "Customer systems of record… unchanged by ITS"; sheet_ids.py:1-17 | verified 2026-07-14 -->
The three stores are:

| Store | Role | Owner |
|-------|------|-------|
| **Smartsheet** | Operator-visible **structured system-of-record** ITS owns, plus mirrors of the D1 live data | ITS (its own operational sheets; the customer's SoR is untouched) |
| **Cloudflare D1** (SQLite) | The **live portal store** — the send-free queue + field-ops working data the Worker reads/writes | Safety Portal Worker + the Mac pull daemons |
| **Box** | The **document system-of-record** — every filed PDF and photo | ITS Box OAuth user account |

<!-- src: CLAUDE.md "ITS *does* own and write its own operational Smartsheet sheets under Op Stds §51" | verified 2026-07-14 -->
The customer's own systems of record (their Smartsheet, Box, Outlook tenants) are **unchanged**
by ITS. The stores catalogued here are ITS-owned operational stores, blessed by Op Stds §51
(ITS-owned structured-SoR write-back).

---

## Store 1 — Smartsheet

### Platform hard caps (state them honestly)

<!-- src: shared/defaults.py:93-133; docs/session_logs/2026-07-13_row-cap-incident-*.md:25 | verified 2026-07-14 -->
Smartsheet enforces **abrupt, non-negotiable per-sheet caps**. There is no graceful degradation
— once a limit is hit, the write **fails outright** and the record is lost. Design every ITS
sheet to stay well under these:

| Cap | Value | Failure mode |
|-----|-------|--------------|
| Cells per sheet | **500,000** | `add_rows` fails at the ceiling; the forensic record is lost |
| Rows per sheet (at ITS widths) | **~20,000** (live-verified `SHEET_ROW_HARD_CAP`) | the row-bound wall at ITS column widths — codified as the `SHEET_ROW_HARD_CAP` constant, not a figure ITS derives at runtime (the ~500,000-cell ÷ column-width relationship is why it lands near 20,000) |
| Columns per sheet | **~400** | new column rejected at the ceiling |
| API request rate | **~300 requests / minute** | HTTP 429; `smartsheet_client` backs off + the circuit breaker trips |

<!-- src: shared/defaults.py:105-119 | verified 2026-07-14 -->
`SHEET_ROW_HARD_CAP = 20_000` in `shared/defaults.py` is the **live-verified** row cap at current
sheet widths (NOT the eval's earlier 5,000 assumption). Past it, `add_rows` fails and watchdog
Check B (open-CRITICAL scan) goes blind because it cannot write. The row-cap rotation policy
(below) exists specifically to keep the two highest-churn sheets away from this wall.

<!-- src: shared/defaults.py:78-91 | verified 2026-07-14 -->
There is also a **per-workspace sheet-count** tripwire, `SHEET_COUNT_CEILING = 1500` (margin
`SHEET_COUNT_MARGIN = 50`). The real per-plan cap is NOT exposed by the Smartsheet API, so this
is a conservative **runaway tripwire**, not the true limit. Every runtime find-or-create of a new
week/period/job sheet runs `sheet_capacity.check_create_headroom` first; a margin breach WARNs and
routes a signal to `ITS_Review_Queue` but the create **still proceeds** (advisory posture).
Evergreen is on a Business/Enterprise plan so capacity is non-limiting today.

### Workspaces

<!-- src: shared/sheet_ids.py:20-43, 188-217 | verified 2026-07-14 -->
ITS organizes its Smartsheet data into these workspaces. The four newest — Safety Portal, Progress
Reporting, Purchase Orders, Subcontracts — are **standalone** and sit outside the §23
audience-separation model; for them, **workspace membership = approval authority** (§46), i.e. the
share list *is* the set of approvers the F22 send gate verifies.

| Workspace constant | Name | Role |
|--------------------|------|------|
| `WORKSPACE_DEMO` | Forefront Portfolio — ITS Demo | Customer-facing portfolio (projects / field reports) |
| `WORKSPACE_SYSTEM` | ITS — System | Operator-only config, logs, queues, daemon health |
| `WORKSPACE_HUMAN_REVIEW` | ITS — Human Review | Evergreen-facing review surfaces (legacy + personnel) |
| `WORKSPACE_OPERATIONS` | ITS — Operations | Master databases (equipment; legacy vendor/sub stubs) |
| `WORKSPACE_ARCHIVE` | ITS — Archive | Closed Projects (the §51 archive-on-closure target) |
| `WORKSPACE_SAFETY_PORTAL` | ITS –– Safety Portal | Safety inputs + the weekly review/approve/send surface |
| `WORKSPACE_PROGRESS_REPORTING` | ITS — Progress Reporting | Progress twin of Safety Portal |
| `WORKSPACE_PURCHASE_ORDERS` | ITS — Purchase Orders | PO vendors, ledger, review surface |
| `WORKSPACE_SUBCONTRACTS` | ITS — Subcontracts | Subcontractor party registry, ledger, review surface |

### Sheet catalog

<!-- src: shared/sheet_ids.py:102-222 (static sheet constants, incl. SHEET_ESTIMATE_LOG/SHEET_RFQ_LOG/SHEET_RFQ_PENDING_REVIEW at :204-206) | verified 2026-07-19 -->
The static, pre-wired sheets ITS reads/writes. IDs are internal identifiers (already committed in
`shared/sheet_ids.py`), not secrets; prefer the **name** in prose.

| Sheet | Workspace / folder | Purpose | Owner workstream |
|-------|--------------------|---------|------------------|
| **ITS_Config** | System / 01 — Config | Runtime config rows (`<workstream>.<key>` + Workstream cell). The single tunable surface | all |
| **Picklist_Sync_Config** | System / 01 — Config | Mapping config for `picklist_sync` (master DB → target picklists) | picklist_sync |
| **ITS_Trusted_Contacts** | System / 01 — Config | Sender allowlist (Invariant 2 Layer 1). **ID = 0 = not yet built** | email triage |
| **ITS_Project_Routing** | System / 01 — Config | Project → Box-folder routing (seeded from `BOX_PROJECT_FOLDERS`) | safety |
| **ITS_Errors** | System / 02 — Logs | Per-occurrence error record (see decorator `@its_error_log`) | all |
| **ITS_Quarantine** | System / 02 — Logs | Non-allowlisted / malicious inbound audit record | email triage |
| **ITS_Review_Queue** | System / 03 — Queues | Low-confidence / flagged items routed to human review | all |
| **ITS_Daemon_Health** | System / 04 — Daemons | One row per polling daemon, heartbeat-updated per cycle | all daemons |
| **ITS_Time_Off** | Human Review / 06 — Personnel | PTO source for the reviewer-chain scheduler | scheduling |
| **WPR_Pending_Review** | Human Review / 01 — Safety Reports | **DECOMMISSIONED** — superseded by WSR_human_review | (retired) |
| **Equipment Master** | Operations / Master DBs | Equipment picklist master | picklist_sync |
| **Vendor DB** / **Subcontractor DB** | Operations / Master DBs | **Legacy stubs, retired-in-place** — superseded by ITS_Vendors / ITS_Subcontractors | (retired) |
| **ITS_Active_Jobs** | Safety Portal | The safety job registry (dropdown source + TO/CC at send) | safety |
| **ITS_Forms_Catalog** | Safety Portal | Form-definition catalog | safety |
| **WSR_human_review** | Safety Portal | Weekly Safety Report review / approve / send surface | safety |
| **Orphaned Reports** | Safety Portal | Portal submissions whose job is not-found / inactive | safety |
| **ITS_Active_Jobs_Progress** | Progress Reporting / Control | Progress twin of ITS_Active_Jobs (own contact columns + Portal Job Key) | progress |
| **WPR_human_review** | Progress Reporting / Control | Weekly Progress Report review / approve / send surface | progress |
| **ITS_Vendors** | Purchase Orders / Control | **Sole** vendor system-of-record (bridge key `VEN-######`) | po_materials |
| **PO_Log** | Purchase Orders / Control | Operator-visible ledger **mirror** of the D1 `purchase_orders` store | po_materials |
| **PO_Pending_Review** | Purchase Orders / Control | PO review / approve / send surface (WSR schema twin) | po_materials |
| **Estimate_Log** | Purchase Orders / Control | Vendor-estimate importer ledger — one row per uploaded estimate (ADR-0004) | po_materials |
| **RFQ_Log** | Purchase Orders / Control | Outbound-RFQ ledger, one row per (rfq, vendor) (ADR-0004) | po_materials |
| **RFQ_Pending_Review** | Purchase Orders / Control | RFQ review / approve / send surface (PO_Pending_Review schema twin); rows tagged `po_materials_rfq` | po_materials |
| **ITS_Subcontractors** | Subcontracts / Control | Subcontractor party SoR (bridge key `SUB-######`) | subcontracts |
| **Subcontract_Log** | Subcontracts / Control | Ledger mirror of the D1 `subcontracts` store | subcontracts |
| **Subcontract_Pending_Review** | Subcontracts / Control | Subcontract review / approve / send surface | subcontracts |

<!-- src: shared/sheet_ids.py:170-217 (FLIP precedes SEED) | verified 2026-07-14 -->
A placeholder ID of `0` means the sheet's builder migration has not run yet; the operator flips the
real ID into `sheet_ids.py` after the builder prints it (the "FLIP precedes SEED" rule — the seeder
refuses to run against a `0`).

### Dynamically-created (find-or-create) sheets and folders

<!-- src: safety_reports/week_folder.py:1-49, 99-168 | verified 2026-07-14 -->
Not every sheet is pre-wired. Some are created at runtime by find-or-create helpers:

- **Per-week Field Reports scaffold** (`safety_reports/week_folder.py`). Under each project's
  Smartsheet Field Reports subfolder, a `Week of YYYY-MM-DD` folder (Monday ISO date) holds exactly
  two sheets: `Daily Reports — Week of YYYY-MM-DD` and `Weekly Rollup — Week of YYYY-MM-DD`. Both are
  **structure-only clones** (`include=[]`) of the Bradley 1 / Week of 2026-03-09 template sheets
  (`TEMPLATE_DAILY_REPORTS_SHEET_ID`, `TEMPLATE_WEEKLY_ROLLUP_SHEET_ID`). Idempotent; a concurrent-
  creation race WARNs and adopts the first match (duplicate cleanup is operator-manual). *(Note: this
  is a **Smartsheet** scaffold, not Box.)*

<!-- src: shared/job_sheet.py:1-63, 182-284 | verified 2026-07-14 -->
- **Per-job tracking sheets** (`shared/job_sheet.py`, "Feature A"). Every job that files a subcontract
  or PO gets its own folder (named by `safety_naming.job_folder_name` — the **same** sanitized name as
  the per-job Box folder) under the workspace's "Jobs" parent (`FOLDER_SC_JOBS` / `FOLDER_PO_JOBS`),
  holding one tracking sheet **structure-cloned from the flat Log itself** (`SHEET_SUBCONTRACT_LOG` /
  `SHEET_PO_LOG`) so the columns match byte-for-byte. Best-effort and supplementary: the flat Log +
  Box stay the ledger SoR; a per-job miss is permanent (no auto-retry) but never fails the filing. The
  create branch runs the §51 A1 margin-check (advisory) and a bounded 5×~2s readiness probe to absorb
  Smartsheet's create→read 404 (errorCode 1006) propagation window.

<!-- src: CLAUDE.md (progress_reports row); shared/defaults.py comment | verified 2026-07-14 -->
- **Per-job progress standing trackers** — `Hours Log`, `Material List`, `Equipment Status`,
  `Material Incidents` — are runtime find-or-create per-job sheets in the Progress Reporting
  workspace, driven by the `field_ops.fieldops_sync` mirror passes (one-way-up, §51). Sheets stay
  **weekly** (match-period-to-cadence).

### Key column schemas

<!-- src: shared/sheet_ids.py:113-138 | verified 2026-07-14 -->
**ITS_Daemon_Health** — 12 columns (`DAEMON_HEALTH_COLUMNS`). The operator-visibility surface for
all polling daemons; each daemon updates its row in place per cycle. Column IDs are pinned in code
because they are stable across UI renames.

| Column | Notes |
|--------|-------|
| daemon_name / workstream | identity |
| enabled | report-filter metadata only — **NOT** the runtime gate (that is `<ws>.<daemon>.polling_enabled` in ITS_Config) |
| interval_seconds / source_id | cadence + source |
| last_heartbeat / last_cycle_status / last_cycle_items_processed | current run state |
| total_cycles | **lifetime monotonic** (title reads "Total Cycles Today"; semantics are lifetime, ARCH-3) |
| last_error_summary / last_error_correlation_id / notes | last-error context |

<!-- src: shared/error_log.py:138-147; shared/errors_rotation.py:13-34 | verified 2026-07-14 -->
**ITS_Errors** — written by the `@its_error_log` decorator on every unhandled exception. Columns:
`Error` (the error code), `Timestamp` (date), `Severity` (INFO / WARN / ERROR / CRITICAL), `Script`,
`Message`, `Traceback`, `Correlation_ID`, and `Resolved At`. A CRITICAL is **terminal only once
`Resolved At` is stamped**; an open CRITICAL (blank `Resolved At`) is NEVER deletable — it is the
"am I on fire" working set. Message + Traceback pass through `shared/redact.py` on the egress legs
(§54 backstop).

<!-- src: shared/review_queue.py:197-207 | verified 2026-07-14 -->
**ITS_Review_Queue** — the below-threshold / flagged item queue. Columns: `Item ID`
(`<workstream>-<YYYYMMDD>-<HHMMSS>` UTC), `Created At`, `Workstream`, `Summary`, `Reason` (PICKLIST,
`ReviewReason` enum), `Severity`, `SLA Tier`, `Source File`, `Payload` (compact JSON), `Status`
(PENDING / IN_REVIEW / APPROVED / REJECTED / ESCALATED — the full `ReviewStatus` StrEnum), `Security Flag`, plus `Assigned To` / `Resolved By` /
`Resolved At` / `Resolution Notes`. The picklist catch-all workstream is `global`.

<!-- src: safety_reports/wsr_review.py:33-65; po_materials/po_review.py:42-56 | verified 2026-07-14 -->
**WSR_human_review** (and its schema twins **PO_Pending_Review** / **Subcontract_Pending_Review**,
which reuse the same `COL_*` constants under "parameterize, not clone", §14). Columns:

| Column | Type / role |
|--------|-------------|
| Job / Project · Job ID | primary + the join key to ITS_Active_Jobs (recipients resolved at send) |
| Week Of | DATE (the Saturday) |
| Compiled PDF | Box link to the compiled packet |
| Email Body | editable — the **source of truth** for the send |
| Recipient TO · CC | display only (authoritative recipients come from ITS_Active_Jobs) |
| Approve for Scheduled Send · Send Now | CHECKBOXes — the F22 gate columns |
| Approved By · Approved At | stamped by the verified approver |
| Send Status | PICKLIST (PENDING / SENDING / SENT / FAILED / HELD) |
| Sent At · Notes | send timestamp + retry-state (Notes-encoded, §19) |
| Workstream | PICKLIST — cross-workstream send guard (a row tagged ≠ the daemon's own is HARD-HELD) |

<!-- src: shared/active_jobs.py:68-131, 202-220 | verified 2026-07-14 -->
**ITS_Active_Jobs** (and its progress twin **ITS_Active_Jobs_Progress**). Columns include `Job ID`,
`Project Name`, `Address`, `Stakeholder Name/Email/Phone`, contact columns (`Safety Reports Contact
Name/Email` on the safety sheet; `Progress Reports Contact Name/Email` on the progress sheet),
`CC 1`–`CC 5` (flattened + de-duped for the send CC list), `Active` (deny-by-default: only "Active"
files/sends), and `Portal Job Key` (the mirror daemon's find-or-create key). These are
operator-typed cells; TO/CC addresses are **unverified operator input** — the send gate, not an
allowlist, is the safety boundary.

### Row-cap rotation (watchdog Check O + storm mode)

<!-- src: shared/defaults.py:93-133; scripts/watchdog.py _check_row_cap_rotation | verified 2026-07-14 -->
Two sheets churn fastest — `ITS_Errors` and `ITS_Review_Queue` — so watchdog **Check O**
(`_check_row_cap_rotation`) keeps them off the 20,000-row wall. The policy:

| Threshold | Constant | Action |
|-----------|----------|--------|
| 15,000 rows | `SHEET_ROW_WARN_THRESHOLD` | WARN — sheet approaching the cap |
| 16,000 rows | `SHEET_ROW_ROTATE_THRESHOLD` | begin rotation |
| retention | `SHEET_ROW_ROTATION_RETENTION_DAYS = 90` | delete **terminal** rows older than 90 days, oldest first |
| delete batch | `SHEET_ROW_ROTATION_DELETE_BATCH = 200` | 200 IDs/call (450 failed live — URL-length HTTP 400) |
| per-run cap | `SHEET_ROW_ROTATION_MAX_BATCHES_PER_RUN = 23` | ≈ 4,600 rows/run; next run re-counts and continues |
| storm floor | `SHEET_ROW_STORM_FLOOR_DAYS = 2` | fallback when 90d yields nothing on an over-cap sheet |

<!-- src: shared/defaults.py:120-133 | verified 2026-07-14 -->
The **storm floor** exists because of the 2026-07-13 incident: the system was only ~8 weeks old, so
the 90-day retention window exceeded the sheet's entire age — nothing was age-eligible, rotation was
structurally dead, and a config-WARN storm filled `ITS_Errors` to the 20,000 cap. When a 90-day pass
yields **zero** eligible rows on an over-the-rotate-mark sheet, rotation re-selects at a 2-day floor
instead. **Invariants at every floor:** open CRITICALs, un-drained queue rows, and rows with
unprovable dates are NEVER deleted. Rotation proceeds during MAINTENANCE (only the operator *page*
is deferred).

```
ITS_Errors / ITS_Review_Queue row-count zones
0 ............ 15,000 ...... 16,000 .............. 20,000
   healthy    | WARN       | ROTATE (90d, then    | HARD CAP
              |            |  2d storm floor)      | writes FAIL
```

---

## Store 2 — Cloudflare D1

<!-- src: safety_portal/migrations/0003_create_portal_tables.sql; CLAUDE.md safety_portal row | verified 2026-07-14 -->
D1 is the Safety Portal Worker's SQLite database (`its-safety-portal-db`). It is the **live portal
store** — the send-free submission queue plus all field-ops working data. Schema is defined by the
numbered migrations in `safety_portal/migrations/` (0001–0053). SQLite has no native datetime type,
so every timestamp is `INTEGER` **unix epoch seconds** (default `(unixepoch())`).

<!-- src: safety_portal/migrations/0003_...:1-9; 0010_...:25-28 | verified 2026-07-14 -->
**Apply-before-deploy is the standing rule.** Every migration header carries an *order dependency*:
apply the migration to live D1 **before** the Worker that reads/writes the new column/table deploys,
or the SELECT/INSERT errors (fail-closed). Always `git pull ~/its` to latest `main` **before**
`wrangler d1 migrations apply` — a stale checkout reports "No migrations to apply" while the live
Worker expects the new tables (the lockout class, forensic #2).

### Auth, roles, and audit

<!-- src: 0001_create_users.sql; 0007_add_user_role_and_audit_log.sql; 0013_add_roles_capabilities.sql; 0006/0009 | verified 2026-07-14 -->
| Table | Key columns | Purpose / owner |
|-------|-------------|-----------------|
| `users` | id, username (UNIQUE), password_hash (bcrypt cost 10), disabled, role (FK → roles), session_epoch | Portal accounts; `requireSession` reads role+disabled per request (fail-closed) |
<!-- src: safety_portal/migrations/0013_add_roles_capabilities.sql; 0023_manager_role.sql:36-37 (manager seed) | verified 2026-07-15 -->
| `roles` | key (PK), label, is_system | Role vocabulary — three seeded: `submitter` (field PM), `manager` (crew-lead mid-tier, added 0023), `admin` (office) |
<!-- src: distinct `cap.*` keys across safety_portal/migrations/*.sql (grep-verified) | verified 2026-07-15 -->
| `capabilities` | key (PK), label, description | The `cap.*` vocabulary — **26 distinct capability keys** across the applied migrations (18 seeded at `0013`, the rest added by `0016`/`0019`/`0023`/`0025`/`0026`/`0027`/`0030`/`0031`/`0039`/`0044`/`0051`) |
| `role_capabilities` | (role_key, capability_key) | Grant junction; unknown/empty role → **no** capabilities (fail-safe) |
| `audit_log` | actor_username, action, target_username, detail | Append-only admin security event stream; never transmitted (Invariant 1) |

### Safety submissions + PDF cache

<!-- src: 0003_...:14-23; 0005_add_submission_transport.sql; 0008_add_submission_attribution.sql; 0011_add_pdf_request_cache.sql; 0012_create_pdf_requests.sql | verified 2026-07-14 -->
| Table | Key columns | Purpose / owner |
|-------|-------------|-----------------|
| `submissions` | submission_uuid (PK), job_id, form_code, work_date, payload_json, amends_uuid, **hmac**, box_verified, filed_at, box_link, actor_username, submitted_as, pdf_requested, box_file_id, pdf_ready_at | The pull-model queue + amend-prefill cache. `portal_poll` drains oldest-unfiled; `intake` files then posts the mark-filed receipt (`box_verified=1`) |
| `filed_pdfs` | (submission_uuid, chunk_index) PK, chunk_total, chunk_b64 | Base64 chunks (≤1 MB decoded, ≤8) of ONE filed PDF, re-downloaded from Box on request; transient cache, pruned 24h past ready |
| `pdf_requests` | (submission_uuid, account) PK, requested_at, ready_at | **Requester-bound** download grants (24h); a different account → 404 |

<!-- src: 0005_...:5-9 (canonical HMAC payload) | verified 2026-07-14 -->
The canonical HMAC payload the Worker signs at `/api/submit` and `portal_poll` re-verifies is
`submission_uuid \n job_id \n form_code \n work_date \n payload_json`. A row whose HMAC does not
verify is **rejected and flagged, never filed** (downgrade defense).

### Send-free actuation queues

<!-- src: 0010_create_publish_requests.sql; 0045_create_config_requests.sql | verified 2026-07-14 -->
Both mirror the External Send Gate: the Worker **validates + enqueues** send-free; a privileged Mac
daemon is the **sole actuator** that commits/deploys.

| Table | Key columns | Purpose / owner |
|-------|-------------|-----------------|
| `publish_requests` | op, parent_form_code, identity, target_form_code, definition_json, status, lease_owner/at | Form-editor publish queue (state machine: queued→validated→tested→merged→live→archived / failed) |
| `config_requests` | workstream, artifact_key, op (edit / add_version), target_version, payload, status, lease_owner/at | §50 versioned-config editor queue (purchaser / tax / terms). Same state machine |

### Jobs + field-ops core

<!-- src: 0003_...:6-10; 0014_urs_core_tables.sql; 0017_jobs_origin_fence.sql; 0021_jobs_sor_fields.sql; 0022_job_counter.sql | verified 2026-07-14 -->
| Table | Key columns | Purpose / owner |
|-------|-------------|-----------------|
| `jobs` | job_id (TEXT PK), project_name, active, status, address, stakeholder_*, safety_contact_*, safety_cc (JSON), progress_contact_*, progress_cc (JSON), lifecycle, **origin**, sync_state, canonical_job_id, mirror_version + per-sheet watermarks (safety/progress), safety_row_id/progress_row_id | The portal-authoritative job registry. `origin='portal'` is **permanently fenced** from the down-sync; the up-sync mirrors dirty rows into BOTH Active-Jobs sheets |
| `job_counter` | id=1, last_value | Atomic `JOB-######` allocator (seed 16 — first portal job is JOB-000017) |
| `clients` | id, name, contact, phone, email | Client reference |
| `personnel` | id, name, username (nullable link to users), trade, active | Crew roster; task/report WHO fields resolve through `personnel.name` (never `users.username`) |
| `equipment` | id, name, kind, identifier, status (fmc / degraded / down), status_note, status_changed_at, status_actor | Fleet roster + denormalized readiness snapshot |
| `equipment_location` | id, equipment_id, job_id, lat/lon, label, read_at (field claim), recorded_at (server), actor_username | Append-only point-in-time location reads (NO live tracking) |
| `task_assignments` | id, job_id, personnel_id, description, status (open / in_progress / done), assigned_by | Task assignments |

<!-- src: 0021_...:19-27 (version vector) | verified 2026-07-14 -->
The `jobs` **version vector** (mirror_version vs per-sheet mirrored_version watermarks) makes the
dual-sheet mirror partial-failure-safe: a job is dirty until *both* sheets confirm, and the vector
encodes exactly which sheet is behind — a first-class self-healing state, never silent divergence.

### Integrity-bar tables (timesheet-grade, load-bearing)

<!-- src: 0015_urs_integrity_bar.sql:1-31 | verified 2026-07-14 -->
These hold data of real value (payroll/billing feed), so they enforce four rules: two distinct
timestamp classes (**record time** `created_at`/`edited_at` is server-authoritative and never bound
from client input; **event time** is a field-reported claim), dual attribution (`actor_username` +
`submitted_as`), an append-only edit chain (`amends_uuid` — an edit is a NEW row, the original is
never mutated) plus an `audit_log` row, and version-pinning of checklist content.

| Table | Key columns | Purpose |
|-------|-------------|---------|
| `time_entries` | uuid (PK), job_id, personnel_id, work_started_at/ended_at, hours, created_at/edited_at, actor_username, submitted_as, amends_uuid, task_id, **mirrored_at** | Crew hours SoR; `mirrored_at IS NULL` is the amend-correct up-sync frontier to the Hours Log |
| `inspections` | uuid (PK), job_id, equipment_id, form_code + version (pin), payload_json, performed_at | Filled machine checklists |
| `equipment_logs` | uuid (PK), equipment_id, log_type (fuel / hours / maintenance / status), value_num, status_value, detail, performed_at | Maintenance / readiness event log |

### Materials + checklist engine

<!-- src: 0019_material_catalog.sql; 0031_job_expected_materials.sql; 0030_job_daily_requirements.sql; 0039_material_list_mirror.sql; 0059_material_tracking.sql | verified 2026-08-07 -->
| Table | Key columns | Purpose |
|-------|-------------|---------|
| `material_catalog` | id, model_id, manufacturer, category, key_specs, source_files (JSON provenance), unit_cost (optional), active | Datasheet-backed material TYPE vocabulary (seeded 36 approved types); soft-delete only |
| `job_expected_materials` | id, job_id, material_id (nullable → catalog), description, qty, unit, expected_date (= expected DELIVERY), status (expected / received / incident), received_*, seq, active, **line_uuid**, unplanned, **part_number**, **category**, **expected_ship_date** | Per-job expected-materials list; `line_uuid` is the one-way-up Material List mirror key. `status` is the COARSE legacy projection — the three-way delivery state is derived from `material_receipt_events` (0059), and `incident` is an orthogonal, sticky flag |
| `material_receipt_events` | id, **event_uuid**, line_id, job_id, shipment_id (optional), kind (delivered / partial / not_delivered), qty, note, event_date, actor | **Append-only delivery ledger** — the delivery system of record. One row per mark, so a part number arriving across several loads keeps its full history. No retire path by construction, so the zero-drop class (#468) is structurally impossible |
| `material_shipments` | id, **shipment_uuid**, line_id, job_id, part_number, bol_number, carrier, qty, unit, ship_date, delivery_date, seq, source (manual / import), active | Scheduled LOADS under an expected-material line. A shipping-log row is a load, not a line — keeping loads here is also what stops the §51 Material List mirror re-projecting thousands of rows per job |
| `job_daily_requirements` | id, job_id, seq, kind (note / confirm / text / form_link), label, form_code, active | Admin-authored additive daily-form overlay, snapshotted into each submission's values |

<!-- src: 0026_checklist_engine.sql; 0036_item_photos.sql; 0040_checklist_recurrences.sql | verified 2026-07-14 -->
| Table | Key columns | Purpose |
|-------|-------------|---------|
| `checklist_templates` | id, kind (daily_default / job_override / generic_inspection / specific_inspection), job_id, title, source_form_code, active | Template headers; the effective daily checklist is a **computed** merge (default minus per-job suppressions ∪ per-job adds), never stored |
| `checklist_items` | id, template_id, seq, item_type (form_linked / manual_attest / count / inspection), label, form_code, target_count, suppresses_default_item_id | Ordered template items |
| `checklist_instances` | id, kind (daily / inspection), job_id, assignee_personnel_id, instance_date, status, rolled_up_submission_uuid + UNIQUE(kind,job,assignee,date) | Materialized per-(job, assignee, date) checklist |
| `checklist_item_states` | id, instance_id, source_item_id, item_type, status, completed_by/at, note, photo_ref, value_num | Per-instance item snapshot + completion |
| `checklist_recurrences` | id, template_id, assignee_personnel_id, job_id, cadence, anchor_date, active, last_generated_date | Recurrence definitions (cron spawns idempotent instances) |

### Photo pools (§34 Option-D, record-only)

<!-- src: 0036_item_photos.sql:11-28; 0037_daily_photo_pool.sql:16-40 | verified 2026-07-14 -->
Both pools follow the **Option-D** posture (ratified 2026-07-03): there is **NO serving route** —
no browser is ever served these bytes. **Delete-on-screen**: D1 holds the bytes only while
`status='pending'`; the Mac `photo_screen` pass NULLs `photo_json` on disposition (clean → Box
`box_file_id` set + `screened_at`; refused → bytes NULLed + CRITICAL naming the account). The SPA
renders **status only**, never an image.

| Table | Key columns | Purpose |
|-------|-------------|---------|
| `item_photos` | id, item_state_id, status (pending / clean / refused), photo_json, hmac, box_file_id, screened_at | One photo per checklist item state (partial UNIQUE enforces "one live") |
| `daily_photo_pool` | id, job_id, work_date, uploaded_by, status, photo_json, hmac, box_file_id, claimed_by_submission | Extra daily-report photos (each uploads individually; the submission carries only tiny refs) |

### Purchase Orders

<!-- src: 0042_po_vendors.sql; 0043_purchase_orders.sql; 0053_po_attachments.sql | verified 2026-07-14 -->
**Money is integer cents everywhere** — no float ever touches a money column; the Worker recomputes
all totals server-side at generate and rejects a client whose displayed totals disagree.

| Table | Key columns | Purpose |
|-------|-------------|---------|
| `po_vendors` | vendor_key (PK, `VEN-######`), vendor_name, address, contact_*, region, supply_categories (JSON), default_terms_profile, gtc_reference, active, origin, sync_state, mirror_version/mirrored_version | D1 **cache** of ITS_Vendors (bidirectional §51 sync with a dirty-row fence) |
| `po_vendor_counter` | id=1, last_value | Portal-minted vendor-key allocator (self-heals past max suffix seen) |
| `purchase_orders` | id (PK), po_uuid (UNIQUE), po_number (UNIQUE, `{job}.{phase}.{seq}.{rev}`), job_no, ship_to_*, delivery_contact_*, terms_profile_id/version, subtotal_cents, tax_mode/rate_bp/cents, shipping_cents, total_cents, status (draft→queued→pending_review→approved→sent, + superseded/canceled), hmac, box_file_id, vendor_key, draft_version | **Authoritative** D1 PO store (PO_Log mirrors it) |
| `po_line_items` | id, po_id, position, part_number, description, qty, unit, unit_cost_cents, extended_cents (server-computed), per-watt fields (watts / panels / pallets / price_per_watt_microcents) | Line items; full-replace on draft update |
| `po_attachments` | id, att_uuid (UNIQUE), po_id, filename, declared_mime, size_bytes, sha256, status (pending / claimed / filed / refused), hmac, box_file_id | Draft-time spec/drawing pool (§34 doc screener); delete-on-disposition |
| `po_attachment_chunks` | (attachment_id, chunk_index) PK, chunk_total, chunk_b64 | Base64 chunks (≤1 MB decoded) of one attachment |

<!-- src: 0043_...:29-32 (HMAC domains) | verified 2026-07-14 -->
The PO HMAC uses domain prefix `po:v1`, attachments use `po-att:v1` — distinct prefixes so a PO
signature can never replay as a submission or an attachment.

### Subcontracts

<!-- src: 0049_subcontractors.sql; 0050_subcontracts.sql | verified 2026-07-14 -->
A 1:1 structural fork of the PO trio (same integer-cents discipline), with polarity deliberately
reversed for the party registry: for `subcontractors`, **Smartsheet is authoritative** and D1
mirrors it; for `subcontracts`, **D1 is authoritative** and Subcontract_Log mirrors D1.

| Table | Key columns | Purpose |
|-------|-------------|---------|
| `subcontractors` | sub_key (PK, `SUB-######`), sub_name, contact_*, region, trades (JSON), msa_reference, coi_reference (pointer only — no gate), license_number, active, origin, sync_state, watermarks | D1 cache of ITS_Subcontractors |
| `subcontractor_counter` | id=1, last_value | Portal-minted `SUB-` allocator |
| `subcontracts` | id (PK), sc_uuid (UNIQUE), sc_number (UNIQUE), job_no, owner_entity (SPV), prime_contractor, site_*, **governing_law_state** (parameterized, default VA), sub_key, trade, exhibit_a_* , price_basis (fixed / not_to_exceed), contract_price_cents, retainage_bp (default 1000 = 10%), subtotal_cents (MUST == contract_price_cents), status (adds `executed` terminal), hmac (`sub:v1`), box_file_id | **Authoritative** D1 subcontract store |
| `sov_lines` | id, subcontract_id, position, item_number, description, qty, unit, unit_price_cents, extended_cents (server-computed) | Schedule-of-Values lines; Σ must equal the contract price |

### Operational

<!-- src: 0033_prune_meta.sql | verified 2026-07-14 -->
| Table | Key columns | Purpose |
|-------|-------------|---------|
| `prune_meta` | id=1, last_run_at, db_size_bytes, size_warn, counters_json, failed_stages_json | One-row durable heartbeat for the daily D1 prune cron; watchdog **Check V** reads it (WARN >48h stale; CRITICAL on failed stages or >6 GB) |

---

## Store 3 — Box

<!-- src: shared/box_client.py:1-51 | verified 2026-07-14 -->
Box is the **document system-of-record** — every filed PDF and screened photo. ITS authenticates via
OAuth 2.0 User Authentication (`shared/box_client.py`) as a real Box user (operator account in
sandbox; a dedicated ITS user at Phase 1.5 cutover). Audit trail and file ownership attribute to that
user. Refresh tokens **rotate on every exchange** and expire 60 days from last use — the critical
invariant is that `_store_tokens` persist the new token to Keychain synchronously, or ITS dies in
~60 days. Watchdog **Check P** WARNs at 50 days idle / CRITICAL at 58.

### Project folder topology

<!-- src: shared/defaults.py:156-176 (BOX_PROJECT_FOLDERS) | verified 2026-07-14 -->
Project folders live under the **ITS DATA** Box root (id 382010286207). Each of the six Forefront
projects (Bradley 1/2, Brimfield 1/2, Huntley, Rockford) has a project-specific clone of the
canonical **1111B** template (folder 383696567483, "Copy for new projects") — `BOX_PROJECT_FOLDERS`
maps project name → Box folder ID. The legacy 1111A-derived clones are archived (not deleted) under
`ITS DATA / 99. Legacy 1111A Clones / <Project> (legacy 1111A)` for audit reference (§14, ≥30 days).

```
ITS DATA  (Box root 382010286207)
├── Bradley 1        (1111B clone)   ── BOX_PROJECT_FOLDERS["Bradley 1"]
├── Bradley 2 …      (one clone per project)
├── ITS Safety Portal
│   └── ITS Portal Submissions
│       └── <per-job folder>            (name = safety_naming.job_folder_name)
│           └── ITS <week folder>       (compiled WSR weekly packet)
├── ITS Photos
│   └── <submission_uuid>/              (screened photo originals)
└── 99. Legacy 1111A Clones/            (archived audit reference)
```

<!-- src: safety_reports/intake.py:1543,1581,2035; portal_poll.py:132-134; box_client.py:499-514 | verified 2026-07-14 -->
ITS-created Box folders are **prefixed `ITS`** so the system's own folders are distinguishable from
the existing job/category tree (`get_or_create_folder`, race-tolerant find-or-create). Named ITS
folders in the safety flow include `ITS Safety Portal`, `ITS Portal Submissions`, and `ITS Photos`
(with a per-`submission_uuid` subfolder holding the §34-screened photo originals — the renderer
consumes only the screened set).

<!-- src: shared/box_client.py:566-583; docs/tech_debt.md:40 | verified 2026-07-14 -->
The canonical write path for job content is `canonical_job_path(customer, job_number, job_name,
year)` → `/Customer/{job_number} — {job_name}/{year}/`. This helper is still a **stub** — the exact
path pattern is an open question with the owner and has no live consumer yet (only a test references
it). Do not treat its format as final.

### Per-job / per-week Box folders

<!-- src: shared/job_sheet.py:1-13 (Box+Smartsheet line up); CLAUDE.md weekly_generate row | verified 2026-07-14 -->
Each job's per-job Box folder is named by `safety_naming.job_folder_name(job_name)` — the **same**
sanitized name the per-job Smartsheet tracking folder uses, so Box and Smartsheet line up. The
weekly compile (`weekly_generate`) files each compiled weekly packet to an `ITS`-prefixed **Box week
folder** (via `upload_bytes_or_new_version`, which preserves Box file-version history on a recompile
rather than accumulating suffixed copies).

### Documentation folder (planned — dark-gated)

<!-- src: docs_pdf/__init__.py:21-24; scripts/build_docs_pdfs.py:18-57; docs/session_logs/2026-07-14_debt-zero-and-security-scrub.md:23,49 | verified 2026-07-14 -->
The enablement-PDF pipeline (`docs_pdf/`, WS3/D2) renders branded manuals from
`docs/enablement/manifest.yaml`. Its **Box publish leg (D2-3)** is built but **ships dark**: `python
-m scripts.build_docs_pdfs --upload` uploads the rendered PDFs to a single operator-designated Box
folder identified by the ITS_Config key `docs_pdf.upload.box_folder_id`, and only when
`docs_pdf.upload.enabled` is true. First activation is an operator ceremony (seed the folder ID +
flip the gate + a live Box smoke). There is no hardcoded folder name — the folder is chosen by the
operator at activation.

---

## Cross-store identity map

<!-- src: 0022_job_counter.sql:10-15; 0017_jobs_origin_fence.sql:7-12; 0043_...:44 | verified 2026-07-14 -->
A few identifiers stitch the three stores together. Knowing these prevents the multi-surface
fan-out class of bug (a value that must be updated in N places at once).

| Identifier | Format | Lives in |
|------------|--------|----------|
| Job ID | `JOB-######` | D1 `jobs.job_id` (PK) · both ITS_Active_Jobs sheets (`Job ID` / `Portal Job Key`) · every report · Box per-job folder name |
| Submission UUID | UUID | D1 `submissions.submission_uuid` · the HMAC payload · Box `ITS Photos/<uuid>/` |
| Vendor / Sub key | `VEN-######` / `SUB-######` | D1 cache PK · ITS_Vendors / ITS_Subcontractors bridge column |
| PO / SC number | `{job_no}.{site_phase}.{supersede_seq}.{revision}` | D1 `purchase_orders.po_number` / `subcontracts.sc_number` · the ledger-mirror sheet |
| Correlation ID | opaque | Threaded across ITS_Errors, the Resend alert, and Sentry for one occurrence |

---

## Edge cases & limitations

<!-- src: shared/sheet_ids.py:106,172; job_sheet.py:45-56; week_folder.py:26-32 | verified 2026-07-14 -->
- **Placeholder `0` sheet IDs** mean an un-built sheet (ITS_Trusted_Contacts today). The seeder
  refuses to run against a `0` ("FLIP precedes SEED").
- **Find-or-create races** are possible at both the folder and sheet level (Smartsheet does not
  enforce name uniqueness). Detected duplicates WARN and the first match is adopted; cleanup is
  operator-manual (bounded blast radius: one empty orphan).
- **Per-job tracking-sheet misses are permanent** — the poll daemons append best-effort with no
  auto-retry; the flat Log + Box remain the SoR, and the manual row-copy repair is in the §43
  runbooks.
<!-- src: 0011_...:37-39; 0037_...:16-24 (delete-on-screen) | verified 2026-07-14 -->
- **D1 photo bytes are transient** — the `filed_pdfs` cache prunes 24h past ready, and the photo
  pools delete bytes on screening. Box is the permanent record; D1 never serves photo bytes back to
  a browser.
<!-- src: HOUSE_REFLEXES.md §7 (mirror-loop re-creation) | verified 2026-07-14 -->
- **"Nuke a job everywhere" is a manual 3-system op.** `purge-job` deletes D1 only; purging a portal
  job while its `ITS_Active_Jobs` row still exists lets the down-sync re-insert it. Delete the
  Smartsheet row **first**, then purge. No automated cleanup spans D1 + Smartsheet folder/week-sheets
  + Box PDFs.
<!-- src: shared/box_client.py:435-461 (list_folder pagination) | verified 2026-07-14 -->
- **Box folder listing is paginated** — `get_folder_by_path` / `_find_child_folder` list at a bounded
  page (default/1000); a target buried past that page won't resolve.

---

## Related docs

- `system_architecture.md` — how the three stores connect (Worker ↔ pull daemons ↔ APIs)
- `daemon_reference.md` — the daemons that read/write these stores + watchdog checks (C, O, P, V)
- `integration_reference.md` — the Smartsheet / Box / D1 client wrappers and their error models
- `security_trust_model.md` — Invariants 1 & 2, HMAC domains, the send gate, §34 screening
- `escalation_matrix.md` — the row-cap / storm-mode / prune-failure operator responses
- `its_config_dictionary.md` — the ITS_Config keys that tune these stores (row-cap, gates)
- `glossary.md` — term definitions (SoR, mirror, integrity bar, Option-D)
- `documentation_index.md` — the Tier-1 documentation corpus index
