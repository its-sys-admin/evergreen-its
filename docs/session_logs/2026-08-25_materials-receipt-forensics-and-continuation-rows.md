---
type: session_log
date: 2026-08-25
status: closed
related_prs: [188, 189, 190]
workstream: field_ops
tags: [materials, forensics, manifest-import, capability-gating, audit, migration]
---

# Session log — 2026-08-24 → 2026-08-25 · Henry's phantom receipt, a continuation row that duplicated a line, and the audit that caught its own migration header

Opened as a forensic investigation: manager Henry Rogers (`rogers.henry`) believed he had marked
Kiwi (JOB-000029) materials delivered on 2026-08-24; the portal showed nothing. A six-lens
structural audit established the mark never reached the server, then traced the mechanism to a
stale UI instruction plus a validator gap. The fix that followed, and a second fix to the
manifest-continuation commit path found along the way, together closed a six-lens standards audit
of both. Three PRs landed same-day, deployed same-day.

## Commits landed

| PR | SHA | Purpose |
|---|---|---|
| #188 | `4d156b3` | `qty` required on `delivered`/`partial` at the shared `readReceiptFields` validator (covers both `/receipt` and the legacy `/receive` alias); per-line inline validation ahead of the two-tap arm; stale receive-confirmation UI copy fixed; migration 0079 grants `manager` → `cap.materials.manage`, removes that cap from `SCOPE_BYPASS_CAPS` |
| #189 | `a6dd199` | Manifest commit route now reads `job_manifest_rows.kind` server-side; a `continuation` row becomes a `material_shipments` load on its parent instead of a duplicate BOM line carrying the parent's forward-filled order quantity |
| #190 | `30a3f06` | Six-lens standards audit of #188/#189: fixes `shipment_new_line`'s null `parentTarget` (409'd a whole `import_as='shipments'` page) and a deselected-parent silent re-parent (cross-checked against the forward-filled part number); flags migration 0079's header for asserting a bypass removal the same commit performs, while omitting that the grant is cross-job WRITE with no matching cross-job READ |

## CI runs / verification

Four-part landing verify, all three:

**PR #188 — four-part verify clean**
- state: MERGED
- mergedAt: 2026-08-25T12:50:16Z
- mergeCommit: 4d156b311f106fb97b62e21822e119778ca97dd2
- main CI on merge commit: SUCCESS (run 32849826001, workflow: ci)

**PR #189 — four-part verify clean**
- state: MERGED
- mergedAt: 2026-08-25T21:19:13Z
- mergeCommit: a6dd1998ff885b930df840f1718eb352e9b396aa
- main CI on merge commit: SUCCESS (run 32900367281, workflow: ci)

**PR #190 — four-part verify clean**
- state: MERGED
- mergedAt: 2026-08-25T23:05:20Z
- mergeCommit: 30a3f066a36bfad85a94bf79f6eca272828ccd73
- main CI on merge commit: SUCCESS (run 32909185712, workflow: ci)

Newest main commit is `30a3f06` (PR #190's merge commit), matching `origin/main` HEAD.

On the final #190 tree:

- vitest (worker): 1622 passed / 82 files
- vitest (spa): 1064 passed / 77 files
- tsc: clean (app + worker + test projects)
- pytest: `test_error_copy_parity`, `test_portal_css_classes`, `test_worker_send_free` — passed
- check_doctrine_drift --strict: exit 0 · build_docs_pdfs --check: 22/22 current
- ruff: clean (no Python files changed this session)
- mypy: 0 errors / 512 source files
- main-branch CI on merge commit: SUCCESS (all three — #188 run 32849826001, #189 run 32900367281,
  #190 run 32909185712)

## The forensic finding (before any code changed)

A six-lens structural audit established, without inference, that no material action was recorded
by anyone on any job on 2026-08-24: `sqlite_sequence` for `material_receipt_events` equalled
`MAX(id)` (nothing inserted-then-deleted), `audit_log` was contiguous, the prune job deleted zero
rows, and the §51 Smartsheet mirror agreed exactly with D1. Henry's `audit_log` history was 3 rows
total, last dated 2026-08-05. Conclusion: his taps never issued a request the server received.
Leading mechanisms identified: a UI still instructing receive-capable users to confirm receipts
from daily-report controls that PR #74 removed on 2026-08-11; a two-tap arm sequence whose first
tap issues no HTTP request; and a daily-report draft that persists to `sessionStorage` but only
counts once actually submitted.

## Decisions made during session

1. **Deploy before migrate, inverting the usual order.** Migration 0079 adds no columns and the
   Worker change only narrows the existing bypass set (`SCOPE_BYPASS_CAPS`). Applying 0079 first
   would have given six managers cross-job material access for however long the deploy took.
   Code shipped tighter than data, never the reverse — migration 0079 is merged but deliberately
   **left unapplied** pending the write-scope question below.
2. **The D1 diagnosis from the forensic workflow was wrong, and the fix direction changed
   accordingly.** It had named a stale within-page `byPart` index as the cause of the continuation
   duplicate; the real cause was the commit route ignoring `job_manifest_rows.kind` entirely.
   Fixing only the index would still have produced duplicate lines on every continuation row.
3. **D3 (the `deliveries_received` table's misleading name) is only half-fixable in this
   session.** Renaming it is a `required_field_keys` legal-floor change and needs a
   `daily-report-v8` publish through the §50 lane — escalated, not attempted here. The code-side
   half (honest copy on the card sitting above the stale table) shipped in #189.
4. **Every control landed this session was proven to RED-light on an injected violation**, then
   restored from byte-for-byte backups — never `git checkout` — before merge.
5. **Migration 0079 stays unapplied pending a decision on write-scope asymmetry.**
   `requireJobScope` currently guards 4 call sites in `fieldops_expected_materials.ts` and none of
   the 7 material CRUD routes or the 9 manifest routes — so the new manager grant is cross-job
   WRITE-capable with no cross-job READ path to match it. Flagged, not resolved.

## Live validation

After #188 deployed, Henry Rogers marked 14 Kiwi lines (2026-08-25 10:24–10:55 PDT), every one
carrying a quantity — against 42 of 45 Kiwi lines that were qty-`NULL` beforehand. Kiwi's
expected/received count moved from 50/0 to 39/11, surfacing three genuine partial deliveries
(real remainders) and three over-deliveries.

## Known-unfixed, carried forward

- Kiwi's 1 pre-existing duplicate manifest line and Deep Lake's 5 (both predate #189; removing
  them now is a production write, not attempted this session).
- Deep Lake: 37 lines show `Delivered` beside a full outstanding balance — 39 qty-`NULL` events
  predating #188. Repair needs real quantities sourced from the office, not inferred.
- D4's other half: shipped-quantity column is still not present on the manifest commit wire.
- D3's other half: the `deliveries_received` table rename needs the `daily-report-v8` §50 publish.
- `tests/test_portal_css_classes.py`'s regex has a known blind spot over roughly 53
  expression-form `className={…}` sites — pre-existing, not touched.
- Two workflow agents in the standards-audit workflow died on API errors mid-run, including the
  skeptic lens — that audit is one adversarial lens short of complete.

## What was NOT touched

- The write-scope asymmetry itself (`requireJobScope` coverage gap) — flagged in #190's review,
  not remediated; migration 0079 stays unapplied until it's resolved.
- Deep Lake's and Kiwi's pre-existing duplicate/qty-NULL manifest rows — production data repair,
  operator call.
- The `daily-report-v8` form publish for the D3 table rename.

## Cross-references

- `docs/tech_debt.md` — the known-unfixed items above are tracked there (added by
  `session-close-maintainer`, running concurrently this session on the info-gap doc, memory
  archive, and tech-debt file; not duplicated here).
- `docs/operations/pr_merge_discipline.md` — four-part verify convention used above.
- Related workstream: `field_ops` (`field_ops/manifest_poll.py`, `safety_portal/worker/fieldops_expected_materials.ts`, `worker/fieldops_materials.ts`).
