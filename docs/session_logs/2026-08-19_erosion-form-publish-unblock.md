---
type: session_log
date: 2026-08-19
status: closed
related_prs: [180, 181]
workstream: safety_portal
tags: [publish-daemon, form-editor, acroform, ci-gate, diff-hygiene, production-verify]
---

# Session log — 2026-08-19 · Two failed form publishes: a test that counted across sections, and a CI gate that read its own cancelled run as failure

`erosion-inspection-v1` — the first form authored end-to-end in the admin form editor — failed to
publish twice. The operator's read was "minimum row was set to one, so why does CI say 4?" Both
failures were real, unrelated, and neither was the form. Fixed both, re-published, and verified the
form live in the portal from the dev host.

## Commits landed

| PR | SHA | Purpose |
|---|---|---|
| #180 | `52df88d` | Per-section `min_rows` counting; superseded-cancellation guard in both CI gates; `ensure_ascii=False` on daemon catalog/form writes |
| #181 | `8dd2326` | The publish itself (daemon-authored): `erosion-inspection-v1` + catalog entry |

## CI runs

| Run | Result |
|---|---|
| PR #180 | test / portal / secrets — SUCCESS |
| main on `52df88d` | SUCCESS (run 32261788436) |
| PR #181 (req-7) | test / portal / secrets — SUCCESS |
| main on `8dd2326` | SUCCESS (run 32280885323) |

Four-part verify on #180:

- pytest: 5711 passed / 4 skipped / 58 deselected
- mypy: 0 errors / 511 source files
- ruff: clean
- main-branch CI on merge commit: SUCCESS

## The two faults

**1. `test_row_tables_emit_exactly_min_rows` counted across the whole document.** Field names are
`f{n}_{key}` off a *per-document* counter (`form_pdf._FieldNamer`), so the name suffix carries no
section identity. The test compared a document-wide count against one section's `min_rows`.
`erosion-inspection-v1` has four tables whose first column is `col_1`, so the count was 4 against an
expectation of 1. The renderer was correct throughout: every count equalled exactly the number of
tables sharing that key (`col_1`→4, `col_4`→3, `col_5`→2), and uniquely-keyed columns passed. 17 of
19 column assertions failed; pytest reported the first.

Key reuse across sections is **legal by contract** — `worker/publishValidation.ts` validates column
keys with `localUnique(colKeys, "column")`, scoped to one section, reserving cross-section
uniqueness for top-level value keys. The SPA validator agrees. That is why the definition cleared
the enqueue gate and only died in a Python unit test.

And the editor *manufactures* this shape: `editorModel.blankSection` seeds every new table with
`col_1`, and `FormEditor`'s `${keyHint}_${fields.length + 1}` restarts numbering per section. **Any
editor-authored form with two or more tables tripped this.** A sweep found 0 of 59 shipped forms
with a cross-section column collision — every prior form was hand-authored with distinct keys, which
is why a test this wrong survived 59 forms.

**2. `_wait_for_ci` treated a superseded CANCELLED check-run as CI failure.** `ci.yml` sets
`concurrency: ci-${{ github.ref }}` with `cancel-in-progress: true`, and the daemon's own
push-then-`pr create` sequence can put two runs on one ref. GitHub cancels the older; its jobs sit
in the rollup as CANCELLED beside the live ones. `CANCELLED` was in `_CI_FAIL_CONCLUSIONS`, so req-6
died on the first 20s poll while the real run still had four minutes to go — and reported bare job
names (`test; portal; secrets`) with no detail, because a cancelled job has no failing step for
`_check_failure_detail` to quote. The bare-names-no-detail shape is the signature.

## Decisions made during session

- **Per-section isolation over document-wide aggregation.** A parallel review proposed summing
  expected counts per column key across the document. Rejected as the primary fix: it preserves a
  false-negative (2 rows in `table1` + 0 in `tablebmp` still sums to 4), which the reviewer
  acknowledged and proposed papering over with a companion synthetic fixture. Demonstrated the miss
  concretely, then shipped **both** — isolation for exact per-section counts, aggregation for the
  real document's totals, since isolation is structurally blind to a section dropped during full
  assembly. Rejected section-qualifying `_FieldNamer` bases: it changes production artifact bytes to
  fix a test, and the 40-char base truncation has only two characters of headroom.
- **Narrow CANCELLED guard, fail-closed.** Only a CANCELLED run with a live-or-succeeded successor
  of the same name is neutralised. A cancellation with no successor (operator cancelling a run) still
  fails. A genuine FAILURE beside a superseded cancellation still raises.
- **Decomposed rather than declared a ratchet regression.** `_is_superseded_cancellation` first
  landed at CC 11 — one over threshold — putting its whole mass in the erosion numerator twice and
  tipping `structural_erosion` past its ceiling (main sat 0.0002 under). Split into named predicates;
  the metric ended *below* main's baseline at 0.3874 vs 0.3888.
- **`ensure_ascii=False` needed no normalization pass.** Re-serialising the live `catalog.json` with
  the fix produced a zero-line diff, proving the committed file was already in the target state — the
  fix makes daemon writes idempotent rather than requiring a one-time reformat.

## Verification

Every control was proven to bite (inject → confirm → revert), not merely observed green:

- Neutralising the cancellation guard reproduces the **exact** production string
  `CI failed for …: test; portal; secrets`
- Reverting `ensure_ascii` reds the serialisation guard
- Monkeypatching `_min_rows` (`+1`, `always 1`, `flat 2`) reds the per-section assertion; clean on revert
- 59 shipped forms + the branch's erosion definition: 60/60 pass, zero regressions

Production verification from the dev host (D1, Smartsheet and the Worker are all cloud-reachable):
req-7 ran `queued → validated → tested → live → archived` (Stage 4 = terminal success), and
`erosion-inspection-v1` was confirmed **in the live SPA bundle** (`/assets/index-f02x1LR7.js`) —
proving Stage 3 `npm run deploy` actually ran, not merely that the merge landed. The
`ensure_ascii` fix is visible in the daemon's own output: req-6's catalog write carried **29**
`\uXXXX` escapes and a 276-line diff; req-7's carried **0** and 33 lines.

The cancellation guard was **not exercised** by req-7 — it produced only two CI runs with no
duplicate push, so req-6's double-push was a one-off rather than deterministic. The test fix alone
unblocked the publish; the guard is insurance.

## Open items handed off

- **`manifest_poll`: two CRITICALs open since 08-11** (`manifest:1`, `manifest:2`) — items fenced
  one-shot with **no** operator ticket and they will not retry. Needs manual disposition.
- **Two `ITS_Review_Queue` items past 2× SLA** since Aug 13/14 (`field_ops-20260813-215718`,
  `progress_reports-20260814-18…`).
- **Alerting push leg failing**: `ResendAuthError('HTTP 403')` observed live on every push attempt,
  so every CRITICAL above reached nobody. Pre-existing; see `project_alerting-path-broken`.
- **Publish deploy gate is fragile**: a 2026-08-18 CRITICAL halted a publish with "could not verify
  remote D1 migration state (fail-closed)" — a Cloudflare API failure, independent of anything fixed
  here. It did not recur on req-7 but remains a live failure mode.
- **Stale doc references to the Florida host** — `docs/tech_debt.md:2447,2458` are forward-looking
  action items naming "the Florida production host"; `CLAUDE.md:233` frames Florida → customer-site
  as a future gate. The production Mac is in **California** and the handover is complete. Session
  logs naming Florida are historical and should stay.
- **`SessionStart` hook asserts the wrong topology on the dev Mac** — it claims the session is rooted
  at the live daemon tree with 60s-to-live edits. That is false on this host (0 ITS launchd jobs,
  0 plists, logs frozen 2026-07-25) and fires every session.

## What was NOT touched

- **PRs #178 / #179 and their `publish/req-5-*` / `publish/req-6-*` branches** — left OPEN by
  operator instruction until the re-publish succeeded. Now superseded by req-7 and safe to close.
- **The form definition** — valid as authored; the duplicate `col_1` keys are legal and were not
  edited. Fixing the editor's key generation would not have unblocked this request.
- **The form editor's key generator** — still emits per-section-restarting `col_N`, and header
  fields (`field_N`) which *are* top-level and therefore genuinely collide across two header
  sections. Caught by validation, so friction not data loss. `uniqueKey()` / `topLevelKeys()` in
  `editorModel.ts` remain dead code whose docstrings claim a guarantee the editor does not implement
  (§52 narrated-not-enforced).
- **The stale-Florida doc references and the CLAUDE.md gate line** — operator declined a cleanup PR.
- **Send-lane posture** — the five send dispatchers stopping 2026-08-17 15:46Z was raised as a
  finding and the operator confirmed it was a deliberate gate flip. No action taken.

## Lessons captured to memory

- `reference_form-editor-duplicate-column-keys` — column keys are section-scoped by contract; the
  editor restarts `col_N` per table; never count AcroForm fields document-wide by name suffix.
- `reference_dev-mac-can-reach-production-state` — D1 / Smartsheet / Worker are all cloud-reachable
  from the dev host with read-only queries, so a production health question rarely needs that box.
- `project_production-host-migration-2026-07-26` — corrected Florida → California; flagged that every
  host fact in it was captured at the Florida siting (timezone especially, since calendar jobs move).
