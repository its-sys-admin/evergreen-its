---
type: reference
date: 2026-07-14
status: active
workstream: null
tags: [documentation-corpus, tier-1]
---

# ITS Daemon Reference

## Purpose

This is the permanent operator reference for every background daemon and scheduled
job that ITS runs on the MacBook. It answers, for each one: what it does, how often
it fires, where its work comes from, which `ITS_Config` switch turns it on, where its
liveness is reported, where its logs are, how it fails, and how to restart it.

<!-- src: scripts/launchd/ (20 org.solutionsmith.its.*.plist files enumerated, template.plist excluded); scripts/watchdog.py:174-262 (TRACKED_JOBS = 16 slugs); HeartbeatReporter( grep — 14 non-test constructors | verified 2026-07-19 -->
There are **20 launchd agents**, enumerated directly from `scripts/launchd/*.plist`
(not from memory; `template.plist` is the scaffold, not an agent). Sixteen of them register
with watchdog Check C for marker-staleness tracking; fourteen write an ITS_Daemon_Health
heartbeat row; the two sets overlap but are not identical (the roster table below gives the
exact split). Use the roster to jump to the daemon you care about, then read its H3 block.
(The 17th — `subcontract-send`, the SC-S4 approval poller — the 18th — `estimate-poll`, the
ADR-0004 vendor-estimate importer — the 19th — `rfq-poll`, the ADR-0004 outbound-RFQ
generation daemon — and the 20th — `rfq-send`, the ADR-0004 R3 outbound-RFQ approval poller
— were added after this doc's first cut; see their rows + sections below. The 21st — `manifest-poll`, the PR3b
materials-manifest importer — was added later still.)

If you only need one thing: **to restart a daemon**, the dashboard verb is
**kickstart** (Class-B ACT, PIN-gated); the shell fallback is
`scripts/launchd/install.sh` (`load` / `unload`) or
`launchctl kickstart -k gui/<uid>/<label>`. Full procedure is in
[Restarting a daemon](#restarting-a-daemon).

## Background — the launchd execution model

### Fresh process per cycle

<!-- src: safety_reports/portal_poll.py:24-28 (launchd schedule docstring); safety_reports/weekly_send_poll.py:65-71 | verified 2026-07-14 -->
ITS daemons are **not long-running loops**. Each interval daemon exposes a single
`*_once()` entry point (`poll_once()`, `publish_once()`, `config_once()`); its
`__main__` guard calls that function exactly once and the process **exits**. launchd
re-launches a brand-new Python process on the next cadence. This means an uncommitted
Python-source edit in the `~/its` working tree goes live on the very next cycle — the
process reads the file fresh each time.

### One-shot-per-`StartInterval` self-heal (Tier 1)

<!-- src: scripts/launchd/org.solutionsmith.its.compile-now-poll.plist:53-63 (RunAtLoad/KeepAlive comment) | verified 2026-07-14 -->
Interval daemons carry `RunAtLoad=true` and **no** `KeepAlive`. The reasoning, quoted
in the plists as "A2 (single-host resilience)":

- `RunAtLoad=true` — after a reboot or OS update, launchd loads the agent and the
  daemon **resumes immediately**, then `StartInterval` re-fires on cadence.
- `KeepAlive` is intentionally **absent** — because each daemon is one-shot-per-fire,
  `KeepAlive=true` would restart it on every clean exit and destroy the interval
  cadence. A crash simply means launchd waits for the next `StartInterval` tick.

<!-- src: scripts/launchd/org.solutionsmith.its.weekly-generate.plist:57-66 (calendar RunAtLoad=false comment) | verified 2026-07-14 -->
Calendar-driven daemons (`StartCalendarInterval`) instead carry `RunAtLoad=false`.
launchd fires a *missed* calendar job on wake, whereas `RunAtLoad=true` would mis-fire
it on every login (e.g. run a Friday compile on a Tuesday boot). Reboot-recovery for
the weekly compiles is the watchdog **Check I** catch-up, not `RunAtLoad`.

<!-- src: scripts/launchd/org.solutionsmith.its.dashboard.plist:69-77 (KeepAlive=true comment) | verified 2026-07-14 -->
The **one** exception is the operator dashboard: it is a persistent HTTP **server**,
so it carries `RunAtLoad=true` **and** `KeepAlive=true` (launchd restarts it if it
exits). It is the only ITS plist where `KeepAlive=true` is correct — a server has no
interval/calendar cadence to protect.

### Liveness — ITS_Daemon_Health + watchdog Check C

<!-- src: shared/sheet_ids.py:111 (SHEET_DAEMON_HEALTH); scripts/watchdog.py:170 (WATCHDOG_MARKER_DIR); scripts/watchdog.py:407-455 (Check C) | verified 2026-07-14 -->
Two independent liveness surfaces exist because a one-shot daemon cannot reliably
report its own death:

| Surface | Mechanism | Who writes it | Who reads it |
|---|---|---|---|
| **ITS_Daemon_Health** sheet (id `6272022823784324`, System workspace / 04 — Daemons) | One row per daemon, updated in place each cycle via `shared/heartbeat.py` `HeartbeatReporter` | The **14** daemons that construct a `HeartbeatReporter` (see roster) | Operator (obs), dashboard daemons panel, watchdog Check G |
| **Watchdog marker files** (`~/its/.watchdog/<slug>.last_run`) | ISO timestamp written each cycle | The **16** `TRACKED_JOBS` daemons | Watchdog **Check C** (marker-staleness floor) |

<!-- src: scripts/watchdog.py:408-455 (Check C body); scripts/watchdog.py:240-287 (TRACKED_JOB_WINDOWS) | verified 2026-07-14 -->
**Check C** (`_check_scheduled_jobs`) runs in every HOURLY watchdog sweep (it is on the
hourly tier — see `DAILY_ONLY_CHECKS`). For each
job in `TRACKED_JOBS` it reads `~/its/.watchdog/<slug>.last_run` and WARNs if the
marker is missing, unreadable, or older than that job's freshness window
(`TRACKED_JOB_WINDOWS`, default 24h). Because a daemon cannot detect its own total
death, Check C is the staleness floor that catches a silently-dead poller; total host
death is caught by the external **Healthchecks.io** ping (the dead-man's switch), since the
watchdog cannot alert about itself — **once armed**; while `system.heartbeat_url` holds its
seed placeholder the ping is skipped and total-host death is undetected.

### ITS_Daemon_Health schema (12 columns)

<!-- src: shared/sheet_ids.py:120-135 (DAEMON_HEALTH_COLUMNS dict) | verified 2026-07-14 -->
The heartbeat sheet has exactly these 12 columns (`DAEMON_HEALTH_COLUMNS` in
`shared/sheet_ids.py`):

| Column key | Meaning |
|---|---|
| `daemon_name` | The daemon's identity string, e.g. `safety_reports.portal_poll` (this is the row key) |
| `workstream` | Owning workstream tag |
| `enabled` | Report-filter metadata **only** — NOT the runtime gate (ARCH-1: the real gate is `<ws>.<daemon>.polling_enabled` in ITS_Config) |
| `interval_seconds` | Configured cadence |
| `source_id` | Source-of-work identifier |
| `last_heartbeat` | Timestamp of the most recent cycle |
| `last_cycle_status` | OK / ERROR / etc. for the last cycle |
| `last_cycle_items_processed` | Item count from the last cycle |
| `total_cycles` | Lifetime **monotonic** counter (ARCH-3; Smartsheet column title reads "Total Cycles Today" but semantics are lifetime, not daily-reset) |
| `last_error_summary` | Short text of the last error, if any |
| `last_error_correlation_id` | Correlation ID linking to ITS_Errors |
| `notes` | Free-form |

<!-- src: field_ops/fieldops_sync.py:551-553 (ARCH-1 note); operator_dashboard/act/daemon_ops.py:83 (is_interval_daemon) | verified 2026-07-14 -->
**ARCH-1 reminder:** the `enabled` checkbox on this sheet is display-filter metadata.
The canonical on/off switch for every daemon is its `polling_enabled` (or
`sync_enabled`) row in **ITS_Config**. Flipping the sheet checkbox does nothing at
runtime.

## Daemon roster

<!-- src: scripts/launchd/*.plist (Label, ProgramArguments, StartInterval/StartCalendarInterval); scripts/launchd/install.sh:78-90 (per-daemon interval defaults) | verified 2026-07-14 -->

| Label (`org.solutionsmith.its.…`) | Runs | Schedule | Family | Heartbeat row? | Check C slug |
|---|---|---|---|---|---|
| `portal-poll` | `safety_reports.portal_poll` | interval, **60s** default | Safety | yes | `safety_portal_poll` |
| `weekly-generate` | `safety_reports.weekly_generate` | calendar, **Fri 14:00** | Safety | no | `safety_weekly_generate` |
| `compile-now-poll` | `safety_reports.compile_now_poll` | interval, **90s** default | Safety (+Progress) | yes | `safety_compile_now_poll` |
| `weekly-send` | `safety_reports.weekly_send_poll` | interval, **900s** default | Safety | yes | `safety_weekly_send_poll` |
| `progress-generate` | `progress_reports.progress_weekly_generate` | calendar, **Fri 14:30** | Progress | no | `progress_weekly_generate` |
| `progress-send` | `progress_reports.progress_send_poll` | interval, **900s** default | Progress | yes | `progress_send_poll` |
| `po-poll` | `po_materials.po_poll` | interval, **90s** default | Purchase Orders | yes | `po_poll` |
| `po-send` | `po_materials.po_send_poll` | interval, **900s** default | Purchase Orders | yes | `po_send_poll` |
| `estimate-poll` | `po_materials.estimate_poll` | interval, **120s** default | Purchase Orders | yes | `estimate_poll` |
| `rfq-poll` | `po_materials.rfq_poll` | interval, **120s** default | Purchase Orders (RFQ) | yes | `rfq_poll` |
| `rfq-send` | `po_materials.rfq_send_poll` | interval, **900s** default | Purchase Orders (RFQ) | yes | `rfq_send_poll` |
| `subcontract-poll` | `subcontracts.subcontract_poll` | interval, **120s** default | Subcontracts | yes | `subcontract_poll` |
| `subcontract-send` | `subcontracts.subcontract_send_poll` | interval, **900s** default | Subcontracts | yes | `subcontract_send_poll` |
| `manifest-poll` | `field_ops.manifest_poll` | interval, **120s** default | Field-Ops (materials) | yes | `manifest_poll` |
| `fieldops-sync` | `field_ops.fieldops_sync` | interval, **90s** default | Field Ops | yes | `fieldops_sync` |
| `publish-daemon` | `safety_reports.publish_daemon` | interval, **120s** fixed | §50 actuator | yes | (not tracked) |
| `config-actuator` | `po_materials.config_actuator` | interval, **120s** fixed | §50 actuator | yes | (not tracked) |
| `picklist-sync` | `scripts/run_picklist_sync.py` | interval, **3600s** fixed | Schema | no | `safety_picklist_sync` |
| `picklist-audit` | `scripts/audit_picklist_drift.py` | calendar, **Sun 15:00** | Schema | no | `safety_picklist_audit` |
| `watchdog` | `scripts/watchdog.py` | interval, **3600s** (hourly tier + a ~24h daily tier) | System | no | (watches others) |
| `dashboard` | `operator_dashboard` | **server** (KeepAlive) | System | no | (KeepAlive) |

<!-- src: scripts/watchdog.py TRACKED_JOBS — 16 slugs (estimate_poll + rfq_poll + rfq_send_poll added); HeartbeatReporter( grep — 14 non-test constructors | verified 2026-07-19 -->
Note the two coverage sets do not fully overlap: **16** daemons write Check-C markers
(the interval pollers plus the four calendar/hourly jobs), and **14** daemons write an
ITS_Daemon_Health heartbeat row (the interval pollers plus the two §50 actuators). The
two actuators (`publish-daemon`, `config-actuator`) heartbeat but are **not** in Check
C; the calendar/hourly jobs (`weekly-generate`, `progress-generate`, `picklist-sync`,
`picklist-audit`) are in Check C but do **not** heartbeat; `watchdog` and `dashboard`
do neither (watchdog cannot watch itself; the dashboard's `KeepAlive` is its liveness).

Interval defaults are baked into the installed plist at `install.sh load` time from the
daemon's `poll_interval_seconds` ITS_Config row (or the per-daemon default if the row is
unreadable). A later ITS_Config change requires a **re-install** to take effect —
the running plist holds the interval.

---

## Safety Reports daemons

### portal-poll — `safety_reports.portal_poll`

<!-- src: safety_reports/portal_poll.py:1-45 (module docstring); safety_reports/portal_poll.py:91 (polling gate); scripts/launchd/install.sh:81,68 (60s default / config key) | verified 2026-07-14 -->

| Field | Value |
|---|---|
| **Purpose** | Puller half of the portal transport. Drains the Worker's send-free D1 queue: per row it recomputes the canonical HMAC and constant-time-compares it (fail = reject + security-flagged Review-Queue row, never handed to intake), then calls `intake.process_portal_submission`, then POSTs `mark-filed` as the receipt. Also runs two fenced best-effort passes: the PR-4 PDF-download cache (`_service_pdf_requests`) and the checklist item-photo §34 screen (`_service_item_photos`). |
| **Interval** | `StartInterval`, default **60s** (`safety_reports.portal_poll.poll_interval_seconds`) |
| **Source of work** | `GET /api/internal/pending` on the Cloudflare Worker (via `shared.portal_client`) |
| **Config gates** | `safety_reports.portal_poll.polling_enabled` (master); `safety_reports.portal_poll.poll_interval_seconds` (cadence); `safety_reports.photo_screen.clamav_enabled` (default **OFF**, photo screen depth) |
| **Heartbeat row** | `safety_reports.portal_poll` — marker slug `safety_portal_poll` (window 5 min) |
| **Log** | `~/its/logs/launchd/portal_poll.out.log` / `.err.log` |
| **Known failure modes** | **Fail-CLOSED**: missing bearer token / HMAC secret / Worker base URL → the cycle does not poll, it logs and halts (a silent drop is forbidden). Bad-HMAC row is one-shot-flagged (never re-spams the queue). Watchdog **Check Q** (fetch outage) and **Check R** (stuck backlog) are the second-opinion pages. Worker base URL must be repointed to the custom domain after a `custom_domain: true` deploy. |
| **Restart** | Dashboard: **kickstart** (or stop→start). Shell: `scripts/launchd/install.sh load org.solutionsmith.its.portal-poll` |

### weekly-generate — `safety_reports.weekly_generate`

<!-- src: safety_reports/weekly_generate.py:1-46 (docstring); safety_reports/weekly_generate.py:133 (@require_active); scripts/launchd/org.solutionsmith.its.weekly-generate.plist:49-56 (Fri 14:00) | verified 2026-07-14 -->

| Field | Value |
|---|---|
| **Purpose** | GENERATION half of the External Send Gate (FM Invariant 1) — zero send, zero AI. Deterministic Sat→Fri compile: gather the week sheet's per-submission PDFs → merge one branded packet → file to an `ITS`-prefixed Box week folder → dual-write a Rollup snapshot row + a **PENDING** `WSR_human_review` row. |
| **Interval** | `StartCalendarInterval` — **Friday 14:00** local (`Weekday 5`, `Hour 14`, `Minute 0`) |
| **Source of work** | `ITS_Active_Jobs`; each Active job's current Sat→Fri week sheet |
| **Config gates** | Kill switch only (`@require_active`) — no `polling_enabled`. The per-row `Compile Now` checkbox and skip-if-already-compiled-and-no-new-docs logic control what actually compiles. |
| **Heartbeat row** | None (calendar one-shot) — marker slug `safety_weekly_generate` (window **8 days**) |
| **Log** | `~/its/logs/launchd/weekly_generate.out.log` / `.err.log` |
| **Known failure modes** | Per-job fence routes a bad job-week to `ITS_Review_Queue` and continues. A missed Friday run is auto-recovered by watchdog **Check I** (`_check_weekly_generate_catchup`), the one daemon launchd cannot self-recover. |
| **Restart** | Not a persistent daemon — it fires on the calendar. Dashboard start/stop the plist; shell `install.sh load org.solutionsmith.its.weekly-generate`. Manual backfill: `python -m safety_reports.weekly_generate --week-start <YYYY-MM-DD>`. |

### compile-now-poll — `safety_reports.compile_now_poll`

<!-- src: safety_reports/compile_now_poll.py:95-140 (docstring); safety_reports/compile_now_poll.py:46-47 (dual gate); scripts/launchd/install.sh:82 (90s default) | verified 2026-07-14 -->

| Field | Value |
|---|---|
| **Purpose** | On-demand "Compile Now" poller. Rather than wait for the Friday compile, an operator who checks **Compile Now** on a week sheet's Rollup row gets a compiled packet within a minute or two. Reuses the SAME `generate_core._compile_job_week` primitive, so on-demand and scheduled compiles are byte-identical. **One** daemon serves both workstreams (safety + progress) by iterating `COMPILE_CONFIGS`. |
| **Interval** | `StartInterval`, default **90s** (`safety_reports.compile_now_poll.poll_interval_seconds`) |
| **Source of work** | Each Active job's current week-sheet Rollup row `Compile Now` trigger (per enabled workstream) |
| **Config gates** | `safety_reports.compile_now_poll.polling_enabled` (default True); `progress_reports.compile_now_poll.polling_enabled` (the progress workstream's own gate) |
| **Heartbeat row** | `safety_reports.compile_now_poll` — marker slug `safety_compile_now_poll` (window 8 min) |
| **Log** | `~/its/logs/launchd/compile_now_poll.out.log` / `.err.log` |
| **Known failure modes** | **Fail-loud**: a failed compile leaves the trigger VISIBLY set and routes the job to the Review Queue (the trigger clears only on success). Single-flight file lock prevents overlapping cycles from double-compiling. This is INTENDED to be always-loaded — on-demand compile only works while it runs. |
| **Restart** | Dashboard **kickstart**; shell `install.sh load org.solutionsmith.its.compile-now-poll` |

### weekly-send — `safety_reports.weekly_send_poll`

<!-- src: safety_reports/weekly_send_poll.py:48-90 (docstring); safety_reports/weekly_send_poll.py:63 (polling gate); scripts/launchd/install.sh:80 (900s default) | verified 2026-07-14 -->

| Field | Value |
|---|---|
| **Purpose** | SEND half of the two-process model. Discovers `WSR_human_review` rows with `Send Now` (immediate) OR `Approve for Scheduled Send` (Monday ≥07:00 Pacific batch) checked, runs the **F22** approval-attestation gate on the driving checkbox, stamps the verified approver, and dispatches each to `weekly_send.send_one_row`. The poller itself has zero send capability — it is an iterator + dispatcher; the handler is the only place `graph_client.send_mail` is called. |
| **Interval** | `StartInterval`, default **900s** (15 min) (`safety_reports.weekly_send.poll_interval_seconds`) |
| **Source of work** | `WSR_human_review` Smartsheet sheet |
| **Config gates** | `safety_reports.weekly_send.polling_enabled`; `safety_reports.weekly_send.poll_interval_seconds` |
| **Heartbeat row** | `safety_reports.weekly_send_poll` — marker slug `safety_weekly_send_poll` (window 30 min) |
| **Log** | `~/its/logs/launchd/weekly_send_poll.out.log` / `.err.log` |
| **Known failure modes** | F22 gate is **fail-closed** (empty approver set blocks all sends). Per-row fence isolates a bad row. Rows HELD on empty/unknown recipient or missing/oversized PDF. Watchdog **Check N** catches rows stuck in `SENDING`; **Check T** catches rows stuck HELD; **Check U** catches approver-set drift. |
| **Restart** | Dashboard **kickstart**; shell `install.sh load org.solutionsmith.its.weekly-send` |

---

## Progress Reporting daemons

### progress-generate — `progress_reports.progress_weekly_generate`

<!-- src: progress_reports/progress_weekly_generate.py:283-325 (docstring); progress_reports/progress_weekly_generate.py:198 (@require_active); scripts/launchd/org.solutionsmith.its.progress-generate.plist:54-61 (Fri 14:30) | verified 2026-07-14 -->

| Field | Value |
|---|---|
| **Purpose** | Progress twin of `weekly-generate` — SAME `generate_core.run_generate` engine, a different config. Iterates `ITS_Active_Jobs_Progress`, compiles each Active job's Sat→Fri week of progress-form PDFs into a Box packet, and dual-writes a Rollup snapshot row + a PENDING `WPR_human_review` row. |
| **Interval** | `StartCalendarInterval` — **Friday 14:30** local (`Weekday 5`, `Hour 14`, `Minute 30`), staggered 30 min after safety's 14:00 (both hold the host compile mutex) |
| **Source of work** | `ITS_Active_Jobs_Progress` |
| **Config gates** | Kill switch only (`@require_active`) — no `polling_enabled` |
| **Heartbeat row** | None (calendar one-shot) — marker slug `progress_weekly_generate` (window **8 days**) |
| **Log** | `~/its/logs/launchd/progress_generate.out.log` / `.err.log` |
| **Known failure modes** | Per-job timeout/memory fence → `ITS_Review_Queue`. No safety-tree fallback (`box_legacy_fallback=False`) — an unset progress Box root surfaces as a config gap, never a silent write to the safety tree. Missed Friday → watchdog **Check I** progress catch-up (`_check_progress_generate_catchup`). §43 tree: `docs/runbooks/progress_weekly_generate.md`. |
| **Restart** | Dashboard start/stop the plist; shell `install.sh load org.solutionsmith.its.progress-generate`. Missed run is operator-recovered by a manual re-run. |

### progress-send — `progress_reports.progress_send_poll`

<!-- src: progress_reports/progress_send_poll.py:236-282 (docstring); progress_reports/progress_send_poll.py:38 (polling gate); scripts/launchd/install.sh:83 (900s default) | verified 2026-07-14 -->

| Field | Value |
|---|---|
| **Purpose** | Progress twin of `weekly-send`. Discovers `WPR_human_review` rows approved (`Send Now` / `Approve for Scheduled Send`), runs the F22 gate against the **Progress Reporting** workspace, stamps the verified approver, and dispatches to `progress_send.send_one_row`. Recipients resolve only from `ITS_Active_Jobs_Progress` — never safety's set. |
| **Interval** | `StartInterval`, default **900s** (15 min) (`progress_reports.progress_send.poll_interval_seconds`) |
| **Source of work** | `WPR_human_review` Smartsheet sheet |
| **Config gates** | `progress_reports.progress_send.polling_enabled`; `progress_reports.progress_send.poll_interval_seconds` |
| **Heartbeat row** | `progress_reports.progress_send_poll` — marker slug `progress_send_poll` (window 30 min) |
| **Log** | `~/its/logs/launchd/progress_send_poll.out.log` / `.err.log` |
| **Known failure modes** | F22 fail-closed (circuit-open / auth error aborts the cycle with zero sends; empty approver set = `EMPTY_ALLOWLIST` blocks all). Per-row fence. `polling_enabled=false` short-circuits (operator pause). §43 tree: `docs/runbooks/progress_send.md`. |
| **Restart** | Dashboard **kickstart**; shell `install.sh load org.solutionsmith.its.progress-send` |

---

## Purchase Orders daemons

### po-poll — `po_materials.po_poll`

<!-- src: po_materials/po_poll.py:330-376 (docstring); po_materials/po_poll.py:142-147 (gates); scripts/launchd/install.sh:85 (90s default) | verified 2026-07-14 -->

| Field | Value |
|---|---|
| **Purpose** | PO pull daemon (the `fieldops_sync` multi-pass model — one host, one lock, one heartbeat). ① **Drafts pass**: `GET /api/po/internal/pending` → per row recompute `po:v1` HMAC + constant-time verify → totals recompute + assert vs the signed values → PO_Log collision double-check → ITS_Vendors snapshot → deterministic render → Box file → `PO_Log` append + `PO_Pending_Review` row → `mark-filed` receipt (last, so any earlier crash re-pulls). ①b **Attachment pass** (§34 doc screen). ② **Vendor down-sync**. ③ **Vendor up-sync**. |
| **Interval** | `StartInterval`, default **90s** (`po_materials.po_poll.poll_interval_seconds`) |
| **Source of work** | `GET /api/po/internal/pending` (+ `…/attachments/pending`, `…/vendors/*`) |
| **Config gates** | `po_materials.po_poll.polling_enabled` (drafts + attachment pass); `po_materials.po_poll.vendors_sync_enabled`; `po_materials.po_poll.status_sync_enabled`; `po_materials.po_attach_screen.clamav_enabled`. **All ship false (dark).** |
| **Heartbeat row** | `po_materials.po_poll` — marker slug `po_poll` (window 8 min). WARNs until loaded AND at least one gate flipped (a loaded-but-all-dark daemon writes no marker by design). |
| **Log** | `~/its/logs/launchd/po_poll.out.log` / `.err.log` |
| **Known failure modes** | A bad-HMAC or totals-mismatch row is one-shot-flagged (CRITICAL + security Review-Queue row on first sighting) and never rendered/filed/marked; the row stays queued in D1 for forensics. The whole attachment pass is fenced (`po_attachment_service_failed`) and can never block PO filing. |
| **Restart** | Dashboard **kickstart**; shell `install.sh load org.solutionsmith.its.po-poll` |

### po-send — `po_materials.po_send_poll`

<!-- src: po_materials/po_send_poll.py:377-419 (docstring); po_materials/po_send_poll.py:42 (polling gate); scripts/launchd/install.sh:86 (900s default) | verified 2026-07-14 -->

| Field | Value |
|---|---|
| **Purpose** | PO twin of the safety/progress send polls. Discovers `PO_Pending_Review` rows approved, runs the F22 gate against the **ITS — Purchase Orders** workspace (§46 — workspace membership = PO approval authority), stamps the approver, and dispatches to `po_send.send_one_row`. Recipients resolve only from `ITS_Vendors`. |
| **Interval** | `StartInterval`, default **900s** (15 min) (`po_materials.po_send.poll_interval_seconds`) |
| **Source of work** | `PO_Pending_Review` Smartsheet sheet |
| **Config gates** | `po_materials.po_send.polling_enabled`; `po_materials.po_send.poll_interval_seconds` |
| **Heartbeat row** | `po_materials.po_send_poll` — marker slug `po_send_poll` (window 30 min) |
| **Log** | `~/its/logs/launchd/po_send.out.log` / `.err.log` (note: `po_send`, not `po_send_poll`) |
| **Known failure modes** | F22 fail-closed (empty approver set = `EMPTY_ALLOWLIST` — the §46 share list of ITS — Purchase Orders must include the approvers). Per-row fence isolates a row with no parseable `po_number`. `polling_enabled=false` short-circuits. §43 tree: `docs/runbooks/po_send.md`. |
| **Restart** | Dashboard **kickstart**; shell `install.sh load org.solutionsmith.its.po-send` |

### estimate-poll — `po_materials.estimate_poll`

<!-- src: po_materials/estimate_poll.py:1-75 (docstring), :120-137 (gate/defaults); scripts/launchd/install.sh:93 (120s default); scripts/watchdog.py (estimate_poll slug, 10-min window) | verified 2026-07-19 -->

| Field | Value |
|---|---|
| **Purpose** | Vendor-estimate pull daemon (ADR-0004 E2) — the Mac half of the estimate importer. A SINGLE pass behind one gate: claim FIRST (crash recovery) → chunk pull + STRICT reassembly → `est:v1` HMAC verify + sha256/size recompute vs the SIGNED values → §34 doc screen (the SAME `po_attach_screen` as PO attachments) → deterministic doc-type gate (pdfplumber inside the killable rlimited `estimate_sandbox` child; invoice/ap_report REFUSED from the PO path, visibly) → surviving docs file the ORIGINAL bytes to Box (ROOT→job→"Purchase Orders"→"Vendor Quotes") → `Estimate_Log` row → disposition-screen page previews (Quartz via the sandbox, best-effort) → result post LAST (`needs_review` + `box_file_id`). AI-FREE (capability-gated in `GATED_SCRIPTS`). |
| **Interval** | `StartInterval`, default **120s** (`po_materials.estimate_poll.poll_interval_seconds`) |
| **Source of work** | `GET /api/po/estimates/internal/pending` on the Cloudflare Worker (via `shared.portal_client`) |
| **Config gates** | `po_materials.estimate_poll.polling_enabled` (**ships false** — dark); `po_materials.estimate_poll.max_pages_preview`; `po_materials.po_attach_screen.clamav_enabled` (SHARED with po_poll's attachment pass, default **OFF**) |
| **Heartbeat row** | `po_materials.estimate_poll` — marker slug `estimate_poll` (window 10 min). WARNs until loaded AND the gate flipped (a loaded-but-dark daemon writes no marker by design). |
| **Log** | `~/its/logs/launchd/estimate_poll.out.log` / `.err.log` |
| **Known failure modes** | **Fail-CLOSED** on missing Worker base URL / estimate bearer / HMAC secret (CRITICAL + ERROR heartbeat, nothing polled). The bearer is the **dedicated** Keychain `ITS_PORTAL_ESTIMATE_TOKEN` (privilege-separated — scopes ONLY `/api/po/estimates/internal/*`; ADR-0004 red-team #1); a **401 anywhere stops the whole cycle** (`estimate_bearer_rejected` CRITICAL — a bad/rotated token never self-heals). Integrity failures (bad HMAC / digest / chunk set) and screen/doc-type refusals are one-shot-flagged (`~/its/state/estimate_poll_flagged.json` — delete an entry to retry after fixing the cause); transient Smartsheet/Box/transport errors leave the row claimed and retried next cycle. A hostile document can never kill the daemon — every hostile parse runs in the killable `estimate_sandbox` child and a wedged parse degrades the doc, not the cycle. §43 tree: `docs/runbooks/estimate_import_path.md`. |
| **Restart** | Dashboard **kickstart**; shell `install.sh load org.solutionsmith.its.estimate-poll` |

### manifest-poll — `field_ops.manifest_poll`

<!-- src: field_ops/manifest_poll.py:1-72 (docstring), :117-141 (gates/defaults); scripts/launchd/install.sh (120s default); scripts/watchdog.py (manifest_poll slug, 10-min window) | verified 2026-08-07 -->

| Field | Value |
|---|---|
| **Purpose** | Materials-manifest import daemon (PR3b / ADR-0005) — the Mac half of the manifest pool. One pass behind one gate: claim FIRST (crash recovery) → chunk pull + STRICT reassembly → `manifest:v1` HMAC verify **and** a separate length/sha256 recompute vs the SIGNED values → §34 doc screen (the SAME `po_attach_screen` as PO attachments and estimates) → cell-grid extract inside the killable rlimited `estimate_sandbox` child (`extract_xlsx_rows` for workbooks, `parse_native` for PDFs) → `manifest_parse` → file the ORIGINAL bytes to Box (ROOT→job→"Materials"→"Manifests") → post the grid in ≤200-row pages → source-page previews (best-effort) → result post LAST (`parsed` + `box_file_id` + the PROPOSED column map). It never picks a quantity column and never commits a material line — the validate screen disposes. AI-FREE (capability-gated in `GATED_SCRIPTS`). |
| **Interval** | `StartInterval`, default **120s** (`field_ops.manifest_poll.poll_interval_seconds`) |
| **Source of work** | `GET /api/fieldops/manifests/internal/pending` on the Cloudflare Worker (via `shared.portal_client`) |
| **Config gates** | `field_ops.manifest_poll.polling_enabled` (the ONE gate; the seeded ITS_Config row from `scripts/migrations/seed_manifest_config.py` is the operator's switch — read ITS_Config for its live value); `po_materials.po_attach_screen.clamav_enabled` (SHARED with the PO/estimate pools, default **OFF**) |
| **Heartbeat row** | `field_ops.manifest_poll` — marker slug `manifest_poll` (window 10 min). WARNs until loaded AND the gate flipped (a loaded-but-dark daemon writes no marker by design). |
| **Log** | `~/its/logs/launchd/manifest_poll.out.log` / `.err.log` |
| **Known failure modes** | **Fail-CLOSED** on missing Worker base URL / manifest bearer / HMAC secret (CRITICAL + ERROR heartbeat, nothing polled); a transient base-URL blip is a distinct WARN, not a credentials CRITICAL. The bearer is the **dedicated** Keychain `ITS_PORTAL_MANIFEST_TOKEN` (privilege-separated — scopes ONLY `/api/fieldops/manifests/internal/*`, the ADR-0004 decision-4 posture, because this process decodes hostile PDF/xlsx bytes); a **401 anywhere stops the whole cycle**. Three refusal classes are deliberately distinct: an **integrity** failure (bad HMAC / digest / chunk set) fires CRITICAL, posts NO result and leaves the bytes in D1 for forensics; a **§34** refusal fires CRITICAL naming the account on MALICIOUS, WARN on SUSPICIOUS; an **unreadable** document (a scan, an empty export, no header row) is an ORDINARY review item asking the office for a better copy, and is the ONLY class whose one-shot flag (`~/its/state/manifest_poll_flagged.json`) a Tier-2 operator may clear to retry. An unresolved Box root leaves the row claimed and deliberately UNFLAGGED so it self-heals. A hostile document can never kill the daemon — every parse runs in the killable child. §43 tree: `docs/runbooks/material_manifest_import.md`. |
| **Restart** | Dashboard **kickstart**; shell `install.sh load org.solutionsmith.its.manifest-poll` |

### rfq-poll — `po_materials.rfq_poll`

<!-- src: po_materials/rfq_poll.py:1-79 (docstring) + :125,147-160 (gates/REQUIRED_CONFIG); scripts/launchd/install.sh:79,95 (config key / 120s default); scripts/watchdog.py:247-254 (TRACKED_JOBS slug) + :317-320 (10-min window) | verified 2026-07-19 -->

| Field | Value |
|---|---|
| **Purpose** | Outbound-RFQ generation daemon (ADR-0004 Lane 2 R2 — the `po_poll` multi-pass model, one host / lock / heartbeat). ① **RFQ pass**: `GET /api/po/rfqs/internal/pending` → per row recompute the `rfq:v1` HMAC + constant-time verify → per PENDING vendor in the signed, sorted fan-out: resolve from the `ITS_Vendors` SoR (read-only, ADR decision 9) → deterministic **PRICE-FREE** render → Box file (§45 ROOT→job→"Purchase Orders"→"RFQs"; §47 version-on-conflict) → `RFQ_Log` + `RFQ_Pending_Review` row → `mark-filed` receipt (LAST, once per rfq — any earlier crash re-pulls the row and every step find-or-skips). ② **Status pass**: `RFQ_Pending_Review` SENT markers → `status-sync` (forward-only). SEND-FREE + AI-free; the actual vendor send is PR-D's `rfq_send`/`rfq_send_poll`. |
| **Interval** | `StartInterval`, default **120s** (`po_materials.rfq_poll.poll_interval_seconds`) — staggers off `portal-poll` (60s) / `po-poll` (90s) |
| **Source of work** | `GET /api/po/rfqs/internal/pending` (queued RFQs + lines + vendor rows + the `rfq:v1` hmac) |
| **Config gates** | `po_materials.rfq_poll.polling_enabled` — **ships false (dark)**; the seeded ITS_Config row (`scripts/migrations/seed_rfq_config.py`) is the operator's switch. |
| **Heartbeat row** | `po_materials.rfq_poll` — marker slug `rfq_poll` (window 10 min). WARNs until loaded AND the gate flipped (a loaded-but-dark daemon writes no marker by design). |
| **Log** | `~/its/logs/launchd/rfq_poll.out.log` / `.err.log` |
| **Known failure modes** | A bad-`rfq:v1`-HMAC or malformed-canonical row is one-shot-flagged (`state/rfq_poll_flagged.json`; CRITICAL + security Review-Queue row on first sighting) and never rendered/filed/marked — the row stays queued in D1 for forensics. An unknown vendor is a per-vendor Review-Queue fence (the OTHER vendors proceed); ALL-vendors-fenced withholds the receipt (never a silent drain). A **401** anywhere → `rfq_bearer_rejected` CRITICAL + the cycle STOPS (the RFQ bearer `ITS_PORTAL_RFQ_TOKEN` is privilege-separated from the PO and estimate tiers). Missing base URL / bearer / HMAC secret → fail-closed CRITICAL, nothing polled. §43 tree: `docs/runbooks/rfq_generation_path.md`. |
| **Restart** | Dashboard **kickstart**; shell `install.sh load org.solutionsmith.its.rfq-poll` |

### rfq-send — `po_materials.rfq_send_poll`

<!-- src: po_materials/rfq_send_poll.py:1-75 (docstring) + :84-92 (config keys) + :105 (heartbeat) + :156-159 (f22 workspace); po_materials/rfq_send.py:1-80 (docstring) + :100-101 (from_mailbox); scripts/launchd/install.sh:80,97 (config key / 900s default); scripts/launchd/org.solutionsmith.its.rfq-send.plist:47-51 (log paths); scripts/watchdog.py:255-262 (TRACKED_JOBS slug) + :335-339 (30-min window) | verified 2026-07-19 -->

| Field | Value |
|---|---|
| **Purpose** | SEND half of the RFQ two-process model (ADR-0004 R3) — the RFQ instantiation of the shared `send_poll_core` engine. Discovers `RFQ_Pending_Review` rows with `Send Now` (immediate) OR `Approve for Scheduled Send` (the Monday ≥07:00 Pacific batch) checked, runs the **F22** approval gate against the **ITS — Purchase Orders** workspace (§46 — the SAME procurement approver set as POs), stamps the verified approver, and dispatches to `rfq_send.send_one_row` → the shared `weekly_send.send_one_row` transmitter. Recipient = the vendor's `Contact Email` read LIVE from `ITS_Vendors` by Vendor Key; CC = the versioned invoice-routing list; **TWO attachments** (the price-free RFQ PDF + the fillable `.xlsx` quote form). From `procurement@`. AI-free (capability-gated in `SEND_SCRIPTS` — `anthropic`/`anthropic_client` **and** `ollama_client` AST-forbidden). |
| **Interval** | `StartInterval`, default **900s** (15 min) (`po_materials.rfq_send.poll_interval_seconds`) — an approval poller, mirrors `po-send` / `subcontract-send` / `weekly-send`. |
| **Source of work** | `RFQ_Pending_Review` Smartsheet sheet (id `3555996805844868`) |
| **Config gates** | `po_materials.rfq_send.polling_enabled` (**ships false** — dark; seeded by `scripts/migrations/seed_rfq_send_config.py`); `po_materials.rfq_send.poll_interval_seconds`; `po_materials.rfq_send.scheduled_send_local` (default `MON 07:00`); `po_materials.rfq_send.from_mailbox` (default `procurement@evergreenmirror.com`) |
| **Heartbeat row** | `po_materials.rfq_send_poll` — marker slug `rfq_send_poll` (window 30 min). WARNs until loaded AND the gate flipped (a loaded-but-dark send daemon writes no marker by design). |
| **Log** | `~/its/logs/launchd/rfq_send.out.log` / `.err.log` (note: `rfq_send`, not `rfq_send_poll`) |
| **Known failure modes** | F22 fail-closed (a circuit-open / auth error aborts the cycle with zero sends; an empty approver set = `EMPTY_ALLOWLIST` blocks all — the §46 share list of ITS — Purchase Orders must include the approvers). Per-row fence. The `po_materials_rfq` **contamination guard** HARD-HELDs (`held_workstream_mismatch` + CRITICAL) any row not tagged for this lane, before the write-ahead `SENDING` marker — cross-lane dispatch by `po-send`/`subcontract-send` is structurally impossible. Rows HELD on unknown Vendor Key / blank vendor email (`held_no_recipient`), missing compiled RFQ PDF (`held_missing_pdf`), or a numberless row (`held_missing_envelope`). A transient Box failure fetching the quote form degrades the send to PDF-only (WARN), never HELD. `polling_enabled=false` short-circuits (the dark default — a send gate never fails open). §43 tree: `docs/runbooks/rfq_send.md`. |
| **Restart** | Dashboard **kickstart**; shell `install.sh load org.solutionsmith.its.rfq-send`. **Go-live is a FIXED high-capability-class External-Send-Gate action** (flip the gate true + load the plist) — escalate to Seth, never a Tier-2 repair. |

---

## Subcontracts daemon

### subcontract-poll — `subcontracts.subcontract_poll`

<!-- src: subcontracts/subcontract_poll.py:471-517 (docstring); subcontracts/subcontract_poll.py:144-146 (gates); scripts/launchd/install.sh:87 (120s default) | verified 2026-07-14 -->

| Field | Value |
|---|---|
| **Purpose** | Subcontract pull daemon (the `po_poll` multi-pass model). ① **Drafts pass**: `GET /api/subcontracts/internal/pending` → per row recompute `sub:v1` HMAC + verify → SOV recompute + assert vs the signed §2.1 Contract Price → Subcontract_Log collision check → ITS_Subcontractors snapshot → deterministic render (**three** files: Subcontract body `.docx` + Exhibit A `.docx` + Annex C Schedule-of-Values `.xlsx`) → three Box uploads → `Subcontract_Log` append + `Subcontract_Pending_Review` row → `mark-filed` receipt. ② **Subcontractor down-sync**. ③ **Up-sync**. ④ **Status pass**. |
| **Interval** | `StartInterval`, default **120s** (`subcontracts.subcontract_poll.poll_interval_seconds`) |
| **Source of work** | `GET /api/subcontracts/internal/pending` |
| **Config gates** | `subcontracts.subcontract_poll.polling_enabled`; `subcontracts.subcontract_poll.subcontractors_sync_enabled`; `subcontracts.subcontract_poll.status_sync_enabled`. **All ship false (dark).** |
| **Heartbeat row** | `subcontracts.subcontract_poll` — marker slug `subcontract_poll` (window 10 min) |
| **Log** | `~/its/logs/launchd/subcontract_poll.out.log` / `.err.log` |
| **Known failure modes** | A bad-HMAC or SOV-mismatch row is one-shot-flagged (CRITICAL + security Review-Queue row) and never rendered/filed/marked; stays queued in D1 for forensics. Deliverables are editable `.docx`/`.xlsx` (not PDF, operator directive). The **send half is now built** (`subcontract-send`, below) and ships dark. §43 tree: `docs/runbooks/subcontract_generation_path.md`. |
| **Restart** | Dashboard **kickstart**; shell `install.sh load org.solutionsmith.its.subcontract-poll` |

### subcontract-send — `subcontracts.subcontract_send_poll`

<!-- src: subcontracts/subcontract_send_poll.py:50 (gate) + :63-66 (heartbeat); scripts/launchd/install.sh:75,89 (config key / 900s default); scripts/watchdog.py:238 (TRACKED_JOBS slug) | verified 2026-07-15 -->

| Field | Value |
|---|---|
| **Purpose** | SEND half of the subcontract two-process model (SC-S4, built by #599) — the subcontract instantiation of the shared send engine. Discovers approved `Subcontract_Pending_Review` rows, runs the **F22** approval gate against the subcontracts send workspace (§46), stamps the verified approver, and dispatches the rendered package. AI-free (capability-gated in `SEND_SCRIPTS`). |
| **Interval** | `StartInterval`, default **900s** (15 min) (`subcontracts.subcontract_send.poll_interval_seconds`) — an approval poller, mirrors `po-send` / `weekly-send`. |
| **Source of work** | `Subcontract_Pending_Review` Smartsheet sheet |
| **Config gates** | `subcontracts.subcontract_send.polling_enabled` (**default False** — dark-ship / CO-1 fail-safe); `subcontracts.subcontract_send.poll_interval_seconds` |
| **Heartbeat row** | `subcontracts.subcontract_send_poll` — marker slug `subcontract_send_poll` (window 30 min). WARNs until loaded AND the gate flipped (a loaded-but-dark daemon writes no marker). |
| **Log** | `~/its/logs/launchd/subcontract_send.out.log` / `.err.log` |
| **Known failure modes** | F22 fail-closed (empty approver set blocks all sends). Per-row fence. `polling_enabled=false` short-circuits (the dark default). §43 tree: `docs/runbooks/subcontract_send.md`. |
| **Restart** | Dashboard **kickstart**; shell `install.sh load org.solutionsmith.its.subcontract-send` |

---

## Field Ops daemon

### fieldops-sync — `field_ops.fieldops_sync`

<!-- src: field_ops/fieldops_sync.py:518-564 (docstring); field_ops/fieldops_sync.py:34,114-136 (gates); scripts/launchd/install.sh:84 (90s default) | verified 2026-07-14 -->

| Field | Value |
|---|---|
| **Purpose** | Mac-side mirror of the job-tracker pivot (D1 primary + dual Active-Jobs mirror). A job is created/edited/lifecycle-changed in the ITS Portal Job Tracker; the Worker records it send-free in D1 (`origin='portal'`, `sync_state='pending'`, bumps `mirror_version`). This daemon pulls the dirty jobs and mirrors each UP into BOTH `ITS_Active_Jobs` (safety) and `ITS_Active_Jobs_Progress` (progress), and drives the progress hours/equipment/materials/incidents/receipts mirror passes plus the Track-6 archive pass. One writer ⇒ the two workstreams never drift (§50/§51). |
| **Interval** | `StartInterval`, default **90s** (`field_ops.fieldops_sync.poll_interval_seconds`) |
| **Source of work** | Dirty portal-origin jobs in D1 (version-vector: `mirror_version` vs `safety_mirrored_version` / `progress_mirrored_version` watermarks); the archive pass drains its OWN `/archive-pending` queue |
| **Config gates** | `field_ops.fieldops_sync.sync_enabled` (canonical master gate, ARCH-1 — pause anytime; **read ITS_Config for live state, never this table**); per-pass `field_ops.fieldops_sync.hours_enabled` / `equipment_enabled` / `materials_enabled` / `incidents_enabled` / `receipts_enabled` / `archive_enabled` |
| **Heartbeat row** | `field_ops.fieldops_sync` — marker slug `fieldops_sync` (window 8 min). When the master gate is on, the marker stays fresh and Check C surfaces only a genuine silent death. |
| **Log** | `~/its/logs/launchd/fieldops_sync.out.log` / `.err.log` |
| **Known failure modes** | **Fail-closed** on missing base URL or bearer (CRITICAL — won't self-heal — plus ERROR heartbeat); 401 on pending-jobs → CRITICAL. Authenticates with `ITS_PORTAL_FIELDOPS_TOKEN` (distinct from portal_poll's internal token). Requires the "Portal Job Key" column on BOTH sheets or `add_rows` KeyErrors. Version-vector effect is at-least-once, idempotent, crash-safe. |
| **Restart** | Dashboard **kickstart**; shell `install.sh load org.solutionsmith.its.fieldops-sync` |

---

## Privileged §50 actuators

These two are the trusted Mac side of the External Send Gate posture: the cloud Worker
can only **enqueue** a request (send-free); the privileged commit/deploy capability
lives on the Mac with the operator's git + wrangler auth. Both are **high-capability +
operator-gated activation** and both write an ITS_Daemon_Health heartbeat but are
**not** in Check-C `TRACKED_JOBS`.

### publish-daemon — `safety_reports.publish_daemon`

<!-- src: safety_reports/publish_daemon.py:189-230 (docstring); safety_reports/publish_daemon.py:81 (polling gate); scripts/launchd/org.solutionsmith.its.publish-daemon.plist:53-54 (120s fixed) | verified 2026-07-14 -->

| Field | Value |
|---|---|
| **Purpose** | Actuates form-publish requests (C12=A pipeline). Per claimed request: pull `GET /api/internal/publish/pending` → atomically CLAIM (lease) → re-validate vs LIVE git HEAD → STAMP `validated` → apply form file(s) to a worktree, commit, open PR, wait for CI, MERGE on green → STAMP `tested` → deploy via local wrangler + fast-forward the live tree + health check → STAMP `live` → regenerate the Box blank archive → STAMP `archived`. Any stage failure → STAMP `failed` + CRITICAL triple-fire. |
| **Interval** | `StartInterval` **120s** (fixed, not parameterized — publishes are infrequent + the cycle is heavy) |
| **Source of work** | `GET /api/internal/publish/pending` (bearer-gated) |
| **Config gates** | `safety_reports.publish_daemon.polling_enabled` |
| **Heartbeat row** | `safety_reports.publish_daemon` (writes ITS_Daemon_Health; **not** a Check-C tracked job) |
| **Log** | `~/its/logs/launchd/publish_daemon.out.log` / `.err.log` |
| **Known failure modes** | **Deploy gate** (forensic class #2): refuses to deploy the Worker ahead of unapplied remote D1 migrations — rows stay pending until the operator runs `migrations apply`, then the next cycle unblocks. Any stage failure stamps `failed(stage, reason)` and fires an operator CRITICAL (never a silent stall). §43 runbook: `safety_reports/README.md`. |
| **Restart** | Dashboard **kickstart**; shell `install.sh load org.solutionsmith.its.publish-daemon` |

### config-actuator — `po_materials.config_actuator`

<!-- src: po_materials/config_actuator.py:424-466 (docstring); po_materials/config_actuator.py:79 (polling gate); scripts/launchd/org.solutionsmith.its.config-actuator.plist:58-59 (120s fixed) | verified 2026-07-14 -->

| Field | Value |
|---|---|
| **Purpose** | The **sole** §50 privileged config actuator. Mirrors `publish_daemon` against the `config_requests` queue: pull `GET /api/internal/config/pending` → CLAIM → re-validate + WRITE the config file vs LIVE git HEAD → STAMP `validated` → commit on a per-request branch, open PR, wait for CI, MERGE → STAMP `tested` → deploy via local wrangler (re-bundles purchaser/tax/terms JSON the Worker imports at build time) + fast-forward + health check → STAMP `live` → no-op terminal → STAMP `archived`. |
| **Interval** | `StartInterval` **120s** (fixed) |
| **Source of work** | `GET /api/internal/config/pending` (bearer-gated) |
| **Config gates** | `po_materials.config_actuator.polling_enabled` |
| **Heartbeat row** | `po_materials.config_actuator` (writes ITS_Daemon_Health; **not** a Check-C tracked job) |
| **Log** | `~/its/logs/launchd/config_actuator.out.log` / `.err.log` |
| **Known failure modes** | Same **deploy gate** as `publish_daemon` (refuses ahead of unapplied D1 migrations). A redeploy is required because the Worker bundles config JSON at build time — an edit is stale in the live Worker until the pipeline re-bundles. Any stage failure → STAMP `failed` + CRITICAL. §43 runbook: `docs/runbooks/config_actuator.md`. |
| **Restart** | Dashboard **kickstart**; shell `install.sh load org.solutionsmith.its.config-actuator` |

---

## Schema / picklist daemons

### picklist-sync — `scripts/run_picklist_sync.py`

<!-- src: scripts/run_picklist_sync.py:565-588 (docstring); scripts/run_picklist_sync.py:324 (@require_active); scripts/launchd/org.solutionsmith.its.picklist-sync.plist:54-55 (3600s) | verified 2026-07-14 -->

| Field | Value |
|---|---|
| **Purpose** | Hourly cross-sheet PICKLIST option sync from master DBs, driven by `Picklist_Sync_Config`. Reference-checked removals (live cell usage blocks a delete → Review-Queue row); two-stage size guardrails; SHA-256 idempotency. |
| **Interval** | `StartInterval` **3600s** (hourly, fixed) |
| **Source of work** | `Picklist_Sync_Config` mappings |
| **Config gates** | Kill switch only (`@require_active`) — no `polling_enabled` |
| **Heartbeat row** | None — marker slug `safety_picklist_sync` (window **3h**) |
| **Log** | `~/its/logs/launchd/run_picklist_sync.out.log` / `.err.log` |
| **Known failure modes** | A single-mapping failure stays at ERROR (recorded in ITS_Errors, no wake-up); **≥3 mappings failed in one run** escalates to CRITICAL triple-fire. One ITS_Errors INFO row per run summarizes examined / applied / skipped / blocked / failed. CLI: `--dry`, `--mapping <id>`, `--smoke-test`. |
| **Restart** | Dashboard start/stop; shell `install.sh load org.solutionsmith.its.picklist-sync` |

### picklist-audit — `scripts/audit_picklist_drift.py`

<!-- src: scripts/audit_picklist_drift.py:590-635 (docstring); scripts/audit_picklist_drift.py:66 (job slug); scripts/launchd/org.solutionsmith.its.picklist-audit.plist:55-63 (Sun 15:00) | verified 2026-07-14 -->

| Field | Value |
|---|---|
| **Purpose** | Weekly server-side picklist-drift audit (Op Stds §35 two-layer enforcement). Verifies each column registered in `picklist_validation.REGISTRY` against Smartsheet, surfacing three drift categories: wrong column type, allowed-set mismatch, and "restrict to picklist values only" toggle off. |
| **Interval** | `StartCalendarInterval` — **Sunday 15:00** local (`Weekday 0`, `Hour 15`, `Minute 0`) |
| **Source of work** | `picklist_validation.REGISTRY` columns (read against live Smartsheet) |
| **Config gates** | None (read-only audit by default) |
| **Heartbeat row** | None — marker slug `safety_picklist_audit` (window **8 days**) |
| **Log** | `~/its/logs/launchd/audit_picklist_drift.out.log` / `.err.log` |
| **Known failure modes** | Exits 1 on any drift finding (operator UI work pending or registry/sheet disagreement); exits 0 clean. `--apply`/`--commit` is the operator reconcile (additive only, dry-run by default). §43 runbook: `docs/runbooks/picklist_drift_reconcile.md`. |
| **Restart** | Dashboard start/stop; shell `install.sh load org.solutionsmith.its.picklist-audit` |

---

## System daemons

### watchdog — `scripts/watchdog.py`

<!-- src: scripts/watchdog.py:2345-2417 (CHECKS registry); scripts/watchdog.py:174-232 (TRACKED_JOBS); scripts/launchd/org.solutionsmith.its.watchdog.plist (StartInterval 3600); scripts/watchdog.py DAILY_ONLY_CHECKS | verified 2026-08-07 -->

| Field | Value |
|---|---|
| **Purpose** | The liveness + integrity sweep — **hourly since 2026-08-07** (was daily 07:00; a 12.7h Smartsheet outage on 2026-08-06 paged zero times because the watchdog had already run for the day). Runs the `CHECKS` registry (live letters A–V; E deferred, F retired, H never existed). Notable checks: **A** stale review-queue, **B** open CRITICALs, **C** `TRACKED_JOBS` marker staleness, **G** alert-dedupe sweep, **I** safety+progress Friday-crash catch-up, **N** stuck-WSR-send, **O** row-cap rotation, **Q/R** portal-poll resilience, **S** main-branch CI green, **T** stale-HELD rows, **U** approver drift, **V** portal-prune health. |
| **Interval** | `StartInterval` — **3600s (hourly)**, `RunAtLoad` true. **Two tiers:** every sweep runs the hourly checks (A B C J K M N P Q R S T V); the `DAILY_ONLY_CHECKS` set (D G I L O U W) runs about once per 24h, gated on the `watchdog_daily.last_run` marker aging past 20h — those checks are expensive, mutating, or daily-rhythm and are harmful at hourly. The marker gate (not a wall-clock hour) means a sleeping host cannot skip the daily tier for a whole day. |
| **Source of work** | Marker files, Smartsheet sheets, circuit breaker, heartbeats, GitHub CI, the portal Worker |
| **Config gates** | None (MAINTENANCE-aware — defers inline-firing checks during MAINTENANCE) |
| **Heartbeat row** | None — a daemon cannot reliably watch itself. Its OWN liveness is the external **Healthchecks.io** ping (the dead-man's switch for total host death), which is skipped until `system.heartbeat_url` is configured. |
| **Log** | `~/its/logs/launchd/watchdog.out.log` / `.err.log` |
| **Known failure modes** | If the watchdog itself dies, only the external **Healthchecks.io** ping surfaces it — and that ping is skipped while `system.heartbeat_url` holds its seed placeholder, so a dead watchdog is currently undetected externally. A missed sweep is absorbed by the next hourly fire; a missed DAILY tier is absorbed by the marker gate (it stays due until it actually runs). |
| **Restart** | Dashboard start/stop; shell `install.sh load org.solutionsmith.its.watchdog` |

### dashboard — `operator_dashboard`

<!-- src: operator_dashboard/__main__.py:637-657 (docstring + main); scripts/launchd/org.solutionsmith.its.dashboard.plist:69-77 (KeepAlive server); operator_dashboard/act/daemon_ops.py:253-263 (controllable_labels excludes dashboard) | verified 2026-07-14 -->

| Field | Value |
|---|---|
| **Purpose** | Localhost-only FastAPI operator console. Read-only observability panels (launchd state, watchdog markers, breaker, heartbeats, locks, log-tail, errors, review-queue) + PIN-gated ACT surface (Class-A ITS_Config editor, Class-B daemon interval/control + breaker-clear + error-log mark-resolved/clear, Class-C secret rotation + PIN change). Binds `127.0.0.1:8484`; exposed over Tailscale with `tailscale serve 8484` — never a public interface. |
| **Interval** | **None** — a persistent server. The only ITS plist with `RunAtLoad=true` **and** `KeepAlive=true` (launchd restarts it if it exits). |
| **Source of work** | HTTP requests (no polling) |
| **Config gates** | Ships **DARK / fail-closed** until `ITS_OPERATOR_PIN` is provisioned in Keychain (constant-time PIN compare). `ITS_DASH_ALLOWED_ORIGINS` (plist env) gates Tailscale origins; localhost is always allowed. |
| **Heartbeat row** | None. Its liveness IS the `KeepAlive` restart-on-exit; the read-only daemons panel shows live launchctl state. |
| **Log** | `~/its/logs/launchd/dashboard.out.log` / `.err.log` |
| **Known failure modes** | `KeepAlive` restarts a crash automatically. Fail-closed until PIN is set — no ACT verb works before then. It is deliberately **excluded** from its own daemon-control allowlist (a service must not stop itself via its own UI). |
| **Restart** | **Not via the dashboard UI** (it excludes its own label). Shell: `launchctl kickstart -k gui/<uid>/org.solutionsmith.its.dashboard` or `install.sh load org.solutionsmith.its.dashboard` |

---

## Restarting a daemon

### Dashboard verbs (PIN-gated Class-B ACT)

<!-- src: operator_dashboard/act/daemon_ops.py:275-316 (control_daemon); operator_dashboard/act/daemon_ops.py:46 (CONTROL_ACTIONS); operator_dashboard/act/daemon_ops.py:318-326 (_run_kickstart) | verified 2026-07-14 -->
The operator dashboard's daemon-control verb (Class-B, PIN + elevated-confirm) exposes
three actions, each mapping to a shell operation. It performs **no** ITS_Config write —
it is pure launchctl process management, so starting a dark daemon does nothing until
its `polling_enabled` gate is on.

| Dashboard action | What it runs | Use when |
|---|---|---|
| **start** | `install.sh load <label>` | The daemon is unloaded and you want it running |
| **stop** | `install.sh unload <label>` | You want to pause a daemon at the process level |
| **kickstart** | `launchctl kickstart -k gui/<uid>/<label>` | The daemon is loaded but wedged — kill the running instance and start fresh (the normal "restart") |

<!-- src: operator_dashboard/act/daemon_ops.py:253-263 (controllable_labels allowlist); operator_dashboard/act/daemon_ops.py:132-192 (edit_interval) | verified 2026-07-14 -->
The controllable set is every `org.solutionsmith.its.*.plist` in `scripts/launchd/`
**minus the dashboard's own label** — a label not in the allowlist is refused before any
launchctl call. Interval daemons additionally support a Class-B **interval edit**
(`edit_interval`), which re-runs `install.sh load` with the new value (re-baking the
StartInterval into the installed plist).

### Shell fallback (`install.sh`)

<!-- src: scripts/launchd/install.sh:22-27 (usage); scripts/launchd/install.sh:157-195 (cmd_load); scripts/launchd/install.sh:214-235 (cmd_status) | verified 2026-07-14 -->
When the dashboard is unavailable, `scripts/launchd/install.sh` is the canonical helper.
It substitutes `__ITS_HOME__` (and `__POLL_INTERVAL_SECONDS__` for interval daemons),
`plutil -lint`s the result, then `bootout`/`bootstrap`s the agent.

```
./install.sh load    <plist> [interval]   # substitute placeholders + bootstrap into launchd
./install.sh unload  <plist>              # bootout and remove from ~/Library/LaunchAgents/
./install.sh status  [plist]             # list loaded ITS jobs (or one if specified)
./install.sh dry-run <plist> [interval]   # print the resolved plist to stdout (no load)
```

`<plist>` accepts the filename (with or without `.plist`) or the label. For an interval
daemon, an optional trailing `[interval]` (positive integer seconds) overrides the
StartInterval; without it, the value comes from the daemon's `poll_interval_seconds`
ITS_Config row, falling back to the per-daemon default. Example:

```
scripts/launchd/install.sh load org.solutionsmith.its.portal-poll 60
```

### Direct launchctl restart

<!-- src: operator_dashboard/act/daemon_ops.py:318-326 (_run_kickstart command form) | verified 2026-07-14 -->
For a loaded daemon, the lowest-level restart is what the dashboard's kickstart runs:

```
launchctl kickstart -k gui/$(id -u)/org.solutionsmith.its.<label>
```

Remember the **fresh-process model**: after any restart, the daemon re-reads its Python
source and its ITS_Config gates on the very next cycle — there is no in-memory state to
clear, and a `polling_enabled=false` gate keeps a freshly-started daemon a no-op until
the gate is flipped.

## Edge cases & limitations

<!-- src: scripts/launchd/install.sh:10-20 (interval baked at install); scripts/watchdog.py:190-231 (dark-daemon WARN semantics) | verified 2026-07-14 -->
- **Interval changes need a re-install.** The StartInterval is baked into the installed
  plist at `install.sh load` time. Editing the `poll_interval_seconds` ITS_Config row
  alone does nothing to a running daemon; re-load (or use the dashboard interval edit).
- **A loaded dark daemon writes no Check-C marker.** `po-poll`, `po-send`,
  `estimate-poll`, `rfq-poll`, `rfq-send`, `subcontract-poll`, `subcontract-send`,
  `progress-*`, and `compile-now-poll` will legitimately WARN in
  Check C until the operator BOTH loads the plist AND flips at least one runtime gate.
  Register + activate together; an all-gates-false loaded daemon is an intentional dark
  no-op, not a fault.
- **The two §50 actuators and the dashboard are absent from Check C.** `publish-daemon`
  and `config-actuator` report via ITS_Daemon_Health heartbeat only; the dashboard
  relies on `KeepAlive`. None of the three has a marker-staleness alert.
- **Timezones are the Mac's local time.** All `StartCalendarInterval` fire times
  (Fri 14:00 / Fri 14:30 / Sun 15:00) are local wall-clock. The watchdog is no longer among
  them — it is interval-driven (hourly) since 2026-08-07.
<!-- src: scripts/launchd/*.plist StandardOutPath vs ProgramArguments (po-send / subcontract-send / rfq-send) | verified 2026-07-19 -->
- **The three `*-send` approval pollers log under the label, not the module.** `po-send`
  writes `po_send.out.log`, `subcontract-send` writes `subcontract_send.out.log`, and
  `rfq-send` writes `rfq_send.out.log` — none of them `*_poll.out.log`. The StandardOut
  path underscores the label's last segment; most daemons' basenames match their module,
  but these three do not. (`weekly-send` and `progress-send` DO match their modules —
  `weekly_send_poll.out.log` / `progress_send_poll.out.log`.)

## Related docs

- [system_architecture.md](system_architecture.md) — the two-layer model, the launchd/
  Worker/Smartsheet/Box topology these daemons live in
- [data_model_reference.md](data_model_reference.md) — the Smartsheet sheets and D1
  tables the daemons read and write (ITS_Active_Jobs, WSR/WPR/PO review sheets, D1 queues)
- [integration_reference.md](integration_reference.md) — the Worker routes
  (`/api/internal/pending`, `/api/po/internal/*`, etc.) that are the pollers' source of work
- [security_trust_model.md](security_trust_model.md) — the External Send Gate two-process
  model, F22 approval attestation, and §34 attachment/photo screening the daemons enforce
- [escalation_matrix.md](escalation_matrix.md) — watchdog check → operator action mapping,
  and the Tier-1/Tier-2/Tier-3 self-heal / successor-operator model
- [glossary.md](glossary.md) — terms (heartbeat, marker, Check C, F22, §50, §51, dark gate)
- [documentation_index.md](documentation_index.md) — the full Tier-1 corpus index
