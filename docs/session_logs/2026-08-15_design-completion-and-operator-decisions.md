---
type: session_log
date: 2026-08-15
status: closed
related_prs: [151]
workstream: null
tags: [session_log, safety_portal, po_materials, subcontracts, operator_dashboard, change_orders, adversarial_review, ci, workflows]
---

# Session log — 2026-08-15 · Design-completion pass, operator decisions Q1–Q5, dashboard wiring

## Summary

The same-day continuation of the Track D2 arc (see `2026-08-15_change-orders-as-documents.md`).
The operator asked for "a design completion/optimization pass of all of these added functions …
surface any questions or high-level findings to me." A 13-agent review workflow (six lenses ×
per-finding adversarial verification × a completeness critic) produced 70 findings — 69 confirmed,
none refuted — clustering into ~a dozen real issues and five genuine operator questions. One PR
(#151, four commits, **manually squash-merged by the operator during a GitHub Actions billing
outage**) landed every confirmed defect/gap, then the operator's answers to all five questions,
then the "fully wire the dashboard" directive. Worker `0da6a1d3` deployed; dashboard restarted
with its new panel live.

## The review's headline findings (all fixed in #151)

- The builder-open one-shot **replayed forever** (consumers' dedupe refs reset on remount; the
  App-held request was never cleared) and showed estimate-import copy for change orders. Fixed
  with consumed-nonce clearing **from an effect** (the reviewer caught the first fix mutating an
  ancestor ref during a child's render — a discarded-concurrent-render hazard) + an origin tag.
- CO generate could sign a **false "remains in force" clause** against a superseded parent —
  fixed with a pre-check AND an atomic correlated-subquery clause in the commit UPDATE's WHERE
  (the status-sync flip's own pattern; nothing downstream re-checks before render).
- CO drafts gained an **identity lock** (vendor/sub + job repoint refused; lines/scope/terms
  free — that *is* the change order).
- Silent-cap class (3×): the 50-row lane cap, the 200-photo offer cap — now flagged on the wire
  and hinted in the SPA. Plus: error-copy collision (`not_deletable` served two meanings),
  doomed always-409 buttons, unconfirmed irreversible marks, a WPR caption input that silently
  discarded keystrokes, UTC-vs-Pacific acceptance dates, and a docs/registry batch (CLAUDE.md
  lane rows, the sha-pinned `data_model_reference.md` re-recorded, runbooks, tech_debt).

## Operator decisions (ratified this session)

1. **Q1/A — supersession blocks on outstanding change orders** (`has_change_orders`, atomic
   NOT EXISTS twin). Further changes go out as the next CO; a canceled CO unblocks.
2. **Q2 — CO file names carry the CH marker** (`{Job}_PO_CH_{number}.pdf` + all five subcontract
   builders; titles unscoped by the directive). The CO grammar helper was promoted to each
   lane's `numbering` module at its third consumer.
3. **Q3 — every CO carries a signed scope declaration** (delta vs restatement) as the first line
   of `sow_text` / Exhibit A work text: seeded at clone, swapped by a builder radio, **enforced
   at generate** (`co_scope_missing`). Decision worth keeping: the declaration rides *already-
   signed text fields* — no store-only column (unsigned legal text rejected on posture), no
   canonical change (the deploy-skew dance rejected on risk). The reviewer correctly rejected a
   bare MAX_SOW bump as non-structural; the landed shape is fit-aware seeding (an at-cap parent
   clones unseeded, never over-bound, never truncated) + the generate-time requirement making a
   missing declaration a recoverable refusal. Exhibit A, not the 512-capped `scope_summary`,
   hosts the subcontract sentence for the same reason.
4. **Q4 — audited countersign undo** (`clear_accepted`: executed → sent). Convergence semantics
   accepted as-is: the ledger sync re-asserts `executed` if Smartsheet still shows it — a true
   correction edits the ledger too (runbook + confirm dialog both say so).
5. **Q5 — RFQ close stays bookkeeping-only** (ratified as-is).

"Fully wire the dashboard" resolved to the operator dashboard (the only surface named
"dashboard" in the system — verified by grep before building): a new read-only
`procurement_lanes` panel (PO_Log / Subcontract_Log / RFQ_Log status counts + CO census +
recent rows, per-lane fail-soft, the ledgers-can-understate caveat pointing at the portal job
screen) + six node briefs carrying the `-CO`-is-not-a-duplicate Tier-2 symptom. The panel's
lazy-import tripped the network-import gating tooth exactly as designed → allowlist enrollment.

## The CI billing incident

Mid-session, every GitHub Actions job began refusing to start ("recent account payments have
failed or your spending limit needs to be increased"): the repo had gone **private** Aug 13
(exposure remediation), putting the project's heaviest-ever CI period (~72 runs / ≈1,100–1,400
job-minutes in 3 days, double-triggered push+PR, plus ~10 full re-runs that existed only for the
four-part leg-4 record) onto the free-tier meter. The merge of #151 was held rather than pushed
through without CI (a red main run would also have made watchdog Check S noise); the operator
manually squash-merged, then **made the repo public again** to restore free minutes — a
conscious trade against the still-unpurged history exposure (see the standing memory/tech-debt).
Main's blocked run re-ran to SUCCESS post-flip; #151's four-part record is complete. Cost-control
options surfaced but not applied: trigger dedupe (+cancel-in-progress on PR branches),
a self-hosted runner on this Mac, retiring full-run leg-4 re-runs.

## Verification

- worker vitest 82 files / 1601 passed · SPA 75 files / 1037 passed · typecheck ×3 clean
- pytest 328 passed (naming/numbering/render/dashboard/system-map parity/gating/parity/CSS/
  docs-currency); `build_docs_pdfs --check` 22/22; ruff + mypy clean
- Bite-proofs: parent-in-force guard, CH token, dashboard panel registration — red on injection,
  green restored (cp-restore, never git checkout)
- `portal-worker-security-reviewer` ×2 (both WARN → every SHOULD-FIX landed pre-merge)
- #151 four-part verify: MERGED · mergedAt 2026-08-15T22:56Z · merge commit bc6e94f · main CI
  on bc6e94f SUCCESS (post-billing re-run)
- Post-deploy: Worker `0da6a1d3` live; dashboard + panel probes 200; all 22 launchd daemons
  exit-0; watchdog sweep no non-OK checks

## Left open

- The five operator questions' deferred siblings live in `docs/tech_debt.md` (ledger mirroring
  of portal marks, WPR upload trace, estimate→CO composition, archived-job writes, CO poll
  test, po_send filename divergence, CI cost controls).
