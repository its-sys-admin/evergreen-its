-- Job-schedule import (ADR-0006) — the schedule IMPORT pool: the D1 landing area for an
-- office-uploaded PROJECT SCHEDULE PDF (a Smartsheet Gantt export), its OCR-proposed grid,
-- and its rendered page previews. The §34 Option-D pattern (po_attachments 0053 →
-- po_estimates 0054 → job_manifests 0060) applied to the schedule lane.
--
-- WHY A POOL: any portal-inbound file is UNTRUSTED (Invariant 2). The Worker only
-- bounds-gates (size / filename / declared-MIME allowlist / magic sniff) and queues the
-- bytes here SEND-FREE; the Mac daemon (field_ops/schedule_poll.py, PR-3 of the lane)
-- pulls them over its OWN bearer tier (PORTAL_SCHEDULE_API_TOKEN — the ADR-0004
-- decision-4 posture: a process that decodes hostile bytes gets its own token), verifies
-- the schedule:v1 HMAC + sha256, runs the §34 screen, renders + OCRs the pages in the
-- KILLABLE sandbox child (Apple Vision — the corpus PDFs carry NO text layer), parses the
-- geometry into a task grid, files the ORIGINAL to Box "<job>/Schedules/", and posts back
-- the grid + previews. Bytes only ever flow Mac-ward; the browser reads the proposed grid
-- + rendered PNG previews, never the original untrusted bytes.
--
-- MIME ALLOWLIST IS PDF ONLY — narrower again than the manifest lane (PDF + XLSX). Every
-- allowed type must be one the parser can actually read (the 0060 rule), and the schedule
-- parser reads Smartsheet Gantt PDF exports; accepting an .xlsx here would queue a
-- document that can only ever end in `refused`, after it had already been screened and
-- stored. Widening to xlsx is a deliberate later decision, not a default.
--
-- LIFECYCLE: pending → claimed (Mac claim-FIRST marker) → (refused | parsed) →
-- (committing → committed | superseded | discarded). Two lane-specific divergences from
-- 0060, both load-bearing:
--
--   • `superseded` — REVISIONS ARE EXPECTED here in a way no manifest ever is: the
--     schedule corpus shows one job re-exported up to 14 times as the PM updates dates
--     and % complete. When revision N's commit lands, revision N−1 flips
--     committed → superseded IN THE SAME final-page batch (supersede-FIRST, so the
--     one-committed partial UNIQUE below never sees two committed rows). `committed`
--     therefore always means "the currently governing schedule", and a superseded row
--     leaves the dedupe index so re-uploading the PREVIOUS revision's exact bytes is
--     legal — that re-upload IS the rollback path for a wrong-file commit.
--
--   • idx_job_schedules_one_committed — ONE governing schedule per job, enforced by the
--     machine rather than by route discipline. The commit route's supersede-first batch
--     ordering exists to satisfy this index.
--
-- DEDUPE IS PER-JOB, EXACT-SHA, over rows still in play (pending/claimed/parsed/
-- committing/committed). A revision is by definition a DIFFERENT PDF, so it never
-- collides; only a byte-identical replay of a live document 409s
-- (duplicate_schedule, never a 500), while refused / discarded / superseded rows leave
-- the index so refusal-retry and rollback re-uploads are accepted. The schedule:v1
-- signature binds job_id, so the per-job widening cannot become a cross-job replay.
--
-- schedule_uuid: the insert-then-resolve identity (the manifest_uuid / est_uuid pattern)
-- — chunk INSERTs resolve schedule_id via a scalar subquery on schedule_uuid, and the
-- schedule:v1 HMAC binds to it so a signed schedule cannot be replayed onto another row.
--
-- committed_through_row: the PAGED-COMMIT WATERMARK (0060 protocol, kept even though a
-- typical schedule is ~60 tasks — uniform replay semantics, and a 300-row utility-scale
-- schedule won't break the lane). resolutions_json is the reconcile ledger (the
-- merge_options_json slot): the human's per-row link / remove / percent-conflict
-- decisions, merged across pages.
--
-- Grid rows + previews are RETAINED after commit (unlike chunks, which die at result):
-- they are the evidence pane the NEXT revision's reconcile screen shows. Discard drops
-- everything.
--
-- APPLY BEFORE DEPLOY: run `npx wrangler d1 migrations apply its-safety-portal-db
-- --remote` BEFORE any Worker build that reads/writes these tables deploys — else the
-- schedule routes 500. Same rule as 0010/0033/0043/0053/0054/0059/0060. (Always
-- `git pull` ~/its to latest main FIRST — the stale-migrations-list lockout class,
-- forensic #2.)

CREATE TABLE IF NOT EXISTS job_schedules (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  schedule_uuid         TEXT    NOT NULL UNIQUE,           -- insert-then-resolve identity (HMAC-bound)
  job_id                TEXT    NOT NULL,                  -- the field-ops job this schedule belongs to
  filename              TEXT    NOT NULL,                  -- sanitized + bounded by the upload route
  declared_mime         TEXT    NOT NULL,                  -- allowlisted at upload (PDF ONLY)
  size_bytes            INTEGER NOT NULL,                  -- DECODED byte count, Worker-verified
  sha256                TEXT    NOT NULL,                  -- hex digest of the decoded bytes, Worker-computed
  hmac                  TEXT    NOT NULL,                  -- HMAC-SHA256 hex over the schedule:v1 canonical string
  status                TEXT    NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','claimed','refused','parsed',
                      'committing','committed','superseded','discarded')),
  detail                TEXT,                              -- refusal / failure machine reason (never bytes)
  profile               TEXT,                              -- schedule_parse profile (gantt_export / grid_export / …)
  uploaded_by           TEXT    NOT NULL,                  -- the AUTHENTICATED session username
  box_file_id           TEXT,                              -- set by the Mac result post (Box Schedules filing)
  row_count             INTEGER,                           -- proposed rows posted, set by the result post
  column_map_json       TEXT,                              -- proposed concept→column mapping (task/start/finish/…)
  header_meta_json      TEXT,                              -- document meta (title line / export stamp / …)
  parse_notes           TEXT,                              -- parser evidence + consistency flags, newline-joined
  resolutions_json      TEXT,                              -- the human's reconcile decisions, merged across pages
  committed_through_row INTEGER NOT NULL DEFAULT 0,        -- paged-commit watermark (see header)
  created_at            INTEGER NOT NULL DEFAULT (unixepoch()),
  screened_at           INTEGER,                           -- stamped by the Mac result post (§34 screen passed)
  parsed_at             INTEGER,                           -- stamped by the Mac result post
  committed_at          INTEGER,                           -- stamped when the last page commits
  superseded_at         INTEGER                            -- stamped when a later revision's commit displaced this row
);
-- The Mac pending scan reads oldest-first.
CREATE INDEX IF NOT EXISTS idx_job_schedules_status ON job_schedules(status, created_at);
-- The per-job browser list reads newest-first.
CREATE INDEX IF NOT EXISTS idx_job_schedules_job ON job_schedules(job_id, created_at);
-- THE dedupe authority — PER-JOB, exact sha, rows still in play (see header): a
-- refused / discarded / SUPERSEDED byte-twin may re-enter (refusal-retry + rollback).
CREATE UNIQUE INDEX IF NOT EXISTS idx_job_schedules_sha_live ON job_schedules(job_id, sha256)
  WHERE status NOT IN ('refused','discarded','superseded');
-- ONE governing schedule per job, machine-enforced (see header): the final commit page
-- flips the old committed row to superseded BEFORE this index sees the new one.
CREATE UNIQUE INDEX IF NOT EXISTS idx_job_schedules_one_committed ON job_schedules(job_id)
  WHERE status = 'committed';

CREATE TABLE IF NOT EXISTS job_schedule_chunks (
  schedule_id INTEGER NOT NULL,                            -- FK-ish → job_schedules.id
  chunk_index INTEGER NOT NULL,
  chunk_total INTEGER NOT NULL,
  chunk_b64   TEXT    NOT NULL,                            -- ≤ 1MB decoded per chunk (0053/0054/0060 shape)
  PRIMARY KEY (schedule_id, chunk_index)
);

-- The OCR-proposed task grid awaiting validation. Verbatim proposed cells — this is what
-- the human edits against (OCR digit misreads like 12/01/25 → 72/01/25 are REAL in the
-- corpus, at confidence 1.0, so confidence is not a filter and the human validate is the
-- only fidelity control). Nothing is pre-collapsed; a duplicated task name within a
-- section survives to become a first-class ambiguous outcome at reconcile.
CREATE TABLE IF NOT EXISTS job_schedule_rows (
  schedule_id INTEGER NOT NULL,                            -- FK-ish → job_schedules.id
  row_index   INTEGER NOT NULL,                            -- 1-based, whole-document, STABLE
  source_page TEXT,                                        -- provenance label ('pdf:p1' / 'pdf:p2')
  kind        TEXT    NOT NULL
    CHECK (kind IN ('header','data','continuation','section','meta')),
  cells_json  TEXT    NOT NULL,                            -- JSON array of the row's proposed cells, verbatim
  flags       TEXT,                                        -- comma-joined consistency flags ('' when clean)
  UNIQUE (schedule_id, row_index)
);
CREATE INDEX IF NOT EXISTS idx_job_schedule_rows_schedule
  ON job_schedule_rows(schedule_id, row_index);

-- Rendered page previews (Quartz, subprocess-isolated on the Mac) so the validate and
-- reconcile screens show the SOURCE Gantt page beside the editable grid without ever
-- serving the original untrusted bytes to a browser. ≤ 1MB decoded per page,
-- Worker-bounded. Retained after commit — the next revision's diff evidence.
CREATE TABLE IF NOT EXISTS job_schedule_previews (
  schedule_id INTEGER NOT NULL,                            -- FK-ish → job_schedules.id
  page        INTEGER NOT NULL,                            -- 1-based
  png_b64     TEXT    NOT NULL,                            -- ≤ 1MB decoded, Worker-bounded
  PRIMARY KEY (schedule_id, page)
);
