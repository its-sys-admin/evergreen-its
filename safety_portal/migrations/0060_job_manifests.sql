-- Materials tracking (PR3b) — the manifest IMPORT pool: the D1 landing area for an
-- office-uploaded BOM / shipping-log document, its parsed grid, and its page previews.
-- The §34 Option-D pattern (po_attachments 0053 → po_estimates 0054) applied to field-ops.
--
-- WHY A POOL: any portal-inbound file is UNTRUSTED (Invariant 2). The Worker only
-- bounds-gates (size / filename / declared-MIME allowlist / magic sniff) and queues the
-- bytes here SEND-FREE; the Mac daemon (field_ops/manifest_poll.py) pulls them over its
-- OWN bearer tier (PORTAL_MANIFEST_API_TOKEN — the ADR-0004 decision-4 posture: the
-- highest-exposure process gets its own token, separate from PO, estimate and RFQ),
-- verifies the manifest:v1 HMAC + sha256, runs the §34 screen, extracts the cell grid in
-- the KILLABLE sandbox child, parses it with field_ops/manifest_parse.py, files the
-- ORIGINAL to Box "<job>/Materials/Manifests/", and posts back the grid + previews.
-- Bytes only ever flow Mac-ward; the browser reads the parsed grid + rendered PNG
-- previews, never the original untrusted bytes.
--
-- MIME ALLOWLIST IS NARROWER THAN THE ESTIMATE LANE — PDF + XLSX only. Every allowed type
-- must be one the parser can actually read; accepting a .docx here would queue a document
-- that can only ever end in `refused`, after it had already been screened and stored.
--
-- LIFECYCLE: pending → claimed (Mac claim-FIRST marker) → (refused | parsed) →
-- (committing → committed | discarded). Chunks are deleted at refusal AND once parsed —
-- D1 holds manifest bytes only while the document is mid-pipeline. The parsed GRID
-- outlives the bytes, because the validate screen edits against it.
--
-- DEDUPE IS PER-JOB, NOT GLOBAL — the one deliberate divergence from 0054. A master BOM
-- legitimately covers sibling jobs (Bradley 1 / Bradley 2 are separate jobs served by one
-- document); a global sha256 index would let the first job to import it lock the second
-- one out with a 409 it could never clear. The partial UNIQUE is therefore on
-- (job_id, sha256) over live rows: an exact-byte replay INTO THE SAME JOB hits the
-- constraint and the upload route maps it to HTTP 409 duplicate_manifest (never a 500),
-- while refused/discarded rows leave the index so a legitimate re-upload after a refusal
-- is accepted.
--
-- manifest_uuid: the insert-then-resolve identity (the att_uuid / est_uuid pattern) —
-- chunk INSERTs resolve manifest_id via a scalar subquery on manifest_uuid, and the
-- manifest:v1 HMAC binds to it so a signed manifest cannot be replayed onto another row.
--
-- committed_through_row: the PAGED-COMMIT WATERMARK. A 900-row master BOM cannot commit
-- inside one Worker request, so /commit lands ~100 source rows per call and advances this
-- watermark IN THE SAME db.batch() as the inserts. A page therefore either fully lands or
-- fully rolls back, and a replayed page is a no-op because the next call resumes strictly
-- above the watermark. status='committing' marks the in-progress window; it is not a lock.
--
-- APPLY BEFORE DEPLOY: run `npx wrangler d1 migrations apply its-safety-portal-db
-- --remote` BEFORE any Worker build that reads/writes these tables deploys — else the
-- manifest routes 500. Same rule as 0010/0033/0043/0053/0054/0059. (Always `git pull`
-- ~/its to latest main FIRST — the stale-migrations-list lockout class, forensic #2.)
-- Depends on 0059 (material_shipments / job_expected_materials.part_number, which the
-- commit route writes); migrations apply in order, so no extra care is needed.

CREATE TABLE IF NOT EXISTS job_manifests (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  manifest_uuid         TEXT    NOT NULL UNIQUE,           -- insert-then-resolve identity (HMAC-bound)
  job_id                TEXT    NOT NULL,                  -- the field-ops job this manifest belongs to
  filename              TEXT    NOT NULL,                  -- sanitized + bounded by the upload route
  declared_mime         TEXT    NOT NULL,                  -- allowlisted at upload (PDF/XLSX ONLY)
  size_bytes            INTEGER NOT NULL,                  -- DECODED byte count, Worker-verified
  sha256                TEXT    NOT NULL,                  -- hex digest of the decoded bytes, Worker-computed
  hmac                  TEXT    NOT NULL,                  -- HMAC-SHA256 hex over the manifest:v1 canonical string
  status                TEXT    NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','claimed','refused','parsed','committing','committed','discarded')),
  detail                TEXT,                              -- refusal / failure machine reason (never bytes)
  profile               TEXT,                              -- manifest_parse profile, set by the Mac result post
  uploaded_by           TEXT    NOT NULL,                  -- the AUTHENTICATED session username
  box_file_id           TEXT,                              -- set by the Mac result post (Box Manifests filing)
  row_count             INTEGER,                           -- parsed rows posted, set by the result post
  column_map_json       TEXT,                              -- ColumnMap: mapping/labels/qty_candidates/qty_default
  header_meta_json      TEXT,                              -- ParsedManifest.meta (CLIENT / PROJECT NAME / …)
  parse_notes           TEXT,                              -- ParsedManifest.notes, newline-joined (evidence, shown)
  mode                  TEXT
    CHECK (mode IN ('merge','add_new')),                   -- chosen by the human at validation, NULL until commit
  merge_options_json    TEXT,                              -- per-row keep/drop/edit + ambiguous resolutions
  committed_through_row INTEGER NOT NULL DEFAULT 0,        -- paged-commit watermark (see header)
  created_at            INTEGER NOT NULL DEFAULT (unixepoch()),
  screened_at           INTEGER,                           -- stamped by the Mac result post (§34 screen passed)
  parsed_at             INTEGER,                           -- stamped by the Mac result post
  committed_at          INTEGER                            -- stamped when the last page commits
);
-- The Mac pending scan reads oldest-first.
CREATE INDEX IF NOT EXISTS idx_job_manifests_status ON job_manifests(status, created_at);
-- The per-job browser list reads newest-first.
CREATE INDEX IF NOT EXISTS idx_job_manifests_job ON job_manifests(job_id, created_at);
-- THE dedupe authority — PER-JOB (see header): live rows only, so a refused/discarded
-- byte-twin may re-enter, and a master BOM may serve sibling jobs.
CREATE UNIQUE INDEX IF NOT EXISTS idx_job_manifests_sha_live ON job_manifests(job_id, sha256)
  WHERE status NOT IN ('refused','discarded');

CREATE TABLE IF NOT EXISTS job_manifest_chunks (
  manifest_id INTEGER NOT NULL,                            -- FK-ish → job_manifests.id
  chunk_index INTEGER NOT NULL,
  chunk_total INTEGER NOT NULL,
  chunk_b64   TEXT    NOT NULL,                            -- ≤ 1MB decoded per chunk (mirrors 0053/0054)
  PRIMARY KEY (manifest_id, chunk_index)
);

-- The parsed GRID awaiting validation. Verbatim source cells — this is what the human
-- edits against, so it is stored exactly as manifest_parse classified it and never
-- pre-collapsed. Duplicate part numbers are universal in the real BOMs (7000153 appears
-- twice in three of the four sample Customer BOMs, under different groupings), so merging
-- here would silently pick a winner; that is the validate page's per-row decision.
CREATE TABLE IF NOT EXISTS job_manifest_rows (
  manifest_id INTEGER NOT NULL,                            -- FK-ish → job_manifests.id
  row_index   INTEGER NOT NULL,                            -- ParsedRow.index — 1-based, whole-document, STABLE
  source_page TEXT,                                        -- ParsedRow.source ('xlsx:Export' / 'pdf:p2:t0')
  kind        TEXT    NOT NULL
    CHECK (kind IN ('header','data','continuation','section','meta')),
  cells_json  TEXT    NOT NULL,                            -- JSON array of the row's cells, verbatim
  flags       TEXT,                                        -- comma-joined ParsedRow.flags ('' when clean)
  UNIQUE (manifest_id, row_index)
);
CREATE INDEX IF NOT EXISTS idx_job_manifest_rows_manifest
  ON job_manifest_rows(manifest_id, row_index);

-- Rendered page previews (Quartz / xlsx render, subprocess-isolated on the Mac) so the
-- validate screen shows the SOURCE beside the editable grid without ever serving the
-- original untrusted bytes to a browser. ≤ 1MB decoded per page, Worker-bounded.
CREATE TABLE IF NOT EXISTS job_manifest_previews (
  manifest_id INTEGER NOT NULL,                            -- FK-ish → job_manifests.id
  page        INTEGER NOT NULL,                            -- 1-based
  png_b64     TEXT    NOT NULL,                            -- ≤ 1MB decoded, Worker-bounded
  PRIMARY KEY (manifest_id, page)
);
