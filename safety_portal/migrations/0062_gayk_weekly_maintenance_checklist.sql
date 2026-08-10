-- INSPECTION-LIBRARY SEED — the GAYK/DOYLE weekly maintenance checklist, the third library template
-- for these machines alongside the two transport checklists seeded by 0061. Closes the tech-debt
-- entry opened with 0061 ("GAYK/DOYLE weekly maintenance checklist is not modeled anywhere").
--
-- PROVENANCE (content source of truth — do not invent items beyond these):
--   • "Weekly Checklist and Maintenance", PAGE 2 of GAYK North America doc 240916, committed at
--     safety_portal/reference_forms/GAYK_DOYLE_Piledriver_Daily_and_Weekly_Checklists.pdf. Page 1 of
--     the same document is the pre-op form equipment-gayk-piledriver-v1 (#44).
--   The source opens "Must be done in addition to the daily checklist" — this is a periodic
--   MAINTENANCE duty, NOT a pre-operation inspection, which is why it lands here as a checklist
--   template and not as an equipment-preinspection form variant: folding it into the daily pre-op
--   form would have made operators attest weekly greasing every single day, and filing it AS an
--   inspection would misclassify a maintenance record.
--
-- ITEM SPLIT: the source's first bullet is a compound sentence covering BOTH greasing and track
-- tension ("Grease all zerk fittings … and remove covers on machine base to check track tension").
-- Per the 0028 convention ("items are the source bullets split into individual short clauses") it
-- becomes two items — seq 10 (grease) and seq 20 (track tension) — because they are separate
-- physical checks with separate pass criteria, and a single checkbox covering both would let a
-- skipped track-tension check ride in on a completed greasing round. Every other item is one source
-- bullet. All nine are manual_attest: the engine already offers an optional note on completion,
-- which is where the source's two write-in lines ("Note deflection:", "Note condition and location
-- of damage or leaks:") land — no schema support is needed for them.
--
-- NO RECURRENCE ROW IS SEEDED, deliberately. checklist_recurrences (0040) requires a NOT NULL
-- assignee_personnel_id AND a NOT NULL job_id — live tenant data this migration cannot know and must
-- not guess — and the feature rides the Worker var RECURRING_CHECKLISTS_ENABLED (read the live
-- deploy for its value, never this file). The admin defines the weekly cadence per job/person in the
-- Inspections UI once a machine is actually on a job; seeding one here would invent an assignment.
--
-- ORDER DEPENDENCY (activation — same standing rule as 0007/0013/0023/0025/0026/0027/0028/0061):
-- apply to the live D1 with
--   wrangler d1 migrations apply its-safety-portal-db --remote
-- BEFORE the next Worker deploy. CONTENT-ONLY (no schema change, no new routes), so the
-- already-deployed Worker renders these rows exactly like the 0028/0061 ones and either order is
-- LOW-RISK — keep apply-before-deploy anyway per the standing rule. (Always `git pull` `~/its` to
-- latest `main` BEFORE applying — the stale-migrations-list lockout class.)
--
-- GUARD / IDEMPOTENCY: identical shape to 0061 — the template row is guarded NOT EXISTS on
-- (kind, title); each item row is guarded on (that template, label); every item re-resolves its
-- template_id by (kind, title) rather than using last_insert_rowid(), so a partial apply is safely
-- resumable and a re-apply is a true no-op. Nothing is deleted.

INSERT INTO checklist_templates (kind, job_id, title, active)
SELECT 'generic_inspection', NULL, 'GAYK/DOYLE Piledriver Weekly Maintenance Check', 1
WHERE NOT EXISTS (SELECT 1 FROM checklist_templates WHERE kind = 'generic_inspection' AND title = 'GAYK/DOYLE Piledriver Weekly Maintenance Check');

INSERT INTO checklist_items (template_id, seq, item_type, label)
SELECT t.id, 10, 'manual_attest', 'Grease all zerk fittings with Kajo MOS 2 grease - mast rollers (one at top, two at centre, one at bottom), top and bottom of the mast up/down cylinder, the mast pivot point, and the machine chassis rotate transport/work point'
FROM checklist_templates t WHERE t.kind = 'generic_inspection' AND t.title = 'GAYK/DOYLE Piledriver Weekly Maintenance Check'
  AND NOT EXISTS (SELECT 1 FROM checklist_items ci WHERE ci.template_id = t.id AND ci.label = 'Grease all zerk fittings with Kajo MOS 2 grease - mast rollers (one at top, two at centre, one at bottom), top and bottom of the mast up/down cylinder, the mast pivot point, and the machine chassis rotate transport/work point');

INSERT INTO checklist_items (template_id, seq, item_type, label)
SELECT t.id, 20, 'manual_attest', 'Remove the covers on the machine base and check track tension - the track should not sag more than 25mm/1"'
FROM checklist_templates t WHERE t.kind = 'generic_inspection' AND t.title = 'GAYK/DOYLE Piledriver Weekly Maintenance Check'
  AND NOT EXISTS (SELECT 1 FROM checklist_items ci WHERE ci.template_id = t.id AND ci.label = 'Remove the covers on the machine base and check track tension - the track should not sag more than 25mm/1"');

INSERT INTO checklist_items (template_id, seq, item_type, label)
SELECT t.id, 30, 'manual_attest', 'Check mast and hammer guides for tightness - no more than 75Nm/55 ft-lbs'
FROM checklist_templates t WHERE t.kind = 'generic_inspection' AND t.title = 'GAYK/DOYLE Piledriver Weekly Maintenance Check'
  AND NOT EXISTS (SELECT 1 FROM checklist_items ci WHERE ci.template_id = t.id AND ci.label = 'Check mast and hammer guides for tightness - no more than 75Nm/55 ft-lbs');

INSERT INTO checklist_items (template_id, seq, item_type, label)
SELECT t.id, 40, 'manual_attest', 'Check hammer carriage adjustment'
FROM checklist_templates t WHERE t.kind = 'generic_inspection' AND t.title = 'GAYK/DOYLE Piledriver Weekly Maintenance Check'
  AND NOT EXISTS (SELECT 1 FROM checklist_items ci WHERE ci.template_id = t.id AND ci.label = 'Check hammer carriage adjustment');

INSERT INTO checklist_items (template_id, seq, item_type, label)
SELECT t.id, 50, 'manual_attest', 'Check mast in/out tube adjustment'
FROM checklist_templates t WHERE t.kind = 'generic_inspection' AND t.title = 'GAYK/DOYLE Piledriver Weekly Maintenance Check'
  AND NOT EXISTS (SELECT 1 FROM checklist_items ci WHERE ci.template_id = t.id AND ci.label = 'Check mast in/out tube adjustment');

INSERT INTO checklist_items (template_id, seq, item_type, label)
SELECT t.id, 60, 'manual_attest', 'Check oil cooler for debris or damage and clean if necessary'
FROM checklist_templates t WHERE t.kind = 'generic_inspection' AND t.title = 'GAYK/DOYLE Piledriver Weekly Maintenance Check'
  AND NOT EXISTS (SELECT 1 FROM checklist_items ci WHERE ci.template_id = t.id AND ci.label = 'Check oil cooler for debris or damage and clean if necessary');

INSERT INTO checklist_items (template_id, seq, item_type, label)
SELECT t.id, 70, 'manual_attest', 'Check radiator for debris or damage and clean if necessary'
FROM checklist_templates t WHERE t.kind = 'generic_inspection' AND t.title = 'GAYK/DOYLE Piledriver Weekly Maintenance Check'
  AND NOT EXISTS (SELECT 1 FROM checklist_items ci WHERE ci.template_id = t.id AND ci.label = 'Check radiator for debris or damage and clean if necessary');

-- Source carries a "Note deflection:" write-in here; it lands in the item's optional completion note.
INSERT INTO checklist_items (template_id, seq, item_type, label)
SELECT t.id, 80, 'manual_attest', 'Check deflection of the mast chain - no more than 25mm/1" - and lubricate with a quality chain lube'
FROM checklist_templates t WHERE t.kind = 'generic_inspection' AND t.title = 'GAYK/DOYLE Piledriver Weekly Maintenance Check'
  AND NOT EXISTS (SELECT 1 FROM checklist_items ci WHERE ci.template_id = t.id AND ci.label = 'Check deflection of the mast chain - no more than 25mm/1" - and lubricate with a quality chain lube');

-- Source carries a "Note condition and location of damage or leaks:" write-in here; same treatment.
INSERT INTO checklist_items (template_id, seq, item_type, label)
SELECT t.id, 90, 'manual_attest', 'Check hoses and hydraulic connections for condition, tightness and leaks'
FROM checklist_templates t WHERE t.kind = 'generic_inspection' AND t.title = 'GAYK/DOYLE Piledriver Weekly Maintenance Check'
  AND NOT EXISTS (SELECT 1 FROM checklist_items ci WHERE ci.template_id = t.id AND ci.label = 'Check hoses and hydraulic connections for condition, tightness and leaks');
