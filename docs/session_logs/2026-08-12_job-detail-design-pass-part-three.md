---
type: session_log
date: 2026-08-12
status: closed
related_prs: [119]
workstream: safety_portal
tags: [session_log, safety_portal, frontend, css, design, job_tracker, jobtracker, ux]
---

# Session log — 2026-08-12 evening · Job-detail design pass, part three — the two-column shell, three real bugs, and a verifier that withheld its verdict

## Summary

Part three of the job-detail conversion begun by #99/#109: the page gains the eyebrow+h1 heading
pair, the WPR two-column shell (sticky left rail ≥1024px, chip scroller below — an operator-ratified
reversal of #109's own chips-at-every-width decision), a working scroll-spy (the CSS for it shipped
in part two; nothing had ever set the attribute it targets), section heads with body-face counts,
new rail entries for Materials/Daily-requirements/Danger-zone, capability-branched empty states, and
a `.job-shell`/`.job-time--voided`/`.job-link` cleanup that finishes converting the page to house
style. One PR, one commit, landed clean. Three real bugs were found and fixed along the way, one of
them shared with the Weekly Report page and worse there (a silent draft-discarding remount). Deploy
shipped exactly this PR's scope. The session closes on a deliberately unresolved note: the
`pr-landed-verifier` proved all four legs individually but declined to say "four-part verify clean,"
because it could not vouch that the repo it was checking (`its-sys-admin/evergreen-its`) is the one
its own brief recognizes as canonical — a doc-staleness question, not a landing question, resolved
by the orchestrating session afterward. This log reports that withholding verbatim rather than
smoothing it into a clean claim.

## PRs landed

| PR | What | Merge SHA | Verify |
|---|---|---|---|
| #119 | Design pass on the job detail view, part three — the two-column shell | `a4e7bd0c46619331d8eb97c9f6a3241c1ee31e5c` | see "The verifier withheld its verdict" below — do not read as "four-part verify clean" |

### #119 — `feat(portal): design pass on the job detail view, part three — the two-column shell`

One commit, 8 files, 675 insertions / 137 deletions. Merged 2026-08-13T00:15:05Z UTC (squash).

Finishes the job-detail conversion to the Schedule/WPR house style begun by #99/#109:

- **Eyebrow + h1** heading pair, matching the Schedule/WPR precedent.
- **The WPR two-column shell** — `.job-shell` reuses the `.wpr` grid recipe: sticky vertical rail at
  ≥1024px, chip scroller below at narrower widths. This is a deliberate REVERSAL of #109's own
  chips-at-every-width decision, ratified by the operator via a plan-mode question this session; the
  CSS comment recording the old rationale was rewritten rather than left to contradict the new
  layout.
- **Scroll-spy that actually spies** — the WPR IntersectionObserver pattern, now driving
  `aria-current` on `.job-rail__link`. Part two had already shipped the CSS for the active state;
  nothing in part two's code ever set the attribute, so the spy was dead on arrival until this PR.
- **Section heads** — `.job-sec__head/__title/__count` with body-face counts: "3 open · 4 shown"
  ("shown," never a total, because legs are paged), "15.50 h logged."
- **Rail entries** for Materials / Daily requirements / Danger zone, each gated by its own section's
  existing capability gate.
- **`.sched-empty` empty states** with capability-branched next-move copy.
- **`.wpr-banner --ok/--warn`** replacing the flat `.banner` family.
- **Three `.sched-skel` shimmers** for a pending detail load.
- **16 bare `btn--*` sites** gained the `.btn` base class.
- **`.job-link`** BRG left-rail accent on the two navigate-away cards.
- **`.job-time--voided`** replacing three inline styles on the voided time row.
- New pure `safety_portal/src/lib/jobtracker_view.ts` (`openTaskCount` / `loggedHours` voided→0
  null→0 / `fmtHours` / pill maps) + `__tests__/jobtracker_view.test.ts` — the `schedule_view.ts` /
  `materials_view.ts` posture, carried to the job-detail page.
- README styles line fixed; a tech-debt entry filed (see below).

## Three real bugs found and fixed in the same PR

1. **Deep-link fall-through.** `view==="detail"` with the fetch still pending fell through the
   `&& selectedJob` gate and rendered the LIST for the load window of every cold `/jobs/:id` visit.
   A dedicated skeleton branch now owns that state instead of falling through to the wrong view.
2. **Danger-zone wrapper rendered for every viewer.** Only the inner `JobArchivePanel` self-hid —
   the surrounding wrapper (including the red label) rendered regardless of capability, contradicting
   its own comment, which claimed "this whole block self-hides." The wrapper now gates on
   `cap.job.archive` AND the worker-withheld `job.archive`.
3. **Rail chips remounted the page on every tap.** The chips were fragment navigations; a fragment
   navigation fires `popstate`, and App's popstate handler bumps `popEpoch`, which REMOUNTS the
   routed page — so every rail tap silently reloaded the detail view at scroll 0. Live-proven in
   headless WebKit (`popped:1, remounted:true`, scroll position never moved). **WeeklyReportPage's
   identically-built rail has the same defect with a worse consequence**: the remount SILENTLY
   DISCARDS an unsaved weekly-report draft, because the `beforeunload` guard only covers real
   browser unloads, not an in-app remount. Both rails now `preventDefault` the click and
   `scrollIntoView` directly, never touching history; regression-tested via `fireEvent.click`'s
   `defaultPrevented` return value.

## Tech debt recorded (filed in-PR)

`docs/tech_debt.md` — "Safari/WebKit ignores `min-height` on native `<select>` — the kit's 44px
tap-target floor silently fails portal-wide in the operator's own browser" `[OPEN 2026-08-12, low]`.
Both the kit's inline-row rule and this PR's own job-shell floor declare the 44px minimum, and
WebKit lays native menulist selects out at ~19–25px anyway — Safari does not honor height/min-height
on `appearance: auto` selects; Chrome does. The operator browses in Safari, so the "zero tap targets
under 44px" claim from the #99/#109 commit messages holds in Chrome and quietly fails on the
operator's own machine. Fix is kit-level (`appearance: none` + custom chevron), deliberately deferred
to its own four-width pass rather than a per-page patch. `ChipX` (17×14px) noted as the same kind of
judgment call for that future pass.

A second, session-close entry (`[OPEN 2026-08-12, low]`) records the one residual of bug #3:
`WeeklyReportPage`'s rail fix landed in #119 but has no page-local `defaultPrevented` regression
test (the job-detail rail has one), and a same-night sweep confirmed no OTHER bare fragment-anchor
site exists in the SPA. (The entry's first draft wrongly claimed the WPR fix was deferred out of
#119; corrected the same night — the fix is live at `WeeklyReportPage.tsx` ~:415-424.)

## Verification

- pytest: not run this session (no Python source changes); the two portal CSS/button guards ran
  green standalone — `tests/test_portal_css_classes.py` + `tests/test_portal_button_variants.py`,
  4 passed
- mypy / ruff: not applicable this session (no Python source changes)
- SPA vitest: 992 passed / 0 failed (976 baseline + 10 new `jobtracker_view` lib tests + 6 new
  chrome/rail tests; one forced edit — `FieldOpsJobTracker.test.tsx`'s voided-row inline-style
  assertion became a class assertion, tracking the `.job-time--voided` change)
- worker vitest: 1520 passed (byte-identical to baseline — this PR touches no Worker code)
- typecheck: clean across all 3 tsconfig projects
- vite build: clean (CI carries no build step — this is the only build gate, and it is local-only)
- main-branch CI on merge commit: SUCCESS (run `31653785682`; `test` + `portal` + `secrets` all
  SUCCESS)

## Live render verification

Chrome extension unavailable this session; verified in headless Playwright-WebKit instead (playwright
1.62.1, scratchpad-installed against the cached webkit-2336 build). **Note for the record:** full-page
screenshots WORKED in this run, unlike the note in the same-day
`2026-08-12_safety-portal-frontend-design-program.md` log, which reported Playwright screenshots
broken in its environment — that finding derived from a different environment path and does not
generalize; don't read the two as contradictory evidence about the same tool.

Served via `npm run build && npx wrangler dev --local`, because vite dev's react-refresh inline
preamble is CSP-blocked headless. Local D1: 73 migrations applied, `test.pm` promoted to admin,
seeded job JOB-000031 (crew/tasks including an overdue task/time including a void chain/equipment/
inspections) plus empty job JOB-000032 as the no-data case.

At 390/768/1024/1440px:

- Zero unclipped horizontal overflow at any width.
- Shell collapses to one column below 1024px; `196px + body` two-column layout at 1024px and 1440px.
- Scroll-spy lights the tapped section at every width, post rail-remount fix.
- Cold `/jobs/:id` shows skeletons and never the list, post deep-link-fall-through fix.
- Five empty states render with their branched next-move copy.
- The voided time row strikes through with its pill still legible.
- The amend banner computes the `--ok` family correctly.

## Deploy

Production was sitting at #114's deploy (version `5190be5e-625e-43f4-b795-f95065855787`, per the
prior same-day session log), so this deploy's scope was exactly #119 — nothing else had landed on
`main` in between. `npm run deploy` from `~/its/safety_portal` → version
`8a4ed389-d1ed-4a8c-85dc-2d189dce90d9`, served from `https://safety.evergreenmirror.com` (custom
domain). Asset hashes rotated as expected: `index-DezTYEo2` → `index-Bsi8Tj1C` (JS),
`index-WeLu-QxJ` → `index-C58Tyaqw` (CSS); the `react-vendor`/runtime chunks stayed stable, which is
the chunking design working as intended, not a partial deploy. Login page returned 200.
In-page verification of the deployed detail view is left to the operator's own eyeball — no
production credentials exist for automated verification, and D1 migration 0002 is local-only by
design, so the deployed job-detail view itself was not re-checked against real production data this
session.

## The verifier withheld its verdict — quoted verbatim

The `pr-landed-verifier` was invoked against PR #119 and reported:

> "state: MERGED / mergedAt: 2026-08-13T00:15:05Z / mergeCommit.oid:
> a4e7bd0c46619331d8eb97c9f6a3241c1ee31e5c / main CI on merge commit: polled to completion — run
> 31653785682, workflow ci, all three jobs (test, portal, secrets) SUCCESS"

and then:

> "Individually, all four legs pass — but I am not emitting 'four-part verify clean,' because that
> phrase is reserved for a verified landing in the repo my brief actually recognizes as canonical,
> and I can't currently vouch that its-sys-admin/evergreen-its is that repo."

This is not a failed leg — every one of the four legs the verifier checked came back positive. It is
the verifier declining to emit the load-bearing summary phrase over a repo-identity question outside
the scope of the four-part check itself: `CLAUDE.md` and older agent briefs still carry
`SolutionSmith-debug/its` in places, and the verifier's own brief did not treat
`its-sys-admin/evergreen-its` as pre-cleared.

The orchestrating session resolved the identity question by a timeline check, not by asserting it
away: `SolutionSmith-debug/its` last pushed 2026-08-07 — cutover day — and is unarchived but stale;
`its-sys-admin/evergreen-its`, created 2026-07-25, carries every push since, including the operator's
own session-log PRs #115/#117 from earlier the same day. This is doc staleness in the older briefs,
not a hijacked remote or a landing failure. The `repo-identity-is-evergreen-its` memory entry was
written to close the gap for future verifier runs.

Per this repo's own writing rule, that resolution is recorded here as what it is — a repo-identity
finding made by the orchestrating session after the verifier ran, not a retroactive "four-part verify
clean" on PR #119. All four legs are individually proven and quoted above; the summary phrase itself
was never emitted by the verifier and is not asserted here on its behalf.

## Decisions made during session

1. **Rail layout reverses #109's chips-at-every-width choice.** Ratified via a plan-mode question to
   the operator rather than assumed; the CSS comment recording the old rationale was rewritten so it
   does not contradict the new sticky-rail behavior.
2. **The Safari/WebKit select tap-target gap is deferred, not patched per-page.** Fixing it correctly
   requires an `appearance: none` treatment applied kit-wide, which would restyle every select in the
   app — that is its own four-width pass, not a rider on this PR. Filed to tech debt instead.
3. **The verifier's withheld verdict is reported as withheld, not resolved into a clean claim.** The
   four legs it checked are all positive and are quoted individually; the repo-identity question it
   raised is answered above by the orchestrating session's own timeline check, kept visibly separate
   from the verifier's output rather than folded into it.

## Open items / next session

- Kit-wide Safari/WebKit select tap-target fix (`appearance: none` + custom chevron), plus the
  `ChipX` (17×14px) judgment call — both filed to `docs/tech_debt.md`, `[OPEN 2026-08-12, low]`,
  revisit at the next kit-wide form-control pass.
- Worktree `/Users/itsmacbook/its-jobdetail-facelift` left in place for operator cleanup, per
  `docs/operations/worktree_discipline.md`.
- In-page production verification of the deployed job-detail view is still operator-eyeball only —
  no automated check exists against production data (no production credentials, and D1 migration
  0002 is local-only by design).
- Older agent-facing docs (`CLAUDE.md` and any agent brief predating the 2026-08-07 host migration)
  still name `SolutionSmith-debug/its` in places; a future doc pass should sweep these so a fresh
  verifier run doesn't have to re-derive the same timeline finding this session did.

## What was NOT touched

- No Worker code changed — worker vitest is byte-identical to baseline, confirming this PR is
  presentational/frontend-only.
- No backend route or capability model changed; every gate referenced (`cap.job.archive`, the
  section gates feeding the new rail entries) already existed and is reused as-is.
- The Safari/WebKit select tap-target defect was diagnosed and filed, not fixed — deliberately
  deferred to its own kit-wide pass (see Decisions #2).
- Production job-detail view was not re-verified against live production data post-deploy (see Open
  items).

## Ops notes

- Post-merge `git pull` of `~/its` was blocked by an untracked, byte-identical duplicate of the #115
  session log; removed after a diff confirmed the two files were identical, not a real conflict.
- The merged remote branch `feat/jobdetail-facelift` was deleted via `gh api` after `gh`'s
  `--delete-branch` local-cleanup step aborted on a main-checkout conflict.
- Worktree `/Users/itsmacbook/its-jobdetail-facelift` left for operator cleanup per worktree
  discipline (not force-deleted from this session).

## Cross-references

- `docs/session_logs/2026-08-12_safety-portal-frontend-design-program.md` — parts one/two of the
  same job-detail conversion (#99, #109) plus the phantom-CSS closure (#114); read together with
  this log for the full three-part arc. Its Playwright-screenshots-broken note does not generalize to
  this session's environment (see "Live render verification" above).
- `docs/tech_debt.md` — the Safari/WebKit select tap-target entry filed this session,
  `[OPEN 2026-08-12, low]`, tagged `safety-portal`/`frontend`/`css`/`design`.
- `docs/operations/pr_merge_discipline.md` — the four-part landing verify; this session's verifier
  output is a case of all four legs passing without the summary phrase, quoted verbatim rather than
  paraphrased into "clean."
- `~/.claude/projects/-Users-itsmacbook-its/memory/repo-identity-is-evergreen-its.md` — the memory
  entry written to close the repo-identity gap this session's verifier surfaced.
- `docs/operations/worktree_discipline.md` — governs the leftover
  `~/its-jobdetail-facelift` worktree noted under Open items.
