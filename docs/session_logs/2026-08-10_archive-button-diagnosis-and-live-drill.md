---
type: session_log
date: 2026-08-10
status: closed
related_prs: [33]
workstream: field_ops
tags: [session_log, field_ops, archive_on_closure, section51, box, smartsheet, house_reflexes, cutover, watchdog]
---

# Session log — 2026-08-10 · The archive button diagnosed, the missing gate row found, and the first attended Box drill

**Filename note.** A different session log already exists at
`docs/session_logs/2026-08-10_outage-diagnosis-alerting-gap-and-gate-activation.md`, covering a
separate 2026-08-06 → 08-10 arc (a Smartsheet outage postmortem + the procurement External-Send-
Gate activation). This log covers a **distinct** same-day session. The two arcs touch at one point:
that session's procurement gate activation is precisely why `po-send`/`rfq-send` were already live
when this session's PR #33 ran into VC-02 reporting them as a false "send-gate violation" — see
Cross-references.

## Purpose

Operator ask: "diagnose why the archive button worked on the website, the safety portal and
deleting the production test job, but did not succeed in moving any of the smartsheet folders or
sheets" — extended mid-session to "inspect box as well." What started as a Smartsheet-only
diagnosis became a full six-container root-cause, a repair, and the first attended live archive
drill on the production host (the drill Track 6's 2026-08-07 close explicitly deferred: "Do not
turn it on yet").

## Pre-flight findings

**Nothing moved on either system — Box included.** The operator's belief that Box had worked was
wrong. Live evidence, checked directly rather than inferred from the daemon's own log:

- `ITS — Archive` workspace held only the legacy empty `Closed Projects` folder.
- Both live per-job Smartsheet folders were untouched: `ITS –– Safety Portal/Production test`
  (id `1566626123409284`) and `ITS — Progress Reporting/Production test` (id `553426158413700`).
- The Box per-job folder was still in the LIVE safety root: `ITS Safety Reports/Production test`
  (id `407341446878`).
- No "ITS Archive" root existed in Box at all.

Zero of six containers had moved. The website/portal/D1 deletion of the production test job had
succeeded at the layer it operates on (D1 + the queue row); the operator read that success as
proof the relocation itself had happened, which it had not.

## Root cause

`field_ops.fieldops_sync.archive_enabled` (`[field_ops]`) **did not exist** as an `ITS_Config` row.
`fieldops_sync.py:639` gates `_archive_pass` on `_archive_enabled()` (line 324), which calls
`_read_bool_setting(CFG_ARCHIVE_ENABLED, DEFAULT_ARCHIVE_ENABLED)` with `DEFAULT_ARCHIVE_ENABLED =
False` (`fieldops_sync.py:159-160`) — a missing row and a `false` row are indistinguishable to that
reader, so a capability that "ships dark" had no switch to find.

D1 confirmed the daemon had never touched the job: `JOB-000030` sat at `archive_state='requested'`,
`archive_attempts=0`, `archive_detail=''`. The daemon had been WARNing `config_row_missing` every
90 seconds — 3,442 occurrences by the time this was found — and nothing escalated, because a WARN
never triple-fires.

The seeder `scripts/migrations/seed_daemon_gate_config.py` shipped the row *spec* in PR #20
(`db35067`, 2026-08-07), and PR #20's own commit body says the row is seeded "in the same change"
— true of the repo, false of the tenant: the seeder was never run against this host. CLAUDE.md and
`docs/ROADMAP.md` both repeated the claim. This is HOUSE_REFLEXES §1 ("trust the live code, never
the claim"), but applied to row **presence** rather than row **value** — the class the reflex
names has mostly bitten on stale value/behavior claims before; this is the same failure mode one
layer up the stack.

**A second, latent blocker was found before it could bite.** `field_ops.box.archive_root_folder_id`
also had no `ITS_Config` row, and no "ITS Archive" root folder existed in Box at all. Had the gate
been flipped first without fixing this, the Smartsheet folders would have moved, both Box
containers would have failed, and the job would have landed `partial` — which is **terminal** for
the queue (`WHERE archive_state IN ('requested','in_progress')` excludes it; see issue #29/#30
below). Repair order was therefore load-bearing.

## Repair — Box destination first, gate last

1. `build_box_roots.py --dry-run`, then live: created Box root "ITS Archive", id `408071931845`.
2. Created the missing `ITS_Config` row `field_ops.box.archive_root_folder_id` (Value
   `408071931845`, Workstream `field_ops`). The operator dashboard's config editor can only
   `update_rows` on an existing row — it cannot create one — so this was done directly against
   Smartsheet, not through the dashboard.
3. Seeded the gate row `field_ops.fieldops_sync.archive_enabled = false`.
4. `verify_cutover.py` VC-03 went from **5 failures to 3** — the 3 remaining are the expected
   pre-cutover sandbox `worker_base_url` rows, unrelated to this repair.
5. The daemon's `config_row_missing` WARN stopped at 17:19:03; subsequent cycles logged
   `archive_enabled[field_ops]=False(ITS_Config)` — the row was now being read, not defaulted.

## Live drill — attended, operator-directed

`docs/ROADMAP.md`'s Track 6 close (2026-08-07 log) had said explicitly: "Do not turn it on yet"
pending an attended Box drill. This session's flip **was** that drill — operator present and
watching, on the job named "Production test," the same job the diagnosis had been run against.

Gate flipped to `true`. At 17:37:13 the cycle logged `archive complete=1 partial=0 failed=0
capped=0 errors=0`.

Verified against the actual trees, not the log line:

- `ITS — Archive/Production test` (id `7569742983653252`) now holds `Progress` and `Safety`
  subfolders, each containing that job's week sheets.
- Box `ITS Archive/Production test/Safety/week of 2026-08-01` exists.
- Both live (pre-archive) trees are now empty of the job.
- Folder **IDs were preserved** on the move — permalinks and cell history survived, per the
  documented Smartsheet-move semantics (`shared/smartsheet_client.py`'s "a folder move CANNOT
  rename" note in CLAUDE.md).
- D1 `archive_detail` lists all six containers: three moved, three "nothing to move" (the flat
  `*_Log` ledgers and other non-relocating surfaces the archive was always documented not to
  touch).
- **Open CRITICALs: 0** after the drill.

This is the first live proof of the Box leg in either direction — every prior Box test in this
repo was mocked (2026-08-07 log, "State at close").

## Issues filed — six, all gaps this diagnosis surfaced directly

| # | Title |
|---|-------|
| [#24](https://github.com/its-sys-admin/evergreen-its/issues/24) | §43: job archive has no successor-remediation runbook (3 failure messages point at a file with no archive symptom) |
| [#25](https://github.com/its-sys-admin/evergreen-its/issues/25) | watchdog: no check covers a stale job-archive request — a job parked at `requested` is invisible to every alerting surface |
| [#26](https://github.com/its-sys-admin/evergreen-its/issues/26) | box: Check P reports "fresh" through a live auth failure, and the refresh lock does not cover the token exchange |
| [#27](https://github.com/its-sys-admin/evergreen-its/issues/27) | `verify_cutover.py` runs nowhere — the one tool that compares declared config to the live tenant is unscheduled (root cause of the inert archive) |
| [#29](https://github.com/its-sys-admin/evergreen-its/issues/29) | archive: a PARTIAL relocation leaves the heartbeat green — no CRITICAL, no watchdog signal, job split across two systems |
| [#30](https://github.com/its-sys-admin/evergreen-its/issues/30) | portal: `JobArchivePanel` tells the operator "The system retries automatically" on a partial — it does not, partial is terminal |

Issue #26 was found opportunistically during the repair: Check P had logged "Box OAuth refresh
token fresh (idle 2d)" straight through a window where a live Box call was failing with
`invalid_grant` — a staleness proxy standing in for a liveness probe, plus a refresh lock that
covers only the token *persist*, not the token *exchange* (`shared/box_client.py:175-185`'s own
comment says boxsdk owns the exchange). This `invalid_grant` initially looked fatal mid-session; it
turned out to be a single-use-token rotation race that self-healed on its own — Box reports a
*consumed* token with the identical wording it uses for an *aged-out* one, which is exactly the
ambiguity #26 exists to fix.

Issue #27 names the actual root cause at the tooling level: `scripts/verify_cutover.py` compares
declared load-bearing config against the live tenant and catches exactly this class of gap — but
`grep -rn verify_cutover .github/workflows/` returns zero hits. Nothing runs it. It would have
caught both missing rows before the button was ever pressed.

## Dashboard question — answered, no work required

Operator asked mid-session whether the archive needed wiring into the operator dashboard. **No.**
`tests/test_system_map.py` passes with no new parity failure — nothing about this session's
findings trips a parity tooth (no new plist, marker, `SHEET_` constant, or watchdog letter landed;
the gate-enrollment test in PR #33 is one-directional, asserting membership in `verify_cutover`,
not a dashboard surface). The gate is **already** in the config-editor registry
(`operator_dashboard/act/registry.py:109-112`) and editable there today.

The decisive structural fact: `WatchdogSweepSource` on the dashboard is generic over whatever
`scripts/watchdog.CHECKS` contains, so a new watchdog check appears on the dashboard automatically,
with zero dashboard code. This is why issue #25 (no watchdog check for a stale archive request) *is*
the dashboard fix — landing that check makes it visible without a `operator_dashboard/` PR.

Two genuine, narrow gaps remain in the system map itself (not dashboard wiring, just map
completeness): `archive_enabled` is absent from `fieldops_sync`'s `extra_gates` node data, and
`job_archive` appears in no node's `error_scripts` list. Neither blocks the drill or the repair;
left open, not filed as separate issues (small enough to fold into whichever PR next touches the
system-map registry for this workstream).

## PR landed

### #33 — `fix(cutover): VC-02 called a months-old activation a send-gate violation; enroll the 4th tier gate`

Found while reading `verify_cutover.py` output during the #27 investigation above: VC-02 was
reporting `po-send` and `rfq-send` as "dark-unloaded SEND daemon IS loaded (send-gate violation)."
Both were **deliberately** activated months earlier — gate `true` in `ITS_Config` and plist loaded,
both halves of the documented first-enable procedure per the constant's own comment ("First-enabling
a send path = remove its label here + load its plist"). Only the last documented step — removing the
label from `DARK_UNLOADED_LABELS` — had never been done. `DARK_UNLOADED_LABELS` is now **empty**, a
statement rather than an oversight: all three send lanes read `polling_enabled = true` and are
loaded. The mechanism is retained in full — re-adding a label makes VC-02 fail again if that daemon
is loaded; emptying the set narrows what it currently asserts, it does not remove the assertion. The
External Send Gate itself (Invariant 1's two-process split + human approval,
`tests/test_capability_gating.py`) is untouched by this PR.

Also enrolled `po_materials.estimate_extract.tier1_xlsx_enabled` in VC-03 — missed when PR-B
enrolled its three ladder siblings, invisible because the row happened to exist anyway (VC-03
asserts row *presence*, so an unenrolled key is simply never checked). It is a real gate:
`estimate_poll` declares it in `REQUIRED_CONFIG` and branches on it.

Both new controls were proven to RED-light on a synthetic violation before being reverted
(HOUSE_REFLEXES §2) — re-adding a label to `DARK_UNLOADED_LABELS` and re-running the tests
confirmed the send-gate-violation path still fires with the real set empty. Tests were rewritten
to assert the *mechanism*, not the *membership*: two tests previously pinned `po-send`/`rfq-send`
by name (would need rewriting on every future activation) and now drive off a synthetic label; a
new posture test asserts the current state directly (set empty, all three lanes expected-loaded)
so a future drift between the constant and the shipped plists fails loudly; and
`test_every_estimate_extraction_tier_gate_is_enrolled` enumerates against `estimate_poll`'s own
constants rather than string literals, so a rename cannot leave it passing against a key that no
longer exists.

The stale claim had fanned out to four more surfaces, all corrected in the same PR: the module
docstring's VC-02 row, plus three forward-looking operator docs (`host_migration_runbook.md`,
`cutover_operator_punchlist.md`, `cutover_checklist.md`) — each rewritten to tell the operator to
derive skip-status from `ITS_Config`, not assert a value in prose (HOUSE_REFLEXES §5). Deliberately
left `docs/production_worker_route_decision.md` and `docs/aug7_delivery_runbook.md` untouched as
historical record — they describe a point-in-time decision, not a live-state claim.

**PR #33 — MERGED.** `state: MERGED  mergedAt: 2026-08-10T19:05:07Z  mergeCommit: db509e4` and
`main-branch CI on merge commit db509e4: success`. Confirmed via `gh pr view` (state/mergedAt/
mergeCommit) and `gh run list --commit db509e43f2af7e52429208506cac3a63fc3cac34` (the `push`-event
`ci` workflow run on `main`, conclusion `success`) — the fourth leg per
`docs/operations/pr_merge_discipline.md`, independently re-verified in this session rather than
taken on report.

Gate results (as run by the session):

```
- pytest: 4681 passed / 2 skipped / 58 deselected
- mypy: 0 errors / 481 source files
- ruff: clean
- main-branch CI on merge commit: SUCCESS
```

`check_doctrine_drift --strict` also reported no blocking drift.

## Decisions worth recording

1. **Repair order was load-bearing.** Box destination (root folder + config row) was fixed *before*
   the gate flip, because a `partial` result is terminal for the archive queue (issues #29/#30) —
   flipping the gate first would have moved the Smartsheet folders, failed both Box containers, and
   stranded the job in a state nothing retries.
2. **Declined a pasted throwaway Smartsheet token in favor of the existing Keychain credential.**
   CLAUDE.md forbids non-Keychain credentials outright, and the pasted token had already appeared in
   a transcript — using it would have both violated the standard and used a credential of uncertain
   provenance.
3. **The mid-session Box `invalid_grant` was investigated, not treated as fatal.** It looked like a
   dead refresh token; it was a single-use-token rotation race that self-healed. This distinction —
   Box uses identical wording for a *consumed* token and an *aged-out* one — is exactly what issue
   #26 exists to fix, so the confusion was filed rather than just worked around.
4. **Historical decision records were deliberately not rewritten.** `production_worker_route_decision.md`
   and `aug7_delivery_runbook.md` still read as though the send lanes were not yet activated; left
   as-is because they document a decision made at a point in time, not the present live state — the
   same distinction PR #33 drew for the *forward-looking* docs it did correct.

## What was NOT touched

- The six issues filed (#24–#27, #29, #30) — diagnosed and written up, not fixed this session.
- The two narrow system-map gaps noted under "Dashboard question" (`archive_enabled` missing from
  `extra_gates`, `job_archive` missing from `error_scripts`) — left for the next PR touching that
  registry.
- `docs/production_worker_route_decision.md` / `docs/aug7_delivery_runbook.md` — deliberately left
  as historical record (Decision 4).
- No §43 runbook entry was written for the archive despite issue #24 naming the gap directly — that
  is #24's own scope, not folded into this session.

## Sequencing context

- What this unblocks: the job-archive workflow (Track 6) now has a live-verified Box leg on the
  production host, closing the last open precondition from the 2026-08-07 close-out log ("Do not
  flip the gate before the attended sandbox drill" — this was that drill, run live rather than in
  sandbox, with the operator present).
- What was prerequisite: PR #20's seeder existing at all (2026-08-07) — the gap was that it was
  never *run* against this tenant, not that it didn't exist.
- Follow-ons: issues #24/#25/#26/#27/#29/#30, all filed with enough detail to be picked up
  independently; #27 in particular (scheduling `verify_cutover.py` somewhere) would have caught this
  entire diagnosis before the button was ever pressed.

## Cross-references

- `docs/session_logs/2026-08-10_outage-diagnosis-alerting-gap-and-gate-activation.md` — the other,
  separately-authored 2026-08-06→08-10 session log. That session's operator-directed procurement
  External-Send-Gate activation (14 gates flipped, `po-send`/`rfq-send` among them) is precisely
  the months-old activation PR #33 reconciles `verify_cutover.py` against here.
- `docs/session_logs/2026-08-07_track6-job-archive-completion-and-deploy.md` — Track 6's prior
  close-out, whose explicit deferred item ("Do not turn it on yet," pending an attended Box drill)
  this session executes.
- `docs/session_logs/2026-08-03_track6-job-archive-workflow-pr0-through-pr5.md` — the earlier Track
  6 installment (PR-0 through PR-5).
- `docs/operations/pr_merge_discipline.md` — the four-part verify applied to PR #33.
- `docs/HOUSE_REFLEXES.md` §1 (trust live code over the claim — applied here to row *presence*, a
  variant of the usual value/behavior drift class), §2 (prove-the-control-bites — PR #33's two new
  controls proven to RED-light before shipping), §5 ("seed the gate row even at `false`" — the
  precise failure this session diagnosed; "a dark-shipped gate needs a seeded row" one layer deeper
  than the usual case, since the seeder *existed* but was never *run*).
- Issues #24, #25, #26, #27, #29, #30 — all filed this session, linked above.
- PR #33 (`db509e4`) — the only PR landed this session.
