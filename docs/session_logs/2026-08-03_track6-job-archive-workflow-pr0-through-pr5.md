---
type: session_log
date: 2026-08-03
status: active
related_prs: [712, 713, 715, 716, 717, 718, 719, 720, 721, 722]
workstream: field_ops
tags: [session_log, field_ops, infrastructure, archive_on_closure, section51, repo_reconcile, local_dev, safety_portal]
---

# Session log — 2026-08-02 → 08-03 · Repo reconcile, a real local dev environment, and Track 6 (job-archive workflow) PR-0 through PR-5

## Purpose

Three phases across one continuous session. (1) Reconcile the development repo up to the
production mirror, which had silently become the furthest-forward portal since the host
migration. (2) Re-found the stale `~/its-demo` worktree as a real local development environment
running the actual app. (3) Execute `docs/ROADMAP.md` Track 6 — the end-to-end job-archive
workflow — from the disarm-the-landmine PR through the daemon's own queue and commit point.

Phases 1 and 2 (PRs #712, #713) were already logged in
[`2026-08-02_repo-reconcile-and-local-dev-environment.md`](./2026-08-02_repo-reconcile-and-local-dev-environment.md);
this log covers them at summary depth for continuity and then carries the narrative into Track 6,
which is the substance of 2026-08-03.

## Phase 1 — the repo reconcile (#712)

**The finding that shaped it:** the furthest-forward portal was not in this repo. The 2026-07-25/26
host migration created a second live repo, `its-sys-admin/evergreen-its`, for the production Mac.
The two forked at `885d4a4` (#710). Dev added one docs commit (#711); production added ten merged
PRs — two of them (#9, #10) carrying real portal code: the signature-capture full-screen sheet, the
iOS scroll fix, the signature-aspect fix in `safety_reports/form_pdf.py`, +36 lines into the
existing `errorCopy.ts`, plus two genuinely new test files (`tests/test_error_copy_parity.py`,
`tests/test_form_pdf.py`).

**Decision — reconcile with a MERGE COMMIT, not a squash.** The repos share an identical object
graph (`885d4a4e8c0b79f7b3c82b52ad83858bd2e39c04`, tree `f6edd51ab478317af663155d037a04fee340717d`,
byte-identical in both), so a true merge makes dev `main` a strict ancestral superset of evergreen
`main` — every future sync in either direction is a fast-forward. A squash would have permanently
forked the histories. Verified post-merge: `evergreen/main` is an ancestor of `origin/main`, 0
production commits missing.

**Decision — one PR, not several.** Forced by the enablement sha-pin: production changed 7 pinned
sources in `docs/enablement/manifest.yaml` and their 7 sha256 lines in the same range; split across
PRs, the first REDs `test_docs_pdf`.

Zero conflicts. Three commits rode on top: session-log AUTO-INDEX regen, `scratchpad/` gitignored
(untracked but not ignored — fails both blocking CI legs, ruff 8 findings + mypy 2), and
`.dev.vars.example` completed to all ten Worker secrets (was missing `PORTAL_ADMIN_API_TOKEN` +
`PORTAL_FIELDOPS_API_TOKEN`, both fail-closed, so a fresh clone could not provision its first user).

## Phase 2 — re-founding local dev (#713)

`~/its-demo` was a worktree 395 commits behind on `feat/solar-equipment-personnel-demo`, whose
cosmetic `demo_*` / `/api/demo/*` / `SolarDashboard.tsx` layer was a styling template for the
FieldOps pages — work main had since absorbed in full. Operator decision: retire it, don't
forward-port; re-found on the reconciled main running the real app. Old branch **preserved** (never
pushed, no PR — the MERGED-delete precondition can never be satisfied, HOUSE_REFLEXES §3). Replaced
the `node_modules` symlink into the live tree with a real `npm ci` (the same footgun class as
`cp -R .venv`). Cleared the stale 2026-06-27 Miniflare sqlite rather than migrating it forward.

**Found while doing it: `vite dev` had been broken repo-wide** since the PO cross-root imports
landed — `worker/po.ts` and `worker/subcontract.ts` read `../../po_materials/{config,terms}` from
outside the vite root with no `server.fs.allow`, so the dev server died at startup denying
`…/po_materials/terms/chint_vendor_v1.md?raw`. `fs.allow` is a dev-server-only restriction —
`npm run build` succeeded in 343ms on the same tree, and CI's `portal` job runs tsc + vitest + build
and never starts a dev server. Every gate stayed green while local development was impossible.
Fixed in #713.

Environment proven end-to-end against a running server: SPA 200, `/api/session` 401 fail-closed,
`/api/bogus` returns the JSON API terminator not the SPA fallback, submitter login (9 capabilities),
admin provisioning through the internal bearer, wrong bearer 401, and
`POST /api/fieldops/job` → `JOB-000017` persisted with `origin='portal'`, `lifecycle='active'`,
`job_no='2026.101'`.

## Phase 3 — ROADMAP Track 6: the job-archive workflow

Plan approved 2026-08-02 (`docs/ROADMAP.md` Track 6, added by #714). An admin archives a job from
the portal and every per-job container in both Smartsheet and Box relocates into an archive tree:

```
SMARTSHEET   ITS — Archive / <Job Name> / {Safety, Progress, Purchase Orders, Subcontracts}/
BOX          ITS Archive   / <Job Name> / {Safety, Progress}/          <- new root
```

Six containers, not eleven — `safety_reports.box.portal_root_folder_id` is the shared Box root for
safety + PO + RFQ + subcontracts, so moving `<safety root>/<Job>` carries `Purchase Orders/`,
`RFQs/`, `Vendor Quotes/` and the subcontract files with it.

Doctrine framing: a §51 scope expansion, a FIXED high-capability class (Op Stds v21 §44). Ratifies
rows 2/3/6/9 of `docs/reports/2026-07-23_project_closure_policy_proposal.md` and closes its#682.
Seth-owned rider; nothing activates before it exists.

### PR-0 (#715) — disarm the landmine + make the lifecycle truthful

Two coupled live defects, neither introduced this session.

1. **The `Archived` dropdown option was an armed landmine.** `fieldops_sync.py:757-763` fired an
   unconfirmed, un-retryable four-sheet relocation on any mirror of an archived job — the job was
   mark-synced immediately after, so a failed move was permanent (`_warn_archive_move_failed` said
   "no auto-retry" in its own WARN text). Never fired against live data — `Closed Projects` has
   never held a sheet — but one dropdown selection away.
2. **`lifecycle` was never on the wire**, so the UI re-derived job state from the legacy `status`
   column, which collapses inactive **and** archived into `'closed'` — an archived job re-displayed
   as "Inactive". `project_closure.md` had accumulated a workaround telling operators to "validate
   by effects, not the dropdown".

Disarmed on all three layers, because any one alone is insufficient — a stale browser tab or a
`curl` still reaches the route, and a hand-set D1 value still reaches the daemon:

| Layer | Change |
|---|---|
| Worker | `POST /:job_id/lifecycle` refuses `archived` -> 409 `use_archive_route` |
| SPA | option removed; an already-archived job's selector is locked |
| Python | trigger removed from the mirror path |

`_archive_closed_job_trackers` **preserved** (§14) with direct unit coverage, no caller, until the
replacement path is live-proven. Fixed the `:596` lifecycle fan-out across its whole surface — both
SQL selects, both wire types, the selector seed, the detail pill, the list-card badge.

Both new controls went through inject -> confirm RED -> revert: re-adding the mirror-path trigger
failed `test_job_mirror_never_archives[archived]` (parametrized over all three lifecycles, not just
`archived`); restoring the status-derived selector seed failed the "an ARCHIVED job reads 'Archived'"
assertion. Live end-to-end against a real dev server confirmed `POST /lifecycle {"lifecycle":
"archived"}` -> `409 {"error":"use_archive_route"}`.

### PR-1 (#716) — Smartsheet folder primitives

`move_folder_to_folder` / `move_folder_to_workspace` / `rename_folder` (breaker-guarded WRITES,
deliberately not retry-enrolled — an archive's correct retry is durable and cross-cycle via the
daemon's own queue, not a 2-attempt in-process backoff) plus reads `get_folder_name` (the resume
probe) and `get_workspace_access_level` (the ADMIN pre-flight).

**Smartsheet's Move Folder cannot rename** — `POST /folders/{id}/move` takes only
`destinationType`/`destinationId`; `newName` belongs to `/copy`, but the SDK's shared
`ContainerDestination` model exposes `new_name` and silently ignores it on move (verified against
the live OpenAPI spec via Context7). Every per-job archive is therefore a two-call, non-atomic
move-then-rename sequence with a resumable intermediate state; a caller must not decide "already
moved?" by name, since the find-or-create paths in `week_sheet`/`hours_log`/`job_sheet` can re-grow
that name in the source at any moment — the resume probe keys off the recorded folder id instead.

Six §30 live smokes added (operator-run, never CI), including a cross-workspace move proving cell
**history** survives — a claim the pre-existing `move_sheet_to_folder` docstring had made with no
test behind it and whose own smoke had never been run. 23 integration tests collect under `-m
integration` (was 17); the default run collects none, because this host's
`ITS_SMARTSHEET_TOKEN` resolves to production.

### PR-2 — `box_client.move_folder` (#717 closed unmerged, re-cut as #718)

Box's `Item.move(parent, name=)` does move+rename in one atomic `PUT /folders/{id}` — no crash
window, unlike Smartsheet's two-call sequence. MOVE-ONLY by construction — no delete or rename
wrapper (`test_box_client_exposes_no_folder_delete_primitive` enforces it); a "move failed -> delete
and re-upload" recovery would be catastrophic and irreversible, so the primitive that would enable
it doesn't exist. Only two Box containers per job move, not five — the same shared-root fact from
PR-0's plan. Conflict handling: a 409 adopts only when the existing child IS the folder already
held (a replay of a completed move); a different folder holding the target name re-raises loud.

The load-bearing §30 live smoke: `download_file(file_id)` after the move succeeds — Box item IDs
are stable across a re-parent, which is what lets an approved-but-unsent weekly packet still send
once its job has been archived (`weekly_send.py` resolves by Box file ID, not path).

**#717 -> #718:** #717 was cut off `feat/archive-pr1-smartsheet-primitives` and carried #716's
pre-squash commit; once #716 squash-merged, #717 went CONFLICTING. Force-push is guardrail-blocked,
so the clean path was closing #717 and re-cutting identical content off `main` as #718.

### PR-3 (#719) — migration 0058 + `cap.job.archive`

Two D1 columns rather than a ten-value enum: `archive_state` (none / requested / in_progress /
complete / partial / failed) and `archive_direction` (archive / unarchive) — one shape, so every
guard is written once. `archive_folder_key` snapshots the folder name at request time because
`project_name` is editable (`fieldops_job_write.ts`, added 2026-07-20) and every container is found
by name; a rename between "operator pressed Archive" and "the daemon reaches this job" would
otherwise strand the relocation.

`cap.job.archive` granted to admin only and explicitly — the 0013 admin catch-all
(`INSERT ... SELECT key FROM capabilities`) does not auto-include capabilities added after it
(the 0044/0051 rule). Verified live on a fresh D1: admin 24 (was 23), manager 12, submitter 9.
Order-dependency stated in the migration header: apply before the Worker that references the
capability deploys, since `resolveCapabilities` is fail-closed.

### PR-4 (#720) — archive/unarchive routes + the prune fence, and the adversarial review

The routes record intent; the Mac-side pass performs the relocation and reports back — separation
that lets the UI say "Archiving..." honestly. Gated on `cap.job.archive`, not
`cap.jobtracker.manage` (every admin holds the latter for routine work; archiving stays separately
narrowable). The typed confirmation (compared against the row's own `project_name`) is a
server-side control per Invariant 2 — client-only would not be a control at all. Un-archive returns
a job to INACTIVE, never active.

**The second live defect this Track fixes:** `prune.ts`'s `jobs` DELETE has no age cutoff and fires
on `active = 0`; an archived job has `active = 0` and holds none of the eight guarded NOT-IN record
types, so the very next 09:00 cron would have deleted the row — taking `archive_state`,
`archive_detail`, and `archive_folder_key` with it, the only record of where each container came
from. `AND archive_state = 'none'` fences it.

**`portal-worker-security-reviewer` found two real MEDIUMs** on the diff:

1. **`jobFolderKey` diverged from Python for non-ASCII Unicode.** The TS implementation filtered a
   C0/C1 codepoint range; Python's `str.isprintable()` is "NOT (Unicode Other or Separator), except
   ASCII space" — `Cc Cf Cs Co Cn Zl Zp Zs`. Verified against live Python: `'Bradley\xa0Solar'` ->
   `'BradleySolar'` (NBSP stripped), a zero-width-space case, and an ideographic-space case all
   stripped by Python but kept by the original TS. Failure mode: Python creates the real folder with
   them stripped, so a snapshot keeping them sends the daemon after a folder that never existed —
   silent, permanent, visible only as an unexplained `archive_state='failed'`. Fixed to a Unicode
   General_Category filter, byte-identical to Python across ten cases, with `test_job_archive_guard.py`
   re-implementing the rule in Python and asserting agreement.
2. **The state guard was check-then-act.** JS branched on a separate `SELECT`, then the `UPDATE`
   wrote blind — two concurrent requests could both observe `'none'` and both write; worse, once the
   daemon writes the same columns from its own process, a request holding a stale read could stomp
   an `in_progress` claim back to `'requested'` underneath an active relocation. Fixed by moving the
   permitted source state into the `UPDATE`'s own `WHERE`; a 0-change result is disambiguated by a
   re-read (404 if the row is gone, 409 if the race was lost).

Plus one LOW: the `confirm.length < 1` floor ran before trimming, so `" "` could clear it.

**An honest limit on what the concurrency fix proves.** A vitest parallel-request test was written
for the TOCTOU fix; injecting the predicate's removal left the entire suite green anyway — workerd
serializes requests and D1 serializes writes, so no interleaving can be staged in that harness at
all. Rather than ship a test implying coverage it lacked, the suite was rewritten to state only what
it actually asserts, and the real mechanical backstop moved to `tests/test_job_archive_guard.py`
(a plain Python process reading the TS source, the `test_error_copy_parity.py` precedent). Both
structural guards then correctly inject-confirm-reverted: removing the in-WHERE predicate failed
`test_archive_update_carries_an_in_where_state_guard`; restoring the C0-only filter failed the
Unicode-property-filter assertion. The genuine proof of the concurrency guard under real
contention only arrives with the daemon-side PR (#721) — the first thing that can actually race it.

### PR-5 (#721) — the daemon's own queue and commit point

`GET /api/internal/fieldops/archive-pending` + `POST /api/internal/fieldops/job-archive-progress`.
Its own queue, not the job-dirty list — `jobs-mark-mirrored` clears `sync_state` the instant both
sheets catch up, which is precisely why the pre-Track-6 archive "did not auto-retry": an unrelated
mirror success silenced it. Cap 25, not the 200 used elsewhere, since each row costs six external
API sequences across two systems.

Forward-only by construction: every UPDATE carries
`AND archive_state IN ('requested','in_progress') AND archive_direction = ?`, so a replay cannot
resurrect a completed archive, and — the sharpest case — a stale ARCHIVE result cannot be applied to
a job the operator has since flipped to un-archive. A stale member is reported in `skipped` rather
than failing the whole batch. Validate-all-then-execute matches the `jobs-mark-mirrored` contract.

Inject -> confirm RED -> revert on the forward-only predicate red-lighted three tests: replay
resurrection, the archive-result-onto-a-reversed-job case, and one-stale-member-doesn't-discard-
the-batch. 15 tests total.

### PR-6 (#722) — `field_ops/job_archive.py` — OPEN, not yet merged

The relocation module: six containers, per-container fences, move-before-rename ordering (renaming
first would hide the folder from the find-or-create paths in `week_sheet`/`hours_log`/`job_sheet`,
which would then grow a fresh empty folder beside it while the archive moved the wrong tree), an
ADMIN pre-flight across all five workspaces (`verify_archive_capability`, refusing loudly rather
than surfacing a 403 mid-archive), and a resume probe keyed off recorded folder IDs, never names.
`state_from_results` keeps `partial` distinct from `failed`. An empty `archive_folder_key` refuses
loudly rather than reporting six false "nothing to move" successes. All checks (`test`/`portal`/
`secrets`/CodeQL) report SUCCESS on the PR as of this log, but the PR itself is **not merged** —
wiring it into `fieldops_sync`'s cycle, the Box leg, and the third Box root all remain.

## Mistakes made this session, recorded honestly

- **ruff N802 three times.** Emphasis-capitalised words in test function names
  (`..._in_ONE_call`, `..._BEFORE_the_rename`, `..._never_RAISES`) tripped ruff's "function name
  should be lowercase" rule. It reached CI RED on #718 specifically because a scoped
  `ruff check <one file>` was run instead of the full-tree check CI actually runs, after the same
  lint had already been hit and fixed once earlier in the session. Fixed in `4c6eaaa`
  ("fix(tests): ruff N802 — lowercase the three move_folder test names"). Captured to memory as
  `feedback_no-uppercase-in-test-names.md`.
- **Stacked-PR friction.** Squash-merging a base PR rewrites its SHA, so a stacked branch cut from
  it goes CONFLICTING, and force-push is guardrail-blocked — each stacking cost a branch re-cut.
  #717 was closed and re-cut as #718 for exactly this reason (see PR-2 above). #721 was cut stacked
  on #720 (it exercises #720's routes) and needed retargeting to `main` once #720 landed. Later PRs
  in the stack were cut directly off `main` wherever the work was actually independent.
- **PR-5's first run 404'd** because it was initially exercised against a tree that didn't yet
  carry #720's routes; resolved by the retarget/re-run once #720 was on `main`.

## Verification

All nine merged PRs are four-part-landing clean per `docs/operations/pr_merge_discipline.md`
(`state=MERGED` · `mergedAt` non-null · `mergeCommit.oid` present · main-branch CI on the merge
commit = SUCCESS, verified via GitHub Actions check-runs for the `test`/`portal`/`secrets` jobs):

```
#712  MERGED  mergeCommit=0533fa421  mainCI=success
#713  MERGED  mergeCommit=1b4b26507  mainCI=success
#714  MERGED  mergeCommit=1d95206be  mainCI=success
#715  MERGED  mergeCommit=3920953ee  mainCI=success
#716  MERGED  mergeCommit=46c401eb3  mainCI=success
#718  MERGED  mergeCommit=57df36313  mainCI=success
#719  MERGED  mergeCommit=b394e509e  mainCI=success
#720  MERGED  mergeCommit=89056ea48  mainCI=success
#721  MERGED  mergeCommit=5634c8703  mainCI=success
```

`#717` is CLOSED, not merged — deliberately superseded by `#718` (identical content, clean base).
`#722` is OPEN — all checks (`test`/`portal`/`secrets`/CodeQL `Analyze` x3) report SUCCESS on the PR
branch, but it has not landed and this log does not count it among landed work.

Final gate figures on the last full run (per PR-6's own report, the most recent):

- pytest: 4575 passed / 2 skipped / 58 deselected
- mypy: clean, 470 source files
- ruff: clean
- main-branch CI on merge commit: SUCCESS

Also clean on the same tree: `check_doctrine_drift --strict`; `npm test` 69 files / 1189 tests;
`npm run test:spa` 55 files / 730 tests.

## What was NOT done / open

- **Track 6 remaining:** wiring the archive pass into `fieldops_sync`'s cycle behind a dark gate
  (the module PR-6/#722 is open but unwired); the Box leg's own daemon consumer; the third Box root
  builder (`scripts/migrations/build_box_roots.py` extension, a `standup.py` seed-tuple addition, and
  the `production_repoint.py:129` suffix trap flagged in the ROADMAP — it matches the literal
  `.portal_root_folder_id`, so an `...archive_root_folder_id` key would be silently skipped at
  cutover); the SPA archive button + typed-confirmation modal; docs (ADR-0005, a `project_closure.md`
  rewrite, a troubleshooting-tree node, a system-map node + brief); the §51 doctrine rider
  (Seth-owned); and the attended sandbox drill.
- **Blocked on the operator:** a sandbox Smartsheet PAT under a distinct Keychain key — this host's
  `ITS_SMARTSHEET_TOKEN` resolves to production, so `pytest -m integration` here writes to the live
  tenant; the Box identity question, deliberately not probed (a `box_client` call rotates the
  refresh token and could break the production daemons); and the push-access decision for
  `evergreen-its` (`SolutionSmith-debug` holds `push: false` there, and that repo has no branch
  protection).
- **Archiving is currently unavailable end-to-end by design** until the remaining PRs land —
  nothing is lost by this, since the pre-Track-6 §51 move has never fired live in this system's
  history.
- Not touched this session: the realistic local-dev seed script (`demo/seed_local.mjs`); the
  two-repo sync convention write-up (blocked on the push-access decision above); the 38 stale local
  branches noted in the prior log (not audited this session either).

## Cross-references

- `docs/session_logs/2026-08-02_repo-reconcile-and-local-dev-environment.md` — the fuller Phase 1/2
  account (decisions, verification detail, non-obvious findings) this log summarizes.
- `docs/ROADMAP.md` Track 6 — the plan this session executed against; still open past PR-5.
- `docs/operations/pr_merge_discipline.md` — the four-part verify all nine merged PRs satisfy.
- `docs/reports/2026-07-23_project_closure_policy_proposal.md` — the disposition table Track 6
  ratifies four rows of.
- `docs/runbooks/project_closure.md` — the §43 runbook Track 6 will rewrite once the workflow is
  live; the "validate by effects, not the dropdown" workaround it currently carries is obsolete as
  of PR-0 but the doc itself is not yet updated.
- `docs/HOUSE_REFLEXES.md` §1 (trust live code, verified against the live OpenAPI spec and live
  Python `isprintable()` behaviour rather than assumed), §2 (prove the control bites — the
  inject-confirm-revert pattern used on every new guard this session, and the honest limit recorded
  on the TOCTOU test), §3 (worktree/stacked-branch discipline — the #717→#718 re-cut).
- `feedback_no-uppercase-in-test-names.md` (memory) — the ruff N802 lesson from this session.
