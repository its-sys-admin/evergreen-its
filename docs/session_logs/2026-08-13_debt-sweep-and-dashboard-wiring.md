---
type: session_log
date: 2026-08-13
status: closed
related_prs: [122]
workstream: null
tags: [session_log, safety_portal, operator_dashboard, infrastructure, css, tech_debt, repo_identity, wiring]
---

# Session log — 2026-08-13 · A tech-debt sweep, dashboard wiring for the new lanes, and finishing the repo-identity migration

## Summary

One PR, three workstreams, each answering an item the last two days of session logs had
explicitly parked. **The select kit** closes the Safari/WebKit tap-target defect the
2026-08-12-evening job-detail session filed and deliberately deferred — a kit-wide, two-rule
layered CSS treatment (a low-specificity fallback + a high-specificity chevron rule that survives
the family rules' `background` shorthand) that fixes every native `<select>` portal-wide, measured
in headless WebKit at four widths. **Dashboard wiring** closes the two real gaps a four-lens
Workflow audit found in the operator dashboard's `/system` schematic and Class-E config console —
a missing pull edge for the one Worker-draining Mac process that had none, and one ladder sibling
missing its console row, the same one-sibling-omitted class PR #110 closed for the send lanes two
days ago. **Repo identity** finishes the classification the 2026-08-10 residual entry had
deliberately left ambiguous, settled this session by evidence (CodeQL is triage-dead against both
possible repo slugs from this host) rather than assumption. Four tech-debt entries closed. Two
concurrency incidents worth narrating — a real ledger conflict with another same-day session,
resolved keep-both after confirming disjoint entries, and a non-fast-forward divergence from a
remote-side merge commit. The `pr-landed-verifier` subagent refused to verify a second time in two
days, for the same underlying reason as its 2026-08-12-evening refusal: its cached brief is stale
against a fix that lands in the very PR it was asked to check. The orchestrating session ran the
four landing legs directly and reports that refusal honestly rather than folding it into a clean
claim.

## PRs landed

| PR | What | Merge SHA | Verify |
|---|---|---|---|
| #122 | Retire the design-pass tech debt, wire the dashboard for the new lanes, finish the repo-identity migration | `ebca3e48488672dce6df5571b86c639b671b6025` | see "The verifier declined again" below — four legs run directly by the orchestrating session, all positive |

Squash merge, merged **2026-08-13T20:22:23Z**. 18 files in the main commit, plus a PR-number-
pinning follow-up commit and a keep-both merge resolving a ledger conflict with a concurrent
session (see "Concurrency incidents" below).

### Workstream 1 — the select kit

The Safari/WebKit min-height defect the 2026-08-12-evening session (#119) diagnosed and
deliberately deferred to "its own four-width pass rather than a per-page patch": Safari/WebKit
ignores `min-height` on `appearance: auto` menulist selects, so the kit's 44px tap-target floor
silently failed on **every** select portal-wide in the operator's own browser, while holding fine
in Chrome.

Fix, exactly as the deferred entry specified: two layered rules appended to `global.css`.

- A bare-`select` fallback at specificity (0,0,1) — 44px floor + kit box-model — that loses
  per-property to the family rules (`.field__input` selects keep their 48px `--tap`, `.dash-row`
  selects keep 44px), so heights stay harmonized *within* each row family rather than being
  flattened to one value everywhere.
- A `select:not([multiple]):not([size]):not(.btn)` rule at (0,3,1) carrying `appearance: none` + a
  BRG data-URI chevron. The elevated specificity is load-bearing, not decorative: the family rules
  set the `background` shorthand, which expands to `background-image: none`, and a lower-
  specificity chevron rule would be erased by it on every select that also matches a family rule.

Zero TSX changes, zero test churn — the audit behind this fix first verified no select test
asserts styling, no `multiple`/`size` selects exist anywhere in the kit, and no dark-ground
selects exist that the new chevron color would fight. The one intentional exception is the
AccountsPage role-changer, a `<select>` classed `btn btn--secondary`; the `:not(.btn)` clause
excludes it and it keeps its native arrow.

`ChipX` got the same pass's other judgment call: the `::before` hit overlay grew from
`inset:-12px` (~41×38px effective against a 17×14px glyph) to `inset:-15px -14px` (≥44×44px in
both dimensions), zero layout shift; the CSS comment recording the old numbers was rewritten.

Also landed in this workstream: the `WeeklyReportPage` rail-tap regression test the same
2026-08-12-evening session had flagged as its one residual — the job-detail rail's twin,
dirtying the draft, asserting `fireEvent.click` returns `false` (`preventDefault` fired),
`scrollIntoView` called, no refetch, and the draft still on screen afterward.

**Measured in headless WebKit at 390/768/1024/1440px:** all 15 job-detail selects ≥44px (were
19–25px before the fix), chevron present, `appearance: none` confirmed, zero horizontal overflow
at any width; the AccountsPage `.field__input` select held its 48px; `ChipX` `elementFromPoint`
hits registered at ±21px on all four axes.

### Workstream 2 — dashboard wiring for the new lanes

A four-lens parallel Workflow audit against the operator dashboard's `/system` schematic and
Class-E config console found the dashboard almost fully wired following #110/#112 — two real gaps,
both closed:

1. **`system_map.py`** gained the `worker → progress_weekly_generate` pull edge ("pull production-
   report aggregate", port bearer) — the one Worker-draining Mac process (the weekly-production-
   report aggregation, `safety_portal/worker/fieldops_report.ts`) that had no pull edge represented
   on the `/system` schematic at all.
2. **`act/registry.py`** gained the `po_materials.estimate_extract.tier1_xlsx_enabled` Class-E
   display row beside its three ladder siblings (Tier 0, Tier 1 PDF, Tier 2/OCR) — the one
   extraction-ladder key with no console row, the same one-sibling-omitted class PR #110 closed for
   the send-lane config rows two days earlier.

Also updated in the same pass: the SPA node's operator brief now names the office-facing surfaces
it actually hosts (estimate uploads, manifest/schedule imports, weekly-report inputs), and the
Worker node's blurb/brief now names the production-report aggregation it performs.

**Confirmed already wired, no change needed** — the audit checked and did not touch: schedule_poll
and manifest_poll nodes/briefs/gates/runbooks, `job_archive` joined into `fieldops_sync` with
Check X represented, watchdog Checks Y and W, the letter-agnostic watchdog-sweep panel, the
Box-roots validity panel (five roots, parity-tested against `standup.BOX_ROOT_CONFIG_ROWS`),
Class-A config gates for every new lane, Class-B daemon verbs and the dynamic start/stop
allowlist, the troubleshoot-tree flows for manifest/schedule/archive/estimate/rfq/subcontract/
weekly-report, `TRACKED_JOBS`, and the shared `HeartbeatReporter` self-provisioning path.

The `/system` node-brief parity teeth **bit during the work, not just after it**: the worker
brief's first draft came in at 134 words against the enforced 40–125-word band and had to be
trimmed to fit — the tooth doing its job, not a loosened gate. Parity suites: 81 passed.

Post-merge: the dashboard daemon was restarted via `launchctl kickstart` (the DASH-12 sanctioned
self-restart mechanism) and `/system` was verified serving the new worker pull edge, `/config`
verified rendering the new xlsx Class-E row.

### Workstream 3 — repo identity, finished

The 2026-08-10 residual entry had deliberately left one question open: of the ~15 remaining
surfaces naming `SolutionSmith-debug/its`, which are live `dev`-remote references that are
correctly still pointed there, and which are stale assertions that should follow the host to
`its-sys-admin/evergreen-its`? This session settled it with evidence rather than assumption:
CodeQL has **no analyses at all** on `its-sys-admin/evergreen-its` ("no analysis found" — default
setup was never re-enabled after the host migration), and the `its-sys-admin` token **403s** on
`SolutionSmith-debug/its`. Neither slug supports "correctly still targets dev" for any live
surface a triager or agent could act against from this host — so classification (a), "leave as a
live reference," had no candidates.

Repointed: all six `.claude/agents/*.md` briefs (`pr-landed-verifier` additionally gained the
blueprint-fork `--repo` caveat and dropped its dead CodeQL-workflow expectation; `codeql-fp-
triager` marked DORMANT until CodeQL default setup is re-enabled on the new repo — an operator
GitHub-settings action, not something this session could do), `CLAUDE.md` (the agent line, and the
observability-stack claim, which now states CodeQL is not yet re-enabled), the `README.md` badge
and blueprint link, `docs/operations/cutover_checklist.md` CL-23's verify command,
`tests/test_hook_block_codeql_dismiss.py` fixtures, and the `ci.yml` gitleaks comment (reworded
owner-neutral).

Left untouched, deliberately, as historical record: session logs, memory-archive, closed
tech-debt entries, the §42 fix-narration comments in `scripts/watchdog.py` / `tests/test_
watchdog.py`, and `context-pack/repo-overview.md` (a frozen 2026-06-12 snapshot — retire-or-
refresh is its own call, not folded into this rename pass).

## Ledger — four entries closed

`docs/tech_debt_closed.md` gained four `RESOLVED 2026-08-13` entries this session:

1. **Safari/WebKit `min-height` selects** — RESOLVED, PR #122 (workstream 1 above).
2. **`WeeklyReportPage` rail-tap regression-test gap** — RESOLVED, PR #122 (workstream 1 above).
3. **Playwright-MCP screenshots broken in this dev environment** — RESOLVED, but *not* by this
   session's own work: closed as delivered by the 2026-08-12-evening session's own update, which
   isolated the fault to the MCP wrapper layer specifically (a bare `playwright` install driving
   the cached webkit build directly works, and has now done so across two sessions). The residual
   ("why does the MCP wrapper time out") names only a conditional-never action per the archive's
   own three-destinations rule, so this session closed the entry rather than leave a
   fully-answered question open.
4. **Repo-identity classification residual** — RESOLVED, PR #122's classification pass
   (workstream 3 above).

Left open-dormant by their own stated triggers, untouched this session: the three inert `fr__*`
allowlist wrappers, and the unwired `ExpectedMaterialsSection` richer inline editor.

## Concurrency incidents

Two, both worth recording because they shaped how the merge actually happened.

1. **`main` moved twice under this PR.** First three unrelated commits landed while this PR was
   open (#121 — the `/api/recent` cap-gate the CS4 pass had missed; #123 — a field-ops Review-Queue
   fix; #118 — a session-log PR). Then, mid-poll for CI, three more landed (#124, #125, #126). **#126
   was another concurrent session's tech-debt triage, touching the SAME two ledger files this PR
   touches** — a genuine conflict in `docs/tech_debt_closed.md` at the top-of-archive insertion
   point. Resolved **keep-both**, after verifying the two sessions had closed *disjoint* sets of
   entries (no double-resolution, no overwritten resolution note).
2. **A non-fast-forward divergence from a remote-side merge.** `gh`'s `update-branch` operation had
   already produced a merge commit on the remote copy of the feature branch; this session's own
   local merge (resolving incident 1) produced a second, divergent merge commit. A pull-merge was
   required before the push would go through — not a rebase, since the ledger conflict resolution
   had already been made and re-resolving it via rebase risked re-opening the same conflict.

## The verifier declined again — reported, not smoothed over

The `pr-landed-verifier` subagent was invoked against PR #122 and refused to emit "four-part
verify clean" — its second refusal in two days, for a related but not identical reason to the
2026-08-12-evening refusal on #119.

An agent's definition is cached at the start of the session that spawns it. This subagent's cached
brief was still the **pre-#122** version — the one still naming `SolutionSmith-debug/its` as the
canonical repo, before this very PR repointed it. Operating on that stale brief, it declined on
repo-identity grounds a second time, and additionally mis-reasoned that "the tree's identity
changed mid-session": it read the session's opening git-log snapshot, `b591b8a "(#112)"`, and
inferred a repo switch from the PR-number sequence, when that PR number is simply the new repo's
own sequence continuing forward — no identity change occurred mid-session, only within the
verifier's own (incorrect) reading of the evidence.

This is by-design skepticism operating on a brief that had not yet caught up with the fix landing
in the same PR it was asked to check — not a landing defect, and not one of the four verify legs
failing. The failed leg is properly named **leg 0 — repo identity**, a precondition check the
verifier's own brief imposes ahead of the four landing legs, not a fifth landing leg and not a
failure any of the standard four checks reported. A freshly-started session invoking the verifier
after this PR's merge would load the corrected brief and not hit this again.

Because the subagent declined to complete its check, the orchestrating session ran the four
landing legs directly:

- state = **MERGED**
- mergedAt = **2026-08-13T20:22:23Z** (non-null)
- mergeCommit.oid = **`ebca3e48488672dce6df5571b86c639b671b6025`** (present)
- main-branch CI on that commit = run **31740530884**, workflow `ci`, all three jobs
  (`test`/`portal`/`secrets`) **completed / success**

All four legs are positive. This is the orchestrating session's own direct verification, run
because the subagent withheld its verdict — it is not being reported as if the subagent itself
said "four-part verify clean," which it did not.

## Decisions

1. **Fix the select kit portal-wide, not per-page.** Alternative considered: patch just the
   job-detail page's selects, since that is the page where the defect was found. Rejected —
   exactly the deferral reasoning the 2026-08-12-evening entry recorded: the defect is kit-level,
   and a per-page patch would leave every other select in the app still under 44px in Safari. This
   session executed the deferred four-width pass the earlier entry specified rather than inventing
   a new approach.
2. **The chevron rule's specificity is deliberately higher than the family rules that set
   `background`, not merely "high enough to apply."** A same-specificity or lower rule would lose
   the cascade to `.field__input`/`.dash-row`'s `background` shorthand and silently drop the
   chevron on every select that also matches a family rule — verified, not assumed, by testing the
   chevron's presence against both family classes.
3. **Repo-identity classification decided by live-tool evidence (CodeQL reachability), not by
   pattern-matching the old brief text.** Alternative considered: leave the ambiguous surfaces as
   the 2026-08-10 entry found them, deferring the call again. Rejected — the entry had already
   preserved the ambiguity once; this session had a concrete, checkable discriminator (does either
   repo slug support a live CodeQL triage from this host) that the earlier session did not run, and
   running it settled the question rather than punting it a third time.
4. **A ledger conflict with a concurrent session is resolved keep-both after verifying disjoint
   entries, not by picking one side.** Alternative considered: take this session's version of
   `tech_debt_closed.md` and drop the other session's insertions, on the theory that a rebase would
   reconcile it later. Rejected — both sessions had genuinely closed different entries; dropping
   either would silently un-resolve real work.
5. **The verifier's refusal is reported as a refusal, with the actual failing precondition named,
   rather than resolved into "four-part verify clean" on its behalf.** Same posture as the
   2026-08-12-evening log: the four landing legs the orchestrating session ran directly are all
   positive and are stated as the orchestrating session's own verification, not attributed to the
   subagent.

## Open items / next session

- **CodeQL default setup is not re-enabled on `its-sys-admin/evergreen-its`.** This is an operator
  GitHub-settings action, not something this session (or any CC session) can do from the repo. Until
  it is re-enabled, `codeql-fp-triager` stays DORMANT (marked as such in its own brief).
- **The `pr-landed-verifier`'s cached-brief staleness is a recurring class, not a one-off.** Two
  refusals in two days, both rooted in the same mechanism (agent definitions cache at session
  start). The fix for the underlying brief is in this PR; a fresh session should not hit this
  again, but any session that spawns the verifier using a brief cached before a doctrine/identity-
  affecting PR merges will.
- Worktree `/Users/itsmacbook/its-debtsweep` left in place for operator cleanup, alongside
  `/Users/itsmacbook/its-jobdetail-facelift` from 2026-08-12 (per `docs/operations/
  worktree_discipline.md` — neither force-deleted from a session).
- Two blueprint-CLONE config fixes were applied by THIS session (local git/gh config, no file
  edit, no PR): `gh repo set-default its-sys-admin/its-blueprint` (a bare `gh pr create` there
  had silently targeted the stale fork parent — the mis-filed PR #79 incident, 2026-08-12), and
  the `upstream` remote's push URL set to the `no_push` sentinel (it previously had push enabled
  to `SolutionSmith-debug/its-blueprint`; fetch from the parent is preserved). `gh repo
  set-default --view` now reports `its-sys-admin/its-blueprint`.

## What was NOT touched

- No SPA component (TSX) changed by the select-kit fix — the whole treatment is two CSS rules;
  the fix intentionally avoided touching component code for a styling-only defect.
- No new Worker route or D1 migration — the dashboard-wiring workstream is entirely Mac-side
  (`system_map.py`, `act/registry.py`); it adds representation of existing capability, not new
  capability.
- Session logs, memory-archive, closed tech-debt entries, and `context-pack/repo-overview.md` were
  deliberately left naming the old repo slug where relevant, as historical record — not swept by
  the repo-identity classification pass.
- The three inert `fr__*` allowlist wrappers and the unwired `ExpectedMaterialsSection` richer
  inline editor — both open-dormant by their own stated triggers, neither of which fired this
  session.

## Verification

- pytest: ~5,402 passed / 0 failed (dot-count from the quiet reporter; `test_publish_daemon`
  excluded — the documented host-local conftest live-state class, green in CI)
- mypy: 0 errors / 503 source files
- ruff: clean
- dashboard parity suites: 81 passed
- SPA: typecheck clean across all 3 tsconfig projects · vitest 993 passed · worker vitest 1520
  passed (byte-identical to baseline — this PR's Worker-relevant surface is representational only)
  · vite build clean
- main-branch CI on merge commit: SUCCESS (run `31740530884` — `test`/`portal`/`secrets` all
  `completed`/`success`)

## Deploy

`npm run deploy` from `~/its/safety_portal` → version `9f6dcc78-1bbf-47e7-ab7e-f106640b039d` at
`safety.evergreenmirror.com`. Scope deliberately surfaced before deploying: this PR's CSS plus
#121's `/api/recent` cap-gate — the only Worker-affecting changes since the prior deploy
(`8a4ed389`). CSS asset hash rotated to `index-B0M8UooH.css`; the deployed stylesheet was fetched
and confirmed carrying the `appearance: none` rule. The dashboard daemon was restarted separately
(not part of the Worker deploy) and verified serving the new `/system` edge and `/config` row.

## Ops notes

- Worktree `/Users/itsmacbook/its-debtsweep` provisioned with its own Python 3.13 venv. Two
  environment gotchas hit and worked around: the system `python3` resolves to 3.9 (used
  `/opt/homebrew/opt/python@3.13` instead), and the fresh venv's bundled pip (21.x) cannot
  PEP-660-install a pyproject-only editable package (upgraded pip first). Left in place for
  operator cleanup per `docs/operations/worktree_discipline.md`.
- Remote feature branch deleted only after the MERGED-state verify (squash-merge repo convention —
  commits-ahead is not a safe delete signal on its own).

## Cross-references

- `docs/session_logs/2026-08-12_job-detail-design-pass-part-three.md` — files the Safari/WebKit
  select tap-target defect this session's workstream 1 closes, and the `WeeklyReportPage`
  regression-test gap this session's workstream 1 also closes; also the session with the first
  `pr-landed-verifier` refusal, same underlying cached-brief mechanism as this session's refusal.
- `docs/session_logs/2026-08-12_box-root-splits-dashboard-validity-and-wiring-audit.md` — PR #110's
  one-sibling-omitted class in the Class-B send-lane config console, the same class this session's
  workstream 2 closed for the estimate-extraction ladder's Class-E display row.
- `docs/tech_debt_closed.md` — the four `RESOLVED 2026-08-13` entries (Safari selects, WPR
  rail-test gap, Playwright-MCP screenshots, repo-identity residual).
- `docs/tech_debt.md` — the two open-dormant entries left untouched: `fr__*` allowlist wrappers,
  `ExpectedMaterialsSection` richer inline editor.
- `docs/operations/pr_merge_discipline.md` — the four-part landing verify this log's legs follow;
  run directly by the orchestrating session after the verifier's refusal, per the same reporting
  discipline the 2026-08-12-evening log established.
- `docs/operations/worktree_discipline.md` — governs the leftover `~/its-debtsweep` and
  `~/its-jobdetail-facelift` worktrees noted under Open items.
- `.claude/agents/pr-landed-verifier.md`, `.claude/agents/codeql-fp-triager.md` — the two agent
  briefs most directly touched by the repo-identity classification pass (blueprint-fork `--repo`
  caveat; DORMANT marking pending CodeQL re-enablement).
