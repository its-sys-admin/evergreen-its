-- 0080 — VOID a delivery mark without destroying it (operator ask, 2026-10-21).
--
-- PROBLEM. The receipt ledger (`material_receipt_events`, 0059) is append-only, and every
-- surface that answers "how much has arrived" SUMs it: the materials page, the stored line
-- projection the §51 Material List mirror reads, the Receipts mirror's Line Qty Received, and
-- the WPR page-5 delivery log. A mistaken mark (a double tap, a mark weeks later for a load
-- that was already marked, a qty typo) therefore inflates the line on every surface, and until
-- now the only remedy was a hand-edit of production D1. The live ledger held two exact repeat
-- marks and 13 over-received lines across two jobs (up to 3x) on 2026-10-21.
--
-- MODEL. A mark is never deleted and never edited. It is VOIDED: annotated with who, when and
-- why, then excluded from every rollup. A corrected entry is a NEW event that names the one it
-- replaces (`supersedes_id`). The Smartsheet Receipts mirror keeps the voided row, annotated, so
-- the operator's sheet stays a complete history.
--
--   voided_at      epoch seconds; NULL = the mark counts. Written ONCE, by the void route's
--                  in-WHERE guard (`voided_at IS NULL`), so a second void is a clean 409.
--   voided_by      the acting ACCOUNT username. Never served raw: reads resolve the personnel
--                  display name, else the account's role label (House Reflex §5, W9).
--   void_reason    REQUIRED at the route (bounded to 500, the ledger's note bound).
--   supersedes_id  the voided event this one corrects. Same line only, and only a voided one
--                  (both enforced at the route, in the INSERT's own WHERE). The partial UNIQUE
--                  index makes "each voided mark is corrected at most once" a storage fact, not
--                  a route convention: two racing corrections cannot both land.
--   sort_id        the event's ORDERING position; NULL means its own id. A correction inherits
--                  COALESCE(original.sort_id, original.id), so a fix recorded weeks later sits
--                  where the mistake sat and does not become "the latest thing the field told
--                  us". Without it, correcting an old partial after a later 'delivered' mark
--                  would flip the line back to partial. Chain-safe: correcting a correction
--                  inherits the same position again.
--
-- Every reader orders by COALESCE(sort_id, id) DESC, id DESC and filters voided_at IS NULL.
--
-- CHANGED 0078 SEMANTICS (operator decision D5, 2026-10-21). 0078 says a curated WPR delivery
-- row "survives any later change to the ledger". A VOID is the exception: a curated row that
-- carries a voided event's uuid is dropped at read time, and the screen says how many were
-- dropped. The stored snapshot is not rewritten. Curated rows saved before this migration carry
-- no uuid and cannot be matched, so they stay until the office re-curates.
--
-- ORDER DEPENDENCY: apply to the live D1 BEFORE the Worker that reads these columns deploys.
-- The list read, the mark route, the Receipts mirror route and the WPR builder all bind them, so
-- a Worker deployed first 500s every materials surface. `git -C ~/its pull origin main` first.
--
-- TO REVERSE: there is no clean down-migration. SQLite DROP COLUMN fails on an indexed column,
-- so drop idx_material_receipt_events_supersedes first. Reversing also discards every void,
-- which restores the over-counts this migration exists to remove. Don't reverse it; escalate.
--
-- Additive and NULL-defaulted: every existing row reads as an active, uncorrected mark in its
-- own position, which is exactly what it was.

ALTER TABLE material_receipt_events ADD COLUMN voided_at     INTEGER;
ALTER TABLE material_receipt_events ADD COLUMN voided_by     TEXT;
ALTER TABLE material_receipt_events ADD COLUMN void_reason   TEXT;
ALTER TABLE material_receipt_events ADD COLUMN supersedes_id INTEGER;  -- soft-ref material_receipt_events.id (the 0059 no-FK posture)
ALTER TABLE material_receipt_events ADD COLUMN sort_id       INTEGER;

CREATE UNIQUE INDEX IF NOT EXISTS idx_material_receipt_events_supersedes
  ON material_receipt_events(supersedes_id) WHERE supersedes_id IS NOT NULL;
