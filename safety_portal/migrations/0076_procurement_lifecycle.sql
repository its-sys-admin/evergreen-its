-- Procurement lifecycle tracking + change orders (Track D, operator directive 2026-08-14).
--
-- The A8 Procurement section was READ-ONLY; the operator now wants the office to TRACK each
-- document through its lifecycle from the job's own screen — generated → submitted to the
-- vendor/subcontractor → accepted — and to record change orders against it.
--
-- DESIGN: the machine `status` CHECKs are NOT rebuilt (a CHECK edit is a full-table copy dance
-- on money data). Instead:
--   • PO "accepted" rides two NEW columns (accepted_at/accepted_by) — `status` stays 'sent',
--     so every Mac status-sync guard (each an exact-prior-state UPDATE) keeps working untouched
--     and the vendor-acceptance fact is portal-owned with provenance.
--   • Subcontract "accepted" IS the existing 'executed' CHECK state (countersigned) — the portal
--     marks it (guarded from 'sent'), converging with the ledger sync's own executed write
--     (also guarded from 'sent' → the loser no-ops). accepted_at/by record WHO logged it.
--   • RFQ close: 'closed' has been in the 0056 CHECK since birth with NO writer (the A8 recon's
--     dead-state finding) — the portal close action is that writer.
--   • Manual "mark submitted" (a document sent OUTSIDE ITS) writes the existing 'sent' state
--     with an audit row; the Mac sync's own sent-write no-ops afterward (guard status='approved').
--     This is an operator-sanctioned relaxation of the "D1 status ≥approved is a Mac-side cache"
--     framing: the portal is now a SECOND, forward-only, audited writer of sent/executed.
--
-- CHANGE ORDERS: one append-only-ish table for both lanes. job_id is DENORMALIZED from the
-- parent document row SERVER-SIDE at create (never client-supplied) so the per-job screen reads
-- one indexed scan. seq is per-document, server-assigned (display "CO #n"). amount_cents is
-- SIGNED (a deductive change order is negative). Soft-delete via active (the schedule-task
-- shape); status pending → approved | rejected with decided_at/decided_by provenance.
--
-- APPLY BEFORE DEPLOY: `npx wrangler d1 migrations apply its-safety-portal-db --remote` BEFORE
-- the Worker that reads these columns deploys (the 0010/0033/0074/0075 rule; `git pull` first).

ALTER TABLE purchase_orders ADD COLUMN accepted_at TEXT;
ALTER TABLE purchase_orders ADD COLUMN accepted_by TEXT;
ALTER TABLE subcontracts    ADD COLUMN accepted_at TEXT;
ALTER TABLE subcontracts    ADD COLUMN accepted_by TEXT;

CREATE TABLE IF NOT EXISTS procurement_change_orders (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id        TEXT    NOT NULL,                          -- denormalized from the parent doc (server-side)
  doc_type      TEXT    NOT NULL CHECK (doc_type IN ('po','subcontract')),
  doc_id        INTEGER NOT NULL,                          -- purchase_orders.id / subcontracts.id
  seq           INTEGER NOT NULL,                          -- per-document, server-assigned (CO #n)
  description   TEXT    NOT NULL,
  amount_cents  INTEGER NOT NULL DEFAULT 0,                -- SIGNED — deductive COs are negative
  status        TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  created_by    TEXT    NOT NULL,
  created_at    INTEGER NOT NULL DEFAULT (unixepoch()),
  decided_by    TEXT,
  decided_at    INTEGER,
  active        INTEGER NOT NULL DEFAULT 1,
  UNIQUE (doc_type, doc_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_change_orders_job ON procurement_change_orders(job_id, active);
CREATE INDEX IF NOT EXISTS idx_change_orders_doc ON procurement_change_orders(doc_type, doc_id, active);
