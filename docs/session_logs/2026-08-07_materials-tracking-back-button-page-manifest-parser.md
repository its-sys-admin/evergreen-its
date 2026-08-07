---
type: session_log
date: 2026-08-07
status: closed
related_prs: [724, 725, 727]
workstream: field_ops
tags: [session_log, field_ops, portal, materials_tracking, section51, ui_accessibility, adversarial_review, manifest_parser]
---

# Session log — 2026-08-06 → 08-07 · Materials-tracking program: back-button contrast fix, per-job Materials page (migration 0059), and the BOM/shipping-log manifest parser

## Purpose

Operator-directed materials-tracking program, planned with Explore + Plan agents and then
executed as three staged PRs: (1) fix an invisible deep-link back button reported from the field,
(2) build the per-job **Materials tracking** page — the standing home for a job's expected
materials, scheduled loads, and delivery ledger that `docs/ROADMAP.md` Track 2 / M2 had left as an
open decision — and (3) build a pure-function parser over the office's real BOM and shipping-log
documents as the foundation for manifest import (PR3b, not started this session).

## PRs landed

### #724 — fix(portal): the deep-link back button is invisible — `.btn--ghost` on a light surface

Deep-linking from the daily field report into a form landed on `FormFillPage`, whose "← Back to My
Tasks" control was `.btn--ghost` — the white-on-green `AppHeader` variant — rendered on the light
page ground (`--c-surface` `#f7f6f2`), roughly 1.04:1 contrast. The same label rendered correctly
green on the post-submit screen, so the two halves of one flow disagreed. Fixed to
`.btn--secondary`, the canonical in-page back control; swept five sibling light-surface ghosts
(`ChecklistItemRow` ×4 → `--secondary`, `RfqBuilderPage` remove-vendor chip → `--danger`).
`global.css` already stated the rule in prose but it lived only at review; new
`tests/test_portal_button_variants.py` promotes it to a CI gate — a plain-text source scan (the
SPA tsconfig carries no `@types/node`, so a vitest suite reading its own source fails typecheck)
that forbids `.btn--ghost` anywhere except a call site that also invokes `logout()` on the same
line. Proven to RED-light on an injected violation, and it then caught one of my own errors mid-session — a
`.not.toContain("btn--ghost")` assertion put the literal class string into a file the scanner reads.

**PR #724 — four-part verify clean.** `state=MERGED` · `mergedAt=2026-08-07T01:02:19Z` ·
`mergeCommit.oid=546adfbdfe1ae160b0610c2d1ecf12c8f2c51058` · main-branch CI on the merge commit
SUCCESS (`ci`/`test` SUCCESS, `ci`/`portal` SUCCESS, `ci`/`secrets` SUCCESS, `CodeQL` SUCCESS).
Independently re-verified against live GitHub this session, not taken on report.

### #725 — feat(portal): per-job Materials tracking page — receipt ledger, shipments, three-way marking

Migration `0059`: three additive columns on `job_expected_materials` (`part_number`, `category`,
`expected_ship_date`) plus two new tables — `material_receipt_events` (append-only delivery
ledger) and `material_shipments` (scheduled loads with BOL/carrier/ship+delivery dates). New Worker
route `POST /api/fieldops/expected-material/:id/receipt` (three-way mark: delivered / partial /
not_delivered), shipment CRUD, `/receive` re-pointed onto the same ledger writer so there is ONE
writer, `RECEIPT_ROLLUP_SQL` exported and shared by the SPA read route and the §51 snapshot so the
portal and Smartsheet cannot disagree, and purge-job cascade extended to both new tables. New
`safety_portal/src/pages/JobMaterialsPage.tsx` at `/jobs/:jobId/materials`, deep-linked from the
Job Tracker's job detail and from the daily report's `expected_materials` mount (no form-definition
change needed — that mount's body is authored in `FormRenderer`). New §43 runbook
`docs/runbooks/job_materials.md` (skeleton status; ledger-not-flag mental model, capability table,
symptom table, escalate-to-Seth boundary) + a troubleshooting-tree node. `docs/ROADMAP.md` Track 2
/ M2's long-open decision is marked RESOLVED against this migration.

**PR #725 — four-part verify clean.** `state=MERGED` · `mergedAt=2026-08-07T02:01:04Z` ·
`mergeCommit.oid=63723e682b446ed8a62b7842702214f4d28d84cf` · main-branch CI on the merge commit
SUCCESS (`ci`/`test` SUCCESS, `ci`/`portal` SUCCESS, `ci`/`secrets` SUCCESS, `CodeQL` SUCCESS).
Independently re-verified against live GitHub this session, not taken on report.

### #727 — feat(field-ops): manifest parser — BOM + shipping-log documents to a reviewable grid

`field_ops/manifest_parse.py`: pure functions over cell grids, no I/O, emitting a normalized grid
plus a PROPOSED column map and a document profile — never emitting import-ready lines, because the
shape itself is the judgement call on a DELTA BOM. 20 tests transcribing each observed structure as
a literal grid, since the source documents are customer data outside the repo.
`scripts/eval_manifest_parse.py` replays the whole extract-to-parse path over the real corpus
(operator-run, never CI): **10/10 documents produce importable rows.**

**PR #727 — four-part verify clean.** `state=MERGED` · `mergedAt=2026-08-07T17:40:02Z` ·
`mergeCommit.oid=437e8fac2f54aa1b50ca54e10a5d154f62028324` · main-branch CI on the merge commit
SUCCESS (`ci`/`test` SUCCESS, `ci`/`portal` SUCCESS, `ci`/`secrets` SUCCESS, `CodeQL` SUCCESS).
Independently re-verified against live GitHub this session, not taken on report — no leg was
found pending at verification time.

## Technical decisions made during session

1. **The `status` CHECK on `job_expected_materials` was deliberately NOT widened** to add
   `partial`/`not_delivered`. The obvious move was a full table rebuild (the `0032`/`0020`
   pattern — SQLite cannot `ALTER` a `CHECK`). Rejected on a sharper ground than the rebuild cost:
   `incident` and a partial delivery are ORTHOGONAL axes — a line that is half-delivered AND
   damaged can hold only one value in a single-column status, so the next delivery mark would erase
   the damage flag. The three-way state is derived on read from the ledger instead
   (`material_receipt_events`, latest event wins), and `status` keeps its exact 0031 legacy meaning
   as a coarse projection five other surfaces already speak. This also avoided a UNIQUE-index
   recreate on the mirror key, a `sqlite_sequence` re-seed, and a five-surface vocabulary fan-out
   (wire-type enum, `statusPill()`, `material_list.py`, `material_incidents.py`, `portal_client`'s
   docstring).
2. **§51 Material List mirror exposure of the new columns deferred to PR4**, not this program.
   `shared/smartsheet_client._resolve_cells` raises `KeyError` for a column title absent from an
   already-created per-job sheet — extending the mirror needed its own migration-and-backfill
   treatment, not a rider on the page build.
3. **`prune.ts` deliberately left unchanged.** Lines are soft-deleted, never hard-deleted, so the
   existing `job_expected_materials` NOT-IN guard covers both new tables transitively —
   independently confirmed by the security reviewer rather than assumed.
4. **The parser emits a proposal, not lines**, and the DELTA-BOM default is evidence-backed rather
   than assumed: `Σ(QUANTITY…) + OVERAGE == REV 2` holds on 47/47 and 48/48 rows across both
   Bonacci files in the real corpus, so the eventual validate page can show its working rather than
   assert it. The operator ratified REV 2 as authoritative with an admin override (see ratified
   decisions below).
5. **A generic "ragged row" flag was written and then REMOVED.** Against the real corpus it produced
   28 false flags across the two shipping logs (a line that legitimately has not shipped yet has no
   ship date/BOL by design) and missed its own motivating case — the Bradley row whose truncated
   description bled a stray "3" into the `GROUPING` column has every cell populated, so a
   ragged-row heuristic would never have caught it. Removed rather than shipped as a false signal.

## Ratified operator decisions (do not re-ask)

1. Delivery marks are an append-only ledger, not a status flag; the line's displayed state is a
   rollup over the ledger.
2. Two-level import model: BOM rows become expected-material *lines*; shipping-log rows become
   *shipments* (loads) attached to a line by part number.
3. A re-upload is a tracked batch, with merge-vs-add-as-new chosen at validation time, not at
   upload time.
4. REV 2 is the authoritative quantity on DELTA BOMs, with office/admin review and a per-upload
   override available.
5. PR4 = Option A — the filed daily report will carry a server-sourced snapshot of the day's
   marks, accepting the inversion of the `expected_materials` mount's prior "files no values"
   contract. Cuts a new `daily-report-v7` form definition.
6. Mirror the receipt ledger to Smartsheet as a per-job `<Job> — Material Receipts` sheet, on the
   `material_incidents.py` append-only pattern.

## Adversarial review — both returned BLOCK, all findings folded pre-merge

**`portal-worker-security-reviewer`** found a real regression I introduced on #725: `/receive`
returned a hardcoded `status: "received"` even on an incident-flagged line where the projection
`UPDATE` deliberately no-ops (sticky-incident behavior), so the daily form would show the crew a
resolved delivery while the §51 mirror still flagged the problem underneath it. Pre-PR that call
path was an honest 409. My own test asserted the DB row but not the response body — exactly the
blind spot the reviewer found. Fixed by reading the persisted status back before responding; the
new assertion is proven to bite (fails when the hardcoded-status regression is reintroduced). Also
folded: the shipment-belongs-to-line check moved INTO the `INSERT`'s own `WHERE` clause (a TOCTOU
class), and a false claim in migration `0059`'s header comment about the §51 snapshot binding the
new columns (it deliberately does not — corrected in the migration's own text).

**`ops-stds-enforcer`** found: `docs/runbooks/material_catalog_admin.md` still documented the
409-on-repeat contract this PR changed; a Worker comment claimed "Documented in the §43 runbook"
when no such entry existed yet (a false claim shipped in code); the new page had no §43 entry at
all at review time; and `docs/ROADMAP.md` Track 2 / M2's "OPEN DECISION" line had effectively been
resolved by the build but still said otherwise. All four fixed before merge.

## CI registry fan-out — three gates, in sequence

Landing #725 tripped three separate registry-parity gates, each catching a real omission:
`tests/test_error_copy_parity.py` (a new Worker error code needs plain-language copy in the
registry — `invalid_kind`/`invalid_part_number` were already taken, so the receipt one became
`invalid_receipt_kind`; an ad-hoc translation map I'd written directly in the page was deleted in
favor of the canonical registry lookup); `tests/test_docs_pdf.py` (enablement-manifest sha drift);
`tests/test_troubleshooting_tree.py` (the new runbook needed a tree node, then a rebuild of both
`build_runbook_xrefs` and `build_troubleshooting_guide`, and the guide itself is manifest-tracked
so its sha needed re-recording too, one layer down from the first fix).

## Mistakes made this session, recorded honestly

- **Scoped `mypy` instead of the full-tree run twice** — ran `mypy <file>` on the file I was
  editing instead of `mypy .`; two errors surfaced only when CI ran the full check.
- **Ran the `.btn--ghost` source-scanning guard before my last edit rather than after**, so it did
  not catch the assertion string it should have caught until a later run.

## A factual correction

A design subagent earlier in the program reported the Deep Lake shipping log as "51 parts across
1,195 continuation rows" (migration `0059`'s first-drafted header comment said "1,246 rows," taken
from the sheet's declared extent). The file actually holds **57 non-empty rows** (51 parts + 5
extra loads) inside a sheet that DECLARES 1,247 rows × 92 columns — the rest is padding. The wrong
figure reached an operator summary and the migration's own header comment before the parser
(#727) falsified it against the real file; the migration comment was corrected in the same PR,
with the correction dated in-line rather than silently replacing the original claim.

## Verification

Final local gate before the last push (per-PR CI reproduced independently above; this is the
whole-tree state at session close):

- pytest: full suite green, no failures
- mypy: `Success: no issues found in 474 source files`
- ruff: `All checks passed!`
- worker vitest: 1192/1192
- SPA vitest: 743/743
- main-branch CI on all three merge commits: SUCCESS (verified independently per PR above)

## What was NOT done / open items handed off

- **PR3b — manifest import transport.** Migration `0060` (`job_manifests` / `job_manifest_chunks`
  / `job_manifest_rows` / `job_manifest_previews`), the §34-screened upload route
  (`worker/fieldops_manifests.ts`, `manifest:v1` HMAC domain tag), the polling daemon, and the
  validate/commit page are all unbuilt. Not started this session.
- **PR4 — daily-report snapshot + confirmation photos + the §51 receipts mirror.** Ratified as
  Option A (above) but not started: the `daily-report-v7` form definition, the server-sourced
  snapshot wiring into the filed PDF, and the `<Job> — Material Receipts` Smartsheet mirror all
  remain.
- **Handoff brief for the next session:** `~/.claude/plans/we-need-to-create-eager-rossum.md` —
  written at this session's close, verified against live `main`, and itself flagged as a hypothesis
  to re-`grep` rather than trust on read (per HOUSE_REFLEXES §1).
- **Nothing deployed.** Migration `0059` was applied to a LOCAL D1 only this session; the live
  Worker has not been redeployed against it.

## Cross-references

- `docs/operations/pr_merge_discipline.md` — the four-part verify all three PRs satisfy.
- `docs/ROADMAP.md` Track 2 / M2 — the design slot #725 resolves; PR3b/PR4 remain open under it.
- `docs/runbooks/job_materials.md` — the §43 skeleton runbook #725 ships; polished §6a content
  still pending the enablement-doc program.
- `docs/runbooks/material_catalog_admin.md` — the sibling runbook whose 409-on-repeat contract this
  program changed and whose text was corrected during adversarial review.
- `docs/tech_debt.md` → "D1-primary tables have no ITS-side backup" — the escalation path the new
  runbook cites for the per-event receipt history, which lives only in D1.
- `~/.claude/plans/we-need-to-create-eager-rossum.md` — the next-session brief for PR3b/PR4.
- `docs/HOUSE_REFLEXES.md` §1 (trust live code — the Deep Lake row-count correction, the parser
  falsifying an earlier design claim against the real files), §2 (prove the control bites — the
  `.btn--ghost` gate inject-confirm-revert, and the sticky-incident regression test proven to bite
  after the security reviewer's finding), §4 (adversarial review as definition-of-done on a
  D1-write-route surface — both reviewers found real issues neither test suite could have).
- `its-blueprint` PR #75 (`155e8cdb`) — the planning-side companion: info-gap doc refresh +
  memory-archive §G80 for this session.
