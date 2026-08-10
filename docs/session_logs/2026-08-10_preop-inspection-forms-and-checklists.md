---
type: session_log
date: 2026-08-10
status: closed
related_prs: [44, 47, 51]
workstream: safety_portal
tags: [session_log, safety_portal, safety_reports, equipment_preinspection, checklists, form_pdf, adversarial_review, house_reflexes, deploy]
---

# Session log — 2026-08-10 · Pre-op inspection forms, GAYK transport/maintenance checklists, and a training waiver

## Purpose

Operator supplied four source PDFs in `~/Downloads` and asked for (a) pre-op inspection forms for
the 360 excavator and GAYK/DOYLE piledriver pushed to the safety portal, and (b) startup + loading
checklists filed under the checklist function. Mid-session the operator additionally approved
building the GAYK weekly maintenance checklist and the GAYK/DOYLE Ram training waiver as a form.
Three PRs landed; both new migrations (0061, 0062) were applied to production D1 and the Worker
deployed twice — verified against the live served bundle and a live D1 query, not just the deploy
log.

## PRs landed

### #44 — `feat(safety-portal): pre-op inspection forms for the 360 excavator + GAYK/DOYLE piledriver, and the GAYK transport checklists`

Two new `equipment-preinspection` variants transcribed from the operator-supplied source PDFs
(`equipment-excavator-360-v1`, 16 items, scale `Okay`/`Defective`; `equipment-gayk-piledriver-v1`,
12 items + a free-text block), plus migration `0061_gayk_transport_checklists.sql` seeding two
`generic_inspection` library templates — "GAYK/DOYLE Piledriver Start-Up Check" (6 items) and
"…Loading & Securing Check" (2 items, one a `count` item with `target_count=4`). All four source
PDFs committed to `safety_portal/reference_forms/`. `form_pdf._OK_WORDS`/`_BAD_WORDS` gained
"OKAY"/"DEFECTIVE" — without them the excavator's own vocabulary falls through to neutral ink while
its skid-steer/telehandler siblings colour green/amber, a scannability regression on the one answer
a reviewer must not miss.

**Modeling decision — the week-grid problem.** The excavator source is a 7-day WEEK GRID (16 rows ×
Mon..Sun, each a Defective/Okay pair). The meta-schema forecloses a matrix: a checklist group is
`additionalProperties:false` and an item files exactly one `{response, comment}` pair in both
renderers. Modeled as ONE submission per operating day keyed to the `work_date` envelope, with the
week reconstituted by `weekly_generate` → `form_pdf.merge_pdfs` (Sat→Fri). Rejected alternatives are
recorded in the definition's `comment` array.

A second commit on this PR (`72252c7`, folded into #44) applied a **6-lens adversarial review** —
13 findings confirmed, 15 refuted:

- **Corrected an overclaim of the session's own making, found independently by 3 lenses.** The 0061
  migration header, a test name, and the PR text all said the `count` item meant a short-chained
  load "cannot be attested complete." False against live HEAD: `worker/fieldops_checklist.ts`
  carries the R1 acknowledged-shortfall path — below target the item stays open, but
  `acknowledge_below_target` plus a REQUIRED note completes it, audited as
  `checklist_item_complete_below_target`. Reworded everywhere to "cannot be closed SILENTLY";
  `target_count` is now also pinned by test so a NULL seed can't make the item unconditionally
  completable.
- **Dropped `week_commencing`** from the excavator form. The source's Monday-start week field
  conflicts with ITS's Saturday→Friday bucketing (`safety_week`/`weekly_generate`) — an
  operator-typed Monday date would sit on a signed page beside a packet whose week starts two days
  earlier, and the two can never agree. `work_date` already determines the week unambiguously.
- **A real dual-renderer bug, found and fixed.** `form_pdf`'s blank fillable defaulted a checklist
  item's comment box to `True`, while the SPA uses `it.comment ?? group.comment_per_item ?? false`.
  Because the Comments column is built when the group flag is set OR any single item opts in, the
  `True` default handed a hand-fill box to every OTHER item in a `comment_per_item: false` group.
  `equipment-gayk-piledriver-v1` is the first mixed-shape group (5 of 12 items opt in) and exposed
  it — the blank PDF offered 12 boxes where the screen shows 5. Fixed
  (`it.get("comment", bool(g.get("comment_per_item")))`); verified in the real AcroForm afterwards —
  exactly the 5 items whose source bullets carry a note line. No-op for every previously-shipped
  group, where the group flag was already `True`.

**PR #44 — four-part verify clean.** `state=MERGED` · `mergedAt=2026-08-10T21:45:00Z` ·
`mergeCommit=14cdd39` · main-branch CI on the merge commit: **SUCCESS**.

### #47 — `feat(safety-portal): GAYK weekly maintenance checklist + the GAYK/DOYLE Ram training waiver form`

Both operator-approved follow-ons from #44, each written up as deferred tech debt in that PR's
adversarial pass.

- Migration `0062_gayk_weekly_maintenance_checklist.sql` — a third `generic_inspection` library
  template, "GAYK/DOYLE Piledriver Weekly Maintenance Check" (9 items), from page 2 of GAYK doc
  240916. It is a library template and NOT an `equipment-preinspection` variant because the source
  states the duties are "in addition to the daily checklist" — folding it into the daily pre-op form
  would make operators attest weekly greasing every shift. The source's compound first bullet
  (greasing + track tension) was **split** into two separate items with separate pass criteria, so a
  skipped track-tension check can't ride in on a completed greasing round. Torque/clearance figures
  (75Nm/55 ft-lbs, 25mm/1") are pinned by test. **No recurrence row seeded**, deliberately —
  `checklist_recurrences` (migration 0040) requires a NOT NULL `assignee_personnel_id` AND `job_id`,
  live tenant data a migration cannot know; cadence is an operator assignment per job/person, and a
  test asserts the table stays empty.
- `equipment-training-waiver-gayk-ram-v1` under a new parent `equipment-training-waiver` — a signed
  acknowledgement (no pass/fail scale, no defect record), with a new `required-content.json` entry
  pinning the hold-harmless clause and the acknowledgement byte-exact. **A test corrected the
  design.** It shipped first as a `variant_label` to future-proof for a second manufacturer, and
  `test_form_catalog::test_single_form_parent_is_null_variant` correctly rejected it: `registry.ts`
  branches binary on `variant_label`, so a lone variant renders a degenerate one-option picklist the
  PM clicks through on every submission — a daily cost for a hypothetical machine. `variant_label` →
  `null`; the identity keeps `-gayk-ram` so a real second manufacturer becomes a new sibling plus a
  `variant_label` on both rows, never an identity rename (which would strand filed submissions).

**PR #47 — four-part verify clean.** `state=MERGED` · `mergedAt=2026-08-10T22:30:45Z` ·
`mergeCommit=322926a` · main-branch CI on the merge commit: **SUCCESS**.

### #51 — `docs(safety-portal): close out the form-surface drift this session created, and correct a Tier-2 instruction that no longer works`

Session-close cleanup for #44/#47 — three items of drift those PRs introduced, plus one pre-existing
item sitting directly on the runbook path for what they changed.

1. **Punch-list vs. database disagreement.** The `0062` punch-list row still read "pending" after
   the migration was applied to production D1 — flipped to ✅. A punch-list that disagrees with the
   database is worse than none; it's the one place an operator checks before deciding whether an
   apply is still owed.
2. **Stale pointer.** `equipment-gayk-piledriver-v1`'s maintainer comment said the weekly
   maintenance checklist was "queued as a follow-up (see docs/tech_debt.md)" — it was built the same
   day (#47) and the entry had moved to `tech_debt_closed.md`. Repointed to name migration 0062
   directly. Verified the edit doesn't change rendering: the `comment` array is maintainer-only, and
   the rendered submission is byte-identical to the pre-edit definition — load-bearing, because
   submissions filed today already resolve against this file.
3. **Self-invalidating count.** `reference_forms/README.md` opened "The 10 source PDFs" while
   holding 16 — wrong through three separate additions. Replaced with a pointer to the table rather
   than a new number that will rot the same way again.
4. **The one that matters — a Tier-2 instruction that silently does nothing.**
   `docs/runbooks/safety_portal_forms.md` told the Successor-Operator that the `ITS_Forms_Catalog`
   Smartsheet drives the portal dropdowns, and that retiring a form means setting that sheet's row
   to Inactive. `src/forms/registry.ts` says outright that catalog.json "REPLACES the never-built
   ITS_Forms_Catalog→D1→/api/forms sync" — so the documented retire step changes NOTHING, and an
   operator following it would believe a form was withdrawn while it stayed live and fillable in the
   field. Corrected to the real mechanism, verified against live code: dropdowns read catalog.json;
   retiring is the Retire button on the portal Forms page, which enqueues a publish request the
   daemon actuates (manifest flip → PR → merge → deploy) and which deletes nothing, so filed
   submissions still render. A dated callout names the old instruction explicitly rather than
   quietly swapping the text, because anyone trained on it has a habit to unlearn. Pre-existing
   drift, not introduced by #44/#47 — but it's the doc a successor reads right after a form change,
   and this session added three forms to that surface.

**Deliberately not done:** the open "a `count` item's recorded NUMBER never reaches the filed
progress record" tech-debt entry stays parked. The completion emit is dark — the live deploy reports
`CHECKLIST_PROGRESS_LOGGING_ENABLED` "false" — so there is no live consumer and no real data, which
is exactly the don't-harden-dormant-subsystems gate (HOUSE_REFLEXES §6).

**PR #51 — four-part verify clean.** `state=MERGED` · `mergedAt=2026-08-10T23:13:35Z` ·
`mergeCommit=e43173f` · main-branch CI on the merge commit: **SUCCESS**.

## Decisions

1. **Model the excavator week-grid as one submission per operating day, not a matrix.** The
   meta-schema's `additionalProperties:false` group shape and the one-{response,comment}-pair-per-item
   contract in both renderers structurally cannot represent a 7-column grid. Alternative rejected:
   inventing a matrix item type — would diverge both renderers from every other form in the catalog
   for one source document. Week is reconstituted downstream by `weekly_generate` →
   `form_pdf.merge_pdfs` (Sat→Fri), the existing mechanism.
2. **Drop the source's `week_commencing` field rather than transcribe it.** Monday-start (source) vs.
   Saturday-start (ITS `safety_week` bucketing) cannot agree by construction; an operator-typed value
   would contradict the packet it's filed beside. `work_date` already disambiguates the week.
3. **`_OK_WORDS`/`_BAD_WORDS` gain "OKAY"/"DEFECTIVE," additive only.** No shipped scale uses either
   word and `_is_confirm_scale` still returns `False` for `["Okay","Defective"]`, so this is a
   pure scannability fix with no layout change to any existing form.
4. **Split the weekly-maintenance source's compound first bullet into two items** (grease; track
   tension). One checkbox over two independent pass criteria lets a skipped track-tension check ride
   in on a completed greasing round — the same failure shape a `count`-vs-checkbox choice guards
   against elsewhere in this session's work.
5. **No recurrence row seeded for the 0062 weekly-maintenance template.** `checklist_recurrences`
   (migration 0040) requires NOT NULL `assignee_personnel_id` and `job_id` — live tenant data a
   migration cannot know. Left as an explicit operator action (see Open items).
6. **`equipment-training-waiver-gayk-ram-v1` ships with `variant_label=null`, not a variant.**
   `test_form_catalog::test_single_form_parent_is_null_variant` caught the future-proofing attempt:
   `registry.ts` branches binary on `variant_label`, so a lone variant renders a degenerate
   one-option picklist on every submission — a real daily cost paid for a hypothetical second
   manufacturer. Identity (`-gayk-ram`) is preserved so a genuine second manufacturer is a new
   sibling row plus a `variant_label`, never an identity rename (which would strand filed
   submissions).
7. **Correct the runbook's Tier-2 retirement instruction rather than leave it as pre-existing drift.**
   Per Op Stds v21 §43 (successor-remediation, document-as-you-build) and §44 (the both-rule — a
   Tier-2 repair must be both documented AND low-capability-class to be operator-safe), an
   instruction that is *documented* but *does nothing* is worse than an undocumented gap: it gives
   the Successor-Operator false confidence that a form was withdrawn while it stays live and
   fillable. Corrected in the same PR that added the forms making the drift newly relevant, per
   HOUSE_REFLEXES §1 (a current-state claim is a hypothesis until verified against live HEAD —
   `registry.ts` was read, not assumed).

## Production deploys

Two apply+deploy cycles, both verified against the live artifact rather than the deploy log:

- **0061** applied to remote `its-safety-portal-db`; Worker deployed (version `cc768fbd`).
- **0062** applied; Worker deployed (version `5814a610`).
- **Live verification:** a production D1 query returns all three GAYK templates (6/2/9 items,
  active); the JS bundle actually served from `https://safety.evergreenmirror.com` contains all
  three form codes plus real item text ("Rated Capacity & Plate Readable", "Kajo hammer paste", the
  hold-harmless clause) — confirming the served asset, not just a successful `wrangler deploy` exit
  code.

## Verification

- pytest: 4797 passed / 2 skipped / 58 deselected
- mypy: clean — no issues found in 482 source files
- ruff: clean
- portal worker vitest: 1250 passed (71 files); SPA vitest: 806 passed (59 files); typecheck clean
  (3 tsc projects)
- main-branch CI on merge commit: SUCCESS (all three PRs)

`tests/test_publish_daemon.py` fails locally on this host only — a conftest live-state write guard
against `Path.home()/"its"/"state"`. Verified identical on pristine `main` with this session's
changes stashed (pre-existing, not introduced here); green in CI. A concurrent session logged the
same finding as an auto-memory candidate during this session — see Cross-references.

## Open items / next session

- **OPEN tech debt (deliberately parked):** a `count` checklist item's recorded NUMBER never reaches
  the filed progress record. Pre-existing (0028's two count items have the same gap); the completion
  emit is dark (`CHECKLIST_PROGRESS_LOGGING_ENABLED` reads `false` on the live deploy), so
  HOUSE_REFLEXES §6 ("don't harden dormant subsystems") applies — no live consumer, no real data.
- **Operator action required:** the weekly maintenance checklist (0062) has no recurrence attached.
  Cadence must be set per job/person in the Inspections UI when a machine goes on a job —
  `checklist_recurrences` could not be seeded by migration (needs live `assignee_personnel_id` +
  `job_id`).
- **Worktree cleanup:** `~/its-preop-forms` and its four merged branches need operator cleanup in a
  normal shell — `git worktree remove` / `git branch -D` are hook-blocked inside Claude Code.

## Cross-references

- `docs/HOUSE_REFLEXES.md` §1 ("trust the live code, never the claim" — the runbook correction in
  #51 was verified against live `registry.ts`, not assumed from the prior instruction's wording),
  §2 (prove-the-control-bites — both new controls in #44 were confirmed to RED-light on a synthetic
  violation: removing "DEFECTIVE" from `_BAD_WORDS` fails the scale-word test; weakening
  `target_count` 4→3 fails the Loading & Securing contract; restoring the `True` comment-box default
  fails the dual-renderer test), §6 (don't-harden-dormant-subsystems — the parked `count`-value
  tech-debt item).
- `docs/tech_debt.md` / `docs/tech_debt_closed.md` — the weekly-maintenance-checklist deferral
  (opened in #44, closed in #47) and the parked `count`-value-not-logged entry (still open).
- `docs/runbooks/safety_portal_forms.md` — corrected retirement mechanism (#51).
- Op Stds v21 §43 (successor-remediation documentation) and §44 (Tier-2 both-rule) — the rationale
  for correcting the runbook's retirement instruction rather than leaving it as drift (Decision 7
  above).
- `docs/session_logs/2026-08-10_outage-diagnosis-alerting-gap-and-gate-activation.md` — a separate
  same-day session; no overlap in files or branches.
- Auto-memory: a concurrent session's finding on `tests/test_publish_daemon.py`'s local-only-host
  failure (conftest live-state write guard) — not authored by this session, noted here for
  continuity per the Verification section above.
