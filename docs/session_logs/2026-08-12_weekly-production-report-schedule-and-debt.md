---
type: session_log
date: 2026-08-12
status: closed
related_prs: [97, 100, 101]
workstream: progress_reports
tags: [session_log, weekly_production_report, progress_reports, adr_0006, tech_debt, regression, live_deploy]
---

# Session log — 2026-08-12 · The report reads the schedule, the lane's debt clears, and one regression I shipped

**Continues from:** `docs/session_logs/2026-08-11_weekly-production-report.md` — that log covers
the build (#81 #82 #83 #87 #88 #89). This one covers the day after: binding page 3 to the schedule
the ADR-0006 lane finished overnight, clearing the two debts that build left, and the regression
the second of those fixes introduced.

## Summary

Three PRs, three deploys. The Weekly Production Report is now complete end to end — every section
of the office's 5-page template populated from live data, page 3 included — and the lane carries
zero open tech-debt entries. One regression was shipped and fixed the same session; it never
reached a client.

## PRs

| PR | | Merge |
|---|---|---|
| #97 | page 3 reads `job_schedule_tasks` (ADR-0006 migration 0071) | `ab527989` |
| #100 | the two debts #88 filed: packet suffix, per-field narrative | `fc70aaa2` |
| #101 | carry-forward was bringing last week's narrative forward | `3df67dd5` |

## What the real schema changed (#97)

The binding was planned against the ADR-0006 lane's design doc; the shipped table differed in one
way that mattered. `percent_done` is `NOT NULL DEFAULT 0`, so the column **cannot represent
"never reported"** — the state the renderer's em dash exists for. The route returns NULL only when
nothing has ever asserted a value: no portal mark (`last_marked_by IS NULL`), no committed schedule
value (`schedule_percent IS NULL`), and `percent_done` still 0. Printing 0% there would tell a
client no work was done, a different and possibly false claim from "nobody has reported".

Found by reading migration 0071 rather than working from the plan's description of it.

Grouping is by `section` in DOCUMENT order (`sort_order`), not alphabetical — the order the office
and client already read on the Gantt. A task with no section lands under "Other" rather than being
dropped; against the live Coker schedule that catches 14 of 66 active tasks, so it is not
hypothetical.

## The two debts (#100)

1. **`packet_suffix` parameterized** exactly as `cover_title` was for the identical class. Safety
   keeps `_WSR` byte-for-byte and the test now pins BOTH bindings. Progress binds `FieldRecords`,
   so a Box week folder reads `..._WPR.pdf` (the client's) beside `..._FieldRecords.pdf` (the
   record) instead of two files distinguishable only by size.
2. **Narrative touched-ness moved from ROW-level to PER FIELD** — three-state, the shape `photos`
   already used: `null` = never touched (seed), `""` = deliberately cleared, text = the office's
   words. `_office_or_seed` no longer takes `saved` at all.

## The regression (#101) — and why the test missed it

Fixing (2) exposed a second behaviour the old flag had been masking. Carry-forward copies the prior
office row; it was also copying `narrative_json`. Under the row-level rule this never showed
(carried ⇒ not saved ⇒ the seed won, so the carried text was never consulted). Removing `saved`
from the decision made the carried text win, and the week of 08-15 came back seeded with "Rain day
08/12 — crew released".

**The test I wrote for exactly this property passed while the behaviour was wrong.** It mocked the
payload, so it asserted the ASSEMBLER's handling of a `None` narrative and never exercised the
Worker's carry-forward that produced the value. A test that mocks the layer under test proves only
that the mock works. The replacement drives the real route — save a narrative on one week, read the
next — and RED-lights against the shipped code.

Found by reading the live route on a carried week, not by any suite. Third time this session-pair
that looking at real output beat green tests.

**Exposure:** live for roughly 30 minutes, only on weeks with no office row of their own, only in
pre-fill text the office sees before saving. Nothing was sent.

## A second mistake, in reporting

Told the operator that a `hazard_topics` bug was still present "on current main" while `~/its` had
only been `fetch`ed, not pulled — so the file being read was the pre-#99 working tree. Fetch moves
the remote ref, not your files. Corrected in the same exchange after re-reading the real file.

## Deploys

Three, each scope-checked before shipping rather than assumed:

- after #97 — scope was exactly one commit (mine); the earlier 16:59 deploy had covered everything
  through `e3500fa`
- after #100 — also carried another session's #98, whose only deployable change was an archive
  panel count ("seven containers" → "eight") that matched their already-live Mac behaviour, so
  deploying made the UI consistent rather than risking it
- after #101 — pulled to current main FIRST at the operator's instruction, having captured a
  `wpr-photos` marker from the live bundle to prove the concurrently-deployed #99 design pass was
  not regressed. Bundle hash unchanged (Worker-only change), marker still present, all routes
  fail-closed.

## Verified live

Full report assembles from the deployed route: 6 schedule sections / 66 tasks / 26 behind, 5
weather days, 3 labor companies, 4 hazard topics, 4 deliveries, office OSHA counts and pending
items. Carry-forward returns NULL narrative on a carried week and the owning week's text on its
own week.

## Verification

- pytest: 5362 passed / 2 skipped / 58 deselected (excluding `tests/test_publish_daemon.py`, red
  on this host from the conftest live-state guard, green in CI)
- vitest worker: 1520 passed / 77 files · vitest SPA: 910 passed
- mypy: clean / 499 source files
- ruff: clean
- main-branch CI on merge commits: SUCCESS (#97, #100; #101 confirmed at close)

## Open

- **Mock demo data** under JOB-000031 "Test" in production D1, left deliberately so the office
  screen can be exercised against a populated job. Cleanup script staged; note it also removes two
  `material_catalog` rows (9001/9002) that are NOT job-scoped.
- **The committed Coker schedule has parse noise** a client would see — the project name became a
  section, 14 tasks are unphased, and names like "Electrical 30%" are milestone rows read
  literally. The report renders the schedule faithfully rather than cleaning it up (runbook
  Symptom 4b); fix belongs on the Schedule page's validate/reconcile screen.
- **Zero open tech-debt entries** remain for this workstream.
