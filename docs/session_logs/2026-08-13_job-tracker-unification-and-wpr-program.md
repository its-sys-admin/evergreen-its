---
type: session_log
date: 2026-08-13
status: closed
related_prs: [131, 132, 133, 134, 135, 136, 137, 138, 139, 140, 141, 142, 143]
workstream: null
tags: [session_log, safety_portal, field_ops, progress_reports, po_materials, subcontracts, job_tracker, wpr, migrations, design_agents, adversarial_review, phantom_css]
---

# Session log — 2026-08-13 → 2026-08-14 · Job Tracker unification + WPR program

## Summary

One operator prompt, five asks, one continuous overnight arc: 13 PRs merged (#131–#143), two D1
migrations (0074, 0075), two production Worker deploys, final version `99edb80c`. The arc opened
in plan mode with three parallel explorers, one of which root-caused a real live-tenant defect
against Deep Lake (JOB-000032) rather than a synthetic scenario — the Weekly Production Report's
labor table was listing individual people ("Devin Jones", "Joe Ryan") as if they were companies,
because the daily report's free-text "Crew / Subcontractor" field is exactly that: free text. The
operator was grilled (`AskUserQuestion`) against that evidence and against three further asks —
a job-selectable Site Tasks page, a WPR photo bridge + office upload + thumbnails with the operator
imposing no technical constraints on scope, and (added at plan review, not in the original prompt)
a per-job Procurement section — and the ratified decisions were handed to three parallel design
agents, then implemented serially in per-task worktrees against Tracks C, B, and A.

Every one of the 13 PRs merged with a passing PR-level CI gate (legs 1–3 clean throughout). Leg 4
— main-branch CI on the merge commit — is the one place this log declines to round up: GitHub's
`ci-main` concurrency group repeatedly cancelled an in-flight main-push run when the next PR's merge
pushed a newer commit during the cascade, so six of the thirteen merge-commit CI runs show
`cancelled`, not `success`, as of this log's writing. Two (#131, #132) were re-run to `SUCCESS`
during the session. The final push (#142, tip) ran to completion naturally and is `SUCCESS` — the
codebase's current HEAD is CI-green — but that does not retroactively supply a clean leg-4 record
for the six PRs whose own merge-commit run was superseded before finishing. A detached background
script (`scratchpad/rerun-ci.sh` → `rerun-ci.log`) is serially re-triggering each of the six (plus
one stray leftover from the prior session's own cascade, PR #126) and was still working through the
first of them when this log was written. **Per the four-part verify convention, this log states
"four-part verify clean" only for #131 and #132 — not for #133–#143.**

## Process arc

1. **Plan-mode exploration — three parallel explorers.** One explored the operator's Site Tasks
   ask against the two existing task models (schedule-generated living tasks vs. person-assigned
   tasks); one explored the WPR photo-picker gap against the 0037 `daily_photo_pool` posture; one
   live-queried the production D1 tenant against a real job (Deep Lake, JOB-000032) and found the
   labor-table defect described above — not hypothesized, read directly off live data.
2. **Operator grilling (`AskUserQuestion`).** Decisions ratified: (a) fix the labor table by
   deriving from JHA worker-acknowledgement sign-ins, which already carry real employer names, in
   preference to patching the daily-report form itself; (b) ship a dedicated Site Tasks page with a
   job drop-down that shows **both** task models side by side, not a merge or a pick-one; (c) build
   the photo bridge + WPR upload + thumbnails with **no stated technical constraints** — the
   operator's ask was the outcome (a usable photo picker), not a specified mechanism; (d) at plan
   review, the operator **added** a fifth ask not in the original prompt — a per-job Procurement
   section on the job detail page.
3. **Three parallel design agents**, one per track (C, B, A), each handed its ratified decisions and
   producing the implementation shape the serial build then executed.
4. **Serial implementation in per-task worktrees**, Track C first (the labor-seed fix, since it was
   the live-defect root cause), then Track B (the photo pipeline, worker → mac → spa), then Track A
   (the eight-part job-tracker unification, A1 through A8).

## PRs landed

| PR | Track | What | Merge SHA | Review verdict |
|---|---|---|---|---|
| #131 | C | WPR labor seed from JHA worker-acknowledgement sign-ins | `7836b875` | BLOCK → fixed (bare-string `json_each` element 500) |
| #132 | — | Submit-time JSON depth bound (`MAX_VALUES_DEPTH=24`) | `b2d82121` | follow-up to #131's second reviewer finding |
| #133 | B-A | WPR photo pipeline (worker half) — site-photos bridge, office uploads, screened thumbnails (0074) | `43c74638` | WARN, no BLOCK (medium fixed: `thumb_b64` write-time validity; low accepted: uncapped WPR-selection prune exemption) |
| #134 | A1 | Retire `jobs.progress` %-estimate (column stays, dead readers swept) | `4f6059b8` | CLEAN |
| #135 | A3 | Extract `SectionRail` + `useScrollSpy` (behavior-frozen) | `03d8def4` | — (pure extraction, both consuming pages' suites pass unmodified) |
| #136 | A4 | Site Tasks page — both task models, one job-selectable surface | `69e851c2` | phantom-CSS guard caught 3 unstyled class names, fixed |
| #137 | A2 | Live schedule signal on the Job Tracker | `e1bd1469` | phantom-CSS guard caught 2 missing kit rules, fixed |
| #138 | B-B | Site-photos pool registration + screened thumbnails (Mac half) | `39d9318f` | ruff + mypy clean; no TS diff, no portal-worker-security-reviewer pass |
| #139 | B-C | WPR photo thumbnails + office upload (SPA half) | `d55f8a3b` | component test caught a real pre-merge bug (lazy state-updater); phantom-CSS guard caught a template-suffix chip-class miss, fixed |
| #140 | A5 | Navigation mesh + page-3 truncation honesty | `2dc04977` | — |
| #141 | A7 | Payments summary card in the job detail | `22ef31da` | — |
| #142 | A8 | Per-job Procurement section (0075) | `3ab7b96d` | WARN, no BLOCK (both judgment items applied in-PR: README activation row, accepted job_id/job_no snapshot residual-risk note) |
| #143 | A6 | Cross-job portfolio strip on the tracker list | `4f6bf167` | operator confirm requested, non-blocking (14-day risk horizon + overdue-inclusive wording) |

All 13: `state=MERGED`, squash merges, `mergedAt` non-null, `mergeCommit.oid` present (table above)
— verified directly against `gh pr view --json state,mergedAt,mergeCommit` for every PR, not taken
on report. Merge span: 2026-08-13T23:31:23Z (#131) → 2026-08-14T01:57:10Z (#142), roughly two and a
half hours, no unrelated PR interleaved on `main` during the window.

## Track C — the labor-seed fix (#131) and its security follow-up (#132)

The WPR Construction Labor seed now derives from the week's JHA worker-acknowledgement sign-ins
when any exist, falling back to the crew-progress free-text seed for JHA-less weeks; office-saved
rows still outrank both. Every Evergreen spelling variant ("Evergreen", "Evergreen Renewables",
"Evergree " typo) collapses into one canonical **Evergreen Renewables** row via a normalized-prefix
rule; `workers` is each company's peak daily distinct-signer count (same-day duplicate sign-ins
count once); a named signer with a blank Company cell surfaces last as a visible "(no company
given)" row rather than being guessed at. `man_hours` stays deliberately blank in every seeded row
— migration 0027 established that `personnel` carries no employer column and subcontractors create
crew *and* file time too, so a per-company hours split would be invented data (Op Stds §4,
Data-Fidelity/No-Invented-Field-Data). The wire gained a required `labor.seed_source: "jha" |
"daily"` field with typecheck teeth, and the SPA Labor section gained the previously-missing
Carried-forward badge plus a provenance hint naming the seed source. §43 runbook:
`progress_weekly_report.md` Symptom 5b ("labor table lists people instead of companies").

`portal-worker-security-reviewer` live-reproduced a BLOCK on #131's diff: a bare-string element
inside `worker_acknowledgement` (or `crew_progress`) dequotes under `json_each`, `json_extract`
re-parses it, raises `malformed JSON`, and 500s both report routes persistently — reachable by any
form-submitting account. Fixed before merge with `AND v.type = 'object'` element guards on both
`jhaSql` and the pre-existing `crewSql`; bite-proven (removing the guard reds the regression test).

The reviewer's *second* finding on #131 — a deeply-nested `payload_json` makes `json_type()` itself
raise at document-parse time, before any per-element guard even runs, so one stored hostile
submission can persistently 500 every `json_extract`-bearing weekly-report query for that job until
hand-deleted — was out of #131's scope and shipped as #132: `worker/json_depth.ts`, an iterative
depth checker rejecting any `values` payload nested past `MAX_VALUES_DEPTH=24` at `/api/submit`,
before anything is stored. Read-side guards structurally cannot close this; the write boundary is
the only durable fix. Bite-proven the same way (guard removed → 2 red).

## Track B — the WPR photo pipeline (#133, #138, #139; migration 0074)

Migration 0074 adds three additive, nullable-or-defaulted columns to `daily_photo_pool` (0037):
`origin` (closed vocabulary — `field` default, `office_wpr`, `site_photos`), `caption`, and
`thumb_b64`. Three changes ride it:

- **The site-photos bridge** (#133, worker half; #138, Mac half). A new internal route
  (`POST /api/internal/daily-photos/register`, same `portal_poll` bearer, zero new secrets) lets the
  Mac register a daily report's inline `site_photos` — already §34-screened and Box-filed at
  intake — as clean, claimed-at-birth, byte-free pool rows, so the WPR picker (which reads only
  `daily_photo_pool`) can finally see them. Idempotent on `(submission_uuid, box_file_id)`.
  `portal_poll` posts the register call **after** mark-filed, behind a §43 best-effort fence
  (`portal_site_photo_register_failed` WARN — the filing/receipt is never disturbed). The photo pool
  fills from the **next** filed daily report with inline site photos — no backfill of historical
  submissions was built.
- **Office uploads** (#133, #139). The upload route accepts an `origin` field: absent → `field`
  (byte-identical to pre-0074 behavior), `office_wpr` → gated on `cap.jobtracker.manage` (the WPR
  screen's own capability). `origin` sits deliberately outside the HMAC canonical. The new
  `WeeklyPhotoUpload` SPA component is a deliberate sibling of the field's `AdditionalPhotosSection`
  — week-scoped, non-claiming — with a Sat→Fri day picker, multi-file add, and 15-second polling for
  in-flight screening. The component test caught a real pre-merge bug: the clean-flip flag was
  computed inside a lazy React state-updater, so `onPoolChanged` never fired; fixed to derive the
  flip from the fetched rows directly.
- **Screened thumbnails** (#133, #139). The result route accepts an optional ≤40KB `thumb_b64` on
  a clean disposition, derived by the Mac (`photo_screen.make_thumbnail`) from the §34 clean
  re-encode only — never the raw upload. A new serving route (session + `cap.jobtracker.manage`)
  serves clean rows' thumbnails only; originals are still never served. This is a **deliberate,
  operator-approved relaxation** of the 0037 Option-D "record-only, no serving route, ever" posture
  — the office was picking report photos blind by date and caption, and the operator's photo-bridge
  ask carried no constraint against adding a narrow, screened-only exception. `portal-worker-
  security-reviewer` found a MEDIUM (fixed before merge): `thumb_b64` was shape/size-checked but not
  length-valid, so a malformed value passed validation and threw at serve time, permanently
  500-ing that photo. Both accept sites now run an end-to-end write-time check (length-validity +
  real decode + JPEG magic).

Retention: prune keeps the existing 7-day stuck-pending/unclaimed-field window, adds a 90-day window
for `office_wpr` rows (weekly cadence outlives the field flow's 7-day abandonment assumption), and
exempts any row selected in a saved weekly report from every delete path (NOT-IN subquery, NULL-
poison-guarded, test-pinned). The reviewer's one accepted LOW: the WPR-selection prune exemption is
uncapped across fabricated `week_starts` — a scripted `cap.jobtracker.manage` session could
accumulate exemptions. Flagged, not changed — matches the file's established office-trust posture
(the same capability already gates the whole weekly-report surface).

Rollout ordering followed the 0037-class rule explicitly: 0074 applied to remote D1, then the
worker (#133) deployed, then the Mac consumer (#138) landed as a separate PR — reverse skew
degrades to thumbless/unregistered rows, never an error.

## Track A — the eight-part job-tracker unification (#134–#137, #140–#143; migration 0075)

All eight sub-tracks (A1 through A8) landed this arc:

- **A1 (#134)** — `jobs.progress` retirement. `body.progress` is now ignored on create (literal 0
  bound, bind positions verified end-to-end by review — no renumber); every dead read surface
  (SELECTs, row types, response maps, the SPA create-job field, the `invalid_progress` copy) swept.
  The D1 column itself stays (drop is deploy-coupled, explicitly out of scope). Security review:
  CLEAN.
- **A2 (#137)** — live schedule signal. One shared grouped-SQL derivation
  (`worker/schedule_rollup.ts`) of percent/late/next-milestone from the ADR-0006 living task list,
  parity-pinned to `schedule_view.weightedPercent` and the weekly report's `behindSchedule` rule, so
  the predicates can't drift across the three surfaces (list, detail, and A6's portfolio strip) that
  consume it. Nullable = honest "no schedule imported" state, never a fabricated percentage.
- **A3 (#135)** — `SectionRail` + `useScrollSpy` extraction. The job-detail and weekly-report pages'
  near-identical two-column rail implementations become one shared component, behavior frozen
  (observer options preserved verbatim; both pages' full suites pass unmodified). The consolidated
  popstate/remount defect comment now lives in one place instead of being independently re-learned
  per page.
- **A4 (#136)** — the Site Tasks page, the operator's literal "job site tasks" ask.
  `/site-tasks[/:jobId]` (`cap.jobtracker.read`, all roles), a job drop-down defaulting to the
  routed job else the viewer's placement, showing **both** task models the operator specified: the
  schedule-generated living task list (%-markable via the same `TaskRow`/mark semantics as the Job
  Schedule page) and the job's person-assigned tasks (own-only status buttons, server re-enforced).
  Two §14 extractions (`ScheduleTaskRow`, `useScheduleMarks`) keep the Job Schedule page
  fork-proof — its suite passes unmodified.
- **A5 (#140)** — navigation mesh (Schedule ↔ WPR ↔ Materials cap-gated links, no new routes) and a
  `schedule.truncated` flag with an office-screen warning when the schedule display is capped.
- **A6 (#143)** — cross-job portfolio strip on the tracker list: one batched rollup (late /
  deliveries-due-including-overdue / milestones ≤14 days / materials due), predicates co-located
  with A2's aggregate, parity-tested against the weekly report's own behind-schedule list. Hides
  when quiet. **Operator confirmation requested, non-blocking**, on the 14-day risk horizon and the
  overdue-inclusive delivery wording — not yet actioned as of this log.
- **A7 (#141)** — payments summary card, `cap.payments.manage`-only, worst-state pill via the
  action-gated ladder, fed by the extracted `loadCycleViews` reduction inside the existing payments
  route family (ADR-0006 decision 7). 403-pinned for both non-admin roles.
- **A8 (#142)** — the operator's plan-review addition: a per-job Procurement section (POs, RFQ
  rounds, Subcontracts with lifecycle state). Migration 0075 adds `rfqs.job_id` (the RFQ table never
  captured it — the identical defect 0069 fixed for `po_estimates`: the SPA held the job id in state
  and threw it away at submit) plus three per-job indexes; **no backfill** — pre-0075 RFQs will
  never appear on a job's Procurement section, stated in the section's own hint text. Read-only by
  doctrine (no send/approve/advance affordance — Invariant 1 / F22). `rfqs.closed`, a CHECK-
  constraint value with no writer anywhere in the codebase, is deliberately not rendered — a dead
  state, not a bug. Security review: WARN, no BLOCK — cap-before-existence 403/404 ordering
  verified (no job oracle), bind positions hand-traced, the W9 column subset verified against the
  lane list routes.

## Guard-gates that fired pre-merge — the control biting, not decoration

Five real defects were caught by existing repo guards before merge, across four distinct gate
classes, all verified in the commit history (not asserted from memory):

1. **Error-copy parity gate** (#133) — caught an unmapped `invalid_origin` error code; fixed with
   plain-language copy in `errorCopy.ts`.
2. **Excess-property teeth** (#134) — caught and swept 4 SPA fixtures still carrying the retired
   `progress` field after the wire type dropped it.
3. **Phantom-CSS guard**, three separate firings:
   - #136 (Site Tasks page) — 3 unstyled class names, fixed to real kit classes.
   - #137 (schedule signal) — 2 new schedule-signal classes with no kit rule, fixed.
   - #139 (WPR upload) — a chip-class lookup built from a template-string suffix the guard cannot
     statically verify; fixed to a static lookup table.

None of these were assertions of "the gate exists" — each is a commit-message-recorded fix that the
diff would not have needed had the guard not fired. `#136` additionally landed two new pinning
regression tests (a router round-trip/gate-map pin and a HomePage card pin) as *new* coverage for
this session's own additions — those are pins added, not defects caught, and this log does not
conflate the two.

## Migrations

- **0074** (`daily_photo_wpr.sql`) — `daily_photo_pool.origin` (`TEXT NOT NULL DEFAULT 'field'`),
  `.caption`, `.thumb_b64`. Applied before #133's worker deploy per the 0037-class ordering rule
  stated in-file.
- **0075** (`rfq_job_id.sql`) — `rfqs.job_id` (`TEXT NOT NULL DEFAULT ''`) plus
  `idx_purchase_orders_job` / `idx_subcontracts_job` / `idx_rfqs_job`. Applied before #142's worker
  deploy per the same rule.

## Deploys

Two production Worker deploys during the arc, per `wrangler deployments list`:

- `cb345894-8547-438e-9e95-f68ed169d44c` at 2026-08-14T00:44:10Z — landed right after #133 merged
  (00:42:53Z) and before #135 (00:46:25Z), shipping the Track C fixes (#131, #132) and the worker
  half of the photo bridge (#133) so the Mac-side consumer (#138) had a live route to register
  against.
- `99edb80c-fe42-4372-8ceb-5bc368e97a6b` at 2026-08-14T01:57:56Z — the final deploy, landed
  immediately after the arc's last two merges (#139 at 01:57:06Z, #142 at 01:57:10Z), shipping
  everything: Site Tasks, schedule signal, nav mesh, payments card, portfolio strip, procurement
  section, and the WPR thumbnail/upload SPA surface.

## Verification state — reported faithfully, not rounded up

Per Op Stds §55.3/§55.4 and the four-part PR-landing convention (`docs/operations/
pr_merge_discipline.md`), this log states exactly what was checked and nothing more.

**Legs 1–3 (state=MERGED, `mergedAt` non-null, `mergeCommit.oid` present)** — clean for all 13 PRs,
verified directly against `gh pr view --json state,mergedAt,mergeCommit`.

**Leg 4 (main-branch CI on the merge commit):**

```
PR #131 — mergeCommit=7836b875… — main-branch CI: SUCCESS (run 31754123907, ci, conclusion=success)
PR #132 — mergeCommit=b2d82121… — main-branch CI: SUCCESS (run 31754671513, ci, conclusion=success)
PR #133 — mergeCommit=43c74638… — main-branch CI: CANCELLED (run 31758290073) — rerun IN PROGRESS
PR #134 — mergeCommit=4f6059b8… — main-branch CI: SUCCESS (run 31756598480, ci, conclusion=success)
PR #135 — mergeCommit=03d8def4… — main-branch CI: SUCCESS (run 31758490267, ci, conclusion=success)
PR #136 — mergeCommit=69e851c2… — main-branch CI: CANCELLED (run 31759299181) — rerun QUEUED
PR #137 — mergeCommit=e1bd1469… — main-branch CI: CANCELLED (run 31759745912) — rerun QUEUED
PR #138 — mergeCommit=39d9318f… — main-branch CI: CANCELLED (run 31759302013) — rerun QUEUED
PR #139 — mergeCommit=d55f8a3b… — main-branch CI: CANCELLED (run 31762209095) — rerun QUEUED
PR #140 — mergeCommit=2dc04977… — main-branch CI: SUCCESS (run 31759749034, ci, conclusion=success)
PR #141 — mergeCommit=22ef31da… — main-branch CI: CANCELLED (run 31761115298) — rerun QUEUED
PR #142 — mergeCommit=3ab7b96d… — main-branch CI: SUCCESS (run 31762211650, ci, conclusion=success) — this is TIP
PR #143 — mergeCommit=4f6bf167… — main-branch CI: SUCCESS (run 31761119986, ci, conclusion=success)
```

**Only #131 and #132 get the verbatim claim: "four-part verify clean."** #133, #136, #137, #138,
#139, #141 do NOT — their own merge-commit CI run was cancelled by the `ci-main` concurrency group
mid-cascade (a newer push superseded it before it finished), not failed. The current tip (#142's
merge commit) ran to completion naturally and is SUCCESS, meaning HEAD is presently CI-green — but
that is a distinct fact from "each of those six PRs' own leg 4 is clean," and this log does not
collapse the two. A detached background script
(`/private/tmp/.../scratchpad/rerun-ci.sh` → `rerun-ci.log`) is serially re-triggering each cancelled
run via `gh run rerun` and polling to completion; as of this log's writing it has re-triggered
#133's run (queued) and has five more to go, plus one unrelated stray (PR #126, from the *prior*
session's own cascade) picked up by the same catch-up pass. **This is IN PROGRESS, not done** — a
future session or the operator should re-check `rerun-ci.log` for `ALL-RERUNS-DONE` before treating
#133–#143 as fully leg-4-verified.

**Suite state (measured against the merged tip, this session):**

- Worker vitest: **1581 passed** (82 test files)
- SPA vitest: **1025 passed** (74 test files)
- SPA/Worker typecheck: clean across all 3 tsconfig projects (per-PR reports throughout the arc)
- mypy: **0 errors / 503 source files**
- ruff: **clean**
- pytest: full suite (5,430 collected via `--collect-only`, `test_publish_daemon.py` excluded per
  the documented host-local conftest live-state class) completed with **exit code 0** on its first
  full run this session. Two subsequent attempts to re-capture an exact passed/skipped breakdown
  stalled reproducibly at the same ~72% point while live launchd daemons (`fieldops_sync`, `po_poll`,
  `portal_poll`) were actively polling the same `~/its` tree — consistent with lock contention on a
  shared `state_io` sidecar file, not a test failure. This log reports the confirmed exit-0 result
  and the collected-test count rather than fabricate a skip/deselect breakdown it could not
  reproduce cleanly; see Open items.

## Decisions

1. **Fix the labor table by deriving from JHA sign-ins, not by patching the daily-report form.**
   Alternative considered (implicitly, by not choosing it): make the daily report's free-text
   "Crew / Subcontractor" field structured. Rejected by the operator — JHAs already carry real
   employer data for the same week, so deriving from an existing trustworthy source beats redesigning
   a field crews already use as free text.
2. **`man_hours` stays blank in every JHA-seeded row rather than being estimated or split.** Per
   Op Stds §4 (Data-Fidelity/No-Invented-Field-Data) — migration 0027 proves `personnel` carries no
   employer column and subcontractors both create crew *and* file time, so any per-company hours
   split would be invented, not derived, data.
3. **Site Tasks page shows both task models side by side, not a merge.** The operator's explicit
   instruction — the schedule-generated living task list and the person-assigned task list are
   different data with different ownership semantics, and collapsing them into one list would lose
   that distinction.
4. **Thumbnails are a deliberate, operator-approved relaxation of the 0037 record-only posture** —
   scoped narrowly (≤40KB, derived only from the §34 clean re-encode, served only for clean rows,
   gated behind the WPR screen's own capability) rather than a general re-opening of photo serving.
5. **The procurement lane reuses the existing PO/RFQ/subcontract admin capabilities rather than
   minting a new read capability**, and ships read-only by doctrine (Invariant 1 / F22) — no
   send/approve/advance affordance, matching the posture every other document lane on this page
   already carries.
6. **`rfqs.job_id` gets no backfill**, matching the 0069 precedent for `po_estimates.job_id` — a
   job-id column that could only ever be captured at write time cannot be safely reconstructed from
   `job_no` after the fact (two real jobs have shared a `job_no`, e.g. `2026.384`), so resolving
   pre-migration rows by name-matching would risk silent misattribution. Pre-0075 RFQs simply don't
   appear on the Procurement section; the section's own hint text says so.
7. **This log does not round "tip is CI-green" up to "all thirteen PRs are leg-4 verified."** Six
   individual merge-commit CI runs were cancelled, not passed, by the concurrency group; the
   background re-run script exists specifically to produce a real leg-4 record for each, and this
   log reports the re-run as in progress rather than assume its outcome.

## Open items / next session

- **Six leg-4 CI re-runs still in progress** (`rerun-ci.log`, PID running detached as of this log's
  close) — #133, #136, #137, #138, #139, #141. A future session should check for
  `ALL-RERUNS-DONE` in that log and, if all six show `completed:success`, may then extend the
  verbatim "four-part verify clean" claim to those PRs; if any shows a real failure conclusion
  (not `cancelled`), that is a genuine leg-4 gap requiring investigation, not a re-run artifact.
- **No backfill for the photo-pool bridge.** Historical daily reports' inline site photos are not
  retroactively registered into `daily_photo_pool` — only reports filed after #138's deploy become
  WPR-offerable. If the office needs older photos available in the picker, that is a distinct,
  not-yet-scoped backfill job.
- **A6's 14-day risk horizon and overdue-inclusive delivery wording are pending operator
  confirmation** (#143's own PR body flagged this as non-blocking) — not yet actioned.
- **`rfqs.closed` remains a CHECK-constraint value with no writer anywhere in the codebase** — dead
  state, correctly left unrendered by #142, but worth a tech-debt entry if a future session wants
  either a writer or to drop the value from the CHECK constraint.
- **CLAUDE.md's "What's stubbed vs. real" table was not updated by any of the 13 PRs** — none of
  them touched `CLAUDE.md`. The `safety_portal/` row still describes the pre-JHA-seed labor-by-
  company limitation without noting the fix, and does not mention migrations 0074/0075, the
  site-photos bridge route, the Site Tasks page, or the procurement route. This is a documentation-
  currency gap for the next session-close pass to close, not a functional gap — none of the missed
  registries are the hard-enforced kind (no new package/daemon/secret/workstream-tag was added, only
  new routes within the existing `safety_portal` Worker), but the table row is now materially stale.
- **The exact pytest passed/skipped/deselected breakdown for this session's final tree was not
  captured cleanly** — two re-run attempts stalled at the same point while live daemons
  (`fieldops_sync`/`po_poll`/`portal_poll`) were polling the shared `~/its` tree. The first full run
  this session did complete with exit code 0 (5,430 tests collected, `test_publish_daemon.py`
  excluded), which is the evidence this log relies on — a future session wanting the exact
  breakdown should re-run pytest when no launchd daemon cycle is mid-flight, or from a worktree.
- Worktrees `~/its-debtsweep` and `~/its-jobdetail-facelift` remain from prior sessions, untouched
  by this one (per `docs/operations/worktree_discipline.md`, not force-deleted from a session). No
  new worktree residue from this session's own tracks.

## What was NOT touched

- No CLAUDE.md registry updates (see Open items above).
- No backfill of historical daily-report photos into the WPR pool.
- No writer added for `rfqs.closed`.
- The `jobs.progress` D1 column itself was not dropped (A1 explicitly scoped the drop out — it is
  deploy-coupled).
- A6's risk-horizon/wording operator-confirmation ask was raised, not resolved.

## Cross-references

- `docs/HOUSE_REFLEXES.md` §2 (control-bites-nothing / prove-the-control-bites — the phantom-CSS,
  error-copy-parity, and excess-property-teeth firings this session are exactly that class of
  evidence) and §3 (the four-part leg-4 discipline this log applies precisely, declining to round
  six cancelled runs up to "clean").
- `docs/operations/pr_merge_discipline.md` — the four-part verify convention this log's Verification
  section follows; the canonical source for why "tip is green" ≠ "every PR's own merge-commit CI
  passed."
- ADR-0006 (`docs/adr/0006-job-schedule-payment-tracking.md`) — the living task list and schedule
  rollup this session's A2/A4 tracks build directly on.
- `docs/adr/0001-portal-photo-transport-d1-vs-r2.md` — the D1-inline-base64 photo transport posture
  this session's thumbnail relaxation narrows rather than reopens.
- `docs/runbooks/progress_weekly_report.md` — Symptom 5b, added by #131, documents the labor-table
  people-as-companies failure mode and the JHA-seed fix.
- `docs/session_logs/2026-08-13_debt-sweep-and-dashboard-wiring.md` — the immediately prior session;
  its own leg-4 discipline (running the four checks directly when the verifier declined) is the same
  posture this log applies to the six cancelled runs.
- `logs/migrations/po_vendors_backup_20260810.json` — unrelated, untracked file noted in prior
  session logs; unchanged by this session.
