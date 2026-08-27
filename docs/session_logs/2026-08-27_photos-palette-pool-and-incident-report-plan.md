---
type: session_log
date: 2026-08-27
status: closed
related_prs: [193, 194]
workstream: safety_portal
tags: [photos, form-builder, incident-report, pdf-rendering, grill-me, migration-0079, phase-b-checklist]
---

# Session log — 2026-08-27 · The incident report never had photos: a photos palette, a composable pool, and PDF grouping in the form builder

Opened on an operator report that the incident report "dropped" its photo function — a
2026-08-26 incident report had to have its photos emailed in — plus three related asks: why only
4 photos are accepted, wanting more photo buttons placeable anywhere on a form, and wanting the
"standalone add photo" item back in the form builder. A `grill-me` alignment session with three
parallel exploration agents ran before any code changed, and overturned the premise: the incident
report never had photos (it published 2026-06-09, three days before the `photo` input type
shipped 2026-06-12), and the form builder never had a standalone add-photo item. Eleven decisions
were ratified, two PRs landed and merged, and a production-Mac Phase B checklist (deploy, migration
0079, and the actual form republish) is handed off unrun — this session lands infrastructure only,
per the sandbox-first / dev-host-lands-PRs-only pattern.

## Commits landed

| PR | Merge commit | Purpose |
|---|---|---|
| #193 | `b165b23` | "+ Photos" palette macro in the form builder (inserts a header + photo field — deliberately NOT a new section type, zero renderer/validator ripple); Max-photos (1..4) editor control; `additional_photos` pool section made builder-composable (one-mount + fixed-key client mirrors of `publishValidation`); `FormRenderer` renders an honest placeholder instead of silent-nothing without an adapter; `FormFillPage` constructs the pool adapter generically (manager/admin; strips stale refs on job/date change, StrictMode-safe last-scope ref guard); `worker/fieldops_daily_photos.ts` — `requireJobScope` removed from the pool routes per decision 10, rows remain uploader-self-scoped, `DAILY_PHOTO_ROLES` unchanged |
| #194 | `5fcbde1` | Submission PDF groups photos per field label (`PhotoGroup` threading; field label had been discarded at intake previously; Box `01..NN` numbering + WPR registrations byte-identical); blank fillable PDF renders a bordered "Photos are attached through the Safety Portal…" note instead of a fake AcroForm text box (photo/signature are widgetless by design — `test_form_archive` + `test_render_smoke` now use a widget-aware predicate); render smoke now synthesizes photos so every C12 publish CI run proves photo-heading rendering; runbooks (`safety_photo_path.md`, `safety_portal_forms.md`) generalized; `docs/tech_debt.md` entry for the deferred WPR caption-prefix |

Both PRs were built by workflow agents in dedicated worktrees (`its-photos-spa` and
`its-photos-py`), each with its own fresh venv/npm environment, then adversarially reviewed
and fixed before merge.

## CI runs — four-part verify (quoted verbatim from `pr-landed-verifier`)

```
PR #193 — four-part verify clean
- state: MERGED
- mergedAt: 2026-08-27T14:09:57Z
- mergeCommit: b165b23bcca6a2db7be65e0a8d116f2079937b99
- main CI on merge commit: SUCCESS (run 33080725588, workflow: ci)
  - secrets: success (11s)
  - test: success (9m32s)
  - portal: success (4m37s)
  - https://github.com/its-sys-admin/evergreen-its/actions/runs/33080725588
```

```
PR #194 — four-part verify clean
- state: MERGED
- mergedAt: 2026-08-27T14:21:59Z
- mergeCommit: 5fcbde1ac043831d88ffda097fff75e20faa1ced
- main CI on merge commit: SUCCESS (run 33081835211, workflow: ci, event: push)
  - job test: success
  - job portal: success
  - job secrets: success
  - run URL: https://github.com/its-sys-admin/evergreen-its/actions/runs/33081835211
```

PR #193 build-time gates (worktree `its-photos-spa`): typecheck clean; vitest worker 82
files/1624 tests; SPA 79 files/1099 tests; CSS-guard 2 passed; zero new classNames.

Final local verification on PR #194's tree (per the session-log four-part line convention,
full-tree run — the Python-touching PR of the two):

- pytest: 5783 passed / 4 skipped / 58 deselected
- mypy: 0 errors / 512 source files
- ruff: clean
- main-branch CI on merge commit: SUCCESS

## Pre-flight findings (grill-me, before any code changed)

The three parallel exploration agents established, without inference from the operator's framing:

1. **The incident report never had photos.** `incident-report-v1`/`v2`/`v3` all published
   2026-06-09 via the C12 auto-publish daemon (PRs #254/#255/#257, E2E validation publishes); the
   `photo` input type itself shipped 2026-06-12 (PR #271) — three days later. Nothing regressed;
   the capability postdates the form. The 2026-08-26 emailed-photos incident was the form working
   exactly as published, not a dropped feature.
2. **The form builder never had a standalone add-photo item.** `photo` has always been a
   per-field Input-dropdown value, not a palette macro. The operator's "folded under checklist"
   memory maps to PR #475 (2026-07-05), which restyled the SITE CHECKLISTS editor to be visually
   identical to the form builder and added a per-item "Requires photo" checkbox there — a
   different lane, untouched by this session.
3. **The 4-photo limit is a payload budget, not a UX ceiling.** Photos ride base64 in the
   submission's D1 row (~2MB practical) → `PAYLOAD_MAX` 1.8MB → 4 × ~280KB client-encoded ≈
   1.49MB b64; 8/submission total. Multiple photo fields already worked end-to-end
   (test-proven 2×4=8). The pool (`additional_photos`, Option-D) is the designed unlimited path
   but rendered only on the Daily tab and was read-only in the builder before this session.
4. **Pre-flight surfaced a stale-migration hazard unrelated to photos.** Migration 0079 (from the
   2026-08-25 materials PRs #188–#190) is merged but **unapplied on remote D1**. The Worker itself
   WAS deployed on 08-25 (`wrangler deployments list`: three deploys, latest 23:14Z — checked after
   an initial "never deployed" misread of this signal), so the live state is #188 code running
   WITHOUT its 0079 capability grant: the manager materials-manage feature is dark/fail-closed, not
   broken. Phase B below must apply 0079 before its own deploy (the publish daemon's gate would
   refuse anyway).

## Adversarial review

- **PR #193 — `portal-worker-security-reviewer`:** no must-fix / should-fix findings. The
  decision-10 containment (removing `requireJobScope` on the pool routes for managers, per
  decision 11 below) was verified by reading the route code and by the live test suite, not
  asserted. `ops-stds-enforcer` separately caught a stale README pool-section description drifted
  from the new builder-composable shape — fixed in the same PR.
- **PR #194 — §34 adversarial pass:** confirmed screening/refusal behavior is byte-identical
  across the change, and caught one real defect before merge — the legacy `screened_photos`
  fallback keyed on falsiness instead of key-absence, so a submission with `photo_groups=[]`
  (a real, valid state — zero photos taken) would have silently fallen through to the old
  ungrouped path instead of rendering correctly empty. Fixed in the same PR. A code comment
  naming not-yet-existing form versions (`incident-report-v4`, the two other v2s) as present fact
  was reworded per the "static text must not assert live catalog state" reflex (HOUSE_REFLEXES §5).

## Decisions made during session

Eleven operator-ratified decisions from the `grill-me` session, plus one host-topology decision
carried from standing convention:

1. **Forward-only, no backfill.** The 2026-08-26 incident and any other pre-photos incident
   reports are not retroactively amended.
2. **One combined effort, not staggered.** The incident report gets one version bump
   (`incident-report-v4`) carrying both the photos capability and whatever else was pending, not
   a photos-only v4 followed by a second bump.
3. **Photos palette macro in the builder** — the "+ Photos" item inserts a header + photo field
   as one action, closing the "standalone add photo" gap without inventing a new section type.
4. **Pool-first capacity strategy.** Inline stays capped at 4/8; raising the inline cap is
   ADR-0001's R2 trigger and was explicitly NOT taken this session — the pool is the intended
   answer to "more photos," not a larger inline limit.
5. **`incident-report-v4` = inline photo field(s) + the composable pool**, both available to the
   form's author in the builder.
6. **Photos stay optional** on the incident report — no legal-floor (`required-content.json`)
   change.
7. **Scope for this pass = `incident-report-v4` + `erosion-inspection-v2` + `material-incident-v2`**
   — the pool capability is added to these three peer forms together, not incident-report alone.
8. **No special WPR handling for non-daily pool photos.** A pool photo attached to, say, an
   erosion inspection can be offered by the WPR's existing auto-select logic on an uncurated week
   exactly like a daily photo would — office curation (the three-state contract) is accepted as
   the control, not a new filter. This is a deliberate accepted residual risk, not an oversight.
9. **Pool roles stay manager/admin** — submitters remain 403'd on pool routes, matching the
   existing daily-photo-pool posture.
10. **Placement scope relaxed for managers on pool routes: any active job**, not just a job the
    manager is scoped to. This is a deliberate loosening from the existing `requireJobScope`
    pattern, carried through the adversarial security review above rather than assumed safe.
11. **Live proof standard = one real `erosion-inspection-v2` submission** carrying both an inline
    photo and a pool photo, run after Phase B's production republish — not a synthetic/local
    smoke.

Host-topology decision (standing convention, reaffirmed): this session runs on the dev Mac and
lands PRs only. Deploy, migration application, and the actual form-builder republish through the
C12 pipeline are production-Mac operations and are handed off below as Phase B, not attempted
here.

## Open items handed off — Phase B (production-Mac checklist)

Nothing in this list has run yet. Until it does, the live portal has none of this session's
capability and the incident report still has no photos in production.

1. `git -C ~/its pull origin main` on the production Mac.
2. Apply migration 0079 (found unapplied during pre-flight, unrelated to this session's own
   changes — see finding 4 above) before deploying.
3. `npm run deploy`.
4. Author `incident-report-v4`, `erosion-inspection-v2`, and `material-incident-v2` **one at a
   time** in the live form editor, through the C12 auto-publish pipeline (not a bulk/manual
   publish).
5. Live proof: file one real `erosion-inspection-v2` submission carrying both an inline photo and
   a pool photo (decision 11).
6. Verify the eager-window behavior once v4/v3 exist side-by-side with the prior versions
   (`reference_eager-forms-n2-window.md` — current+immediately-previous only).
7. Verify the blank-PDF portal note (PR #194) renders correctly in the Box-archived fillable PDF
   for at least one of the three republished forms.

Accepted residual risks carried into Phase B, not treated as blockers:
- WPR auto-select can offer incident-report/erosion-inspection/material-incident pool photos on
  an uncurated week (decision 8) — office curation is the control.
- `daily-report-v7`'s PDF layout changed as a side effect of PR #194: the previously merged
  "Site Photos" grid is now a per-field heading plus an "Additional site photos" pool section.
  Deliberate, not flagged as a defect.

## What was NOT touched

- The daily-report photo pool's existing role/scope model — read and reused as the pattern for
  decisions 9–10, not modified for the daily lane itself.
- `required-content.json` / the legal-floor gate — decision 6 keeps photos optional, so no
  required-content change was needed on any of the three forms in scope.
- The inline photo-count cap (still 1..4 via the Max-photos editor control; ADR-0001's R2 trigger
  not pulled — decision 4).
- Any production-Mac action — deploy, migration apply, form republish, live proof — all of Phase B
  above.

## Cross-references

- `docs/tech_debt.md` — WPR caption-prefix deferred item added in PR #194 (session-close
  maintenance tracks it, not duplicated here).
- `docs/operations/pr_merge_discipline.md` — four-part verify convention used above.
- `docs/HOUSE_REFLEXES.md` §5 — "static text must not assert a live gate/catalog state," applied
  to the PR #194 comment fix noted under Adversarial review.
- ADR-0001 — the R2 inline-cap-raise trigger, explicitly not pulled (decision 4).
- `reference_eager-forms-n2-window.md` (memory) — governs Phase B item 6.
- Related workstream: `safety_portal` (form builder, `worker/fieldops_daily_photos.ts`) and
  `safety_reports` (`intake.py`'s PDF render path, `photo_screen.py`).
