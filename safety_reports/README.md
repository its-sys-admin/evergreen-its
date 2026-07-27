# Safety Reports — Phase 1 Active Build Target

Reference docs: **Safety Reports Mission v5** and **Safety Reports Brief v6** in the
planning project.

> **RETIRED 2026-06-05 — `intake_poll.py` (the safety email-intake poller).** The prose
> below that describes `intake_poll` as a live launchd polling daemon reading the `safety@`
> mailbox is **historical**. The safety intake is superseded by the Safety Portal **PULL**
> model (`portal_poll.py`, PLANNED; `decision_phase5-portal-transport`): the Worker queues
> submissions in D1; `portal_poll` pulls + HMAC-verifies + hands them to `intake.py`. The
> shared Graph plumbing is **preserved** for Email Triage. `intake.py` (the engine) stays.
> `WPR_Pending_Review` is decommissioned-by-doc (still used by the live weekly daemons
> pending the WSR rewire). See `CLAUDE.md` for the current-state table.

## Decision state (as of 2026-05-21)

- **5 resolved (2026-05-13)**: three intake document types (Q1), Outlook inbox addresses (Q3),
  ambiguous-report review surface routes to Teala Paradise (Q7), weekly cadence + gated send
  architecture (Q9), and WPR canonical template resolved-in-principle (Q2 — drafting deferred
  until inspection of mirror templates).
- **4 deferred-then-resolved (2026-05-21)**: see `workstreams/safety-reports/mission.md` in
  the its-blueprint repo for the full resolution. Q4 (job lookup = folder constants in
  `shared/sheet_ids.py`), Q5 (tracking row schema lives on `Daily Reports — Week of <date>`
  sheets in Field Reports tree, 9 columns), Q6 (Box taxonomy = `1111A (Copy for new projects)`
  template), Q8 (recipients in ITS_Config keyed `safety_reports.recipients.<job>` with
  JSON-list values). R3 session 1 (intake.py wiring) is unblocked.

## Three scripts — External Send Gate two-process model

Per Foundation Mission v8 Invariant 1, generation and send live in separate scripts:

- **`intake.py`** — `process_message(message_id)` runs the 12-stage pipeline per inbound
  message at `safety@evergreenmirror.com` (sandbox) / `safety@evergreenrenewables.com`
  (production). Sender allowlist enforced first (defense-in-depth on top of the Entra app's
  Application Access Policy + the operator-curated allowlist row); non-allowlist mail goes
  to Quarantine. Classifies into one of the 5 Daily Reports picklist categories (Daily JHA,
  Tool Box Talk, Equipment Check Sheets, Safe Work Observation, Other), extracts structured
  fields via Anthropic tool-use JSON-mode, files to Box, writes Daily Reports tracking row.
  **No customer-facing send capability** — `shared.graph_client` READ methods only
  (`get_message`, `list_attachments`, `download_attachment`); `send_mail` is AST-forbidden
  via `tests/test_capability_gating.py` + `tests/test_intake_capability_gating.py`.
- **`intake_poll.py`** — launchd-driven polling daemon (PR #59). Lists unread messages from
  the Graph mailbox each cycle, calls `intake.process_message`, calls `graph_client.mark_read`
  on success statuses. Replaces the prior Mail.app rule trigger. Maintains a 1000-entry FIFO
  seen-set at `~/its/state/safety_intake_processed.json` (defense-in-depth idempotency) and
  a heartbeat at `~/its/state/safety_intake_heartbeat.txt`. **Same capability gating as
  intake.py** — added to GATED_SCRIPTS.
- **`weekly_generate.py`** — SHIPPED (R3 Session 2). launchd-scheduled Friday 2:00 PM ET via
  `org.solutionsmith.its.weekly-generate.plist` (`StartCalendarInterval`, Weekday=5, Hour=14).
  Reads the week's Daily Reports + Weekly Rollup rows for each project, drafts a Weekly Project
  Report (WPR) via Anthropic Sonnet 4.6 with the `generate_weekly_project_report` tool schema
  (`schemas/safety_weekly_generate.json`), writes drafts to `WPR_Pending_Review` with
  `Approved for Send=false`. **No send capability** — `graph_client`, `send_mail`, `resend`,
  `smtplib`, `email.mime` AST-forbidden via `tests/test_capability_gating.py`. ZERO_DATA_WEEK
  branch writes placeholder rows when a project had no reports that week (reviewer decides
  whether to send-as-such, hold, or follow up with field PM). Idempotent: existing approved
  rows are skipped; existing unapproved rows have their `Draft Body` / `Recipients` / `Notes`
  replaced but approval columns never touched. Empty reviewer chain → CRITICAL abort, no
  Anthropic spend. Watchdog Check C tracks freshness via `safety_weekly_generate.last_run`
  marker (8-day per-job window).
- **`weekly_send.py`** + **`weekly_send_poll.py`** — SHIPPED (R3 Session 3). Per-event
  handler + polling daemon (default 15-min `StartInterval`, configurable via ITS_Config row
  `safety_reports.weekly_send.poll_interval_seconds`). Trigger model is polling (not static
  Monday cron) because approval is dynamic — Teala can approve Friday afternoon, Saturday
  morning, or Monday morning, and send-latency matters for sponsor experience.
  `weekly_send.send_one_row(row_id)` reads one `WPR_Pending_Review` row, validates state +
  Recipients, sends via `graph_client.send_mail` (content_type=Text for v0.1.0; HTML deferred),
  computes Late Send (informational only — never gates sending), updates row to
  `Send Status=SENT`. **No Anthropic API capability** — `anthropic_client`, `anthropic`
  AST-forbidden via `tests/test_capability_gating.py::SEND_SCRIPTS`. Refusal contract:
  `[GENERATION_FAILED:` tag refuses even when Approved (belt-and-suspenders); empty Recipients
  skips silently (the `[NO_RECIPIENTS]` design hold from weekly_generate). Retry-state
  tag-encoded in Notes column because the live sheet lacks dedicated `Send Retry Count` and
  `Last Send Error` columns — graceful-degrade per Op Stds v11 §23.3.

A separate `wpr_notify.py` runs at Friday 6:00 PM, Saturday 12:00 PM, Sunday 12:00 PM, and
Monday 6:00 AM ET to nag approvers about unapproved WPRs.

## Three intake document types

Field PMs submit three categories of safety-related documents:

1. **Daily safety brief + Daily Job Site Safety Worksheet (JSS).** Most frequent — every active
   workday.
2. **Machine pre-inspections.** Skid steer, lifts, other equipment. Per-machine, per-shift.
3. **Weekly toolbox talks.** Fridays. Documents attendance and topic; one per crew per week.

The intake script first classifies the inbound document by type, then runs the type-specific
extraction prompt. Misclassification routes to `ITS_Review_Queue` for human resolution
(reviewer: Teala Paradise).

## Adversarial Input Handling

Every Anthropic API call processing inbound mail:

- Email content wrapped in `<untrusted_content source="email-body">…</untrusted_content>` via
  `shared.untrusted_content.wrap()`.
- System prompt includes `shared.untrusted_content.system_boilerplate()`.
- Sender allowlist enforced at Mail.app rule level; non-allowlist routes to Quarantine.
  `shared.quarantine.is_allowlisted()` is the helper for any post-rule checks.
- Extraction output runs through `shared.anomaly_logger.check()`; anomalies route to
  `ITS_Review_Queue` with `security_flag=True`.

## Weekly cadence (Q9 resolved)

- **Generate**: Friday 2:00 PM ET. Drafts written to `WPR_Pending_Review` unchecked.
- **Approval deadline**: Friday 6:00 PM ET. Approver checks `Approved for Send`.
- **Notification cadence**: Friday 6:00 PM, Saturday 12:00 PM, Sunday 12:00 PM, Monday 6:00 AM
  ET. Recipients: Jacob Stephens + Teala Paradise (configurable in
  `ITS_Config.notification_recipients`).
- **Send**: Monday 6:00 AM ET. Idempotent — the guard keys on `Send Status == SENT` (see `weekly_send.py`); `Sent At` is stamped atomically with the status, so they agree, but `Send Status` is the authoritative check.
- **Late approval**: row approved after Monday 6:00 AM sends next business day with
  `Late Send` flag set; owner notified.
- **Unapproved Monday morning**: row held indefinitely. Never auto-sent unreviewed.

## WPR_Pending_Review sheet columns [DECOMMISSIONED 2026-06-05 — superseded by WSR_human_review]

> Superseded by `WSR_human_review` for the portal pull flow. `weekly_generate`/`weekly_send`
> still read/write WPR (the columns below remain the live schema) until the Phase-5 rewire.

`Customer`, `Job`, `Week`, `Draft Body`, `Recipients`, `Approved for Send` (checkbox),
`Approved By` (contact), `Approved At`, `Sent At`, `Send Status`, `Late Send` (checkbox), `Notes`.

## Status

- **`intake.py`** — wired end-to-end as of 2026-05-21, Graph-trigger refactor 2026-05-22 (PR #59).
  Sender allowlist → quarantine, project resolution, Anthropic classify+extract with
  `<untrusted_content>` tagging + tool-use JSON-mode output, confidence gate → review queue,
  anomaly check (sentinel + model-self-report) → review queue, week-folder resolution, Daily
  Reports row write, Box upload, row update with Box URL. The success watermark is now
  `graph_client.mark_read` (called by `intake_poll`), replacing the prior
  `.eml → .eml.processed` rename. 12-stage pipeline; see the module docstring for the
  per-stage breakdown.
- **`intake_poll.py`** — launchd-driven polling daemon shipped 2026-05-22 (PR #59). One
  poll cycle per launchd interval; fetches unread messages from Graph, calls
  `intake.process_message` per message, marks each as read on non-error statuses. See the
  module docstring for the per-cycle behavior + push-vs-record separation notes.
- **`weekly_generate.py`** — R3 session 2 scope. Not yet started.
- **`weekly_send.py`** — R3 session 3 scope. Not yet started.

`weekly_summary.py` (the pre-cascade scaffold) was DELETED 2026-07-03 — its deletion
condition (the `weekly-generate` plist loaded on the production MacBook, no orphan
`weekly-summary` plist) was verified via `launchctl list`. `intake_poll.py`'s RETIRED
tombstone was deleted the same day (R4-F2; no `safety-intake` launchd job remains).

## intake configuration surface (ITS_Config workstream `safety_reports`)

| Setting | Default | What it does |
|---|---|---|
| `safety_reports.intake.allowed_senders` | `["seths@evergreenmirror.com"]` | JSON list of allowlisted sender addresses or `@domain.com` patterns. Non-matching senders route to `ITS_Quarantine` without any Anthropic call. |
| `safety_reports.intake.classification_model` | `claude-sonnet-4-6` | Anthropic model ID for the classify+extract tool-use call. |
| `safety_reports.intake.box_filing_enabled` | `true` | When `false`, intake writes the Daily Reports row but skips Box upload and tags Notes / Action Items with `[box_filing_disabled]`. |
| `safety_reports.intake.review_queue_on_low_confidence` | `true` | When `true` and `confidence < confidence_threshold`, the message routes to `ITS_Review_Queue` (Reason=low-confidence-extraction) instead of the Daily Reports row. |
| `safety_reports.intake.confidence_threshold` | `0.75` | Float threshold for the confidence gate. |
| `safety_reports.intake.mailbox` | `safety@evergreenmirror.com` | Microsoft Graph mailbox polled by `intake_poll`. Cutover to production address happens at the Phase 1.5 sandbox-to-production tenant swap. |
| `safety_reports.intake.poll_interval_seconds` | `60` | Integer seconds between poll cycles. Read at install time by `scripts/install_safety_intake_daemon.sh` and substituted into the launchd plist's `StartInterval`. Re-run the installer after changing this row. |
| `safety_reports.intake.polling_enabled` | `true` | Per-workstream kill switch for the polling daemon. `false` short-circuits each cycle. Distinct from the global `system.state` kill switch — use this when you want every OTHER ITS workstream to keep running. |

Classification + pipeline rows are seeded by `scripts/migrations/seed_safety_intake_config.py`.
Polling rows (last three) are seeded by `scripts/migrations/seed_safety_intake_polling_config.py`.
Both are idempotent re-run safe.

## Installing the intake polling daemon

Replaces the prior Mail.app rule trigger (`intake.py` was previously invoked per inbound .eml
by an Apple Mail rule; the cutover landed in PR #59). The new trigger is a launchd-driven
polling daemon reading directly from Microsoft Graph.

```sh
# One-time: seed the three ITS_Config rows (idempotent — safe to re-run).
python3 scripts/migrations/seed_safety_intake_polling_config.py

# Install the launchd agent. Reads poll_interval_seconds from ITS_Config
# at install time and substitutes into the plist. Re-run after changing
# the interval row.
scripts/install_safety_intake_daemon.sh
```

Verify with:

```sh
launchctl list | grep org.solutionsmith.its.safety-intake
ls -lh ~/its/state/safety_intake_heartbeat.txt   # bumped each cycle
tail -f ~/its/logs/launchd/safety_intake_poll.err.log
```

Uninstall:

```sh
scripts/uninstall_safety_intake_daemon.sh
```

The Mail.app rule (if installed from the pre-PR-#59 runbook) should be **deleted post-cutover**
to avoid dual-processing. The .eml hot-folder may also be retired — `intake.py` no longer
reads from it.

## How to smoke-test intake

Post-merge smoke against the sandbox (`evergreenmirror.com`):

1. **Anthropic key**: ensure `ITS_ANTHROPIC_KEY` is in macOS Keychain
   (`security add-generic-password -a "$USER" -s "ITS_ANTHROPIC_KEY" -w`).
2. **Graph credentials**: ensure `ITS_MS_TENANT_ID` / `ITS_MS_CLIENT_ID` /
   `ITS_MS_CLIENT_SECRET` are in Keychain and the app has Mail.ReadWrite on the safety
   mailbox via Application Access Policy.
3. **Send the smoke email**: from `seths@evergreenmirror.com` (or any allowlisted sender)
   to `safety@evergreenmirror.com`. Subject example: `Bradley 1 — Daily JHA — 2026-05-22`.
   Body should mention the project name + a paraphrased summary. Attach a representative PDF.
4. **Wait one poll cycle** (default 60 s).
5. **Observe**: tail `~/its/logs/launchd/safety_intake_poll.err.log` for an `intake SUCCESS`
   INFO line and a `poll cycle: fetched=1 processed=1 marked_read=1 errors=0` summary line.
6. **Verify Smartsheet**: open Bradley 1's current week Daily Reports sheet; a new row
   with your subject's title and a Box URL in Notes / Action Items should be present.
7. **Verify Box**: navigate to
   `ITS DATA / Bradley 1 / (Project # & Name) Field / A. Onsite Reporting & Tracking / A. Safety Plan & Reports / D. JSA's/`.
   The uploaded PDF should be named `<report_date>_<category>_<original-filename>`.
8. **Verify mark_read**: the test message should now be marked as read in the inbox
   (Outlook web client or Outlook desktop).

To exercise the review-queue branch, send a smoke email whose project is ambiguous
(`Bradley 1 vs Bradley 2 …`) and confirm a `PENDING` row appears in
`ITS_Review_Queue` with `Reason=ambiguous-classification`.

## Troubleshooting the polling daemon

| Symptom | Likely cause | Where to look |
|---|---|---|
| Daemon installed but no poll cycles in log | launchd job failed to load | `launchctl print gui/$(id -u)/org.solutionsmith.its.safety-intake` |
| `poll cycle: skipped_disabled` lines in log | `polling_enabled=false` in ITS_Config | ITS_Config row `safety_reports.intake.polling_enabled` |
| Heartbeat file stale (>2 poll intervals old) | Daemon stuck or not running | `~/its/state/safety_intake_heartbeat.txt`; restart with the install script |
| Lock file present but no daemon running | Crash during a previous cycle left a stale lock | Delete `~/its/state/safety_intake.lock` and restart |
| Same message processed twice | Seen-set state was lost or `mark_read` failed | `~/its/state/safety_intake_processed.json` + the stderr log; delete duplicate row in Daily Reports |
| Manual rerun needed for one message | Operator-initiated retry | `python -m safety_reports.intake <message_id>` (CLI wrapper around process_message) |
| `daemon_health_write_failed` entries in ITS_Errors | Heartbeat write to ITS_Daemon_Health failed (PR #59.5) | Check the heartbeat row at `ITS_Daemon_Health`; row may have been deleted or columns renamed. The cache auto-invalidates on 404 — see Operator visibility below |
| Daemon missing from ITS_Daemon_Health | Row not yet provisioned | A missing row now **self-provisions** on the next cycle (A1). If `daemon_health_write_failed` / `self-provision create failed` persists, or a `daemon_health_race_duplicate` WARN appears, follow [`docs/runbooks/daemon_health_self_provision.md`](../docs/runbooks/daemon_health_self_provision.md) |
| **Watchdog Check P CRITICAL — "portal_poll pending-fetch OUTAGE" (A4)** | The Worker is unreachable / bearer rejected / base URL wrong → portal_poll cannot fetch pending and **filing is STOPPED**. (Second-opinion to portal_poll's own inline CRITICAL.) | **Low-class:** confirm the Worker is up and reachable, then restart the daemon — once a fetch succeeds the counter resets and Check P clears. Look at `safety_reports.portal.worker_base_url` (ITS_Config) and the `ITS_Daemon_Health` row. **Escalate to Seth (high-class: secrets/auth)** if the bearer token or `ITS_PORTAL_INTERNAL_TOKEN` Keychain entry needs rotation. |
| **Watchdog Check Q WARN — "portal_poll unfiled backlog STUCK" (A4)** | portal_poll fetches fine but intake is erroring on **every** row (a saturated page draining nothing) → submissions pile up behind the page cap. | **Low-class:** grep recent `ITS_Errors` for the per-row intake error driving the failures; once the underlying cause is cleared (e.g. a bad job/form mapping) the backlog drains and the latch clears on the next cycle. **Escalate to Seth (high-class: code)** if the per-row failure is a code defect, not a data fix. |

## Operator visibility — ITS_Daemon_Health (PR #59.5)

Every poll cycle writes a heartbeat row to `ITS_Daemon_Health` (sheet `6272022823784324`,
under `ITS — System / 04 — Daemons`). The row keyed `safety_reports.intake_poll`
carries the canonical operator-facing status. **Self-provision (A1):** if a
daemon's row is absent, the daemon creates its own row (registration columns —
Daemon Name / Workstream / Enabled / Interval Seconds / Source ID) on the next
cycle and the per-cycle columns fill immediately after; no manual seed step is
needed. The create is failure-isolated (heartbeat never blocks the daemon) and
race-safe (a duplicate-on-race adopts the first row and WARNs
`daemon_health_race_duplicate`). See
[`docs/runbooks/daemon_health_self_provision.md`](../docs/runbooks/daemon_health_self_provision.md)
for the Tier-2 remediation. The status columns:

- **Last Heartbeat** — UTC ISO timestamp of the most recent successful poll cycle.
- **Last Cycle Status** — `OK` (errors=0) or `WARN` (errors>0). `ERROR` / `SKIPPED` are
  reserved for future failure modes the daemon doesn't currently reach inline.
- **Last Cycle Items Processed** — message count from the most recent cycle.
- **Total Cycles Today** — lifetime monotonic counter (despite the column title; see
  PR #59.5 ARCH-3 — semantics are lifetime, not daily-reset, to avoid a
  read-before-write round trip per cycle).
- **Last Error Summary** + **Last Error Correlation ID** — only set when status=WARN or
  ERROR. Grep ITS_Errors with the correlation ID for the full traceback.
- **Enabled** — operator-facing filter checkbox. **Read for report filtering only —
  not as a runtime gate.** The daemon's on/off switch is the `polling_enabled`
  row in ITS_Config (PR #59.5 ARCH-1: one canonical runtime gate, one operator
  filter flag, no overlap). Toggling `Enabled` in ITS_Daemon_Health does NOT halt
  the daemon; toggle `polling_enabled` in ITS_Config instead.

Where to look:

- **Heartbeat fresh?** Operator-view report (Smartsheet) filters
  `Last Heartbeat < now - 2*Interval Seconds` → those daemons are stale.
- **Cycle errors?** Filter `Last Cycle Status != OK`. The correlation ID
  jumps from the row to the matching `ITS_Errors` rows.
- **Disable the daemon?** Set `safety_reports.intake.polling_enabled=false` in
  `ITS_Config`. The next poll cycle short-circuits without touching Graph or the
  heartbeat sheet. The previous heartbeat row stays as-is until the daemon resumes.

Cache files (local, under `~/its/state/`):

- `heartbeat_row_ids.json` — `{daemon_name: {row_id, total_cycles}}`. Persists the
  ITS_Daemon_Health row ID across launchd-poll-once process restarts (otherwise each
  cycle would re-resolve via `find_row_by_primary`, adding a Smartsheet read per cycle).
  Also stores the lifetime `total_cycles` counter. The file auto-recovers from a
  404 (row deleted/re-seeded) by invalidating the entry; the next cycle re-resolves.

## Portal pull daemon (`portal_poll.py`) — Phase 5

The Safety Portal pull model (`decision_phase5-portal-transport`). `portal_poll`
drains the Worker's D1 queue: `GET /api/internal/pending` → per-row HMAC verify
(`shared/portal_hmac.py`) → `intake.process_portal_submission` → `POST
/api/internal/mark-filed` (the receipt). It is a generation-side daemon with **zero
external-send capability** (enrolled in `tests/test_capability_gating.py`); the
human-approved send is the separate `weekly_send` process.

> **NOT live-verified.** The end-to-end chain (portal → Worker → poll → intake →
> Box → Smartsheet → WSR → send) is validated in the deploy session, which also
> deploys the Worker, seeds the secrets below, and loads this daemon's launchd job.

### Configuration surface

| Key (ITS_Config workstream `safety_reports`) | Default | Meaning |
|---|---|---|
| `safety_reports.portal_poll.polling_enabled` | `true` | Runtime kill switch. `false` short-circuits each cycle (canonical on/off gate — NOT the ITS_Daemon_Health `Enabled` checkbox, which is report-filter metadata). |
| `safety_reports.portal_poll.poll_interval_seconds` | `60` | launchd cadence (read at install time). |
| `safety_reports.portal.worker_base_url` | — | The Worker origin, e.g. `https://…workers.dev`. **Fail-closed: if unset, the daemon does NOT poll.** |

Keychain entries (mirror the Worker's `PORTAL_INTERNAL_API_TOKEN` + `HMAC_PAYLOAD_SECRET`):

- `ITS_PORTAL_INTERNAL_TOKEN` — the bearer for `/api/internal/*`. **Fail-closed if absent.**
- `ITS_PORTAL_HMAC_SECRET` — the per-row HMAC verify secret. **Fail-closed if absent.**

State files (under `~/its/state/`): `portal_poll_heartbeat.txt`, `portal_poll.lock`,
`portal_poll_seen.json` (the idempotency seen-set — `{uuid: {status, box_link}}`).

### Mark-filed (drain) policy

- `processed` / `already_filed` → mark-filed with the Box link (queue drains).
- `review_queue` (unknown/inactive job, unknown form, malformed payload/date,
  unresolved Box) → **also drained**, because a re-pull can't fix it; the
  **Review Queue entry is the operator's action item** and holds the full payload
  for manual re-filing. _Trade-off: the portal shows "filed" though it went to review._
- `error` (transient Smartsheet/Box auth·rate·5xx) → **NOT drained** → auto-retries
  next cycle.
- **HMAC verify failure** → rejected: anomaly-logged + Review-Queue-flagged
  (`security_flag=True`, CRITICAL) + recorded `rejected` in the seen-set (one-shot,
  no re-spam) + **never filed, never drained** (the row stays in D1 for forensics).

### §43 Successor-Operator remediation runbook

Low-capability-class repairs the trained Successor-Operator may perform (no code,
no secrets/Keychain, no doctrine):

| Symptom | Likely cause | Low-class repair |
|---|---|---|
| `poll cycle: skipped_disabled` in the log | `polling_enabled=false` | Set `safety_reports.portal_poll.polling_enabled=true` in ITS_Config to resume; `false` to pause. |
| `portal_creds_missing` CRITICAL; daemon not polling | Worker base URL row missing, or the daemon is running before the deploy session seeded secrets | Confirm the `safety_reports.portal.worker_base_url` ITS_Config row is set. If the row exists and it still fails, the Keychain secrets are missing → **escalate to Seth** (secrets/auth = high-class). |
| `portal_creds_transient` WARN; one cycle skipped | Smartsheet was briefly unreachable (circuit OPEN, or a rate-limit/5xx blip before the breaker trips) when reading the Worker base URL — NOT a misconfig; the WARN names the exact condition | **None — transient, self-heals** when Smartsheet recovers (the next cycle resumes filing). Only if it persists across many cycles / Check-C flags `safety_portal_poll` stale is the outage sustained → **escalate to Seth**. |
| `portal_config_transient` WARN | Smartsheet was briefly unreachable while reading the `polling_enabled` gate — the cycle proceeds on the default (enabled) and typically resolves into `portal_creds_transient` one step later | **None — transient, self-heals**, same posture as `portal_creds_transient`. Sustained → **escalate to Seth**. |
| `portal_pending_fetch_failed` ERROR each cycle | Worker unreachable / network blip | Transient — self-heals when the Worker is reachable. If it persists >1 h, **escalate to Seth**. |
| Submissions stuck in `ITS_Review_Queue` with reason `job_not_found` / `job_inactive` | The job isn't an Active row in `ITS_Active_Jobs` (config lag) | Add/activate the job in `ITS_Active_Jobs`, then **re-file the submission manually** from the Review-Queue payload (the auto-drain means it won't re-pull): `python -m safety_reports.intake` is email-only — re-filing a portal submission is a **developer task → escalate to Seth**. |
| Submission drained to `ITS_Review_Queue` with reason `smartsheet_validation` | Smartsheet rejected a write as invalid (HTTP 400, e.g. a job name so long the `"<project> — week of <Sat>"` week-sheet name would exceed Smartsheet's 50-char cap, errorCode 1041). The name is now auto-truncated, so this should be rare. | Low-class: if the cause is an over-long job name, **shorten the `Project Name` in `ITS_Active_Jobs` to ≤29 chars**, then re-file from the Review-Queue payload (re-filing a portal submission is a **developer task → escalate to Seth**). Any other 400 cause → **escalate to Seth** (code/data shape). |
| `portal_hmac_failure` CRITICAL | A pulled row's signature didn't verify (tamper, or a secret mismatch between Worker and Mac) | **Escalate to Seth** — this is a security event (secrets/auth = high-class). Do NOT clear it. |
| `daemon_health_write_failed` / daemon missing from `ITS_Daemon_Health` | Heartbeat-row write failed | Same as the intake daemon — see [`docs/runbooks/daemon_health_self_provision.md`](../docs/runbooks/daemon_health_self_provision.md). |

**Escalate-to-Seth boundary (high-capability-class — always escalate):** anything
touching the **HMAC/bearer secrets or the Keychain** (`portal_hmac_failure`,
`portal_creds_missing` when the row exists), any **code change**, or re-filing a
drained submission. The four fixed high-class categories (External Send Gate,
secrets/auth, doctrine, code) always escalate regardless of documentation.

### Portal user login / admin (Phase 7) — §43 (high-class, escalate to Seth)

Portal user provisioning + session revocation is the **secrets/auth + code** boundary —
a FIXED high-capability class — so it is **developer-operator only** (Seth). There is NO
Tier-2 repair; the Successor-Operator escalates.

| Symptom | Likely cause | Action |
|---|---|---|
| Field PMs all 401 / can't log in right after a deploy | migration 0006 (`users.disabled`) wasn't applied BEFORE the Worker redeploy → `requireSession` fail-closes every session | **Escalate to Seth** (apply 0006 to live D1; the per-request read then succeeds). |
| `portal_admin` CLI returns `(401) — admin bearer rejected` | Keychain `ITS_PORTAL_ADMIN_TOKEN` ≠ Worker `PORTAL_ADMIN_API_TOKEN`, or unset | **Escalate to Seth** (secrets/auth). |
| Need to add / reset / disable a portal user | routine provisioning | **Escalate to Seth** — it's `python -m safety_reports.portal_admin …` with the admin bearer (developer-operator task). |
| Need to clear a test / decommissioned job from the portal dropdown + its D1 data | a fully-removed job lingers `active=1` (the daemon `/sync` refuses an empty job set, so removing the last job from `ITS_Active_Jobs` can't deactivate it) | **Escalate to Seth** (admin-bearer + data delete = high-class). Fix is `python -m safety_reports.portal_admin purge-job <JOB-ID>` — hard-deletes the job + its D1 rows (submissions, filed-PDF cache, pdf_requests). Box + the week sheet keep the durable record; this only clears the D1 transport cache. The daily prune also auto-deletes any inactive job once it has no submissions, so this is only needed for an *immediate* clear. |

Activation steps + the route/CLI reference live in `safety_portal/README.md` (Phase 7).

## Weekly compile (`weekly_generate.py`) — Phase 5b

The DETERMINISTIC weekly compile (the Anthropic narrative core was retired). For each
Active job's Saturday→Friday week it merges the per-submission PDFs recorded on the
week sheet (`form_pdf.merge_pdfs`), files the packet to an `ITS`-prefixed Box week
folder, and **dual-writes**: (a) the week sheet's read-only **Rollup** manifest row;
(b) one **WSR_human_review** row per (job, week) — Email Body seeded from a fixed
template (the reviewer edits it; it is the source of truth `weekly_send` transmits),
Recipient TO/CC display resolved from `ITS_Active_Jobs`, Send Status PENDING.

> **NOT live-verified.** Friday-fire 14:00 Pacific (`StartCalendarInterval`); the
> watchdog Check-I catch-up re-runs a missed Friday. `--week-start <ISO>` backfills.
> Box file-attach to the WSR row is deferred to the deploy session — the Compiled PDF
> column carries the Box link (one-click reviewable), which is sufficient for review.

### Trigger + idempotency
- **Friday auto-compile** + **`Compile Now`** checkbox on the week sheet's Rollup row
  (forces an out-of-band recompile).
- **Skip-if-already-compiled-and-no-new-docs**: if a Rollup row exists and no
  submission is newer than its `compiled_at` watermark (and Compile Now is unchecked),
  the week is skipped. **Never closes the week** — a later submission + a recompile
  just refresh the packet + the WSR Compiled-PDF link; the WSR Email Body + approval
  columns are NEVER touched on an existing row (re-sending an updated packet is a
  deliberate operator re-approval, F22-gated).
- **Empty week** → still writes the Rollup + WSR row (a silent skip would look like
  daemon failure); the WSR row carries no packet, so `weekly_send` HELDs it.

### §43 Successor-Operator remediation runbook

| Symptom | Likely cause | Low-class repair |
|---|---|---|
| A job's weekly packet never appears | The job isn't Active in `ITS_Active_Jobs`, or a transient Smartsheet error blocked auto-provisioning its per-job folder/week sheet | Confirm the job is Active in `ITS_Active_Jobs`. A `weekly_generate.compile_failed` Review-Queue entry names the cause. The per-job folder + week sheet auto-provision under the ITS — Safety Portal workspace (no config step), so a persistent failure is a transient-Smartsheet or workspace-permission issue → **escalate to Seth**. |
| Packet is short (missing a submission) | A per-submission PDF had no Box link or failed to download (`weekly_generate.submission_no_link` / `submission_download_failed` WARN) | The Rollup row Notes list the gap. Re-trigger by checking **Compile Now** on the week sheet's Rollup row once the missing PDF is in Box; if the Box file is genuinely gone, **escalate to Seth**. |
| Operator wants to recompile after a late submission | New doc arrived after the Friday compile | Check **Compile Now** on the week sheet's Rollup row (or wait for the next Friday — a new doc auto-triggers a recompile). |
| `weekly_generate.no_downloadable_pdfs` ERROR | Box unreachable or every submission link broke | Transient → self-heals (recompile). Persistent → **escalate to Seth** (Box auth = secrets, high-class). |
| WSR row shows an old packet after recompile | Expected — a recompile updates the Compiled PDF link but never the human Email Body or approval columns | Re-review the updated Compiled PDF; re-approve to re-send (a deliberate human re-approval; the prior send is not auto-repeated). |

**Escalate-to-Seth boundary (high-class):** Box auth/secrets, the ITS service
account lacking Admin on the ITS — Safety Portal workspace (blocks per-job
folder/week-sheet auto-provision), sheet IDs (config/topology), any code change.
The compile itself is
generation-only (no external send) — the human-approved send is the separate
`weekly_send` process.

## Weekly send (`weekly_send.py` + `weekly_send_poll.py`) — Phase 5c

The send half of the External Send Gate (Invariant 1) for the portal flow, repointed
WPR_Pending_Review → **WSR_human_review**. `weekly_send_poll` discovers WSR rows with
`Send Now` (immediate) OR `Approve for Scheduled Send` (the Monday-≥07:00-Pacific
batch) checked, runs the **F22** approval-attestation gate on the driving checkbox,
stamps the verified approver (Approved By/At), and dispatches `weekly_send.send_one_row`.

`send_one_row` resolves recipients **at send time from `ITS_Active_Jobs`** (TO = the
job's safety-reports contact; CC = the non-empty CC 1–5; stakeholder NOT on the
envelope) — NOT the WSR display columns. Body = the WSR `Email Body` (the reviewer's
edits are the source of truth). The compiled Box PDF is attached. **HELD** (refuse,
no send) on empty/unknown TO or a missing Compiled PDF; **FAILED** + retry on a
transient Graph/Box error.

> **NOT live-verified.** Transport stays on Graph (the Resend-vs-Graph decision is
> separate). Scheduled cadence (`Approve for Scheduled Send`) is the
> `safety_reports.weekly_send.scheduled_send_local` ITS_Config window (default
> `MON 07:00`, Pacific, DST-aware). `Send Now` fires on the next poll cycle.

### §43 Successor-Operator remediation runbook

| Symptom | Likely cause | Low-class repair |
|---|---|---|
| A WSR row stays PENDING after approval | `Approve for Scheduled Send` is checked but it's not yet the Monday-07:00 window | Expected — it sends at the next Monday ≥07:00 Pacific cycle. For immediate send, check `Send Now` instead. |
| WSR row went **HELD** | No Compiled PDF, or the job's safety-reports contact (TO) is empty/unknown | The Notes cell carries the reason. Missing PDF → check `Compile Now` on the week sheet's Rollup row to recompile. Empty TO → set the Safety Reports Contact on the job in `ITS_Active_Jobs`, then re-check `Send Now`. |
| `approval_unverified` CRITICAL/WARN | The approve checkbox was flipped by an actor who is NOT a member of the ITS — Safety Portal workspace (CRITICAL), the workspace has no individual members (CRITICAL), or a benign un-approve race (WARN) | UNAUTHORIZED/EMPTY → **escalate to Seth** (send-gate = high-class; authorizing == being shared on the workspace, an owner access decision — do NOT change workspace sharing yourself). Benign race → no action. |
| `weekly_send.graph_auth_failed` CRITICAL | Graph send credentials rejected | **Escalate to Seth** (secrets/auth = high-class). |
| `weekly_send.retries_exhausted` CRITICAL | A row hit MAX_SEND_RETRIES on transient Graph errors | Investigate the `[LAST_SEND_ERROR: …]` Notes tag; if transient is resolved, clear the `[SEND_RETRY_COUNT: N]` tag (low-class) to re-arm, else **escalate to Seth**. |
| `weekly_send.post_send_row_update_failed` CRITICAL | The email sent but the row didn't flip to SENT (DOUBLE-SEND RISK) | Manually set the WSR row's Send Status = SENT to prevent a re-send (low-class, time-sensitive). |

**Escalate-to-Seth boundary (high-class):** the **External Send Gate** itself, the
authorized-approver allowlist / Graph secrets (auth), doctrine, code. The
authorized-approver set is config-driven but is part of the send-gate trust boundary
— co-resolve approver changes with Seth.

## §43 runbook — publish daemon (`publish_daemon.py`, slice 3b)

The form-editor publish actuator. The admin's Publish enqueues a `publish_requests` row
(send-free); this daemon pulls, re-validates vs git HEAD, commits, runs the CI render
smoke, merges, deploys via the operator's local wrangler, health-checks, and stamps the
admin Status Monitor through Queued→Validated→Tested→Live→Archived. Any stage failure
stamps `failed(stage, reason)` (RED in the monitor) + fires an operator CRITICAL.

**HIGH-CAPABILITY (Tier-3 / co-resolve with Seth):** this daemon COMMITS + DEPLOYS code.
Activation (loading the launchd job) and every failure needing a git/secret/deploy action
escalate to the Developer-Operator. The low-class repairs below are the only Tier-2-safe ones.

| Symptom | Likely cause | Repair |
|---------|--------------|--------|
| A publish row stuck `queued` | daemon not loaded / `polling_enabled=false` | Confirm `safety_reports.publish_daemon.polling_enabled=true` in ITS_Config (low-class); if the job isn't loaded, that's a Tier-3 activation — **escalate to Seth**. |
| `publish_daemon.creds_unresolved` (ERROR) | missing Worker base URL or `ITS_PORTAL_INTERNAL_TOKEN` | secrets/auth = high-class — **escalate to Seth**. |
| `failed(validated)` | the form failed the meta-schema / manifest re-check vs live HEAD (e.g. a key collision against a form added since enqueue) | The admin re-edits + re-publishes in the editor (low-class); the reason is in the monitor + the CRITICAL. |
| `failed(tested)` | CI red on the auto-opened PR (the 3-renderer render smoke caught a non-degraded render) OR merge blocked | Read the PR's CI; a real render regression is **code → escalate to Seth**; a transient CI/merge flake → the admin re-publishes (low-class). |
| `failed(live)` | wrangler deploy / fast-forward / health check failed | deploy + Cloudflare auth = high-class — **escalate to Seth**. |
| `failed(archived)` | the Box blank-archive regen failed (the form IS live; only the DR archive lagged) | Re-run `python -m scripts.generate_form_archive --upload` (low-class); if Box auth is the cause, **escalate to Seth**. |

**Escalate-to-Seth boundary (high-class):** the publish deploy itself, git push / Cloudflare
(wrangler) / Keychain auth, doctrine, and code (a real CI render-smoke failure). Loading
the daemon is a Developer-Operator operation.
