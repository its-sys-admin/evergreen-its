-- rfqs.job_id + the three per-job procurement indexes (Track A8, 2026-08-13).
--
-- WHY: the job tracker gains a per-job Procurement section (POs / RFQs / Subcontracts with
-- lifecycle state). purchase_orders and subcontracts have carried the canonical `job_id`
-- (JOB-######) since birth; `rfqs` does NOT — 0056 deliberately snapshots job data at draft
-- time, and 0070's own header records that a backfill is impossible ("rfqs holds no job_id to
-- join from"). This is character-for-character the defect 0069 fixed for po_estimates: the SPA
-- (RfqBuilderPage) resolves the job BY job_id, holds it in state, and then throws it away at
-- submit. The column captures what the client already knows, from now on.
--
-- '' (not NULL) mirrors 0043/0050/0069: the lanes' snapshot columns are NOT-NULL-with-empty
-- sentinels, and '' composes with the NOT-NULL convention their queries assume. NO backfill —
-- pre-existing RFQs will never appear on a job's Procurement section (stated in the section's
-- hint); resolving them by job_no would be the silent-wrong-site corruption 0064/0069 exist to
-- eliminate (2026.384 is TWO jobs).
--
-- The three indexes cover the new per-job read (GET /api/fieldops/jobs/:job_id/procurement) —
-- without them every job-detail load full-scans three tables.
--
-- APPLY BEFORE DEPLOY: run `npx wrangler d1 migrations apply its-safety-portal-db --remote`
-- BEFORE the Worker that binds rfqs.job_id / the procurement route deploys. Same rule as
-- 0010/0033/0074. (Always `git pull` ~/its to latest main FIRST — forensic #2.)

ALTER TABLE rfqs ADD COLUMN job_id TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_purchase_orders_job ON purchase_orders(job_id);
CREATE INDEX IF NOT EXISTS idx_subcontracts_job    ON subcontracts(job_id);
CREATE INDEX IF NOT EXISTS idx_rfqs_job            ON rfqs(job_id);
