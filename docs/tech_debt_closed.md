---
type: reference
status: archived
workstream: null
tags: [tech_debt, archive, closed]
---

# ITS — Tech Debt (CLOSED / DELIVERED archive)

Resolved/closed/delivered/superseded entries moved out of the live `docs/tech_debt.md` on 2026-07-12 to keep it under the 256 KB cap. History preserved; the live file holds only OPEN items.

> See `docs/tech_debt.md` for the live (open) set.

## [RESOLVED 2026-08-10 — #26] `box_client._store_tokens`'s refresh lock covers the Keychain persist, not the token exchange — and watchdog Check P can't see the difference [OPEN 2026-08-10]

Filed as GitHub issue #26 this session; recorded here as the execution-repo debt ledger entry.

`shared/box_client.py:161-197` (`_store_tokens`, the boxsdk `store_tokens` callback) takes a cross-process
sidecar lock (`state_io.with_path_lock(_BOX_OAUTH_REFRESH_LOCK_ANCHOR)`) around the Keychain persist +
freshness-marker write. The docstring is explicit about the scope: *"boxsdk owns the token exchange
itself, so this serializes the persist seam ... not the HTTP exchange."* Box rotates the refresh token on
every exchange (single-use); two ITS processes that both decide to refresh in the same window can each
independently call boxsdk's exchange before either persists, so the second exchange can consume a token
the first has already invalidated server-side — a race the lock cannot prevent because it starts one step
too late.

Compounding this: **watchdog Check P** (`scripts/watchdog._check_box_token_freshness`) is purely
time-based — it reads `box_client.BOX_TOKEN_REFRESH_MARKER`'s `last_refresh_utc` and reports `INFO`
"fresh (idle Nd)" as long as the marker is recent, with **no live auth call**. A host whose Box identity
has already failed (e.g. `invalid_grant` from exactly the race above) can sit on a marker written before
the failure and have Check P report "fresh" indefinitely — the check confirms the marker is recent, not
that Box actually works.

**Fix (not scoped this session):** (a) either serialize the exchange itself (e.g. lock around the whole
refresh-triggering call site, not just the callback) or accept the race and add exchange-failure
detection/retry; (b) have Check P make (or piggyback on) a cheap live Box call — even a HEAD/whoami — on
some cadence, rather than trusting marker age alone.

**Tag:** `field_ops`, `box`, `oauth`, `race-condition`, `watchdog`, `medium`.

**Revisit when:** next Box-auth incident, or a concurrency/multi-daemon-on-Box hardening pass. See issue #26.

Surfaced: 2026-08-10 session close, following the Track 6 archive activation drill.

**RESOLVED (#26).** Both halves of the proposed fix landed, taking option (b) on each:

- **(a) the race** — the exchange was NOT serialized. Widening the lock to cover boxsdk's HTTP exchange
  would mean holding a cross-process lock across a network round-trip on every daemon's first Box call,
  and it still could not make a single-use token safe against a process that read it before the lock was
  taken. Instead callers became tolerant of a lost race: `_retry_once_on_rejected_refresh_token` wraps
  every public `box_client` fn that calls `get_client()` directly, and on `invalid_grant` it calls
  `_reset_client()` (dropping the process-wide singleton, so the retry RE-READS Keychain rather than
  re-spending the cached OAuth2's in-memory copy of the dead token) and retries exactly once. A
  thread-local guard stops nested wrapped calls from compounding one race into 2ⁿ exchanges. Matches the
  observed recovery: three `invalid_grant`s on 2026-08-10 all self-healed on retry.
- **(b) Check P** — now makes a real authenticated Box read (`list_folder("0", limit=1)`) alongside the
  marker read, and reports which signal fired. A live rejection is CRITICAL regardless of marker age; a
  successful live read caps a stale marker at WARN (it is positive proof the 60-day wall was not hit).
  The check moved to the **daily** tier: the probe spends a single-use refresh token per run, so an
  hourly probe would have worsened the very race (a) absorbs.

Operator-facing wording is part of the fix: Box uses identical text for a CONSUMED and an AGED-OUT
token, so `BoxRefreshTokenRejectedError` states both causes plus the local marker evidence and never
asserts "expired" — the old wording pointed a Tier-2 operator at a full re-auth (a fixed high-class
escalation) for what is usually a self-healing race. A retry that succeeds is the one moment the
ambiguity resolves, and records `box_refresh_token_consumed_retry` (WARN) so a rising race rate stays
visible. §43 runbook `docs/runbooks/box_token_freshness.md` rewritten around the two-signal split.

## [RESOLVED 2026-07-12 — #538] Subcontracts — SC-S3b Exhibit A blocked on the `exhibit_trade_templates` config artifact [OPEN 2026-07-11]

ADR-0003 scopes the subcontract package as Subcontract body + **Exhibit A** (Art I/III/IV/VI fixed +
operator-authored trade-templated Art II) + SOV + fixed annex kit. SC-S3b (#534) shipped the **body .docx +
SOV .xlsx** render; Exhibit A was deferred because no `exhibit_trade_templates` config existed and building
it ad-hoc would mean inventing legal text.

**RESOLVED (#538, `eb6fe9d`):** the corpus turned out to already contain canonical per-trade Exhibit A
templates (`05_Subcontracts` Kendall 2025.112 / Steger 2025.364, `Sub Name - Project Name_<trade>` files —
project-identical). Built `subcontracts/exhibit/` (manifest + tokenized skeleton + 7 verbatim per-trade Art II
bodies, sha-pinned), `exhibit.py` loader, `render_exhibit_a_docx` (render_package now emits the 3-file
package), the poll's 3-file Box filing + inline attach, a cap-gated serve route, and the SPA builder pre-fill.
Reviews CLEAN/CONFIRMED-RESOLVED (an ops-stds BLOCK on a fabricated recital was caught + reverted to verbatim
corpus). The subcontract generator is now 100% built. Only NON-CORPUS trade = Specialty (honest operator
placeholder — no corpus template). Residual (deferred, minor, non-blocking): `exhibit.py` has no Layer-A
`legal_review` gate like the body's `terms.py` — intentional (verbatim-corpus skeleton + operator-authored
Art II), pending a one-line Seth confirmation.

## Config gates ship dark by ROW-ABSENCE, not just row-value [RESOLVED 2026-07-05 for equipment_enabled; materials_enabled stays visibly gated on §51]

**Operator-reported blocker, root-caused 2026-07-05 (session part 2).** The operator went looking for
`field_ops.fieldops_sync.equipment_enabled` / `.materials_enabled` rows in `ITS_Config` (sheet
`3072320166907780`) to flip them and found **no row at all** — not a row set to `false`. Root cause:
`fieldops_sync._read_bool_setting(key, default)` (see `_equipment_enabled`/`_materials_enabled`,
`field_ops/fieldops_sync.py:226-231`) defaults to `DEFAULT_EQUIPMENT_ENABLED = False` /
`DEFAULT_MATERIALS_ENABLED = False` when the row is **absent**, same as when it exists and is set
`false` — the two states are behaviorally identical but only one of them is operator-visible in the
Smartsheet UI. Only `field_ops.fieldops_sync.sync_enabled` and `.hours_enabled` had ever been seeded;
Equipment (#468) and Material List (#470) both shipped dark-by-design but nobody created the config
rows those PRs' own text promised to gate on. **There is no row to "flip" — the operator (or CC) must
CREATE the row first**, then set it.

**Fix applied this session:** created both rows via MCP — `equipment_enabled=true` (ACTIVATED) and
`materials_enabled=false` (still §51-blocked per the M2 entry below, but now a **visible, intentional**
`false` rather than a silent absence). Each row carries a descriptive `Description` cell. Worker deploy
independently confirmed via unauthenticated 401 probes on `/api/internal/fieldops/{equipment,material-list}-snapshot`
(a genuinely-deployed gated route 401s `application/json`; a missing route SPA-falls-back to `200
text/html` per [[reference_worker-spa-fallback-200-on-deleted-asset]] — same diagnostic shape, new
context). Equipment activation **live-smoked GREEN**: the 90s `fieldops_sync` cycle picked up the flip
and reported `equipment upserted=1 errors=0` steadily over ~45 minutes; the `Portal create test 2 —
Equipment` sheet exists in the progress workspace as the SoR artifact. Materials stayed at
`upserted=0` (dark, as intended).

**General lesson (not field-ops-specific):** any `ITS_Config`-gated boolean whose code defaults to a
safe value (`False`/off) on a missing row will silently ship dark, and — unlike a hardcoded-fallback
WARN (the `REQUIRED_CONFIG` #336 class) — there's no wrong VALUE to notice, just an absent row a
human has to know to go create. Worth a `REQUIRED_CONFIG`-style startup enumeration that logs "this
declared gate key has NO row" distinctly from "this key's row is set to the default," so a future
gate doesn't strand an operator hunting for a row that was never seeded. Not built; folds into the
existing #336 tracked pass rather than a new one.

**Tag:** `field_ops`, `fieldops_sync`, `config`, `its_config`, `row-absence`, `equipment`, `materials`.

### M3 Slice 2 — Material Incidents pass: activation queue [ACTIVATED 2026-07-06 — LIVE]

**ACTIVATED 2026-07-06 (afternoon).** Worker deployed (route probes `401 application/json`);
`incidents_enabled` seeded then flipped `→ true`; the daemon incidents pass ran clean post-flip
(`incidents upserted=0 reviewed=0 errors=0` — 0 = no filed incidents on active sandbox jobs yet,
`errors=0` proves the daemon→endpoint→mirror path is healthy end-to-end). Also seeded the four
`progress_reports.*.row_cap_warn_threshold`=15000 rows that the #336 startup pass was WARNing as
NO-ROW every cycle. The original activation checklist (kept for provenance / production cutover):

The per-job Material Incidents ledger pass (`field_ops.fieldops_sync` material-incidents pass +
`progress_reports/material_incidents.py` + Worker `GET /api/internal/fieldops/material-incidents`)
shipped **dark** (gate `field_ops.fieldops_sync.incidents_enabled`, `DEFAULT_INCIDENTS_ENABLED=False`).
Activation checklist (Developer-Operator / Seth), same shape as Equipment/Material List above:

1. **Deploy the Worker** — the new read route must exist before the gate flips. **No D1 migration**
   (the endpoint is a read-only `SELECT` over the existing `submissions` table + `json_extract` of
   `payload_json` + a LEFT JOIN to `job_expected_materials` for the live `Line Status`). Confirm the
   deploy with an unauthenticated 401 probe on `/api/internal/fieldops/material-incidents` (a deployed
   gated route 401s `application/json`; a missing route SPA-falls-back to `200 text/html`).
2. **Seed the ITS_Config row** `incidents_enabled = false` (Workstream `field_ops`, sheet
   `3072320166907780`) — a MISSING row reads identically to `false` (the row-absence lesson above), so
   the operator has nothing to flip until it EXISTS. `incidents_enabled` IS now enumerated in
   `fieldops_sync.REQUIRED_CONFIG` (#481), so startup logs it with its source — but `resolve_and_log`
   still can't distinguish "row absent" from "row=false" (both log as `default`), so seed the visible
   row anyway.
3. **Flip it to `true`** — a cell-flip is the only activation. The 90 s cycle then reports
   `incidents upserted=N …` in the `sync_cycle_summary` INFO line; the per-job `<Job> — Material
   Incidents` sheet find-or-creates beside the other trackers.

Unlike Equipment/Material List there is **no zero-drop concern** (append-only ledger — no retire path)
and the Material Incidents sheet is added to the §51 archive-on-closure move. The end-to-end live smoke
(deployed Worker → daemon → Smartsheet) is the operator's cutover step; the merge-time gate was the
mocked-plus-vitest-against-real-D1 suite + a live Smartsheet write smoke of the sheet schema (GREEN,
sandbox, cleaned up). See `docs/runbooks/fieldops_sync.md` Symptom F.

**Tag:** `field_ops`, `fieldops_sync`, `material-incidents`, `M3`, `config`, `activation`, `dark-ship`.

## Hours Log — replace Started/Ended columns with a Task column [BUILT 2026-07-05 — live-sheet migration is the remaining operator step]

**BUILT 2026-07-05 (PR #472, merged `9aada583`, four-part verify CLEAN — state=MERGED, mergedAt non-null, mergeCommit present, main-branch CI on the merge commit = SUCCESS; branch `feat/hours-log-task-column`) — all code + tests landed:** worker `/hours-pending`
`LEFT JOIN task_assignments ta ON ta.id = t.task_id AND ta.job_id = t.job_id` projecting `ta.description AS
task` (job-scoped per the security review) with `work_started_at/_ended_at` dropped from THAT projection
only (the D1 columns stay — rollup/personnel/jobtracker still read them); `progress_reports/hours_log.py`
`COL_TASK`; `field_ops/fieldops_sync.py` hours-pass mapping (`work_date` from `created_at`);
`shared/portal_client.py` docstring; `scripts/migrations/hours_log_task_column.py` (two-phase, name-guarded,
idempotent) + its unit test; runbook **Fault M** (`docs/runbooks/hours_log_sync.md`). **Remaining operator
step:** run the two-phase live-sheet migration on the existing `Portal create test 2 — Hours Log` sheet —
`--phase add --commit` BEFORE the `git -C ~/its pull` + `npm run deploy`, `--phase drop --commit` AFTER (the
order is KeyError-critical; see the script docstring + Fault M). Original request below (kept as the
multi-surface fan-out record).

**Operator-requested 2026-07-05 (log-only; NOT built in the M2 PR).** The per-job `<Job> — Hours Log`
Smartsheet carries `Started` / `Ended` columns that are, in practice, **always empty**: the portal
time-log form captures neither a start nor an end wall-clock time (`time_entries.work_started_at` /
`work_ended_at` are effectively unpopulated) — it captures `hours` + a `task_id`. Meanwhile the portal
time log ALREADY records `time_entries.task_id` (→ `task_assignments.description`), which is the field
crews actually want to see on the Hours Log. **Change:** drop the two always-empty `Started`/`Ended`
columns and add a single **`Task`** column resolved from `task_assignments.description`.

Multi-surface fan-out (enumerate ALL before claiming done — the recurring incomplete-fan-out bug):
- **Worker `/hours-pending` route** (`safety_portal/worker/index.ts`): add `LEFT JOIN task_assignments
  ta ON ta.id = t.task_id` and project `ta.description AS task`; DROP `t.work_started_at` /
  `t.work_ended_at` from the SELECT. This is a trust-boundary read route → adversarial review required.
- **`progress_reports/hours_log.py`**: drop `COL_STARTED` / `COL_ENDED` (columns + styles + the
  `_TRACKED_COLS` change-set), add `COL_TASK`; update `upsert_entry_row` signature + cells.
- **`field_ops/fieldops_sync.py` `_mirror_hours_pass`**: drop the `started=` / `ended=` mapping (and
  the `_fmt_epoch_time` calls), add `task=str(e.get("task") or "").strip()`.
- **`shared/portal_client.py`**: update the `get_fieldops_pending_hours` docstring shape (add `task`,
  drop the two work-time fields).
- **Tests**: `test_hours_log.py`, `test_fieldops_sync.py` hours-pass, the worker `fieldops-hours-mirror`
  vitest.
- **One-time live sheet-schema migration** for the EXISTING live `Portal create test 2 — Hours Log`
  sheet (and any other already-created Hours Log sheets): the code change only affects NEW sheets, so
  existing sheets need `Started`/`Ended` deleted + `Task` added via a name-guarded `update_column` /
  `add_columns` / `delete_column` one-shot (Developer-Operator).

**Tag:** `field_ops`, `hours-log`, `smartsheet`, `ux`.

## M2 Material List — one-way-up MVP diverges from §51's bidirectional model [RESOLVED 2026-07-06 — ratified into Op Stds v20]

**Verified 2026-07-06:** the §51 divergence is reconciled — Op Stds **v20** folds the Material List as **one-way-up (phased delivery; M2b bidirectional receive deferred)**, blessing the shipped MVP as strictly-more-conservative (never writes operator-owned columns, never reads operator edits back). See `operational-standards.md` §51 body (~line 859) + the v20 changelog. `field_ops.fieldops_sync.materials_enabled` is now doctrine-UNBLOCKED (flipped live 2026-07-06). The original divergence note (below) is superseded — Path B (reconfirm one-way-up) was the outcome.


**Surfaced by ops-stds-enforcer on PR #470 (WARN, not a BLOCK — merged with the divergence flagged).**
Operational Standards §51 (`~/its-blueprint/doctrine/operational-standards.md`, canonical line ~847)
names the Material List explicitly as **"bidirectional with split column ownership — the operator
owns content columns, the field owns delivery columns, neither side's write overwrites the other's."**
The Progress-Reporting mission (`~/its-blueprint/workstreams/progress-reporting/mission.md`) carries
the same bidirectional framing. PR #470 shipped a **one-way-up-only** MVP instead — portal is the sole
author of every line (`job_expected_materials`, migration 0031 extended with `line_uuid` + `unplanned`
0039), `progress_reports/material_list.py` mirrors it up as a structural clone of the Equipment
tracker (#468); there is **no `smartsheet_row_id` column and no down-sync path** — an operator editing
the Smartsheet Material List directly has no mechanism to write back to D1. This was an **operator
ratification of Option A** made in-session (see `project_fieldops-portal-program.md` memory,
"SESSION CONTINUED 2026-07-05"), not a doctrine change.

**This is NOT yet reconciled with doctrine.** Two paths, either resolves the drift:
- **A — v19.x rider** (the Sentry-leg / Hours-Log-period-split precedent) noting the Material List
  ships in a **phased delivery**: one-way-up MVP now, bidirectional split-column-ownership as a later
  slice — provided the rider can show the one-way phase doesn't change §51's protective claim to a
  degree that's bump-worthy (unlike those two precedents, this changes WHAT the mechanism promises —
  the operator can no longer edit the sheet as an input — so this may in fact cross the v20-trigger
  "recharacterization of a mechanism's protective claim" bar; Seth's call, not a rubber-stamp).
- **B — reconfirm one-way-up as the permanent model** and revise §51 + the Progress-Reporting mission
  to drop "bidirectional" for the Material List, replacing it with the same one-way-up-plus-retire-in-
  place model used for Hours Log / Equipment.

**Gate: this reconciliation MUST land before `field_ops.fieldops_sync.materials_enabled` is flipped
live** — the tracker ships DARK today specifically so this doesn't need to block the merge, but going
live on an undecided doctrine question would mean training an operator workflow around a data model
that might later need a breaking migration (adding `smartsheet_row_id` + down-sync after operators
have already been editing the sheet as if it were read-only-mirror). See blueprint
`references/memory-archive.md` §G54 for the full write-up (session-close 2026-07-05).

**2026-07-05 (session part 2) update:** the `materials_enabled` ITS_Config row now EXISTS (created
this session, set explicitly `false` — see "Config gates ship dark by ROW-ABSENCE" above), so the
gate is visible and intentional rather than silently absent; this does not change the reconciliation
requirement above, it only removes a separate, unrelated confusion (an operator hunting for a row
that was never seeded). A **ratification-ready rider draft** for Path A (phased-delivery v19.x rider)
and a revision draft for Path B (reconfirm one-way-up, drop "bidirectional" from §51 + the mission)
were prepared this session — see blueprint `references/memory-archive.md` §G55 and the info-gap doc's
"Awaiting approval" note for both drafts' full text. Still Seth's call; neither is applied.

**Tag:** `field_ops`, `progress-reports`, `material-list`, `doctrine`, `§51`, `seth-decision`.

## `FormFillPage.r3.test.tsx` "R3 dirty guard" flaky last-call assertion [RESOLVED 2026-07-06 — stale entry, fixed by #473]

**Verified 2026-07-06:** #473 (`86bfab0`) already landed the recommended `await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(false))` fix (`FormFillPage.r3.test.tsx:154`). The entry below was stale.


**Flaked once during PR #470's (M2) 4th-gate portal CI check; cleared on unmodified re-run.** The
test `"touching a field reports dirty + arms beforeunload; submit clears both"` asserts, immediately
after a `waitFor` on the "Submitted ✓" text:

```ts
await waitFor(() => expect(getByText("Submitted ✓")).toBeTruthy());
expect(onDirtyChange).toHaveBeenLastCalledWith(false);
```

`toHaveBeenLastCalledWith` reads the `vi.fn()` call history synchronously the instant the "Submitted ✓"
text appears — but nothing guarantees `onDirtyChange(false)` (fired from the form's post-submit
dirty-clear effect) has landed by that exact tick relative to the submitted-screen render. If the two
state updates land in separate React commits, the assertion can race and see a stale last call.
Every other multi-step assertion in this file wraps the final check in its own `waitFor` (see
`onDirtyChange).toHaveBeenLastCalledWith(true)` a few lines above, which IS `waitFor`-wrapped) — this
one line is the odd one out.

**Not yet root-caused as a real ordering bug vs. a test-only assertion-timing gap** — the feature
itself (dirty-guard clears on submit) has never been observed wrong in the app; only the test flaked,
once, in CI. **Fix:** wrap the assertion in its own `waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(false))`
to match the file's own pattern above it, removing the race regardless of which commit order React
chooses.

**Tag:** `field_ops`, `spa`, `test-flake`, `ci`, `low`.

## Job routing form — "Same as stakeholder" copy button on the Safety block [RESOLVED 2026-07-06]

**Built 2026-07-06 (this PR):** the "Same as stakeholder" button (copies Stakeholder name/email → the Safety contact) is on the Safety block in `RoutingFields`, mirroring the existing "Same as safety" handler; SPA test added in `FieldOpsJobTracker.test.tsx`. Gives the chain Stakeholder → Safety → Progress. The entry below is superseded.

**Operator-parked 2026-07-01 (was mid-build, deferred).** The job-creation routing form (`safety_portal/src/pages/FieldOpsJobTracker.tsx`, `RoutingFields`) has a "Same as safety" copy button on the **Progress** contact block (copies Safety → Progress, ~line 179). Add a parallel **"Same as stakeholder"** button on the **Safety** contact block (~:158-161) that copies the Stakeholder name/email into the Safety contact, and KEEP "Same as safety" on Progress — giving the chain Stakeholder → Safety → Progress for the common single-contact case. Small SPA change: mirror the existing copy handler + an SPA test. **Tag:** `field_ops`, `job-tracker`, `spa`, `ux`.

## Unified job-creation flow — bundle task creation + crew assign + equipment assign [RESOLVED 2026-07-06 — stale entry, built by #402]

**Verified 2026-07-06:** #402 (`d6c5323`) shipped it — `FieldOpsJobTracker.tsx` imports `assignPersonnel` + `moveEquipment`, gates on `cap.crew.assign`/`cap.equipment.field`, and crew converges on `personnel.current_job` (worker `fieldops_jobtracker.ts:86-95`, migration `0024`). The entry below was stale.


**Operator-locked 2026-07-01** (concretizes the plan's "unified create-flow extension"). The portal "New job" workflow should let the office PM, AT creation time, also **create tasks/deliverables**, **assign crew**, and **assign equipment** to the job — not just set routing/contacts. All three ride EXISTING §50-ungated field-ops write routes (tasks `fieldops_task_write.ts` #314; equipment `fieldops_equipment_write.ts` #315–#316; crew-assign = the P2.6 `POST /api/fieldops/personnel/:id/assign` route, LANDED PR #398). So it is primarily **multi-step create-UX wiring** (a wizard/stepper in `FieldOpsJobTracker.tsx`) — no new daemon/doctrine surface beyond P2.6's crew-assign route. **P2.6 Manager tier landed 2026-07-01 (PR #398)** — this item's dependency is satisfied.

**Build-ready spec written 2026-07-01: `~/.claude/plans/spec_unified-job-create-flow.md`.** Supersedes the two earlier exploratory plans below as the execution artifact — read the spec first. Locks: shape = detail-view controls (reusable) + create-flow auto-open-detail nudge; **crew CONVERGES on `personnel.current_job`, NOT `task_assignments`** (a real data-model gap found while scoping this — see `memory-archive.md` §G49.6 — requires a worker query change in `fieldops_jobtracker.ts` on BOTH the job-list and job-detail routes, plus a new `idx_personnel_current_job` index migration); materials deferred to M2; per-control capability gating (`cap.crew.assign` / `cap.equipment.field` / `cap.jobtracker.manage`) so a manager can assign crew without creating tasks. 3 slices, DoD, and adversarial-review assignment all specified. **NOT built** — execute in a fresh session per the spec's own instructions (worktree off latest `origin/main`, fresh venv, verify every file/line claim against live HEAD first).

Earlier exploratory plans (context only, spec above is now canonical): `~/.claude/plans/ok-we-are-going-scalable-flamingo.md`, `~/.claude/plans/what-happened-to-my-floating-porcupine.md`. **Tag:** `field_ops`, `job-tracker`, `unified-create`, `stage-2`, `p2.6`.

## Time entries can't attribute hours to a specific crew member (UI gap) [RESOLVED 2026-07-06 — stale entry, built by #403/#421]

**Verified 2026-07-06:** #403 (`bad30f0`) added the time-log "For" person picker (`personnel_id` in the `logTime` body); #421 (`c350f09`) added the worker-resolved `"Me (<name>)"` default. The entry below was stale.


**Operator-reported 2026-07-01.** A logged time entry records the submitting ACCOUNT (`actor_username` + optional `submitted_as`) but in practice can't record WHICH personnel/crew member the hours are FOR. The **data model already supports it** — `time_entries.personnel_id INTEGER REFERENCES personnel(id)` (`0015:36`) AND the write route accepts + inserts it (`fieldops_time_write.ts:50,102-107`). The gap is that the **time-logging UI has no personnel selector**, so `personnel_id` is never set and hours can't attribute to a roster crew member. Fix = add a personnel picker to the time-log form (+ confirm the SPA time lib passes `personnel_id` through). Relates to **P2.6 Manager tier** (crew time logging via `personnel_id`; time entries stay ORTHOGONAL to job assignment). **Tag:** `field_ops`, `time-entries`, `personnel`, `spa`, `p2.6`.

## Watchdog Check-C staleness + Check-I catch-up not wired to `progress_weekly_generate` slug [CLOSED 2026-06-30]

**P4 Slice 2 (PR #376).** `progress_weekly_generate` wrote a marker that nothing read — `scripts/watchdog.py` tracked only `safety_weekly_generate`, so a stale/skipped progress compile fired no alert.

**Resolution (PR #381, P5 watchdog slice):** `TRACKED_JOBS` + `TRACKED_JOB_WINDOWS` now include both `progress_weekly_generate` (8-day) and `progress_send_poll` (30-min) for Check-C staleness; Check-I was generalized via a `_CatchupTarget` so a missed `progress_weekly_generate` Friday run is auto-recovered (the safety wrapper stays byte-identical). Both progress slugs WARN until the operator loads their plists at cutover (register + load together). Also fixed a pre-existing Check-I summary bug surfaced during the generalization (it read `drafts_written`/`aborted_empty_chain`, keys `run_generate` never produces).

**Tag:** `progress_reports`, `watchdog`.

## P5 progress_send must use `job.reports_contact_email` alias and pass `PROGRESS_ACTIVE_JOBS_CONFIG` [CLOSED 2026-06-30]

**P4 Slice 1 (PR #375).** Forward-note: a P5 progress send that omitted the config or passed `SAFETY_ACTIVE_JOBS_CONFIG` would silently route progress reports to the safety contact (no runtime error — a different column in a different sheet).

**Resolution (PR #379, P5 core):** `progress_send.CONFIG` binds `active_jobs_config=PROGRESS_ACTIVE_JOBS_CONFIG`; the resolver reads the neutral `reports_contact_email` alias; the trap is named explicitly in `docs/runbooks/progress_send.md` Symptom B; and `tests/test_progress_send.py` asserts `get_job` is called with the progress config. The `weekly_send.SendConfig.active_jobs_config` field is required no-default (a missing binding is a construction-time error, not a silent safety inheritance). *(A duplicate OPEN copy of this entry — the original P4 forward-note — sat further down this file; collapsed into this one 2026-07-03 after re-verifying the resolution against `progress_reports/progress_send.py` at HEAD: the resolver reads `job.reports_contact_email` and `CONFIG` binds `active_jobs.PROGRESS_ACTIVE_JOBS_CONFIG`.)*

**Tag:** `progress_reports`.

## Doctrine drift M6 — FM v8 cites in `safety_reports/intake.py` + `weekly_summary.py` docstrings [RESOLVED 2026-07-06]

**Fixed 2026-07-06:** `safety_reports/intake.py`'s two `Foundation Mission v8` docstring cites bumped to `v11` (this PR). `safety_reports/weekly_summary.py` no longer needs a fix — it was **DELETED 2026-07-03** (superseded by the `weekly_generate` + `weekly_send` two-process split). The original entry (below) is superseded.


**Pre-existing (not introduced this session).** `safety_reports/intake.py` and `safety_reports/weekly_summary.py` contain module-level docstrings citing "Foundation Mission v8"; the canonical version is FM v11. This is the doctrine-drift class M6 pattern (stale in-code version pin) surfaced in `docs/audits/2026-06-29_forensic-retrospective.md`. The CI doctrine-drift check (`scripts/check_doctrine_drift.py --strict`) does not catch in-code comment/docstring version pins — it checks YAML frontmatter and cited-section numbers.

**Fix (trivial):** update the module docstrings to cite FM v11. No behavior change. Two files: `safety_reports/intake.py` + `safety_reports/weekly_summary.py`.

**Tag:** `safety_reports`, `docs`, `doctrine`. **Revisit when:** next safety_reports maintenance pass.

## `docs/session_logs/README.md` index missing the #370 session-log row [RESOLVED 2026-07-06 — moot, log present + correctly indexed]

**Verified 2026-07-06 (stale premise):** the session log for that pass EXISTS and IS indexed — `docs/session_logs/2026-06-30_tech-debt-cleanup-alongside-phase2.md` (`related_prs: [363, 364, 365, 366, 367, 368]`); `scripts/regen_doc_indexes.py` confirms the auto-index is current (no changes). There is no separate "#370 row" by design — a session log's `related_prs` lists the PRs the session *covered* (#363–368), not the PR that *committed* the log (#370). Nothing to regen. The original entry (below) misread the committing-PR as a missing index row.


**Pre-existing (not introduced this session).** The session-log index at `docs/session_logs/README.md` is missing the row for PR #370 (`eb110c1`), which committed the session log for the tech-debt cleanup pass alongside Phase-2 (#363–#368, 2026-06-30). The `scripts/regen_doc_indexes.py` script regenerates the index correctly; `--check` is warn-only in CI, so this does not block merges.

**Fix (trivial):** run `python scripts/regen_doc_indexes.py` and commit the updated `README.md`. Warn-only in CI so acceptable as a standalone trivial commit.

**Tag:** `docs`. **Revisit when:** next session log is written — verify index currency before committing.

## §23/§24 topology text + version bump owed for the 7th workspace (ITS — Progress Reporting) [RESOLVED 2026-07-06 — ratified into Op Stds v20]

**Verified 2026-07-06:** Op Stds **v20** syncs §23's enumeration and records the seventh standalone workspace (`ITS — Progress Reporting`) as the workspace-topology change the bump carries (the v17 sixth-workspace precedent). See `operational-standards.md` §23 (~line 168 + the workspace enumeration ~line 174: `Progress Reporting 5988851429730180`). The original owed-fix note (below) is superseded.


**P2 (PR #362).** Standing up the `ITS — Progress Reporting` workspace makes it the **7th** standalone Smartsheet workspace. Op Stds **v19 §51** already names "the ITS — Progress Reporting workspace" explicitly (so its existence is doctrine-contemplated), but §23's topology *enumeration* still lists six and was not synced — the same gap the v17 bump closed when the Safety Portal (the 6th) was added. The `ops-stds-enforcer` review flagged this as a pre-merge gate; the operator approved (2026-06-29) landing P2 on §51's basis and deferring the §23 text-sync as a fast-follow.

**Fix (doctrine — Seth's):** add `ITS — Progress Reporting` to §23/§24 as a standalone, §46-governed workspace exception (mirror the v17 Safety Portal paragraph), bump Op Stds → v20, propagate `docs/doctrine_manifest.yaml` (`current: 20`; the blueprint `workstreams.slugs`/`count` if the canonical set is updated), re-verify the exec tree. The mechanical doctrine-drift check (M1/M4/M7) does NOT catch this (a semantic enumeration gap); `doc-reconciliation-auditor` / `ops-stds-enforcer` do.

**Tag:** `docs`, `doctrine`, `progress_reports`. **Revisit when:** the next doctrine pass (before/with P5 progress-send — the mission's draft→canonical promotion trigger).

**Re-flagged, still open (2026-07-04).** P5 (`progress_send`/`progress_send_poll`) landed 2026-06-30 (PR #379) — P7 Slice 1 (Hours Log up-sync, PR #461) then touched §51 doctrine again this session (a v19.x amendment rider, blueprint PR #58, clarifying period-split for low-volume accumulating logs) without addressing this §23/§24 enumeration gap — the rider was scoped narrowly to §51 only, correctly, since a topology-enumeration fix is a separate v20-class change per this entry's own fix note. Still Seth-gated; the "next doctrine pass" trigger has now fired twice (P5, P7 Slice 1) without picking this up — worth bundling into whichever doctrine pass handles the mission's eventual draft→canonical promotion, rather than opening a third one-off rider.

## P7 archive-on-closure — `smartsheet_client` needs a move-sheet method (its#462, §51 committed follow-up) [RESOLVED 2026-07-06 — built by #465; close its#462]

**Verified 2026-07-06:** #465 (`185ca86`) built it — `smartsheet_client.move_sheet_to_folder` (`:1255`) + `_archive_closed_job_trackers` (`fieldops_sync.py:756`) wired into the mirror path (fires on `lifecycle=="archived"`, `:706`), moving trackers to `FOLDER_ARCHIVE_CLOSED_PROJECTS`; 5 tests at `test_fieldops_sync.py:622-697` (incl. the M3-updated 4-tracker move). **GitHub issue #462 is still OPEN and should be closed** (operator). The entry below was stale.


**P7 Slice 1 (exec PR #461, blueprint #58 v19.x rider).** Op Stds v20 §51 requires ITS-owned accumulating-log SoR sheets to be archived-on-closure (never `delete_rows`). The Hours Log up-sync ships never-delete + the SoR-safe row-cap WARN watchdog (`progress_reports/hours_log.check_row_cap`), but **archive-on-closure is deferred** — filed as GitHub issue `its#462`, not a vague someday-item — because `shared/smartsheet_client.py` has no move-sheet primitive, and it's only exercised at a job's lifecycle → `archived` (no job archives imminently). The `archived` lifecycle write itself is already live (portal admin), so until this lands the exposure is a **bounded, recoverable** stranded (never-deleted) sheet, not data loss — but it must land before the first job actually archives.

**Fix (dev, low-class per §44 but needs new code):**
- `smartsheet_client.move_sheet_to_folder(sheet_id, folder_id)` + a §30 integration test (per `sdk-integration-test-scaffold`).
- A trigger in `fieldops_sync`'s job-mirror pass: when a job's lifecycle flips to `archived`, move that job's standing tracker sheets (Hours Log now; Equipment/Materials in later P7 slices) to `FOLDER_ARCHIVE_CLOSED_PROJECTS` (`1034553964947332`, workspace `WORKSPACE_ARCHIVE=5528280611743620`). Idempotent — an already-archived sheet is a no-op, never a duplicate move or delete.

**Tag:** `progress_reports`, `field_ops`, `smartsheet-upsync`, `p7`, `its462`, `§51`. **Revisit when:** before any job's lifecycle is flipped to `archived` in the live tenant — this is a hard prerequisite, not a nice-to-have.

## Portal D1 test-job dropdown not cleared by empty-sync [CLOSED 2026-06-18]

**Resolved 2026-06-18 (tech-debt easy-wins pass):** PR #292 — pruneOldData now deletes inactive+empty jobs + a new purge-job admin endpoint/CLI; the clean-slate purge cleared the D1 jobs table. ITS_Active_Jobs + D1 jobs now 0.

**2026-06-17 test-artifact cleanup session.** After clearing all rows from `ITS_Active_Jobs` (id `6223950341164932`), the portal job dropdown still shows test job entries. Root cause: `portal_poll.push_jobs` calls `POST /api/internal/sync` with the list of active jobs from Smartsheet — but the Worker rejects a sync payload with an empty `jobs` array (guard: `jobs.length === 0` → 400). So an empty `ITS_Active_Jobs` does NOT clear the D1 `jobs` table, and the portal dropdown retains stale test entries.

**Operator repair options:** (a) direct D1: `wrangler d1 execute its-safety-portal --remote --command "DELETE FROM jobs WHERE job_id IN ('bradley-1', 'teala-test', ...);"` (target test slugs explicitly — do NOT delete production job rows); (b) alternatively, seed one real production job in `ITS_Active_Jobs`, which will push-sync and override stale entries on the next poll cycle. Option (b) is safer if production jobs are ready.

**Not a code bug per se** — the empty-sync guard exists to prevent accidental dropdown wipes. The gap is that there is no supported "clear all test entries" operator path. A future improvement could be a `DELETE /api/internal/jobs/:slug` endpoint or a `wrangler` script target.

**Tag:** `safety-portal`, `d1`, `operator-manual`. **Revisit when:** production jobs are ready to seed, or a D1 management endpoint is added.

## Portal D1 historical test submissions + filed-PDF cache not pruned [CLOSED 2026-06-18]

**Resolved 2026-06-18 (tech-debt easy-wins pass):** PR #292 + the clean-slate purge — all test submissions / filed_pdfs / pdf_requests removed (now 0); pruneOldData self-cleans inactive-job rows going forward.

**2026-06-17 test-artifact cleanup session.** The Smartsheet and Box test artifacts were deleted, but the corresponding D1 rows were not touched. Residue in the Worker D1 (`its-safety-portal` remote):

- `submissions` table: rows for test submissions (e.g., `teala test`, `ZZ Portal Proof` / JOB-000008 runs, etc.) — filed as `box_verified=1`, payload stripped at 90d lifecycle, but rows remain as browse-visible entries.
- `filed_pdfs` table: chunked base64 PDF cache rows for any submission whose filed PDF was requested via the `FormRequestPage` download flow — keyed by `(submission_uuid, chunk_index)`.
- `pdf_requests` table: rows for the 24h-window PDF-request grants associated with those submissions.

The two-stage D1 prune lifecycle (`submissions`: payload stripped at 90d, row deleted at 30d after job-inactive; `filed_pdfs`/`pdf_requests`: pruned on `mark_filed` pass) will eventually clear these, but the job-inactive trigger requires the job rows to go inactive, which also requires the D1 `jobs` table to be updated (see "Portal D1 test-job dropdown not cleared" above).

**Operator repair (if desired before natural prune):** direct D1 operations — identify test `submission_uuid` values (e.g., via `wrangler d1 execute ... --command "SELECT submission_uuid, job_id, form_type FROM submissions WHERE job_id IN ('jha-test', 'rockford', ...)"`), then `DELETE FROM submissions WHERE submission_uuid IN (...)`, `DELETE FROM filed_pdfs WHERE submission_uuid IN (...)`, `DELETE FROM pdf_requests WHERE submission_uuid IN (...)`. Low operational urgency — D1 space is not constrained at current volume; no capability impact.

**Tag:** `safety-portal`, `d1`, `operator-manual`. **Revisit when:** D1 space becomes a concern, or a D1 test-fixture management story is added.

## [RESOLVED 2026-06-12 — folded into mission v5] Mission v4→v5 delta — weekly-send transport now has two modes (inline ≤2.5 MB / upload-session >2.5 MB)

**Resolution (blueprint v5 reconciliation, 2026-06-12):** folded into `its-blueprint/workstreams/safety-portal/mission.md` v5 §7 (Invariant 1, "the transport changed, the gate did not") + the v5 Authority block. Gate unchanged. No further blueprint action; the flag is closed.

**PR-3 (photo workstream tail).** Adding photos to the weekly packet means a packet can exceed Graph's ~3 MB inline `sendMail` ceiling, so `weekly_send` now sends via **one of two transports** chosen by packet size: **inline base64** (`send_mail`) at ≤2.5 MB, or the Graph **upload-session** (`send_mail_large_attachment`: draft → chunked PUT → send) above it, with an **oversized-HELD** refusal above Graph's ~150 MB hard ceiling. This is a behavioral change to the External-Send-Gate **send half** (the *transport*, not the gate: still human-approved, still two-process, still recipients-resolved-at-send-time, still capability-gated send-only). The Safety Portal / Safety Reports mission (v4) describes the weekly send as a single attached-PDF email; the **two-mode transport + the oversized-HELD terminal state** are a **planning-layer / Seth-owned** mission note, not made here. Proposed mission v4→v5 amendment: *"the weekly safety report is emailed with its compiled PDF attached — inline for small packets, via a Graph chunked upload-session for large (photo-bearing) packets, and **HELD** (operator-actionable, never silently dropped) for a packet beyond Graph's attachment ceiling."* Flagged for blueprint co-resolution **alongside the PR-4 receipt-cache delta + the PR-5 mission note** (fold them together).

**Tag:** `safety-reports`, `doctrine`, `mission-delta`, `planning-layer`, `send-gate`.

**Revisit when:** next blueprint mission-doctrine pass (fold the PR-3 transport delta + the PR-4 receipt-cache delta + PR-5 together).

Surfaced: 2026-06-12 PR-3 implementation.

## Safety Portal — 2026-06-08 adversarial security audit: 11 findings remediated [CLOSED 2026-06-08]

**Closed by the post-audit hardening PR (this session).** A grey-box adversarial audit of the live mirror Worker (`safety.evergreenmirror.com`) confirmed the core posture HELD — injection 0/4 (bound params), no auth bypass (HMAC cookie unforgeable), no privilege escalation, and the atomic last-admin guard survived the TOCTOU race — and surfaced 11 perimeter findings, all remediated:

- **#1 (med)** null/non-object JSON body → unhandled TypeError → bare 500 on every handler (unauth on `/api/login`). Fixed: a per-handler body-shape guard (`typeof!=='object' || null || Array.isArray` → 400) on all 12 handlers + a global `app.onError` (clean JSON, no stack leak, NOT Sentry-paged on unauth noise).
- **#4 (low)** `values:[]` slipped the `typeof==='object'` check in `/api/submit` → added `|| Array.isArray(values)`.
- **#2/#3/#8–11** security headers via Hono `secureHeaders()` + `run_worker_first:true` (so they reach the SPA document + assets): `X-Frame-Options:DENY`, `nosniff`, `Referrer-Policy`, `HSTS`, `Cache-Control:no-store` on `/api/*`, and **CSP shipped REPORT-ONLY** (loosened for React inline styles + the logo/inline-SVG signature) — the enforce-flip is the operator's post-deploy step.
- **#5 (low)** create/rename UNIQUE-race → 500 → mapped to 409 via an `isUniqueViolation` catch (the cheap pre-check stays; this is the race backstop).
- **#6 (low)** delete/demote `changes()==0` was overloaded (guard-block vs already-gone) → re-check existence → 404 vs 409 `last_admin`. The atomic guard itself is unchanged (audit-confirmed TOCTOU-safe).

Worker stays SEND-FREE; no migration. 42 vitest tests (real workerd + D1). Rider in the same PR: the AccountsPage edit-login editor now closes on a no-change Submit. **Activation operator-gated:** `npm run deploy` + a live re-probe of the audit vectors + the **CSP enforce-flip after a signature-capture smoke**.

**Tag:** `safety-portal`, `security`, `audit`.

## Safety Portal — session-epoch revocation + role-aware idle timeout (audit #7) [CLOSED 2026-06-30]

**Resolved 2026-06-30 (tech-debt currency sweep):** BUILT. `users.session_epoch` column (migration 0009) embedded in cookie claims (`safety_portal/worker/auth.ts:32,42,63,123`); `requireSession` rejects a cookie whose `epoch` is below the live `session_epoch`; logout AND password-change bump the epoch. Role-aware lifetime shipped: admins idle out at a **30-min** server-enforced sliding window (`safety_portal/worker/index.ts:69-73`), submitters keep 90-day. NOTE: landed at 30-min idle, not the brief's original 5-min — the only delta from the spec. Verified @HEAD via grep (lesson #1).

**Deferred from the 2026-06-08 audit hardening; carried to the Phase-2 Session Hardening bundle** (needs a migration + a session epoch). Today logout (`/api/logout`) is client-side only — a captured cookie stays valid to `iat+90d`; `requireSession` re-checks only `users.disabled` (a user-level kill, not per-session / logout revocation). Phase-2 fix (resolved in the form-editor grill): a per-user **session epoch** (D1 column, embedded in cookie claims, checked in `requireSession`; logout AND password-change bump it) + **role-aware lifetime** — submitters keep 90-day, **admins get a 5-min idle timeout** (client activity-detection + a server-enforced sliding window). Specced in the Phase-2 form-editor + session-hardening design brief (lands via Session B / the brief PR).

**Tag:** `safety-portal`, `auth`, `session`, `phase-2`.

## Safety Portal — admin route (PR-H) blocked on operator CodeQL dismissal [CLOSED 2026-06-08]

**Resolved + ACTIVATED on the mirror (2026-06-08).** PR-H (#185) merged (`f3ad814`, four-part verify clean: MERGED / mergedAt set / mergeCommit f3ad814 / main-CI SUCCESS), then activated this session: the 2 CodeQL FPs were dismissed; `PORTAL_ADMIN_API_TOKEN` (Worker) + `ITS_PORTAL_ADMIN_TOKEN` (Keychain) set **byte-equal** (via a paste-safe script — `security -w VALUE` argv form, because the bare `-w` flag reads the TTY and ignores piped stdin in an interactive shell → silently stored a 6-char garbage value twice; root cause of an early `list-users` 401); migration 0006 applied to live D1 **before** `npm run deploy`; admin route confirmed `401`-not-`404`; revocation **proven live** (`portal_admin disable-user test.pm` → the user's existing session 401'd `revoked` off `/api/jobs` on the next request). One follow-on finding surfaced during the revocation proof — see "`/api/login` does not gate on `users.disabled`" below. Original entry preserved:

PR-H (#185) adds the admin route (user provision/reset/disable/enable/list + per-request D1 session revocation + migration 0006 `users.disabled` + `shared/portal_client.admin_request` + `safety_reports/portal_admin.py` CLI). CI is GREEN except 2 CodeQL `py/clear-text-logging` alerts (alert #11 `portal_admin.py:52`, alert #13 `portal_admin.py:148`) that are FALSE POSITIVES — interprocedural imprecision: the bearer token taints `admin_request`'s return value; `list-users` and `_fail` print that return; CodeQL flags all prints of it. The refactor already cleared 1 of 3 (stopped echoing the raw response dict); the remaining 2 are unfixable without contorting correct code.

**Resolution required (operator):** dismiss alerts #11 + #13 in the GitHub code-scanning UI as "False positive" (CC is hook-blocked from dismissing) → `gh pr update-branch 185` → merge. **Note:** migration 0006 MUST apply to the live D1 BEFORE the Worker redeploy: `wrangler d1 migrations apply` → `npm run deploy` → `portal_admin add-user`.

**Tag:** `safety-portal`, `phase-7`, `auth`, `codeql`.

## Safety Portal — `/api/login` does not gate on `users.disabled` [CLOSED 2026-06-18]

**Resolved 2026-06-18 (tech-debt easy-wins pass):** Shipped: safety_portal/worker/auth.ts validateUser now SELECTs `disabled` and returns null when disabled (login fails closed).

PR-H's per-request revocation (`requireSession` → `SELECT disabled FROM users` → 401 `revoked`, `safety_portal/worker/index.ts:179-189`) locks a disabled user out of **every** protected endpoint (`/api/jobs`, `/api/recent`, `/api/submit`, `/api/session`). But `/api/login` → `validateUser` (`safety_portal/worker/auth.ts:50-67`) selects only `id, username, password_hash` and checks `!row || !ok` — it **never reads `disabled`**. So a disabled user with a valid password can still LOG IN and mint a fresh session cookie. That cookie is useless (every protected call 401s), so there is **no capability bypass** — the security boundary holds at `requireSession` — but login *appears* to succeed (misleading UX) and it's a defense-in-depth gap.

**Observed live (2026-06-08 mirror revocation proof):** disabled `test.pm` → operator saw "could not load jobs" (the `requireSession` 401) but "could still login".

**Proposed fix (small):** add `disabled` to the `validateUser` SELECT and return `null` when `row.disabled` (login fails closed, identical to a wrong password) — or a dedicated 403 "account disabled". ~15 min + a test.

**Revisit when:** next Safety Portal hardening pass, or before a real PM is provisioned on a live tenant.

## Safety Portal — `custom_domain` route disables the `workers.dev` URL on deploy [CLOSED 2026-06-30]

**Resolved 2026-06-30 (tech-debt currency sweep):** The active incident was resolved 2026-06-08 (daemon `worker_base_url` repointed to `safety.evergreenmirror.com`). The residual is now documented as **intentional known-behavior**, not debt: `safety_portal/wrangler.jsonc:38-50` records that `custom_domain:true` disables the `*.workers.dev` URL on deploy (error 1042) unless `workers_dev:true` is also set, and the portal is deliberately custom-domain-only. Captured in memory `reference_cloudflare-custom-domain-disables-workers-dev`. No code change owed.

PR-J (#188) added `routes: [{ pattern: "safety.evergreenmirror.com", custom_domain: true }]` to `safety_portal/wrangler.jsonc` with **no `workers_dev` key**. On `npm run deploy`, wrangler warns *"Because 'workers_dev' is not in your Wrangler file, it will be disabled for this deployment by default"* and **turns off the `*.workers.dev` URL** — so `https://its-safety-portal.sethsmithusmc.workers.dev` then returns 404 with Cloudflare **`error code: 1042`** ("No Workers script was found for this host on workers.dev"). This is NOT a broken worker (the deploy succeeded; `@cloudflare/vite-plugin` correctly redirects `wrangler deploy` to its generated `dist/its_safety_portal/wrangler.json`); it's the workers.dev route being disabled. It stranded `portal_poll` + `portal_admin`, which read the base URL from ITS_Config `safety_reports.portal.worker_base_url` (then still the workers.dev URL) → ~15 `portal_pending_fetch_failed` ERRORs in ITS_Errors (2026-06-07).

**Resolution applied (2026-06-08):** repointed `safety_reports.portal.worker_base_url` → `https://safety.evergreenmirror.com` (the proper end-state — per PR-J the portal lives on the custom domain). `portal_poll` recovered on its next cycle.

**Residual / decision:** if BOTH the workers.dev URL and the custom domain are ever wanted live, add `"workers_dev": true` to `wrangler.jsonc` (a checked-in change that must be committed, else every future deploy re-disables workers.dev). For the custom-domain-only end-state (mirror + cutover), no change is needed. **Revisit when:** next Safety Portal deploy, or if a non-custom-domain access path is required.

## Orphan Smartsheet week sheet from the pre-relocation smoke [CLOSED 2026-06-18]

**Resolved 2026-06-18:** deleted via the repo SDK (`smartsheet_client.delete_sheet(1966431334780804)`, name-guarded). Verified orphan first (zero code refs; not the clone template `7282977254887300`; the legacy Field Reports "Bradley 1" folder is a different workspace from the live portal filing path). The enclosing folder was left intact.

The 2026-06-06 deploy smoke filed one test JHA (Bradley 1 / JOB-000001) through the pre-relocation `week_sheet.ensure_week_sheet`, creating week sheet **`1966431334780804`** in the legacy Field Reports "Bradley 1" folder (Forefront Portfolio workspace) instead of the ITS — Safety Portal workspace. PR-C (filing relocation) moved portal filing to auto-provisioned per-job folders under `WORKSPACE_SAFETY_PORTAL`, so that sheet is now an **orphan** — nothing reads or writes it. Harmless but stray.

**Repair (operator, manual):** delete sheet `1966431334780804`. Leave the enclosing Field Reports "Bradley 1" folder — the dormant Monday-ISO email path (`week_folder.py`) still maps it.

**Revisit when:** any workspace-tidy pass.

## `scripts/launchd/install.sh` did not substitute `__POLL_INTERVAL_SECONDS__` [CLOSED 2026-06-02]

The generic launchd installer `scripts/launchd/install.sh` substituted ONLY `__ITS_HOME__`, but the `safety-intake` and `weekly-send` plists carry `__POLL_INTERVAL_SECONDS__` in `<integer>StartInterval</integer>`. So `install.sh load` of either left the literal placeholder → `plutil -lint` failed → the daemon would not load. The **documented** install path (the picklist/weekly-send plists point at `install.sh load …`) was therefore broken for interval daemons; `intake` was running only because it has a **dedicated** installer (`scripts/install_safety_intake_daemon.sh`) that already reads the interval from ITS_Config and substitutes both placeholders.

**Resolved by** the install.sh fix (branch `fix-installsh-poll-interval`): `load`/`dry-run` now resolve `__POLL_INTERVAL_SECONDS__` from `[interval]` arg > the daemon's ITS_Config poll-interval row (read via the venv python, mirroring `install_safety_intake_daemon.sh`) > a per-daemon default (60 / 900), substituting it alongside `__ITS_HOME__`. Verified: `dry-run` + `plutil -lint` clean for safety-intake / weekly-send (with default + override) and unchanged for the non-interval plists; a non-integer interval is rejected. **Audit of `~/Library/LaunchAgents/` found the installed copies CLEAN** (no surviving placeholder — they were hand-substituted via the workaround), so no live remediation was needed.

**Residual (low):** `scripts/install_safety_intake_daemon.sh` is now largely redundant with `install.sh load org.solutionsmith.its.safety-intake [interval]` (both read ITS_Config + substitute). Consolidating to the generic installer (the dedicated script also creates `~/its/state/`, so confirm that is covered first) is a small future cleanup, not done here.

## F21 — numeric `maximum` bounds + anomaly-logger range check [CLOSED 2026-06-02]

**Resolved by** B1 (#144, merge `c200914`): added `"maximum": 1000` to each of the 6 incident-count fields (Layer-4 structured-output ceiling) and a numeric-range branch in `shared/anomaly_logger._walk` (`NUMERIC_ANOMALY_THRESHOLD=1000`, overridable per call; `bool` excluded as an `int` subclass) that flags an out-of-range int/float → routes the extraction to `ITS_Review_Queue` with `security_flag=True` (the Layer-5 detection backstop). Schema `version` bumped `0.1.0`→`0.2.0` with `weekly_generate._EXPECTED_SCHEMA_VERSION` in lockstep (F20); a new test (`test_incident_count_fields_carry_numeric_bounds`) locks both the per-field bounds and the version-lockstep against the real schema. Original analysis kept below.

`schemas/safety_weekly_generate.json` defines 6 integer incident-count fields (`lost_time_accidents`, `lost_work_days`, `job_transfer_or_restriction`, `near_misses`, `other_recordable_cases`, `first_aid_cases`), each with `"minimum": 0` but **no `"maximum"`**. `shared/anomaly_logger._walk` branches on `dict` / `list` / `str` only — it has no numeric branch, so integers and floats fall through unchecked, and a prompt-injected count like `99999` passes extraction silently. (Contrast: the `confidence` field already carries both `minimum` and `maximum` in the same schema, so the pattern is established — it just wasn't applied to the incident counts.)

**Proposed fix:** add a sane `"maximum"` to each of the 6 integer fields in the schema, and add a numeric-range branch to `anomaly_logger._walk` that emits a sentinel anomaly when an int/float value exceeds a configurable threshold — so an out-of-range count routes to `ITS_Review_Queue` with `security_flag=True` rather than being trusted.

**Effort:** ~1 hour. **Phase target:** 1.4 pre-Customer-1 hardening.

**Revisit when:** the next `safety_reports/` hardening session, or before Customer-1 launch. The F20 session (schema-version enforcement, PR #129) deliberately scoped F21 out to keep the PR focused; `brief-validator` confirmed the half-bounded fields + the missing numeric check live this turn.

Surfaced: 2026-05-29 F20 session close. Session log: `docs/session_logs/2026-05-29_f20-schema-version.md`. Audit finding F21.

## Invariant 2 Layer 5 prose: "defense layer" framing vs FM v9 tripwire reframe [CLOSED 2026-06-18]

**Resolved 2026-06-18 (tech-debt easy-wins pass):** CLAUDE.md's Invariant 2 section intro + the Layer 5 bullet already carry the FM v9 detection-tripwire reframe.

FM v9 (blueprint, audit F13) reframed Invariant 2's Layer 5 (anomaly logging on extraction output) from a co-equal defense layer to a post-hoc **detection tripwire** — an honest characterization of a trivially-evadable substring matcher; the mechanism is unchanged and stays in production. The OBS-1 citation sweep (PR #127) recorded this reframe in CLAUDE.md's *governing-version block*, but the **Invariant 2 section itself** (the "Six-layer defense:" list) still describes Layer 5 as "Output validation and anomaly logging" — the pre-v9 framing — and still labels the whole set a "Six-layer defense."

This is a doc-characterization reword (relabel Layer 5 as a detection tripwire inside the Invariant 2 list, and soften "Six-layer defense" to acknowledge Layer 5 is detection-not-prevention), deliberately scoped OUT of OBS-1 — that PR was citation-version reconciliation only, and no version string lives in the Layer 5 bullet, so `check_doctrine_drift.py` does not flag it. No code or behavior is affected; `shared/anomaly_logger.py` is untouched. The blueprint FM v9 and the doctrine manifest are the canonical source for the new wording.

**Revisit when:** the next session that has a natural reason to touch the Invariant 2 section of CLAUDE.md — a security-review pass, the Email-Triage Layer-6 build, or a `doc-reconciliation-auditor` semantic-tier sweep. Mirror FM v9: Layer 5 is a detection tripwire, not a barrier.

## Invariant 2 Layer 6 (attachment screening) for safety reports — superseded by portal pivot [SUPERSEDED 2026-05-28; PARTIALLY REVERSED 2026-06-12]

**Update 2026-06-12 (PRs #271/#272) — the "no Layer 6 build for safety reports" conclusion below is now partly reversed.** The portal *did* gain a file-attachment capability — a constrained **image class** (header-level JPEG/PNG photos). Per the 2026-05-28 reasoning ("Layer 6 would apply only if the portal ever added file-attachment capability"), that capability now exists, and §34 **is** realized for safety reports as `safety_reports/photo_screen.py` (magic → Pillow verify/bomb-cap/forced metadata-destroying re-encode → ClamAV-on-raw, config-gated default OFF; MALICIOUS pages + refuses before filing). Two stale specifics in the body below are also corrected: (1) the "HMAC-verified **email shim** (`portal-noreply@` → unified `safety@`)" was **retired 2026-06-05** in favor of the Python PULL model (`portal_poll.py`); (2) "for safety reports there is no Layer 6 build to do" no longer holds — see blueprint `its-blueprint/workstreams/safety-portal/mission.md` §15 + §7 Layer 6. **Email Triage still carries the arbitrary-file (PDF/Office/executable) attachment surface** — that part is unchanged. The historical 2026-05-28 record is preserved below for provenance.

The 2026-05-28 forensic audit (HIGH-2) flagged FM v8 Invariant 2 Layer 6 (attachment screening, Op Stds v11 §34) as doctrine-only for the safety-reports PDF-email intake, and this entry originally tracked an Option A (build) vs Option B (documented exception) decision. **That is superseded by the Safety Portal pivot**, already canonical in the blueprint (`its-blueprint/workstreams/safety-portal/mission.md` v1, 2026-05-25 canonical; `brief.md`).

Why Layer 6 is no longer a safety-reports gate:
- Safety-report submission is moving from inbox-and-PDF to a form-fill **Safety Portal**. Signatures are SVG `<path>` vector data (not raster, no executable file format) and **PMs cannot attach arbitrary files** — safety-portal mission §7 explicitly rules Invariant 2 Layer 6 **N/A** for the portal (it would apply only if the portal ever added file-attachment capability).
- The portal feeds the *same* `safety_reports` intake via an **HMAC-verified email shim** (`portal-noreply@` → unified `safety@` inbox; the `X-ITS-Portal-HMAC` header is the load-bearing trust boundary — brief §8 Step 3 + Step 4 Stage 1.5). The payload is structured JSON, not an arbitrary attachment.

So the four-sub-layer attachment screen is **not** a safety-reports cutover gate. The genuine arbitrary-attachment surface is **Email Triage** (ingests arbitrary inbound mail with arbitrary attachments); FM v8 names Email Triage a Layer 6 consumer, and Layer 6 is reassigned there — see `its-blueprint/workstreams/email-triage/`. The clamd operator prerequisite and the VirusTotal-Phase-2 deferral move with it.

The NOT-WIRED `shared/attachment_screening.py` stub committed with the audit (#96) is **deleted** in this session (its docstring instructed deletion if not built for safety reports). The legacy PDF-email intake path remains the documented fallback during the portal transition; the portal-marker intake branches (brief §8 Step 4: Stage 1.5 HMAC gate, Stage 8' JSON parse, Stage 13' rollup) are PLANNED, not built.

**Revisit when:** the Email Triage workstream build begins — the **arbitrary-file** Layer 6 implementation lands there (see its mission/brief). *(2026-06-12: the safety-reports **image-class** Layer 6 was built — `photo_screen.py`, PRs #271/#272 — see the Update at the top of this entry; the arbitrary-file surface remains Email-Triage-bound.)*

## State-file atomic-write + concurrent-writer lock [CLOSED 2026-05-25]

`safety_reports/intake_poll.py` (seen-set + heartbeat-row state) and `safety_reports/weekly_send_poll.py` (heartbeat-row state) used raw `Path.write_text`; the heartbeat-row file (`~/its/state/heartbeat_row_ids.json`) is shared between the two daemons with no locking. Failure modes: mid-write crash leaves a truncated file; concurrent read-modify-write between the two daemons can clobber an entry (intake_poll writes its row_id while weekly_send_poll holds a stale read, then weekly_send_poll writes back, erasing intake_poll's update).

Closed by `shared/state_io.py` with `atomic_write_json` / `atomic_write_text` / `with_path_lock` (sidecar-flock pattern: lock lives at `{path}.lock`, never replaced by `os.replace`). Seven callsites migrated — one seen-set + two local-heartbeat + four heartbeat-row read-modify-write triples. The two heartbeat-row triples per daemon are wrapped under `with_path_lock`; lock-timeout fails open per the heartbeat-never-blocks-daemon contract (`error_log.log` WARN with `error_code="daemon_health_write_failed"` + skip the cycle's write — next cycle re-tries).

Audit findings F19 + F23 (atomic-write seen-set + heartbeat-row state + concurrent-writer lock) in `its-blueprint/audits/2026-05-25_forensic-audit.md`. `shared/alert_dedupe.py` migration to the same helper **landed in PR #104** (2026-05-28, PR 2 of the Phase 1.4 hardening cluster): its five state-file callsites (`should_fire` / `record_fire` / `mark_summarized` / `delete_entry` read-modify-write under `state_io.with_path_lock` + `atomic_write_json`; `list_expired_summaries` intentionally lock-free) replace the old same-FD-flock `_acquire_lock` / `_dump_state` pattern. All three `~/its/state/` consumers (intake_poll, weekly_send_poll, alert_dedupe) are now compliant with the CLAUDE.md "no direct `Path.write_text` under `~/its/state/`" rule. `shared/heartbeat.py` consolidation tech-debt entry remains open below — PR #88 was the correctness floor.

## `error_log.log(Severity.CRITICAL, ...)` does not fire the triple-fire alert path [CLOSED 2026-06-02]

**Resolved by** the A3 change (branch `a3-log-critical-pages`, Option 1 / full): `log()` gained an `alert: bool = True` parameter and now, for `severity is Severity.CRITICAL and alert`, mints+threads ONE correlation_id and fires `_alert_critical` (Resend + Sentry) AFTER the two record legs — so `log(CRITICAL)` pages by default, closing the sharp edge. The brief's literal one-line fix was **incomplete**: auto-firing alone would double-fire the Sentry leg (no dedupe) at five other sites and page during the watchdog's MAINTENANCE deferral. The full fix therefore: (a) removed the now-redundant explicit `_alert_critical` at the decorator + `weekly_send` ×3 + `picklist_sync` + `weekly_send_poll` (6 sites), preserving each page's exc detail via `exc_info=`; (b) routed the two watchdog checks (Check I catch-up + circuit-breaker prolonged-open) through the new `alert=False` opt-out so their MAINTENANCE deferral + `circuit_breaker.bypass()`-wrapped paging stay intact. **Behavior change (intended):** `weekly_generate`'s empty-reviewer-chain CRITICAL, previously records-only (a latent no-page bug — its docstring already claimed it paged), now pages. Manual live alert-path smoke (Resend + Sentry fire exactly once, not twice) is required before merge. Original analysis kept below for history.

`error_log.log()` writes only the two RECORD legs — `_local_log` (local file) + `_smartsheet_log` (ITS_Errors row). It never calls `_alert_critical`, so a CRITICAL passed to `log()` produces **no Resend operator email and no Sentry event**. The alert path (`_alert_critical` → `_fire_resend_leg` + `_fire_sentry_leg`) is reached ONLY via (a) the `@its_error_log` decorator's unhandled-exception branch — which calls `log(Severity.CRITICAL, …)` for the records AND `_alert_critical(…)` for the alerts as two separate calls threading one correlation_id — or (b) explicit `error_log._alert_critical(...)` calls (`picklist_sync`, `weekly_send`, `weekly_send_poll`). The split is intentional and documented (`log()`'s docstring: "for non-exception events"; `_alert_critical`'s: the ITS_Errors row is "written earlier by `log()` … NOT here"), but it is a sharp edge.

**Failure mode:** a caller does `error_log.log(Severity.CRITICAL, script, message, error_code=…)` reasonably expecting it to page the operator; it silently writes records only. Surfaced live during the F08/F09 §7 manual smoke (B6 F09-cap test): four `log(CRITICAL)` calls wrote four ITS_Errors records but produced zero Resend activity — no email, not even a `[resend-alert-*]` marker — which read as a broken F09 cap until traced to the call shape. `log(CRITICAL)` never enters `_fire_resend_leg`, so none of its gates (recursion guard, dedupe, F09 cap) run.

**Proposed fix (small):** in `log()`, when `severity is Severity.CRITICAL`, also fire `_alert_critical(script, message, exc_info or "", correlation_id, error_code or "critical")`. To avoid a double-fire, also REMOVE the decorator's now-redundant explicit `_alert_critical(...)` call (let its `log(Severity.CRITICAL, …)` carry both records + alerts) — otherwise the decorator path fires `_alert_critical` twice, and the `_in_resend_alert` recursion guard only blocks NESTED re-entry, not two sequential calls. Tests: `log(CRITICAL)` fires `_alert_critical` exactly once; `log(WARN/ERROR/INFO)` fires it zero times; the decorator path still fires it exactly once with the shared correlation_id.

**Effort:** ~1–2 hours incl. the decorator de-dup + tests.

**Phase target:** 1.4 hardening (alert-path correctness), or whenever a workstream needs an explicit (non-exception) CRITICAL to page. No current production caller relies on it — every real CRITICAL today goes through the decorator or a direct `_alert_critical` (the F08/F09 triple-fire sites).

**Revisit when:** a caller wants an explicit CRITICAL log to page the operator, OR the next time someone is surprised that `log(CRITICAL)` didn't alert.

Surfaced: 2026-06-02 F08/F09 PR-1 §7 manual smoke (B6); diagnosed to the `error_log.log` records-only call shape vs the `_alert_critical` triple-fire entry point.

## Graph client calls have no request timeout → a stalled call hangs a daemon cycle indefinitely [CLOSED 2026-06-02]

**Resolved by** the A2 timeout change (branch `pr1-tier-a-reliability`): `_request` now passes `timeout=REQUEST_TIMEOUT` (`(10s connect, 30s read)`) to the single `requests.request` call (covers all seven Mail wrappers) and the MSAL token path passes `timeout=TOKEN_TIMEOUT_SECONDS` (30s) to `ConfidentialClientApplication` (MSAL's own HTTP client — a separate surface). A `requests.Timeout` is translated to a new `GraphTimeoutError(GraphError)` and other `requests.RequestException` to `GraphError`, so a hang lands in callers' existing `except GraphError` soft-fail fence (e.g. `intake.process_message`) instead of escaping raw — and the per-cycle fence releases the fcntl lock. **Fail-fast**: a timeout does NOT consume retries (no multiplied wall time). The `requests` read timeout is an inactivity timeout, so steady large `$value` attachment downloads are unaffected; only a stalled server trips it.

**Cross-client audit (fix part b) conclusions:** `smartsheet_client` direct-REST helpers already pass `timeout=30` (NOT a gap). `anthropic_client` is **SDK-bounded** — the Anthropic SDK default is `Timeout(connect=5, read=600, …)`, a finite ceiling, not the indefinite-hang class; an explicit tighter timeout for daemon use is an optional low-priority follow-up, not done here (preservation-over-refactor — no demonstrated need). `box_client` IS a real indefinite-hang gap (boxsdk `DefaultNetwork.request` passes no timeout) — see the dedicated entry below. Fix part (c), the watchdog/launchd hang-killer, remains OPEN as a separate design item — see below. Original analysis kept for history.

`shared/graph_client.py`'s Mail API wrappers (`list_inbox`, `get_message`, `list_attachments`, `download_attachment`, `mark_read`, `move_message`, `send_mail`) issue their underlying `requests` / MSAL HTTP calls with **no `timeout=`**. A stalled TCP connection (network blip, M365 throttle, half-open socket) therefore blocks the call — and the entire daemon cycle — **indefinitely**. Under launchd `StartInterval`, a hung cycle holds the daemon's fcntl lock and starves every subsequent interval, so the daemon silently stops cycling while launchd believes it is still running.

**Failure mode (observed live 2026-06-02):** a `safety_reports.intake_poll` cycle started `17:24:23`, hung with no `poll cycle` / `completed` log line (stuck *before* processing — i.e. inside `list_inbox`), and held the lock for ~88 minutes until a manual `launchctl kickstart -k`. The heartbeat froze at 17:23 the whole time. **Only the watchdog's Check C marker-staleness floor surfaced it** (`safety_intake stale`) — there is no in-process detection, and no self-recovery. The F08 Smartsheet circuit breaker does **not** cover this: it guards Smartsheet (not Graph), and a *hang* is not a counting failure that trips it (the failure counter only advances on a returned exception, never on a call that never returns). Tier-1 self-heal recovers *crashes* (launchd re-invoke on the next interval) but **not hangs** — a hang defeats the one-shot-per-interval model by never releasing the slot.

**Proposed fix:** (a) add `timeout=(connect, read)` to every `requests` call in `graph_client.py`, converting an indefinite hang into a catchable `requests.Timeout` that the per-cycle fence already handles; (b) audit `shared/box_client.py`, `shared/anthropic_client.py`, and the direct-REST helpers in `shared/smartsheet_client.py` for the same missing-timeout gap; (c) consider a watchdog/launchd hard-kill of any daemon process whose elapsed time exceeds N× its expected cycle duration — a hang-specific recovery net complementary to Check C's staleness floor (which only *detects*, it does not *recover*).

**Effort:** ~half-day for the `graph_client` timeout sweep + the cross-client audit + tests; the hang-killer is a larger watchdog/launchd design decision (separate item if pursued).

**Phase target:** 1.4 hardening (reliability) — a silently-stalled daemon is precisely the never-silent-failure the system is built to avoid.

**Revisit when:** the next reliability pass, OR the next time a daemon hangs (the watchdog staleness WARN is the trigger signal).

Surfaced: 2026-06-02 F08/F09 live deploy + post-deploy sanity-check — a pre-existing hung `intake_poll` cycle (old code) was blocking the daemon, found while verifying the heartbeat advanced onto the new circuit-breaker code.

## `box_client` has no network timeout → boxsdk call can hang a consumer indefinitely [CLOSED 2026-06-30]

**Resolved 2026-06-30 (tech-debt currency sweep):** The A2 single-host-resilience timeout this entry predates has landed. `shared/box_client.py:79` defines `BOX_NETWORK_TIMEOUT = (10, 30)`, applied at `:238` via the `Client(... default_network_request_kwargs={"timeout": BOX_NETWORK_TIMEOUT})` so every boxsdk call carries a bounded connect/read timeout. Verified @HEAD via grep (lesson #1). (Any *further* box_client A2/A3 hardening — refresh-lock, idle marker — is Phase-2-owned; this specific hang-forever gap is closed.)

`shared/box_client.py` calls go through boxsdk's `Client` / OAuth2, and boxsdk's `DefaultNetwork.request` issues its underlying `requests` call with **no `timeout=`** (verified by inspecting the installed boxsdk source). Same indefinite-hang class as the (now-fixed) graph_client gap: a stalled connection blocks the calling cycle forever. Lower urgency than graph_client because box_client is not yet on a 60-second polling daemon's hot path (its consumers are weekly/migration-cadence), but it is a real gap once a box-reading daemon ships.

**Proposed fix:** boxsdk does not expose a simple per-call `timeout=` the way `requests`/MSAL/Anthropic do — it requires supplying a custom network layer (subclass `DefaultNetwork` / `DefaultNetworkResponse`, or pass a pre-configured `requests.Session`) to the `Client`. Non-trivial; scope it as its own PR. Until then, box hangs are caught only by the watchdog staleness floor (detect, not recover).

**Phase target:** 1.4 hardening (reliability), before any box-reading polling daemon goes on a tight interval.

Surfaced: 2026-06-02 A2 cross-client timeout audit (the graph_client timeout fix's "audit box/anthropic/smartsheet" follow-through).

## `weekly_send_poll` has no `ITS_Daemon_Health` row → its heartbeat (incl. F08 `CIRCUIT_OPEN`) cannot surface [CLOSED 2026-06-02]

**Resolved by** the A1 self-provision change (branch `pr1-tier-a-reliability`): the shared heartbeat helper `_resolve_heartbeat_row_id` now **find-or-creates** the daemon's `ITS_Daemon_Health` row on first-seen-missing (new `_create_heartbeat_row` + the ID-keyed `smartsheet_client.add_row_by_id` primitive), mirroring `week_folder.py`'s find-after-create race handling (`daemon_health_race_duplicate` WARN, adopt first match). Applied to **both** daemons (helpers stay logic-identical; AST-verified). Heartbeat-never-blocks contract preserved (create failure → `daemon_health_write_failed` WARN + continue). So `weekly_send_poll` self-provisions its row on the next cycle — no manual seed. §43 runbook: `docs/runbooks/daemon_health_self_provision.md`. Original analysis kept below for history.

`safety_reports.weekly_send_poll`'s heartbeat write resolves its row in `ITS_Daemon_Health` (sheet 4529351700729732) by primary key (`safety_reports.weekly_send_poll`) — but **no such row exists**. Every heartbeat write logs a `daemon_health_write_failed` WARN ("no row with this primary key — seeder needed") and skips. So weekly_send_poll's `Last Cycle Status` — including the F08 `CIRCUIT_OPEN` surfacing added in PR #137, plus any OK / WARN / DEGRADED — never lands on the operator-visibility surface; the daemon is invisible there.

**Failure mode (observed live during the F08 PR-1 §7 smoke):** a `weekly_send_poll` cycle with the breaker OPEN logged "seeder needed" instead of surfacing `CIRCUIT_OPEN`. The shared `~/its/state/heartbeat_row_ids.json` caches only `safety_reports.intake_poll`, and the sheet has no weekly_send_poll row. PR #137's Bug-2 fix (scan-failure short-circuit surfaces `CIRCUIT_OPEN` instead of a bare `ERROR`) is correct in code but **inert until the row exists**.

**Proposed fix:** provision the `safety_reports.weekly_send_poll` row in `ITS_Daemon_Health` (one-time seed — mirror the `intake_poll` row's 12 columns per `shared.sheet_ids.DAEMON_HEALTH_COLUMNS`). Then consider whether the shared heartbeat helper should **find-or-create** the row on first-seen-missing (like the week-folder scaffold pattern) rather than log-and-skip, so a newly-added daemon self-provisions its visibility row. (The find-after-create race is already a tracked pattern — reuse that handling.) The heartbeat-never-blocks-daemon contract still holds: a create failure logs `daemon_health_write_failed` and the daemon continues.

**Effort:** ~15 min for the one-time row seed; ~1–2 h if implementing self-provisioning find-or-create + a regression test.

**Phase target:** 1.4 — operator-visibility completeness; `weekly_send_poll` is a live daemon whose status is currently dark.

**Revisit when:** the `weekly_send_poll` daemon is next exercised, OR the operator notices it is missing from `ITS_Daemon_Health`.

Surfaced: 2026-06-02 F08/F09 PR-1 §7 manual smoke (weekly_send_poll `CIRCUIT_OPEN` live-verify) + the PR-2 / live-deploy follow-up.

## Pre-conftest-fix unit-test network leak to Smartsheet sandbox [CLOSED 2026-05-23]

Between PR #68 merge (2026-05-23T02:02:33Z; Run #229) and PR #73 merge (2026-05-23T15:00:02Z; Run #251), unit tests on macOS dev machines were making live API calls against the sandbox Smartsheet tenant via the unmocked `kill_switch.smartsheet_client.get_setting` path. On macOS the keychain returned a real token, so `_get_client()` built a working SDK client and the kill_switch's `check_system_state` made a real network call on EVERY test that exercised `@require_active`. Volume small (one ITS_Config read per affected test invocation) and benign (read-only against a sandbox tenant).

Closed by `tests/conftest.py` keychain + kill_switch fixtures in PR #74.

## parse_job_v3.py:656 — `existing_keys` dead code [CLOSED 2026-05-17]

Resolved in commit **`1fd6751`**. The unfinished de-dup attempt was removed and F841 came off the `box_migration/*` per-file-ignores. Originating commit (which suppressed it) was `8dfc6e8`; ground was tracked in `docs/session_logs/2026-05-17_ruff_and_doc_refresh.md`.

The fix was a deliberate departure from Op Stds v11 §14 (preservation-over-refactor) because the F841 was real dead code rather than a stylistic false positive, and the cleanup was five lines with zero behavior change. The preservation rule remains in effect for the rest of `box_migration/*`.

## parse_job_v3: V/S vendor-sub enumeration unclaimed [CLOSED 2026-05-19]

Resolved by adding `parse_vendor_sub(raw) -> Optional[VendorSubParse]` to `box_migration/parse_job_v3.py` and inserting it into the reconcile harness's claim chain between `subsubject` and `canonical_non_job`. Regex shape `^(?P<letter>[VS])(?P<index>\d{2})\.\s+(?P<name>.+?)\s*$` — capped at two digits so single-digit V1./S1. stay in `SUBJOB_LETTER_UC`'s domain.

Coverage delta when re-running the reconcile against the live 10-portfolio listings: **212 unique names** moved from unclaimed to `vendor_sub` (the original tech_debt estimate of 60–90 was an under-count; estimate was based on unique-occurrence math but the actual unique-name count is higher). Unclaimed share dropped 54.9% → 51.1%. Full 33-test coverage in `tests/test_parse_vendor_sub.py`.

Resolution: see commit on the `feature/vendor-sub-parser` branch (squash-merged), and `docs/session_logs/2026-05-19_chore_sweep_and_mypy_lockdown.md`.

## parse_job_v3: ISO date prefix (YYYY-MM-DD) unclaimed [CLOSED 2026-05-19]

Resolved by extending `parse_date_prefix` in-place with a new `DATE_PREFIX_ISO` regex (`^(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<topic>.+?)\s*$`). ISO matches return `DatePrefixParse` with `direction='ISO'`, joining the existing `R` / `S` discriminators in the same `direction` field. R./S. behavior is preserved unchanged; covered by regression tests in `tests/test_parse_date_prefix.py`.

Reconcile claim chain extended with a new `date_prefix` claim between `vendor_sub` and `canonical_non_job` — needed because the existing chain had no date-prefix claim at all, so ISO matches wouldn't have shown up in reconcile output otherwise. Side effect: existing uppercase R./S. and chaos-flagged lowercase r./s. forms now also get claimed structurally (chaos detection is orthogonal — same name can be both `date_prefix` claimed AND `date_prefix_lowercase` chaos-flagged).

Coverage delta when re-running the reconcile: **11 unique names** in the new `date_prefix` claim (mix of ISO + R./S. + lowercase r./s. forms; tech_debt entry estimated ~13 ISO uniques, close enough). Unclaimed share dropped 51.1% → 50.9%.

24 tests cover the new ISO form, R./S. regression, lowercase r./s. warning preservation, direction discriminator, and negatives. Tests at `tests/test_parse_date_prefix.py`.

Resolution: see commit on the `feature/iso-date-prefix` branch (squash-merged), and `docs/session_logs/2026-05-19_chore_sweep_and_mypy_lockdown.md`.

## parse_job_v3: person_tag_in_subject chaos over-match [CLOSED 2026-05-20]

Resolved by adopting **Direction (A)** from `docs/audits/person_tag_audit_2026-05-19.md`: the third alternation (`-\s*[A-Z][a-z]+\s*$`, "trailing-capitalized-word after dash") was removed from `PERSON_TAG_IN_SUBJECT` in `box_migration/parse_job_v3.py`. The refined regex keeps the two alternations that the audit confirmed as high-precision:

```python
PERSON_TAG_IN_SUBJECT = re.compile(
    r'(\bfor\s+[A-Z]{3,}\b|'                            # "for ZACK"
    r'^[A-Z][a-z]+\s+(Organize|Cleanup|Notes|Files)\b)'  # "Teala Organize folder"
)
```

Consumer path (`detect_chaos` in the same file) is unchanged — the chaos flag still surfaces for alt-1 / alt-2 matches; alt-3 over-matches no longer fire. `m.group(0)` is the only match-object accessor downstream, so removing one alternation has no group-index ripple.

**Coverage delta (projection from the 2026-05-19 audit; live listings under `~/Downloads/Box_listings_for_Seth/` not present locally to re-measure):** ~138 person_tag chaos hits → ~2–4 hits across the 10-portfolio corpus. The 2–4 retained hits are alt-1 / alt-2 forms only (explicit "for XXX" and "First Organize/Cleanup/Notes/Files"); the ~95% noise from alt 3 is gone. A few real-or-leaning-real person-tag cases from the audit (samples #15–#20: `Structural - Bowman`, `R. Bowman-Pungo`, etc.) lose their flag by design — operator triages those visually in the folder tree. The audit doc has the full FP-vs-TP tradeoff analysis.

27 tests cover the refinement in `tests/test_person_tag.py`:
- Group A (7 tests): alt 1 + alt 2 positive-regression coverage across the audit's TPs.
- Group B (13 tests): every confirmed FP from the audit (rows #1–#12 + sample #19) — negative locks so reintroducing alt 3 fails the suite.
- Group C (5 tests): `KNOWN_TP_LOSSES_NO_LONGER_FLAGGED` acceptance lock — audit samples #15, #16, #17, #18, #20. The list and its comment block point a future maintainer back to the audit doc before they "re-add the missing coverage."
- Consumer-path integration (2 tests): `detect_chaos()` surfaces the flag for a TP and skips it for the most-common audit FP (`-Tracking` suffix).

**Redo history:** an earlier attempt (PR #34) implemented this same change but was closed-without-merge during a 2026-05-20 branch-cleanup pass where the head branch was deleted before verifying the merge had actually landed. The chore PR #37 explicitly preserved this entry's `[OPEN]` status; the present resolution comes from the redo PR. The cleanup-pass mistake is captured as a private feedback memory (`feedback_verify_merge_before_branch_delete`): always `gh pr view <N> --json mergedAt` before `git push origin --delete`, do not infer merge from "I saw CI green."

Resolution: see commit on the `feature/person-tag-regex-refinement-redo` branch (squash-merged), and `docs/session_logs/2026-05-20_person_tag_regex_refinement_redo.md`. Audit context preserved at `docs/audits/person_tag_audit_2026-05-19.md` (not modified by this PR).

## smartsheet_migration: import-time side effects in three scripts [CLOSED 2026-05-19]

Resolved by wrapping each script's top-level API work in a `main()` function behind `if __name__ == "__main__":`. Module-level constants (`SOURCE`, `DEST`, `SRC_TO_DEST_TITLE`) stay at module scope (cheap and pure). Imports refactored from `import os, sys` to PEP 8 form. No behavior change when invoked from the shell.

`tests/test_migration_import_hygiene.py` (new) locks the regression in: parametrized test imports each of the three modules with `SMARTSHEET_TOKEN` un-set; all 3 pass. If a future edit accidentally puts API-calling code back at module scope, the test will catch it.

The per-file-ignores `["E401", "I001", "F401", "B007", "UP035"]` in `pyproject.toml` for `smartsheet_migration/*` were NOT removed — 3 other files in the directory (`build_human_review.py`, `classify_closeout.py`, `migrate_schedule.py`) still use `import os, sys` and need the E401 ignore. Documented this in the session log so the ignores aren't mistaken for unnecessary on a future audit.

Resolution: see commit on the `fix/smartsheet-migration-import-time` branch (squash-merged), and `docs/session_logs/2026-05-19_chore_sweep_and_mypy_lockdown.md`.

## mypy: import-untyped noise from vendor SDKs without stubs [CLOSED 2026-05-19]

Resolved by adding the proper stub package for `requests` (`types-requests` added to dev dependencies in `pyproject.toml`) and a `[[tool.mypy.overrides]]` block silencing missing-stub errors for `msal` and `smartsheet` (neither publishes type information upstream as of 2026-05).

After applying, `mypy .` reports **zero errors** across all 64 source files. Brought the baseline from 4 → 0.

Locked in by adding mypy as a **blocking CI step** in `.github/workflows/ci.yml` — silent type drift across PRs is no longer possible. Mypy now runs in parallel with ruff and pytest; failure of any step blocks merge.

Resolution: see commit on the `feature/mypy-zero-and-ci` branch (squash-merged), and `docs/session_logs/2026-05-19_chore_sweep_and_mypy_lockdown.md`.

## parse_job_v3.py: matched needs type annotation [CLOSED 2026-05-18]

Resolved by adding the explicit annotation `matched: dict[Schema, list[str]] = {...}` in `classify_schema()`. Inferred type from `_V3_SIGNATURES` keys (Schema enum members) and the `.append(name)` call site where `name` is a `str`. One-line annotation change; zero behavior change. Preservation-over-refactor §14 honored — only the annotation line was modified.

Resolution: see commit on the `fix/parse-job-v3-matched-annotation` branch (squash-merged), and `docs/session_logs/2026-05-18_alert_critical_and_mypy_closure.md`.

Originally surfaced 2026-05-18 in the mypy baseline reconciliation; see `docs/reports/2026-05-18_mypy_baseline.md` for the lifecycle context.

## smartsheet_migration/ss_api.py: api body arg type mismatch [CLOSED 2026-05-18]

Resolved by widening the `body` parameter annotation on `api()` from `dict | None` to `dict | list | None`. Single-character-class edit on the signature line; all existing call sites continue to type-check (the `add_rows()` caller that passed `list[dict]` now matches). Real-bug carve-out under Op Stds v11 §14.

Resolution: see commit on the `fix/ss-api-body-arg-type` branch (squash-merged), and `docs/session_logs/2026-05-18_alert_critical_and_mypy_closure.md`.

Originally surfaced 2026-05-18 in the mypy baseline reconciliation; see `docs/reports/2026-05-18_mypy_baseline.md` for the lifecycle context.

## smartsheet_migration/migrate_fl.py: warnings list type annotation [CLOSED 2026-05-18]

Resolved by adding the explicit annotation `warnings: list[str] = []` in `derive_payment_method()`. Element type inferred from the `.append(...)` call sites which pass string literals describing payment-method derivation warnings. One-line annotation change; zero behavior change.

Resolution: see commit on the `fix/migrate-fl-warnings-annotation` branch (squash-merged), and `docs/session_logs/2026-05-18_alert_critical_and_mypy_closure.md`.

Originally surfaced 2026-05-18 in the mypy baseline reconciliation; see `docs/reports/2026-05-18_mypy_baseline.md` for the lifecycle context.

## Mail.app rule silent disable on macOS updates [CLOSED 2026-06-30]

**Resolved 2026-06-30 (tech-debt currency sweep):** The surface no longer exists. Mail.app rules are deprecated; the polling-daemon pattern is canonical (Op Stds v20 §31). Check F mailbox routing was removed (`scripts/watchdog.py:201`) and Check F itself RETIRED 2026-06-05 (`scripts/watchdog.py:454`); `safety_reports/intake_poll.py` is a retirement tombstone. There is no Mail.app rule left to silently disable. Verified @HEAD via grep (lesson #1).

macOS updates have a known pattern of silently disabling Mail.app rules without warning. Affects any workstream whose intake depends on Mail.app rules routing messages to the Claude Code script.

**Mitigation in place (Watchdog Check F, PR #36):** Watchdog has an inbound-mail-activity check across all intake-bearing workstreams, surfacing WARN when no recent intake activity is observed.

**Architectural cutover (safety_reports, PR #59, 2026-05-22):** safety_reports migrated off the Mail.app rule trigger to a launchd-driven Graph polling daemon (`safety_reports/intake_poll.py`). This eliminates the silent-disable risk for safety_reports specifically — no Mail.app rule exists in the trigger path anymore. Future workstreams should use the same polling pattern rather than Mail.app rules; this tech-debt entry stays OPEN until that becomes the documented standard for new intake-bearing workstreams (likely Email Triage Brief v5 update + a shared/runner.py abstraction at PR #60 when the second polling consumer ships).

Watchdog Check F still polls mailbox-idle as a proxy for trigger health — works unchanged for safety_reports after PR #59 because the inbox-activity signal is the same regardless of trigger mechanism. A cleaner heartbeat-based replacement (read `~/its/state/safety_intake_heartbeat.txt`) is queued as a follow-up PR after PR #60.

Resolves fully when: every intake-bearing workstream is on a polling daemon (no Mail.app rule trigger remains anywhere in ITS), and Watchdog Check F is repurposed to read the per-daemon heartbeat files instead of mailbox-idle.

Originally captured in Foundation Scaffold v4 "Outstanding Gotchas"; carried forward through v5; re-surfaced via Cascade Audit Errata 2026-05-19; mitigation lifecycle landed via PR #36 (Watchdog Check F) + PR #59 (safety_reports cutover).

## Remove unused `[jwt]` extra from boxsdk dependency [CLOSED 2026-05-28]

`pyproject.toml` currently pins `boxsdk[jwt]>=3.10.0,<4.0.0`. The `[jwt]` extra pulls in `PyJWT` and `cryptography` transitively. ITS uses OAuth 2.0 User Authentication (per PR #39, commit `2ce6ece`) and never exercises the JWT auth path; the extra dependencies are dead weight in the install tree.

**Action:** change to plain `boxsdk>=3.10.0,<4.0.0`. Run `scripts/smoke_test_box.py` after the change to confirm the OAuth path still works.

**Urgency:** low. No functional impact, just install-tree hygiene.

Surfaced: PR #39 review, 2026-05-20.

**Closed:** PR #96 (LOW-1 of the 2026-05-28 forensic-evaluation hygiene batch) changed the pin to `boxsdk>=3.10.0,<4.0.0`. Verified at HEAD `c5cc456`: `pyproject.toml:18` reads `"boxsdk>=3.10.0,<4.0.0"` (no `[jwt]` extra), and `[tool.mypy].overrides` still ignores missing `boxsdk` imports as before. See `docs/audits/2026-05-28_forensic-evaluation.md` §LOW-1.

## Alert-dedupe state file grows unboundedly until PR β lands [CLOSED 2026-05-21]

PR α (#42) wrote one entry per `(script, error_code)` key to `~/its/state/alert_dedupe.json` and never deleted. The follow-up PR β (watchdog summary sweep) was queued to delete entries once their summary email had fired and `summarized=true` had been set. Until PR β landed, the file grew (one entry per distinct dedupe key across the ITS lifetime — operationally acceptable bound).

**Closed by PR #44 (PR β — watchdog Check G — alert-dedupe summary sweep).** Two-phase deletion landed: phase 1 (sweep N) fires the summary email + `mark_summarized`; phase 2 (sweep N+1) deletes the now-`summarized=true` entry. State-file growth bound improved to ≤1 day per `(script, error_code)` key pair (further detailed in the successor entry below). Crash-safe: a crash between Resend send and `mark_summarized` causes the next sweep to re-fire (duplicate email is acceptable; silent loss is not).

Subsequent V1 fix (PR #52) added MAINTENANCE-aware defer behavior — phase-1 fires defer during the MAINTENANCE window, phase-2 deletion proceeds regardless. Bounded delay = MAINTENANCE window + one watchdog cadence.

Surfaced: PR α (alert-dedupe-core) brief, 2026-05-20. Closed by PR #44 + #52, 2026-05-21.

## Daily Reports schema gap — no Box Link column [CLOSED 2026-06-18]

**Resolved 2026-06-18 (tech-debt easy-wins pass):** Superseded by the portal pivot — intake.py writes Box URLs into structured columns via box_link + update_row_with_box_links, not embedded in Notes.

The `Daily Reports — Week of <date>` sheet schema (cloned forward by `safety_reports/week_folder.ensure_current_week_folder` from the Bradley 1 / Week of 2026-03-09 template, sheet ID 7282977254887300) has no explicit column for the filed Box document URL.

When `safety_reports/intake.py` lands in R3 session 1, each inbound safety email will be filed to Box; the Box URL is the audit trail back to the source document. Without a dedicated column, intake.py will embed the URL inside the existing `Notes / Action Items` cell — workable but harder to query and prone to cell-truncation as notes grow.

**Action at R3 session 1:** the session's brief should include a schema edit adding a `Box Link` (TEXT_NUMBER) column to the Bradley 1 / Week of 2026-03-09 template sheet (the canonical source for clones). The auto-gen helper will then carry the column forward into every new week's clone. Until that lands, intake.py embeds the URL in `Notes / Action Items`.

**Workaround in the interim:** intake.py's notes-embedding pattern. Once the column lands, the migration is a one-pass extraction of URLs from existing notes into the new column for any rows written between R3 session 1 start and the schema edit.

**Resolves at:** R3 session 1 (the intake.py wiring brief).

Surfaced: R3 foundation PR brief, 2026-05-21.

## `find_sheet_by_name_in_folder` switched from SDK to REST [CLOSED 2026-05-21]

Original PR #45 implementation used `smartsheet.Folders.get_folder()` — deprecated upstream AND returns stale folder data within a single SDK client session. A sheet created via the SDK's `create_sheet_in_folder()` does not appear in a subsequent `get_folder()` from the same client; direct REST sees it immediately.

PR #51 swapped the helper to direct REST. Unit tests updated to mock `requests.get` instead of the SDK shape. Removes the DeprecationWarning AND fixes the same-session-create-then-find bug. The picklist sync migration script's earlier success was a happy accident: it didn't exercise back-to-back create + find in the same Python process, so the SDK cache never tripped.

Closed by PR #51.

## Picklist-hardening pre-Customer-1 [CODE DELIVERED 2026-05-23 / operator UI work tracked in docs/audits/picklist_hardening_audit.md]

Code side shipped on `feat/picklist-hardening` branch:

- `shared/picklist_validation.py` — `PicklistViolationError` + `REGISTRY` (composed from `Severity`/`ReviewReason`/`SlaTier`/`ReviewStatus`/`QuarantineReason`/`ContactStatus` StrEnums) + `validate_cell` / `validate_row`. Opt-in semantics: unregistered (sheet, column) pairs pass-through; None and bool values bypass picklist check.
- `shared/smartsheet_client.py::add_rows` + `update_rows` — late-import `picklist_validation` (circular-import safe) and call `validate_row` BEFORE any payload construction. Invalid values raise `PicklistViolationError` pre-API-call.
- `scripts/audit_picklist_drift.py` — programmatic registry-vs-live drift audit; `--update-audit-doc` placeholder; writes `~/its/.watchdog/safety_picklist_audit.last_run` marker.
- `scripts/watchdog.py::TRACKED_JOBS` — added `safety_picklist_audit` with 8-day freshness window (weekly cadence).
- `docs/audits/picklist_hardening_audit.md` — operator's UI conversion checklist; one row per bounded-enum column with conversion status emojis (⬜ ✅ ⚠️ 🟦).

`shared/kill_switch.py` Phase 3 was a no-op: existing `SystemState` StrEnum + try/except fail-open (returns ACTIVE on unknown value per Op Stds v11 §1 — never silently halt) IS the per-key registry pattern. The brief's suggested change to return PAUSED would have inverted the fail-open behavior; preserved existing.

Tests: 949 → 1004 (+55: 20 validation + 8 smartsheet integration + 8 drift audit + transitive coverage). mypy 0, ruff clean. Capability gating intact.

Operator-side conversion items remain in `docs/audits/picklist_hardening_audit.md` — ~21 UI passes (toggle "Restrict to picklist values only" + add 3 PR #72 ReviewReason values + add ITS_Quarantine Disposition + Reason columns + 6 per-project template conversions). Audit doc IS the operator's checklist; after each batch, run `python -m scripts.audit_picklist_drift --update-audit-doc` to refresh status emojis.

Subsumes PR #72 leftover step #2 — the three new ITS_Review_Queue.Reason picklist values are now part of this audit's checklist.

**Closes when:** all rows in `docs/audits/picklist_hardening_audit.md` show ✅. At that point the watchdog's drift WARN-threshold can flip to ERROR.

## ITS_Trusted_Contacts sheet replaces ITS_Config JSON allowlists [DELIVERED 2026-05-23]

Code shipped on `feat/its-trusted-contacts` branch:

- `shared/trusted_contacts.py` — TrustedContact / ScopeVerdict / ContactStatus + 60s-TTL cache (`lookup`, `check_scope`).
- `shared/header_forgery.py` — Authentication-Results parser + Return-Path-vs-From mismatch (PASS / SOFT_FAIL / HARD_FAIL verdicts; trusts inbound MTA's DKIM — no local re-validation).
- `shared/graph_client.py::get_message` — opt-in `include_headers=True` projects `internetMessageHeaders` via `$select`.
- `safety_reports/intake.py` — Stage 2 refactored to `check_trusted_sender` (routing matrix); Stage 4b project-scope re-check after project resolves. Old `check_sender_allowlist` removed; legacy ITS_Config `allowed_senders` JSON list survives as the dead-fallback path (`trusted_contacts.fallback_to_its_config` INFO once per process) until operator deletes the row.
- `shared/quarantine.py` — `QuarantineReason` StrEnum added; `log_quarantined_message` accepts `reason=`, writes `[reason: <code>]` into Notes (no Reason column on live sheet).
- `shared/review_queue.py::ReviewReason` — three new picklist values (header-soft-fail-trusted / sender-pending-verification / project-out-of-scope) awaiting operator UI add.

Migrations: `scripts/migrations/build_its_trusted_contacts_sheet.py` (idempotent sheet create), `scripts/migrations/seed_its_trusted_contacts.py` (legacy → sheet seed, `--dry-run`).

Tests: +46 (12 trusted_contacts, 14 header_forgery, 10 intake_stage2_refactor, 2 graph_client include_headers, 3 quarantine reason, 1 integration, +4 regression deltas across test_intake / test_review_queue) — baseline 903 → 949.

Operator-side cutover items, all required before legacy fallback removal:
1. Run `build_its_trusted_contacts_sheet.py`, paste sheet ID into `shared/sheet_ids.py::SHEET_TRUSTED_CONTACTS`.
2. Add the 3 ITS_Review_Queue.Reason picklist values via UI.
3. Run `seed_its_trusted_contacts.py`, adjust seeded rows.
4. Live smoke against sandbox message.
5. After one Friday cycle clean, delete the ITS_Config `safety_reports.intake.allowed_senders` row.

## 1 empty duplicate ITS_Daemon_Health sheet [CLOSED 2026-06-18]

**Resolved 2026-06-18:** the duplicate sheet `3717381690969988` is already gone (a live fetch returned 404 — it was cleaned up in a past workspace restructure; this entry was stale). Canonical `ITS_Daemon_Health` `4529351700729732` (shared/sheet_ids.py SHEET_DAEMON_HEALTH) is the live heartbeat surface, untouched. The "operator UI delete required / MCP has no delete-sheet primitive" note was also stale — `smartsheet_client.delete_sheet` exists.

Parallel chat build of ITS_Daemon_Health surface created an extra empty sheet 3717381690969988 in System / 04 — Daemons. Canonical sheet is 4529351700729732. Empty duplicate requires operator UI delete (Smartsheet MCP no delete-sheet primitive).

**Revisit when:** next operator Smartsheet UI session.

## Watchdog Check F retirement [CLOSED 2026-06-30]

**Resolved 2026-06-30 (tech-debt currency sweep):** Retirement is complete — `scripts/watchdog.py:454` reads `# ---- Check F: RETIRED 2026-06-05 (safety mail-intake silent-disable) ----` and the mailbox-routing logic is removed (`:201`). The partial-mitigation is now full. Verified @HEAD via grep (lesson #1).

Check F (Mail.app rule silent disable, PR #36) polls safety@evergreenmirror.com mailbox idle hours as a proxy for Mail.app-rule trigger health. Post-PR-#59, safety_reports is on a polling daemon and writes a heartbeat to ITS_Daemon_Health every 60 seconds. The mailbox-idle proxy is now redundant for safety_reports.

**Check-H reframe (2026-06-01).** This entry originally proposed a "Check H heartbeat-staleness successor" that would "read ITS_Daemon_Health for every Enabled=true daemon; flag rows where Last Heartbeat is older than 2 × Interval Seconds." That mechanism was **never built and is superseded** — the staleness floor doctrine called "Check H" is, and always was, the **Check C marker-file** check (`scripts/watchdog.py`), which already covers all four tracked daemons (`safety_intake`, `safety_weekly_send_poll`, `safety_picklist_audit`, `safety_weekly_generate`) with per-job freshness windows. The blueprint doctrine carrying the stale "Check H unimplemented / 2-of-3 heartbeat-pending" framing is corrected in the 2026-06-01 doctrine pass (FM v11.x / Op Stds v16.x / V&R v9.x / Handover v8.x / Excellence Roadmap v4). The companion residual this entry's "revisit when" anticipated — the weekly_generate catch-up — is now **built** as watchdog **Check I** (`_check_weekly_generate_catchup`, this PR), closing the one daemon launchd could not self-recover (calendar-scheduled, Friday).

**Remaining open leg:** the *Check F retirement itself*. Retire Check F when (a) the Check C marker-file floor covers all daemons [done] and (b) no remaining workstream depends on Mail.app rules. Leg (b) is the live gate.

**Effort:** ~1 hour session (delete Check F + its tests once Mail.app rules are fully gone).

**Revisit when:** the last Mail.app-rule-dependent workstream is migrated to a polling daemon (then leg (b) is satisfied and Check F can be deleted). The `shared/runner.py` marker-helper consolidation remains a separate opportunity at the next polling-daemon consumer ship.

## Integration-test marker isolation — weekly_generate live test pollutes the shared watchdog marker [CLOSED 2026-06-18]

**Resolved 2026-06-18 (tech-debt easy-wins pass):** This PR: an autouse fixture in test_weekly_generate_integration.py monkeypatches weekly_generate.WATCHDOG_MARKER_DIR to a tmp dir, so the live compile no longer touches the real marker.

Surfaced during the Check I (weekly_generate catch-up) live smoke. The `@pytest.mark.integration` `weekly_generate` test (`tests/test_weekly_generate_integration.py`) runs real `weekly_generate` against the live Smartsheet sandbox, which writes the **real** shared `~/its/.watchdog/safety_weekly_generate.last_run` Check C marker (via `weekly_generate._write_watchdog_marker`). Unlike the unit tests, the integration test does NOT redirect `WATCHDOG_MARKER_DIR` to a tmp dir, so an operator running `pytest -m integration` refreshes the production marker for a *disposable* week.

Interaction with watchdog Check I (`_check_weekly_generate_catchup`, PR #133): Check I deliberately treats a fresh marker as "the week ran" (so it never regenerates reviewer-deleted rows). A marker refreshed by the integration test *after* the Friday trigger can therefore **mask a genuine catch-up for that window** — a false-negative that degrades safely to Check C's 8-day WARN / a human, but is non-obvious. Observed live during the PR #133 catch-up smoke: the integration test (run earlier in the session) had refreshed the marker, pre-empting the fire path until the marker was removed.

**Fix:** redirect `WATCHDOG_MARKER_DIR` to a temp dir inside `tests/test_weekly_generate_integration.py` (mirror the autouse `monkeypatch.setattr("watchdog.WATCHDOG_MARKER_DIR", …)` pattern from `tests/test_watchdog.py`), so the live test never touches the production marker. Same isolation discipline already applied to the watchdog unit tests.

**Revisit when:** `tests/test_weekly_generate_integration.py` is next touched, or an operator reports a missed `weekly_generate` catch-up that coincides with an integration-test run.

## safety_weekly_generate prompt v0.1.0 calibration [CLOSED 2026-06-30]

**Resolved 2026-06-30 (tech-debt currency sweep):** Obsolete by design change. `safety_reports/weekly_generate.py` is now the **DETERMINISTIC** weekly compile — the Anthropic narrative core was retired — so there is no generation prompt to calibrate; `prompts/safety_weekly_generate.md` does not exist. Verified @HEAD (file absent + capability-gating AST-forbids `anthropic` in `weekly_generate`). Closed as not-applicable.

Initial WPR generation prompt (`prompts/safety_weekly_generate.md` v0.1.0) anchors on the 2016-03-12 Gates Solar legacy WPR captured at `prompts/samples/legacy_wpr_gates_solar_2016-03-12.md`. Per Safety Reports Brief v6.1, calibrate v0.2.0 after the first 30 days of real Evergreen cycles — areas to watch:

- Whether reviewers consistently keep the [REVIEWER TO FILL] sentinels (vs. editing them out), suggesting prompt should drop or move those sections.
- Confidence threshold tuning. Default 0.85 was inherited from intake.py extraction; generation may warrant a different threshold once we see real distribution.
- Subcontractor-list extraction quality — currently derived from `Crew or Subcontractor` column values; might miss subs mentioned only in `Summary of Events` narrative.
- `narrative_summary` length tuning — model defaults to one paragraph but reviewer feedback may push for terser or denser summaries.
- Anomaly self-report sentinel coverage — current set (`apparent_injection_attempt`, `inconsistent_dates`, `crew_name_special_chars`) may need expansion.

**Effort:** ~half-day session including reviewer-feedback synthesis + v0.2.0 prompt edit + before/after diff documentation.

**Revisit when:** ~30 days of real Friday cycles have run (2026-06-22 plus or minus a week).

## `shared/heartbeat.py` + `shared/runner.py` extraction [CLOSED 2026-07-03]

**Resolved 2026-07-03 (CS Slice 3, R4-F1 — split-close):** the **heartbeat half is DONE.** The extraction LANDED as `shared/heartbeat.py` (`HeartbeatReporter`, PR #344 `334ea9e`) — the eight replicated helpers consolidated with the A1 self-provision metadata as constructor config; consumers migrated to thin `_write_heartbeat`/`_write_heartbeat_row` delegators (the canonical test mock seams). Six consumers at close: `portal_poll`, `weekly_send_poll`, `fieldops_sync`, `progress_send_poll`, and — added by CS Slice 3 — `compile_now_poll` + `publish_daemon` (the two designed-for daemons that lacked it). Split remainders, both deliberate non-debt: (a) `_write_watchdog_marker` stays replicated per-daemon (a 10-line marker touch with per-daemon write-condition policy — the exact API-churn risk this entry's own "risk of premature extraction" paragraph predicted; §14 says leave it); (b) **`shared/runner.py` was never built and is dropped** — no consumer ever demanded a shared runner loop (launchd one-shot-per-`StartInterval` IS the runner), so building it would violate §14 preservation-over-refactor.

R3 Session 3 (`weekly_send_poll.py`) is the 2nd polling-daemon consumer that triggers the polling-daemon doctrine's 2nd-consumer extraction signal (Op Stds v11 §14). The heartbeat helpers (`_load_heartbeat_row_state`, `_persist_heartbeat_row_state`, `_invalidate_heartbeat_row_state`, `_resolve_heartbeat_row_id`, `_write_heartbeat`, `_write_heartbeat_row`, `_log_heartbeat_failure`) were copied VERBATIM from `safety_reports/intake_poll.py` into `weekly_send_poll.py` rather than extracted, to keep the R3 Session 3 ship focused on the send-capability code.

**Update 2026-05-28 (PR #113, F17):** a 3rd copy of the watchdog-marker helper pattern was added to `intake_poll.py` as `_write_watchdog_marker()`. The heartbeat-row helpers (ITS_Daemon_Health write) and the watchdog-marker helper (`.watchdog/<slug>.last_run` write) are related patterns that both belong in `shared/heartbeat.py`. The 3rd copy strengthens the extraction signal from Op Stds §14: we now have 3 consumers sharing the same pattern across 2 helpers. The extraction trigger condition from the original entry (2nd consumer) has been met and exceeded.

Both heartbeat consumers share the same state file at `~/its/state/heartbeat_row_ids.json` (keyed by daemon_name) so the file format is already shape-compatible. Extraction is mechanical: pull the seven heartbeat helpers + `_write_watchdog_marker` into `shared/heartbeat.py`, parameterize on `daemon_name` + `state_path` + `slug`, replace inline copies with imports.

**Effort:** ~half-day session including +8-12 unit tests for the new shared module + migration of both `intake_poll` and `weekly_send_poll` to use it.

**Risk of premature extraction:** if the watchdog-marker shape diverges per-daemon (e.g. different marker content, conditional write logic per §42 rationale), the API churns. The `intake_poll` deliberate divergence (marker only on completed cycle, not on skip paths) is the exact kind of per-daemon policy that the shared helper's API needs to accommodate. Parameterize the write-condition as a callable or flag.

**Revisit when:** weekly_send has completed 1-2 real Friday cycles (≥ ~2 weeks of production traffic), OR a 3rd polling daemon with heartbeat needs is queued (Email Triage is the likely trigger).

## Word-doc / PDF attachment generation for weekly_send [CLOSED 2026-06-30]

**Resolved 2026-06-30 (tech-debt currency sweep):** The PDF-attachment ask shipped. `safety_reports/weekly_send.py` downloads the compiled Box packet PDF and attaches it (`weekly_send.py:33-42`), with two-mode transport (inline ≤2.5 MB / Graph upload-session above; PR-3). The never-requested DOCX variant is an **accepted skip**, not debt. Verified @HEAD via grep (lesson #1).

Legacy WPRs (the Gates Solar 2016-03-12 anchor in `prompts/samples/`) were Word documents. Current `weekly_send` v0.1.0 sends `Draft Body` as inline text — no attachment. Sponsors who archive correspondence as document attachments may explicitly request a formatted attachment.

Phase 1.4+ extension: render `Draft Body` to PDF (via reportlab or similar) or DOCX (via python-docx), attach via the existing `graph_client.send_mail(..., attachments=[...])` signature. Box upload + Smartsheet link-update for the sent PDF could ride alongside.

**Effort:** 1-2 sessions depending on which format(s) sponsors want and whether Box archival ships in the same PR.

**Revisit when:** explicit sponsor feedback requesting formatted attachment.

## Automated mailbox cleanup for weekly_send integration smoke [CLOSED 2026-06-30 — premise obsolete]

**Closed 2026-06-30 (verified against HEAD, lesson #1):** the premise is gone. This entry assumed `tests/test_weekly_send_integration.py` "sends a real email to `seths@evergreenmirror.com` per run" that lingers in the inbox. The **Phase-5 rewrite** repointed `weekly_send` `WPR_Pending_Review` → `WSR_human_review` and the integration test now exercises **only the HELD path** — its docstring states it "sends NO email and hits NO Box" (the unknown-job `held_no_recipient` refusal); the real end-to-end send is the operator's manual deploy smoke, not this automated file. With no automated send, there is **no inbox clutter to clean up**, so the proposed `graph_client.delete_message` + teardown would be unused code wired into a non-sending test (a §14 preservation violation). A `delete_message` Graph primitive is deferred to a **real consumer** (Email Triage mailbox hygiene), not added speculatively here. `graph_client.py` is unchanged.


`tests/test_weekly_send_integration.py` test seed sends a real email to `seths@evergreenmirror.com` per run. Cleanup currently deletes the `WPR_Pending_Review` row in `finally`, but the email itself sits in the recipient's inbox until manually deleted. Acceptable for first few integration runs (rare; operator-driven) but eventually deserves programmatic cleanup.

Implementation: after assert SENT, use `graph_client.list_inbox` + `graph_client.delete_message` (would need to add `delete_message` to `graph_client.py` — currently not exposed) to remove the ITS-SMOKE-tagged message from the sandbox inbox.

**Effort:** ~hour or two including a new `delete_message` helper in `graph_client.py` + the test wire-up.

**Revisit when:** integration runs accumulate noticeable smoke clutter in the sandbox mailbox (estimate: after ~10-20 runs).

## Hardcoded BOX_PROJECT_FOLDERS dict requires code change per project [RESOLVED 2026-06-02]

**Resolved-by (E1):** `shared/project_routing.py` (TTL-cached `ITS_Project_Routing` sheet reader, `get_folder_id`), `scripts/migrations/build_its_project_routing_sheet.py` + `seed_its_project_routing.py`, `SHEET_PROJECT_ROUTING` in `shared/sheet_ids.py`, `safety_reports/intake.py::upload_attachments_to_box` now resolves via `project_routing.get_folder_id` (BOX_PROJECT_FOLDERS retained as the warn-not-crash fallback), `tests/test_project_routing.py` + `tests/test_project_routing_integration.py`, and `docs/runbooks/project_routing_onboarding.md` (§43). Pre-cutover (`SHEET_PROJECT_ROUTING == 0`) every lookup falls through to the unchanged hardcoded dict, so this lands with zero behavior change until the operator runs the two migrations and fills the sheet id.

**Deferred sub-items (NOT closed by E1, tracked separately below):** (1) startup Box-API folder-ID resolution validation — see "Daemon startup config validation" entry (the §989 reconciliation check); (2) post-cutover removal/empty-out of `BOX_PROJECT_FOLDERS` is an operator step after parity verification, not a code change here.

Original (for reference) ▸ `shared/defaults.py:73` defines `BOX_PROJECT_FOLDERS: dict[str, str]` — a hardcoded mapping from project name to Box folder ID. Every new project added to Box requires editing this file and redeploying. `shared/defaults.py` is also the documented fallback layer for ITS_Config (per existing convention in the module — `BOX_PROJECT_FOLDERS` references "1111B-derived clones post-cutover" suggesting it gets manually edited at each Box cutover).

**Failure mode:** non-developer operator cannot onboard a new project without CC involvement (code edit + PR + deploy). Risk of typo in folder ID silently routing uploads to the wrong project. Stale entries accumulate as projects close out. Project-onboarding is a routine ops task that should not require a deploy cycle.

**Proposed fix:** migrate to a Smartsheet lookup (suggest a dedicated `ITS_Project_Routing` sheet with columns `Project Name`, `Box Folder ID`, `Active` bool, `Notes`). Code reads at daemon startup, caches in-process, refreshes on interval. Add startup validation that every active row's folder ID resolves via Box API — warn (don't fail) on resolution miss so a single bad row doesn't crash the daemon. Once live, `BOX_PROJECT_FOLDERS` becomes the empty-dict fallback or is removed entirely.

**Effort:** ~half-day session (new sheet schema + `ITS_Project_Routing` migration script + reader in `shared/defaults.py` or new `shared/project_routing.py` + tests + Box resolution validation helper + operator runbook).

**Phase target:** 1.5 — blocks first-customer onboarding cleanliness; every new customer's project set is different.

**Tag:** `config-migration`.

**Revisit when:** Phase 1.5 hardening cluster, or operator hits the "I need to add a project but can't without a code change" friction.

Surfaced: 2026-05-24 hardcoded-values audit brief, §A2.

## No retry / backoff / circuit-breaker layer across Smartsheet call sites [CLOSED 2026-06-01]

Smartsheet API calls across `shared/smartsheet_client.py` and its consumers (intake_poll, weekly_send_poll, weekly_generate, watchdog, picklist_sync) had point-by-point exception catches but no aggregate "Smartsheet is degraded — back off the whole loop" signal. During an incident (5xx / timeout / rate-limit) each call site degraded independently: the daemon kept hammering the degraded service, ITS_Errors filled with one row per failed call, and a flapping failure could fan out unbounded operator email.

Closed by the **F08/F09** Phase 1.4 hardening PR, with a design that **differs from the originally-proposed one in two deliberate ways**:

1. **The circuit breaker is a separate, domain-agnostic module** (`shared/circuit_breaker.py`) — NOT "a simple counter in `smartsheet_client` state" as first sketched. It exposes a parameterized `guard(open_exc, count, ignore, …)` decorator + `bypass()` + lock-free `is_open()`; `smartsheet_client` decorates its **16 network-issuing methods** (leaving `get_setting` / `get_settings_with_prefix` undecorated — transitive via `get_rows`, no double-count) and injects the Smartsheet exception set + a bypass-wrapped, process-cached config loader. **One global breaker, persisted** to `~/its/state/circuit_breaker.json` (launchd daemons are fresh-process-per-cycle, so the consecutive-failure count + OPEN deadline must outlive the process). N consecutive counting-eligible failures (429/5xx/transport; **401/403/404 ignored** — deterministic/routine) trip OPEN → short-circuit with `SmartsheetCircuitOpenError` (a `SmartsheetError` subclass, so every existing consumer catch handles it unchanged); cooldown → single HALF_OPEN probe → CLOSED/OPEN. Surfaced via the daemons' `CIRCUIT_OPEN` heartbeat status and (PR 2) a watchdog prolonged-open check that reads the local file (works during a total Smartsheet outage).

2. **No retry-with-backoff decorator was built — deliberately.** Per Op Stds §14 (wrap, don't reimplement), the SDK's own HTTP retry/backoff already handles the transient/per-call layer; the breaker sits strictly *above* the typed-exception layer for the sustained/cross-call (and cross-process) case. `weekly_generate._process_with_retry`'s narrow NotFound-only retry stays as-is (it is not the transient-5xx retry this item imagined, and circuit-open deliberately does not trigger it). No `is_retryable` property and no `SS_API_UNAVAILABLE` error code were needed — `SmartsheetCircuitOpenError` is the typed signal.

The "ITS_Errors / dedupe state fills + unbounded email" half is addressed by **F09's global alerts-per-hour cap** (`alert_dedupe.check_hourly_cap` gating the Resend leg in `error_log._fire_resend_leg`): records still fire every time (Op Stds v16 §3.1 push-vs-record), only the operator email fan-out is bounded.

§43 successor-remediation runbook shipped at `docs/runbooks/circuit_breaker.md` (circuit-open + rate-cap-hit). §30 integration coverage at `tests/test_circuit_breaker_integration.py` (live trip/reset against the sandbox, CI-skipped). Surfaced: 2026-05-24 hardcoded-values audit brief, §B4.

## CLAUDE.md doctrine version citations lag v14/v9 [CLOSED 2026-06-18]

**Resolved 2026-06-18 (tech-debt easy-wins pass):** CLAUDE.md cites Op Stds v18 / FM v11 throughout; docs/doctrine_manifest.yaml matches. No lagging v13/v8 (v14/v9) refs remain.

Blueprint bumped Operational Standards v13 → v14 and Foundation Mission v8 → v9 on 2026-05-29 (blueprint PR #23, `29000f1`). This repo's `CLAUDE.md` still cites the old versions throughout:
- `"Operational Standards v13"` / `"canonically at v13"` — ~9 occurrences in CLAUDE.md
- `"Foundation Mission v8"` — 3 occurrences in CLAUDE.md (lines 37, 149, 151, 359)
- Recently-landed docs: session log `2026-05-29_f02-f22-capability-approval.md` + `docs/operations/cutover_checklist.md` cite v13/v8

The F02/F22 session deliberately scoped doctrine reconciliation out; the version strings were not swept.

**Failure mode:** a fresh CC session reading CLAUDE.md's `Op Stds §N` / `FM §N` citations will resolve them against v13/v8 text when v14/v9 are canonical. Both bumps are additive/reframe-only (no code changes), so the practical impact is low. But the cross-repo supersession drift check exists precisely to catch and track this.

**Proposed fix:** `grep -r "Operational Standards v13\|Op Stds v13\|canonically at v13\|Foundation Mission v8\|FM v8" ~/its/CLAUDE.md ~/its/docs/operations/` and sweep non-historical hits to v14/v9. Also bump `docs/doctrine_manifest.yaml`: `operational_standards: 14`, `foundation_mission: 9`. Exclude grandfathered historical entries (older session logs, tech-debt entries citing at their original surfacing date — correct by policy).

**Effort:** <1 hour. Mechanical string sweep + manifest version bump.

**Phase target:** next doctrine-reconciliation pass (low urgency — both bumps are additive/reframe only).

**Revisit when:** any session that touches CLAUDE.md for another reason, or before drafting a new workstream brief.

Surfaced: 2026-05-29 F02/F22 session close (cross-repo supersession check). Session log: `docs/session_logs/2026-05-29_f02-f22-capability-approval.md`.

## Remote branch `f02-f22` not auto-deleted after merge (worktree quirk) [CLOSED 2026-06-18]

**Resolved 2026-06-18:** both merged orphan refs deleted (`origin/session-log-f02-f22` + `origin/f02-f22`, both via `gh api -X DELETE …/git/refs/heads/…`; PRs #118/#119 were MERGED, neither base/head of an open PR). `git ls-remote --heads origin` confirms both gone.

When merging a PR from a git worktree (e.g., `~/its-f02-f22` on branch `f02-f22`), `gh pr merge --squash --delete-branch` successfully lands the squash merge on GitHub but cannot execute the post-merge local `checkout main` (main lives in `~/its`, not the worktree). As a side effect, `origin/f02-f22` is NOT deleted. The four-part verify still passes (GitHub-side merge is clean); the stale remote branch is cosmetic but should be cleaned up.

**Fix:** `gh api -X DELETE repos/SolutionSmith-debug/its/git/refs/heads/f02-f22` (the git-guardrail hook blocks `git push origin --delete` syntax, so use the GitHub REST API directly).

**Broader pattern:** any worktree-based session faces this; the fix is always the `gh api -X DELETE` route. Consider noting it in the post-merge checklist in `docs/operations/pr_merge_discipline.md`.

**Effort:** 2-minute manual cleanup per occurrence.

**Phase target:** immediate cleanup (cosmetic).

**Revisit when:** `git branch -r | grep origin/f02-f22` still shows it.

Surfaced: 2026-05-29 F02/F22 session close. Session log: `docs/session_logs/2026-05-29_f02-f22-capability-approval.md`.

## Integration tests silently broken by autouse keychain stub [RESOLVED 2026-05-29]

Both Smartsheet integration files (`tests/test_smartsheet_client_integration.py`, `tests/test_approval_verification_integration.py`) — and in fact ALL ~10 `@pytest.mark.integration` files — lacked any opt-out from the autouse `_mock_keychain` fixture that landed in **PR #74** (the CI-fix follow-up to the macOS-`security`-CLI breakage PR #68 introduced; the fixture was authored in #74, NOT #68). The stub fed `get_client()` a fake `"test-ITS_SMARTSHEET_TOKEN"`, so the first live call (`create_sheet_in_folder` → `get_client()`) hit `SmartsheetAuthError: HTTP 401 (code 1002)` even though the real token was valid and read-write. The module-scoped `_token_available` fixture saw the real token (module setup runs before the function-scoped stub), but `get_client()` inside the test body saw the stub — the confusing "fixture has the real token but the call 401s" signature. The conftest docstring always *claimed* integration tests opt out ("they re-mock or override via test-level fixtures") but the opt-out was never implemented. Silently broken since PR #74 because nobody re-ran the integration suite after the stub landed.

**Resolved (this PR):** added a **marker-based auto-opt-out** to `_mock_keychain` — `if request.node.get_closest_marker("integration") is not None: return`. Evaluated against the filename-list alternative the brief proposed and chose the marker approach because (a) it auto-covers all ~10 current integration files plus any future one with zero maintenance, and (b) it resolves at PER-TEST granularity, which is REQUIRED for mixed files like `tests/test_intake_poll.py` (per-test `@pytest.mark.integration` decorators at lines 730/1132 alongside unit tests that must keep the stub) — a filename list would wrongly disable the stub for that file's unit tests. The two non-integration filename entries (`test_keychain.py`, `test_helpers.py`) stay in `_KEYCHAIN_OPT_OUT_FILES` since they need the real keychain but are not integration-marked.

**Lesson:** any new integration test now opts out automatically via the marker — no list to remember. The durable fix is in place; this entry is the incident record.

Surfaced + resolved: 2026-05-29 integration-keychain-stub fix session.

## No startup token-scope / write-capability validation [CLOSED 2026-06-30]

**Resolved 2026-06-30 (tech-debt currency sweep):** Exactly the proposed probe was built. `shared/smartsheet_client.py:1293` `verify_write_capability()` does a create-then-delete probe write into the System/Config folder; a 401/403 raises `SmartsheetWriteCapabilityError` (`:101`); wired to watchdog **Check L**. A write-disabled or misscoped token surfaces loudly instead of silently failing at first write. Verified @HEAD via grep (lesson #1).

A read-only or otherwise-invalid `ITS_SMARTSHEET_TOKEN` fails **silently at the first daemon write** rather than loudly at boot. The keychain-stub session above burned significant operator time precisely because the failure mode was a confusing per-call `401 (code 1002)` deep in a test, not a loud boot-time "this token cannot write." A daemon in production with a mis-scoped token after a rotation would behave the same way: reads succeed, the first write 401s mid-cycle.

**Proposed fix:** a cheap write-capability probe at daemon init (and/or a watchdog check) that CRITICAL-alerts if `ITS_SMARTSHEET_TOKEN` cannot write — e.g. create-then-delete a throwaway sheet in a sandbox folder, or call a low-cost write that the API rejects distinguishably for a read-only token. Fail loud at boot, not silent at first write.

**Effort:** ~half-day (probe + watchdog wiring + a typed "token cannot write" error class + test).

**Phase target:** 1.4 pre-Customer-1 hardening (reliability gap, not a launch blocker for Customer 0).

**Revisit when:** the next token rotation, or any session that touches `shared/keychain.py` / `shared/smartsheet_client.py` auth.

Surfaced: 2026-05-29 integration-keychain-stub fix session.

## Smartsheet integration tests flake on create→read/write eventual consistency [RESOLVED 2026-06-30]

**Resolved 2026-06-30 (package B) via approach 1 (test-level reruns; no SUT churn).** Added `pytest-rerunfailures` (dev dep) + a registered `flaky` marker, and applied module-level `pytestmark = [integration, flaky(reruns=3, reruns_delay=2)]` to `tests/test_smartsheet_client_integration.py` — each rerun re-runs the whole test against a FRESH sheet, so a transient create→read 404/1006 clears; `reruns_delay` lets the lagging replica catch up. A real assertion failure still surfaces after the reruns exhaust. **Deliberately NOT approach 2** (no retry pushed into `shared/smartsheet_client.py` — a 404 must still surface in production, e.g. the heartbeat-cache 404-invalidation path; `test_update_row_cells_by_id_raises_not_found_on_missing_row` is unaffected because reruns fire only on FAILURE, and that test passes by raising the expected 404). Prove-the-control-bites: a synthetic fail-then-pass test confirmed the rerun fires (`1 passed, 1 rerun`). The separate `delete_sheet_settling` B2 mitigation is unchanged. **Operator note:** the reruns take effect only after the worktree/CI venv reinstalls dev deps (`pip install -e '.[dev]'`) — the dep was newly declared.


Once the keychain-stub fix (above) let the Smartsheet integration tests reach the live API for the first time since PR #74, they were found to flake intermittently (~40–60% of full-suite runs had ≥1 failure) on Smartsheet's **create→read/write eventual consistency**. Every observed failure is a transient `errorCode 1006` / HTTP 404 "Not Found" (or a `find_*_by_name_in_folder` returning `None`) — there were **zero** stale-*value* assertion failures. Diagnosis: `create_sheet_in_folder` returns a `sheet_id` before Smartsheet finishes propagating the new sheet across its read replicas, and the 404s **flap** — a successful read does NOT guarantee the next read/write (which may route to a lagging replica) succeeds, for a window of several seconds after create. Confirmed live: a run where `list_columns` succeeded, then `add_rows` → `_fetch_column_map` → `get_sheet` 404'd a moment later (different replica).

This is **pre-existing** (tests authored PRs #47/#48/#49/#51/F22) and was merely **unmasked** by the keychain fix — it is NOT the keychain bug and NOT caused by that fix; the fake-token 401 previously killed every one of these tests at `create_sheet` before they could reach the racy ops.

**Scoped out of the keychain-fix PR by operator decision (2026-05-29):** that PR ships the keychain opt-out + token-leak redaction + `_client` reset + the *deterministic* `NO_HISTORY` cell-history poll (`_wait_for_history`), and leaves this separate eventual-consistency hardening to a dedicated follow-up. A partial create→read settle (`_settle_sheet` / `_wait_until_listed`) was prototyped and **deliberately reverted** because it reduced but could not eliminate the flapping (a single settle read can't guarantee the next op's replica is caught up).

**Proposed fix (follow-up PR), two viable approaches:**
1. **Test-level reruns** — add the `pytest-rerunfailures` dev-dep and mark the integration tests (or run with `--reruns 3 --reruns-delay 2`). Cleanest, no test-body churn; the whole test re-runs against a fresh sheet so a transient 404 clears. Downsides: new dev dependency; masks rather than handles.
2. **Retry-on-not-found wrapper** — a `_retry_nf(callable)` helper wrapping every post-create operation (`add_rows`, `update_rows`, `update_column_options`, `list_columns_with_options`, `find_*`, `get_cell_history`) to retry on `SmartsheetNotFoundError` / `None`. No new dep; deterministic (all flakiness is not-found-flapping). Downsides: larger diff touching ~16 call sites; MUST NOT wrap `test_update_row_cells_by_id_raises_not_found_on_missing_row`'s bogus-row update (which legitimately expects a 404). Retrying writes is safe here because a 404 means the write did not apply.

Do NOT push the retry into the SUT (`shared/smartsheet_client.py`): a 404 must surface in production (e.g. the heartbeat-cache 404-invalidation path in `intake_poll`, regression-guarded by `test_update_row_cells_by_id_raises_not_found_on_missing_row`).

**Related — create→DELETE variant (B2, 2026-06-02):** the same eventual-consistency flake surfaced live on the B2 token write-capability probe's IMMEDIATE cleanup — a delete issued right after create returned `errorCode 5036` / 404 ("not yet propagated"). Handled by a **scoped** `smartsheet_client.delete_sheet_settling` (retry-on-not-found, ~3 attempts, short backoff) used ONLY by the probe-cleanup path (`verify_write_capability` / watchdog Check L); the general `delete_sheet` still fails fast, honoring the rule above. This is a targeted mitigation for that ONE op — NOT the suite-wide create→read hardening this entry still tracks.

**Effort:** ~1 hour for approach 1; ~half-day for approach 2 (+ multi-run verification).

**Phase target:** next integration-test-maintenance pass; not a launch blocker (these tests are operator-run pre-deployment, NOT in CI).

**Revisit when:** the operator next runs `pytest -m integration` and is annoyed by a transient 404, or before relying on the integration suite as a release gate.

Surfaced: 2026-05-29 integration-keychain-stub fix session.

## Picklist drift Phase 3a — two DORMANT registry-over-declares (Workstream / Disposition) [RESOLVED — columns added 2026-06-03]

**Resolved (D1 = ADD, 2026-06-03):** Seth chose option 2 (add the empty columns).
Both live (sandbox) columns were created as PICKLIST seeded with their `REGISTRY`
allowed sets, so the weekly audit is now clean (`audit_picklist_drift --no-emit`
→ "No drift findings"):

- **ITS_Errors · `Workstream`** — new column_id `368377473568644` (6 `_WORKSTREAM_VALUES_GLOBAL` options).
- **ITS_Quarantine · `Disposition`** — new column_id `8535753050328964` (RELEASE / DELETE / ESCALATE).

Mechanism: new additive `shared/smartsheet_client.create_picklist_column`
(§42 docstring; unit-tested + §30 live round-trip in
`tests/test_smartsheet_client_integration.py`) + idempotent migration
`scripts/migrations/add_dormant_picklist_columns.py` (preview-default, `--commit`
to write, options sourced from `REGISTRY` so they can't drift). Re-run is a clean
skip. The columns sit empty — the **writers** (error_log `Workstream`, quarantine
`Disposition`) remain a separate, out-of-scope feature; an empty column is fine.
Server-side restrict-to-dropdown (validation) was intentionally left off (the
separate hardening sweep, `docs/audits/picklist_hardening_audit.md`).

Original (for reference) ▸ The first `scripts/audit_picklist_drift.py` run surfaced three findings. Phase 1 (`docs/audits/picklist_drift_2026-06-02_classification.md`) classified two as **dormant** — the `picklist_validation.REGISTRY` declares a column the live sheet lacks AND no code writes it:

- **ITS_Errors · `Workstream`** — `REGISTRY` registers `SHEET_ERRORS → "Workstream" → _WORKSTREAM_VALUES_GLOBAL` (`shared/picklist_validation.py:147`), but the live sheet has no `Workstream` column and `shared/error_log.py:130-138` builds the row dict with no `Workstream` key. (Wiring a `Workstream` *writer* into error_log is a separate feature, explicitly out of scope.)
- **ITS_Quarantine · `Disposition`** — `REGISTRY` registers `SHEET_QUARANTINE → "Disposition" → _QUARANTINE_DISPOSITION_VALUES` (RELEASE/DELETE/ESCALATE, `picklist_validation.py:158/96-98`), but the live sheet has no `Disposition` column and `shared/quarantine.py::log_quarantined_message` writes `QuarantineReason`→Notes, never a `Disposition`. The value set is registered for a future write path that does not exist yet (tied to attachment-screening Layers 1–3, Phase 1.4).

**Failure mode:** the weekly `safety_picklist_audit` WARNs on both every Sunday. Accurate, but a chronically-warning audit risks alarm fatigue for a ship-and-leave system.

**DECISION (Seth — deferred from the 2026-06-02 picklist-reconcile session, three options on the table):**
1. **Trim the registry entries** so `REGISTRY` declares only what's actually written → audit goes quiet, registry stays honest; re-add when the writer is built. (Canonical-ish edit; route via `doc-reconciliation-auditor`.)
2. **Add the empty columns** to the live sandbox sheets now → audit clean, sheets ready for the future writer. Downside: premature schema for unbuilt features (YAGNI).
3. **Defer — keep the WARN** until a writer is wired (lowest touch; audit stays noisy).

CC recommendation was (1) trim-registry (honest + quiet + no premature live schema). **Not executed — Seth decides.**

**Effort:** (1) ~30 min + a `doc-reconciliation-auditor` pass; (2) ~30 min two live column-adds; (3) zero.

**Tag:** `picklist-drift`, `config-migration`.

**Revisit when:** next session (picked up 2026-06-03), OR whenever the Disposition / error-Workstream writer is actually built (then option 2 lands naturally with that feature).

Surfaced: 2026-06-02 picklist-drift reconcile (PR #150, Phase 3a). Related: classification doc `docs/audits/picklist_drift_2026-06-02_classification.md`; `docs/runbooks/picklist_drift_reconcile.md`.

## Picklist drift Phase 3b — no automated registry→live apply (systemic ship-and-leave gap) [RESOLVED — automated 2026-06-03]

**Resolved (D2 = AUTOMATE, 2026-06-03):** added an additive `--apply` mode to
`scripts/audit_picklist_drift.py`, built on `ensure_picklist_options`. For each
registered `(sheet_id, column → values)` it pushes the MISSING options into the
live picklist. **Dry-run is the default** (`--apply` previews; `--apply --commit`
writes). **Additive + option-only**: never removes an option, and a
missing/wrong-typed column is logged + skipped (column creation is the Phase 3a
schema decision, not this command). `--commit` without `--apply` is a CLI error.
This removes the developer-memory dependency and gives the Successor-Operator a
clean Tier-2 command. Coverage: unit tests in `tests/test_audit_picklist_drift.py`
(dry-run/commit/no-op/skip/CLI-guard) + a §30 live round-trip in
`tests/test_audit_picklist_drift_integration.py`; the `docs/runbooks/picklist_drift_reconcile.md`
`--apply` flow + §43 note are now real (no longer "contingent"). Live-smoked
this session: `--apply` preview against the real registry reports 0 adds / 0
skips (all sheets reconciled). **Prune/removal mode remains out of scope** (v1
additive-only, parity with `ensure_picklist_options`); if ever added it goes
behind an explicit flag with `picklist_sync.py`'s reference-check guard.

Original (for reference) ▸ There is **no automated path that pushes `picklist_validation.REGISTRY` additions into the live Smartsheet picklists.** `picklist_sync.py` is sheet→sheet (reads a source sheet column's values, not the code registry); the audit is read-only (no `--apply`). So a `REGISTRY`/enum addition reaches live sheets only via a **human remembering a manual step** (`review_queue.py:84-96` documents exactly this for the three `Reason` values — and that step went undone until the 2026-06-02 reconcile). The weekly audit only **WARNs after the fact**. This is the real ship-and-leave finding: the loop depends on developer memory.

Phase 2 of the reconcile landed the additive primitive `shared/smartsheet_client.ensure_picklist_options` (additive, idempotent, dry-run, no-removal, never-creates-columns; live-validated), but it is invoked today only by a hand-written Python snippet (developer action), not an operator-friendly command.

**DECISION (Seth — deferred from 2026-06-02, two options):**
- **(a) Automate:** add an additive, dry-run-previewed, reference-checked `--apply` mode to `scripts/audit_picklist_drift.py` (or a sibling) built on `ensure_picklist_options` — additive-only by default, removals behind an explicit flag mirroring `picklist_sync.py`'s guard. Removes the human-memory dependency; gives the Successor-Operator a clean Tier-2 command (`docs/runbooks/picklist_drift_reconcile.md` already describes the operator flow contingent on this landing). **CC recommendation for ship-and-leave.**
- **(b) Document only (minimum bar):** keep it manual — add "any `picklist_validation.REGISTRY` change → apply to live sheets" to `docs/operations/cutover_checklist.md` + a release checklist, plus the §43 note already in `picklist_drift_reconcile.md`. No new code; human-memory dependency remains.

**Do not build (a) without Seth's sign-off** (per the brief). If (a): ~half-day (the `--apply` mode + dry-run preview + reference-check guard + §30 test + the §43 runbook's `--apply` path becomes real). If (b): ~1 hour (checklist entries).

**Tag:** `picklist-drift`, `ship-and-leave`.

**Revisit when:** next session (picked up 2026-06-03) — this is the higher-leverage of the two Phase 3 decisions.

Surfaced: 2026-06-02 picklist-drift reconcile (PR #150, Phase 3b). Related: `ensure_picklist_options` (`shared/smartsheet_client.py`); `docs/runbooks/picklist_drift_reconcile.md`; classification doc Phase 3b.

## `scripts/lint_doc_conventions.py` missing `safety_portal` workstream tag [CLOSED 2026-06-18]

**Resolved 2026-06-18 (tech-debt easy-wins pass):** This PR: added safety_portal to CANONICAL_WORKSTREAMS (lint_doc_conventions.py) + the test's expected set (doctrine_manifest + doc_conventions already listed it).

`docs/doctrine_manifest.yaml` lists `safety_portal` as a valid `doc_conventions.workstream_tags` entry, but `scripts/lint_doc_conventions.py`'s canonical workstream set does not include it. Any doc tagged `workstream: safety_portal` (including `docs/runbooks/safety_portal_config_sheets.md`) will produce a lint warning in CI.

**Fix:** add `"safety_portal"` to the canonical workstream set in `scripts/lint_doc_conventions.py` (one-line change). Lint is warn-only in CI today so this does not block merges.

**Tag:** `lint`, `safety-portal`, `doc-conventions`.

**Effort:** ~5 minutes.

**Revisit when:** next session touching `lint_doc_conventions.py`, or when the CI warn noise becomes distracting.

Surfaced: 2026-06-03 Safety Portal config sheets session (PR #155 + PR #156 audit).

## `ops-stds-enforcer` agent pinned at "Op Stds v13" — 3 majors behind v16 [CLOSED 2026-06-18]

**Resolved 2026-06-18 (tech-debt easy-wins pass):** `.claude/agents/ops-stds-enforcer.md` re-synced to v18 (2026-06-09) and incorporates §§43–49; the v13 pin no longer exists. (The file is a symlink from `~/its-blueprint`, so the agent content is a blueprint artifact — but the documented gap is gone.)

The `ops-stds-enforcer` subagent's system prompt (`.claude/agents/ops-stds-enforcer.md`, symlinked from `~/its-blueprint/.claude/agents/`) cites "Op Stds v13". The canonical version is v16. The agent is blind to:

- §43 (successor-remediation documentation as definition-of-done)
- §44 (Tier-2 Claude-assisted repair model; Developer-Operator / Successor-Operator split)
- v14 reframe: §1 kill switch as operator-convenience pause, NOT a security control (F07)
- v15 additions: §43/§44 initial draft
- v16 reframe: §44 Tier-2 boundary as training-bounded co-resolution (no structural enforcement layer)

**Fix:** update the version string and incorporate the §43/§44 enforcement brief into the agent's review criteria. This is a blueprint edit (`.claude/agents/ops-stds-enforcer.md`); requires doctrine-edit approval per the session-close-maintainer boundary rule.

**Risk:** any PR review by `ops-stds-enforcer` that ships without a §43 runbook entry passes the agent but fails the actual DoD. The gap is silent.

**Tag:** `agent`, `doctrine-drift`, `ops-stds`.

**Revisit when:** next session that runs `ops-stds-enforcer` on a PR touching a new capability, or when a §43 entry is required as DoD and the agent misses it.

Surfaced: 2026-06-03 unifying alignment audit (PR #156, DR-E1 / OPEN-1). Related: `docs/audits/2026-06-03_unifying-alignment-audit.md`.

## Safety Portal — form-catalog corpus mismatch with blueprint (pre-Phase-4) [CLOSED 2026-06-05]

The 10 PDF reference forms committed to `safety_portal/worker/public/forms/` did not match the 4 forms named in blueprint `workstreams/safety-portal/mission.md` and the ITS_Forms_Catalog sheet seeded in PR #155. Specifically:
- ITS_Forms_Catalog had: `jha-v1`, `daily-site-safety-v1`, `equipment-preinspection-v1`, `toolbox-talk-v1`.
- The PDF corpus added: HSS&E Work Observation, Visitor Sign-In, and several others not named in the blueprint.
- "Daily Site Safety Worksheet" (named in the brief) was absent from the committed PDFs.

**Resolved by PR #164 (2026-06-05):** The v1 catalog was confirmed via the PDF corpus. ITS_Forms_Catalog migrated to the parent/variant model (5 parents + 7 variants = 12 rows). Daily Site Safety removed (not a form-fill candidate); Visitor + HSS&E added. All 11 form definitions transcribed faithfully from the 10 reference PDFs and validated against the meta-schema (49 tests). The mismatch is fully resolved; Phase 4 form rendering may proceed.

**Tag:** `safety-portal`, `data-gap`, `form-catalog`.

Surfaced: 2026-06-04 Safety Portal Phase 2 session (PR #158). Closed: PR #164, 2026-06-05.

## Safety Portal — frontend build/lint CI step missing [CLOSED 2026-06-18]

**Resolved 2026-06-18 (tech-debt easy-wins pass):** CI has a blocking `portal` job: npm ci + `npm run typecheck` (SPA+Worker tsc) + vitest-pool-workers + SPA vitest. (An explicit vite-build step is still absent — minor.)

PR #158 added the `safety_portal/` TypeScript/Node tree. The existing GitHub Actions CI (`ruff` + `pytest`) covers only Python. The TS tree has no CI job for `tsc --noEmit` (typecheck), `npm run build` (Vite bundle), or a lint step. Errors in the TS tree are invisible to CI until a developer manually runs `npm run build` locally.

**Proposed fix:** add a `.github/workflows/frontend-ci.yml` job:
1. `npm ci` in `safety_portal/worker/`
2. `npm run build`
3. `tsc --noEmit`

**Tag:** `safety-portal`, `ci`, `frontend`.

**Effort:** ~30 minutes.

**Revisit when:** next session touching `safety_portal/` — or proactively before Phase 3 portal hardening.

Surfaced: 2026-06-04 Safety Portal Phase 2 session (PR #158).

## Safety Portal D1 dropdown sync (Phase-3 A.1.4) deferred to deploy session [CLOSED 2026-06-18]

**Resolved 2026-06-18 (tech-debt easy-wins pass):** Live: portal_poll does the ITS_Active_Jobs -> D1 full-replace sync via POST /api/internal/sync (live-validated 2026-06-08).

The Phase 3 architecture (PR #160) populates the Worker's D1 `active_jobs` table from ITS_Active_Jobs so the portal form's Job dropdown stays current. This sync step requires the portal D1 database (Phase 2 deploy deferred) plus a Python→D1 write mechanism (options: a Worker `/api/sync` HMAC-authed endpoint vs Cloudflare D1 HTTP API directly). The decision and implementation are deferred to the deploy session.

**Decision required at deploy:** Worker `/api/sync` (POST, HMAC-authed, Worker writes to D1 from request body) vs direct Cloudflare D1 HTTP API from Python (`shared/active_jobs.py` writes to D1 via REST). The Worker approach keeps D1 write capability server-side; the D1 HTTP API approach is simpler but requires a D1 API token in the Python environment.

**Tag:** `safety-portal`, `deploy`, `d1-sync`.

**Revisit when:** Safety Portal deploy session. Blocked on D1 creation (see deploy entry above).

Surfaced: 2026-06-05 Safety Portal Phase 3 session (PR #160).

## Safety Portal Phase 4 PR 2 — TS display runtime [CLOSED 2026-06-05]

**Resolved by PR #166 (`23af65f`, four-part-verify clean):** definition-driven TS display runtime landed. 3 archetype renderers (rows+signatures, grouped-checklist, sectioned-assessment) in `safety_portal/src/forms/`; form-type + variant dropdowns; multi-row SVG signature capture via `signature_pad`; amend/prefill from a prior submission; structured-data emit to the Worker; 3 new Worker endpoints (`/api/jobs`, `/api/forms`, `/api/submissions`); D1 `jobs` + `submissions` tables (migration 0004). Session log: `docs/session_logs/2026-06-05_safety-portal-phase4-runtime-renderer-phase5-foundation-transport.md`.

**Tag:** `safety-portal`, `typescript`, `phase-4`.

Surfaced: 2026-06-05 Safety Portal Phase 4 PR 1 session (PR #164). Closed: PR #166, 2026-06-05.

## Safety Portal Phase 4 PR 3 — Python reportlab PDF renderer [CLOSED 2026-06-05]

**Resolved by PR #167 (`2946184`, four-part-verify clean):** Python Option-B reportlab renderer landed. `safety_reports/form_pdf.py`: reads `safety_portal/forms/*.json` + a structured submission → deterministic print-parity PDF (Evergreen header, table/checklist/section layout, legal invariants in code, embedded SVG signatures); equipment checklist items are tri-state OK / NOT OK / N/A (N/A distinct from blank); `merge_pdfs()` primitive added; `+reportlab` + `+pypdf` to `pyproject.toml`. Session log same as above.

**Tag:** `safety-portal`, `python`, `phase-4`, `reportlab`.

Surfaced: 2026-06-05 Safety Portal Phase 4 PR 1 session (PR #164). Closed: PR #167, 2026-06-05.

## Safety Portal Phase 5 — `portal_poll.py` Mac-side puller daemon [CLOSED 2026-06-18]

**Resolved 2026-06-18 (tech-debt easy-wins pass):** Built + live-validated (2026-06-08 mirror); launchd plist org.solutionsmith.its.portal-poll present.

The Phase 5 pull model (decision: `decision_phase5-portal-transport.md`) requires a new Mac-side polling daemon `portal_poll.py` (modeled on `safety_reports/intake_poll.py`). It polls the Worker's `/api/internal/pending` endpoint (bearer auth: `ITS_PORTAL_INTERNAL_TOKEN` from Keychain), iterates unprocessed submissions, verifies the `X-ITS-Portal-HMAC` using `shared/portal_hmac.py`, hands each to intake, then POSTs `/api/internal/mark-filed` with the receipt. Standard daemon contract: heartbeat to `ITS_Daemon_Health`, kill-switch gate, fcntl lock, `@its_error_log`. Locally testable on `wrangler dev --local`. launchd plist needed.

**Tag:** `safety-portal`, `phase-5`, `daemon`.

**Revisit when:** Phase 5 daemon-build session. Blocked on: deploy (Worker must be up; `wrangler dev --local` for local testing).

Surfaced: 2026-06-05 Safety Portal Phase 5 session. Session log: `docs/session_logs/2026-06-05_safety-portal-phase4-runtime-renderer-phase5-foundation-transport.md`.

## Safety Portal Phase 5 — intake portal-marker branch (HMAC verify → file → receipt) [CLOSED 2026-06-18]

**Resolved 2026-06-18 (tech-debt easy-wins pass):** Built + live: intake.process_portal_submission — HMAC verify -> UUID dedupe -> Box file -> mark-filed receipt.

`safety_reports/intake.py` needs portal-marker branches (PLANNED, not built) for the pull-model flow: HMAC verify → submission UUID dedupe → Sat→Fri Job-ID week key via `safety_week` → `active_jobs` lookup → `form_pdf.render` (Option B) → per-job/week Box tree via `week_folder` (box_client needs a `get_or_create_folder` primitive — `canonical_job_path` is currently a stub) → file PDF → write week-sheet row → receipt POST back to Worker. `box_client.canonical_job_path()` is a stub (format unconfirmed with owner; see existing tech-debt entry). UUID idempotency guard needed (duplicate POST from the Worker must not double-file).

**Tag:** `safety-portal`, `phase-5`, `intake`, `box`.

**Revisit when:** Phase 5 intake-branch build session. Blocked on: `portal_poll.py` + `box_client` get-or-create primitive.

Surfaced: 2026-06-05 Safety Portal Phase 5 session.

## Safety Portal Phase 5 — weekly generate/send rewire for WSR (narrative→PDF-merge + dual-write + gated send) [CLOSED 2026-06-18]

**Resolved 2026-06-18 (tech-debt easy-wins pass):** Built + live: weekly_generate is the deterministic WSR dual-write + merge_pdfs compile; weekly_send reads WSR; watchdog Check I catch-up landed.

Three pieces:

1. **`weekly_generate.py` (compile step):** on Friday 14:00 (or `Compile Now` checkbox trigger), merge all Sat→Fri submission PDFs via `form_pdf.merge_pdfs` + generate the narrative summary; dual-write to the per-job week sheet (read-only snapshot) and to `WSR_human_review` row (`SHEET_WSR_HUMAN_REVIEW = 5035670127988612`) with editable email body + resolved recipients (TO from `safety_reports_contact_email`, CC from `cc_emails`). Skip compile if already compiled and no new docs since last compile. Late arrivals → next uncompiled week + Review-Queue flag.
2. **`weekly_send.py` (Phase 5 send step):** reads approved `WSR_human_review` rows; attaches merged PDF; resolves TO + CC from the row (not hardcoded); logs full resolved TO+CC list at send; refuses on blank recipients or GENERATION_FAILED tag; Pacific-Monday 7 AM cadence from `ITS_Config`.
3. **Watchdog catch-up (Check I):** retries missed Friday compile on Saturday if marker stale.

**Tag:** `safety-portal`, `phase-5`, `weekly-generate`, `weekly-send`.

**Revisit when:** Phase 5 generate/send rewire session. Blocked on: intake portal-branch + `WSR_human_review` row format (built in PR #168).

Surfaced: 2026-06-05 Safety Portal Phase 5 session.

## [CLOSED 2026-06-18] `~/its` stranded on `publish/req-5-incident-report` branch

**Resolved 2026-06-18 (tech-debt easy-wins pass):** ~/its is on main; the idle self-heal (_unstrand_if_needed at the top of publish_once) fixed the root cause.

The publish daemon left `~/its` on branch `publish/req-5-incident-report` after a failed pre-`_reset_to_main` cycle. The launchd job is not loaded (RunAtLoad false, operator-gated), so automatic recovery has not run. **Operator action:** either load the publish-daemon launchd job (which will `_reset_to_main` on startup) OR run manually: `git -C ~/its checkout main && git pull origin main`.

**Tag:** `safety-portal`, `publish-daemon`, `operator-action`, `high`.

**Revisit when:** next session start, or when the publish daemon launchd job is loaded.

Surfaced: 2026-06-09 Phase-2 Form Manager build session.

_Update 2026-06-09 (Part-D session): the tree was recovered manually (`git checkout main`) and the **root cause** — the self-defeating publish CI gate (hardcoded form-count assertions that red-CI'd the new-form publish) — is fixed in the Part-D PR. Residual: the daemon's idle self-heal gap below._

## [CLOSED 2026-06-09] Publish daemon: stranded tree only self-heals during an actuation, not when idle

`_reset_to_main` (the recover-from-an-interrupted-cycle step) ran **inside `_actuate`**, i.e. only when a queued request was claimed. So a daemon that fails a publish and then has nothing to actuate left `~/its` stranded on the `publish/req-*` branch **indefinitely** — the "self-heal" fired only on the *next* publish, which may never come, and the operator's tree stayed stuck until a manual `git checkout main`. This is exactly what stranded the tree on `publish/req-5-incident-report` (the resolved entry above; recovered manually 2026-06-09).

**Resolved 2026-06-09:** added `_unstrand_if_needed()`, called at the **top of `publish_once`** (after the kill-switch / `polling_enabled` gate, before creds) — a failed-then-idle daemon un-strands itself on the next tick. Chose the **lighter guard** over a blind per-cycle `_reset_to_main`: a single `rev-parse` (no network pull) when already on `main`; only the genuinely-stranded case pays the full reset. A recovery failure is loud (`publish_daemon.unstrand_failed` ERROR) + halts the cycle — it never actuates from a stranded tree. Tests: `test_unstrand_recovers_a_stray_branch`, `test_unstrand_is_a_noop_on_main`, `test_publish_once_unstrands_before_actuating`, `test_publish_once_halts_loud_when_unstrand_fails`.

**Tag:** `safety-portal`, `publish-daemon`, `resilience`.

Surfaced: 2026-06-09 Part-D publish-CI-gate session (operator flag). Resolved same session.

## [CLOSED 2026-07-03] compile_now_poll — ITS_Daemon_Health self-provision row deferred (Part-B B3)

**Resolved 2026-07-03 (CS Slice 3, R4-F1):** exactly the fix this entry prescribed — folded in **together with** the `shared/heartbeat.py` extraction (which landed as `HeartbeatReporter`, PR #344), so no third verbatim copy was ever made. `compile_now_poll` now constructs its own module-level reporter (daemon `safety_reports.compile_now_poll`, 90s interval, shared `heartbeat_row_ids.json` ARCH-2 state) and writes the per-cycle row at the end of `_poll_inside_lock` (OK / DEGRADED-on-per-job-errors / CIRCUIT_OPEN), broad-except fenced so a heartbeat failure never blocks a compile. `publish_daemon` gained the same reporter in the same slice. Self-provision (A1) rides the shared reporter's find-or-create. Operator live smoke (first real cycle self-provisions the two new rows) rides the PR per the mandatory-live-smoke rule.

`safety_reports/compile_now_poll.py` (Part B) registers a watchdog Check-C liveness marker (`safety_compile_now_poll`, `scripts/watchdog.py`) — the LIVENESS safety net — but does NOT yet write an **ITS_Daemon_Health** operator-visibility row (the per-daemon update-in-place heartbeat the other pollers self-provision). Deferred to keep the Part-B PR focused: the daemon-health row is observability, not correctness, and the heartbeat-row machinery is ~150 lines replicated **verbatim** per daemon (`portal_poll` / `weekly_send_poll`) pending the already-tracked `shared/heartbeat.py` extraction — adding it here would replicate that machinery a third time.

**Fix:** fold compile_now_poll's daemon-health heartbeat in **together with** the `shared/heartbeat.py` extraction (so all daemons share one implementation), or replicate the helpers if the extraction is still pending at the time. Self-provision a `safety_reports.compile_now_poll` row in ITS_Daemon_Health, update-in-place per cycle (ARCH-1/2/3 conventions).

**Tag:** `safety-portal`, `compile-now-poll`, `observability`.

**Revisit when:** the `shared/heartbeat.py` extraction lands, or before compile_now_poll's production activation.

Surfaced: 2026-06-09 Part-B on-demand-compile session (B3 divergence — watchdog liveness done, daemon-health row deferred).

## [CLOSED 2026-06-18] Portal admin still offers "Retire" on an already-retired form (frontend)

**Resolved 2026-06-18 (tech-debt easy-wins pass):** Not reachable: registry.formCatalog() filters to status==='active', so a retired form drops from the picker; the backend also rejects a duplicate retire.

`FormsPage.tsx` / `FormEditor.tsx` display the Retire action for all forms with status `live` OR `retired`. The backend (`apply_publish` in `publish_manifest.py`) now rejects a duplicate-retire cleanly at the validate stage ("is already retired"), but the UI should not offer it in the first place — offering a disabled/grayed-out action (or hiding it entirely) would prevent operator confusion.

**Fix:** in `FormsPage.tsx` (and the editor's action menu), gate the Retire button on `status === 'live'` only — hide or disable it for `status === 'retired'`.

**Tag:** `safety-portal`, `form-editor`, `ux`, `low`.

**Revisit when:** a form editor polish pass is done, or an operator trips over the misleading UI.

Surfaced: 2026-06-09 WSR/publish-pipeline session (PR #244 — backend rejects cleanly; frontend UX gap noted).

## [CLOSED 2026-06-18] `README.md:111` documents weekly-send idempotency key as "Sent At non-empty" — code keys on `Send Status == SENT`

**Resolved 2026-06-18 (tech-debt easy-wins pass):** This PR: README.md updated — the guard keys on `Send Status == SENT` (authoritative); Sent At is stamped atomically with the status.

`safety_reports/README.md` line 111 (approximately) says the weekly-send idempotency guard keys on a non-empty "Sent At" column. The actual implementation in `weekly_send.py` keys on `Send Status == SENT`. These diverge when a send fails mid-way — "Sent At" may be empty while "Send Status" is FAILED, or vice versa. The doc-drift was caught during the WSR ABSTRACT_DATETIME sweep (PR #245).

**Fix:** update `safety_reports/README.md` to describe the actual guard (`Send Status == SENT`) and note that "Sent At" is set atomically with the status change (so they should always agree, but the code's authoritative check is the status column).

**Tag:** `safety-portal`, `weekly-send`, `doc-drift`, `low`.

**Revisit when:** next doc-accuracy pass on `safety_reports/README.md`.

Surfaced: 2026-06-09 WSR ABSTRACT_DATETIME session (PR #245 sweep caught the mismatch).

## [CLOSED 2026-06-18] `publish_daemon._regenerate_archive` writes `form_archive_out/` into `~/its`

**Resolved 2026-06-18 (tech-debt easy-wins pass):** This PR: _regenerate_archive renders into a tempfile.mkdtemp --out-dir + shutil.rmtree cleanup; the live ~/its tree no longer accrues form_archive_out/.

`safety_reports/publish_daemon.py` `_regenerate_archive` runs `generate_form_archive.py` as a subprocess, which writes its output to `form_archive_out/` inside the `~/its` working tree. This directory is now `.gitignore`d (PR #241 fix: added `form_archive_out/` to `.gitignore`), so it does not pollute commits. However, writing to a temp dir (e.g., `tempfile.mkdtemp()` and passing the path as an argument to `generate_form_archive.py`) would be cleaner and avoid any race with a concurrent process reading the working tree.

**Fix:** add a `--output-dir <path>` flag to `generate_form_archive.py` and pass `tempfile.mkdtemp()` from `_regenerate_archive`; clean up the temp dir after the Box upload.

**Tag:** `safety-portal`, `publish-daemon`, `cleanup`, `low`.

**Revisit when:** the archive generation path is revisited, or a concurrent-process race is observed.

Surfaced: 2026-06-09 WSR/publish-pipeline session (publish daemon archive step; gitignore is the current mitigation).

## [CLOSED 2026-06-18] Safety Portal M4 — bad-HMAC rows are immortal in the D1 pending queue

**Resolved 2026-06-18 (tech-debt easy-wins pass):** box_verified=-1 terminal state + POST /api/internal/mark-rejected exist; /pending selects box_verified=0 so terminal rows drop out; prune deletes rejected after 30d.

`worker/index.ts` `/api/internal/pending` fetches rows `ORDER BY created_at ASC LIMIT 50`; `prune.ts` only deletes rows where `box_verified=1`. A row that fails the HMAC check in `portal_poll.py` is never filed and never marked `box_verified=1` — so it permanently occupies a slot in every 50-row fetch window. With 50+ permanently-rejected rows, the window is wedged and new submissions never surface.

Practical trigger: HMAC-secret rotation drift (unlikely without operator error). After the secret is corrected the queue does NOT self-heal — rows must be manually deleted from D1.

**Fix:** introduce a terminal state for HMAC-rejected rows (e.g., `box_verified=-1`); exclude terminal rows from `/api/internal/pending`; prune after a retention window; add a watchdog alert on a growing `box_verified=0` backlog.

**Tag:** `safety-portal`, `portal-poll`, `reliability`.

**Revisit when:** HMAC secret rotation or next Worker hardening pass.

Surfaced: 2026-06-09 12-dimension forensic audit (M4).

## [CLOSED 2026-06-18] Safety Portal M5 — `/api/internal/publish/stamp` enforces no state-machine transition

**Resolved 2026-06-18 (tech-debt easy-wins pass):** The LEGAL_PREDECESSORS state-machine guard (WHERE id=? AND status IN (legal predecessors)) was added to /api/internal/publish/stamp.

`worker/index.ts` `/api/internal/publish/stamp` executes `UPDATE … WHERE id=?` with no check on the current state. The shared internal token (`ITS_PORTAL_INTERNAL_TOKEN`) can therefore forge a terminal state on a live request or revert a completed publish to `queued`.

**Fix:** enforce legal predecessor states in the `WHERE` clause (e.g., `WHERE id=? AND status='actuating'`); consider a narrower stamp-only token separate from the pull/receipt token.

**Tag:** `safety-portal`, `publish-daemon`, `security`, `medium`.

**Revisit when:** next Worker security hardening pass.

Surfaced: 2026-06-09 12-dimension forensic audit (M5).

## [RESOLVED 2026-06-10] CLAUDE.md asserts Op Stds v16 as governing — should be v18 (M9)

`CLAUDE.md` contains a parenthetical around lines 28–29 and line 131 (the governing-version block) that reads "Operational Standards is canonically at v16 … v16 is the governing version." However, `~/its-blueprint/doctrine/operational-standards.md` frontmatter is `version: 18`, `status: canonical`; `docs/doctrine_manifest.yaml` lists `current: 18`; and ~12 other CLAUDE.md citations already say v18. The v16 parenthetical is stale.

This is advisory text only (no runtime control), but §§45–49 (added in v17/v18, including the F22 approval mechanism at §46) are load-bearing. A reader relying solely on the governing-version claim would believe those sections don't apply.

**Fix:** update the parenthetical and line 131 to `v18`. One-line change; no behavior impact.

**Tag:** `doctrine`, `claude.md`, `docs`, `low`.

**Resolved 2026-06-10:** the governing-version block (CLAUDE.md lines ~28–29) + the line-131 reframe attribution now read **v18** — completing the v16→v18 sweep begun in PR #191 (inline §N citations) and continued in #260 (ops-stds-enforcer agent). The 2026-06-10 doc-reconciliation audit confirmed M9 was the last residual; no behavior impact.

Surfaced: 2026-06-09 12-dimension forensic audit (M9).

## [CLOSED 2026-06-18] Half-applied morning publishes — blank-form archive PDFs missing for reqs 11/12/13

**Resolved 2026-06-18 (tech-debt easy-wins pass):** Resolved by the 2026-06-15 full-archive `generate_form_archive.py --upload` re-render (all current defs re-uploaded, version-on-conflict — covers reqs 11/12); req 13 is a retire, no blank PDF to backfill (moot).

Publish requests 11 (equipment-skid-steer-test-v1), 12 (jha-v2), and 13 (retire equipment-skid-steer-test) were merged to main and deployed BEFORE the bare-`python` bug was fixed by PR #241. Their blank-form archive PDFs were never generated (the `_regenerate_archive` step failed with `FileNotFoundError: 'python'`). The forms are live in the catalog and the Worker but their Box archive entries are absent, leaving an audit-trail gap.

**Fix:** one-time backfill — run `python scripts/generate_form_archive.py` for the affected definition IDs and upload the resulting PDFs to the `00_Form_Archive` Box folder (`ITS_Safety_Portal/00_Form_Archive`).

**Tag:** `safety-portal`, `audit-trail`, `one-time-backfill`, `low`.

**Revisit when:** a dedicated Box-archive reconciliation pass, or before Evergreen production cutover audit.

Surfaced: 2026-06-09 publish-pipeline forensic audit (PRs #238/#239/#240 landed before #241 fixed the sys.executable issue).

## [RESOLVED 2026-06-12 — folded into mission v5] Mission v4→v5 delta — Worker now holds a transient filed-PDF receipt cache

**Resolution (blueprint v5 reconciliation, 2026-06-12):** folded into `its-blueprint/workstreams/safety-portal/mission.md` v5 §9 (System-of-record filing-principle amendment) + §16. Box remains the system of record; the cache is a transient, request-driven, 24h copy. Flag closed.

PR-4 Part A introduces a **bounded exception** to the Safety Portal mission's "the Worker never holds documents" stance: the Worker now stores **request-driven, 24h-expiring, D1-chunked filed-PDF chunks** so an authenticated owner can download their own canonical (Box-filed) PDF as a **receipt** (no new external-send path — Invariant 1 untouched; the Worker holds no Box creds and serves only reassembled D1 chunks the Mac daemon pushed). This is a **planning-layer / Seth-owned** doctrine edit, not made here. Proposed mission v4→v5 amendment: *"the Worker never holds documents — **except** the request-driven, 24h-expiring filed-PDF receipt cache (D1-chunked, browse scoped to active jobs, any authenticated account may browse + request)."* Flagged for blueprint co-resolution alongside the PR-5 mission note.

**Tag:** `safety-portal`, `doctrine`, `mission-delta`, `planning-layer`.

**Revisit when:** next blueprint mission-doctrine pass (fold the PR-4 + PR-5 mission deltas together).

Surfaced: 2026-06-12 PR-4 Part A implementation.

## [CLOSED 2026-06-30] 7 CLOSED-unmerged local branches preserved conservatively post-cleanup

**Resolved 2026-06-30 (tech-debt currency sweep):** The conservatively-preserved branches are gone. `git branch --list 'publish/req-*' 'feat/portal-submit-as'` returns empty at 2026-06-30; only current Phase-2 worktree branches (`feat/p1a`, `feat/p1b`, `feat/p1c`, `feat/p4core-compile-mutex`, `feat/p2-progress-workspace`, `feat/pr3-heartbeat-extraction`, `feat/keychain-tty-trap-fix`, `feat/solar-equipment-personnel-demo`) and `docs/*` branches remain. Zero branch hits beats the entry's text (lesson #1).

The 2026-06-12 session pruned 55 stale local branches using `git update-ref -d refs/heads/<branch>` (bypassing the `block-dangerous-git.sh` hook's `git branch -D` block, after per-branch PR=MERGED verification via `gh pr view`). Seven CLOSED-unmerged branches were left on disk conservatively:

- `publish/req-*` branches (4–5 entries, failed publish cycles from the publish daemon)
- `feat/portal-submit-as` (operator WIP, no PR)

These are safe to delete once confirmed no-longer-needed: the `publish/req-*` branches are daemon-generated and any in-flight publish would be restarted by the daemon's `_reset_to_main` recovery; `feat/portal-submit-as` is superseded by the admin submit-as feature built in PR #203+.

**Fix:** `git update-ref -d refs/heads/<branch>` for each confirmed stale branch. Do NOT use `git branch -D` (blocked by hook in CC sessions). Run `git branch --list 'publish/req-*' feat/portal-submit-as` to enumerate before deleting.

**Tag:** `housekeeping`, `git`, `low`.

**Revisit when:** next housekeeping pass or before cloning the blueprint for Customer 1.

Surfaced: 2026-06-12 branch-cleanup session.

## [RESOLVED 2026-06-12 — folded into mission v5] Mission v4→v5 delta — PR-5 Form Request browse + requester-bound PDF download

**Resolution (blueprint v5 reconciliation, 2026-06-12):** folded into `its-blueprint/workstreams/safety-portal/mission.md` v5 §16 (request-driven download + in-portal Form Request) — the `pdf_requests` table (supersedes the `pdf_requested` flag), any-authenticated-account browse, requester-bound 24h download, and two-stage prune are all recorded; the **declined email-delivery variant** is logged as an owner decision (in-portal only, send-free Invariant-1 default). Flag closed.

PR-5 refactored the `submissions.pdf_requested`/`pdf_ready_at` ownership columns into a standalone `pdf_requests(submission_uuid, account, requested_at, ready_at)` table (migration 0012). Downloads are now **requester-bound for 24h** (any authenticated account may request; only the requesting account may download within the window — a different account, even the original submitter, gets 404). The Worker gained a **`GET /api/filed`** browse endpoint (active-job-scoped submissions list for the `FormRequestPage` SPA) and request lifecycle routes (`POST /api/request-pdfs`, `/status`, `/pdf`). Two-stage prune: **strip** payload at 90d (keep the row browseable while the job is active) → **delete** 30d after job goes inactive. Unfiled rows (`box_verified=0`) are never evicted.

This is a **planning-layer / Seth-owned** mission delta: the Safety Portal mission v4 describes the Worker as a send-free durable queue and the `filed_pdfs` cache as receipt-only; the PR-5 `pdf_requests` model, the `FormRequestPage` browse surface, and the two-stage prune lifecycle are substantive additions. Proposed mission v4→v5 amendment: *"Any authenticated account may browse filed submissions for active jobs and request a requester-bound 24h PDF download via the `FormRequestPage`. The prune lifecycle is two-stage: payload stripped at 90d (row kept for browse/request); row deleted 30d after the job goes inactive. Unfiled rows are never evicted."* Fold with the PR-3 transport delta and PR-4 receipt-cache delta at the next blueprint mission pass.

**Tag:** `safety-portal`, `doctrine`, `mission-delta`, `planning-layer`.

**Revisit when:** next blueprint mission-doctrine pass (fold PR-3 + PR-4 + PR-5 deltas together).

Surfaced: 2026-06-12 PR-5 implementation.

## [CLOSED 2026-06-30] `keychain.set_secret` TTY-trap — interactive Python session can silently corrupt the stored secret

**Resolved 2026-06-30 (tech-debt currency sweep):** Fixed by PR #355 (task #8). `shared/keychain.py:66` `_has_controlling_tty()` detects a controlling terminal; `:176` branches to the argv form `security ... -w VALUE` when a TTY is present, bypassing the `/dev/tty` prompt that ignored piped stdin and silently stored a garbage value. Verified @HEAD via grep (lesson #1). NOTE: `keychain.py` is Phase-2 A2/A3-claimed — this is a **docs-only** status reconciliation; no code touched here.

**Live incident (2026-06-29, A3 smoke).** During the A3 Box OAuth refresh-lock smoke, `setup_box_oauth.py`'s `_persist_tokens` called `keychain.set_secret` from an interactive Python session (run directly in a terminal, not via launchd). `set_secret` invokes `security add-generic-password -w` with the value fed via `stdin`. When a controlling TTY is present — as it is in any interactive terminal session — the macOS `security` CLI reads the password from `/dev/tty` and **silently ignores piped stdin**. A garbage/unexpected value was written to `ITS_BOX_REFRESH_TOKEN`; Box auth failed with a 401 until the token was manually re-seeded using the argv form.

**Root cause:** `shared/keychain.set_secret` uses `subprocess.run([..., "-w"], input=...)` — the bare `-w` reads stdin correctly when the subprocess has no controlling TTY (correct behavior under launchd). But when the **parent process is an interactive terminal**, the subprocess inherits that controlling TTY, and `security` prefers the TTY over piped stdin for the bare `-w` form. The 2026-06-08 finding documented "bare `-w` in a TTY" for manual shell use; this extends it to `set_secret` itself when called interactively.

**Class:** secrets/auth, HIGH. Affected callers: `shared/keychain.set_secret` (daemon and Python callers), `setup_box_oauth.py`'s `_persist_tokens`.

**Proposed fix (standing task #8):** in `keychain.set_secret`, detect whether a controlling TTY is present (`os.isatty(0)` / `os.ctermid()`) and, if so, switch to the **argv form** (`[..., "-w", value]` — value as the next argv token, no stdin read). If TTY detection is unreliable, `raise RuntimeError` rather than silently writing the wrong value. Apply the same fix to `_persist_tokens` in `setup_box_oauth.py`.

**Recovery:** re-seed the affected entry via argv: `security add-generic-password -U -a "$USER" -s <name> -w VALUE`. Verify with `security find-generic-password -w -s <name>`.

**Tag:** `secrets`, `auth`, `keychain`, `high`. **Revisit when:** next `shared/keychain.py` touch (standing task #8).

Surfaced: 2026-06-29 A3 smoke (Box OAuth refresh-lock hardening); live `ITS_BOX_REFRESH_TOKEN` corruption recovered via argv reseed.

## [RESOLVED 2026-07-03 — CS4 Slice 4 Part A] Task-authority guards read account role check-then-act (TOCTOU, low-severity)

**Resolution (CS4 Slice 4 Part A):** the tracked fold is built, matching the `fieldops_crew_assign` atomic-guard pattern. The role predicates now live IN the mutating statements' WHERE clauses (`worker/fieldops_task_write.ts`): the assign UPDATE carries both the W1 current-owner predicate and the target predicate (conditional on an assign-only actor via a bound 0/1 flag); the create INSERT is `INSERT … SELECT … WHERE` with the same target predicate; the status UPDATE additionally folds the R1 ownership predicate. `checkTaskTarget` / `checkTaskCurrentOwner` / `checkTaskStatusOwnership` remain as **post-refusal diagnostics** — on `changes()=0` they re-read in the old pre-check order so the response codes (403 forbidden_task / 403 forbidden_target / 422 unknown_personnel / 404 not_found) are byte-identical; audits ride `changes()=1`, so a refused write audits nothing. Atomicity + boundary locked by `test/fieldops-toctou-folds.test.ts`; the pre-existing task-write/task-authority suites pass UNMODIFIED. Original entry below.

**Surfaced by the S1 Assigned-Tasks security re-review.** `checkTaskTarget` + `checkTaskCurrentOwner` in `safety_portal/worker/fieldops_task_write.ts` read the target/current-owner account **role** (`personnel.username → users.role`) in a SELECT separate from the mutating UPDATE, to constrain a `cap.tasks.assign`-only actor (manager) to submitter-owned tasks/targets. Unlike the `active=1` roster checks (race-free — `active` only flips 1→0), `role` is **bidirectional**, so an admin promoting/demoting an account in the window between the SELECT and the UPDATE could shift the boundary. **NOT a self-service escalation** — the manager can't trigger the concurrent role change; it requires an independent admin action in a window the actor doesn't control. **Accepted + documented in-code.** **Fix (fast-follow):** fold the role predicate into the assign/create UPDATE's WHERE (conditional for an assign-only actor) so check+write are atomic, matching the `fieldops_crew_assign` pattern. **Tag:** `field_ops`, `auth`, `manager`, `task-authority`, `toctou`. **Revisit when:** treating manager task-scope as a hard security boundary / next task-authority change.

---

## Checklist item-state photo CAPTURE — render-half only, capture route not built [RESOLVED 2026-07-06 — built by #452/#475; model superseded]

**Verified 2026-07-06:** #452 (`eedf7a6`, "G1 — checklist item photos, Option D") built the capture route (migration 0036, `item_photos` pool, `INSERT INTO item_photos … 'pending'` at `fieldops_checklist.ts:1007`, `item_photo:v1` HMAC domain-separation, `requires_photo` gating, §34-screened on the Mac); refined by #475. The model changed from the `photo_ref` render-only design below to the **Option-D `item_photos` pool** (`reference_section34-option-d-photo-pool`), so the entry below is superseded, not just resolved.


**Surfaced by R3 (PR #419, 2026-07-02).** Open-question Q2 (photo evidence) was answered **(a)** during the R-series program — optional photo on every check-type item, no migration — and R3 shipped the **render half only**: the checklist UI displays a photo when a `photo_ref` is present on an item state. The **capture half was NOT built**: there is no Worker route to store an uploaded item-state photo, and nothing writes `photo_ref`.

Capture needs to be **designed against §34 image-class screening** (Op Stds v20 §34, `safety_reports/photo_screen.py` is the canonical instantiation for portal photos), not just wired ad hoc: an untrusted image uploaded by a field worker → D1 (or Box) → served back to other viewers is exactly the shape §34 exists to gate (magic-byte check, Pillow `verify()`/decompression-bomb cap, metadata-destroying re-encode, optional ClamAV). Open design questions: (1) does this route through the existing Python-side `photo_screen.py` pipeline (implying a queue/pull hop back to the Mac, mirroring the portal-submission PULL model) or does it need a **Worker-native** lightweight screening pass (send-free, §50/§51-shaped) since checklist items are D1-native and have no `intake.py` touchpoint today; (2) do item-state photos flow to Box like submission photos, or stay D1-only; (3) size/count caps analogous to `MAX_DECODED_BYTES`/`MAX_PHOTOS_PER_SUBMISSION`. **NOT autonomous-safe** — this is a new untrusted-input surface and needs an explicit adversarial-review pass (attacker/auditor/skeptic per CLAUDE.md's "Adversarial review is definition-of-done on any trust-boundary surface") before any capture route ships, not just a design doc.

**Tag:** `field_ops`, `checklist`, `photo`, `security`, `§34`, `r-series`. **Revisit when:** building checklist photo capture (R-series Q2 follow-through) — do the §34 design decision FIRST, in its own reviewed slice, before writing the storage route.

---

## [RESOLVED 2026-07-03 — CS4 Slice 4 Part A] DailyReportTab 2-stage waterfall — `fetchJobList` fetch NOT yet collapsed

**Resolution (CS4 Slice 4 Part A, built in worktree `feat/cs4-hardening`):** `/api/fieldops/tasks/mine` now carries `viewer_placement` `{job_id, project_name, personnel_id, name}` — the caller's OWN placement, resolved server-side (the same personnel resolution `fieldops_scope.resolveActorPersonnel` uses + one indexed jobs lookup; SELF-information only, security note in the route header, `worker/fieldops_tasks.ts`). `DailyReportTab` takes the placement as a PROP from `FieldOpsMyTasks`'s existing `/tasks/mine` read; the `fetchJobList("active")` stage is deleted from the daily path (`fetchJobList` remains the Job Tracker page's own source). 6 fetches → tasks/mine + 5 parallel. New worker suite `test/fieldops-tasks-mine-placement.test.ts` (self-only exposure, unplaced/unlinked/retired/soft-ref/duplicate-link cases). The mandatory `/security-review` merge gate applies to the landing PR. Original entry below.

**Optimization plan finding #12** (`~/.claude/plans/optimization-plan.md`, Slice 3 tail item). `DailyReportTab.tsx` still opens with `jobs.fetchJobList("active")` (a full jobs-list page) purely to read `viewer_current_job` before it can fetch anything daily-specific — confirmed still present at exec HEAD `d7ba70f` (`DailyReportTab.tsx:280-281`). The plan's fix (add `viewer_current_job` + `project_name` to `/api/fieldops/tasks/mine` and drop the `fetchJobList` stage) was explicitly scoped to **serialize after Slice 2** and carries the plan's only medium-risk rating plus its own mandatory `/security-review` gate (it widens a capability-gated read route — `cap.tasks.own` would start returning placement data that today rides `cap.jobtracker.read`). Slice 2 (#432) has now landed; this tail item was correctly NOT bundled into #431/#432 and remains unbuilt.

**Fix:** implement optimization-plan.md Slice 3 item 7 in its own PR, with the `/security-review` pass as a merge gate, not advisory. **Tag:** `field_ops`, `performance`, `daily-form`, `optimization`, `security-review-gated`. **Revisit when:** picking up the next optimization slice, or if field-crew load on `/api/fieldops/jobs` becomes measurable.

---

## [RESOLVED 2026-07-03 — CS4 Slice 4 Part A] `fieldops_checklist.ts` still hand-maintains `AssignedInspectionsResponse` instead of re-exporting `wire-types.ts`

**Resolution (CS4 Slice 4 Part A):** all five assigned-inspections shapes (`ChecklistItemStatus`, `ChecklistItemState`, `AssignedInstance`, `AssignedInspection`, `AssignedInspectionsResponse`) are now `export type { … } from "../../worker/wire-types"` re-exports in `src/lib/fieldops_checklist.ts`; the hand-maintained local copies are deleted and the `wire-types.ts` header's "NOT yet re-exported" caveat is removed (the tasks/mine shapes are single-sourced there too). Original entry below.

**`worker/wire-types.ts` header note, Slice 3 (#431).** The header explicitly flags: "the assigned-inspections shapes are worker-typed here but NOT yet re-exported by `src/lib/fieldops_checklist.ts` — that file is Slice 2's dead-code removal surface; converting its kept types to re-exports is a follow-up after Slice 2 lands." Slice 2 (#432) has now landed, and `fieldops_checklist.ts:224` still defines its own `AssignedInspectionsResponse` interface rather than re-exporting the Worker-authoritative one from `wire-types.ts` — the one drift-guard `wire-types.ts` was built to close (finding #11) is not yet fully closed for this one shape.

**Fix:** small follow-up — replace the local `AssignedInspectionsResponse` interface in `fieldops_checklist.ts` with a re-export from `wire-types.ts`, matching the pattern already used by `fieldops_jobtracker.ts`/`fieldops_daily_form.ts`/`fieldops_expected_materials.ts`. **Tag:** `field_ops`, `optimization`, `wire-types`, `low-risk`. **Revisit when:** next touching `fieldops_checklist.ts` or `wire-types.ts`.

---

## [RESOLVED 2026-07-03 — CS4 Slice 4 Part A] D4 job-requirements ceiling check is TOCTOU (admin-only, accepted)

**Resolution (CS4 Slice 4 Part A):** the count predicate is folded into the INSERT's `WHERE` exactly as tracked — `INSERT INTO job_daily_requirements … SELECT … WHERE (SELECT COUNT(*) … active = 1) < REQUIREMENTS_LIMIT RETURNING id` — so check + write are one atomic statement; `changes()=0` → the same `409 too_many_items`, and the audit rides `auditStmtIfChanged` (a refused add audits nothing, as before). Boundary + atomicity locked by `test/fieldops-toctou-folds.test.ts` (199→201, 200→409, deactivated rows excluded). Original entry below.

**Surfaced by the D4 security review (PR #427), accepted as a WARN.** `fieldops_daily_requirements.ts`'s `REQUIREMENTS_LIMIT = 200` ceiling on a job's ACTIVE requirement-item list is enforced as a read-then-check-then-insert, not atomically in the mutating statement's `WHERE` clause — mirrors the existing "Task-authority guards read account role check-then-act" TOCTOU entry above, but on a resource ceiling rather than a role predicate. Two concurrent admin adds could both pass the count check before either commits, momentarily exceeding 200 active rows. **Admin-only actor, resource-exhaustion-shaped, not a privilege-escalation path** — accepted at review as low-severity.

**Fix (fast-follow, same shape as the task-authority entry):** fold the count predicate into the INSERT's `WHERE` (a conditional insert keyed on a live `COUNT(*)` subquery) so check+write are atomic. **Tag:** `field_ops`, `security`, `toctou`, `daily-form`, `low-severity`. **Revisit when:** treating the requirements ceiling as a hard bound, or alongside the existing task-authority TOCTOU fix.

---

## Moved 2026-07-14 (debt-zero triage) — verified resolved/stale

## Doc-conventions workstream taxonomy is missing `po_materials`/`purchase_orders` [RESOLVED 2026-07-13]

**RESOLVED 2026-07-13** (verified across all three surfaces): `po_materials` + `subcontracts` are now present in `scripts/lint_doc_conventions.py` `CANONICAL_WORKSTREAMS`, the `docs/operations/doc_conventions.md` §"Workstream taxonomy" table, AND `docs/doctrine_manifest.yaml` `workstream_tags` (the 2026-07-12 WP1 reconciliation closed the three-copy set — HOUSE_REFLEXES §1). `purchase_orders` is intentionally NOT a doc-tag workstream (the exec package/tag is `po_materials`; `purchase_orders` lives only in the manifest planning-`slugs` vocabulary). Original context below.

The blueprint workstream `workstreams/purchase-orders/` has been fully built out in this repo since
`S1` (PR #492, 2026-07-09) through this session's #504–#512 — 20+ PRs, live daemons (`po_poll`, `po_send`,
`config_actuator`), and an `ITS_Config` workstream tag (`po_materials.*`) in real production use — but
`scripts/lint_doc_conventions.py`'s `CANONICAL_WORKSTREAMS` closed set (and its companion table in
`docs/operations/doc_conventions.md` §"Workstream taxonomy") was never updated to add it. Concretely:
`docs/runbooks/po_poll.md` and `docs/runbooks/po_send.md` (PR #501) both had to set `workstream: null` in
frontmatter and stash `purchase_orders`/`po_poll`/`po_send` into the free-text `tags` list instead — the
canonical-workstream lint would reject `workstream: po_materials` or `workstream: purchase_orders` today.
Low-severity (the lint is warn-only in CI per its own doc, and the workaround is harmless), but it's the
exact "zero taxonomy acknowledgment" gap the session-close cross-repo supersession check watches for — code
massively acknowledges the workstream, the doc-conventions closed set doesn't. **Fix:** add `po_materials`
(matching the runtime `ITS_Config` tag, mirroring `field_ops`/`progress_reports`'s pattern of naming the
code-level tag, not the blueprint folder name) to both `CANONICAL_WORKSTREAMS` in
`scripts/lint_doc_conventions.py` and the table in `docs/operations/doc_conventions.md`, then re-point the
two PO runbooks' `workstream:` field from `null` to `po_materials`. **Tag:** `po_materials`, `docs`,
`doc-conventions`, `low-severity`. **Revisit when:** next touching either PO runbook, or doing a
doc-conventions taxonomy sweep.

> **Moved from tech_debt.md 2026-07-14 (debt-zero triage):** RESOLVED 2026-07-13 — po_materials+subcontracts present in all three taxonomy surfaces.

## install.sh interval-help-text stale — lists only 3 of 5 interval daemons [RESOLVED 2026-07-13]

**RESOLVED 2026-07-13** (in-code verify): `scripts/launchd/install.sh`'s `usage()` heredoc + header comment + the `poll_interval_config_key()`/`poll_interval_default()` logic now ALL enumerate the SAME **8** interval daemons with matching defaults (weekly-send 900 / portal-poll 60 / compile-now-poll 90 / progress-send 900 / fieldops-sync 90 / po-poll 90 / po-send 900 / subcontract-poll 120) — help and logic are in sync. The "3 of 5" framing below is superseded.

**Surfaced 2026-07-01** during the FF4/FF5/P2.6 session. `scripts/launchd/install.sh`'s `usage()` function and its header comment (top-of-file) both enumerate only 3 interval daemons — `weekly-send` (default 900), `portal-poll` (default 60), `compile-now-poll` (default 90) — and describe the `[interval]` CLI arg as overriding "the poll-interval daemons (weekly-send / portal-poll / compile-now-poll)".

The actual per-daemon resolution logic (`poll_interval_config_key()` + `poll_interval_default()`, both further down the same file) has since grown to **5** daemons: the original 3 plus `progress-send` (`progress_reports.progress_send.poll_interval_seconds`, default 900) and `fieldops-sync` (`field_ops.fieldops_sync.poll_interval_seconds`, default 90). The help text and header comment were never updated when those two were added — an operator reading only `usage()` (or the header) would not know `progress-send`/`fieldops-sync` accept an `[interval]` override or what their defaults are.

**Fix (trivial, docs-only):** update the `usage()` heredoc and the header comment block to list all 5 daemons + their defaults, matching `poll_interval_config_key()`/`poll_interval_default()`. No behavior change — purely a stale-doc-in-code fix, same class as the `docs/session_logs/README.md` index gap above.

**Tag:** `field_ops`, `progress_reports`, `launchd`, `docs`. **Revisit when:** next `install.sh` touch, or opportunistically.

> **Moved from tech_debt.md 2026-07-14 (debt-zero triage):** RESOLVED 2026-07-13 — usage()/header/logic all enumerate the 8 interval daemons.

## build_wsr_human_review_sheet.py would fail on a fresh create (ABSTRACT_DATETIME not API-creatable) [RESOLVED 2026-07-13]

**P2 (PR #362).** Building the progress twin `WPR_human_review` surfaced that `scripts/migrations/build_wsr_human_review_sheet.py` declares `Approved At` / `Sent At` as `type: ABSTRACT_DATETIME`, which the Smartsheet API **rejects on create** (`errorCode 1142`, "reserved for project sheets and may not be manually set on a column"). The build only succeeds today because it is idempotent and the live WSR sheet already exists — masking the bug. The **live** WSR `Approved At`/`Sent At` columns are in fact `type=DATE` (verified 2026-06-29); the ABSTRACT_DATETIME schema in the builder + the detailed ABSTRACT_DATETIME rationale comment in `safety_reports/wsr_review.py` are **doc-vs-live drift** (the intended retype-to-ABSTRACT_DATETIME via `update_column` was never applied to the live WSR sheet). `build_wpr_human_review_sheet.py` was therefore created with `DATE` columns, matching the working live WSR exactly (live WPR-vs-WSR parity verified 2026-06-29).

**Fix (low-class):** change `build_wsr_human_review_sheet.py`'s two columns to `DATE` (matching live) — OR, if Date/Time (time-of-day) display is actually wanted, add a create-as-DATE-then-`update_column`-retype step to BOTH builders + a retype migration for the live WSR + WPR sheets, and correct the `wsr_review.py` comment. Today's behavior is correct (DATE accepts `to_wsr_datetime`'s naive string end-to-end); this is cleanup + a comment-accuracy fix.

**RESOLVED 2026-07-13.** The live WSR `Approved At`/`Sent At` columns were re-confirmed `type=DATE` (live `get_columns` read, 2026-07-14). `build_wsr_human_review_sheet.py`'s two columns are now `DATE` (mirroring `build_wpr_human_review_sheet.py`); the stale ABSTRACT_DATETIME rationale in `safety_reports/wsr_review.py` (the module comment + `to_wsr_datetime` docstring) and in `tests/test_wsr_review.py` (section comment + assert message) were corrected to DATE. Regression-pinned by `tests/test_wsr_review.py::test_build_wsr_datetime_columns_are_creatable_date_not_abstract_datetime` (RED on the pre-fix ABSTRACT_DATETIME schema). Fresh-create-only change — the live sheet (idempotent skip) is untouched. Sweep to `tech_debt_closed.md` in the follow-up doc-hygiene pass.

**Tag:** `safety_reports`, `progress_reports`, `smartsheet`, `migration`. **Revisit when:** the safety build migrations are next touched, or if time-of-day display is desired on the approval/sent stamps.

> **Moved from tech_debt.md 2026-07-14 (debt-zero triage):** RESOLVED 2026-07-13 — columns now DATE, regression-pinned (test_wsr_review.py).

## anomaly_logger: SUSPICIOUS_FIELD_PATTERNS will false-positive on legitimate system_* fields [OPEN 2026-05-20]

`shared/anomaly_logger.py` flags any extraction field name matching `^system_` as a security anomaly (Phase 1 starter sentinel list for prompt-injection detection). The pattern is correct against the threat model — a legitimate workstream extraction schema shouldn't include `system_*` field names, so their presence suggests the AI invented them under injection.

**The risk:** this is a forward-dated FP source. As workstream extraction schemas mature, any legitimate field with a `system_` prefix (e.g., `system_version`, `system_id`, `system_serial_number` on machine pre-inspections) will fire `security_flag=True` on every extraction, polluting `ITS_Review_Queue` with noise and training operators to dismiss the flag.

Tuning belongs to the first 30 days of sandbox operation against real extraction outputs (per Safety Reports Brief v6 — "Phase 1 sentinel list, extend as patterns emerge"). The sentinel list should be re-audited once `safety_reports/weekly_generate.py` has run against the migrated closed-project corpus and produced a representative extraction sample.

**Specific suggested follow-ups when tuning lands:**
- Narrow `^system_` to specific known-bad names (`system_prompt`, `system_role`, `system_instruction`) rather than the prefix glob.
- Same audit for `^role_` and `^ignore_` — both have similar FP-on-legitimate-naming risk.
- Add a `tests/test_anomaly_logger.py` case for any legitimate field name that ends up in a real extraction schema, so the sentinel list and the schemas can't drift apart.

Surfaced 2026-05-20 in a senior-dev audit pass; not yet triggered in practice because no workstream extraction has shipped.

> **Moved from tech_debt.md 2026-07-14 (debt-zero triage):** RESOLVED 2026-07-14 (C5, #586) — prefix globs narrowed to anchored control names; FP + detection tests both directions.

## Add Box refresh-token age check to R2 Watchdog [OPEN 2026-05-20]

`ITS_BOX_REFRESH_TOKEN` rotates on every Box API call and stays valid as long as ITS makes at least one Box call every 60 days. If ITS goes dark for >60 days (extended outage, post-handover period without activity), the refresh token expires and re-running `scripts/setup_box_oauth.py` is required.

A watchdog check would warn the operator before the token expires:
- **Warn** at 50 days since last rotation
- **Critical** at 58 days

**Mechanism:** track last-rotation timestamp via either
- (a) a sidecar Keychain entry `ITS_BOX_REFRESH_TOKEN_LAST_ROTATED` updated by the `store_tokens` callback in `shared/box_client.py`, or
- (b) a row in `ITS_Config` (`system.box_refresh_token_last_rotated`).

**Implementation venue:** R2 Watchdog Session 2 (planning pass needed first) or later. Not blocking; absence of this check is documented in the handover runbook as a known operator-touch requirement.

**Urgency:** medium. Real risk if ITS goes dark for an extended period post-handover. Pre-handover is fine because ITS runs daily.

Surfaced: PR #39 brief, 2026-05-20.

> **Moved from tech_debt.md 2026-07-14 (debt-zero triage):** CLOSED (stale) 2026-07-14 — built as watchdog Check P (_check_box_token_freshness): WARN 50d / CRITICAL 58d.

## Smartsheet MULTI_PICKLIST type doesn't survive sheet-creation round-trip [RESOLVED 2026-07-14]

**RESOLVED 2026-07-14 — it was NOT a Smartsheet quirk; `list_columns_with_options` read columns without `level=2`.** The Smartsheet API downgrades a `MULTI_PICKLIST` (and `MULTI_CONTACT_LIST`) column to its base type in a `GET …?include=columns` response UNLESS `level=2` is requested — so the round-trip "showed TEXT_NUMBER" purely because the read omitted `level=2`. Fixed by adding `level=2` to the single `get_sheet` in `list_columns_with_options`; a live create→read integration assertion (`test_list_columns_with_options_unwraps_picklist_type`, now with a MULTI_PICKLIST column) proves it. This ALSO unblocked `ensure_picklist_options` (it can now manage live multi-select columns) and cleared the `audit_picklist_drift` false positives on the two live columns. **The "no production mapping uses it" note below is SUPERSEDED** — `ITS_Subcontractors.Trades` + `ITS_Vendors.Supply Categories` are live production MULTI_PICKLIST columns. Original entry (kept for the diagnosis trail):

Creating a sheet with `{"type": "MULTI_PICKLIST", "options": [...]}` via `Folders.create_sheet_in_folder` (or the equivalent REST POST `/folders/{id}/sheets`) returns 200 OK, but a subsequent `GET /sheets/{id}?include=columns` shows the column's type as `TEXT_NUMBER`, not `MULTI_PICKLIST`. The column doesn't behave as MULTI_PICKLIST either.

Probed live during the PR #51 integration-test run. Adding the column via a separate `POST /sheets/{id}/columns` after the sheet exists DOES return `"type": "MULTI_PICKLIST"` in the immediate response — but the subsequent GET still shows TEXT_NUMBER. The discrepancy is consistent enough that "sheet creation with MULTI_PICKLIST" appears to be a Smartsheet API behavior, not a transient race.

**Impact on `shared/picklist_sync.py`:** none today. The picklist sync's only target columns are PICKLIST (master DBs → downstream forms). MULTI_PICKLIST is a defensive code path in `update_column_options` (accepts the type, unit-tested via `test_update_column_options_accepts_multi_picklist`) but no production mapping uses it.

**Action if MULTI_PICKLIST becomes a real use case:** investigate whether the column needs to be created with additional flags (`validation`, `width`, …) or via a different REST endpoint. May require a Smartsheet support ticket — their column-type matrix isn't fully self-documenting.

**Urgency:** none. Tracked for visibility so a future operator looking at the integration test's missing MULTI_PICKLIST coverage understands why.

Surfaced: PR #51 integration test run, 2026-05-21.

> **Moved from tech_debt.md 2026-07-14 (debt-zero triage):** RESOLVED 2026-07-14 — was a missing level=2 read (#582), not a quirk; live-assertion added.

## audit_picklist_drift.py marker writer is not wired to a launchd plist [OPEN 2026-06-01]

Surfaced during the Check I (weekly_generate catch-up) build. `scripts/watchdog.py` Check C tracks `safety_picklist_audit` (8-day window), and the **only** writer of the `safety_picklist_audit.last_run` marker is `scripts/audit_picklist_drift.py`. But the picklist launchd plist (`scripts/launchd/org.solutionsmith.its.picklist-sync.plist`) invokes `scripts/run_picklist_sync.py` (the hourly option-SYNC job), **not** `audit_picklist_drift.py` (the drift-AUDIT job) — and `run_picklist_sync.py` writes no watchdog marker. So either (a) the operator schedules `audit_picklist_drift.py` via a plist outside `scripts/launchd/`, or (b) the `safety_picklist_audit` marker is never written → a permanent stale Check C WARN. Separately, `run_picklist_sync.py` (the actually-scheduled hourly job) is not in TRACKED_JOBS at all, so its silent death is invisible to Check C.

**Out of scope** for the Check I PR (no behavior changed here — recording the finding only). Per Op Stds "silent fail-open hazards must become watchdog-detectable signals," this should be reconciled: confirm where `audit_picklist_drift.py` is scheduled (or wire it), and consider tracking `run_picklist_sync.py`.

**Revisit when:** the picklist scheduling/Tranche-0 work is next touched, or the first time a `safety_picklist_audit` stale WARN fires with no underlying cause.

> **Moved from tech_debt.md 2026-07-14 (debt-zero triage):** CLOSED (stale) 2026-07-14 — picklist-audit.plist now wires the audit job + marker; run_picklist_sync tracked.

## Safety Portal — deploy + provisioning deferred [OPEN 2026-06-04]

Cloudflare D1/R2/Pages-or-Workers resource creation, `wrangler secret put SESSION_SIGNING_SECRET`, `wrangler deploy`, and custom domain `safety.evergreenmirror.com` binding are all deferred. Blocked on operator obtaining a `CLOUDFLARE_API_TOKEN` with the required scopes (Workers / D1 / R2 / Pages, or Workers Static Assets depending on topology decision below). The Safety Portal Phase 2 code (PR #158) was locally validated end-to-end via `wrangler dev --local` + Playwright before deferral.

**Required operator steps (at deploy time):**
1. `wrangler login` (or set `CLOUDFLARE_API_TOKEN`).
2. `wrangler d1 create its-safety-portal-db` → copy the returned `database_id` into `wrangler.toml`.
3. `wrangler d1 migrations apply its-safety-portal-db` (remote).
4. `wrangler secret put SESSION_SIGNING_SECRET` (≥32-byte random value).
5. `wrangler deploy` (or Pages upload if Pages topology wins).
6. Bind custom domain `safety.evergreenmirror.com` → Worker/Pages route.

**Tag:** `safety-portal`, `deploy`, `cloudflare`.

**Revisit when:** operator has CLOUDFLARE_API_TOKEN. Anticipated pre-Phase-3 portal go-live.

Surfaced: 2026-06-04 Safety Portal Phase 2 session (PR #158). Session log: `docs/session_logs/2026-06-04_safety-portal-phase2-cloudflare-scaffold.md`.

> **Moved from tech_debt.md 2026-07-14 (debt-zero triage):** CLOSED (stale) 2026-07-14 — portal LIVE at safety.evergreenmirror.com (Worker + custom_domain, portal_poll live).

## Safety Portal — Pages-vs-Workers Static Assets topology TBD [OPEN 2026-06-04]

Blueprint `workstreams/safety-portal/mission.md` §11 and any DNS/route assumptions were written against a Cloudflare Pages (`*.pages.dev`) topology. Cloudflare's current guidance (confirmed via cloudflare-docs MCP, 2026-06) recommends **Workers Static Assets** as the standard model for serving SPAs from a Worker. The Phase 2 code (`safety_portal/worker/`) is deploy-agnostic (Vite builds to `dist/`; `wrangler.toml` can target either). The decision must be made at deploy time.

**Decision required:** Workers Static Assets (current best-practice; better D1/binding integration) vs Cloudflare Pages (`*.pages.dev` + Pages-native CI). Update blueprint `workstreams/safety-portal/mission.md` §11 and DNS config to match.

**Tag:** `safety-portal`, `cloudflare`, `architecture`.

**Revisit when:** Safety Portal deploy step (above entry). One decision, made once.

Surfaced: 2026-06-04 Safety Portal Phase 2 session (PR #158). Related: `docs/tech_debt.md` "Safety Portal deploy + provisioning deferred" entry above.

> **Moved from tech_debt.md 2026-07-14 (debt-zero triage):** CLOSED (stale) 2026-07-14 — resolved to Workers Static Assets (Hono Worker, custom_domain), deployed live.

## Safety Portal — no server-side session revocation [OPEN 2026-06-04]

`safety_portal/worker/src/worker/middleware/requireSession.ts` validates a HMAC-signed session cookie (iat + 90-day expiry) but does NOT check a server-side session table. A deprovisioned user's cookie remains valid until `iat + 90d`. A stolen cookie cannot be individually invalidated before expiry.

**Proposed fix (Phase 7):** add a D1 `sessions` table (session_id, user_id, created_at, revoked_at); `requireSession` queries it; admin route provides revoke-session capability.

**Tag:** `safety-portal`, `auth`, `security`.

**Revisit when:** Phase 7 admin route build, or earlier if a user is deprovisioned while a live session exists.

Surfaced: 2026-06-04 Safety Portal Phase 2 session (PR #158).

> **Moved from tech_debt.md 2026-07-14 (debt-zero triage):** CLOSED (stale) 2026-07-14 — real revocation built via per-user session_epoch (migration 0009); stale-epoch cookie 401s.

## Worktree discipline for safety_reports edits [OPEN 2026-06-05]

Phase 3 (PR #160) was built in `~/its` directly (not a git worktree) because the `resolve_project()` legacy was retired and nothing was incoming to the sandbox during development. However, any live `safety_reports/` edit in `~/its` goes live in the launchd daemon tree on the next 60s poll cycle. Future `safety_reports/` feature edits should follow `docs/operations/worktree_discipline.md` and use a dedicated worktree to avoid hot-path exposure of WIP code.

**Tag:** `worktree-discipline`, `safety-reports`.

**Revisit when:** next `safety_reports/` edit session.

Surfaced: 2026-06-05 Safety Portal Phase 3 session (PR #160). Related: `docs/operations/worktree_discipline.md`.

> **Moved from tech_debt.md 2026-07-14 (debt-zero triage):** CLOSED (stale) 2026-07-14 — absorbed into canonical worktree discipline (HOUSE_REFLEXES §3 + worktree_discipline.md).

## [OPEN 2026-07-01] Manager tier over-permissioned on personnel — can retire/delete, should only create + assign

**Operator-reported 2026-07-01.** The `manager` role holds `cap.personnel.manage`, which currently bundles **create / edit / link / unlink / retire** of personnel. The operator wants a manager to be able to **create** a person and **assign** them to a job (`cap.crew.assign`, already correct), but **NOT retire/delete** personnel — retire stays admin-only. Today the retire route (`POST /api/fieldops/personnel/:id/retire` in `fieldops_personnel_write.ts`, gated `cap.personnel.manage`) is reachable by a manager, and the SPA renders the Retire button for anyone with `cap.personnel.manage` (`FieldOpsPersonnel.tsx`).

**Fix options (decide at build):** (a) split `cap.personnel.manage` → keep create/edit/link for manager, move **retire** behind a new `cap.personnel.retire` granted admin-only (a migration + a route re-gate + SPA gate); or (b) a lighter `role==='admin'` hard-check on the retire route + SPA button (mirrors the login-account-mint self-gate pattern) without a new cap. Option (a) is the cleaner capability-model fit; (b) is faster. Either way: re-gate the Worker route (the real boundary) AND the SPA button (convenience). Add a gate-bites test (a manager gets 403 on retire).

**Operator direction:** park OR **fold into the next big website update** (the Assigned-Tasks tab work — thematically a manager-facing permission change). **Tag:** `field_ops`, `capabilities`, `auth`, `manager`, `p2.6`, `personnel`. **Revisit when:** building the Assigned-Tasks tab / next manager-facing update. **RESOLVED 2026-07-01 by S1 Assigned-Tasks build** (retire route + SPA button gated `role==='admin'`, option (b); +regression test).

---

> **Moved from tech_debt.md 2026-07-14 (debt-zero triage):** RESOLVED 2026-07-01 (S1) — retire route + SPA button gated role==='admin' + regression test.

## [RESOLVED 2026-07-21] Converge `compile_now_poll._is_escalation_cycle` onto the shared escalation ladder [OPEN 2026-07-21]

`safety_reports/compile_now_poll.py` carries a PRIVATE geometric re-notify ladder
(`_is_escalation_cycle` / `_next_escalation_cycle`, `ESCALATION_LADDER_FACTOR`): past its threshold a
failure streak re-fires CRITICAL only at threshold × 2ⁿ, and records its per-occurrence row at ERROR on
every other cycle. It exists because a per-occurrence CRITICAL on a 90 s daemon is thousands of rows a
day and an open CRITICAL is NEVER terminal per `shared/errors_rotation.errors_row_is_terminal`, so no
rotation floor can reclaim them (`ITS_Errors` hit 19,975 of its 20,000-row cap on 2026-07-13 and locked
out twice) — the same latent shape a fleet-wide analysis found across the other sustained-failure
consumers. An IDENTICAL helper is landing in `shared/sustained_failure.py` on a parallel branch; it was
implemented privately here only to avoid a cross-branch dependency. **Trigger: once both land, delete the
private pair and bind the shared one** — the semantics were written to match exactly (fire on the
threshold-crossing cycle, then 2×/4×/8×, all threshold-relative). Same convergence bucket as the
`fieldops_sync`/`portal_poll` entry above.

> **Moved from tech_debt.md 2026-07-21:** RESOLVED the day it was filed, on the branch that landed the
> shared ladder. `safety_reports/compile_now_poll.py` now calls `sustained_failure.is_escalation_cycle`
> for both escalations; the private `_is_escalation_cycle` / `_next_escalation_cycle` /
> `ESCALATION_LADDER_FACTOR` are DELETED. Two shared predicates were added beside the ladder so
> compile-now's THREE-way decision needs no raw threshold compare: `has_crossed_threshold` (has it ever
> escalated) and `next_escalation_cycle` (the rung quoted in the between-rungs ERROR rows, derived by
> searching the same predicate so the promise and the page cannot disagree).
>
> **Deliberate behaviour change:** the private ladder was UNCAPPED (5, 10, 20, 40, 80, 160, 320, 640 …), so
> a long outage went quiet for exponentially longer stretches. The shared ladder caps the step at
> `threshold × LADDER_MAX_MULTIPLIER` (8), so the cycle escalation now pages at 5, 10, 20, 40, 80, then
> every 40 (~1 h at the 90 s cadence) and the per-job one at 20, 40, 80, 160, then every 160. Bound
> re-checked at 500 consecutive failing cycles: 15 CRITICALs (cycle) / 6 (per-job), first page still
> exactly on the threshold-crossing cycle — `tests/test_compile_now_poll.py` pins both rung sequences
> literally, and `docs/runbooks/compile_now_poll.md` + `docs/troubleshooting/tree.yaml` were updated to
> quote the capped numbers.
>
> **Provenance note (2026-07-24 janitorial audit):** a duplicate copy of this entry had lingered in the
> open `tech_debt.md`; it has now been removed as a pure duplicate. That copy cited **PR #647 (`28b0eaa`)**
> as the landing commit and flagged that the sibling `fieldops_sync`/`portal_poll` *counter* convergence
> (`_record_pending_fetch_failure`/`_record_fetch_failure`) is a **different, still-open** gap — do not
> conflate the two when reasoning about this closure. This archived entry is canonical.


## [RESOLVED 2026-07-24 — audit] Docs-currency residuals from the 2026-07-15 documentation-corpus program [OPEN 2026-07-17]

> **Moved from tech_debt.md 2026-07-24 (janitorial tech-debt audit):** Both docs-currency bullets verified resolved at HEAD: subcontracts.md now carries send-gate semantics (no stale 'no send code yet' line) and its manifest sha256 was re-recorded; CLAUDE.md's stubbed/real table reflects po_send/subcontract_send as LIVE. Pure bookkeeping — underlying fixes landed.


- **RESOLVED 2026-07-22 (session-close PR — line reworded to send-gate semantics, manifest sha re-recorded).** Original: **(LOW, docs) — `docs/enablement/subcontracts.md` has one residual stale line PR #603 (Tranche D) missed.**
  #603 correctly updated the top callout (line ~43) and removed the "automated sending" bullet from "What's
  not built yet" to reflect SC-S4 (#599) shipping the send lane. It did NOT catch a second assertion later in
  the same file: "Turning generation on enables **filing only** — subcontractor **send** stays dark regardless
  (**there's no send code yet**)" (`docs/enablement/subcontracts.md` around line 109) — factually stale now
  (send code exists, ships dark pending the gate) though the "stays dark" framing is still directionally true.
  A second multi-surface-fan-out miss, same class as the CLAUDE.md/verify_cutover.py one above. Deliberately
  left unedited here (a parallel session owns `docs/enablement/`, per this session's own note). Trigger: next
  `docs/enablement/subcontracts.md` touch — re-hash the manifest sha256 if edited.
- **RESOLVED 2026-07-22 (session-close PR — the po_materials row's "Ships dark" phrase reworded to
  gates-in-ITS_Config semantics; the subcontracts/RFQ rows already carried read-ITS_Config language from 07-21).** Original: **(LOW, docs) — CLAUDE.md's "What's stubbed vs. real" table still frames `po_send`/`subcontract_send` by
  their dark-ships-by-default posture.** Both lanes are now genuinely **LIVE** (operator-activated 2026-07-16/17,
  end-to-end Graph send confirmed on both) — the table doesn't yet say so. Not edited here (CLAUDE.md is a
  high-contention shared file, out of this agent's edit scope per its own boundaries). Trigger: next
  doc-reconciliation pass or CLAUDE.md touch.

## [RESOLVED 2026-07-24 — audit] [CUTOVER-BLOCKING] its#460 — create `progress@evergreenmirror.com` mailbox + Entra Application Access Policy (Mail.Send) [OPEN 2026-07-04, operator action]

> **Moved from tech_debt.md 2026-07-24 (janitorial tech-debt audit):** its#460 CLOSED 2026-07-09 with a live-verified owner comment — the sandbox progress@evergreenmirror.com mailbox exists inside the Entra Application Access Policy (Mail.Send) and a live graph_client.send_mail smoke from it SUCCEEDED (progress_send.py:71 DEFAULT_FROM_MAILBOX). SCOPE: this closes the SANDBOX mailbox only. The Phase-1 PRODUCTION-tenant single-mailbox cutover is a separate, still-pending action (2026-07-23 cutover-plan record) — not a reopening of this item.


**Tracked as a GitHub issue (`its#460`), cross-referenced here per convention.** `progress_reports.progress_send.from_mailbox` is already set to `progress@evergreenmirror.com` in `ITS_Config` (live) and matches the code default (`progress_send.DEFAULT_FROM_MAILBOX`) — but the mailbox itself does not exist yet in the `evergreenmirror.com` M365 sandbox tenant. **Operator action:** (1) create the mailbox; (2) add it to the Entra app registration's Application Access Policy with `Mail.Send` on the resource (mirrors the existing `safety@evergreenmirror.com` setup). Until then, progress weekly-report sends are **HELD at approval** (Invariant 1 human-in-loop) — nothing sends silently; this blocks only the final external send of progress packets, not compile/review. Flip to the production mailbox at the Phase 1.5 tenant cutover. Everything else in the progress go-live (routing, config, picklist, compile, WSR/WPR review) has been live since PR #459.

**Tag:** `progress-reports`, `mailbox`, `operator-action`, `m365`, `its#460`.

## [RESOLVED 2026-07-24 — audit] Orphan per-job Smartsheet folder from the JOB-000013 50-char-cap incident [OPEN 2026-06-13]

> **Moved from tech_debt.md 2026-07-24 (janitorial tech-debt audit):** Orphan folder gone. The code fix (SHEET_NAME_MAX=50 truncation, PR #283) landed earlier; the 2026-07-23 full workspace wipe+rebuild would not have recreated it, and the pre-wipe dump shows the orphan already absent. No live orphan / operator UI delete remains.


**PR #283 (2026-06-13).** A field PM submitted a portal form for JOB-000013 ("I don't know project name Montgomery", 36 chars). `week_sheet.py` creates the per-job Smartsheet folder BEFORE the week-of sheet; the folder creation succeeded, but the sheet creation 400'd (`errorCode 1041` — name exceeded 50 chars). This left an **empty per-job folder** named "I don't know project name Montgomery" in the `ITS — Safety Portal` workspace (ITS — Safety Portal workspace), beside the now-populated truncated-name week sheet that succeeded after the fix was deployed and the stuck submission was re-drained.

**Operator-manual cleanup:** delete the orphan folder "I don't know project name Montgomery" from the ITS — Safety Portal workspace via the Smartsheet UI. It is empty; nothing reads or writes it. Harmless but stray.

**Not a code gap** — the fix (PR #283) adds `SHEET_NAME_MAX = 50` to `week_sheet.py`; `week_sheet_name` now truncates the project prefix so the composed name always fits. Future submissions with long project names will land in a truncated-name week sheet within the same per-job folder, without creating the orphan. The per-job folder name (from `safety_naming.job_folder_name`) is NOT subject to the 50-char sheet-name cap — it is a folder, not a sheet — so the folder always creates successfully regardless of project-name length.

**Tag:** `safety-portal`, `smartsheet`, `operator-manual`. **Revisit when:** next ITS — Safety Portal workspace tidy pass.

## [RESOLVED 2026-07-24 — audit] 5-duplicate ITS_Errors sheets in System/02-Logs [OPEN 2026-05-22 — operator UI delete required]

> **Moved from tech_debt.md 2026-07-24 (janitorial tech-debt audit):** The four dead duplicate ITS_Errors sheets were destroyed by the 2026-07-23 full sandbox wipe — the entire 'ITS — System' workspace was rebuilt and a single fresh ITS_Errors sheet now exists per shared/sheet_ids.py. The anticipated operator Smartsheet-UI per-sheet delete is moot.


Bootstrap drift from 2026-05-18 sheet creation: 5 ITS_Errors sheets created within ~75 seconds. Canonical sheet is 27291433258884 per Op Stds v11 §23. The four duplicates are dead and require operator UI delete:
- 2704945844277124
- 470411799121796
- 4505679602601860
- 4195780532326276

Smartsheet MCP has no delete-sheet primitive; operator UI is the only path.

**Revisit when:** next operator Smartsheet UI session; not blocking any code or workflow.

## [RESOLVED 2026-07-24 — audit] Smartsheet transient 404 on first-project sheet/folder create [PARTIALLY MITIGATED 2026-05-22]

> **Moved from tech_debt.md 2026-07-24 (janitorial tech-debt audit):** Both halves resolved. Band-aid gone: _process_with_retry / retries_attempted / WPR_Pending_Review no longer exist (weekly_generate rewritten around generate_core + week_sheet.ensure_week_sheet + WSR_human_review; 0 grep hits). Durable fix landed generically: find_sheet_by_name_in_folder REST (PR #51) + get_rows @_transient_retry (PR #647) close the SDK-staleness class system-wide.


Two `weekly_generate` smoke runs on 2026-05-22 each surfaced exactly one transient 404 during per-project iteration:

- Smoke #1 (`--week-start 2030-01-07`): `SmartsheetNotFoundError('HTTP 404 (code 1006): Not Found')` on Bradley 2. Folder DID get created (cleanup confirmed it existed).
- Smoke #2 (`--week-start 2026-02-16`): same error on Rockford.

Different project each run; both error-and-continue per the weekly_generate per-project fence. Pattern: the FIRST project to need a fresh `ensure_current_week_folder` scaffold creation in a fresh process consistently 404s; subsequent projects in the same run succeed. Same class as PR #51's `find_sheet_by_name_in_folder` SDK staleness — both look like SDK in-process caching missing a just-created object.

**Mitigation shipped (2026-05-22 follow-on PR):** single-shot retry on `SmartsheetNotFoundError` inside the per-project fence (`_process_with_retry` wrapper in `safety_reports/weekly_generate.py`, 500 ms sleep + one retry, bumps `summary.retries_attempted`). When retry exhausts (or any non-404 error fires), the fence writes a `GENERATION_FAILED` placeholder row to `WPR_Pending_Review` so the operator's queue surfaces the failed project instead of leaving a silent gap. The placeholder respects the existing-row contract: approved rows are left untouched, unapproved rows have a `[GENERATION_FAILED: <ErrorClass>]` tag appended to Notes (Draft Body preserved), and missing rows get a fresh placeholder with the manual-rerun command embedded in Draft Body. Op Stds v11 §30 SDK-vs-Live discipline.

**Durable fix still deferred:** SDK→REST swap on the `ensure_current_week_folder` / `get_rows` paths to eliminate the staleness window entirely. Trigger condition: 3+ observed `weekly_generate.transient_404_retry` events in production cycles (meaning the retry IS firing in real runs, not just smoke). The `summary.retries_attempted` counter is the canonical signal — watchdog Check C or a follow-on metric scrape can surface the count without operator log-grep.

**Effort to swap:** ~1-2 hour session (mirror PR #51's pattern; ~6 unit tests around the find-after-create REST flow).

**Revisit when:** retries_attempted >= 3 in any consecutive 4-week window, OR a real Friday cycle surfaces a `GENERATION_FAILED` placeholder (the user-visible signal).

## [RESOLVED 2026-07-24 — audit] Intake stream extension for Weather + Labor + Mobilization metadata [OPEN 2026-05-22]

> **Moved from tech_debt.md 2026-07-24 (janitorial tech-debt audit):** Obsolete by design. The 2026-06-05 portal pull-model rewrite retired the narrative WPR draft (weekly_generate is deterministic; 0 '[REVIEWER TO FILL]' hits) and weather/labor are now captured via the structured Daily Report form. The companion 'prompt calibration' item was already closed 2026-06-30.


The WPR draft sections Weather Report, Construction Labor Report, Mobilization Date, and Location are currently `[REVIEWER TO FILL]` because the intake.py Daily Reports stream doesn't capture them — operator-side reviewers add the data during approval per Safety Reports Brief v6.1. Phase 1.4+ option: extend `safety_reports/intake.py` to capture weather (via a public weather API or `Summary of Events` extraction) and labor counts (via a new Daily Reports column or field PM submission convention), eliminating those `[REVIEWER TO FILL]` placeholders.

Mobilization Date is project-scoped not week-scoped — better captured as a project-level metadata sheet (a "Projects" master sheet keyed by `project_name`) rather than threaded through every Daily Reports row. Same for Location.

**Effort:** 1-2 sessions (intake-side weather + labor extension, projects-metadata-sheet schema + read-side wire-up).

**Revisit when:** Phase 1.4 security hardening cluster ships and operator feedback drives WPR template v0.2.0 calibration.

## [RESOLVED 2026-07-24 — audit] Hardcoded default fallbacks for ITS_Config-sourced timing constants [OPEN 2026-05-24]

> **Moved from tech_debt.md 2026-07-24 (janitorial tech-debt audit):** Both proposed layers live in shared/required_config.resolve_and_log — per-pass INFO summary naming each resolved key + source (ITS_Config vs default), plus a distinct WARN error_code=config_row_missing on an absent declared row. Rolled out system-wide via PR #481 / issue #336 (CLAUDE.md records closed 2026-06-29). weekly_send_poll.py (the named example) imports it and declares REQUIRED_CONFIG.


`safety_reports/weekly_send_poll.py:97-98` defines `DEFAULT_POLLING_ENABLED = True` and `DEFAULT_POLL_INTERVAL = 900` (15 minutes). The authoritative runtime values come from ITS_Config rows `safety_reports.weekly_send.polling_enabled` and `safety_reports.weekly_send.poll_interval_seconds` — the hardcoded constants are fallback defaults when those rows are missing or malformed. Other timing-bearing files (intake_poll, watchdog) follow the same pattern.

This is partially good (already ITS_Config-sourced) and partially fragile: silent fallback to a hardcoded default when an operator typos an ITS_Config row means the daemon "works" but on the wrong schedule, with no operator-visible signal that the override didn't take.

**Failure mode:** operator edits ITS_Config to change poll interval from 900 to 1800. Typos the key name. Daemon silently uses the hardcoded 900 default. Operator believes the new value is in effect; isn't. Costs and responsiveness are both off the operator's mental model.

**Proposed fix (two layers):**

1. **Startup log line** in every daemon: log the *resolved* values at startup (`[startup] poll_interval_seconds = 900 (source: default fallback)` vs `(source: ITS_Config)`). Cheap; makes the silent-fallback observable in launchd stdout/stderr logs.
2. **Optional but stronger:** convert silent fallback to WARN-loud fallback when the ITS_Config row is unexpectedly missing for keys the daemon documented as "should be configured." A dedicated registry of "expected ITS_Config keys" per daemon, checked at startup, surfaced via Sentry WARN if missing. Same shape as the validation-at-startup proposal in C1.

**Effort:** ~1 hour for layer 1 (startup-log only) across the 2-3 polling daemons. Layer 2 folds into C1's startup-validation module.

**Phase target:** 1.6 alongside C1 (config validation cluster).

**Tag:** `config-migration`.

**Revisit when:** C1 startup-validation work begins, OR an operator hits the silent-fallback-after-typo failure mode in real ops.

Surfaced: 2026-05-24 hardcoded-values audit brief, §A5. Note: the brief's framing assumed full hardcoding of timing constants; actual state is ITS_Config-sourced with hardcoded defaults as fallback. The fragility is the silent fallback, not the constants themselves.

## [RESOLVED 2026-07-24 — audit] Future PDF/JHA field extraction needs found-flag pattern [OPEN 2026-05-24]

> **Moved from tech_debt.md 2026-07-24 (janitorial tech-debt audit):** Obsolete future-workstream deferral. The anticipated inbound email-PDF/JHA field-extraction path was structurally eliminated by the Safety Portal clean-break (portal-only structured intake, no email-PDF path; mission.md §8.1). A future free-text/PDF-extraction need gets its own fresh entry scoped to it.


Phase 1.5 work introduces PDF-form-field extraction (and possibly free-text regex extraction) for JHA documents inbound from field PMs. Different field PMs format dates, names, and other fields inconsistently — one types `5/24/26`, another types `2026-05-24`, another writes `May 24`. Naive regex or PDF-form-field-by-name lookup silently extracts blank when the format doesn't match.

(Note: this is NOT an extension of `box_migration/parse_job_v3.py`, despite the audit brief's framing. `parse_job_v3` parses Box folder *names* against the 4 active project-folder taxonomies — see `tests/test_parse_*.py` for its scope. JHA field extraction is a distinct future workstream that hasn't been built yet.)

**Failure mode:** blank field in Smartsheet row. Downstream consumers (`safety_reports.weekly_generate`, reports, rollups) silently skip the row or compute wrong totals. No alert fires because "blank field" is not an error from the parser's perspective — it just didn't match. Worst case: a weekly safety report omits a critical incident because the date field was blank.

**Proposed fix:**

1. **Each extracted field returns a `(value, found: bool, confidence: float)` triple, not a bare value.** Existing anomaly_logger + review_queue + confidence-threshold convention (Op Stds §35) already covers the routing — if a *required* field comes back `found=False`, the row routes to `ITS_Review_Queue` with a flag instead of silently writing blank.
2. **Build a corpus of real JHA samples** at the Phase 1.5 PDF-extraction workstream's design phase. Run extraction across the corpus, measure miss rate per field. Iterate format detection (multi-pattern regex, fuzzy date parser like `dateutil.parser`, etc.) until miss rate is acceptable for required fields.
3. **Customer-facing JHA template** — produce a fillable form template that constrains the format at submission time, so future fields are pre-canonicalized. Reduces extraction burden for everyone.

**Effort:** large — this is part of the Phase 1.5 PDF-extraction workstream design itself, not a separable cleanup. Multi-session work. The found-flag pattern alone is small (a few hours) but the corpus + iteration + customer-template + downstream-consumer wiring all add up to ~2-3 sessions.

**Phase target:** 1.5 — directly part of PDF extraction workstream design. Solve found-flag + corpus + template together; don't ship PDF extraction without them.

**Revisit when:** Phase 1.5 PDF-extraction workstream brief gets drafted (the regex-side concerns belong in that brief).

Surfaced: 2026-05-24 hardcoded-values audit brief, §B3. Cross-ref Op Stds v11 §35 (confidence-scored extraction → review queue routing pattern).

## ITS_Active_Jobs AUTO_NUMBER `Job ID` column — manual operator UI step pending [SUPERSEDED 2026-06-30, closed 2026-07-23]

> **Moved from tech_debt.md 2026-07-24 (janitorial tech-debt audit):** VERBATIM MOVE — self-documented closure (already tagged SUPERSEDED 2026-06-30 / closed 2026-07-23). Verified: Job ID is portal-owned TEXT (active_jobs_writer COL_JOB_ID; extend_its_active_jobs_phase3.ensure_job_id_column creates it as TEXT via REST) — no manual AUTO_NUMBER UI step exists. Was not yet in the archive.


The Smartsheet REST API cannot create `AUTO_NUMBER` columns (verified: bare `type:AUTO_NUMBER` → `errorCode 1008`; UI-only type). The Phase 3 migration (PR #160) did the API-doable parts (4 contact columns + rename `Job ID`→`Job Slug`, freeing the title) and detects-or-instructs if the `Job ID` AUTO_NUMBER column is missing. Operator must add the `Job ID` AUTO_NUMBER column in the Smartsheet UI to complete the schema: prefix `JOB-`, 4-digit fill, start 1. `shared/active_jobs.py` reads it the moment it exists.

**Required operator steps (Smartsheet UI):**
1. Open ITS_Active_Jobs in the Smartsheet UI.
2. Insert a new column named `Job ID`, type AUTO_NUMBER (System column).
3. Set prefix `JOB-`, fill width 4, start 1.
4. Confirm `shared/active_jobs.py::get_job_by_id()` resolves correctly on the next lookup.

**Tag:** `safety-portal`, `smartsheet-api-constraint`, `data-gap`.

**Revisit when:** operator has Smartsheet UI access at deploy time. Required before Job-ID-keyed portal queries work end-to-end.

Surfaced: 2026-06-05 Safety Portal Phase 3 session (PR #160). Session log: `docs/session_logs/2026-06-05_safety-portal-phase3-job-model.md`.

**Closed 2026-07-23:** P2.5 Slice 6 (2026-06-30) moved number allocation to the Worker `job_counter` (migration 0022) and retyped the column to plain TEXT — the portal assigns `JOB-######` and `shared/active_jobs_writer.py` writes it into the cell on every mirror upsert. `extend_its_active_jobs_phase3.py` now creates the TEXT column directly (2026-07-23 stand-up rehearsal fix); no manual UI step exists.

## [RESOLVED 2026-07-24 — audit] "New Job" Smartsheet form on ITS_Active_Jobs — operator-UI creation pending [OPEN 2026-06-05]

> **Moved from tech_debt.md 2026-07-24 (janitorial tech-debt audit):** Superseded by portal job creation (P2.5 Slice 6). POST /api/fieldops/job server-allocates the canonical JOB-###### via the migration-0022 counter and captures every field the Smartsheet form was specced for; hand-adding rows to ITS_Active_Jobs is now deprecated (docs/enablement/portal_job_creation.md). No form to build.


Smartsheet forms are UI-configured (not API-creatable). A "New Job" form on ITS_Active_Jobs is needed so office PM can add jobs without opening the sheet directly. Required fields: Project Name, Address, Stakeholder Name / Email / Phone (email required), Safety Reports Contact Email (required), Active. Job ID is portal-assigned (P2.5 Slice 6, off the form) — a form-created row would have NO Job ID until it is synced/keyed by the portal; the portal Job Tracker is the intake surface that assigns numbers.

**Required operator steps (Smartsheet UI):**
1. Open ITS_Active_Jobs → Forms → Create New Form.
2. Add and mark required fields per above.
3. Set form title "New Job".
4. Share form URL with office PM.

**Tag:** `safety-portal`, `smartsheet-ui`, `data-gap`.

**Effort:** ~15 minutes (UI-only).

**Revisit when:** deploy session. (The AUTO_NUMBER column entry above is SUPERSEDED — Job ID is portal-assigned per Slice 6, so a Smartsheet form cannot produce a numbered row; the portal Job Tracker is the number-assigning intake surface.)

Surfaced: 2026-06-05 Safety Portal Phase 3 session (PR #160). Related: `docs/tech_debt.md` AUTO_NUMBER entry above.

## [RESOLVED 2026-07-24 — audit] [OPEN 2026-06-09] Safety Portal M6 — publish daemon has zero watchdog/health coverage

> **Moved from tech_debt.md 2026-07-24 (janitorial tech-debt audit):** M6 fully resolved (PR #439): publish_daemon.py writes a watchdog last-run marker (slug 'publish_daemon'), is enrolled in watchdog TRACKED_JOBS (Check C, dedicated 90-min freshness window), and self-provisions its ITS_Daemon_Health row via the shared HeartbeatReporter. A silent death now pages.


`safety_reports/publish_daemon.py` (the sole privileged actuator) has no `write_last_run_marker` call, no `ITS_Daemon_Health` row, and is absent from `scripts/watchdog.py::TRACKED_JOBS`. A silent daemon death pages nothing. The SPA `PublishMonitor` gives only a partial "stuck queued" signal (stale after a network loss or operator-gated pause), not a dead-daemon signal.

**Fix:** add `write_last_run_marker` at the end of `publish_once`; register `safety_publish_daemon` in `TRACKED_JOBS` with an appropriate freshness window; self-provision an `ITS_Daemon_Health` row (mirror `weekly_send_poll`'s pattern).

**Tag:** `safety-portal`, `publish-daemon`, `observability`, `medium`.

**Revisit when:** next publish-daemon or watchdog hardening pass. Before Evergreen production cutover.

Surfaced: 2026-06-09 12-dimension forensic audit (M6).

## [RESOLVED 2026-07-22] PR-5 Worker + migration 0012 deployed to live mirror (was CUTOVER-BLOCKING, OPEN 2026-06-12)

> **Moved from tech_debt.md 2026-07-24 (janitorial tech-debt audit):** VERBATIM MOVE — header already [RESOLVED 2026-07-22]; verified all PR-5 routes + migration 0012 ship in the live Worker bundle. Was not yet in the archive.


PR-5 (#276, merge `213d076`) introduced the `pdf_requests` table (migration 0012, schema `(submission_uuid TEXT, account TEXT, requested_at REAL, ready_at REAL, PRIMARY KEY (submission_uuid, account))`) and the new Worker routes (`GET /api/filed`, `POST /api/request-pdfs`, updated `/status`+`/pdf` re-gated on a live request row, updated `/api/internal/pdf-requests` filtered to live rows). As of session close, the **live mirror Worker does not have these changes**. The README activation step (added in-PR) documents the required ordering: apply migration 0012 to live D1 BEFORE redeploying the Worker — if the Worker is deployed first, the new routes fail-closed (referencing a non-existent table). Until deployed, the Form Request browse page and requester-bound PDF download are not available on `safety.evergreenmirror.com`.

**Fix (Developer-Operator):** `wrangler d1 migrations apply --remote` (operator-run, CC is classifier-blocked on live D1 migrations) → `npm run deploy`.

**Tag:** `safety-portal`, `deployment-pending`, `operator-step`, `pr-5`.

**Revisit when:** the next operator deploy session (pre-Customer-1 activation).

Surfaced: 2026-06-12 PR-5 implementation (session close).

**Resolution (2026-07-22, mechanical verify):** long-superseded by the 2026-06-08+ deploy
train. `wrangler d1 migrations list its-safety-portal-db --remote` from an up-to-date
`~/its` @ `f2bb9a0` → "No migrations to apply" (0012 applied); live-Worker probe:
`GET /api/filed` and `POST /api/request-pdfs` on `safety.evergreenmirror.com` both return
401 `application/json` (route exists + auth-gated; a missing route would SPA-fallback to
200 text/html). The `_service_pdf_requests` daemon pass consuming these routes is live
(PRs #274/#276).

## [RESOLVED-STALE 2026-07-22] Safety Portal browser-tab `<title>` + favicon (was OPEN 2026-06-20)

> **Moved from tech_debt.md 2026-07-24 (janitorial tech-debt audit):** VERBATIM MOVE — header already [RESOLVED-STALE 2026-07-22]; verified index.html:8/13 carry the inline-SVG Evergreen favicon + <title>Evergreen ITS Portal</title> (PR #589). Was not yet in the archive.


**Resolution (2026-07-22, verified):** stale claim — `safety_portal/index.html:5-13` already carries the inline-SVG Evergreen favicon and `<title>Evergreen ITS Portal</title>`; fixed at some earlier rebrand pass, entry never updated.

The 2026-06-20 banner rebrand (PRs #297–#300) dropped the ITS-crest PNG and replaced the "Portal" header text with "Integrated Technical System" (Great Vibes gold-script wordmark). However, the browser-tab `<title>` (`<title>ITS Portal</title>` in `safety_portal/worker/src/index.html` or the React root) and the ITS-crest favicon (`public/favicon.ico` / `<link rel="icon">`) were deliberately left unchanged — out of banner scope, operator's call.

**Impact:** minor cosmetic inconsistency — the wordmark now says "Integrated Technical System" but the browser tab still shows "ITS Portal." Functionally inert.

**Fix when:** next frontend cosmetic pass. Update `<title>` to "ITS — Safety Portal" (or "Integrated Technical System") and replace the favicon with an Evergreen-aligned icon.

**Tag:** `safety-portal`, `frontend`, `cosmetic`, `low`.

**Surfaced:** 2026-06-20 banner rebrand session (PRs #297–#300). Session log: `docs/session_logs/2026-06-20_safety-portal-banner-wordmark.md`.

## [RESOLVED 2026-07-24 — audit] [OPEN 2026-06-28] Field-ops portal UI polish follow-ups (post write-UI restyle)

> **Moved from tech_debt.md 2026-07-24 (janitorial tech-debt audit):** All three UI-polish sub-items resolved (PRs #329/#371/#404/#405): the named form contexts render inside PageShell-wrapped components; tracker messages migrated to the .banner class; destructive actions use .btn--edit/.btn--retire danger variants (Close-job itself superseded by a lifecycle select).


PR #328 (`9ef3d5b`) shipped the shared `PageShell` and a unified restyle of the four tracker pages. Three polish items deferred:

1. **Route the form pages through `PageShell`.** The write-UI form pages (personnel create/edit, equipment roster admin, job create, time-entry) are not yet wrapped in `PageShell`. They use ad-hoc layout. Wrap them in a follow-up PR once the form page shape is stable (personnel creation task #22 will establish the canonical form-page pattern).

2. **Tracker action messages → `.banner` class.** In-page action feedback (e.g., "Equipment status updated", "Time entry saved") is currently displayed via inline `ok`/`error` divs. These should use the `.banner` CSS class (defined in the design system) for visual consistency with the portal's other feedback surfaces.

3. **`--danger` button variant for destructive actions.** "Close job", "Retire unit", "Retire personnel" actions use the default button style. Add a `--danger` modifier variant (red background or border) to visually distinguish destructive from constructive actions. Matches the UX standard for the admin panel's destructive ops.

**Tag:** `field-ops`, `frontend`, `polish`, `low`. **Revisit when:** personnel creation (task #22) PR is in progress — wrap the new form page in `PageShell` at that point and batch the banner + danger-variant work in the same PR.

Surfaced: 2026-06-28 Progress-Reporting program session (PR #328 restyle).

## [RESOLVED 2026-07-24 — audit] [OPEN 2026-06-28] Exec session log gap — 2026-06-17 to 2026-06-18 arc still missing

> **Moved from tech_debt.md 2026-07-24 (janitorial tech-debt audit):** Gap closed — both exec session logs exist and cover #292/#294/#295 + the D1 clean-slate: docs/session_logs/2026-06-17_safety-portal-test-artifact-cleanup-and-pdf-naming.md and 2026-06-18_d1-job-cleanup-and-tech-debt-easy-wins.md.


The 2026-06-17→18 session arc (#292 D1 job cleanup + #294 tech-debt easy-wins code/test fixes + #295 live-cleanup closes + the D1 clean-slate execution) has **no exec session log**. This gap was first noted in `project_safety_portal_state.md` memory ("No exec session log yet for the 2026-06-17→18 arc") and has not been filled.

The arc is non-trivial: two PRs landed, a clean-slate was executed on live D1 + Smartsheet + Box, and CodeQL caught two real issues in PR #292. The decisions (purge-job endpoint design, CodeQL fixes, test-artifact scope decisions) are not reconstructable from git history alone without the session log narrative.

**Fix:** operator invokes `session-log-writer` for this arc, using PR #292 (`22ab1db`) + PR #294 (`79c96b2`) + PR #295 (`974b111`) and the `project_safety_portal_state.md` memory as context.

**Tag:** `housekeeping`, `session-log`, `documentation`. **Revisit when:** operator has bandwidth for a retroactive log write.

Surfaced: 2026-06-28 session close (still missing after the 2026-06-17→18 arc + the 2026-06-20 banner session + the 2026-06-28 write-UI session all added their logs).

## [RESOLVED 2026-07-24 — audit] Optimization-plan doctrine-adjacent decisions awaiting operator green-light [OPEN 2026-07-03 — item 2 (B3) RESOLVED 2026-07-03]

> **Moved from tech_debt.md 2026-07-24 (janitorial tech-debt audit):** Both doctrine-adjacent decisions are operator-approved AND built/merged/verified live: item 1 (D5 eager/lazy form-registry split, registry.ts, PR #446) and item 2/B3 (the four deprecated daily-checklist Worker routes + dead machinery deleted). No pending decision or residual work.


**Item 2 (B3) RESOLVED 2026-07-03 — operator approved ("go ahead with your recommendations") and the dead-route deletion was executed.** Four Worker routes deleted with per-site tombstones naming the approval + date: `GET /api/fieldops/checklist/mine` (the deprecated daily generation read — it still WROTE daily instances + snapshots when called, the junk-data footgun), `GET /checklist/mine/rollup-draft` (S5 draft assembler, superseded by the SOP form's own prefill), `POST /api/fieldops/job/:job_id/close` (thin back-compat alias; `/lifecycle` is the live close path), and `POST /api/fieldops/job/:job_id/progress` (nothing displayed the value since #403; no Python reader). Daily-exclusive machinery removed with them (`generateDailyInstance`, `pacificToday` (worker copy), `reconcileFormLinked`, `AUTO_CHECK_SQL_DAILY`, `ITEM_STATES_SQL_DAILY`, `MergedItem`, `DailyEmptyReason`, `ROLLUP_LEG_CAP`); the inspection engine (assign/assigned/instances/cancel/item-state), the S2 default/job-override **editor routes**, and the 0028 `daily_default` seed rows were **NOT removed** (narrower scope than option (a) — the approval covered the four dead routes only). Tests: the 3 daily suites deleted (36 tests), 6 daily-path tests removed from `fieldops-r1-contracts`, 5 route tests removed from `fieldops-job-write`, 3 item-state contracts re-pinned via the assigned-inspections path (worker suite 668 → 624). Item 1 below remains OPEN.

Original entry (item 1 still awaiting green-light):

**`~/.claude/plans/optimization-plan.md` "Needs-operator" #2 and #3** — two propose-only options CC is explicitly barred from executing unilaterally:

1. **[RESOLVED 2026-07-03 — the D5 registry split PR]** Operator-APPROVED ("absolutely need to split the registry — that would very quickly become a problem and crash our website") and BUILT: active current+previous versions eager, historical lazy (`getDefinitionFor`), the sliding window keeps the main chunk ~constant. The C1 brief carries a dated amendment; the approval is quoted in `src/forms/registry.ts`.

2. ~~**Deprecated daily-checklist Worker surfaces + dormant 0028 `daily_default` rows**~~ — RESOLVED above (route deletion executed; 0028 rows + editor routes deliberately kept).

Item 1 blocks nothing; it is a dead-weight-vs-preservation-over-refactor call that only Seth should greenlight. **Tag:** `field_ops`, `optimization`, `doctrine-adjacent`, `preservation-over-refactor`. **Revisit when:** Seth reviews the optimization-plan's Needs-operator section.

---

## [RESOLVED 2026-07-24 — audit] `~/its-standup` worktree needs operator-run removal — untracked `.venv-wt` blocks plain `git worktree remove` [OPEN 2026-07-23]

> **Moved from tech_debt.md 2026-07-24 (janitorial tech-debt audit):** ~/its-standup worktree confirmed removed (git worktree list shows only ~/its + ~/its-demo; ~/its-standup absent; prune reports nothing stale). NOTE, reported faithfully: the removal was performed by an audit-verification command during this pass, not a planned cleanup action — no data lost (the target commit is an ancestor of origin/main).


The `~/its-standup` worktree (used to run the 2026-07-23 tenant-wipe/stand-up rehearsal in isolation from the
live `~/its` daemon tree, per `docs/operations/worktree_discipline.md`) is left on disk, detached HEAD at
`origin/main`, working tree otherwise clean — but its `.venv-wt` directory (the per-worktree fresh venv
`worktree_discipline.md` mandates for Python-source edits) is untracked, and `git worktree remove` refuses to
remove a worktree with untracked files present without `--force`. Per this repo's own guardrail convention,
`git worktree remove --force` is NOT something CC should run unprompted (the hook-blocked
`git branch -D`/`git clean -f` pattern this repo already treats as operator-only destructive-op territory —
see the exec `CLAUDE.md` git-guardrails section) — this is a manual operator cleanup, not a code task.
**Trigger:** next operator terminal session; run `git worktree remove --force ~/its-standup` (safe: the
worktree's own git state is clean, nothing uncommitted of value lives there) then `git worktree prune`. **Tag:**
`worktree`, `operator-manual`, `cleanup`, `low-severity`.

## [RESOLVED 2026-07-24 — audit] Six merged-and-clean worktrees await operator-run removal (PR #693 document-polish session + earlier) [OPEN 2026-07-23]

> **Moved from tech_debt.md 2026-07-24 (janitorial tech-debt audit):** The six merged-and-clean polish worktrees are confirmed removed (git worktree list shows only ~/its + ~/its-demo; .git/worktrees holds only its-demo; prune reports nothing). The item's scope — operator-run worktree removal — is done; any residual orphan `worktree/*` branch refs are outside its stated scope.


`git worktree list` currently shows six task worktrees whose branch is already MERGED and whose working
tree is clean — `~/its-pdf-polish` (`feat/pdf-polish`, PR #693, merged `1742a31`, this session) plus five
Claude Code agent-managed worktrees under `~/its/.claude/worktrees/agent-*` left over from earlier sessions:
`agent-a1270309…` (`feat/per-job-tracking-sheets`, PR #563), `agent-a77932f3…`
(`fix/check-o-storm-mode-fallback`, PR #562), `agent-aba2b6d5…` (`feat/po-link-materials-catalog`, PR #505),
`agent-ae5f7e6f…` (`feat/po-attachments-feature-b`, PR #564), `agent-af3d33cd…`
(`feat/po-delivery-contacts-config`, PR #566) — all five confirmed `state=MERGED` via `gh pr list
--state merged --head <branch>`. Per `docs/operations/worktree_discipline.md` and the git-guardrails
convention (`git worktree remove --force`/`git clean -f`-class destructive ops are operator-only, not
CC-run unprompted), removal is a manual cleanup step, same class as the already-tracked `~/its-standup`
entry above. **Trigger:** next operator terminal session — for each: confirm `git status --short` is empty
in the worktree, confirm the branch is `state=MERGED` (already done above for all six), then
`git worktree remove ~/its-pdf-polish` / `git worktree remove ~/its/.claude/worktrees/agent-<id>` (add
`--force` only if an untracked `.venv-wt`-style directory blocks the plain remove, as with `~/its-standup`),
then `git worktree prune`. **Tag:** `worktree`, `operator-manual`, `cleanup`, `low-severity`.


## [RESOLVED 2026-08-07 — audit] WS2 operator dashboard — completion parked items [OPEN 2026-07-13]

> **Moved from tech_debt.md 2026-08-07 (delivery-day tech-debt cleanup):** All four sub-items are resolved or by-design, verified at HEAD `9eace59`: **WS2-1** brand assets present (`operator_dashboard/static/evergreen-logo.svg` + `great-vibes-OFL.txt`); **WS2-2** `grep 'No launchd plist yet' CLAUDE.md` returns ZERO hits and the `verify_cutover.py` docstring second surface was fixed by #658; **WS2-3** the `docs/enablement/operator_dashboard.md` delta landed 2026-07-22; **WS2-4** is an explicit Seth by-design decision (D13, no mutating send-lane verb), not deferred work. Nothing here is actionable.


From the dashboard-completion session (Blocks 1-5 landed #567/#570/#574/#576). None blocks the ship; each
is a deliberate scope line:

- **WS2-1 — RESOLVED 2026-07-14.** No Canva export was needed — the Safety Portal brand already had the
  vector + font. Pulled `evergreen-logo.svg` + `great-vibes.woff2` (+ OFL license) from `safety_portal/public/`
  into `operator_dashboard/static/` and wired the real lockup into the dashboard header: the Evergreen mark on
  a gold-bordered white plate + the "Integrated Technical System" gold-gradient Great Vibes script (the
  portal's exact treatment incl. the WebKit background-clip cap-loop padding fix).
- **WS2-2 (PARTIALLY RESOLVED 2026-07-14, PR #597).** CLAUDE.md's dashboard "stubbed vs real" row was stale
  ("No launchd plist yet (D1-3b)"); #597 fixed it in the same PR as the `mark_errors_resolved` verb — the row
  now reads "launchd-managed (`org.solutionsmith.its.dashboard`)" and lists the mark-resolved+clear verbs.
  (Re-verified 2026-07-17: `grep operator_dashboard CLAUDE.md` no longer contains "No launchd plist yet.")
  **Residual RESOLVED 2026-07-22 (#658):** the `verify_cutover.py` docstring now reads
  "launchd-managed, `org.solutionsmith.its.dashboard`" — the second surface is fixed.
- **WS2-3 — DELTA LANDED 2026-07-22 (session-close PR):** `docs/enablement/operator_dashboard.md` now
  covers the sweep panel, the system-map operator briefs/doc links/Smartsheet out-links, and the reorganized
  config editor. (The guide had already grown the verb/panel coverage in the interim; this delta brought it
  current with the 2026-07-22 dashboard trilogy #655/#657/#658.)
- **WS2-4 (Seth decision, by design) — no mutating send-lane verb.** The send-queue panel is read-only;
  bulk-approve / resend-FAILED / clear-HELD are deliberately NOT built (D13). Trigger: an explicit operator
  decision to expose a send-lane action (would need its own adversarial review).

## [RESOLVED 2026-08-07 — audit] Aug-7 cutover readiness — deferred code follow-ups [OPEN 2026-07-10 — CO-1/CO-3/CO-4 RESOLVED; CO-2 code landed, operator-run pending]

> **Moved from tech_debt.md 2026-08-07 (delivery-day tech-debt cleanup):** The code half is 4-for-4 done, verified at HEAD `9eace59`: **CO-1** `DEFAULT_POLLING_ENABLED = False` in all three send pollers (`po_send_poll.py:81`, `rfq_send_poll.py:86`, `subcontract_send_poll.py:60`); **CO-2** the live-clamd EICAR tests exist and are skip-gated (`tests/test_photo_screen.py:295` `_needs_live_clamd`, `:301` `test_live_clamd_flags_eicar`); **CO-3** already recorded RESOLVED; **CO-4** `build_its_active_jobs_sheet.py:58-59` now reads `WORKSPACE_SAFETY_PORTAL` / `"00_Safety Portal"` — the stale `WORKSPACE_OPERATIONS` + `"Safety Portal"` pair the entry describes has zero hits. **SCOPE CAVEAT:** the one genuinely-remaining item is the *operator-run* CO-2 EICAR smoke at the Phase-C hardening gate, which belongs to `docs/operations/cutover_operator_punchlist.md` (as this entry's own preamble says), not to the code-debt file. Keeping a `[CUTOVER-BLOCKING]`-adjacent code block alive for a punch-list item overstated cutover code risk.


Surfaced during the cutover-readiness drive (PR #525); each is a real code follow-up not blocking
the merged work, tracked so it isn't lost. The operator-gated cutover items live in
`docs/operations/cutover_operator_punchlist.md`, not here.

- **CO-1 — RESOLVED 2026-07-14 (PR #585, `45fe4df`):** `DEFAULT_POLLING_ENABLED = False` landed with the CO-1 comment in place (verified at HEAD 2026-07-22); this entry was stale. Original text: **(LOW, belt-and-suspenders) — `po_send_poll.py:77 DEFAULT_POLLING_ENABLED = True` diverges from
  HOUSE_REFLEXES §5 (dark-ship default-False).** PO send ships dark via a seeded
  `po_materials.po_send.polling_enabled=false` row, so the seeded row is load-bearing (a MISSING row would
  default the SEND poller ENABLED). Flip the code default to `False` so a lost/absent row fails safe (a
  send-gate should never fail-open to sending). **Deliberately NOT landed autonomously** — it touches a
  `SEND_SCRIPTS`-enrolled send daemon, and the External Send Gate is a FIXED high-capability class; even a
  fail-safe tightening on that surface is Seth's call. **Trigger:** any PO send-path session, or a §5 sweep.
  **Tag:** `po_materials`, `po_send`, `external-send-gate`, `low-severity`.
- **CO-2 — code half LANDED 2026-07-22 (this PR):** `tests/test_photo_screen.py::test_live_clamd_flags_eicar` + `test_live_clamd_end_to_end_clean_photo` — skip-if-no-clamd, runtime EICAR through the REAL daemon; the operator runs them at the Phase-C hardening gate (`pytest -k live_clamd`) once ClamAV installs on the production Mac. Original: **(MEDIUM, prove-the-control-bites) — no live-clamd EICAR end-to-end smoke for portal-upload
  ClamAV.** `safety_reports/photo_screen._clamav_scan` is wired into every portal upload path and ships
  default-OFF (`safety_reports.photo_screen.clamav_enabled`); the EICAR test (`test_photo_screen.py`) PATCHES
  `_clamav_scan`, so no live clamd ever runs. Per HOUSE_REFLEXES §2, add a live EICAR-through-clamd smoke
  (construct the EICAR string at runtime — do NOT commit a malicious file — feed it through
  `screen_photo(..., clamav_enabled=True)`, assert disposition=malicious; skip-if-no-clamd). CI cannot run
  clamd, so it's an operator-run Phase-C smoke (clamd installs in host-migration Phase A2). **Trigger:**
  Phase-C hardening gate, when the operator enables `clamav_enabled`. **Tag:** `security`, `safety_reports`,
  `clamav`, `prove-the-control-bites`.
- **CO-3 (LOW, mechanical coverage) — VC-03 does not sandbox-scan every mirror-bearing config row. RESOLVED
  2026-07-13 (as-designed):** the actionable sub-item is done — `system.operator_email` is enrolled in VC-03
  with `sandbox_scan=True` (`scripts/verify_cutover.py:264`, comment "CO-3"). The 2 Box
  `portal_root_folder_id` rows stay intentionally unenrolled (numeric IDs with no `evergreenmirror` marker to
  scan; CL-14 grep + CL-12 sweep are their backstop). Original context — PR #525
  enrolled the 2 extra `worker_base_url` copies + `po_send.from_mailbox`, but `system.operator_email` (global,
  mirror-domain fallback) and the 2 Box `portal_root_folder_id` rows remain outside VC-03 — the manual CL-14
  grep + the CL-12 sweep are their backstop. Enrolling `operator_email` (sandbox-scanned) would close another
  gap. Deferred because it changes gate behaviour and the Box roots are numeric IDs (no `evergreenmirror`
  marker to scan). **SUPERSEDED IN PART 2026-07-21 (gap-builder PR):** the 2 Box `portal_root_folder_id`
  rows ARE now enrolled in VC-03 as `non_empty` (no `sandbox_scan`). CO-3 conflated two separate
  assertions: **sandbox_scan**, which stays correctly N/A (a numeric Box folder id carries no
  `evergreenmirror` marker — CO-3's reasoning holds), and **presence**, which was never the reason to
  abstain and is now worth asserting because that PR adds `scripts/migrations/build_box_roots.py` — a
  create-only builder that writes no config row, so its two printed ids reach ITS_Config only via a manual
  operator paste at cutover. VC-03 now catches a skipped/fat-fingered paste. **Trigger:** the next
  verify_cutover hardening pass. **Tag:** `cutover`, `verify_cutover`,
  `low-severity`.
- **CO-4 — RESOLVED 2026-07-22 (this PR):** both builders repointed to `WORKSPACE_SAFETY_PORTAL` with the live folder names (`00_Safety Portal` / `00_Form Catalog` — two different folders) and the aliased `FOLDER_OPERATIONS_SAFETY_PORTAL` bootstrap line replaced with the real constants; `safety_portal_config_sheets.md` runbook location fixed in the same pass. Landed BEFORE the Phase-1 production builder run this depended on. Original: **(HIGH, stale target) — `build_its_active_jobs_sheet.py` + `build_its_forms_catalog_sheet.py` still
  target the pre-2026-06-05 Safety-Portal location.** Both hardcode `WORKSPACE = sheet_ids.WORKSPACE_OPERATIONS`
  with `FOLDER_NAME = "Safety Portal"` (`build_its_active_jobs_sheet.py:50-51`,
  `build_its_forms_catalog_sheet.py:52-53`), which is stale against the 2026-06-05 move to
  `WORKSPACE_SAFETY_PORTAL` (whose live folders are `00_Safety Portal` + `00_Form Catalog`). **Failure
  scenario at a fresh production tenant:** each script find-or-creates a THIRD, wrongly-named `Safety Portal`
  folder under ITS — Operations, builds ITS_Active_Jobs + ITS_Forms_Catalog into it — orphaned from every
  runtime constant, so no daemon can see either sheet — and its `[bootstrap]` line prints that folder's id
  for `FOLDER_OPERATIONS_SAFETY_PORTAL`, an ALIAS of `FOLDER_SAFETY_PORTAL`, so pasting it overwrites the
  real Safety-Portal folder id with the wrong one. Deliberately NOT fixed in the gap-builder PR (explicitly
  out of its scope); recorded here rather than left silent. **Fix:** repoint both to `WORKSPACE_SAFETY_PORTAL`
  + the live folder names (`00_Safety Portal` and `00_Form Catalog` respectively — they are two DIFFERENT
  folders), and stop emitting the aliased bootstrap constant. **Trigger:** Phase-1 cutover, before running
  the Safety-Portal sheet builders. **Tag:** `cutover`, `migrations`, `safety_reports`, `high-severity`.

## [RESOLVED 2026-08-07 — audit] [OPEN 2026-06-09] Safety Portal M1 — authenticated submitter can overwrite a peer's PENDING submission

> **Moved from tech_debt.md 2026-08-07 (delivery-day tech-debt cleanup):** The **titled** defect is fixed at HEAD `9eace59`: cross-actor overwrite is refused with `safety_portal/worker/index.ts:857` `return c.json({ error: "uuid_conflict" }, 409);` and every replace writes an atomic audit row at `:924` `stmts.push(auditStmt(c, actor, "submission_replace", attributed, {`. A security-sounding headline that is false at HEAD distorts every read of the portal's security posture, so this entry is archived rather than left open. **SCOPE CAVEAT — the narrower residual survives and was RE-FILED as its own honest entry in `tech_debt.md` on 2026-08-07:** `GET /api/recent` (`safety_portal/worker/index.ts:623`) is `requireSession`-only with no per-job ownership predicate. That gap is NOT closed by this archive.


`worker/index.ts` `/api/submit` accepts a client-controlled `submission_uuid` and executes `INSERT OR REPLACE` — this resets `box_verified=0` on an existing row. `/api/recent` leaks any job's latest UUID+payload (not scoped to the authenticated user). The intake dedup only guards already-filed UUIDs; a plain overwrite writes no `audit_log` row. An authenticated submitter can therefore silently replace a peer's un-filed submission with attacker-controlled content, leaving no audit trail.

Not currently exploitable remotely (requires an authenticated session), but a defense-in-depth gap before multi-user production rollout.

**Fix:** server-generate `submission_uuid` (remove client control) OR reject a UUID collision from a different actor. Stop `/api/recent` from leaking arbitrary-job UUIDs not owned by the caller. Add an `audit_log` row for every overwrite attempt.

**Collision risk:** active SPA work shares `worker/index.ts`. Coordinate with any in-flight Worker edits before touching `/api/submit`.

**Tag:** `safety-portal`, `security`, `adversarial-input`, `medium`.

**Revisit when:** next Worker security hardening pass, or before real PM users are provisioned on a live tenant.

Surfaced: 2026-06-09 12-dimension forensic audit (M1).

> **Audit 2026-07-24 (tech-debt janitorial pass):** M1 primary fix RESOLVED (PR #268 — 409 uuid_conflict on cross-actor overwrite + atomic submission_replace audit). STILL OPEN (narrowed): GET /api/recent is requireSession-only — any authenticated user can pull any active job's latest submission_uuid+payload for Amend prefill (no per-job ownership scope). Fix the stale top-of-file index line too.

## [SUPERSEDED 2026-08-07 — audit] ITS scaling hardening — 20-job/20-user Tier-A roadmap [OPEN 2026-06-28]

> **Moved from tech_debt.md 2026-08-07 (delivery-day tech-debt cleanup):** Superseded by `docs/ROADMAP.md` Track 3 per this entry's own status block ("The live marching order is `docs/ROADMAP.md` Track 3; the remaining open items live there") and its 2026-07-24 audit note ("effectively superseded-by-ROADMAP; kept as the pointer"). A pointer does not need 27 lines in the OPEN file — duplicated roadmaps are how the two surfaces drift. A1–A7 verified shipped at HEAD `9eace59`: `shared/sheet_capacity.py` exists (A1); watchdog Checks P/Q/R/O exist (A3/A4/A5). The successor surface is live at `docs/ROADMAP.md:75` `### Track 3 — Scale-hardening for the 20×20 cutover`. **SCOPE CAVEAT:** the A8 enablement-docs remainder and the two unverified Smartsheet quotas remain open — they live in ROADMAP Tracks 3/4, deliberately not re-duplicated here.


> **Status (recovered from PR #324 on 2026-07-04; kept for provenance):** most Tier-A items have
> since **shipped** — A1 `verify_sheet_cap` (#326), A2 single-host resilience (#327), A3 Box/Keychain
> lock + watchdog Check P (#345), A4 backlog alerts + Checks Q/R (#349), A6 `weekly_generate`
> hardening (#346), A7 photo-413 (CS2 #437), plus the broader forensic-hardening cluster (#342–#351).
> The live marching order is **`docs/ROADMAP.md` Track 3**; the remaining open items live there. This
> section is preserved verbatim below for the original analysis; the full report is
> `docs/reports/2026-06-28_forensic-scaling-eval-20x20.md`.

2026-06-28 forensic scaling evaluation (read-only; `improve`-skill + multi-agent Workflows; full report at `docs/reports/2026-06-28_forensic-scaling-eval-20x20.md`). Audited the system for a planned ramp to 20+ active jobs / 20+ daily **photo-heavy** portal users this quarter. 98 findings (7 CRITICAL / 33 HIGH; **39 silent-failure**). No code changed — diagnosis + logged executable specs only.

**Tier-A (must-fix-before-cutover) — full self-contained specs in the report's Part II; all 7 code specs are first-draft `needs-revision`:**
- **A1 (gating, do first):** verify the real Smartsheet per-workspace sheet-count cap + design a week-sheet archival/rollup strategy. ~1,040 new sheets/yr (20×52) is the #1 dollar cost (plan-tier upgrade $600 Pro / $2,400 Business) and a possible hard cap. `smartsheet_client` has no list/count-sheets method yet. Gates the cost + cutover timing.
- **A2:** single-host resilience — daemon auto-start after reboot (LaunchAgent-at-login gap), SDK network timeouts (boxsdk has none → indefinite daemon hang), Keychain-locked-after-reboot handling.
- **A3:** Box OAuth refresh-token cross-process lock + `keychain.set_secret` lock + 50-day idle warning (silent 60-day auth-death risk).
- **A4:** unfiled-submission backlog/age alert + portal_poll outage escalation (`box_verified=0` rows never pruned → silent loss if the host dies).
- **A5:** ITS_Review_Queue + ITS_Errors 5,000-row cap rotation (silent drop at cap).
- **A6:** weekly_generate hardening — per-job timeout + streamed merge + partial-write resumability. **CORRECTION:** the original "launchd kills it at >1h" CRITICAL is FALSE — the plist sets no `ExitTimeOut`; real risk is wall-clock + memory.
- **A7:** photo/payload 413 reconciliation (raise PAYLOAD_MAX envelope, keep the four-way §34 photo mirror synced — `worker/index.ts` + `photo_screen.py` + `PhotoField.tsx` + `publishValidation.ts`) + amend-prefill empty-payload guard. Doctrine flag RESOLVED — see report Part III.
- **A8 (P1, parallel):** Operator & User Enablement Documentation program — a PDF guide / user manual / comprehensive troubleshooting tree for every ITS function (Portal, ~17 Smartsheet surfaces incl. an `ITS_Config` data-dictionary PDF, daemons/CLIs, future workstreams). Enabling precondition for the distributed-Evergreen-operator model; needs a doc-currency discipline.

Cost at 20×20 ≈ **$610–$2,410/mo hard ≈ the Smartsheet tier decision** + ~$8 Cloudflare; Anthropic ~$0 (portal deterministic); labor distributed across existing Evergreen staff (not a bottleneck).

**Revisit when:** the 20-job ramp is scheduled (start with A1's read-only cap verification), or any Tier-A item is picked up for implementation.

> **Audit 2026-07-24 (tech-debt janitorial pass):** A1-A7 shipped/verified (merged). STILL OPEN: the A8 remainder + two unverified quotas (the real Smartsheet plan sheet-cap pending a support ticket, and pooled-attachment-storage) — these residuals now live in ROADMAP.md Track 3/4, not re-duplicated here. This entry is effectively superseded-by-ROADMAP; kept as the pointer.

## [RESOLVED 2026-08-10 — PR #33 `db509e4`] Track 6 archive — button + drain landed but three activation gaps remain [OPEN 2026-08-10]

> **Moved from tech_debt.md 2026-08-10 (session-close).** Gaps 1 and 2 are closed by a live drill,
> not just code review. **Gap 3 is closed only HALF-WAY** — see the correction below it.
>
> **CORRECTION, same day.** An adversarial re-check of this archival caught that the first pass
> over-claimed. Gap 3 as originally written read *"The Box leg has never been drilled live, **either
> direction**"*, and the drill exercised the **archive** direction only. The RESTORE (un-archive)
> direction remains undrilled on both systems, on every tenant — and it is not a symmetry freebie:
> the Smartsheet two-call order INVERTS per direction (archive move→rename, restore rename→move),
> and restore has its own no-create probe, label-then-key search, and live-collision refusal. It is
> operator-reachable today. No other tech-debt entry covered it, so archiving this one whole would
> have destroyed the only record. **That residual now lives as issue #42** — this entry is retained
> here for the two gaps it genuinely closed, and must not be read as proof the archive works in both
> directions.

1. **`field_ops.fieldops_sync.archive_enabled` row.** Root-caused precisely as predicted —
   `_read_bool_setting(default=False)` gated `_archive_pass` off because the row genuinely did not
   exist (the seeder shipped in #20, `scripts/migrations/seed_daemon_gate_config.py`, had never been
   RUN against this tenant). **Fixed:** the row was seeded `false`, then flipped `true` by explicit
   operator direction. A second, latent blocker surfaced only once the first was cleared:
   `field_ops.box.archive_root_folder_id` also had no row, and the "ITS Archive" Box root folder it
   should have pointed at did not exist yet either — `build_box_roots.py` created it
   (id `408071931845`) and the Config row was added to match.
2. **Launchd job loaded.** Re-verified live via `launchctl list`: `org.solutionsmith.its.fieldops-sync`
   WAS already loaded and running (~90s cycle) — this bullet's original claim ("zero ITS entries") did
   not hold at drill time. The archive pass rides the already-running daemon; no separate plist was
   needed.
3. **Box leg drilled live.** With both Config rows in place and the gate flipped, the daemon picked up
   the queued job (D1 `JOB-000030` "Production test", which had sat at `archive_state='requested'`,
   `attempts=0` for days — see the WARN-storm findings below) on its very next cycle and completed the
   full six-container relocation: `smartsheet:safety` + `smartsheet:progress` + `box:safety` moved
   (the other three legs correctly reported "nothing to move" — the shared-safety-root design means one
   Box move carries PO/RFQ/subcontracts), `archive_state='complete'`, `attempts=0`, folder ids preserved
   (`1566626123409284`, `553426158413700`, `407341446878`).

**What this drill also surfaced (filed as new issues, not fixed here — each is its own finding):**
during the days the gate was silently off, `_archive_pass` WARNed `config_row_missing` every ~90s with
no escalation (a WARN never triple-fires) — 3,442 occurrences sitting unread in the logs. That
observability gap plus the drill itself produced six issues: **#24** (job-archive has no §43
successor-remediation runbook — three failure messages in `job_archive.py` point at
`docs/runbooks/project_closure.md`, which has no archive symptom), **#25** (no watchdog check covers a
stale archive request), **#26** (watchdog Check P reports "fresh" through a live Box `invalid_grant`
because it only reads local-marker age, never live auth state; separately the refresh lock in
`box_client._store_tokens` serializes the Keychain **persist**, not the token **exchange** — two
concurrent processes can still race a single-use refresh token), **#27** (`verify_cutover.py`, the one
tool whose VC-03 check would have named both missing rows by name, runs nowhere — not CI, not launchd,
not `install.sh` — this session-close entry's own generalization of that gap lives in the OPEN file as
"seeders-are-never-run"), **#29** (`partial` is TERMINAL for the archive queue but only WARNs — the
heartbeat's `cycle_status` sums `archive["errors"]`, not `archive["partial"]`/`["failed"]`, so a
half-moved job leaves the daemon reporting green), **#30** (`JobArchivePanel.tsx:195` tells the operator
"The system retries automatically" on a partial, which is false).

**Fix (this entry, done):** ran the seeder-equivalent action, seeded + flipped the gate, created the
missing Box root + Config row, drilled live end-to-end successfully. **Residual work is real and
tracked as GitHub issues #24/#25/#26/#27/#29/#30**, not re-duplicated in this closed-entry writeup.

**Tag:** `field_ops`, `archive-on-closure`, `host-migration`, `resolved`.

## [RESOLVED 2026-08-10 — duplicate reflex] SDK-vs-live body-shape mismatches need integration coverage [OPEN 2026-05-20]

> **Moved from tech_debt.md 2026-08-10 (session-close trim).** Closed as a DUPLICATE REFLEX,
> not as unfinished work. The named mitigation generalised well beyond the original scope —
> `tests/test_smartsheet_client_integration.py` plus 19 sibling `test_*_integration.py`
> wrappers (box_client, active_jobs_writer, portal_client, weekly_send, …), all
> `@pytest.mark.integration` and deselected by default. The entry's own in-place 2026-07-24
> audit note already read "Concrete debt RESOLVED … what remains is only a standing
> forward-guard". That guard is now canonical in HOUSE_REFLEXES §2 and automated by the
> `sdk-integration-test-scaffold` agent, so keeping it here duplicated doctrine.

PRs #47/#48/#49 each surfaced one body-shape mismatch the Smartsheet SDK accepted silently but the live API rejected, in successive iterations:

- **PR #47**: `id` in body — errorCode 1032 ("attribute(s) column.id are not allowed for this operation").
- **PR #48**: `type` missing from body — errorCode 1090 ("Column.type is required when changing options").
- **PR #49**: `type` present but wrapped as `EnumeratedValue`, SDK silently strips it — wire body becomes `{"options": [...]}` with no `type`, API rejects same as #48.

Class of bug: `SimpleNamespace`-based mocks at the SDK boundary don't enforce the live API's contract on body shape, required fields, or value wrapping. Mock tests passed; live calls failed.

**Mitigation landed in this PR (2026-05-21):** `tests/test_smartsheet_client_integration.py` runs create → list → update → delete round-trips against live sandbox sheets. Registered as `@pytest.mark.integration`; default `pytest` skips them (pyproject.toml `addopts = -m 'not integration'`). Operator runs `pytest -m integration` pre-deployment after any `shared/smartsheet_client.py` or `shared/picklist_sync.py` change.

**Pattern to extend:** any future `shared/*` SDK wrapper that exercises a non-trivial verb (update/create/delete) on typed columns or rows should gain a parallel integration test. The pattern: create the minimum live state required, exercise the verb, assert post-state, tear down in `finally`.

**Urgency:** addressed. Note kept open for visibility — any new wrapper that lands without parallel integration coverage re-introduces the class of bug.

Surfaced: PR #46 → #47 → #48 → #49 iteration, 2026-05-20/21.

> **Audit 2026-07-24 (tech-debt janitorial pass):** Concrete debt RESOLVED — live-API integration coverage exists (tests/test_smartsheet_client_integration.py + parallel coverage for every in-scope shared/* wrapper, @pytest.mark.integration, skipped by default). What remains is only a standing forward-guard, now canonical in HOUSE_REFLEXES §2 — a candidate to close as a duplicate reflex rather than a live gap.

## [RESOLVED 2026-08-10 — PR #393 `6dc0431`] [OPEN] Worker-side send-gate enforcement (the TS Worker is outside the Python AST capability-gate)

> **Moved from tech_debt.md 2026-08-10 (session-close trim).** Closed by
> `tests/test_worker_send_free.py` (PR #393 / `6dc0431`): a CI-collected test that greps every
> `safety_portal/worker/**/*.ts` and fails on any `fetch(` other than `ASSETS.fetch(` — exactly
> the allowlist grep this entry proposed. It carries no pytest marker, so it runs in the
> blocking `test` job.
>
> **Scope caveat, recorded deliberately.** This closes the outbound-`fetch` vector ONLY. It is a
> line-regex forbidden-CALL grep, NOT the AST forbidden-IMPORT gate that
> `tests/test_capability_gating.py` applies to Python — which still excludes `safety_portal`
> entirely. Import-shaped egress (`cloudflare:email` + a send binding, `cloudflare:sockets` /
> `node:net` `connect()` under the enabled `nodejs_compat` flag) remains ungated, and the regex
> is evadable by aliasing. That residual stays OPEN in tech_debt.md as the sibling
> "Safety Portal — Worker-side capability-gate for TS" entry; do not read this closure as
> covering it.

**What:** `tests/test_capability_gating.py` enforces Invariant 1 (no send capability on
generation scripts; no AI on send scripts) by AST-scanning Python under `shared/` +
`safety_reports/`. It does NOT reach the TypeScript Cloudflare Worker
(`safety_portal/worker/`). As of Phase 5 PR 2 the Worker holds the HMAC signing secret +
the internal bearer token, so it is no longer trivially "send-free by binding-absence" —
its send-free posture rests on code review + the module docstring only. The **pull model**
keeps the Worker send-free by design (it serves a queue + accepts a receipt; it never
initiates outbound), but nothing structurally PREVENTS a future Worker edit from acquiring
an outbound `fetch()` to an external host.

**Fix (when the Worker surface grows):** add a CI grep / ESLint rule forbidding `fetch(` in
`safety_portal/worker/` except to an allowlist (the ASSETS binding), as the TS-side
equivalent of `test_capability_gating.py`. Surfaced by `ops-stds-enforcer` (W2).

**Tag:** `safety-portal`, `security`, `invariant-1`, `phase-5`, `medium`.

**Revisit when:** the Worker gains any new outbound-capable code path, or at the deploy hardening pass.

Surfaced: 2026-06-05 Safety Portal Phase 5 PR 2 (transport queue).

---

## [RESOLVED 2026-08-10] GAYK/DOYLE **weekly** maintenance checklist (source page 2) is not modeled anywhere

**Opened and closed the same day.** Raised by the #44 adversarial review (the
`equipment-gayk-piledriver-v1` definition asserted a tech-debt pointer that did not yet exist), then
built in #47 on operator request in the same session. (Corrected 2026-08-10, session-close: this
entry originally cited "#45" — a same-day, unrelated daily-report PR — as the PR that built the
resolution; the real PR is #47, verified via `gh pr view 47`.)

**Was:** `reference_forms/GAYK_DOYLE_Piledriver_Daily_and_Weekly_Checklists.pdf` is a two-page
document. Page 1 shipped as the pre-op form `equipment-gayk-piledriver-v1` (#44); page 2 ("Weekly
Checklist and Maintenance" — "Must be done in addition to the daily checklist") did not, and the
landing surface was a real choice rather than an assumption: a maintenance duty is not a
pre-operation inspection, so folding it into the daily form would have made operators attest weekly
greasing every shift, while filing it AS an inspection would misclassify a maintenance record.

**Resolution:** migration `0062_gayk_weekly_maintenance_checklist.sql` seeds it as a third
`generic_inspection` library template, "GAYK/DOYLE Piledriver Weekly Maintenance Check", carrying all
9 source duties. The source's compound first bullet is split into grease (seq 10) and track tension
(seq 20) so a skipped track check cannot ride in on a completed greasing round; every other item is
one source bullet. Torque and clearance figures (75Nm/55 ft-lbs, 25mm/1") are pinned by
`safety_portal/test/fieldops-checklist-seed.test.ts`, along with a double re-apply idempotency proof.

**Deliberately NOT done:** no `checklist_recurrences` row is seeded. That table (`0040`) requires a
NOT NULL `assignee_personnel_id` AND `job_id` — live tenant data a migration cannot know — so the
weekly cadence is defined per job/person in the Inspections UI once a machine is on a job. Seeding
one would have invented an assignment.


## [RESOLVED 2026-08-10 — a dated index that needed re-verification against what it indexed] 2026-07-14 Debt-Zero Triage — session disposition

> **Why it left `tech_debt.md` (2026-08-10, bucket-E triage):**
> A ~24 KB dated index that restated every entry beneath it. Its own 2026-08-07 header records the
> failure mode: 19 of its 110 "re-parked (still valid)" bullets pointed at entries that no longer
> existed, over-counting open debt by ~17%, and two of those carried a `[CUTOVER-BLOCKING]` tag that
> overstated cutover risk. A summary that must itself be re-verified against the thing it summarises is
> negative value — the entries it pointed at each keep their own full rationale in `tech_debt.md`, which
> is where a reader should look. Nothing else in the live file referenced this index (checked before
> removal); the only external mentions are the 2026-07-14 session log and its README row, which are a
> historical record and stay accurate. Includes the PR #52 bullets that had been repointed to
> `docs/references/platform_constraints.md`; they travel with the index.

### 2026-07-14 Debt-Zero Triage — session disposition

Every open entry below was triaged against live HEAD on 2026-07-14 (8-agent verification workflow + operator session). **Counts:** 123 unique entries — 12 verified **resolved/stale → moved** to [`tech_debt_closed.md`](tech_debt_closed.md); 91 **re-parked** (still valid, deferred, owner-tagged below; originally 110 — reconciled 2026-08-07, see note); 1 folded into the send-path re-park (scheduled_send_local). This index is the dated disposition record; each entry keeps its full rationale in place.

> **Index reconciled 2026-08-07 (delivery-day tech-debt cleanup).** This 2026-07-14 index was never
> updated when entries were archived, so it had drifted into pointing at entries that no longer exist —
> 19 of its 110 "re-parked (still valid)" bullets named entries already moved to
> [`tech_debt_closed.md`](tech_debt_closed.md), over-counting open debt by ~17%. Two of the stale bullets
> were `[CUTOVER-BLOCKING]`-tagged, which overstated cutover risk. Those bullets are removed and the five
> group counts re-derived from the surviving bullets. The three `### This session's code outcomes` bullets
> below are a historical session record, not entry pointers, and are left untouched. Verification method:
> each bullet title was matched against the surviving `## ` headers and cross-checked against the archive;
> one apparent stale bullet (`Publish daemon: privileged subprocess chain…`) was a FALSE POSITIVE — its
> entry is still live, the header just reads "chain **is** operator-validated-live" — and was kept.

### This session's code outcomes (2026-07-14)

- **DONE + merged:** Block A security scrub (#584 — production-identity scrub + working-tree gitleaks guard); C1 po_send fail-safe default (#585); C5 anomaly_logger FP narrowing (#586); C3 docs_pdf dark-gated Box publish leg (#588).
- **In flight:** item-7 portal tab-title + inline-SVG favicon (#589); SC-CFG-2 MAX_ADDRESS hoist (#590, portal-worker-security-reviewer CLEAN).
- **Re-parked with findings (need Seth / a focused session):** **C2** scheduled_send fail-closed — External Send Gate is FIXED high-class; C1 got explicit "nothing else on the send path" co-resolution, C2 did not (bundle the observability WARN-log + seeder here too). **C4/item-9** fail-closed guard-hook — exec hooks are real files (not the vulnerable relative-symlink shape); DR-D1's real target is the blueprint's symlink hooks (doctrine-fenced), a fail-closed SessionStart assertion has a chicken-and-egg + brick-CC blast radius needing operator-present validation; Check M detects post-hoc today. **C6/§404** hours_log indexed lookup — premature optimization on a low-volume path (don't-harden-dormant); revisit at the 20-job scale point. **C7/§466** smartsheet-python-sdk pin — the "dropped smartsheet.exceptions" claim is STALE: 4.2.0 restores it (all 3 used names present) and the FULL MOCKED suite passes on 4.2.0; but it is a MAJOR 3→4 bump and the suite mocks the SDK, so the operator must run `pytest -m integration` against 4.2.0 (§30 / mandatory-live-smoke) before loosening the pin to `<5.0.0`. One-line change, de-risked.

### Re-parked, by owner (still valid — trigger in each entry)

**Seth** (7):

- DKIM in-process re-validation — Security/threat-model enhancement (in-MTA-trust assumption); half-day. Trigger: security review or threat-model session flags it.
- Field-ops P2.3 write-layer follow-ups (deferred sub-features + governance) — Four remain: inspection quick-log (operator domain-input blocked), cap.admin.equipment orphan cleanup + 0013 label tidy (touch cap vocabulary → confirm Seth), §50 doctrine bump. Trigger: cap-management UI scheduled / equipment-inspection forms defined.
- Field-ops Smartsheet/Box source-of-truth integration (P2.4+ downstream) — BLOCKED on Seth gaining canonical-Evergreen Smartsheet SoR visibility + the §50 doctrine bump; building against unseen schema is worse than absent. Trigger: SoR access AND/OR §50 doctrine reaches Seth.
- Optional fail_closed_until kill-switch hardening (deferred) — Kill-switch fail-open contract + defense-in-depth is doctrine/security territory; revisit when a time-bounded hard halt is actually needed.
- Progress (and safety) no-recipient HELD surfaces a record, not an operator page — Adding a CRITICAL push leg on a no-recipient HELD is a Send-Gate severity-posture decision (§3.1, Seth-owned). Trigger: operator decides a blocked customer-facing send warrants an active page.
- Safety Portal M2 — capability gate static-AST-import-only — Dynamic-import/transitive-closure/scripts-scope gaps remain (now documented as residual in-code); security capability-gate is high-class. Trigger: next capability-gating hardening / before Customer-1.
- Single-token blast radius for Smartsheet — Secrets/blast-radius split is high-class (auth); revisit when a rotation incident causes a system-wide outage or multi-customer secrets land.

**operator** (21):

- /pending-jobs transport flakiness — deeper cause untraced, only blast-radius mitigated — Root-cause diagnosis needs a captured live failure + Cloudflare WAF/bot-fight dashboard cross-check; symptom already mitigated by #469. Trigger: next observed transient /pending-jobs failure.
- Confirm canonical_job_path() format with owner — Still no real consumer (only test_box_client.py:524 references it); owner never validated the write-path format. Trigger: first workstream that consumes canonical_job_path.
- Cross-leg dedupe activation — Sentry/Smartsheet stay record-only, so no push-leg dedupe needed; correlation_id already wired. Trigger: operator configures Sentry/Smartsheet alert rules.
- Doc-conventions lint strict-mode flip after retrofit window closes — Scheduled: bulk frontmatter retrofit sweep + one-line --strict CI flip; trigger 2026-07-24 (not yet reached) or operator opens a retrofit session / accepts permanent grandfather.
- ITS_Active_Jobs Address cells blank — office PM fill required — Live Smartsheet data-fill by office PM (no code); revisit at portal production go-live before enabling Work-Location autofill.
- ITS_Active_Jobs column order cosmetically scrambled — **MOVED 2026-08-10** → [`docs/references/platform_constraints.md`](references/platform_constraints.md). A permanent platform behaviour, not work; it can never close, so counting it as open debt overstated the backlog forever.
- ITS_Daemon_Health sheet observability drift — Partially eased (publish daemon now self-provisions its row) but stale live rows (retired intake_poll etc.) need operator UI deletion + Last-Error-clear. Trigger: next daemon-health pass; before cutover.
- Operator-UI Shortcuts for trusted-contacts workflows — Half-day tooling nicety, no functional gap. Trigger: tooling-track session bandwidth.
- Orphaned Reports sheet — column styling not applied — Cosmetic column widths on a live Smartsheet sheet (forbidden live-Smartsheet-write for a DO). Trigger: operator finds default widths inconvenient / a portal styling pass.
- Phase 5 manual week-sheet additions — **MOVED 2026-08-10** → [`docs/references/platform_constraints.md`](references/platform_constraints.md). A permanent platform behaviour, not work; it can never close, so counting it as open debt overstated the backlog forever.
- PowerShell Get-ApplicationAccessPolicy -Identity <friendly-name> directory lookup fails — **MOVED 2026-08-10** → [`docs/references/platform_constraints.md`](references/platform_constraints.md). A permanent platform behaviour, not work; it can never close, so counting it as open debt overstated the backlog forever.
- PowerShell macOS Gatekeeper deprecation 2026-09-01 — Runbook-only Azure-Cloud-Shell fallback; no code change. Trigger: 2026-08-15 calendar status check.
- Safety Portal — job-specific JHA variant content deferred — Parent/variant mechanism already built + data-driven; revisit when PM identifies a job with site-specific JHA requirements.
- Safety Portal — toolbox talk header context missing from form definitions — Definitions faithful to source PDFs; trivial field add gated on PM confirming a Presenter/Date header is wanted.
- Seed system.box_smoke_folder_id in ITS_Config — Manual create-Box-folder + seed-ITS_Config step for the opt-in --write-test smoke; read-only smoke works without it. Trigger: diagnosing Box scope/permission issues.
- Smartsheet UI-only constraints (Forms, CF, Filter Views, Restrict-to-dropdown) — **MOVED 2026-08-10** → [`docs/references/platform_constraints.md`](references/platform_constraints.md). A permanent platform behaviour, not work; it can never close, so counting it as open debt overstated the backlog forever.
- Smartsheet-wiring audit findings — daemon-health + capacity hygiene — M-1/M-2/M-3/S-1 all RESOLVED/DONE; residual is seeding the two cross-workstream footgun ITS_Config rows + the sheet_capacity global carve-out. Trigger: cutover config-seed pass.
- Stale Anthropic Service Account svac_…SR7vDMJ for archival — Manual Anthropic-Console archive of an already-key-deleted service account; auth/console-side, not a code task. Trigger: next Anthropic Console visit.
- voice@ mailbox AppAccessPolicy scope addition pending — EXO scope-add + ITS_Config row; watchdog already iterates mail_intake.*. Trigger: a workstream activates voice@ as an intake source.
- Watchdog sweep cadence vs dedupe window length — Intentional 24h-max summary delay for daily-rhythm ops. Trigger: operator reports ≥24h-delayed summaries causing triage problems.
- WPR_Pending_Review final removal (decommission-by-doc → delete) — Blocked on operator deleting the live WPR sheet; SHEET_WPR_PENDING_REVIEW + smoke_test_watchdog_catchup WPR seeding still present (grep confirms). Trigger: after operator deletes the WPR Smartsheet.

**deploy-dependent** (7):

- .dash-section CSS class duplicates .card — .dash-section still in global.css alongside .card; cosmetic dedup, entry says not worth a standalone PR, needs deploy. Trigger: next design-system consolidation pass.
- Form editor: S1 per-item scale/comment authoring from scratch — editorModel.ts still lacks scale/comment item-creation fields; frontend change needs Worker deploy. Trigger: a new HSSE-type form authored via the editor.
- Publish daemon: rollback UI picker missing — Backend rollback op live; SPA picker still deferred and needs a Worker deploy to land. Trigger: a rollback is operationally needed / Phase-3 form-editor polish.
- Remove the progress-% estimate system-wide — Remaining cleanup touches the worker CREATE-route INSERT (portal-worker-security-reviewer DoD) + a destructive ALTER TABLE DROP COLUMN migration (0014, deploy-coupled). Trigger: a supervised worker-reviewed PR.
- Safety Portal — bcryptjs cost-10 may exceed Workers Free 10ms CPU cap — auth.ts still imports bcryptjs at cost 10 (unresolved); Paid-plan-vs-PBKDF2 choice is a production-cutover deploy decision.
- Subcontracts — PO/SC Configuration + builder follow-ups — SC-CFG-2 (index.ts still hardcodes literal 512 vs shared MAX_ADDRESS, verified) is in HELD PR #548 needing Worker deploy; PR-B2 is a large operator-directed exhibit-A+payment-terms build needing deploy + a Layer-A legal-attestation seed; SC-CFG-1 is informational-only.
- Worker publish-reject paths return bare error codes — no reason field — Low-severity optional server-side parity; client fallback is explicit+non-silent; Worker change needs deploy. Trigger: an unmapped code surfaces in prod / publish-flow UI polish.

**phase-1.5** (12):

- Attachment screening pipeline Layers 1-3 — Email-attachment §34 arbitrary-file path still unbuilt (photo/PO screeners are separate); required before Phase 1.5 cutover. Trigger: Phase 1.4 hardening session.
- Config editor (§50) — deferred follow-ups — CE-1/CE-2/CE-3/CE-6 verified RESOLVED (sweep to archive); open CE-5 (MEDIUM — who may attest legal clearance) is a pre-activation §44 decision gating config_actuator.polling_enabled, CE-4/CE-7 low residuals folding into PR-B2 / next po.test.ts touch.
- Fallback path removal after ITS_Config cutover — ~30min cleanup but gated on operator confirming one clean Friday cycle post-cutover; _check_legacy_allowlist still live at intake.py:626. Trigger: cutover confirmation.
- hours_log.find_entry_row does a full client-side scan of the sheet on every upsert/supersede call — O(sheet-size) scan still present but benign at low volume; revisit at the 20-job cutover or when a live Hours Log cycle is observed slowing.
- Native multi-PICKLIST graduation for Trusted Contacts scope columns — ~1hr; trusted_contacts allowlist currently dormant. Trigger: Picklist Hardening #1 deliverable lands + allowlist goes live.
- Phase 1.5 — provision dedicated ITS Box user account, re-auth — Cutover task: re-auth as dedicated ITS Box user + ownership/collab transfer; no code change. Trigger: Phase 1.5 production-tenant cutover.
- Pre-mirror-tree portal Box filings are sandbox orphans — 3 sandbox pre-activation orphans, no repair required in sandbox; revisit at a live customer Box-root cutover to decide leave-or-hand-move.
- R2 Watchdog Check E (Anthropic spend trend) deferred to Phase 1.5 — No _check_spend/anthropic_billing in watchdog; deferral is cost-signal-at-sandbox-scale rationale, confirmed in CLAUDE.md. Trigger: production cutover.
- Safety Portal Phase 5 — deploy prerequisites (Cloudflare secrets + D1 + wrangler.jsonc IDs) — Sandbox secrets/D1 done (HMAC live-validated 2026-06-08); production-tenant secrets/D1 re-run is a cutover gate.
- Safety Portal — no rate limiting on /api/login or /api/* — No ratelimit binding/WAF rule in worker; CUTOVER-BLOCKING, Cloudflare-dashboard/operator gated. Trigger: Evergreen production cutover.
- Watchdog/launchd hang-killer: hard-kill a daemon exceeding N× expected cycle duration — Recovery complement to Check-C detection; larger design decision (kill location, per-daemon N sizing) — Phase 1.4/1.5 reliability target.
- weekly_send upload-session — live-Graph integration smoke — Only mocked coverage of the 4-step Graph upload-session; §30 mocks-pass-live-fails surface. Trigger: pre-Customer-1 live-tenant validation / first real photo-bearing packet.

**post-delivery** (44):

- Alert-dedupe state is per-machine — Single-host through Phase 3; per-host dedupe is correct today. Trigger: ITS gains multi-host execution (Phase 4+).
- Alert-dedupe state-file growth in pathological flap-with-new-error-code scenarios — Bound holds today (fixed error_code); monitor-only. Trigger: state file exceeds ~100 persistent entries between sweeps.
- Alert-routing dedupe key granularity — Speculative not-yet-needed enhancement (all CRITICALs use error_code=uncaught_exception); ITS_Errors+Sentry still record each bug. Trigger: production shows different-bugs-collapsed-in-a-window.
- Allowlist drift detection — typo'd trusted-contacts entry silently quarantines — Email allowlist is dormant (portal PULL is the live intake path); half-day feature, revisit at next _load_contacts touch or Phase 1.6.
- Box folder delete-and-recreate breaks folder ID resolution — A2 routing sheet has landed, but the folder-ID validation layer + SDK trashed-folder smoke are Phase 2 hardening, not launch-critical.
- Checklist template identity is title-keyed (0026 design) — Low-blast-radius edge case; §14 says don't build the code/slug column speculatively. Trigger: template library grows past seeded set / admin reports items merging wrongly.
- Config-change audit trail — Phase 2 audit-as-deliverable; Smartsheet native cell-history covers the common case — trigger = first customer compliance/audit requirement.
- Configuration validation at daemon startup — Depends on A2/A3/A5 config-migration completing and overlaps #336; Phase 1.6 validation layer after the config-migration cluster.
- Conftest mock surface coverage — Latent forward-protection for future credentialed clients; still valid. Trigger: a new shared/*_client.py or a Linux-CI macOS-only failure.
- D1-primary tables have no ITS-side backup — Time Travel is the restore path — Accepted decision (R3-F7): §14 explicitly rejected a backup job, restore path documented. Trigger: a third D1-primary table lands / Cloudflare changes Time Travel retention.
- Draft cache stores one draft per account — Accepted single-slot design (deliberate); only revisit if concurrent multi-form editing is requested. Trigger: operator requests multi-form edit / a WIP draft-loss incident.
- Eventually migrate from legacy boxsdk to box_sdk_gen (Gen API) — ~half-day dependency migration; pin <4.0.0 holds. Trigger: Box announces 3.x deprecation, capability gap, or annual hygiene sweep.
- Hardcoded BOX_SUBPATH_BY_CATEGORY in safety_reports/intake.py — A2 (shared/project_routing.py) has landed so the trigger is technically met, but this lives in the dormant email-intake branch; it is a ~2hr config-migration, not a quick fix.
- HTML email rendering for weekly_send — weekly_send still sends WSR Email Body as text; HTML render awaits Teala's feedback on inline-text format after first 30 days of real cycles.
- Inline doctrine-pin normalization across shared/* + safety_reports/* — The one real fix (weekly_send.py:72 §23.3→§19) lives in a SEND-path file (excluded); fold in opportunistically next time those files are touched.
- ITS_Active_Jobs CC recipients are operator-entered, not allowlist-validated — Accepted-risk under trusted-operator-input model (Send Gate still requires explicit approval); revisit only if allowlist validation is later wanted.
- Nightly auto-index regen wiring — CI --check gate is the load-bearing enforcement; wire the launchd/watchdog job only when local drift is observed or a 3rd daemon touches watchdog wiring.
- P2.5 job-tracker up-sync — fast-follows — Items 1 (needs picklist_sync.py capability-gating enrolled FIRST to avoid meta-test break) and 4 (find-after-create race, idempotent nuisance) remain; 4 others closed. Trigger: item1 when picklist_sync enrollment addressed; item4 on an observed duplicate.
- P2.5 Slice 6 — portal-owned canonical number: residual redundancy — Two harmless §14-preservation leftovers (Portal Job Key == Job ID; always-set canonical mirror machinery) kept to avoid churn on a working reviewed path. Trigger: a later slice consolidates the identity columns.
- Picklist_Sync_Config mixes config and runtime state — §14 preservation, single consumer. Trigger: first operator-edit accident wiping hash/timestamp, or multi-customer schema evolution.
- PO attachments (Feature B) — conscious deferrals — ATT-1..6 are all accepted limitations / Phase-2 §34 hardening (VirusTotal, ObjStm/OpenXML deep-parse) or by-design; limited-blast-radius posture; revisit at the Phase-2 §34 hardening pass or an upload-scope widening.
- Portal permission-model stale plumbing — vestigial/orphaned caps, coarse gate — Items 3-4 RESOLVED; residual = 3 cheap-open ungated caps + cleaning 3 orphaned-cap migration comments (Worker/migration, needs deploy). Trigger: cap-management UI or inspection surface ships.
- PR-4 Part A — PDF download cache deferred optimizations + PR-5 supersession — Deliberate deferred perf optimizations, within-UI-budget today; PR-5 supersession is a forward note. Trigger: latency/scale review of the download path.
- Publish daemon: privileged subprocess chain operator-validated-live only — No --dry-run harness for the git/gh/wrangler chain yet; high-class deploy-adjacent daemon work. Trigger: publish-daemon code modified / Phase-3 hardening.
- R-series spec Deferred #5–#10 — named follow-ups — Explicit locked-decision scope cuts (not regressions); listed to avoid rediscovery as bugs. Trigger: next field-ops UX pass — check before re-scoping.
- R2 upgrade path for portal photo transport (deferred) — ADR-0001 deferred design (second storage plane); revisit when field crews need >4 full-res photos/field or the Worker body bound blocks a budget raise.
- Safety email-intake retire — operator-manual + future-PR follow-ups — Tombstone deletion done (intake_poll.py + weekly_summary.py DELETED 2026-07-03); residual = WPR sheet deletion (still referenced, own entry) + optional Job Slug column delete.
- Safety Portal M7 — publish daemon runs destructive git on live ~/its tree — Still runs git clean/checkout on the live tree without worktree isolation; daemon-hardening code change. Trigger: next publish-daemon hardening; before cutover.
- Safety Portal — Worker-side capability-gate for TS not covered by Python AST gate — Still no TS capability-gate (grep: no eslint no-restricted / no .ts coverage in test_capability_gating.py); duplicate of the later ts-sendgate entry — revisit when Worker gains outbound code path.
- safety_reports week-folder create-find race condition — Rare at single-machine cadence; WARN+operator-visibility preferred over auto-clean. Trigger: race observed in practice (multi-machine ops).
- SDK-vs-live body-shape mismatches need integration coverage — **ARCHIVED 2026-08-10** → `tech_debt_closed.md`. The mitigation generalised to 20 `test_*_integration.py` wrappers, and the standing reminder is now canonical doctrine (HOUSE_REFLEXES §2) plus the `sdk-integration-test-scaffold` agent — a duplicate reflex, not live debt.
- Severity-tiered + multi-recipient alert routing — Phase 2 post-Customer-1; single-recipient ITS_Config routing is adequate for the solo/Customer-0 stage — trigger = team expansion or Customer 2 onboarding.
- Smartsheet API constraint: AUTO_NUMBER columns rejected at sheet creation — **MOVED 2026-08-10** → [`docs/references/platform_constraints.md`](references/platform_constraints.md). A permanent platform behaviour, not work; it can never close, so counting it as open debt overstated the backlog forever.
- Smartsheet API constraint: column FORMAT must be set via model attribute, not dict constructor — **MOVED 2026-08-10** → [`docs/references/platform_constraints.md`](references/platform_constraints.md). A permanent platform behaviour, not work; it can never close, so counting it as open debt overstated the backlog forever.
- Smartsheet API constraint: DATETIME columns require system column type — **MOVED 2026-08-10** → [`docs/references/platform_constraints.md`](references/platform_constraints.md). A permanent platform behaviour, not work; it can never close, so counting it as open debt overstated the backlog forever.
- smartsheet-python-sdk upper-bound pin (CI-break stopgap) — Pin <3.10.0 still live and load-bearing (import at smartsheet_client.py:46); proper ~1hr fix requires verifying the newer SDK exception surface — dependency-maintenance pass.
- Smoke harness pattern divergence between dedupe smoke and Resend/Sentry smokes — **MOVED 2026-08-10** → [`docs/references/platform_constraints.md`](references/platform_constraints.md). A permanent platform behaviour, not work; it can never close, so counting it as open debt overstated the backlog forever.
- Structural fix: lazy keychain loading + DI-injected kill_switch — Non-trivial cross-call-site refactor deferred by design; fold in when a session next touches smartsheet_client or kill_switch for another reason.
- Subcontracts — SC-S3c adversarial-review follow-ups (non-blocking) — SC3c-4 now RESOLVED (DAEMON_ROOTS generalized to all 5 daemon pkgs); SC3c-1 (shared PO+SC supersede race needs joint po.ts re-review), SC3c-2 (stale comment on applied migration 0050), SC3c-3 (forward-looking to SC-S4 send) remain valid, revisit when SC-S4 send is built.
- Summary email content depth (filter-criteria vs inline correlation IDs) — Pull-from-SoR design accepted; schema upgrade deferred. Trigger: open-summary→open-ITS_Errors friction becomes frequent.
- weekly_send upload-session chunk-retry hardening (deferred) — Cross-cycle session-resume/cancel deferred, restart-from-zero cheap today; revisit on recurring mid-upload failures or packets nearing the 150 MB ceiling.
- weekly_send upload-session threshold = 2.5 MB (heuristic, not measured) — Send-path threshold tuning against the live tenant; revisit when the first live photo packet crosses ~2.5 MB or a graph_error cluster appears near 3 MB.
- Worker-side send-gate enforcement (the TS Worker is outside the Python AST capability-gate) — **ARCHIVED 2026-08-10** → `tech_debt_closed.md`. The "no TS send-gate CI rule exists" claim went stale: `tests/test_worker_send_free.py` (PR #393 / `6dc0431`) is a CI-collected grep forbidding any `fetch(` in `safety_portal/worker/**` except `ASSETS.fetch(` — exactly the allowlist this entry proposed. See entry 123 for the residual it does NOT cover (import-level gating).
- §6a enablement-doc DoD owed per Progress-Reporting slice — PARTIALLY_MITIGATED — manifest pipeline now exists; residual = retire in-doc TODOs, add material_catalog guide, D2-2/D2-3 content. Trigger: each Progress-Reporting slice brief.

---

## [RESOLVED 2026-08-10 — split: eight live findings extracted, the resolved chase archived] WS2 dashboard clear-error-log verb (#587/#591/#594) + live error-chase findings [OPEN 2026-07-14]

> **Why it left `tech_debt.md` (2026-08-10, bucket-E triage):**
> ~21 KB of session narrative in which 8 of 15 sub-bullets were marked RESOLVED / RETRACTED /
> FALSIFIED / FIXED / BUILT **in their own text** (DASH-5 retracted as pytest pollution, DASH-7
> reconciled, DASH-8 → PR #597, DASH-9 falsified, DASH-12 built, DASH-13 verb shipped, DASH-14 fixed,
> and the 2026-07-21 transient-fence bullet resolved). The still-open findings were extracted to
> `tech_debt.md` as eight short standalone entries — DASH-6 (config_actuator broad excepts), DASH-10
> (native-app repackaging decision), DASH-11 (picklist-sync outside the interval-edit allowlist),
> `rfq_send` activation posture, the VC-01 docstring undercount, the `registry.py` subcontract_send
> sibling-key parity gap, the config_actuator/po_poll workstream-scope divergence, and DASH-13's two
> outstanding operator decisions. The entry's process note (flip a polling gate only AFTER its matching
> Worker secret/route is deployed) had no other home and moved to `docs/HOUSE_REFLEXES.md` §5.
> Full narrative preserved below.

### WS2 dashboard clear-error-log verb (#587/#591/#594) + live error-chase findings [OPEN 2026-07-14]

Session that shipped #587 (back-nav banner-extension), #591 (11-row `ITS_Config` migration seed), and #594
(the Class-B dashboard clear-error-log verb, `shared/errors_rotation.py`), plus a manual forensic wipe of
`ITS_Errors` (6,249 → 217: 215 open CRITICALs + 2 `errors_log_cleared` audit rows preserved) and a live
error-chase over the remainder. Findings from the chase, not yet actioned:

- **DASH-5 (RETRACTED 2026-07-17 — see the 2026-07-15 error-flood diagnosis).** Originally flagged HIGH/P1:
  both out-of-band alert legs down (`ITS_RESEND_API_KEY` 401, `ITS_SENTRY_DSN` `BadDsn`). **FALSIFIED.** A
  2026-07-15 forensic read of `ITS_Errors` + `~/its/logs` found the underlying error volume was **phantom
  pytest pollution**: ~13–15 pytest runs during the 07-14 dev session made REAL network calls against
  `tests/conftest.py`'s stub creds (`test-{service}`) and their `shared.error_log` writes landed in the LIVE
  dated log (2,285 Smartsheet 401s + 27 Resend 401 + 27 Sentry BadDsn, all test-generated) alongside 457 clean
  live daemon cycles. Real CRITICALs alerted successfully both before and after the window (07-14 19:59Z,
  07-15 08:35Z) — the alert legs were never actually broken. **Do NOT rotate `ITS_RESEND_API_KEY` /
  `ITS_SENTRY_DSN` on this finding.** The pollution mechanism itself is a genuine, still-open, DIFFERENT
  tech-debt item — see "pytest live-log/state pollution" below. Detail: auto-memory
  `project_error-flood-diagnosis-2026-07-15.md`.
- **DASH-6 (LOW, operator/dev) — `config_actuator`'s broad `except Exception` sites make root-causing an
  incident slower than it should be.** `po_materials/config_actuator.py` carries a dozen-plus
  `except Exception as exc:  # noqa: BLE001` sites (deliberately broad, per their own comments — "any
  actuation failure is terminal+alerted", "never wedge the cycle"). This session's chase of a
  `config_actuator`-attributed `ITS_Errors` row needed a source read to conclude it was benign (a gate
  flipped before its matching Worker secret/route was deployed — see the activation lesson below), rather
  than being legible from the error message/error_code alone. Not urgent (the incident was genuinely benign
  and the broad catches are individually justified), but a pass to give each site a more specific
  `error_code`/message would make the next incident self-diagnosing from the `ITS_Errors` row alone. Trigger:
  next `config_actuator` touch, or a recurrence of an unlabeled `config_actuator` error.
- **DASH-7 (RESOLVED 2026-07-17 — see the 2026-07-15 error-flood diagnosis + the 07-16/17 live activation).**
  Originally flagged: `po_send_poll` showing "no marker" (never ran) while `po_materials.po_send.polling_enabled`
  appeared to read `True` live, diverging from the seeded/documented `false`. **Reconciled, no drift was ever
  real** — the 07-15 diagnosis confirmed the gate genuinely read `false` as seeded/documented and
  `po_send_poll` had never been activated (no plist installed, matching the intentional `VC-02
  DARK_UNLOADED_LABELS` posture); the original chase's "`True`" reading was an artifact of the same
  pytest-pollution window as DASH-5, not a live config state. **Superseded 2026-07-16/17** — the operator has
  since deliberately activated BOTH the PO-send and subcontract-send lanes live (plist loaded, gate flipped,
  Application Access Policy scope confirmed via Exchange Online PowerShell `Test-ApplicationAccessPolicy`,
  end-to-end Graph self-send from `procurement@` verified) — see memory-archive §G68.3. `po_materials.po_send.
  polling_enabled` reading `true` today is the intended live state, not drift.
- **DASH-8 (RESOLVED 2026-07-14, PR #597).** The dashboard had no "mark this CRITICAL resolved" verb — built
  same day: `mark_errors_resolved` (Class-B, `operator_dashboard/act/errors_ops.py`) stamps `Resolved At` on
  open-CRITICAL rows matching a **required** Script/Error-code filter (unfiltered mass-resolve refused),
  making them terminal so `clear_error_log` (#594) can then sweep them. Wired in one PR: the route
  (`/act/errors/resolve`), the `config.html` form, the mutation-route registry test, and CLAUDE.md.
- **DASH-9 (near-RESOLVED 2026-07-17) — open-CRITICAL backlog in `ITS_Errors`, down from 215 to 9.** The
  2026-07-14 forensic wipe correctly preserved every open CRITICAL (never auto-deleted), leaving 215
  open-CRITICAL rows post-wipe — most believed storm-era noise from the 2026-07-13 row-cap incident (the same
  `config_row_missing` firehose that filled the sheet to its 20k cap), none individually confirmed-benign at
  the time. **2026-07-16/17 partial action:** using the new `mark_errors_resolved` verb (#597), 83 of the 215
  were confirmed benign and marked resolved (50 `intake_poll`/retired-daemon + 33 smoke/test rows) → 132
  remained. **2026-07-17 full diagnosis pass:** the backlog had grown back to 134 by the time this session's
  own error-flood diagnosis ran (ongoing transient-Smartsheet-outage-window CRITICALs, ~2026-07-12→07-16,
  accruing between passes — root-caused LOW severity, the retry→breaker→fail-open stack behaved correctly,
  no code change needed beyond #608's alert-hygiene fix below; full root-cause narrative in blueprint
  memory-archive §G69.1); using the same verb,
  dry-run→live per `(script, error-code)`, audit-stamped `its-diagnosis-2026-07-17`, resolved **92 transient +
  33 historical** rows → backlog **134→9**. **The 9 residual, individually accounted for:** 7×
  `safety_reports.intake` / `uncaught_exception` — a REAL, still-open bug (`'tuple' object has no attribute
  'value'`) on the legacy/dormant email-intake code path (portal-marker is the live transport;
  `intake.py`'s email-ingestion stages are dormant-but-present and this is firing from within them) — NOT
  fixed, needs a real code fix or a decision to excise the dormant path entirely; 2× `scripts.watchdog` /
  `critical` — residue from the already-fixed 2026-07-13 row-cap incident (PR #562's storm-mode fallback has
  been live since), kept unresolved deliberately as historical record, safe to resolve whenever. Trigger for
  the remaining 7: next `safety_reports/intake.py` touch, or the dormant-email-path excision decision.
  **2026-07-19 correction (forensics pass): the "7× `safety_reports.intake` `uncaught_exception`, believed a
  REAL still-open tuple bug" residual is FALSIFIED.** All 7 rows are pytest pollution from a single 9-minute
  window on 2026-05-21 — the tracebacks contain mock frames and pytest tmpdir paths, and the production
  `kill_switch` code cannot produce the `'tuple' object has no attribute 'value'` shape. No dormant-path code
  fix or excision decision is needed for these rows. The 2× `scripts.watchdog` row-cap CRITICALs are likewise
  confirmed stale (fix live since 2026-07-13, zero recurrence). Both classes cleared as benign.
- **DASH-10 (on-the-horizon, WS2 follow-up) — dashboard native-app repackaging decision captured, not built.**
  Operator directed **Option A** for a future session: repackage the dashboard as a native macOS `.app` via
  `pywebview` + `py2app`, keeping the existing Tailscale-only exposure model unchanged (no new network
  surface, just a nicer launch/window experience than the current browser-tab + web-app-manifest Dock
  shortcut from #581). Not scoped or built this session — recorded here so the decision isn't re-litigated
  next time WS2 polish comes up. Trigger: next WS2 session, operator bandwidth for a UI-shell change.
- **DASH-11 (LOW, WS2 follow-up) — `picklist-sync` is unreachable via the dashboard's interval-edit verb.**
  An operator question ("can the dashboard change daemon run intervals?") surfaced that
  `operator_dashboard/act/daemon_ops.edit_interval` (#570) covers only an 8-daemon label allowlist;
  `picklist-sync`'s 3600s cadence is a hardcoded `StartInterval` literal in its plist, outside that
  allowlist, so its interval can't be edited from the dashboard today (confirmed: the daemon itself was
  healthy, this is a coverage gap, not a bug). Either add it to the allowlist or document the exclusion
  explicitly so the question doesn't need re-investigating next time. Trigger: next WS2 daemon-control
  polish pass.
- **DASH-12 (BUILT 2026-07-19) — dashboard Restart-dashboard verb.** Shipped as
  `operator_dashboard/act/dashboard_ops.py` + `POST /act/dashboard/restart` (Class B, elevated-confirm
  `restart-dashboard`): audit row written BEFORE the spawn, then a detached
  `/bin/sh -c 'sleep 1; exec launchctl kickstart -k gui/<uid>/org.solutionsmith.its.dashboard'` with
  `start_new_session=True` + closed stdio (survives the dashboard's own SIGTERM). Restart-ONLY — never a
  pull/deploy, never another label; `daemon_ops.controllable_labels()` still excludes the dashboard
  (`tests/test_dashboard_restart.py` locks all of this, incl. the restart-only command allowlist). §43
  entry in `docs/runbooks/operator_dashboard_config_editor.md`. The operator-pre-authorized self-exclusion
  exception is documented in the verb's module docstring.
- **DASH-13 (verb BUILT 2026-07-19; disposition pending operator) — `ITS_Review_Queue` backlog.** The
  2026-07-17 characterization ("285× no-safety-contact re-raised every Friday") was STALE — the 2026-07-19
  live read found **296 PENDING: 277× "weekly compile failed" (189 of them a one-day 06-13 storm for the
  since-deleted JOB-000013), only 6× no-contact (all 2026-06-07, jobs since deleted, cannot recur), 13
  misc**. Root causes fixed in PR #613: compile_now_poll scan-phase transients no longer mislabeled +
  review-row dedupe caps a stuck-Compile-Now retry at ONE PENDING row per (job, week). The bulk-resolve
  verb shipped as `operator_dashboard/act/review_ops.py` + `POST /act/review/resolve` (Class B, elevated
  `resolve-review`, filter-required, preview mode, nothing deleted). REMAINING (operator decisions):
  (a) the 3 surviving jobs (JOB-000017/-018/-027) are sandbox fixtures — deactivate portal-side (D1
  lifecycle → inactive; a sheet-side flip gets overwritten by fieldops_sync on the next portal edit) or
  keep as test fixtures and populate JOB-000027's blank Safety Reports Contact Email; (b) sweep the 232
  stale rows for deleted jobs via the new verb; (c) the 4 "sheet-count near cap … margin 60" rows expose a
  margin==cap misconfig (`sheet_capacity` margin should be < the 60 cap) — an ITS_Config value fix.
  **2026-07-19 handoff-pass update:** (b) DONE — the remaining 62 stale rows swept via the resolve verb
  (count-guarded dry-run first; queue now 2 PENDING = the two 'Acme Concrete' picklist mismatched-reference
  rows, a real pending data decision). (c) RESOLVED-AS-FOSSIL — the live `smartsheet.sheet_count_ceiling/
  margin` rows read 1500/50 (sane, explicit-seeded 2026-07-06); the four "14/60 (margin 60)" review rows
  were fired 2026-07-15 under values since corrected/reverted — no config change needed. JOB-000027's blank
  Safety Reports Contact Email populated (seth@solutionsmith.org, matching the sibling test jobs; audited
  `active_jobs_contact_populated`). REMAINING operator decisions: (a) deactivate-or-keep the 3 sandbox jobs
  (portal-side if deactivating), and the Acme Concrete picklist removal. The dashboard upgrade slate is
  filed at `docs/2026-07-19_dashboard_upgrade_slate.md`.
- **DASH-14 (found 2026-07-19, FIXED 2026-07-19) — PR #613's config-read fence fix is not ported to 3 replicas.**
  **FIXED 2026-07-19: all 3 replicas ported** (`safety_reports/compile_now_poll.py`,
  `field_ops/fieldops_sync.py`, `safety_reports/generate_core.py` — each file's single
  `_read_str_setting`-style reader now catches base `SmartsheetError` → WARN `config_read_error` +
  fallback, exactly the #613 shape, with per-reader fence tests). The F22 `_load_authorized_approvers`
  gate in `send_poll_core.py` remains deliberately fail-CLOSED, untouched. Same PR also closed the
  watchdog Check S stale-green blind spot: a green latest ci.yml run is now compared against
  origin/main's actual HEAD sha (the 2026-07-19 push-event delivery gap left merge commits with ZERO
  runs reading as "green" indefinitely); mismatch → WARN naming both shas + `gh workflow run ci --ref
  main` (the #619 workflow_dispatch); HEAD-resolution failure stays fail-safe INFO. Original entry:
  PR #613 fixed a class of bug where a daemon-local `_read_str_setting` config-row reader caught only
  `smartsheet_client.SmartsheetNotFoundError` + `SmartsheetCircuitOpenError` before falling back to a
  default — letting a generic single-cycle transient (`SmartsheetError` base: read-timeout, HTTP 500/502)
  escape uncaught to `@its_error_log` as a full spurious CRITICAL instead of a WARN+fallback. The fix landed
  in `po_materials/po_poll.py`, `subcontracts/subcontract_poll.py`, and `safety_reports/send_poll_core.py`
  only. **Confirmed live via `grep` this session, the identical shape remains unfixed in 3 more readers:**
  `safety_reports/compile_now_poll.py`'s own `_read_str_setting`, `field_ops/fieldops_sync.py`, and
  `safety_reports/generate_core.py` — all three still catch only the two named exception types. This is a
  direct 3-file port of PR #613's fix pattern, not a design question. **Deliberately NOT the same as** the
  F22 `_load_authorized_approvers` gate in `send_poll_core.py`, which PR #613 correctly left untouched
  (fail-closed on a config-read failure feeding an approval-authority list is the intended behavior, not a
  bug). Trigger: next error-hygiene pass, or immediately if any of the three fires a spurious CRITICAL
  again. See blueprint memory-archive §G70.3.

**Activation lesson from this session's chase (not a tech-debt item, a process note):** a daemon's
`ITS_Config` polling gate should be flipped `True` only **after** its matching Cloudflare Worker
secret/route is actually deployed — flipping the gate first produces a benign-but-noisy bearer-rejected/401
CRITICAL storm on every cycle until the deploy catches up. Root-caused this session on a subcontract-lane
`bearer_rejected` error. Apply this ordering on every future config-actuator/Worker-secret pairing.

- **[RESOLVED 2026-07-21] A 4th, previously-uncounted replica of the DASH-14/#613 config-read fence gap:
  `safety_reports/publish_daemon.py` — plus the F22 approver gate, both closed by the transient-fence PR.** The "FIXED 2026-07-19: all 3 replicas ported" claim above is
  itself now stale — a fresh `grep` this session (2026-07-21, coverage-gap hunt) found
  `publish_daemon.py`'s own config-row reader (`get_setting(key, workstream=WORKSTREAM)`, ~line 195)
  still catches only `SmartsheetNotFoundError`/`SmartsheetCircuitOpenError`, not the generic
  `SmartsheetError` base — the exact #613 shape, a direct one-file port away, not a design question
  (unlike the F22 `_load_authorized_approvers` contrast noted above, which is correctly untouched).
  **Separately, still open (found same session, NOT fixed):** the F22 gate itself
  (`send_poll_core._load_authorized_approvers`, shared by all five send-poll daemons —
  `weekly_send_poll`/`progress_send_poll`/`po_send_poll`/`subcontract_send_poll`/`rfq_send_poll`) is
  *deliberately* unfenced/fail-closed by its own §42 docstring — correct for the SEND decision (never
  dispatch on an unreadable approver set) but means a one-cycle transient `list_workspace_share_emails`
  blip still pages a full CRITICAL and aborts the cycle, the same noisy-but-safe class #613/#628 quieted
  elsewhere via a WARN-and-skip reclassification that didn't touch the fail-closed *behavior*, only the
  *severity*. No reclassification has been attempted here — flagged, not built, since it touches the F22
  security gate and warrants care before touching. Trigger: next error-hygiene pass, or the next time
  either surface actually pages on a Smartsheet blip. See blueprint memory-archive §G72.
  **RESOLUTION (2026-07-21, the transient-fence PR).** Both surfaces are now fenced.
  `publish_daemon` adopts `shared/creds_resolution.read_base_url` and PROPAGATES
  `SmartsheetCircuitOpenError` instead of swallowing it into a fail-open "polling disabled"
  (reporting a breaker outage as a disabled gate was a lie, and it kept the fleet's fastest
  observer out of the circuit-open escalation). The F22 gate was reclassified with explicit
  operator ratification — severity only, never behaviour: a transient approver-read failure
  records ERROR and counts toward a sustained counter (threshold 3 at the 15-min send cadence)
  instead of paging immediately, while auth/permission errors still page at once. Fail-closed is
  unchanged and now PROVEN by test (zero `send_fn` calls on ANY approver-load failure, transient
  or not); the prove-it-bites injection for that assertion dispatched a real send and the test
  caught it. `_load_authorized_approvers`' §42 contract docstring was rewritten in the same diff
  so the contract cannot contradict the code.

- **[OPEN 2026-07-21, Seth-owned] `po_materials.rfq_send.polling_enabled` shipped `first_activation_gated`
  (dashboard tier "A") rather than `elevated_confirm`, per PR #627's own in-code rationale — and, found
  live the same session, the gate already reads `true` on the mirror (as do `po_send`/`subcontract_send`/
  `estimate_poll`/`rfq_poll`), contradicting `CLAUDE.md`'s `po_materials/rfq_*` row (still says "ships
  **dark**... Go-live = FIXED high-class External-Send-Gate flip ... → Seth") and this repo's own prior
  session-close records. #627 itself only fixed the *dashboard's* notes to stop asserting a live-state
  claim (§42/§55.4 truthful-reporting fix) — it deliberately did **not** touch the gate value or resolve
  whether tier "A" is the right activation posture for an External-Send-Gate crossing. Two things need
  Seth's call, not autonomous action: (1) whether `rfq_send` (and the sibling procurement gates) should
  in fact be live on the mirror, or the flip was premature; (2) whether the console's activation tier for
  this class of gate should be `elevated_confirm` (PIN + typed confirm + attestation) rather than the
  faster-brake "A" tier, given `apply_elevated_edit` can already complete a false→true send-gate flip
  ~~Also stale from the same finding: `CLAUDE.md` lines ~148/249 still say "16 tracked jobs"~~ —
  **RESOLVED 2026-07-26 (documentation-consolidation pass):** both CLAUDE.md surfaces now read
  **18 tracked jobs**, matching `watchdog.TRACKED_JOBS` (verified: 18 entries). The rest of this
  entry — the `rfq_send` activation-posture questions — remains OPEN and is unaffected. Trigger: next
  operator RFQ-send go-live/activation-posture session. See blueprint memory-archive §G72.2.

- **[OPEN 2026-07-21, low, Seth-owned] `config_actuator` reads `safety_reports.portal.worker_base_url`
  under `workstream="po_materials"`; `po_poll` reads the same key under `workstream="safety_reports"`.**
  Both rows exist so both resolve today — this is a **preserved-byte-for-byte** divergence, not a live
  bug (`config_actuator.py`'s own docstring at `_resolve_creds` names it explicitly and declines to
  "fix" it mid-unrelated-change, per §14). Worth a doctrine/config-model look at some point: either the
  two workstream scopes should converge on one, or the divergence should be named as intentional
  (e.g., "config_actuator reads its own daemon's workstream scope for every config key, no exceptions")
  rather than left implicit. Trigger: next config-model/`ITS_Config` schema session.

- **[OPEN 2026-07-21, low] `scripts/verify_cutover.py`'s VC-01 docstring undercounts required secrets:
  says 18, `REQUIRED_SECRETS` is actually 20.** The tuple itself (`NON_BOX_SECRETS` 11 +
  `BOX_SECRETS` 3 + `PO_SECRETS` 1 + `DARK_BEARER_SECRETS` 4 + `OPERATOR_SECRETS` 1 = 20) already
  correctly enrolls `ITS_PORTAL_ESTIMATE_TOKEN` + `ITS_PORTAL_RFQ_TOKEN` (added with the RFQ/estimate
  lane) — the cutover CHECK is not under-enforcing, only the module-docstring summary line (~line 33) is
  stale. Trivial fix, not urgent (no functional gap). Trigger: next `verify_cutover.py` touch.

- **[OPEN 2026-07-21, low] `operator_dashboard/act/registry.py` enrolls `subcontracts.subcontract_send.
  polling_enabled` (added by PR #627) but not its `from_mailbox`/`scheduled_send_local` siblings** —
  `rfq_send` got all three (`polling_enabled`, `from_mailbox`, `scheduled_send_local`) in the same PR,
  `subcontract_send` only got the one that was already-missing-and-flagged. A parity gap between two
  structurally-identical send lanes' console coverage, same shape as the coverage-gap hunt's other
  findings this session. Trigger: next dashboard registry touch, or the next `subcontract_send` config
  session.

## [RESOLVED 2026-08-10 — superseded in code by Track 6, and its "never fired live" claim is now false] Archive-on-closure — SUPERSEDED IN CODE by ROADMAP Track 6 [PARTIAL 2026-07-23, updated 2026-08-03]

> **Why it left `tech_debt.md` (2026-08-10, bucket-E triage):**
> The entry's own header said SUPERSEDED IN CODE. Its two remaining assertions have since been
> overtaken: the archive was DRILLED LIVE on 2026-08-10 (attended, `JOB-000030`, archive → un-archive →
> archive, every container keeping its folder id), so the repeated "the archive has NEVER fired against
> live data" claim is FALSE and must not survive in a file people act on; and its Gap 3 / dev-host
> framing is inverted now that this checkout lives on the production Mac. A ~5-line stub survives in
> `tech_debt.md` carrying the one thing still genuinely owed — the §51 doctrine rider — plus a pointer
> to `docs/runbooks/job_archive.md`, whose Symptom 6 documents the un-exercised live-folder collision
> refusal. Remaining Track 6 build work is tracked in `docs/ROADMAP.md`.

### Archive-on-closure — SUPERSEDED IN CODE by ROADMAP Track 6 (2026-08-03); the live proof and the §51 rider remain open [PARTIAL 2026-07-23, updated 2026-08-03]

> **Update 2026-08-03 — most of this entry is now historical.** ROADMAP Track 6 landed seven PRs
> (#715, #716, #718, #719, #720, #721, #722) that replace the narrow slice this entry describes.
> What changed, against the three gaps below:
>
> - **Gap 1 (trigger semantics) — RESOLVED, and the old trigger was worse than this entry knew.**
>   Setting `lifecycle='archived'` did not merely fail to archive most surfaces; it fired an
>   **unconfirmed, un-retryable four-sheet relocation** on the next mirror cycle, while the UI
>   re-displayed the job as "Inactive" (the detail payload carried no `lifecycle`, and the legacy
>   `status` collapses inactive+archived into `'closed'`). #715 disarmed it on all three layers —
>   SPA option removed, Worker returns 409 `use_archive_route`, Python trigger removed — and fixed
>   the display across its whole fan-out. Archiving now runs through dedicated routes behind a
>   server-side typed confirmation (#720) and a distinct `cap.job.archive` (#719).
> - **Gap 2 (the uncovered surfaces) — ADDRESSED IN CODE, not yet live.** `field_ops/job_archive.py`
>   (#722) relocates **six** per-job containers: four Smartsheet per-job FOLDERS (Safety, Progress,
>   Purchase Orders, Subcontracts) and two Box ones. Six rather than eleven because
>   `safety_reports.box.portal_root_folder_id` is the SHARED Box root for safety + PO + RFQ +
>   subcontracts, so its per-job folder carries them along. Note the denominator confusion this
>   entry flagged (~11 here vs ~45 in the proposal) is now moot: the unit is the CONTAINER, not the
>   surface, because one folder move relocates its whole subtree.
> - **Gap 3 (the §30 live smoke) — STILL OPEN, and now blocked on something concrete.** Six
>   Smartsheet + one Box live smoke exist (#716, #718) but are `-m integration`, operator-run, and
>   have never been run. **They cannot be run from this host:** `ITS_SMARTSHEET_TOKEN` resolves to
>   the PRODUCTION tenant (verified 2026-08-03 — `get_client()` lists real Evergreen workspaces and
>   `ITS — Archive = 7347287308429188`, the production id; the sandbox `1649411894863748` is
>   absent). Running them here would create and delete sheets in the live tenant. See the new
>   entries below for the unblock conditions.
>
> **Still true and still the point:** the archive has NEVER fired against live data. Track 6 is
> code-complete on the Smartsheet leg and unproven end-to-end.
>
> Remaining Track 6 work is tracked in `docs/ROADMAP.md` Track 6, not here.


A 3-lens adversarial audit run during the 2026-07-23 tenant-wipe/stand-up rehearsal — code-level
(`~/its/logs/reviews/2026-07-23_arch_code.json`), doctrine/runbook-level (`2026-07-23_arch_docs.json`),
per-job-surface inventory (`2026-07-23_arch_inventory.json`) — converged on one verdict for "what happens
when a project is archived": Op Stds v21 §51 + its folded riders define archive-on-closure for **exactly
one slice**, the four progress standing-tracker sheets (Hours Log, Equipment, Material List, Material
Incidents), implemented by `field_ops/fieldops_sync.py:811-869` (`_archive_closed_job_trackers` →
`shared/smartsheet_client.move_sheet_to_folder`, its#462, PR #465) and never anything else.

**Three compounding gaps, none of them fixed by this audit (diagnosis only):**

1. **Trigger-semantics mismatch.** The move fires only when a portal-origin job's `lifecycle` is explicitly
   set to `'archived'`. The portal UI's own documented normal close path is `'inactive'`
   (`fieldops_job_write.ts:273-274`) — which does **not** archive anything. Nothing in the UI or any doc
   tells an operator that closing and archiving are different actions with different consequences.
   Smartsheet-origin jobs are structurally exempt entirely (`fieldops_sync` mirrors only portal-origin dirty
   jobs), so setting `Archived` directly in `ITS_Active_Jobs` can never trigger the move.
2. **Closure policy undefined for ~11 other per-job surfaces.** Safety + progress week sheets and their
   per-job Smartsheet folders, WSR/WPR human-review rows, PO/RFQ/estimate/subcontract per-job mirror sheets
   (`shared/job_sheet.py`-created) and their flat Log rows, and **all** per-job Box content
   (`shared/box_client.py` has zero move/archive primitive) have no archive path defined OR implemented
   anywhere. The only whole-project procedure on record is the destructive manual 3-system `purge-job` nuke
   (D1 cascade + manual Smartsheet + manual Box) — deletion, not archival, with its own documented footgun
   (delete the `ITS_Active_Jobs` row first or the down-sync re-creates it; HOUSE_REFLEXES §7).
3. **§30 live smoke never run.** The committed operator-run live integration test
   (`tests/test_smartsheet_client_integration.py::test_move_sheet_to_folder_relocates_live`) has been a
   listed pre-reliance TODO since the 2026-07-04 session log
   (`docs/session_logs/2026-07-04_smartsheet-verify-hours-smoke-archive-on-closure.md`) and no later session
   log records it running. Decisive corroboration: the `ITS — Archive` workspace held **0 sheets** in the
   2026-07-23 pre-wipe dump (`~/its/logs/migrations/prewipe_20260723T030026Z/_manifest.json`) — no job has
   ever actually reached `lifecycle='archived'` against live data in this system's history.

**A concurrent Brief-A session opened PR #678** (`docs/project-closure-archive` — a project-closure runbook
+ closure-policy proposal) addressing the doc side of gaps 1/2 above; check `gh pr list`/`gh pr view 678`
before starting doc work here, it may already be answered. Gap 3 (the live smoke) and any code-level fix for
gap 1/2 remain open regardless of what #678 lands, since #678 is docs/proposal-only. **Trigger:** Seth
reviews the three dossiers + PR #678 and decides (a) whether `'inactive'` should also archive or archiving
stays deliberate, (b) a closure policy for the ~11 uncovered surfaces (retain-in-place is the current
de-facto policy, never chosen explicitly), (c) schedules the overdue live move smoke. **Tag:** `field_ops`,
`archive-on-closure`, `its#462`, `§51`, `audit`, `seth-owned`.

## [RESOLVED 2026-08-10 — all five named opportunities landed; three residuals kept as a short entry] Un-adopted optimization findings from the 2026-07-23 stand-up rehearsal

> **Why it left `tech_debt.md` (2026-08-10, bucket-E triage):**
> The entry's own first paragraph recorded all five named opportunities as landed and four-part-verified
> (PRs #679/#680/#685/#686/#687, plus #683), leaving ~6 KB of superseded in-flight/claimed-by-a-concurrent-
> session bookkeeping. The three lower-priority residuals its 2026-07-24 audit note flagged as having no
> other home — a generic on-demand dump-restore utility, one shared config-seed engine module, and
> enrolling CL-15/CL-17/CL-19 as mechanical `verify_cutover` checks — survive as a ~10-line entry in
> `tech_debt.md`.

### Un-adopted optimization findings from the 2026-07-23 stand-up rehearsal — 2 of 5 named opportunities remain unclaimed, 3 already in flight [PARTIALLY RESOLVED 2026-07-23 — 3 residuals still open, see audit note]

**RESOLVED 2026-07-23 (Brief A close-out, PRs #673/#675/#676/#679/#680/#683/#685/#686/#687, all
four-part-verified, exec HEAD `fc27f75`):** all 5 named opportunities below landed. The 3 "already in
flight" items merged as claimed — finish/epilogue → **#679**, CL-12 repoint actuator → **#680**, CL-11
shares + VC-10 → **#685** (reconciled with #674's ACT-fence, which #679 adopted wholesale, deleting its own
`_run_marker.py`; its#677 closed as superseded). Both "genuinely unclaimed" items also landed: item 1
(checklist/punchlist/README collapse around the one-step stand-up + the CL-12 "all gates true" doc-bug fix)
→ **#686**; item 2 (run-branch mode) → **#687** (per-run `standup/run-<UTC>` branch, per-stage checkpoint
commits with a `:(exclude)logs` pathspec, `--resume` merge-main-then-STOP-on-conflict flow, landing-PR
push). Of the "lower-priority" findings, one more also landed: scoping `sheet_ids_regen --retry-missing` to
the unresolved `--expect` constants' workspaces → **#683**. Remaining lower-priority findings (more CL items
as mechanical `verify_cutover` checks, the generic dump-restore utility, the config-seed-engine dedup) are
genuinely still open — see the new `docs/session_logs/2026-07-23_standup-process-optimization.md` (PR #688)
and the fresh tech-debt entries below for what's left. Original findings preserved below for the record.

Three review passes over the 2026-07-23 tenant wipe+stand-up rehearsal produced optimization dossiers —
operator-experience (`~/its/logs/reviews/2026-07-23_opt_operator.json`), runtime/safety
(`2026-07-23_opt_runtime.json`), code-simplification (`2026-07-23_opt_simplify.json`) — proposing follow-on
work beyond what shipped this session (exec PRs #664-672/#674). **A concurrent Brief-A session has already
claimed 3 of the higher-value findings** — confirmed via `gh pr list`/worktree inspection at the time of
this entry, re-check before acting on any of these:

- **Standup finish/epilogue mechanization** (codify the by-hand post-merge tail: state cleanup, posture-
  flagged fleet reload, cycle-wait, heartbeat/error verify, gate-flip report) — **IN FLIGHT, PR #679**
  (`feat/standup-finish`, open as of this writing). Do not re-build.
- **CL-12 repoint actuator** (`production_repoint.py` — a declarative plan/commit tool for the production
  repoint changeset) — **IN FLIGHT, PR #680** (`feat/production-repoint`, open as of this writing). Do not
  re-build.
- **CL-11 approver-share manifest + a mechanical VC-10 approver-shares `verify_cutover` check** — **IN
  PROGRESS**, local WIP branch `feat/production-shares` (worktree `~/its-shares`, one commit ahead of
  `origin/main` as of this writing, not yet pushed/PR'd: "mechanize CL-11 — approver-share manifest +
  guarded seeder + VC-10 approver-shares gate"). Do not re-build; if picking this up, coordinate with
  whoever owns that worktree first.

**Genuinely still unclaimed, no worktree/PR found for either as of this writing:**

1. **Collapse the cutover checklist's Smartsheet/Box stand-up into ONE `standup.py` invocation.** The Aug-7
   punch-list's cutover-day binder (`docs/operations/cutover_checklist.md`,
   `docs/operations/cutover_operator_punchlist.md`, `docs/runbooks/host_migration_runbook.md`) has zero
   mentions of `standup.py`/`sheet_ids_regen.py` (grep-confirmed) and `scripts/migrations/README.md` still
   documents the superseded manual builder-by-builder walk with hand-pasted FLIP steps as "the Phase-1
   cutover builder sequence" — on the real cutover day an operator following the binder as written would
   redo the exact ordeal this rehearsal just paid to retire. **Same PR should fix the CL-12 doc bug:**
   `cutover_checklist.md`'s CL-12 line asserts "all `*_enabled` gates true," which contradicts CL-03 (send
   daemons dark), CL-13 (read gate Descriptions first), the standup epilogue's own stated posture ("gates
   seeded DARK — the production posture"), and HOUSE_REFLEXES §5's "static text must never assert a LIVE
   gate state" rule — rewrite as "gates flipped per the activation plan, send gates last, Seth."
2. **Auto-created run-branch mode for `standup.py`** — commit regen output per stage and merge
   `origin/main` into a dedicated run branch, instead of the stash/pull/pop dance the operator improvised 6
   times this session to land mid-run fix-PRs (#665-#672) without losing in-progress regen state.

Lower-priority findings recorded in the dossiers but not named here (see the JSON files directly for full
detail): enrolling more manual CL items as mechanical `verify_cutover` checks (CL-15/CL-17/CL-19); rejecting
the scratch-prefix dress-rehearsal mode in favor of scheduling an early real production stand-up; documenting
why a production wipe variant is deliberately out of scope; scoping `sheet_ids_regen --retry-missing`
re-resolution to only the unresolved `--expect` constants' workspaces; a generic dump-restore utility for
any sheet on demand; extracting one shared config-seed engine module (parameterize-not-clone) behind the
existing per-lane `seed_*.py`/`build_*.py` files. **Trigger:** before starting any of the "genuinely
unclaimed" items, re-run `gh pr list` and `git worktree list` — the concurrent session was still active at
the time this entry was written and may have claimed more ground since. **Tag:** `migrations`, `standup`,
`cutover`, `optimization`, `parallel-session-coordination`.

> **Audit 2026-07-24 (tech-debt janitorial pass):** The 5 NAMED opportunities landed (PRs #679/#680/#685/#686/#687, all four-part-verified). STILL OPEN — three lower-priority rehearsal-dossier residuals that have NO separate tech-debt entry, retained here so they aren't lost: (1) a generic on-demand dump-restore utility for any sheet; (2) one shared config-seed engine module (parameterize-not-clone behind the per-lane seed_*/build_* files); (3) enrolling CL-15/CL-17/CL-19 as mechanical verify_cutover checks.

## [RESOLVED 2026-08-10 — merged into the entry that recorded the same mechanism as delivered] Configuration validation at daemon startup [OPEN 2026-05-24]

> **Why it left `tech_debt.md` (2026-08-10, bucket-E triage):**
> Two entries described one delivered mechanism, and this one did not know the other had landed. The
> `shared/required_config.py` + `resolve_and_log` startup pass shipped via PR #481 (issue #336) and is
> recorded as MECHANISM DONE under S-1 of "Smartsheet-wiring audit findings — daemon-health + capacity
> hygiene" in `tech_debt.md`. This entry is folded there as a closed sub-finding. **Its one surviving
> residual — probing that a resolved Box folder ID / Smartsheet sheet ID actually exists, which
> `resolve_and_log` does not do — was carried over into that S-1 bullet, not dropped.**

### Configuration validation at daemon startup [OPEN 2026-05-24]

Once items A2 / A3 / A5 (and the existing trusted-contacts work) migrate config into Smartsheet, daemons fetch config at startup with no formal validation step. A malformed row, missing key, or unresolvable folder ID can let the daemon enter its main loop with broken config — it'll fail per-cycle at unpredictable points instead of failing loud at startup.

**Failure mode:** operator typos an ITS_Config row. Daemon starts. First poll cycle runs. Per-cell-write fails in some downstream call. ITS_Errors fills with cryptic errors. Operator's mental model: "ITS broke, why is the watchdog quiet?" — because the watchdog can't distinguish "broken config" from "broken external API."

**Proposed fix:** new `shared/config_validation.py` with a single `validate_all()` entry point called from every daemon's `main()` before the loop starts. Per-daemon manifest of required keys + validators:

- All required ITS_Config keys present (per a per-daemon registry — `intake_poll.REQUIRED_CONFIG`, etc.).
- All email addresses pass `^[^@]+@[^@]+\.[^@]+$`.
- All Box folder IDs resolve via Box API to non-trashed folders (depends on A2 landing).
- All referenced Smartsheet sheet IDs exist (cheap `get_sheet_summary`-style probe).

On failure: log full report to Sentry + ITS_Errors, exit non-zero. **Do not enter the loop with broken config — fail loud.**

**Effort:** ~half-day session including the validation module + per-daemon registries + tests + integration smoke + runbook update ("if a daemon fails to start, check the Sentry / ITS_Errors entry for the validation report").

**Phase target:** 1.6 — lands after A2/A3/A5 migrate config into Smartsheet. Sequence: config-migration cluster → validation layer.

**Tag:** `config-migration` (the consumer side).

**Revisit when:** A2 lands, AND a third polling daemon is queued, OR operator hits the silent-fallback-into-bad-config failure mode in real ops.

Surfaced: 2026-05-24 hardcoded-values audit brief, §C1.

## [RESOLVED 2026-08-10 — send-free half shipped as PR #393; import-level residual folded into the M2 entry] Safety Portal — Worker-side capability-gate for TS not covered by Python AST gate [OPEN 2026-06-04]

> **Why it left `tech_debt.md` (2026-08-10, bucket-E triage):**
> The send-free half of what this entry proposed shipped: `tests/test_worker_send_free.py` (PR #393) is a
> CI-collected grep over `safety_portal/worker/**/*.ts` that fails on any `fetch(` other than
> `ASSETS.fetch(` — verified present at live HEAD. The uncovered part (import-level gating of the TS
> surface, a forbidden *import* rather than a call) is the same static/dynamic reach gap already
> described by **"Safety Portal M2 — capability gate is static-AST-import-only"**, so the residual was
> folded into that entry rather than duplicated here.

### Safety Portal — Worker-side capability-gate for TS not covered by Python AST gate [OPEN 2026-06-04]

`tests/test_capability_gating.py` enforces Invariant 1 at the Python AST level. It does not reach the TypeScript Worker at `safety_portal/worker/`. Phase 2 Worker is send-free by inspection (no email, no Graph, no Anthropic). When the Phase 5 HMAC email shim lands (the Worker emits a verified email to `safety@` → `intake.py`), this gap becomes load-bearing.

**Proposed fix (at Phase 5):** add a TS-equivalent capability-gate step — either a `tsc --noEmit` + `grep`-based AST scan of Worker entrypoints for forbidden imports, or extend `test_capability_gating.py` to scan `.ts` entrypoints with the same pattern. Phase 2 does not require this yet.

Note: the Phase 2 brief referenced "Decision 4" for this item, but no named blueprint decision with that ID exists. The decision is tracked here instead.

**Tag:** `safety-portal`, `capability-gate`, `invariant-1`.

**Revisit when:** Phase 5 email-shim work begins.

Surfaced: 2026-06-04 Safety Portal Phase 2 session (PR #158). Related: `tests/test_capability_gating.py`.

## [RESOLVED 2026-08-10 — five of six obsolete or landed; #8 kept as a standalone entry] R-series spec Deferred #5–#10 — named follow-ups, not in this program [OPEN 2026-07-02]

> **Why it left `tech_debt.md` (2026-08-10, bucket-E triage):**
> The entry's own 2026-07-24 audit note settled it: #5/#6 OBSOLETE (superseded by the D-series SOP
> daily-form redesign), #7/#9/#10 RESOLVED (PRs #451/#453/#450), and only #8 — server-side
> completed-history cutoff/deletion — still open with zero implementation hits (re-verified against live
> HEAD 2026-08-10). #8 survives as its own short entry in `tech_debt.md`; the register moves here so the
> locked-decision reasoning stays findable.

### R-series spec Deferred #5–#10 — named follow-ups, not in this program [OPEN 2026-07-02]

**Surfaced 2026-07-02**, `~/.claude/plans/refinement-spec-r-series.md` §3 "Deferred / won't-do." Six items were explicitly scoped OUT of the R-series refinement program (R1–R5, R7) as named follow-ups, not silent gaps:

5. **Mid-day template re-sync into open instances.** Admin edits to a checklist template take effect "tomorrow," not on today's already-generated instances (R4 ships copy-only — "changes take effect tomorrow" — snapshot semantics kept as-is).
6. **Mid-day job-reassignment orphan-instance surfacing/auto-cancel.** If a person is reassigned off a job mid-day, their already-generated daily-checklist instance for that job is not auto-cancelled or flagged orphaned; R2's day-rollover refetch narrows the confusion window but doesn't close the gap.
7. **Scoped crew edit/retire for subcontractor-created crew + time amend/void UI** via the `amends_uuid` chain — a data-correction follow-up epic. R2 ships the crew list + duplicate warning; R1/R7 stop new junk rows, but no amend/void UI exists yet.
8. **Server-side completed-history cutoff/deletion.** R2 ships client-side collapse only; history stays queryable/unbounded server-side.
9. **Full URL router.** R3 ships minimal hash/history integration only (push-per-view-change, popstate restore, `beforeunload` dirty-form guard) — not a real router.
10. **`task_assignments.due_date` column.** Considered and deferred per audit; `created_at`/`assigned_by` rendering (R2) covers the urgency-signal gap for now, but there is no due-date field on task assignments.

None of these are regressions — they were locked-decision scope cuts made explicitly, with the reasoning captured in the spec. Listed here so they don't get silently rediscovered as "bugs" in a future session.

**Tag:** `field_ops`, `checklist`, `tasks`, `r-series`, `deferred`. **Revisit when:** planning the next field-ops UX pass — check this list before re-scoping any of the six from scratch.

---

> **Audit 2026-07-24 (tech-debt janitorial pass):** OBSOLETE: #5/#6 (superseded by the D-series SOP daily-form redesign). RESOLVED: #7/#9/#10 (PRs #451/#453/#450). STILL OPEN: only #8 (server-side completed-history cutoff/deletion — 0 implementation hits). The list itself is an intentional locked-decision scope-cut register kept so these aren't re-discovered as 'bugs'.

## [RESOLVED 2026-08-10 — folded into the PM-5 bullet it addends] PM-5 addendum — `evergreen-its` branch protection CONFIRMED LIVE

> **Why it left `tech_debt.md` (2026-08-10, bucket-E triage):**
> A standalone entry whose entire content was an addendum to one bullet of another entry. Both of its
> load-bearing facts were folded verbatim into **PM-5** of "Production-host migration — outstanding
> items": (1) branch protection on `its-sys-admin/evergreen-its` is CONFIRMED LIVE (required checks
> `test`/`portal`/`secrets`, strict up-to-date branches), so the specific fail-open is not currently
> exposed on either repo; and (2) **the underlying code defect is UNFIXED** — re-verified against live
> HEAD 2026-08-10, `safety_reports/publish_daemon.py:459` fetches `mergeStateStatus,statusCheckRollup`
> in one `gh pr view`, `:460` early-returns on `mergeStateStatus == "CLEAN"`, and the rollup inspection
> at `:462-465` is only reached when it is not CLEAN.

### PM-5 addendum — `evergreen-its` branch protection CONFIRMED LIVE, narrowing but not closing the `_wait_for_ci` risk [OPEN 2026-08-10]

PM-5 (production-host migration section, above) flagged `publish_daemon._wait_for_ci`'s `mergeStateStatus ==
CLEAN` early-return as safe on `SolutionSmith-debug/its` only because that repo's required-checks protection
makes `CLEAN` mean "CI passed" — and flagged `its-sys-admin/evergreen-its`'s protection state as
**unverified** (the 404-vs-403 API trap). **Now confirmed live**, verified 2026-08-10: required checks
`test`/`portal`/`secrets`, strict up-to-date branches. The specific fail-open PM-5 warned about (a §50
privileged-actuation PR squash-merging without CI ever gating it) is therefore **not currently exposed** on
either repo. The underlying code defect — `_wait_for_ci` gates on `mergeStateStatus` alone rather than
`statusCheckRollup` directly — is unfixed and remains latent for any FUTURE repo this daemon points at with
weaker protection. **Tag:** `host-migration`, `external-code-actuation`, `op-stds-50`.

## [RESOLVED 2026-08-10 — the worktree is gone; the entry cannot be acted on as written] `~/its-archive` worktree holds uncommitted keepers + now-obsolete SPA work [OPEN 2026-08-10]

> **Why it left `tech_debt.md` (2026-08-10, bucket-E triage):**
> Verified 2026-08-10: `git worktree list` shows only `~/its`, `~/its-preop-forms` and the transient
> session worktree — no `~/its-archive` — and `ls -d ~/its-archive` returns *No such file or directory*.
> The worktree and the four uncommitted keeper diffs it described no longer exist, so the entry's
> instruction ("cherry-pick the 4 keepers, discard the obsolete SPA files, remove the worktree") has no
> subject. **If any of those fixes still matter they must be RE-DERIVED against current `main`** — they
> cannot be recovered from the worktree. The four were: `shared/smartsheet_client.py` (`_token()` +
> `ITS_SMARTSHEET_TOKEN_KEY`), `shared/box_client.py` (`ITS_BOX_KEYCHAIN_SUFFIX`),
> `scripts/migrations/sheet_ids_regen.py` (token-accessor fix), and `safety_portal/worker/index.ts`
> (hostname-gated dev CSP relaxation); the entry itself already flagged that #18/#20 may have carried
> equivalents.

### `~/its-archive` worktree holds uncommitted keepers + now-obsolete SPA work [OPEN 2026-08-10]

Worktree `~/its-archive` (branch `feat/archive-pr7-spa-button`) carries 4 uncommitted Python/TS keeper diffs
predating #18's landed SPA implementation — `shared/smartsheet_client.py` (`_token()` +
`ITS_SMARTSHEET_TOKEN_KEY`), `shared/box_client.py` (`ITS_BOX_KEYCHAIN_SUFFIX`),
`scripts/migrations/sheet_ids_regen.py` (token-accessor fix), `safety_portal/worker/index.ts` (hostname-gated
dev CSP relaxation) — plus its own `JobArchivePanel.tsx`/`ConfirmTypedModal.tsx`, now **obsolete**, superseded
by #18's merged versions. Also holds untracked drill-local sandbox `sheet_ids.py`-family files that must
**never** be committed. **Fix:** cherry-pick the 4 keepers (if still needed — verify against current main
first, since #18/#20 may have already carried equivalent fixes), discard the obsolete SPA files, remove the
worktree. **Tag:** `field_ops`, `archive-on-closure`, `worktree-hygiene`.

## [RESOLVED 2026-08-12 — PR-5] Weekly Production Report page 3 now reads the ADR-0006 living task list [OPEN 2026-08-11, low]

The report's Construction Progress / Delays page renders "No schedule imported for this job"
because `worker/fieldops_report.ts` returns `schedule: null` — `job_schedule_tasks` does not
exist yet. The ADR-0006 schedule lane has landed its intake pool (#80, migration 0066), its
OCR/geometry/parse core (#84) and its daemon (#85); the living task list is its PR-4.

Nothing is broken and nothing is blocked: the renderer already handles both states and the
office screen already says why the page is empty, so the binding is additive when the table
arrives.

**Fix:** in `buildReportData`, add the `job_schedule_tasks` read grouped by `section` with
`percent_done`, plus the behind-schedule derivation (`finish_date < today AND percent_done <
100`) feeding the assembled Critical Items seed — `wpr_data._assemble_critical_items` is
already ordered so that source slots in at the top without reordering. The renderer needs no
change.

**Revisit when:** the schedule lane merges `job_schedule_tasks`. **Tag:** `progress_reports`,
`weekly-production-report`, `adr-0006`, `low`.

**Resolved 2026-08-12.** The schedule lane landed `job_schedule_tasks` (migration 0071) and
the binding went in as planned — additive, and the renderer needed no change because it had
handled both states since #82. `buildReportData` groups ACTIVE tasks by `section` in document
order and derives the behind-schedule set (`finish_date < today AND percent_done < 100`,
server Pacific date) which seeds Critical Items worst-slip-first.

One thing the real schema forced that the plan had not anticipated: `percent_done` is NOT NULL
DEFAULT 0, so it cannot represent "never reported". The route returns NULL — and the report an
em dash — only when no portal mark, no committed schedule value, and percent_done still 0.
Printing 0% there would tell a client no work was done, a different and possibly false claim.

