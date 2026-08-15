-- 0077: change orders become DOCUMENTS (Track D2), replacing the 0076 record model.
--
-- Operator directive (2026-08-14): "Change orders aren't just a description … change
-- orders should feed from the original contract or order and then have a fully new
-- generated contract." A change order is therefore a normal lane document — a draft
-- CLONED from a sent parent (full configuration + lines/SOV), edited by the office,
-- and generated/rendered/reviewed/sent through the lane's existing pipeline. The
-- parent stays in force: a CO sets change_order_of (NOT supersedes_*_id), so the
-- supersession flip sites are structurally inert for it.
--
-- Numbering: a CO never allocates a family revision (revision stays NULL — the
-- family MAX subquery excludes it via `revision IS NOT NULL`); at generate it mints
-- `{parent po_number}-CO{co_seq}`. Uniqueness rides po_number UNIQUE plus the
-- partial unique index below. co_seq is allocated at clone time (MAX+1 over ALL of
-- the parent's COs regardless of status, so a canceled GENERATED CO's seq is never
-- reused; a pruned never-generated draft's seq may be — same reasoning as the
-- numbering-reuse guard in prune.ts, its number was never minted or filed).
--
-- STORE-ONLY (the estimate_id precedent, worker/po.ts): change_order_of / co_seq
-- must NEVER enter canonicalPoJson / canonicalSubJson or the po:v1 / sub:v1 HMAC
-- strings — the Mac-side recompute would break. The CO relationship IS signed,
-- because the parent number is embedded in the signed po_number/sc_number; the Mac
-- renders the change-order clause by deriving from that signed string.

ALTER TABLE purchase_orders ADD COLUMN change_order_of INTEGER;
ALTER TABLE purchase_orders ADD COLUMN co_seq INTEGER;
ALTER TABLE subcontracts ADD COLUMN change_order_of INTEGER;
ALTER TABLE subcontracts ADD COLUMN co_seq INTEGER;

CREATE UNIQUE INDEX idx_po_change_order_seq
  ON purchase_orders(change_order_of, co_seq) WHERE change_order_of IS NOT NULL;
CREATE UNIQUE INDEX idx_sub_change_order_seq
  ON subcontracts(change_order_of, co_seq) WHERE change_order_of IS NOT NULL;

-- The 0076 record model (description + signed amount + approve/reject) is superseded
-- by the document model above and had zero rows at drop time (verified live
-- 2026-08-14, day-one table). Its indexes drop with it.
DROP TABLE procurement_change_orders;
