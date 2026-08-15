---
type: session_log
date: 2026-08-15
status: closed
related_prs: [148, 149]
workstream: null
tags: [session_log, safety_portal, po_materials, subcontracts, job_tracker, change_orders, migrations, adversarial_review]
---

# Session log — 2026-08-15 · Change orders as full generated documents (Track D2)

## Summary

One operator directive, delivered the morning after Track D shipped: "Change orders aren't
just a description. It is a whole view, contract and change of Configuration … change orders
should feed from the original contract or order and then have a fully new generated contract."
That superseded the day-old record model (#147's `procurement_change_orders` — description +
signed amount + approve/reject) before it ever held a row. Two PRs (#148 worker/SPA + migration
0077, #149 Mac render clauses), one D1 migration applied live, one production Worker deploy —
version `cf21dc5b`, replacing `ee1c63e5`.

The shipped model: a change order is a **normal lane document**. `POST /api/po/:id/change-order`
(and the subcontract twin) clones the sent — subcontracts also executed — parent's full
configuration plus line items/SOV into a fresh draft linked by new store-only columns
`change_order_of`/`co_seq`; the office edits what changed in the lane builder it is handed into,
and the document flows through the existing generate → Mac render → Box → review sheet →
F22-approved send pipeline like any contract. The parent **stays in force**: a CO never sets
`supersedes_*_id`, so every supersession flip site (both Mac status-syncs, both manual
`mark_submitted` flips) is structurally inert against it — pinned by tests on both lanes.

## Decisions worth keeping

- **The CO relationship is signed via the number, not the columns.** At generate a CO skips
  family-revision allocation entirely (`revision` stays NULL — it never consumes a slot in the
  `(job_no, site_phase, supersede_seq)` MAX) and mints `{parent_number}-CO{co_seq}`. Because the
  parent's number is embedded in the CO's *signed* `po_number`/`sc_number`, the Mac renderers
  derive the change-order clause from data the HMAC already covers, and `change_order_of`/`co_seq`
  stay out of the po:v1/sub:v1 canonicals (the `estimate_id` store-only precedent). The rejected
  alternative — adding the columns to both canonicals — would have required an atomic two-sided
  deploy with a drained queue; the recon flagged that as the universal-HMAC-failure hazard class,
  and the signed-number derivation gets the same integrity for free.
- **No CO-of-CO; a bad CO is corrected by the next CO against the base.** Superseding a CO is
  likewise refused (its clone would collide on `(change_order_of, co_seq)`, and re-issue via
  supersession is the wrong instrument). Base documents supersede; COs amend.
- **Totals on a CO chain display per document and are never summed.** Whether the office drafts
  a CO as a delta or a restated contract is their drafting choice; a computed "current value"
  would assert math the system cannot know (§4 no-invented-data).
- **The subcontract clause lives in the .docx layer, not the body template.** The body text is a
  strict token-fill of the sha-pinned, legal-review-gated template; the CO notice is inserted by
  `subcontract_docx.py` directly under the title, so attested body language is untouched and no
  `_vN`/legal-review cycle was needed.
- **0077 is a bidirectional migration.** It both adds columns the new Worker reads everywhere and
  DROPs the table the *previous* Worker's CO-record routes read — so apply + deploy share one
  maintenance window (the migration header and README punch-list row now say exactly that). This
  was the security review's single BLOCKER (W11): the code passed all eight named design
  invariants clean, but the order-dependency documentation was missing and the DROP raised it
  above the routine forgot-a-doc-line case.

## Process arc

Recon agent first (revision/numbering/HMAC/prefill machinery — it corrected the planned number
grammar from a fabricated `PO-…-001` form to the real five-segment D7 dotted number, and proved
the `-CO` suffix opaque-safe across every live surface, the only shape-aware code being
dead-but-tested `parse_*` functions with zero non-test callers). Then two parallel worktrees:
the worker/SPA build inline in `its-co`, the Mac render clauses delegated to an agent in
`its-co-mac` with its own fresh venv. Both bite-proofed a control before trusting it (worker: an
injected CO clone that sets `supersedes_po_id` reds the never-retires-the-parent test; Mac: an
injected clause typo reds the render test), both restored bytes via `cp`, never `git checkout`.
`portal-worker-security-reviewer` ran as DoD on the worker diff; the Mac diff is render-only over
verified records (no trust-boundary trigger) and was reviewed directly. One React defect was
caught by the existing App.router suite, not by review: the new App-level one-shot refs were
first placed *below* the signed-out early return, changing the hook count across auth
transitions — moved above the early returns with a comment saying why they must stay there.

## Verification

- pytest (touched suites, worktree venv): 91 passed; full suite red only in the known host-local
  `test_publish_daemon` class (29, green in CI); 0 failures outside that file
- mypy: clean over 504 source files · ruff: clean
- worker vitest (real workerd + D1, 0077 applied): 82 files / 1593 passed
- SPA vitest: 75 files / 1034 passed · typecheck ×3 clean · error-copy parity + phantom-CSS green
- Live post-deploy probe: both lanes carry `change_order_of`/`co_seq`, both partial unique
  indexes present, `procurement_change_orders` dropped, Worker version `cf21dc5b`
- main-branch CI: tip merge commit (`87e95e69`, #149) SUCCESS. #148's own merge-commit run was
  cancelled by the ci-main concurrency cascade (the known class) and its re-run was still in
  flight when this log was written — **four-part verify is stated clean for #149; #148 is legs
  1–3 clean + tip-green with the leg-4 re-run pending.**

## Left deliberately untouched

- A superseded parent's existing COs: not modelled (the create gate blocks *new* COs on a
  superseded parent; what an old CO means once its base is replaced is a commercial question for
  the planning layer, not a schema one).
- RFQs have no CO concept (rounds close; an accepted quote becomes a PO via estimate import).
- A CO cover-page treatment richer than the clause (AIA-style original-sum/net-change/new-sum
  block) — would need the office to assert delta semantics the system deliberately doesn't infer.
