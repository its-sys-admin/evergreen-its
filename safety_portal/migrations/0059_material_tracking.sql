-- Materials tracking (PR2) — per-line PART NUMBER + category + expected SHIP date, the
-- append-only receipt-EVENT ledger, and the scheduled-SHIPMENT level attached to a line.
--
-- WHY: `job_expected_materials` (0031) records WHAT a job expects, and the field marks a line
-- received once. That one-shot model cannot express the real world the office works in: a single
-- part number arrives across several truckloads over weeks — the Deep Lake shipping log records
-- 51 parts as 56 rows, the extra 5 being additional loads of a part already listed, each with its
-- own ship date, delivery date and BOL. A manager had no way to say "half the piles showed up" —
-- the receive route guards `status='expected'` IN-WHERE, so a second mark was a clean 409.
-- (Corrected 2026-08-07: this comment first claimed "1,246 rows", taken from the sheet's declared
-- extent. The sheet declares 1,247 rows × 92 columns and actually holds 57 non-empty rows × 12
-- columns — the rest is padding. The multi-load shape is real; its volume in that file is not.)
--
-- STATUS VOCABULARY — DELIBERATELY NOT WIDENED. `job_expected_materials.status` keeps its 0031
-- CHECK (expected|received|incident) and its exact meaning. SQLite cannot ALTER a CHECK, so
-- widening it would force a full table rebuild (the 0032/0020 pattern) — and that rebuild is not
-- the real objection. The real objection is that `incident` and a partial delivery sit on
-- ORTHOGONAL AXES: a line that is partly delivered AND has a reported problem can hold only one
-- value, so the next delivery mark would erase the incident flag (or vice-versa). The three-way
-- delivery state (delivered|partial|not_delivered) therefore lives in material_receipt_events
-- below and is DERIVED on read (latest event by id wins). `status` stays the coarse legacy
-- projection that the daily form, the §51 Material List mirror and the M3 Material Incidents
-- ledger already speak:
--   expected = not yet fully delivered (never-marked / partial / not-delivered all land here)
--   received = the field asserted the line is complete
--   incident = a problem was flagged — STICKY; a later delivery event never overwrites it.
-- Not widening also keeps five surfaces still: wire-types ExpectedMaterialStatus, statusPill(),
-- material_list.py STATUS_*, material_incidents.py COL_LINE_STATUS, and portal_client's docstring.
--
-- expected_date KEEPS ITS 0031 MEANING = the expected DELIVERY date. It is already labelled
-- "Expected Date" on the Material List sheet and "expected <date>" in two SPA surfaces, so
-- re-pointing it at "ship" would silently change the meaning of live data on all of them. The
-- new expected_ship_date is the SHIP date, and the UI relabels the pair as ship/delivery.
--
-- ORDER DEPENDENCY (activation — the standing rule, same as 0031/0037/0039): apply to the live D1
-- BEFORE the Worker deploys. The same PR's GET /api/fieldops/expected-materials binds
-- part_number / category / expected_ship_date AND both new tables, and the two new write routes
-- (/expected-material/:id/receipt, /material-shipment*) bind them too, so a Worker deployed ahead
-- of this migration 500s those surfaces. (The §51 internal material-list snapshot is NOT among
-- them — it is deliberately untouched here and still selects only pre-0059 columns; its mirror
-- exposure lands with PR4's receipts-ledger sheet.)
-- `git -C ~/its pull origin main` to latest BEFORE `wrangler d1 migrations apply`
-- (the stale-migrations-list lockout class). Depends on 0031 (table) + 0039 (line_uuid);
-- migrations apply in order, so no extra care is needed.

-- ── 1. Expected-material LINE additions (instant, backward-compatible ALTERs) ─────────────────
ALTER TABLE job_expected_materials ADD COLUMN part_number        TEXT;  -- BOM part no.; PR3 matches shipments on it
ALTER TABLE job_expected_materials ADD COLUMN category           TEXT;  -- BOM grouping (e.g. 'HARDWARE'), display-only
ALTER TABLE job_expected_materials ADD COLUMN expected_ship_date TEXT;  -- YYYY-MM-DD; expected_date stays = DELIVERY

-- PR3's importer resolves a shipping-log row to a line by (job_id, part_number); the materials
-- page also groups by it. Not UNIQUE: a manifest legitimately repeats a part number across rows
-- (7000153 appears twice in three of the four sample Customer BOMs, under different groupings).
CREATE INDEX IF NOT EXISTS idx_job_expected_materials_part
  ON job_expected_materials(job_id, part_number);

-- ── 2. Receipt EVENTS — the append-only ledger; the delivery system of record ─────────────────
-- Modelled on the material_incidents ledger posture, NOT the material_list snapshot posture:
-- append-only, so there is no retire path and no reconcile-zeroed branch, which makes the
-- "count drops to zero → stale rows persist" class (#468) structurally impossible.
CREATE TABLE IF NOT EXISTS material_receipt_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  event_uuid  TEXT    NOT NULL,                    -- stable key, crypto.randomUUID() at the ONLY insert path
  line_id     INTEGER NOT NULL,                    -- soft-ref job_expected_materials.id (no FK — the 0031 posture)
  job_id      TEXT    NOT NULL,                    -- denormalized from the line: scopes reads, prune guard, purge cascade
  shipment_id INTEGER,                             -- OPTIONAL soft-ref material_shipments.id (which load this was)
  kind        TEXT    NOT NULL
              CHECK (kind IN ('delivered','partial','not_delivered')),
  qty         REAL,                                -- received on THIS event; NULL for not_delivered
  note        TEXT,                                -- bounded route-side (MAX_NOTE = 500, the 0031 bound)
  event_date  TEXT,                                -- YYYY-MM-DD the delivery happened (defaults to Pacific today)
  actor       TEXT    NOT NULL,                    -- acting ACCOUNT username; reads resolve DISPLAY NAME ONLY (W9)
  created_at  INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_material_receipt_events_uuid ON material_receipt_events(event_uuid);
CREATE INDEX        IF NOT EXISTS idx_material_receipt_events_line ON material_receipt_events(line_id, id);
CREATE INDEX        IF NOT EXISTS idx_material_receipt_events_job  ON material_receipt_events(job_id, id);

-- ── 3. Scheduled SHIPMENTS — the second level, attached to a LINE ─────────────────────────────
-- A shipping-log row is a LOAD, not a line: one part number ships across many loads, each with
-- its own dates and BOL. Keeping loads here (rather than as extra expected-material lines) is
-- also what keeps the §51 Material List mirror small — the mirror re-projects every active LINE
-- each sync cycle, so 1,233 loads modelled as lines would be 1,233 mirrored rows per job.
CREATE TABLE IF NOT EXISTS material_shipments (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  shipment_uuid TEXT    NOT NULL,                  -- stable key (PR3 import idempotency + any future mirror)
  line_id       INTEGER NOT NULL,                  -- soft-ref job_expected_materials.id
  job_id        TEXT    NOT NULL,                  -- denormalized from the line (same reasons as the events table)
  part_number   TEXT,                              -- what the shipping log said — PROVENANCE of the match
  bol_number    TEXT,                              -- BOL / load number (the sample logs call it 'BOL' and 'LD#')
  carrier       TEXT,
  qty           REAL,
  unit          TEXT,
  ship_date     TEXT,                              -- YYYY-MM-DD
  delivery_date TEXT,                              -- YYYY-MM-DD
  seq           INTEGER NOT NULL DEFAULT 0,        -- display order within a line
  source        TEXT    NOT NULL DEFAULT 'manual'
                CHECK (source IN ('manual','import')),  -- PR3 writes 'import'; vocabulary sized for it NOW
  active        INTEGER NOT NULL DEFAULT 1,        -- soft-delete; history kept (the 0031 posture)
  created_at    INTEGER NOT NULL DEFAULT (unixepoch()),
  created_by    TEXT                               -- ACCOUNT username; display-name-only on read (W9)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_material_shipments_uuid ON material_shipments(shipment_uuid);
CREATE INDEX        IF NOT EXISTS idx_material_shipments_line ON material_shipments(line_id, active, seq);
CREATE INDEX        IF NOT EXISTS idx_material_shipments_job  ON material_shipments(job_id, active);
