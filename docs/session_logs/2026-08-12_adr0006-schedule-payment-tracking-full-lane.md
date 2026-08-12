---
type: session_log
date: 2026-08-12
status: closed
related_prs: [80, 84, 85, 90, 91, 92, 93]
workstream: field_ops
tags: [session_log, adr-0006, schedule, ocr, gantt, payments, reconcile, adversarial_review, gate_activation, live_e2e]
---

# Session log — 2026-08-11 → 2026-08-12 · ADR-0006 Job Schedule / Progress + Payment tracking — full lane, planned through live E2E

## Summary

One complete workstream lane, start to finish, in a single session arc: the operator was
grilled (`AskUserQuestion`) against a 32-document corpus survey of real Evergreen project-schedule
exports, ratified eight decisions recorded in `docs/adr/0006-job-schedule-payment-tracking.md`,
and the lane was then built as seven PRs (PR-1 through PR-7 of the ADR), each adversarially
reviewed before merge, each landing four-part verify clean. The gate
(`field_ops.schedule_poll.polling_enabled`) was flipped live on the production tenant and the
`org.solutionsmith.its.schedule-poll` plist loaded during this same arc, and the full lane —
upload → sandboxed OCR → validate → commit → field mark-off → revision reconcile → payment
state machine — was exercised end-to-end against mirror job **JOB-000031**. This is the first
ADR in the corpus to go from "operator grilled" to "live-validated on the production tenant" in
one continuous session.

## PRs landed

| PR | Slice | Merge SHA | Adversarial review | Verify |
|---|---|---|---|---|
| #80 | PR-1 — intake pool: migration 0066 `job_schedules` (superseded status, one-committed partial UNIQUE), `fieldops_schedules.ts`, `schedule:v1` HMAC both sides + cross-domain parity proofs, dedicated bearer `PORTAL_SCHEDULE_API_TOKEN` | `5dba0c5` | portal-worker-security-reviewer: **CLEAN** | four-part verify clean |
| #84 | PR-2 — OCR/geometry/parse core: `estimate_sandbox.ocr_page_words` (Vision runs **inside** the sandbox child; rotation ladder — Vision silently drops 90°-rotated text, measured 64→135 words on one page; Task-Name-position layout invariant picks true orientation; date-token scoring), `schedule_ocr`/`schedule_geometry`/`schedule_parse`, 32/32 corpus qualification, an AST dispatch-parity test RED-proven before the real branch existed | `fa8c588` | ops-stds-enforcer: **BLOCK** (raw customer schedule text in committed fixtures) → fixed before merge: fixtures ANONYMIZED at capture time (ADR amendment below) + §42 canonical headings added | four-part verify clean |
| #85 | PR-3 — `schedule_poll.py` daemon + full registry enrollment in the same PR (transient-fence / capability-gating / state-write meta-tests RED-proofed unenrolled first) | `01bef5e` | ops-stds review: **WARN** (family-wide §30 gap — internal-pool `portal_client` wrappers have no live-integration smoke, across manifest/estimate/RFQ/schedule alike — logged to tech-debt, not schedule-specific) | four-part verify clean |
| #90 | PR-4 — living task list (`job_schedule_tasks`, migration 0071) + validate screen + `/jobs/:jobId/schedule` | `41a4720` | portal-worker-security-reviewer: **2 BLOCKs**, both fixed before merge (below) | four-part verify clean |
| #91 | PR-5 — field mark-off: `cap.schedule.mark` (migration 0072), %-chips, milestone-done, delivered marks | `58be312` | portal-worker-security-reviewer: **WARN**, no BLOCK (TOCTOU on milestone-binary percent fixed in-WHERE, SQL-level-proven) | four-part verify clean |
| #92 | PR-6 — revision reconcile: `schedule_diff.ts` pure engine, three-way percent, blocking removals, rename-linking, `ScheduleReconcilePage` | `637e591` | portal-worker-security-reviewer: **WARN**, no BLOCK (three-way percent decided by an atomic CASE re-reading `last_marked_by` AT WRITE TIME, fixed and SQL-mirror-tested) | four-part verify clean |
| #93 | PR-7 — payments: migration 0073, `cap.payments.manage` (admin-only), `payments_derive.ts` pure state machine, terms/cycles/receipts | `1badba7` | Security review: **WARN → fixed** (README 0073 punch-list row); suspend-guard RED-proven by temporarily removing the in-WHERE nonpayment-notice check | four-part verify clean |

**All seven PRs — four-part verify clean.** Independently re-verified against live GitHub for
this log (`gh pr view --json mergedAt,mergeCommit,state` + `gh run list --branch main --commit
<sha>`), not taken on report:

```
PR #80 — state=MERGED · mergedAt=2026-08-12T00:13:36Z · mergeCommit=5dba0c5… · main-branch CI on merge commit: SUCCESS (ci, conclusion=success)
PR #84 — state=MERGED · mergedAt=2026-08-12T01:29:44Z · mergeCommit=fa8c588… · main-branch CI on merge commit: SUCCESS (ci, conclusion=success)
PR #85 — state=MERGED · mergedAt=2026-08-12T02:31:48Z · mergeCommit=01bef5e… · main-branch CI on merge commit: SUCCESS (ci, conclusion=success)
PR #90 — state=MERGED · mergedAt=2026-08-12T05:10:18Z · mergeCommit=41a4720… · main-branch CI on merge commit: SUCCESS (ci, conclusion=success)
PR #91 — state=MERGED · mergedAt=2026-08-12T06:24:56Z · mergeCommit=58be312… · main-branch CI on merge commit: SUCCESS (ci, conclusion=success)
PR #92 — state=MERGED · mergedAt=2026-08-12T07:37:08Z · mergeCommit=637e591… · main-branch CI on merge commit: SUCCESS (ci, conclusion=success)
PR #93 — state=MERGED · mergedAt=2026-08-12T08:40:25Z · mergeCommit=1badba7… · main-branch CI on merge commit: SUCCESS (ci, conclusion=success)
```

Merge timestamps run 2026-08-11 17:13 Pacific (PR #80) through 2026-08-12 01:40 Pacific (PR #93)
— one continuous overnight arc, no gap PR from another lane interleaved (`b210053`/`66d6758`/
`4e0fe04`/`3ecc4bd` between #90 and its neighbors on `main` are the Weekly-Production-Report and
PO-lane arcs closing out in parallel, not part of this chain).

## The corpus survey and the eight ratified decisions

The design phase read all 32 real Evergreen project-schedule PDF exports at
`~/Desktop/PJCT SCHDLS` (10 jobs, up to 14 dated revisions per job — Bonacci alone has 14) before
any code. Two findings shaped the whole lane: **31 of 32 PDFs have no text layer** (vector glyph
outline text, ~2,600–3,400 path objects per page, under 110 real characters — title/footer only),
which rules out deterministic PDF-text extraction and makes render+OCR mandatory; and **Apple
Vision OCR works but misreads digits at full confidence** (`12/01/25` → `72/01/25` observed),
which rules out confidence-score filtering and makes the human validate screen the only fidelity
control — the same posture ADR-0004's red-team finding #2 established for the estimate lane,
inherited verbatim rather than re-derived.

The operator was grilled via `AskUserQuestion` against this evidence and ratified eight decisions
(ADR-0006 §"Operator decisions," all final, 2026-08-11): (1) intake is PDF-upload + OCR + validate
— not XLSX, not direct Smartsheet read; (2) progress is portal mark-off + revision reconcile,
portal % preserved unless a human explicitly takes the revision's value; (3) payments ship as
terms + cycles + derived-state display only — the reminder engine is a later fold-in, and any
future notice document rides the External Send Gate, permanent, no exception; (4) schedule
visible to all roles, payment section admin-only; (5) "delivery" means both payment receipts and
a distinct Deliveries-phase task flag; (6) payment terms attach per JOB, prefilled from the same
client's most recent job; (7) fresh start — no corpus backfill, the corpus PDFs become fixtures
only; (8) mark-off is quick-% chips (0/25/50/75/100 + exact) plus a done-checkbox for milestones.

## Non-obvious decisions

1. **PDF-only MIME, not XLSX.** Decision 1 above; recorded explicitly in the ADR's Consequences
   section as a deliberate narrowing — a future XLSX intake widens the allowlist as its own
   decision when the parser can read a Smartsheet XLSX export, not assumed now.
2. **`superseded` joins the pool status machine — the lane's one status divergence from the 0060
   manifest-lane precedent.** Revisions are the norm (not the exception) for this corpus, so
   exactly one governing schedule per job is enforced by a partial UNIQUE
   (`WHERE status='committed'`), and the final commit page flips the prior governing row
   committed→superseded FIRST in the same batch. A superseded row leaves the per-job exact-sha
   dedupe index — re-uploading a displaced revision's exact bytes IS the rollback path for a
   wrong-file commit, deliberately reusing dedupe as the recovery mechanism rather than building a
   separate undo.
3. **Office schedule surfaces ride the existing `cap.jobtracker.manage`** (the 0060
   ride-an-existing-cap precedent — admin-only in practice, since manager is withheld it per
   decision 0023) rather than minting a new capability for read/manage. Two genuinely NEW
   capabilities were minted only where the existing set had no analog: `cap.schedule.mark`
   (submitter+manager+admin, per-job scoped, PR-5) for field progress marks, and
   `cap.payments.manage` (admin only, PR-7) because payment data is commercially sensitive and
   appears in no other route's response.
4. **Vision OCR runs INSIDE the sandbox child — spiked and proven the same day, not deferred.**
   The spike (ocrmac under RLIMIT_CPU + RLIMIT_AS) returned identical results to an in-process
   call, so this lane carries **no** ADR-0004 §Vision deviation: both the hi-DPI render and the
   OCR pass on hostile bytes are subprocess-isolated, same posture as every other byte-handling
   step. New sandbox entry points got real dispatch branches rather than falling through to the
   allocation-bomb landmine branch — an AST dispatch-parity test was written and RED-proven
   (a branchless allowlist name fails naming itself) before the real branch existed.
5. **The rotation ladder plus a layout-invariant orientation picker**, not a single-pass OCR call.
   Vision silently drops most 90°-rotated text (measured 64 vs 135 words on the same page) and
   reads 180°-flipped text well enough that confidence alone can't distinguish it from correct —
   only the Task-Name-position layout invariant (where the task-name column sits relative to the
   date columns) reliably picks the true orientation across both the grid-view and Gantt-view
   export styles the corpus contains.
6. **Committed OCR fixtures are ANONYMIZED captures, not raw ones — an ADR amendment forced by
   adversarial review, not planned going in.** PR-2's `ops-stds-enforcer` review BLOCKED on real
   customer schedule text (project/client names, task lists, dates) riding into permanent git
   history, which contradicted the sibling manifest/estimate lanes' explicit
   no-customer-content-in-fixtures precedent — even though the ADR's decision 7 ("the corpus PDFs
   become test fixtures") had gestured toward committing raw captures. Resolution taken as the
   conservative reading: client/project identifiers are SUBSTITUTED at capture-write time
   (Coker→Kestrel, KSI→Acme, Bonacci→Baseline, Deeplake→Clearlake, …) while the Vision geometry,
   the industry-standard task vocabulary, the real dates, and the real misread patterns
   (`12125125`) survive unchanged — those are what the tests actually exercise, and none of them
   identify the customer's projects. Anonymization is baked into
   `capture_schedule_fixtures.py`'s `_ANONYMIZE` map at write time, not a one-off edit, so a
   recapture cannot regress it. Reversing this to commit raw captures would take its own explicit
   recorded operator decision.
7. **Baseline immutability + `last_marked_by` as the reconcile predicate, not a stored progress
   log.** `baseline_start`/`baseline_finish` stamp at each task's own FIRST commit and are never
   rewritten (the slip-measurement anchor); there is no dedicated progress-events table —
   `audit_log` rows (from/to in detail) are the history. `last_marked_by IS NOT NULL` is the
   single predicate distinguishing a portal-marked task from one whose percent only ever came from
   a schedule import, and it is what PR-6's three-way reconcile percent logic keys on
   (`rev == schedule_percent → keep portal; last_marked_by NULL → take revision; else CONFLICT,
   default keep portal`) — decided by an atomic CASE re-reading `last_marked_by` AT WRITE TIME
   after the PR-6 review flagged a window where a mark landing mid-reconcile could otherwise be
   clobbered.
8. **Payment state is DERIVED, never stored — decision 10, held through implementation.**
   `job_payment_cycles` rows are manual (no auto-cadence generation) with a stored
   server-computed `due_date` snapshot, and `job_payment_receipts` is append-only; every visible
   state (draft/awaiting/due_soon/overdue/nonpayment_notice_due/notice_sent/
   suspension_notice_due/suspension_sent/paid, plus paid_late/balance modifiers) comes from a pure
   function (`payments_derive.ts`, server `today` in Pacific) computed at read time. Notice clocks
   key off RECORDED notice dates only — the machine never pretends a notice went out, and the
   suspend action is hard-gated: it 409s without a recorded nonpayment notice, enforced in the
   `WHERE` clause (not just application logic) and RED-proven by temporarily removing that guard
   and confirming the write succeeded wrongly before restoring it.
9. **Gate activation proceeded 2026-08-12 under the operator's complete-testing directive, after
   reading the `field_ops.schedule_poll.polling_enabled` row's full Description** — the
   HOUSE_REFLEXES §5 discipline (a documented precondition in a gate's Description is a doctrine
   action, not an autonomous one) was followed rather than skipped: the row was fetched and its
   cells read before the flip, not just its row id.

## Adversarial-review findings fixed before merge (the two PR-4 BLOCKs)

Both found by `portal-worker-security-reviewer` on the diff itself, both fixed before #90's merge:

1. **Stuck-committing survives replay.** The commit's page batch and its finalize batch are two
   separate transactions; an interruption between them left a schedule at `status='committing'`
   with a maxed watermark, and the replay guard answered `done:true` on watermark alone — false
   success, permanently stuck. Fix: the finalize batch (supersede-first + commit, one batch,
   idempotent) now ALSO runs from the replay branch, so the client's own documented re-post
   repairs the stuck state; a regression test crafts the exact stuck state and proves the replay
   lands `status='committed'`. The reviewer separately noted `worker/fieldops_manifests.ts`'s
   `/commit` shares the identical shape — logged to `docs/tech_debt.md`, not fixed in this arc
   (open item below).
2. **Discard-mid-commit orphans tasks.** Discarding a schedule stuck in `committing` left its
   already-inserted tasks ACTIVE and attributed to a terminal schedule, and
   `countForeignActiveTasks` then refused EVERY future upload for that job with
   `revision_reconcile_not_available` — a permanent lockout with no reconcile path until PR-6 even
   existed. Fix: the discard batch now cascade-deactivates tasks whose `source_schedule_id` is the
   discarded row (safe by construction — committed/superseded rows are not discardable, so a
   governing schedule's tasks can never be swept), audits the count, and the validate screen's
   discard copy was corrected to match.

## Live end-to-end validation (mirror JOB-000031, production tenant)

- **Upload → OCR → validate → commit.** First real schedule import through the live daemon: 68
  rows recognized (`schedule filed: 'Project Schedule - KSI - Coker 8.5.26.pdf' ... rows=68`,
  05:27 UTC) → 62 tasks committed with baselines stamped, filed to Box
  `ITS Safety Reports/Test/Schedules`.
- **All six mark-off behaviors** exercised: %-chips, milestone done-checkbox, delivered date
  mark, regression (percent moving backward, allowed and audited), a milestone rejecting a
  non-{0,100} value, and an office `/:id/edit` percent edit correctly leaving `last_marked_by`
  NULL.
- **Revision reconcile**, a second real document imported for the same job (`schedule 2 ('Project
  Schedule - KSI - Coker.pdf', job JOB-000031) carries active content (L2:pdf_active_content:
  JavaScript) — proceeding per the 2026-08-11 disposition`, 07:50 UTC — the manifest lane's
  suspicious-warn carve-out proceeding live on the schedule lane's own §34 pass; filed at
  07:53 UTC, 69 rows recognized): the diff produced **23 new / 40 updated / 1 linked / 19
  removed**; portal's 50% survived unclobbered on the one linked-and-marked task (the
  `last_marked_by`-keyed three-way percent logic holding at real scale); **zero baseline
  violations**; the supersede transition held (old row committed→superseded before the new one
  became governing).
- **Payments full state-machine walk** (Worker+SPA only, no Mac-side daemon or log surface by
  design — decision 10): `nonpayment_notice_due` correctly triggered at 31 days past due; the
  suspend action correctly 409'd before a notice was recorded (the RED-proven guard holding live,
  not just in the unit test); `suspension_notice_due` → `suspension_sent` walked forward once the
  notice was recorded; a partial-payment receipt correctly derived a remaining balance;
  `paid_late` derived correctly once a late receipt closed the cycle; a fresh cycle correctly
  read `draft`.
- **`schedule.tester` account** was provisioned specifically for this E2E pass and disabled again
  once the walk completed — not left live on the production tenant.

## Open items / next session

- **The manifest lane's `/commit` replay guard carries the exact same finalize-gap PR-4's review
  found and fixed for schedules** (`docs/tech_debt.md`, "manifest commit's replay guard has the
  same finalize-gap the schedule lane just fixed," OPEN 2026-08-11, medium) — same shape, not yet
  applied to `worker/fieldops_manifests.ts`.
- **Family-wide §30 gap**: the internal-pool `portal_client` wrapper families (manifest, estimate,
  RFQ, weekly-production-report, and now schedule's six functions) have unit + Worker-side vitest
  coverage but no paired operator-run `-m integration` smoke driving the Python wrappers against
  the real deployed Worker (`docs/tech_debt.md`, OPEN 2026-08-11, medium). PR #85's review raised
  it as family-wide, not schedule-specific; proposed fix is one
  `tests/test_portal_client_pools_integration.py` walking each pool family, with enrolling a new
  pool family in it becoming part of the same-PR registry DoD going forward.
- **The alert engine is designed-for but not built** (ADR-0006 decision 12): behind-schedule,
  not-started slip, stall, baseline slip, delivery slip, contract-milestone risk, and
  payment-reminder states are all new code over the schema landed this arc, with a future
  dedicated `PORTAL_ALERTS_API_TOKEN` read tier. No schema change is required for it to arrive.
- **Weekly Production Report page 3 (Construction Progress / Delays)** was an empty state
  ("No schedule imported for this job") when this arc opened, because `job_schedule_tasks` did
  not exist yet (`docs/tech_debt.md`, OPEN 2026-08-11, low). PR-4 (#90) landed the table this
  session; the report-side read (`buildReportData` grouping by section with
  `percent_done` + the behind-schedule derivation feeding the assembled Critical Items seed) is
  still unbound as of this log's close — the renderer needs no change, only the read.
- **`logs/migrations/po_vendors_backup_20260810.json`** remains untracked, carried over unchanged
  from prior session logs — still needs a Seth commit-vs-gitignore call.

## What was NOT touched

- The alert/reminder engine (decision 12, deferred fold-in by design — no schema gap to close).
- The Weekly Production Report's page-3 binding to the new `job_schedule_tasks` table (schema now
  exists; the report-side read was not part of this arc).
- The manifest lane's matching replay-guard fix (logged, not applied here — a distinct PR).
- The family-wide §30 portal_client integration-smoke gap (logged, not built here).
- `logs/migrations/po_vendors_backup_20260810.json`'s commit-vs-gitignore disposition.

## Cross-references

- `docs/adr/0006-job-schedule-payment-tracking.md` — the canonical design record: corpus findings,
  all eight ratified operator decisions, the twelve implementation decisions, and the
  2026-08-11 anonymized-fixtures amendment.
- `docs/runbooks/schedule_import_path.md` — the §43 successor-remediation runbook for
  `schedule_poll`, 9 symptoms, landed with PR #85.
- `docs/tech_debt.md` — three open items this arc surfaced or left open: the family-wide §30
  portal_client integration-smoke gap (PR #85 review), the manifest-lane replay-guard analog
  (PR #90 review), and the Weekly-Production-Report page-3 binding (pre-existing, now unblocked).
- `docs/HOUSE_REFLEXES.md` §2 (both PR-4 BLOCKs were found by adversarial review on the diff
  itself, before merge — the control biting exactly as intended, the same class as the
  2026-08-11 manifest-lane discard-race finding) and §5 (the gate-activation Description-read
  discipline, applied here rather than skipped).
- `docs/operations/pr_merge_discipline.md` — the four-part verify applied to all seven PRs, each
  independently re-checked against live GitHub for this log.
- ADR-0004 (`docs/adr/0004-...`) and ADR-0005 — the estimate-lane red-team finding #2 (confidence
  is not a filter, human validate is the fidelity control) and the manifest-lane §34
  suspicious-warn disposition, both inherited verbatim by this lane rather than re-derived.

## Verification (final state, PR-7 / #93)

```
- pytest: 5354 passed / 2 skipped / 58 deselected (test_publish_daemon excluded — host-local
  conftest live-state guard, per memory; the operator-run schedule corpus-qualification suite
  likewise excluded from this count)
- mypy: 0 errors / 499 source files
- ruff: clean
- main-branch CI on merge commit: SUCCESS (all seven PRs, independently re-verified)
```
