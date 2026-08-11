---
type: reference
date: 2026-07-15
status: active
workstream: docs
tags: [documentation-corpus, troubleshooting-tree, generated]
---

# ITS Troubleshooting Guide

Pick the workflow you are blocked at, then the step, then the symptom that matches. Each symptom lists the signals, the ordered checks, the ordered resolutions, and who resolves it. This guide is generated from `docs/troubleshooting/tree.yaml` — the same source that drives the dashboard troubleshooter — so the two never drift.

**Resolution classes:** _Operator-resolvable (solo)_ = documented + low blast radius. _Escalate to Seth (co-resolve)_ = touches the Send Gate, secrets/auth, doctrine, or a code/deploy change, or is novel. When unsure, escalate.

## Workflows

- **Safety report — portal submission to sent weekly packet** — A field submission enters at the send-free portal, is pulled + filed on the Mac, compiled into a weekly packet, human-approved, and sent. The generation and send halves are separate processes (External Send Gate).
- **Progress report — intake, routing, compile, send** — The Safety-Reports twin for weekly progress packets, on its own Active-Jobs sheet.
- **Field-ops sync — portal job/hours/materials/equipment/incidents to Smartsheet** — The portal is the writer of record for jobs and field capture; fieldops-sync mirrors dirty portal-origin records UP into the two Active-Jobs sheets and the standing trackers.
- **Purchase order — build, config, pull/render/file, send** — The deterministic PO pipeline (no AI). Ships dark until its gates are flipped.
- **Subcontract — build, legal gate, pull/render, send** — The deterministic subcontract-package pipeline (no AI), PO-mirror. Ships dark.
- **Email intake — the superseded safety path (portal PULL is canonical)** — Safety email intake was RETIRED; the Safety Portal PULL model supersedes it. The shared Graph plumbing is preserved for a future Email-Triage workstream.
- **Config change — the §50 privileged actuation rail** — The cloud can only ENQUEUE a config request (send-free); the config-actuator commits it on the Mac (validate → PR → CI → merge → deploy → stamp live).
- **Operator dashboard — auth tiers and Class A/B/C actions** — The localhost-only console; read-only panels plus PIN-gated actions over Tailscale.
- **The daemon plane — liveness, breaker, alerts, row-cap, picklists, guards** — The shared infrastructure every workstream rides on: launchd, heartbeats + markers, the circuit breaker, the alert path, row-cap rotation, and the schema/guard sweeps.
- **Publish + Box filing — form publish, Box token, portal prune** — The form-publish actuator (C12=A), the Box document store the packets file into, and the portal-prune housekeeping.

## Safety report — portal submission to sent weekly packet

A field submission enters at the send-free portal, is pulled + filed on the Mac, compiled into a weekly packet, human-approved, and sent. The generation and send halves are separate processes (External Send Gate).

### Field user submits the form in the portal

| What happens | |
|---|---|
| Worker route | `POST /api/submit` |

**Healthy signals:**
- The user sees a success screen; the submission lands in D1 (send-free) with an HMAC signature.

#### The portal rejects the submission or the user is bounced to login.

**Resolution class:** Operator-resolvable (solo)

**Signals:** HTTP 401, session expired, capability denied

**Checks (in order):**
- Confirm the user's account is enabled and has the submit capability (portal admin dashboard).
- Check the browser is on the current portal origin (a stale bookmark to the old *.workers.dev URL 401s after a custom-domain deploy).

**Resolutions (in order):**
- Re-share/enable the account or re-issue the login; point the user at the custom-domain URL.

**See also:** runbook `docs/runbooks/safety_portal_admin_dashboard.md`

### portal-poll pulls the submission and hands it to intake

| What happens | |
|---|---|
| Daemon | `portal-poll` |
| Worker route | `GET /api/internal/pending` |
| Sheets | `ITS_Review_Queue` |
| Config gates | `safety_reports.portal_poll.polling_enabled`, `safety_reports.photo_screen.clamav_enabled` |

**Healthy signals:**
- The portal-poll heartbeat row is fresh (dashboard Daemon status panel).
- The D1 pending backlog drains; filed submissions get a mark-filed receipt.

#### portal-poll cannot reach the Worker; nothing is being pulled.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** sustained pending-fetch failures, CRITICAL portal_poll fetch, worker_base_url unset

**Checks (in order):**
- Dashboard Daemon status → is the heartbeat stale / last error set?
- Is safety_reports.portal.worker_base_url set to the custom domain (a custom_domain deploy disables *.workers.dev)?
- Is the Mac bearer token present in Keychain (fail-closed if absent)?

**Resolutions (in order):**
- If the base URL is wrong, correct it and let the next cycle recover.
- If the outage is a Worker/Cloudflare problem or a token issue, escalate.

**See also:** watchdog `_check_portal_poll_fetch_outage`

#### The Worker has pending rows but the backlog is not draining.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** stuck pending backlog, saturated page draining nothing

**Checks (in order):**
- Is portal-poll running and its gate on (designed-dark check first)?
- Are rows failing HMAC verify (one-shot-flagged, never filed) — check the Review Queue for security-flagged rows?

**Resolutions (in order):**
- Toggle the polling gate on if it is dark by design; otherwise inspect the flagged rows and escalate a systemic HMAC/secret mismatch.

**See also:** watchdog `_check_portal_poll_pending_backlog`

#### A submission is refused before filing with a security-flagged Review-Queue row.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** photo MALICIOUS, security_flag=True, CRITICAL naming the account

**Checks (in order):**
- Review the flagged item in the Review Queue; note the submitting account.

**Resolutions (in order):**
- Confirm the disposition with the field user; the item is deliberately not filed. Escalate if it indicates account compromise.

**See also:** runbook `docs/runbooks/safety_photo_path.md`

#### A user cannot download a filed PDF from the portal (404).

**Resolution class:** Operator-resolvable (solo)

**Signals:** PDF download 404, requester-bound 24h, different account

**Checks (in order):**
- Portal filed-PDF downloads are bound to the REQUESTING account for 24h; a different account gets a 404 by design (not a fault).

**Resolutions (in order):**
- Have the original requester download it, or re-request the download; see the PDF-download runbook.

**See also:** runbook `docs/runbooks/safety_portal_pdf_download.md`

### Below-confidence / flagged items route to human review

| What happens | |
|---|---|
| Sheets | `ITS_Review_Queue` |

**Healthy signals:**
- The Review Queue has no long-stale PENDING rows (dashboard Review queue panel).

#### Items sit in the Review Queue past their SLA.

**Resolution class:** Operator-resolvable (solo)

**Signals:** stale review queue, PENDING past SLA tier

**Checks (in order):**
- Dashboard Review queue panel → which rows are oldest / which workstream?

**Resolutions (in order):**
- Work the queued items (approve / reject / escalate) per the item's reason.

**See also:** runbook `docs/runbooks/review_queue_triage.md` · watchdog `_check_stale_review_queue`

#### A reviewer-chain assignment is missing or points at an out-of-office reviewer.

**Resolution class:** Operator-resolvable (solo)

**Signals:** reviewer chain forward gap, no reviewer for the coming period

**Checks (in order):**
- Confirm the reviewer chain has coverage for the upcoming window.

**Resolutions (in order):**
- Fill the chain gap / set the PTO override per the scheduling config.

**See also:** runbook `docs/runbooks/time_off_reviewer_chain.md` · watchdog `_check_reviewer_chain_forward`

#### A submission landed under the wrong form workflow / category.

**Resolution class:** Operator-resolvable (solo)

**Signals:** form miscategorized, wrong workflow, recategorize needed

**Checks (in order):**
- Confirm the intended form category with the submitter.

**Resolutions (in order):**
- Recategorize the form per the recategorize runbook (a documented, low-class move).

**See also:** runbook `docs/runbooks/form_workflow_recategorize.md`

### weekly-generate compiles the Sat→Fri packet (generation half)

| What happens | |
|---|---|
| Daemon | `weekly-generate` |
| Sheets | `ITS_Active_Jobs`, `WSR_human_review` |

**Healthy signals:**
- Friday 14:00 the compile runs; a Rollup snapshot + a PENDING WSR_human_review row appear per job/week.

#### The Friday compile did not run (laptop asleep / crash) and no packet was produced.

**Resolution class:** Operator-resolvable (solo)

**Signals:** missed weekly-generate, no Rollup row for the week

**Checks (in order):**
- Dashboard → was the weekly-generate marker written this week?
- Did the watchdog catch-up fire on the next wake?

**Resolutions (in order):**
- The watchdog Check-I catch-up recovers a missed Friday automatically; if it did not, run the documented manual backfill.

**See also:** runbook `docs/runbooks/safety_weekly_generate.md` · watchdog `_check_weekly_generate_catchup`

#### One job's week failed to compile and was routed to the Review Queue; others compiled fine.

**Resolution class:** Operator-resolvable (solo)

**Signals:** per-job fence, ITS_Review_Queue compile failure

**Checks (in order):**
- Inspect the Review-Queue row for the failing job/week (which document / which error).

**Resolutions (in order):**
- Fix the offending source document and re-trigger the compile for that job (Compile Now), per the runbook.

**See also:** runbook `docs/runbooks/safety_weekly_generate.md`

### On-demand "Compile Now" produces a packet without waiting for Friday

| What happens | |
|---|---|
| Daemon | `compile-now-poll` |
| Sheets | `ITS_Active_Jobs` |
| Config gates | `safety_reports.compile_now_poll.polling_enabled`, `progress_reports.compile_now_poll.polling_enabled` |

**Healthy signals:**
- Checking Compile Now on a week's Rollup row produces a packet within a minute or two.

#### Compile Now was checked but no packet appears and the checkbox stays set.

**Resolution class:** Operator-resolvable (solo)

**Signals:** Compile Now trigger still set, compile-now-poll heartbeat stale

**Checks (in order):**
- Is compile-now-poll loaded and its gate on? (It is INTENDED to be always-loaded — a stale marker WARNs by design until loaded.)
- Did the compile fail and route the job to the Review Queue (the trigger clears only on success)?

**Resolutions (in order):**
- Load the compile-now-poll plist if unloaded; otherwise inspect the Review-Queue row and fix the source, per the runbook.

**See also:** runbook `docs/runbooks/compile_now_poll.md`

#### A CRITICAL says the compile-now trigger scan is failing for most jobs (or the Active-Jobs read itself failed) several cycles running.

**Resolution class:** Operator-resolvable (solo)

**Signals:** compile_now_scan_sustained, compile_now_poll.scan_failed, ITS_Active_Jobs read FAILED, compile-now-poll heartbeat DEGRADED

**Checks (in order):**
- Read the CRITICAL message — how many of how many scanned jobs, and does it name an ITS_Active_Jobs read failure?
- Read the "cause(s):" clause — SmartsheetError / HTTP 5xx / timeout is a platform incident; a code error (TypeError, AttributeError, KeyError) is NOT transient and escalates to Seth immediately.
- Can you open ITS_Active_Jobs and a week sheet in the Smartsheet browser? A broad Smartsheet incident is the usual cause.
- Are other daemons erroring in ITS_Errors in the same window (platform-wide, not this daemon)?
- ITS_Daemon_Health safety_reports.compile_now_poll — Last Cycle At still advancing means the daemon is alive and retrying.
- Is there a compile_now_job_scan_sustained row in the same window? Read ITS WORDING, not the job count. "while other jobs scan fine" means at least one job DID scan — a per-job fault wearing a Fault-D costume; work the "a CRITICAL names ONE job" checks below first, a renamed folder never self-heals. "as did EVERY other scanned job (N/N) … BROAD scan outage" means nothing scanned at all; stay here and rename nothing.
- Message ends "…and N more"? The complete failing set with per-job consecutive counts is on the ITS host in ~/its/state/compile_now_job_scan_failures.json — ask Claude to read it.

**Resolutions (in order):**
- Smartsheet cause and most jobs named → wait and watch; this self-heals. Nothing is lost — the Compile Now checkbox stays set and the daemon retries every ~90s, so the packet compiles once Smartsheet reads recover.
- A code-error cause, or a renamed/moved sheet found by the per-job checks → this is NOT the self-healing case; do the rename-back repair or escalate the code error to Seth.
- The CRITICAL re-pages on a DOUBLING-then-FIXED interval (5, 10, 20, 40, 80, then every 40 consecutive failing cycles — roughly hourly at the 90s cadence), not every cycle; every cycle in between still writes an ERROR compile_now_poll.scan_failed row. Recovery is proven by those ERROR rows stopping, NOT by the CRITICAL going quiet.
- Mark the CRITICAL resolved once new ERROR rows stop appearing. Do NOT disable the daemon to silence it (that hides the outage and stops the recovery retries).
- Still firing after ~30 minutes with Smartsheet reachable → hand Claude the Correlation_ID to diagnose.

**See also:** runbook `docs/runbooks/compile_now_poll.md`

#### A CRITICAL names ONE job whose compile-now trigger scan has failed for many consecutive cycles. The row comes in TWO wordings that mean opposite things — read which one you have first.

**Resolution class:** Operator-resolvable (solo)

**Signals:** compile_now_job_scan_sustained, compile_now_poll.job_scan_failed, this job's week sheet is unreachable, BROAD scan outage, NOT a per-job fault

**Checks (in order):**
- FIRST — which wording? "while other jobs scan fine" = an isolated per-job fault; continue below. "as did EVERY other scanned job (N/N) … BROAD scan outage, NOT a per-job fault" = nothing scanned at all (on a small deployment a total Smartsheet outage makes EVERY job look 20-cycles dead); none of the checks below apply — go to the compile-now scan-outage node and rename nothing.
- The row names the workstream, project, job ID and week — confirm that week sheet exists in that project's Jobs folder in the matching workspace.
- Compare the Project Name in ITS_Active_Jobs / ITS_Active_Jobs_Progress against the Smartsheet folder name; a rename is the usual cause.
- A compile_now_scan_sustained CRITICAL alongside an isolated-fault row does NOT mean a broad outage — on a small deployment one broken job is already half the scanned jobs, so both rows fire for the SAME fault. The isolated-fault row is the specific one; work it first. Decide by the WORDING, never by counting named jobs.

**Resolutions (in order):**
- Isolated-fault wording only. If a folder or week sheet was renamed, rename it back to match ITS_Active_Jobs; if the job row's Project Name was edited by mistake, restore it. The rows stop on the next successful scan (~90s).
- The CRITICAL re-pages at 20, 40, 80, 160 and then every 160 consecutive cycles (320, 480 …), not every cycle; in between the same fault writes an ERROR compile_now_poll.job_scan_failed row carrying "next CRITICAL at N consecutive cycle(s)". Recovery is proven by those ERROR rows stopping.
- Never set the job Inactive to silence it — that silently stops its reports.
- A missing or deleted week sheet, or anything that looks like sharing/permissions, escalates to Seth (re-creating or re-sharing a week sheet is not a Tier-2 repair).

**See also:** runbook `docs/runbooks/compile_now_poll.md`

### Human approves a WSR row; weekly-send transmits it (send half)

| What happens | |
|---|---|
| Daemon | `weekly-send` |
| Sheets | `WSR_human_review`, `ITS_Active_Jobs` |
| Config gates | `safety_reports.weekly_send.polling_enabled` |

**Healthy signals:**
- An approved row (Send Now / Approve for Scheduled Send) is sent; Sent At + Send Status are stamped.

#### An approved packet is not sent; Send Status shows a HELD reason.

**Resolution class:** Operator-resolvable (solo)

**Signals:** held_no_recipient, held_missing_pdf, held_missing_envelope

**Checks (in order):**
- Dashboard / the WSR row → read the Send Status. Recipients resolve at send time from ITS_Active_Jobs.
- held_no_recipient → the job's safety-reports contact is empty; held_missing_pdf → the compiled packet is absent; held_missing_envelope → addressing metadata is missing.

**Resolutions (in order):**
- Fix the job's contact in ITS_Active_Jobs (held_no_recipient), re-compile the week (held_missing_pdf), or supply the missing envelope field, then re-approve. HELD is an operator-actionable stop, never a silent drop.

**See also:** runbook `docs/runbooks/safety_weekly_send.md` · watchdog `_check_stale_held_rows`

#### A packet is held for size or a workstream-tag mismatch.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** held_oversized_packet, held_workstream_mismatch, held_failed

**Checks (in order):**
- held_oversized_packet → the compiled packet exceeds the upload-session ceiling; held_workstream_mismatch → the row's Workstream tag is not "safety" (contamination guard, fires a CRITICAL); held_failed → transient send failures exhausted retries.

**Resolutions (in order):**
- For oversized, reduce the packet (fewer/lighter photos) and re-compile. A workstream mismatch is a contamination guard — escalate (it should never happen on a correctly-seeded row). held_failed → investigate the transient error and re-approve.

**See also:** runbook `docs/runbooks/safety_weekly_send.md`

#### A row is stuck in the SENDING state (write-ahead marker set, no terminal status).

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** stuck SENDING, SENDING with no Sent At

**Checks (in order):**
- Was there a crash between the SENDING marker and the send completing?

**Resolutions (in order):**
- Follow the send runbook to clear the stuck marker safely; do not blindly re-send (double-send risk).

**See also:** runbook `docs/runbooks/safety_weekly_send.md` · watchdog `_check_stuck_wsr_send`

#### Sends are blocked, or the who-may-approve set changed unexpectedly.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** EMPTY_ALLOWLIST, approver drift, F22 fail-closed

**Checks (in order):**
- Is the send workspace's share list empty or changed (§46 authorization-by-workspace-share)?

**Resolutions (in order):**
- Re-share the approver(s) into the send workspace. A change to who-may-approve is auth territory — confirm with Seth.

**See also:** watchdog `_check_approver_drift`

## Progress report — intake, routing, compile, send

The Safety-Reports twin for weekly progress packets, on its own Active-Jobs sheet.

### Progress submissions are ingested and routed

| What happens | |
|---|---|
| Sheets | `ITS_Active_Jobs_Progress` |
| Config gates | `progress_reports.intake_enabled` |

**Healthy signals:**
- Progress submissions route to the right job/week; the intake gate is on.

#### No progress items are being ingested.

**Resolution class:** Operator-resolvable (solo)

**Signals:** progress intake gate off, designed-dark

**Checks (in order):**
- Read progress_reports.intake_enabled — NOTE it is scoped under Workstream=safety_reports (intake's own workstream), a known footgun.

**Resolutions (in order):**
- If ingestion is intended, flip the gate (read its Description first); if it is dark by design, this is not a fault.

**See also:** runbook `docs/runbooks/progress_intake_routing.md`

### progress-generate compiles the weekly progress packet

| What happens | |
|---|---|
| Daemon | `progress-generate` |
| Sheets | `ITS_Active_Jobs_Progress`, `WPR_human_review` |

**Healthy signals:**
- Friday 14:30 the progress compile runs (staggered 30 min after safety); a PENDING WPR row appears.

#### The Friday progress compile did not run.

**Resolution class:** Operator-resolvable (solo)

**Signals:** missed progress-generate, no WPR row

**Checks (in order):**
- Was the progress-generate marker written this week? Did the catch-up fire on wake?

**Resolutions (in order):**
- The watchdog progress catch-up recovers a missed Friday; otherwise run the documented manual re-run.

**See also:** runbook `docs/runbooks/progress_weekly_generate.md` · watchdog `_check_progress_generate_catchup`

### progress-send transmits an approved WPR row

| What happens | |
|---|---|
| Daemon | `progress-send` |
| Sheets | `WPR_human_review`, `ITS_Active_Jobs_Progress` |
| Config gates | `progress_reports.progress_send.polling_enabled` |

**Healthy signals:**
- An approved WPR row is sent; recipients resolve only from ITS_Active_Jobs_Progress (never safety's set).

#### An approved progress packet is not sent (HELD or blocked).

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** held_no_recipient, EMPTY_ALLOWLIST, F22 fail-closed

**Checks (in order):**
- Read the WPR Send Status; confirm the progress workspace approver share list is non-empty (§46).

**Resolutions (in order):**
- Fix the job's progress contact or the approver share, then re-approve. Auth/approver changes → confirm with Seth.

**See also:** runbook `docs/runbooks/progress_send.md`

## Field-ops sync — portal job/hours/materials/equipment/incidents to Smartsheet

The portal is the writer of record for jobs and field capture; fieldops-sync mirrors dirty portal-origin records UP into the two Active-Jobs sheets and the standing trackers.

### A job / hours / materials record is created or edited in the portal

| What happens | |
|---|---|
| Worker route | `POST /api/fieldops/*` |

**Healthy signals:**
- The record lands in D1 with origin='portal', sync_state='pending', and a bumped mirror_version.

#### A crew/task record shows a username instead of a display name.

**Resolution class:** Operator-resolvable (solo)

**Signals:** username shown where a name belongs, attribution wrong

**Checks (in order):**
- Confirm the WHO field resolves through personnel.name, not users.username.

**Resolutions (in order):**
- Correct the personnel record / mapping per the job-management runbook.

**See also:** runbook `docs/runbooks/safety_portal_job_management.md`

#### A manager cannot mark a delivery on a job's Materials page, or a line shows the wrong received total, or a line still shows a problem after the goods arrived.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** no Delivered / Partially delivered buttons, forbidden_job, running total looks wrong, still shows Problem reported

**Checks (in order):**
- Marking needs cap.materials.receive AND a manager/admin role — a submitter holds the capability but not the role (the same gate the daily report uses).
- Non-admins only reach the job they are PLACED on (personnel.current_job); a 403 forbidden_job is the designed scope, not a fault.
- The received total is the SUM over an append-only ledger — open the line's history, which shows every event with its quantity, note, date and who recorded it.
- The incident flag is STICKY by design; a delivery mark records the delivery without clearing the flag.

**Resolutions (in order):**
- Fix the role in Accounts or the placement on the job's crew, per the runbook.
- A duplicate or mistaken ledger event is corrected by recording a FURTHER event — the ledger is append-only and has no delete path.
- Correcting a total at source, or clearing a stuck incident flag, is Seth's — escalate.

**See also:** runbook `docs/runbooks/job_materials.md`

### fieldops-sync mirrors dirty jobs UP into both Active-Jobs sheets

| What happens | |
|---|---|
| Daemon | `fieldops-sync` |
| Sheets | `ITS_Active_Jobs`, `ITS_Active_Jobs_Progress` |
| Config gates | `field_ops.fieldops_sync.sync_enabled` |

**Healthy signals:**
- The fieldops-sync heartbeat is fresh (this daemon is live); dirty jobs mirror within a cycle.

#### fieldops-sync is not mirroring; a CRITICAL fired.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** fail-closed missing base URL/bearer, 401 on pending-jobs, ERROR heartbeat

**Checks (in order):**
- Is the Worker base URL set and the field-ops bearer (ITS_PORTAL_FIELDOPS_TOKEN, distinct from portal_poll's token) present?
- Do BOTH Active-Jobs sheets have the "Portal Job Key" column (a missing column KeyErrors add_rows)?

**Resolutions (in order):**
- A missing column is a documented low-class fix (add it); a token/auth failure is auth territory — escalate.

**See also:** runbook `docs/runbooks/fieldops_sync.md`

#### A job did not mirror or wrote to the wrong row.

**Resolution class:** Operator-resolvable (solo)

**Signals:** job write conflict, version-vector mismatch

**Checks (in order):**
- Compare mirror_version vs the safety/progress mirrored-version watermarks for the job.

**Resolutions (in order):**
- Follow the job-write runbook; the sync is idempotent + crash-safe, so a re-run is safe.

**See also:** runbook `docs/runbooks/fieldops_job_write.md`

### Hours / equipment / materials / incidents / receipts mirror passes

| What happens | |
|---|---|
| Daemon | `fieldops-sync` |
| Config gates | `field_ops.fieldops_sync.hours_enabled`, `field_ops.fieldops_sync.equipment_enabled`, `field_ops.fieldops_sync.materials_enabled`, `field_ops.fieldops_sync.incidents_enabled`, `field_ops.fieldops_sync.receipts_enabled` |

**Healthy signals:**
- Each enabled pass mirrors its records; the Hours Log is one-way-up (§51).

#### A tracker (hours/equipment/materials/incidents/receipts) is not mirroring.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** per-pass gate off, designed-dark, materials gate has a rider precondition

**Checks (in order):**
- Read the per-pass gate row's FULL Description before flipping — some carry an explicit precondition (e.g. materials_enabled's §51-rider block). A dark pass is not a fault.

**Resolutions (in order):**
- Flip the gate only if its documented precondition is met; a doctrine-preconditioned flip is a §44 high-class action — confirm with Seth.

**See also:** runbook `docs/runbooks/hours_log_sync.md`

#### A delivery mark made in the portal never appears on the job's Material Receipts sheet, or a line's Line Qty Received / Line Status stops moving.

**Resolution class:** Operator-resolvable (solo)

**Signals:** material_receipts_perjob_failed, material_receipts_row_cap_check_failed, material_receipts_sheet_race_duplicate, receipts pass gate off

**Checks (in order):**
- The ledger re-projects IN FULL every cycle (no watermark), so a transient fault self-heals on the next cycle — wait one 90s cycle before treating it as a fault.
- Two columns (Line Status, Line Qty Received) are DERIVED and legitimately change after an event lands; a moving value is the rollup working, not corruption.
- Check field_ops.fieldops_sync.receipts_enabled, then the error journal for the codes above.

**Resolutions (in order):**
- Gate off → flip it back on (no documented precondition); rows catch up on the first cycle back. A permanent per-event failure opens a Review Queue row naming the job — follow it.
- Duplicate rows after a create race → keep the daemon's row (the one that keeps updating); the duplicate is inert. Escalate only if duplicates keep appearing.

**See also:** runbook `docs/runbooks/fieldops_sync.md`

#### A crew time entry needs an office-side correction/amendment.

**Resolution class:** Operator-resolvable (solo)

**Signals:** time amendment needed, crew correction

**Checks (in order):**
- Identify the entry and the correct value with the crew lead.

**Resolutions (in order):**
- Apply the amendment via the documented time-amend procedure.

**See also:** runbook `docs/runbooks/fieldops_time_amend.md`

### manifest-poll screens, parses, and files an uploaded BOM / shipping log

| What happens | |
|---|---|
| Daemon | `manifest-poll` |
| Worker route | `GET /api/fieldops/manifests/internal/pending` |
| Sheets | `ITS_Review_Queue` |
| Config gates | `field_ops.manifest_poll.polling_enabled`, `po_materials.po_attach_screen.clamav_enabled` |

**Healthy signals:**
- With the gate on, an office-uploaded manifest is HMAC- and digest-verified, §34-screened, read in a killable sandbox child, filed to Box (job → Materials → Manifests), and lands `parsed` with a reviewable grid + proposed column map for the validate screen. It never commits a line by itself.

#### Uploaded manifests are not being pulled or parsed.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** manifest-poll gate off, designed-dark, no marker written, manifest_creds_missing

**Checks (in order):**
- Is manifest-poll loaded AND field_ops.manifest_poll.polling_enabled flipped? A loaded-but-dark daemon writes no marker by design.
- If both are on, look for manifest_creds_missing (CRITICAL) — the daemon is fail-closed and will not poll half-configured.

**Resolutions (in order):**
- Load the plist and flip the gate together; a first activation is a §44 capability action — confirm with Seth. Missing credentials always escalate.

**See also:** runbook `docs/runbooks/material_manifest_import.md`

#### An uploaded manifest was refused (unreadable or MALICIOUS), failed integrity, or imported with an active-content warning.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** manifest_unreadable, manifest_active_content, manifest_malicious, manifest_integrity_failed

**Checks (in order):**
- Read the ITS_Review_Queue row (refusals) or the validate screen's parse notes (warnings). An unreadable document (a scan, an empty export, no header row) is ORDINARY — ask the office for a text-based PDF or the source spreadsheet.
- manifest_active_content is a WARNING, not a refusal (disposition changed 2026-08-11, this lane only) — the document imported; the note says to take care opening the ORIGINAL from Box. Nothing to repair.
- A security_flag row is not a readability problem; an integrity failure means the bytes disagree with what was signed.

**Resolutions (in order):**
- For a readability refusal only, clear that manifest's id from state/manifest_poll_flagged.json to retry. NEVER clear a flag for a MALICIOUS verdict or an integrity failure — both escalate, and the bytes are retained for investigation.

**See also:** runbook `docs/runbooks/material_manifest_import.md`

#### The office's import from the validate screen errors, or the manifest shows "committing" after an interrupted import.

**Resolution class:** Operator-resolvable (solo)

**Signals:** not_committable, line_cap_exceeded, invalid_material_id, ambiguous_unresolved, manifest_worker_rejected

**Checks (in order):**
- These are Worker refusals of the COMMIT half (the human dispose), not daemon faults — the daemon's work already finished when the grid appeared.
- ambiguous_unresolved means a part number matches more than one existing line and the office hasn't picked a target — a merge or shipments import refuses rather than guessing.
- The preview's projected totals are per-mode (add-vs-merge); line_cap_exceeded means genuinely NEW lines would push the job past its cap.

**Resolutions (in order):**
- not_committable / invalid_material_id → re-open the validate screen and retry from a fresh load; if it persists, the manifest's status no longer allows a commit — escalate rather than forcing it.
- ambiguous_unresolved → run the preview and decide each listed part number, then import again.
- An import interrupted mid-way is RESUMABLE (re-importing continues from the watermark) or ABANDONABLE (Discard works on a "committing" manifest; lines from pages that already imported stay on the job's list — the screen says so).
- manifest_worker_rejected in ITS_Errors means the Worker permanently refused the daemon's grid post (a 4xx) — the manifest is one-shot-flagged; remediate via the runbook's flag-file procedure after the cause is fixed.

**See also:** runbook `docs/runbooks/material_manifest_import.md`

### The archive pass relocates a closed job's seven containers (and brings them back)

| What happens | |
|---|---|
| Daemon | `fieldops-sync` |
| Worker route | `GET /api/internal/fieldops/archive-pending (the daemon's work queue) + GET /api/internal/fieldops/archive-health (watchdog Check X's read-only view, which unlike the queue can see the terminal partial/failed states)` |
| Config gates | `field_ops.fieldops_sync.archive_enabled`, `field_ops.box.archive_root_folder_id` |

**Healthy signals:**
- Pressing Archive in the portal records intent only; within a cycle or two the card reads "Archived. All 7 folders are filed under ITS — Archive / <Job>".
- Four Smartsheet folders (Safety, Progress, Purchase Orders, Subcontracts) and three Box folders (Safety, Progress, Purchase Orders) are relocated — a move, never a delete; ids, permalinks and history are preserved.
- The cycle summary reads `archive complete=N partial=0 failed=0 capped=0 errors=0`; no `job_archive` rows in ITS_Errors.
- Watchdog Check X (daily) reports `Job archives healthy` — no job stopped at partial/failed and none aging in the queue.

#### An Archive press never moves anything; the portal still says "Archiving… Waiting for the office Mac to pick this up."

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** archive pass gate off, designed-dark, no ITS_Errors row at all, stale fieldops_sync heartbeat, Job archive(s) not progressing, STUCK in the queue past

**Checks (in order):**
- Is field_ops.fieldops_sync.archive_enabled on, AND field_ops.fieldops_sync.sync_enabled (the whole daemon) too? A dark pass runs silently by design — no log, no error.
- Is the fieldops_sync heartbeat fresh in ITS_Daemon_Health, and is system.state ACTIVE (not PAUSED/MAINTENANCE)?
- Reload the page before believing it — the Archive card stops self-refreshing after about ten minutes while still saying it updates itself.

**Resolutions (in order):**
- Nothing is lost and no re-press is needed — the job stays queued and is picked up on the first cycle after the cause is fixed.
- Reloading the launchd job is low-class; turning a gate ON is a capability activation — confirm with Seth.

**See also:** runbook `docs/runbooks/job_archive.md` · watchdog `_check_stale_job_archives`

#### An archive stopped at "Partly archived — N of 7 folders moved" or "Nothing moved", and does not resume.

**Resolution class:** Operator-resolvable (solo)

**Signals:** archive_state partial, archive_state failed, archive_container_failed, DEGRADED cycle, N archive(s) STOPPED, will not resume without a re-press, Job archive(s) not progressing, N STOPPED (partial/failed

**Checks (in order):**
- partial/failed are TERMINAL for the daemon (the queue serves only requested/in_progress) — NOTHING resumes it until a human presses "Try again".
- Read ITS_Errors Script `job_archive`, code `archive_container_failed` — it names the job, the SYSTEM and the container. The panel alone cannot tell Smartsheet's "Safety"/"Progress" from Box's.
- Three Box containers stuck (4 of 7) points at a Box root config row; all seven stuck points at something common (permissions, a system-wide outage).

**Resolutions (in order):**
- For a plain transient, press Try again in the portal — it re-raises the request and resets the attempt counter. All seven are re-attempted; the already-moved ones report "nothing to move", so only the stuck ones actually do work.
- Never drag the folders by hand — a folder moved out of band is invisible to the pass and to every find-or-create path, which splits the job across two folders.
- The same container failing twice through "Try again", or a named cause (Box root, ADMIN shortfall), escalates.

**See also:** runbook `docs/runbooks/job_archive.md`

#### No job archives at all, or exactly the three Box folders fail while the four Smartsheet ones succeed.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** archive_preflight_not_admin, archive_preflight_unreadable, fieldops_archive_fetch_auth_failed, Box root unset, 4 of 7 moved

**Checks (in order):**
- archive_preflight_not_admin (Script `job_archive`) — the ITS Smartsheet identity lacks ADMIN on one of the five workspaces, so the pass skips loudly and moves NOTHING. Every queued job stays queued; no re-press needed once the share is fixed.
- Box-only failures — read field_ops.box.archive_root_folder_id (and the three source roots, safety_reports/progress_reports/po_materials .box.portal_root_folder_id). An unset root REFUSES rather than reporting an empty tree as archived.
- fieldops_archive_fetch_auth_failed (CRITICAL) — the field-ops bearer was rejected; nothing was relocated and nothing is lost.

**Resolutions (in order):**
- Report which workspace and access level the pre-flight named; workspace sharing is approval authority (§46) and token identity is auth — both escalate.
- A Box root id comes from build_box_roots.py and a WRONG one files documents into the wrong tree without failing — setting it is Seth's. Afterwards press "Try again"; already-moved containers report "nothing to move".

**See also:** runbook `docs/runbooks/job_archive.md`

#### An un-archive refuses — a folder already holds the job's name in the live tree.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** archive_container_failed on unarchive, already holds that name in the live tree, no merge primitive, this collision branch has never fired for real (issue #42)

**Checks (in order):**
- The job was written to while archived, so an ordinary find-or-create re-grew its live folder. Neither Smartsheet nor Box can merge two folders, so the pass refuses loudly rather than fusing them — the refusal is correct.
- Look at both homes (the live <Job> folder and ITS — Archive / <Job> / <Workstream>) and note what is in each. Change nothing.
- The Try again button on the red card raises an ARCHIVE request, not a resumed restore — recoverable (the job returns to fully archived) but read the confirmation modal.

**Resolutions (in order):**
- Reconciling two folders is hand work in two external systems with no undo; escalate. Both directions were drilled live 2026-08-10 (issue

**See also:** runbook `docs/runbooks/job_archive.md`

## Purchase order — build, config, pull/render/file, send

The deterministic PO pipeline (no AI). Ships dark until its gates are flipped.

### A PO is built + signed in the portal

| What happens | |
|---|---|
| Worker route | `POST /api/po/submit` |

**Healthy signals:**
- The draft lands in the send-free D1 pool with a po:v1 HMAC binding its cents totals.

#### A PO is refused at pull with a totals mismatch.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** totals_mismatch, one-shot-flagged, CRITICAL naming the account

**Checks (in order):**
- The Mac recomputes cents totals and asserts them vs the signed values; a mismatch is flagged, never rendered/filed.

**Resolutions (in order):**
- Inspect the flagged row (kept in D1 for forensics) and confirm whether it is a client error or tampering — escalate a systemic mismatch.

**See also:** runbook `docs/runbooks/po_poll.md`

### PO purchaser / tax / terms config drives the render

**Healthy signals:**
- The bundled purchaser/tax/terms JSON matches what the operator edited (served-equals-source).

#### A PO renders with stale purchaser/tax/terms after an edit.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** config edited but Worker still stale, redeploy pending

**Checks (in order):**
- Did the §50 config-actuator pipeline re-bundle + redeploy the Worker after the edit (the Worker bundles config at build time)?

**Resolutions (in order):**
- The config change flows through the config-actuator rail (enqueue → CI → deploy); wait for the deploy, or see the config-change workflow.

**See also:** runbook `docs/runbooks/operator_dashboard_config_editor.md`

### po-poll verifies, renders, files, and logs the PO

| What happens | |
|---|---|
| Daemon | `po-poll` |
| Worker route | `GET /api/po/internal/pending` |
| Sheets | `PO_Log`, `PO_Pending_Review` |
| Config gates | `po_materials.po_poll.polling_enabled`, `po_materials.po_poll.vendors_sync_enabled`, `po_materials.po_attach_screen.clamav_enabled` |

**Healthy signals:**
- With the gate on, drafts render to Box + a PO_Log row + a PO_Pending_Review row; a mark-filed receipt lands last.

#### POs are not being pulled/filed.

**Resolution class:** Operator-resolvable (solo)

**Signals:** po-poll gate off, designed-dark, no marker written

**Checks (in order):**
- Is po-poll loaded AND at least one po_materials.po_poll.* gate flipped? A loaded-but-all-dark daemon writes no marker by design.

**Resolutions (in order):**
- If PO processing is intended, load the plist and flip the gate; otherwise it is dark by design (not a fault).

**See also:** runbook `docs/runbooks/po_poll.md`

#### A PO attachment is refused (SUSPICIOUS/MALICIOUS) or the attachment pass errored.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** po_attachment_service_failed, attachment refused before filing

**Checks (in order):**
- The attachment pass is fenced and can never block PO filing; inspect the Review-Queue row for a refused attachment.

**Resolutions (in order):**
- Confirm the attachment with the buyer; a malicious disposition names the account and is refused by design. Escalate suspected compromise.

**See also:** runbook `docs/runbooks/po_poll.md`

### estimate-poll screens, classifies, and files an uploaded vendor estimate

| What happens | |
|---|---|
| Daemon | `estimate-poll` |
| Worker route | `GET /api/po/estimates/internal/pending` |
| Sheets | `Estimate_Log`, `ITS_Review_Queue` |
| Config gates | `po_materials.estimate_poll.polling_enabled`, `po_materials.po_attach_screen.clamav_enabled` |

**Healthy signals:**
- With the gate on, an office-uploaded estimate is HMAC-verified, §34-screened, doc-type-classified, filed to Box (PO root → job → Vendor Quotes) + an Estimate_Log row, and lands needs_review with page previews for the disposition screen.

#### Uploaded estimates are not being pulled/filed.

**Resolution class:** Operator-resolvable (solo)

**Signals:** estimate-poll gate off, designed-dark, no marker written

**Checks (in order):**
- Is estimate-poll loaded AND po_materials.estimate_poll.polling_enabled flipped? A loaded-but-dark daemon writes no marker by design (ships dark until the E2 go-live).

**Resolutions (in order):**
- If estimate import is intended, load the plist and flip the gate (go-live is done with Seth); otherwise it is dark by design (not a fault).

**See also:** runbook `docs/runbooks/estimate_import_path.md`

#### An uploaded estimate is refused (wrong doc type, SUSPICIOUS/MALICIOUS screen, or an integrity failure).

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** wrong_doc_type, screen refused before filing, estimate_integrity_failure, one-shot-flagged

**Checks (in order):**
- An invoice/A/P report refusal is BY DESIGN (never parsed as PO line items) — verify the document really is one; a screen/integrity refusal has a Review-Queue row naming the reason.

**Resolutions (in order):**
- Wrong-doc-type on a real invoice → resolve the row and route the document to accounts; a misclassified quote is re-uploaded. MALICIOUS or integrity failures escalate (security, FIXED high-class).

**See also:** runbook `docs/runbooks/estimate_import_path.md`

### rfq-poll renders and files a composed request-for-quote per vendor

| What happens | |
|---|---|
| Daemon | `rfq-poll` |
| Worker route | `GET /api/po/rfqs/internal/pending` |
| Sheets | `RFQ_Log`, `RFQ_Pending_Review`, `ITS_Vendors`, `ITS_Review_Queue` |
| Config gates | `po_materials.rfq_poll.polling_enabled` |

**Healthy signals:**
- With the gate on, a composed RFQ is rfq:v1 HMAC-verified, rendered once per vendor as a PRICE-FREE PDF (ITS_Vendors snapshot), filed to Box (PO root → job → RFQs) + an RFQ_Log (rfq, vendor) row, staged on RFQ_Pending_Review (PENDING, Workstream po_materials_rfq), and receipted back once; SENT stamps mirror back via status-sync.

#### Composed RFQs are not being pulled/filed.

**Resolution class:** Operator-resolvable (solo)

**Signals:** rfq-poll gate off, designed-dark, no marker written

**Checks (in order):**
- Is rfq-poll loaded AND po_materials.rfq_poll.polling_enabled flipped? A loaded-but-dark daemon writes no marker by design (ships dark until the R2 go-live).

**Resolutions (in order):**
- If RFQ generation is intended, build the two RFQ sheets, load the plist, and flip the gate (go-live is done with Seth); otherwise it is dark by design (not a fault).

**See also:** runbook `docs/runbooks/rfq_generation_path.md`

#### An RFQ (or one vendor's copy) is refused / missing.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** rfq_hmac_failure, rfq_vendor_unknown, rfq_all_vendors_fenced, one-shot-flagged

**Checks (in order):**
- An unknown-vendor fence names the Vendor Key in its Review-Queue row. The vendors that resolved do file, but the RFQ itself stays queued and converges to a vendors_fenced flag — expect 2-3 PENDING rows for one RFQ, which is the convergence and not a second fault. A deactivated vendor is NOT this case (the resolver matches the key alone and reads no status column). If EVERY vendor is fencing, ITS_Vendors is empty rather than missing one row — a seeding activation step, see po_poll Symptom 10. An HMAC failure is a security event with a CRITICAL + security-flagged row.

**Resolutions (in order):**
- Unknown vendor → add/fix the ITS_Vendors row FIRST, then clear that RFQ's vendors_fenced entry from state/rfq_poll_flagged.json (order is load-bearing — unflagging first re-serves into an unchanged sheet and just re-fences). The original RFQ then re-serves and files the missing copy itself; do NOT compose a replacement (it double-files) — cancel it in the portal if unwanted. Resolve the leftover PENDING rows afterwards or they trip Check A. HMAC entries are NEVER cleared and escalate (security, FIXED high-class).

**See also:** runbook `docs/runbooks/rfq_generation_path.md`

### po-send transmits an approved PO

| What happens | |
|---|---|
| Daemon | `po-send` |
| Sheets | `PO_Pending_Review`, `ITS_Vendors` |
| Config gates | `po_materials.po_send.polling_enabled` |

**Healthy signals:**
- An approved PO_Pending_Review row is sent from procurement@; recipients resolve from ITS_Vendors.

#### An approved PO is not sent.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** held_no_recipient, EMPTY_ALLOWLIST, po-send gate off (fail-safe default false)

**Checks (in order):**
- Is po-send loaded and its gate on? (The gate defaults false so a missing row fails safe.)
- Is the ITS — Purchase Orders workspace share list (the §46 approver set) non-empty?

**Resolutions (in order):**
- Load/flip the gate if sending is intended; re-share approvers. Auth changes → confirm with Seth.

**See also:** runbook `docs/runbooks/po_send.md`

### rfq-send transmits an approved request-for-quote to its vendor

| What happens | |
|---|---|
| Daemon | `rfq-send` |
| Sheets | `RFQ_Pending_Review`, `ITS_Vendors` |
| Config gates | `po_materials.rfq_send.polling_enabled` |

**Healthy signals:**
- An approved RFQ_Pending_Review row is sent from procurement@ with TWO attachments — the price-free RFQ PDF + the fillable xlsx quote form; the recipient resolves from ITS_Vendors by Vendor Key. Tagged po_materials_rfq so po-send / subcontract-send can never dispatch it.

#### An approved RFQ is not sent.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** held_no_recipient, held_missing_envelope, held_workstream_mismatch, EMPTY_ALLOWLIST, rfq-send gate off (fail-safe default false)

**Checks (in order):**
- Is rfq-send loaded and its gate on? (The gate defaults false so a missing row fails safe; go-live is a FIXED External-Send-Gate flip done with Seth.)
- Is the ITS — Purchase Orders workspace share list (the §46 approver set, shared with POs) non-empty? Is the vendor's Contact Email present in ITS_Vendors?

**Resolutions (in order):**
- Fix the vendor email / restore the numberless-row Notes tag, then re-approve; re-share approvers. A contamination HELD or gate flip is Send-Gate class → Seth.

**See also:** runbook `docs/runbooks/rfq_send.md`

## Subcontract — build, legal gate, pull/render, send

The deterministic subcontract-package pipeline (no AI), PO-mirror. Ships dark.

### A subcontract is built (with Exhibit A / SoV) in the portal

| What happens | |
|---|---|
| Worker route | `POST /api/subcontracts/submit` |

**Healthy signals:**
- The draft lands in the send-free D1 pool with a sub:v1 HMAC; the SoV sums to the §2.1 Contract Price.

#### A subcontract is refused at pull with an SoV mismatch or fails the legal gate.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** SOV-mismatch, Layer-A legal gate, one-shot-flagged

**Checks (in order):**
- The Mac recomputes the SoV and asserts it vs the signed Contract Price; the Layer-A §50 legal gate must pass.

**Resolutions (in order):**
- Inspect the flagged row (kept in D1 for forensics); a legal-gate failure is doctrine/legal territory — escalate.

**See also:** runbook `docs/runbooks/subcontract_generation_path.md`

### subcontract-poll verifies and renders the package (three files)

| What happens | |
|---|---|
| Daemon | `subcontract-poll` |
| Worker route | `GET /api/subcontracts/internal/pending` |
| Sheets | `Subcontract_Log`, `Subcontract_Pending_Review` |
| Config gates | `subcontracts.subcontract_poll.polling_enabled` |

**Healthy signals:**
- With the gate on, the package renders as editable .docx/.xlsx (Subcontract + Exhibit A + Annex C SoV) to Box + logs a row.

#### Subcontracts are not being pulled/rendered.

**Resolution class:** Operator-resolvable (solo)

**Signals:** subcontract-poll gate off, designed-dark

**Checks (in order):**
- Is subcontract-poll loaded and its gate flipped? Check the CURRENT value of subcontracts.subcontract_poll.polling_enabled — do not assume; these gates shipped false but are flipped as each lane goes live.

**Resolutions (in order):**
- Load + flip if intended; if the gate is deliberately off, leave it.

**See also:** runbook `docs/runbooks/subcontract_generation_path.md`

### subcontract-send transmits an approved package

| What happens | |
|---|---|
| Daemon | `subcontract-send` |
| Sheets | `Subcontract_Pending_Review` |
| Config gates | `subcontracts.subcontract_send.polling_enabled` |

**Healthy signals:**
- An approved subcontract package is sent through the shared send lane; recipients resolve from ITS_Subcontractors.

#### An approved subcontract package is not sent.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** held_no_recipient, EMPTY_ALLOWLIST, subcontract-send gate off

**Checks (in order):**
- Is subcontract-send loaded and its gate on? Is the subcontracts send-workspace approver share non-empty (§46)?

**Resolutions (in order):**
- Load/flip if intended; re-share approvers. Auth/send-gate territory → confirm with Seth.

**See also:** runbook `docs/runbooks/subcontract_send.md`

## Email intake — the superseded safety path (portal PULL is canonical)

Safety email intake was RETIRED; the Safety Portal PULL model supersedes it. The shared Graph plumbing is preserved for a future Email-Triage workstream.

### An email arrives at a former intake address

**Healthy signals:**
- Nothing is expected to happen — the portal PULL model is the live safety intake path.

#### An email to the old safety intake address is not processed.

**Resolution class:** Operator-resolvable (solo)

**Signals:** no safety-intake daemon, portal PULL supersedes email

**Checks (in order):**
- Confirm there is no loaded safety-intake job (there is not — the poller was retired).

**Resolutions (in order):**
- Direct the sender to the portal. This is the designed state, not a fault. A future Email-Triage workstream would re-enable an allowlisted email path.

## Config change — the §50 privileged actuation rail

The cloud can only ENQUEUE a config request (send-free); the config-actuator commits it on the Mac (validate → PR → CI → merge → deploy → stamp live).

### An operator enqueues a config edit (dashboard / portal)

| What happens | |
|---|---|
| Worker route | `POST /api/internal/config` |

**Healthy signals:**
- The request lands in the config_requests queue (send-free), pending.

#### The config editor rejects an edit.

**Resolution class:** Operator-resolvable (solo)

**Signals:** validation error, editor rejected

**Checks (in order):**
- Confirm the edit conforms to the config schema (the editor validates before enqueue).

**Resolutions (in order):**
- Correct the edit per the config-editor runbook.

**See also:** runbook `docs/runbooks/operator_dashboard_config_editor.md`

#### A capability does nothing at all and the switch for it cannot be found anywhere in ITS_Config.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** load-bearing ITS_Config row(s) MISSING or BLANK, row MISSING, Value BLANK, config_row_missing, INVISIBLE off-switch, the capability is silently inert

**Checks (in order):**
- A missing row and a blank Value both read as `false` to a daemon (`_read_bool_setting(default=False)`), so the capability is inert with NO switch to flip — this is the 2026-08-10 three-day archive outage exactly.
- Watchdog Check Y (verify_cutover VC-03, run daily) names each absent row as `<setting> [<workstream>]` — note BOTH halves, ITS_Config is keyed on Setting AND Workstream.
- Read the row's Description in docs/references/its_config_dictionary.md before anything else — it may carry a doctrine precondition on activation.

**Resolutions (in order):**
- Seeding a missing ITS_Config row is a FIXED high-capability class — escalate to Seth with the row names; a Tier-2 operator does not create one.
- After the row is seeded, mark the open CRITICAL resolved — an open CRITICAL is never terminal, so Check B re-reports it forever otherwise.

**See also:** runbook `docs/runbooks/operator_dashboard_config_editor.md` · watchdog `_check_cutover_config`

### config-actuator validates, PRs, merges, and deploys the change

| What happens | |
|---|---|
| Daemon | `config-actuator` |
| Worker route | `GET /api/internal/config/pending` |
| Config gates | `po_materials.config_actuator.polling_enabled` |

**Healthy signals:**
- Each request advances validated → tested → live → archived; the Worker re-bundles the config JSON on deploy.

#### A config request is stuck pending / not deploying.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** deploy gate, unapplied D1 migrations, main CI red

**Checks (in order):**
- Is main-branch CI green on the latest commit (the four-part step-4 gate)?
- Does the deploy gate refuse ahead of unapplied remote D1 migrations (it will not deploy the Worker ahead of the DB)?

**Resolutions (in order):**
- A red main CI or a needed migration/deploy is code/deploy territory — escalate; the actuator will unblock once the operator applies migrations and CI is green.

**See also:** runbook `docs/runbooks/config_actuator.md` · watchdog `_check_main_branch_ci_green`

## Operator dashboard — auth tiers and Class A/B/C actions

The localhost-only console; read-only panels plus PIN-gated actions over Tailscale.

### Operator authenticates (PIN → elevated re-PIN)

| What happens | |
|---|---|
| Daemon | `dashboard` |

**Healthy signals:**
- The dashboard answers on 127.0.0.1:8484 over Tailscale; PIN entry unlocks read panels, an elevated re-PIN + typed confirm unlocks ACT verbs.

#### The dashboard rejects the PIN or no ACT verb works.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** fail-closed until ITS_OPERATOR_PIN set, PIN lockout, elevated confirm required

**Checks (in order):**
- Is ITS_OPERATOR_PIN provisioned in Keychain? (The dashboard ships DARK / fail-closed until it is.)
- Is the request coming over an allowed origin (localhost / Tailscale)?

**Resolutions (in order):**
- Provision or change the PIN via the sensitive-tier procedure (in-dashboard change-PIN is current-PIN-gated). Secrets/PIN are auth territory — coordinate with Seth.

**See also:** runbook `docs/runbooks/operator_dashboard_sensitive_tier.md`

### Class A/B/C actions (config edit, daemon control, secret rotation)

| What happens | |
|---|---|
| Daemon | `dashboard` |

**Healthy signals:**
- Class-A ITS_Config edits enqueue via the §50 rail; Class-B daemon control runs launchctl; Class-C rotates secrets write-only.

#### A daemon-control action ran but the daemon still does nothing.

**Resolution class:** Operator-resolvable (solo)

**Signals:** launchctl ran but daemon dark, polling_enabled still false

**Checks (in order):**
- Daemon control is pure process management — starting a dark daemon does nothing until its polling_enabled gate is on.

**Resolutions (in order):**
- Flip the runtime gate (Class-A config edit) in addition to starting the process; read the gate's Description first.

**See also:** runbook `docs/runbooks/operator_dashboard_config_editor.md`

## The daemon plane — liveness, breaker, alerts, row-cap, picklists, guards

The shared infrastructure every workstream rides on: launchd, heartbeats + markers, the circuit breaker, the alert path, row-cap rotation, and the schema/guard sweeps.

### Heartbeats + watchdog Check C keep daemons honest

| What happens | |
|---|---|
| Daemon | `watchdog` |
| Sheets | `ITS_Daemon_Health`, `ITS_Errors` |

**Healthy signals:**
- Every live daemon's heartbeat row is fresh; the daily 07:00 watchdog reports no stale markers and no open CRITICALs.

#### A daemon stopped running and its marker went stale.

**Resolution class:** Operator-resolvable (solo)

**Signals:** stale Check-C marker, TRACKED_JOBS staleness

**Checks (in order):**
- Dashboard Daemon status → which marker is stale? Is the daemon dark by design (a loaded-all-dark daemon writes no marker) or genuinely dead?

**Resolutions (in order):**
- Re-run / kickstart the daemon if it is genuinely dead (documented, low-class); if it is dark by design, no action.

**See also:** watchdog `_check_scheduled_jobs`

#### There are open CRITICAL errors.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** open CRITICAL, ITS_Errors CRITICAL not resolved

**Checks (in order):**
- Dashboard Errors panel → read the open CRITICALs and their correlation IDs.

**Resolutions (in order):**
- Work each CRITICAL to resolution; mark resolved via the dashboard once handled.

**See also:** runbook `docs/runbooks/its_errors_triage.md` · watchdog `_check_open_criticals`

#### A daemon is running but has no ITS_Daemon_Health row.

**Resolution class:** Operator-resolvable (solo)

**Signals:** missing heartbeat row, self-provision failed

**Checks (in order):**
- Did the daemon fail to self-provision its health row (heartbeat writes never block primary work)?

**Resolutions (in order):**
- Follow the self-provision runbook; a heartbeat write failure logs to ITS_Errors and the daemon continues.

**See also:** runbook `docs/runbooks/daemon_health_self_provision.md`

### Circuit breaker + the alert path

| What happens | |
|---|---|
| Daemon | `watchdog` |

**Healthy signals:**
- The Smartsheet breaker is closed; alerts fire (triple-fire) and dedupe correctly.

#### The Smartsheet circuit breaker is open and writes are paused.

**Resolution class:** Operator-resolvable (solo)

**Signals:** circuit breaker open, prolonged-open, smartsheet_circuit_open_sustained

**Checks (in order):**
- Is it a transient storm (auto-recovers after cooldown) or a prolonged open with a real underlying cause?
- A `smartsheet_circuit_open_sustained` CRITICAL (Script `shared.smartsheet_client`) is the fleet-wide sub-hour page for the SAME condition — one page for the whole fleet, not one per daemon.

**Resolutions (in order):**
- For a transient storm past cooldown, clearing the local breaker state file is a documented low-class action. If the root cause is high-class (auth, deploy), escalate.
- The fleet circuit-open window needs no operator action — the first daemon to complete a successful Smartsheet read closes it.

**See also:** runbook `docs/runbooks/circuit_breaker.md` · watchdog `_check_circuit_breaker_prolonged_open`

#### Alerts went quiet during what looks like an incident.

**Resolution class:** Operator-resolvable (solo)

**Signals:** alerts-per-hour cap reached, rate-cap window

**Checks (in order):**
- Was the per-hour alert cap reached (it self-clears when the hour rolls over)?

**Resolutions (in order):**
- Diagnose/clear the underlying flapping script per the breaker/alerts runbook.

**See also:** runbook `docs/runbooks/circuit_breaker.md` · watchdog `_check_alert_rate_cap_window`

#### Expected alert-dedupe summaries did not appear.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** alert dedupe summary missing

**Checks (in order):**
- Check the alert-dedupe state/summary (watchdog Check G consumes it).

**Resolutions (in order):**
- The dedupe path fails open (extra emails OK, missed wake-ups not); investigate a systemic gap and escalate if wake-ups were missed.

**See also:** watchdog `_check_alert_dedupe_summaries`

### Sheet row-cap rotation (+ storm mode)

| What happens | |
|---|---|
| Daemon | `watchdog` |
| Sheets | `ITS_Errors`, `ITS_Review_Queue` |

**Healthy signals:**
- The two highest-churn sheets stay under the ~20,000-row wall; rotation deletes only TERMINAL rows.

#### A sheet is approaching / hit the row cap (HTTP 5634 class) and rotation is straining.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** row-cap rotation WARN, storm-mode 48h floor, 5634

**Checks (in order):**
- Dashboard/watchdog → is rotation keeping up? Did it fall back to the storm-mode 48h floor?

**Resolutions (in order):**
- Rotation runs automatically (deletes terminal rows, never open CRITICALs/PENDING). A CRITICAL means even the storm floor found nothing deletable — escalate.

**See also:** watchdog `_check_row_cap_rotation`

### On-disk log-directory rotation (archive, never delete)

| What happens | |
|---|---|
| Daemon | `watchdog` |
| Sheets | `ITS_Errors` |

**Healthy signals:**
- The ~/its/logs directory stays bounded; daily <YYYY-MM-DD>.log files older than 14 days are gzipped IN PLACE (never unlinked), and each launchd .out.log is copy→gz→truncated in place.

#### The watchdog reports the on-disk log directory is growing / rotation is straining, or disk is under pressure.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** log_dir_rotation WARN, log_dir_rotation CRITICAL, logs dir unbounded, disk pressure

**Checks (in order):**
- Dashboard/watchdog → did the log-dir rotation pass run this cycle, and how much did it archive? Is free disk low, or is ~/its/logs still growing after a run?

**Resolutions (in order):**
- v1 rotation ARCHIVES only — it gzips daily files in place and copy→gz→truncates launchd .out.log files; it NEVER deletes, so there is nothing destructive to undo. Re-run the watchdog (or preview with --dry) per the runbook. A persistently CRITICAL / wedged pruner, or a disk that keeps filling after a clean run, is code/capacity class — escalate.

**See also:** runbook `docs/runbooks/log_dir_rotation.md` · watchdog `_check_log_dir_rotation`

### Hourly picklist sync from master DBs

| What happens | |
|---|---|
| Daemon | `picklist-sync` |

**Healthy signals:**
- The hourly sync applies option changes; reference-checked removals block a delete rather than orphan a cell.

#### Picklist mappings failed to sync.

**Resolution class:** Operator-resolvable (solo)

**Signals:** ≥3 mappings failed (CRITICAL), reference-checked removal blocked

**Checks (in order):**
- Read the per-run ITS_Errors summary (examined/applied/skipped/blocked/failed).

**Resolutions (in order):**
- A blocked removal (live cell usage) is expected — reconcile per the drift runbook; ≥3 failures is a CRITICAL to investigate.

**See also:** runbook `docs/runbooks/picklist_drift_reconcile.md`

### Weekly picklist-drift audit

| What happens | |
|---|---|
| Daemon | `picklist-audit` |

**Healthy signals:**
- The Sunday audit reports no drift (column type, allowed-set, restrict-to-picklist toggle).

#### The weekly audit reports picklist drift.

**Resolution class:** Operator-resolvable (solo)

**Signals:** drift finding (exit 1), allowed-set mismatch, restrict toggle off

**Checks (in order):**
- Read the audit output; which column and which drift category?

**Resolutions (in order):**
- Reconcile additively per the drift runbook (dry-run by default); a registry/sheet disagreement may need a code change — escalate.

**See also:** runbook `docs/runbooks/picklist_drift_reconcile.md`

### Guard sweeps — blueprint symlinks + token write-capability

| What happens | |
|---|---|
| Daemon | `watchdog` |

**Healthy signals:**
- The blueprint .claude guard symlinks resolve; the Smartsheet token can write.

#### The Smartsheet token can no longer write.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** token write-capability probe failed, write scope lost

**Checks (in order):**
- Did the token-write probe fail (a scope/permission change)?

**Resolutions (in order):**
- Token scope/auth is a fixed high-class category — escalate; the developer re-scopes.

**See also:** runbook `docs/runbooks/token_write_capability.md` · watchdog `_check_token_write_capability`

#### The blueprint guard symlinks dangle.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** blueprint guard symlinks WARN

**Checks (in order):**
- Did the .claude guard symlinks fail to resolve (a moved/renamed blueprint checkout)?

**Resolutions (in order):**
- Restore the symlink targets; a WARN-only signal, but confirm the blueprint checkout with Seth (doctrine adjacency).

**See also:** watchdog `_check_blueprint_guard_symlinks`

## Publish + Box filing — form publish, Box token, portal prune

The form-publish actuator (C12=A), the Box document store the packets file into, and the portal-prune housekeeping.

### Packets + documents file into Box

**Healthy signals:**
- The Box OAuth token is fresh (rotates every exchange); filings land under the per-job / per-week folders.

#### Box uploads fail; the token is stale / near expiry.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** Box token stale, refresh-token freshness WARN, 60-day expiry risk

**Checks (in order):**
- Dashboard/watchdog → the box-token freshness marker. Box is a SINGLE-CONSUMER account — a second consumer (or a failed persist of the rotated token) breaks it within ~60 days.
- Never run a second Box consumer against the same account.

**Resolutions (in order):**
- The refresh token rotates every use and must be persisted; a stale/broken token is auth territory — escalate (re-run the guided OAuth setup).

**See also:** runbook `docs/runbooks/box_token_freshness.md` · watchdog `_check_box_token_freshness`

### publish-daemon actuates a form-publish request

| What happens | |
|---|---|
| Daemon | `publish-daemon` |
| Worker route | `GET /api/internal/publish/pending` |
| Config gates | `safety_reports.publish_daemon.polling_enabled` |

**Healthy signals:**
- A claimed publish request advances validated → tested → live → archived (PR → CI → merge → deploy → Box blank archive).

#### A form-publish request is stuck / stamped failed.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** publish deploy gate, stamped failed(stage), unapplied D1 migrations

**Checks (in order):**
- Did the deploy gate refuse ahead of unapplied D1 migrations? Which stage stamped failed?

**Resolutions (in order):**
- A failed publish stage fires a CRITICAL (never a silent stall); the fix (apply migrations, deploy, code) is code/deploy territory — escalate.

**See also:** runbook `docs/runbooks/safety_portal_forms.md`

### Portal prune housekeeping

**Healthy signals:**
- The portal prune keeps the D1 pools bounded; prune health is green.

#### Portal prune is not keeping D1 bounded / prune health is failing.

**Resolution class:** Escalate to Seth (co-resolve)

**Signals:** portal prune health WARN, D1 pool growth

**Checks (in order):**
- Dashboard/watchdog → the portal-prune health signal.

**Resolutions (in order):**
- Follow the prune-health runbook; a systemic prune failure (Worker/D1) is deploy territory — escalate.

**See also:** runbook `docs/runbooks/portal_prune_health.md` · watchdog `_check_portal_prune_health`
