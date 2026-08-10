---
type: session_log
date: 2026-08-07
status: closed
related_prs: [726, 728, 18, 20, 21]
workstream: field_ops
tags: [session_log, field_ops, archive_on_closure, section51, box, smartsheet, two_repo_split, deploy, adversarial_review, section44, house_reflexes]
---

# Session log — 2026-08-07 · Track 6 completion: the Box leg, the un-archive leg, the archive button, the drain, and the deploy

Code and deploy landed 2026-08-07; this log's four-part verify and final gate figures were
independently re-run against live `main` on 2026-08-10 to close it out — see "Verification" below
for what was re-confirmed rather than taken on report.

## Purpose

Continue `docs/ROADMAP.md` Track 6 (the end-to-end job-archive workflow) from the 2026-08-03
handoff, whose explicit open ask was the Box leg. Five PRs later, across **two repositories**, the
path is complete end-to-end — button → D1 → queue → relocation → commit point → UI — and the
portal carrying it is deployed. The gate stays deliberately dark; see "State at close."

## Repo topology moved under this session

Mid-session, `~/its` `origin` was repointed to **its-sys-admin/evergreen-its (production)**, with
`SolutionSmith-debug/its` demoted to a secondary remote (`dev`). The two repositories **diverged
twice in one day** despite the #712 merge-commit reconciliation design (2026-08-02/03) meant to
keep future syncs fast-forward-only: `git log` on production shows a second reconciliation merge
this same day (`ed03877` "Merge pull request #15 from its-sys-admin/reconcile/import-development",
folding the manifest-import lane + 27 commits back in). The split is real and recurring, not a
one-time event the earlier design fully retired.

**Production `main` now has branch protection** (`test`+`portal`+`secrets` required, `strict: true`,
`enforce_admins: true`) — verified live via `gh api repos/its-sys-admin/evergreen-its/branches/main/protection`
with the `its-sys-admin` account. This **contradicts** `docs/tech_debt.md` PM-5, which recorded the
production repo's branch-protection state as "unverified" (the 404-without-admin gotcha,
`reference_github-protection-404-without-admin.md`) and flagged `publish_daemon._wait_for_ci`'s
`mergeStateStatus == "CLEAN"` early-return as a live fail-open risk if the daemon ever loaded there
before protection existed. Protection now demonstrably exists; PM-5's risk framing needs updating,
not just its guess — a fix for a later session, not made here.

**Push access resolved** the same way PM-5 anticipated it eventually would: `gh auth switch --user
its-sys-admin` (`admin: true, push: true` confirmed via `gh api repos/its-sys-admin/evergreen-its
--jq .permissions`), rather than a durable cross-account grant on the `SolutionSmith-debug` token.

## PRs landed

### Development repo (`SolutionSmith-debug/its`)

#### #726 — `feat(field-ops): job_archive's Box leg — the two containers that carry the documents`

The Smartsheet half (landed 2026-08-03) moved four folders and honestly reported the Box two as
`box leg pending`. This PR makes them move: `archive_job`'s slot loop had a
`slot.system != "smartsheet"` branch appending a not-moved stub, replaced with a call to
`archive_box_container`. Everything downstream — `state_from_results`, the per-container fence, the
D1 commit point, the SPA's per-container list — was already built to handle Box results. Source
lookup rides the new find-only `box_client.find_child_folder` (the former private
`_find_child_folder`, made public); `get_or_create_folder` would manufacture the very folder whose
absence means there is nothing to move. An unset Box root (`_read_box_root`) **raises into the
per-container fence** rather than returning `None` — a silent `None` would report both containers
relocated while the documents sat where they were. New config row
`field_ops.box.archive_root_folder_id` fanned out same-PR to `build_box_roots.py` (a third root,
"ITS Archive," a sibling not a child of either existing tree), `standup.py`'s seed tuple, VC-03
(`non_empty`, never forced), the §50 dashboard registry, the config dictionary + manifest sha, and —
the trap that would have failed silently — `production_repoint.ALLOWED_SETTING_SUFFIXES`, which
matches Setting names by literal suffix and skips non-matching rows without error; a
production cutover would have silently kept a sandbox Box folder id without the enrolment. Also
found and fixed: the pre-existing `test_job_archive.py` fixture left `get_setting` unpatched, so
every `archive_job()` call had been making a real (slow, network-dependent) Smartsheet request that
failed into the fence rather than being mocked; and `docs/references/its_config_dictionary.md` was
already stale on `main` before this PR touched it (`system.operator_email` still held the old
sandbox address against the 2026-07-23 decision value in `shared/defaults.py`) — corrected in the
regen, not caused by this PR.

**PR #726 — four-part verify clean.** `state=MERGED` · `mergedAt=2026-08-07T17:29:18Z` ·
`mergeCommit=799e2d624dd7ecd448308cc825794d9c335363bb` · main-branch CI on the merge commit
**SUCCESS** (`ci` SUCCESS, `Push on main` SUCCESS). Independently re-verified against live GitHub,
not taken on report.

#### #728 — `feat(field-ops): the un-archive leg — restore, and the order that inverts`

The Worker's `/archive-pending` and commit-point routes have always served and validated both
directions; only the Python side knew how to archive. Running `archive_job` against an un-archive
row previously did not error — it searched the live tree, found nothing (the containers are in the
archive), reported six clean "nothing to move" successes, and posted `complete`, telling the
operator their job was restored while every folder stayed archived. `run_archive_pass` is now the
single entry point, dispatching on the queue row's `archive_direction`; an unrecognised direction
**refuses** rather than defaulting to archive. **The two-call Smartsheet order inverts per
direction** — archive is move→rename, restore is rename→move — because every live path
(`week_sheet._ensure_job_folder`, `hours_log._ensure_job_folder`, `job_sheet.ensure_job_sheet`)
find-or-**creates** by job name: a folder sitting in the live tree under the wrong name is invisible
to those finders, so the next filing grows a duplicate beside it. Both orderings are chosen so the
crash window can never leave a mis-named folder in the *live* tree; each residual window is confined
to the archive side, where nothing find-or-creates, and is repaired by re-issuing the second call.
`resolve_archived_container` searches by label **and** folder key, precisely because the restore's
crash window can leave a container renamed-but-not-yet-moved, which a label-only search would
misread as "nothing to move." A restore onto a live folder that has re-grown the job's name
**refuses loudly** rather than merging — neither system has a merge primitive, so fusing two job
trees would be unrecoverable. Box needs neither order or refusal machinery: its `PUT` carries parent
and name together.

**PR #728 — four-part verify clean.** `state=MERGED` · `mergedAt=2026-08-07T17:48:37Z` ·
`mergeCommit=cdf83e3129eca9c58a37c56e9adc09442d817db6` · main-branch CI on the merge commit
**SUCCESS** (`ci` SUCCESS, `Push on main` SUCCESS). Independently re-verified against live GitHub,
not taken on report.

### Production repo (`its-sys-admin/evergreen-its`)

#### #18 — `feat(portal): the archive button — intent, typed confirmation, and honest progress`

The archive has been API-reachable since #720; this is the surface that makes it pressable.
Pressing Archive writes `archive_state='requested'` — it does not archive. The panel **polls
`job.archive.state`** rather than treating a 200 as done, because claiming completion the instant a
flag flips is exactly the lie the old lifecycle dropdown told. The confirmation dialog is
**typed, not `window.confirm`** — names all six containers and their system, states plainly what
does *not* move (flat `*_Log` ledgers, WSR/WPR review rows, the `ITS_Active_Jobs` row, `ITS DATA`,
`ITS Photos`, every portal record), and arms only on the job's exact project name, trim-only and
**case-sensitive** — matching the server's `archiveTransition` rule exactly, so the button's enabled
state and the server's verdict can never disagree. It is an affordance, not the control: the Worker
re-checks `confirm` against the row's own `project_name` server-side, so skipping the dialog is still
refused. A partial ("4 of 6 moved") names each stuck container and its reason and tells the operator
**not** to drag folders by hand — a manual fix would desynchronise recorded folder ids from where the
folders actually are. Ported from pre-existing uncommitted work onto current `main` via a 3-way
apply (zero conflicts), and given 27 tests it never had — including a stale-text-cannot-pre-arm-a-
reopened-modal case (closing mid-type on one job and reopening on another must not present an armed
button carrying the previous phrase) and a busy-disarms case against double-submit. No daemon drains
the queue yet; pressing this writes `requested` and nothing picks it up until #20.

**PR #18 — four-part verify clean.** `state=MERGED` · `mergedAt=2026-08-07T22:16:54Z` ·
`mergeCommit=a7a14449eb3e53d200fb6a17f2c09dd965fc4d1f` · main-branch CI **SUCCESS**. Independently
re-verified against live GitHub, not taken on report.

#### #20 — `feat(field-ops): wire the archive pass into fieldops_sync — the queue that actually retries`

The relocation module (`job_archive.py`, #722) has had no caller since it landed; pressing Archive
wrote `requested` and nothing picked it up. This is the drain — independent of #18 (Python vs.
TypeScript, disjoint files). `run_archive_pass` reads its **own** queue
(`/api/internal/fieldops/archive-pending`), not the job-dirty list — the pre-Track-6 archive rode the
dirty list, which `jobs-mark-mirrored` clears the instant an unrelated mirror succeeds, which is
precisely why the old WARN said "no auto-retry" about itself. `/archive-pending` keeps serving a job
until its archive reaches a terminal state, so a failure is genuinely resumable. Pre-flight
(`verify_archive_capability`, five workspace ADMIN reads) runs **once per cycle, not per job** —
their answer can't differ within a cycle — and a failed pre-flight **skips the whole batch**, since
every job would 403 on the same shortfall and attempting them individually burns N attempts against
an uncappable-by-the-queue condition. The retry cap (`MAX_ARCHIVE_ATTEMPTS`) is enforced **here**
because the Worker serves any `('requested','in_progress')` row regardless of attempt count — without
this skip, a permanent condition (deleted destination, an unfixed share) would re-fire the six-
container sequence every cycle forever. Direction is forwarded **verbatim, never assumed** — the
Worker's own UPDATE is forward-only on `(state, direction)`, so a wrong direction silently no-ops
server-side, and a hardcoded `archive` would be exactly how an un-archive gets marked complete
without happening. A failed commit-point post is **WARN, not CRITICAL** — the folders already moved,
only the report failed, and the idempotent re-run self-heals; escalating it would train the operator
to ignore the page. Gate `field_ops.fieldops_sync.archive_enabled` ships OFF, **with its row seeded
false in this same change** (HOUSE_REFLEXES §5 — a missing row and a `false` row are the same to a
`default=False` reader, so the switch must exist to be found). One finding worth recording: the
shared `fieldops_sync` test fixture needed a new `archive_enabled` seam — `_archive_enabled` resolves
a live ITS_Config row, which conftest's hermeticity guard rejects, and this surfaced only in the
**full** suite (60 failures), never in the file's own tests run alone.

**PR #20 — four-part verify clean.** `state=MERGED` · `mergedAt=2026-08-07T22:09:17Z` ·
`mergeCommit=db350676bbd285f2f41c018da228460c2488791d` · main-branch CI **SUCCESS**. Independently
re-verified against live GitHub, not taken on report.

#### #21 — `docs(roadmap,claude-md): Track 6 status — the button and the drain landed`

Docs-only, following #18 and #20. The ROADMAP still listed both as **REMAINING** — the stale-
current-state class this repo's own forensics rank first (class #3, recurred 16×), and worse on a
roadmap than elsewhere: a reader plans to build what already exists. Corrects that, and corrects two
claims this same author had written **half-true earlier the same day**: documenting the Box leg
(#726), the ROADMAP/CLAUDE.md text said "Smartsheet is move-THEN-rename" and "the resume probe keys
off the RECORDED folder id, never a name" — true only of the archive direction. The un-archive leg
(#728) then inverted the order and added a name-based search on the restore side, making each
sentence true of only one direction; #21 names the direction each sentence describes. On the gate,
the status text was written to point at ITS_Config for the live value rather than assert one
(HOUSE_REFLEXES §1/§5 — a "ships dark" claim is redundant the day it's written and wrong the day
someone flips it, per the 2026-07-21 procurement-gate incident), while stating plainly: **"Do not
turn it on yet"** — neither direction has been exercised live on the Box side, the Smartsheet half
was drilled against the sandbox 2026-08-06, and every Box test is mocked.

**PR #21 — four-part verify clean.** `state=MERGED` · `mergedAt=2026-08-07T22:45:31Z` ·
`mergeCommit=d3f56698ccfc4c3628c00f6d0e68e9ff3b357466` · main-branch CI **SUCCESS**. Independently
re-verified against live GitHub, not taken on report.

## Prove-the-control-bites — 20 injections across the four feature PRs

Every guard in #726/#728/#18/#20 was verified by injecting the synthetic violation, confirming a RED
result, then reverting (HOUSE_REFLEXES §2). Five per PR:

| PR | Injection | Result |
|---|---|---|
| #726 | drop `.archive_root_folder_id` from the repoint suffix allowlist | FAILED |
| #726 | `_read_box_root` returns `""` instead of raising | FAILED |
| #726 | resolve the Box source with `get_or_create_folder` | FAILED |
| #726 | attach a `description` to a SOURCE root | FAILED |
| #726 | add a fourth Box slot (the "six-not-eleven" tempting fix) | FAILED |
| #728 | move-then-rename on the restore | FAILED |
| #728 | drop the live-collision refusal | FAILED |
| #728 | search the archive by label only | FAILED |
| #728 | `ensure_` instead of `find_` on the restore path | FAILED |
| #728 | default an unrecognised direction to `archive` | FAILED |
| #18 | arming rule made case-insensitive | FAILED |
| #18 | clear-on-open removed (stale text pre-arms a reopened modal) | FAILED |
| #18 | Enter submits regardless of match | FAILED |
| #18 | panel renders without `cap.job.archive` | FAILED |
| #18 | stuck containers no longer named in the partial banner | FAILED |
| #20 | drop the retry cap | FAILED |
| #20 | hardcode `direction='archive'` | FAILED |
| #20 | pre-flight per job instead of per cycle | FAILED |
| #20 | post an empty updates array | FAILED |
| #20 | escalate a failed commit-point post to CRITICAL | FAILED |

## The deploy

The Cloudflare Worker was deployed carrying #18/#20's changes: version `51095e57-da4a-4bad-b052-
7125cdd872a6`, created `2026-08-07T23:16:34Z` per `npx wrangler deployments list` (re-confirmed
2026-08-10, still the current deployment — no redeploy since). 13 changed assets, no pending D1
migrations for this Track (migration `0058` landed 2026-08-03 with #719, already applied). Verified
by fetching the live bundle from `safety.evergreenmirror.com` and confirming the archive-panel code
is present, and that the live `index.html` references the just-built asset hash rather than a cached
prior one (HOUSE_REFLEXES §7 "deploy nothing-changed = browser cache" — checked at the deploy-output
level, not by eyeballing the browser).

## A finding surfaced re-verifying this log: the ROADMAP names an ADR slot that's already taken

`docs/ROADMAP.md` Track 6 still says "Design record lands as `docs/adr/0005-job-archive-workflow.md`"
— but `docs/adr/0005-materials-manifest-import.md` landed the same day (PR #729, the PR3b
foundation slice, logged separately in
`2026-08-07_manifest-import-transport-pr3b.md`) and has already claimed that number. `ls
~/its/docs/adr/` confirms only the manifest-import file exists at `0005`; no
`job-archive-workflow` file exists under any number. This is a real, if minor, collision — the
archive's ADR must land as `0006` (or later), not `0005` as currently written, whenever it's
drafted. Not fixed here (out of scope for a session log — this repo's convention is doctrine/design
docs are not edited by `session-log-writer`); flagged for the next session that touches either the
ROADMAP or drafts the archive ADR.

## State at close — precise, not overstated

The archive is **complete end-to-end** (button → D1 → queue → relocation → commit point → UI) but
**INERT**:

- The gate row `field_ops.fieldops_sync.archive_enabled` **does not exist** in live ITS_Config —
  reads `SmartsheetNotFoundError`. The seeder (`scripts/migrations/seed_daemon_gate_config.py`)
  shipped in #20 but was never run against the live tenant.
- **No launchd jobs are loaded on this host** — `launchctl list | grep its.` returns zero matches,
  confirmed this session. `fieldops_sync` cannot drain the queue even if the gate were flipped, until
  the fleet is loaded.
- **The Box side has never been drilled live in either direction.** The Smartsheet half was drilled
  against the sandbox 2026-08-06; every Box test in this repo is mocked. `docs/tech_debt.md`'s
  2026-08-03 "Box identity on the dev host is UNCONFIRMED" entry is still open and still governs: do
  not call `box_client` to find out casually, since a Box refresh-token exchange rotates the grant
  and could break production if the identities are shared.

**Do not flip the gate before the attended sandbox drill.** This is the ROADMAP's own instruction
(#21), restated here because it is the operative fact at session close, not a formality.

## Verification

Re-run against live `main` (post-#21, this working tree) on 2026-08-10, not merely quoted from the
PR bodies:

- pytest: 4728 passed / 2 skipped / 58 deselected (53.59s)
- mypy: `Success: no issues found in 480 source files` (excludes the untracked, gitignored
  `scratchpad/` directory, which is not part of any CI checkout and is not this session's concern)
- ruff: `All checks passed!`
- portal typecheck: clean (3 tsconfigs — app, worker, test)
- worker vitest: 1236 passed / 71 files
- SPA vitest: 770 passed / 58 files
- main-branch CI on all five merge commits: SUCCESS (independently re-verified per PR above, not
  taken on report)

## What was NOT done / open items

- **The attended sandbox drill (Box half).** The load-bearing precondition before the gate flips —
  a wrong Box identity is undetectable in-band (Box has no ownership discriminator to probe), and
  the first live archive would relocate a customer's closed-out documents if the identity is wrong.
- **`production_shares_manifest.json` needs `WORKSPACE_ARCHIVE`** with a byte-exact name (Safety
  Portal uses two EN DASHes where the other workspace names use one EM dash) — and an operator
  decision on *who* is shared on it, since a cross-workspace move changes who can READ the relocated
  contents (§46).
- **Docs**: the archive ADR (now needing renumbering to `0006`+, see above), the
  `project_closure.md` rewrite, a troubleshooting-tree node, and a system-map node **with its
  brief** — `tests/test_system_map.py` enforces the brief requirement on any new node.
- **The §51 doctrine rider** — Seth-owned, planning-layer, ratifying the archive scope expansion.
  Nothing activates before it exists.
- **Seeding the `archive_enabled` gate row** and **loading the launchd fleet** on this host — both
  operator actions, not attempted this session.
- **The PM-5 tech-debt entry's risk framing** — now stale given branch protection is confirmed live
  on production; not corrected in this session (out of scope for `session-log-writer`; flagged
  above for whichever session next touches `docs/tech_debt.md`).

## Cross-references

- `docs/ROADMAP.md` Track 6 — the plan this session completes structurally; still gated on the
  drill, the shares manifest, docs, and the doctrine rider before go-live.
- `docs/session_logs/2026-08-03_track6-job-archive-workflow-pr0-through-pr5.md` — the prior
  installment (PR-0 through PR-5, #715–#722), whose explicit handoff item — the Box leg — this
  session opens with.
- `docs/session_logs/2026-08-02_repo-reconcile-and-local-dev-environment.md` — the first
  reconciliation of the two repositories; this session's topology note records that it did not hold.
- `docs/operations/pr_merge_discipline.md` — the four-part verify all five PRs satisfy, across both
  repositories.
- `docs/tech_debt.md` — "Sandbox Smartsheet PAT" and "Box identity on the dev host is UNCONFIRMED"
  (both `OPEN 2026-08-03`), still the governing blockers on the drill; PM-5, whose branch-protection
  framing this session's finding contradicts.
- `docs/HOUSE_REFLEXES.md` §1 (trust live code — the ADR-number collision found by `ls`, not
  assumed from the ROADMAP's own text; the PM-5 contradiction found by a live `gh api` call), §2
  (prove the control bites — 20 inject-confirm-revert cycles across four PRs), §5 (a dark-shipped
  gate needs a seeded row; a status doc should state semantics, not assert a live value — #21's own
  self-correction of #726/#728's half-true claims from earlier the same day).
- `reference_github-protection-404-without-admin.md` (memory) — the gotcha PM-5 was written under;
  this session's admin-token check is the first live resolution of it for the production repo.
