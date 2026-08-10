-- INSPECTION-LIBRARY SEED — two generic_inspection templates for GAYK/DOYLE pile-driver transport:
-- a Start-Up Check and a Loading & Securing Check. Admins assign these per-job/per-person from the
-- Inspections library exactly like the six ER-Safety-Manual templates seeded by 0028 Part 2; nothing
-- here touches the daily_default template or any job_override.
--
-- PROVENANCE (content source of truth — do not invent items beyond these):
--   • "START-UP INSTRUCTIONS – LOADING INSTRUCTIONS – SECURING THE LOADS" (GAYK North America,
--     doc 240917, committed at safety_portal/reference_forms/GAYK_DOYLE_Startup_Loading_Securing_Loads.pdf).
--     Its three headed sections map to these two templates: START-UP INSTRUCTIONS -> template 1;
--     LOADING INSTRUCTIONS + SECURING THE LOADS -> template 2.
--   Item labels are the source sentences split into individual short attestation clauses, in source
--   order. Two source statements are deliberately NOT items because neither is a check the operator
--   performs: the "Example Pic Only" aside, and the note that these machines carry no GPS antenna bar.
--   The diesel-sufficiency item stays on the START-UP template because that is where the source
--   prints it, even though its text refers to loading — it is checked before the machine is driven
--   onto the trailer, so it belongs to the earlier checklist.
--
-- TRANSCRIPTION NORMALIZATIONS (the only two places an item label departs from doc 240917):
--   • seq 10 reads "completes its cycle of building pressure"; the source reads "continues its cycle
--     of building pressure". Transcribed as "completes" because the very next source sentence —
--     "Once the high-pressure pump shuts off then you can start the pile driver" — shows completion
--     is what is meant, and "continues" reads as an instruction to keep waiting indefinitely.
--     Worth confirming with GAYK on the next revision.
--   • seq 40 appends "(no red diesel)". That clause is NOT on doc 240917; it is carried from the
--     sibling GAYK daily checklist, doc 240916 ("clean clear diesel (no red diesel!)"), committed
--     alongside as reference_forms/GAYK_DOYLE_Piledriver_Daily_and_Weekly_Checklists.pdf. Same
--     machine, same fuel rule, and the consequence of red diesel in this tank is the reason the
--     warning is highlighted on 240917 — but it is a cross-document borrow, so it is flagged here
--     rather than left to look like verbatim 240917 text. ("clear clean" word order IS 240917's.)
--
-- WHY TEMPLATE 2 IS SHORT (2 items): the source's LOADING and SECURING sections are two sentences of
-- instruction plus photographs. Everything else a rigger does (tensioning, re-checking after the
-- first miles) is real practice but is NOT in this document, and this file is not the place to invent
-- it — see forms/README.md "no invention". Add items via the admin UI
-- (POST /api/fieldops/checklist/inspection/:id/item, cap.checklist.manage) if the operator wants them.
--
-- ORDER DEPENDENCY (activation — same standing rule as 0007/0013/0023/0025/0026/0027/0028): apply to
-- the live D1 with
--   wrangler d1 migrations apply its-safety-portal-db --remote
-- BEFORE the next Worker deploy. NOTE this migration is CONTENT-ONLY (no schema change, no new
-- routes): the already-deployed Worker renders these rows exactly like the 0028 ones, so either order
-- is LOW-RISK — keep apply-before-deploy anyway per the standing rule. (Always `git pull` `~/its` to
-- latest `main` BEFORE `wrangler d1 migrations apply` — the stale-migrations-list lockout class.)
--
-- GUARD / IDEMPOTENCY: template rows are guarded NOT EXISTS on (kind, title) — a re-apply, or an
-- admin-created template with the same title, is never duplicated. Item rows are guarded on (that
-- template, label), and every item re-resolves its template_id by (kind, title) rather than using
-- last_insert_rowid(), so a partial apply is safely resumable. Nothing is deleted by this migration.

-- ── Template 1 — Start-Up Check. [source doc 240917, section "START-UP INSTRUCTIONS"] ──────────────
INSERT INTO checklist_templates (kind, job_id, title, active)
SELECT 'generic_inspection', NULL, 'GAYK/DOYLE Piledriver Start-Up Check', 1
WHERE NOT EXISTS (SELECT 1 FROM checklist_templates WHERE kind = 'generic_inspection' AND title = 'GAYK/DOYLE Piledriver Start-Up Check');

INSERT INTO checklist_items (template_id, seq, item_type, label)
SELECT t.id, 10, 'manual_attest', 'Turn the ignition to the first notch and wait until the high-pressure pump completes its cycle of building pressure'
FROM checklist_templates t WHERE t.kind = 'generic_inspection' AND t.title = 'GAYK/DOYLE Piledriver Start-Up Check'
  AND NOT EXISTS (SELECT 1 FROM checklist_items ci WHERE ci.template_id = t.id AND ci.label = 'Turn the ignition to the first notch and wait until the high-pressure pump completes its cycle of building pressure');

INSERT INTO checklist_items (template_id, seq, item_type, label)
SELECT t.id, 20, 'manual_attest', 'Confirm the high-pressure pump has shut off, then start the pile driver'
FROM checklist_templates t WHERE t.kind = 'generic_inspection' AND t.title = 'GAYK/DOYLE Piledriver Start-Up Check'
  AND NOT EXISTS (SELECT 1 FROM checklist_items ci WHERE ci.template_id = t.id AND ci.label = 'Confirm the high-pressure pump has shut off, then start the pile driver');

INSERT INTO checklist_items (template_id, seq, item_type, label)
SELECT t.id, 30, 'manual_attest', 'Increase the throttle to load the machine before moving the pile driver'
FROM checklist_templates t WHERE t.kind = 'generic_inspection' AND t.title = 'GAYK/DOYLE Piledriver Start-Up Check'
  AND NOT EXISTS (SELECT 1 FROM checklist_items ci WHERE ci.template_id = t.id AND ci.label = 'Increase the throttle to load the machine before moving the pile driver');

INSERT INTO checklist_items (template_id, seq, item_type, label)
SELECT t.id, 40, 'manual_attest', 'Confirm there is enough diesel to load the pile driver - if not, add clear clean diesel only (no red diesel)'
FROM checklist_templates t WHERE t.kind = 'generic_inspection' AND t.title = 'GAYK/DOYLE Piledriver Start-Up Check'
  AND NOT EXISTS (SELECT 1 FROM checklist_items ci WHERE ci.template_id = t.id AND ci.label = 'Confirm there is enough diesel to load the pile driver - if not, add clear clean diesel only (no red diesel)');

INSERT INTO checklist_items (template_id, seq, item_type, label)
SELECT t.id, 50, 'manual_attest', 'Confirm the diesel level is within the amount allowed while transporting in the container'
FROM checklist_templates t WHERE t.kind = 'generic_inspection' AND t.title = 'GAYK/DOYLE Piledriver Start-Up Check'
  AND NOT EXISTS (SELECT 1 FROM checklist_items ci WHERE ci.template_id = t.id AND ci.label = 'Confirm the diesel level is within the amount allowed while transporting in the container');

-- Conditional duty — phrased "or N/A" per the 0028 convention so a normal start does not block completion.
INSERT INTO checklist_items (template_id, seq, item_type, label)
SELECT t.id, 60, 'manual_attest', 'If the machine ran out of diesel while loading: after refuelling, cycle the key to the first notch and let pressure build 5 times, then fire on the 6th once the high-pressure pump shuts down (or N/A)'
FROM checklist_templates t WHERE t.kind = 'generic_inspection' AND t.title = 'GAYK/DOYLE Piledriver Start-Up Check'
  AND NOT EXISTS (SELECT 1 FROM checklist_items ci WHERE ci.template_id = t.id AND ci.label = 'If the machine ran out of diesel while loading: after refuelling, cycle the key to the first notch and let pressure build 5 times, then fire on the 6th once the high-pressure pump shuts down (or N/A)');

-- ── Template 2 — Loading & Securing Check. [source doc 240917, sections "LOADING INSTRUCTIONS" and
-- "SECURING THE LOADS"] The securing item is a `count` (not manual_attest) because the source states
-- a hard number — 4 places, 2 per side — and a count makes the shortfall EXPLICIT rather than
-- invisible: a plain completion below target is refused (`below_target`, item stays open, value
-- recorded), and the only way to close it short is the deliberate acknowledge_below_target path,
-- which REQUIRES a non-empty explanatory note and audits under its own action,
-- `checklist_item_complete_below_target` (worker/fieldops_checklist.ts, the R1 branch — a
-- below-target count must not permanently wedge a checklist). So a short-chained load cannot be
-- closed SILENTLY. It is deliberately NOT a hard block: hard-blocking is a Worker change (a per-item
-- opt-out of the R1 path), not a seed change, and would re-introduce the wedge R1 exists to fix. ────
INSERT INTO checklist_templates (kind, job_id, title, active)
SELECT 'generic_inspection', NULL, 'GAYK/DOYLE Piledriver Loading & Securing Check', 1
WHERE NOT EXISTS (SELECT 1 FROM checklist_templates WHERE kind = 'generic_inspection' AND title = 'GAYK/DOYLE Piledriver Loading & Securing Check');

INSERT INTO checklist_items (template_id, seq, item_type, label)
SELECT t.id, 10, 'manual_attest', 'Load the pile driver with the long mast facing towards the cab of the truck'
FROM checklist_templates t WHERE t.kind = 'generic_inspection' AND t.title = 'GAYK/DOYLE Piledriver Loading & Securing Check'
  AND NOT EXISTS (SELECT 1 FROM checklist_items ci WHERE ci.template_id = t.id AND ci.label = 'Load the pile driver with the long mast facing towards the cab of the truck');

INSERT INTO checklist_items (template_id, seq, item_type, label, target_count)
SELECT t.id, 20, 'count', 'Hooks and binder chains placed on the undercarriage securing points - 4 places, 2 on each side (enter how many are fitted)', 4
FROM checklist_templates t WHERE t.kind = 'generic_inspection' AND t.title = 'GAYK/DOYLE Piledriver Loading & Securing Check'
  AND NOT EXISTS (SELECT 1 FROM checklist_items ci WHERE ci.template_id = t.id AND ci.label = 'Hooks and binder chains placed on the undercarriage securing points - 4 places, 2 on each side (enter how many are fitted)');
