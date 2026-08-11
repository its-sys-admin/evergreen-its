---
type: session_log
date: 2026-08-11
status: closed
related_prs: [59, 60, 61, 62, 63, 66, 71, 72, 74]
workstream: field_ops
tags: [session_log, materials, manifest, live_fire, portal_security_review, review_queue, operator_decisions, daily_report]
---

# Session log — 2026-08-10 → 2026-08-11 · Manifest finish, the first real documents, and four same-day operator decisions

**Continues from:** `docs/session_logs/2026-08-10_overnight-materials-standup-and-reconcile.md` (PR #62
and earlier) — that log covers the overnight autonomous half (stand-up, the errorCode-1135 fix,
the receipts mirror's first live fire). This log picks up from PR #62's merge and covers the
day-half: the manifest-lane completion PR, the FIRST real vendor documents through the lane on
the production tenant, four operator decisions taken and implemented the same day, and the live
validation that followed.

## Summary

Nine PRs landed, all four-part verify clean (seven directly, two transitively). The centerpiece
is #66 — the manifest importer's merge/dispose/security pass that closes all nine defects (A1–A5,
B6–B9) the 2026-08-10 audit had filed, plus an adversarial-review blocker found in the PR's own
diff and fixed before merge. Then the operator ran the **first real vendor documents** through
the lane on the production tenant: a Bradley BOM refused on a PDF `OpenAction` artifact, which
exposed a second occurrence of the field_ops Review-Queue workstream-vocabulary gap (the same
latent class as the progress_reports P4→P5 case) — the alert chain held (CRITICAL fired exactly
as designed) even though the ticket itself was lost, and the fix landed folded into #66 before
that PR's own merge. Four operator decisions followed same-day from watching the lane work
against real documents (#71, #72, #74, plus B7's explicit resolve inside #66) and were all
implemented same-day. Two production BOMs were imported and committed for real (165 active lines,
zero data-fidelity defects at scale); the receipts and material-list mirrors, the incident
flag/resolve cycle, and the new daily-report deep-link card all validated live.

## PRs landed

| PR | What | Merge SHA | Verify |
|---|---|---|---|
| #59 | `ensure_columns` errorCode-1135 fix + row-cap seeder row (overnight; carried into this log's chain) | `66ce500` | SUCCESS, direct |
| #60 | Dashboard wiring: receipts gate, manifest edges, tree nodes | `75a6c99` | own-commit CI **cancelled**; landed transitively — see below |
| #61 | Stand-up now runs the manifest seeder (E18) | `1742db2` | own-commit CI **cancelled**; landed transitively — see below |
| #62 | Docs unification pass — stale claims reconciled, morning checklist | `362c4e4` | SUCCESS, direct |
| #63 | 15s timeouts on two documented row-seeding test flakes — one had just taken out main CI on #60's commit | `5993357` | SUCCESS, direct |
| #66 | Manifest lane finished: real merge, server-enforced ambiguity, shipping-log dispose, nine audit defects closed, first-real-document vocabulary fix folded in | `add93dd` | SUCCESS, direct |
| #71 | Operator decision: `suspicious` → proceed-with-warning for the manifest lane (malicious still refuses) | `b525346` | SUCCESS, direct |
| #72 | Operator decision: Remove on the manifest list for refused/stale uploads | `a546428` | SUCCESS, direct |
| #74 | Operator decision: daily-report expected-materials becomes a deep-link card | `f2e237c` | SUCCESS, direct |

**9 of 9 PRs four-part verify clean (7 directly, 2 transitively: #60 and #61 via ancestor commit
`362c4e4` / PR #62's successful run). No PR in this batch is genuinely not landed.**

Per-PR: #59 SUCCESS direct (66ce500); #60 cancelled→transitive via 362c4e4; #61
cancelled→transitive via 362c4e4; #62 SUCCESS direct (362c4e4); #63 SUCCESS direct (5993357);
#66 SUCCESS direct (add93dd); #71 SUCCESS direct (b525346); #72 SUCCESS direct (a546428); #74
SUCCESS direct (f2e237c). Independently re-verified against live GitHub for this log
(`gh run list --branch main --commit <sha>`), not taken on report — #60's and #61's own merge
commits confirmed `conclusion=cancelled` (#63's timeout fix, landed on top, is what let #62's run
go green and carry both forward).

### #60 / #61 — why "cancelled" is not "not landed"

`docs/tech_debt.md`'s #58 entry had already predicted the flaky-test class: `fieldops-manifests
.test.ts`'s "refuses a commit past the line cap" seeds 450 rows one INSERT at a time (~6.5s under
load, against a 5000ms default timeout) — the documented sibling of the 200-row
`fieldops-daily-photo.test.ts` seed. Main-branch CI on #60's merge commit (`75a6c99`) hit exactly
this and failed; #61's commit inherited the same red main and its own run was cancelled by the
next push. #63 applied the entry's pre-scoped fix (per-test `{ timeout: 15_000 }` on the two named
suites, no logic change) directly on top, and main-branch CI on #62's merge commit (`362c4e4`) —
which contains #60 and #61 as ancestors — ran green. Per `docs/operations/pr_merge_discipline.md`,
a PR whose own merge-commit CI failed and was fixed forward by a later commit is landed once a
descendant commit's CI passes with the original PR's changes intact; both #60 and #61 satisfy
that here.

## The manifest lane finish (#66)

Closed all nine defects the 2026-08-10 audit had filed against the manifest-import correctness
cluster:

- **A1** — quantity now rides the Columns-table concepts state (the dead `qtyCol` path deleted);
  a regression pin proves a remap reaches the wire.
- **A2 (merge is real)** — a unique match is a guarded in-place `UPDATE` (`COALESCE`, the
  document's non-null fields win); a locked (received/flagged) match reports `skipped_locked` and
  is never rewritten.
- **A3** — mode-aware plan totals; discard now works mid-`committing`.
- **A4** — ambiguity resolution is enforced **server-side**: resolutions are re-validated against
  the server's own match set, and a forged line id 409s before any write.
- **A5** — `manifest_poll` clamps to the Worker's bounds with visible parse notes; a Worker 4xx
  is classified PERMANENT (one-shot flag + ticket, `manifest_worker_rejected`) via
  `PortalTransportError.status_code`.
- **B6** — the quantity projection now runs on flagged lines too (quantities track the ledger,
  status stays sticky; flagging no longer clobbers `qty_received`). The drift-encoding test was
  deliberately rewritten and **RED-verified against the old guard — 3 failures** before the fix.
- **B7** — `POST /:id/resolve-incident` (role-gated, note required, ledger-derived status) +
  Materials-page UI — the explicit resolve action, operator-requested same-day.
- **B8** — shipping-log rows dispose into `material_shipments` loads: deterministic
  `shipment_uuid` (`mf<id>-r<row>`), `source='import'`; an unmatched part creates its line and its
  load atomically. Chosen over fencing the lane off, per operator direction.
- **B9** — line delete now cascades its loads, audited.

**The batch order is itself the security fix.** Adversarial review
(`portal-worker-security-reviewer`) found a **blocker the diff itself had opened**: making
`committing` manifests discardable (the A3 change) created a discard-vs-in-flight-commit race
where a row write could land on a just-discarded manifest and still report `ok:true`. Fixed before
merge: statement 0 of the commit batch is the state transition; every subsequent row write is
guarded `EXISTS(manifest committing at THIS watermark)`; statement 0's `meta.changes` is checked
(a lost race now returns an honest 409); response counts derive from per-statement
`meta.changes`; every audit row is `IfChanged` (no lying audit rows). A cascade-audit `>=1` guard
and a unified `MAX_BOL` (64) were folded in from the same review pass; cross-job resolution
forging, `COALESCE`-clearing, and sequence collisions were attacked and came back clean.

## The first real documents — and what they exposed

The operator ran the **first real vendor documents** through the lane on the production tenant:
Bradley 1 Customer BOM (job Deep Lake) refused at §34 Layer 2 —
`L2:pdf_active_content:OpenAction` — a correct refusal (an auto-run-on-open action, an artifact
most vendor-exported PDFs carry).

That refusal exposed a second occurrence of a known latent class: `manifest_poll` tickets under
`workstream="field_ops"`, which was in **neither** `review_queue.VALID_WORKSTREAMS` **nor**
`picklist_validation._WORKSTREAM_VALUES_GLOBAL` — the exact gap both files already documented for
`progress_reports` at the P4→P5 transition. Every mocked test had passed; the first live refusal
raised `ValueError` inside `review_queue.add()`. **The alert chain held even though the ticket
itself was lost:** `safe_add` caught the loss and escalated `CRITICAL
review_queue_ticket_failed`, exactly as issue #41 designed it to. The vocabulary, not the
alerting, was the gap.

Fix (folded into #66's branch before its own squash-merge, commit `296c21e` on
`feat/manifest-finish`): both `VALID_WORKSTREAMS` and `_WORKSTREAM_VALUES_GLOBAL` gained
`field_ops`; the live tenant's Review-Queue Workstream picklist got the matching option; and a
new **structural parity test** enumerates every ticketing daemon's `WORKSTREAM` constant against
`VALID_WORKSTREAMS` (and pins the two sets against each other) — so the next workstream to join
fails in CI by name, not on its first live refusal.

The same real-document session produced one operator-requested UI fix, also folded into #66: a
line the importer will refuse (no description, no catalog pick) used to block the whole import
behind a bare count. The warning now names each failing row and offers ✕ Clear per row + ✕ Clear
all, through one single-pass state update (a remove-one-at-a-time loop would have hit shifted
indexes on hand-added rows).

## Operator decisions (2026-08-11, all implemented same-day)

1. **§34 `suspicious` → proceed-with-warning, manifest lane only (#71).** The Bradley BOM's
   `OpenAction` refusal is an artifact "virtually every vendor-exported PDF carries" — real
   documents were going to keep tripping it. Implemented: `suspicious` now proceeds with a WARN
   (`manifest_active_content`) + a parse note on the validate screen; **`malicious` still refuses,
   unchanged, always.** Scoped to the manifest lane specifically — the PO-attachment and
   vendor-estimate lanes keep the refuse posture, because those are office-uploaded procurement
   documents whose bytes ITS only opens inside the killable sandbox, a different trust profile
   from a vendor-supplied BOM. The refused-suspicious test was deliberately rewritten (now asserts
   imports + files + warns, no ticket) rather than deleted. **A §34 disposition rider is owed
   doctrine-side — Seth-owned, recorded, not actioned here.** Dedupe excludes refused rows, so the
   same Bradley file re-uploads clean with no re-export once this lands.
2. **B7 explicit resolve** — folded into #66 (above); listed here because it was an operator
   decision taken alongside #71/#72/#74, not a pre-planned audit item.
3. **B8 full shipping-log dispose** — also folded into #66; the operator chose the full dispose
   path (shipping-log rows create `material_shipments` loads) over fencing shipping-log import off
   for a later slice.
4. **Manifest-list Remove (#72).** The "Import from a document" list had no way to clear a
   refused or stale upload. Two-step Remove (ConfirmDelete pattern) discards a non-imported
   manifest; an **imported** manifest keeps its row (provenance — the Worker refuses to discard
   it) and reads "Imported". The list route now excludes `discarded` rows; retained in D1,
   dedupe already ignored them, so the same file can be re-uploaded with no re-export.
5. **Daily-report expected-materials → deep-link card (#74).** The daily form no longer renders
   every expected-material line. Operator framing: the daily form should show the day's *shape*,
   not repeat content the Materials page already owns. New section renders counts only ("N
   lines · M still expected · K flagged") + a "Materials tracking →" deep link; no per-line
   content, no per-line actions. This is the section's **third contract in four days** — v5/v6 had
   none, v7 introduced a full snapshot table, this PR replaces the snapshot with the card. The
   one-tap Confirm-receipt action is gone, which closes issue #58's asymmetry debt cleanly (all
   marking now goes through the Materials page's two-tap three-way mark). "Report a problem" moved
   to the Materials page mark row — it had lived only on the daily form, so cutting the list would
   have silently deleted the capability; the prefilled incident-form deep-link did not survive the
   move (incidents filed from the forms menu are valid, unlinked, per the documented Worker path).
   No values are seeded for new filings (renders the classic absent-key note line); already-filed
   v7 snapshots are untouched and still render as tables — `form_pdf` itself was not touched, and
   both PDF pins stayed green.

Also landed in this arc, operator-requested, not tied to a numbered defect: the manifest-import
job heading now resolves a real job **name** via a `project_name` projection in the
expected-materials read (was showing only the job key).

## Live validation (production tenant)

- **Two real BOMs imported and committed**, 81 + 91 rows → **165 active lines, zero null
  `qty` / zero null `part_number`** — A1's Columns-table remap holding at real scale, not just
  in the regression pin.
- **Test — Material List** mirrored 170 rows in a single pass.
- **Test — Material Receipts** recorded 4 events with correct derived-column rollup
  (partial→delivered resolves to received).
- **Flag → resolve cycle**, 12 seconds apart, both visible in `audit_log`; the sheets correctly
  show *nothing* changed between the two events — a cycle-sampled projection working as designed,
  not a missed write.
- **Daily report filed through the new deep-link card** — confirmed rendering the counts-only
  section, not the old snapshot table.
- **Remove exercised live** — manifest #2 discarded and dropped from the list.
- **Two Worker deploys** this arc; final live version `1cceb431`, bundle markers verified
  decompressed (not stale-cached — the "deploy nothing changed" false alarm class from
  HOUSE_REFLEXES §7 was checked for and ruled out).

## A harness event worth recording

Mid-session, the auto-mode classifier locked onto what it read as a security-weakening arc — it
blocked, in sequence, the disposition edit, the test run that would have proven it, and then an
unrelated docs edit. Resolved operator-side with an exact-anchor apply script plus a permission-
mode switch, not by arguing the classifier down. Recorded here because it cost real session time
and because the trigger (§34 suspicious→warn, a genuine loosening of a security control, done on
explicit operator direction and scoped to one lane) is exactly the shape future sessions doing
similar operator-directed loosening should expect to hit.

Separately, testing surfaced that a same-file re-upload is blocked while a prior import of that
file is still `committed` — the per-job sha dedupe correctly covers live statuses, not just
`discarded`/refused ones. This is correct behavior, not a bug, but it means **the merge-mode live
fire is the one remaining untested manifest path**: every live document this session imported hit
`add_new`, never the merge disposition. Planned next: import Brimfield 2 against the existing Test
job to exercise a real merge live.

## Decisions

1. **The vocabulary-gap fix rode inside #66 rather than shipping as a standalone hotfix PR.** It
   was discovered and fixed on the `feat/manifest-finish` branch before that PR's own squash-merge
   (commit `296c21e`, merged into the branch ~14 minutes before #66 landed) — landing it separately
   would have meant either reverting live-refusal tickets for a lane already flagged CRITICAL, or
   racing a hotfix against an in-flight feature branch touching the same files. Folding it in kept
   the fix and the feature that exposed it in one reviewed, four-part-verified unit.
2. **§34 suspicious→warn was scoped to the manifest lane only, not applied globally (#71).** The
   PO-attachment and vendor-estimate lanes keep the stricter refuse posture. Rationale: those lanes
   process office-uploaded procurement documents inside the killable sandbox — a materially
   different trust boundary from a vendor-supplied BOM arriving through the manifest lane. Loosening
   globally would have widened the exception past what the operator actually asked for.
3. **B8 (shipping-log dispose) was built as the real integration, not fenced off for a later
   slice.** The operator chose the full path specifically to unblock live shipping-log documents
   the same day real BOMs were being tried — deferring it would have left a second real-document
   category refusing on day one for no live reason.
4. **The daily-report expected-materials section's third contract (#74) replaces rather than
   extends the v7 snapshot.** Extending v7 (e.g., truncating the line list) was considered and
   rejected — a partial line list on a form is worse than no line list, because it implies
   completeness it doesn't have. The counts+deep-link card makes no claim about individual lines
   at all, which is honest about what a form snapshot can show days after filing.

## Open items / next session

- **§34 disposition rider is owed doctrine-side** (Seth-owned) — the manifest lane's
  suspicious→proceed-with-warning carve-out needs to be reflected in canonical doctrine, not just
  in code + this log.
- **Merge-mode live fire is untested.** Every real document imported this session hit `add_new`;
  planned: import Brimfield 2 against the existing Test job to exercise A2's merge path for real.
- **Shipping-log import live fire is untested** — B8 is built and unit/integration-tested but has
  not yet ingested a real shipping-log document.
- **Bound-photo / migration-0063 cross-job 422 smoke** — carried over from the overnight log, still
  deferred.
- **Old v5/v6 filed PDF eyeball** — carried over from the overnight log's morning checklist, still
  deferred.
- **Manifest #1 (Bradley 1 Customer BOM, Deep Lake) remains refused-visible** on the production
  tenant — the operator can Remove it (#72) but has not yet done so; left as a live example of the
  suspicious-warn path if #71 needs a real specimen to check against.
- **`logs/migrations/po_vendors_backup_20260810.json` is still untracked** as of this session's
  close (carried over from the prior session log's open item; unchanged here, still needs a Seth
  call on commit-vs-gitignore).

## What was NOT touched

- The §34 doctrine rider itself — code shipped, doctrine text not drafted (Decision/Open-items
  above).
- The merge-mode and shipping-log-import live fires — deferred, not skipped silently.
- `logs/migrations/po_vendors_backup_20260810.json` — flagged again, not resolved.
- Manifest #1's Remove — left in place deliberately as a live refused-suspicious specimen.

## Cross-references

- `docs/session_logs/2026-08-10_overnight-materials-standup-and-reconcile.md` — the immediate
  predecessor session (through PR #62); this log continues directly from its close.
- `docs/tech_debt.md` — the 2026-08-10 audit entry this session's #66 closes (A1–A5, B6–B9); the
  #58 entry #63 fixed forward (the two row-seeding test timeouts); issue #41 (Review-Queue
  safe-ticketing design) named directly by the field_ops vocabulary-gap finding.
- `docs/runbooks/material_manifest_import.md` — Symptom 3 updated by #71 (the suspicious-warn
  carve-out); the manifest lane's §43 runbook.
- `shared/review_queue.py` / `shared/picklist_validation.py` — `field_ops` joined
  `VALID_WORKSTREAMS` / `_WORKSTREAM_VALUES_GLOBAL` 2026-08-11, same latent class and same-day
  precedent as `progress_reports` at P4→P5 (see the inline comment at
  `shared/review_queue.py:119`).
- `docs/HOUSE_REFLEXES.md` §2 (adversarial review found the discard-race blocker #66's own diff
  opened, before merge — the control bit exactly as intended) and §1 (the field_ops vocabulary gap
  is the second live occurrence of "mocks structurally cannot see this class").
- `docs/operations/pr_merge_discipline.md` — the four-part verify applied to all nine PRs; the
  #60/#61 cancelled→transitive reasoning above follows its "fixed forward by a descendant commit"
  resolution path, not a revert.

## Verification (final state, PR #74)

```
- pytest: 4867 passed / 2 skipped (test_publish_daemon deselected — host-local conftest
  live-state guard, diagnosed in the overnight predecessor log)
- mypy: 0 errors (touched files)
- ruff: clean
- main-branch CI on merge commit: SUCCESS (9/9 four-part clean, 2 transitive per above)
```
