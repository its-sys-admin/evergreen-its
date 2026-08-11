---
type: session_log
date: 2026-08-11
status: closed
related_prs: [43, 49, 52, 56, 57, 65]
workstream: field_ops
tags: [session_log, field_ops, archive_on_closure, watchdog, tech_debt, box, smartsheet, deploy, house_reflexes]
---

# Session log — 2026-08-10 → 2026-08-11 · Archive follow-ups: the restore drill, six PRs, a deploy, and a tech-debt trim under cap

**Filename note.** This session continues directly from the (as of this writing, uncommitted)
`docs/session_logs/2026-08-10_archive-button-diagnosis-and-live-drill.md`, whose only PR was
**#33** (merged `2026-08-10T19:05:07Z`). Everything below happened after that merge. Two other
same-day logs exist for unrelated concurrent sessions —
`2026-08-10_outage-diagnosis-alerting-gap-and-gate-activation.md` (#11–#16) and
`2026-08-10_pr4-materials-delivery-workflow-completion.md` (#38, #40, #45, #48, #50) — not
touched here; PR #50 from the latter and PR #47 from
`2026-08-10_preop-inspection-forms-and-checklists.md` (#44, #47, #51) surface below only as
undeployed Worker code this session's deploy step had to account for.

## Summary

Six PRs landed (#43, #49, #52, #56, #57, #65), all four-part clean, plus one attended live
drill and one production deploy. The session closed out every issue the prior session's
diagnosis had filed (#24–#27, #29, #30) and one it filed against itself mid-stream (#42,
recovered after an earlier tech-debt archival pass nearly destroyed the only written record of
the gap it described). The arc: fix the archive's partial/terminal messaging and heartbeat
(#43) → drill the restore direction live for the first time, then re-archive to prove adoption
not duplication (closing #42) → ship the §43 runbook the archive had shipped without (#49) →
split platform constraints out of an over-cap tech-debt file (#52), then finish the trim under
cap while catching a doc-lint bug that had been linting nothing from every workflow-agent
worktree (#56) → build watchdog Check X (archive detector) and fix a Box liveness probe that
reported healthy through a live auth failure (#57) → deploy four PRs' worth of accumulated
Worker code in one operator-run pass → build watchdog Check Y to run `verify_cutover.py`'s
VC-03 daily instead of never, closing the actual root cause of the whole prior diagnosis (#65).
Three corrections to my own prior claims are recorded plainly below, per the discipline this
session was otherwise enforcing on everyone else.

## The near-miss that filed issue #42

Before any of the numbered PRs below, an earlier tech-debt archival pass this session ran found
a `docs/tech_debt.md` entry reading *"Track 6 archive … three activation gaps remain"* and,
seeing two of the three gaps genuinely closed on 2026-08-10 (the config rows now existed, 21
launchd jobs loaded), archived the whole entry as resolved. An adversarial re-check caught that
the third gap — *"the Box leg has never been drilled live, either direction"* — was only half
true: the **archive** direction had been drilled the same day, but the **restore** direction had
not, on either system, and no other tech-debt entry or issue covered it. Archiving the entry
would have destroyed the only written record of a real, operator-reachable, never-exercised
production path (`field_ops/job_archive.py`'s restore branch — a distinct code path, not a
mirror of archive: the Smartsheet two-call order inverts per direction, and a live-folder-name
collision refuses rather than merging). Issue **#42** was filed at `21:07:05Z` to be that record.

This near-miss is why the later tech-debt trim (PR #56) was run under an explicit rule — see
Decision 3 below.

## PRs landed

### #43 — `fix(archive): a stopped archive was invisible and the UI told the operator to wait for a retry` (closes #29, #30)

A `partial` or `failed` archive is **terminal** for the daemon — the queue serves only
`archive_state IN ('requested','in_progress')` — but two defects hid that fact. **#29:**
`_archive_pass`'s per-cycle tally counted `partial`/`failed` into `items_processed` but never
into `total_errors`, so `cycle_status` resolved `"OK"` and the heartbeat stayed green on a job
split across two systems with one WARN row as the only trace. Stopped archives now count toward
the cycle's errors (DEGRADED), named distinctly from ordinary pass failures because "it will try
again next cycle" is true of every other error here and false of this one. **#30:**
`JobArchivePanel` told the operator "The system retries automatically" on exactly the states
that do not retry, actively steering them away from the "Try again" control sitting beside the
banner; corrected to name the control. The PR also lands two system-map joins found while
scoping #25 (`archive_enabled` into `fieldops_sync`'s `extra_gates`; `job_archive` into its
`error_scripts`) — joined to the daemon's own node rather than given a new one, since the
archive has no plist, heartbeat, or gate of its own.

**PR #43 — four-part verify clean.** `state=MERGED` · `mergedAt=2026-08-10T21:18:44Z` ·
`mergeCommit=a1fbd9863c04772156c840400133e0b31b157fd3` · main-branch CI on the merge commit
**SUCCESS**. Independently re-verified against live GitHub, not taken on report.

### Live drill — restore, then re-archive (closing #42)

Operator pressed **Un-archive** in the portal on `JOB-000030` ("Production test") — deliberately
not driven by a hand-written D1 row, because the browser route is the path under test: it stamps
state **and** writes the audit row. Daemon logged `archive complete=1`. Verified against the
trees, not the log:

| Container | Restored to | Folder id |
|---|---|---|
| Smartsheet safety | `ITS –– Safety Portal` | `1566626123409284` — unchanged |
| Smartsheet progress | `ITS — Progress Reporting` | `553426158413700` — unchanged |
| Box safety | `ITS Safety Reports` | `407341446878` — unchanged |

Week sheets and the filed PDF intact; both archive `<Job>` shells left completely empty
(`folders=0 sheets=0 reports=0`, Box item count 0); D1 reset to neutral
(`archive_state='none'`); `lifecycle` went to `inactive`, not `active` — a restore is a
retrieval, not a re-opening, by design.

**Re-archive, immediately after**, proved something the first (predecessor-session) drill
could not: `ensure_archive_job_folder` **adopted** the existing empty shells rather than growing
duplicates beside them — same ids (`7569742983653252`, `408073705972`) — and the filed PDF rode
along. Full cycle **archive → un-archive → archive** proven, every folder id preserved through
all three moves. Closed #42. The one residual — a restore meeting a live folder that has
re-grown the job's name — has still never fired (nothing re-created the job in the live tree
while it was archived); documented as `docs/runbooks/job_archive.md` Symptom 6 rather than
tracked as an open issue, and marked novel under the both-rule so it escalates if it is ever
seen for real.

### #49 — `docs(archive): the §43 runbook the job archive shipped without (#24)`

New `docs/runbooks/job_archive.md`, nine symptoms, each traced to real code. Two findings
inside it: `_log_container_failure`'s WARN told the operator "the job stays on the archive
queue and retries next cycle" — false for the outcome it fires on, since a container failure
yields a partial/failed that leaves the queue; corrected. And the panel cannot say **which**
container is stuck — `LABEL_SAFETY`/`LABEL_PROGRESS` are reused across both systems with no
slot key rendered, so "Safety" and "Progress" each appear twice with no system shown — the
runbook routes the operator to the `archive_container_failed` WARN instead, which carries
`{system}/{label} ({key})`.

The runbook's first commit was written before the restore drill ran and asserted in four
places that the un-archive direction "has never been run against live data." A second commit
in the same PR corrected all four once the drill (above) had completed — narrowing the caveat
rather than dropping it, since the live-folder-collision refusal genuinely has never fired.
Symptom 9 also dropped a "delete the empty orphan" housekeeping line per operator direction:
the absence of a Box folder-delete primitive is by design
(`tests/test_box_client_exposes_no_folder_delete_primitive` enforces it), so the correct action
is none — not documented as a gap, because it isn't one.

**PR #49 — four-part verify clean.** `state=MERGED` · `mergedAt=2026-08-10T22:42:34Z` ·
`mergeCommit=166a32b517088c3162b9370b5da9d666840b529e` · main-branch CI on the merge commit
**SUCCESS**. Independently re-verified against live GitHub, not taken on report.

### #52 — `docs(tech-debt): platform constraints are not debt — give them their own file`

`docs/tech_debt.md` had reached 290 KB against its own 256 KB cap. A full triage of all 133
entries — each classified, resolved claims adversarially re-checked — found 11 permanent
platform constraints (a Smartsheet column-format API rejection, features that exist only in a
vendor web UI, a Cloudflare D1 transient that resolves on retry, etc.) that can never close and
were overstating the backlog forever. Moved to new `docs/references/platform_constraints.md`,
grouped by platform, each moved entry leaving a repointed index bullet. Established the rule
now in `tech_debt.md`'s header: **three destinations, one rule each — open work stays, finished
work archives, a constraint we will never fix becomes reference. If an entry names no action a
person could take, it does not belong in the debt file.**

Honest accounting, stated in the PR itself: this recovers 13.8 KB and lands the file at 276 KB
— still ~14 KB **over** cap. Bucket A alone was never going to fix the size, and the triage said
so before the pass started; the remaining overage (12 stale/duplicate entries, 69 KB, including
the two largest entries in the file) was deliberately not bundled here, "the last archival pass
that trusted its own judgement deleted the only record of a live gap, which had to be recovered
as issue #42."

**PR #52 — four-part verify clean.** `state=MERGED` · `mergedAt=2026-08-10T23:22:22Z` ·
`mergeCommit=eba80ff5d4ea4efe40ae9675e89ca45f4140738b` · main-branch CI on the merge commit
**SUCCESS**. Independently re-verified against live GitHub, not taken on report.

### #56 — `docs(tech-debt): bucket E — the file lands 35 KB under cap, plus two defects the trim exposed`

Three commits. `docs/tech_debt.md` **276,334 → 226,495 bytes — 35 KB under its 256 KB cap**, 12
stale/duplicate entries archived to `tech_debt_closed.md`, including the two largest entries in
the file (a dated triage index restating everything beneath it; a dashboard error-chase whose
own text marked 8 of 14 sub-bullets RESOLVED/RETRACTED/FALSIFIED/FIXED). Run explicitly under
the rule named above: the pass caught **four live residuals the triage brief had missed
entirely**, which would otherwise have been destroyed — a `config_actuator`/`po_poll`
workstream-scope divergence filed under neither "resolved" nor "extract"; DASH-13's two
outstanding operator decisions (grep-confirmed to exist nowhere else); and an "activation
lesson" process note (flip a polling gate only *after* its matching Worker secret/route is
deployed) that was not tech debt at all and moved to `HOUSE_REFLEXES.md` §5, the canonical home
CLAUDE.md names for exactly that. Three brief claims were **refuted rather than applied** —
`fetch_latest_inbound_timestamp`'s docstring did not still cite the retired Check F; the VC-01
secret undercount was 21 actual against 18 stated, not 20; and the doc-lint baseline of "~89
warnings" turned out to be an artifact of the bug below, not a real count.

**Second commit — the doc-conventions gate had been linting nothing.** `walk_docs` tested
`path.parts` against the **absolute** path; `REPO_ROOT` is absolute, so a checkout living under
a dot-directory — exactly where `.claude/worktrees/<id>/` puts every workflow-agent worktree —
matched `.claude` as a path component on every file and silently skipped all of them, printing
"no violations" having linted zero documents. Proven on one commit: **89 warnings from a normal
checkout, zero from `.claude/worktrees/wf_b6f4a003-650-1`** — identical content, identical
script. Every agent running this gate from a workflow worktree, including PR #49 earlier the
same day, had gotten a green light that meant silence. Fixed by testing the path relative to
the repo root; regression test proven to RED-light on the old filter.

**Third commit — the roadmap still said "do not turn it on yet."** `docs/ROADMAP.md`'s Track 6
status block read "Still never exercised LIVE on either system" and listed the attended Box
drill as REMAINING, hours after the drill (above) had run. Corrected; the superseded
precondition is kept verbatim rather than deleted, since the reasoning behind it was sound and
the drill discharged it rather than invalidating it.

**PR #56 — four-part verify clean.** `state=MERGED` · `mergedAt=2026-08-11T00:26:13Z` ·
`mergeCommit=cb8ef983ac2b7df4769522ef3d13644184bba335` · main-branch CI on the merge commit
**SUCCESS**. Independently re-verified against live GitHub, not taken on report.

*(PR #55, "fix(archive): 'Try again' resumed the wrong direction, and un-archive had no resume
path," merged `2026-08-11T00:02:39Z` between #52 and #56 — that is another concurrent session's
work, closing only the first half of issue #54. It surfaces below only as one of the four
undeployed Worker PRs the deploy step discovered.)*

### #57 — `feat(watchdog): Check X + a Box liveness probe — and the two blockers that made both report healthy (#25, #26)`

**Check X (#25).** `/api/internal/fieldops/archive-pending` serves only
`requested`/`in_progress`, structurally blind to `partial`/`failed` — exactly the state where a
job sits half-relocated with nothing to resume it. New `GET /api/internal/fieldops/archive-health`
widens to all four states (same bearer guard, bound SQL, cap 200, read-only). Check X escalates
on a request aging past ~20 cycles and on any stopped archive regardless of age, and reads the
pass gate through `fieldops_sync`'s own accessor so it can never disagree with the daemon.

**Check P / Box liveness (#26).** Check P had been reporting "Box OAuth refresh token fresh
(idle 2d)" straight through a live `invalid_grant` failure — a staleness proxy standing in for
a liveness probe. It now performs a real authenticated read on the daily tier. My own premise
for the implementer was wrong and correcting it is the fix: `get_client()` is a process-wide
singleton whose `boxsdk.OAuth2` holds the refresh token **in memory**, so a bare retry re-spends
the dead token and fails identically — `_reset_client()` is the load-bearing step, not the
retry itself. Box also uses **identical wording** for a consumed (rotated) token and a
genuinely aged-out one; the typed `BoxRefreshTokenRejectedError` names both causes rather than
guessing "expired," since guessing wrong points a Tier-2 operator at a fixed high-class
re-auth escalation for what may be a self-healing race.

**The adversarial review found two blockers, each making its own new check report healthy in
exactly the scenario it exists to detect.** Check X read the Worker base URL under `field_ops`
when the row is owned by `safety_reports` (verified live: `field_ops` → NotFound, the real URL
lives under `safety_reports`) — it would have reported INFO and skipped forever, and every
Check X test stubbed the resolver wholesale so the suite structurally could not reach the bug.
And the Box liveness probe rode `list_folder`/`search`, whose HTTP fires on **iteration**, one
frame outside the `_call` translation wrapper — a rejected token escaped as a raw
`BoxOAuthException`, was never mapped to the typed error, the retry could not match it, and
Check P fell back to "marker fresh." That is the #26 bug reproduced by the #26 fix. Both now
materialize inside `_call`. This is the lazy-iteration gap `scripts/migrations/build_box_roots.py`
had documented and deliberately deferred as out of scope; the new liveness probe made it
load-bearing.

Also corrected `docs/runbooks/job_archive.md` Symptom 6, which had said "Try again" after a
failed un-archive raises an *archive* request — true until PR #55 (issue #54, another session's
fix, landed under this runbook) made it resume the failed direction instead.

**PR #57 — four-part verify clean.** `state=MERGED` · `mergedAt=2026-08-11T01:03:06Z` ·
`mergeCommit=2c9b8ef88443cc530c81699142bb845b371572f7` · main-branch CI on the merge commit
**SUCCESS**. Independently re-verified against live GitHub, not taken on report.

### The deploy

PR #57's own body flagged the new `/archive-health` route as inert until deployed ("Deploy —
required, not done here"). Running it surfaced a wider scope than "deploy my route": `wrangler
deploy` publishes whatever is on `main`, and by this point `main` had accumulated **four** PRs
of undeployed Worker code — #47 (GAYK weekly-maintenance checklist + training waiver form,
`2026-08-10_preop-inspection-forms-and-checklists.md`), #50 (daily-photo line binding,
`2026-08-10_pr4-materials-delivery-workflow-completion.md`), #55 (the "Try again" direction fix,
another concurrent session), and #57 (this session's Check X route) — plus migration
`0063_daily_photo_line_binding.sql`, pending from #50, never applied. Surfaced to the operator
rather than assumed; the operator chose migration-first-then-deploy-all-four.

Sequence: `git pull origin main` first (the live checkout was 2 commits behind — a stale-checkout
deploy is the documented cause of the 2026-06-28 universal portal lockout, HOUSE_REFLEXES §1) →
`wrangler d1 migrations apply` (a Cloudflare 7403 transient fired on the first call and
succeeded on retry — the exact D1-transient gotcha PR #52 had extracted into
`platform_constraints.md` hours earlier) → `npm run deploy` (Version `472c0402`, 12 assets) →
verified `/archive-health` moved from 404 to 401 (route now live, bearer still required, no
open door), the SPA still served 200 `text/html`, and Check X reaching the route end-to-end.

### #65 — `feat(watchdog): Check Y — run verify_cutover VC-03 daily instead of never (#27)`

`scripts/verify_cutover.py` is the only tool that compares declared load-bearing config against
the live tenant, and `grep -rn verify_cutover .github/workflows/` returns zero hits — it ran
nowhere. On 2026-08-10 the Track 6 archive sat inert for three days because two `ITS_Config`
rows did not exist. Detection was never the gap: `shared/required_config.py` WARNed
`config_row_missing` for that gate **3,442 times**; a WARN never triple-fires, so nothing
escalated. Check Y is the escalation observation always lacked — read-only, DAILY tier, capped
re-notify ladder, MAINTENANCE-aware, with severity partitioned as the operator specified: row
**missing** → CRITICAL; row present but **blank** → CRITICAL (an equally invisible off-switch);
`requirement=='true'` but not true → WARN (a paused gate is an operator choice, not a defect);
sandbox residue → WARN.

**It bites.** The review replayed the actual outage — deleting exactly those two rows from a
copy of the live 118-row config — and Check Y returned CRITICAL on the first sweep, naming
both. Live baseline is green: 0 missing / 0 blank across 53 enrolled rows, only the 3 known
sandbox `worker_base_url` rows at WARN. A companion gates-only parity test defines "is this a
gate" mechanically (`ConfigKey.kind == "bool"`) rather than by judgement: 100 declared keys, 27
outside `CONFIG_ROWS`, 24 of them correctly excluded as non-bool tunables, leaving 3 —
`po_materials.po_send.polling_enabled` enrolled (its structural twins `subcontract_send` /
`rfq_send` already were; the absence was an oversight), and the two `safety_reports.intake.*`
gates exempted after verifying (not assuming) they belong only to the retired email pipeline
(`intake_poll.py`, deleted 2026-07-03).

**Two blocking findings from the adversarial review.** The fail-soft floor guarded the index
keys (Setting, Workstream) but not the **Value** column — dropping Value would resolve ~118
healthy *pairs* while every value read `None`, producing a CRITICAL telling the operator to
seed 53 rows they can see are already populated; now floors on non-blank values too, proven RED
on the old guard. And the VC-02 exclusion comment claimed VC-02 "asserts a STALE plist set" —
false at HEAD: VC-02 passes today because `DARK_UNLOADED_LABELS` was emptied on 2026-08-10 (by
PR #33, the **prior** session) once every send lane was activated. Corrected, and VC-02 is now
named as the strongest follow-on candidate rather than silently re-excluded.

**PR #65 — four-part verify clean.** `state=MERGED` · `mergedAt=2026-08-11T19:11:08Z` ·
`mergeCommit=64c82951f444935366439b7a41ce47bd10e65ed0` · main-branch CI on the merge commit
**SUCCESS**. Independently re-verified against live GitHub, not taken on report.

## Verification (final state, PR #65)

```
- pytest: 4856 passed / 2 skipped / 58 deselected
- mypy: 0 errors / 482 source files
- ruff: clean
- main-branch CI on merge commit: SUCCESS
```

`tests/test_publish_daemon.py` is excluded from every pytest count quoted above and in every PR
body this session — 29 pre-existing local failures that pass in CI, unrelated to any change
landed here. `check_doctrine_drift --strict` reported no blocking drift at each PR.

## Corrections made mid-session

Recorded plainly, matching the discipline this session applied to the prior session's stale
claims:

1. **My brief for #27 (PR #65) claimed a mass-drift false positive would fire ~53 CRITICALs.**
   `_run_check` emits one record per `CheckResult`, so a mass-drift condition is one CRITICAL
   with a capped summary, not a 53-row burst. The guard itself was right; the justification was
   inflated by ~53×, and it appeared in two places before being corrected.
2. **My brief for #26 (PR #57) asserted a bare retry would work.** It does not —
   `get_client()`'s process-wide singleton holds the dead refresh token in memory, so a bare
   retry re-spends it. `_reset_client()` is the actual fix.
3. **The first version of the #29 test (PR #43) did not bite.** It asserted only on the
   `_cycle_error_summary` pure helper and stayed green when the fix was reverted. Rewritten to
   drive the real `_sync_inside_lock` tally, which now fails `OK` vs `DEGRADED` on revert.

## Decisions

1. **Declined to write the D1 row by hand for the restore drill.** The browser route is the
   path under test — it stamps state *and* writes the audit row — so a hand-written row would
   have drilled a different, less representative path. (Op Stds §30 live-smoke discipline: prove
   the real path, not a synthetic stand-in.)
2. **Surfaced the deploy's true scope rather than assuming "deploy my route."** `wrangler
   deploy` publishes whatever is on `main`; deploying without checking would have silently
   shipped three other sessions' unreviewed-by-me Worker changes. The operator chose the
   sequencing (migration first, then all four).
3. **The tech-debt trim (PR #56) ran under an explicit "never destroy the only record of live
   work" rule**, written directly from this session's own earlier mistake — an archival pass
   nearly dropped the still-open "restore never drilled" gap, requiring its recovery as issue
   #42 (see above). The rule caught four further live residuals the triage brief had missed.
4. **VC-02 was deliberately not bundled into #65** despite reading clean today. It is the
   strongest follow-on (an unloaded plist is exactly as invisible as a missing config row, and
   this outage needed both to go unnoticed), but enrolling it was a scope decision for a future
   session, not a fix owed by this one — recorded as its own tech-debt entry rather than
   silently deferred.

## Open items / next session

- **Issue #54 remains OPEN.** PR #55 (another concurrent session) closed only its first finding
  ("Try again" resumes the wrong direction); the remaining findings need an owner to close out
  the issue.
- **`logs/migrations/po_vendors_backup_20260810.json` is untracked** and keeps `verify_cutover`
  VC-07 (working-tree-clean) permanently red — confirmed still present and untracked at session
  close. This is a data-sensitivity call (commit it, matching the `logs/migrations/*` convention,
  or gitignore-and-delete it, since it carries live `ITS_Vendors` contact data), not a mechanical
  one — needs Seth.
- **Four new `docs/tech_debt.md` entries were drafted at session close and are UNCOMMITTED in the
  working tree as of this log** (not landed — flagging so the next session doesn't miss them):
  `regen_doc_indexes.py::find_readmes` has the identical absolute-path hidden-dir bug PR #56 fixed
  in `lint_doc_conventions.py::walk_docs`, unfixed on the sibling script; the `build_box_roots.py`
  lazy-iteration comment is now stale since PR #57 closed both functions it named; VC-02 as a
  Check-Y-shaped daily-sweep follow-on (Decision 4); and the `po_vendors_backup` file above.
- **The live-folder collision refusal** (restore meeting a live folder that re-grew the job's
  name) has still never fired in any drill. Documented as `docs/runbooks/job_archive.md`
  Symptom 6, marked novel under the both-rule, not tracked as an open issue.

## What was NOT touched

- Issue #54's remaining findings (B/C) — out of scope; another session's issue.
- VC-02 enrollment into a watchdog check — deliberately deferred (Decision 4).
- The `regen_doc_indexes.py` sibling bug and the `build_box_roots.py` stale comment — filed as
  tech-debt, not fixed.
- The `po_vendors_backup` untracked file — flagged, not committed or deleted; the call belongs
  to Seth.

## Cross-references

- `docs/session_logs/2026-08-10_archive-button-diagnosis-and-live-drill.md` — the immediate
  predecessor session (PR #33), whose diagnosis filed #24–#27, #29, #30, and whose first archive
  drill is what this session's restore drill completed into a full cycle.
- `docs/session_logs/2026-08-10_outage-diagnosis-alerting-gap-and-gate-activation.md` and
  `docs/session_logs/2026-08-10_pr4-materials-delivery-workflow-completion.md` — unrelated
  concurrent same-day sessions; PR #50 from the latter and PR #47 from
  `2026-08-10_preop-inspection-forms-and-checklists.md` were bundled into this session's deploy
  step as undeployed Worker code, not authored here.
- `docs/runbooks/job_archive.md` — the §43 runbook (PR #49), corrected by PR #57's doc commit.
- `docs/references/platform_constraints.md` — new (PR #52); the D1-7403-transient entry is the
  exact gotcha the deploy step (above) hit and recovered from on retry.
- `docs/tech_debt.md` / `docs/tech_debt_closed.md` — trimmed to 226,495 bytes (PR #56), 35 KB
  under the 256 KB cap; four further entries drafted uncommitted at this session's close (see
  Open items).
- `docs/HOUSE_REFLEXES.md` §1 (trust live code over the claim — the doc-lint bug, the roadmap
  fix, the VC-02 comment correction), §2 (prove-the-control-bites — Check X and Check P's fixes
  proven to RED-light before shipping), §5 (the "flip a polling gate only after its Worker
  route deploys" lesson, newly added by PR #56; "seed the gate row" precedent from the prior
  session).
- `docs/operations/pr_merge_discipline.md` — the four-part verify applied to all six PRs above.
- Issues #24, #25, #26, #27, #29, #30, #42 — all CLOSED this session (via PR #43: #29, #30; PR
  #49: #24; PR #57: #25, #26; PR #65: #27; live drill: #42). Issue #54 remains OPEN.
