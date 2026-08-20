---
type: session_log
date: 2026-08-20
status: closed
related_prs: [185, 186]
workstream: safety_portal
tags: [progress_reports, weekly-production-report, materials-lane, grid-viewport, ux, deploy]
---

# Session log — 2026-08-20 · Two operator-requested portal features: WPR material-list curation and resizable review grids (both deployed live)

Two operator-requested features, planned together via `grill-me` (7 ratified decisions), built in
parallel worktrees, adversarially reviewed, merged as two PRs, and deployed live to
`safety.evergreenmirror.com` from the dev Mac (Florida). PR #185 turns the Weekly Production
Report's Material Deliveries/Material Problems section — previously the page's only fully
read-only block — into an operator-curated snapshot using the same three-state contract already
proven on WPR photos. PR #186 adds sticky headers + an operator-resizable viewport to the two
biggest review grids in the portal (manifest validate, estimate disposition) and folds in a live
CSS regression from PR #114.

## Commits landed

| PR | Merge commit | Purpose |
|---|---|---|
| #185 | `0576dfd747e0b52cbd9a0fceb87675185771035e` | `deliveries_json`/`incidents_json` (migration 0078) on `job_weekly_report_inputs`; curated-wins precedence in the one shared `buildReportData`; office-editable Material Deliveries/Material Problems tables; adversarial-review fixes folded in (413 body-size guard, no-silent-caps flags/banners, unmarked office-added delivery rows, `DELIVERY_KINDS`/`RECEIPT_KINDS` unification) |
| #186 | `795acd168d8fcd3444ce631028aaa8d04b9c5cc5` | New `GridViewport` component (sticky `thead`, drag-resizable, Compact/Tall/Max presets, per-screen `localStorage` persistence); wired into `ManifestValidatePage` and `EstimateDispositionPage`; PR #114 CSS regression (inline `gridTemplateColumns` defeating the responsive split) fixed with a regression pin; first page-level test coverage of `EstimateDispositionPage` |

Follow-up commit `ab586f5` (folded into #185's history before merge) fixed a first-run CI failure
in `tests/test_error_copy_parity.py` — `invalid_deliveries`/`invalid_material_incidents` lacked
`ERROR_COPY` entries. This is the registry-fan-out class the gate exists to catch, and it caught it.

## CI runs — four-part verify (quoted verbatim from `pr-landed-verifier`)

```
PR #186 (feat/grid-viewport) — four-part verify clean
- state: MERGED
- mergedAt: 2026-08-20T17:17:49Z
- mergeCommit: 795acd168d8fcd3444ce631028aaa8d04b9c5cc5 (matches expected)
- main CI on merge commit: SUCCESS (run 32396820042, workflow: ci)

PR #185 (feat/wpr-materials-curation) — four-part verify clean
- state: MERGED
- mergedAt: 2026-08-20T17:38:59Z
- mergeCommit: 0576dfd747e0b52cbd9a0fceb87675185771035e (matches expected)
- main CI on merge commit: SUCCESS (run 32398779564, workflow: ci)
```

Final local verification (per the session-log four-part line convention):

- pytest: exit 0, full suite (PR-1 worktree: full pytest exit 0; PR-2 worktree: workerd 1602
  passed / SPA 1051 passed [13 new] / guards 4 passed — exact aggregate count not captured, CI
  authoritative for both merge commits)
- mypy: Success — 0 errors, no issues found in 512 source files
- ruff: clean
- main-branch CI on merge commit: SUCCESS (`795acd1` and `0576dfd`, both quoted above)

PR-1 worktree detail: typecheck clean across all 3 `tsconfig`s; workerd suite 1613 passed (12
new); SPA suite 1045 passed (8 new + `errorCopy`).

## Decisions made during session

- **Ratified via `grill-me` (7 decisions) before any code:** curation-snapshot model, not a
  per-item overlay and not auto-merge — matching the existing WPR-photos contract rather than
  inventing a second curation shape on the same page. Report-scoped only (never writes back to
  `material_receipt_events`/`material_shipments`/`submissions`) — curating the client document
  must never corrupt the field ledger. PDF layout unchanged — Material Problems stays a screen
  table feeding the Critical-Items seed, no new PDF table added for this pass. Grid resize gets a
  drag handle plus fixed presets with `localStorage` persistence, not free-form-only. Sticky +
  resizable ships on BOTH the manifest validate grid and the estimate disposition grid in the same
  PR, not staggered. Deploy to live ratified as part of the same session, not held for a separate
  go/no-go.
- **Curation applied at the top-level payload keys inside the ONE shared `buildReportData`,
  not duplicated per consumer.** This is what bought zero-Python-change consumption across the
  office screen, the Mac PDF's page-5 log, and the Critical-Items seed — the alternative (each
  consumer resolving curation itself) would have been a second N-implementation surface to keep
  in sync, the exact class of bug HOUSE_REFLEXES §1 warns about.
- **Office-added delivery rows stay deliberately UNMARKED (`kind ""`, em-dash chip) rather than
  masquerading as a ledger `Delivered` record.** An operator-typed row has no receipt event behind
  it; presenting it with a real `DELIVERY_KINDS` value would misrepresent its provenance on a
  client-facing document — a §4 data-fidelity call surfaced during adversarial review.
- **Stale-tab reset-to-auto accepted for contract consistency with photos**, even though it means
  an operator who leaves a curated tab open loses the curation on an absent-key save — matching
  the already-shipped and understood photos behavior rather than introducing a second edge-case
  rule for materials.
- **`DELIVERY_KINDS` now imports and exports `RECEIPT_KINDS`** (one runtime vocabulary) instead of
  keeping two independently-maintained kind lists — a review finding folded in before merge rather
  than deferred.
- **Grid resize state lives inside `GridViewport`, not the parent page**, so a drag frame never
  triggers a table-subtree reconcile — a perf decision proven, not assumed (see below).

## Prove-the-control-bites (3 injected, confirmed, reverted)

1. Injected a photos-style carry-forward read into the materials path → the exclusion test
   RED-lit → reverted. Confirms materials curation genuinely does NOT carry forward week to week,
   unlike the photos line at `shapeOffice` (a trap the design pass named explicitly).
2. Injected a bogus `className` into `GridViewport` markup → the CSS phantom-class guard RED-lit →
   reverted.
3. Injected a per-frame children remount into the resize path → the children-stability perf-
   contract test RED-lit → reverted. Confirms the drag handle does not cause the wrapped table to
   re-render on every pointermove.

## Deploy (dev Mac, freshly pulled to `0576dfd`)

- `wrangler d1 migrations apply its-safety-portal-db --remote` — applied migration 0078;
  `deliveries_json`/`incidents_json` columns verified present via `PRAGMA table_info`.
- `npm run deploy` — new Worker version `60184444-75f7-4d25-ace7-afe07908e9ff` on custom domain
  `safety.evergreenmirror.com`; new asset hash `index-DXKKc9aH.js` (was `index-DezTYEo2.js`).
- Live smoke against the served bundle: positive greps for `gridvp__scroll`, `"Curated for this
  report"`, `"Reset to imported"`; live CSS carries the sticky-header rule; an unauthenticated
  internal route returned 401 (fail-closed, as expected).
- One transient wrangler remote-D1 `7403` hit during the apply, cleared on retry — not investigated
  further, consistent with known Cloudflare API flakiness rather than a migration defect.
- The bearer-gated internal-route smoke returned 403 — traced to the dev Mac's Keychain holding a
  **stale pre-rotation copy** of `ITS_PORTAL_INTERNAL_TOKEN` (live bearers now live only on the
  California production Mac per the July host migration). Confirmed NOT a deploy defect; the 401
  fail-closed smoke above is the one that matters from this host.

## Open items handed off

- **Operator visual check of both screens on the live site** — the automated smokes confirm the
  bundle and CSS shipped; they don't confirm the office screen *looks* right at real widths. Needs
  a human pass.
- **Friday's Mac compile (`weekly_generate`) will be the first live exercise of the internal route
  with the real (California-host) bearer** — the dev-Mac 403 above means this session did not
  prove the bearer-gated path end-to-end; that proof lands with the next scheduled compile.

## What was NOT touched

- **PDF layout / rendering.** Material Problems remains a screen table feeding the Critical-Items
  seed; no new PDF table was added, per the ratified decision.
- **The photos three-state contract itself** — reused as-is, not modified, to avoid destabilizing
  the already-shipped and tested photos curation path.
- **The manual Tier-3 table** — deliberately left without `GridViewport` wiring; only the manifest
  validate and estimate disposition grids were in scope.
- **`material_receipt_events`, `material_shipments`, `submissions`** — curation never writes back
  to any of these; pinned by test per the report-scoped decision.

## Lessons captured to memory

- Registry-fan-out class recurred once more and was caught by the intended gate
  (`tests/test_error_copy_parity.py`) on the very first CI run of #185 — reinforces
  HOUSE_REFLEXES §1's "a datum has N implementations" reflex; no new memory entry needed, existing
  one already covers this shape.
- A newly-surfaced CSS-guard blind spot (template-literal-embedded classNames invisible to the
  existing regex-based phantom-class guard) was logged as a `docs/tech_debt.md` entry during #186,
  not captured to persistent memory this session — low enough volume to track as debt rather than
  a standing reflex.
