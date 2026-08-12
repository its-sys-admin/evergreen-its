---
type: session_log
date: 2026-08-12
status: closed
related_prs: [99, 103, 109, 114]
workstream: safety_portal
tags: [session_log, safety_portal, frontend, design, css, schedule, materials, weekly_production_report, phantom_css, ux]
---

# Session log — 2026-08-12 · A frontend design program on the Safety Portal SPA — the phantom-CSS bug family, and four PRs deployed live

## Summary

The operator reported the Schedule and Weekly Production Report pages as clunky, and the weekly
report as "extremely rough." The root cause of the latter was mechanical, not aesthetic:
`WeeklyReportPage` referenced fifteen `wr__*` class names that were never defined in any
stylesheet, so it rendered as raw unstyled HTML, and it was the only page in the portal skipping
`PageShell`. That finding drove a four-PR program across the day — Schedule + Weekly Production
Report redesign (#99), manual schedule-task editing (#103), job-detail + materials design with a
derived "outstanding quantity" the lane had never computed (#109), and a closing audit that found
sixty-two undefined class names app-wide and closed the bug family with a permanent CI guard
(#114). All four PRs are four-part verify clean and all four are deployed live to
`https://safety.evergreenmirror.com`. One reported bug (a percent-edit asymmetry between office and
field) was investigated and found to be correct behavior, not a defect — no change made. Visual
verification for the whole program was numeric rather than pixel-based: Playwright screenshots are
broken in this environment (a trivial static page times out), so every claim about overflow and tap
targets was proven by measuring computed styles and geometry in-browser instead.

## PRs landed

| PR | What | Merge SHA | Verify |
|---|---|---|---|
| #99 | Design pass on the Schedule + Weekly Production Report pages; fixes the unstyled-HTML root cause and a `hazard_topics` data-loss bug | `0106e8f2bf4bbd8532f34c4ef06cb0a9d7ade870` | four-part verify clean |
| #103 | Manual schedule-task editing (add/edit/deactivate) inside the existing row disclosure | `0207560ff49ee666faea154e7f9c788d679d76c8` | four-part verify clean |
| #109 | Design pass on job detail + materials tracking; derives the outstanding-quantity number the lane never computed | `678d3f48cdea6085fc4849bc2c37fd9549aa2e43` | four-part verify clean |
| #114 | Closes the phantom-CSS bug family — 62 undefined classes down to 14 explained allowlist entries, with a permanent guard test | `e3da945ccb02c3fcb81fc8ccadf02f50dc0ec6ac` | four-part verify clean |

**4 of 4 PRs four-part verify clean.**

### #99 — `feat(portal): design pass on the Schedule + Weekly Production Report pages`

```
PR #99 — four-part verify clean
          mergedAt 2026-08-12T18:34:39Z · mergeCommit 0106e8f2bf4bbd8532f34c4ef06cb0a9d7ade870
          main CI run 31628344332 → success (test / portal / secrets all success)
```

### #103 — `feat(schedule): the office can edit a task by hand, not only by re-importing`

```
PR #103 — four-part verify clean
          mergedAt 2026-08-12T19:08:47Z · mergeCommit 0207560ff49ee666faea154e7f9c788d679d76c8
          main CI run 31631208166 → success (test / portal / secrets all success)
```

### #109 — `feat(portal): design pass on the job detail view + materials tracking`

```
PR #109 — four-part verify clean
          mergedAt 2026-08-12T22:04:17Z · mergeCommit 678d3f48cdea6085fc4849bc2c37fd9549aa2e43
          main CI run 31645367421 → success (test / portal / secrets all success)
```

### #114 — `fix(portal): close the phantom-CSS bug family — 62 undefined classes down to 14, with a guard`

```
PR #114 — four-part verify clean
          mergedAt 2026-08-12T22:47:54Z · mergeCommit e3da945ccb02c3fcb81fc8ccadf02f50dc0ec6ac
          main CI run 31648425938 → success (test / portal / secrets all success)
```

## Deploys

Worker `its-safety-portal` → `https://safety.evergreenmirror.com`, one deploy per PR, in order:

| PR | Deploy (version id) |
|---|---|
| #99 | `974a30ab-e893-449f-ae7b-08214ab9f252` |
| #103 | `af7eeca9-e5d8-468e-aee2-fe8a65d0bcda` |
| #109 | `9c7e7fe4-bab4-46f4-85ea-ece8410a89fc` |
| #114 | `5190be5e-625e-43f4-b795-f95065855787` |

All four confirmed present in `wrangler deployments list --name its-safety-portal`.

## What was built

**#99 — Schedule + Weekly Production Report.** No new brand color, no new typeface — the design
thesis is that the portal's existing signature (the day-rail, a BRG rail with a gold tick marking
the phase of the day) extends to project scale, so gold means "where you are in time" product-wide.
New layer `src/styles/schedule-report.css`. Schedule page: hero, collapsible sections with rollups,
search + status filters, a CSS-only Gantt timeline (`src/components/ScheduleTimeline.tsx`), mark-off
moved into a per-row disclosure with 48px targets, office surfaces as `<details>` drawers. New pure
`src/lib/schedule_view.ts` derives LATE and SLIP against the immutable baseline anchor, and surfaces
the server's `truncated` flag — the page had been silently showing a partial schedule. Weekly
report: `PageShell`, an authorship design (sections tagged "Yours to fill" vs "From the field";
derived sections recede visually), sticky section rail, sticky save bar. Also fixed a data-loss bug:
the screen cannot edit `hazard_topics` but hard-coded `hazard_topics: []` on save, wiping it every
time.

**#103 — manual schedule-task editing.** `src/components/ScheduleTaskEditor.tsx`. The Worker has
always exposed add/edit/deactivate under `cap.jobtracker.manage`; nothing in the SPA called them
against a committed task list, so a one-line correction meant re-exporting from Smartsheet and
running a full reconcile. Gated separately from the field's `cap.schedule.mark` inside the same row
disclosure. The form warns that a hand-added task is absent from the next export (the next reconcile
reads it as a REMOVAL) and that renaming re-keys `match_key`.

**#109 — job-detail + materials design pass.** New `src/lib/materials_view.ts` derives the
OUTSTANDING quantity — the lane recorded expected and received and never subtracted them, so "what
is still owed to this job" was unanswerable; every field was already on the wire, so no backend
change. Job detail: wide well, hero of counts, section rail, and the archive danger-zone moved from
ninth of nineteen blocks to last. Kit-level fixes: `.dash-card`, `.dash__msg`, `.field__inline` were
referenced and never defined; `.dash-tasklist li` and `.dash-card__head` lacked flex-wrap; inputs
with hard `size=` attributes overflowed at 390px. Plus the operator's follow-up: the job page's
materials list now ships collapsed in a drawer and every BOM type group collapses.

**#114 — closed the phantom-CSS bug family.** An audit found sixty-two undefined class names →
21 dead references removed, 11 classes defined, 3 deletions refused after cross-checking against
test suites and Python renderers (`.receipt` is queried by a vitest suite AND a Python renderer;
deleting it would have thrown in one and silently vacated a negative assertion in another). New
guard `tests/test_portal_css_classes.py` with a 14-entry explained allowlist, plus a second test
that fails when an allowlist entry outlives its reference. Attempted in vitest first and it could
not read the stylesheets — Vite's CSS pipeline returns an empty string even with `?raw` — so it
scans TSX from Python, the `test_portal_button_variants.py` pattern.

## Decisions and corrections that belong in the record

1. **The milestone-percent asymmetry is not a bug.** `/edit` accepts a fractional percent on a
   milestone; `/progress` refuses it. This was reported as inconsistent, and is deliberate: office
   percent edit is CURATION (never stamps `last_marked_by`), a field mark is a MARK, and the
   reconcile's %-conflict predicate depends on that distinction. No change made — "fixing" it would
   have broken revision reconciliation.
2. **Cloudflare asset propagation was twice mis-read as a live incident.** Both deploys were fine.
   The error was fetching asset paths captured from an earlier cached HTML response; the correct
   check is to re-read the HTML each time and verify assets by content-type, since the SPA fallback
   returns 200 + `index.html` for a missing asset.
3. **A materials drawer initially rendered its shell without a capability gate**, showing an empty
   "Materials" disclosure to sessions lacking the cap. Caught by the existing M1 test before ship.
4. **`.dash-completed` stayed in the allowlist rather than being deleted.** It is undefined in every
   stylesheet but is queried by two test files — the obvious "delete the dead reference" cleanup
   would have broken one suite and made the other pass for the wrong reason.

## Method notes

- **Screenshots are broken in this Playwright build** — a trivial `<h1>` page times out — so all
  visual verification this session was numeric: computed styles, unclipped-overflow, and tap-target
  geometry measured in-browser at 390/768/1024/1440. Result: zero unclipped horizontal overflow and
  zero sub-44px tap targets on every redesigned surface.
- **Every new derivation and control was proved to red-light on an injected synthetic violation and
  then reverted** — weighted-percent, the late rule, the outstanding-quantity rule, the capability
  gate, the append-ordinal rule, and both CSS guards. (House Reflexes §2, prove-the-control-bites.)
- **`main` was landing PRs faster than an 8-minute CI cycle**, causing repeated BEHIND merge races;
  combining work onto one branch and running a single update-branch → CI → merge cycle was the
  workaround.
- **A throwaway Vite preview harness** (stubbed fetch + fixture data) stood in for the app because
  local wrangler D1 is broken on this host (see `reference_wrangler-local-d1-cf-alarm-fault.md`); it
  was deleted before each commit, and every CSS class added was checked to be USED before shipping.

## Open items / next session

Three low-severity tech-debt entries were filed at session close (`docs/tech_debt.md`, all tagged
`safety-portal`/`frontend`/`css`/`design`, all `[OPEN 2026-08-12, low]`), deliberately left open
rather than shipped this session:

1. Three of the CSS allowlist's 14 entries (`fr__job-reqs`, `fr__expected-materials`,
   `fr__additional-photos`) are inert wrappers left bare by choice, not permanently unstylable —
   revisit when a design pass next touches those `FormRenderer.tsx` sections specifically.
2. `ExpectedMaterialsSection`'s richer multi-control editor and a delivery-mark row were designed
   during #109 but deleted unwired rather than shipped as dead CSS — no code trace exists; the
   tech-debt entry is the only record, written from session context, not a diff.
3. Playwright screenshots are broken in this dev environment (see Method notes above); root cause
   not diagnosed — first diagnostic step is reproducing outside the MCP wrapper to isolate
   MCP-layer vs. Playwright-layer.

## What was NOT touched

- The milestone-percent asymmetry (see Decisions #1) — investigated, confirmed correct, left as-is.
- No backend/Worker route changes in #109 or #99's schedule/materials work — both derivations
  (`schedule_view.ts`, `materials_view.ts`) work from fields already on the wire.
- No rename of any existing CSS class outside the phantom-CSS closure in #114 — the 3 refused
  deletions were deliberately left alone after cross-checking live consumers.

## Cross-references

- `~/its-blueprint/references/memory-archive.md` §G84 — the deep narrative for this same program
  (written mid-session, while #114 was still open; this log records all four PRs as landed and
  deployed, superseding that in-flight status note).
- `docs/tech_debt.md` — three `[OPEN 2026-08-12, low]` entries filed at session close (see Open
  items above).
- `docs/session_logs/2026-08-12_adr0006-schedule-payment-tracking-full-lane.md` and
  `docs/session_logs/2026-08-12_weekly-production-report-schedule-and-debt.md` — other same-day
  session logs covering different PR ranges (#80/#84/#85/#90/#91/#92/#93 and #97/#100/#101
  respectively); not the same work as this log, cited here only so a reader scanning 2026-08-12
  doesn't mistake one for the other.
- `docs/HOUSE_REFLEXES.md` §2 (prove-the-control-bites — every new derivation/control this session
  was red-verified before shipping) and the Playwright-screenshot-broken note under §7 (known
  platform gotchas) as a candidate future addition.
- `docs/operations/pr_merge_discipline.md` — the four-part verify applied to all four PRs above.

## Verification (final state, PR #114)

```
- pytest: 5450 passed / 4 skipped / 58 deselected
- mypy: clean / 503 source files
- ruff: clean
- main-branch CI on merge commit: SUCCESS
```

Additional gate figures on the merged tree, beyond the standard four-part quartet: worker vitest
1520 passed (77 files); SPA vitest render-smoke 976 passed (68 files); typecheck clean across all
three tsconfigs.
