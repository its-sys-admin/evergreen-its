-- Daily-photo → material-line binding (PR4). Lets a delivery-confirmation photo say WHICH
-- expected-material line (and optionally which receipt EVENT) it evidences, instead of landing
-- in the day's photo pool as one more undifferentiated image.
--
-- WHY: a manager marking "Partially delivered" photographs the pallet count or the damaged
-- corner. Today that photo files into the daily report's additional-photos pool with nothing
-- tying it to the line it is about, so the delivery ledger records the CLAIM ("40 arrived") and
-- the photo evidencing it sits somewhere else entirely. Anyone auditing a disputed delivery
-- later has to match them up by eye, by timestamp.
--
-- THE BINDING IS SERVER-SIDE, AND THAT IS THE WHOLE POINT. The submission's photo references
-- keep their EXACT `{pool_id, caption?}` shape — `parseAdditionalPhotoRefs` rejects any other
-- key precisely so an attacker-controlled foreign key cannot ride payload_json into a filed
-- submission. So the binding is established at UPLOAD, where the Worker validates it against
-- the job's own active lines, and is never accepted from the submission body.
--
-- BOTH COLUMNS ARE DENORMALIZED COPIES. The authoritative values ride INSIDE `photo_json`,
-- which is what the daily-photo HMAC covers ("daily_photo:v1" \n job_id \n work_date \n
-- photo_json, recomputed Mac-side before any byte is screened). A column added alongside is NOT
-- signature-covered, so anything trust-bearing must read the value out of the verified
-- photo_json — these columns exist to make the pool QUERYABLE by line, nothing more.
--
-- NAMING TRAP (two live identifiers, deliberately not interchangeable):
--   • job_expected_materials.line_uuid  — TEXT, the stable per-line mirror key (0039)
--   • job_expected_materials.id         — INTEGER, what material_receipt_events.line_id and
--                                         material_shipments.line_id soft-ref (0059)
-- `line_uuid` here is the TEXT one, matching how the daily form already deep-links a line into
-- a material-incident submission. `receipt_event_id` is the INTEGER material_receipt_events.id.
--
-- Soft refs, no FKs — the 0031/0059 posture: a purge or a line deactivation must not cascade
-- into a photo record, and a dangling binding degrades to "unbound photo", never a broken row.
--
-- ORDER DEPENDENCY (the standing rule): apply to live D1 BEFORE the Worker deploys. The upload
-- route binds these columns, so a Worker deployed ahead of this migration 500s every daily-photo
-- upload. `git -C ~/its pull origin main` to latest BEFORE `wrangler d1 migrations apply` — the
-- stale-migrations-list lockout class. Depends on 0037 (daily_photo_pool) and 0059
-- (material_receipt_events); migrations apply in order, so no extra care is needed.

ALTER TABLE daily_photo_pool ADD COLUMN line_uuid TEXT;          -- soft-ref job_expected_materials.line_uuid (TEXT)
ALTER TABLE daily_photo_pool ADD COLUMN receipt_event_id INTEGER; -- soft-ref material_receipt_events.id (INTEGER)

-- The read this enables: "every photo bound to this line", for the materials page's delivery
-- history. Partial — the overwhelming majority of pool rows are ordinary day photos with no
-- binding at all, and indexing their NULLs would be pure overhead.
CREATE INDEX IF NOT EXISTS idx_daily_photo_pool_line
  ON daily_photo_pool(line_uuid, id)
  WHERE line_uuid IS NOT NULL;
