---
type: session_log
date: 2026-08-11
status: closed
related_prs: [81, 82, 83, 87, 88, 89]
workstream: progress_reports
tags: [session_log, weekly_production_report, progress_reports, client_facing, external_send_gate, operator_dashboard, live_deploy]
---

# Session log — 2026-08-11 · The Weekly Production Report: the progress lane gets a client-facing artifact

**Trigger:** the operator pointed at `~/Desktop/WPRs` (55 client-facing reports, four document
families) and asked to verify the current weekly progress rollup, reconcile it with those
documents, and land an end state where each week's job progress becomes a report the office sends
to the client.

## Summary

Six PRs landed, five with four-part verify clean at time of writing (#89 in CI). Migration 0067
applied to production D1 and the Worker deployed mid-session; the client-facing report generates
from Friday's compile against already-deployed backend code.

**What the verification found.** `progress_weekly_generate` was emailing clients
`merge_pdfs([cover, rollup_page, index, *every daily-report PDF])` — a 30–70 page stack of
internal field paperwork (PPE attestations, OSHA compliance walks, CM check-ins, a
Standards-of-Conduct guidance page) behind a fixed seed body reading *"please see the attached
documents for the week of…"*. No prose, no percentages, no curated photos. The lane was live and
had run the previous Friday.

**What the reconciliation found.** Most of the target document is an aggregation problem, not a
capture problem: `daily-report-v7` already records weather, average temp, manpower, Crew/
Subcontractor Progress, QC spot checks, deliveries received, equipment on site and Tomorrow's
Progress Goals, all queryable from `submissions.payload_json`. Three sections are NOT derivable
and never will be — the six OSHA case counts (`incident-report-v3` carries no case
classification), labor-by-company man-hours (`personnel` has no employer column), and pending
RFIs/submittals/COs (tracked nowhere) — plus two judgment calls that must stay human: which days
are inclement weather days (a contractual delay claim) and which photos represent the week.

## Operator decisions

Taken by AskUserQuestion before any code, and each one changed the build:

1. **Target format** — the classic 5-page *Evergreen Weekly Production Report*, not the April-2026
   Bonacci narrative variant and not the client-specific Brookfield Exhibit N.
2. **Percent-complete** — *"do not build this it will read from the job schedule page that is
   currently being built."* This made the ADR-0006 lane a hard dependency and kept a whole
   capture surface out of scope.
3. **Narrative** — deterministic assembly from captured field text, **no AI**.
4. **Daily reports** — stay internal; they become the source record, not the client's attachment.
5. **Weather** — render only what is captured (conditions + avg temp), not the template's
   max/avg/min temperature, humidity and wind columns.
6. **Gap sections** — one weekly office-input screen, carried forward week to week.
7. **Empty week** — HELD for the office to decide, not silently sent or silently skipped.

## PRs

| PR | | Merge |
|---|---|---|
| #81 | aggregation route + office record (migration 0067) | `173e3e67` |
| #82 | `form_pdf.render_production_report` — the 5-page renderer | `826ac1c0` |
| #83 | `wpr_data` assembler + the swap (report to client, dailies internal) | `a48786f3` |
| #87 | the weekly office screen + two defect fixes | `4e0fe04d` |
| #88 | tech-debt close-out (3 entries) | `66d67582` |
| #89 | dashboard descriptions that still said "packet" | in CI |

## Defects found, and how

**Two `json_each` defects (#81), caught by a test I wrote expecting it to pass.** SQLite evaluates
a table-valued function as it builds the row source, BEFORE the WHERE clause can filter — so a
WHERE-side `json_type(...) = 'array'` guard does not protect it. One daily report with a malformed
`crew_progress` would have 500'd the entire weekly report, for every job and every week, until a
human found it. The first fix used the one-argument `json_type(json_extract(...))`, which
re-parses an already-extracted scalar and raises on the identical value; **the suite stayed red
through that fix**. The two-argument `json_type(doc, path)` is what works.

**Two defects from rendering the document and looking at it (#87).** After 109 green tests across
the three build PRs, a live render against production data found: safety-meeting names duplicating
their own variant on page 1 (*"Toolbox Talk — Severe Weather (Tornadoes, High Winds, Lightning and
Flooding) — Severe Weather — Tornadoes, High Winds, Lightning and Flooding"* — the containment
check compared raw strings, so parentheses-vs-em-dash read as new information), and both narrative
sections rendering blank. The second turned out to be a real data-model coupling, not a bug —
recorded as tech debt rather than papered over (see below).

**Four stale operator descriptions (#89), caught by reading rather than running.** The parity
tests were green throughout and stayed green: nothing structural was missing. What was wrong was
that the `/system` map blurb, two node briefs and a troubleshooting-tree step title all still
described the pre-report behaviour. The tree step title only surfaced because the dashboard's
loader was exercised to confirm it *resolves* the new nodes, rather than trusting that editing
`tree.yaml` was sufficient.

## Non-obvious decisions

**Migration 0067, not 0066.** The parallel ADR-0006 schedule session had claimed 0066 in its plan.
Taking 0067 avoided the collision; 0066 landed hours later exactly as their plan said.

**Two opt-in `GenerateConfig` seams, not a fork.** `client_report_provider` and `empty_week_hold`,
both defaulted off, following the `rollup_page_provider` precedent — so the safety compile is
byte-identical (§14). The provider closure lives in the progress module so `generate_core` gains
no `portal_client` / keychain / renderer import and stays a pure engine (§42).

**`Send Status = HELD`, uppercase, with `held_no_activity` in Notes.** The lowercase `held_*`
strings are RESULT codes (the `weekly_send._mark_held` contract); writing one into the
picklist-gated cell would raise `PicklistViolationError`. This removed a registry change the plan
had wrongly anticipated.

**A failed report falls back to the packet and WARNs** rather than raising. A weekly cadence that
quietly stops is worse than one wrong-shaped document with a WARN in ITS_Errors.

**The three-state photo contract.** `NULL` = auto-select, `[]` = explicitly none, list = the
office's picks. Collapsing the first two would silently re-populate a photo page the office had
deliberately cleared — the reason a present-but-malformed `photos` value now 400s instead of
degrading.

## Live deploy

Verified scope before touching production: the schedule session's 0066 was already applied and
their Worker routes already deployed (an initial 404 probe was a wrong path guess on my part, not
a missing deploy — re-checked before reporting it). Their newest commit was Python-only, so the
deploy published only this lane's work.

Applied 0067 `--remote`, deployed via `npm run deploy` (vite first). Verified live: both routes
answer 401 rather than 404 (an initial 404 on the session route was propagation, confirmed by
re-probe before reporting a defect); an authenticated call returns real production data
(`hazard_form_codes: ['jha-v3']` from an actual filed JHA); and all four malformed-window cases
return 400 with **no data field leaked**.

**#87 is merged but NOT deployed.** By then #86 (procurement) had landed with 7 deployable files
and migrations 0069/0070 unapplied — deploying would have published another session's lane and
required their migrations first. Left to that session, with the reasoning surfaced to the operator.

## Tech debt filed (#88)

1. Progress packets still file as `..._WSR.pdf` — safety's suffix on a progress artifact,
   pre-existing, now visible beside a correctly-named `_WPR.pdf` in the same Box folder.
2. **The `saved` flag is ROW-level** (medium) — it decides per FIELD whether the office's value
   beats the derived seed, which is right for an edited field and wrong for an untouched one. The
   office screen compensates by pre-filling, so a client-facing document's correctness currently
   depends on a screen behaviour rather than a storage invariant.
3. Page 3 stays an empty state until `job_schedule_tasks` lands.

## Corrections issued

Told the operator twice that the fail-open send gate meant reports could go out unattended on
Friday. That was wrong: `progress_send_poll` only dispatches rows a human has approved through the
F22 gate. The gate failing open means the daemon runs, not that it sends. Corrected on verifying
the live code — which also showed the posture is report-lanes-fail-open (safety AND progress) vs
procurement-lanes-fail-closed, a more precise framing than the runbook's "the exception".

## Verification

- pytest: 5354 passed / 2 skipped / 58 deselected (excluding `tests/test_publish_daemon.py`, red
  on this host from the conftest live-state guard, green in CI)
- vitest worker: 1397 passed / 73 files · vitest SPA: 862 passed / 61 files
- mypy: clean / 496 source files
- ruff: clean
- main-branch CI on merge commits: SUCCESS (#81, #82, #83, #87, #88)

## Open

- **Deploy of #87** — entangled with the procurement session's unapplied 0069/0070.
- **Page 3** — blocked on the ADR-0006 living task list (their PR-4). The binding is additive and
  the renderer already handles both states.
- **Mock demo data** under JOB-000031 "Test" in production D1, left deliberately so the office
  screen can be exercised against a populated job; cleanup script staged. Includes two
  `material_catalog` rows (9001/9002) that are NOT job-scoped.
