---
type: session_log
date: 2026-08-10
status: active
workstream: field_ops
tags: [session_log, materials, receipts_mirror, standup, live_smoke, errorCode_1135, dashboard, reconciliation, overnight]
---

# Session log — 2026-08-10/11 overnight · Materials stand-up on the production host, the 1135 live failure, and the unification pass

**Host:** production Mac (live launchd fleet). **Mode:** extended autonomous overnight, operator away.
**Started from:** main `2c9b8ef` (#57), already pulled by the day's parallel sessions.
**Mandate:** execute the dev-Mac deployment brief (stand up + verify the PR4 materials/delivery
workflow), then a forensic reconcile of the parallel sessions' work, tech debt, and the dashboard's
currency — without interrupting the live portal.

## The short version

The stand-up's mechanical half was already done by a concurrent session (0063 applied 01:48Z,
Worker deployed 01:49Z — correctly ordered; live bundle serves `daily-report-v7`). What the brief
could not know: **the Material List back-fill (#40) had been failing live every 90s cycle** —
`ensure_columns` sent per-column indices and Smartsheet errorCode 1135 rejects the whole
multi-column add ("Input column index N is different from the first input column index M"). Mocks
structurally cannot see the rule; the 2026-08-10 audit had flagged `ensure_columns` as the one
new primitive with no live smoke. Fixed (#59, four-part clean), verified live: **Kiwi — Material
List went 14 → 17 columns** on the first post-fix cycle, and the every-cycle WARN storm (1,937
`config_row_missing` + 98 `material_list_column_backfill_failed` rows) went to **zero delta**.

Two ITS_Config rows the code declared were missing on the tenant (the
repo-seeder-is-not-a-tenant-row class): the `receipts_enabled` gate itself and the receipts
row-cap threshold. The first existed in the seeder but the seeder had never been run here; the
second was in no seeder at all (audit E21). Seeder extended (#59), run once — both rows created.

**The receipts mirror's first-ever live fire succeeded.** Gate Description read first (no
precondition), flipped true, and the next cycle created `Kiwi — Material Receipts` with exactly
the right subset: 1 of the 3 D1 receipt events (the other two belong to the inactive job "Test" —
excluded by the bounds-to-active-jobs read, as designed). The live row also demonstrates the
documented sticky-incident semantics (`Line Status: incident` beside a `Partial` mark).

## PRs landed this session (all its-sys-admin/evergreen-its)

| PR | What | Verify |
|---|---|---|
| #59 | `ensure_columns` 1135 fix (shared index; RED-verified regression test) + the fifth row-cap seeder row + its pin test | four-part clean (main CI on `66ce500` SUCCESS) |
| #60 | Dashboard wiring: receipts gate on the fieldops_sync node, receipts identity in trackers `error_scripts`, five-tracker briefs, 4 manifest_poll edges, `receipts_ledger_stale` + `manifest_commit_refused` tree nodes, structural parity teeth (`test_every_fieldops_pass_gate_is_on_the_node`, `test_manifest_poll_is_not_an_edgeless_orphan`), receipts row-cap in the ACT registry, guide + xrefs regen | merged via auto-merge monitor; verify recorded in the PR thread |
| #61 | standup runs `seed_manifest_config.py` (E18) — seeders list hoisted to `SEEDER_STAGE_SCRIPTS`, guard test now checks what standup RUNS, not what the directory holds; `ITS_PORTAL_MANIFEST_TOKEN` added to the A5 table (E22, 20→21) | merged via auto-merge monitor; verify recorded in the PR thread |
| (this) | docs unification: ROADMAP Track 2 truth, runbook rewrites (hours_log_sync Fault F → the disarmed-hook truth; project_closure archive rows), stale-pin fixes, cc-brief supersession banner, three tech-debt entries + two annotations, morning operator checklist | — |

## Live tenant changes (all reversible, none send-path)

- `progress_reports.material_receipts.row_cap_warn_threshold` = 15000 — row created (seeder).
- `field_ops.fieldops_sync.receipts_enabled` — row created (seeder, `false`), then flipped `true`
  after reading its full Description (no precondition; pause-anytime semantics).
- `Kiwi — Material List`: three columns appended by the fixed back-fill (the daemon's own write).
- `Kiwi — Material Receipts`: sheet created by the mirror's first fire (the daemon's own write).
- No Worker deploy by this session (the 01:49Z deploy was the concurrent session's); no external
  send surface touched; no gate other than `receipts_enabled` changed.

## Reconciliation verdicts (audit → post-PR4 HEAD, adversarially re-checked)

- **CLOSED by the dev session:** C11 (daily-report-v7 + pins), C12 (0063 photo→line binding, HMAC
  inside `photo_json`), C13 (`additional_photos` editor arms + fixture teeth).
- **C10 is a ratified narrowing, not a gap:** two-tap covers the three delivery marks only, per
  operator direction; the daily report's one-tap Confirm receipt is tracked UX debt (#58).
- **STILL OPEN (now filed as tech debt with file:line):** the manifest correctness cluster
  A1–A5 + B6–B9 (high — the lane is LIVE; "do not import a real BOM yet" is in the morning
  checklist), the fieldops_sync resilience cluster D14–D17 (medium; D14 is the un-finished #41
  conversion — six sites), and the two designed-but-unbuilt halves (shipments mirror, manifest
  byte-pool prune).
- **Diagnosed in passing:** the 29 `test_publish_daemon.py` local failures are the
  `conftest.py:531` live-state guard firing on this host (tests reach
  `~/its/state/publish_daemon_config_read_failures.json.lock`); CI green because its checkout
  isn't `~/its`. Entry annotated.

## What only a human can finish (deferred, not dropped)

`docs/handoffs/2026-08-11_morning-operator-checklist.md` — the two-tap feel on a phone, a real v7
PDF from Box, an old v5/v6 PDF's note line, the bound-photo upload + cross-job 422, and a fresh
delivery mark moving the receipts sheet's derived columns. The manifest parser eval stays WAIVED
(corpus is on the dev Mac).

## Gates

- pytest: 4834 passed / 2 skipped / 0 failed (worktree, `test_publish_daemon.py` deselected — the
  diagnosed host-local guard; materials suites 221/221 green on the live tree)
- mypy: 0 errors (changed files; 29 dashboard files clean)
- ruff: clean
- main-branch CI on merge commit: SUCCESS (`66ce500`, #59; #60/#61 recorded at their merge)
