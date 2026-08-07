---
type: session_log
date: 2026-08-02
status: closed
related_prs: [712, 713]
workstream: infrastructure
tags: [session_log, infrastructure, reconcile, two_repo_split, local_dev, safety_portal, archive_on_closure, planning]
---

# Session log — 2026-08-02 · Repo reconcile, a real local dev environment, and the archive-workflow plan

## Purpose

Two deliverables and one plan. (1) Reconcile the development repo up to the production
mirror, which had silently become the furthest-forward portal. (2) Re-found the stale
`~/its-demo` worktree as a *real* local development environment running the actual app,
so the next feature can be built against current code. (3) Plan the end-to-end
archive-job workflow the operator asked for.

## The finding that shaped the session

**The furthest-forward portal was not in this repo.** The 2026-07-25/26 host migration
created a second live repo, `its-sys-admin/evergreen-its`, for the production Mac. The two
forked at `885d4a4` (#710). Dev added one docs commit (#711); production added **ten**
merged PRs — and two of them (#9, #10) were real portal code: the signature-capture
full-screen sheet, the iOS scroll fix, a controlled pad, a dirty guard, the signature-aspect
fix in `safety_reports/form_pdf.py`, and +36 lines of `errorCopy.ts`. Anything built on dev
`main` would have been built against a portal missing all of it.

## Decisions

1. **Reconcile with a MERGE COMMIT, not a squash.** The consequential call. The repos share
   an *identical object graph* — verified `885d4a4e8c0b79f7b3c82b52ad83858bd2e39c04` with
   tree `f6edd51ab478317af663155d037a04fee340717d` byte-identical in both. A true merge makes
   dev `main` a strict ancestral **superset** of evergreen `main`, so every future sync in
   either direction is a fast-forward. A squash or cherry-pick would have permanently forked
   the histories and committed us to cherry-picking forever.
2. **One PR, not several.** Forced by the enablement sha-pin: production changed 7 pinned
   sources in `docs/enablement/manifest.yaml` *and* their 7 `sha256:` lines in the same range.
   Split across PRs, the first REDs `test_docs_pdf::test_committed_manifest_is_self_consistent`
   and cannot be fixed without the other. #9/#10 are also a code pair.
3. **Retire the demo layer rather than forward-port it.** `~/its-demo` was 395 commits behind
   with a `demo_*` / `/api/demo/*` / `SolarDashboard` / `#solar` layer that existed as a
   *styling template* for the FieldOps pages — work main has since absorbed in full. Its
   worker patch was written against a `worker/index.ts` that has since grown to 3057 lines.
   The new environment runs the real app on the real schema.
4. **Preserve `feat/solar-equipment-personnel-demo`.** It was never pushed and has no PR, so
   the `state=MERGED` precondition for a branch delete can never be satisfied
   (HOUSE_REFLEXES §3: preserve OPEN / CLOSED-unmerged / no-PR branches).
5. **Do not probe the Box credential.** Determining which account this host holds requires an
   API call, and Box refresh tokens rotate on every exchange. If dev and production share a
   grant, one call breaks the production daemons mid-travel. Left as an operator item.

## PRs landed

**#712 — `chore(reconcile): import evergreen-its PRs #1–#10`** (merge commit `0533fa421`)

Zero conflicts. `docs/tech_debt.md` was the only file both sides touched and the hunks did
not even overlap (production at orig 147/358, dev at 478/2277/2287). 44 files, +2531/−202.
Three commits rode on top: the session-log AUTO-INDEX regen (#711 added its log but never
regenerated), `scratchpad/` gitignored, and `.dev.vars.example` completed to all ten Worker
secrets.

**#713 — `fix(portal): restore vite dev`** (merge commit `1b4b26507`)

`server.fs.allow` for the cross-root PO imports. See "Non-obvious findings" below.

## Verification

Both PRs four-part-landing clean (`docs/operations/pr_merge_discipline.md`):

- **#712** — `state=MERGED` · `mergedAt=2026-08-03T03:20:04Z` · `mergeCommit=0533fa421` ·
  main-branch CI on the merge commit **SUCCESS** (`ci` + `Push on main`).
- **#713** — `state=MERGED` · `mergedAt=2026-08-03T03:39:13Z` · `mergeCommit=1b4b26507` ·
  main-branch CI on the merge commit **SUCCESS** (`ci` + `Push on main`).

Pre-push gates on the merged tree, run in an isolated worktree with its own venv:

- pytest: 4520 passed / 2 skipped / 51 deselected
- mypy: no issues, 467 source files
- ruff: clean
- `check_doctrine_drift --strict`: exit 0 (M1/M4/M7 clean)
- portal typecheck: clean (3 tsconfigs)
- `npm test` (workerd + D1): 66 files / 1136 tests
- `npm run test:spa`: 55 files / 727 tests

**Imported tests proven to run, not merely green** (HOUSE_REFLEXES §2 — a green on an
uncollected test proves nothing): `git ls-files` confirmed all four new test files tracked;
`pytest --collect-only` reported `test_error_copy_parity.py` → 2 and `test_form_pdf.py` → 83,
both passing; and the SPA count moved **53 → 55 files / 685 → 727 tests** against dev main,
i.e. the two imported `.test.tsx` files genuinely joined the run.

Worktree isolation proven both ways: the worktree venv reported
`Editable project location: /Users/sethsmith/its-reconcile` while `~/its/.venv` stayed
`/Users/sethsmith/its`. The live tree was confirmed untouched throughout —
`shared/sheet_ids.py` byte-identical to `origin/main` at every checkpoint.

Ancestry property confirmed post-merge: `evergreen/main` is an ancestor of `origin/main`;
0 production commits missing, 6 ahead.

## Non-obvious findings worth recording

- **`vite dev` had been broken repo-wide** since the PO cross-root imports landed.
  `worker/po.ts` and `worker/subcontract.ts` read `../../po_materials/{config,terms}` — above
  the vite root — and `vite.config.ts` had no `server.fs.allow`, so the dev server died at
  startup with `Denied ID …/po_materials/terms/chint_vendor_v1.md?raw`. **`fs.allow` is a
  dev-server-only restriction**: `npm run build` succeeded in 343 ms on the same tree, and
  CI's `portal` job runs tsc + vitest + build and never starts a dev server. Every gate stayed
  green while local development was impossible. Fixed in #713.
- **The `Archived` lifecycle option is a live, armed landmine.** `fieldops_sync.py:757-763`
  fires the four-sheet §51 relocation on any mirror of a `lifecycle=='archived'` job — with no
  confirmation, no retry, and a UI that then displays "Inactive". It has never fired against
  live data, but it is one dropdown selection away from doing so.
- **`prune.ts` would delete an archived job out from under its own archive record.** The
  `jobs` DELETE stage (`:331-357`) has **no age cutoff at all** and fires on `active = 0` plus
  eight NOT-IN guards. An archived job whose only artifacts are Smartsheet folders and Box
  files holds none of the eight.
- **This host's `ITS_SMARTSHEET_TOKEN` resolves to PRODUCTION.** `get_client()` lists 16
  workspaces including real Evergreen data and `ITS — Archive = 7347287308429188` (the
  production id); the sandbox archive `1649411894863748` is absent. `pytest -m integration`
  run from `~/its` today would create and delete live production sheets. The sandbox is
  currently reachable only through the MCP connectors, which authenticate as
  `seths@evergreenmirror.com`.
- **Smartsheet Move Folder cannot rename.** `POST /folders/{id}/move` accepts only
  `destinationType` + `destinationId`; `newName` is a *Copy Folder* parameter. The SDK's
  shared `ContainerDestination` model exposes `new_name`, so it serializes and is silently
  ignored. Box's `Item.move(parent, name=)` renames atomically in the same call — an asymmetry
  that drives the archive design.
- **`project_name` is editable** (`fieldops_job_write.ts:352-377`, added 2026-07-20) while the
  in-file comment at `:328-332` still claims only routing fields are touched. Every per-job
  container is keyed by `safety_naming.job_folder_name(project_name)` with no rename
  propagation, so a rename orphans a job's folders. The existing archive helper already has
  this exposure.
- **23 capabilities, not 22** (admin 23 / manager 12 / submitter 9, queried against a live
  local D1). The CLAUDE.md count is stale.
- **The `:596` lifecycle re-display bug confirmed live**, not by reading code:
  `GET /api/fieldops/jobs` genuinely omits `lifecycle` from the wire shape, which is why the
  SPA re-derives it from the legacy `status` and shows an archived job as "Inactive".
- **F22 approval is not at risk from archival** — `send_poll_core` resolves authority from a
  fixed `f22_workspace_id` constant, never from a row's location, and the review sheets never
  move.

## The local development environment

`~/its-demo` now runs the real portal at the reconciled HEAD: all 57 migrations on a fresh
local D1, the `node_modules` symlink into the live tree replaced with a real install (an
`npm install` through that symlink would have written into the live tree — the same class as
the `cp -R .venv` footgun), and the stale 2026-06-27 Miniflare sqlite removed rather than
migrated forward.

Proven end-to-end against a running server: SPA shell 200 · `/api/session` 401 fail-closed ·
`/api/bogus` returns the JSON API terminator rather than the SPA fallback · login as the
migration-seeded submitter (9 capabilities) · **admin provisioning through the internal bearer
route** · wrong bearer 401 · `POST /api/fieldops/job` → `JOB-000017` persisted with
`origin='portal'`, `lifecycle='active'`, `job_no='2026.101'` (the full 0021 + 0057 SoR set).

The admin-provisioning result is the concrete proof that the `.dev.vars.example` fix mattered:
`requireAdminToken` is fail-closed, so without `PORTAL_ADMIN_API_TOKEN` a fresh clone
following the documented bootstrap path could never create its first user.

## What was NOT done / deliberately deferred

- **The realistic seed script.** The environment runs and is usable, but is populated only by
  the migration seeds plus one hand-created job. The planned `demo/seed_local.mjs` (driving
  the real HTTP API so bcrypt and the submission HMAC come from the Worker's own code rather
  than being hand-computed) is not built.
- **The entire archive workflow.** Planned, not started — see below.
- **The two-repo sync convention.** Blocked: `SolutionSmith-debug` has `push: false` on
  `its-sys-admin/evergreen-its`, and that repo has no branch protection.
- **Stale local branches.** 38 remain from prior sessions; not audited this session.

## Open items handed off

1. **Sandbox Smartsheet PAT** under a distinct Keychain key + an opt-in override, so the
   archive drill cannot run against production by accident.
2. **Box identity on this host** — resolve by inspection or by minting a separate dev
   credential. Not probed, deliberately.
3. **Push-access decision** for `evergreen-its`, which gates writing the sync convention into
   `docs/operations/pr_merge_discipline.md`.
4. **Archive-workflow build** — scoped as **`docs/ROADMAP.md` Track 6**, which is the canonical
   home for it (the session's own working plan file was scratch and is not reachable from a fresh
   session, per the anti-sprawl contract). Its first PR — de-arm the lifecycle landmine, fence
   `prune.ts`, fix the `:596` fan-out — needs none of the above; only the live drill does.
5. **§51 doctrine rider** — the archive scope expansion is a FIXED high-capability class.
   Seth-owned, planning-layer. Ratifies proposal rows 2/3/6/9 from
   `docs/reports/2026-07-23_project_closure_policy_proposal.md` and closes its#682.
6. **Docs currency** — the 22-vs-23 capability count, and the stale
   `project_closure.md:51-55` display-quirk workaround once `:596` is fixed.

## Cross-references

- `docs/operations/pr_merge_discipline.md` — the four-part verify both PRs satisfy.
- `docs/reports/2026-07-23_project_closure_policy_proposal.md` — the ~45-surface disposition
  table the archive plan ratifies four rows of.
- `docs/runbooks/project_closure.md` — the §43 runbook the archive work will rewrite.
- `docs/session_logs/2026-07-26_production-host-migration-phase1.md` and
  `2026-07-26_production-host-standup.md` — the two halves of the host migration, written from
  opposite hosts; this session merged the second into dev.
- `docs/HOUSE_REFLEXES.md` §1 (trust live code), §2 (prove the control bites), §3 (worktree
  discipline) — all three were load-bearing here.
