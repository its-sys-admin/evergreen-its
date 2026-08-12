---
type: session_log
date: 2026-08-11
status: closed
related_prs: [69, 73, 75, 78, 86]
workstream: field_ops
tags: [session_log, field_ops, job_identifier, site_phase, client_dedupe, materials, equipment_catalog, procurement, migration, house_reflexes]
---

# Session log — 2026-08-11 → 2026-08-12 · The Evergreen job-identifier ask that uncovered twelve validators, a silent-corruption bug, a client-dedupe defect, an equipment-catalog reconciliation, and procurement site propagation (five PRs)

## Summary

The operator asked for two small things: accept Evergreen's three-segment job identifier
(`20xx.xxx.x`), and drop the create-time Safety-CC minimum. Neither was small. A fan-out map found
the two-segment `job_no` rule implemented **twelve** independent times across the codebase — five
Worker regexes (one buried inline inside `parseRouting`, invisible to a "fix the consts" pass),
five SPA copies, two Python numbering parsers — plus six hard `maxLength={8}` inputs that would
have silently truncated `2026.384.1` as it was typed, and one genuinely dangerous surface:
`po.ts`'s name-prefix fallback regex was unanchored and matched `2026.384.1 Coker Solar`, returning
the truncated `2026.384` — a real, wrong, plausible-looking job number belonging to a **different
site of the same project**. Every other validator in the fan-out refused loudly on the new format;
that one corrupted silently. The operator ratified a model that reads the third segment as a new
`site_phase` field rather than widening `job_no` to three segments (#69, migration 0064) — a choice
that kept eleven of the twelve validators untouched. Checking how the operator would actually enter
the four real jobs then surfaced a second defect: live D1 held four client rows all named "KSI"
(one per job) and none named "Qcells," because job creation inserted a client row unconditionally
against a free-text box and `jobs.client_id` is create-only — fixed with find-or-create plus a real
client picker (#73). A separate operator-requested BOM reconciliation (ten manifests, three racking
vendors, 666 lines, 218 unique parts) found zero real overlap with the existing 37-row equipment
catalogue — an automated first pass reported five matches and all five were false positives — so the
catalogue grew by the 28 equipment rows that were actually verified (#75). Two more items from the
operator's punch list closed via #78 (material_catalog category normalization) and #86 (site
propagation through the estimate→PO and RFQ lanes); a fifth punch-list item turned out to already
be resolved. All five PRs are four-part verify clean; four production jobs were created via direct
D1 write (job-create is session-gated and no credential was available), all migrations applied
`--remote`, and the Worker was deployed with the live bundle verified to contain #86's code.

## The arc — two small asks, twelve validators, and one silent-corruption bug

**Ask 1** was to accept the Evergreen job identifier `20xx.xxx.x` (three segments — year, project,
site). **Ask 2** was to drop the create-time Safety-CC minimum. Before touching either, a fan-out
map of the existing two-segment `job_no` rule found it independently implemented **twelve** times:
five Worker regexes — including one **inline inside `parseRouting`** that a pass fixing only the
named constants would have missed entirely — five SPA copies, and two Python numbering parsers.
Six inputs across the SPA carried a hard `maxLength={8}`, which would have silently truncated
`2026.384.1` mid-keystroke with no error. One surface was worse than silent truncation:
`po.ts`'s name-prefix fallback, `/^(\d{4}\.\d{3})/`, was **unanchored** — it matched
`"2026.384.1 Coker Solar"` and returned the truncated `"2026.384"`, a job number that is real,
plausible, and belongs to a **different site of the same project**. Every other validator in the
fan-out refused loudly on an unrecognized three-segment input; only this one corrupted silently by
returning a wrong-but-valid-looking answer.

**The model (operator decision).** The third segment is `site_phase`, a field the D7 numbering
scheme already carries conceptually — not a widening of `job_no` to three segments. `job_no` stays
two-segment; migration 0064 adds `jobs.site_phase`. This kept exactly **one** of the twelve
validators changing. The rejected alternative — a single three-segment `job_no` — would have
produced **six-segment** document numbers downstream and inverted the two pytest tables that
explicitly assert a six-segment number must be rejected.

**The client-dedupe defect.** Checking how the operator would actually enter the four real jobs
(two Qcells, two KSI) surfaced a second, unrelated defect: live D1 already held **four** client rows
all named "KSI" — one per job that had touched a KSI site — and **zero** named "Qcells," because
`new_client` inserted unconditionally on every job create and the SPA offered only a free-text
client box. `jobs.client_id` is create-only (no route ever updates it afterward), so entering the
four real jobs as-is would have permanently produced a fifth and sixth "KSI" row plus two "Qcells"
rows. Fixed in #73 with a §45 find-or-create on the client insert, a new
`GET /api/fieldops/clients`, and a real client picker replacing the free-text box.

**The BOM reconciliation.** Separately, the operator asked for a materials/equipment reconciliation
against ten manifests spanning three racking vendors, five different column layouts, 666 lines, and
218 unique parts — checked against the existing 37-row catalogue, with which there was **zero**
real overlap. An automated first pass reported five candidate matches; all five were false
positives on generic words (`KIT`, `TRACKER`, `SERIES`), and one paired a Nevados part number to a
GameChange product. The operator scoped the fix to **equipment only**, not all 218 parts, landing
as 28 verified rows (#75).

**Closing the punch list.** The operator then asked for five flagged items to be closed out; #78 and
#86 closed four of them (one — item 4 — turned out to already be done: the Acme2 row was already
`active=0`).

## PRs landed

| PR | What | Merge SHA | Verify |
|---|---|---|---|
| #69 | Accept the Evergreen three-segment job identifier (`20xx.xxx.x`) as `site_phase`; drop the create-time Safety-CC minimum | `5b70e83c129c64f826e30ff6a21ac63ecb4113b0` | four-part clean |
| #73 | Find-or-create the job's client; real client picker on the create form | `54df6027c8f83274cc1c6428df02ecf339119326` | four-part clean |
| #75 | Add the 28 pieces of equipment found in the project BOM corpus | `cbba9b8a5cfa9ee210dd11ce27e041947856dbf0` | four-part clean |
| #78 | Normalise the two hand-added `material_catalog` categories | `45175231d4461f9a298e12c0e1eab62bdeeb3126` | four-part clean |
| #86 | Carry the job's site through the estimate→PO and RFQ lanes | `3ecc4bdf69c2a5180712817f2079ba29223098be` | four-part clean |

**5 of 5 PRs four-part verify clean.**

### #69 — `feat(portal): accept the Evergreen three-segment job identifier (20xx.xxx.x); drop the create-time Safety-CC minimum`

```
state=MERGED mergedAt=2026-08-11T20:11:33Z mergeCommit=5b70e83c129c64f826e30ff6a21ac63ecb4113b0 main-branch CI: ci=success
```

### #73 — `fix(portal): find-or-create the job's client, and give the create form a real picker`

```
state=MERGED mergedAt=2026-08-11T21:08:02Z mergeCommit=54df6027c8f83274cc1c6428df02ecf339119326 main-branch CI: ci=success
```

### #75 — `feat(materials): add the 28 pieces of equipment found in the project BOM corpus`

```
state=MERGED mergedAt=2026-08-11T21:16:36Z mergeCommit=cbba9b8a5cfa9ee210dd11ce27e041947856dbf0 main-branch CI: ci=success
```

### #78 — `fix(materials): normalise the two hand-added material_catalog categories`

```
state=MERGED mergedAt=2026-08-12T02:40:36Z mergeCommit=45175231d4461f9a298e12c0e1eab62bdeeb3126 main-branch CI: ci=success
```

### #86 — `feat(procurement): carry the job's site into the estimate→PO and RFQ lanes`

```
state=MERGED mergedAt=2026-08-12T02:49:09Z mergeCommit=3ecc4bdf69c2a5180712817f2079ba29223098be main-branch CI: ci=success
```

## What went wrong and was corrected

Recorded plainly — this is the useful part, not a footnote:

1. **Cited "`/api/jobs` returns 401" as proof a widened SELECT worked.** It was not proof:
   `requireSession` short-circuits before the handler runs, so the query never executed. Re-verified
   by running the three widened SELECTs directly against live D1.
2. **Warned that a local `wrangler` fault (`_cf_ALARM has 3 columns but 2 values`) would also break
   the `--remote` deploy.** Wrong — the fault lives in the LOCAL `workerd` runtime, which `--remote`
   never starts. Remote deploy verified healthy independently.
3. **Read the Bonacci PV module model off a rendered page image as `VSUN44MH-132BMHR-DG`.** Text
   extraction from the same PDF shows `VSUN###N-132BMHR-DG` — a literal placeholder string in the
   vendor's own document, not a real model number. The module was left **out** of the catalogue
   rather than inventing the missing digits.
4. **The first draft of migration 0065 used `SELECT * FROM (VALUES ...) AS v(a,b,c)`** — valid
   PostgreSQL, rejected by SQLite. Caught by running it against a real database, not by inspection.
5. **A dry-run of the job-creation SQL showed the counter double-advancing and audit rows
   duplicating on re-run**; both fixed before touching production. The counter fix was load-bearing:
   left as found, the next UI-created job would have collided with `JOB-000034` on the primary key.
6. **The first dry-run of migration 0068 was invalid** — the migration-apply loop had already
   applied 0068 to the database before the "baseline" was measured against it. Re-run with 0068
   excluded from the loop to get a real baseline.
7. **In #86, `optStr` returns `NULL` for an absent field while `job_id` is `NOT NULL`**, so the
   first draft 500'd on every estimate upload. Separately, a first RFQ test was appended to an
   existing test case and broke that case's audit-count assertion — which is why the new
   assertions now live in their own dedicated tests rather than riding an existing one.

## Prove-the-control-bites

Every new control added this session was injected as a synthetic violation, confirmed RED, then
reverted, per HOUSE_REFLEXES §2:

- Restoring the unanchored prefix regex in `po.ts`.
- Narrowing the job-number input regex back to two segments.
- Removing only the `(?![\d.]) ` lookahead from the new identifier pattern.
- Dropping the edit-form's segment rejoin.
- Dropping the PO builder's site auto-fill.
- Restoring the unconditional client `INSERT`.
- Restoring always-send-`new_client` on the create form.
- Emitting `.0` for `site_phase == 0` instead of the expected suppression.
- Un-scoping the RFQ allocator from `site`.
- Dropping `job_id` on estimate upload.
- Removing the disposition screen's site seeding.
- Hard-coding the POST body's `site_phase` to `0` while leaving the input showing `1`.

Each injection was confirmed to fail its corresponding test before being reverted.

## Decisions

1. **The third segment of the Evergreen identifier is `site_phase`; `job_no` stays two-segment
   (#69).** The alternative — a three-segment `job_no` — was rejected because it would have produced
   six-segment downstream document numbers and inverted two existing pytest tables asserting a
   six-segment number must be rejected. This choice also confined the fan-out fix to one validator
   instead of twelve.
2. **Catalogue scope from the BOM corpus is equipment only, not all 218 parts (#75).** The
   automated first-pass matcher's 100% false-positive rate (5 for 5, including a cross-vendor
   mismatch) made a full 218-part reconciliation unsafe to attempt in this session; the operator
   scoped it to the 28 verified equipment rows.
3. **Safety CC is optional at job create (#69).** Drops the prior create-time minimum-CC
   requirement per direct operator ask.
4. **The Bonacci VSUN module was left out of the catalogue rather than guessed.** The vendor's own
   PDF carries a literal placeholder (`VSUN###N-132BMHR-DG`) where the real model number belongs;
   inventing digits to fill it in was rejected as data fabrication (Op Stds §4, Data-Fidelity /
   No-Invented-Field-Data).
5. **The four real production jobs were entered via direct D1 write, with the audit actor recorded
   as `'d1-direct-import'` rather than borrowed from a real username.** Job creation is
   session-gated and no operator credential was available in this session; using a real username
   would have misattributed the write in the audit trail.

## Operational events

- **Four production jobs created** by direct D1 write (job-create is session-gated; no credential
  available this session): MH405 → `2026.384.1` (Qcells), OG593 → `2026.384.2` (Qcells), Indian
  Creek → `2025.201.3` (KSI), Minooka → `2026.391.1` (KSI). Audit actor recorded as
  `'d1-direct-import'`. All four mirrored to both `ITS_Active_Jobs` and
  `ITS_Active_Jobs_Progress`.
- **Fully deployed.** All migrations applied `--remote`; the Worker was deployed and the live
  bundle was verified to contain #86's code.
- **Migrations renumbered mid-flight.** What started as 0066/0067 was renumbered to 0068/0069/0070
  after other concurrent sessions landed `0066_job_schedules` and
  `0067_job_weekly_report_inputs` first; every in-code citation of the old numbers was renumbered
  along with the filenames.

## Current live state

- Equipment catalogue: **67 rows**.
- Jobs: **9** total, **7** carrying a non-zero `site_phase`.
- Clients: exactly **1** Qcells row (the dedupe defect from #73 fully resolved on the live tenant).

## Open items / next session

- **The real Bonacci VSUN module number is unresolved** — the vendor PDF carries a literal
  placeholder; needs the real digits from the operator, not a code fix.
- **`tests/test_rfq_poll.py:689`'s `startswith` assertion cannot detect the 0070 shape change** —
  it will pass on both the old and new RFQ-number shapes and needs a sharper assertion.
- **Pre-0069 estimates have `job_id=''`** and are not backfillable from existing data.
- **Pre-0070 RFQ drafts generate site-less numbers** until each is individually re-saved.
- **The local `wrangler` `_cf_ALARM has 3 columns but 2 values` fault is still open** — confirmed
  local-runtime-only (does not affect `--remote` deploys) but not root-caused or fixed.
- **The BOM-matching tooling built for the reconciliation pass exists only in an ephemeral
  scratchpad** — not committed, not productionized. A future BOM reconciliation would need to
  rebuild or formally land it.

## What was NOT touched

- The 190 non-equipment parts from the BOM corpus (218 total minus the 28 equipment rows landed)
  — deliberately out of scope per Decision 2, not silently dropped.
- The `wrangler` local-runtime `_cf_ALARM` fault — diagnosed enough to rule out a `--remote`
  deploy risk, not fixed.
- `tests/test_rfq_poll.py:689` — flagged as a detection gap, not sharpened this session.
- The Bonacci VSUN module catalogue row — left absent rather than filled with a placeholder or a
  guess.

## Cross-references

- `docs/session_logs/2026-08-11_materials-live-fire-manifest-finish-and-operator-decisions.md` and
  `docs/session_logs/2026-08-11_archive-followups-deploy-and-tech-debt-trim.md` — other
  same-day/adjacent-day session logs covering different PR ranges (#59–#66/#71/#72/#74 and
  #43–#65 respectively); not the same work as this log, cited here only so a reader scanning
  2026-08-11 doesn't mistake one for the other.
- `docs/HOUSE_REFLEXES.md` §1 ("a datum has N implementations — enumerate ALL of them first"; the
  twelve-validator fan-out and the `po.ts` unanchored-regex silent-corruption bug are a textbook
  instance) and §2 (prove-the-control-bites — every new control in this session was RED-verified
  before shipping).
- `docs/operations/pr_merge_discipline.md` — the four-part verify applied to all five PRs above.
- Migration files `0064` (`jobs.site_phase`), `0068`/`0069`/`0070` (renumbered from the original
  0066/0067 after a concurrent-session collision) — carry the schema and numbering-scheme changes
  this log describes.

## Verification (final state, PR #86)

```
- pytest: 5237 passed / 2 skipped / 58 deselected
- mypy: no issues in 482 source files
- ruff: clean
- main-branch CI on merge commit: SUCCESS
```

Additional gate figures on the merged tree for #86, beyond the standard four-part quartet: portal
vitest 1402 passed (71 files); SPA vitest 852 passed (60 files); typecheck clean across all three
tsconfigs; enablement docs all 22 current.
