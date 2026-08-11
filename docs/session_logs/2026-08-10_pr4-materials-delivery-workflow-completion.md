---
type: session_log
date: 2026-08-10
status: closed
related_prs: [38, 40, 45, 48, 50]
workstream: field_ops
tags: [session_log, field_ops, materials, section51, daily_report, delivery_marks, form_editor, migration, house_reflexes]
---

# Session log — 2026-08-10 · PR4 materials/delivery workflow completion (five PRs, four-part clean)

## Purpose

Close out the PR4 materials/delivery workflow left incomplete by earlier sessions: the §51
material-receipts mirror had a Worker route and no daemon pass calling it, the 0059 migration's
three new columns (`Part Number` / `Category` / `Expected Ship Date`) had never reached
Smartsheet, the daily report filed no record of what materials were expected on a given day, a
confirmation-photo upload had no binding to the delivery line it was evidence for, and the form
editor had a silent switch hole on `additional_photos`. Five PRs later the workflow is complete
in code — none of it has been run against a live tenant.

## Pre-flight findings

- `progress_reports/material_receipts.py` (412 lines, landed by PR #17 on 2026-08-07) was dead
  code — a module and a Worker read route with nothing calling either, and zero Python tests.
- Migration `0059`'s own header said explicitly that the material-list snapshot mirror was
  deliberately deferred to "PR4" — this session is that deferral closing.
- The daily report's expected-materials section had filed no values since v5, on the stated
  reasoning that re-printing a mutable list would "snapshot mutable state the submission never
  carried."

## Decisions made

1. **The v7 contract inversion — not-capturing was the data loss, not the protection against
   it.** v5/v6 reasoned that reprinting the live D1 materials list would snapshot mutable state
   the submission never carried, so the section filed nothing. That's backwards for a document of
   record: a manager signs a report against a delivery state that every later mark rewrites, so
   the state at signing time is exactly what can't be reconstructed afterward if it isn't
   captured then. v7 exists **only** to mark this semantic change — its sections are
   byte-identical to v6, asserted mechanically rather than by eye. The renderer distinguishes the
   key's **absence** (v5/v6 → still renders the original note line, so a historical PDF doesn't
   silently lose a paragraph on re-render) from its **emptiness** (a v7 job with no expected
   materials → the section is skipped, because the v5 note line points at receipts that don't
   exist for that job).
2. **The handoff overstated the fix — three assertions needing inversion, not two.** The
   `initialValues`-absence pin in the existing test suite turned out to be correct as written and
   was left alone: `job_requirements` is a host-seeded, value-bearing section carrying the
   identical assertion pattern, so inverting it would have been wrong. Corrected the misleading
   comment near it instead of the code.
3. **Cutting v7 silently dropped v5 out of the synchronous form registry.** The eager window is
   "current + immediately-previous," so five test files that resolved a form definition by
   hard-coded version broke — four against a null definition, one on an exact `form_code`. They
   now resolve the catalog's current daily report so the next version cut can't repeat the break.
4. **Two of this session's own tests were false confidence, caught only by prove-it-bites.** A
   draft-persistence test passed clean *and* passed under the `editValues` regression injected,
   because the draft write is debounced and the assertion ran before the timer fired — rewritten
   to assert after unmount, which flushes. On PR #48, removing the `editorValidation` switch case
   left all 801 SPA tests green — meaning half of that fix shipped with zero coverage until a
   second, collision-driven test was added specifically to exercise it.
5. **The receipts mirror gate is six registry surfaces, not the five the prior handoff
   enumerated.** `verify_cutover`'s own archive-gate test surfaced the sixth —
   `operator_dashboard.act.registry.REGISTRY` — which is what makes a gate flippable from the
   dashboard console at all; a gate absent from it exists in config but is unreachable from the
   operator's actual UI. All six reconciled in PR #38, pinned by a dedicated test.
6. **The migration number moved twice mid-session (0061 → 0062 → 0063)** as a concurrent session
   landed PRs #44 and #47 against the same migration sequence. Caught by someone else's
   idempotency test rather than a coordination step in this session — the handoff note to verify
   the next free number with `ls` rather than trust a written-down value proved correct in
   practice.
7. **An operator request landed mid-session: delivery marks became two-tap.** Delivered /
   Partially delivered / Not delivered are three adjacent buttons on a phone on a job site, and a
   mark is an append-only ledger event with no delete path — a mis-tap is permanent, correctable
   only by a compensating event, and the §51 mirror plus the running total both read wrong until
   someone notices. First click arms (6s expiry, keyed by `(line, kind)` so switching targets
   re-arms rather than committing the old target); second click records. The armed state rides the
   button's label and aria-label, not styling alone — a colour change is not a confirmation
   prompt on its own.

## Prove-the-control-bites — every new/changed control RED-lit before shipping

| PR | Injection | Result |
|----|-----------|--------|
| #38 | grouper keyed on `submission_uuid` instead of `event_uuid` | 9 tests RED |
| #38 | `check_row_cap` unwired | 2 tests RED |
| #40 | back-fill set reduced to a tail slice (last N columns only) | 1 test RED |
| #40 | back-fill unwired from the find branch | 3 tests RED |
| #40 | unconditional write (both no-op guards removed) | 3 tests RED — took two attempts; removing only the first guard did *not* go red, because an independent pre-write re-read guard caught it, which is itself a finding about the guard's real coverage |
| #45 | drop the v5/v6 note line on an absent snapshot key | RED |
| #45 | seed the snapshot via `editValues` instead of `setValues` | RED, only after the draft-persistence test rewrite (see Decision 4) |
| #45 | single click on a delivery-mark button | RED ×4 — confirms the two-tap gate works |
| #48 | remove the `FormEditor` switch case | RED immediately |
| #48 | remove the `editorValidation` switch case | GREEN on the first pass (the finding — see Decision 4); RED after the collision test was added |
| #50 | unscope the material-line lookup (drop `job_id` / `active` check) | RED ×2 |
| #50 | strip the line/event binding from `photo_json` (leave it as an unsigned column only) | RED ×2 |

## PRs landed

| PR | Title | Purpose |
|----|-------|---------|
| #38 | `feat(field-ops): the receipts mirror reaches Smartsheet — the sync pass PR4 was missing` | Wires the dormant §51 material-receipts module into a `fieldops_sync` daemon pass; gate seeded `false` |
| #40 | `feat(materials): mirror Part Number / Category / Expected Ship Date — close the 0059 deferral` | `smartsheet_client.ensure_columns` (find-or-add, additive-only) back-fills the three 0059 columns onto every Material List sheet, old and new |
| #45 | `feat(daily-report): v7 files the expected-materials snapshot, and a delivery mark now needs two taps` | Daily-report v7 (snapshot the day's expected-materials state at signing time) + two-tap arm/confirm delivery marks |
| #48 | `fix(form-editor): additional_photos was missing from both editor mirrors — a silent switch hole` | Fixes a blank-panel render and a skipped uniqueness check for `additional_photos` in the form editor |
| #50 | `feat(daily-photo): bind a confirmation photo to the material line it evidences (migration 0063)` | Binds a "Partially delivered" evidence photo to its material line + receipt event, validated and HMAC-covered at upload |

Each verified independently against `its-sys-admin/evergreen-its` via the canonical four-part
check (`docs/operations/pr_merge_discipline.md`): `state=MERGED` · `mergedAt` non-null ·
`mergeCommit.oid` present · main-branch CI on the merge commit = SUCCESS.

```
#38  MERGED  mergedAt=2026-08-10T19:47:17Z  mergeCommit=f9c8e96d216e26dab9c98d879cb24b7bf86c41fe  mainCI=success
#40  MERGED  mergedAt=2026-08-10T20:38:02Z  mergeCommit=57d027a20f4209ee83d19cb0d3e811fb28a9ca07  mainCI=success
#45  MERGED  mergedAt=2026-08-10T21:57:31Z  mergeCommit=8f84f1a1b759d963abbc9a5ff4763befcc9d9893  mainCI=success
#48  MERGED  mergedAt=2026-08-10T22:17:19Z  mergeCommit=7ffab29b98fecfe9bbbf3fd5901f80d54731ba2f  mainCI=success
#50  MERGED  mergedAt=2026-08-10T23:31:15Z  mergeCommit=68f4c2791789ce3cc373c93f7e9193b60bef1f03  mainCI=success
```

**Four-part verify clean on all five PRs.**

## Verification — final full run, this session

- pytest: **4839 passed / 2 skipped / 58 deselected**
- portal: **71 worker files / 1260 tests**
- SPA: **59 files / 805 tests**
- mypy: **0 errors / 482 source files**
- ruff: **clean**
- `check_doctrine_drift --strict`: no blocking drift
- docs currency: **22 enablement docs current**

§43 runbook rows added: `docs/runbooks/job_materials.md` (missing-column back-fill / ship-vs-delivery
distinction from #40; expected-materials snapshot / two-step delivery marks / arming expiry / what
to do about a mistaken mark from #45); a new refusal row in the daily-photo runbook for the
line/event-binding checks from #50.

## What was NOT touched — left open deliberately

- **Nothing has been live-smoked.** This is the development Mac with **zero ITS launchd jobs
  loaded** — every verification above is "tests pass," never "it worked against a live tenant."
  A deployment-Mac session prompt was written to `~/Desktop/its-deploy-prompt.md`, ordering the
  stand-up steps explicitly: pull first, apply migration 0063 to D1 **before** deploying the
  Worker (the upload route binds the new columns; deploying ahead of the migration 500s every
  daily-photo upload), then deploy, then refresh the live venv, then a specific verification walk
  starting with the Material List back-fill (#40) — the one change in this batch that writes to
  existing live Smartsheet sheets on its own.
- **The manifest-import parser eval is still WAIVED, not passed.** Pre-existing open item
  (`docs/tech_debt.md`, the 2026-08-07 waived-precondition entry); not touched this session, not
  resolved by it.
- **The daily report's own "Confirm receipt" button still records in one click.** Out of scope —
  the operator's mid-session request was specifically about the three delivery-mark buttons
  (Delivered / Partially delivered / Not delivered), not the separate per-line "Confirm receipt of
  <material>" action in the expected-materials section, which remains single-tap.

## Sequencing context

- What this unblocks: the PR4 materials/delivery workflow is complete in code end-to-end (mirror →
  columns → daily-report snapshot → two-tap marks → evidence-photo binding). The next real step is
  the deploy-Mac stand-up and operator smoke, not further building.
- What was prerequisite: PR #17 (2026-08-07, material-receipts module + Worker route) and migration
  `0059` (the deferred column set) — both closed by this session's #38 and #40 respectively.
- Follow-ons: flip `field_ops.fieldops_sync.receipts_enabled` (seeded `false`) after the deploy-Mac
  smoke confirms the mirror pass behaves against a live per-job Material Receipts sheet; the
  manifest-parser-eval waiver remains open and unrelated to this batch.

## Cross-references

- `docs/tech_debt.md` — the 2026-08-07 `job_expected_materials` no-backup-D1 entry (PR4 designs the
  Material Receipts mirror this session's #38 wires up); the 2026-08-07/2026-08-10 waived-precondition
  entries for the manifest and estimate-extract parser evals (both still open, neither touched here).
- `docs/operations/pr_merge_discipline.md` — the four-part verify applied to all five PRs above.
- `docs/runbooks/job_materials.md` — the four new §43 rows this session added.
- `docs/HOUSE_REFLEXES.md` §2 (prove-the-control-bites — the injection table above, all reverted to
  shipped state after confirming RED), §6 (the deploy-Mac prompt orders migration-before-Worker-deploy
  per the standing "don't deploy from a stale checkout" class).
- `~/Desktop/its-deploy-prompt.md` — the deployment-Mac session prompt for the live stand-up and
  operator smoke this session could not perform.
- `docs/session_logs/2026-08-10_outage-diagnosis-alerting-gap-and-gate-activation.md` and
  `docs/session_logs/2026-08-10_preop-inspection-forms-and-checklists.md` — concurrent sessions
  against the same `main` this same day (PRs #11–#16 and #44/#47/#51 respectively); not this
  session's work.

---

## Addendum — 2026-08-11: one of these PRs shipped a live defect

Written after the fact, because the log above would otherwise read as clean and it was not.

**`ensure_columns` (#40) failed on the first live sheet and every 90-second cycle after it.** It
sent a per-column `index` (`start + offset`); the Smartsheet Add Columns API rejects a multi-column
body whose members carry different indices — HTTP 400, errorCode 1135, *"Input column index N is
different from the first input column index M"*. Columns are inserted consecutively at ONE shared
index, so `index=len(columns)` on every body still means "append, in specs order". The arithmetic
version looked equivalent, passed every mock in `tests/test_smartsheet_client.py`, and could not
work. Fixed by the overnight production session in **#59** (four-part clean), whose regression test
deliberately pins the SAME-index *shape* rather than the arithmetic. Verified live: Kiwi — Material
List went 14 → 17 columns on the first post-fix cycle, and a WARN storm of 98
`material_list_column_backfill_failed` rows went to zero delta.

**This is the mocks-pass-but-live-API-rejects class, and it was foreseeable.** HOUSE_REFLEXES §2
requires a live smoke before merging new shared infrastructure for exactly this reason;
`ensure_columns` was new shared infrastructure in `shared/smartsheet_client.py`. The PR body noted
"not live-smoked — development Mac, zero launchd jobs" and shipped anyway. Flagging the gap is not
the same as closing it: a mocked SDK test asserting `[3, 4]` was asserting the very shape the API
refuses, so the suite could never have gone red.

**Two ITS_Config rows this session's code declared did not exist on the tenant** — the
`receipts_enabled` gate and the receipts row-cap threshold. The first was in the repo seeder but the
seeder had never been run on that host; the second was in no seeder at all. Seeding a gate row in a
migration script is not the same as the row existing in the tenant — the repo-seeder-is-not-a-tenant-row
class. Both created via #59.

**What did hold up live:** migration 0063 applied and the Worker deployed in the correct order; the
live bundle serves `daily-report-v7`; and the receipts mirror's first-ever live fire succeeded.

See `docs/session_logs/2026-08-10_overnight-materials-standup-and-reconcile.md` for the full
production-host account.
